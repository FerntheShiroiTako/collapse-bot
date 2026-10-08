"""Switching the Roblox API off, and the bot owner's override."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from banbot.app import build_shared_roblox_resolver, build_shared_thumbnail_client
from banbot.core.gateway import MemberInfo
from banbot.core.identity import Identified, IdentityResolver, IdentitySource, Unidentified
from banbot.integrations.bloxlink import BloxlinkLink, BloxlinkNoLink
from banbot.settings.config import BOT_OWNER_IDS, ConfigError, GlobalConfig

ENV = {"DISCORD_TOKEN": "t", "MASTER_KEY": "k"}


def test_roblox_switches_default_on_and_parse():
    assert GlobalConfig.from_env(ENV).roblox_api_enabled is True
    cfg = GlobalConfig.from_env({**ENV, "ROBLOX_API_ENABLED": "false", "ROBLOX_THUMBNAILS_ENABLED": "off"})
    assert (cfg.roblox_api_enabled, cfg.roblox_thumbnails_enabled) == (False, False)
    with pytest.raises(ConfigError):
        GlobalConfig.from_env({**ENV, "ROBLOX_API_ENABLED": "maybe"})


def test_switched_off_roblox_builds_no_clients():
    off = GlobalConfig.from_env({**ENV, "ROBLOX_API_ENABLED": "false"})
    assert build_shared_roblox_resolver(off, session=None) is None
    assert build_shared_thumbnail_client(off, session=None) is None  # thumbnails need Roblox too
    no_pics = GlobalConfig.from_env({**ENV, "ROBLOX_THUMBNAILS_ENABLED": "false"})
    assert build_shared_thumbnail_client(no_pics, session=None) is None
    assert build_shared_roblox_resolver(no_pics, session=None) is not None


class FakeBloxlink:
    def __init__(self, links):
        self.links = links

    async def lookup(self, discord_id):
        rid = self.links.get(discord_id)
        return BloxlinkLink(discord_id, rid, {}) if rid else BloxlinkNoLink(discord_id, "not verified")


def test_without_roblox_bloxlink_members_are_still_checked_and_nickname_ones_skipped():
    identity = IdentityResolver(resolver=None, bloxlink=FakeBloxlink({1: 895565849}), gateway=None)
    linked, tagged = asyncio.run(identity.identify([MemberInfo(1, None), MemberInfo(2, "Bob (@bob)")]))
    assert isinstance(linked, Identified) and linked.user.id == 895565849
    assert linked.source is IdentitySource.BLOXLINK
    assert isinstance(tagged, Unidentified) and "switched off" in tagged.detail


def test_bot_owner_can_moderate_without_the_mod_role():
    from banbot.core.review import ReviewQueue
    queue = ReviewQueue(guild_id=1, store=None, poster=None, banner=None, gateway=None, clock=NS(), mod_role_id=7)
    owner = next(iter(BOT_OWNER_IDS))
    assert queue.can_moderate([], owner)
    assert not queue.can_moderate([], 123)
    assert queue.can_moderate([7], 123)
