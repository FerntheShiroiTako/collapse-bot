"""discord.py adapters: Gateway (members / live nickname / ban / messages) and ReviewPoster (embeds + buttons).

Everything discord.py-specific is confined to this module and bot.py.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import discord

from banbot import brand
from banbot.core.flags import FlagOutcome, source_label
from banbot.core.gateway import BanError, MemberInfo, MemberNotFound
from banbot.core.review import REASON_BAN_EVASION
from banbot.storage.store import ReviewRow

if TYPE_CHECKING:
    from banbot.app import App, AppRegistry

log = logging.getLogger(__name__)

CUSTOM_ID_PREFIX = "banbot:review:"


def nickname_of(member: discord.Member) -> str | None:
    """The name this member shows in the server: server nickname, else global display name.

    A bare Discord username can never contain "(@...)" so the fallback cannot create false matches.
    """
    return member.nick or member.global_name


class DiscordGateway:
    def __init__(self, client: discord.Client, guild_id: int):
        self._client = client
        self._guild_id = guild_id

    def _guild(self) -> discord.Guild:
        g = self._client.get_guild(self._guild_id)
        if g is None:
            raise RuntimeError(f"bot is not in guild {self._guild_id} (or the guild is not cached yet)")
        return g

    async def list_members(self) -> list[MemberInfo]:
        out: list[MemberInfo] = []
        async for m in self._guild().fetch_members(limit=None):
            if m.bot:
                continue
            out.append(MemberInfo(m.id, nickname_of(m)))
        return out

    async def fetch_nickname(self, discord_id: int) -> str | None:
        try:
            m = await self._guild().fetch_member(discord_id)  # always hits the API, never the cache
        except discord.NotFound as e:
            raise MemberNotFound(str(discord_id)) from e
        return nickname_of(m)

    async def ban(self, discord_id: int, *, reason: str) -> None:
        try:
            await self._guild().ban(discord.Object(id=discord_id), reason=reason, delete_message_seconds=0)
        except (discord.Forbidden, discord.HTTPException) as e:
            raise BanError(f"{type(e).__name__}: {e}") from e

    async def _channel(self, channel_id: int) -> discord.abc.Messageable | None:
        ch = self._client.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self._client.fetch_channel(channel_id)
            except discord.HTTPException:
                log.error("channel %s not found / not accessible", channel_id)
                return None
        if not isinstance(ch, discord.abc.Messageable):
            log.error("channel %s is not a text channel", channel_id)
            return None
        return ch

    async def _forum_channel(self, channel_id: int) -> discord.ForumChannel | None:
        """Forum channels aren't Messageable - you create a thread (a "post") in them, you don't send()."""
        ch = self._client.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self._client.fetch_channel(channel_id)
            except discord.HTTPException:
                log.error("forum channel %s not found / not accessible", channel_id)
                return None
        if not isinstance(ch, discord.ForumChannel):
            log.error("channel %s is not a forum channel", channel_id)
            return None
        return ch

    async def send_text(self, channel_id: int, text: str) -> int | None:
        ch = await self._channel(channel_id)
        if ch is None:
            return None
        msg = await ch.send(text[:2000], allowed_mentions=discord.AllowedMentions.none())
        return msg.id

    async def send_dm(self, discord_id: int, text: str) -> bool:
        try:
            user = self._client.get_user(discord_id) or await self._client.fetch_user(discord_id)
            await user.send(text[:2000], allowed_mentions=discord.AllowedMentions.none())
            return True
        except discord.Forbidden:
            log.info("DM to %s not delivered: user has DMs closed or blocked the bot", discord_id)
            return False
        except discord.HTTPException as e:
            log.warning("DM to %s failed: %s", discord_id, e)
            return False

    def guild_name(self) -> str:
        g = self._client.get_guild(self._guild_id)
        return g.name if g else "this server"


# ---------------------------------------------------------------------- review embeds + buttons

COLOR_PENDING = brand.MINT       # awaiting a decision
COLOR_CONFIRMED = brand.SAND     # a source's Confirmed
COLOR_INFO = brand.UMBER         # past offender / informational
COLOR_UNVERIFIED = brand.SAGE    # lookups failed
COLOR_RESOLVED = brand.PINE      # closed


def display_status(row: ReviewRow) -> str:
    """Inconclusive rows carry the provider's internal status "error"; show mods what it means instead."""
    return "Unverified" if row.outcome == "inconclusive" else row.status_name


def _case_color(row: ReviewRow) -> discord.Color:
    if row.outcome == "inconclusive":
        return COLOR_UNVERIFIED
    if row.outcome == "past_offender":
        return COLOR_INFO
    if row.status_name == "Confirmed":
        return COLOR_CONFIRMED
    return COLOR_PENDING


def build_review_embed(row: ReviewRow, *, report: bool = False, resolution: str | None = None,
                       details: bool = True) -> discord.Embed:
    """The case card. `resolution` switches it to its closed form. `details=False` leaves out the source's
    reasons, for posts Collapse can't edit later (see log_detection)."""
    kind = "Detection" if report else "Case"
    status = display_status(row)
    if resolution is not None:
        title = f"Resolved · {kind} #{row.id}"
        color = COLOR_RESOLVED
    else:
        title = f"{kind} #{row.id} · {status}"
        color = _case_color(row)
    e = discord.Embed(title=title, color=color)
    if row.avatar_url:
        e.set_thumbnail(url=row.avatar_url)

    e.add_field(name="Member", value=f"<@{row.discord_id}>\n`{row.discord_id}`", inline=True)
    if row.roblox_id:
        e.add_field(
            name="Roblox",
            value=f"[{row.roblox_username}](https://www.roblox.com/users/{row.roblox_id}/profile)\n`{row.roblox_id}`",
            inline=True,
        )
    else:
        e.add_field(name="Roblox", value=f"{row.roblox_username}\n`unresolved`", inline=True)
    e.add_field(name="Status", value=status, inline=True)

    e.add_field(name="Link", value=source_text(row.identity_source), inline=True)
    e.add_field(name="Reason", value=reason_text(row.reason, row.provider), inline=True)
    if row.nickname:
        e.add_field(name="Nickname", value=row.nickname[:256], inline=True)

    if details and row.summary and row.summary != "(no details)":
        e.add_field(name="Details", value=row.summary[:1024], inline=False)

    if resolution is not None:
        e.add_field(name="Outcome", value=resolution[:1024], inline=False)

    source = brand.EVASION_SOURCE if row.provider == "banbot" else f"{source_label(row.provider)} via Rayward"
    footer = f"{source} · {row.created_at:%d %b %Y %H:%M} UTC"
    if report:
        footer += " · report only"
    e.set_footer(text=footer)
    return e


async def strip_details(client: discord.Client, channel_id: int, message_id: int) -> None:
    """Remove the Details field (the source's reasons) from an already-posted case message, keeping
    everything else, including its buttons. Used once a case is older than 24 hours."""
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    message = await channel.fetch_message(message_id)  # type: ignore[union-attr]
    if not message.embeds:
        return
    old = message.embeds[0]
    if not any(f.name == "Details" for f in old.fields):
        return
    new = old.copy()
    new.clear_fields()
    for f in old.fields:
        if f.name != "Details":
            new.add_field(name=f.name, value=f.value, inline=f.inline)
    await message.edit(embed=new)


def source_text(source: str) -> str:
    return {
        "bloxlink": "Bloxlink (verified)",
        "nickname": "Nickname tag (unverified)",
    }.get(source, source)


def reason_text(reason: str, provider: str) -> str:
    if reason.startswith("status:"):
        status = reason.split(":", 1)[1]
        if status == "Past Offender":
            return "Previously flagged, since cleared. No action required."
        return f"{source_label(provider)} status: {status}."
    return {
        "confirmed_requires_review": f"{source_label(provider)} status: Confirmed. Awaiting approval.",
        "inconclusive_exhausted": "Lookups failed repeatedly. Account status unverified.",
        "ban_evasion": "Ban evasion: Collapse already banned this Roblox account here under a different Discord account.",
    }.get(reason, reason)


class ReviewView(discord.ui.View):
    def __init__(self, review_id: int):
        super().__init__(timeout=None)
        self.add_item(ApproveButton(review_id))
        self.add_item(DenyButton(review_id))


class _ReviewButton(discord.ui.DynamicItem[discord.ui.Button], template=r"banbot:review:(?P<id>[0-9]+):(?P<action>approve|deny)"):
    action: str = ""
    label: str = ""
    style: discord.ButtonStyle = discord.ButtonStyle.secondary

    def __init__(self, review_id: int):
        super().__init__(discord.ui.Button(
            label=self.label, style=self.style, custom_id=f"{CUSTOM_ID_PREFIX}{review_id}:{self.action}"
        ))
        self.review_id = review_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        action = match["action"]
        klass = ApproveButton if action == "approve" else DenyButton
        return klass(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        registry: AppRegistry | None = getattr(interaction.client, "registry", None)
        app = registry.get(interaction.guild_id) if registry and interaction.guild_id else None
        if app is None:
            await interaction.response.send_message(
                "The bot is still starting, or this server hasn't finished /setup. Try again in a moment.",
                ephemeral=True)
            return
        user = interaction.user
        role_ids = [r.id for r in user.roles] if isinstance(user, discord.Member) else []
        # Cheap server-side gate before deferring, so non-mods get an instant ephemeral "not allowed".
        if not app.review_queue.can_moderate(role_ids, user.id):
            await interaction.response.send_message("You don't have permission to do that.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if self.action == "approve":
            decision = await app.review_queue.approve(self.review_id, actor_id=user.id, actor_role_ids=role_ids)
        else:
            decision = await app.review_queue.deny(self.review_id, actor_id=user.id, actor_role_ids=role_ids)
        await interaction.followup.send(decision.message, ephemeral=True)
        await _drop_buttons_if_closed(app, interaction, self.review_id)


async def _drop_buttons_if_closed(app: App, interaction: discord.Interaction, review_id: int) -> None:
    """The mod-channel post is closed by ReviewQueue itself; this catches the other copy (the forum log
    thread), which Collapse holds no reference to, once the case is resolved."""
    row = app.store.get_review(app.cfg.guild_id, review_id)
    if row is None or row.status == "pending" or interaction.message is None:
        return
    if (row.channel_id, row.message_id) == (interaction.channel_id, interaction.message.id):
        return  # the mod-channel post; already updated to its closed form
    try:
        await interaction.message.edit(view=None)
    except discord.HTTPException as e:
        log.info("case #%s: could not remove buttons from message %s (%s)", review_id, interaction.message.id, e)


class ApproveButton(_ReviewButton, template=r"banbot:review:(?P<id>[0-9]+):(?P<action>approve)"):
    action = "approve"
    label = "Ban"
    style = discord.ButtonStyle.danger


class DenyButton(_ReviewButton, template=r"banbot:review:(?P<id>[0-9]+):(?P<action>deny)"):
    action = "deny"
    label = "Dismiss"
    style = discord.ButtonStyle.secondary


class DiscordReviewPoster:
    def __init__(
        self, gateway: DiscordGateway, mod_channel_id: int, *,
        log_forum_channel_id: int | None = None, tag_map: dict[str, int] | None = None,
    ):
        self._gateway = gateway
        self._channel_id = mod_channel_id
        self._log_forum_channel_id = log_forum_channel_id
        self._tag_map = tag_map or {}

    @property
    def channel_id(self) -> int:
        """The mod channel new cases are posted to."""
        return self._channel_id

    async def post(self, row: ReviewRow) -> tuple[int, int] | None:
        ch = await self._gateway._channel(self._channel_id)
        if ch is None:
            log.warning("review #%s: mod channel %s not found or not accessible; not posted", row.id, self._channel_id)
            return None
        msg = await ch.send(
            embed=build_review_embed(row),
            view=ReviewView(row.id),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return (self._channel_id, msg.id)

    async def post_report(self, row: ReviewRow) -> tuple[int, int] | None:
        ch = await self._gateway._channel(self._channel_id)
        if ch is None:
            log.warning("report #%s: mod channel %s not found or not accessible; not posted", row.id, self._channel_id)
            return None
        msg = await ch.send(
            embed=build_review_embed(row, report=True),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return (self._channel_id, msg.id)

    async def update(self, row: ReviewRow, resolution: str) -> None:
        if row.channel_id is None or row.message_id is None:
            return
        ch = await self._gateway._channel(row.channel_id)
        if ch is None:
            return
        try:
            msg = await ch.fetch_message(row.message_id)  # type: ignore[attr-defined]
        except discord.HTTPException:
            log.warning("review #%s: original message %s not found", row.id, row.message_id)
            return
        # Closed form: title becomes "Resolved", the outcome is recorded, and the buttons are removed.
        await msg.edit(embed=build_review_embed(row, resolution=resolution), view=None)

    async def log_detection(self, row: ReviewRow) -> None:
        """Copy in the detection-log forum, if one is configured. One thread per detection. An open case
        gets the same Ban/Dismiss buttons as the mod-channel post, acting on the same case. Collapse keeps
        no reference to these threads, so it can't update them when the case is resolved elsewhere: their
        buttons go away the next time someone presses one (see _ReviewButton.callback), and the post never
        includes the flag reasons, which Rayward's terms say must be removed after 24 hours."""
        if self._log_forum_channel_id is None:
            return
        forum = await self._gateway._forum_channel(self._log_forum_channel_id)
        if forum is None:
            return
        try:
            await forum.create_thread(
                name=_thread_title(row),
                embed=build_review_embed(row, report=(row.status == "reported"), details=False),
                allowed_mentions=discord.AllowedMentions.none(),
                applied_tags=self._applied_tags(forum, row),
                view=ReviewView(row.id) if row.status == "pending" else discord.utils.MISSING,
            )
        except discord.HTTPException:
            log.exception("failed to log detection #%s to the forum", row.id)

    def _applied_tags(self, forum: discord.ForumChannel, row: ReviewRow) -> list[discord.ForumTag]:
        category = _tag_category(row)
        tag_id = self._tag_map.get(category) if category else None
        if tag_id is None:
            return []
        tag = forum.get_tag(tag_id)
        if tag is None:
            log.warning(
                "detection #%s: configured '%s' tag (id %s) no longer exists on forum %s; posting untagged",
                row.id, category, tag_id, forum.id,
            )
            return []
        return [tag]


def _thread_title(row: ReviewRow) -> str:
    title = f"#{row.id} · {row.roblox_username} · {display_status(row)}"
    return title[:100]  # Discord forum thread names are capped at 100 characters


def _tag_category(row: ReviewRow) -> str | None:
    """Which of FORUM_TAG_CATEGORIES this detection belongs to, most specific first. None = post untagged."""
    if row.reason == REASON_BAN_EVASION:
        return "ban_evasion"
    if row.status == "reported":
        return "reported"
    if row.outcome == FlagOutcome.CONFIRMED.value:
        return "confirmed"
    if row.outcome == FlagOutcome.REVIEW.value:
        return "review"
    if row.outcome == FlagOutcome.PAST_OFFENDER.value:
        return "past_offender"
    return None
