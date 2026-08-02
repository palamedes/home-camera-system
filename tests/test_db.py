"""Database layer against the isolated test DB."""

import time

from conftest import add_camera


def test_user_lifecycle(db):
    assert db.user_count() == 0
    uid = db.create_user("alice", "hash", role="admin")
    assert db.user_count() == 1
    assert db.admin_count() == 1
    row = db.user_by_name("alice")
    assert row["id"] == uid and row["role"] == "admin"

    db.set_user_role(uid, "viewer")
    assert db.admin_count() == 0
    db.set_user_password(uid, "hash2")
    assert db.user_by_id(uid)["password_hash"] == "hash2"

    db.delete_user(uid)
    assert db.user_count() == 0


def test_session_expiry(db):
    uid = db.create_user("bob", "hash")
    db.create_session("live-token", uid, ttl_seconds=3600)
    db.create_session("dead-token", uid, ttl_seconds=-10)  # already expired
    assert db.session("live-token") is not None
    assert db.session("dead-token") is None  # expiry is filtered in the query
    db.purge_expired_sessions()
    assert db.session("live-token") is not None


def test_deleting_user_cascades_sessions(db):
    uid = db.create_user("carol", "hash")
    db.create_session("t", uid, ttl_seconds=3600)
    db.delete_user(uid)
    assert db.session("t") is None


def test_camera_crud_and_delete_cascade(db):
    add_camera(db, "cam1", "Cam One")
    assert db.camera("cam1")["name"] == "Cam One"
    db.update_camera("cam1", name="Renamed", record=0)
    cam = db.camera("cam1")
    assert cam["name"] == "Renamed" and cam["record"] == 0

    # Attach a segment and a virtual camera, then confirm delete_camera sweeps
    # both so nothing dangles.
    db.add_segment("cam1", "/x/a.mp4", start_ts=100.0, duration=60.0, size=10, codec="h264")
    db.add_virtual_camera("cam1", "vc", 0.0, 0.0, 1.57, "{}")
    db.delete_camera("cam1")
    assert db.camera("cam1") is None
    assert db.segments_in_range("cam1", 0, 1e12) == []
    assert db.virtual_cameras() == []


def test_segments_range_includes_straddling_segment(db):
    add_camera(db, "cam1")
    # A segment [100,160) that starts before the query window but runs into it
    # must be returned, or playback clips the first partial minute.
    db.add_segment("cam1", "/x/a.mp4", 100.0, 60.0, 10, "h264")
    db.add_segment("cam1", "/x/b.mp4", 160.0, 60.0, 10, "h264")
    db.add_segment("cam1", "/x/c.mp4", 400.0, 60.0, 10, "h264")
    rows = db.segments_in_range("cam1", 150.0, 200.0)
    paths = [r["path"] for r in rows]
    assert paths == ["/x/a.mp4", "/x/b.mp4"]  # c is out of range


def test_segment_bounds_and_stats(db):
    add_camera(db, "cam1")
    assert db.segment_bounds("cam1") is None
    db.add_segment("cam1", "/x/a.mp4", 100.0, 60.0, 10, "h264")
    db.add_segment("cam1", "/x/b.mp4", 200.0, 60.0, 20, "h264")
    assert db.segment_bounds("cam1") == (100.0, 260.0)
    stats = db.camera_stats("cam1")
    assert stats["segments"] == 2 and stats["bytes"] == 30


def test_add_segment_ignores_duplicate_path(db):
    add_camera(db, "cam1")
    db.add_segment("cam1", "/x/a.mp4", 100.0, 60.0, 10, "h264")
    db.add_segment("cam1", "/x/a.mp4", 100.0, 60.0, 10, "h264")  # same path
    assert db.camera_stats("cam1")["segments"] == 1


def test_segments_older_than(db):
    add_camera(db, "cam1")
    db.add_segment("cam1", "/x/old.mp4", 100.0, 60.0, 10, "h264")
    db.add_segment("cam1", "/x/new.mp4", 10_000.0, 60.0, 10, "h264")
    old = db.segments_older_than_for_camera("cam1", cutoff=5_000.0)
    assert [r["path"] for r in old] == ["/x/old.mp4"]


def test_clip_crud(db):
    add_camera(db, "cam1")
    cid = db.add_clip("cam1", "My Clip", "/clips/x.webm", "video/webm", 1234,
                      duration=12.5)
    row = db.clip(cid)
    assert row["name"] == "My Clip" and row["size"] == 1234
    assert len(db.clips()) == 1
    db.delete_clip(cid)
    assert db.clip(cid) is None


def test_virtual_camera_crud(db):
    add_camera(db, "cam1")
    vid = db.add_virtual_camera("cam1", "Door", 1.0, 0.5, 1.2, "{}")
    v = db.virtual_camera(vid)
    assert v["name"] == "Door" and v["parent_id"] == "cam1"
    db.delete_virtual_camera(vid)
    assert db.virtual_camera(vid) is None
