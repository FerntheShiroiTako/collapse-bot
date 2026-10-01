"""The pipeline's routing rules: flagged accounts go to mods, failures are never clean, nothing bans."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from banbot.core.flags import FlagOutcome, FlagResult
from banbot.core.gateway import MemberInfo
from banbot.core.identity import Identified, IdentitySource
from banbot.core.pipeline import Bucket, Pipeline
from banbot.integrations.roblox import RobloxUser
from banbot.settings.config import RetryConfig


class FakeReviews:
    def __init__(self):
        self.calls = []

    async def enqueue(self, case):
        self.calls.append(("queue", case.reason))
        return NS(id=len(self.calls)), True

    async def report(self, case):
        self.calls.append(("report", case.reason))
        return NS(id=len(self.calls)), True


class FakeStore:
    def __init__(self):
        self.inconclusive = {}

    def clear_inconclusive(self, guild_id, discord_id):
        self.inconclusive.pop(discord_id, None)

    def get_inconclusive(self, guild_id, discord_id):
        return self.inconclusive.get(discord_id)

    def upsert_inconclusive(self, guild_id, *, discord_id, attempts, exhausted, **_):
        self.inconclusive[discord_id] = NS(attempts=attempts, exhausted=exhausted, sweep_id=None)

    def record_sweep_result(self, *a, **k):
        pass


class FakeClock:
    def now(self):
        from datetime import datetime, timezone
        return datetime(2026, 9, 28, tzinfo=timezone.utc)


def make_pipeline(*, report_only=False, max_retries=5):
    cfg = NS(guild_id=1, report_only=report_only, retry=RetryConfig(max_retries=max_retries))
    reviews = FakeReviews()
    store = FakeStore()
    pipe = Pipeline(cfg=cfg, store=store, gateway=None, identity=None, provider=NS(name="rotector"),
                    review_queue=reviews, clock=FakeClock())
    return pipe, reviews, store


IDENT = Identified(
    MemberInfo(9, "owl (@example_user)"),
    RobloxUser(id=1834022045, name="example_user", display_name="", requested_username="example_user"),
    IdentitySource.BLOXLINK, "example_user",
)


def flag(outcome, name):
    return FlagResult(outcome, "rotector", None, name, {}, "")


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("outcome,name,reason", [
    (FlagOutcome.CONFIRMED, "Confirmed", "confirmed_requires_review"),
    (FlagOutcome.REVIEW, "Flagged", "status:Flagged"),
])
def test_flagged_accounts_open_a_case(outcome, name, reason):
    pipe, reviews, _ = make_pipeline()
    assert run(pipe._route(IDENT, flag(outcome, name), None)) is Bucket.REVIEW
    assert reviews.calls == [("queue", reason)]


def test_report_only_posts_notices_instead_of_cases():
    pipe, reviews, _ = make_pipeline(report_only=True)
    assert run(pipe._route(IDENT, flag(FlagOutcome.CONFIRMED, "Confirmed"), None)) is Bucket.REPORTED
    assert run(pipe._route_ban_evasion(IDENT, 555, None)) is Bucket.REPORTED
    assert [c[0] for c in reviews.calls] == ["report", "report"]


def test_ban_evasion_goes_to_mods():
    pipe, reviews, _ = make_pipeline()
    assert run(pipe._route_ban_evasion(IDENT, 555, None)) is Bucket.REVIEW
    assert reviews.calls == [("queue", "ban_evasion")]


def test_clear_and_past_offender_open_nothing():
    pipe, reviews, _ = make_pipeline()
    assert run(pipe._route(IDENT, flag(FlagOutcome.CLEAR, "Unflagged"), None)) is Bucket.CLEAR
    assert run(pipe._route(IDENT, flag(FlagOutcome.PAST_OFFENDER, "Past Offender"), None)) is Bucket.PAST_OFFENDER
    assert reviews.calls == []


@pytest.mark.parametrize("outcome", [FlagOutcome.INCONCLUSIVE, FlagOutcome.UNMAPPED])
def test_failures_are_never_clean(outcome):
    pipe, reviews, store = make_pipeline()
    assert run(pipe._route(IDENT, flag(outcome, "error"), None)) is Bucket.INCONCLUSIVE
    assert store.inconclusive[9].attempts == 1
    assert reviews.calls == []


def test_exhausted_retries_become_an_unverified_case():
    pipe, reviews, store = make_pipeline(max_retries=1)
    run(pipe._route(IDENT, flag(FlagOutcome.INCONCLUSIVE, "error"), None))
    run(pipe._route(IDENT, flag(FlagOutcome.INCONCLUSIVE, "error"), None))
    assert store.inconclusive[9].exhausted
    assert reviews.calls == [("queue", "inconclusive_exhausted")]


def test_pipeline_has_no_way_to_ban():
    # The only ban call lives in core/enforcement.py; the pipeline must not even hold a Banner.
    pipe, _, _ = make_pipeline()
    assert not any("ban" in name.lower() and "evasion" not in name.lower() for name in vars(pipe))
