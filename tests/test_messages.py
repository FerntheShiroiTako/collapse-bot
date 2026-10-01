from pathlib import Path

import pytest

from banbot.settings import messages

BOT_DIR = Path(__file__).resolve().parent.parent
VALUES = dict(server="Test Server", roblox_username="example_user", roblox_id=1, status="Confirmed")


def test_parse_sections_and_ignore_notes():
    text = "notes up here\n[welcome] - not a header\n\n[welcome]\nHi!\n\n[ban_dm]\nBye {server}\n"
    assert messages.parse(text) == {"welcome": "Hi!", "ban_dm": "Bye {server}"}


def test_shipped_messages_file_has_every_section():
    found = messages.load(BOT_DIR / "messages.txt")
    assert set(found) == {"welcome", "ban_dm", "ban_dm_evasion"}
    assert "{appeal}" in found["ban_dm"]


def test_rotector_ban_dms_name_rotector_and_evasion_dms_do_not():
    # Rayward's terms: an action on its data must name the source, so the member knows who to appeal to.
    found = messages.load(BOT_DIR / "messages.txt")
    for text in (found["ban_dm"], messages.DEFAULT_BAN_DM):
        assert "Rotector" in text and "rotector.com" in text
    for text in (found["ban_dm_evasion"], messages.DEFAULT_BAN_DM_EVASION):
        assert "Rotector" not in text


def test_missing_file_falls_back(tmp_path):
    assert messages.load(tmp_path / "nope.txt") == {}


def test_appeal_line_fills_placeholder():
    out = messages.render_ban_dm("Banned from {server}.\n\n{appeal}\n\nBye.", appeal_url="https://discord.gg/x", **VALUES)
    assert out == "Banned from Test Server.\n\nYou can appeal this ban here: https://discord.gg/x\n\nBye."


def test_no_appeal_link_leaves_no_gap():
    out = messages.render_ban_dm("Banned from {server}.\n\n{appeal}\n\nBye.", **VALUES)
    assert out == "Banned from Test Server.\n\nBye."


def test_appeal_line_appended_when_template_lacks_placeholder():
    out = messages.render_ban_dm("Banned.", appeal_url="https://discord.gg/x", **VALUES)
    assert out.endswith("You can appeal this ban here: https://discord.gg/x")


def test_unknown_placeholder_is_left_alone():
    assert messages.render_ban_dm("{server} {oops}", **VALUES) == "Test Server {oops}"


def test_default_ban_dm_renders_cleanly():
    out = messages.render_ban_dm(messages.DEFAULT_BAN_DM, **VALUES)
    assert "{" not in out and not out.endswith("\n")


@pytest.mark.parametrize("raw,expected", [
    ("", None),
    ("  ", None),
    ("discord.gg/abc123", "https://discord.gg/abc123"),
    ("discord.com/invite/Abc", "https://discord.com/invite/Abc"),
    ("https://forms.gle/xyz", "https://forms.gle/xyz"),
])
def test_appeal_url_normalisation(raw, expected):
    assert messages.normalize_appeal_url(raw) == expected


@pytest.mark.parametrize("raw", ["not a link", "ftp://x.y", "https://" + "a" * 250 + ".com"])
def test_appeal_url_rejects_bad_input(raw):
    with pytest.raises(ValueError):
        messages.normalize_appeal_url(raw)
