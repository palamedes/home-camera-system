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


def test_archived_camera_keeps_its_never_delete_footage(app_module, db):
    """Removing a camera with "Keep footage" must not silently downgrade its
    retention. An archived camera drops out of db.cameras() by default, and the
    orphan branch would then prune it at the global cap — destroying footage the
    operator explicitly marked never-delete."""
    cfg = app_module.cfg
    add_camera(db, "cam1", retention_seconds=0)      # "Never (until space)"
    now = time.time()
    ancient = _segment_file(cfg, "cam1/ancient.mp4")
    db.add_segment("cam1", str(ancient), now - 30 * 86400, 60.0, 100, "h264")

    db.set_camera_archived("cam1", True)
    RetentionService(cfg, db).run_once()

    assert ancient.exists()


def test_archived_camera_keeps_long_retention_override(app_module, db):
    """A 90-day override outlives archiving even though the global cap is 7d."""
    cfg = app_module.cfg
    add_camera(db, "cam2", retention_seconds=90 * 86400)
    now = time.time()
    old = _segment_file(cfg, "cam2/thirty_days.mp4")
    db.add_segment("cam2", str(old), now - 30 * 86400, 60.0, 100, "h264")

    db.set_camera_archived("cam2", True)
    RetentionService(cfg, db).run_once()

    assert old.exists()


# --- event markers must not outlive the footage they annotate ---------------
# Age alone cannot decide this: the size quota and free-space backstop prune
# footage long before max_age_days, which left tick marks on the history
# timeline pointing at footage that was already gone.

def test_event_markers_are_pruned_with_their_footage(app_module, db):
    cfg = app_module.cfg
    add_camera(db, "cam1")
    now = time.time()
    # Footage only goes back 2 days...
    recent = _segment_file(cfg, "cam1/recent.mp4")
    db.add_segment("cam1", str(recent), now - 2 * 86400, 60.0, 100, "h264")
    # ...but markers exist from 3 days ago (their footage was already pruned by
    # the size quota) and from within the retained window.
    db.add_event("cam1", now - 3 * 86400, "person")
    db.add_event("cam1", now - 86400, "person")

    RetentionService(cfg, db).run_once()

    kept = [e["ts"] for e in db.events_in_range("cam1", 0, 1e12)]
    assert len(kept) == 1
    assert kept[0] > now - 2 * 86400


def test_markers_inside_the_footage_window_survive(app_module, db):
    cfg = app_module.cfg
    add_camera(db, "cam1")
    now = time.time()
    seg = _segment_file(cfg, "cam1/a.mp4")
    db.add_segment("cam1", str(seg), now - 3600, 60.0, 100, "h264")
    db.add_event("cam1", now - 1800, "vehicle")

    RetentionService(cfg, db).run_once()

    assert len(db.events_in_range("cam1", 0, 1e12)) == 1


def test_a_camera_with_no_footage_keeps_its_detection_log(app_module, db):
    """Recording switched off: those events are a log, not a broken promise, so
    they must not be mass-deleted."""
    cfg = app_module.cfg
    add_camera(db, "cam1", record=0)
    now = time.time()
    db.add_event("cam1", now - 3 * 86400, "person")

    RetentionService(cfg, db).run_once()

    assert len(db.events_in_range("cam1", 0, 1e12)) == 1
