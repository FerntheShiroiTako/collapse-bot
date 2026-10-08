"""Live checks against a guild's configured keys and Discord permissions: what /setup's "Test & Finish"
button reports. Plain functions returning CheckResult; setup_panel.py renders them.
"""
from __future__ import annotations

from dataclasses import dataclass

import discord

from banbot.integrations.bloxlink import BloxlinkClient, BloxlinkFailure
from banbot.settings.config import FORUM_TAG_CATEGORIES, Config
from banbot.core.flags import CombinedProvider, FlagProvider, source_label

ROBLOX_TEST_USER_ID = 1  # the "Roblox" system account - always exists, safe to look up
DISCORD_TEST_USER_ID = 1  # not a real linkable account; any non-auth answer proves the key works


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


async def check_rayward(provider: CombinedProvider) -> list[CheckResult]:
    """One check per source, so a key that works for Rotector but isn't approved for another source says so."""
    return [await _check_rayward_source(source) for source in provider.sources]


async def _check_rayward_source(provider: FlagProvider) -> CheckResult:
    name = f"Rayward API key ({source_label(provider.name)})"
    try:
        results = await provider.lookup([ROBLOX_TEST_USER_ID])
    except Exception as e:
        return CheckResult(name, False, f"{type(e).__name__}: {e}")
    r = results.get(ROBLOX_TEST_USER_ID)
    if r is None:
        return CheckResult(name, False, "no response for the test lookup")
    if r.outcome.value != "inconclusive":
        return CheckResult(name, True, "connected")
    low = r.detail.lower()
    if "401" in low or "403" in low or "auth" in low:
        return CheckResult(name, False, r.detail)
    return CheckResult(name, False, f"test lookup failed: {r.detail}")


async def check_bloxlink(bloxlink: BloxlinkClient | None) -> CheckResult | None:
    if bloxlink is None:
        return None
    try:
        result = await bloxlink.lookup(DISCORD_TEST_USER_ID)
    except Exception as e:
        return CheckResult("Bloxlink API key", False, f"{type(e).__name__}: {e}")
    if isinstance(result, BloxlinkFailure):
        return CheckResult("Bloxlink API key", False, result.detail)
    return CheckResult("Bloxlink API key", True, "connected")


def check_discord_permissions(guild: discord.Guild, cfg: Config) -> list[CheckResult]:
    out: list[CheckResult] = []
    me = guild.me
    if me is None:
        return [CheckResult("Bot permissions", False, "bot member not cached yet; try again in a moment")]

    perms = me.guild_permissions
    out.append(CheckResult(
        "Ban Members permission", perms.ban_members,
        "" if perms.ban_members else "grant Ban Members to the bot's role",
    ))

    mod_role = guild.get_role(cfg.mod_role_id)
    if mod_role is None:
        out.append(CheckResult("Mod role", False, "role not found (deleted since /setup?)"))
    elif me.top_role <= mod_role:
        out.append(CheckResult(
            "Role position", False, "the bot's own role must be positioned above the mod role to ban its members",
        ))
    else:
        out.append(CheckResult("Role position", True))

    out.append(_check_channel(guild, me, cfg.mod_channel_id, "Mod channel"))
    if cfg.summary_channel_id and cfg.summary_channel_id != cfg.mod_channel_id:
        out.append(_check_channel(guild, me, cfg.summary_channel_id, "Summary channel"))
    if cfg.log_forum_channel_id:
        out.append(_check_channel(guild, me, cfg.log_forum_channel_id, "Detection log forum"))
        out.append(_check_forum_tags(guild, cfg))
    return out


def _check_forum_tags(guild: discord.Guild, cfg: Config) -> CheckResult:
    ch = guild.get_channel(cfg.log_forum_channel_id)
    if not isinstance(ch, discord.ForumChannel):
        return CheckResult("Detection log tags", False, "forum channel not accessible")
    # A forum set to require tags rejects any post without one, so every category needs a tag mapped.
    if ch.flags.require_tag and len(cfg.forum_tag_ids) < len(FORUM_TAG_CATEGORIES):
        return CheckResult(
            "Detection log tags", False,
            "the forum requires a tag on every post: map a tag for every category in /config → More Settings "
            "→ Detection Log Tags, or turn off Require Tags on the forum",
        )
    if not cfg.forum_tag_ids:
        return CheckResult("Detection log tags", True, "none mapped, so threads are posted without tags")
    available = {t.id for t in ch.available_tags}
    missing = [category for category, tag_id in cfg.forum_tag_ids.items() if tag_id not in available]
    if missing:
        return CheckResult(
            "Detection log tags", False,
            f"tag(s) mapped for {', '.join(missing)} no longer exist on the forum. Map them again in /config",
        )
    return CheckResult("Detection log tags", True, f"{len(cfg.forum_tag_ids)}/5 categories mapped")


def _check_channel(guild: discord.Guild, me: discord.Member, channel_id: int, label: str) -> CheckResult:
    ch = guild.get_channel(channel_id)
    if ch is None:
        return CheckResult(label, False, "channel not found (deleted, or the bot cannot see it)")
    perms = ch.permissions_for(me)
    ok = perms.view_channel and perms.send_messages and perms.embed_links and perms.read_message_history
    if ok:
        return CheckResult(label, True)
    missing = [n for n, has in (
        ("View Channel", perms.view_channel), ("Send Messages", perms.send_messages), ("Embed Links", perms.embed_links),
        ("Read Message History", perms.read_message_history),
    ) if not has]
    return CheckResult(label, False, f"bot needs: {', '.join(missing)}")
