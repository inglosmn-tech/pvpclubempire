# PVPClub Empire

Existing `discord.py` bot updated with MCTiers lookups, persistent meow statistics, event and Tierwatch tools, community utilities, AutoMod, moderation and a XP and voice leveling system.

## Render deployment and automatic GitHub deploys

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `python main.py`
- **Runtime:** Python 3.10 or newer
- **Health check:** `/health`

The bot starts its Discord connection, loads every cog in `extensions/`, and starts the PvPClubTiers website from `main.py`. The existing GitHub Actions workflow validates Python syntax and the embedded website JavaScript on pushes to `main`.

To deploy every GitHub update automatically, connect the Render Web Service to this repository (`inglosmn-tech/pvpclubempire`) and the `main` branch, then set **Auto-Deploy** to **On Commit** in Render. This repository cannot link a Render account or enable that dashboard setting by itself; it is a one-time Render setup. Keep the service on an always-on plan suitable for a Discord Gateway bot, because a service that sleeps will disconnect the bot.

Keep the `extensions/` folder alongside `main.py`; the bot loads those cogs at startup.

### Required environment variables

| Variable | Required | Purpose |
|---|---:|---|
| `BOT_TOKEN` | Yes | Discord bot token |
| `DATABASE_URL` | Yes on Render | Neon/PostgreSQL connection string |
| `PORT` | Render-provided | PvPClubTiers website port; do not set this yourself |
| `MONGO_URI` | Required for PvPClubTiers | MongoDB Atlas connection string (starts with `mongodb+srv://` or `mongodb://`) |
| `DISCORD_GUILD_ID` | Optional | Discord server ID used to select the public PvPClubTiers website data |

### Optional environment variables

| Variable | Purpose |
|---|---|
| `DATABASE_STORAGE_LIMIT_MB` | Set this to the real database quota shown in your Neon plan, in MiB. Enables 80% and 95% usage warnings and percentage display in `?storage`. Do not guess this value. |
| `PVPCLUBTIERS_WEBSITE_URL` | Public tier website URL for the Discord panels; defaults to Render's `RENDER_EXTERNAL_URL`. |
| `OPENAI_API_KEY` | Enables optional asynchronous OpenAI Moderation checks in AutoMod, including configured NSFW text and image filtering. Without it, fast local checks still run. |
| `ALLOW_EPHEMERAL_STORAGE=1` | Allows JSON-only storage for temporary local development. Do not use this for a production Render deployment. |
| `DATA_FILE` | Local JSON snapshot path; defaults to `bot_data.json`. |

Never commit `.env`, `BOT_TOKEN`, or a real database connection string to GitHub.

## Important storage note

No finite Neon plan can guarantee storage will never fill. This version reduces avoidable growth and reports usage:

- Meow totals use one PostgreSQL row per Discord user, updated in place, rather than one stored record per message.
- Meow streaks, birthdays and levels use one row per guild/user and update those rows in place.
- The hot counters are not copied into the main JSONB config row or kept in an ever-growing in-memory cache when PostgreSQL is active.
- The local JSON snapshot excludes normalized counters/XP when PostgreSQL is active; PostgreSQL is the source of truth for those records.
- AutoMod stores bounded 24-hour strike and spam-incident history per member. Spam incidents remove recent spam messages first, issue three escalating warnings after the first incident, then apply increasing timeouts.
- Ended giveaways older than 90 days and closed events older than 180 days are removed from the config state. Manual warning history is retained up to 50 entries per member.
- Tierwatch is limited to 50 watched Minecraft profiles per server; custom AutoMod patterns are limited to 30 per server.
- `/storage` (prefix: `?storage`) displays the PostgreSQL database size. Set `DATABASE_STORAGE_LIMIT_MB` to your actual Neon quota to enable automated warnings at 80% and 95%.

Lifetime meow counts, level totals, streak bests and birthdays are intentionally retained. They are compact aggregate rows; pruning them would destroy leaderboard/history data. `pg_database_size` reports the current PostgreSQL database size, while Neon Console remains the authoritative view of project/plan storage usage.

## Discord Developer Portal and bot permissions

Enable these privileged gateway intents:

- **Server Members Intent** (welcome messages, autoroles, birthdays, bot approval and raid verification)
- **Message Content Intent** (prefix commands, meow counting and message AutoMod)
- **Auto Moderation Configuration** and **Auto Moderation Execution** intents (native Discord AutoMod synchronization and event delivery)

Give the bot the permissions needed for the features you enable: Manage Roles, Manage Server, Manage Channels, Manage Messages, Manage Webhooks, Moderate Members, Kick Members, View Audit Log (for bot inviter lookup), View Channels, Send Messages, Embed Links and Read Message History. Anti-macro checks and spam escalation need Manage Messages and Moderate Members. Bot approval and raid quarantine need Manage Roles; bot approval rejection and optional unverified-member kicks need Kick Members. Native AutoMod synchronization requires Manage Server. Keep the bot's highest role above every role it must assign or moderate.

## First-time setup

1. Set `BOT_TOKEN`, `DATABASE_URL` and `MONGO_URI` in Render → Environment. Neon/PostgreSQL remains for existing PVPClub features; MongoDB Atlas stores PvPClubTiers data.
2. Set `DATABASE_STORAGE_LIMIT_MB` to the real limit shown by your Neon plan if you want percentage warnings.
3. Enable the Discord gateway intents above. Message Content Intent is required for `?` prefix commands and trivia answer checking.
4. Deploy, then run the setup commands below in your server.

## Setup commands

- `?meowrolessetup` — creates Meow milestone roles and `meowking` for the current #1 lifetime meower.
- `?suggestsetup #suggestions` — configure the suggestions channel (Manage Messages or Manage Server); members submit via `/suggest`, moderators review.
- `?setwelcome #welcome` — configure normal welcome messages.
- `?autorole @role` — assign a role to new members.
- `?birthdaysetup #birthdays` — configure birthday announcements; members set their date with `/birthday`.
- `?logssetup #server-logs` — configure general server logs.
- `?setmodlog #mod-logs` — configure moderation/AutoMod logs.
- `?ticketsetup` — create the ticket panel.
- `?tierwatchsetup #tier-announcements` — configure official MCTiers watch alerts.
- `?eventsetup #events` — configure events and the Event Ping opt-in panel.
- `?tiersetup` — create/check the 10 shared LT5–HT1 tier roles and 11 gamemode tester roles, `#queues` cards, the one-IGN `#request-test` panel, private `TEST SESSIONS`, `#tier-info`, `#tier-leaderboard`, results/activity/log channels and matchmaking. Requires `MONGO_URI`.
- `?v3` — refresh the V3 Sign Up and gamemode queue panels in place. It preserves existing queue entries and each queue's open/closed status. Requires `MONGO_URI`.
- `?settier <gamemode> <ign>` — server-owner-only manual tier assignment. Looks up an existing PvPClubTiers player by Minecraft username and opens a tier selector; records the change in history and updates their overall tier role.
- `?transfer-tiers @old-account @new-account` — server-owner-only transfer of a player's gamemode tiers and tier history to their replacement Discord account. The old account's active tiers are cleared. The destination must not already have tiers; ELO and duel records stay on the old account.
- `?resetuptest` — retire old public test tickets and thread-only queue/request forum channels, cancel waiting entries, close all gamemode queues and rebuild the text-channel panels. It preserves results, activity, logs, general support tickets and unrelated channels.
- `#queues` has one status card per gamemode, with Join/Leave buttons, a 20-player waitlist and first-to format. Players can join only while the matching gamemode queue is open. Only members with that gamemode's Tester role can open or close it; administrators do not bypass this role check. Existing waitlist entries stay in place when a queue closes and are notified when it reopens. The request panel is a global signup: players enter their IGN once, are added automatically to every open gamemode queue, and are added to future queues as testers open them. **Leave all waitlists** cancels the global signup and all waiting entries.
- `?testerappsetup #review-channel` — configure the tester application review channel and refresh the Apply for Tier Tester panel in `#tier-info`. Applicants choose a gamemode and submit their Minecraft IGN, current tier/rank, testing experience and reason; the form is sent to the configured review channel. Only the server owner can accept or reject. Accepted applicants receive only that gamemode's Tester role. Run `?tiersetup` first. Requires `MONGO_URI`.
- `?triviasetup` — create/configure `#trivia` and start a new question every 20 minutes. Correct answers earn one point.
- `?triviastop` — pause scheduled trivia.
- `?spamchannel` — mark the current channel as a spam zone where normal bot AutoMod is bypassed but the anti-macro human check stays active. Use `?spamchannel off` to disable it or `?spamchannel status` to check.
- `?storage` — inspect database storage usage.
- `?levelsetup` or `/level-setup` — set up XP/voice leveling, a `#level-ups` channel and neutral milestone roles. Existing bot-created cat-themed level roles and channels are renamed when possible.
- `?v1` — rename categories and channels into the decorative `emoji | CATEGORY` and `『emoji』 channel-name` styles.
- `?catinvasion` — enable random cat events in the current channel; first exact `MEOW` within 60 seconds wins 25 XP. Events run every 3–6 hours between 10:00 and 22:00 (Amsterdam time).
- `?catinvasionstop` — disable Cat Invasion.

## Security and AutoMod setup

- `?botapprovalsetup #bot-approval` — server-owner-only setup. Newly joining bots are restricted to the review channel until the actual server owner approves or rejects them. Existing bots are grandfathered when setup is first run.
- `/raid setup` — server-owner-only setup for the verification channel, rolling join threshold (default 10 humans / 10 seconds), protection window and optional kick delay after a failed/expired challenge. Use `/raid enable`, `/raid disable` and `/raid status` to control or inspect it. Quarantined humans can only view the verification panel channel until they solve their personal arithmetic challenge.
- `/automod nsfw setup` — server-owner-only setup for the moderation log and sexual/minors text confidence thresholds; requires `OPENAI_API_KEY`. Use `/automod nsfw on`, `/automod nsfw off` and `/automod nsfw status` to manage or inspect explicit text and supported image filtering.
- `/automod sync` and `/automod native-status` — server-owner-only synchronization and inspection of PVPClub Empire-owned native Discord AutoMod rules. Manually created rules are left untouched.
- `/temporaryrole grant` — give a member a role for `10m`, `2h`, `1d`, `7d` or up to `30d`; re-granting extends the existing temporary expiry. Use `/temporaryrole remove` and `/temporaryrole list` to manage active assignments. Requires Manage Roles, PostgreSQL and a bot role above both the target member and the assigned role.


**Important NSFW limitation:** OpenAI's standard moderation endpoint's `sexual/minors` category applies to text only. The image path checks the supported sexual-image category; it does not classify suspected CSAM images. Do not upload suspected CSAM to the standard moderation endpoint. If OpenAI moderation is unavailable or rate-limited, AI filtering fails open and writes an operational warning; local AutoMod rules continue to run.

## Useful commands

- `/tier` — official MCTiers player lookup. The old `/mctiers` command is not registered.
- `/ign`, `/tiers`, `/profile`, `/tier-leaderboard`, `/tier-rank`, `/search`, `/compare`, `/history`, `/recent` — PvPClubTiers player commands. `/tier-leaderboard` shows the overall table and each gamemode's top three.
- `/website` — post the PvPClub Empire tier-list website URL in the current channel.
- `/set-tier` — record the result in a private test session; the server owner can also set any member's tier from any channel. `/close` closes the session. Only the queued player, assigned tester and bot can view a private test channel; the server owner/staff can manage PvPClubTiers sessions.
- `/testerstats`, `/testerboard`, `/tierban`, `/tierunban`, `/tier-adjust`, `/tier-recalculate`, `/tier-stats`, `/tier-cooldowns` — PvPClubTiers staff and tier administration.
- `/duel`, `/duel-score`, `/duel-cancel`, `/elo`, `/elo-leaderboard` — competitive duels with ELO ratings and win/loss tracking.
- `/trivia-leaderboard` — trivia points leaderboard.
- `/tierwatch`, `/tierunwatch`, `/tierwatchlist` — manage official profile change tracking.
- `/meowstreak`, `/meowstats`, `/meowboard` — meow statistics and leaderboard.
- `/suggest`, `/birthday`, `/birthdayremove` — community utilities.
- `/rank`, `/leaderboard`, `/level-admin`, `/level-reset` — server leveling and XP.
- `/automod toggle`, `/automod module`, `/automod addterm`, `/automod removeterm`, `/automod immune-role`, `/automod immune-channel`, `/automod invite-code`, `/automod setlog` — AutoMod configuration.

## Prefix and slash command changes

- `?setmodlog` and `?ticketsetup` are prefix commands.
- `/meow-setup` and `/meow-reset` have been replaced by `/level-setup` and `/level-reset`; `?levelsetup` is also available as a prefix command.
- `/tier` is the MCTiers player lookup command; `/mctiers` is not registered.
- `/cat` and `/roast` are not registered.
- `/fban` is a 30-second timed presentation only. It does not ban, kick or timeout the member; the message explicitly states that no Discord moderation action was applied. `/ban` remains the actual ban command.

## Data migration

On first startup with `DATABASE_URL`, the bot imports old JSON-backed meow totals, streaks, XP and birthdays into normalized PostgreSQL tables with idempotent upserts. Existing tables from the previous persistence build are reused. After migration, PostgreSQL is the source of truth for these records.

## Testing note

Python syntax and static logic checks are included in the delivery verification. A live Discord login, slash-command registration, actual moderation actions and a real Neon connection must be tested after deploying with your own Discord and Neon credentials.
