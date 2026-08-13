"""The schedules rebuild must not lose anybody's schedules.

Adding device targets meant camera_id had to stop being NOT NULL, which SQLite
cannot do in place — the table is recreated and copied. That is the single most
destructive statement in the codebase, so it is exercised against a database
built with the OLD schema.
"""

import sqlite3
import time

from nvr.db import Database

OLD_SCHEDULES = """
CREATE TABLE schedules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id  TEXT    NOT NULL,
    action     TEXT    NOT NULL,
    days       INTEGER NOT NULL DEFAULT 127,
    start_min  INTEGER NOT NULL,
    end_min    INTEGER NOT NULL,
    value      TEXT    NOT NULL DEFAULT 'on',
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
"""


def _legacy_db(tmp_path):
    """A database as it existed before device schedules."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEDULES)
    now = int(time.time())
    conn.executemany(
        "INSERT INTO schedules (camera_id, action, days, start_min, end_min, "
        "value, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("driveway", "record", 127, 540, 1020, "on", 1, now),
            ("driveway", "light", 62, 1320, 360, "on", 1, now),
            ("fe-p", "nightvision", 127, 1200, 300, "bw", 0, now),
        ],
    )
    conn.commit()
    conn.close()
    return path


def test_existing_schedules_survive_the_rebuild(tmp_path):
    path = _legacy_db(tmp_path)
    before = sqlite3.connect(path).execute(
        "SELECT id, camera_id, action, days, start_min, end_min, value, enabled "
        "FROM schedules ORDER BY id"
    ).fetchall()
    assert len(before) == 3

    Database(path)          # opening runs the migration

    rows = sqlite3.connect(path).execute(
        "SELECT id, camera_id, action, days, start_min, end_min, value, enabled "
        "FROM schedules ORDER BY id"
    ).fetchall()
    assert rows == before, "the rebuild changed or dropped existing schedules"


def test_the_rebuilt_table_accepts_device_schedules(tmp_path):
    db = Database(_legacy_db(tmp_path))
    sid = db.add_schedule(device_id="porch", action="power", days=127,
                          start_min=1080, end_min=1380)
    row = db.one("SELECT * FROM schedules WHERE id = ?", (sid,))
    assert row["device_id"] == "porch"
    assert row["camera_id"] is None


def test_migration_is_idempotent(tmp_path):
    path = _legacy_db(tmp_path)
    Database(path)
    Database(path)          # second open must be a no-op, not a second rebuild
    count = sqlite3.connect(path).execute("SELECT COUNT(*) FROM schedules").fetchone()[0]
    assert count == 3


def test_a_fresh_database_already_has_the_new_shape(tmp_path):
    db = Database(tmp_path / "fresh.db")
    cols = {r["name"] for r in db.query("PRAGMA table_info(schedules)")}
    assert "device_id" in cols
