"""SQLite persistence: per-guild settings, sweep progress, per-member sweep results, review queue,
inconclusive bucket, audit log, applied bans. Single-file, survives restarts, zero infrastructure.

Every guild the bot moderates shares this one database, scoped by guild_id. API keys are stored
encrypted (storage/crypto.py) and decrypted only when read back into a GuildSettings.
"""
from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from banbot.storage.crypto import SecretBox
from banbot.settings.guild import GuildSettings

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id                   INTEGER PRIMARY KEY,
    rayward_api_key_enc        TEXT,
    bloxlink_api_key_enc       TEXT,
    mod_role_id                INTEGER,
    mod_channel_id              INTEGER,
    summary_channel_id         INTEGER,
    log_forum_channel_id       INTEGER,
    tag_confirmed_id           INTEGER,
    tag_review_id              INTEGER,
    tag_past_offender_id       INTEGER,
    tag_ban_evasion_id         INTEGER,
    tag_reported_id            INTEGER,
    master_role_id             INTEGER,
    configurator_role_id       INTEGER,
    sweep_trigger_user_ids     TEXT NOT NULL DEFAULT '',
    sweep_trigger_role_id      INTEGER,
    report_only                INTEGER NOT NULL DEFAULT 0,
    dry_run                    INTEGER NOT NULL DEFAULT 1,
    ban_dm_enabled             INTEGER NOT NULL DEFAULT 1,
    appeal_url                 TEXT,
    notify_starter_on_sweep_complete INTEGER NOT NULL DEFAULT 1,
    setup_completed             INTEGER NOT NULL DEFAULT 0,
    setup_by                   INTEGER,
    setup_at                   TEXT,
    updated_at                 TEXT,
    updated_by                 INTEGER,
    removed_at                 TEXT       -- set when the bot is removed from the guild; its data is purged later
);

CREATE TABLE IF NOT EXISTS sweeps (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    status            TEXT    NOT NULL,          -- running | retrying | finished | failed
    active            INTEGER,                   -- 1 while running/retrying, NULL otherwise
    started_at        TEXT    NOT NULL,
    finished_at       TEXT,
    started_by        INTEGER,
    cursor_member_id  INTEGER NOT NULL DEFAULT 0,
    total_members     INTEGER,
    dry_run           INTEGER NOT NULL,
    counts_json       TEXT,
    error             TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS sweeps_one_active ON sweeps(guild_id) WHERE active = 1;
CREATE INDEX IF NOT EXISTS sweeps_guild ON sweeps(guild_id, id);

CREATE TABLE IF NOT EXISTS sweep_results (
    sweep_id         INTEGER NOT NULL,
    discord_id       INTEGER NOT NULL,
    bucket           TEXT    NOT NULL,
    roblox_username  TEXT,
    roblox_id        INTEGER,
    detail           TEXT,
    updated_at       TEXT    NOT NULL,
    PRIMARY KEY (sweep_id, discord_id)
);

CREATE TABLE IF NOT EXISTS review_queue (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id           INTEGER NOT NULL,
    discord_id         INTEGER NOT NULL,
    roblox_id          INTEGER,
    roblox_username    TEXT    NOT NULL,
    provider           TEXT    NOT NULL,
    outcome            TEXT    NOT NULL,
    status_name        TEXT    NOT NULL,
    reason             TEXT    NOT NULL,
    nickname           TEXT,
    raw_response_json  TEXT    NOT NULL,
    summary            TEXT    NOT NULL,
    status             TEXT    NOT NULL DEFAULT 'pending',   -- pending | approved | denied | reported
    channel_id         INTEGER,
    message_id         INTEGER,
    created_at         TEXT    NOT NULL,
    resolved_at        TEXT,
    resolved_by        INTEGER,
    resolution_note    TEXT,
    raw_redacted_at    TEXT,
    last_seen_at       TEXT,
    seen_count         INTEGER NOT NULL DEFAULT 1,
    identity_source    TEXT    NOT NULL DEFAULT 'nickname',
    avatar_url         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS review_queue_one_pending
    ON review_queue(guild_id, discord_id, COALESCE(roblox_id, -1)) WHERE status = 'pending';
-- Report-only mode: one notice per member + Roblox id + status; re-runs bump seen_count instead of re-posting.
CREATE UNIQUE INDEX IF NOT EXISTS review_queue_one_report
    ON review_queue(guild_id, discord_id, COALESCE(roblox_id, -1), status_name) WHERE status = 'reported';
CREATE INDEX IF NOT EXISTS review_queue_guild ON review_queue(guild_id, status, id);

CREATE TABLE IF NOT EXISTS inconclusive (
    guild_id         INTEGER NOT NULL,
    discord_id       INTEGER NOT NULL,
    sweep_id         INTEGER,
    roblox_username  TEXT,
    roblox_id        INTEGER,
    stage            TEXT    NOT NULL,   -- resolve | flag | recheck
    last_error       TEXT,
    attempts         INTEGER NOT NULL,
    first_seen_at    TEXT    NOT NULL,
    next_retry_at    TEXT,
    exhausted        INTEGER NOT NULL DEFAULT 0,
    review_id        INTEGER,
    PRIMARY KEY (guild_id, discord_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id           INTEGER NOT NULL,
    discord_id         INTEGER NOT NULL,
    roblox_id          INTEGER,
    roblox_username    TEXT,
    nickname_at_ban    TEXT,
    provider           TEXT,
    status_name        TEXT,
    raw_response_json  TEXT    NOT NULL,
    decision_path      TEXT    NOT NULL,   -- mod_approved
    approved_by        INTEGER,
    dry_run            INTEGER NOT NULL,
    ban_succeeded      INTEGER,            -- NULL = not attempted (dry run / pending), 1 / 0
    ban_error          TEXT,
    created_at         TEXT    NOT NULL,
    raw_redacted_at    TEXT,
    dm_sent            INTEGER,            -- NULL = no DM attempted (dry run / no template), 1 / 0
    dm_error           TEXT
);
CREATE INDEX IF NOT EXISTS audit_log_guild ON audit_log(guild_id, id);

CREATE TABLE IF NOT EXISTS bans_applied (
    guild_id    INTEGER NOT NULL,
    discord_id  INTEGER NOT NULL,
    roblox_id   INTEGER,
    audit_id    INTEGER,
    banned_at   TEXT NOT NULL,
    PRIMARY KEY (guild_id, discord_id)
);
-- Ban-evasion lookup: the same Roblox account showing up under a different Discord account.
CREATE INDEX IF NOT EXISTS bans_applied_roblox ON bans_applied(guild_id, roblox_id);

CREATE TABLE IF NOT EXISTS api_usage (
    guild_id INTEGER NOT NULL,
    api      TEXT    NOT NULL,
    day      TEXT    NOT NULL,   -- UTC date, YYYY-MM-DD
    count    INTEGER NOT NULL,
    PRIMARY KEY (guild_id, api, day)
);
"""


class SweepAlreadyRunning(Exception):
    pass


def _ts(dt: datetime) -> str:
    return dt.isoformat()


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _ids_to_csv(ids: frozenset[int]) -> str:
    return ",".join(str(i) for i in sorted(ids))


def _csv_to_ids(csv: str | None) -> frozenset[int]:
    if not csv:
        return frozenset()
    return frozenset(int(p) for p in csv.split(",") if p.strip())


@dataclass(frozen=True)
class SweepRow:
    id: int
    guild_id: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    started_by: int | None
    cursor_member_id: int
    total_members: int | None
    dry_run: bool
    counts: dict[str, int]
    error: str | None

    @property
    def active(self) -> bool:
        return self.status in ("running", "retrying")


@dataclass(frozen=True)
class ReviewRow:
    id: int
    guild_id: int
    discord_id: int
    roblox_id: int | None
    roblox_username: str
    provider: str
    outcome: str
    status_name: str
    reason: str
    nickname: str | None
    raw_response_json: str
    summary: str
    status: str
    channel_id: int | None
    message_id: int | None
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: int | None
    resolution_note: str | None
    last_seen_at: datetime | None = None
    seen_count: int = 1
    identity_source: str = "nickname"
    avatar_url: str | None = None


@dataclass(frozen=True)
class InconclusiveRow:
    guild_id: int
    discord_id: int
    sweep_id: int | None
    roblox_username: str | None
    roblox_id: int | None
    stage: str
    last_error: str | None
    attempts: int
    first_seen_at: datetime
    next_retry_at: datetime | None
    exhausted: bool
    review_id: int | None


@dataclass(frozen=True)
class BanAppliedRow:
    guild_id: int
    discord_id: int
    roblox_id: int | None
    audit_id: int | None
    banned_at: datetime


class Store:
    def __init__(self, path: str | Path = ":memory:", *, master_key: str | None = None):
        p = str(path)
        if p != ":memory:":
            Path(p).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._secrets = SecretBox(master_key) if master_key else None
        with self._lock:
            if p != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._migrate()  # bring an older database's tables up to date first
            self._conn.executescript(SCHEMA)  # then create anything missing (new tables, indexes)

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        """Columns added after the initial multi-guild schema. `CREATE TABLE IF NOT EXISTS` never alters
        an existing table, so an older database needs these added by hand. A table that does not exist
        yet is skipped here and created complete by SCHEMA. Append to this list; never remove/reorder."""
        for table, column, ddl in (
            ("guild_settings", "log_forum_channel_id", "INTEGER"),
            ("guild_settings", "master_role_id", "INTEGER"),
            ("guild_settings", "configurator_role_id", "INTEGER"),
            ("review_queue", "avatar_url", "TEXT"),
            ("guild_settings", "tag_confirmed_id", "INTEGER"),
            ("guild_settings", "tag_review_id", "INTEGER"),
            ("guild_settings", "tag_past_offender_id", "INTEGER"),
            ("guild_settings", "tag_ban_evasion_id", "INTEGER"),
            ("guild_settings", "tag_reported_id", "INTEGER"),
            ("guild_settings", "appeal_url", "TEXT"),
            ("guild_settings", "removed_at", "TEXT"),
        ):
            existing = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if not existing or column in existing:
                continue
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            log.warning("migrated: added column %s.%s", table, column)

    # ------------------------------------------------------------------ helpers
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self._exec(sql, params).fetchone()

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self._exec(sql, params).fetchall()

    def _enc(self, plaintext: str | None) -> str | None:
        if plaintext is None:
            return None
        assert self._secrets is not None, "Store was built without a master_key; cannot store secrets"
        return self._secrets.encrypt(plaintext)

    def _dec(self, token: str | None) -> str | None:
        if token is None:
            return None
        assert self._secrets is not None, "Store was built without a master_key; cannot read secrets"
        return self._secrets.decrypt(token)

    # ------------------------------------------------------------------ guild settings
    def _guild_settings(self, r: sqlite3.Row) -> GuildSettings:
        return GuildSettings(
            guild_id=r["guild_id"],
            rayward_api_key=self._dec(r["rayward_api_key_enc"]),
            bloxlink_api_key=self._dec(r["bloxlink_api_key_enc"]),
            mod_role_id=r["mod_role_id"],
            mod_channel_id=r["mod_channel_id"],
            summary_channel_id=r["summary_channel_id"],
            log_forum_channel_id=r["log_forum_channel_id"],
            tag_confirmed_id=r["tag_confirmed_id"],
            tag_review_id=r["tag_review_id"],
            tag_past_offender_id=r["tag_past_offender_id"],
            tag_ban_evasion_id=r["tag_ban_evasion_id"],
            tag_reported_id=r["tag_reported_id"],
            master_role_id=r["master_role_id"],
            configurator_role_id=r["configurator_role_id"],
            sweep_trigger_user_ids=_csv_to_ids(r["sweep_trigger_user_ids"]),
            sweep_trigger_role_id=r["sweep_trigger_role_id"],
            report_only=bool(r["report_only"]),
            dry_run=bool(r["dry_run"]),
            ban_dm_enabled=bool(r["ban_dm_enabled"]),
            appeal_url=r["appeal_url"],
            notify_starter_on_sweep_complete=bool(r["notify_starter_on_sweep_complete"]),
            setup_completed=bool(r["setup_completed"]),
            setup_by=r["setup_by"],
            setup_at=_dt(r["setup_at"]),
            updated_at=_dt(r["updated_at"]),
            updated_by=r["updated_by"],
        )

    def get_guild_settings(self, guild_id: int) -> GuildSettings | None:
        r = self._one("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
        return self._guild_settings(r) if r else None

    def get_or_create_guild_settings(self, guild_id: int) -> GuildSettings:
        existing = self.get_guild_settings(guild_id)
        if existing is not None:
            return existing
        self._exec("INSERT OR IGNORE INTO guild_settings(guild_id) VALUES (?)", (guild_id,))
        return self.get_guild_settings(guild_id)  # type: ignore[return-value]

    def update_guild_settings(self, guild_id: int, *, by: int | None, at: datetime, **fields: Any) -> GuildSettings:
        """Partial update. Recognised keys mirror GuildSettings' fields (minus guild_id/timestamps)."""
        self.get_or_create_guild_settings(guild_id)  # ensure a row exists
        columns: dict[str, Any] = {}
        if "rayward_api_key" in fields:
            columns["rayward_api_key_enc"] = self._enc(fields["rayward_api_key"])
        if "bloxlink_api_key" in fields:
            columns["bloxlink_api_key_enc"] = self._enc(fields["bloxlink_api_key"])
        for key in (
            "mod_role_id", "mod_channel_id", "summary_channel_id", "log_forum_channel_id", "sweep_trigger_role_id",
            "master_role_id", "configurator_role_id", "appeal_url",
            "tag_confirmed_id", "tag_review_id", "tag_past_offender_id", "tag_ban_evasion_id", "tag_reported_id",
        ):
            if key in fields:
                columns[key] = fields[key]
        if "sweep_trigger_user_ids" in fields:
            columns["sweep_trigger_user_ids"] = _ids_to_csv(fields["sweep_trigger_user_ids"])
        for key in (
            "report_only", "dry_run", "ban_dm_enabled",
            "notify_starter_on_sweep_complete", "setup_completed",
        ):
            if key in fields:
                columns[key] = int(bool(fields[key]))
        if "setup_by" in fields:
            columns["setup_by"] = fields["setup_by"]
            columns["setup_at"] = _ts(at)
        if not columns:
            return self.get_guild_settings(guild_id)  # type: ignore[return-value]
        columns["updated_at"] = _ts(at)
        columns["updated_by"] = by
        set_sql = ", ".join(f"{k} = ?" for k in columns)
        self._exec(f"UPDATE guild_settings SET {set_sql} WHERE guild_id = ?", (*columns.values(), guild_id))
        return self.get_guild_settings(guild_id)  # type: ignore[return-value]

    def configured_guild_ids(self) -> list[int]:
        """Every guild that has opened /setup at least once. Deliberately NOT filtered by
        setup_completed: a guild can be fully is_ready (and already handling commands/joins via
        AppRegistry.get, which checks readiness directly) without having clicked Test & Finish yet, and
        background loops (retry, sweep-resume) must still cover it - "inconclusive is never silently
        dropped" has to hold regardless of whether the admin ran diagnostics. Every caller already
        guards with `if registry.get(guild_id) is None: continue`, so an incomplete row here is harmless."""
        return [r["guild_id"] for r in self._all("SELECT guild_id FROM guild_settings")]

    # ------------------------------------------------------------------ sweeps
    @staticmethod
    def _sweep(r: sqlite3.Row) -> SweepRow:
        return SweepRow(
            id=r["id"],
            guild_id=r["guild_id"],
            status=r["status"],
            started_at=_dt(r["started_at"]),  # type: ignore[arg-type]
            finished_at=_dt(r["finished_at"]),
            started_by=r["started_by"],
            cursor_member_id=r["cursor_member_id"],
            total_members=r["total_members"],
            dry_run=bool(r["dry_run"]),
            counts=json.loads(r["counts_json"]) if r["counts_json"] else {},
            error=r["error"],
        )

    def create_sweep(self, guild_id: int, *, started_at: datetime, started_by: int | None, dry_run: bool) -> SweepRow:
        try:
            cur = self._exec(
                "INSERT INTO sweeps(guild_id, status, active, started_at, started_by, dry_run) "
                "VALUES (?, 'running', 1, ?, ?, ?)",
                (guild_id, _ts(started_at), started_by, int(dry_run)),
            )
        except sqlite3.IntegrityError as e:
            raise SweepAlreadyRunning("a sweep is already active") from e
        return self.get_sweep(guild_id, cur.lastrowid)  # type: ignore[arg-type]

    def get_sweep(self, guild_id: int, sweep_id: int) -> SweepRow:
        r = self._one("SELECT * FROM sweeps WHERE id = ? AND guild_id = ?", (sweep_id, guild_id))
        if r is None:
            raise KeyError(sweep_id)
        return self._sweep(r)

    def get_active_sweep(self, guild_id: int) -> SweepRow | None:
        r = self._one("SELECT * FROM sweeps WHERE guild_id = ? AND active = 1", (guild_id,))
        return self._sweep(r) if r else None

    def latest_sweep(self, guild_id: int) -> SweepRow | None:
        r = self._one("SELECT * FROM sweeps WHERE guild_id = ? ORDER BY id DESC LIMIT 1", (guild_id,))
        return self._sweep(r) if r else None

    def set_sweep_total(self, sweep_id: int, total: int) -> None:
        self._exec("UPDATE sweeps SET total_members = ? WHERE id = ?", (total, sweep_id))

    def set_sweep_cursor(self, sweep_id: int, last_member_id: int) -> None:
        self._exec(
            "UPDATE sweeps SET cursor_member_id = MAX(cursor_member_id, ?) WHERE id = ?",
            (last_member_id, sweep_id),
        )

    def set_sweep_status(self, sweep_id: int, status: str, *, error: str | None = None) -> None:
        active = 1 if status in ("running", "retrying") else None
        self._exec(
            "UPDATE sweeps SET status = ?, active = ?, error = COALESCE(?, error) WHERE id = ?",
            (status, active, error, sweep_id),
        )

    def finish_sweep(self, sweep_id: int, *, finished_at: datetime, status: str, counts: dict[str, int]) -> None:
        self._exec(
            "UPDATE sweeps SET status = ?, active = NULL, finished_at = ?, counts_json = ? WHERE id = ?",
            (status, _ts(finished_at), json.dumps(counts), sweep_id),
        )

    # ------------------------------------------------------------------ sweep results
    def record_sweep_result(
        self,
        sweep_id: int,
        discord_id: int,
        bucket: str,
        *,
        roblox_username: str | None,
        roblox_id: int | None,
        detail: str | None,
        at: datetime,
    ) -> None:
        self._exec(
            """INSERT INTO sweep_results(sweep_id, discord_id, bucket, roblox_username, roblox_id, detail, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sweep_id, discord_id) DO UPDATE SET
                 bucket = excluded.bucket, roblox_username = excluded.roblox_username,
                 roblox_id = excluded.roblox_id, detail = excluded.detail, updated_at = excluded.updated_at""",
            (sweep_id, discord_id, bucket, roblox_username, roblox_id, detail, _ts(at)),
        )

    def terminal_member_ids(self, sweep_id: int) -> set[int]:
        """Members whose result in this sweep is final (anything but inconclusive)."""
        rows = self._all(
            "SELECT discord_id FROM sweep_results WHERE sweep_id = ? AND bucket != 'inconclusive'", (sweep_id,)
        )
        return {r["discord_id"] for r in rows}

    def sweep_counts(self, sweep_id: int) -> Counter[str]:
        rows = self._all("SELECT bucket, COUNT(*) AS n FROM sweep_results WHERE sweep_id = ? GROUP BY bucket", (sweep_id,))
        return Counter({r["bucket"]: r["n"] for r in rows})

    # ------------------------------------------------------------------ review queue
    @staticmethod
    def _review(r: sqlite3.Row) -> ReviewRow:
        return ReviewRow(
            id=r["id"],
            guild_id=r["guild_id"],
            discord_id=r["discord_id"],
            roblox_id=r["roblox_id"],
            roblox_username=r["roblox_username"],
            provider=r["provider"],
            outcome=r["outcome"],
            status_name=r["status_name"],
            reason=r["reason"],
            nickname=r["nickname"],
            raw_response_json=r["raw_response_json"],
            summary=r["summary"],
            status=r["status"],
            channel_id=r["channel_id"],
            message_id=r["message_id"],
            created_at=_dt(r["created_at"]),  # type: ignore[arg-type]
            resolved_at=_dt(r["resolved_at"]),
            resolved_by=r["resolved_by"],
            resolution_note=r["resolution_note"],
            last_seen_at=_dt(r["last_seen_at"]),
            seen_count=r["seen_count"],
            identity_source=r["identity_source"],
            avatar_url=r["avatar_url"],
        )

    def record_report(
        self,
        guild_id: int,
        *,
        discord_id: int,
        roblox_id: int | None,
        roblox_username: str,
        provider: str,
        outcome: str,
        status_name: str,
        reason: str,
        nickname: str | None,
        raw_response_json: str,
        summary: str,
        at: datetime,
        identity_source: str = "nickname",
        avatar_url: str | None = None,
    ) -> tuple[ReviewRow, bool]:
        """Report-only mode. Returns (row, created). A repeat detection of the same member + Roblox id + status
        updates the existing row (last_seen_at, seen_count) instead of creating a new one."""
        with self._lock:
            try:
                cur = self._exec(
                    """INSERT INTO review_queue(guild_id, discord_id, roblox_id, roblox_username, provider, outcome,
                       status_name, reason, nickname, raw_response_json, summary, status, created_at, last_seen_at,
                       seen_count, identity_source, avatar_url)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reported', ?, ?, 1, ?, ?)""",
                    (guild_id, discord_id, roblox_id, roblox_username, provider, outcome, status_name, reason,
                     nickname, raw_response_json, summary, _ts(at), _ts(at), identity_source, avatar_url),
                )
                return self.get_review(guild_id, cur.lastrowid), True  # type: ignore[arg-type]
            except sqlite3.IntegrityError:
                self._exec(
                    """UPDATE review_queue SET last_seen_at = ?, seen_count = seen_count + 1, nickname = ?
                       WHERE guild_id = ? AND discord_id = ? AND COALESCE(roblox_id, -1) = COALESCE(?, -1)
                       AND status_name = ? AND status = 'reported'""",
                    (_ts(at), nickname, guild_id, discord_id, roblox_id, status_name),
                )
                r = self._one(
                    "SELECT * FROM review_queue WHERE guild_id = ? AND discord_id = ? "
                    "AND COALESCE(roblox_id, -1) = COALESCE(?, -1) AND status_name = ? AND status = 'reported'",
                    (guild_id, discord_id, roblox_id, status_name),
                )
                assert r is not None
                return self._review(r), False

    def reported(self, guild_id: int, limit: int = 50) -> list[ReviewRow]:
        return [self._review(r) for r in self._all(
            "SELECT * FROM review_queue WHERE guild_id = ? AND status = 'reported' ORDER BY last_seen_at DESC LIMIT ?",
            (guild_id, limit))]

    def unposted_reported(self, guild_id: int) -> list[ReviewRow]:
        """Every 'reported' row that never made it to Discord, regardless of age (no LIMIT - unlike
        reported() above, this backs startup recovery and a large backlog must not hide old rows)."""
        return [self._review(r) for r in self._all(
            "SELECT * FROM review_queue WHERE guild_id = ? AND status = 'reported' AND message_id IS NULL "
            "ORDER BY id",
            (guild_id,))]

    def enqueue_review(
        self,
        guild_id: int,
        *,
        discord_id: int,
        roblox_id: int | None,
        roblox_username: str,
        provider: str,
        outcome: str,
        status_name: str,
        reason: str,
        nickname: str | None,
        raw_response_json: str,
        summary: str,
        at: datetime,
        identity_source: str = "nickname",
        avatar_url: str | None = None,
    ) -> tuple[ReviewRow, bool]:
        """Returns (row, created). Never creates a second pending row for the same member + Roblox id."""
        with self._lock:
            try:
                cur = self._exec(
                    """INSERT INTO review_queue(guild_id, discord_id, roblox_id, roblox_username, provider, outcome,
                       status_name, reason, nickname, raw_response_json, summary, status, created_at, identity_source,
                       avatar_url)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                    (guild_id, discord_id, roblox_id, roblox_username, provider, outcome, status_name, reason,
                     nickname, raw_response_json, summary, _ts(at), identity_source, avatar_url),
                )
                return self.get_review(guild_id, cur.lastrowid), True  # type: ignore[arg-type]
            except sqlite3.IntegrityError:
                r = self._one(
                    "SELECT * FROM review_queue WHERE guild_id = ? AND discord_id = ? "
                    "AND COALESCE(roblox_id, -1) = COALESCE(?, -1) AND status = 'pending'",
                    (guild_id, discord_id, roblox_id),
                )
                assert r is not None
                return self._review(r), False

    def get_review(self, guild_id: int, review_id: int) -> ReviewRow | None:
        r = self._one("SELECT * FROM review_queue WHERE id = ? AND guild_id = ?", (review_id, guild_id))
        return self._review(r) if r else None

    def set_review_message(self, review_id: int, channel_id: int, message_id: int) -> None:
        self._exec("UPDATE review_queue SET channel_id = ?, message_id = ? WHERE id = ?", (channel_id, message_id, review_id))

    def pending_reviews(self, guild_id: int) -> list[ReviewRow]:
        return [self._review(r) for r in self._all(
            "SELECT * FROM review_queue WHERE guild_id = ? AND status = 'pending' ORDER BY id", (guild_id,))]

    def open_review_for(self, guild_id: int, discord_id: int) -> ReviewRow | None:
        """The member's most recent case that's still open or was posted as a report-only notice."""
        r = self._one(
            "SELECT * FROM review_queue WHERE guild_id = ? AND discord_id = ? AND status IN ('pending', 'reported') "
            "ORDER BY id DESC LIMIT 1", (guild_id, discord_id))
        return self._review(r) if r else None

    def all_reviews(self, guild_id: int) -> list[ReviewRow]:
        """Every detection ever recorded for this guild, any status (pending/approved/denied/reported)."""
        return [self._review(r) for r in self._all(
            "SELECT * FROM review_queue WHERE guild_id = ? ORDER BY id", (guild_id,))]

    def resolve_review(self, review_id: int, *, status: str, by: int, at: datetime, note: str | None = None) -> bool:
        """Atomically claim a pending row. Returns False if it was already resolved (double-click safe)."""
        cur = self._exec(
            "UPDATE review_queue SET status = ?, resolved_by = ?, resolved_at = ?, resolution_note = ? "
            "WHERE id = ? AND status = 'pending'",
            (status, by, _ts(at), note, review_id),
        )
        return cur.rowcount == 1

    def set_review_note(self, review_id: int, note: str) -> None:
        self._exec("UPDATE review_queue SET resolution_note = ? WHERE id = ?", (note, review_id))

    # ------------------------------------------------------------------ inconclusive
    @staticmethod
    def _inc(r: sqlite3.Row) -> InconclusiveRow:
        return InconclusiveRow(
            guild_id=r["guild_id"],
            discord_id=r["discord_id"],
            sweep_id=r["sweep_id"],
            roblox_username=r["roblox_username"],
            roblox_id=r["roblox_id"],
            stage=r["stage"],
            last_error=r["last_error"],
            attempts=r["attempts"],
            first_seen_at=_dt(r["first_seen_at"]),  # type: ignore[arg-type]
            next_retry_at=_dt(r["next_retry_at"]),
            exhausted=bool(r["exhausted"]),
            review_id=r["review_id"],
        )

    def get_inconclusive(self, guild_id: int, discord_id: int) -> InconclusiveRow | None:
        r = self._one("SELECT * FROM inconclusive WHERE guild_id = ? AND discord_id = ?", (guild_id, discord_id))
        return self._inc(r) if r else None

    def upsert_inconclusive(
        self,
        guild_id: int,
        *,
        discord_id: int,
        sweep_id: int | None,
        roblox_username: str | None,
        roblox_id: int | None,
        stage: str,
        last_error: str,
        attempts: int,
        now: datetime,
        next_retry_at: datetime | None,
        exhausted: bool,
        review_id: int | None = None,
    ) -> None:
        self._exec(
            """INSERT INTO inconclusive(guild_id, discord_id, sweep_id, roblox_username, roblox_id, stage,
                                        last_error, attempts, first_seen_at, next_retry_at, exhausted, review_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, discord_id) DO UPDATE SET
                 sweep_id = excluded.sweep_id, roblox_username = excluded.roblox_username, roblox_id = excluded.roblox_id,
                 stage = excluded.stage, last_error = excluded.last_error, attempts = excluded.attempts,
                 next_retry_at = excluded.next_retry_at, exhausted = excluded.exhausted,
                 review_id = COALESCE(excluded.review_id, inconclusive.review_id)""",
            (guild_id, discord_id, sweep_id, roblox_username, roblox_id, stage, last_error, attempts, _ts(now),
             _ts(next_retry_at) if next_retry_at else None, int(exhausted), review_id),
        )

    def clear_inconclusive(self, guild_id: int, discord_id: int) -> None:
        self._exec("DELETE FROM inconclusive WHERE guild_id = ? AND discord_id = ?", (guild_id, discord_id))

    def active_inconclusive(
        self, guild_id: int, *, sweep_id: int | None = None, unassigned_only: bool = False
    ) -> list[InconclusiveRow]:
        """Rows still awaiting retry (not exhausted)."""
        if sweep_id is not None:
            rows = self._all(
                "SELECT * FROM inconclusive WHERE guild_id = ? AND exhausted = 0 AND sweep_id = ? ORDER BY next_retry_at",
                (guild_id, sweep_id))
        elif unassigned_only:
            rows = self._all(
                "SELECT * FROM inconclusive WHERE guild_id = ? AND exhausted = 0 AND sweep_id IS NULL ORDER BY next_retry_at",
                (guild_id,))
        else:
            rows = self._all(
                "SELECT * FROM inconclusive WHERE guild_id = ? AND exhausted = 0 ORDER BY next_retry_at", (guild_id,))
        return [self._inc(r) for r in rows]

    # ------------------------------------------------------------------ audit + bans
    def write_audit(
        self,
        guild_id: int,
        *,
        discord_id: int,
        roblox_id: int | None,
        roblox_username: str | None,
        nickname_at_ban: str | None,
        provider: str | None,
        status_name: str | None,
        raw_response_json: str,
        decision_path: str,
        approved_by: int | None,
        dry_run: bool,
        at: datetime,
    ) -> int:
        cur = self._exec(
            """INSERT INTO audit_log(guild_id, discord_id, roblox_id, roblox_username, nickname_at_ban, provider,
                                     status_name, raw_response_json, decision_path, approved_by, dry_run, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, discord_id, roblox_id, roblox_username, nickname_at_ban, provider, status_name,
             raw_response_json, decision_path, approved_by, int(dry_run), _ts(at)),
        )
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def set_audit_result(self, audit_id: int, *, succeeded: bool, error: str | None) -> None:
        self._exec("UPDATE audit_log SET ban_succeeded = ?, ban_error = ? WHERE id = ?", (int(succeeded), error, audit_id))

    def set_audit_dm(self, audit_id: int, *, sent: bool, error: str | None) -> None:
        self._exec("UPDATE audit_log SET dm_sent = ?, dm_error = ? WHERE id = ?", (int(sent), error, audit_id))

    def mark_banned(self, guild_id: int, discord_id: int, roblox_id: int | None, audit_id: int, at: datetime) -> None:
        self._exec(
            "INSERT OR REPLACE INTO bans_applied(guild_id, discord_id, roblox_id, audit_id, banned_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, discord_id, roblox_id, audit_id, _ts(at)),
        )

    @staticmethod
    def _ban_applied(r: sqlite3.Row) -> BanAppliedRow:
        return BanAppliedRow(
            guild_id=r["guild_id"], discord_id=r["discord_id"], roblox_id=r["roblox_id"],
            audit_id=r["audit_id"], banned_at=_dt(r["banned_at"]),  # type: ignore[arg-type]
        )

    def prior_ban_for_roblox_id(
        self, guild_id: int, roblox_id: int, *, exclude_discord_id: int
    ) -> BanAppliedRow | None:
        """Ban-evasion lookup: has this Roblox account already been banned in this server, under some
        OTHER Discord account? Used to catch the same person rejoining on a fresh account."""
        r = self._one(
            "SELECT * FROM bans_applied WHERE guild_id = ? AND roblox_id = ? AND discord_id != ? "
            "ORDER BY banned_at LIMIT 1",
            (guild_id, roblox_id, exclude_discord_id),
        )
        return self._ban_applied(r) if r else None

    # ------------------------------------------------------------------ daily API budgets
    def usage_count(self, guild_id: int, api: str, day: str) -> int:
        r = self._one("SELECT count FROM api_usage WHERE guild_id = ? AND api = ? AND day = ?", (guild_id, api, day))
        return int(r["count"]) if r else 0

    def usage_increment(self, guild_id: int, api: str, day: str, n: int = 1) -> int:
        with self._lock:
            self._exec(
                """INSERT INTO api_usage(guild_id, api, day, count) VALUES (?, ?, ?, ?)
                   ON CONFLICT(guild_id, api, day) DO UPDATE SET count = count + excluded.count""",
                (guild_id, api, day, n),
            )
            return self.usage_count(guild_id, api, day)

    # ------------------------------------------------------------------ deletion (see the privacy policy)
    def mark_guild_removed(self, guild_id: int, at: datetime) -> None:
        """The bot left this guild. Wipe its API keys straight away and start the clock on deleting the rest."""
        self._exec(
            "UPDATE guild_settings SET removed_at = ?, rayward_api_key_enc = NULL, bloxlink_api_key_enc = NULL, "
            "setup_completed = 0 WHERE guild_id = ?", (_ts(at), guild_id))

    def clear_guild_removed(self, guild_id: int) -> None:
        """The bot was added back. Its settings (but not its wiped keys) are kept."""
        self._exec("UPDATE guild_settings SET removed_at = NULL WHERE guild_id = ?", (guild_id,))

    def guilds_removed_before(self, cutoff: datetime) -> list[int]:
        return [r["guild_id"] for r in self._all(
            "SELECT guild_id FROM guild_settings WHERE removed_at IS NOT NULL AND removed_at < ?", (_ts(cutoff),))]

    def purge_guild(self, guild_id: int, *, dry_run: bool = False) -> dict[str, int]:
        """Delete everything stored about one guild. Returns rows per table; with dry_run, only counts them."""
        steps = [
            ("sweep_results", "FROM sweep_results WHERE sweep_id IN (SELECT id FROM sweeps WHERE guild_id = ?)"),
            *[(t, f"FROM {t} WHERE guild_id = ?") for t in (
                "sweeps", "review_queue", "inconclusive", "audit_log", "bans_applied", "api_usage", "guild_settings")],
        ]
        with self._transaction(dry_run) as conn:
            return {label: self._count_and_delete(conn, label, frm, guild_id, dry_run) for label, frm in steps}

    def purge_user(self, discord_id: int, *, dry_run: bool = False) -> dict[str, int]:
        """Delete everything stored about one Discord user, in every guild: their checks, cases, bans and
        audit rows. Where they only appear as the moderator behind a decision, the reference is removed
        and the row stays. Returns rows per table; with dry_run, only counts them."""
        deletes = [(t, f"FROM {t} WHERE discord_id = ?") for t in (
            "sweep_results", "review_queue", "inconclusive", "audit_log", "bans_applied")]
        # (label, rows to change, the change)
        anonymise = [
            ("cases they decided", "SELECT COUNT(*) FROM review_queue WHERE resolved_by = ?",
             "UPDATE review_queue SET resolved_by = NULL, resolution_note = NULL WHERE resolved_by = ?"),
            ("bans they approved", "SELECT COUNT(*) FROM audit_log WHERE approved_by = ?",
             "UPDATE audit_log SET approved_by = NULL WHERE approved_by = ?"),
            ("sweeps they started", "SELECT COUNT(*) FROM sweeps WHERE started_by = ?",
             "UPDATE sweeps SET started_by = NULL WHERE started_by = ?"),
            ("settings they changed", "SELECT COUNT(*) FROM guild_settings WHERE setup_by = ? OR updated_by = ?",
             "UPDATE guild_settings SET setup_by = CASE WHEN setup_by = ? THEN NULL ELSE setup_by END, "
             "updated_by = CASE WHEN updated_by = ? THEN NULL ELSE updated_by END WHERE setup_by = ? OR updated_by = ?"),
        ]
        out: dict[str, int] = {}
        with self._transaction(dry_run) as conn:
            for label, frm in deletes:
                out[label] = self._count_and_delete(conn, label, frm, discord_id, dry_run)
            for label, count_sql, change_sql in anonymise:
                out[label] = conn.execute(count_sql, (discord_id,) * count_sql.count("?")).fetchone()[0]
                if out[label] and not dry_run:
                    conn.execute(change_sql, (discord_id,) * change_sql.count("?"))
            triggers = 0
            for r in conn.execute("SELECT guild_id, sweep_trigger_user_ids FROM guild_settings "
                                  "WHERE sweep_trigger_user_ids != ''").fetchall():
                ids = _csv_to_ids(r["sweep_trigger_user_ids"])
                if discord_id in ids:
                    triggers += 1
                    if not dry_run:
                        conn.execute("UPDATE guild_settings SET sweep_trigger_user_ids = ? WHERE guild_id = ?",
                                     (_ids_to_csv(ids - {discord_id}), r["guild_id"]))
            out["sweep trigger lists"] = triggers
        return out

    @staticmethod
    def _count_and_delete(conn: sqlite3.Connection, label: str, frm: str, param: int, dry_run: bool) -> int:
        n = conn.execute(f"SELECT COUNT(*) {frm}", (param,)).fetchone()[0]
        if n and not dry_run:
            conn.execute(f"DELETE {frm}", (param,))
        return n

    @contextlib.contextmanager
    def _transaction(self, dry_run: bool):
        """All-or-nothing, so a failed purge never leaves half a guild or user behind."""
        with self._lock:
            if not dry_run:
                self._conn.execute("BEGIN")
            try:
                yield self._conn
                if not dry_run:
                    self._conn.execute("COMMIT")
            except Exception:
                if not dry_run:
                    self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ retention (Rotector ToS: raw data <= 24h)
    def redact_raw_older_than(self, cutoff: datetime, now: datetime) -> tuple[int, list[tuple[int, int]]]:
        """Rayward's terms: data from its API must not be kept for more than 24 hours. Across every guild,
        replace raw provider JSON older than `cutoff` with the bare status, and delete the Rotector reasons
        stored as a case's summary. What remains is Collapse's record of its own decision.

        Returns (rows touched, [(channel_id, message_id)] of posted cases whose Discord message still shows
        the reasons and needs its Details field removed)."""
        touched = 0
        to_strip: list[tuple[int, int]] = []
        for table in ("audit_log", "review_queue"):
            extra = ", channel_id, message_id" if table == "review_queue" else ""
            rows = self._all(
                f"SELECT id, raw_response_json{extra} FROM {table} WHERE raw_redacted_at IS NULL AND created_at < ?",
                (_ts(cutoff),),
            )
            for r in rows:
                reduced = _reduce_raw(r["raw_response_json"], now)
                if table == "review_queue":
                    self._exec(
                        "UPDATE review_queue SET raw_response_json = ?, raw_redacted_at = ?, summary = '' WHERE id = ?",
                        (reduced, _ts(now), r["id"]),
                    )
                    if r["channel_id"] and r["message_id"]:
                        to_strip.append((r["channel_id"], r["message_id"]))
                else:
                    self._exec(
                        "UPDATE audit_log SET raw_response_json = ?, raw_redacted_at = ? WHERE id = ?",
                        (reduced, _ts(now), r["id"]),
                    )
                touched += 1
        return touched, to_strip


def _reduce_raw(raw_json: str, now: datetime) -> str:
    try:
        raw = json.loads(raw_json)
    except (TypeError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        return json.dumps({"redacted_at": now.isoformat(), "note": "raw response removed per provider retention terms"})
    reduced: dict[str, Any] = {
        "redacted_at": now.isoformat(),
        "note": "Rotector details removed after 24 hours (Rayward terms); only the status is kept for the audit trail",
    }
    for key in ("id", "flagType", "statusLabel", "error", "provider", "status"):
        if key in raw:
            reduced[key] = raw[key]
    return json.dumps(reduced, ensure_ascii=False, sort_keys=True)


def retention_cutoff(now: datetime, hours: int, margin_s: float = 0) -> datetime:
    """Rows created before this are due for clearing. `margin_s` is how often the clearing runs: subtracting it
    means nothing can outlive `hours` while it waits for the next run."""
    return now - timedelta(hours=hours, seconds=-margin_s)
