"""Virtual-camera API: CRUD (admin) and read access (viewer)."""

from conftest import add_camera


def test_admin_creates_and_reads_virtual(admin_client, db):
    add_camera(db, "fish", fisheye=1)
    r = admin_client.post("/api/cameras/fish/virtual", json={
        "name": "Front Door", "yaw": 1.2, "pitch": -0.3, "fov": 1.4,
        "calib": {"center": [0.5, 0.5]},
    })
    assert r.status_code == 200
    vid = r.json()["id"]

    got = admin_client.get(f"/api/virtual/{vid}")
    assert got.status_code == 200
    body = got.json()
    assert body["name"] == "Front Door"
    assert body["yaw"] == 1.2
    assert body["calib"] == {"center": [0.5, 0.5]}


def test_virtual_requires_name(admin_client, db):
    add_camera(db, "fish", fisheye=1)
    r = admin_client.post("/api/cameras/fish/virtual", json={"name": "  "})
    assert r.status_code == 400


def test_virtual_unknown_parent_404(admin_client):
    r = admin_client.post("/api/cameras/ghost/virtual", json={"name": "x"})
    assert r.status_code == 404


def test_update_and_delete_virtual(admin_client, db):
    add_camera(db, "fish", fisheye=1)
    vid = admin_client.post("/api/cameras/fish/virtual",
                            json={"name": "V"}).json()["id"]
    assert admin_client.put(f"/api/virtual/{vid}",
                            json={"name": "Renamed", "yaw": 2.0}).status_code == 200
    v = db.virtual_camera(vid)
    assert v["name"] == "Renamed" and v["yaw"] == 2.0
    assert admin_client.delete(f"/api/virtual/{vid}").status_code == 200
    assert db.virtual_camera(vid) is None


def test_viewer_cannot_create_or_delete_virtual(viewer_client, db):
    add_camera(db, "fish", fisheye=1)
    vid = db.add_virtual_camera("fish", "V", 0.0, 0.0, 1.57, "{}")
    assert viewer_client.post("/api/cameras/fish/virtual",
                              json={"name": "x"}).status_code == 403
    assert viewer_client.delete(f"/api/virtual/{vid}").status_code == 403


def test_viewer_reads_virtual_only_if_parent_visible(viewer_client, db):
    add_camera(db, "shown", fisheye=1, viewer_visible=1)
    add_camera(db, "hidden", fisheye=1, viewer_visible=0)
    v_ok = db.add_virtual_camera("shown", "ok", 0.0, 0.0, 1.57, "{}")
    v_no = db.add_virtual_camera("hidden", "no", 0.0, 0.0, 1.57, "{}")
    assert viewer_client.get(f"/api/virtual/{v_ok}").status_code == 200
    assert viewer_client.get(f"/api/virtual/{v_no}").status_code == 404
