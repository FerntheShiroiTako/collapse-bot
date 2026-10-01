"""The /setup and /config wizard: one interactive panel (role/channel selects, modals for the two API
keys, toggle buttons for the safety switches, and a Test & Finish button that runs diagnostics before
marking the guild ready). Both commands open the same ConfigPanel - /setup is just the first visit.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from banbot.settings import messages
from banbot import brand
from banbot.bot import diagnostics
from banbot.app import build_guild_http_clients
from banbot.settings.guild import GuildSettings, build_guild_config
from banbot.settings.config import FORUM_TAG_CATEGORIES, ConfigError
from banbot.util import utcnow

TAG_CATEGORY_LABELS: dict[str, str] = {
    "confirmed": "Confirmed",
    "review": "Review",
    "past_offender": "Past Offender",
    "ban_evasion": "Ban Evasion",
    "reported": "Reported",
}

if TYPE_CHECKING:
    from banbot.app import AppRegistry
    from banbot.storage.store import Store

log = logging.getLogger(__name__)


def _check(ok: bool) -> str:
    return "✅" if ok else "❌"


def summary_embed(settings: GuildSettings, guild: discord.Guild, tier: str) -> discord.Embed:
    missing = settings.missing_fields()
    color = brand.MINT if settings.is_ready else brand.SAND
    e = discord.Embed(title=f"{brand.NAME} configuration", color=color)
    e.add_field(name="Rayward API key", value="Set" if settings.rayward_api_key else f"{_check(False)} Not set (required)")
    e.add_field(name="Bloxlink API key",
                value="Set" if settings.bloxlink_api_key else "Off (nickname parsing only)")
    e.add_field(name="Master role",
                value=f"<@&{settings.master_role_id}>" if settings.master_role_id else "(Manage Server permission only)")
    e.add_field(name="Configurator role",
                value=f"<@&{settings.configurator_role_id}>" if settings.configurator_role_id else "Not set")
    e.add_field(name="Mod role",
                value=f"<@&{settings.mod_role_id}>" if settings.mod_role_id else f"{_check(False)} Not set")
    e.add_field(name="Mod channel",
                value=f"<#{settings.mod_channel_id}>" if settings.mod_channel_id else f"{_check(False)} Not set")
    e.add_field(name="Summary channel",
                value=f"<#{settings.summary_channel_id}>" if settings.summary_channel_id else "(same as mod channel)")
    e.add_field(name="Detection log (forum)",
                value=f"<#{settings.log_forum_channel_id}>" if settings.log_forum_channel_id else "Off")
    trig = []
    if settings.sweep_trigger_role_id:
        trig.append(f"<@&{settings.sweep_trigger_role_id}>")
    if settings.sweep_trigger_user_ids:
        trig.append(", ".join(f"<@{u}>" for u in sorted(settings.sweep_trigger_user_ids)))
    e.add_field(name="Sweep trigger", value=" and ".join(trig) if trig else f"{_check(False)} Not set")
    e.add_field(name="Dry run", value=("On: Ban presses are recorded, nobody is banned" if settings.dry_run else "**Off: Ban presses ban members**"))
    e.add_field(name="Report only", value=("On: detections are posted without buttons" if settings.report_only else "Off"))
    if missing:
        e.description = f"{_check(False)} **Still needed:** " + ", ".join(missing)
    elif not settings.setup_completed:
        e.description = f"{_check(True)} Everything required is set. Click **Test & Finish** to test the keys and permissions."
    else:
        e.description = f"{_check(True)} Setup is complete. Change anything below, or run /sweep start."
    access = "Master access" if tier == "master" else "Configurator access (no API keys)"
    e.set_footer(text=f"{guild.name} · {access} · changes save immediately")
    return e


class ConfigPanel(discord.ui.View):
    def __init__(self, *, registry: "AppRegistry", store: "Store", guild: discord.Guild, settings: GuildSettings,
                tier: str):
        super().__init__(timeout=900)
        self.registry = registry
        self.store = store
        self.guild = guild
        self.settings = settings
        self.tier = tier  # "master" | "configurator" - "none" never reaches this class
        self.add_item(_ModRoleSelect())
        self.add_item(_ModChannelSelect())
        self.add_item(_TriggerRoleSelect())
        if tier == "master":
            self.add_item(_RaywardKeyButton())
            self.add_item(_BloxlinkKeyButton())
        self.add_item(_ModesButton())
        self.add_item(_MoreSettingsButton())
        if tier == "master":
            self.add_item(_AccessControlButton())
        self.add_item(_TestAndFinishButton())

    async def apply(self, interaction: discord.Interaction, **fields) -> None:
        self.settings = self.store.update_guild_settings(self.guild.id, by=interaction.user.id, at=utcnow(), **fields)
        self.registry.invalidate(self.guild.id)
        await interaction.response.edit_message(embed=summary_embed(self.settings, self.guild, self.tier), view=self)

    async def refresh(self, interaction: discord.Interaction) -> None:
        self.settings = self.store.get_guild_settings(self.guild.id) or self.settings
        embed = summary_embed(self.settings, self.guild, self.tier)
        if interaction.response.is_done():
            await interaction.message.edit(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)


# ---------------------------------------------------------------------- selects (role / channel)

class _ModRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Mod role: can press Ban and Dismiss on cases", row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.apply(interaction, mod_role_id=self.values[0].id)


class _TriggerRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Sweep trigger role (optional): can run /sweep start", row=2, min_values=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.apply(interaction, sweep_trigger_role_id=self.values[0].id if self.values else None)


class _ModChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="Mod channel: where cases are posted",
                         channel_types=[discord.ChannelType.text], row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.apply(interaction, mod_channel_id=self.values[0].id)


class _SummaryChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="Summary channel (optional): defaults to the mod channel",
                         channel_types=[discord.ChannelType.text], row=0, min_values=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.apply(interaction, summary_channel_id=self.values[0].id if self.values else None)


class _LogForumChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="Detection log forum (optional): one thread per detection",
                         channel_types=[discord.ChannelType.forum], row=1, min_values=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.apply(interaction, log_forum_channel_id=self.values[0].id if self.values else None)


# ---------------------------------------------------------------------- modals (secrets / free text)

class _KeyModal(discord.ui.Modal):
    def __init__(self, panel: ConfigPanel, *, field_name: str, title: str, label: str, current: str | None):
        super().__init__(title=title)
        self.panel = panel
        self.field_name = field_name
        self.value_input = discord.ui.TextInput(
            label=label, required=False, max_length=300,
            placeholder="Only you see this. Leave blank to remove the key.",
            default="" if not current else None,  # never echo a real key back into the box
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = self.value_input.value.strip() or None
        await self.panel.apply(interaction, **{self.field_name: value})


class _RaywardKeyButton(discord.ui.Button["ConfigPanel"]):
    def __init__(self):
        super().__init__(label="Rayward Key", style=discord.ButtonStyle.primary, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        panel: ConfigPanel = self.view  # type: ignore[assignment]
        modal = _KeyModal(
            panel, field_name="rayward_api_key", title="Rayward API key (rayward.app/signin)",
            label="Rayward key (starts with rwd_)", current=panel.settings.rayward_api_key,
        )
        await interaction.response.send_modal(modal)


class _BloxlinkKeyButton(discord.ui.Button["ConfigPanel"]):
    def __init__(self):
        super().__init__(label="Bloxlink Key", style=discord.ButtonStyle.secondary, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        panel: ConfigPanel = self.view  # type: ignore[assignment]
        modal = _KeyModal(
            panel, field_name="bloxlink_api_key", title="Bloxlink API key (blox.link)",
            label="Bloxlink server key", current=panel.settings.bloxlink_api_key,
        )
        await interaction.response.send_modal(modal)


class _TriggerUsersModal(discord.ui.Modal, title="Sweep trigger users"):
    def __init__(self, target: "ConfigPanel | _MoreSettingsView"):
        super().__init__()
        self.target = target
        current = ",".join(str(u) for u in sorted(target.settings.sweep_trigger_user_ids))
        self.ids_input = discord.ui.TextInput(
            label="Comma-separated Discord user IDs", required=False, max_length=500,
            default=current or None, placeholder="e.g. 123456789012345678, 234567890123456789",
        )
        self.add_item(self.ids_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.ids_input.value.strip()
        try:
            ids = frozenset(int(p.strip()) for p in raw.split(",") if p.strip())
        except ValueError:
            await interaction.response.send_message(
                "That isn't a list of user IDs. Enter numeric IDs separated by commas.", ephemeral=True)
            return
        await self.target.apply(interaction, sweep_trigger_user_ids=ids)


class _AppealLinkModal(discord.ui.Modal, title="Appeal link"):
    def __init__(self, target: "_MoreSettingsView"):
        super().__init__()
        self.target = target
        self.url_input = discord.ui.TextInput(
            label="Appeals server invite or form link", required=False, max_length=messages.APPEAL_MAX_LEN,
            default=target.settings.appeal_url or None,
            placeholder="e.g. discord.gg/yourappeals or https://forms.gle/... (blank to remove)",
        )
        self.add_item(self.url_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            url = messages.normalize_appeal_url(self.url_input.value)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await self.target.apply(interaction, appeal_url=url)


class _AppealLinkButton(discord.ui.Button["_MoreSettingsView"]):
    def __init__(self):
        super().__init__(label="Appeal Link", style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: _MoreSettingsView = self.view  # type: ignore[assignment]
        await interaction.response.send_modal(_AppealLinkModal(view))


class _TriggerUsersButton(discord.ui.Button["_MoreSettingsView"]):
    def __init__(self):
        super().__init__(label="Trigger Users", style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: _MoreSettingsView = self.view  # type: ignore[assignment]
        await interaction.response.send_modal(_TriggerUsersModal(view))


# ---------------------------------------------------------------------- more settings (channels used less often)

def _more_settings_embed(settings: GuildSettings) -> discord.Embed:
    e = discord.Embed(title="More settings", color=brand.SAGE)
    e.add_field(name="Summary channel",
                value=f"<#{settings.summary_channel_id}>" if settings.summary_channel_id else "(same as mod channel)",
                inline=False)
    e.add_field(name="Detection log (forum)",
                value=f"<#{settings.log_forum_channel_id}>" if settings.log_forum_channel_id else "Off", inline=False)
    if settings.log_forum_channel_id:
        mapped = len(settings.forum_tag_map())
        e.add_field(name="Detection log tags", value=f"{mapped}/{len(FORUM_TAG_CATEGORIES)} categories mapped",
                    inline=False)
    trig_users = ", ".join(f"<@{u}>" for u in sorted(settings.sweep_trigger_user_ids)) if settings.sweep_trigger_user_ids else "None"
    e.add_field(name="Sweep trigger users", value=trig_users, inline=False)
    e.add_field(name="Appeal link (sent in the ban DM)",
                value=settings.appeal_url or "Not set. The ban DM tells members to contact your mods.", inline=False)
    return e


class _MoreSettingsButton(discord.ui.Button["ConfigPanel"]):
    def __init__(self):
        super().__init__(label="More Settings", style=discord.ButtonStyle.secondary, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        panel: ConfigPanel = self.view  # type: ignore[assignment]
        await interaction.response.send_message(
            embed=_more_settings_embed(panel.settings), view=_MoreSettingsView(panel), ephemeral=True)


class _MoreSettingsView(discord.ui.View):
    def __init__(self, panel: ConfigPanel):
        super().__init__(timeout=300)
        self.panel = panel
        self.add_item(_SummaryChannelSelect())
        self.add_item(_LogForumChannelSelect())
        self.add_item(_TriggerUsersButton())
        self.add_item(_AppealLinkButton())
        self.add_item(_TagsButton())

    @property
    def settings(self) -> GuildSettings:
        return self.panel.settings

    async def apply(self, interaction: discord.Interaction, **fields) -> None:
        self.panel.settings = self.panel.store.update_guild_settings(
            self.panel.guild.id, by=interaction.user.id, at=utcnow(), **fields)
        self.panel.registry.invalidate(self.panel.guild.id)
        await interaction.response.edit_message(embed=_more_settings_embed(self.panel.settings), view=self)


# ---------------------------------------------------------------------- detection log forum tags

def _tags_embed(settings: GuildSettings, forum: discord.ForumChannel) -> discord.Embed:
    e = discord.Embed(
        title="Detection log tags", color=brand.SAGE,
        description=(
            f"Pick a tag from <#{forum.id}> for each kind of detection. Threads in a category set to "
            "(none) are posted without a tag. To add tags, edit the forum channel (Edit Channel → Tags)."
        ),
    )
    tag_map = settings.forum_tag_map()
    for category in FORUM_TAG_CATEGORIES:
        tag_id = tag_map.get(category)
        tag = forum.get_tag(tag_id) if tag_id else None
        value = tag.name if tag else ("(none)" if tag_id is None else "⚠ This tag was deleted. Pick another.")
        e.add_field(name=TAG_CATEGORY_LABELS[category], value=value, inline=True)
    return e


class _TagCategorySelect(discord.ui.Select["_TagsView"]):
    def __init__(self, category: str, *, tags: list[discord.ForumTag], current: int | None, row: int):
        self.category = category
        options = [discord.SelectOption(label="(none)", value="none", default=current is None)]
        for tag in tags[:24]:
            options.append(discord.SelectOption(
                label=tag.name, value=str(tag.id), emoji=tag.emoji or None, default=tag.id == current,
            ))
        super().__init__(placeholder=f"{TAG_CATEGORY_LABELS[category]} tag", options=options, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        field = f"tag_{self.category}_id"
        await self.view.apply(interaction, **{field: None if value == "none" else int(value)})


class _TagsView(discord.ui.View):
    def __init__(self, panel: ConfigPanel, forum: discord.ForumChannel):
        super().__init__(timeout=300)
        self.panel = panel
        self.forum = forum
        tag_map = panel.settings.forum_tag_map()
        for row, category in enumerate(FORUM_TAG_CATEGORIES):
            self.add_item(_TagCategorySelect(
                category, tags=forum.available_tags, current=tag_map.get(category), row=row,
            ))

    async def apply(self, interaction: discord.Interaction, **fields) -> None:
        self.panel.settings = self.panel.store.update_guild_settings(
            self.panel.guild.id, by=interaction.user.id, at=utcnow(), **fields)
        self.panel.registry.invalidate(self.panel.guild.id)
        await interaction.response.edit_message(
            embed=_tags_embed(self.panel.settings, self.forum), view=_TagsView(self.panel, self.forum))


class _TagsButton(discord.ui.Button["_MoreSettingsView"]):
    def __init__(self):
        super().__init__(label="Detection Log Tags", style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: _MoreSettingsView = self.view  # type: ignore[assignment]
        settings = view.settings
        if not settings.log_forum_channel_id:
            await interaction.response.send_message(
                "Set a detection log forum first, then map its tags here.",
                ephemeral=True)
            return
        forum = view.panel.guild.get_channel(settings.log_forum_channel_id)
        if not isinstance(forum, discord.ForumChannel):
            await interaction.response.send_message(
                "The bot can't open that forum channel. Check that it still exists and that the bot can see it.",
                ephemeral=True)
            return
        if not forum.available_tags:
            await interaction.response.send_message(
                f"<#{forum.id}> has no tags yet. Add some under Edit Channel → Tags, then map them here.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=_tags_embed(settings, forum), view=_TagsView(view.panel, forum), ephemeral=True)


# ---------------------------------------------------------------------- access control (master-tier only)

def _access_control_embed(settings: GuildSettings) -> discord.Embed:
    e = discord.Embed(
        title="Access control", color=brand.SAGE,
        description=(
            "The Master role can change every setting, including the API keys and this panel, like Manage "
            "Server can. The Configurator role can change everything except the API keys."
        ),
    )
    e.add_field(name="Master role",
                value=f"<@&{settings.master_role_id}>" if settings.master_role_id else "(Manage Server permission only)",
                inline=False)
    e.add_field(name="Configurator role",
                value=f"<@&{settings.configurator_role_id}>" if settings.configurator_role_id else "Not set",
                inline=False)
    return e


class _MasterRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Master role: every setting, like Manage Server", row=0, min_values=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.apply(interaction, master_role_id=self.values[0].id if self.values else None)


class _ConfiguratorRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Configurator role: every setting except the API keys", row=1, min_values=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.apply(interaction, configurator_role_id=self.values[0].id if self.values else None)


class _AccessControlButton(discord.ui.Button["ConfigPanel"]):
    def __init__(self):
        super().__init__(label="Access Control", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction: discord.Interaction) -> None:
        panel: ConfigPanel = self.view  # type: ignore[assignment]
        await interaction.response.send_message(
            embed=_access_control_embed(panel.settings), view=_AccessControlView(panel), ephemeral=True)


class _AccessControlView(discord.ui.View):
    def __init__(self, panel: ConfigPanel):
        super().__init__(timeout=300)
        self.panel = panel
        self.add_item(_MasterRoleSelect())
        self.add_item(_ConfiguratorRoleSelect())

    async def apply(self, interaction: discord.Interaction, **fields) -> None:
        self.panel.settings = self.panel.store.update_guild_settings(
            self.panel.guild.id, by=interaction.user.id, at=utcnow(), **fields)
        self.panel.registry.invalidate(self.panel.guild.id)
        await interaction.response.edit_message(embed=_access_control_embed(self.panel.settings), view=self)


# ---------------------------------------------------------------------- mode toggles

class _ModesButton(discord.ui.Button["ConfigPanel"]):
    def __init__(self):
        super().__init__(label="Safety Modes", style=discord.ButtonStyle.secondary, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        panel: ConfigPanel = self.view  # type: ignore[assignment]
        await interaction.response.send_message(
            "Safety modes for **" + panel.guild.name + "**. Turning off Dry Run asks you to confirm, because "
            "after that a mod pressing Ban really bans the member.",
            view=_ModesView(panel), ephemeral=True,
        )


class _ModesView(discord.ui.View):
    def __init__(self, panel: ConfigPanel):
        super().__init__(timeout=300)
        self.panel = panel
        self.add_item(_ToggleButton("report_only", "Report Only", row=0))
        self.add_item(_ToggleButton("dry_run", "Dry Run", row=0, confirm_on_disable=True))
        self.add_item(_ToggleButton("notify_starter_on_sweep_complete", "DM Sweep Starter", row=1))
        self.add_item(_ToggleButton("ban_dm_enabled", "Ban DM", row=2))


class _ToggleButton(discord.ui.Button["_ModesView"]):
    def __init__(self, field: str, label: str, *, row: int, confirm_on_disable: bool = False):
        self.field = field
        self.confirm_on_disable = confirm_on_disable
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: _ModesView = self.view  # type: ignore[assignment]
        panel = view.panel
        current = getattr(panel.settings, self.field)
        turning_off = current is True
        if turning_off and self.confirm_on_disable:
            await interaction.response.send_message(
                f"Turn **{self.label}** off? " + _danger_text(self.field),
                view=_ConfirmView(panel, self.field, False, self.label), ephemeral=True,
            )
            return
        panel.settings = panel.store.update_guild_settings(
            panel.guild.id, by=interaction.user.id, at=utcnow(), **{self.field: not current}
        )
        panel.registry.invalidate(panel.guild.id)
        await interaction.response.send_message(
            f"{self.label} is now **{'on' if not current else 'off'}**.", ephemeral=True)


def _danger_text(field: str) -> str:
    if field == "dry_run":
        return "From then on, a mod pressing Ban really bans the member."
    return "The change applies straight away."


class _ConfirmView(discord.ui.View):
    def __init__(self, panel: ConfigPanel, field: str, new_value: bool, label: str):
        super().__init__(timeout=60)
        self.panel = panel
        self.field = field
        self.new_value = new_value
        self.label = label

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.panel.settings = self.panel.store.update_guild_settings(
            self.panel.guild.id, by=interaction.user.id, at=utcnow(), **{self.field: self.new_value}
        )
        self.panel.registry.invalidate(self.panel.guild.id)
        await interaction.response.edit_message(content=f"{self.label} is now **{'on' if self.new_value else 'off'}**.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="No change made.", view=None)


# ---------------------------------------------------------------------- diagnostics

class _TestAndFinishButton(discord.ui.Button["ConfigPanel"]):
    def __init__(self):
        super().__init__(label="Test & Finish", style=discord.ButtonStyle.success, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        panel: ConfigPanel = self.view  # type: ignore[assignment]
        settings = panel.settings
        missing = settings.missing_fields()
        if missing:
            await interaction.response.send_message(
                "Still needed before testing: " + ", ".join(missing), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            cfg = build_guild_config(panel.registry.global_cfg, settings)
        except ConfigError as e:
            await interaction.followup.send(f"Configuration is invalid: {e}", ephemeral=True)
            return

        provider, bloxlink = build_guild_http_clients(cfg, panel.registry.session)
        results = [await diagnostics.check_rayward(provider)]
        bloxlink_result = await diagnostics.check_bloxlink(bloxlink)
        if bloxlink_result is not None:
            results.append(bloxlink_result)
        results += diagnostics.check_discord_permissions(panel.guild, cfg)

        lines = [f"{_check(r.ok)} **{r.name}**" + (f": {r.detail}" if r.detail else "") for r in results]
        all_ok = all(r.ok for r in results)
        if all_ok:
            panel.settings = panel.store.update_guild_settings(
                panel.guild.id, by=interaction.user.id, at=utcnow(), setup_completed=True, setup_by=interaction.user.id,
            )
            panel.registry.invalidate(panel.guild.id)
            lines.append("\n**All checks passed. Setup is complete.**")
        else:
            lines.append("\nFix the checks marked ❌, then click Test & Finish again.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        await panel.refresh(interaction)


async def open_panel(interaction: discord.Interaction, *, registry: "AppRegistry", store: "Store", tier: str) -> None:
    assert interaction.guild is not None
    settings = store.get_or_create_guild_settings(interaction.guild.id)
    panel = ConfigPanel(registry=registry, store=store, guild=interaction.guild, settings=settings, tier=tier)
    await interaction.response.send_message(
        embed=summary_embed(settings, interaction.guild, tier), view=panel, ephemeral=True)
