import sqlite3
from datetime import datetime, timezone

import pytest

from banbot.storage.crypto import CryptoError, SecretBox, generate_key
from banbot.storage.store import SCHEMA, Store, SweepAlreadyRunning

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db" / "t.sqlite3", master_key=generate_key())
    yield s
    s.close()


def case(**over):
    base = dict(discord_id=9, roblox_id=1834022045, roblox_username="example_user", provider="rotector",
                outcome="confirmed", status_name="Confirmed", reason="confirmed_requires_review",
                nickname=None, raw_response_json="{}", summary="", at=NOW)
    base.update(over)
    return base


def test_creates_its_data_folder(tmp_path):
    Store(tmp_path / "new" / "folder" / "db.sqlite3").close()
    assert (tmp_path / "new" / "folder" / "db.sqlite3").exists()


def test_settings_round_trip_and_keys_are_encrypted(store, tmp_path):
    store.update_guild_settings(1, by=1, at=NOW, rayward_api_key="rwd_secret", appeal_url="https://discord.gg/x",
                                dry_run=False)
    s = store.get_guild_settings(1)
    assert (s.rayward_api_key, s.appeal_url, s.dry_run) == ("rwd_secret", "https://discord.gg/x", False)
    raw = store._one("SELECT rayward_api_key_enc FROM guild_settings WHERE guild_id = 1", ())[0]
    assert "rwd_secret" not in raw


def test_new_servers_start_in_dry_run(store):
    assert store.get_or_create_guild_settings(42).dry_run is True


def test_one_pending_case_per_member(store):
    first, created1 = store.enqueue_review(1, **case())
    second, created2 = store.enqueue_review(1, **case())
    assert created1 and not created2 and first.id == second.id


def test_a_case_resolves_only_once(store):
    row, _ = store.enqueue_review(1, **case())
    assert store.resolve_review(row.id, status="approved", by=5, at=NOW)
    assert not store.resolve_review(row.id, status="denied", by=6, at=NOW)


def test_one_active_sweep_per_server(store):
    store.create_sweep(1, started_at=NOW, started_by=None, dry_run=True)
    with pytest.raises(SweepAlreadyRunning):
        store.create_sweep(1, started_at=NOW, started_by=None, dry_run=True)
    store.create_sweep(2, started_at=NOW, started_by=None, dry_run=True)  # other servers unaffected


def test_old_database_gets_new_columns(tmp_path):
    # A database from before appeal_url existed: create it, drop the column, reopen.
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("ALTER TABLE guild_settings DROP COLUMN appeal_url")
    conn.close()
    s = Store(path, master_key=generate_key())
    s.update_guild_settings(1, by=1, at=NOW, appeal_url="https://discord.gg/x")
    assert s.get_guild_settings(1).appeal_url == "https://discord.gg/x"
    s.close()


def test_wrong_master_key_is_detected():
    token = SecretBox(generate_key()).encrypt("hello")
    with pytest.raises(CryptoError):
        SecretBox(generate_key()).decrypt(token)
