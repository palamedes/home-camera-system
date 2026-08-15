"""Covering schedules in the scheduler loop.

The defining decision here is that a covering schedule fires once when its
window OPENS, rather than being asserted for the whole window the way a relay
is. A shade you raised by hand at noon must stay up; a scheduler that re-closed
it every thirty seconds would be unusable.
"""

import pytest

from nvr import shades
from nvr.scheduler import SchedulerService


@pytest.fixture
def moves(monkeypatch):
    sent = []

    def set_position(host, mac, device_type, position, *, api_key, hub_token):
        sent.append((mac, position))
        return {}

    monkeypatch.setattr(shades, "set_position", set_position)
    return sent


@pytest.fixture
def sched(db):
    return SchedulerService(config=None, db=db)


@pytest.fixture
def house(db):
    db.add_shade_hub(id="hub1", name="Hub", host="10.0.0.5", token="T" * 16)
    bedroom = db.add_room("Bedroom")
    office = db.add_room("Office")
    for cid, room, layer in [
        ("bed-sheer", bedroom, "sheer"),
        ("bed-black", bedroom, "blackout"),
        ("off-sheer", office, "sheer"),
    ]:
        db.add_covering(id=cid, hub_id="hub1", name=cid, room_id=room, layer=layer,
                        device_type="10000000")
    return {"bedroom": bedroom, "office": office}


MON = 0


def test_a_group_schedule_moves_its_layer_in_its_room(db, sched, house, moves):
    db.add_schedule(action="cover", covering_room_id=house["bedroom"],
                    covering_layer="blackout", days=127,
                    start_min=1260, end_min=1320, value="100")
    sched.apply(MON, 1260)
    assert moves == [("bed-black", 100)]


def test_it_fires_once_not_every_tick(db, sched, house, moves):
    """The whole point of the rising edge: raise a shade by hand mid-window and
    it stays raised."""
    db.add_schedule(action="cover", covering_room_id=house["bedroom"],
                    days=127, start_min=420, end_min=480, value="0")
    sched.apply(MON, 420)
    sched.apply(MON, 430)
    sched.apply(MON, 470)
    assert len(moves) == 2  # both bedroom coverings, once each


def test_it_fires_again_after_the_window_closes_and_reopens(db, sched, house, moves):
    db.add_schedule(action="cover", covering_id="bed-sheer", days=127,
                    start_min=420, end_min=480, value="0")
    sched.apply(MON, 420)
    sched.apply(MON, 600)          # outside; arms the edge again
    sched.apply(MON, 421)          # next day's window
    assert moves == [("bed-sheer", 0), ("bed-sheer", 0)]


def test_nothing_happens_outside_the_window(db, sched, house, moves):
    db.add_schedule(action="cover", covering_id="bed-sheer", days=127,
                    start_min=420, end_min=480, value="0")
    sched.apply(MON, 200)
    assert moves == []


def test_a_disabled_schedule_is_ignored(db, sched, house, moves):
    db.add_schedule(action="cover", covering_id="bed-sheer", days=127,
                    start_min=420, end_min=480, value="0", enabled=0)
    sched.apply(MON, 420)
    assert moves == []


def test_a_schedule_with_no_room_covers_the_whole_house(db, sched, house, moves):
    db.add_schedule(action="cover", covering_layer="sheer", days=127,
                    start_min=420, end_min=480, value="0")
    sched.apply(MON, 420)
    assert sorted(m[0] for m in moves) == ["bed-sheer", "off-sheer"]


def test_a_schedule_with_neither_selector_covers_everything(db, sched, house, moves):
    db.add_schedule(action="cover", days=127, start_min=420, end_min=480, value="0")
    sched.apply(MON, 420)
    assert len(moves) == 3


def test_a_disabled_covering_is_skipped(db, sched, house, moves):
    db.update_covering("bed-black", enabled=0)
    db.add_schedule(action="cover", covering_room_id=house["bedroom"], days=127,
                    start_min=420, end_min=480, value="0")
    sched.apply(MON, 420)
    assert [m[0] for m in moves] == ["bed-sheer"]


def test_a_disabled_hub_is_skipped(db, sched, house, moves):
    db.update_shade_hub("hub1", enabled=0)
    db.add_schedule(action="cover", days=127, start_min=420, end_min=480, value="0")
    sched.apply(MON, 420)
    assert moves == []


def test_a_weekday_mask_is_honoured(db, sched, house, moves):
    db.add_schedule(action="cover", covering_id="bed-sheer", days=0b0000001,
                    start_min=420, end_min=480, value="0")   # Monday only
    sched.apply(1, 420)     # Tuesday
    assert moves == []
    sched.apply(MON, 420)
    assert moves == [("bed-sheer", 0)]


def test_one_unreachable_motor_does_not_stop_the_group(db, sched, house, monkeypatch):
    """A weak 433 MHz link is the normal failure. The rest of the room must
    still move, and the loop must survive."""
    moved = []

    def set_position(host, mac, device_type, position, *, api_key, hub_token):
        if mac == "bed-sheer":
            raise shades.ShadeError("out of range")
        moved.append(mac)
        return {}

    monkeypatch.setattr(shades, "set_position", set_position)
    db.add_schedule(action="cover", covering_room_id=house["bedroom"], days=127,
                    start_min=420, end_min=480, value="100")
    sched.apply(MON, 420)
    assert moved == ["bed-black"]
    assert db.covering("bed-sheer")["last_error"] == "out of range"


def test_a_successful_move_is_recorded(db, sched, house, moves):
    db.add_schedule(action="cover", covering_id="bed-sheer", days=127,
                    start_min=420, end_min=480, value="65")
    sched.apply(MON, 420)
    row = db.covering("bed-sheer")
    assert row["last_position"] == 65
    assert row["last_error"] is None


def test_a_bad_position_is_logged_not_fatal(db, sched, house, moves):
    """A value that is not a number must not take the scheduler thread down —
    that would silently stop recording schedules too."""
    db.add_schedule(action="cover", covering_id="bed-sheer", days=127,
                    start_min=420, end_min=480, value="soon")
    sched.apply(MON, 420)
    assert moves == []


def test_a_wrapping_window_fires_at_its_start(db, sched, house, moves):
    db.add_schedule(action="cover", covering_id="bed-black", days=127,
                    start_min=1320, end_min=360, value="100")   # 22:00-06:00
    sched.apply(MON, 1320)
    assert moves == [("bed-black", 100)]


def test_covering_schedules_do_not_disturb_the_other_kinds(db, sched, house, moves):
    """apply() runs every kind in one pass; a covering rule must not be picked
    up by the record or power handlers."""
    db.add_schedule(action="cover", covering_id="bed-sheer", days=127,
                    start_min=420, end_min=480, value="0")
    sched.apply(MON, 420)
    assert sched._record_state == {}
    assert sched._device_state == {}


def test_a_schedule_needs_exactly_one_kind_of_target(db):
    with pytest.raises(ValueError):
        db.add_schedule(camera_id="front", action="cover", days=127,
                        start_min=0, end_min=60)
    with pytest.raises(ValueError):
        db.add_schedule(action="record", covering_id="c1", days=127,
                        start_min=0, end_min=60)


def test_a_covering_schedule_is_either_one_covering_or_a_group(db):
    with pytest.raises(ValueError):
        db.add_schedule(action="cover", covering_id="c1", covering_room_id=1,
                        days=127, start_min=0, end_min=60)


# --- the 10-minute grace window, which is the real production shape ---------

def test_a_grace_window_fires_once_across_its_whole_span(db, sched, house, moves):
    db.add_schedule(action="cover", covering_id="bed-black", days=127,
                    start_min=1020, end_min=1030, value="100")
    for minute in range(1020, 1031):
        sched.apply(MON, minute)
    assert moves == [("bed-black", 100)]


def test_a_restart_inside_the_grace_window_still_applies_the_rule(db, house, moves):
    """Edge state lives in memory, so a fresh scheduler mid-window sees a rising
    edge and catches up — which is what the grace period is for."""
    db.add_schedule(action="cover", covering_id="bed-black", days=127,
                    start_min=1020, end_min=1030, value="100")
    restarted = SchedulerService(config=None, db=db)
    restarted.apply(MON, 1026)          # came up 6 minutes late
    assert moves == [("bed-black", 100)]


def test_a_restart_after_the_grace_window_does_not_move_anything(db, house, moves):
    """Hours later is not a catch-up, it is a shade moving on its own."""
    db.add_schedule(action="cover", covering_id="bed-black", days=127,
                    start_min=1020, end_min=1030, value="100")
    restarted = SchedulerService(config=None, db=db)
    restarted.apply(MON, 1200)
    assert moves == []


def test_two_rules_at_different_times_on_different_days(db, sched, house, moves):
    """Sundays at 5pm, Fridays at 7pm — the case that motivated the design."""
    db.add_schedule(action="cover", covering_layer="sheer", days=0b1000000,
                    start_min=1020, end_min=1030, value="100")   # Sunday 17:00
    db.add_schedule(action="cover", covering_layer="sheer", days=0b0010000,
                    start_min=1140, end_min=1150, value="100")   # Friday 19:00
    sched.apply(6, 1020)                      # Sunday 17:00
    sunday = len(moves)
    assert sunday == 2                        # both sheers
    sched.apply(6, 1140)                      # Sunday 19:00 — Friday rule idle
    assert len(moves) == sunday
    sched.apply(4, 1140)                      # Friday 19:00
    assert len(moves) == sunday + 2
