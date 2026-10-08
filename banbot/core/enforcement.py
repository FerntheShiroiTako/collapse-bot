"""The ONLY place that calls the Discord ban API, and only ever for a case a mod approved.

Guarantees:
- every ban attempt (real or dry-run) writes an audit row first;
- dry-run never touches the ban API;
- a failed ban is recorded and the member is NOT marked banned;
- an approved ban is always sent to Discord, even if Collapse banned this member before: they may have
  been unbanned and come back, and Discord accepts a ban on someone who is already banned.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum

from banbot.settings.messages import render_ban_dm
from banbot import brand
from banbot.core.flags import RAYWARD_SOURCES, source_label
from banbot.core.gateway import BanError, Gateway
from banbot.storage.store import Store
from banbot.util import Clock

log = logging.getLogger(__name__)

DECISION_MOD_APPROVED = "mod_approved"


class BanOutcome(str, Enum):
    BANNED = "banned"
    WOULD_BAN = "would_ban"  # dry-run
    FAILED = "ban_failed"


@dataclass(frozen=True)
class BanRequest:
    discord_id: int
    roblox_id: int | None
    roblox_username: str | None
    nickname_at_ban: str | None
    provider: str
    status_name: str
    raw_response_json: str
    decision_path: str
    approved_by: int | None = None


@dataclass(frozen=True)
class BanResult:
    outcome: BanOutcome
    audit_id: int | None
    error: str | None = None


class Banner:
    def __init__(
        self, *, guild_id: int, store: Store, gateway: Gateway, clock: Clock, dry_run: bool, ban_delay_s: float = 0.0,
        dm_templates: dict[str, str] | None = None, appeal_url: str | None = None,
    ):
        self._guild_id = guild_id
        self._store = store
        self._gateway = gateway
        self._clock = clock
        self.dry_run = dry_run
        self._ban_delay_s = ban_delay_s  # gentle pacing between real bans
        self._dm_templates = dm_templates  # ban source -> DM text; None = do not message banned members
        self._appeal_url = appeal_url
        # Serializes concurrent ban() calls for the same member (e.g. two mods pressing Ban on the same
        # member's cases at once), so their audit rows and bans don't interleave.
        self._locks: dict[int, asyncio.Lock] = {}

    async def ban(self, req: BanRequest) -> BanResult:
        lock = self._locks.setdefault(req.discord_id, asyncio.Lock())
        async with lock:
            return await self._ban_locked(req)

    async def _ban_locked(self, req: BanRequest) -> BanResult:
        now = self._clock.now()
        audit_id = self._store.write_audit(
            self._guild_id,
            discord_id=req.discord_id,
            roblox_id=req.roblox_id,
            roblox_username=req.roblox_username,
            nickname_at_ban=req.nickname_at_ban,
            provider=req.provider,
            status_name=req.status_name,
            raw_response_json=req.raw_response_json,
            decision_path=req.decision_path,
            approved_by=req.approved_by,
            dry_run=self.dry_run,
            at=now,
        )

        if self.dry_run:
            log.warning(
                "DRY RUN - would ban discord=%s roblox=%s (%s) nickname=%r path=%s approved_by=%s audit=%s",
                req.discord_id, req.roblox_id, req.roblox_username, req.nickname_at_ban,
                req.decision_path, req.approved_by, audit_id,
            )
            return BanResult(BanOutcome.WOULD_BAN, audit_id)

        # The DM has to go out BEFORE the ban: once banned, the member no longer shares a server with the
        # bot and Discord will refuse to deliver it. A failed DM never blocks the ban.
        await self._notify(req, audit_id)

        reason = f"[{brand.NAME}] {source_label(req.provider)}:{req.status_name} | roblox {req.roblox_username} ({req.roblox_id}) | {req.decision_path}"
        if req.approved_by:
            reason += f" by {req.approved_by}"
        try:
            await self._gateway.ban(req.discord_id, reason=reason[:512])
        except BanError as e:
            log.error("BAN FAILED discord=%s: %s (audit=%s)", req.discord_id, e, audit_id)
            self._store.set_audit_result(audit_id, succeeded=False, error=str(e))
            return BanResult(BanOutcome.FAILED, audit_id, str(e))
        except Exception as e:  # anything unexpected is still a failure, never a silent success
            log.exception("BAN FAILED (unexpected) discord=%s (audit=%s)", req.discord_id, audit_id)
            self._store.set_audit_result(audit_id, succeeded=False, error=f"{type(e).__name__}: {e}")
            return BanResult(BanOutcome.FAILED, audit_id, str(e))

        self._store.set_audit_result(audit_id, succeeded=True, error=None)
        self._store.mark_banned(self._guild_id, req.discord_id, req.roblox_id, audit_id, self._clock.now())
        if self._ban_delay_s > 0:
            await self._clock.sleep(self._ban_delay_s)
        log.warning(
            "BANNED discord=%s roblox=%s (%s) path=%s approved_by=%s audit=%s",
            req.discord_id, req.roblox_id, req.roblox_username, req.decision_path, req.approved_by, audit_id,
        )
        return BanResult(BanOutcome.BANNED, audit_id)

    async def _notify(self, req: BanRequest, audit_id: int) -> None:
        if not self._dm_templates:
            return
        # The DM must name the source this ban is based on: Rotector and ban evasion have their own text,
        # every other Rayward source shares one that names it. Never fall back to another source's text.
        template = self._dm_templates.get(req.provider)
        if template is None and req.provider in RAYWARD_SOURCES:
            template = self._dm_templates.get("other")
        if not template:
            log.warning("no ban DM template for source %r; not sending a DM", req.provider)
            return
        source = RAYWARD_SOURCES.get(req.provider)
        text = render_ban_dm(
            template,
            server=self._gateway.guild_name(),
            roblox_username=req.roblox_username,
            roblox_id=req.roblox_id,
            status=req.status_name,
            source=source.full_name if source else source_label(req.provider),
            source_appeal=source.appeal if source else "",
            appeal_url=self._appeal_url,
        )
        try:
            sent = await self._gateway.send_dm(req.discord_id, text)
            self._store.set_audit_dm(audit_id, sent=sent, error=None if sent else "not delivered")
        except Exception as e:  # never let a DM problem stop the ban
            log.warning("ban DM to %s raised: %s", req.discord_id, e)
            self._store.set_audit_dm(audit_id, sent=False, error=f"{type(e).__name__}: {e}")
