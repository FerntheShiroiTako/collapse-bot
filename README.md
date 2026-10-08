# Collapse

Discord bot that works out each member's Roblox account (from **Bloxlink**, falling back to a
`nickname (@robloxusername)` server nickname), checks it against every flag source on the **Rayward** API
(Rotector, RCR, TASE, RAB, Okappiki and ServerSweep), and puts anything worth a look in front of a moderator.

**The bot never bans anyone on its own.** Every flagged account is posted to the mod channel with
**Ban** / **Dismiss** buttons, and a ban happens only when a mod presses Ban.

**Collapse works in any server.** One bot process can moderate many Discord servers at once. Each server's own
admins connect their own Rayward/Bloxlink API keys and choose their own safety settings through `/setup`, so
nobody has to touch the bot's `.env` or restart the process just to add a server.

Safety rules baked in:

- **Inconclusive is never clean.** Any API error, timeout, missing entry or unknown status goes to a retry bucket,
  never to "no action". A Bloxlink outage cannot make a member look unflagged.
- **No automatic bans, ever.** There is no setting that lets the bot ban on its own. Every flagged account,
  Confirmed included, waits for a mod to press Ban.
- **Every ban gets an audit record** (Discord ID, Roblox ID, nickname at ban time, raw provider response,
  timestamp, decision path, approving mod).
- **Idempotent**: re-running a sweep never double-bans, double-posts or double-logs.
- A **report-only** mode is also available per server: detections are posted as plain notices with no buttons.
- Every new server starts in **dry run** (no real bans) until an admin turns it off in `/setup` or `/config`.

## Running the bot (once, for whoever hosts it)

Everything below happens inside this `bot/` folder. You need **Python 3.12 or newer**. The bot finds its
`.env`, `messages.txt` and database relative to this folder, so it doesn't matter where you start it from.

### Windows

1. Install Python 3.12+ from python.org (tick "Add python.exe to PATH").
2. `copy .env.example .env`, then fill in `DISCORD_TOKEN`. For `MASTER_KEY`, run `run.bat genkey` and paste
   the result.
3. Double-click `run.bat` (or run it from a terminal). The first run creates `.venv` and installs
   dependencies; after that it starts straight away. Close the window or press Ctrl+C to stop.

After pulling new code, run `run.bat update` once to refresh dependencies.

### Ubuntu VPS (22.04 or newer)

```bash
git clone <your repo> collapse && cd collapse/bot
chmod +x run.sh deploy/install-ubuntu.sh
./deploy/install-ubuntu.sh        # first run: installs Python 3.12 if needed and creates .env
nano .env                         # add DISCORD_TOKEN (MASTER_KEY is already filled in)
./deploy/install-ubuntu.sh        # second run: installs and starts the systemd service
```

Run the script as the normal user the bot should run as, not root; it uses `sudo` where needed. It installs
a `collapse` systemd service that starts on boot and restarts the bot if it crashes.

| Task | Command |
|---|---|
| Status | `systemctl status collapse` |
| Live logs | `journalctl -u collapse -f` |
| Restart (e.g. after editing `messages.txt`) | `sudo systemctl restart collapse` |
| Update | `git pull && ./deploy/install-ubuntu.sh` |
| Stop | `sudo systemctl stop collapse` |

To run it in the foreground instead (for testing), use `./run.sh`; it works the same way as `run.bat`.

### Manual setup

```bash
python -m venv .venv
.venv/bin/pip install -e .          # Windows: .venv\Scripts\pip install -e .
cp .env.example .env                # then fill it in; `python -m banbot genkey` makes a MASTER_KEY
.venv/bin/python -m banbot          # Windows: .venv\Scripts\python -m banbot
```

`.env` only holds **process-wide** settings now; there's no server-specific config in it at all.

| Key | Purpose |
|---|---|
| `DISCORD_TOKEN` | The bot's Discord application token |
| `MASTER_KEY` | Encrypts every server's Rayward/Bloxlink keys before they're written to SQLite. Generate one with `run.bat genkey` / `./run.sh genkey` (or `python -m banbot genkey`) and keep it secret; losing it means every server has to run `/setup` again. |
| `ROBLOX_API_ENABLED` | `false` stops every Roblox API call. Bloxlink-linked members are still checked (shown by Roblox id, not username); members identified only by a `(@username)` nickname tag can't be checked and count as having no linked account. Default `true`. |
| `ROBLOX_THUMBNAILS_ENABLED` | `false` drops the avatar pictures on case embeds. Pictures get one 5-second attempt, so a slow Roblox never holds up a case. Default `true`. |
| `RAYWARD_BASE_URL`, `ROBLOX_BASE_URL`, `BLOXLINK_BASE_URL` | API hosts, shared by every server |
| `BLOXLINK_MIN_INTERVAL_S`, `BLOXLINK_DAILY_LIMIT`, `BLOXLINK_DAILY_RESERVE` | Bloxlink pacing and its 2,000 requests/UTC-day quota (tracked separately per server's own key) |
| `SWEEP_BATCH_SIZE`, `ROBLOX_BATCH_SIZE`, `ROTECTOR_BATCH_SIZE` | Batch sizes (caps 200 / 100, verified) |
| `*_MIN_INTERVAL_S`, `HTTP_*`, `SWEEP_CHUNK_DELAY_S`, `BAN_DELAY_S` | Rate limiting and 429/5xx backoff |
| `RETRY_MAX_RETRIES`, `RETRY_BASE_DELAY_S`, `RETRY_MAX_DELAY_S`, `RETRY_POLL_INTERVAL_S` | Inconclusive-bucket retries (exponential backoff) |
| `MESSAGES_FILE` | One text file (default `messages.txt`, in this folder) holding the welcome post and the default ban DM, each under its own `[section]`; missing sections fall back to built-in text. Read at startup, so restart after editing. |
| `DB_PATH` | SQLite file (default `data/banbot.sqlite3`, relative to this folder), shared by every server and isolated internally by guild id |
| `RAW_RETENTION_HOURS` | After this, the flag reasons and raw response are deleted from the database and from case messages (Rayward's terms: 24 h at most, so this can't be set higher). The cleanup runs every 10 minutes and acts early enough that nothing passes the limit. |
| `REMOVED_GUILD_RETENTION_DAYS` | Days (default 30) after the bot is removed from a server before everything stored about it is deleted. Its API keys are wiped straight away. |

**Terms and privacy:** the site has a terms of service (`#terms`) and privacy policy (`#privacy`). Put their full URLs, `https://collapseproject.uk/#terms` and `https://collapseproject.uk/#privacy`, in the Developer Portal under General Information. The bot follows Rayward's terms (rayward.app/terms): flag details are deleted after 24 hours (`RAW_RETENTION_HOURS` can't exceed 24), Ban checks every source again before banning, and ban DMs name the source the ban is based on.

**Discord developer portal, once:** enable the **Server Members Intent** under Bot → Privileged Gateway
Intents. It's required; without it the bot can't see who's actually in a server to check them. Then generate
an invite URL with the `bot` + `applications.commands` scopes and these permissions:

| Permission | What it's for |
|---|---|
| **Ban Members** | the only permission actually used to remove anyone, and it's server-wide, not per-channel |
| **View Channel**, **Send Messages**, **Embed Links**, **Read Message History** | posting review cases and summaries, needed wherever a server points the bot: the mod channel, the summary channel, and the detection log forum if one's set. Easiest is granting all three on the bot's role server-wide, so whichever channels get picked later are already covered |

Anyone can then add the bot to their own server with that invite link. No code change or restart needed per
server. One thing Discord doesn't expose as an invite permission: the bot's own role has to sit **above** the
mod role (and generally above anyone it might need to ban) in Role settings, or the ban itself will fail even
with Ban Members granted. `/setup`'s **Test & Finish** checks all of this, role position included, before
marking a server ready, and `/config` can re-run the same check any time.

## Adding Collapse to a server (per server, by that server's own admins)

1. Invite the bot using the link above.
2. Run **`/setup`** (requires the **Manage Server** permission; see **Who can run /setup and /config** below
   to delegate this without handing that permission out). It opens a panel where you:
   - paste your own **Rayward** key (from <https://rayward.app/signin>), required;
   - optionally paste a **Bloxlink** server key (from <https://blox.link/dashboard/developers>, scoped to
     this server) - leave it unset and the bot falls back to nickname parsing alone;
   - pick the **mod role** (who can press Ban/Dismiss) and **mod channel** (where cases are posted);
   - open **More Settings** for the less-common options: a **summary channel**, specific **trigger users**,
     and an optional **detection log forum**. Pick a Discord *forum* channel there and every detection gets
     its own thread, a permanent searchable archive kept separate from the mod queue;
   - leave **Dry run** on until you've read what it does (below).
3. Click **Test & Finish**. It makes a real (harmless) call with each key, checks the bot's permissions and
   role position in your server, and only marks setup complete once everything passes.
4. Re-open the same panel any time with **`/config`** to change anything: keys, roles, channels, or the
   safety switches, without redoing the whole wizard. Make sure the bot's own role sits **above** the mod
   role, or it won't be able to ban anyone with it.

### Who can run /setup and /config

By default that's anyone with Discord's own **Manage Server** permission. To delegate it without handing
that out, an admin (Manage Server, or Administrator) opens **Access Control** in the panel and sets:

- **Master role**: full access. Every setting, including pasting or replacing the two API keys, and who
  holds this role or the Configurator role. Basically Manage Server, just scoped to this bot.
- **Configurator role**: access to everything *except* the API keys. Mod role/channel, trigger role/users,
  the detection log forum, every safety-mode toggle. Someone with only this role sees no more than
  "Set"/"Not set" for a key, and has no way to view, replace or clear it.

Holding either role is enough on its own (no Manage Server needed); a member with both is treated as Master.
Leaving both unset (the default) means only Manage Server/Administrator can configure the bot, same as
before this existed.

## How a check works

**1. Identify the Roblox account** ([banbot/core/identity.py](banbot/core/identity.py)), in priority order:

| Source | How | Result |
|---|---|---|
| **Bloxlink** | `GET api.blox.link/v4/public/guilds/{guild}/discord-to-roblox/{user}` | The member verified this account, so it wins outright. Gives a Roblox ID; usernames are then fetched in bulk via `POST users.roblox.com/v1/users` (max 200/request). |
| **Nickname** | trailing `(@username)`, validated against Roblox username rules (3–20 chars, letters/digits, at most one `_`, not first/last) | Used only when Bloxlink has no link. Resolved to an ID via `POST /v1/usernames/users` (max 200/request). |

The distinction that matters: Bloxlink saying **"this member never verified"** is a real answer, so the
nickname is tried next, and if that fails too, the member is *Unresolved* (logged, no action). Bloxlink
**failing** (network error, bad key, rate limit, or a response shape the client can't read) is *not* an
answer, so it becomes *Inconclusive* and gets retried. A member is never skipped because a lookup broke, and
a Bloxlink outage never silently falls through to a nickname a bad actor controls.

Which source was used is stored on the case and shown to mods in the embed, so an unverified nickname match is
never mistaken for a verified link.

**Bloxlink's daily quota.** Every lookup is counted in SQLite against the UTC day (so a restart can't reset the
count). A sweep may spend down to `BLOXLINK_DAILY_RESERVE` and no further; join checks may
use the reserve. When a sweep can't pay for its next chunk it **pauses until the UTC reset** and tells the mod
channel; members are never failed into the retry bucket just because the bot ran out of quota. If a join check
lands after the quota is gone, the member is parked until the reset without using up one of their retry attempts.
At sweep start, if the member count exceeds today's remaining budget, the bot posts how many days the sweep will
take. `/sweep status` shows the live count.

**2. Look the account up on every Rayward source**: Rotector, RCR, TASE, RAB, Okappiki and ServerSweep
(`POST /v2/lookup/<source>/roblox/user` for each, batched, max 100 ids, all using *this server's own* Rayward key).
The list, with each source's name and appeal link, is `RAYWARD_SOURCES` in [banbot/core/flags.py](banbot/core/flags.py).
All six share one status mapping, the `FLAG_TYPES` table in
[banbot/integrations/rayward.py](banbot/integrations/rayward.py). The others return a subset of Rotector's
statuses: RCR 0, 1, 2 and 5; TASE, RAB, Okappiki and ServerSweep only 0 and 2.

| `flagType` | As shipped | With report-only mode on |
|---|---|---|
| 0 Unflagged | Clear – no action | Nothing (counted as Clear) |
| 2 Confirmed | **Review queue, with buttons** | Posted to mod channel |
| 1 Flagged, 5 Mixed, 3 Queued, 4 Provisional Flag, 8 Redacted, 10 Awaiting Human Review | **Review queue, with buttons** | Posted to mod channel |
| 6 Past Offender | Allowed, logged only | Posted to mod channel (informational) |
| error / timeout / 503 / missing / anything else | Inconclusive → retried → review queue | Inconclusive → retried → posted if still unverified |

Unknown `flagType` values are logged at ERROR and treated as inconclusive, never as clean.

When the sources disagree, the most serious answer wins (`combine()` in
[banbot/core/flags.py](banbot/core/flags.py)): Confirmed, then Flagged/Mixed/etc., then *couldn't check*, then
Past Offender, then Unflagged. So a flag from **any** source goes to the mods, and an account that one source can't check is retried, never
treated as clean, even if every other source says Unflagged. The case names the source that flagged it, and
lists any other sources that flag it too.

**3. A mod presses Ban** → the member is DMed, then banned, then an audit record is written. Nothing else bans.

The same pipeline runs for every member on join, in every server the bot has completed `/setup` in.

### Ban evasion

Discord bots have no access to IP addresses at all. That's Discord's own Trust & Safety territory, not
something any bot can see. What Collapse *can* do, and does automatically: if a member's resolved Roblox
account was already banned in this server under a **different** Discord account, that counts as a
Confirmed-equivalent hit: it lands in the review queue with the reason "Ban evasion" and the prior account
named (or, in report-only mode, is posted as a notice). The flag lookup isn't even consulted for
these; a returning banned account is already reason enough.
This runs on every join and every sweep, for every server, with no extra setup.

## Running the sweep

`/sweep start` runs it, restricted to the trigger role/user(s) chosen in `/setup`. It walks every non-bot
member in ascending ID order in chunks, persists its cursor after each chunk, and only one sweep can be
active per server at a time. If the bot crashes or is restarted, every server's in-progress sweep resumes
automatically on startup (or via `/sweep resume`); members already processed are skipped.

After the main pass the sweep waits for its inconclusive members to be retried (exponential backoff, up to
`RETRY_MAX_RETRIES`). Members that still can't be verified are **escalated to the review queue** and counted
as inconclusive. Then a summary is posted to the summary channel and, unless turned off in `/config`, DMed to
whoever ran `/sweep start`:

```
**Sweep #3 complete** · 1234 members, 14m07s
Clear: **1120**
No linked account: **87**
Past offender: **4**
Sent to review: **21**
Unverified: **2**
-# Rotector, RCR, TASE, RAB, Okappiki and ServerSweep via Rayward
```

Under report-only, `Sent to review` is replaced by `Reported`. A sweep never bans anyone, so there's no ban
count; bans from Ban-button presses land in the audit log.

Other commands: `/sweep status`, `/sweep resume`, `/sweep abort`, `/check @member` (run the pipeline for one
member), `/reviews` (paginated list of *open* cases, 10 per page with Prev/Next), `/detections` (every
detection ever recorded, any status - see below), `/help` (command summary).

## Detection log (optional forum archive)

Set a forum channel under `/config` → **More Settings** → **Detection log forum**, and every detection that
would go to the review queue or a report also gets its own thread there (title: `#<id> · <roblox username> ·
<status>`), posted once, when first detected. An open case's thread carries the same **Ban** / **Dismiss**
buttons as the mod-channel post, acting on the same case with the same permission checks, so mods can work
from either place. The thread is otherwise never edited or removed. Collapse keeps no reference to it, so
when a case is resolved in the mod channel, the thread's buttons stay until someone presses one: that
press says the case is already resolved and removes them. Leave it unset to skip this entirely; nothing
changes about how cases are handled.

**Bot owner.** The Discord user IDs in `BOT_OWNER_IDS` ([banbot/settings/config.py](banbot/settings/config.py))
are full admins in every server the bot is in, whatever that server's settings say: `/setup` and `/config`,
Ban/Dismiss in the mod channel and the forum, `/reviews`, `/detections`, `/check` and `/sweep`.

**Tags.** Once a forum channel is set, `/config` → **More Settings** → **Detection Log Tags** lets you map
each detection category to a tag on that forum. Create the tags themselves on the forum channel first
(Discord: right-click the channel → Edit Channel → Tags), then pick which one applies to **Confirmed**,
**Review**, **Past Offender**, **Ban Evasion** and **Reported** threads - a category left unmapped just
posts without a tag. `/setup`'s **Test & Finish** flags any mapped tag that's since been deleted from the
forum channel, so a stale mapping never posts silently untagged without you knowing.

For a full export instead of a live archive, `/detections` covers every row ever written to the review queue
for the server (pending, approved, denied, and reported) in two forms, picked with its `format` option:

- **CSV file** (default) — one row per detection: when, the Discord and Roblox accounts, status, reason and
  identification method in the same plain-English wording mods see in the review embeds, queue status, and
  who resolved it and when. Opens straight into a spreadsheet.
- **Username list** — a quick paginated embed of just the distinct Roblox usernames that have ever been
  detected (deduplicated, alphabetical), for a fast "who's in here" look without downloading anything.

## Review queue

Cases are posted to the mod channel as an embed (member, Roblox account, status, why it's here, how the
account was linked, and any provider detail) with **Ban** / **Dismiss** buttons. If the flagged Roblox
account has a renderable avatar, the embed's thumbnail shows their current headshot (Roblox's public
thumbnail API, no key needed). This is purely cosmetic: a failed or missing lookup just means the embed
posts with no picture, never a delay to the detection itself.

- Only members with the mod role can press the buttons. The role is checked server-side at click time, so
  editing the message or replaying the interaction gets you nothing; everyone else gets an ephemeral "you
  don't have permission to do that".
- **Ban** → the member is DMed (see below), then banned, then an audit record is written naming the approving mod.
- **Dismiss** → logged (who, when), no action.
- Once resolved, the embed title changes to **"Resolved · Case #N"**, the outcome line is appended (who
  acted and what happened), and the buttons are removed, so a case a mod has already looked at can't be
  double-clicked or mistaken for one still awaiting a decision.
- One pending case per member + Roblox ID, so a re-run never duplicates. Cases live in SQLite and the buttons keep
  working across restarts (persistent `DynamicItem` handlers); anything that failed to reach Discord is re-posted
  on the next startup.

Every case reaches this queue, `Confirmed` included. The bot has no way to ban on its own. That matches
Rotector's own terms, which note that flags "should be reviewed by a human
before you act on them".

## Ban DM

Right before a member is banned (a mod pressed **Ban**), the bot DMs them. Each server can switch this
off with the **Ban DM** toggle in `/config` → **Safety Modes**. The text is the `[ban_dm]` section of
`messages.txt` (in the project root; restart the bot after editing it) for a ban based on Rotector, `[ban_dm_other]`
for one based on any other source, and `[ban_dm_evasion]` for ban evasion, each falling back to a built-in default. Ban checks
the account again first and uses the source that flags it *now*, so the DM always names the right source.

Placeholders, filled in per ban:

| Placeholder | Value |
|---|---|
| `{server}` | The guild's name |
| `{roblox_username}` | The Roblox username the ban is based on |
| `{roblox_id}` | The Roblox user ID |
| `{status}` | The source's status for the account, e.g. `Confirmed` |
| `{appeal}` | "You can appeal this ban here: <link>", using the server's appeal link; empty if none is set |
| `{source}` | `[ban_dm_other]` only: the source's name, e.g. `RCR (Roblox Criminal Records)` |
| `{source_appeal}` | `[ban_dm_other]` only: where to appeal with that source |

### Appeal link

Each server can point banned members at its own appeals server or form: `/config` → **More Settings** →
**Appeal Link**. It accepts a full URL or a Discord invite (`discord.gg/...` works without `https://`);
leave it blank to remove it. Masters and Configurators can both set it. The link goes wherever `{appeal}`
sits in the ban DM, and if the text has no `{appeal}` it's added at the end, so a server's link is never
dropped.

Turn the **Ban DM** toggle off in `/config` to send nothing. A DM that fails (the member has DMs closed,
blocked the bot, or already left) never blocks or delays the ban; the attempt and outcome get recorded on the
audit row (`dm_sent`, `dm_error`) either way. In dry-run mode no DM is sent, since no ban happens.

## Turning bans off again

Each server controls its own safety switches from `/config` → **Safety Modes** (turning off *Dry run* asks
for a second confirmation, since it makes a mod's Ban press real):

- **Dry run ON** (the default for every new server): the full pipeline still runs and cases still get
  buttons, but every approved ban becomes a "would ban" log line plus an audit row. Inspect
  them with `sqlite3 data/banbot.sqlite3 "select * from audit_log where guild_id = <your server id>"`.
- **Report only ON**: no bans, no buttons. Detections are posted as plain notices for mods to act on
  manually. One notice per member + Roblox ID + status; a re-run bumps `seen_count` rather than re-posting,
  and a status change (e.g. `Flagged` → `Confirmed`) posts a fresh notice.

Changes from `/config` take effect immediately, no restart needed. `/config` also shows the server's current
settings at a glance, and `/setup`'s **Test & Finish** button (also reachable from `/config`) re-checks both
API keys and the bot's Discord permissions on demand.

## Upgrading

The bot's schema is defined in `storage/store.py`'s `SCHEMA`; on startup any missing table or index is created,
and columns added in newer versions are added to older databases (`Store._migrate`). Just restart. An in-progress sweep resumes from where it stopped, in
every server, automatically.

## Deleting data

The privacy policy promises deletion on request, so the bot has two commands. Each only lists what it would
delete until you add `--yes`, and both are safe to run while the bot is running.

```bash
python -m banbot purge-user 123456789012345678          # everything stored about one Discord user
python -m banbot purge-user 123456789012345678 --yes    # actually delete it
python -m banbot purge-guild 123456789012345678 --yes   # everything stored about one server
```

`purge-user` deletes the person's checks, cases, bans and audit rows in every server. Where they only appear as
the moderator behind a decision, their ID is removed and the row stays. Case messages already posted in Discord
aren't touched; that server's moderators can delete them.

You rarely need `purge-guild`: when the bot is removed from a server its API keys are wiped immediately, and
the rest of its data is deleted automatically after `REMOVED_GUILD_RETENTION_DAYS`.

## Code layout

```
banbot/
  __main__.py      entry point (python -m banbot [genkey])
  app.py           builds one App (pipeline, review queue, sweeps...) per server
  brand.py         product name and palette
  core/            moderation logic, no discord.py: pipeline, identity, review, enforcement, sweep, retries
  integrations/    HTTP clients: Rayward (all six sources), Bloxlink, Roblox, shared request plumbing
  settings/        .env config, per-server settings, messages.txt handling
  storage/         SQLite store and API-key encryption
  bot/             discord.py layer: client and commands, embeds and buttons, /setup panel, /help
tests/             pytest suite
deploy/            Ubuntu install script and systemd unit
```

`docs/Collapse-Code-Guide.pdf` (in the repo root) walks through all of it in detail.

## Tests

```bash
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip install -e ".[dev]"
.venv/bin/python -m pytest
```

The suite covers pipeline routing (flagged accounts go to mods, failures are never clean, the pipeline can't
ban), message rendering and appeal links, the database's idempotency guarantees and migrations, and that
discord.py stays inside `banbot/bot`.
