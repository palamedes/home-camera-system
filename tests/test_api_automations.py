"""The automations API and the generic inbound hook.

The hook is the one endpoint in Sentry that anything on the network can drive
with only a shared secret, so its auth and its blast radius get the attention
here.
"""

import pytest


@pytest.fixture
def porch(db):
    db.add_device(id="porch", name="Porch light", driver="shelly", host="10.0.0.7")
    return "porch"


@pytest.fixture
def ran(monkeypatch, app_module):
    """Watch what an automation did, without touching a relay."""
    calls = []
    monkeypatch.setattr(app_module.devicelib, "set_state",
                        lambda device, on: (calls.append((device["id"], on)), on)[1])
    monkeypatch.setattr(app_module.devicelib, "toggle",
                        lambda device: (calls.append((device["id"], "toggle")), True)[1])
    return calls


def _make(client, **over):
    body = {
        "name": "Porch light on person",
        "trigger_kind": "event",
        "match": {"event_type": "person"},
        "actions": [{"kind": "device", "device_id": "porch", "state": "on"}],
    }
    body.update(over)
    return client.post("/api/automations", json=body)


# --- CRUD ------------------------------------------------------------------

def test_creating_one(admin_client, porch):
    r = _make(admin_client)
    assert r.status_code == 200
    body = r.json()
    assert body["slug"] == "porch-light-on-person"
    assert body["url"] == "/api/hook/run/porch-light-on-person"


def test_slugs_do_not_collide(admin_client, porch):
    first = _make(admin_client).json()["slug"]
    second = _make(admin_client).json()["slug"]
    assert first != second


def test_a_name_is_required(admin_client):
    assert _make(admin_client, name="  ").status_code == 400


@pytest.mark.parametrize("actions", [
    [], "nope", [{"kind": "wat"}],
    [{"kind": "device"}],
    [{"kind": "webhook", "url": "ftp://nope"}],
    [{"kind": "covering", "position": 200}],
])
def test_broken_actions_are_refused_at_save_time(admin_client, actions):
    """A typo must not wait until 2am and a driveway detection to surface."""
    assert _make(admin_client, actions=actions).status_code == 400


@pytest.mark.parametrize("over", [
    {"trigger_kind": "telepathy"},
    {"cooldown_seconds": -1},
    {"cooldown_seconds": "soon"},
    {"days": 999},
    {"start_min": 1500},
])
def test_bad_configuration_is_refused(admin_client, porch, over):
    assert _make(admin_client, **over).status_code == 400


def test_editing_and_deleting(admin_client, porch, db):
    aid = _make(admin_client).json()["id"]
    r = admin_client.patch(f"/api/automations/{aid}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert admin_client.delete(f"/api/automations/{aid}").status_code == 200
    assert db.automations() == []


def test_a_viewer_cannot_see_or_touch_automations(viewer_client, porch):
    """They carry the shared token and can switch the house around."""
    assert viewer_client.get("/api/automations").status_code == 403
    assert _make(viewer_client).status_code == 403


def test_the_payload_offers_what_the_editor_needs(admin_client, porch, db):
    db.add_room("Bedroom")
    data = admin_client.get("/api/automations").json()
    assert "person" in data["event_types"]
    assert [d["id"] for d in data["devices"]] == ["porch"]
    assert [r["name"] for r in data["rooms"]] == ["Bedroom"]
    assert data["token"]


# --- running ---------------------------------------------------------------

def test_running_from_the_ui(admin_client, porch, ran):
    aid = _make(admin_client).json()["id"]
    r = admin_client.post(f"/api/automations/{aid}/run")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert ran == [("porch", True)]


def test_a_run_reports_failure_rather_than_claiming_success(
        admin_client, monkeypatch, app_module, porch):
    def boom(device, on):
        raise app_module.devicelib.DeviceError("relay unreachable")

    monkeypatch.setattr(app_module.devicelib, "set_state", boom)
    aid = _make(admin_client).json()["id"]
    body = admin_client.post(f"/api/automations/{aid}/run").json()
    assert body["ok"] is False and body["errors"]


# --- the inbound hook ------------------------------------------------------

def test_the_hook_needs_a_token(client, app_module, db, admin_client, porch):
    from conftest import make_user
    make_user(app_module, db, "someone", "password123")
    slug = _make(admin_client).json()["slug"]
    assert client.get(f"/api/hook/run/{slug}",
                      follow_redirects=False).status_code == 403
    assert client.get(f"/api/hook/run/{slug}?token=wrong",
                      follow_redirects=False).status_code == 403


def test_the_hook_runs_the_automation(client, admin_client, porch, ran):
    slug = _make(admin_client).json()["slug"]
    token = admin_client.get("/api/automation/token").json()["token"]
    r = client.get(f"/api/hook/run/{slug}?token={token}")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert ran == [("porch", True)]


def test_the_hook_runs_a_hook_only_automation(client, admin_client, porch, ran):
    """The point of trigger_kind='hook': no event fires it, only the URL."""
    slug = _make(admin_client, trigger_kind="hook").json()["slug"]
    token = admin_client.get("/api/automation/token").json()["token"]
    assert client.get(f"/api/hook/run/{slug}?token={token}").status_code == 200
    assert ran == [("porch", True)]


def test_the_hook_will_not_run_a_disabled_automation(
        client, admin_client, porch, ran):
    """Disabling must actually disarm it, including via the URL."""
    body = _make(admin_client).json()
    admin_client.patch(f"/api/automations/{body['id']}", json={"enabled": False})
    token = admin_client.get("/api/automation/token").json()["token"]
    r = client.get(f"/api/hook/run/{body['slug']}?token={token}")
    assert r.status_code == 404
    assert ran == []


def test_an_unknown_slug_is_a_404_not_a_hint(client, admin_client, porch):
    token = admin_client.get("/api/automation/token").json()["token"]
    assert client.get(f"/api/hook/run/ghost?token={token}").status_code == 404


def test_the_token_header_works_too(client, admin_client, porch, ran):
    """Some callers cannot put a secret in a query string."""
    slug = _make(admin_client).json()["slug"]
    token = admin_client.get("/api/automation/token").json()["token"]
    r = client.post(f"/api/hook/run/{slug}", headers={"X-Sentry-Token": token})
    assert r.status_code == 200


# --- end to end: an event drives the house ---------------------------------

def test_an_event_fires_a_matching_automation(admin_client, app_module, porch, ran):
    """The whole point: a detection Sentry raised switches something on."""
    _make(admin_client, match={"event_type": "person", "camera_id": "drive"})
    app_module.alerts.emit(type="person", camera_id="drive", camera_name="Drive",
                           notify=False)
    service = app_module.automations
    assert service._queue.qsize() == 1
    automation_id, context = service._queue.get_nowait()
    service.run_now(automation_id, context)
    assert ran == [("porch", True)]


def test_an_event_from_the_wrong_camera_does_nothing(admin_client, app_module, porch, ran):
    _make(admin_client, match={"event_type": "person", "camera_id": "drive"})
    app_module.alerts.emit(type="person", camera_id="porch_cam",
                           camera_name="Porch", notify=False)
    assert app_module.automations._queue.qsize() == 0


def test_a_broken_automation_never_stops_an_event_being_recorded(
        admin_client, app_module, db, monkeypatch, porch):
    """Event recording is the NVR's job; automations are a bonus on top and
    must never be able to take it down."""
    def boom(event):
        raise RuntimeError("automation engine exploded")

    monkeypatch.setattr(app_module.automations, "handle_event", boom)
    app_module.alerts.emit(type="person", camera_id="drive", notify=False)
    assert len(db.recent_events(limit=10)) == 1


# --- the page --------------------------------------------------------------

def test_the_page_renders_for_an_admin(admin_client):
    r = admin_client.get("/automations")
    assert r.status_code == 200 and "automations.js" in r.text


def test_a_viewer_is_sent_away_from_the_page(viewer_client):
    r = viewer_client.get("/automations", follow_redirects=False)
    assert r.status_code == 303
