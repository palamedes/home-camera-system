"""SQLite storage.

Holds users, sessions, cameras, and the recording index. Connections are
per-thread: the recorder, pruner, and web workers all touch this concurrently,
and a sqlite3 connection is not safe to share across threads.

WAL mode matters here — the segment indexer writes every minute per camera
while the UI reads the timeline, and WAL keeps readers from blocking on it.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS cameras (
    id            TEXT    PRIMARY KEY,
    name          TEXT    NOT NULL,
    host          TEXT    NOT NULL,
    port          INTEGER NOT NULL DEFAULT 80,
    brand         TEXT,
    model         TEXT,
    serial        TEXT,
    mac           TEXT,
    username      TEXT,
    password      TEXT,
    main_url      TEXT,
    sub_url       TEXT,
    record        INTEGER NOT NULL DEFAULT 1,
    record_stream TEXT    NOT NULL DEFAULT 'main',
    -- When set, recording auto-stops at this epoch time (a bounded window like
    -- "record for the next 3 days"). NULL means record continuously.
    record_until  REAL,
    -- Per-camera retention in seconds; NULL falls back to the global limit.
    retention_seconds INTEGER,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL,
    last_seen     INTEGER
);

CREATE TABLE IF NOT EXISTS segments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id  TEXT    NOT NULL,
    path       TEXT    NOT NULL UNIQUE,
    start_ts   REAL    NOT NULL,
    duration   REAL    NOT NULL,
    size       INTEGER NOT NULL,
    codec      TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_lookup ON segments(camera_id, start_ts);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Serialises writes. SQLite handles concurrent writers by returning
        # SQLITE_BUSY; taking the lock ourselves avoids relying on that.
        self._write_lock = threading.Lock()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
        self._migrate()
        self.path.chmod(0o600)  # camera passwords live in here

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        CREATE TABLE IF NOT EXISTS never alters an existing table, so new
        columns have to be added explicitly. Each is guarded by a presence
        check, making this safe to run on every startup.
        """
        have = {row["name"] for row in self.query("PRAGMA table_info(cameras)")}
        additions = {
            "record_until": "ALTER TABLE cameras ADD COLUMN record_until REAL",
            "retention_seconds": "ALTER TABLE cameras ADD COLUMN retention_seconds INTEGER",
        }
        for column, statement in additions.items():
            if column not in have:
                self.execute(statement)

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.connect().execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._write_lock:
            conn = self.connect()
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            return cur

    # ---- users -----------------------------------------------------------

    def user_count(self) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0

    def create_user(self, username: str, password_hash: str) -> int:
        cur = self.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, int(time.time())),
        )
        return int(cur.lastrowid or 0)

    def user_by_name(self, username: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM users WHERE username = ?", (username,))

    def user_by_id(self, user_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM users WHERE id = ?", (user_id,))

    # ---- sessions --------------------------------------------------------

    def create_session(self, token: str, user_id: int, ttl_seconds: int) -> None:
        now = int(time.time())
        self.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now, now + ttl_seconds),
        )

    def session(self, token: str) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM sessions WHERE token = ? AND expires_at > ?",
            (token, int(time.time())),
        )

    def delete_session(self, token: str) -> None:
        self.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def purge_expired_sessions(self) -> None:
        self.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))

    # ---- cameras ---------------------------------------------------------

    def cameras(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM cameras"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return self.query(sql + " ORDER BY name")

    def camera(self, camera_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM cameras WHERE id = ?", (camera_id,))

    def add_camera(self, **fields: Any) -> None:
        fields.setdefault("created_at", int(time.time()))
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        self.execute(
            f"INSERT INTO cameras ({cols}) VALUES ({marks})", tuple(fields.values())
        )

    def update_camera(self, camera_id: str, **fields: Any) -> None:
        if not fields:
            return
        assigns = ", ".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE cameras SET {assigns} WHERE id = ?",
            (*fields.values(), camera_id),
        )

    def delete_camera(self, camera_id: str) -> None:
        self.execute("DELETE FROM cameras WHERE id = ?", (camera_id,))
        self.execute("DELETE FROM segments WHERE camera_id = ?", (camera_id,))

    # ---- segments --------------------------------------------------------

    def add_segment(
        self, camera_id: str, path: str, start_ts: float, duration: float,
        size: int, codec: str | None,
    ) -> None:
        self.execute(
            "INSERT OR IGNORE INTO segments "
            "(camera_id, path, start_ts, duration, size, codec, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (camera_id, path, start_ts, duration, size, codec, int(time.time())),
        )

    def segments_in_range(
        self, camera_id: str, start: float, end: float
    ) -> list[sqlite3.Row]:
        """Segments overlapping [start, end), including one that starts before
        `start` but runs into it — otherwise playback would clip the first
        partial minute."""
        return self.query(
            "SELECT * FROM segments WHERE camera_id = ? "
            "AND start_ts < ? AND (start_ts + duration) > ? "
            "ORDER BY start_ts",
            (camera_id, end, start),
        )

    def segment_bounds(self, camera_id: str) -> tuple[float, float] | None:
        row = self.one(
            "SELECT MIN(start_ts) AS lo, MAX(start_ts + duration) AS hi "
            "FROM segments WHERE camera_id = ?",
            (camera_id,),
        )
        if not row or row["lo"] is None:
            return None
        return float(row["lo"]), float(row["hi"])

    def known_paths(self, camera_id: str) -> set[str]:
        return {
            r["path"]
            for r in self.query(
                "SELECT path FROM segments WHERE camera_id = ?", (camera_id,)
            )
        }

    def total_size(self) -> int:
        row = self.one("SELECT COALESCE(SUM(size), 0) AS n FROM segments")
        return int(row["n"]) if row else 0

    def oldest_segments(self, limit: int = 500) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM segments ORDER BY start_ts LIMIT ?", (limit,)
        )

    def segments_older_than(self, cutoff: float, limit: int = 500) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM segments WHERE start_ts < ? ORDER BY start_ts LIMIT ?",
            (cutoff, limit),
        )

    def delete_segment(self, segment_id: int) -> None:
        self.execute("DELETE FROM segments WHERE id = ?", (segment_id,))

    def camera_stats(self, camera_id: str) -> dict[str, Any]:
        row = self.one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes "
            "FROM segments WHERE camera_id = ?",
            (camera_id,),
        )
        bounds = self.segment_bounds(camera_id)
        return {
            "segments": int(row["n"]) if row else 0,
            "bytes": int(row["bytes"]) if row else 0,
            "oldest": bounds[0] if bounds else None,
            "newest": bounds[1] if bounds else None,
        }
