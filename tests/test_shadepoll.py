"""The background poller that keeps Sentry's idea of the blinds honest.

Without it, using the wall remote leaves the page showing the last position
Sentry itself commanded — a confidently wrong number, which is worse than
showing none. The tests care about three things: that it does not hammer a
433 MHz link, that one dead motor cannot starve the other nine, and that a
value Sentry wrote as a TARGET gets replaced by where the shade actually got to.
"""

import time

import pytest

from nvr import shades
from nvr.shadepoll import ShadePollService


@pytest.fixture
def house(db):
    db.add_shade_hub(id="hub1", name="Hub", host="10.0.0.5", token="T" * 16)
    for cid in ("c1", "c2", "c3"):
        db.add_covering(id=cid, hub_id="hub1", name=cid, layer="sheer",
                        device_type="10000000")
    return ["c1", "c2", "c3"]


@pytest.fixture
def reads(monkeypatch):
    """Record every ReadDevice instead of transmitting one."""
    calls = []

    def read_device(host, mac, device_type):
        calls.append(mac)
        return {"currentPosition": 42, "batteryLevel": 780, "RSSI": -70,
                "wirelessMode": 1}

    monkeypatch.setattr(shades, "read_device", read_device)
    return calls


@pytest.fixture
def service(db, monkeypatch):
    svc = ShadePollService(config_obj=None, db=db)
    # The pacing between motors is real airtime management, not something to
    # sit through in a test.
    monkeypatch.setattr("nvr.shadepoll.BETWEEN_COVERINGS", 0)
    return svc


# --- the ordinary pass -----------------------------------------------------

def test_a_pass_reads_every_covering(db, service, house, reads):
    assert service.poll_all() == 3
    assert sorted(reads) == ["c1", "c2", "c3"]


def test_a_reading_replaces_the_commanded_target(db, service, house, reads):
    """Sentry records the target when it issues a move; the shade reports where
    it actually got to, and that is what should end up on screen."""
    db.update_covering("c1", last_position=100)
    service.poll_all()
    assert db.covering("c1")["last_position"] == 42


def test_a_pass_stores_battery_and_signal(db, service, house, reads):
    service.poll_all()
    row = db.covering("c1")
    assert row["battery_mv"] == 780
    assert row["rssi"] == -70
    assert row["bidirectional"] == 1
    assert row["last_seen"] is not None


def test_a_disabled_covering_is_left_alone(db, service, house, reads):
    db.update_covering("c2", enabled=0)
    service.poll_all()
    assert "c2" not in reads


def test_a_disabled_hub_is_left_alone(db, service, house, reads):
    db.update_shade_hub("hub1", enabled=0)
    assert service.poll_all() == 0
    assert reads == []


def test_one_dead_motor_does_not_stop_the_others(db, service, house, monkeypatch):
    def read_device(host, mac, device_type):
        if mac == "c1":
            raise shades.ShadeError("out of range")
        return {"currentPosition": 7}

    monkeypatch.setattr(shades, "read_device", read_device)
    assert service.poll_all() == 2
    assert db.covering("c1")["last_error"] == "out of range"
    assert db.covering("c2")["last_position"] == 7


def test_a_success_clears_an_earlier_error(db, service, house, reads):
    db.update_covering("c1", last_error="out of range")
    service.poll_all()
    assert db.covering("c1")["last_error"] is None


# --- backing off a motor that keeps failing --------------------------------

def test_a_repeatedly_failing_motor_is_rested(db, service, house, monkeypatch):
    """At the far end of a weak link, one dead shade must not soak the radio
    the other nine are sharing."""
    attempts = []

    def read_device(host, mac, device_type):
        attempts.append(mac)
        if mac == "c1":
            raise shades.ShadeError("out of range")
        return {"currentPosition": 7}

    monkeypatch.setattr(shades, "read_device", read_device)
    for _ in range(3):
        service.poll_all()
    assert attempts.count("c1") == 3

    attempts.clear()
    service.poll_all()
    assert "c1" not in attempts, "kept hammering a motor that never answers"
    # The healthy ones are unaffected.
    assert sorted(attempts) == ["c2", "c3"]


def test_a_rested_motor_is_tried_again_eventually(db, service, house, monkeypatch):
    attempts = []

    def read_device(host, mac, device_type):
        attempts.append(mac)
        if mac == "c1":
            raise shades.ShadeError("out of range")
        return {"currentPosition": 7}

    monkeypatch.setattr(shades, "read_device", read_device)
    for _ in range(3):
        service.poll_all()
    attempts.clear()
    # Rested for a fixed number of passes, then retried — a shade that comes
    # back must recover without anyone restarting anything.
    for _ in range(8):
        service.poll_all()
    assert "c1" in attempts


# --- the burst after Sentry moves something --------------------------------

def test_moving_something_schedules_a_burst(db, service, house):
    service.watch("c1")
    assert "c1" in service._burst_until


def test_a_burst_reads_the_covering_soon(db, service, house, reads, monkeypatch):
    """A motor takes seconds to travel, so the value recorded at command time
    is aspirational until this confirms it."""
    service.watch("c1")
    # Due immediately rather than waiting out the real interval.
    service._burst_next["c1"] = 0.0
    service._run_bursts(time.time())
    assert reads == ["c1"]


def test_a_burst_stops_when_the_shade_has_had_time_to_travel(
        db, service, house, reads):
    service.watch("c1")
    service._burst_next["c1"] = 0.0
    # Well past the window.
    service._run_bursts(time.time() + 600)
    assert reads == []
    assert "c1" not in service._burst_until


def test_a_burst_does_not_count_against_the_backoff(db, service, house, monkeypatch):
    """A shade in motion can miss a read; that is not evidence it is dead."""
    def read_device(host, mac, device_type):
        raise shades.ShadeError("busy")

    monkeypatch.setattr(shades, "read_device", read_device)
    service.watch("c1")
    for _ in range(5):
        service._burst_next["c1"] = 0.0
        service._run_bursts(time.time())
    assert service._resting.get("c1", 0) == 0


def test_moving_a_rested_motor_forgives_it(db, service, house):
    """A move that succeeded proves the link works, so leaving it rested would
    be ignoring the best evidence available."""
    service._resting["c1"] = 6
    service._failures["c1"] = 2
    service.watch("c1")
    assert service._resting.get("c1", 0) == 0
    assert service._failures.get("c1", 0) == 0


# --- pacing ----------------------------------------------------------------

def test_the_full_pass_is_hourly_not_constant(db, house, reads, monkeypatch):
    """Each read is a round trip over 433 MHz; ten motors is real airtime spent
    on something that changes only when a person touches a remote."""
    monkeypatch.setattr("nvr.shadepoll.BETWEEN_COVERINGS", 0)
    svc = ShadePollService(config_obj=None, db=db)
    svc.run_once()
    first = len(reads)
    assert first == 3
    # Ticking again immediately must not re-read anything.
    svc.run_once()
    svc.run_once()
    assert len(reads) == first


def test_the_next_pass_happens_once_the_hour_is_up(db, house, reads, monkeypatch):
    monkeypatch.setattr("nvr.shadepoll.BETWEEN_COVERINGS", 0)
    svc = ShadePollService(config_obj=None, db=db)
    svc.run_once()
    svc._next_full = 0.0          # pretend an hour went by
    svc.run_once()
    assert len(reads) == 6


def test_a_broken_interval_cannot_kill_the_poller(monkeypatch, db):
    """The sleep sits after the try, as everywhere else — a bad value there
    would take the thread down silently and permanently."""
    from nvr import config as config_module
    assert config_module.safe_interval("nonsense", default=3600.0, minimum=60.0) \
        == 3600.0
