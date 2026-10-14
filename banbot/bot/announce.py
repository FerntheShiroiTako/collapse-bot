"""/announce: the bot owner pushes one message to every server Collapse is in, from a DM with the bot.

Each server gets it in its mod channel; a server with no usable mod channel gets it in its system channel,
or failing that the first text channel the bot can post in. The owner writes the text in a modal, sees a
preview, and nothing is sent until they press Send.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import discord

from banbot import brand
from banbot.settings.config import DEVELOPER_ID
from banbot.storage.store import Store
from banbot.util import utcnow

log = logging.getLogger(__name__)

STYLE_EMBED = "embed"
STYLE_PLAIN = "plain"
# Modal input caps at 4000; a plain message at 2000, less room for a role mention and a line break.
MAX_LEN = {STYLE_EMBED: 4000, STYLE_PLAIN: 1970}
SEND_DELAY_S = 1.0  # between servers, to stay well clear of Discord's rate limits


def build_announcement(text: str, style: str, *, ping: str | None = None,
                       ping_role: discord.Role | None = None) -> dict:
    """kwargs for Messageable.send. `ping` goes on the first line, and only `ping_role` can actually be
    pinged, so nothing typed in the text itself (@everyone, roles, users) ever pings anyone."""
    allowed = discord.AllowedMentions(everyone=False, users=False, roles=[ping_role] if ping_role else False)
    kwargs: dict = {"allowed_mentions": allowed}
    if style == STYLE_EMBED:
        kwargs["embed"] = discord.Embed(title=f"Announcement from the {brand.NAME} developer", description=text,
                                        color=brand.SAGE, timestamp=utcnow())
        if ping:
            kwargs["content"] = ping
    else:
        kwargs["content"] = f"{ping}\n{text}" if ping else text
    return kwargs


def _can_post(ch: discord.abc.GuildChannel | None, me: discord.Member | None) -> bool:
    if not isinstance(ch, discord.TextChannel) or me is None:
        return False
    perms = ch.permissions_for(me)
    return perms.view_channel and perms.send_messages and perms.embed_links


def fallback_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """The system channel, else the first text channel the bot can post in (as the welcome message does)."""
    for ch in [guild.system_channel, *guild.text_channels]:
        if _can_post(ch, guild.me):
            return ch  # type: ignore[return-value]
    return None


@dataclass
class BroadcastResult:
    mod_channel: int = 0
    fallback: int = 0
    failed: list[str] = field(default_factory=list)
    no_ping: list[str] = field(default_factory=list)  # sent, but the mod role couldn't be pinged there


def _mod_role(guild: discord.Guild, role_id: int | None) -> tuple[discord.Role | None, str | None]:
    """(role to mention, or None) and why it won't actually ping, if it won't."""
    role = guild.get_role(role_id) if role_id else None
    if role is None:
        return None, "mod role no longer exists" if role_id else "no mod role set"
    me = guild.me
    if not role.mentionable and not (me and me.guild_permissions.mention_everyone):
        return role, "mod role isn't mentionable and the bot lacks Mention @everyone"
    return role, None


async def broadcast(guilds: list[discord.Guild], store: Store, text: str, style: str, *,
                    ping_mod_role: bool = False) -> BroadcastResult:
    out = BroadcastResult()
    for i, guild in enumerate(guilds):
        if i:
            await asyncio.sleep(SEND_DELAY_S)
        settings = store.get_guild_settings(guild.id)
        mod_ch = guild.get_channel(settings.mod_channel_id) if settings and settings.mod_channel_id else None
        target = mod_ch if _can_post(mod_ch, guild.me) else fallback_channel(guild)  # type: ignore[arg-type]
        if target is None:
            out.failed.append(f"{guild.name} ({guild.id}): no channel the bot can post in")
            continue
        role, why_not = _mod_role(guild, settings.mod_role_id if settings else None) if ping_mod_role else (None, None)
        try:
            await target.send(**build_announcement(text, style, ping=role.mention if role else None, ping_role=role))
        except discord.HTTPException as e:
            out.failed.append(f"{guild.name} ({guild.id}): {e}")
            continue
        if why_not:
            out.no_ping.append(f"{guild.name} ({guild.id}): {why_not}")
        if target is mod_ch:
            out.mod_channel += 1
        else:
            out.fallback += 1
    log.warning("announcement sent: %d mod channels, %d fallback channels, %d failed, %d without the role ping",
                out.mod_channel, out.fallback, len(out.failed), len(out.no_ping))
    return out


class AnnounceModal(discord.ui.Modal, title="Announcement to every server"):
    def __init__(self, style: str, guilds: Callable[[], list[discord.Guild]], store: Store, *, ping_mod_role: bool):
        super().__init__()
        self.style, self.guilds, self.store, self.ping_mod_role = style, guilds, store, ping_mod_role
        self.text = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph,
                                         max_length=MAX_LEN[style])
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        n = len(self.guilds())
        view = ConfirmView(self.text.value, self.style, self.guilds, self.store, ping_mod_role=self.ping_mod_role)
        ping_note = " Each server's mod role is pinged (shown as **@Mod role** here)." if self.ping_mod_role else ""
        await interaction.response.send_message(
            f"Preview below. **Send** posts it to {n} server{'s' if n != 1 else ''}.{ping_note}",
            allowed_mentions=discord.AllowedMentions.none())
        preview = build_announcement(self.text.value, self.style, ping="**@Mod role**" if self.ping_mod_role else None)
        view.message = await interaction.followup.send(**preview, view=view, wait=True)


class ConfirmView(discord.ui.View):
    def __init__(self, text: str, style: str, guilds: Callable[[], list[discord.Guild]], store: Store, *,
                 ping_mod_role: bool):
        super().__init__(timeout=600)
        self.text, self.style, self.guilds, self.store, self.ping_mod_role = text, style, guilds, store, ping_mod_role
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == DEVELOPER_ID

    async def _close(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=None)

    @discord.ui.button(label="Send", style=discord.ButtonStyle.danger)
    async def send(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._close(interaction)
        guilds = self.guilds()
        await interaction.followup.send(f"Sending to {len(guilds)} server(s)...")
        r = await broadcast(guilds, self.store, self.text, self.style, ping_mod_role=self.ping_mod_role)
        lines = [f"Sent. Mod channel: **{r.mod_channel}** · fallback channel: **{r.fallback}** · "
                 f"failed: **{len(r.failed)}**"]
        lines += _listing("Failed", r.failed)
        if self.ping_mod_role:
            lines += _listing("Sent without the mod role ping", r.no_ping)
        await interaction.followup.send("\n".join(lines)[:2000], allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._close(interaction)
        await interaction.followup.send("Cancelled. Nothing was sent.")

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


def _listing(heading: str, items: list[str], limit: int = 10) -> list[str]:
    """A capped bullet list for the result message; every item also goes to the log."""
    if not items:
        return []
    for item in items:
        log.warning("announcement: %s: %s", heading.lower(), item)
    out = [f"**{heading}:**", *[f"- {i}" for i in items[:limit]]]
    if len(items) > limit:
        out.append(f"...and {len(items) - limit} more (see the logs)")
    return out
