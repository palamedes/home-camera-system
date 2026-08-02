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
    -- 'admin' can change everything and manage users; 'viewer' can only watch.
    role          TEXT    NOT NULL DEFAULT 'admin',
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
    -- 1 if this is a 360/fisheye camera (auto-detected, admin-overridable).
    fisheye       INTEGER NOT NULL DEFAULT 0,
    -- 1 if non-admin "viewer" accounts may see this camera at all.
    viewer_visible INTEGER NOT NULL DEFAULT 1,
    -- 1 if the raw camera tile appears on the dashboard/Cameras grids. A 360
    -- can be hidden here while its virtual cameras still show.
    show_on_grid  INTEGER NOT NULL DEFAULT 1,
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

-- A virtual camera is a fixed dewarp view aimed out of a fisheye parent:
-- an orientation (yaw/pitch/fov) plus a snapshot of the parent's calibration,
-- so it renders identically in any browser. Live-only; dewarping happens
-- client-side from the parent's stream.
CREATE TABLE IF NOT EXISTS virtual_cameras (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id  TEXT    NOT NULL,
    name       TEXT    NOT NULL,
    yaw        REAL    NOT NULL DEFAULT 0,
    pitch      REAL    NOT NULL DEFAULT 0,
    fov        REAL    NOT NULL DEFAULT 1.5708,
    calib      TEXT,
    viewer_visible INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vcam_parent ON virtual_cameras(parent_id);

-- Saved clips: permanent exports kept on the box (never pruned). A clip is
-- tied to the camera it came from (for access control) and optionally the
-- virtual camera it was dewarped through.
CREATE TABLE IF NOT EXISTS clips (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id  TEXT    NOT NULL,
    vcam_id    INTEGER,
    name       TEXT    NOT NULL,
    start_ts   REAL,
    duration   REAL,
    path       TEXT    NOT NULL,
    mime       TEXT,
    size       INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clips_created ON clips(created_at);
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
            "fisheye": "ALTER TABLE cameras ADD COLUMN fisheye INTEGER NOT NULL DEFAULT 0",
            "viewer_visible": "ALTER TABLE cameras ADD COLUMN viewer_visible INTEGER NOT NULL DEFAULT 1",
            "show_on_grid": "ALTER TABLE cameras ADD COLUMN show_on_grid INTEGER NOT NULL DEFAULT 1",
        }
        for column, statement in additions.items():
            if column not in have:
                self.execute(statement)

        vcam_cols = {row["name"] for row in self.query("PRAGMA table_info(virtual_cameras)")}
        if vcam_cols and "viewer_visible" not in vcam_cols:
            self.execute("ALTER TABLE virtual_cameras ADD COLUMN viewer_visible INTEGER NOT NULL DEFAULT 1")

        user_cols = {row["name"] for row in self.query("PRAGMA table_info(users)")}
        if "role" not in user_cols:
            # Existing accounts predate roles; they were the sole operator, so
            # they become admins.
            self.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")

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

    def create_user(self, username: str, password_hash: str, role: str = "admin") -> int:
        cur = self.execute(
            "INSERT INTO users (username, password_hash, role, created_at) "
            "VALUES (?, ?, ?, ?)",
            (username, password_hash, role, int(time.time())),
        )
        return int(cur.lastrowid or 0)

    def user_by_name(self, username: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM users WHERE username = ?", (username,))

    def user_by_id(self, user_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM users WHERE id = ?", (user_id,))

    def users(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM users ORDER BY created_at")

    def admin_count(self) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'")
        return int(row["n"]) if row else 0

    def set_user_role(self, user_id: int, role: str) -> None:
        self.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))

    def set_user_password(self, user_id: int, password_hash: str) -> None:
        self.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)
        )

    def delete_user(self, user_id: int) -> None:
        self.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        self.execute("DELETE FROM users WHERE id = ?", (user_id,))

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
        self.execute("DELETE FROM virtual_cameras WHERE parent_id = ?", (camera_id,))

    # ---- virtual cameras -------------------------------------------------

    def virtual_cameras(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM virtual_cameras ORDER BY name")

    def virtual_camera(self, vid: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM virtual_cameras WHERE id = ?", (vid,))

    def add_virtual_camera(
        self, parent_id: str, name: str, yaw: float, pitch: float,
        fov: float, calib: str,
    ) -> int:
        cur = self.execute(
            "INSERT INTO virtual_cameras "
            "(parent_id, name, yaw, pitch, fov, calib, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (parent_id, name, yaw, pitch, fov, calib, int(time.time())),
        )
        return int(cur.lastrowid or 0)

    def delete_virtual_camera(self, vid: int) -> None:
        self.execute("DELETE FROM virtual_cameras WHERE id = ?", (vid,))

    # ---- clips -----------------------------------------------------------

    def clips(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM clips ORDER BY created_at DESC")

    def clip(self, clip_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM clips WHERE id = ?", (clip_id,))

    def add_clip(
        self, camera_id: str, name: str, path: str, mime: str, size: int,
        vcam_id: int | None = None, start_ts: float | None = None,
        duration: float | None = None,
    ) -> int:
        cur = self.execute(
            "INSERT INTO clips "
            "(camera_id, vcam_id, name, start_ts, duration, path, mime, size, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (camera_id, vcam_id, name, start_ts, duration, path, mime, size,
             int(time.time())),
        )
        return int(cur.lastrowid or 0)

    def delete_clip(self, clip_id: int) -> None:
        self.execute("DELETE FROM clips WHERE id = ?", (clip_id,))

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

    def segments_older_than_for_camera(
        self, camera_id: str, cutoff: float, limit: int = 500
    ) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM segments WHERE camera_id = ? AND start_ts < ? "
            "ORDER BY start_ts LIMIT ?",
            (camera_id, cutoff, limit),
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
