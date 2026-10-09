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
from banbot.settings.config import ANNOUNCER_ID
from banbot.storage.store import Store
from banbot.util import utcnow

log = logging.getLogger(__name__)

STYLE_EMBED = "embed"
STYLE_PLAIN = "plain"
MAX_LEN = {STYLE_EMBED: 4000, STYLE_PLAIN: 2000}  # modal input caps at 4000; a plain message at 2000
SEND_DELAY_S = 1.0  # between servers, to stay well clear of Discord's rate limits


def build_announcement(text: str, style: str) -> dict:
    """kwargs for Messageable.send. Mentions are always off, so nothing in the text can ping anyone."""
    kwargs: dict = {"allowed_mentions": discord.AllowedMentions.none()}
    if style == STYLE_EMBED:
        e = discord.Embed(title=f"Announcement from the {brand.NAME} developer", description=text,
                          color=brand.SAGE, timestamp=utcnow())
        kwargs["embed"] = e
    else:
        kwargs["content"] = text
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


async def broadcast(guilds: list[discord.Guild], store: Store, text: str, style: str) -> BroadcastResult:
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
        try:
            await target.send(**build_announcement(text, style))
        except discord.HTTPException as e:
            out.failed.append(f"{guild.name} ({guild.id}): {e}")
            continue
        if target is mod_ch:
            out.mod_channel += 1
        else:
            out.fallback += 1
    log.warning("announcement sent: %d mod channels, %d fallback channels, %d failed",
                out.mod_channel, out.fallback, len(out.failed))
    return out


class AnnounceModal(discord.ui.Modal, title="Announcement to every server"):
    def __init__(self, style: str, guilds: Callable[[], list[discord.Guild]], store: Store):
        super().__init__()
        self.style, self.guilds, self.store = style, guilds, store
        self.text = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph,
                                         max_length=MAX_LEN[style])
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        n = len(self.guilds())
        view = ConfirmView(self.text.value, self.style, self.guilds, self.store)
        await interaction.response.send_message(
            f"Preview below. **Send** posts it to {n} server{'s' if n != 1 else ''}.",
            allowed_mentions=discord.AllowedMentions.none())
        view.message = await interaction.followup.send(**build_announcement(self.text.value, self.style), view=view,
                                                       wait=True)


class ConfirmView(discord.ui.View):
    def __init__(self, text: str, style: str, guilds: Callable[[], list[discord.Guild]], store: Store):
        super().__init__(timeout=600)
        self.text, self.style, self.guilds, self.store = text, style, guilds, store
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == ANNOUNCER_ID

    async def _close(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=None)

    @discord.ui.button(label="Send", style=discord.ButtonStyle.danger)
    async def send(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._close(interaction)
        guilds = self.guilds()
        await interaction.followup.send(f"Sending to {len(guilds)} server(s)...")
        r = await broadcast(guilds, self.store, self.text, self.style)
        lines = [f"Sent. Mod channel: **{r.mod_channel}** · fallback channel: **{r.fallback}** · "
                 f"failed: **{len(r.failed)}**"]
        lines += [f"- {f}" for f in r.failed[:20]]
        if len(r.failed) > 20:
            lines.append(f"...and {len(r.failed) - 20} more (see the logs)")
            for f in r.failed[20:]:
                log.warning("announcement failed: %s", f)
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
