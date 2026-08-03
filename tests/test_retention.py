"""Retention pruning: age limits, per-camera overrides, index/disk consistency.

Only the age limits are exercised deterministically. The size quota and
free-space backstop key off the real filesystem's disk_usage (a large disk in
CI), so they won't fire against a few tiny fixture files — testing them would
mean mocking shutil.disk_usage, which buys little over the age path.
"""

import time

from conftest import add_camera
from nvr.retention import RetentionService


def _segment_file(cfg, name, data=b"x" * 100):
    path = cfg.storage.recordings_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_age_prune_removes_old_keeps_new(app_module, db):
    cfg = app_module.cfg
    add_camera(db, "cam1")
    now = time.time()
    old = _segment_file(cfg, "cam1/old.mp4")
    new = _segment_file(cfg, "cam1/new.mp4")
    # Global limit is 7 days (test config).
    db.add_segment("cam1", str(old), now - 8 * 86400, 60.0, 100, "h264")
    db.add_segment("cam1", str(new), now - 3600, 60.0, 100, "h264")

    result = RetentionService(cfg, db).run_once()

    assert result["deleted"] == 1
    assert not old.exists()          # pruned from disk
    assert new.exists()              # within retention
    # Index and disk stay consistent: the pruned row is gone too.
    remaining = [r["path"] for r in db.segments_in_range("cam1", 0, 1e12)]
    assert remaining == [str(new)]


def test_per_camera_override_prunes_tighter_than_global(app_module, db):
    cfg = app_module.cfg
    add_camera(db, "cam1", retention_seconds=3600)  # keep only the last hour
    now = time.time()
    stale = _segment_file(cfg, "cam1/stale.mp4")
    fresh = _segment_file(cfg, "cam1/fresh.mp4")
    db.add_segment("cam1", str(stale), now - 2 * 3600, 60.0, 100, "h264")  # 2h old
    db.add_segment("cam1", str(fresh), now - 600, 60.0, 100, "h264")       # 10m old

    RetentionService(cfg, db).run_once()

    assert not stale.exists()  # older than the 1h override
    assert fresh.exists()


def test_orphan_segments_pruned_by_global_age(app_module, db):
    """Footage from a since-deleted camera still ages out under the global
    limit rather than lingering forever."""
    cfg = app_module.cfg
    now = time.time()
    orphan = _segment_file(cfg, "gone/old.mp4")
    # No camera row for 'gone' — it was deleted, segments left behind.
    db.add_segment("gone", str(orphan), now - 30 * 86400, 60.0, 100, "h264")

    RetentionService(cfg, db).run_once()

    assert not orphan.exists()
    assert db.segments_in_range("gone", 0, 1e12) == []


def test_nothing_pruned_when_all_fresh(app_module, db):
    cfg = app_module.cfg
    add_camera(db, "cam1")
    now = time.time()
    f = _segment_file(cfg, "cam1/recent.mp4")
    db.add_segment("cam1", str(f), now - 60, 60.0, 100, "h264")
    result = RetentionService(cfg, db).run_once()
    assert result["deleted"] == 0
    assert f.exists()


def test_never_keeps_old_footage_when_space_is_free(app_module, db):
    """retention_seconds == 0 means 'never delete by age' — old footage survives
    as long as there's room."""
    cfg = app_module.cfg
    add_camera(db, "cam1", retention_seconds=0)
    now = time.time()
    ancient = _segment_file(cfg, "cam1/ancient.mp4")
    db.add_segment("cam1", str(ancient), now - 30 * 86400, 60.0, 100, "h264")
    RetentionService(cfg, db).run_once()
    assert ancient.exists()


def test_rolling_window_keeps_recent_while_recording(app_module, db):
    """Rolling keep removes footage older than (newest segment - window),
    keeping ~the rolling amount during active recording."""
    cfg = app_module.cfg
    add_camera(db, "cam1", rolling_keep_seconds=3600, retention_seconds=0)
    now = time.time()
    old = _segment_file(cfg, "cam1/old.mp4")
    recent = _segment_file(cfg, "cam1/recent.mp4")
    db.add_segment("cam1", str(old), now - 3 * 3600, 60.0, 100, "h264")  # 3h old
    db.add_segment("cam1", str(recent), now - 600, 60.0, 100, "h264")    # newest

    RetentionService(cfg, db).run_once()

    assert not old.exists()   # older than newest minus the 1h window
    assert recent.exists()


def test_rolling_window_holds_footage_when_recording_stopped(app_module, db):
    """The window is anchored to the newest segment, so when recording stops the
    last captured window is held rather than aged out on a clock."""
    cfg = app_module.cfg
    add_camera(db, "cam1", rolling_keep_seconds=3600, retention_seconds=0)
    now = time.time()
    # Recording stopped 5 days ago; this is the newest (and only) segment.
    held = _segment_file(cfg, "cam1/held.mp4")
    db.add_segment("cam1", str(held), now - 5 * 86400, 60.0, 100, "h264")

    RetentionService(cfg, db).run_once()

    assert held.exists()   # within its own rolling window; no hard cap to purge it


def test_hard_cap_purges_held_footage(app_module, db):
    """The absolute 'delete after' cap eventually purges footage the rolling
    window would otherwise hold."""
    cfg = app_module.cfg
    add_camera(db, "cam1", rolling_keep_seconds=3600, retention_seconds=4 * 86400)
    now = time.time()
    held = _segment_file(cfg, "cam1/held.mp4")
    db.add_segment("cam1", str(held), now - 5 * 86400, 60.0, 100, "h264")  # 5 days old

    RetentionService(cfg, db).run_once()

    assert not held.exists()   # past the 4-day hard cap
