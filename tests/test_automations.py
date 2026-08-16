"""The automation framework: trigger -> window -> actions.

This is the part of Sentry that acts on the house without anybody watching, so
the tests lean on the failure modes rather than the happy path: a jammed action
must not take the detector thread down, a loitering person must not command the
porch light fifty times, and a broken action must be caught when it is SAVED
rather than at 2am when it fires.
"""

import json
import time

import pytest

from nvr import automations as auto


# --- validation ------------------------------------------------------------

def test_a_device_action_is_accepted():
    out = auto.validate_actions([{"kind": "device", "device_id": "porch", "state": "on"}])
    assert out == [{"kind": "device", "device_id": "porch", "state": "on"}]


def test_actions_cannot_be_empty():
    """An automation that does nothing is a mistake, not a configuration."""
    with pytest.raises(auto.AutomationError):
        auto.validate_actions([])


@pytest.mark.parametrize("action,reason", [
    ({"kind": "nonsense"}, "unknown kind"),
    ({"kind": "device"}, "no device"),
    ({"kind": "device", "device_id": "p", "state": "sideways"}, "bad state"),
    ({"kind": "covering", "position": 150}, "position out of range"),
    ({"kind": "covering", "position": "shut"}, "position not a number"),
    ({"kind": "webhook", "url": "ftp://x"}, "not http"),
    ({"kind": "webhook", "url": "http://x", "method": "DELETE"}, "bad method"),
    ({"kind": "device", "device_id": "p", "for_seconds": "ages"}, "bad duration"),
    ({"kind": "device", "device_id": "p", "for_seconds": 999999}, "duration too long"),
])
def test_a_broken_action_is_refused_when_saved(action, reason):
    with pytest.raises(auto.AutomationError):
        auto.validate_actions([action])
    assert reason


def test_a_timed_device_action_keeps_its_duration():
    out = auto.validate_actions(
        [{"kind": "device", "device_id": "porch", "state": "on", "for_seconds": 300}]
    )
    assert out[0]["for_seconds"] == 300


def test_match_drops_wildcards():
    """'any camera' should be expressed by leaving the field alone, so the
    stored pattern stays a plain 'these must be equal' check."""
    assert auto.validate_match({"event_type": "person", "camera_id": "any"}) \
        == {"event_type": "person"}
    assert auto.validate_match(None) == {}


@pytest.mark.parametrize("pattern,event,expected", [
    ({}, {"event_type": "person"}, True),
    ({"event_type": "person"}, {"event_type": "person"}, True),
    ({"event_type": "person"}, {"event_type": "vehicle"}, False),
    ({"event_type": "person", "camera_id": "drive"},
     {"event_type": "person", "camera_id": "drive"}, True),
    ({"camera_id": "drive"}, {"event_type": "person", "camera_id": "porch"}, False),
    ({"camera_id": "drive"}, {"event_type": "person"}, False),
])
def test_matching(pattern, event, expected):
    assert auto.matches(pattern, event) is expected


# --- windows ---------------------------------------------------------------

def test_no_window_means_any_time():
    assert auto.in_window(127, None, None, weekday=2, minute=720)


def test_a_window_bounds_the_trigger():
    # 20:00 - 06:00, i.e. after dark
    assert auto.in_window(127, 1200, 360, weekday=2, minute=1300)
    assert not auto.in_window(127, 1200, 360, weekday=2, minute=720)
    assert auto.in_window(127, 1200, 360, weekday=3, minute=60)   # the tail


def test_a_day_mask_without_times_still_applies():
    assert auto.in_window(0b0000001, None, None, weekday=0, minute=720)
    assert not auto.in_window(0b0000001, None, None, weekday=1, minute=720)


# --- running ---------------------------------------------------------------

class FakeDevices:
    def __init__(self):
        self.calls = []
        self.fail = False

    def set_state(self, device, on):
        if self.fail:
            raise RuntimeError("relay unreachable")
        self.calls.append((device["id"], on))
        return on

    def toggle(self, device):
        self.calls.append((device["id"], "toggle"))
        return True


class FakeShades:
    def __init__(self):
        self.calls = []

    def set_position(self, host, mac, device_type, position, *, api_key, hub_token):
        self.calls.append((mac, position))
        return {}


@pytest.fixture
def devices():
    return FakeDevices()


@pytest.fixture
def shades():
    return FakeShades()


@pytest.fixture
def service(db, devices, shades):
    return auto.AutomationService(config=None, db=db, devices=devices, shades=shades)


def _automation(db, **over):
    fields = dict(
        name="Porch light", slug="porch-light", trigger_kind="event",
        match=json.dumps({"event_type": "person"}),
        actions=json.dumps([{"kind": "device", "device_id": "porch", "state": "on"}]),
        cooldown_seconds=0, days=127,
    )
    fields.update(over)
    return db.add_automation(**fields)


@pytest.fixture
def porch(db):
    db.add_device(id="porch", name="Porch light", driver="shelly", host="10.0.0.7")
    return "porch"


def test_running_switches_the_device(db, service, devices, porch):
    aid = _automation(db)
    result = service.run_now(aid)
    assert result["errors"] == []
    assert devices.calls == [("porch", True)]


def test_a_failing_action_is_recorded_not_raised(db, service, devices, porch):
    """Half a scene is better than none, and the error must be visible."""
    devices.fail = True
    aid = _automation(db)
    result = service.run_now(aid)
    assert result["errors"] and "unreachable" in result["errors"][0]
    assert db.automation(aid)["last_error"]


def test_one_bad_action_does_not_stop_the_others(db, service, devices, porch):
    aid = _automation(db, actions=json.dumps([
        {"kind": "device", "device_id": "ghost", "state": "on"},
        {"kind": "device", "device_id": "porch", "state": "on"},
    ]))
    result = service.run_now(aid)
    assert len(result["errors"]) == 1
    assert devices.calls == [("porch", True)]


def test_running_counts_and_timestamps(db, service, porch):
    aid = _automation(db)
    service.run_now(aid)
    service.run_now(aid)
    row = db.automation(aid)
    assert row["run_count"] == 2 and row["last_run"] is not None


def test_a_disabled_device_is_not_commanded(db, service, devices, porch):
    db.update_device("porch", enabled=0)
    aid = _automation(db)
    result = service.run_now(aid)
    assert devices.calls == [] and result["errors"]


def test_a_covering_action_moves_the_right_group(db, service, shades):
    db.add_shade_hub(id="hub1", name="Hub", host="10.0.0.5", token="T" * 16)
    room = db.add_room("Bedroom")
    db.add_covering(id="sheer1", hub_id="hub1", name="s", room_id=room, layer="sheer",
                    device_type="10000000")
    db.add_covering(id="black1", hub_id="hub1", name="b", room_id=room,
                    layer="blackout", device_type="10000000")
    aid = _automation(db, actions=json.dumps([
        {"kind": "covering", "position": 100, "layer": "blackout", "room_id": room},
    ]))
    service.run_now(aid)
    assert shades.calls == [("black1", 100)]


def test_an_unknown_automation_raises(service):
    with pytest.raises(auto.AutomationError):
        service.run_now(9999)


# --- event dispatch --------------------------------------------------------

def test_a_matching_event_is_queued(db, service, porch):
    aid = _automation(db)
    service.handle_event({"event_type": "person", "camera_id": "drive"})
    assert service._queue.qsize() == 1
    assert service._queue.get_nowait()[0] == aid


def test_a_non_matching_event_is_ignored(db, service, porch):
    _automation(db, match=json.dumps({"event_type": "vehicle"}))
    service.handle_event({"event_type": "person"})
    assert service._queue.qsize() == 0


def test_a_hook_only_automation_ignores_events(db, service, porch):
    """It has a URL; that is the only thing that should drive it."""
    _automation(db, trigger_kind="hook")
    service.handle_event({"event_type": "person"})
    assert service._queue.qsize() == 0


def test_a_disabled_automation_ignores_events(db, service, porch):
    _automation(db, enabled=0)
    service.handle_event({"event_type": "person"})
    assert service._queue.qsize() == 0


def test_the_cooldown_stops_a_loitering_person_spamming_the_relay(db, service, porch):
    """A person in frame raises an event every poll. Without this the porch
    light is commanded dozens of times a minute."""
    _automation(db, cooldown_seconds=300)
    for _ in range(5):
        service.handle_event({"event_type": "person"})
    assert service._queue.qsize() == 1


def test_a_zero_cooldown_fires_every_time(db, service, porch):
    _automation(db, cooldown_seconds=0)
    for _ in range(3):
        service.handle_event({"event_type": "person"})
    assert service._queue.qsize() == 3


def test_an_out_of_window_event_does_not_fire(db, service, porch):
    """Only between 03:00 and 03:01, which 'now' is almost certainly not."""
    lt = time.localtime()
    now_min = lt.tm_hour * 60 + lt.tm_min
    start = (now_min + 120) % 1440
    _automation(db, start_min=start, end_min=(start + 1) % 1440)
    service.handle_event({"event_type": "person"})
    assert service._queue.qsize() == 0


def test_a_broken_pattern_does_not_take_the_detector_down(db, service, porch):
    """Whatever is in the column, the camera poller must survive reading it."""
    _automation(db, match="{not json")
    service.handle_event({"event_type": "person"})
    assert service._queue.qsize() == 0


def test_a_full_queue_drops_rather_than_blocking(db, service, porch):
    """handle_event runs on the detector thread; blocking there would stall
    detection for every other camera."""
    _automation(db, cooldown_seconds=0)
    service._queue.maxsize = 2
    for _ in range(10):
        service.handle_event({"event_type": "person"})
    assert service._queue.qsize() == 2


# --- timed reverts ---------------------------------------------------------

def test_a_timed_action_schedules_its_own_off(db, service, devices, porch):
    aid = _automation(db, actions=json.dumps([
        {"kind": "device", "device_id": "porch", "state": "on", "for_seconds": 300},
    ]))
    service.run_now(aid)
    assert devices.calls == [("porch", True)]
    assert len(service._reverts) == 1


def test_a_revert_fires_once_due(db, service, devices, porch):
    aid = _automation(db, actions=json.dumps([
        {"kind": "device", "device_id": "porch", "state": "on", "for_seconds": 1},
    ]))
    service.run_now(aid)
    service._reverts = [(time.time() - 1, "porch", False)]   # make it due
    service._apply_due_reverts()
    assert devices.calls[-1] == ("porch", False)
    assert service._reverts == []


def test_a_revert_that_is_not_due_stays_pending(db, service, devices, porch):
    service._reverts = [(time.time() + 600, "porch", False)]
    service._apply_due_reverts()
    assert len(service._reverts) == 1
    assert devices.calls == []
