"""ffmpeg's stderr must never be an undrained pipe.

A long-lived child whose stderr pipe nobody reads blocks forever once the
64 KiB kernel buffer fills. For the recorder that means ffmpeg stops writing
segments while still *running* — poll() stays None, so the supervisor sees a
healthy process and the UI shows a recording camera that has silently stopped.
"""

import subprocess

import pytest

from conftest import add_camera
from nvr import recorder as recorder_mod


@pytest.fixture
def rec(app_module, db):
    add_camera(db, "cam1")
    return recorder_mod.CameraRecorder(dict(db.camera("cam1")), app_module.cfg)


def test_recorder_never_uses_a_stderr_pipe(rec, monkeypatch):
    captured = {}

    class FakeProc:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(recorder_mod.subprocess, "Popen", fake_popen)
    rec.start()

    assert captured["stderr"] is not subprocess.PIPE
    # It should be a writable file object (or DEVNULL if the log could not open)
    assert captured["stderr"] == subprocess.DEVNULL or hasattr(captured["stderr"], "write")


def test_recorder_writes_ffmpeg_output_to_a_log_file(rec, monkeypatch):
    class FakeProc:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0

    monkeypatch.setattr(recorder_mod.subprocess, "Popen", lambda cmd, **kw: FakeProc())
    rec.start()
    assert rec.log_path.exists()
    rec.stop()


def test_last_error_is_read_from_the_log_after_a_crash(rec, monkeypatch):
    """The diagnostic still works now that stderr goes to a file."""
    class DeadProc:
        def poll(self): return 1
        def terminate(self): pass
        def wait(self, timeout=None): return 1

    monkeypatch.setattr(recorder_mod.subprocess, "Popen", lambda cmd, **kw: DeadProc())
    rec.start()
    rec.log_path.write_text("some warning\nConnection refused\n")
    rec.process = DeadProc()

    rec.check()

    assert rec.last_error == "Connection refused"
    assert rec.restarts >= 1


def test_a_huge_ffmpeg_log_does_not_block_the_recorder(rec, monkeypatch):
    """The regression this guards: >64 KiB of output used to wedge ffmpeg."""
    class FakeProc:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0

    monkeypatch.setattr(recorder_mod.subprocess, "Popen", lambda cmd, **kw: FakeProc())
    rec.start()
    handle = rec._log_handle
    assert handle is not None
    handle.write("x" * (256 * 1024))   # 4x the pipe buffer that used to deadlock
    handle.flush()
    rec.stop()
    assert rec.log_path.stat().st_size >= 256 * 1024
