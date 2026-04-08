# CongoBot

A Discord bot for the Congo RP community that manages member onboarding, embassy access, and enemy country intelligence — integrated with the [WarEra.io](https://warera.io) game.

---

## What It Does

### Member Onboarding

When a new member joins the server, CongoBot opens a private onboarding channel and guides them through one of three paths:

| Path | Who it's for | What happens |
|---|---|---|
| **Visitor** | Anyone exploring | Assigns the Visitor role after confirming their WarEra identity |
| **Citizen** | Congo citizens in WarEra | Assigns the Citizen role after verifying citizenship via a company rename token |
| **Embassy** | Foreign government officials | Sets up an embassy channel for their country and assigns appropriate access |

Identity is verified by asking the user to **rename one of their WarEra companies** to a randomly generated token. The bot polls WarEra's API every minute to check for the rename, then automatically completes the flow once confirmed.

### Embassy System

Each country gets a dedicated embassy channel with two permission tiers:

- **Read access** (`Embassy {Country} {Flag}` role) — all members of that country's embassy
- **Write access** (`Embassy {Country} {Flag} - Officials` role) — government officials (President, Vice President, Minister of Foreign Affairs)

Embassy roles are **colored to match the country's flag** and are **hoisted** so members appear in their own section of the member list.

Officials with write access can grant write permissions to other embassy members using `/addwrite`. Write grants are tracked and automatically revoked if the granting official loses their government role.

### Automated Role Auditing

Every day at **07:00 UTC**, the bot cross-references all tracked members against WarEra's live data:

- Citizens who lost Congo citizenship are downgraded to Visitor
- Visitors who gained a government role are notified via DM
- Embassy officials who lost their government role are downgraded to Visitor
- Embassy officials who changed country are automatically moved to the new country's embassy
- Write grants made by demoted officials are cascade-revoked (grantees are notified via DM)

### Activity Tracker

The bot can track enemy countries' player activity on a **15-minute polling cycle**. For each tracked country it records how many players are online and in which level bracket (low/mid/high/master). Data is shown as a visual heatmap + hourly bar chart.

Use `/track` to start tracking a country and `/track-stats` to view the heatmap for any stored history window.

### Eco/War Shift Monitor

During every 15-minute activity poll the bot also classifies each active player's build as **eco**, **war**, or **hybrid** based on their WarEra skill investment. If the war-player ratio shifts by more than the configured threshold between two consecutive polls, an alert is posted in the configured channel and the Senate role is mentioned.

Eco skills: `entrepreneurship`, `energy`, `production`, `companies`, `management`. All other invested skills are counted as war skills.

### Inactivity Management

- After **7 days** of no activity in an onboarding channel, the user is warned in the channel
- After **14 days**, the user is kicked from the server and the channel is deleted

### Automatic Backups

The database is backed up automatically every hour to `data/congobot.db.bak`. You can also trigger a manual backup at any time with `/backup-db`.

---

## Commands

### User Commands

| Command | Description |
|---|---|
| `/reset-request` | Deletes your current onboarding channel and immediately starts a new one |
| `/retry-application` | Re-pings your country's officials if your embassy request is still pending approval |
| `/request-write` | Requests write access in your country's embassy channel |

---

### Embassy Commands

| Command | Who can use | Description |
|---|---|---|
| `/addwrite` | President, VP, or MoFA | Grants write access in the current embassy channel to a registered embassy member |
| `/senate-addwrite` | Senate | Grants write access in a member's embassy channel as a Senate guarantor |

---

### Activity Tracker Commands

All tracker commands require the **Senate role**.

| Command | Parameters | Description |
|---|---|---|
| `/track` | `country_id` | Start tracking a country's player activity every 15 minutes |
| `/track-stop` | `country_id` | Stop tracking a country |
| `/track-purge` | `country_id` | Delete all stored snapshots for a country (keeps tracking active) |
| `/track-now` | `country_id` | Take an immediate snapshot for a tracked country |
| `/track-stats` | `country_id`, `days` (optional, default 30) | Show heatmap + best attack windows for a tracked country |
| `/track-recalibrate` | `country_id` | Backfill active-user count on old snapshots using the current active player count |
| `/track-debug` | `country_id` | Show raw API fields for one user from a country (for diagnostics) |

---

### Admin / Senate Commands

Commands marked **[Admin]** require Discord Administrator permission. Commands marked **[Senate]** require the configured Senate role.

| Command | Who | Parameters | Description |
|---|---|---|---|
| `/setup` | Admin | — | Interactive wizard to configure the bot (categories, roles, eco/war alerts) |
| `/config` | Admin | — | Displays current configuration with `.env`-ready values to copy |
| `/test-onboarding` | Senate | `user` | Simulates a member join for a user (opens their onboarding channel) |
| `/test-visitor` | Senate | `user` | Instantly completes the visitor flow for a user |
| `/test-citizen` | Senate | `user` | Instantly completes the citizen flow (skips WarEra country check) |
| `/test-embassy` | Senate | `user` | Instantly completes the embassy flow |
| `/admin-restore` | Admin | `member`, `role_type`, `warera_id` | Manually adds a member to the database without re-running onboarding |
| `/admin-restore-write` | Admin | `grantor`, `grantee` | Restores a write grant between two embassy members after a DB reset |
| `/admin-restore-senate-write` | Admin | `senator`, `grantee` | Restores a Senate write grant after a DB reset |
| `/admin-restore-localroles` | Admin | — | Re-links all citizens to their correct Congolese government Discord roles |
| `/admin-db-status` | Admin | — | Lists all tracked members and cross-checks their Discord roles against the database |
| `/admin-diagnose-member` | Admin | `member` | Shows raw WarEra data and local role sync result for a specific member |
| `/admin-eco-status` | Admin/Senate | `country_id` | Shows live eco/war/hybrid build breakdown for a tracked country + last stored snapshot |
| `/admin-run-audit` | Admin | — | Manually triggers the daily role audit (embassy sync, write grant validation) |
| `/admin-reverify-embassies` | Admin | `category` (optional) | Renames embassy channels to the current schema and starts re-verification for all non-senate members |
| `/admin-reverify-government` | Admin | `channel` | Requires all members with access to a channel to re-verify a government/congress role |
| `/backup-db` | Admin | — | Forces an immediate database backup |

---

## Setup

### Prerequisites

- A Discord bot application with a token ([Discord Developer Portal](https://discord.com/developers/applications))
- Docker and Docker Compose installed on your server
- The bot invited to your server with the following permissions:
  - Manage Channels
  - Manage Roles
  - Kick Members
  - Send Messages
  - Read Message History

> **Privileged Intents:** Enable **Server Members Intent** and **Message Content Intent** in the Discord Developer Portal under your bot's settings.

---

### 1. Clone the Repository

```bash
git clone <repo-url>
cd CongoBot
```

### 2. Create the `.env` File

Copy the example below and fill in your values:

```env
DISCORD_TOKEN=your_bot_token_here
DISCORD_GUILD_ID=your_guild_id_here
```

You can optionally override the database path (defaults to `data/congobot.db`):

```env
DB_PATH=data/congobot.db
```

You can also pre-seed the bot configuration via environment variables. These are used as fallback values if the database is ever lost — they never overwrite values already in the database:

```env
# Core setup
SETUP_ONBOARDING_CATEGORY_ID=
SETUP_EMBASSY_CATEGORY_ID=
SETUP_SENATE_ROLE_ID=
SETUP_VISITOR_ROLE_ID=
SETUP_CITIZEN_ROLE_ID=

# Congo government roles (local Discord roles for citizens)
SETUP_LOCAL_ROLE_PRESIDENT_ID=
SETUP_LOCAL_ROLE_VICE_PRESIDENT_ID=
SETUP_LOCAL_ROLE_MFA_ID=
SETUP_LOCAL_ROLE_ECONOMY_ID=
SETUP_LOCAL_ROLE_DEFENSE_ID=
SETUP_LOCAL_ROLE_CONGRESS_ID=

# Optional: exempt from /admin-reverify-government
SETUP_ELDERS_ROLE_ID=

# Optional: eco/war shift monitor
SETUP_ECO_WAR_ALERT_CHANNEL_ID=
SETUP_ECO_WAR_THRESHOLD=20

# Optional: WarEra API key (raises rate limit from 100 → 200 req/min)
WARERA_API_KEY=
```

> Run `/config` after setup to get the exact values to place here.

---

### 3. Start the Bot

```bash
docker compose up -d --build
```

The bot will start and connect to Discord. The SQLite database is stored at **`./data/congobot.db`** — a plain file you can copy and back up. It persists across restarts and rebuilds as long as the file is not deleted.

```bash
# View logs
docker compose logs -f

# Stop the bot
docker compose down
```

---

### 4. Run `/setup` in Discord

Once the bot is online, run `/setup` in your server (requires Administrator permission). The wizard walks you through these steps:

1. **Onboarding category** — where private onboarding channels are created
2. **Embassy category** — where embassy channels are created
3. **Senate role** — members with this role can run admin/senate commands
4. **Visitor role** — assigned to verified visitors
5. **Citizen role** — assigned to verified Congo citizens
6–11. **Congo government roles** — local Discord roles for President, VP, MoFA, Economy, Defense, Congress
12. **Elders/Retirement role** *(optional)* — exempt from `/admin-reverify-government`
13. **WarEra API key** *(optional)* — raises the rate limit from 100 to 200 req/min
14. **Eco/war alert channel** *(optional)* — text channel where eco/war shift alerts are posted
15. **Eco/war alert threshold** *(optional)* — % of active players that must shift to trigger an alert (default: 20%)

After completing `/setup`, the bot is fully operational.

> **Important:** Run `/config` immediately after `/setup` and copy the output into your `.env` file. If `data/congobot.db` is ever lost, the bot restores its configuration automatically on the next restart without needing `/setup` again.

---

### 5. Start Tracking Countries *(optional)*

Use `/track <country_id>` to begin monitoring an enemy country's activity. The country ID is the 24-character hex ID from the WarEra URL for that country. Once tracking is active:

- The bot takes a snapshot every **15 minutes**
- Each snapshot records online player count by level bracket and classifies active players as eco/war/hybrid
- `/track-stats` generates a heatmap showing peak activity times and best attack windows
- Eco/war shift alerts fire automatically to the configured channel if the threshold is crossed

---

### Updating

```bash
git pull
docker compose up -d --build
```

The database is unaffected by rebuilds.

---

### Recovering from a Lost Database

If `data/congobot.db` is lost or corrupted, the bot recreates an empty database on the next start. Bot configuration is restored automatically from the `.env` fallback values. Member data must be re-entered manually:

1. **`/admin-restore`** — re-adds each member (visitor, citizen, or embassy official) with their WarEra ID
2. **`/admin-restore-write`** — re-creates write grants inside embassy channels
3. **`/admin-restore-senate-write`** — re-creates Senate-issued write grants
4. **`/admin-restore-localroles`** — re-links all citizens to their Congolese government roles
5. **`/admin-db-status`** — confirms all entries are correct and that every member's Discord role is in sync

---

### Backing Up the Database

The bot automatically backs up the database to `./data/congobot.db.bak` every hour. You can also trigger a manual backup with `/backup-db` at any time.

```bash
cp data/congobot.db data/congobot.db.bak
```

---

### Removing the Bot

```bash
# Stop and delete all data
docker compose down
rm data/congobot.db data/congobot.db.bak

# Stop without deleting data
docker compose down
```

---

### Author

Liquidos
