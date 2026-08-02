"""Saved-clips API: save, fetch, delete, and access control."""

from conftest import add_camera


def _save(client, camera_id="cam1", name="Test clip", content=b"fake-webm-bytes"):
    return client.post(
        "/api/clips",
        data={"camera_id": camera_id, "name": name},
        files={"file": ("clip.webm", content, "video/webm")},
    )


def test_save_and_fetch_clip_roundtrip(admin_client, db):
    add_camera(db, "cam1")
    r = _save(admin_client, content=b"hello-clip")
    assert r.status_code == 200
    clip_id = r.json()["id"]

    row = db.clip(clip_id)
    assert row["camera_id"] == "cam1" and row["size"] == len(b"hello-clip")

    got = admin_client.get(f"/api/clips/{clip_id}/file")
    assert got.status_code == 200
    assert got.content == b"hello-clip"


def test_empty_clip_rejected(admin_client, db):
    add_camera(db, "cam1")
    assert _save(admin_client, content=b"").status_code == 400


def test_save_clip_for_unknown_camera_404(admin_client):
    assert _save(admin_client, camera_id="ghost").status_code == 404


def test_delete_clip_removes_file(admin_client, db, app_module):
    add_camera(db, "cam1")
    clip_id = _save(admin_client).json()["id"]
    path = app_module.Path(db.clip(clip_id)["path"])
    assert path.exists()
    assert admin_client.delete(f"/api/clips/{clip_id}").status_code == 200
    assert db.clip(clip_id) is None
    assert not path.exists()


def test_viewer_cannot_save_clip_for_hidden_camera(viewer_client, db):
    add_camera(db, "secret", viewer_visible=0)
    assert _save(viewer_client, camera_id="secret").status_code == 404


def test_viewer_cannot_fetch_clip_from_hidden_camera(admin_client, viewer_client, db):
    # Admin saves a clip on a viewer-hidden camera; the viewer must not read it.
    add_camera(db, "secret", viewer_visible=0)
    clip_id = _save(admin_client, camera_id="secret").json()["id"]
    assert viewer_client.get(f"/api/clips/{clip_id}/file").status_code == 404
    assert viewer_client.delete(f"/api/clips/{clip_id}").status_code == 404


def test_viewer_can_fetch_clip_from_visible_camera(admin_client, viewer_client, db):
    add_camera(db, "lobby", viewer_visible=1)
    clip_id = _save(admin_client, camera_id="lobby").json()["id"]
    assert viewer_client.get(f"/api/clips/{clip_id}/file").status_code == 200
