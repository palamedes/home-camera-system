"""An archived camera ("Remove -> Keep footage") must be completely inert.

db.camera() deliberately still resolves archived rows so History and playback
keep working, which means every *other* consumer has to check the flag itself.
Each test here pins one place that did not.
"""

import time

import pytest

from conftest import add_camera


@pytest.fixture
def archived(db):
    add_camera(db, "porch", "Porch")
    db.set_camera_archived("porch", True)
    return "porch"


def test_schedules_no_longer_drive_an_archived_camera(app_module, db, archived, monkeypatch):
    """Otherwise a removed camera's floodlight still comes on every evening."""
    from nvr.scheduler import SchedulerService
    calls = []
    monkeypatch.setattr(app_module.camera_control, "set_light",
                        lambda camera, on: calls.append((camera["id"], on)))
    sched = SchedulerService(app_module.cfg, db)
    sched._call_control("light", archived, lambda cam: app_module.camera_control.set_light(cam, True))
    assert calls == []


def test_schedules_do_not_flip_an_archived_cameras_record_flag(app_module, db, archived):
    """A later Restore must not silently start recording."""
    from nvr.scheduler import SchedulerService
    SchedulerService(app_module.cfg, db)._apply_record([], 0, 0)
    db.update_camera(archived, record=0)
    SchedulerService(app_module.cfg, db)._apply_record(
        [{"camera_id": archived, "action": "record", "days": 127,
          "start_min": 0, "end_min": 1439, "value": "on", "enabled": 1}], 0, 60)
    assert db.camera(archived)["record"] == 0


def test_automation_hook_404s_for_an_archived_camera(client, admin_client, db, archived):
    """A scene switch must not keep controlling a camera you removed."""
    tok = admin_client.get("/api/automation/token").json()["token"]
    r = client.get(f"/api/hook/cameras/{archived}/light?state=on&token={tok}")
    assert r.status_code == 404


def test_virtual_cameras_of_an_archived_parent_leave_the_grids(admin_client, db, archived):
    """Their go2rtc stream is gone, so they would be permanently dead tiles."""
    db.add_virtual_camera(archived, "Porch view", 0.0, 0.0, 1.5, "{}")
    for page in ("/", "/cameras", "/wall"):
        assert "Porch view" not in admin_client.get(page).text, page


def test_indexer_still_indexes_an_archived_cameras_final_segment(app_module, db, archived, monkeypatch):
    """The last segment closes after the flag is set; it must not be stranded."""
    from nvr import recorder as recorder_mod
    cfg = app_module.cfg
    path = cfg.storage.recordings_dir / archived / "2026-01-01" / "last.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 100)
    old = time.time() - 300
    import os
    os.utime(path, (old, old))
    monkeypatch.setattr(recorder_mod, "probe_segment", lambda p: (60.0, "h264", False))

    svc = recorder_mod.RecordingService(cfg, db, app_module.go2rtc)
    assert svc.index_new_segments() == 1
    assert len(db.segments_in_range(archived, 0, 1e12)) == 1


def test_storage_estimate_counts_archived_footage_in_its_rate(app_module, db, archived):
    """total_size() includes archived bytes, so the span must too."""
    now = time.time()
    db.add_segment(archived, "/tmp/a.mp4", now - 86400, 60.0, 1000, "h264")
    db.add_segment(archived, "/tmp/b.mp4", now - 100, 60.0, 1000, "h264")
    est = app_module.retention.estimate()
    assert est["bytes_per_day"] > 0


def test_new_camera_sorts_after_existing_virtuals(db):
    add_camera(db, "a", "A")
    db.add_virtual_camera("a", "V", 0.0, 0.0, 1.5, "{}")
    add_camera(db, "b", "B")
    vsort = db.virtual_cameras()[0]["sort_order"]
    assert db.camera("b")["sort_order"] > vsort
