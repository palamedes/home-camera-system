"""Shared test fixtures.

The app wires its config, database, and background services at import time
(nvr/main.py module scope). So before importing anything from the package we
point SENTRY_CONFIG and SENTRY_DATA_DIR at a throwaway temp directory — every
test then runs against an isolated SQLite file and clip/recording dirs, never
the real ~/Cameras/data. The go2rtc/recorder/retention threads are only started
by the lifespan handler, which we deliberately never enter, so no camera
processes spin up during tests.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# --- isolate the app's storage BEFORE nvr.* is imported anywhere ------------
_TMP = Path(tempfile.mkdtemp(prefix="sentry-tests-"))
(_TMP / "config").mkdir()
(_TMP / "config" / "config.yaml").write_text(
    f"""
server:
  host: "127.0.0.1"
  port: 8080
  session_days: 30
storage:
  recordings_dir: {_TMP / "recordings"}
  clips_dir: {_TMP / "clips"}
  max_usage: 80%
  max_age_days: 7
playback:
  qsv_device: null
"""
)
os.environ["SENTRY_CONFIG"] = str(_TMP / "config" / "config.yaml")
os.environ["SENTRY_DATA_DIR"] = str(_TMP / "data")


@pytest.fixture(scope="session")
def app_module():
    """Import the app once, with storage already redirected to the temp dir.

    Mutating camera routes call go2rtc.reload()/recording.sync(), which would
    otherwise launch the real go2rtc binary and recorder threads. We're testing
    HTTP/DB behaviour, not the video pipeline, so neuter those side effects.
    """
    from nvr import main  # noqa: WPS433 — deliberately late, after env is set
    main.go2rtc.reload = lambda *a, **k: None
    main.go2rtc.start = lambda *a, **k: None
    main.recording.sync = lambda *a, **k: None
    main.recording.start = lambda *a, **k: None
    return main


@pytest.fixture()
def db(app_module):
    """The live app database, wiped clean before each test."""
    d = app_module.db
    for table in ("clips", "virtual_cameras", "segments", "schedules",
                  "cameras", "sessions", "users"):
        d.execute(f"DELETE FROM {table}")
    return d


@pytest.fixture()
def client(app_module, db):
    """An anonymous TestClient that does NOT run the lifespan (no go2rtc)."""
    from fastapi.testclient import TestClient
    return TestClient(app_module.app)


# --- helpers ----------------------------------------------------------------

def make_user(app_module, db, username="admin", password="password123",
              role="admin"):
    from nvr import auth
    return db.create_user(username, auth.hash_password(password), role=role)


def login(client, username="admin", password="password123"):
    """Log a seeded user in; returns the client with its session cookie set."""
    r = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return client


@pytest.fixture()
def login_as(app_module, db):
    """Factory: seed a user (if needed) and return a fresh, independently
    logged-in TestClient. Two calls give two separate sessions, so a test can
    act as an admin and a viewer at the same time."""
    from fastapi.testclient import TestClient

    def _make(username, role="admin", password="password123"):
        if db.user_by_name(username) is None:
            make_user(app_module, db, username, password, role=role)
        c = TestClient(app_module.app)
        return login(c, username, password)

    return _make


@pytest.fixture()
def admin_client(login_as):
    return login_as("admin", role="admin")


@pytest.fixture()
def viewer_client(login_as):
    return login_as("viewer", role="viewer")


def add_camera(db, camera_id="front", name="Front Door", **overrides):
    fields = dict(
        id=camera_id, name=name, host="10.0.0.9", port=80,
        main_url="rtsp://x/main", sub_url="rtsp://x/sub",
        viewer_visible=1, show_on_grid=1, fisheye=0,
    )
    fields.update(overrides)
    db.add_camera(**fields)
    return camera_id
