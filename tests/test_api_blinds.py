"""Blinds: rooms, hubs, coverings, groups and schedules.

No real UDP here — the protocol layer has its own tests. What matters at this
level is who may do what, that a group action reports honestly when half of it
fails, and that losing a room never loses the hardware behind it.
"""

import pytest

from nvr import shades


@pytest.fixture
def hub(db):
    db.add_shade_hub(id="hub1", name="Shade hub", host="10.0.0.5",
                     api_key=None, token="T" * 16, protocol="0.9")
    return "hub1"


@pytest.fixture
def room(db):
    return db.add_room("Bedroom")


def _covering(db, cid, hub_id="hub1", room_id=None, layer="sheer", **over):
    fields = dict(id=cid, hub_id=hub_id, name=cid, layer=layer,
                  device_type="10000000")
    if room_id is not None:
        fields["room_id"] = room_id
    fields.update(over)
    db.add_covering(**fields)
    return cid


@pytest.fixture
def moves(monkeypatch, app_module):
    """Record every move instead of transmitting one."""
    sent = []

    def set_position(host, mac, device_type, position, *, api_key, hub_token):
        sent.append((mac, "position", position))
        return {}

    def operate(host, mac, device_type, action, *, api_key, hub_token):
        sent.append((mac, "action", action))
        return {}

    monkeypatch.setattr(app_module.shadelib, "set_position", set_position)
    monkeypatch.setattr(app_module.shadelib, "operate", operate)
    return sent


# --- page and payload ------------------------------------------------------

def test_the_blinds_page_renders(admin_client):
    r = admin_client.get("/blinds")
    assert r.status_code == 200
    assert "blinds.js" in r.text


def test_the_payload_carries_everything_the_page_needs(admin_client, hub, room, db):
    _covering(db, "c1", room_id=room, layer="blackout")
    data = admin_client.get("/api/blinds").json()
    assert [h["id"] for h in data["hubs"]] == ["hub1"]
    assert [r["name"] for r in data["rooms"]] == ["Bedroom"]
    assert data["coverings"][0]["layer_label"] == "Blackout"
    assert data["can_edit"] is True


def test_a_hub_never_leaks_its_key(admin_client, db):
    db.add_shade_hub(id="h", name="H", host="10.0.0.5",
                     api_key="12ab345c-d67e-8f", token="T" * 16)
    payload = admin_client.get("/api/blinds").json()["hubs"][0]
    assert "api_key" not in payload and "token" not in payload
    # Whether a key exists is not a secret, and the UI needs it.
    assert payload["has_key"] is True


def test_battery_is_reported_in_volts_and_an_estimated_percent(admin_client, hub, db):
    _covering(db, "c1")
    db.update_covering("c1", battery_mv=784)
    covering = admin_client.get("/api/blinds").json()["coverings"][0]
    assert covering["battery_volts"] == 7.84
    assert covering["battery_percent"] == 77


# --- rooms -----------------------------------------------------------------

def test_admin_adds_a_room(admin_client):
    r = admin_client.post("/api/blinds/rooms", json={"name": "Living Room"})
    assert r.status_code == 200 and r.json()["name"] == "Living Room"


def test_a_room_needs_a_name(admin_client):
    assert admin_client.post("/api/blinds/rooms", json={"name": " "}).status_code == 400


def test_a_viewer_cannot_add_a_room(viewer_client):
    assert viewer_client.post("/api/blinds/rooms", json={"name": "X"}).status_code == 403


def test_deleting_a_room_keeps_its_coverings(admin_client, db, hub, room):
    """Losing a room must never lose the hardware — an unassigned covering can
    be re-homed, whereas a deleted one has to be rediscovered from the hub."""
    _covering(db, "c1", room_id=room)
    assert admin_client.delete(f"/api/blinds/rooms/{room}").status_code == 200
    assert db.covering("c1") is not None
    assert db.covering("c1")["room_id"] is None


def test_deleting_a_room_takes_its_group_schedules(admin_client, db, room):
    db.add_schedule(action="cover", covering_room_id=room, days=127,
                    start_min=420, end_min=480, value="0")
    admin_client.delete(f"/api/blinds/rooms/{room}")
    assert db.covering_schedules() == []


def test_renaming_a_room(admin_client, room):
    r = admin_client.patch(f"/api/blinds/rooms/{room}", json={"name": "Den"})
    assert r.status_code == 200 and r.json()["name"] == "Den"


# --- hubs ------------------------------------------------------------------

def test_adding_a_hub_pulls_in_its_coverings(admin_client, monkeypatch, app_module, db):
    monkeypatch.setattr(app_module.shadelib, "device_list", lambda host: {
        "mac": "aabbccddeeff", "protocol": "0.9", "token": "T" * 16,
        "devices": [
            {"mac": "aabbccddeeff0001", "deviceType": "10000000"},
            {"mac": "aabbccddeeff0002", "deviceType": "10000000"},
        ],
    })
    r = admin_client.post("/api/blinds/hubs", json={"host": "192.168.1.50"})
    assert r.status_code == 200
    assert len(r.json()["added"]) == 2
    assert len(db.coverings()) == 2


def test_a_hub_that_does_not_answer_is_a_502(admin_client, monkeypatch, app_module):
    def boom(host):
        raise shades.ShadeError("no answer to GetDeviceList")

    monkeypatch.setattr(app_module.shadelib, "device_list", boom)
    r = admin_client.post("/api/blinds/hubs", json={"host": "192.168.1.99"})
    assert r.status_code == 502
    assert "no answer" in r.json()["error"]


def test_a_hub_needs_an_address(admin_client):
    assert admin_client.post("/api/blinds/hubs", json={}).status_code == 400


def test_refreshing_never_removes_a_covering_that_went_quiet(
        admin_client, monkeypatch, app_module, db, hub):
    """A motor out of radio range vanishes from the hub's list. Deleting it
    would throw away its name and room assignment over a dropped packet."""
    _covering(db, "c1", room_id=None)
    db.update_covering("c1", name="Bedroom West")
    monkeypatch.setattr(app_module.shadelib, "device_list", lambda host: {
        "mac": "hub1", "protocol": "0.9", "token": "T" * 16, "devices": [],
    })
    monkeypatch.setattr(app_module.shadelib, "read_device",
                        lambda *a, **k: {"currentPosition": 3, "wirelessMode": 1})
    assert admin_client.post("/api/blinds/hubs/hub1/refresh").status_code == 200
    assert db.covering("c1")["name"] == "Bedroom West"


def test_refresh_polls_and_stores_telemetry(
        admin_client, monkeypatch, app_module, db, hub):
    _covering(db, "c1")
    monkeypatch.setattr(app_module.shadelib, "device_list", lambda host: {
        "mac": "hub1", "protocol": "0.9", "token": "U" * 16, "devices": [],
    })
    monkeypatch.setattr(app_module.shadelib, "read_device", lambda *a, **k: {
        "currentPosition": 42, "batteryLevel": 770, "RSSI": -98, "wirelessMode": 1,
    })
    admin_client.post("/api/blinds/hubs/hub1/refresh")
    row = db.covering("c1")
    assert row["last_position"] == 42
    assert row["battery_mv"] == 770
    assert row["rssi"] == -98
    assert row["bidirectional"] == 1
    # The token rotates when the hub restarts, so it is refreshed not cached.
    assert db.shade_hub("hub1")["token"] == "U" * 16


def test_one_dead_motor_does_not_stop_the_others_being_polled(
        admin_client, monkeypatch, app_module, db, hub):
    _covering(db, "c1")
    _covering(db, "c2")

    def read(host, mac, device_type):
        if mac == "c1":
            raise shades.ShadeError("out of range")
        return {"currentPosition": 8, "wirelessMode": 1}

    monkeypatch.setattr(app_module.shadelib, "device_list", lambda host: {
        "mac": "hub1", "token": "T" * 16, "devices": [],
    })
    monkeypatch.setattr(app_module.shadelib, "read_device", read)
    r = admin_client.post("/api/blinds/hubs/hub1/refresh")
    assert r.json()["polled"] == 1
    assert db.covering("c1")["last_error"] == "out of range"
    assert db.covering("c2")["last_position"] == 8


def test_a_key_of_the_wrong_length_is_refused_with_advice(admin_client, hub):
    r = admin_client.patch("/api/blinds/hubs/hub1", json={"api_key": "12ab345cd67e8f"})
    assert r.status_code == 400
    assert "dashes" in r.json()["error"]


def test_a_key_of_the_right_length_is_accepted(admin_client, hub, db):
    r = admin_client.patch("/api/blinds/hubs/hub1",
                           json={"api_key": "12ab345c-d67e-8f"})
    assert r.status_code == 200
    assert db.shade_hub("hub1")["api_key"] == "12ab345c-d67e-8f"


def test_deleting_a_hub_takes_its_coverings_and_their_schedules(
        admin_client, db, hub):
    _covering(db, "c1")
    db.add_schedule(action="cover", covering_id="c1", days=127,
                    start_min=420, end_min=480, value="0")
    admin_client.delete("/api/blinds/hubs/hub1")
    assert db.coverings() == []
    assert db.covering_schedules() == []


def test_a_viewer_cannot_touch_hubs(viewer_client, hub):
    assert viewer_client.post("/api/blinds/hubs", json={"host": "x"}).status_code == 403
    assert viewer_client.delete("/api/blinds/hubs/hub1").status_code == 403
    assert viewer_client.post("/api/blinds/hubs/discover").status_code == 403


# --- coverings -------------------------------------------------------------

def test_editing_a_covering(admin_client, db, hub, room):
    _covering(db, "c1")
    r = admin_client.patch("/api/blinds/coverings/c1", json={
        "name": "West window", "layer": "blackout", "room_id": room,
    })
    assert r.status_code == 200
    row = db.covering("c1")
    assert row["name"] == "West window"
    assert row["layer"] == "blackout"
    assert row["room_id"] == room


def test_an_unknown_layer_is_refused(admin_client, db, hub):
    _covering(db, "c1")
    r = admin_client.patch("/api/blinds/coverings/c1", json={"layer": "frosted"})
    assert r.status_code == 400


def test_an_unknown_room_is_refused(admin_client, db, hub):
    _covering(db, "c1")
    assert admin_client.patch("/api/blinds/coverings/c1",
                              json={"room_id": 9999}).status_code == 404


def test_a_covering_can_be_unassigned_from_its_room(admin_client, db, hub, room):
    _covering(db, "c1", room_id=room)
    admin_client.patch("/api/blinds/coverings/c1", json={"room_id": None})
    assert db.covering("c1")["room_id"] is None


def test_a_viewer_cannot_rename_a_covering(viewer_client, db, hub):
    _covering(db, "c1")
    assert viewer_client.patch("/api/blinds/coverings/c1",
                               json={"name": "X"}).status_code == 403


# --- moving things ---------------------------------------------------------

def test_moving_one_covering_to_a_position(admin_client, db, hub, moves):
    _covering(db, "c1")
    r = admin_client.post("/api/blinds/coverings/c1/command", json={"position": 60})
    assert r.status_code == 200
    assert moves == [("c1", "position", 60)]
    assert db.covering("c1")["last_position"] == 60


def test_open_and_close_record_the_extremes(admin_client, db, hub, moves):
    _covering(db, "c1")
    admin_client.post("/api/blinds/coverings/c1/command", json={"action": "close"})
    assert db.covering("c1")["last_position"] == 100
    admin_client.post("/api/blinds/coverings/c1/command", json={"action": "open"})
    assert db.covering("c1")["last_position"] == 0


def test_stop_does_not_claim_a_position(admin_client, db, hub, moves):
    """A stop lands somewhere unknown; asserting a number would be a lie."""
    _covering(db, "c1")
    db.update_covering("c1", last_position=40)
    admin_client.post("/api/blinds/coverings/c1/command", json={"action": "stop"})
    assert db.covering("c1")["last_position"] == 40


def test_a_viewer_may_move_a_covering(viewer_client, db, hub, moves):
    """Opening a blind is closer to switching on a lamp than to reconfiguring
    the NVR. A household where only one person can raise the shades is not a
    feature."""
    _covering(db, "c1")
    assert viewer_client.post("/api/blinds/coverings/c1/command",
                              json={"action": "open"}).status_code == 200


@pytest.mark.parametrize("body", [
    {"position": 101}, {"position": -1}, {"position": "up"},
    {"action": "wiggle"}, {},
])
def test_a_nonsense_command_is_rejected(admin_client, db, hub, moves, body):
    _covering(db, "c1")
    assert admin_client.post("/api/blinds/coverings/c1/command",
                             json=body).status_code == 400


def test_a_refused_move_is_a_502_and_is_remembered(
        admin_client, monkeypatch, app_module, db, hub):
    _covering(db, "c1")

    def boom(*a, **k):
        raise shades.ShadeError("AccessToken error")

    monkeypatch.setattr(app_module.shadelib, "operate", boom)
    r = admin_client.post("/api/blinds/coverings/c1/command", json={"action": "open"})
    assert r.status_code == 502
    assert db.covering("c1")["last_error"] == "AccessToken error"


def test_a_disabled_covering_is_inert(admin_client, db, hub, moves):
    _covering(db, "c1", enabled=0)
    assert admin_client.post("/api/blinds/coverings/c1/command",
                             json={"action": "open"}).status_code == 404
    assert moves == []


def test_a_disabled_hub_is_inert(admin_client, db, hub, moves):
    _covering(db, "c1")
    db.update_shade_hub("hub1", enabled=0)
    assert admin_client.post("/api/blinds/coverings/c1/command",
                             json={"action": "open"}).status_code == 404
    assert moves == []


# --- groups ----------------------------------------------------------------

def test_a_group_moves_only_the_named_layer(admin_client, db, hub, room, moves):
    _covering(db, "sheer1", room_id=room, layer="sheer")
    _covering(db, "black1", room_id=room, layer="blackout")
    r = admin_client.post("/api/blinds/group/command",
                          json={"room_id": room, "layer": "blackout",
                                "action": "close"})
    assert r.status_code == 200
    assert moves == [("black1", "action", "close")]


def test_a_group_with_no_layer_moves_both(admin_client, db, hub, room, moves):
    _covering(db, "sheer1", room_id=room, layer="sheer")
    _covering(db, "black1", room_id=room, layer="blackout")
    admin_client.post("/api/blinds/group/command",
                      json={"room_id": room, "action": "close"})
    assert {m[0] for m in moves} == {"sheer1", "black1"}


def test_a_group_with_no_room_is_the_whole_house(admin_client, db, hub, room, moves):
    _covering(db, "a", room_id=room, layer="sheer")
    _covering(db, "b", room_id=None, layer="sheer")
    admin_client.post("/api/blinds/group/command",
                      json={"layer": "sheer", "action": "open"})
    assert {m[0] for m in moves} == {"a", "b"}


def test_a_group_skips_disabled_coverings(admin_client, db, hub, room, moves):
    _covering(db, "on", room_id=room)
    _covering(db, "off", room_id=room, enabled=0)
    admin_client.post("/api/blinds/group/command",
                      json={"room_id": room, "action": "open"})
    assert [m[0] for m in moves] == ["on"]


def test_a_group_reports_partial_failure_honestly(
        admin_client, monkeypatch, app_module, db, hub, room):
    """Partial success is the normal failure on a weak 433 MHz link. Saying
    "done" when half the room did not move would be a lie."""
    _covering(db, "good", room_id=room)
    _covering(db, "bad", room_id=room)

    def operate(host, mac, device_type, action, *, api_key, hub_token):
        if mac == "bad":
            raise shades.ShadeError("out of range")
        return {}

    monkeypatch.setattr(app_module.shadelib, "operate", operate)
    r = admin_client.post("/api/blinds/group/command",
                          json={"room_id": room, "action": "close"})
    assert r.status_code == 200
    body = r.json()
    assert body["moved"] == ["good"]
    assert body["failed"][0]["id"] == "bad"
    assert "out of range" in body["failed"][0]["error"]


def test_a_group_that_matches_nothing_is_a_404(admin_client, hub, room, moves):
    assert admin_client.post("/api/blinds/group/command",
                             json={"room_id": room, "action": "open"}).status_code == 404


def test_a_viewer_may_move_a_group(viewer_client, db, hub, room, moves):
    _covering(db, "c1", room_id=room)
    assert viewer_client.post("/api/blinds/group/command",
                              json={"room_id": room, "action": "open"}).status_code == 200


# --- schedules -------------------------------------------------------------

def test_scheduling_a_group(admin_client, db, hub, room):
    r = admin_client.post("/api/blinds/schedules", json={
        "room_id": room, "layer": "blackout", "position": 100,
        "days": 127, "start_min": 1260, "end_min": 1320,
    })
    assert r.status_code == 200
    assert r.json()["layer"] == "blackout"
    assert db.covering_schedules()[0]["value"] == "100"


def test_scheduling_one_covering_clears_the_group_selector(
        admin_client, db, hub, room):
    """Naming a covering AND a group would be ambiguous; the id wins."""
    _covering(db, "c1", room_id=room)
    admin_client.post("/api/blinds/schedules", json={
        "covering_id": "c1", "room_id": room, "layer": "sheer", "position": 0,
        "days": 127, "start_min": 420, "end_min": 480,
    })
    row = db.covering_schedules()[0]
    assert row["covering_id"] == "c1"
    assert row["covering_room_id"] is None and row["covering_layer"] is None


@pytest.mark.parametrize("over", [
    {"position": 101}, {"position": "down"}, {"position": None},
    {"days": 0}, {"days": 999}, {"start_min": 1500},
    {"start_min": 480, "end_min": 480},
])
def test_a_bad_schedule_is_rejected(admin_client, hub, room, over):
    body = {"room_id": room, "position": 50, "days": 127,
            "start_min": 420, "end_min": 480, **over}
    assert admin_client.post("/api/blinds/schedules", json=body).status_code == 400


def test_scheduling_an_unknown_covering_is_a_404(admin_client, hub):
    assert admin_client.post("/api/blinds/schedules", json={
        "covering_id": "ghost", "position": 0, "days": 127,
        "start_min": 420, "end_min": 480,
    }).status_code == 404


def test_a_viewer_cannot_schedule(viewer_client, hub, room):
    assert viewer_client.post("/api/blinds/schedules", json={
        "room_id": room, "position": 0, "days": 127,
        "start_min": 420, "end_min": 480,
    }).status_code == 403


def test_disabling_and_deleting_a_schedule(admin_client, db, hub, room):
    sid = admin_client.post("/api/blinds/schedules", json={
        "room_id": room, "position": 0, "days": 127,
        "start_min": 420, "end_min": 480,
    }).json()["id"]
    assert admin_client.patch(f"/api/blinds/schedules/{sid}",
                              json={"enabled": False}).status_code == 200
    assert db.covering_schedules()[0]["enabled"] == 0
    assert admin_client.delete(f"/api/blinds/schedules/{sid}").status_code == 200
    assert db.covering_schedules() == []


def test_the_schedule_routes_do_not_touch_camera_schedules(
        admin_client, db, hub, add_camera_fixture):
    """A camera schedule must not be reachable through the blinds endpoints."""
    sid = db.add_schedule(camera_id="front", action="record", days=127,
                          start_min=0, end_min=60)
    assert admin_client.delete(f"/api/blinds/schedules/{sid}").status_code == 404
    assert admin_client.patch(f"/api/blinds/schedules/{sid}",
                              json={"enabled": False}).status_code == 404


@pytest.fixture
def add_camera_fixture(db):
    from conftest import add_camera
    return add_camera(db)


# --- the automation hook ---------------------------------------------------

def test_the_hook_needs_a_token(client, app_module, db, hub):
    """Seed a user first: with an empty user table every path 303s to /setup,
    and a followed redirect reads as a 200 that looks like an auth hole."""
    from conftest import make_user
    make_user(app_module, db)
    _covering(db, "c1")
    assert client.get("/api/hook/blinds/group?action=open",
                      follow_redirects=False).status_code == 403
    assert client.get("/api/hook/blinds/group?token=wrong&action=open",
                      follow_redirects=False).status_code == 403


def test_the_hook_moves_a_group(client, admin_client, db, hub, room, moves):
    _covering(db, "c1", room_id=room, layer="blackout")
    token = admin_client.get("/api/automation/token").json()["token"]
    r = client.get(f"/api/hook/blinds/group?token={token}"
                   f"&room_id={room}&layer=blackout&position=100")
    assert r.status_code == 200
    assert moves == [("c1", "position", 100)]


def test_the_hook_rejects_a_bad_layer(client, admin_client, db, hub, moves):
    _covering(db, "c1")
    token = admin_client.get("/api/automation/token").json()["token"]
    r = client.get(f"/api/hook/blinds/group?token={token}&layer=frosted&action=open")
    assert r.status_code == 400
    assert moves == []


# --- refresh actually reaches the hub --------------------------------------

def test_refresh_picks_up_a_newly_paired_covering(
        admin_client, monkeypatch, app_module, db, hub):
    """The regression that hid blind #10: pairing a shade at the hub must show
    up in Sentry when the page's Refresh is used."""
    _covering(db, "aabbccddeeff0001")
    monkeypatch.setattr(app_module.shadelib, "device_list", lambda host: {
        "mac": "hub1", "protocol": "0.9", "token": "T" * 16,
        "devices": [
            {"mac": "aabbccddeeff0001", "deviceType": "10000000"},
            {"mac": "aabbccddeeff000a", "deviceType": "10000000"},
        ],
    })
    monkeypatch.setattr(app_module.shadelib, "read_device",
                        lambda *a, **k: {"currentPosition": 0, "wirelessMode": 1})
    r = admin_client.post("/api/blinds/hubs/hub1/refresh")
    assert r.status_code == 200
    assert r.json()["added"] == ["aabbccddeeff000a"]
    assert {c["id"] for c in db.coverings()} == {
        "aabbccddeeff0001", "aabbccddeeff000a"
    }


def test_refresh_is_idempotent(admin_client, monkeypatch, app_module, db, hub):
    """Pressing it twice must not duplicate anything."""
    monkeypatch.setattr(app_module.shadelib, "device_list", lambda host: {
        "mac": "hub1", "token": "T" * 16,
        "devices": [{"mac": "aa01", "deviceType": "10000000"}],
    })
    monkeypatch.setattr(app_module.shadelib, "read_device",
                        lambda *a, **k: {"currentPosition": 0})
    admin_client.post("/api/blinds/hubs/hub1/refresh")
    second = admin_client.post("/api/blinds/hubs/hub1/refresh")
    assert second.json()["added"] == []
    assert len(db.coverings()) == 1


def test_a_viewer_cannot_re_enumerate_a_hub(viewer_client, hub):
    """The page-head Refresh falls back to a plain reload for viewers."""
    assert viewer_client.post("/api/blinds/hubs/hub1/refresh").status_code == 403
