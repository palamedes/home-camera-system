"""Segment indexing: what gets indexed, and what gets cleaned up.

The indexer is the only thing that decides whether a recorded file becomes
playable history. It also now deletes files it can prove are unreadable — the
riskiest operation in the codebase, so the safety rails are tested explicitly.
"""

import time

import pytest

from conftest import add_camera
from nvr import recorder as recorder_mod


@pytest.fixture
def service(app_module, db):
    """A RecordingService wired to the test config/db, with no threads."""
    return recorder_mod.RecordingService(app_module.cfg, db, app_module.go2rtc)


def _segment(cfg, camera_id, name, *, age_seconds=0.0, data=b"x" * 100):
    path = cfg.storage.recordings_dir / camera_id / "2026-01-01" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if age_seconds:
        old = time.time() - age_seconds
        import os
        os.utime(path, (old, old))
    return path


def test_good_segment_is_indexed(service, app_module, db, monkeypatch):
    cfg = app_module.cfg
    add_camera(db, "camgood")
    path = _segment(cfg, "camgood", "good.mp4", age_seconds=60)
    monkeypatch.setattr(recorder_mod, "probe_segment", lambda p: (60.0, "h264", True))

    assert service.index_new_segments() == 1
    assert [r["path"] for r in db.segments_in_range("camgood", 0, 1e12)] == [str(path)]
    assert path.exists()


def test_unreadable_segment_is_deleted_once_old(service, app_module, db, monkeypatch):
    """A crash-truncated file is unplayable, so it must not be indexed — and it
    must not linger forever either, since retention only prunes indexed rows."""
    cfg = app_module.cfg
    add_camera(db, "camtrunc")
    path = _segment(cfg, "camtrunc", "truncated.mp4", age_seconds=7200)
    # ffprobe ran and reached a verdict: the file is garbage.
    monkeypatch.setattr(recorder_mod, "probe_segment", lambda p: (None, None, True))

    assert service.index_new_segments() == 0
    assert not path.exists()
    assert db.segments_in_range("camtrunc", 0, 1e12) == []


def test_unreadable_but_recent_segment_is_left_alone(service, app_module, db, monkeypatch):
    """Never delete something that might still be mid-write."""
    cfg = app_module.cfg
    add_camera(db, "camfresh")
    path = _segment(cfg, "camfresh", "fresh.mp4", age_seconds=120)
    monkeypatch.setattr(recorder_mod, "probe_segment", lambda p: (None, None, True))

    service.index_new_segments()
    assert path.exists()


def test_broken_ffprobe_never_deletes_anything(service, app_module, db, monkeypatch):
    """THE critical safety rail: if ffprobe itself cannot run, every segment
    probes as 'unreadable'. Deleting on that would wipe the entire archive."""
    cfg = app_module.cfg
    add_camera(db, "camprobe")
    paths = [
        _segment(cfg, "camprobe", f"seg{i}.mp4", age_seconds=86400) for i in range(5)
    ]
    # probed=False -> ffprobe missing/timed out, no verdict on the file.
    monkeypatch.setattr(recorder_mod, "probe_segment", lambda p: (None, None, False))

    service.index_new_segments()
    assert all(p.exists() for p in paths), "a broken ffprobe deleted real footage"


def test_rebuilt_database_reindexes_rather_than_deletes(service, app_module, db, monkeypatch):
    """If the index is ever lost, valid footage must be recovered, not erased."""
    cfg = app_module.cfg
    add_camera(db, "camrebuild")
    paths = [
        _segment(cfg, "camrebuild", f"seg{i}.mp4", age_seconds=86400) for i in range(3)
    ]
    monkeypatch.setattr(recorder_mod, "probe_segment", lambda p: (60.0, "h264", True))

    added = service.index_new_segments()

    assert added == 3
    assert all(p.exists() for p in paths)
    assert len(db.segments_in_range("camrebuild", 0, 1e12)) == 3


def test_zero_byte_segment_is_removed(service, app_module, db):
    cfg = app_module.cfg
    add_camera(db, "camzero")
    path = _segment(cfg, "camzero", "empty.mp4", age_seconds=60, data=b"")

    service.index_new_segments()
    assert not path.exists()


def test_probe_duration_still_returns_a_pair(monkeypatch):
    """Back-compat: the old two-value helper keeps its shape."""
    monkeypatch.setattr(recorder_mod, "probe_segment", lambda p: (12.5, "hevc", True))
    assert recorder_mod.probe_duration("whatever") == (12.5, "hevc")


# --- probe_segment: the verdict that gates deletion -------------------------
# ffprobe exits non-zero and prints an empty JSON object for BOTH a truncated
# MP4 and an intact file it cannot open, so these cases pin the only signal
# that separates them.

def _real_mp4(path):
    """A genuinely valid little MP4, built with ffmpeg."""
    import subprocess
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=duration=2:size=320x240:rate=10",
         "-c:v", "libx264", str(path)],
        check=True, capture_output=True,
    )
    return path


def test_probe_reads_a_valid_file(tmp_path):
    good = _real_mp4(tmp_path / "good.mp4")
    duration, codec, condemned = recorder_mod.probe_segment(good)
    assert duration and duration > 0
    assert codec == "h264"
    assert condemned is False


def test_probe_condemns_a_truncated_file(tmp_path):
    good = _real_mp4(tmp_path / "good.mp4")
    half = tmp_path / "half.mp4"
    half.write_bytes(good.read_bytes()[:3000])   # no moov atom
    duration, _, condemned = recorder_mod.probe_segment(half)
    assert duration is None
    assert condemned is True


def test_probe_does_not_condemn_a_file_it_cannot_open(tmp_path):
    """THE case that matters: an INTACT recording we lack permission to read
    must never be judged corrupt. Footage restored under the wrong owner, or a
    disk throwing I/O errors, would otherwise be deleted."""
    import os
    good = _real_mp4(tmp_path / "good.mp4")
    os.chmod(good, 0o000)
    try:
        duration, _, condemned = recorder_mod.probe_segment(good)
    finally:
        os.chmod(good, 0o644)
    assert duration is None
    assert condemned is False, "intact but unreadable footage was condemned"


def test_probe_does_not_condemn_a_missing_file(tmp_path):
    duration, _, condemned = recorder_mod.probe_segment(tmp_path / "gone.mp4")
    assert duration is None and condemned is False


def test_unreadable_permissions_do_not_delete_footage(service, app_module, db):
    """End to end: the indexer must leave an unreadable-but-intact file alone."""
    import os
    cfg = app_module.cfg
    add_camera(db, "camperm")
    path = _real_mp4(cfg.storage.recordings_dir / "camperm" / "2026-01-01" / "x.mp4")
    old = time.time() - 7200
    os.utime(path, (old, old))
    os.chmod(path, 0o000)
    try:
        service.index_new_segments()
        assert path.exists(), "the indexer deleted intact footage it could not read"
    finally:
        os.chmod(path, 0o644)
