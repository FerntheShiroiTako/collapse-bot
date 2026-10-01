"""Rules from Rayward's terms (rayward.app/terms) that the bot has to keep."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from banbot.core.enforcement import BanOutcome, BanResult
from banbot.core.flags import FlagOutcome, FlagResult
from banbot.core.review import ReviewQueue
from banbot.settings.config import ConfigError, GlobalConfig
from banbot.settings.guild import GuildSettings
from banbot.storage.crypto import generate_key
from banbot.storage.store import Store

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
REASONS = {"id": 5, "flagType": 2, "statusLabel": "Confirmed", "category": "x",
           "reasons": [{"type": "profile", "title": "Inappropriate profile"}]}


def new_store(tmp_path):
    return Store(tmp_path / "t.sqlite3", master_key=generate_key())


def add_case(store, at, **over):
    fields = dict(discord_id=9, roblox_id=5, roblox_username="example_user", provider="rotector",
                  outcome="confirmed", status_name="Confirmed", reason="confirmed_requires_review", nickname=None,
                  raw_response_json=json.dumps(REASONS), summary="Reasons:\n- Inappropriate profile", at=at)
    fields.update(over)
    row, _ = store.enqueue_review(1, **fields)
    return row


# ------------------------------------------------------------------ data must not be kept past 24 hours

def test_rotector_details_are_removed_after_24_hours(tmp_path):
    store = new_store(tmp_path)
    old = add_case(store, NOW - timedelta(hours=25))
    store.set_review_message(old.id, 111, 222)
    fresh = add_case(store, NOW - timedelta(hours=1), discord_id=10)

    touched, to_strip = store.redact_raw_older_than(NOW - timedelta(hours=24), NOW)

    assert touched == 1 and to_strip == [(111, 222)]
    kept = store.get_review(1, old.id)
    assert kept.summary == ""
    raw = json.loads(kept.raw_response_json)
    assert "reasons" not in raw and "category" not in raw and "reason_types" not in raw
    assert raw["statusLabel"] == "Confirmed"  # the bare status stays as Collapse's record of its own decision
    assert store.get_review(1, fresh.id).summary  # under 24 hours: untouched


def test_retention_cannot_be_configured_past_24_hours():
    with pytest.raises(ConfigError):
        GlobalConfig.from_env({"DISCORD_TOKEN": "t", "MASTER_KEY": "k", "RAW_RETENTION_HOURS": "48"})


# ------------------------------------------------------------------ never act on stale flag data

class FakeBanner:
    def __init__(self):
        self.requests = []

    async def ban(self, req):
        self.requests.append(req)
        return BanResult(BanOutcome.WOULD_BAN, 1)


class FakeProvider:
    def __init__(self, result):
        self.result = result

    async def lookup(self, ids):
        if isinstance(self.result, Exception):
            raise self.result
        return {} if self.result is None else {ids[0]: self.result}


class FakePoster:
    async def update(self, row, resolution):
        self.resolution = resolution


def make_queue(tmp_path, provider_result):
    store = new_store(tmp_path)
    row = add_case(store, NOW - timedelta(days=3))
    banner, poster = FakeBanner(), FakePoster()
    queue = ReviewQueue(
        guild_id=1, store=store, poster=poster, banner=banner, clock=NS(now=lambda: NOW),
        gateway=NS(fetch_nickname=lambda _id: asyncio.sleep(0, result="owl")), mod_role_id=7,
        provider=FakeProvider(provider_result),
    )
    return queue, store, row, banner, poster


def flag(outcome, name):
    return FlagResult(outcome, "rotector", None, name, {"statusLabel": name}, "")


def approve(queue, row):
    return asyncio.run(queue.approve(row.id, actor_id=42, actor_role_ids=[7]))


def test_ban_uses_the_status_rotector_gives_now(tmp_path):
    queue, store, row, banner, _ = make_queue(tmp_path, flag(FlagOutcome.REVIEW, "Flagged"))
    assert approve(queue, row).ok
    assert banner.requests[0].status_name == "Flagged"  # not the 3-day-old "Confirmed"
    assert store.get_review(1, row.id).status == "approved"


def test_no_ban_when_rotector_no_longer_flags_the_account(tmp_path):
    queue, store, row, banner, poster = make_queue(tmp_path, flag(FlagOutcome.CLEAR, "Unflagged"))
    decision = approve(queue, row)
    assert decision.ok and "nobody was banned" in decision.message
    assert banner.requests == []
    assert store.get_review(1, row.id).status == "denied"
    assert "Unflagged" in poster.resolution


@pytest.mark.parametrize("result", [None, RuntimeError("down"), "inconclusive"])
def test_no_ban_and_case_stays_open_when_rotector_cant_be_reached(tmp_path, result):
    if result == "inconclusive":
        result = FlagResult.inconclusive("rotector", "timeout")
    queue, store, row, banner, _ = make_queue(tmp_path, result)
    decision = approve(queue, row)
    assert not decision.ok
    assert banner.requests == []
    assert store.get_review(1, row.id).status == "pending"


# ------------------------------------------------------------------ an open case follows the mod channel

class MovingPoster:
    channel_id = 999  # the current mod channel

    def __init__(self):
        self.posted = []

    async def post(self, row):
        self.posted.append(row.id)
        return (self.channel_id, 5000 + row.id)

    async def log_detection(self, row):
        pass


def test_open_case_is_reposted_when_mod_channel_changed(tmp_path):
    from banbot.core.review import ReviewCase
    store = new_store(tmp_path)
    old = add_case(store, NOW - timedelta(hours=1))
    store.set_review_message(old.id, 111, 222)  # posted in a channel that's no longer the mod channel
    poster = MovingPoster()
    queue = ReviewQueue(guild_id=1, store=store, poster=poster, banner=FakeBanner(), gateway=None,
                        clock=NS(now=lambda: NOW), mod_role_id=7)
    case = ReviewCase(discord_id=9, roblox_id=5, roblox_username="example_user", nickname=None,
                      flag=flag(FlagOutcome.CONFIRMED, "Confirmed"), reason="confirmed_requires_review")

    row, created = asyncio.run(queue.enqueue(case))
    assert not created and poster.posted == [old.id]
    assert (row.channel_id, row.message_id) == (999, 5000 + old.id)
    assert store.open_review_for(1, 9).id == old.id

    asyncio.run(queue.enqueue(case))  # already in the right channel now: no second post
    assert poster.posted == [old.id]


# ------------------------------------------------------------------ attribution in ban DMs

def test_each_ban_source_gets_its_own_dm_text():
    templates = GuildSettings(guild_id=1).ban_dm_templates({})
    assert "Rotector" in templates["rotector"]
    assert "Rotector" not in templates["banbot"]
    assert GuildSettings(guild_id=1, ban_dm_enabled=False).ban_dm_templates({}) is None
