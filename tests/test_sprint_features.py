"""Coverage for the features built during the Aug 2026 sprint, which shipped
without tests: the automation hook, crop virtual cameras, unified grid ordering,
and per-camera storage targeting.
"""

import pytest

from conftest import add_camera


# --- automation hook (token-authed, session-exempt) ------------------------

@pytest.fixture
def light(app_module, monkeypatch):
    """Capture set_light calls instead of reaching for a real camera."""
    calls = []
    monkeypatch.setattr(app_module.camera_control, "set_light",
                        lambda camera, on: calls.append(on))
    monkeypatch.setattr(app_module.camera_control, "get_controls",
                        lambda camera: {"light": False, "night_vision": None})
    return calls


def _token(admin_client):
    return admin_client.get("/api/automation/token").json()["token"]


def test_hook_rejects_a_missing_token(client, admin_client, db, light):
    add_camera(db, "front")
    _token(admin_client)
    r = client.get("/api/hook/cameras/front/light?state=on")
    assert r.status_code == 403
    assert light == []


def test_hook_rejects_a_wrong_token(client, admin_client, db, light):
    add_camera(db, "front")
    _token(admin_client)
    r = client.get("/api/hook/cameras/front/light?state=on&token=nope")
    assert r.status_code == 403
    assert light == []


def test_hook_works_without_a_session(client, admin_client, db, light):
    """The whole point: a switch or Home Assistant has no login."""
    add_camera(db, "front")
    tok = _token(admin_client)
    r = client.get(f"/api/hook/cameras/front/light?state=on&token={tok}")
    assert r.status_code == 200 and r.json()["light"] is True
    assert light == [True]


def test_hook_toggle_uses_current_state(client, admin_client, db, light):
    add_camera(db, "front")
    tok = _token(admin_client)
    # get_controls reports light=False, so a toggle must turn it on.
    r = client.get(f"/api/hook/cameras/front/light?state=toggle&token={tok}")
    assert r.json()["light"] is True


def test_hook_rejects_an_unknown_state(client, admin_client, db, light):
    add_camera(db, "front")
    tok = _token(admin_client)
    r = client.get(f"/api/hook/cameras/front/light?state=explode&token={tok}")
    assert r.status_code == 400
    assert light == []


def test_hook_404s_for_an_unknown_camera(client, admin_client, db, light):
    tok = _token(admin_client)
    r = client.get(f"/api/hook/cameras/ghost/light?state=on&token={tok}")
    assert r.status_code == 404


def test_token_is_not_readable_without_a_login(client, app_module, db):
    # Seed a user first: with an empty user table the app is in first-run setup
    # mode and redirects everything to /setup, which would mask the real answer.
    from conftest import make_user
    make_user(app_module, db, "admin", "password123")
    r = client.get("/api/automation/token", follow_redirects=False)
    assert r.status_code == 401


def test_viewer_cannot_read_or_rotate_the_token(viewer_client):
    assert viewer_client.get("/api/automation/token").status_code == 403
    assert viewer_client.post("/api/automation/token").status_code == 403


def test_regenerating_the_token_revokes_the_old_one(client, admin_client, db, light):
    add_camera(db, "front")
    old = _token(admin_client)
    new = admin_client.post("/api/automation/token").json()["token"]
    assert new != old

    assert client.get(f"/api/hook/cameras/front/light?state=on&token={old}").status_code == 403
    assert client.get(f"/api/hook/cameras/front/light?state=on&token={new}").status_code == 200


# --- crop virtual cameras --------------------------------------------------

def test_crop_virtual_round_trips_its_rectangle(admin_client, db):
    add_camera(db, "wide")
    rect = {"x": 0.25, "y": 0.1, "w": 0.4, "h": 0.35}
    r = admin_client.post("/api/cameras/wide/virtual",
                          json={"name": "Front door", "mode": "crop", "calib": rect})
    assert r.status_code == 200
    vid = r.json()["id"]

    got = admin_client.get(f"/api/virtual/{vid}").json()
    assert got["mode"] == "crop"
    assert got["crop"] == rect


def test_virtual_defaults_to_fisheye_mode(admin_client, db):
    add_camera(db, "dome", fisheye=1)
    vid = admin_client.post("/api/cameras/dome/virtual",
                            json={"name": "North", "yaw": 0.5}).json()["id"]
    got = admin_client.get(f"/api/virtual/{vid}").json()
    assert got["mode"] == "fisheye"
    assert got["crop"] == {}


def test_unknown_virtual_mode_is_rejected(admin_client, db):
    add_camera(db, "wide")
    r = admin_client.post("/api/cameras/wide/virtual",
                          json={"name": "Bad", "mode": "hologram"})
    assert r.status_code == 400


def test_virtual_requires_a_name(admin_client, db):
    add_camera(db, "wide")
    r = admin_client.post("/api/cameras/wide/virtual", json={"name": "  ", "mode": "crop"})
    assert r.status_code == 400


# --- unified camera + virtual ordering -------------------------------------

def test_cameras_and_virtuals_share_one_order(admin_client, db):
    add_camera(db, "a", "A")
    add_camera(db, "b", "B")
    vid = db.add_virtual_camera("a", "Virtual", 0.0, 0.0, 1.5, "{}")

    r = admin_client.post("/api/cameras/order",
                          json={"order": [f"vcam:{vid}", "cam:b", "cam:a"]})
    assert r.status_code == 200 and r.json()["count"] == 3

    assert db.virtual_camera(vid)["sort_order"] == 0
    assert db.camera("b")["sort_order"] == 1
    assert db.camera("a")["sort_order"] == 2


def test_order_ignores_unknown_tokens(admin_client, db):
    add_camera(db, "a", "A")
    r = admin_client.post("/api/cameras/order",
                          json={"order": ["cam:ghost", "vcam:999", "cam:a"]})
    assert r.status_code == 200 and r.json()["count"] == 1
    assert db.camera("a")["sort_order"] == 0


def test_order_rejects_a_non_list(admin_client, db):
    assert admin_client.post("/api/cameras/order", json={"order": "cam:a"}).status_code == 400


def test_viewer_cannot_reorder(viewer_client, db):
    add_camera(db, "a", "A")
    assert viewer_client.post("/api/cameras/order",
                              json={"order": ["cam:a"]}).status_code == 403


def test_new_camera_sorts_after_existing_ones(db):
    add_camera(db, "a", "A")
    db.set_camera_sort([(5, "a")])
    add_camera(db, "b", "B")
    assert db.camera("b")["sort_order"] > db.camera("a")["sort_order"]


# --- per-camera storage target ---------------------------------------------

def test_preferred_volume_round_trips(admin_client, db):
    add_camera(db, "front")
    assert admin_client.patch("/api/cameras/front",
                              json={"preferred_volume": "/mnt/nas"}).status_code == 200
    assert db.camera("front")["preferred_volume"] == "/mnt/nas"


def test_empty_preferred_volume_clears_the_pin(admin_client, db):
    add_camera(db, "front", preferred_volume="/mnt/nas")
    admin_client.patch("/api/cameras/front", json={"preferred_volume": ""})
    assert db.camera("front")["preferred_volume"] is None


def test_recorder_falls_back_when_the_pinned_volume_is_gone(app_module, db):
    """A pin must never stop a camera recording — an absent drive falls back to
    the pool rather than failing."""
    from nvr.recorder import RecordingService
    svc = RecordingService(app_module.cfg, db, app_module.go2rtc)
    camera = {"id": "front", "preferred_volume": "/definitely/not/mounted"}
    fallback = app_module.cfg.storage.recordings_dir
    assert svc._volume_for(camera, fallback) == fallback


def test_recorder_honours_a_mounted_pin(app_module, db):
    from nvr.recorder import RecordingService
    svc = RecordingService(app_module.cfg, db, app_module.go2rtc)
    vol = app_module.cfg.storage.volumes[0]
    camera = {"id": "front", "preferred_volume": str(vol.path)}
    assert svc._volume_for(camera, None) == vol.path


# --- navigation: Clips moved from a nav tab to a button on Cameras ----------

def test_clips_is_reachable_from_the_cameras_page(admin_client, db):
    from conftest import add_camera
    add_camera(db)
    body = admin_client.get("/cameras").text
    assert 'href="/clips"' in body


def test_clips_stays_reachable_with_no_cameras(admin_client):
    """Saved clips outlive the cameras they came from, so the button must not
    be conditional on having any."""
    assert 'href="/clips"' in admin_client.get("/cameras").text


def test_the_clips_page_offers_a_way_back(admin_client):
    """It no longer has a nav tab, so without this it is a dead end."""
    assert 'href="/cameras"' in admin_client.get("/clips").text


def test_the_cameras_tab_highlights_while_on_clips(admin_client):
    """Clips sits under Cameras in the nav; the tab should not go dark."""
    body = admin_client.get("/clips").text
    assert 'href="/cameras" class="active"' in body


# --- dashboard visibility is independent of grid visibility -----------------

def test_a_camera_can_be_on_the_grid_but_off_the_dashboard(admin_client, db):
    from conftest import add_camera
    add_camera(db, "front", "Front Door")
    admin_client.patch("/api/cameras/front", json={"show_on_dashboard": False})
    assert "Front Door" in admin_client.get("/cameras").text
    assert "Front Door" not in admin_client.get("/").text


def test_a_camera_can_be_on_the_dashboard_but_off_the_grid(admin_client, db):
    from conftest import add_camera
    add_camera(db, "front", "Front Door")
    admin_client.patch("/api/cameras/front", json={"show_on_grid": False})
    assert "Front Door" in admin_client.get("/").text
    assert "Front Door" not in admin_client.get("/cameras").text


def test_the_two_flags_default_together(admin_client, db):
    """A fresh camera shows in both places, as it always did."""
    from conftest import add_camera
    add_camera(db, "front", "Front Door")
    row = db.camera("front")
    assert row["show_on_grid"] == 1 and row["show_on_dashboard"] == 1


def test_the_migration_backfills_from_the_old_flag(db):
    """Upgrading must not silently put every hidden camera on the dashboard —
    a plain DEFAULT 1 would have done exactly that."""
    from conftest import add_camera
    add_camera(db, "hidden", "Hidden Cam", show_on_grid=0, show_on_dashboard=0)
    db.execute("UPDATE cameras SET show_on_dashboard = show_on_grid")
    assert db.camera("hidden")["show_on_dashboard"] == 0


def test_a_virtual_camera_has_its_own_dashboard_flag(admin_client, db):
    from conftest import add_camera
    add_camera(db, "front", "Front Door")
    vid = db.add_virtual_camera(
        parent_id="front", name="Porch View", yaw=0.0, pitch=0.0, fov=90.0,
        calib="{}", mode="crop",
    )
    # PUT, not PATCH — asserted, so calling the wrong verb fails loudly
    # instead of leaving the row untouched and the test passing by luck.
    r = admin_client.put(f"/api/virtual/{vid}", json={"show_on_dashboard": False})
    assert r.status_code == 200, r.text
    assert db.one("SELECT show_on_dashboard FROM virtual_cameras WHERE id = ?",
                  (vid,))["show_on_dashboard"] == 0


# --- mobile navigation ------------------------------------------------------

def test_the_nav_has_a_toggle_for_narrow_screens(admin_client):
    """Seven destinations do not fit a phone header; the nav collapses behind
    a button whose visibility is decided by CSS, not by JS."""
    body = admin_client.get("/").text
    assert 'id="nav-toggle"' in body
    assert 'aria-controls="main-nav"' in body
    assert 'id="main-nav"' in body


def test_the_toggle_starts_closed(admin_client):
    assert 'aria-expanded="false"' in admin_client.get("/").text
