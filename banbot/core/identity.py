"""Step 1+2: work out which Roblox account a Discord member belongs to.

Two sources, in priority order:
  1. Bloxlink  - the member verified their account, so this is authoritative. Gives a Roblox id;
                 the username is then fetched in bulk from Roblox for display.
  2. Nickname  - `display (@username)`. Gives a username, resolved to an id in bulk by Roblox.

Bloxlink saying "this member never verified" is a definite answer, so we fall through to the
nickname. Bloxlink *failing* (network, bad key, rate limit, unreadable body) is not an answer, so
it becomes Inconclusive and is retried - a member must never be skipped because a lookup broke.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from banbot.integrations.bloxlink import BloxlinkClient, BloxlinkFailure, BloxlinkLink, BloxlinkNoLink
from banbot.core.budget import DailyBudget
from banbot.core.gateway import Gateway, MemberInfo
from banbot.core.nickname import parse_nickname
from banbot.integrations.roblox import ResolveFailure, RobloxResolver, RobloxUser

log = logging.getLogger(__name__)


class IdentitySource(str, Enum):
    BLOXLINK = "bloxlink"
    NICKNAME = "nickname"


@dataclass(frozen=True)
class Identified:
    member: MemberInfo
    user: RobloxUser
    source: IdentitySource
    username: str  # what we matched on: the Bloxlink account's name, or the nickname's (@username)


@dataclass(frozen=True)
class Unidentified:
    """A definite "no Roblox account we can see": no Bloxlink link and no usable nickname."""

    member: MemberInfo
    detail: str


@dataclass(frozen=True)
class Unverifiable:
    """A lookup broke. Never treat as "no account" - this goes to the inconclusive bucket."""

    member: MemberInfo
    stage: str  # "bloxlink" | "resolve"
    username: str | None
    roblox_id: int | None
    detail: str


IdentityResult = Identified | Unidentified | Unverifiable

STAGE_BLOXLINK = "bloxlink"
STAGE_BLOXLINK_QUOTA = "bloxlink_quota"  # daily budget spent; retry after the UTC reset, not on backoff
STAGE_RESOLVE = "resolve"


class IdentityResolver:
    def __init__(
        self,
        *,
        resolver: RobloxResolver | None,  # None: Roblox API switched off (ROBLOX_API_ENABLED=false)
        bloxlink: BloxlinkClient | None,
        gateway: Gateway,
        bloxlink_budget: DailyBudget | None = None,
    ):
        self._resolver = resolver
        self._bloxlink = bloxlink
        self._gateway = gateway
        self._budget = bloxlink_budget

    @property
    def bloxlink_budget(self) -> DailyBudget | None:
        return self._budget if self._bloxlink is not None else None

    async def identify(self, members: Sequence[MemberInfo], *, use_reserve: bool = True) -> list[IdentityResult]:
        """`use_reserve=False` for sweeps: they stop at the reserve so join checks keep working all day."""
        out: list[IdentityResult] = []
        by_id: dict[int, int] = {}  # roblox id -> index in `out`, awaiting a username
        pending_names: dict[str, list[int]] = {}  # lowercased username -> indices in `out`

        for m in members:
            idx = len(out)
            link = await self._try_bloxlink(m, use_reserve=use_reserve)

            if isinstance(link, BloxlinkFailure):
                stage = STAGE_BLOXLINK_QUOTA if link.kind == "quota" else STAGE_BLOXLINK
                out.append(Unverifiable(m, stage, None, None, link.detail))
                continue

            if isinstance(link, BloxlinkLink) and self._resolver is None:
                # No Roblox API to fetch a username with; the id is all the flag lookup needs.
                rid = link.roblox_id
                user = RobloxUser(id=rid, name=str(rid), display_name=str(rid), requested_username="")
                out.append(Identified(m, user, IdentitySource.BLOXLINK, str(rid)))
                continue

            if isinstance(link, BloxlinkLink):
                # Placeholder; the username arrives from the bulk id lookup below.
                out.append(Unverifiable(m, STAGE_RESOLVE, None, link.roblox_id, "awaiting username lookup"))
                by_id[link.roblox_id] = idx
                continue

            # No Bloxlink link (or Bloxlink disabled): fall through to the nickname.
            parsed = parse_nickname(m.nickname)
            if not parsed.ok:
                why = parsed.failure.value  # type: ignore[union-attr]
                detail = f"no Bloxlink link and nickname {why}" if isinstance(link, BloxlinkNoLink) else why
                out.append(Unidentified(m, detail))
                continue
            username = parsed.username  # type: ignore[assignment]
            if self._resolver is None:
                out.append(Unidentified(m, "Roblox API is switched off, so nickname tags can't be looked up"))
                continue
            out.append(Unverifiable(m, STAGE_RESOLVE, username, None, "awaiting username resolution"))
            pending_names.setdefault(username.lower(), []).append(idx)

        if by_id:
            await self._fill_from_ids(out, by_id)
        if pending_names:
            await self._fill_from_names(out, pending_names)
        return out

    # ------------------------------------------------------------------ sources
    async def _try_bloxlink(self, m: MemberInfo, *, use_reserve: bool):
        if self._bloxlink is None:
            return BloxlinkNoLink(m.id, "bloxlink disabled")
        if self._budget is not None:
            if not self._budget.can_spend(1, use_reserve=use_reserve):
                return BloxlinkFailure(
                    m.id,
                    f"Bloxlink daily budget spent ({self._budget.describe()})"
                    + ("" if use_reserve else "; remainder reserved for join checks"),
                    "quota",
                )
            self._budget.spend(1)
        try:
            return await self._bloxlink.lookup(m.id)
        except Exception as e:
            log.exception("Bloxlink client raised for discord=%s", m.id)
            return BloxlinkFailure(m.id, f"{type(e).__name__}: {e}")

    async def _fill_from_ids(self, out: list[IdentityResult], by_id: dict[int, int]) -> None:
        try:
            looked_up = await self._resolver.lookup_ids(list(by_id))
        except Exception as e:
            log.exception("Roblox id lookup raised for %d ids", len(by_id))
            looked_up = {}
            detail = f"{type(e).__name__}: {e}"
            for rid, idx in by_id.items():
                prev = out[idx]
                assert isinstance(prev, Unverifiable)
                out[idx] = Unverifiable(prev.member, STAGE_RESOLVE, None, rid, detail)
            return

        for rid, idx in by_id.items():
            prev = out[idx]
            assert isinstance(prev, Unverifiable)
            r = looked_up.get(rid)
            if isinstance(r, RobloxUser):
                out[idx] = Identified(prev.member, r, IdentitySource.BLOXLINK, r.name)
            else:
                detail = r.detail if isinstance(r, ResolveFailure) else "no result for this Roblox id"
                # Bloxlink named an id Roblox will not describe. Still not clean: keep it inconclusive.
                out[idx] = Unverifiable(prev.member, STAGE_RESOLVE, None, rid,
                                        f"Bloxlink gave roblox id {rid} but Roblox lookup failed: {detail}")

    async def _fill_from_names(self, out: list[IdentityResult], pending: dict[str, list[int]]) -> None:
        names = list(pending)
        try:
            resolved = await self._resolver.resolve(names)
        except Exception as e:
            log.exception("resolver raised for %d usernames", len(names))
            resolved = {}
            for key, idxs in pending.items():
                for idx in idxs:
                    prev = out[idx]
                    assert isinstance(prev, Unverifiable)
                    out[idx] = Unverifiable(prev.member, STAGE_RESOLVE, prev.username, None, f"{type(e).__name__}: {e}")
            return

        for key, idxs in pending.items():
            r = resolved.get(key)
            for idx in idxs:
                prev = out[idx]
                assert isinstance(prev, Unverifiable)
                if isinstance(r, RobloxUser):
                    out[idx] = Identified(prev.member, r, IdentitySource.NICKNAME, prev.username or r.name)
                else:
                    detail = r.detail if isinstance(r, ResolveFailure) else "resolver returned nothing for this username"
                    out[idx] = Unverifiable(prev.member, STAGE_RESOLVE, prev.username, None, detail)
