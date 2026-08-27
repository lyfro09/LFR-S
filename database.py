"""SQLite persistence for LFR Spam.

Every public operation is asynchronous and runs blocking sqlite3 work outside
the Discord event loop. Mutating multi-step operations use transactions so a
double click cannot spend points or redeem a key twice.
"""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
import string
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _quoted_identifier(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def _migrate_legacy_schema(self, connection: sqlite3.Connection) -> None:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

        if "users" in tables:
            user_columns = [
                row["name"]
                for row in connection.execute("PRAGMA table_info(users)").fetchall()
            ]
            if "total_runs" not in user_columns or "vip_trial_used" not in user_columns:
                if len(user_columns) < 7:
                    raise sqlite3.DatabaseError("Unsupported legacy users schema")
                source = [self._quoted_identifier(name) for name in user_columns]
                trial_source = source[7] if len(source) > 7 else "0"
                connection.executescript(
                    """
                    CREATE TABLE users_migrated (
                        user_id INTEGER PRIMARY KEY,
                        points INTEGER NOT NULL DEFAULT 0,
                        total_runs INTEGER NOT NULL DEFAULT 0,
                        total_messages INTEGER NOT NULL DEFAULT 0,
                        vip_until INTEGER NOT NULL DEFAULT 0,
                        created_at INTEGER NOT NULL,
                        last_daily INTEGER NOT NULL DEFAULT 0,
                        vip_trial_used INTEGER NOT NULL DEFAULT 0
                    );
                    """
                )
                connection.execute(
                    f"""
                    INSERT INTO users_migrated(
                        user_id, points, total_runs, total_messages, vip_until,
                        created_at, last_daily, vip_trial_used
                    )
                    SELECT {source[0]}, COALESCE({source[1]}, 0),
                           COALESCE({source[2]}, 0), COALESCE({source[3]}, 0),
                           COALESCE({source[4]}, 0), COALESCE({source[5]}, ?),
                           COALESCE({source[6]}, 0), COALESCE({trial_source}, 0)
                    FROM users
                    """,
                    (int(time.time()),),
                )
                connection.executescript(
                    """
                    DROP TABLE users;
                    ALTER TABLE users_migrated RENAME TO users;
                    """
                )

        if "spam_history" not in tables:
            history_signature = {
                "user_id",
                "started_at",
                "requested_messages",
                "sent_messages",
                "delay",
                "mode",
                "status",
            }
            for table in tables - {"users", "keys", "sqlite_sequence"}:
                quoted_table = self._quoted_identifier(table)
                columns = {
                    row["name"]
                    for row in connection.execute(
                        f"PRAGMA table_info({quoted_table})"
                    ).fetchall()
                }
                if history_signature <= columns:
                    connection.execute(
                        f"ALTER TABLE {quoted_table} RENAME TO spam_history"
                    )
                    break

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            self._migrate_legacy_schema(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    points INTEGER NOT NULL DEFAULT 0,
                    total_runs INTEGER NOT NULL DEFAULT 0,
                    total_messages INTEGER NOT NULL DEFAULT 0,
                    vip_until INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    last_daily INTEGER NOT NULL DEFAULT 0,
                    vip_trial_used INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS keys (
                    key TEXT PRIMARY KEY,
                    duration_seconds INTEGER NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0,
                    used_by INTEGER,
                    used_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS spam_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    requested_messages INTEGER NOT NULL,
                    sent_messages INTEGER NOT NULL DEFAULT 0,
                    failed_messages INTEGER NOT NULL DEFAULT 0,
                    delay REAL NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'random',
                    status TEXT NOT NULL DEFAULT 'running'
                );

                CREATE INDEX IF NOT EXISTS idx_users_points
                    ON users(points DESC);
                CREATE INDEX IF NOT EXISTS idx_users_runs
                    ON users(total_runs DESC);
                CREATE INDEX IF NOT EXISTS idx_users_messages
                    ON users(total_messages DESC);
                CREATE INDEX IF NOT EXISTS idx_users_vip
                    ON users(vip_until);
                CREATE INDEX IF NOT EXISTS idx_spam_history_user_started
                    ON spam_history(user_id, started_at DESC);

                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL UNIQUE,
                    guild_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL,
                    ticket_type TEXT NOT NULL CHECK(ticket_type IN ('support', 'report')),
                    category TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    description TEXT NOT NULL,
                    target TEXT,
                    evidence TEXT,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open', 'closed')),
                    claimed_by INTEGER,
                    panel_message_id INTEGER,
                    created_at INTEGER NOT NULL,
                    closed_at INTEGER,
                    closed_by INTEGER
                );

                CREATE TABLE IF NOT EXISTS ticket_members (
                    ticket_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    PRIMARY KEY(ticket_id, user_id),
                    FOREIGN KEY(ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_one_open_per_type
                    ON tickets(guild_id, owner_id, ticket_type)
                    WHERE status = 'open';
                CREATE INDEX IF NOT EXISTS idx_tickets_channel
                    ON tickets(channel_id);
                CREATE INDEX IF NOT EXISTS idx_ticket_members_ticket
                    ON ticket_members(ticket_id);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "vip_trial_used" not in columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN vip_trial_used INTEGER NOT NULL DEFAULT 0"
                )

            history_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(spam_history)"
                ).fetchall()
            }
            history_migrations = {
                "failed_messages": "INTEGER NOT NULL DEFAULT 0",
                "mode": "TEXT NOT NULL DEFAULT 'random'",
                "status": "TEXT NOT NULL DEFAULT 'failed'",
                "finished_at": "INTEGER",
            }
            for name, definition in history_migrations.items():
                if name not in history_columns:
                    connection.execute(
                        f"ALTER TABLE spam_history ADD COLUMN {name} {definition}"
                    )

            # A process restart cannot resume an in-memory send loop. Mark any
            # abandoned rows so /history never reports a permanently active run.
            now = int(time.time())
            connection.execute(
                """
                UPDATE spam_history
                SET status = 'failed', finished_at = COALESCE(finished_at, ?),
                    failed_messages = MAX(0, requested_messages - sent_messages)
                WHERE status = 'running'
                """,
                (now,),
            )

    @staticmethod
    def _ensure_user(connection: sqlite3.Connection, user_id: int, now: int) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO users(user_id, created_at) VALUES (?, ?)",
            (user_id, now),
        )

    async def get_user(self, user_id: int) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_user_sync, user_id)

    def _get_user_sync(self, user_id: int) -> dict[str, Any]:
        now = int(time.time())
        with self._connection() as connection:
            self._ensure_user(connection, user_id, now)
            row = connection.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return dict(row)

    async def record_spam(self, user_id: int, sent: int, reward: int) -> None:
        await asyncio.to_thread(self._record_spam_sync, user_id, sent, reward)

    def _record_spam_sync(self, user_id: int, sent: int, reward: int) -> None:
        now = int(time.time())
        with self._connection() as connection:
            self._ensure_user(connection, user_id, now)
            connection.execute(
                """
                UPDATE users
                SET total_runs = total_runs + 1,
                    total_messages = total_messages + ?,
                    points = points + ?
                WHERE user_id = ?
                """,
                (max(0, sent), max(0, reward), user_id),
            )

    async def create_spam_session(
        self,
        user_id: int,
        started_at: int,
        requested_messages: int,
        delay: float,
        mode: str,
    ) -> int:
        return await asyncio.to_thread(
            self._create_spam_session_sync,
            user_id,
            started_at,
            requested_messages,
            delay,
            mode,
        )

    def _create_spam_session_sync(
        self,
        user_id: int,
        started_at: int,
        requested_messages: int,
        delay: float,
        mode: str,
    ) -> int:
        with self._connection() as connection:
            self._ensure_user(connection, user_id, started_at)
            cursor = connection.execute(
                """
                INSERT INTO spam_history(
                    user_id, started_at, requested_messages, delay, mode, status
                ) VALUES (?, ?, ?, ?, ?, 'running')
                """,
                (user_id, started_at, requested_messages, delay, mode),
            )
            return int(cursor.lastrowid)

    async def finish_spam_session(
        self,
        session_id: int,
        user_id: int,
        finished_at: int,
        sent_messages: int,
        failed_messages: int,
        status: str,
        reward: int,
    ) -> None:
        await asyncio.to_thread(
            self._finish_spam_session_sync,
            session_id,
            user_id,
            finished_at,
            sent_messages,
            failed_messages,
            status,
            reward,
        )

    def _finish_spam_session_sync(
        self,
        session_id: int,
        user_id: int,
        finished_at: int,
        sent_messages: int,
        failed_messages: int,
        status: str,
        reward: int,
    ) -> None:
        allowed_statuses = {"completed", "failed", "stopped"}
        if status not in allowed_statuses:
            raise ValueError("Unsupported spam status")
        sent_messages = max(0, int(sent_messages))
        failed_messages = max(0, int(failed_messages))
        reward = max(0, int(reward)) if sent_messages > 0 else 0
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_user(connection, user_id, finished_at)
            updated = connection.execute(
                """
                UPDATE spam_history
                SET finished_at = ?, sent_messages = ?, failed_messages = ?,
                    status = ?
                WHERE id = ? AND user_id = ? AND status = 'running'
                """,
                (
                    finished_at,
                    sent_messages,
                    failed_messages,
                    status,
                    session_id,
                    user_id,
                ),
            ).rowcount
            if updated != 1:
                raise sqlite3.IntegrityError("Spam session is missing or already finished")
            connection.execute(
                """
                UPDATE users
                SET total_runs = total_runs + 1,
                    total_messages = total_messages + ?,
                    points = points + ?
                WHERE user_id = ?
                """,
                (sent_messages, reward, user_id),
            )

    async def get_spam_history(
        self, user_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._get_spam_history_sync, user_id, limit)

    def _get_spam_history_sync(
        self, user_id: int, limit: int
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, started_at, finished_at, requested_messages,
                       sent_messages, failed_messages, delay, mode, status
                FROM spam_history
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, max(1, min(int(limit), 50))),
            ).fetchall()
        return [dict(row) for row in rows]

    async def claim_daily(
        self, user_id: int, reward: int, cooldown: int
    ) -> tuple[bool, int]:
        return await asyncio.to_thread(
            self._claim_daily_sync, user_id, reward, cooldown
        )

    def _claim_daily_sync(
        self, user_id: int, reward: int, cooldown: int
    ) -> tuple[bool, int]:
        now = int(time.time())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_user(connection, user_id, now)
            row = connection.execute(
                "SELECT last_daily FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            remaining = int(row["last_daily"]) + cooldown - now
            if remaining > 0:
                return False, remaining
            connection.execute(
                "UPDATE users SET points = points + ?, last_daily = ? WHERE user_id = ?",
                (reward, now, user_id),
            )
            return True, 0

    async def get_top(self, metric: str, limit: int = 10) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._get_top_sync, metric, limit)

    def _get_top_sync(self, metric: str, limit: int) -> list[dict[str, Any]]:
        allowed = {"points", "total_runs", "total_messages"}
        if metric not in allowed:
            raise ValueError("Unsupported leaderboard metric")
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT user_id, {metric} AS value FROM users "
                f"ORDER BY {metric} DESC, user_id ASC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    async def purchase_vip(
        self, user_id: int, price: int, duration_seconds: int
    ) -> tuple[bool, int, int]:
        return await asyncio.to_thread(
            self._purchase_vip_sync, user_id, price, duration_seconds
        )

    def _purchase_vip_sync(
        self, user_id: int, price: int, duration_seconds: int
    ) -> tuple[bool, int, int]:
        now = int(time.time())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_user(connection, user_id, now)
            row = connection.execute(
                "SELECT points, vip_until FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if int(row["points"]) < price:
                return False, int(row["points"]), int(row["vip_until"])
            vip_until = max(now, int(row["vip_until"])) + duration_seconds
            connection.execute(
                "UPDATE users SET points = points - ?, vip_until = ? WHERE user_id = ?",
                (price, vip_until, user_id),
            )
            return True, int(row["points"]) - price, vip_until

    async def redeem_key(self, user_id: int, key: str) -> tuple[bool, str, int]:
        return await asyncio.to_thread(self._redeem_key_sync, user_id, key)

    def _redeem_key_sync(self, user_id: int, key: str) -> tuple[bool, str, int]:
        now = int(time.time())
        normalized = key.strip().upper()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT duration_seconds, used FROM keys WHERE key = ?", (normalized,)
            ).fetchone()
            if row is None:
                return False, "not_found", 0
            if int(row["used"]):
                return False, "used", 0
            self._ensure_user(connection, user_id, now)
            user = connection.execute(
                "SELECT vip_until FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            vip_until = max(now, int(user["vip_until"])) + int(
                row["duration_seconds"]
            )
            updated = connection.execute(
                """
                UPDATE keys SET used = 1, used_by = ?, used_at = ?
                WHERE key = ? AND used = 0
                """,
                (user_id, now, normalized),
            ).rowcount
            if updated != 1:
                return False, "used", 0
            connection.execute(
                "UPDATE users SET vip_until = ? WHERE user_id = ?",
                (vip_until, user_id),
            )
            return True, "ok", vip_until

    async def use_vip_trial(self, user_id: int, duration_seconds: int) -> tuple[bool, int]:
        return await asyncio.to_thread(
            self._use_vip_trial_sync, user_id, duration_seconds
        )

    def _use_vip_trial_sync(self, user_id: int, duration_seconds: int) -> tuple[bool, int]:
        now = int(time.time())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_user(connection, user_id, now)
            row = connection.execute(
                "SELECT vip_trial_used, vip_until FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if int(row["vip_trial_used"]):
                return False, int(row["vip_until"])
            vip_until = max(now, int(row["vip_until"])) + duration_seconds
            connection.execute(
                "UPDATE users SET vip_trial_used = 1, vip_until = ? WHERE user_id = ?",
                (vip_until, user_id),
            )
            return True, vip_until

    async def create_keys(self, duration_seconds: int, amount: int) -> list[str]:
        return await asyncio.to_thread(
            self._create_keys_sync, duration_seconds, amount
        )

    def _create_keys_sync(self, duration_seconds: int, amount: int) -> list[str]:
        alphabet = string.ascii_uppercase + string.digits
        created: list[str] = []
        with self._connection() as connection:
            while len(created) < amount:
                parts = [
                    "".join(secrets.choice(alphabet) for _ in range(4))
                    for _ in range(3)
                ]
                key = "LFR-" + "-".join(parts)
                try:
                    connection.execute(
                        "INSERT INTO keys(key, duration_seconds) VALUES (?, ?)",
                        (key, duration_seconds),
                    )
                except sqlite3.IntegrityError:
                    continue
                created.append(key)
        return created

    async def grant_vip(self, user_id: int, duration_seconds: int) -> int:
        return await asyncio.to_thread(
            self._grant_vip_sync, user_id, duration_seconds
        )

    def _grant_vip_sync(self, user_id: int, duration_seconds: int) -> int:
        now = int(time.time())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_user(connection, user_id, now)
            row = connection.execute(
                "SELECT vip_until FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            vip_until = max(now, int(row["vip_until"])) + duration_seconds
            connection.execute(
                "UPDATE users SET vip_until = ? WHERE user_id = ?",
                (vip_until, user_id),
            )
            return vip_until

    async def set_permanent_vip(self, user_id: int, vip_until: int) -> None:
        await asyncio.to_thread(
            self._set_permanent_vip_sync, user_id, vip_until
        )

    def _set_permanent_vip_sync(self, user_id: int, vip_until: int) -> None:
        now = int(time.time())
        with self._connection() as connection:
            self._ensure_user(connection, user_id, now)
            connection.execute(
                "UPDATE users SET vip_until = ? WHERE user_id = ?",
                (vip_until, user_id),
            )

    async def remove_vip(self, user_id: int) -> None:
        await asyncio.to_thread(self._remove_vip_sync, user_id)

    def _remove_vip_sync(self, user_id: int) -> None:
        now = int(time.time())
        with self._connection() as connection:
            self._ensure_user(connection, user_id, now)
            connection.execute(
                "UPDATE users SET vip_until = 0 WHERE user_id = ?", (user_id,)
            )

    async def change_points(self, user_id: int, delta: int) -> int:
        return await asyncio.to_thread(self._change_points_sync, user_id, delta)

    def _change_points_sync(self, user_id: int, delta: int) -> int:
        now = int(time.time())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_user(connection, user_id, now)
            connection.execute(
                "UPDATE users SET points = MAX(0, points + ?) WHERE user_id = ?",
                (delta, user_id),
            )
            row = connection.execute(
                "SELECT points FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            return int(row["points"])

    async def get_global_stats(self) -> dict[str, int]:
        return await asyncio.to_thread(self._get_global_stats_sync)

    def _get_global_stats_sync(self) -> dict[str, int]:
        now = int(time.time())
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS users,
                       COALESCE(SUM(total_runs), 0) AS runs,
                       COALESCE(SUM(total_messages), 0) AS messages,
                       COALESCE(SUM(CASE WHEN vip_until > ? THEN 1 ELSE 0 END), 0)
                           AS active_vip
                FROM users
                """,
                (now,),
            ).fetchone()
        return {name: int(row[name]) for name in row.keys()}

    async def is_healthy(self) -> bool:
        try:
            return await asyncio.to_thread(self._health_sync)
        except sqlite3.Error:
            return False

    def _health_sync(self) -> bool:
        with self._connection() as connection:
            return connection.execute("SELECT 1").fetchone()[0] == 1

    async def get_ticket_by_channel(self, channel_id: int) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_ticket_by_channel_sync, channel_id)

    def _get_ticket_by_channel_sync(self, channel_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM tickets WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    async def get_open_ticket(
        self, guild_id: int, owner_id: int, ticket_type: str
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            self._get_open_ticket_sync, guild_id, owner_id, ticket_type
        )

    def _get_open_ticket_sync(
        self, guild_id: int, owner_id: int, ticket_type: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM tickets
                WHERE guild_id = ? AND owner_id = ? AND ticket_type = ?
                      AND status = 'open'
                """,
                (guild_id, owner_id, ticket_type),
            ).fetchone()
        return dict(row) if row is not None else None

    async def create_ticket(
        self,
        *,
        channel_id: int,
        guild_id: int,
        owner_id: int,
        ticket_type: str,
        category: str,
        subject: str,
        description: str,
        target: str | None = None,
        evidence: str | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._create_ticket_sync,
            channel_id,
            guild_id,
            owner_id,
            ticket_type,
            category,
            subject,
            description,
            target,
            evidence,
        )

    def _create_ticket_sync(
        self,
        channel_id: int,
        guild_id: int,
        owner_id: int,
        ticket_type: str,
        category: str,
        subject: str,
        description: str,
        target: str | None,
        evidence: str | None,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tickets(
                    channel_id, guild_id, owner_id, ticket_type, category,
                    subject, description, target, evidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_id,
                    guild_id,
                    owner_id,
                    ticket_type,
                    category,
                    subject,
                    description,
                    target,
                    evidence,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM tickets WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row)

    async def set_ticket_panel(self, ticket_id: int, message_id: int) -> None:
        await asyncio.to_thread(self._set_ticket_panel_sync, ticket_id, message_id)

    def _set_ticket_panel_sync(self, ticket_id: int, message_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE tickets SET panel_message_id = ? WHERE id = ?",
                (message_id, ticket_id),
            )

    async def claim_ticket(self, ticket_id: int, staff_id: int) -> tuple[bool, dict[str, Any]]:
        return await asyncio.to_thread(self._claim_ticket_sync, ticket_id, staff_id)

    def _claim_ticket_sync(
        self, ticket_id: int, staff_id: int
    ) -> tuple[bool, dict[str, Any]]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE tickets SET claimed_by = ?
                WHERE id = ? AND status = 'open' AND claimed_by IS NULL
                """,
                (staff_id, ticket_id),
            ).rowcount
            row = connection.execute(
                "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
        return changed == 1, dict(row)

    async def close_ticket(self, ticket_id: int, closed_by: int) -> dict[str, Any]:
        return await asyncio.to_thread(self._close_ticket_sync, ticket_id, closed_by)

    def _close_ticket_sync(self, ticket_id: int, closed_by: int) -> dict[str, Any]:
        now = int(time.time())
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE tickets
                SET status = 'closed', closed_at = ?, closed_by = ?
                WHERE id = ? AND status = 'open'
                """,
                (now, closed_by, ticket_id),
            )
            row = connection.execute(
                "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
        return dict(row)

    async def reopen_ticket(self, ticket_id: int) -> dict[str, Any]:
        return await asyncio.to_thread(self._reopen_ticket_sync, ticket_id)

    def _reopen_ticket_sync(self, ticket_id: int) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT guild_id, owner_id, ticket_type FROM tickets WHERE id = ?",
                (ticket_id,),
            ).fetchone()
            if row is None:
                raise LookupError("Ticket not found")
            duplicate = connection.execute(
                """
                SELECT id FROM tickets
                WHERE guild_id = ? AND owner_id = ? AND ticket_type = ?
                      AND status = 'open' AND id != ?
                """,
                (row["guild_id"], row["owner_id"], row["ticket_type"], ticket_id),
            ).fetchone()
            if duplicate is not None:
                raise sqlite3.IntegrityError("An open ticket of this type already exists")
            connection.execute(
                """
                UPDATE tickets
                SET status = 'open', closed_at = NULL, closed_by = NULL
                WHERE id = ?
                """,
                (ticket_id,),
            )
            updated = connection.execute(
                "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
        return dict(updated)

    async def add_ticket_member(self, ticket_id: int, user_id: int) -> None:
        await asyncio.to_thread(self._add_ticket_member_sync, ticket_id, user_id)

    def _add_ticket_member_sync(self, ticket_id: int, user_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO ticket_members(ticket_id, user_id) VALUES (?, ?)",
                (ticket_id, user_id),
            )

    async def remove_ticket_member(self, ticket_id: int, user_id: int) -> bool:
        return await asyncio.to_thread(
            self._remove_ticket_member_sync, ticket_id, user_id
        )

    def _remove_ticket_member_sync(self, ticket_id: int, user_id: int) -> bool:
        with self._connection() as connection:
            changed = connection.execute(
                "DELETE FROM ticket_members WHERE ticket_id = ? AND user_id = ?",
                (ticket_id, user_id),
            ).rowcount
        return changed == 1

    async def get_ticket_members(self, ticket_id: int) -> list[int]:
        return await asyncio.to_thread(self._get_ticket_members_sync, ticket_id)

    def _get_ticket_members_sync(self, ticket_id: int) -> list[int]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT user_id FROM ticket_members WHERE ticket_id = ? ORDER BY user_id",
                (ticket_id,),
            ).fetchall()
        return [int(row["user_id"]) for row in rows]

    async def delete_ticket(self, ticket_id: int) -> None:
        await asyncio.to_thread(self._delete_ticket_sync, ticket_id)

    def _delete_ticket_sync(self, ticket_id: int) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
