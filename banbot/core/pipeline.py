"""Per-member pipeline. Used by both the monthly sweep and member-join events.

1. Identify the member's Roblox account: Bloxlink first, nickname `(@username)` as fallback (identity.py).
2. Look the account up with the flag provider.
3. Route the outcome: review queue / report / log / retry.

Hard rules enforced here:
- Inconclusive is never clean: every error/timeout/unexpected response lands in the inconclusive bucket.
- The pipeline never bans. Every flagged account goes to a mod; a ban only happens when a mod presses
  Ban on the case (review.py -> enforcement.py).
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta
from enum import Enum
from typing import Sequence

from banbot.settings.config import Config
from banbot.core.flags import FlagOutcome, FlagProvider, FlagResult
from banbot.core.gateway import Gateway, MemberInfo
from banbot.core.identity import STAGE_BLOXLINK_QUOTA, Identified, IdentityResolver, Unidentified
from banbot.core.review import (
    REASON_BAN_EVASION,
    REASON_CONFIRMED,
    REASON_INCONCLUSIVE_EXHAUSTED,
    REASON_STATUS,
    ReviewCase,
    ReviewQueue,
)
from banbot.storage.store import Store
from banbot.util import Clock

log = logging.getLogger(__name__)


class Bucket(str, Enum):
    UNRESOLVED = "unresolved"
    INCONCLUSIVE = "inconclusive"
    CLEAR = "clear"
    PAST_OFFENDER = "past_offender"
    REVIEW = "review"
    REPORTED = "reported"  # report-only: posted as a notice, no buttons


STAGE_FLAG = "flag"


class Pipeline:
    def __init__(
        self,
        *,
        cfg: Config,
        store: Store,
        gateway: Gateway,
        identity: IdentityResolver,
        provider: FlagProvider,
        review_queue: ReviewQueue,
        clock: Clock,
    ):
        self._cfg = cfg
        self._store = store
        self._gateway = gateway
        self._identity = identity
        self._provider = provider
        self._reviews = review_queue
        self._clock = clock

    @property
    def bloxlink_budget(self):
        return self._identity.bloxlink_budget

    # ------------------------------------------------------------------ entry point
    async def process(self, members: Sequence[MemberInfo], *, sweep_id: int | None = None) -> Counter[str]:
        counts: Counter[str] = Counter()

        # Step 1: identify each member's Roblox account (Bloxlink, then nickname).
        # Sweeps may not spend the Bloxlink reserve; join checks (sweep_id None) may.
        identified: list[Identified] = []
        for result in await self._identity.identify(members, use_reserve=sweep_id is None):
            if isinstance(result, Identified):
                identified.append(result)
            elif isinstance(result, Unidentified):
                log.info("unresolved: discord=%s nickname=%r (%s)",
                         result.member.id, result.member.nickname, result.detail)
                self._store.clear_inconclusive(self._cfg.guild_id, result.member.id)  # a definite answer; nothing to retry
                self._record(sweep_id, result.member, Bucket.UNRESOLVED, detail=result.detail)
                counts[Bucket.UNRESOLVED.value] += 1
            elif result.stage == STAGE_BLOXLINK_QUOTA:
                # Out of daily quota is not a failed check: park until the UTC reset without using an attempt.
                budget = self._identity.bloxlink_budget
                retry_at = budget.resets_at() + timedelta(seconds=60) if budget else None
                await self.mark_inconclusive(
                    result.member, sweep_id, stage=result.stage,
                    username=result.username, roblox_id=result.roblox_id, detail=result.detail,
                    retry_at=retry_at, count_attempt=False,
                )
                counts[Bucket.INCONCLUSIVE.value] += 1
            else:
                await self.mark_inconclusive(
                    result.member, sweep_id, stage=result.stage,
                    username=result.username, roblox_id=result.roblox_id, detail=result.detail,
                )
                counts[Bucket.INCONCLUSIVE.value] += 1
        if not identified:
            return counts

        # Step 1.5: ban evasion - this exact Roblox account was already banned here under a DIFFERENT
        # Discord account. Skip the flag lookup entirely; we already know enough to act.
        still_to_check: list[Identified] = []
        for ident in identified:
            prior = self._store.prior_ban_for_roblox_id(
                self._cfg.guild_id, ident.user.id, exclude_discord_id=ident.member.id
            )
            if prior is None:
                still_to_check.append(ident)
                continue
            bucket = await self._route_ban_evasion(ident, prior.discord_id, sweep_id)
            counts[bucket.value] += 1
        identified = still_to_check
        if not identified:
            return counts

        # Step 2: flag lookup (batched)
        flags = await self._safe_lookup([i.user.id for i in identified])
        for ident in identified:
            fr = flags.get(ident.user.id) or FlagResult.inconclusive(
                self._provider.name, "provider returned no entry for this id"
            )
            bucket = await self._route(ident, fr, sweep_id)
            counts[bucket.value] += 1
        return counts

    async def _route_ban_evasion(self, ident: Identified, prior_discord_id: int, sweep_id: int | None) -> Bucket:
        """Routed like a Rotector Confirmed hit: straight to a mod (or posted as a notice in report-only)."""
        log.warning("ban evasion: discord=%s roblox=%s previously banned here as discord=%s",
                    ident.member.id, ident.user.id, prior_discord_id)
        fr = FlagResult(
            FlagOutcome.CONFIRMED, "banbot", None, "Ban Evasion", None,
            f"Collapse already banned this Roblox account in this server (as <@{prior_discord_id}>, "
            f"discord id {prior_discord_id}).",
        )
        if self._cfg.report_only:
            row, created = await self._report(ident, fr, reason=REASON_BAN_EVASION)
            self._record(sweep_id, ident.member, Bucket.REPORTED, username=ident.username, roblox_id=ident.user.id,
                         detail=f"{fr.status_name}; report #{row.id}" + ("" if created else " (already posted)"))
            return Bucket.REPORTED
        return await self._queue(ident, fr, sweep_id, reason=REASON_BAN_EVASION)

    # ------------------------------------------------------------------ steps
    async def _safe_lookup(self, ids: list[int]) -> dict[int, FlagResult]:
        try:
            return await self._provider.lookup(ids)
        except Exception:
            log.exception("flag provider raised; treating %d ids as inconclusive", len(ids))
            return {}

    async def _route(self, ident: Identified, fr: FlagResult, sweep_id: int | None) -> Bucket:
        m, user, username = ident.member, ident.user, ident.username
        o = fr.outcome
        if o is FlagOutcome.UNMAPPED:
            log.error("UNMAPPED flag status for discord=%s roblox=%s: %s -> inconclusive", m.id, user.id, fr.status_name)
            await self.mark_inconclusive(m, sweep_id, stage=STAGE_FLAG, username=username, roblox_id=user.id,
                                         detail=f"unmapped provider status {fr.status_name}")
            return Bucket.INCONCLUSIVE
        if o is FlagOutcome.INCONCLUSIVE:
            await self.mark_inconclusive(m, sweep_id, stage=STAGE_FLAG, username=username, roblox_id=user.id,
                                         detail=fr.detail)
            return Bucket.INCONCLUSIVE

        # From here on we have a definitive answer; the member is no longer inconclusive.
        self._store.clear_inconclusive(self._cfg.guild_id, m.id)

        if o is FlagOutcome.CLEAR:
            log.info("clear: discord=%s roblox=%s (%s via %s)", m.id, user.id, username, ident.source.value)
            self._record(sweep_id, m, Bucket.CLEAR, username=username, roblox_id=user.id, detail=fr.status_name)
            return Bucket.CLEAR
        if o is FlagOutcome.PAST_OFFENDER:
            log.warning("past offender (allowed): discord=%s roblox=%s (%s via %s)",
                        m.id, user.id, username, ident.source.value)
            if self._cfg.report_only:
                await self._report(ident, fr, reason=f"{REASON_STATUS}:{fr.status_name}")
            self._record(sweep_id, m, Bucket.PAST_OFFENDER, username=username, roblox_id=user.id, detail=fr.status_name)
            return Bucket.PAST_OFFENDER
        if o in (FlagOutcome.REVIEW, FlagOutcome.CONFIRMED) and self._cfg.report_only:
            row, created = await self._report(ident, fr, reason=f"{REASON_STATUS}:{fr.status_name}")
            self._record(sweep_id, m, Bucket.REPORTED, username=username, roblox_id=user.id,
                         detail=f"{fr.status_name}; report #{row.id}" + ("" if created else " (already posted)"))
            return Bucket.REPORTED
        if o is FlagOutcome.REVIEW:
            return await self._queue(ident, fr, sweep_id, reason=f"{REASON_STATUS}:{fr.status_name}")
        if o is FlagOutcome.CONFIRMED:
            return await self._queue(ident, fr, sweep_id, reason=REASON_CONFIRMED)

        # Should be unreachable; never fall through to "clean".
        log.error("unhandled FlagOutcome %r for discord=%s -> inconclusive", o, m.id)
        await self.mark_inconclusive(m, sweep_id, stage=STAGE_FLAG, username=username, roblox_id=user.id,
                                     detail=f"unhandled outcome {o!r}")
        return Bucket.INCONCLUSIVE

    def _case(self, ident: Identified, fr: FlagResult, reason: str) -> ReviewCase:
        return ReviewCase(
            discord_id=ident.member.id,
            roblox_id=ident.user.id,
            roblox_username=ident.user.name,
            nickname=ident.member.nickname,
            flag=fr,
            reason=reason,
            identity_source=ident.source.value,
        )

    async def _report(self, ident: Identified, fr: FlagResult, *, reason: str):
        return await self._reviews.report(self._case(ident, fr, reason))

    async def _queue(self, ident: Identified, fr: FlagResult, sweep_id: int | None, *, reason: str) -> Bucket:
        row, created = await self._reviews.enqueue(self._case(ident, fr, reason))
        detail = f"{reason}; review #{row.id}" + ("" if created else " (already pending)")
        self._record(sweep_id, ident.member, Bucket.REVIEW, username=ident.username, roblox_id=ident.user.id,
                     detail=detail)
        return Bucket.REVIEW

    # ------------------------------------------------------------------ inconclusive bucket
    async def mark_inconclusive(
        self, m: MemberInfo, sweep_id: int | None, *, stage: str, username: str | None, roblox_id: int | None,
        detail: str, retry_at: datetime | None = None, count_attempt: bool = True,
    ) -> None:
        """`retry_at` overrides the exponential backoff; `count_attempt=False` parks the member without
        moving them closer to exhaustion (used when *we* ran out of quota, which is not their fault)."""
        now = self._clock.now()
        prev = self._store.get_inconclusive(self._cfg.guild_id, m.id)
        attempts = (prev.attempts if prev else 0) + (1 if count_attempt else 0)
        max_attempts = 1 + self._cfg.retry.max_retries
        log.warning("inconclusive (%s, attempt %d/%d): discord=%s username=%r roblox=%s: %s",
                    stage, attempts, max_attempts, m.id, username, roblox_id, detail)

        if not count_attempt:
            when = retry_at or (now + timedelta(seconds=self._cfg.retry.delay_for(max(attempts, 1))))
            self._store.upsert_inconclusive(
                self._cfg.guild_id,
                discord_id=m.id, sweep_id=sweep_id if sweep_id is not None else (prev.sweep_id if prev else None),
                roblox_username=username, roblox_id=roblox_id, stage=stage, last_error=detail, attempts=attempts,
                now=now, next_retry_at=when, exhausted=False,
            )
            self._record(sweep_id, m, Bucket.INCONCLUSIVE, username=username, roblox_id=roblox_id,
                         detail=f"{stage}: {detail} (parked until {when:%Y-%m-%d %H:%M} UTC)")
            return

        if attempts >= max_attempts:
            # Exhausted: still never clean. Escalate to the review queue (decided with Fern) and keep the row.
            log.error("inconclusive EXHAUSTED after %d attempts: discord=%s username=%r -> review queue", attempts, m.id, username)
            fr = FlagResult.inconclusive(self._provider.name, f"checks failed {attempts} times at stage '{stage}': {detail}")
            case = ReviewCase(
                discord_id=m.id, roblox_id=roblox_id, roblox_username=username or "?", nickname=m.nickname,
                flag=fr, reason=REASON_INCONCLUSIVE_EXHAUSTED,
            )
            row, _ = await (self._reviews.report(case) if self._cfg.report_only else self._reviews.enqueue(case))
            self._store.upsert_inconclusive(
                self._cfg.guild_id,
                discord_id=m.id, sweep_id=sweep_id if sweep_id is not None else (prev.sweep_id if prev else None),
                roblox_username=username, roblox_id=roblox_id, stage=stage, last_error=detail, attempts=attempts,
                now=now, next_retry_at=None, exhausted=True, review_id=row.id,
            )
            self._record(sweep_id, m, Bucket.INCONCLUSIVE, username=username, roblox_id=roblox_id,
                         detail=f"exhausted after {attempts} attempts; review #{row.id}")
            return

        delay = self._cfg.retry.delay_for(attempts)
        when = retry_at or (now + timedelta(seconds=delay))
        self._store.upsert_inconclusive(
            self._cfg.guild_id,
            discord_id=m.id, sweep_id=sweep_id if sweep_id is not None else (prev.sweep_id if prev else None),
            roblox_username=username, roblox_id=roblox_id, stage=stage, last_error=detail, attempts=attempts,
            now=now, next_retry_at=when, exhausted=False,
        )
        self._record(sweep_id, m, Bucket.INCONCLUSIVE, username=username, roblox_id=roblox_id,
                     detail=f"{stage}: {detail} (attempt {attempts}, retry in {int(delay)}s)")

    def _record(
        self, sweep_id: int | None, m: MemberInfo, bucket: Bucket, *,
        username: str | None = None, roblox_id: int | None = None, detail: str | None = None,
    ) -> None:
        if sweep_id is None:
            return
        self._store.record_sweep_result(
            sweep_id, m.id, bucket.value, roblox_username=username, roblox_id=roblox_id, detail=detail, at=self._clock.now()
        )
