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
    -- Hard maximum age in seconds: footage older than this is deleted no matter
    -- how much free space there is. NULL falls back to the global limit; 0 means
    -- "never delete by age" (keep until space runs out).
    retention_seconds INTEGER,
    -- Rolling minimum in seconds: the recent window that is protected from
    -- space-based pruning (only the free-space safety floor may override it).
    -- NULL/0 means nothing is protected.
    rolling_keep_seconds INTEGER,
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
    -- 1 if this virtual camera's tile appears on the dashboard/Cameras grids.
    show_on_grid   INTEGER NOT NULL DEFAULT 1,
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

-- Time-of-day schedules that drive per-camera actions. Each row is one rule:
-- "on these weekdays, between these minutes-of-day, this action holds". The
-- SchedulerService walks these every minute and applies them. Windows are in
-- server local time; end_min < start_min means the window wraps past midnight.
CREATE TABLE IF NOT EXISTS schedules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Exactly one target is set; the other is NULL.
    camera_id  TEXT,
    device_id  TEXT,
    -- Camera: 'record' | 'light' | 'nightvision'. Device: 'power'.
    action     TEXT    NOT NULL,
    -- 7-bit weekday mask: bit0=Mon .. bit6=Sun.
    days       INTEGER NOT NULL DEFAULT 127,
    -- Minutes past midnight, 0..1439. end_min may be < start_min (wraps).
    start_min  INTEGER NOT NULL,
    end_min    INTEGER NOT NULL,
    -- Action parameter: nightvision mode (auto|color|bw); "on" for light/record.
    value      TEXT    NOT NULL DEFAULT 'on',
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_camera ON schedules(camera_id);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Camera the event belongs to, or a synthetic id like 'river' for
    -- non-camera sources (flood alerts). NULL is allowed for the same reason.
    camera_id  TEXT,
    ts         REAL    NOT NULL,
    -- 'person' | 'vehicle' | 'animal' | 'motion' | 'flood' ...
    type       TEXT    NOT NULL,
    label      TEXT    NOT NULL DEFAULT '',
    score      REAL,
    -- Free-form JSON for source-specific detail (stage in ft, category, ...).
    meta       TEXT    NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_camera_ts ON events(camera_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

-- App-level settings the UI can edit at runtime (weather, alerts, ...), stored
-- one JSON blob per section. These override the matching config.yaml section on
-- startup; infrastructure settings (server, go2rtc, paths) stay in the file.
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Calendars. A household calendar: each person can keep their own, and there is
-- at least one shared calendar everybody sees and can add to (owner_user_id
-- NULL). The iCloud columns are unused until CalDAV sync lands, but they live
-- here now so turning sync on is not a schema migration.
CREATE TABLE IF NOT EXISTS calendars (
    id            TEXT    PRIMARY KEY,
    name          TEXT    NOT NULL,
    color         TEXT    NOT NULL DEFAULT '#2563eb',
    -- NULL = shared with the whole household.
    owner_user_id INTEGER,
    -- 'local' | 'icloud'
    source        TEXT    NOT NULL DEFAULT 'local',
    remote_id     TEXT,
    sync_token    TEXT,
    last_sync_utc REAL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    calendar_id TEXT    NOT NULL,
    -- iCalendar UID; generated locally, preserved for anything from iCloud so
    -- the same event is never duplicated on a re-sync.
    uid         TEXT    NOT NULL UNIQUE,
    title       TEXT    NOT NULL,
    description TEXT,
    location    TEXT,
    -- Epoch seconds, UTC. All-day events sit at local midnight and set all_day.
    start_utc   REAL    NOT NULL,
    end_utc     REAL    NOT NULL,
    all_day     INTEGER NOT NULL DEFAULT 0,
    -- Raw RRULE, kept verbatim for round-tripping. Not expanded yet.
    rrule       TEXT,
    tzid        TEXT,
    source      TEXT    NOT NULL DEFAULT 'local',
    etag        TEXT,
    href        TEXT,
    created_by  INTEGER,
    updated_utc REAL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calendar_events_window
    ON calendar_events (start_utc, end_utc);

-- Non-camera devices Sentry can control over plain HTTP on the LAN: relays and
-- smart switches (Shelly first, but the driver is just a name here). Kept
-- deliberately generic — a driver knows how to build the on/off/toggle request
-- for its kind, everything else is common.
CREATE TABLE IF NOT EXISTS devices (
    id          TEXT    PRIMARY KEY,
    name        TEXT    NOT NULL,
    -- Driver key, e.g. 'shelly' | 'http'. Decides how requests are built.
    driver      TEXT    NOT NULL DEFAULT 'shelly',
    host        TEXT    NOT NULL,
    -- Which output/relay on a multi-channel device.
    channel     INTEGER NOT NULL DEFAULT 0,
    username    TEXT,
    password    TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    -- Last known on/off state and when we last reached it, for the UI.
    last_state  INTEGER,
    last_seen   REAL,
    last_error  TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);

-- Rooms. Purely an organising layer for window coverings today; the eventual
-- top-down floorplan view hangs off this table too, which is why the geometry
-- columns exist now rather than as a later migration.
CREATE TABLE IF NOT EXISTS rooms (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    -- Reserved for the floorplan: a stored image and this room's outline on it.
    plan_image TEXT,
    plan_shape TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

-- Connector / Motionblinds bridges. One box drives every motor paired to it
-- over 433 MHz; Sentry talks to the box over UDP on the LAN.
CREATE TABLE IF NOT EXISTS shade_hubs (
    id         TEXT    PRIMARY KEY,   -- the hub's MAC, as the protocol reports it
    name       TEXT    NOT NULL,
    host       TEXT    NOT NULL,
    -- 16-character key from the vendor app, dashes included. NULL means
    -- read-only: discovery and polling still work, writes may be refused.
    api_key    TEXT,
    -- Last token the hub handed out. Half of the AccessToken handshake, and it
    -- changes when the hub restarts, so it is refreshed rather than trusted.
    token      TEXT,
    protocol   TEXT,
    enabled    INTEGER NOT NULL DEFAULT 1,
    last_seen  REAL,
    last_error TEXT,
    created_at INTEGER NOT NULL
);

-- Window coverings: one motorised shade or blind.
CREATE TABLE IF NOT EXISTS coverings (
    id            TEXT    PRIMARY KEY,  -- device MAC from the hub
    hub_id        TEXT    NOT NULL,
    room_id       INTEGER,
    name          TEXT    NOT NULL,
    -- The layer this covering forms on the window. A dual-roller window has
    -- one of each: 'sheer' is light-filtering (you can see through it),
    -- 'blackout' is room-darkening (you cannot). Grouping actions key off this.
    layer         TEXT    NOT NULL DEFAULT 'sheer',
    -- 'shade' (rolls up) | 'blind' (slatted, tilts) | 'curtain'. Only affects
    -- wording and whether a tilt control is offered.
    kind          TEXT    NOT NULL DEFAULT 'shade',
    device_type   TEXT    NOT NULL DEFAULT '10000000',
    -- Whether the motor reports its real position, or can only be commanded.
    bidirectional INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    -- Cached telemetry so the page renders instantly instead of waiting on RF.
    -- Position is the protocol's own scale: 0 open, 100 closed.
    last_position INTEGER,
    battery_mv    INTEGER,
    rssi          INTEGER,
    last_seen     REAL,
    last_error    TEXT,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coverings_room ON coverings(room_id);
CREATE INDEX IF NOT EXISTS idx_coverings_hub ON coverings(hub_id);

-- Task lists. A list is the *thing* the work belongs to — the house, the boat,
-- the car — not a workflow stage. It is the board's columns, deliberately: for
-- a household, "which thing is this about" sorts the work usefully, whereas
-- To-do/Doing/Done mostly creates a column nobody moves cards out of.
CREATE TABLE IF NOT EXISTS task_lists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    color      TEXT    NOT NULL DEFAULT '#2563eb',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    -- NULL when its list was deleted: the work outlives the category.
    list_id      INTEGER,
    title        TEXT    NOT NULL,
    notes        TEXT,
    -- NULL means nobody in particular has it yet.
    assignee_id  INTEGER,
    -- Epoch seconds, UTC. NULL means no due date, and it stays off the
    -- calendar. Due tasks are surfaced as all-day calendar entries.
    due_utc      REAL,
    done         INTEGER NOT NULL DEFAULT 0,
    done_utc     REAL,
    created_by   INTEGER,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_list ON tasks(list_id);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_utc);

-- What is on the LAN, as annotated by a person. Keyed by MAC because that is
-- the stable identity: an address changes with the DHCP lease, and the whole
-- point is that "this is the dishwasher" survives that.
--
-- Rows are created by the network scan and outlive it, so a device that is
-- switched off keeps its label — and first_seen means a genuinely new arrival
-- on the network is visible as new, which is worth knowing in a house that
-- runs a security system.
CREATE TABLE IF NOT EXISTS lan_devices (
    mac          TEXT    PRIMARY KEY,
    label        TEXT,
    -- One of nvr.netscan.DEVICE_KINDS; 'unknown' until somebody says.
    kind         TEXT    NOT NULL DEFAULT 'unknown',
    notes        TEXT,
    last_address TEXT,
    first_seen   REAL,
    last_seen    REAL,
    -- Ignore this one in the "unidentified" count without labelling it.
    dismissed    INTEGER NOT NULL DEFAULT 0,
    -- Present in the very first scan, so it is part of the existing picture of
    -- the network rather than an arrival. Without this every device reads as
    -- "new" on first run, which makes the flag worthless exactly when somebody
    -- is first looking at the list.
    baseline     INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);

-- Automations: bind something happening to something being done. Sentry knows
-- things nothing else on the LAN knows; this is how it acts on them without a
-- separate home-automation stack.
CREATE TABLE IF NOT EXISTS automations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL,
    -- URL-safe id. Every automation is reachable at /api/hook/run/<slug>
    -- regardless of its trigger, which is the generic "poke Sentry" endpoint.
    slug             TEXT    NOT NULL UNIQUE,
    enabled          INTEGER NOT NULL DEFAULT 1,
    -- 'event' fires from a detection Sentry raised; 'hook' only from its URL.
    trigger_kind     TEXT    NOT NULL DEFAULT 'hook',
    -- JSON event pattern, e.g. {"event_type":"person","camera_id":"driveway"}.
    -- Absent keys match anything.
    match            TEXT    NOT NULL DEFAULT '{}',
    -- JSON list of actions: device / covering / webhook.
    actions          TEXT    NOT NULL DEFAULT '[]',
    -- A person loitering raises an event every poll; without this the porch
    -- light would be commanded dozens of times a minute.
    cooldown_seconds INTEGER NOT NULL DEFAULT 60,
    -- Optional window, so "porch light on" can mean "after dark". NULL times
    -- mean the trigger alone decides.
    days             INTEGER NOT NULL DEFAULT 127,
    start_min        INTEGER,
    end_min          INTEGER,
    last_run         REAL,
    last_error       TEXT,
    run_count        INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL
);
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
            "rolling_keep_seconds": "ALTER TABLE cameras ADD COLUMN rolling_keep_seconds INTEGER",
            "fisheye": "ALTER TABLE cameras ADD COLUMN fisheye INTEGER NOT NULL DEFAULT 0",
            "viewer_visible": "ALTER TABLE cameras ADD COLUMN viewer_visible INTEGER NOT NULL DEFAULT 1",
            "show_on_grid": "ALTER TABLE cameras ADD COLUMN show_on_grid INTEGER NOT NULL DEFAULT 1",
            # Pin a camera's recordings to one pool volume (its path); NULL = follow
            # the normal pool overflow.
            "preferred_volume": "ALTER TABLE cameras ADD COLUMN preferred_volume TEXT",
            # Soft-delete: an archived camera is removed from live views and never
            # recorded, but its row + footage index survive so its history stays
            # viewable (and retention keeps managing the footage).
            "archived": "ALTER TABLE cameras ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
        }
        for column, statement in additions.items():
            if column not in have:
                self.execute(statement)

        # Manual grid/wall ordering. Backfill existing rows in their current
        # (name) order so the first render looks identical to before.
        if "sort_order" not in have:
            self.execute(
                "ALTER TABLE cameras ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
            )
            for i, row in enumerate(self.query("SELECT id FROM cameras ORDER BY name")):
                self.execute(
                    "UPDATE cameras SET sort_order = ? WHERE id = ?", (i, row["id"])
                )

        vcam_cols = {row["name"] for row in self.query("PRAGMA table_info(virtual_cameras)")}
        if vcam_cols and "viewer_visible" not in vcam_cols:
            self.execute("ALTER TABLE virtual_cameras ADD COLUMN viewer_visible INTEGER NOT NULL DEFAULT 1")
        if vcam_cols and "show_on_grid" not in vcam_cols:
            self.execute("ALTER TABLE virtual_cameras ADD COLUMN show_on_grid INTEGER NOT NULL DEFAULT 1")
        # 'fisheye' (yaw/pitch/fov dewarp) or 'crop' (a rectangular sub-region of
        # a normal camera, stored as a normalised rect in `calib`).
        if vcam_cols and "mode" not in vcam_cols:
            self.execute(
                "ALTER TABLE virtual_cameras ADD COLUMN mode TEXT NOT NULL DEFAULT 'fisheye'"
            )
        # Virtuals share the cameras' sort_order space so the grid can interleave
        # them. Backfill so existing virtuals trail after the cameras (the old
        # "cameras first, then virtuals" layout) until the user reorders.
        if vcam_cols and "sort_order" not in vcam_cols:
            self.execute(
                "ALTER TABLE virtual_cameras ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
            )
            base_row = self.one("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM cameras")
            base = base_row["n"] if base_row else 0
            for i, row in enumerate(
                self.query("SELECT id FROM virtual_cameras ORDER BY name")
            ):
                self.execute(
                    "UPDATE virtual_cameras SET sort_order = ? WHERE id = ?",
                    (base + i, row["id"]),
                )

        # "Show on grid" used to mean both "on the Cameras page and wall" and
        # "on the dashboard". Splitting them needs a backfill, not just a
        # DEFAULT: a plain default of 1 would put every deliberately hidden
        # camera straight back onto the dashboard.
        if "show_on_dashboard" not in have:
            self.execute(
                "ALTER TABLE cameras ADD COLUMN show_on_dashboard "
                "INTEGER NOT NULL DEFAULT 1"
            )
            self.execute("UPDATE cameras SET show_on_dashboard = show_on_grid")
        if vcam_cols and "show_on_dashboard" not in vcam_cols:
            self.execute(
                "ALTER TABLE virtual_cameras ADD COLUMN show_on_dashboard "
                "INTEGER NOT NULL DEFAULT 1"
            )
            self.execute(
                "UPDATE virtual_cameras SET show_on_dashboard = show_on_grid"
            )

        # Schedules gained device targets, which also means camera_id had to
        # stop being NOT NULL. SQLite cannot relax a column constraint in place,
        # so the table is rebuilt — in ONE transaction via executescript, because
        # a half-applied rebuild would lose every schedule.
        sched_cols = {row["name"] for row in self.query("PRAGMA table_info(schedules)")}
        if sched_cols and "device_id" not in sched_cols:
            conn = self.connect()
            with self._write_lock:
                conn.executescript(
                    """
                    BEGIN;
                    CREATE TABLE schedules_new (
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        camera_id  TEXT,
                        device_id  TEXT,
                        action     TEXT    NOT NULL,
                        days       INTEGER NOT NULL DEFAULT 127,
                        start_min  INTEGER NOT NULL,
                        end_min    INTEGER NOT NULL,
                        value      TEXT    NOT NULL DEFAULT 'on',
                        enabled    INTEGER NOT NULL DEFAULT 1,
                        created_at INTEGER NOT NULL
                    );
                    INSERT INTO schedules_new
                        (id, camera_id, action, days, start_min, end_min,
                         value, enabled, created_at)
                    SELECT id, camera_id, action, days, start_min, end_min,
                           value, enabled, created_at FROM schedules;
                    DROP TABLE schedules;
                    ALTER TABLE schedules_new RENAME TO schedules;
                    COMMIT;
                    """
                )

        # Window-covering targets. A covering schedule either names one covering
        # or describes a group ("the blackouts in the bedroom"), so the selector
        # columns are nullable and read as "any".
        sched_cols = {row["name"] for row in self.query("PRAGMA table_info(schedules)")}
        covering_additions = {
            "covering_id": "ALTER TABLE schedules ADD COLUMN covering_id TEXT",
            # NULL room = every room; NULL layer = both layers.
            "covering_room_id": "ALTER TABLE schedules ADD COLUMN covering_room_id INTEGER",
            "covering_layer": "ALTER TABLE schedules ADD COLUMN covering_layer TEXT",
        }
        for column, statement in covering_additions.items():
            if sched_cols and column not in sched_cols:
                self.execute(statement)

        # The MAC is the stable identity; the address is just where it was
        # last seen. Learned on first successful contact, and used to re-find a
        # device after a DHCP lease change — which otherwise breaks the
        # integration silently, with "it stopped working" as the only symptom.
        for table in ("devices", "shade_hubs", "cameras"):
            cols = {row["name"] for row in self.query(f"PRAGMA table_info({table})")}
            if cols and "mac" not in cols:
                self.execute(f"ALTER TABLE {table} ADD COLUMN mac TEXT")

        lan_cols = {row["name"] for row in self.query("PRAGMA table_info(lan_devices)")}
        if lan_cols and "baseline" not in lan_cols:
            self.execute(
                "ALTER TABLE lan_devices ADD COLUMN baseline INTEGER NOT NULL DEFAULT 0"
            )
            # Everything already recorded predates the idea of a baseline, so
            # it IS the baseline — otherwise the upgrade would announce the
            # whole house as newly arrived.
            self.execute("UPDATE lan_devices SET baseline = 1")

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
            try:
                cur = conn.execute(sql, tuple(params))
                conn.commit()
                return cur
            except BaseException:
                # Roll back, or this thread's cached connection stays inside an
                # open write transaction holding the WAL writer lock — for the
                # life of the process. Every other writer (segment indexer,
                # retention, login) would then block for the full busy timeout
                # and fail with "database is locked", each swallowed by a
                # background thread's except-and-retry, so recording and pruning
                # would quietly stop with nothing but slow log noise.
                conn.rollback()
                raise

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
        # Their tasks survive, unassigned: somebody still has to do the thing,
        # and jobs vanishing with an account would be a nasty surprise.
        self.execute(
            "UPDATE tasks SET assignee_id = NULL WHERE assignee_id = ?", (user_id,)
        )
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

    def cameras(
        self, enabled_only: bool = False, include_archived: bool = False
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM cameras"
        clauses = []
        if enabled_only:
            clauses.append("enabled = 1")
        if not include_archived:
            clauses.append("archived = 0")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return self.query(sql + " ORDER BY sort_order, name")

    def archived_cameras(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM cameras WHERE archived = 1 ORDER BY name"
        )

    def set_camera_archived(self, camera_id: str, archived: bool) -> None:
        """Soft-delete / restore. Leaves `enabled` untouched so restoring resumes
        the camera's prior state; while archived it's excluded from cameras() so
        it isn't streamed or recorded regardless."""
        self.execute(
            "UPDATE cameras SET archived = ? WHERE id = ?",
            (1 if archived else 0, camera_id),
        )

    def camera(self, camera_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM cameras WHERE id = ?", (camera_id,))

    def add_camera(self, **fields: Any) -> None:
        fields.setdefault("created_at", int(time.time()))
        if "sort_order" not in fields:
            # New cameras land at the end of the grid, not the top. Cameras and
            # virtuals share one order space, so take the max across both or a
            # new camera collides with an existing virtual and lands mid-grid.
            row = self.one(
                "SELECT COALESCE(MAX(m), -1) + 1 AS n FROM ("
                "SELECT MAX(sort_order) AS m FROM cameras "
                "UNION ALL SELECT MAX(sort_order) FROM virtual_cameras)"
            )
            fields["sort_order"] = row["n"] if row else 0
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
        self.execute("DELETE FROM schedules WHERE camera_id = ?", (camera_id,))

    def set_camera_sort(self, pairs: list[tuple[int, str]]) -> None:
        """Apply explicit (sort_order, camera_id) pairs. Cameras and virtuals
        share one order space so the grid can interleave them."""
        for order, camera_id in pairs:
            self.execute(
                "UPDATE cameras SET sort_order = ? WHERE id = ?", (order, camera_id)
            )

    def set_virtual_sort(self, pairs: list[tuple[int, int]]) -> None:
        """Apply explicit (sort_order, virtual_id) pairs (shared with cameras)."""
        for order, vid in pairs:
            self.execute(
                "UPDATE virtual_cameras SET sort_order = ? WHERE id = ?", (order, vid)
            )

    # ---- virtual cameras -------------------------------------------------

    def virtual_cameras(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM virtual_cameras ORDER BY sort_order, name")

    def virtual_camera(self, vid: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM virtual_cameras WHERE id = ?", (vid,))

    def add_virtual_camera(
        self, parent_id: str, name: str, yaw: float, pitch: float,
        fov: float, calib: str, mode: str = "fisheye",
    ) -> int:
        # Append to the end of the shared grid order (after every camera + vcam).
        row = self.one(
            "SELECT MAX(m) AS n FROM ("
            "SELECT MAX(sort_order) AS m FROM cameras "
            "UNION ALL SELECT MAX(sort_order) FROM virtual_cameras)"
        )
        sort_order = ((row["n"] if row and row["n"] is not None else -1) + 1)
        cur = self.execute(
            "INSERT INTO virtual_cameras "
            "(parent_id, name, yaw, pitch, fov, calib, created_at, sort_order, mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (parent_id, name, yaw, pitch, fov, calib, int(time.time()), sort_order, mode),
        )
        return int(cur.lastrowid or 0)

    def delete_virtual_camera(self, vid: int) -> None:
        self.execute("DELETE FROM virtual_cameras WHERE id = ?", (vid,))

    # ---- calendars --------------------------------------------------------

    def calendars(self, user_id: int | None = None) -> list[sqlite3.Row]:
        """Calendars a user may see: the shared household ones plus their own.

        Passing None returns every calendar (for admin listings and the sync
        loop); it is never used to answer a browser request directly.
        """
        if user_id is None:
            return self.query("SELECT * FROM calendars ORDER BY sort_order, name")
        return self.query(
            "SELECT * FROM calendars WHERE owner_user_id IS NULL OR owner_user_id = ? "
            "ORDER BY sort_order, name",
            (user_id,),
        )

    def calendar(self, calendar_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM calendars WHERE id = ?", (calendar_id,))

    def add_calendar(self, **fields: Any) -> None:
        fields.setdefault("created_at", int(time.time()))
        if "sort_order" not in fields:
            row = self.one("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM calendars")
            fields["sort_order"] = row["n"] if row else 0
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        self.execute(
            f"INSERT INTO calendars ({cols}) VALUES ({marks})", tuple(fields.values())
        )

    def update_calendar(self, calendar_id: str, **fields: Any) -> None:
        if not fields:
            return
        assigns = ", ".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE calendars SET {assigns} WHERE id = ?",
            (*fields.values(), calendar_id),
        )

    def delete_calendar(self, calendar_id: str) -> None:
        self.execute("DELETE FROM calendars WHERE id = ?", (calendar_id,))
        # Its events go with it, or they become invisible orphans no UI can reach.
        self.execute("DELETE FROM calendar_events WHERE calendar_id = ?", (calendar_id,))

    # ---- calendar events --------------------------------------------------

    def calendar_events(
        self, start_utc: float, end_utc: float, calendar_ids: list[str] | None = None
    ) -> list[sqlite3.Row]:
        """Events overlapping [start, end). An event counts if any part of it
        falls in the window, so a multi-day trip shows on every day it covers."""
        sql = (
            "SELECT * FROM calendar_events "
            "WHERE start_utc < ? AND end_utc > ?"
        )
        params: list[Any] = [end_utc, start_utc]
        if calendar_ids is not None:
            if not calendar_ids:
                return []
            marks = ", ".join("?" for _ in calendar_ids)
            sql += f" AND calendar_id IN ({marks})"
            params.extend(calendar_ids)
        return self.query(sql + " ORDER BY start_utc", tuple(params))

    def calendar_event(self, event_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM calendar_events WHERE id = ?", (event_id,))

    def add_calendar_event(self, **fields: Any) -> int:
        fields.setdefault("created_at", int(time.time()))
        fields.setdefault("updated_utc", time.time())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.execute(
            f"INSERT INTO calendar_events ({cols}) VALUES ({marks})",
            tuple(fields.values()),
        )
        return int(cur.lastrowid or 0)

    def update_calendar_event(self, event_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields.setdefault("updated_utc", time.time())
        assigns = ", ".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE calendar_events SET {assigns} WHERE id = ?",
            (*fields.values(), event_id),
        )

    def delete_calendar_event(self, event_id: int) -> None:
        self.execute("DELETE FROM calendar_events WHERE id = ?", (event_id,))

    # ---- devices (relays / smart switches) --------------------------------

    def devices(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM devices"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return self.query(sql + " ORDER BY sort_order, name")

    def device(self, device_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM devices WHERE id = ?", (device_id,))

    def add_device(self, **fields: Any) -> None:
        fields.setdefault("created_at", int(time.time()))
        if "sort_order" not in fields:
            row = self.one("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM devices")
            fields["sort_order"] = row["n"] if row else 0
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        self.execute(
            f"INSERT INTO devices ({cols}) VALUES ({marks})", tuple(fields.values())
        )

    def update_device(self, device_id: str, **fields: Any) -> None:
        if not fields:
            return
        assigns = ", ".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE devices SET {assigns} WHERE id = ?", (*fields.values(), device_id)
        )

    def delete_device(self, device_id: str) -> None:
        self.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        # Cascade, or its schedules linger forever pointing at nothing —
        # invisible in the UI and impossible to remove. Same trap that
        # delete_camera fell into.
        self.execute("DELETE FROM schedules WHERE device_id = ?", (device_id,))

    # ---- rooms -----------------------------------------------------------

    def rooms(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM rooms ORDER BY sort_order, name")

    def room(self, room_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM rooms WHERE id = ?", (room_id,))

    def add_room(self, name: str, **fields: Any) -> int:
        fields.setdefault("created_at", int(time.time()))
        if "sort_order" not in fields:
            row = self.one("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM rooms")
            fields["sort_order"] = row["n"] if row else 0
        cols = ", ".join(("name", *fields))
        marks = ", ".join("?" for _ in range(len(fields) + 1))
        cur = self.execute(
            f"INSERT INTO rooms ({cols}) VALUES ({marks})",
            (name, *fields.values()),
        )
        return int(cur.lastrowid or 0)

    def update_room(self, room_id: int, **fields: Any) -> None:
        if not fields:
            return
        assigns = ", ".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE rooms SET {assigns} WHERE id = ?", (*fields.values(), room_id)
        )

    def delete_room(self, room_id: int) -> None:
        """Deleting a room keeps its coverings — they become unassigned.

        Losing a room must never lose the hardware behind it; an orphaned
        covering is visible under "Unassigned" and can be re-homed, whereas a
        cascaded one would have to be rediscovered from the hub.
        """
        self.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
        self.execute("UPDATE coverings SET room_id = NULL WHERE room_id = ?", (room_id,))
        self.execute(
            "DELETE FROM schedules WHERE covering_room_id = ?", (room_id,)
        )

    def set_room_order(self, ordered_ids: list[int]) -> None:
        for index, room_id in enumerate(ordered_ids):
            self.execute(
                "UPDATE rooms SET sort_order = ? WHERE id = ?", (index, room_id)
            )

    # ---- shade hubs ------------------------------------------------------

    def shade_hubs(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM shade_hubs"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return self.query(sql + " ORDER BY name")

    def shade_hub(self, hub_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM shade_hubs WHERE id = ?", (hub_id,))

    def add_shade_hub(self, **fields: Any) -> None:
        fields.setdefault("created_at", int(time.time()))
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        self.execute(
            f"INSERT INTO shade_hubs ({cols}) VALUES ({marks})", tuple(fields.values())
        )

    def update_shade_hub(self, hub_id: str, **fields: Any) -> None:
        if not fields:
            return
        assigns = ", ".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE shade_hubs SET {assigns} WHERE id = ?", (*fields.values(), hub_id)
        )

    def delete_shade_hub(self, hub_id: str) -> None:
        """Removing a hub removes the coverings behind it, and their schedules.

        Unlike a room, a covering cannot outlive its hub — there is no other
        way to reach the motor. Same cascade trap as delete_camera.
        """
        for covering in self.coverings(hub_id=hub_id):
            self.execute(
                "DELETE FROM schedules WHERE covering_id = ?", (covering["id"],)
            )
        self.execute("DELETE FROM coverings WHERE hub_id = ?", (hub_id,))
        self.execute("DELETE FROM shade_hubs WHERE id = ?", (hub_id,))

    # ---- coverings (shades / blinds) -------------------------------------

    def coverings(self, hub_id: str | None = None, room_id: int | None = None,
                  enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM coverings"
        clauses, params = [], []
        if hub_id is not None:
            clauses.append("hub_id = ?")
            params.append(hub_id)
        if room_id is not None:
            clauses.append("room_id = ?")
            params.append(room_id)
        if enabled_only:
            clauses.append("enabled = 1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return self.query(sql + " ORDER BY sort_order, name", params)

    def covering(self, covering_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM coverings WHERE id = ?", (covering_id,))

    def add_covering(self, **fields: Any) -> None:
        fields.setdefault("created_at", int(time.time()))
        if "sort_order" not in fields:
            row = self.one("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM coverings")
            fields["sort_order"] = row["n"] if row else 0
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        self.execute(
            f"INSERT INTO coverings ({cols}) VALUES ({marks})", tuple(fields.values())
        )

    def update_covering(self, covering_id: str, **fields: Any) -> None:
        if not fields:
            return
        assigns = ", ".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE coverings SET {assigns} WHERE id = ?",
            (*fields.values(), covering_id),
        )

    def delete_covering(self, covering_id: str) -> None:
        self.execute("DELETE FROM coverings WHERE id = ?", (covering_id,))
        self.execute("DELETE FROM schedules WHERE covering_id = ?", (covering_id,))

    def set_covering_order(self, ordered_ids: list[str]) -> None:
        for index, covering_id in enumerate(ordered_ids):
            self.execute(
                "UPDATE coverings SET sort_order = ? WHERE id = ?", (index, covering_id)
            )

    def schedules_for_covering(self, covering_id: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM schedules WHERE covering_id = ? ORDER BY start_min",
            (covering_id,),
        )

    def covering_schedules(self) -> list[sqlite3.Row]:
        """Every covering schedule, single-target and group alike."""
        return self.query(
            "SELECT * FROM schedules WHERE action = 'cover' ORDER BY start_min"
        )

    # ---- LAN devices (the annotated network inventory) --------------------

    def lan_devices(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM lan_devices ORDER BY last_seen DESC")

    def lan_device(self, mac: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM lan_devices WHERE mac = ?", (mac,))

    def seen_lan_device(self, mac: str, address: str, when: float) -> None:
        """Record a sighting, creating the row on first contact.

        first_seen is set once and never updated, so a device that appears on
        the network for the first time can be shown as new.
        """
        self.execute(
            "INSERT INTO lan_devices (mac, last_address, first_seen, last_seen, "
            "created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(mac) DO UPDATE SET last_address = excluded.last_address, "
            "last_seen = excluded.last_seen",
            (mac, address, when, when, int(when)),
        )

    def mark_lan_baseline(self) -> None:
        """Treat everything currently recorded as the existing network."""
        self.execute("UPDATE lan_devices SET baseline = 1")

    def update_lan_device(self, mac: str, **fields: Any) -> None:
        if not fields:
            return
        assigns = ", ".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE lan_devices SET {assigns} WHERE mac = ?",
            (*fields.values(), mac),
        )

    def forget_lan_device(self, mac: str) -> None:
        self.execute("DELETE FROM lan_devices WHERE mac = ?", (mac,))

    # ---- automations -----------------------------------------------------

    def automations(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM automations"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return self.query(sql + " ORDER BY name")

    def automation(self, automation_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM automations WHERE id = ?", (automation_id,))

    def automation_by_slug(self, slug: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM automations WHERE slug = ?", (slug,))

    def add_automation(self, **fields: Any) -> int:
        fields.setdefault("created_at", int(time.time()))
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.execute(
            f"INSERT INTO automations ({cols}) VALUES ({marks})",
            tuple(fields.values()),
        )
        return int(cur.lastrowid or 0)

    def update_automation(self, automation_id: int, **fields: Any) -> None:
        if not fields:
            return
        assigns = ", ".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE automations SET {assigns} WHERE id = ?",
            (*fields.values(), automation_id),
        )

    def delete_automation(self, automation_id: int) -> None:
        self.execute("DELETE FROM automations WHERE id = ?", (automation_id,))

    # ---- task lists and tasks --------------------------------------------

    def task_lists(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM task_lists ORDER BY sort_order, name")

    def task_list(self, list_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM task_lists WHERE id = ?", (list_id,))

    def add_task_list(self, name: str, **fields: Any) -> int:
        fields.setdefault("created_at", int(time.time()))
        if "sort_order" not in fields:
            row = self.one("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM task_lists")
            fields["sort_order"] = row["n"] if row else 0
        cols = ", ".join(("name", *fields))
        marks = ", ".join("?" for _ in range(len(fields) + 1))
        cur = self.execute(
            f"INSERT INTO task_lists ({cols}) VALUES ({marks})",
            (name, *fields.values()),
        )
        return int(cur.lastrowid or 0)

    def update_task_list(self, list_id: int, **fields: Any) -> None:
        if not fields:
            return
        assigns = ", ".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE task_lists SET {assigns} WHERE id = ?",
            (*fields.values(), list_id),
        )

    def delete_task_list(self, list_id: int) -> None:
        """Deleting a list keeps its tasks — they become uncategorised.

        Same reasoning as rooms: the category is a label, the work is the
        thing. Cascading would quietly delete somebody's jobs because a
        heading was tidied up.
        """
        self.execute("DELETE FROM task_lists WHERE id = ?", (list_id,))
        self.execute("UPDATE tasks SET list_id = NULL WHERE list_id = ?", (list_id,))

    def set_task_list_order(self, ordered_ids: list[int]) -> None:
        for index, list_id in enumerate(ordered_ids):
            self.execute(
                "UPDATE task_lists SET sort_order = ? WHERE id = ?", (index, list_id)
            )

    def tasks(self, include_done: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM tasks"
        if not include_done:
            sql += " WHERE done = 0"
        # Undone first, then soonest due (nulls last), then manual order.
        return self.query(
            sql + " ORDER BY done, due_utc IS NULL, due_utc, sort_order, id"
        )

    def task(self, task_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def tasks_due_between(self, start: float, end: float) -> list[sqlite3.Row]:
        """Tasks with a due date inside a window, for the calendar.

        Completed tasks stay out: the calendar answers "what is coming up",
        and a finished chore is not.
        """
        return self.query(
            "SELECT * FROM tasks WHERE due_utc IS NOT NULL AND done = 0 "
            "AND due_utc >= ? AND due_utc < ? ORDER BY due_utc",
            (start, end),
        )

    def add_task(self, title: str, **fields: Any) -> int:
        fields.setdefault("created_at", int(time.time()))
        if "sort_order" not in fields:
            row = self.one("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM tasks")
            fields["sort_order"] = row["n"] if row else 0
        cols = ", ".join(("title", *fields))
        marks = ", ".join("?" for _ in range(len(fields) + 1))
        cur = self.execute(
            f"INSERT INTO tasks ({cols}) VALUES ({marks})", (title, *fields.values())
        )
        return int(cur.lastrowid or 0)

    def update_task(self, task_id: int, **fields: Any) -> None:
        if not fields:
            return
        assigns = ", ".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE tasks SET {assigns} WHERE id = ?", (*fields.values(), task_id)
        )

    def delete_task(self, task_id: int) -> None:
        self.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def set_task_order(self, ordered_ids: list[int]) -> None:
        for index, task_id in enumerate(ordered_ids):
            self.execute(
                "UPDATE tasks SET sort_order = ? WHERE id = ?", (index, task_id)
            )

    def unassign_tasks_for_user(self, user_id: int) -> None:
        """A deleted user's tasks stay, unassigned. Somebody still has to do
        the thing, and losing the list with the account would be a surprise."""
        self.execute(
            "UPDATE tasks SET assignee_id = NULL WHERE assignee_id = ?", (user_id,)
        )

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

    def update_clip_path(self, clip_id: int, path: str) -> None:
        self.execute("UPDATE clips SET path = ? WHERE id = ?", (path, clip_id))

    # ---- schedules -------------------------------------------------------

    def schedules(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM schedules ORDER BY camera_id, start_min")

    def schedules_for(self, camera_id: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM schedules WHERE camera_id = ? ORDER BY start_min",
            (camera_id,),
        )

    def schedules_for_device(self, device_id: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM schedules WHERE device_id = ? ORDER BY start_min",
            (device_id,),
        )

    def add_schedule(
        self, camera_id: str | None = None, action: str = "record", days: int = 127,
        start_min: int = 0, end_min: int = 0, value: str = "on", enabled: int = 1,
        device_id: str | None = None, covering_id: str | None = None,
        covering_room_id: int | None = None, covering_layer: str | None = None,
    ) -> int:
        """A schedule targets exactly one kind of thing: a camera, a device, or
        window coverings.

        Coverings are the one target that can be a *selector* instead of an id —
        "the blackouts in the bedroom", or with both selector fields NULL,
        "every covering in the house". So a covering schedule is identified by
        its action rather than by a non-NULL id, which is why the check below
        is not simply "exactly one id is set".
        """
        is_cover = action == "cover"
        if sum((bool(camera_id), bool(device_id), is_cover)) != 1:
            raise ValueError(
                "a schedule needs exactly one target: camera_id, device_id, "
                "or action='cover'"
            )
        if not is_cover and (covering_id or covering_room_id or covering_layer):
            raise ValueError("covering fields require action='cover'")
        if is_cover and covering_id and (covering_room_id or covering_layer):
            raise ValueError(
                "a covering schedule names one covering or a group, not both"
            )
        cur = self.execute(
            "INSERT INTO schedules "
            "(camera_id, device_id, covering_id, covering_room_id, covering_layer, "
            " action, days, start_min, end_min, value, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (camera_id, device_id, covering_id, covering_room_id, covering_layer,
             action, days, start_min, end_min, value, enabled, int(time.time())),
        )
        return int(cur.lastrowid or 0)

    def delete_schedule(self, schedule_id: int) -> None:
        self.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))

    def set_schedule_enabled(self, schedule_id: int, on: bool) -> None:
        self.execute(
            "UPDATE schedules SET enabled = ? WHERE id = ?",
            (1 if on else 0, schedule_id),
        )

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

    def all_segments(self) -> list[sqlite3.Row]:
        return self.query("SELECT id, camera_id, path, size FROM segments")

    @staticmethod
    def _under_pattern(prefix: str) -> str:
        """A LIKE pattern matching paths inside directory `prefix`.

        Anchored to a directory boundary: a bare `prefix + '%'` also matches
        sibling volumes that merely share the prefix ('/mnt/nvr' matching
        '/mnt/nvr2/...'), so pruning the volume that's low on space could delete
        another drive's footage and free nothing. LIKE's own wildcards are
        escaped too — '_' matches any character, and it's common in real paths
        ('/mnt/sentry_data').
        """
        escaped = prefix.rstrip("/").replace("\\", "\\\\")
        escaped = escaped.replace("%", "\\%").replace("_", "\\_")
        return escaped + "/%"

    def recorded_bytes_under(self, prefix: str) -> int:
        """Total recorded bytes whose path is under `prefix` — i.e. the space one
        storage volume is using."""
        row = self.one(
            "SELECT COALESCE(SUM(size), 0) AS n FROM segments "
            "WHERE path LIKE ? ESCAPE '\\'",
            (self._under_pattern(prefix),),
        )
        return int(row["n"]) if row else 0

    def oldest_segments_under(self, prefix: str, limit: int = 500) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM segments WHERE path LIKE ? ESCAPE '\\' "
            "ORDER BY start_ts LIMIT ?",
            (self._under_pattern(prefix), limit),
        )

    def update_segment_path(self, segment_id: int, path: str) -> None:
        self.execute("UPDATE segments SET path = ? WHERE id = ?", (path, segment_id))

    # ---- events ----------------------------------------------------------

    def add_event(
        self, camera_id: str | None, ts: float, type: str,
        label: str = "", score: float | None = None,
        meta: str = "{}",
    ) -> int:
        cur = self.execute(
            "INSERT INTO events (camera_id, ts, type, label, score, meta, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (camera_id, ts, type, label, score, meta, int(time.time())),
        )
        return int(cur.lastrowid or 0)

    def events_in_range(
        self, camera_id: str, start: float, end: float, limit: int = 500
    ) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM events WHERE camera_id = ? AND ts >= ? AND ts < ? "
            "ORDER BY ts LIMIT ?",
            (camera_id, start, end, limit),
        )

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        )

    def prune_events_older_than(self, cutoff: float) -> int:
        cur = self.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        return int(cur.rowcount or 0)

    def prune_events_for_camera_older_than(self, camera_id: str, cutoff: float) -> int:
        cur = self.execute(
            "DELETE FROM events WHERE camera_id = ? AND ts < ?", (camera_id, cutoff)
        )
        return int(cur.rowcount or 0)

    def event_camera_ids(self) -> list[str]:
        return [
            r["camera_id"]
            for r in self.query("SELECT DISTINCT camera_id FROM events")
        ]

    # ---- app settings ----------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        row = self.one("SELECT value FROM app_settings WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def all_settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.query("SELECT key, value FROM app_settings")}

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
