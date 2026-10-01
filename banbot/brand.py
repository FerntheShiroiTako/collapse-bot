"""Collapse's public name and colour palette, in one place.

Internal identifiers (the `banbot` package, the `banbot:review:` button custom_id prefix, the "banbot"
provider value stored in SQLite) deliberately keep the old name: renaming them would break persistent
buttons on already-posted case embeds and existing database rows. Only what users see is branded.

Colours are plain 0xRRGGBB ints, which discord.Embed accepts directly. The website uses the same five
values (site/css/styles.css).
"""
from __future__ import annotations

NAME = "Collapse"
EVASION_SOURCE = f"{NAME} ban-evasion check"

PINE = 0x1F3D3A    # deepest green - closed/resolved things
SAGE = 0x52796F    # primary brand colour - panels, help, lists, unverified cases
MINT = 0xA8C3B0    # soft green - ready / awaiting a decision
SAND = 0xE6DCC6    # cream - highest contrast on Discord's dark theme, so used for the most urgent state
UMBER = 0x3A2F2A   # dark brown - informational only (nearly invisible on Discord dark, on purpose)
