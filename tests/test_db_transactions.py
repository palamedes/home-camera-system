"""A failed write must not poison the connection.

SQLite holds the WAL writer lock for an open transaction. Connections are
cached per thread, so a write that raises without rolling back leaves that
thread wedged permanently — and every other writer blocks on the busy timeout
and then fails, which the background threads swallow and retry forever.
"""

import threading

import pytest

from conftest import add_camera


def test_failed_write_does_not_leave_an_open_transaction(db):
    add_camera(db, "cam1")
    with pytest.raises(Exception):
        db.execute("UPDATE cameras SET name = ? WHERE id = ?", (None, "cam1"))
    assert db.connect().in_transaction is False


def test_writes_still_work_after_a_failure(db):
    add_camera(db, "cam1")
    with pytest.raises(Exception):
        db.execute("UPDATE cameras SET name = ? WHERE id = ?", (None, "cam1"))
    db.update_camera("cam1", name="Recovered")
    assert db.camera("cam1")["name"] == "Recovered"


def test_another_thread_can_still_write_after_a_failure(db):
    """The real symptom: the segment indexer and retention run on other threads."""
    add_camera(db, "cam1")
    with pytest.raises(Exception):
        db.execute("UPDATE cameras SET name = ? WHERE id = ?", (None, "cam1"))

    result = {}

    def writer():
        try:
            db.add_segment("cam1", "/tmp/x.mp4", 1.0, 60.0, 100, "h264")
            result["ok"] = True
        except Exception as exc:            # pragma: no cover - failure path
            result["error"] = repr(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    thread.join(timeout=15)

    assert not thread.is_alive(), "a second thread blocked on the writer lock"
    assert result.get("ok"), f"second thread could not write: {result.get('error')}"
