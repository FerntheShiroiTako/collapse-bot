"""Wires the pieces together.

`build_app` builds one guild's pipeline out of already-constructed adapters (used directly by tests,
which build fakes). `AppRegistry` is what the bot actually uses: it lazily builds and caches one `App`
per guild, out of that guild's own Rayward/Bloxlink keys (store.py, encrypted at rest) plus the
process-wide GlobalConfig. A guild with no /setup yet, or an incomplete one, has no App - callers get
None and should point the user at /setup.
"""
from __future__ import annotations

from dataclasses import dataclass

import aiohttp
import discord

from banbot.integrations.bloxlink import BloxlinkClient, HttpBloxlinkClient
from banbot.core.budget import DailyBudget
from banbot.settings.config import Config, GlobalConfig
from banbot.bot.adapters import DiscordGateway, DiscordReviewPoster
from banbot.core.enforcement import Banner
from banbot.settings.guild import build_guild_config
from banbot.core.identity import IdentityResolver
from banbot.core.flags import RAYWARD_SOURCES, CombinedProvider, FlagProvider
from banbot.integrations.rayward import RaywardSourceProvider
from banbot.core.gateway import Gateway
from banbot.integrations.net import AiohttpRequester, Throttle, ThreadedHttpRequester
from banbot.core.pipeline import Pipeline
from banbot.core.retrier import InconclusiveRetrier
from banbot.core.review import ReviewPoster, ReviewQueue
from banbot.integrations.roblox import HttpRobloxResolver, RobloxResolver
from banbot.storage.store import Store
from banbot.core.sweep import SweepRunner
from banbot.integrations.thumbnails import HttpRobloxThumbnailClient, RobloxThumbnailClient
from banbot.util import Clock, SystemClock


@dataclass
class App:
    cfg: Config
    store: Store
    gateway: Gateway
    clock: Clock
    banner: Banner
    review_queue: ReviewQueue
    pipeline: Pipeline
    retrier: InconclusiveRetrier
    sweeps: SweepRunner


def build_app(
    cfg: Config,
    *,
    store: Store,
    gateway: Gateway,
    poster: ReviewPoster,
    resolver: RobloxResolver | None,
    provider: FlagProvider,
    bloxlink: BloxlinkClient | None = None,
    thumbnails: RobloxThumbnailClient | None = None,
    clock: Clock | None = None,
    ban_dm_templates: dict[str, str] | None = None,
) -> App:
    clock = clock or SystemClock()
    banner = Banner(
        guild_id=cfg.guild_id, store=store, gateway=gateway, clock=clock, dry_run=cfg.dry_run,
        ban_delay_s=cfg.rate_limit.ban_delay_s, dm_templates=ban_dm_templates, appeal_url=cfg.appeal_url,
    )
    review_queue = ReviewQueue(
        guild_id=cfg.guild_id, store=store, poster=poster, banner=banner, gateway=gateway, clock=clock,
        mod_role_id=cfg.mod_role_id, report_only=cfg.report_only, thumbnails=thumbnails, provider=provider,
    )
    budget = DailyBudget(
        guild_id=cfg.guild_id, store=store, clock=clock, api="bloxlink",
        limit=cfg.bloxlink_daily_limit, reserve=cfg.bloxlink_daily_reserve,
    )
    identity = IdentityResolver(resolver=resolver, bloxlink=bloxlink, gateway=gateway, bloxlink_budget=budget)
    pipeline = Pipeline(
        cfg=cfg, store=store, gateway=gateway, identity=identity, provider=provider,
        review_queue=review_queue, clock=clock,
    )
    retrier = InconclusiveRetrier(guild_id=cfg.guild_id, store=store, gateway=gateway, pipeline=pipeline, clock=clock)
    sweeps = SweepRunner(cfg=cfg, store=store, gateway=gateway, pipeline=pipeline, retrier=retrier, clock=clock)
    return App(
        cfg=cfg, store=store, gateway=gateway, clock=clock, banner=banner, review_queue=review_queue,
        pipeline=pipeline, retrier=retrier, sweeps=sweeps,
    )


def build_guild_http_clients(
    cfg: Config, session: aiohttp.ClientSession
) -> tuple[CombinedProvider, BloxlinkClient | None]:
    """The two integrations that are keyed per-guild: Rayward (always) and Bloxlink (optional)."""
    rl = cfg.rate_limit
    # One requester, so both sources share one throttle: they're the same key on the same host.
    rayward_req = AiohttpRequester(
        session, throttle=Throttle(rl.rotector_min_interval_s), timeout_s=rl.http_timeout_s,
        max_retries=rl.http_max_retries, backoff_base_s=rl.http_backoff_base_s, backoff_max_s=rl.http_backoff_max_s,
        name=f"rayward[{cfg.guild_id}]",
    )
    provider = CombinedProvider([
        RaywardSourceProvider(rayward_req, source=source, api_key=cfg.rayward_api_key,
                              base_url=cfg.rayward_base_url, batch_size=cfg.rotector_batch_size)
        for source in RAYWARD_SOURCES
    ])

    bloxlink: BloxlinkClient | None = None
    if cfg.bloxlink_api_key:
        bloxlink_req = AiohttpRequester(
            session, throttle=Throttle(rl.bloxlink_min_interval_s), timeout_s=rl.http_timeout_s,
            max_retries=rl.http_max_retries, backoff_base_s=rl.http_backoff_base_s,
            backoff_max_s=rl.http_backoff_max_s, name=f"bloxlink[{cfg.guild_id}]",
        )
        bloxlink = HttpBloxlinkClient(
            bloxlink_req, api_key=cfg.bloxlink_api_key, base_url=cfg.bloxlink_base_url, guild_id=cfg.guild_id
        )
    return provider, bloxlink


def build_shared_roblox_resolver(global_cfg: GlobalConfig, session: aiohttp.ClientSession) -> RobloxResolver | None:
    """Roblox's username/id lookup takes no key and is rate-limited by source IP, not per guild, so every
    guild shares one resolver (and one throttle) instead of each hammering Roblox independently.

    On aiohttp specifically, requests to /v1/users and /v1/usernames/users have been observed to hang
    for the full timeout while curl and stdlib http.client succeed instantly against the same host from
    the same machine - see integrations/net.py's ThreadedHttpRequester. Bloxlink and Rayward are
    unaffected and stay on AiohttpRequester (build_guild_http_clients, below)."""
    if not global_cfg.roblox_api_enabled:
        return None
    rl = global_cfg.rate_limit
    roblox_req = ThreadedHttpRequester(
        throttle=Throttle(rl.roblox_min_interval_s), timeout_s=rl.http_timeout_s,
        max_retries=rl.http_max_retries, backoff_base_s=rl.http_backoff_base_s, backoff_max_s=rl.http_backoff_max_s,
        name="roblox",
    )
    return HttpRobloxResolver(roblox_req, base_url=global_cfg.roblox_base_url, batch_size=global_cfg.roblox_batch_size)


def build_shared_thumbnail_client(
    global_cfg: GlobalConfig, session: aiohttp.ClientSession
) -> RobloxThumbnailClient | None:
    """Same reasoning as the resolver above: no per-guild key, so one shared client and throttle, and
    http.client rather than aiohttp, which Roblox's edge drops from this host (thumbnails.roblox.com too).

    A case is posted only after its picture lookup returns, and the picture is cosmetic, so this gets one
    attempt with a short timeout instead of the usual retries: a slow Roblox costs a case 5 seconds at
    most, never the ~90 the retry loop took."""
    if not (global_cfg.roblox_api_enabled and global_cfg.roblox_thumbnails_enabled):
        return None
    rl = global_cfg.rate_limit
    thumb_req = ThreadedHttpRequester(
        throttle=Throttle(rl.roblox_thumbnail_min_interval_s), timeout_s=min(rl.http_timeout_s, 5.0),
        max_retries=0, backoff_base_s=rl.http_backoff_base_s, backoff_max_s=rl.http_backoff_max_s,
        name="roblox-thumbnails",
    )
    return HttpRobloxThumbnailClient(thumb_req, base_url=global_cfg.roblox_thumbnails_base_url)


class AppRegistry:
    """Lazily builds and caches one App per guild. Call invalidate() after /setup or /config changes
    anything that feeds into Config or the HTTP clients (keys, batch/base-url settings)."""

    def __init__(
        self, *, client: discord.Client, global_cfg: GlobalConfig, store: Store, session: aiohttp.ClientSession,
        texts: dict[str, str],
    ):
        self._client = client
        self._global_cfg = global_cfg
        self._store = store
        self._session = session
        self._texts = texts  # the loaded messages file
        self._resolver = build_shared_roblox_resolver(global_cfg, session)
        self._thumbnails = build_shared_thumbnail_client(global_cfg, session)
        self._apps: dict[int, App] = {}

    @property
    def global_cfg(self) -> GlobalConfig:
        return self._global_cfg

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    def get(self, guild_id: int) -> App | None:
        cached = self._apps.get(guild_id)
        if cached is not None:
            return cached
        settings = self._store.get_guild_settings(guild_id)
        if settings is None or not settings.is_ready:
            return None
        cfg = build_guild_config(self._global_cfg, settings)
        gateway = DiscordGateway(self._client, guild_id)
        poster = DiscordReviewPoster(
            gateway, cfg.mod_channel_id, log_forum_channel_id=cfg.log_forum_channel_id, tag_map=cfg.forum_tag_ids,
        )
        provider, bloxlink = build_guild_http_clients(cfg, self._session)
        dm_templates = settings.ban_dm_templates(self._texts)
        app = build_app(
            cfg, store=self._store, gateway=gateway, poster=poster, resolver=self._resolver,
            provider=provider, bloxlink=bloxlink, thumbnails=self._thumbnails, ban_dm_templates=dm_templates,
        )
        self._apps[guild_id] = app
        return app

    def invalidate(self, guild_id: int) -> None:
        self._apps.pop(guild_id, None)

    def configured_guild_ids(self) -> list[int]:
        return self._store.configured_guild_ids()
