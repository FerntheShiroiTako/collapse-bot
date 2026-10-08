"""Checking Rotector and RCR together: any source's flag is acted on, a failed source is never clean."""
import asyncio

import pytest

from banbot.core.flags import CombinedProvider, FlagOutcome, FlagResult, combine
from banbot.integrations.net import JsonResponse
from banbot.integrations.rayward import RaywardSourceProvider


def flag(outcome, provider, name="", detail=""):
    return FlagResult(outcome, provider, None, name or outcome.value, {}, detail)


def fail(provider):
    return FlagResult.inconclusive(provider, "HTTP 503")


@pytest.mark.parametrize("rotector, rcr, want_outcome, want_provider", [
    (flag(FlagOutcome.CLEAR, "rotector"), flag(FlagOutcome.CLEAR, "rcr"), FlagOutcome.CLEAR, "rotector"),
    (flag(FlagOutcome.CLEAR, "rotector"), flag(FlagOutcome.REVIEW, "rcr"), FlagOutcome.REVIEW, "rcr"),
    (flag(FlagOutcome.REVIEW, "rotector"), flag(FlagOutcome.CONFIRMED, "rcr"), FlagOutcome.CONFIRMED, "rcr"),
    (flag(FlagOutcome.CONFIRMED, "rotector"), flag(FlagOutcome.CONFIRMED, "rcr"), FlagOutcome.CONFIRMED, "rotector"),
    # one source clean and the other unreachable is not clean
    (flag(FlagOutcome.CLEAR, "rotector"), fail("rcr"), FlagOutcome.INCONCLUSIVE, "rcr"),
    (flag(FlagOutcome.PAST_OFFENDER, "rotector"), fail("rcr"), FlagOutcome.INCONCLUSIVE, "rcr"),
    # a flag is still a flag when the other source is down
    (fail("rotector"), flag(FlagOutcome.REVIEW, "rcr"), FlagOutcome.REVIEW, "rcr"),
    (flag(FlagOutcome.PAST_OFFENDER, "rotector"), flag(FlagOutcome.CLEAR, "rcr"), FlagOutcome.PAST_OFFENDER, "rotector"),
])
def test_most_severe_answer_wins(rotector, rcr, want_outcome, want_provider):
    got = combine([rotector, rcr])
    assert (got.outcome, got.provider) == (want_outcome, want_provider)


def test_other_sources_flags_are_noted_in_the_detail():
    got = combine([flag(FlagOutcome.CONFIRMED, "rotector", "Confirmed", "Status: Confirmed"),
                   flag(FlagOutcome.REVIEW, "rcr", "Flagged")])
    assert got.provider == "rotector"
    assert got.detail == "Status: Confirmed\nAlso flagged: RCR lists it as Flagged."


def test_inconclusive_detail_names_the_failing_source():
    assert combine([flag(FlagOutcome.CLEAR, "rotector"), fail("rcr")]).detail == "RCR: HTTP 503"


class FakeSource:
    def __init__(self, name, answer):
        self.name, self.answer = name, answer

    async def lookup(self, ids):
        if isinstance(self.answer, Exception):
            raise self.answer
        return {i: self.answer for i in ids}


def test_a_source_that_raises_makes_the_lookup_inconclusive():
    provider = CombinedProvider([FakeSource("rotector", flag(FlagOutcome.CLEAR, "rotector")),
                                 FakeSource("rcr", RuntimeError("boom"))])
    got = asyncio.run(provider.lookup([5]))[5]
    assert got.outcome is FlagOutcome.INCONCLUSIVE and got.provider == "rcr"


def test_rcr_source_calls_its_own_endpoint_and_maps_statuses():
    calls = []

    async def requester(method, url, *, json_body=None, headers=None, **_):
        calls.append((method, url, json_body))
        return JsonResponse(200, {"success": True, "data": {
            "5": {"id": 5, "flagType": 2, "reasons": []},
            "6": {"id": 6, "flagType": 0},
        }}, {})

    provider = RaywardSourceProvider(requester, source="rcr", api_key="rwd_x",
                                     base_url="https://roscoe.rayward.app/", batch_size=100)
    got = asyncio.run(provider.lookup([5, 6]))
    assert calls == [("POST", "https://roscoe.rayward.app/v2/lookup/rcr/roblox/user", {"ids": [5, 6]})]
    assert (got[5].outcome, got[5].provider, got[5].status_name) == (FlagOutcome.CONFIRMED, "rcr", "Confirmed")
    assert got[6].outcome is FlagOutcome.CLEAR


# ------------------------------------------------------------------ every source, and its ban DM

def test_every_rayward_source_is_checked():
    from banbot.core.flags import RAYWARD_SOURCES
    assert set(RAYWARD_SOURCES) == {"rotector", "rcr", "tase", "rab", "okappiki", "serversweep"}


def test_awaiting_human_review_goes_to_the_mods():
    from banbot.integrations.rayward import map_flag_type
    assert map_flag_type(10)[2] is FlagOutcome.REVIEW


@pytest.mark.parametrize("provider, must_say, must_not_say", [
    ("tase", ["TASE", "discord.gg/VH4e8Wxfmd"], ["Rotector", "{source"]),
    ("rcr", ["RCR (Roblox Criminal Records)", "discord.gg/y7pGa2MR2G"], ["Rotector", "{source"]),
    ("rotector", ["Rotector", "rotector.com"], ["{source"]),
])
def test_ban_dm_names_the_source_the_ban_is_based_on(provider, must_say, must_not_say):
    from types import SimpleNamespace as NS
    from banbot.core.enforcement import Banner, BanRequest
    from banbot.settings.guild import GuildSettings

    sent = []

    async def send_dm(user_id, text):
        sent.append(text)
        return True

    banner = Banner(guild_id=1, store=NS(set_audit_dm=lambda *a, **k: None),
                    gateway=NS(guild_name=lambda: "Test Server", send_dm=send_dm),
                    clock=None, dry_run=False, dm_templates=GuildSettings(guild_id=1).ban_dm_templates({}))
    req = BanRequest(discord_id=9, roblox_id=5, roblox_username="example_user", nickname_at_ban=None,
                     provider=provider, status_name="Confirmed", raw_response_json="{}",
                     decision_path="mod_approved", approved_by=42)
    asyncio.run(banner._notify(req, 1))
    assert len(sent) == 1
    assert all(s in sent[0] for s in must_say), sent[0]
    assert not any(s in sent[0] for s in must_not_say), sent[0]
