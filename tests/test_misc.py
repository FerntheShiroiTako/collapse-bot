import ast
from types import SimpleNamespace as NS
from pathlib import Path

import pytest

from banbot.core.nickname import ParseFailure, parse_nickname
from banbot.settings.config import ConfigError, GlobalConfig, RetryConfig

PACKAGE = Path(__file__).resolve().parent.parent / "banbot"


@pytest.mark.parametrize("nickname,username", [
    ("owl (@example_user)", "example_user"),
    ("Owl(@Roblox123)", "Roblox123"),
])
def test_nickname_tag_is_parsed(nickname, username):
    assert parse_nickname(nickname).username == username


@pytest.mark.parametrize("nickname,failure", [
    (None, ParseFailure.NO_NICKNAME),
    ("just a name", ParseFailure.NO_MATCH),
    ("owl (@_bad)", ParseFailure.INVALID_USERNAME),
    ("owl (@ab)", ParseFailure.INVALID_USERNAME),
])
def test_bad_nickname_tags_are_rejected(nickname, failure):
    assert parse_nickname(nickname).failure is failure


def test_retry_backoff_doubles_then_caps():
    r = RetryConfig(base_delay_s=30, max_delay_s=900)
    assert [r.delay_for(n) for n in range(1, 8)] == [30, 60, 120, 240, 480, 900, 900]


def test_config_requires_token_and_key():
    with pytest.raises(ConfigError):
        GlobalConfig.from_env({})


def test_config_rejects_batch_sizes_above_api_caps():
    with pytest.raises(ConfigError):
        GlobalConfig.from_env({"DISCORD_TOKEN": "t", "MASTER_KEY": "k", "ROTECTOR_BATCH_SIZE": "500"})


@pytest.mark.parametrize("layer", ["core", "settings", "storage", "integrations"])
def test_only_the_bot_layer_imports_discord(layer):
    """discord.py stays in banbot/bot and app.py; everything else is testable without it."""
    offenders = []
    for path in (PACKAGE / layer).rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            if any(n == "discord" or n.startswith("discord.") or n.startswith("banbot.bot") for n in names):
                offenders.append(path.name)
    assert offenders == []


def test_channel_check_asks_for_read_message_history():
    # Collapse edits a case message after it's resolved, which needs Read Message History.
    from banbot.bot.diagnostics import _check_channel

    def perms(**have):
        base = dict(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)
        base.update(have)
        return NS(**base)

    def check(p):
        channel = NS(permissions_for=lambda _me: p)
        return _check_channel(NS(get_channel=lambda _id: channel), None, 1, "Mod channel")

    assert check(perms()).ok
    result = check(perms(read_message_history=False))
    assert not result.ok and "Read Message History" in result.detail
