"""Relocatable storage: path validation, the recordings pool (multi-volume),
lenient boot overlay, and the migration that consolidates stranded footage."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from nvr import appsettings
from nvr.appsettings import SettingError
from nvr.config import StorageVolume
from nvr.storage_migrate import StorageMigrator
from conftest import add_camera


@pytest.fixture(autouse=True)
def _restore_storage(app_module):
    """These tests mutate the shared cfg.storage pool; snapshot and restore so
    nothing leaks into other tests in the session."""
    s = app_module.cfg.storage
    orig = (list(s.volumes), s.clips_dir)
    yield
    s.volumes, s.clips_dir = list(orig[0]), orig[1]


# --- path validation -------------------------------------------------------

def test_validate_rejects_unwritable(app_module):
    info = appsettings.validate_storage_dir(Path("/proc/nonexistent/sentry"), create=True)
    assert info["ok"] is False


def test_validate_ok_reports_space(app_module, tmp_path):
    info = appsettings.validate_storage_dir(tmp_path / "rec", create=True)
    assert info["ok"] is True
    assert info["free"] > 0 and info["total"] > 0


# --- the recordings pool ---------------------------------------------------

def test_apply_volumes_sets_pool(app_module, db, tmp_path):
    cfg = app_module.cfg
    v1, v2 = tmp_path / "local", tmp_path / "nas"
    appsettings.apply_volumes(cfg, db, [
        {"path": str(v1), "cap": "50%"},
        {"path": str(v2), "cap": "400G"},
    ])
    assert [str(v.path) for v in cfg.storage.volumes] == [str(v1), str(v2)]
    assert cfg.storage.volumes[1].cap == "400G"
    assert v1.is_dir() and v2.is_dir()               # created
    assert cfg.storage.recordings_dir == v1          # primary = first


def test_apply_volumes_requires_one(app_module, db):
    with pytest.raises(SettingError):
        appsettings.apply_volumes(app_module.cfg, db, [])


def test_apply_volumes_rejects_bad_path_and_dupes(app_module, db, tmp_path):
    cfg = app_module.cfg
    with pytest.raises(SettingError):
        appsettings.apply_volumes(cfg, db, [{"path": "/proc/x/nope"}])
    with pytest.raises(SettingError):
        appsettings.apply_volumes(cfg, db, [
            {"path": str(tmp_path / "a")}, {"path": str(tmp_path / "a")},
        ])


def test_volumes_overlay_applied_on_load(app_module, db, tmp_path):
    cfg = app_module.cfg
    appsettings.apply_volumes(cfg, db, [{"path": str(tmp_path / "p"), "cap": "70%"}])
    import types
    fresh = types.SimpleNamespace(storage=__import__("nvr.config", fromlist=["StorageConfig"]).StorageConfig(),
                                  weather=cfg.weather, alerts=cfg.alerts,
                                  server=cfg.server, go2rtc=cfg.go2rtc,
                                  discovery=cfg.discovery, playback=cfg.playback)
    appsettings.load_overrides(fresh, db)
    assert [str(v.path) for v in fresh.storage.volumes] == [str(tmp_path / "p")]
    assert fresh.storage.volumes[0].cap == "70%"


def test_volumes_overlay_keeps_unmounted_volume(app_module, db, tmp_path):
    # An unmounted volume stays in the list (fstab may remount it); we do NOT
    # drop it the way we drop a bad single-path relocation.
    db.set_setting("volumes", json.dumps([{"path": str(tmp_path / "gone"), "cap": "80%"}]))
    import types
    from nvr.config import StorageConfig
    cfg = types.SimpleNamespace(storage=StorageConfig(),
                                weather=app_module.cfg.weather, alerts=app_module.cfg.alerts,
                                server=app_module.cfg.server, go2rtc=app_module.cfg.go2rtc,
                                discovery=app_module.cfg.discovery, playback=app_module.cfg.playback)
    appsettings.load_overrides(cfg, db)
    assert [str(v.path) for v in cfg.storage.volumes] == [str(tmp_path / "gone")]


# --- clips dir still relocatable via apply_storage -------------------------

def test_apply_storage_moves_clips_dir(app_module, db, tmp_path):
    cfg = app_module.cfg
    new_clips = tmp_path / "nas" / "clips"
    applied = appsettings.apply_storage(cfg, db, {"clips_dir": str(new_clips)})
    assert applied["clips_dir"] == str(new_clips)
    assert cfg.storage.clips_dir == new_clips
    assert new_clips.is_dir()


# --- migration: consolidate stranded footage onto the primary --------------

def test_migrator_moves_stranded_segments_and_clips(app_module, db, tmp_path):
    cfg = app_module.cfg
    new = tmp_path / "new"
    cfg.storage.volumes = [StorageVolume(new / "recordings", "80%")]
    cfg.storage.clips_dir = new / "clips"
    cfg.storage.recordings_dir.mkdir(parents=True)
    cfg.storage.clips_dir.mkdir(parents=True)

    add_camera(db, "cam1")
    seg = tmp_path / "old" / "recordings" / "cam1" / "2026-08-05" / "10-00-00.mp4"
    seg.parent.mkdir(parents=True)
    seg.write_bytes(b"x" * 500)
    db.add_segment("cam1", str(seg), time.time() - 3600, 60.0, 500, "h264")
    clip = tmp_path / "old" / "clips" / "cam1-1.webm"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"y" * 300)
    db.add_clip("cam1", "A clip", str(clip), "video/webm", 300)

    mig = StorageMigrator(cfg, db)
    mig._run()
    assert mig.status()["moved"] == 2 and mig.status()["failed"] == 0

    new_seg = cfg.storage.recordings_dir / "cam1" / "2026-08-05" / "10-00-00.mp4"
    assert new_seg.exists() and not seg.exists()
    assert db.all_segments()[0]["path"] == str(new_seg)


def test_migrator_leaves_footage_on_any_pool_volume(app_module, db, tmp_path):
    # A file on volume 2 must NOT be migrated — it's already in the pool.
    cfg = app_module.cfg
    v1, v2 = tmp_path / "v1", tmp_path / "v2"
    cfg.storage.volumes = [StorageVolume(v1, "80%"), StorageVolume(v2, "80%")]
    add_camera(db, "cam1")
    onv2 = v2 / "cam1" / "2026-08-05" / "s.mp4"
    onv2.parent.mkdir(parents=True)
    onv2.write_bytes(b"z" * 100)
    db.add_segment("cam1", str(onv2), time.time(), 60.0, 100, "h264")

    mig = StorageMigrator(cfg, db)
    mig._run()
    assert mig.status()["total"] == 0      # nothing stranded


# --- overflow: recorder volume selection -----------------------------------

def test_choose_volume_overflows_when_first_is_full(app_module, db, tmp_path):
    from nvr.recorder import RecordingService
    cfg = app_module.cfg
    v1, v2 = tmp_path / "v1", tmp_path / "v2"
    v1.mkdir()
    v2.mkdir()
    cfg.storage.volumes = [StorageVolume(v1, "1K"), StorageVolume(v2, "80%")]
    add_camera(db, "cam1")
    svc = RecordingService(cfg, db, None)

    assert svc.choose_volume() == v1              # empty -> primary

    seg = v1 / "cam1" / "2026-08-05" / "s.mp4"
    seg.parent.mkdir(parents=True)
    seg.write_bytes(b"x" * 4096)
    db.add_segment("cam1", str(seg), time.time(), 60.0, 4096, "h264")   # over the 1K cap
    assert svc.choose_volume() == v2              # overflow to the next volume


def test_choose_volume_skips_unavailable(app_module, db, tmp_path):
    from nvr.recorder import RecordingService
    cfg = app_module.cfg
    missing, present = tmp_path / "not-mounted", tmp_path / "present"
    present.mkdir()
    cfg.storage.volumes = [StorageVolume(missing, "80%"), StorageVolume(present, "80%")]
    svc = RecordingService(cfg, db, None)
    assert svc.choose_volume() == present         # absent volume skipped


# --- endpoints -------------------------------------------------------------

def test_volume_endpoints_admin_only(app_module, viewer_client):
    assert viewer_client.get("/api/settings/volumes").status_code == 403
    assert viewer_client.post("/api/settings/volumes", json={}).status_code == 403


def test_volumes_apply_endpoint(app_module, admin_client, tmp_path):
    v = tmp_path / "pool1"
    r = admin_client.post("/api/settings/volumes",
                          json={"volumes": [{"path": str(v), "cap": "90%"}]})
    assert r.status_code == 200
    assert [x["path"] for x in r.json()["volumes"]] == [str(v)]
    assert app_module.cfg.storage.recordings_dir == v
