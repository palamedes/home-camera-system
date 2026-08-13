"""Scheduling for devices: "on during this window", off outside it.

Devices reuse the camera schedule machinery (weekday mask + start/end with
midnight wrap), so these tests focus on what is different: the relay is a
network call that can fail, and a schedule must target exactly one thing.
"""

import pytest

from nvr import devices as devicelib
from nvr.scheduler import SchedulerService

MON, TUE = 0, 1
ALL_DAYS = 0b1111111


@pytest.fixture
def device(app_module, db):
    db.add_device(id="porch", name="Porch", driver="shelly", host="10.0.0.5")
    return "porch"


@pytest.fixture
def relay(device, monkeypatch):
    """A stand-in relay that records what it was told, and never uses the LAN."""
    calls = []
    monkeypatch.setattr(devicelib, "set_state",
                        lambda dev, on: calls.append((dev["id"], on)) or on)
    return calls


@pytest.fixture
def sched(app_module, db):
    return SchedulerService(app_module.cfg, db)


def test_device_turns_on_inside_its_window(db, sched, relay):
    db.add_schedule(device_id="porch", action="power", days=ALL_DAYS,
                    start_min=18 * 60, end_min=23 * 60)
    sched.apply(MON, 19 * 60)
    assert relay == [("porch", True)]


def test_device_turns_off_when_the_window_closes(db, sched, relay):
    db.add_schedule(device_id="porch", action="power", days=ALL_DAYS,
                    start_min=18 * 60, end_min=23 * 60)
    sched.apply(MON, 19 * 60)
    sched.apply(MON, 23 * 60 + 5)
    assert relay == [("porch", True), ("porch", False)]


def test_the_relay_is_not_told_the_same_thing_twice(db, sched, relay):
    """Edge-triggered: a 30s loop must not hammer the relay every tick."""
    db.add_schedule(device_id="porch", action="power", days=ALL_DAYS,
                    start_min=18 * 60, end_min=23 * 60)
    for minute in (19 * 60, 19 * 60 + 1, 20 * 60):
        sched.apply(MON, minute)
    assert relay == [("porch", True)]


def test_a_window_wrapping_midnight_works(db, sched, relay):
    db.add_schedule(device_id="porch", action="power", days=1 << MON,
                    start_min=22 * 60, end_min=6 * 60)
    sched.apply(MON, 23 * 60)
    assert relay == [("porch", True)]
    relay.clear()
    sched.apply(TUE, 3 * 60)      # the tail belongs to Monday's window
    assert relay == []


def test_days_are_respected(db, sched, relay):
    db.add_schedule(device_id="porch", action="power", days=1 << MON,
                    start_min=18 * 60, end_min=23 * 60)
    sched.apply(TUE, 19 * 60)
    assert ("porch", True) not in relay


def test_a_scheduled_device_is_asserted_off_outside_its_window(db, sched, relay):
    """A timer means "on then, off otherwise" — the same contract as a camera
    light schedule."""
    db.add_schedule(device_id="porch", action="power", days=ALL_DAYS,
                    start_min=18 * 60, end_min=23 * 60)
    sched.apply(MON, 9 * 60)
    assert relay == [("porch", False)]


def test_a_manual_override_is_not_fought_every_tick(db, sched, relay):
    """Turning the light on by hand outside its window must stick until the next
    boundary, not be switched off again 30 seconds later."""
    db.add_schedule(device_id="porch", action="power", days=ALL_DAYS,
                    start_min=18 * 60, end_min=23 * 60)
    sched.apply(MON, 9 * 60)          # asserts off once
    relay.clear()
    for minute in (9 * 60 + 1, 10 * 60, 11 * 60):
        sched.apply(MON, minute)
    assert relay == []


def test_a_disabled_schedule_does_nothing(db, sched, relay):
    sid = db.add_schedule(device_id="porch", action="power", days=ALL_DAYS,
                          start_min=18 * 60, end_min=23 * 60)
    db.set_schedule_enabled(sid, False)
    sched.apply(MON, 19 * 60)
    assert relay == []


def test_a_disabled_device_is_left_alone(db, sched, relay):
    db.update_device("porch", enabled=0)
    db.add_schedule(device_id="porch", action="power", days=ALL_DAYS,
                    start_min=18 * 60, end_min=23 * 60)
    sched.apply(MON, 19 * 60)
    assert relay == []


def test_an_unreachable_relay_is_retried_next_tick(db, sched, device, monkeypatch):
    """The state is recorded only after the device accepts it, so a relay that
    was unplugged during the window still comes on when it returns."""
    attempts = []

    def flaky(device, on):
        attempts.append(on)
        if len(attempts) == 1:
            raise devicelib.DeviceError("10.0.0.5: timed out")
        return on

    monkeypatch.setattr(devicelib, "set_state", flaky)
    db.add_schedule(device_id="porch", action="power", days=ALL_DAYS,
                    start_min=18 * 60, end_min=23 * 60)

    sched.apply(MON, 19 * 60)
    assert db.device("porch")["last_error"]

    sched.apply(MON, 19 * 60 + 1)
    assert attempts == [True, True]
    assert db.device("porch")["last_error"] is None
    assert db.device("porch")["last_state"] == 1


def test_camera_schedules_still_work_alongside(db, sched, relay):
    """The two kinds share a table; neither may pick up the other's rows."""
    from conftest import add_camera
    add_camera(db, "cam1")
    db.add_schedule("cam1", "record", ALL_DAYS, 0, 1439)
    db.add_schedule(device_id="porch", action="power", days=ALL_DAYS,
                    start_min=18 * 60, end_min=23 * 60)
    sched.apply(MON, 19 * 60)
    assert db.camera("cam1")["record"] == 1
    assert relay == [("porch", True)]


def test_a_schedule_needs_exactly_one_target(db):
    with pytest.raises(ValueError):
        db.add_schedule(camera_id="cam1", device_id="porch", action="power",
                        days=ALL_DAYS, start_min=0, end_min=60)
    with pytest.raises(ValueError):
        db.add_schedule(action="power", days=ALL_DAYS, start_min=0, end_min=60)


# --- API -------------------------------------------------------------------

def test_device_schedule_crud(admin_client, db, relay):
    r = admin_client.post("/api/devices/porch/schedules",
                          json={"days": ALL_DAYS, "start_min": 1080, "end_min": 1380})
    assert r.status_code == 200
    sid = r.json()["id"]
    assert r.json()["device_id"] == "porch"

    listed = admin_client.get("/api/devices/porch/schedules").json()
    assert [s["id"] for s in listed] == [sid]

    assert admin_client.patch(f"/api/devices/porch/schedules/{sid}",
                              json={"enabled": False}).status_code == 200
    assert db.one("SELECT enabled FROM schedules WHERE id = ?", (sid,))["enabled"] == 0

    assert admin_client.delete(f"/api/devices/porch/schedules/{sid}").status_code == 200
    assert admin_client.get("/api/devices/porch/schedules").json() == []


@pytest.mark.parametrize("body,reason", [
    ({"days": 0, "start_min": 0, "end_min": 60}, "no days"),
    ({"days": 999, "start_min": 0, "end_min": 60}, "bad mask"),
    ({"days": 127, "start_min": 60, "end_min": 60}, "zero length"),
    ({"days": 127, "start_min": -1, "end_min": 60}, "negative"),
    ({"days": 127, "start_min": "six", "end_min": 60}, "not a number"),
])
def test_invalid_windows_are_rejected(admin_client, db, relay, body, reason):
    r = admin_client.post("/api/devices/porch/schedules", json=body)
    assert r.status_code == 400, reason


def test_schedules_of_an_unknown_device_are_404(admin_client):
    assert admin_client.get("/api/devices/ghost/schedules").status_code == 404
    assert admin_client.post("/api/devices/ghost/schedules",
                             json={"days": 127, "start_min": 0, "end_min": 60}).status_code == 404


def test_a_schedule_cannot_be_reached_through_the_wrong_device(admin_client, db, relay):
    db.add_device(id="other", name="Other", driver="shelly", host="10.0.0.6")
    sid = admin_client.post("/api/devices/porch/schedules",
                            json={"days": 127, "start_min": 0, "end_min": 60}).json()["id"]
    assert admin_client.delete(f"/api/devices/other/schedules/{sid}").status_code == 404


def test_viewers_cannot_schedule_devices(viewer_client):
    assert viewer_client.get("/api/devices/porch/schedules").status_code == 403


def test_deleting_a_device_removes_its_schedules(admin_client, db, relay):
    """Otherwise the rows linger pointing at nothing — invisible and unremovable."""
    admin_client.post("/api/devices/porch/schedules",
                      json={"days": ALL_DAYS, "start_min": 1080, "end_min": 1380})
    assert db.schedules_for_device("porch")

    admin_client.delete("/api/devices/porch")

    assert db.schedules_for_device("porch") == []
    assert db.schedules() == []


def test_deleting_a_device_leaves_other_schedules_alone(admin_client, db, relay):
    from conftest import add_camera
    add_camera(db, "cam1")
    db.add_schedule("cam1", "record", ALL_DAYS, 0, 60)
    db.add_device(id="other", name="Other", driver="shelly", host="10.0.0.6")
    admin_client.post("/api/devices/other/schedules",
                      json={"days": ALL_DAYS, "start_min": 60, "end_min": 120})
    admin_client.post("/api/devices/porch/schedules",
                      json={"days": ALL_DAYS, "start_min": 1080, "end_min": 1380})

    admin_client.delete("/api/devices/porch")

    assert len(db.schedules_for_device("other")) == 1
    assert len(db.schedules_for("cam1")) == 1
