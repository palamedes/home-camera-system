"""Removing a camera: archive (keep footage) vs delete (erase it).

Removal used to wipe the segment index but leave the files on disk — footage
that was neither playable nor prunable. These tests pin the two-way contract,
including the invariant that "Keep footage" really does keep it.
"""

import time

import pytest

from conftest import add_camera


@pytest.fixture
def cam(db):
    return add_camera(db, "front", "Front Door")


def _segment(cfg, camera_id, name="seg.mp4"):
    path = cfg.storage.recordings_dir / camera_id / "2026-01-01" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 100)
    return path


# --- archive ---------------------------------------------------------------

def test_archive_hides_camera_but_keeps_its_row_and_footage(admin_client, db, cam, app_module):
    path = _segment(app_module.cfg, cam)
    db.add_segment(cam, str(path), time.time() - 3600, 60.0, 100, "h264")

    assert admin_client.post(f"/api/cameras/{cam}/archive").status_code == 200

    # Gone from the normal camera list (so it isn't streamed, recorded or shown)
    assert [c["id"] for c in db.cameras()] == []
    # ...but the row and its footage survive, so History still resolves.
    assert db.camera(cam) is not None
    assert db.archived_cameras()[0]["id"] == cam
    assert path.exists()
    assert len(db.segments_in_range(cam, 0, 1e12)) == 1


def test_archived_camera_is_excluded_from_the_api_listing(admin_client, db, cam):
    admin_client.post(f"/api/cameras/{cam}/archive")
    listed = admin_client.get("/api/cameras").json()
    assert all(c["id"] != cam for c in listed)


def test_restore_brings_the_camera_back(admin_client, db, cam):
    admin_client.post(f"/api/cameras/{cam}/archive")
    assert admin_client.post(f"/api/cameras/{cam}/restore").status_code == 200
    assert [c["id"] for c in db.cameras()] == [cam]
    assert db.archived_cameras() == []


def test_archive_of_unknown_camera_is_404(admin_client):
    assert admin_client.post("/api/cameras/nope/archive").status_code == 404


def test_viewer_cannot_archive(viewer_client, db, cam):
    assert viewer_client.post(f"/api/cameras/{cam}/archive").status_code == 403
    assert db.camera(cam)["archived"] == 0


# --- delete ----------------------------------------------------------------

def test_delete_without_purge_keeps_the_files(admin_client, db, cam, app_module):
    path = _segment(app_module.cfg, cam)
    db.add_segment(cam, str(path), time.time(), 60.0, 100, "h264")

    assert admin_client.delete(f"/api/cameras/{cam}").status_code == 200

    assert db.camera(cam) is None
    assert path.exists()


def test_delete_with_purge_erases_the_footage(admin_client, db, cam, app_module):
    path = _segment(app_module.cfg, cam)
    db.add_segment(cam, str(path), time.time(), 60.0, 100, "h264")

    r = admin_client.delete(f"/api/cameras/{cam}?purge=true")

    assert r.status_code == 200 and r.json()["purged"] is True
    assert db.camera(cam) is None
    assert not path.exists()


def test_delete_cascades_to_segments_virtuals_and_schedules(admin_client, db, cam, app_module):
    """The cascade was accidentally orphaned once by an edit; pin it."""
    path = _segment(app_module.cfg, cam)
    db.add_segment(cam, str(path), time.time(), 60.0, 100, "h264")
    db.add_virtual_camera(cam, "Porch", 0.0, 0.0, 1.5, "{}")
    db.add_schedule(cam, "record", days=127, start_min=0, end_min=60)

    admin_client.delete(f"/api/cameras/{cam}")

    assert db.segments_in_range(cam, 0, 1e12) == []
    assert [v for v in db.virtual_cameras() if v["parent_id"] == cam] == []
    assert db.schedules_for(cam) == []
