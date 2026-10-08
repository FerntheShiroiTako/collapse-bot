"""Provider-agnostic flag outcomes. Every provider maps its raw statuses onto exactly these."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Protocol, Sequence

from banbot import brand

log = logging.getLogger(__name__)


class FlagOutcome(str, Enum):
    CONFIRMED = "confirmed"  # -> Step 4 (live nickname re-check, then ban) or review if configured
    REVIEW = "review"  # Flagged / Mixed / anything a human must look at
    PAST_OFFENDER = "past_offender"  # allow, log only
    CLEAR = "clear"  # no action
    INCONCLUSIVE = "inconclusive"  # API error / timeout / missing -> retry, never clean
    UNMAPPED = "unmapped"  # status we do not know -> treated as inconclusive, logged loudly


@dataclass(frozen=True)
class Source:
    label: str  # short public name for cards and logs; anything shown about a flag names its source (Rayward's terms)
    full_name: str  # how the ban DM names it
    appeal: str  # where a banned member can dispute the listing, given in the ban DM


# Every Rayward source, in lookup order. Keys are Rayward's source ids (GET /v2/providers), which are also
# the provider value stored on rows. Appeal links are the providers' appealUrl; RAB has none, so its website.
RAYWARD_SOURCES: dict[str, Source] = {
    "rotector": Source("Rotector", "Rotector", "https://rotector.com"),
    "rcr": Source("RCR", "RCR (Roblox Criminal Records)", "https://discord.gg/y7pGa2MR2G"),
    "tase": Source("TASE", "TASE", "https://discord.gg/VH4e8Wxfmd"),
    "rab": Source("RAB", "RAB", "https://moco-co.org/"),
    "okappiki": Source("Okappiki", "Okappiki", "https://okappiki.com/appeal"),
    "serversweep": Source("ServerSweep", "ServerSweep", "https://discord.gg/KdVhtSE4Jp"),
}
CHECKED_SOURCES = "Rayward's sources (" + ", ".join(s.label for s in RAYWARD_SOURCES.values()) + ")"


def source_label(provider: str) -> str:
    if provider == "banbot":
        return brand.EVASION_SOURCE
    source = RAYWARD_SOURCES.get(provider)
    return source.label if source else provider


@dataclass(frozen=True)
class FlagResult:
    outcome: FlagOutcome
    provider: str
    status_code: int | None  # raw provider status (Rayward flagType), None on error
    status_name: str  # human label: "Confirmed", "Mixed", "error", "unmapped(7)"...
    raw: dict[str, Any] | None  # the provider's raw entry for this id, None when we got nothing
    detail: str = ""  # short human summary of the raw response, or the error text

    def raw_json(self) -> str:
        if self.raw is not None:
            return json.dumps(self.raw, ensure_ascii=False, sort_keys=True)
        return json.dumps({"error": self.detail, "provider": self.provider, "status": self.status_name})

    @staticmethod
    def inconclusive(provider: str, detail: str) -> "FlagResult":
        return FlagResult(FlagOutcome.INCONCLUSIVE, provider, None, "error", None, detail)


class FlagProvider(Protocol):
    name: str

    async def lookup(self, roblox_ids: Sequence[int]) -> dict[int, FlagResult]:
        """Must return an entry for every id requested. Errors become INCONCLUSIVE, never CLEAR."""
        ...


# Which answer wins when sources disagree. A flag from any source is acted on. Below that, a source that
# couldn't answer beats a clean answer from another, because "one source says clean" isn't "clean".
SEVERITY = {
    FlagOutcome.CONFIRMED: 5,
    FlagOutcome.REVIEW: 4,
    FlagOutcome.UNMAPPED: 3,
    FlagOutcome.INCONCLUSIVE: 2,
    FlagOutcome.PAST_OFFENDER: 1,
    FlagOutcome.CLEAR: 0,
}
FLAGGED = (FlagOutcome.CONFIRMED, FlagOutcome.REVIEW)


def combine(results: Sequence[FlagResult]) -> FlagResult:
    """One answer for one account out of several sources' answers: the most severe one, with the other
    sources' flags (if any) noted in its detail so mods see every source that flagged the account."""
    primary = max(results, key=lambda r: SEVERITY[r.outcome])  # max() keeps the first on ties: source order
    others = [r for r in results if r is not primary and r.outcome in FLAGGED]
    if primary.outcome is FlagOutcome.INCONCLUSIVE:
        return replace(primary, detail=f"{source_label(primary.provider)}: {primary.detail}")
    if not others:
        return primary
    also = "; ".join(f"{source_label(r.provider)} lists it as {r.status_name}" for r in others)
    detail = f"{primary.detail}\nAlso flagged: {also}." if primary.detail else f"Also flagged: {also}."
    return replace(primary, detail=detail)


class CombinedProvider:
    """Looks each account up on every source and keeps the most severe answer (see combine())."""

    def __init__(self, sources: Sequence[FlagProvider], *, name: str = "rayward"):
        self.name = name
        self.sources = list(sources)

    async def lookup(self, roblox_ids: Sequence[int]) -> dict[int, FlagResult]:
        answers = await asyncio.gather(*(s.lookup(roblox_ids) for s in self.sources), return_exceptions=True)
        per_source: list[dict[int, FlagResult]] = []
        for source, found in zip(self.sources, answers):
            if isinstance(found, BaseException):
                log.error("flag source %s raised: %r", source.name, found)
                found = {}
            per_source.append(found)
        out: dict[int, FlagResult] = {}
        for rid in roblox_ids:
            results = [
                found.get(rid) or FlagResult.inconclusive(source.name, "source returned no entry for this id")
                for source, found in zip(self.sources, per_source)
            ]
            out[rid] = combine(results)
        return out
