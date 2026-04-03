import aiosqlite
import os
import shutil
from datetime import datetime
from typing import Optional, Dict, List

DB_PATH = os.getenv('DB_PATH', 'data/congobot.db')
DB_BACKUP_PATH = DB_PATH + '.bak'

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id TEXT PRIMARY KEY,
    onboarding_category_id TEXT,
    embassy_category_id TEXT,
    senate_role_id TEXT,
    visitor_role_id TEXT,
    citizen_role_id TEXT,
    local_role_president_id TEXT,
    local_role_vice_president_id TEXT,
    local_role_mfa_id TEXT,
    local_role_economy_id TEXT,
    local_role_defense_id TEXT,
    local_role_congress_id TEXT,
    warera_api_key TEXT,
    elders_role_id TEXT
);

CREATE TABLE IF NOT EXISTS user_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    channel_id TEXT,
    warera_id TEXT,
    warera_username TEXT,
    country_id TEXT,
    country_name TEXT,
    requested_role TEXT,
    status TEXT DEFAULT 'pending',
    verification_token TEXT,
    inactivity_warned INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    last_activity_at TEXT DEFAULT (datetime('now')),
    completed_at TEXT,
    UNIQUE(discord_id, guild_id)
);

CREATE TABLE IF NOT EXISTS embassy_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    country_id TEXT NOT NULL,
    country_name TEXT,
    country_flag TEXT,
    warera_role TEXT,
    access_level TEXT,
    embassy_channel_id TEXT,
    embassy_role_id TEXT,
    embassy_write_role_id TEXT,
    approval_status TEXT DEFAULT 'pending',
    approval_message_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS write_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grantor_discord_id TEXT NOT NULL,
    grantor_warera_id TEXT NOT NULL,
    grantee_discord_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    country_id TEXT NOT NULL,
    write_role_id TEXT NOT NULL,
    grant_type TEXT NOT NULL DEFAULT 'official',
    granted_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tracked_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    warera_id TEXT NOT NULL,
    assigned_role TEXT NOT NULL,
    country_id TEXT,
    discord_role_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(discord_id, guild_id)
);

CREATE TABLE IF NOT EXISTS scheduled_deletions (
    channel_id TEXT PRIMARY KEY,
    delete_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_approvals (
    message_id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    requester_discord_id TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reverification_pending (
    discord_id     TEXT NOT NULL,
    guild_id       TEXT NOT NULL,
    roles_to_remove TEXT NOT NULL,
    reverify_type  TEXT NOT NULL,
    warn_count     INTEGER DEFAULT 0,
    last_warned_at TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (discord_id, guild_id)
);

CREATE TABLE IF NOT EXISTS tracked_countries (
    country_id   TEXT PRIMARY KEY,
    country_name TEXT,
    country_flag TEXT,
    channel_id   TEXT NOT NULL,
    guild_id     TEXT NOT NULL,
    started_by   TEXT NOT NULL,
    started_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS activity_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    country_id    TEXT NOT NULL,
    snapshot_time TEXT NOT NULL,
    total_users   INTEGER NOT NULL,
    online_count  INTEGER NOT NULL,
    online_low    INTEGER NOT NULL DEFAULT 0,
    online_mid    INTEGER NOT NULL DEFAULT 0,
    online_high   INTEGER NOT NULL DEFAULT 0,
    online_master INTEGER NOT NULL DEFAULT 0,
    active_users  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_snapshots_country_time
    ON activity_snapshots (country_id, snapshot_time);
"""


class Database:
    def __init__(self):
        self.db_path = DB_PATH

    async def init(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        # Back up the current DB before anything else, so .bak always
        # reflects the last successful run (useful after a bad restart).
        if os.path.exists(self.db_path):
            shutil.copy2(self.db_path, DB_BACKUP_PATH)
        # Auto-restore from backup if the main database is missing
        elif os.path.exists(DB_BACKUP_PATH):
            shutil.copy2(DB_BACKUP_PATH, self.db_path)
        async with aiosqlite.connect(self.db_path) as db:
            for statement in SCHEMA.strip().split(';'):
                stmt = statement.strip()
                if stmt:
                    await db.execute(stmt)
            # Migrations for existing databases
            try:
                await db.execute('ALTER TABLE embassy_requests ADD COLUMN embassy_write_role_id TEXT')
            except Exception:
                pass  # Column already exists
            for col in (
                'local_role_president_id',
                'local_role_vice_president_id',
                'local_role_mfa_id',
                'local_role_economy_id',
                'local_role_defense_id',
                'local_role_congress_id',
                'warera_api_key',
            ):
                try:
                    await db.execute(f'ALTER TABLE guild_config ADD COLUMN {col} TEXT')
                except Exception:
                    pass  # Column already exists
            try:
                await db.execute(
                    "ALTER TABLE write_grants ADD COLUMN grant_type TEXT NOT NULL DEFAULT 'official'"
                )
            except Exception:
                pass  # Column already exists
            try:
                await db.execute('ALTER TABLE activity_snapshots ADD COLUMN active_users INTEGER')
            except Exception:
                pass  # Column already exists
            try:
                await db.execute('ALTER TABLE guild_config ADD COLUMN elders_role_id TEXT')
            except Exception:
                pass  # Column already exists
            await db.commit()

    async def backup(self):
        """Copy the live database to congobot.db.bak using SQLite's safe backup API."""
        async with aiosqlite.connect(self.db_path) as src:
            async with aiosqlite.connect(DB_BACKUP_PATH) as dst:
                await src.backup(dst)

    # ── Guild Config ─────────────────────────────────────────────────────────

    async def get_guild_config(self, guild_id: str) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM guild_config WHERE guild_id = ?', (guild_id,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def set_guild_config(self, guild_id: str, **kwargs):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                'SELECT 1 FROM guild_config WHERE guild_id = ?', (guild_id,)
            ) as cur:
                exists = await cur.fetchone()
            if exists:
                set_clause = ', '.join(f'{k} = ?' for k in kwargs)
                await db.execute(
                    f'UPDATE guild_config SET {set_clause} WHERE guild_id = ?',
                    [*kwargs.values(), guild_id]
                )
            else:
                kwargs['guild_id'] = guild_id
                cols = ', '.join(kwargs.keys())
                placeholders = ', '.join('?' * len(kwargs))
                await db.execute(
                    f'INSERT INTO guild_config ({cols}) VALUES ({placeholders})',
                    list(kwargs.values())
                )
            await db.commit()

    # ── User Requests ─────────────────────────────────────────────────────────

    async def get_user_request(self, discord_id: str, guild_id: str) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM user_requests WHERE discord_id = ? AND guild_id = ?',
                (discord_id, guild_id)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def create_user_request(self, discord_id: str, guild_id: str, channel_id: str,
                                   requested_role: str = None):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO user_requests
                   (discord_id, guild_id, channel_id, requested_role, status,
                    created_at, last_activity_at)
                   VALUES (?, ?, ?, ?, 'awaiting_warera_id', datetime('now'), datetime('now'))""",
                (discord_id, guild_id, channel_id, requested_role)
            )
            await db.commit()

    async def update_user_request(self, discord_id: str, guild_id: str, **kwargs):
        if 'completed_at' not in kwargs:
            kwargs['last_activity_at'] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        async with aiosqlite.connect(self.db_path) as db:
            set_clause = ', '.join(f'{k} = ?' for k in kwargs)
            await db.execute(
                f'UPDATE user_requests SET {set_clause} WHERE discord_id = ? AND guild_id = ?',
                [*kwargs.values(), discord_id, guild_id]
            )
            await db.commit()

    async def delete_user_request(self, discord_id: str, guild_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'DELETE FROM user_requests WHERE discord_id = ? AND guild_id = ?',
                (discord_id, guild_id)
            )
            await db.commit()

    async def get_pending_requests_by_status(self, guild_id: str, status: str) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM user_requests WHERE guild_id = ? AND status = ?',
                (guild_id, status)
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_inactive_requests(self, guild_id: str, days: int) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""SELECT * FROM user_requests
                    WHERE guild_id = ?
                    AND status NOT IN ('completed', 'rejected')
                    AND last_activity_at <= datetime('now', '-{days} days')""",
                (guild_id,)
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    # ── Reverification ───────────────────────────────────────────────────────

    async def create_reverification(
        self, discord_id: str, guild_id: str,
        roles_to_remove: list, reverify_type: str
    ):
        import json
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO reverification_pending
                   (discord_id, guild_id, roles_to_remove, reverify_type,
                    warn_count, last_warned_at, created_at)
                   VALUES (?, ?, ?, ?, 0, NULL, datetime('now'))""",
                (discord_id, guild_id, json.dumps(roles_to_remove), reverify_type)
            )
            await db.commit()

    async def get_reverification(self, discord_id: str, guild_id: str) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM reverification_pending WHERE discord_id = ? AND guild_id = ?',
                (discord_id, guild_id)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def get_all_pending_reverifications(self, guild_id: str) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM reverification_pending WHERE guild_id = ?', (guild_id,)
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def delete_reverification(self, discord_id: str, guild_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'DELETE FROM reverification_pending WHERE discord_id = ? AND guild_id = ?',
                (discord_id, guild_id)
            )
            await db.commit()

    async def update_reverification_warn(self, discord_id: str, guild_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE reverification_pending
                   SET warn_count = warn_count + 1, last_warned_at = datetime('now')
                   WHERE discord_id = ? AND guild_id = ?""",
                (discord_id, guild_id)
            )
            await db.commit()

    async def upsert_embassy_request_for_reverify(
        self, discord_id: str, guild_id: str,
        country_id: str, country_name: str, country_flag: str
    ):
        """Insert or update an embassy_request for reverification (preserves existing role data)."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                'SELECT id FROM embassy_requests WHERE discord_id = ? AND guild_id = ?',
                (discord_id, guild_id)
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                await db.execute(
                    """UPDATE embassy_requests
                       SET country_id = ?, country_name = ?, country_flag = ?
                       WHERE discord_id = ? AND guild_id = ?
                       AND id = (SELECT MAX(id) FROM embassy_requests
                                 WHERE discord_id = ? AND guild_id = ?)""",
                    (country_id, country_name, country_flag,
                     discord_id, guild_id, discord_id, guild_id)
                )
            else:
                await db.execute(
                    """INSERT INTO embassy_requests
                       (discord_id, guild_id, country_id, country_name, country_flag)
                       VALUES (?, ?, ?, ?, ?)""",
                    (discord_id, guild_id, country_id, country_name, country_flag)
                )
            await db.commit()

    # ── Embassy Requests ──────────────────────────────────────────────────────

    async def create_embassy_request(
        self, discord_id: str, guild_id: str, country_id: str,
        country_name: str, country_flag: str, warera_role: str, access_level: str
    ) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """INSERT INTO embassy_requests
                   (discord_id, guild_id, country_id, country_name, country_flag, warera_role, access_level)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (discord_id, guild_id, country_id, country_name, country_flag, warera_role, access_level)
            )
            await db.commit()
            return cur.lastrowid

    async def get_embassy_request(self, discord_id: str, guild_id: str) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM embassy_requests WHERE discord_id = ? AND guild_id = ?
                   ORDER BY id DESC LIMIT 1""",
                (discord_id, guild_id)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def update_embassy_request(self, discord_id: str, guild_id: str, **kwargs):
        async with aiosqlite.connect(self.db_path) as db:
            set_clause = ', '.join(f'{k} = ?' for k in kwargs)
            await db.execute(
                f"""UPDATE embassy_requests SET {set_clause}
                    WHERE discord_id = ? AND guild_id = ?
                    AND id = (SELECT MAX(id) FROM embassy_requests WHERE discord_id = ? AND guild_id = ?)""",
                [*kwargs.values(), discord_id, guild_id, discord_id, guild_id]
            )
            await db.commit()

    async def get_embassy_request_by_approval_msg(self, message_id: str) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM embassy_requests WHERE approval_message_id = ?', (message_id,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    # ── Tracked Users ─────────────────────────────────────────────────────────

    async def upsert_tracked_user(
        self, discord_id: str, guild_id: str, warera_id: str,
        assigned_role: str, country_id: str = None, discord_role_id: str = None
    ):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO tracked_users
                   (discord_id, guild_id, warera_id, assigned_role, country_id, discord_role_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (discord_id, guild_id, warera_id, assigned_role, country_id, discord_role_id)
            )
            await db.commit()

    async def get_tracked_user(self, discord_id: str, guild_id: str) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM tracked_users WHERE discord_id = ? AND guild_id = ?',
                (discord_id, guild_id)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def get_all_tracked_users(self, guild_id: str) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM tracked_users WHERE guild_id = ?', (guild_id,)
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def remove_tracked_user(self, discord_id: str, guild_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'DELETE FROM tracked_users WHERE discord_id = ? AND guild_id = ?',
                (discord_id, guild_id)
            )
            await db.commit()

    # ── Scheduled Deletions ───────────────────────────────────────────────────

    async def schedule_deletion(self, channel_id: str, delete_at: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'INSERT OR REPLACE INTO scheduled_deletions (channel_id, delete_at) VALUES (?, ?)',
                (channel_id, delete_at)
            )
            await db.commit()

    async def get_due_deletions(self) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM scheduled_deletions WHERE REPLACE(delete_at, 'T', ' ') <= datetime('now')"
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def remove_deletion(self, channel_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'DELETE FROM scheduled_deletions WHERE channel_id = ?', (channel_id,)
            )
            await db.commit()

    # ── Pending Approvals ─────────────────────────────────────────────────────

    async def add_pending_approval(self, message_id: str, guild_id: str, requester_discord_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'INSERT OR REPLACE INTO pending_approvals (message_id, guild_id, requester_discord_id) VALUES (?, ?, ?)',
                (message_id, guild_id, requester_discord_id)
            )
            await db.commit()

    async def get_pending_approval(self, message_id: str) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM pending_approvals WHERE message_id = ?', (message_id,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def remove_pending_approval(self, message_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'DELETE FROM pending_approvals WHERE message_id = ?', (message_id,)
            )
            await db.commit()

    # ── Write Grants ──────────────────────────────────────────────────────────

    async def add_write_grant(
        self, grantor_discord_id: str, grantor_warera_id: str,
        grantee_discord_id: str, guild_id: str, country_id: str, write_role_id: str,
        grant_type: str = 'official'
    ):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO write_grants
                   (grantor_discord_id, grantor_warera_id, grantee_discord_id,
                    guild_id, country_id, write_role_id, grant_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (grantor_discord_id, grantor_warera_id, grantee_discord_id,
                 guild_id, country_id, write_role_id, grant_type)
            )
            await db.commit()

    async def get_all_write_grants(self, guild_id: str) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM write_grants WHERE guild_id = ?', (guild_id,)
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_write_grants_by_grantor(self, grantor_discord_id: str, guild_id: str) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM write_grants WHERE grantor_discord_id = ? AND guild_id = ?',
                (grantor_discord_id, guild_id)
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_write_grants_by_grantee(self, grantee_discord_id: str, guild_id: str) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM write_grants WHERE grantee_discord_id = ? AND guild_id = ?',
                (grantee_discord_id, guild_id)
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def remove_write_grant(self, grantor_discord_id: str, grantee_discord_id: str, guild_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """DELETE FROM write_grants
                   WHERE grantor_discord_id = ? AND grantee_discord_id = ? AND guild_id = ?""",
                (grantor_discord_id, grantee_discord_id, guild_id)
            )
            await db.commit()

    async def remove_all_write_grants_by_grantor(self, grantor_discord_id: str, guild_id: str) -> List[Dict]:
        """Remove all grants made by this grantor and return them (to revoke roles)."""
        grants = await self.get_write_grants_by_grantor(grantor_discord_id, guild_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'DELETE FROM write_grants WHERE grantor_discord_id = ? AND guild_id = ?',
                (grantor_discord_id, guild_id)
            )
            await db.commit()
        return grants

    # ── Tracked Countries (activity tracker) ──────────────────────────────────

    async def add_tracked_country(
        self, country_id: str, country_name: str, country_flag: str,
        channel_id: str, guild_id: str, started_by: str
    ):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO tracked_countries
                   (country_id, country_name, country_flag, channel_id, guild_id, started_by)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (country_id, country_name, country_flag, channel_id, guild_id, started_by)
            )
            await db.commit()

    async def remove_tracked_country(self, country_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'DELETE FROM tracked_countries WHERE country_id = ?', (country_id,)
            )
            await db.commit()

    async def get_tracked_country(self, country_id: str) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM tracked_countries WHERE country_id = ?', (country_id,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def get_all_tracked_countries(self) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute('SELECT * FROM tracked_countries') as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    # ── Activity Snapshots ────────────────────────────────────────────────────

    async def insert_activity_snapshot(
        self, country_id: str, snapshot_time: str,
        total_users: int, online_count: int,
        online_low: int, online_mid: int, online_high: int, online_master: int,
        active_users: int = None
    ):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO activity_snapshots
                   (country_id, snapshot_time, total_users, online_count,
                    online_low, online_mid, online_high, online_master, active_users)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (country_id, snapshot_time, total_users, online_count,
                 online_low, online_mid, online_high, online_master, active_users)
            )
            await db.commit()

    async def backfill_active_users(self, country_id: str, active_users: int) -> int:
        """Set active_users on all snapshots for a country. Returns rows updated."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                'UPDATE activity_snapshots SET active_users = ? WHERE country_id = ?',
                (active_users, country_id)
            )
            await db.commit()
            return cur.rowcount

    async def get_activity_snapshots(self, country_id: str, since_days: int = 30) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM activity_snapshots
                   WHERE country_id = ?
                   AND snapshot_time >= datetime('now', ?)
                   ORDER BY snapshot_time ASC""",
                (country_id, f'-{since_days} days')
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_snapshot_count(self, country_id: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                'SELECT COUNT(*) FROM activity_snapshots WHERE country_id = ?', (country_id,)
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0

    async def purge_activity_snapshots(self, country_id: str) -> int:
        """Delete all snapshots for a country. Returns the number of rows deleted."""
        count = await self.get_snapshot_count(country_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'DELETE FROM activity_snapshots WHERE country_id = ?', (country_id,)
            )
            await db.commit()
        return count
