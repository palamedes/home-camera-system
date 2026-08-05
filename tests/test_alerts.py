"""Alert dispatcher: recording, webhook delivery, cooldown, test ping."""

from __future__ import annotations

import json

import pytest

from nvr import alerts as alerts_mod
from nvr.alerts import AlertService
from nvr.config import AlertsConfig


class _Cfg:
    def __init__(self, alerts):
        self.alerts = alerts


class _Resp:
    def raise_for_status(self):
        pass


@pytest.fixture()
def posted(monkeypatch):
    """Capture webhook POSTs instead of hitting the network."""
    calls = []

    def _post(url, json=None, **kwargs):
        calls.append({"url": url, "json": json})
        return _Resp()

    monkeypatch.setattr(alerts_mod.httpx, "post", _post)
    return calls


def _svc(db, **alert_kwargs):
    cfg = _Cfg(AlertsConfig(**alert_kwargs))
    return AlertService(cfg, db)


def test_emit_records_event_even_without_webhook(app_module, db):
    svc = _svc(db, enabled=False)
    eid = svc.emit(type="person", camera_id="cam1", camera_name="Front")
    assert eid > 0
    rows = db.recent_events()
    assert len(rows) == 1
    assert rows[0]["type"] == "person"
    assert rows[0]["label"] == "Person detected"


def test_notify_noop_when_disabled(app_module, db, posted):
    svc = _svc(db, enabled=False, webhook_url="http://hook/x")
    assert svc.notify({"type": "person", "camera_id": "c"}) is False
    assert posted == []


def test_notify_posts_payload_when_enabled(app_module, db, posted):
    svc = _svc(db, enabled=True, webhook_url="http://hook/x")
    ok = svc.notify({
        "type": "person", "camera_id": "c1", "camera_name": "Front",
        "label": "Person detected",
    })
    assert ok is True
    assert len(posted) == 1
    body = posted[0]["json"]
    assert body["source"] == "sentry"
    assert body["type"] == "person"
    assert body["camera"] == "Front"
    assert "Person detected" in body["message"]


def test_cooldown_suppresses_repeat(app_module, db, posted):
    svc = _svc(db, enabled=True, webhook_url="http://hook/x", cooldown_seconds=999)
    a = svc.notify({"type": "person", "camera_id": "c1"})
    b = svc.notify({"type": "person", "camera_id": "c1"})
    assert a is True and b is False          # same key within cooldown
    # A different kind on the same camera is a different key -> allowed.
    c = svc.notify({"type": "vehicle", "camera_id": "c1"})
    assert c is True
    assert len(posted) == 2


def test_emit_applies_cooldown_but_still_records(app_module, db, posted):
    svc = _svc(db, enabled=True, webhook_url="http://hook/x", cooldown_seconds=999)
    svc.emit(type="person", camera_id="c1", camera_name="Front")
    svc.emit(type="person", camera_id="c1", camera_name="Front")
    # Both recorded (timeline wants every hit) but only one webhook (cooldown).
    assert len(db.recent_events()) == 2
    assert len(posted) == 1


def test_test_ping_needs_url_then_sends(app_module, db, posted):
    svc = _svc(db, enabled=False, webhook_url="")
    with pytest.raises(ValueError):
        svc.test()
    svc2 = _svc(db, enabled=False, webhook_url="http://hook/x")
    assert svc2.test() is True                # bypasses enabled
    assert posted[0]["json"]["type"] == "test"


def test_test_endpoint_admin_only(app_module, admin_client, viewer_client):
    # Viewer forbidden; admin gets a structured response (400 since no URL set).
    assert viewer_client.post("/api/alerts/test").status_code == 403
    r = admin_client.post("/api/alerts/test")
    assert r.status_code in (200, 400)
