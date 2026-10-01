"""Command-line entry point.

    python -m banbot                          run the bot
    python -m banbot genkey                   print a new MASTER_KEY
    python -m banbot purge-user <discord id>  delete everything stored about one Discord user
    python -m banbot purge-guild <guild id>   delete everything stored about one server
Both purge commands only list what they would delete until you add --yes. They can run while the bot is running.

Relative paths (.env, MESSAGES_FILE, DB_PATH) resolve against the bot's own folder, not the current
directory, so the bot behaves the same whether it's started from a terminal, a .bat file or systemd.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from banbot.bot.client import run
from banbot.settings import messages
from banbot.settings.config import ConfigError, GlobalConfig
from banbot.storage.crypto import CryptoError, SecretBox, generate_key
from banbot.storage.store import Store

BOT_DIR = Path(__file__).resolve().parent.parent


def _resolve(path: str) -> str:
    p = Path(path)
    return str(p if p.is_absolute() else BOT_DIR / p)


def _purge(command: str, ident: int, *, confirmed: bool) -> int:
    load_dotenv(BOT_DIR / ".env")
    db = Path(_resolve(os.environ.get("DB_PATH", "data/banbot.sqlite3")))
    if not db.exists():
        print(f"no database at {db}", file=sys.stderr)
        return 2
    store = Store(db)  # no master key needed: nothing here reads a secret
    try:
        run_purge = store.purge_user if command == "purge-user" else store.purge_guild
        counts = run_purge(ident, dry_run=not confirmed)
    finally:
        store.close()
    what = "user" if command == "purge-user" else "server"
    print(f"{'Deleted' if confirmed else 'Would delete'} for {what} {ident}:")
    for label, n in counts.items():
        print(f"  {label}: {n}")
    if not confirmed:
        print("Nothing was changed. Run it again with --yes to delete.")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["genkey"]:
        print(generate_key())
        return 0
    if len(argv) >= 2 and argv[0] in ("purge-user", "purge-guild") and argv[1].isdigit() and set(argv[2:]) <= {"--yes"}:
        return _purge(argv[0], int(argv[1]), confirmed="--yes" in argv)
    if argv:
        print(__doc__, file=sys.stderr)
        return 2

    load_dotenv(BOT_DIR / ".env")
    # Windows consoles default to cp1252, which mangles the bullets/emoji in summaries and embeds.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)

    try:
        global_cfg = GlobalConfig.from_env()
        SecretBox(global_cfg.master_key)
    except (ConfigError, CryptoError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    global_cfg = dataclasses.replace(global_cfg, db_path=_resolve(global_cfg.db_path))

    texts = messages.load(_resolve(os.environ.get("MESSAGES_FILE", "messages.txt")))

    # systemd stops services with SIGTERM; treat it like Ctrl+C so discord.py shuts down cleanly.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    run(global_cfg, texts=texts)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
