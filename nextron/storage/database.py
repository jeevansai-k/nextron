"""Local SQLite statistics store (``~/.config/nextron/stats.db``).

Nothing is ever transmitted anywhere. The database exists so a long-running
session can answer questions like "how many identities did I rotate through
tonight" and "which exit countries have I used" after a restart.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from nextron.core.exceptions import StorageError
from nextron.storage import paths

log = logging.getLogger(__name__)

__all__ = ["Database", "SessionStats"]

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT    NOT NULL,
    ended_at      TEXT,
    routing_mode  TEXT    NOT NULL,
    app_version   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
    occurred_at TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,
    level       TEXT    NOT NULL DEFAULT 'info',
    message     TEXT    NOT NULL DEFAULT '',
    payload     TEXT
);

CREATE TABLE IF NOT EXISTS rotations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
    occurred_at  TEXT    NOT NULL,
    kind         TEXT    NOT NULL,           -- 'tor' | 'vpn'
    detail       TEXT,                       -- circuit id or profile name
    exit_ip      TEXT,
    exit_country TEXT,
    duration_ms  INTEGER,
    success      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS ip_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
    observed_at TEXT    NOT NULL,
    scope       TEXT    NOT NULL,            -- 'public' | 'vpn' | 'tor'
    ip          TEXT    NOT NULL,
    country     TEXT
);

CREATE TABLE IF NOT EXISTS verifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
    checked_at  TEXT    NOT NULL,
    mode        TEXT    NOT NULL,
    passed      INTEGER NOT NULL,
    checks      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS dns_counters (
    day       TEXT PRIMARY KEY,
    queries   INTEGER NOT NULL DEFAULT 0,
    blocked   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_events_session   ON events(session_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_rotations_kind   ON rotations(kind, occurred_at);
CREATE INDEX IF NOT EXISTS idx_ip_history_scope ON ip_history(scope, observed_at);
"""


@dataclass(frozen=True, slots=True)
class SessionStats:
    """Aggregated lifetime counters shown on the Logs / About screens."""

    sessions: int = 0
    tor_rotations: int = 0
    vpn_switches: int = 0
    unique_exit_ips: int = 0
    countries: tuple[str, ...] = ()
    dns_queries: int = 0
    dns_blocked: int = 0
    verifications_passed: int = 0
    verifications_failed: int = 0

    @property
    def dns_block_rate(self) -> float:
        if not self.dns_queries:
            return 0.0
        return self.dns_blocked / self.dns_queries * 100.0


class Database:
    """Thin async wrapper around the statistics database."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or paths.database_file()
        self._conn: aiosqlite.Connection | None = None
        self._session_id: int | None = None

    # -- lifecycle ---------------------------------------------------------- #

    @property
    def session_id(self) -> int | None:
        return self._session_id

    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> None:
        """Open the database and apply the schema (idempotent)."""
        if self._conn is not None:
            return
        paths.ensure_layout()
        try:
            self._conn = await aiosqlite.connect(self._path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.executescript(_SCHEMA)
            await self._conn.commit()
        except (aiosqlite.Error, OSError) as exc:
            self._conn = None
            raise StorageError(f"Cannot open {self._path}: {exc}") from exc
        log.debug("Statistics database ready at %s", self._path)

    async def close(self) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.commit()
            await self._conn.close()
        except aiosqlite.Error as exc:  # pragma: no cover
            log.warning("Database close failed: %s", exc)
        finally:
            self._conn = None

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise StorageError("Database is not connected")
        return self._conn

    # -- writes ------------------------------------------------------------- #

    async def start_session(self, routing_mode: str, app_version: str) -> int:
        conn = self._require()
        cursor = await conn.execute(
            "INSERT INTO sessions (started_at, routing_mode, app_version)"
            " VALUES (?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), routing_mode, app_version),
        )
        await conn.commit()
        self._session_id = int(cursor.lastrowid or 0)
        return self._session_id

    async def end_session(self) -> None:
        if self._session_id is None or self._conn is None:
            return
        await self._conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), self._session_id),
        )
        await self._conn.commit()

    async def record_event(
        self,
        event_type: str,
        message: str = "",
        *,
        level: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> None:
        conn = self._require()
        await conn.execute(
            "INSERT INTO events (session_id, occurred_at, event_type, level, message,"
            " payload) VALUES (?, ?, ?, ?, ?, ?)",
            (
                self._session_id,
                datetime.now().isoformat(timespec="seconds"),
                event_type,
                level,
                message,
                json.dumps(payload, default=str) if payload else None,
            ),
        )
        await conn.commit()

    async def record_rotation(
        self,
        kind: str,
        *,
        detail: str | None = None,
        exit_ip: str | None = None,
        exit_country: str | None = None,
        duration_ms: int | None = None,
        success: bool = True,
    ) -> None:
        conn = self._require()
        await conn.execute(
            "INSERT INTO rotations (session_id, occurred_at, kind, detail, exit_ip,"
            " exit_country, duration_ms, success) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._session_id,
                datetime.now().isoformat(timespec="seconds"),
                kind,
                detail,
                exit_ip,
                exit_country,
                duration_ms,
                int(success),
            ),
        )
        await conn.commit()

    async def record_ip(
        self, scope: str, ip: str, country: str | None = None
    ) -> None:
        conn = self._require()
        await conn.execute(
            "INSERT INTO ip_history (session_id, observed_at, scope, ip, country)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                self._session_id,
                datetime.now().isoformat(timespec="seconds"),
                scope,
                ip,
                country,
            ),
        )
        await conn.commit()

    async def record_verification(
        self, mode: str, passed: bool, checks: list[dict[str, Any]]
    ) -> None:
        conn = self._require()
        await conn.execute(
            "INSERT INTO verifications (session_id, checked_at, mode, passed, checks)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                self._session_id,
                datetime.now().isoformat(timespec="seconds"),
                mode,
                int(passed),
                json.dumps(checks, default=str),
            ),
        )
        await conn.commit()

    async def bump_dns_counters(self, queries: int = 0, blocked: int = 0) -> None:
        """Accumulate DNS counters for today (called periodically, not per query)."""
        if not queries and not blocked:
            return
        conn = self._require()
        day = datetime.now().date().isoformat()
        await conn.execute(
            "INSERT INTO dns_counters (day, queries, blocked) VALUES (?, ?, ?)"
            " ON CONFLICT(day) DO UPDATE SET"
            " queries = queries + excluded.queries,"
            " blocked = blocked + excluded.blocked",
            (day, queries, blocked),
        )
        await conn.commit()

    # -- reads -------------------------------------------------------------- #

    async def _scalar(self, sql: str, params: tuple = ()) -> Any:
        conn = self._require()
        async with conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else 0

    async def stats(self) -> SessionStats:
        """Compute lifetime statistics."""
        conn = self._require()
        countries: list[str] = []
        async with conn.execute(
            "SELECT exit_country, COUNT(*) AS hits FROM rotations"
            " WHERE exit_country IS NOT NULL GROUP BY exit_country"
            " ORDER BY hits DESC LIMIT 10"
        ) as cursor:
            countries = [row["exit_country"] async for row in cursor]

        return SessionStats(
            sessions=await self._scalar("SELECT COUNT(*) FROM sessions"),
            tor_rotations=await self._scalar(
                "SELECT COUNT(*) FROM rotations WHERE kind = 'tor'"
            ),
            vpn_switches=await self._scalar(
                "SELECT COUNT(*) FROM rotations WHERE kind = 'vpn'"
            ),
            unique_exit_ips=await self._scalar(
                "SELECT COUNT(DISTINCT ip) FROM ip_history"
            ),
            countries=tuple(countries),
            dns_queries=await self._scalar("SELECT SUM(queries) FROM dns_counters"),
            dns_blocked=await self._scalar("SELECT SUM(blocked) FROM dns_counters"),
            verifications_passed=await self._scalar(
                "SELECT COUNT(*) FROM verifications WHERE passed = 1"
            ),
            verifications_failed=await self._scalar(
                "SELECT COUNT(*) FROM verifications WHERE passed = 0"
            ),
        )

    async def recent_rotations(self, limit: int = 25) -> list[dict[str, Any]]:
        conn = self._require()
        async with conn.execute(
            "SELECT occurred_at, kind, detail, exit_ip, exit_country, success"
            " FROM rotations ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            return [dict(row) async for row in cursor]

    async def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        conn = self._require()
        async with conn.execute(
            "SELECT occurred_at, event_type, level, message FROM events"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            return [dict(row) async for row in cursor]

    async def ip_history(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._require()
        async with conn.execute(
            "SELECT observed_at, scope, ip, country FROM ip_history"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            return [dict(row) async for row in cursor]

    async def purge(self, *, keep_sessions: int = 20) -> int:
        """Drop all but the newest *keep_sessions* sessions. Returns rows removed."""
        conn = self._require()
        cursor = await conn.execute(
            "DELETE FROM sessions WHERE id NOT IN ("
            " SELECT id FROM sessions ORDER BY id DESC LIMIT ?)",
            (keep_sessions,),
        )
        await conn.commit()
        await conn.execute("VACUUM")
        return cursor.rowcount or 0
