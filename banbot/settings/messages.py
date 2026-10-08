"""Editable message text: the welcome post and the ban DM.

The texts live in one plain file (MESSAGES_FILE, default messages.txt), split into sections that each
start with their name in square brackets on a line of its own:

    [welcome]
    Thanks for adding **Collapse**! ...

    [ban_dm]
    You have been removed from {server}. ...

A header must be exactly `[name]` on its own line. Everything up to the next header is that message,
trimmed of surrounding blank lines; anything above the first header is ignored, so it's free for notes.
Unknown section names are ignored with a warning. The file is read once at startup, so restart the bot
after editing it. A missing or blank section falls back to the built-in text below.

Ban DM placeholders are filled per ban; an unknown placeholder is left as-is rather than crashing the ban.
`{appeal}` becomes a line pointing at the server's own appeal link (set in /config -> More Settings), or
disappears when the server hasn't set one. A template without `{appeal}` still gets the line appended,
so a server's appeal link is never silently dropped.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

KNOWN_SECTIONS = ("welcome", "ban_dm", "ban_dm_other", "ban_dm_evasion")
_HEADER = re.compile(r"^\[([a-z_]+)\]\s*$")

DEFAULT_WELCOME = (
    "Thanks for adding **Collapse**! It checks members' linked Roblox accounts against Rayward's flag sources "
    "and never bans anyone without a moderator pressing a button.\n\n"
    "An admin with **Manage Server** needs to run **/setup** to connect this server's own Rayward "
    "(and optionally Bloxlink) API key, pick a mod role/channel, and choose who can run sweeps. "
    "Nothing happens until setup is complete."
)

# Rayward's terms require every action taken on its data to name the source, so the member knows who
# to appeal to. Each source's bans name that source; ban-evasion bans come from Collapse's own records instead.
DEFAULT_BAN_DM = (
    "You have been removed from {server}.\n\n"
    "The Roblox account linked to your Discord profile ({roblox_username}) is listed as \"{status}\" by "
    "Rotector, which this server uses to screen members. If you think the listing is wrong, you can "
    "appeal it with Rotector at https://rotector.com.\n\n"
    "{appeal}"
)

# Every other Rayward source (RCR, TASE, RAB, ...): {source} and {source_appeal} name the one this ban is
# based on (RAYWARD_SOURCES in core/flags.py).
DEFAULT_BAN_DM_OTHER = (
    "You have been removed from {server}.\n\n"
    "The Roblox account linked to your Discord profile ({roblox_username}) is listed as \"{status}\" by "
    "{source}, which this server uses to screen members. If you think the listing is wrong, you can "
    "appeal it with {source} at {source_appeal}.\n\n"
    "{appeal}"
)

DEFAULT_BAN_DM_EVASION = (
    "You have been removed from {server}.\n\n"
    "The Roblox account linked to your Discord profile ({roblox_username}, ID {roblox_id}) was banned "
    "from this server before, under a different Discord account.\n\n"
    "{appeal}"
)


def parse(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            current = sections.setdefault(m.group(1), [])
        elif current is not None:
            current.append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def load(path: str | Path | None) -> dict[str, str]:
    """Read the messages file. Missing file or blank sections are simply absent from the result; each
    caller decides its own fallback."""
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        log.warning("MESSAGES_FILE %s does not exist; using built-in messages", p)
        return {}
    found = parse(p.read_text(encoding="utf-8"))
    for name in found.keys() - set(KNOWN_SECTIONS):
        log.warning("MESSAGES_FILE %s: unknown section [%s] ignored (known: %s)", p, name, ", ".join(KNOWN_SECTIONS))
    return {name: text for name, text in found.items() if name in KNOWN_SECTIONS and text}


# ---------------------------------------------------------------------- ban DM

APPEAL_MAX_LEN = 200
_INVITE_HOSTS = ("discord.gg/", "discord.com/invite/", "www.discord.com/invite/")


class _Safe(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def normalize_appeal_url(raw: str | None) -> str | None:
    """Validate an appeal link typed into /config. Blank clears it. Accepts http(s) URLs, and Discord
    invites typed without a scheme (`discord.gg/abc`). Raises ValueError with a user-facing message."""
    value = (raw or "").strip()
    if not value:
        return None
    if value.lower().startswith(_INVITE_HOSTS):
        value = "https://" + value
    if not re.fullmatch(r"https?://[^\s<>]+\.[^\s<>]+", value, flags=re.IGNORECASE):
        raise ValueError(
            "That doesn't look like a link. Use a full URL (https://...) or a Discord invite (discord.gg/...).")
    if len(value) > APPEAL_MAX_LEN:
        raise ValueError(f"That link is too long (max {APPEAL_MAX_LEN} characters).")
    return value


def appeal_text(appeal_url: str | None) -> str:
    return f"You can appeal this ban here: {appeal_url}" if appeal_url else ""


def render_ban_dm(template: str, *, appeal_url: str | None = None, **values: object) -> str:
    if appeal_url and "{appeal}" not in template:
        template = template.rstrip() + "\n\n{appeal}"
    values["appeal"] = appeal_text(appeal_url)
    text = template.format_map(_Safe({k: "" if v is None else v for k, v in values.items()}))
    # An empty {appeal} on its own line would leave a gap; collapse runs of blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:2000]
