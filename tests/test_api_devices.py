"""Devices: relays and smart switches Sentry controls over local HTTP."""

import pytest

from nvr import devices as devicelib


@pytest.fixture
def fake_relay(app_module, monkeypatch):
    """A stand-in Shelly: remembers its state, never touches the network."""
    state = {"on": False, "calls": 0}
    monkeypatch.setattr(devicelib.ShellyDriver, "set_state",
                        staticmethod(lambda d, on: (state.update(on=on, calls=state["calls"] + 1), on)[1]))
    monkeypatch.setattr(devicelib.ShellyDriver, "get_state",
                        staticmethod(lambda d: state["on"]))
    monkeypatch.setattr(devicelib.ShellyDriver, "identify",
                        staticmethod(lambda d: {"model": "S1G4", "firmware": "1.0.0"}))
    return state


def _add(client, **over):
    body = {"name": "Porch light", "host": "192.168.1.50", "driver": "shelly"}
    body.update(over)
    return client.post("/api/devices", json=body)


# --- CRUD ------------------------------------------------------------------

def test_add_and_list_a_device(admin_client, db):
    r = _add(admin_client)
    assert r.status_code == 200
    device_id = r.json()["id"]
    listed = admin_client.get("/api/devices").json()
    assert [d["id"] for d in listed["devices"]] == [device_id]
    assert any(d["value"] == "shelly" for d in listed["drivers"])


def test_device_password_is_never_listed(admin_client, db):
    _add(admin_client, password="s3cret")
    body = admin_client.get("/api/devices").text
    assert "s3cret" not in body


def test_name_and_host_are_required(admin_client):
    assert _add(admin_client, name="  ").status_code == 400
    assert _add(admin_client, host="").status_code == 400


def test_unknown_driver_is_rejected(admin_client):
    assert _add(admin_client, driver="telepathy").status_code == 400


def test_ids_do_not_collide(admin_client):
    first = _add(admin_client).json()["id"]
    second = _add(admin_client).json()["id"]
    assert first != second


def test_update_and_delete(admin_client, db):
    device_id = _add(admin_client).json()["id"]
    assert admin_client.patch(f"/api/devices/{device_id}",
                              json={"name": "Renamed", "enabled": False}).status_code == 200
    assert db.device(device_id)["name"] == "Renamed"
    assert db.device(device_id)["enabled"] == 0
    assert admin_client.delete(f"/api/devices/{device_id}").status_code == 200
    assert db.device(device_id) is None


def test_bad_channel_is_a_400_not_a_500(admin_client):
    device_id = _add(admin_client).json()["id"]
    assert admin_client.patch(f"/api/devices/{device_id}",
                              json={"channel": "left"}).status_code == 400


# --- control ---------------------------------------------------------------

def test_on_off_and_toggle(admin_client, db, fake_relay):
    device_id = _add(admin_client).json()["id"]

    assert admin_client.post(f"/api/devices/{device_id}/state",
                             json={"state": "on"}).json()["state"] is True
    assert fake_relay["on"] is True

    assert admin_client.post(f"/api/devices/{device_id}/state",
                             json={"state": "off"}).json()["state"] is False

    assert admin_client.post(f"/api/devices/{device_id}/state",
                             json={"state": "toggle"}).json()["state"] is True


def test_state_is_remembered_for_the_ui(admin_client, db, fake_relay):
    device_id = _add(admin_client).json()["id"]
    admin_client.post(f"/api/devices/{device_id}/state", json={"state": "on"})
    row = db.device(device_id)
    assert row["last_state"] == 1 and row["last_seen"] and row["last_error"] is None


def test_an_unreachable_device_reports_502_and_records_why(admin_client, db, monkeypatch):
    device_id = _add(admin_client).json()["id"]
    monkeypatch.setattr(devicelib.ShellyDriver, "set_state", staticmethod(
        lambda d, on: (_ for _ in ()).throw(devicelib.DeviceError("192.168.1.50: timed out"))))
    r = admin_client.post(f"/api/devices/{device_id}/state", json={"state": "on"})
    assert r.status_code == 502
    assert "timed out" in r.json()["error"]
    assert "timed out" in db.device(device_id)["last_error"]


def test_bad_state_is_rejected(admin_client, fake_relay):
    device_id = _add(admin_client).json()["id"]
    assert admin_client.post(f"/api/devices/{device_id}/state",
                             json={"state": "sideways"}).status_code == 400


def test_test_endpoint_identifies_the_device(admin_client, fake_relay):
    device_id = _add(admin_client).json()["id"]
    r = admin_client.post(f"/api/devices/{device_id}/test")
    assert r.status_code == 200
    assert r.json()["info"]["model"] == "S1G4"


def test_control_of_a_missing_device_is_404(admin_client):
    assert admin_client.post("/api/devices/ghost/state", json={"state": "on"}).status_code == 404


# --- permissions -----------------------------------------------------------

def test_viewers_cannot_see_or_control_devices(viewer_client, admin_client, fake_relay):
    device_id = _add(admin_client).json()["id"]
    assert viewer_client.get("/api/devices").status_code == 403
    assert viewer_client.post(f"/api/devices/{device_id}/state",
                              json={"state": "on"}).status_code == 403
    assert viewer_client.delete(f"/api/devices/{device_id}").status_code == 403


# --- the hook (a wall switch calling Sentry) -------------------------------

def test_hook_controls_a_device_without_a_session(client, admin_client, db, fake_relay):
    device_id = _add(admin_client).json()["id"]
    token = admin_client.get("/api/automation/token").json()["token"]
    r = client.get(f"/api/hook/devices/{device_id}/state?state=on&token={token}")
    assert r.status_code == 200 and r.json()["state"] is True
    assert fake_relay["on"] is True


def test_hook_rejects_a_bad_token(client, admin_client, fake_relay):
    device_id = _add(admin_client).json()["id"]
    admin_client.get("/api/automation/token")
    r = client.get(f"/api/hook/devices/{device_id}/state?state=on&token=wrong")
    assert r.status_code == 403
    assert fake_relay["calls"] == 0


def test_hook_ignores_a_disabled_device(client, admin_client, db, fake_relay):
    device_id = _add(admin_client).json()["id"]
    admin_client.patch(f"/api/devices/{device_id}", json={"enabled": False})
    token = admin_client.get("/api/automation/token").json()["token"]
    r = client.get(f"/api/hook/devices/{device_id}/state?state=on&token={token}")
    assert r.status_code == 404
    assert fake_relay["calls"] == 0


# --- driver behaviour ------------------------------------------------------

def test_toggle_defaults_to_on_when_state_is_unknown(monkeypatch):
    """A button should still do something if the relay won't report itself."""
    monkeypatch.setattr(devicelib.ShellyDriver, "get_state", staticmethod(lambda d: None))
    seen = {}
    monkeypatch.setattr(devicelib.ShellyDriver, "set_state",
                        staticmethod(lambda d, on: seen.setdefault("on", on) or on))
    devicelib.toggle({"driver": "shelly", "host": "x", "channel": 0})
    assert seen["on"] is True
