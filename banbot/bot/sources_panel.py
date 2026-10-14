"""/sources: the developer switches individual Rayward flag sources on or off for every server at once.

The state is saved in the database (source_state) and applies from the next lookup; no restart. A switched-off
source keeps its name and appeal link for existing cases and ban DMs, it just isn't looked up. The last source
that's on can't be switched off: with nothing to ask, every check would come back unverified.
"""
from __future__ import annotations

import logging

import discord

from banbot import brand
from banbot.core.flags import RAYWARD_SOURCES, active_sources, is_enabled, set_disabled_sources
from banbot.settings.config import DEVELOPER_ID
from banbot.storage.store import Store
from banbot.util import utcnow

log = logging.getLogger(__name__)


def _embed() -> discord.Embed:
    lines = [f"{'🟢' if is_enabled(k) else '⚫'} **{s.label}** {'on' if is_enabled(k) else 'off'}"
             for k, s in RAYWARD_SOURCES.items()]
    e = discord.Embed(title="Flag sources", description="\n".join(lines), color=brand.SAGE)
    e.set_footer(text="Applies to every server from the next lookup. Press a source to switch it.")
    return e


class _SourceButton(discord.ui.Button["SourcesView"]):
    def __init__(self, key: str):
        on = is_enabled(key)
        super().__init__(label=RAYWARD_SOURCES[key].label,
                         style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary)
        self.key = key

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SourcesView = self.view  # type: ignore[assignment]
        turning_on = not is_enabled(self.key)
        if not turning_on and len(active_sources()) == 1:
            await interaction.response.send_message(
                f"{RAYWARD_SOURCES[self.key].label} is the only source on. Switch another one on first.")
            return
        view.store.set_source_enabled(self.key, turning_on, by=interaction.user.id, at=utcnow())
        set_disabled_sources(view.store.disabled_sources())
        log.warning("source %s switched %s by %s", self.key, "on" if turning_on else "off", interaction.user.id)
        await interaction.response.edit_message(embed=_embed(), view=SourcesView(view.store))


class SourcesView(discord.ui.View):
    def __init__(self, store: Store):
        super().__init__(timeout=600)
        self.store = store
        for key in RAYWARD_SOURCES:
            self.add_item(_SourceButton(key))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == DEVELOPER_ID


async def open_sources_panel(interaction: discord.Interaction, store: Store) -> None:
    await interaction.response.send_message(embed=_embed(), view=SourcesView(store))
