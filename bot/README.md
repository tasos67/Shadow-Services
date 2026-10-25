# AllInOne — Discord Bot

An all-in-one Discord bot built with **Python 3.11+** and **discord.py 2.x**.
It bundles moderation, ticketing, welcome/leave, reaction roles, giveaways,
security, logging, leveling, economy and a large set of utility features into a
single, fully slash-command driven bot.

Every feature is configurable **per server** through slash commands and a guided
`/setup` wizard. All state is stored locally in SQLite — no external services
required.

---

## Table of contents

- [Feature overview](#feature-overview)
- [Requirements](#requirements)
- [Creating the bot (Developer Portal)](#creating-the-bot-developer-portal)
- [Privileged intents](#privileged-intents)
- [Inviting the bot](#inviting-the-bot)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the bot](#running-the-bot)
- [Project structure](#project-structure)
- [Commands](#commands)
- [Troubleshooting](#troubleshooting)

---

## Feature overview

| System | Highlights |
| --- | --- |
| 🧭 **Core** | `/ping`, `/help`, `/botinfo`, `/serverinfo`, `/userinfo`, `/avatar`, `/settings`, `/setup` |
| 🎫 **Tickets** | Button / select / dropdown panels, private channels, claim, add/remove, rename, transfer, transcripts (HTML + text), inactivity auto-close, 1–5 star ratings, per-user limits |
| 👋 **Welcome & Leave** | Embed / text / generated image-card messages, placeholders, DM welcome, autorole (humans & bots), button + math-captcha verification |
| 🎭 **Reaction Roles** | Reaction-emoji, button and select-menu role panels, persistent across restarts |
| 🎉 **Giveaways** | Requirements (roles, account age, level), bypass roles, blacklist, pause/resume, reroll, live entry count, restart-safe |
| 🔒 **Security** | Anti-spam, anti-invite, anti-link, anti-caps, anti-mention, bad-words, anti-raid, anti-nuke (audit-log based), whitelist, lockdown, warning escalation |
| 🛡️ **Moderation** | Ban / tempban / unban / kick / softban / timeout / warn, purge, notes, full case system with sequential case IDs and tempban expiry task |
| 📜 **Logging** | Dedicated log channel per event type: messages, members, channels, roles, voice, invites, joins/leaves, server updates |
| 📈 **Leveling** | Message + voice XP, configurable curve and cooldown, leaderboards, per-level role rewards, generated rank cards |
| 💰 **Economy** | Cash / bank, daily streaks, work, beg, gambling, shop with role items, inventory, leaderboards |
| 🤖 **Custom Commands** | Custom prefix-style commands, tags and keyword auto-responders |
| 💡 **Suggestions** | Suggestion channels with voting, staff approve/deny/implement and notes |
| ⭐ **Starboard** | Automatic highlighting of the best messages with a configurable threshold |
| 🛠️ **Tools** | Reminders, polls, AFK status |
| 🎂 **Birthdays** | Per-server birthday registration and daily announcements |
| 🔊 **Voice** | Join-to-create temporary voice channels with ownership controls |
| 🎲 **Fun & Utility** | 8ball, coinflip, dice, choose, rock-paper-scissors, meme, trivia, embed builder, announce, say, roleinfo, channelinfo, icon, banner |
| 🧩 **Games & Extras** | Counting game, sticky messages, auto-publish for announcement channels |

---

## Requirements

- **Python 3.11 or newer** (3.12 recommended).
- A Discord application / bot token (free).
- The Python packages in [`requirements.txt`](requirements.txt):

  ```
  discord.py>=2.4
  aiosqlite>=0.20
  python-dotenv>=1.0
  Pillow>=10.0
  ```

---

## Creating the bot (Developer Portal)

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   and sign in.
2. Click **New Application**, give it a name, and accept the terms.
3. Open the **Bot** tab in the left sidebar.
4. Click **Reset Token** (or **Add Bot** if it is a fresh application) and copy
   the token. **Never share or commit this token** — treat it like a password.
   If it ever leaks, reset it immediately.
5. Copy the **Application ID** from the **General Information** tab. You will
   need it for the invite link below.
6. Still on the **Bot** tab, scroll to **Privileged Gateway Intents** and enable
   the three intents listed in the [next section](#privileged-intents).

---

## Privileged intents

This bot needs the following **privileged** intents. They must be switched **ON**
in the Developer Portal (Bot → Privileged Gateway Intents), otherwise the bot
will fail to log in:

| Intent | Why it is needed |
| --- | --- |
| ✅ **Server Members Intent** | Join/leave messages, autorole, member caching, welcome/leveling |
| ✅ **Message Content Intent** | Auto-responders, custom commands, counting, XP from messages, auto-mod content checks |
| ✅ **Presence Intent** | Optional — used by `/botinfo` and presence rotation |

> If you leave the Presence intent off, set `intents.presences = False` in
> `config.py`; everything else keeps working.

---

## Inviting the bot

Build an invite URL, replacing `YOUR_CLIENT_ID` with your **Application ID**:

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=8&scope=bot%20applications.commands
```

- `scope=bot%20applications.commands` registers the slash commands.
- `permissions=8` is **Administrator**, which is the simplest way to make every
  feature work out of the box.

Prefer least privilege? Generate the invite with the checkboxes on the
**OAuth2 → URL Generator** page instead and grant only what you need. The bot's
features may request most of the following permissions:

```
View Channels, Send Messages, Send Messages in Threads, Embed Links,
Attach Files, Read Message History, Add Reactions, Use External Emojis,
Manage Messages, Manage Channels, Manage Roles, Manage Webhooks,
Kick Members, Ban Members, Moderate Members, Manage Nicknames,
Manage Guild (Server), Mention Everyone, Connect, Speak, Move Members,
Mute Members, Deafen Members, Priority Speaker
```

Requires the **`applications.commands`** scope in addition to **`bot`**.

---

## Installation

```bash
# 1. Get the code (clone or download and unzip this folder)
cd bot

# 2. Create and activate a virtual environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create your .env from the template
copy .env.example .env      # Windows
cp .env.example .env        # macOS / Linux

# 5. Edit .env and fill in DISCORD_TOKEN and OWNER_ID (see below)
```

---

## Configuration

Configuration lives in a single `.env` file at the project root — **the token is
never hard-coded**. Copy `.env.example` to `.env` and fill in the values:

| Variable | Required | Description |
| --- | --- | --- |
| `DISCORD_TOKEN` | ✅ | Your bot token from the Developer Portal. |
| `OWNER_ID` | ✅ | Your Discord user ID. Grants access to owner-only commands. |
| `DEV_GUILD_IDS` | ➖ | Optional comma-separated guild IDs. When set, slash commands sync **instantly** to those servers (great for testing). Leave empty to sync globally (may take up to an hour to appear). |

```env
DISCORD_TOKEN=your-bot-token-here
OWNER_ID=000000000000000000
DEV_GUILD_IDS=000000000000000000
```

### Per-server settings

Everything is configurable in Discord itself:

- Run **`/setup`** — a wizard that walks an administrator through each category
  (tickets, welcome, logging, security, leveling, economy and more) and saves
  the values you provide.
- Run **`/settings view`** to inspect every stored value and
  **`/settings reset`** to wipe all configuration for a server.

---

## Running the bot

```bash
python main.py
```

On a successful start you will see log lines such as:

```
[timestamp] [INFO    ] bot       Loaded extension cogs.core
[timestamp] [INFO    ] bot       Synced 42 commands to guild 123456789
[timestamp] [INFO    ] bot       Logged in as AllInOne#1234 (id ...) | 1 guilds
```

- Logs are written to **`logs/bot.log`** (rotating, 5 MB × 5 files) and to the
  console. All uncaught errors and tracebacks are recorded there.
- The SQLite database is created automatically at **`data/bot.db`**.

> **Windows note:** if `python` is not recognised, use the full path to your
> interpreter (e.g. `py -3.12 main.py` or the path shown by `where python`).

---

## Project structure

```
bot/
├── main.py               # entry point: bot class, cog loader, error handler
├── config.py             # env loading, theme colours, paths, intents
├── database.py           # aiosqlite wrapper + full schema + settings store
├── check.py              # development smoke test (compile + load all cogs)
├── requirements.txt
├── .env.example
├── README.md
├── cogs/                 # one module per feature/system
│   ├── core.py           # /ping /help /settings /setup …
│   ├── tickets.py
│   ├── welcome.py
│   ├── reactionroles.py
│   ├── giveaways.py
│   ├── security.py
│   ├── moderation.py
│   ├── logs.py
│   ├── leveling.py
│   ├── economy.py
│   ├── customcommands.py
│   ├── suggestions.py
│   ├── starboard.py
│   ├── tools.py
│   ├── birthday.py
│   ├── tempvoice.py
│   ├── fun.py
│   └── misc.py
└── utils/                # shared helpers
    ├── embeds.py         # themed embed builders
    ├── checks.py         # permission / owner / guild checks
    ├── helpers.py        # durations, placeholders, cooldowns, xp maths
    ├── paginator.py      # embed paginator view
    ├── views.py          # ConfirmView and other reusable views
    ├── transcript.py     # HTML + text ticket transcripts
    ├── imagecard.py      # Pillow welcome card renderer
    ├── rankcard.py       # Pillow leveling rank card renderer
    ├── moderation.py     # shared case / mod-log / escalation helpers
    └── errors.py         # global slash-command error handler
```

---

## Commands

> Slash commands are grouped by category. Commands that change server
> configuration require **Manage Server** (or **Administrator**), and moderation
> commands require the relevant moderation permission or a configured staff role.

<!-- COMMANDS:START -->
All commands are **slash commands**. Commands that change server configuration
require **Manage Server** (or **Administrator**); moderation commands require the
relevant moderation permission or a role configured with `/modrole`. Use `/help`
in Discord to browse the same list with a category menu.

### 🧭 General

- `/ping` — Check the bot's latency.
- `/help` — List all commands, grouped by category.
- `/botinfo` — Show information about the bot.
- `/serverinfo` — Show information about this server.
- `/userinfo` — Show information about a member.
- `/avatar` — Show a member's avatar.
- `/settings view` — Show every configured setting for this server.
- `/settings reset` — Delete every stored setting for this server.
- `/setup` — Walk an administrator through the basic bot setup.

### 🛡️ Moderation

- `/modrole add` — Allow a role to use moderation commands.
- `/modrole remove` — Remove a role from the moderation staff list.
- `/modrole list` — List the roles that may use moderation commands.
- `/modlog set` — Set the channel where moderation cases are logged.
- `/modlog clear` — Disable the moderation log channel.
- `/escalation view` — Show the current warning escalation rules.
- `/escalation set` — Add or replace a warning escalation rule.
- `/escalation reset` — Restore the default warning escalation rules.
- `/ban` — Ban a member from the server.
- `/unban` — Unban a user by id.
- `/kick` — Kick a member from the server.
- `/tempban` — Temporarily ban a member.
- `/softban` — Ban then immediately unban a member (softban).
- `/timeout` — Timeout (mute) a member for a duration.
- `/untimeout` — Remove a member's timeout.
- `/warn` — Warn a member and apply warning escalation.
- `/warnings` — List a member's warnings.
- `/clearwarnings` — Clear every warning for a member.
- `/purge` — Bulk delete recent messages in this channel.
- `/slowmode` — Set the slowmode delay of a channel.
- `/lock` — Lock a channel so @everyone cannot send messages.
- `/unlock` — Unlock a previously locked channel.
- `/nickname` — Change or clear a member's nickname.
- `/note` — Add a private note to a member's record.
- `/case` — Show a single moderation case.
- `/modlogs` — Show the moderation history of a member or the server.
- `/dm_punishments` — Enable or disable punishment DMs to members.

### 📜 Logging

- `/logs set` — Set the log channel for one event.
- `/logs all` — Send every event to a single channel.
- `/logs toggle` — Enable or disable one logging event.
- `/logs ignore` — Manage ignored channels and users for logging.
- `/logs config` — Show the current logging configuration.

### 🎫 Tickets

- `/ticket setup` — Configure the ticket system (panel, category, roles, logs).
- `/ticket panel` — Post the ticket panel (buttons or a select menu).
- `/ticket open` — Open a ticket manually.
- `/ticket close` — Close the current ticket (with a transcript).
- `/ticket delete` — Close a ticket and delete its channel immediately.
- `/ticket claim` — Claim the current ticket.
- `/ticket add` — Add a user to the current ticket.
- `/ticket remove` — Remove a user from the current ticket.
- `/ticket rename` — Rename the current ticket channel.
- `/ticket transfer` — Transfer the current ticket to another user.
- `/ticket list` — List all open tickets on this server.
- `/ticket settings` — Show the current ticket configuration.
- `/ticket inactivity` — Set how many minutes of inactivity before a ticket auto-closes.
- `/ticket limit` — Set the maximum number of open tickets per user.
- `/ticket rating` — Toggle the rating question after a ticket is closed.

### 👋 Welcome & Leave

- `/welcome channel` — Set the welcome channel and enable welcome messages.
- `/welcome message` — Set the welcome message text.
- `/welcome style` — Choose how welcome messages are displayed.
- `/welcome toggle` — Enable or disable welcome messages.
- `/welcome dm` — Also send the welcome message as a DM.
- `/welcome preview` — Preview the welcome message in this channel.
- `/leave channel` — Set the leave channel and enable leave messages.
- `/leave message` — Set the leave message text.
- `/leave toggle` — Enable or disable leave messages.
- `/leave preview` — Preview the leave message in this channel.
- `/autorole human` — Set the role given automatically to human members.
- `/autorole bot` — Set the role given automatically to bot members.
- `/autorole disable` — Disable both auto-roles.
- `/verification setup` — Configure the verification system.
- `/verification post` — Post the verification panel in a channel.
- `/verification disable` — Disable the verification system.

### 🎭 Reaction Roles

- `/reactionrole create` — Create a role panel.
- `/reactionrole add` — Add a role entry to an existing panel.
- `/reactionrole remove` — Remove a role entry from a panel.
- `/reactionrole delete` — Delete a role panel and its message.
- `/reactionrole list` — List all role panels on this server.

### 🎉 Giveaways

- `/giveaway start` — Start a new giveaway.
- `/giveaway end` — End a giveaway immediately.
- `/giveaway reroll` — Draw new winners for an ended giveaway.
- `/giveaway pause` — Pause a running giveaway.
- `/giveaway resume` — Resume a paused giveaway.
- `/giveaway delete` — Delete a giveaway from the database.
- `/giveaway list` — List giveaways on this server.
- `/giveaway blacklist` — Add or remove a user from the giveaway blacklist.
- `/giveaway bypass` — Add or remove a bypass role for giveaway requirements.

### 🔒 Security

- `/security whitelist add` — Whitelist a user or role (mention or ID).
- `/security whitelist remove` — Remove a user or role from the whitelist.
- `/security whitelist list` — List every whitelisted user and role.
- `/security toggle` — Enable or disable a protection (or all of them).
- `/security enable` — Master switch for the whole security system.
- `/security config` — Show the current security configuration.
- `/security threshold` — Set a numeric security threshold.
- `/security punishment` — Set the default or per-rule auto-moderation punishment.
- `/security badword` — Add or remove a word from the bad-words filter.
- `/security badwords` — List every word in the bad-words filter.
- `/security raid` — Configure anti-raid protection.
- `/security nuke` — Configure anti-nuke protection.
- `/security ignore` — Manage channels excluded from auto-moderation.
- `/security lockdown` — Lock or unlock every text channel manually.
- `/security clear` — Clear a user's automod action history.

### 📈 Leveling

- `/level role add` — Grant a role when a member reaches a level.
- `/level role remove` — Remove the role configured for a level.
- `/level role list` — List every configured level-up role.
- `/level ignore channel` — Ignore or un-ignore a channel for XP.
- `/level ignore role` — Ignore or un-ignore a role for XP.
- `/level enable` — Enable or disable message XP.
- `/level message` — Set the level-up announcement message.
- `/level channel` — Set (or clear) the level-up announcement channel.
- `/level multiplier` — Set the global XP multiplier.
- `/level xp` — Set the random XP range granted per message.
- `/level cooldown` — Set the cooldown between XP grants.
- `/level voice` — Configure voice channel XP.
- `/level stackroles` — Keep every lower level role instead of only the highest.
- `/level reset` — Reset a member's level and XP.
- `/level set` — Set a member's level.
- `/level give` — Give a member extra XP.
- `/rank` — Show your rank card with level and XP progress.
- `/leaderboard` — Show the XP leaderboard for this server.

### 💰 Economy

- `/economy leaderboard` — Show the richest members on the server.
- `/economy give` — Give a member money.
- `/economy remove` — Remove money from a member's wallet.
- `/economy reset` — Reset a member's entire economy profile.
- `/economy config` — Show the current economy configuration.
- `/economy enable` — Enable or disable the whole economy system.
- `/economy currency` — Set the currency name and symbol.
- `/economy daily` — Set the base daily reward amount.
- `/economy work` — Set the work reward range.
- `/economy startbalance` — Set the balance given to new members.
- `/economy gamblemax` — Set the maximum gamble bet.
- `/economy streakbonus` — Toggle the daily streak bonus.
- `/shop list` — Browse the items available in the shop.
- `/shop add` — Add an item to the shop (or update an existing one).
- `/shop remove` — Remove an item from the shop.
- `/shop buy` — Buy an item from the shop.
- `/gamble coinflip` — Bet on a coin flip — win double your bet.
- `/gamble dice` — Roll a die — guess the result for 6x your bet.
- `/gamble slots` — Spin the slot machine for symbol-based payouts.
- `/balance` — Show your cash, bank balance and net worth.
- `/daily` — Claim your daily reward and build a streak.
- `/work` — Work a shift to earn some money.
- `/beg` — Beg for a small amount of spare change.
- `/deposit` — Move money from your wallet into your bank.
- `/withdraw` — Move money from your bank into your wallet.
- `/pay` — Send money from your wallet to another member.
- `/inventory` — Show the items a member owns.

### 🤖 Custom Commands

- `/customcommand add` — Create a new custom command.
- `/customcommand edit` — Change the reply of a custom command.
- `/customcommand remove` — Delete a custom command.
- `/customcommand list` — List every custom command of this server.
- `/customcommand prefix` — Change the prefix used for custom commands.
- `/customcommand toggle` — Enable or disable prefix custom commands.
- `/autoresponder add` — Create an automatic reply.
- `/autoresponder remove` — Delete an auto-responder.
- `/autoresponder list` — List every auto-responder of this server.
- `/autoresponder toggle` — Enable or disable auto-responders.
- `/tag create` — Create a new tag.
- `/tag get` — Show the content of a tag.
- `/tag edit` — Change the content of a tag.
- `/tag delete` — Delete a tag.
- `/tag info` — Show details about a tag.
- `/tag list` — List the tags of this server, or of one member.
- `/tag transfer` — Transfer ownership of one of your tags.
- `/tag random` — Show a random tag of this server.
- `/tag search` — Search tags by name.

### 💡 Suggestions

- `/suggestion approve` — Mark a suggestion as approved.
- `/suggestion deny` — Mark a suggestion as denied.
- `/suggestion implement` — Mark a suggestion as implemented.
- `/suggestion edit` — Change the text of a suggestion.
- `/suggestion delete` — Delete a suggestion and its message.
- `/suggestion list` — List the suggestions of this server.
- `/suggestion config` — Show the current suggestion configuration.
- `/suggestion setup` — Set the suggestion and staff channels.
- `/suggest` — Submit a suggestion to this server.

### ⭐ Starboard

- `/starboard channel` — Set the starboard channel and enable it.
- `/starboard emoji` — Set the emoji used to star messages.
- `/starboard threshold` — Set how many reactions are needed to star a message.
- `/starboard toggle` — Enable or disable the starboard.
- `/starboard ignore` — Add, remove or list channels ignored by the starboard.
- `/starboard selfstar` — Allow members to star their own messages.
- `/starboard show` — Force a message onto the starboard.
- `/starboard remove` — Remove a message from the starboard.
- `/starboard config` — Show the current starboard configuration.

### 🛠️ Tools

- `/remind set` — Set a reminder that will be sent to you later.
- `/remind list` — List your pending reminders.
- `/remind cancel` — Cancel one of your reminders.
- `/remind clear` — Cancel every one of your reminders.
- `/poll create` — Create a poll with 2-10 options.
- `/poll end` — Close a poll early and show its final results.
- `/poll list` — List the polls that are still running.
- `/afk set` — Set an AFK status with an optional reason.
- `/afk clear` — Remove your AFK status.
- `/afk list` — List everyone who is currently AFK.

### 🎂 Birthdays

- `/birthday set` — Register your birthday so the server can celebrate it.
- `/birthday remove` — Remove your registered birthday.
- `/birthday next` — Show the next upcoming birthdays on this server.
- `/birthday list` — List every birthday registered on this server.
- `/birthday preview` — Preview the configured birthday message.
- `/birthday channel` — Set the channel where birthday messages are posted.
- `/birthday message` — Set the birthday message template.
- `/birthday role` — Set an optional role given on members' birthdays.
- `/birthday toggle` — Enable or disable birthday announcements.

### 🔊 Voice

- `/tempvoice setup` — Configure the join-to-create voice system.
- `/tempvoice config` — Show the temporary voice configuration.
- `/tempvoice lock` — Lock your channel so nobody else can join.
- `/tempvoice unlock` — Unlock your channel so others can join again.
- `/tempvoice limit` — Change the user limit of your channel.
- `/tempvoice rename` — Rename your channel.
- `/tempvoice permit` — Allow a member to join your channel.
- `/tempvoice reject` — Deny a member access to your channel.
- `/tempvoice kick` — Disconnect a member from your channel.
- `/tempvoice transfer` — Give ownership of your channel to another member.
- `/tempvoice claim` — Claim a temporary channel whose owner has left.

### 🎲 Fun & Utility

- `/8ball` — Ask the magic 8-ball a question.
- `/coinflip` — Flip a coin.
- `/dice` — Roll one or more dice.
- `/choose` — Let the bot pick one of your options.
- `/rps` — Play rock, paper, scissors against the bot.
- `/meme` — Post a random meme from Reddit.
- `/trivia` — Answer a multiple choice trivia question.
- `/embed` — Build and post a custom embed in a channel.
- `/announce` — Post an announcement in a channel.
- `/say` — Send text through the bot in a channel.
- `/roleinfo` — Show information about a role.
- `/channelinfo` — Show information about a channel.
- `/icon` — Show the server icon.
- `/banner` — Show the server banner.

### 🧩 Games & Extras

- `/counting setup` — Set the counting channel and enable the game.
- `/counting toggle` — Enable or disable the counting game.
- `/counting stats` — Show the counting statistics for this server.
- `/counting reset` — Reset the count back to zero.
- `/sticky set` — Add or replace a sticky message in a channel.
- `/sticky remove` — Remove the sticky message from a channel.
- `/sticky edit` — Change the text of an existing sticky message.
- `/sticky list` — List every sticky message on this server.
- `/autopublish add` — Add an announcement channel to the auto-publish list.
- `/autopublish remove` — Remove a channel from the auto-publish list.
- `/autopublish list` — List the channels that auto-publish.
<!-- COMMANDS:END -->

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `DISCORD_TOKEN is not configured` | You did not create `.env`, or the token is still the placeholder. Copy `.env.example` → `.env` and set the values. |
| Bot logs in but slash commands do not appear | Set `DEV_GUILD_IDS` for instant sync, or wait up to an hour for global sync. Also ensure the invite used the `applications.commands` scope. |
| `Privileged intent provided is not enabled` | Enable **Server Members**, **Message Content** (and **Presence**) in the Developer Portal → Bot → Privileged Gateway Intents. |
| Commands respond with "You need the …" | The command is permission-gated; use an account with the listed permission or the configured staff role. |
| Ticket/welcome messages are not sent | Check the bot has **Manage Channels**, **Manage Roles** and **Send Messages** in the relevant channel/category. |
| `database is locked` | Only one bot instance may use `data/bot.db` at a time. Stop the other instance. |
| Anything else | Check `logs/bot.log` — full tracebacks are written there. |

---

Built with [discord.py](https://github.com/Rapptz/discord.py). Provided as-is;
review the permissions you grant before running in a production server.
