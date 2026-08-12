"""Privilege boundaries that a review found broken. Each test here pins a fix
for a real defect, so a future refactor can't quietly reopen it.
"""

import pytest

from conftest import add_camera


# --- go2rtc proxy: camera credentials must never reach the browser ----------

def test_api_streams_is_not_proxyable(admin_client, db):
    """go2rtc's stream dump embeds rtsp://user:password@host for every camera.
    It takes no `src`, so the per-camera check could not gate it — even an admin
    should not be able to pull it through the proxy."""
    add_camera(db, "front")
    r = admin_client.get("/go2rtc/api/streams")
    assert r.status_code != 200
    assert "rtsp://" not in r.text


def test_proxy_requires_src(admin_client, db):
    """Omitting `src` used to skip the access check entirely."""
    add_camera(db, "front")
    assert admin_client.get("/go2rtc/api/stream.mjpeg").status_code == 400


def test_viewer_cannot_proxy_a_hidden_camera(viewer_client, db):
    add_camera(db, "secret", viewer_visible=0)
    r = viewer_client.get("/go2rtc/api/stream.mjpeg?src=secret")
    assert r.status_code == 403


def test_viewer_cannot_proxy_a_hidden_cameras_substream(viewer_client, db):
    """The `_sub` suffix is stripped to resolve the camera; it must not be a
    way around the check."""
    add_camera(db, "secret", viewer_visible=0)
    r = viewer_client.get("/go2rtc/api/stream.mjpeg?src=secret_sub")
    assert r.status_code == 403


def test_camera_api_never_returns_credentials(admin_client, db):
    add_camera(db, "front", username="admin", password="hunter2",
               main_url="rtsp://admin:hunter2@10.0.0.9/main")
    body = admin_client.get("/api/cameras").text
    assert "hunter2" not in body
    assert "rtsp://" not in body


# --- schedules: hidden cameras must not leak their timetable ---------------

def test_viewer_cannot_read_a_hidden_cameras_schedules(viewer_client, db):
    add_camera(db, "secret", viewer_visible=0)
    db.add_schedule("secret", "light", days=127, start_min=1260, end_min=300)
    assert viewer_client.get("/api/cameras/secret/schedules").status_code == 404


def test_viewer_can_read_a_visible_cameras_schedules(viewer_client, db):
    add_camera(db, "front", viewer_visible=1)
    db.add_schedule("front", "record", days=127, start_min=0, end_min=60)
    r = viewer_client.get("/api/cameras/front/schedules")
    assert r.status_code == 200 and len(r.json()) == 1


# --- login redirect --------------------------------------------------------

@pytest.mark.parametrize("target", [
    "//evil.example",          # protocol-relative -> off-site
    "///evil.example",
    "/\\evil.example",         # backslash variant some browsers normalise
    "https://evil.example",
])
def test_login_next_cannot_leave_the_site(client, app_module, db, target):
    from conftest import make_user
    make_user(app_module, db, "admin", "password123")
    r = client.post(
        "/login",
        data={"username": "admin", "password": "password123", "next": target},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_login_next_keeps_a_normal_path(client, app_module, db):
    from conftest import make_user
    make_user(app_module, db, "admin", "password123")
    r = client.post(
        "/login",
        data={"username": "admin", "password": "password123", "next": "/clips"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/clips"


# --- clip uploads: no rendering arbitrary content on our origin ------------

def test_uploaded_clip_content_type_is_not_trusted(admin_client, db):
    """A stored text/html clip would execute as a page on this origin."""
    add_camera(db, "front")
    r = admin_client.post(
        "/api/clips",
        data={"camera_id": "front", "name": "evil", "vcam_id": ""},
        files={"file": ("x.html", b"<script>alert(1)</script>", "text/html")},
    )
    assert r.status_code == 200, r.text
    clip_id = r.json()["id"]

    assert db.clip(clip_id)["mime"] in ("video/mp4", "video/webm")

    got = admin_client.get(f"/api/clips/{clip_id}/file")
    assert got.status_code == 200
    assert "text/html" not in got.headers["content-type"]
    assert got.headers.get("x-content-type-options") == "nosniff"
