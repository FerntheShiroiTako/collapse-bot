"""The /help command: a short overview plus a topic picker for the rest, written the way you'd actually
explain the bot to a new moderator rather than as a dry feature list.
"""
from __future__ import annotations

import discord

from banbot import brand

TOPICS: dict[str, tuple[str, str]] = {
    "overview": (
        "Overview",
        "Collapse looks up the Roblox account linked to each member and checks it against Rotector's flag "
        "data. Flagged accounts are posted to your mod channel with Ban and Dismiss buttons. The bot never "
        "bans anyone on its own; a ban only happens when a mod presses Ban.\n\n"
        "A lookup that fails or times out is treated as unknown, and the member is checked again later. "
        "Each server uses its own Rayward key and starts in dry run, so nothing is banned until an admin "
        "turns dry run off.",
    ),
    "checks": (
        "How a check works",
        "A check runs when a member joins and when a sweep reaches them. First the bot works out which "
        "Roblox account is theirs: a Bloxlink verification if they have one, otherwise a `(@username)` "
        "tag at the end of their nickname.\n\n"
        "If Collapse already banned that Roblox account here under a different Discord account, the case "
        "goes to your mods as ban evasion. Otherwise the account is looked up on Rotector through Rayward. "
        "Unflagged accounts are left alone, and past offenders (flagged before, cleared since) are logged "
        "without action. Anything Rotector flags, Confirmed included, goes to your mods.",
    ),
    "reviews": (
        "Review queue and logs",
        "Each case is posted to your mod channel with Ban and Dismiss buttons. Only your mod role can use "
        "them, and the buttons disappear once a case is resolved. Ban checks the account on Rotector "
        "again first, and only bans if it's still flagged. After 24 hours, Rotector's reasons are removed "
        "from the case, as Rayward's terms require.\n\n"
        "`/reviews` lists the cases that are still open, ten per page. `/detections` goes back further and "
        "lists every detection recorded for your server, resolved or not, as a CSV file or as a list of "
        "Roblox usernames.\n\n"
        "To keep a permanent record, set a forum channel as the detection log in `/config` → More "
        "Settings. Every detection then gets its own thread, which stays as it was whatever happens to the "
        "case.",
    ),
    "sweeps": (
        "Sweeps",
        "`/sweep start` checks every member in the server the same way a join does. Only your sweep "
        "triggers (a role, specific people, or both) can start one, and a server runs one sweep at a "
        "time. If the bot restarts partway through, the sweep carries on from where it stopped.\n\n"
        "`/sweep status` shows progress, `/sweep resume` restarts a paused sweep and `/sweep abort` stops "
        "one. When a sweep finishes, its summary is posted to your summary channel (or the mod channel if "
        "you haven't set one) and sent by DM to whoever started it, unless you've switched that off.",
    ),
    "modes": (
        "Safety modes",
        "With dry run on, which is the default, pressing Ban records what would have happened and bans "
        "nobody. Report only goes further: detections are posted as plain notices with no buttons.\n\n"
        "Both switches are in `/config` → Safety Modes, along with the DM sent to banned members and the "
        "DM of sweep summaries. Turning dry run off asks you to confirm first, because it makes Ban "
        "presses real.\n\n"
        "To point banned members at your appeals server or form, set an Appeal Link in `/config` → More "
        "Settings. It's added to the ban DM.",
    ),
    "setup": (
        "Setup and access",
        "`/setup` connects your Rayward key, sets the mod role and channel, and chooses who can run "
        "sweeps. Checks start once those are set, and Test & Finish confirms the keys and permissions "
        "work. `/config` opens the same panel later.\n\n"
        "By default only members with Manage Server can change settings. You can give access without "
        "that permission from the panel's Access Control section: the Master role can change everything, "
        "including the API keys, and the Configurator role can change everything except the keys.",
    ),
}

DEFAULT_TOPIC = "overview"


def _embed(topic: str) -> discord.Embed:
    title, body = TOPICS[topic]
    e = discord.Embed(title=f"{brand.NAME} help: {title}", description=body, color=brand.SAGE)
    e.set_footer(text="Pick a topic below for more.")
    return e


class _TopicSelect(discord.ui.Select["HelpView"]):
    def __init__(self, current: str):
        options = [
            discord.SelectOption(label=title, value=key, default=(key == current))
            for key, (title, _body) in TOPICS.items()
        ]
        super().__init__(placeholder="Jump to a topic…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        topic = self.values[0]
        view: HelpView = self.view  # type: ignore[assignment]
        view.set_topic(topic)
        await interaction.response.edit_message(embed=_embed(topic), view=view)


class HelpView(discord.ui.View):
    def __init__(self, *, owner_id: int, topic: str = DEFAULT_TOPIC):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.topic = topic
        self.add_item(_TopicSelect(topic))

    def set_topic(self, topic: str) -> None:
        self.topic = topic
        self.clear_items()
        self.add_item(_TopicSelect(topic))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Run /help yourself to browse this.", ephemeral=True)
            return False
        return True


async def send_help(interaction: discord.Interaction) -> None:
    view = HelpView(owner_id=interaction.user.id)
    await interaction.response.send_message(embed=_embed(DEFAULT_TOPIC), view=view, ephemeral=True)
