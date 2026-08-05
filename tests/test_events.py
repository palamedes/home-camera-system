"""EventService: edge-triggered detection from (stubbed) Reolink AI state."""

from __future__ import annotations

import pytest

from conftest import add_camera
from nvr import events as events_mod
from nvr.config import AlertsConfig
from nvr.events import EventService


class _Cfg:
    def __init__(self, alerts):
        self.alerts = alerts


class _RecordingAlerts:
    """Stand-in alert sink that records emitted events."""

    def __init__(self):
        self.emitted = []

    def emit(self, **kwargs):
        self.emitted.append(kwargs)


class _FakeClient:
    """Fake ReolinkClient whose ai_state is driven by a shared mutable dict."""

    def __init__(self, state):
        self._state = state

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self):
        pass

    def ai_state(self, channel=0):
        return dict(self._state)

    def motion_state(self, channel=0):
        return self._state.get("motion")


@pytest.fixture()
def wired(app_module, db, monkeypatch):
    add_camera(db, "cam1", brand="reolink", username="u", password="p")
    state = {}
    monkeypatch.setattr(events_mod, "ReolinkClient", lambda **kw: _FakeClient(state))
    sink = _RecordingAlerts()
    cfg = _Cfg(AlertsConfig(enabled=True, detect=["person", "vehicle"]))
    svc = EventService(cfg, db, sink)
    return svc, state, sink


def test_rising_edge_emits_once(wired):
    svc, state, sink = wired
    state["person"] = False
    svc.poll_once()
    assert sink.emitted == []           # no alarm yet

    state["person"] = True
    svc.poll_once()
    assert len(sink.emitted) == 1       # 0 -> 1 fires
    assert sink.emitted[0]["type"] == "person"
    assert sink.emitted[0]["camera_id"] == "cam1"

    svc.poll_once()
    assert len(sink.emitted) == 1       # still high -> no repeat


def test_falling_then_rising_re_emits(wired):
    svc, state, sink = wired
    state["person"] = True
    svc.poll_once()
    state["person"] = False
    svc.poll_once()
    state["person"] = True
    svc.poll_once()
    assert len(sink.emitted) == 2       # two distinct rising edges


def test_only_configured_kinds_fire(wired):
    svc, state, sink = wired
    # 'animal' isn't in detect -> ignored even on a rising edge.
    state["animal"] = True
    state["vehicle"] = True
    svc.poll_once()
    kinds = {e["type"] for e in sink.emitted}
    assert kinds == {"vehicle"}


def test_non_reolink_camera_skipped(app_module, db, monkeypatch):
    add_camera(db, "amcrest", brand="amcrest")
    called = {"n": 0}

    def _boom(**kw):
        called["n"] += 1
        raise AssertionError("should not construct a client for non-Reolink")

    monkeypatch.setattr(events_mod, "ReolinkClient", _boom)
    svc = EventService(_Cfg(AlertsConfig(enabled=True)), db, _RecordingAlerts())
    svc.poll_once()
    assert called["n"] == 0


def test_service_inert_when_disabled(app_module, db):
    svc = EventService(_Cfg(AlertsConfig(enabled=False)), db, _RecordingAlerts())
    svc.start()
    assert svc._thread is None          # never spins up a polling thread
