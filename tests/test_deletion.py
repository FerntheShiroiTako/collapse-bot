"""Deleting what Collapse stores, as the privacy policy promises."""
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from banbot.storage.crypto import generate_key
from banbot.storage.store import Store, retention_cutoff

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
BOT_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.sqlite3", master_key=generate_key())
    yield s
    s.close()


def fill(store, guild_id, member=9, mod=42, sweep=True):
    """One guild's worth of rows: settings, a sweep, a case a mod decided, a ban, an inconclusive row."""
    store.update_guild_settings(guild_id, by=mod, at=NOW, rayward_api_key="rwd_x", mod_role_id=2, mod_channel_id=3,
                                sweep_trigger_user_ids=frozenset({mod, 77}), appeal_url="https://discord.gg/x")
    if sweep:  # a guild can only have one active sweep
        sweep = store.create_sweep(guild_id, started_at=NOW, started_by=mod, dry_run=True)
    else:
        sweep = store.get_active_sweep(guild_id)
    store.record_sweep_result(sweep.id, member, "review", roblox_username="u", roblox_id=5, detail="x", at=NOW)
    row, _ = store.enqueue_review(guild_id, discord_id=member, roblox_id=5, roblox_username="u", provider="rotector",
                                  outcome="confirmed", status_name="Confirmed", reason="r", nickname=None,
                                  raw_response_json="{}", summary="s", at=NOW)
    store.resolve_review(row.id, status="approved", by=mod, at=NOW, note=f"approved by {mod}")
    audit = store.write_audit(guild_id, discord_id=member, roblox_id=5, roblox_username="u", nickname_at_ban=None,
                              provider="rotector", status_name="Confirmed", raw_response_json="{}",
                              decision_path="mod_approved", approved_by=mod, dry_run=False, at=NOW)
    store.mark_banned(guild_id, member, 5, audit, NOW)
    store.upsert_inconclusive(guild_id, discord_id=member, sweep_id=sweep.id, roblox_username="u", roblox_id=5,
                              stage="flag", last_error="e", attempts=1, now=NOW, next_retry_at=NOW, exhausted=False)
    store.usage_increment(guild_id, "bloxlink", NOW, 3)


def rows(store, table, guild_id=None):
    sql = f"SELECT COUNT(*) FROM {table}" + (" WHERE guild_id = ?" if guild_id else "")
    return store._one(sql, (guild_id,) if guild_id else ()).__getitem__(0)


def test_purging_a_guild_leaves_other_guilds_alone(store):
    fill(store, 1); fill(store, 2)
    counts = store.purge_guild(1)
    assert counts["review_queue"] == 1 and counts["sweep_results"] == 1 and counts["guild_settings"] == 1
    for table in ("guild_settings", "sweeps", "review_queue", "inconclusive", "audit_log", "bans_applied", "api_usage"):
        assert rows(store, table, 1) == 0 and rows(store, table, 2) >= 1, table
    assert rows(store, "sweep_results") == 1  # guild 2's result survives


def test_dry_run_counts_and_changes_nothing(store):
    fill(store, 1)
    before = {t: rows(store, t) for t in ("review_queue", "audit_log", "bans_applied", "guild_settings")}
    assert store.purge_guild(1, dry_run=True)["review_queue"] == 1
    assert store.purge_user(9, dry_run=True)["review_queue"] == 1
    assert {t: rows(store, t) for t in before} == before


def test_purging_a_user_deletes_their_rows_everywhere(store):
    fill(store, 1, member=9); fill(store, 2, member=9); fill(store, 2, member=10, sweep=False)
    counts = store.purge_user(9)
    assert counts["review_queue"] == 2 and counts["bans_applied"] == 2 and counts["audit_log"] == 2
    for table in ("sweep_results", "review_queue", "inconclusive", "audit_log", "bans_applied"):
        assert store._one(f"SELECT COUNT(*) FROM {table} WHERE discord_id = 9")[0] == 0, table
        assert store._one(f"SELECT COUNT(*) FROM {table} WHERE discord_id = 10")[0] == 1, table


def test_a_moderator_is_removed_from_decisions_but_the_rows_stay(store):
    fill(store, 1, member=9, mod=42)
    counts = store.purge_user(42)
    assert counts["cases they decided"] == 1 and counts["bans they approved"] == 1
    assert counts["sweeps they started"] == 1 and counts["settings they changed"] == 1
    assert counts["sweep trigger lists"] == 1
    case = store.all_reviews(1)[0]
    assert case.resolved_by is None and case.resolution_note is None  # the note named them
    assert store.get_guild_settings(1).sweep_trigger_user_ids == frozenset({77})
    assert store._one("SELECT approved_by, ban_succeeded IS NULL FROM audit_log")["approved_by"] is None
    assert rows(store, "review_queue") == 1 and rows(store, "audit_log") == 1  # the records themselves stay


def test_removed_guild_loses_its_keys_at_once_and_its_data_after_the_wait(store):
    fill(store, 1)
    store.mark_guild_removed(1, NOW)
    settings = store.get_guild_settings(1)
    assert settings.rayward_api_key is None and not settings.setup_completed
    assert rows(store, "review_queue", 1) == 1  # data waits out the retention period
    assert store.guilds_removed_before(NOW - timedelta(days=30)) == []
    assert store.guilds_removed_before(NOW + timedelta(days=31)) == [1]
    store.clear_guild_removed(1)  # added back before then
    assert store.guilds_removed_before(NOW + timedelta(days=31)) == []


def test_a_failed_purge_changes_nothing(store, monkeypatch):
    fill(store, 1)
    real = Store._count_and_delete  # a staticmethod: reading it off the class gives the plain function
    calls = []

    def boom(conn, label, frm, param, dry_run):
        calls.append(label)
        if label == "bans_applied":
            raise RuntimeError("disk full")
        return real(conn, label, frm, param, dry_run)

    monkeypatch.setattr(Store, "_count_and_delete", staticmethod(boom))
    with pytest.raises(RuntimeError):
        store.purge_user(9)
    assert "bans_applied" in calls
    assert rows(store, "review_queue") == 1 and rows(store, "sweep_results") == 1  # earlier deletes rolled back


def test_nothing_outlives_the_limit_between_runs():
    # Cleaning runs every 10 minutes, so rows must be cleared 10 minutes early to never pass 24 hours.
    assert retention_cutoff(NOW, 24, 600) == NOW - timedelta(hours=23, minutes=50)


def test_purge_commands_ask_for_confirmation(tmp_path):
    db = tmp_path / "data" / "banbot.sqlite3"
    s = Store(db, master_key=generate_key()); fill(s, 1); s.close()
    env = {"DB_PATH": str(db), "PATH": ""}

    def run(*args):
        return subprocess.run([sys.executable, "-m", "banbot", *args], cwd=BOT_DIR, env={**env, "SYSTEMROOT": "C:/Windows"},
                              capture_output=True, text=True)

    shown = run("purge-guild", "1")
    assert shown.returncode == 0 and "Would delete" in shown.stdout and "--yes" in shown.stdout
    check = Store(db); assert rows(check, "review_queue", 1) == 1; check.close()

    done = run("purge-guild", "1", "--yes")
    assert done.returncode == 0 and "Deleted" in done.stdout
    check = Store(db); assert rows(check, "review_queue", 1) == 0; check.close()

    assert run("purge-guild", "abc").returncode == 2  # not a number: shows usage instead of guessing


def test_rows_older_than_the_cutoff_are_deleted_and_settings_kept(store):
    fill(store, 1)
    assert not any(store.purge_older_than(NOW).values())  # nothing is older than now
    counts = store.purge_older_than(NOW + timedelta(days=31))
    for table in ("review_queue", "inconclusive", "audit_log", "bans_applied", "api_usage", "sweep_results"):
        assert counts[table] >= 1 and rows(store, table, None if table == "sweep_results" else 1) == 0, table
    assert rows(store, "guild_settings", 1) == 1
    assert rows(store, "sweeps", 1) == 1  # still active: a running sweep is never deleted


def test_finished_sweeps_go_once_their_results_have(store):
    fill(store, 1)
    sweep = store.get_active_sweep(1)
    store.finish_sweep(sweep.id, finished_at=NOW, status="finished", counts={})
    assert store.purge_older_than(NOW + timedelta(days=31))["sweeps"] == 1
    assert rows(store, "sweeps", 1) == 0
