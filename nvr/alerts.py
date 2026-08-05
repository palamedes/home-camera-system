"""Alert delivery.

A single small dispatcher every detector funnels through. It does two things:
record the event in the database (so it shows on the timeline / event list) and
POST a JSON payload to a configured webhook (so you get notified). The webhook
is transport-agnostic on purpose — point it at Home Assistant, a Discord/Slack
relay, ntfy, or your own script.

Two guards keep it from becoming a nuisance:

  * cooldown — the same (camera, kind) won't re-notify more often than
    cooldown_seconds, so a person loitering in frame is one alert, not fifty.
  * enabled/url — with alerts disabled or no URL set, notify() is a no-op, but
    events are still recorded (the timeline works without a webhook).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import httpx

log = logging.getLogger("nvr.alerts")

# Human labels for the event kinds we raise.
_LABELS = {
    "person": "Person detected",
    "vehicle": "Vehicle detected",
    "animal": "Animal detected",
    "motion": "Motion detected",
    "flood": "River level alert",
    "test": "Test alert",
}


class AlertService:
    def __init__(self, config: Any, db: Any):
        self.config = config
        self.db = db
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def cfg(self) -> Any:
        return self.config.alerts

    # -- recording + notifying ----------------------------------------------

    def emit(
        self,
        *,
        type: str,
        camera_id: str | None = None,
        camera_name: str | None = None,
        label: str | None = None,
        ts: float | None = None,
        score: float | None = None,
        meta: dict[str, Any] | None = None,
        message: str | None = None,
        notify: bool = True,
    ) -> int:
        """Record an event and (optionally) fire a notification for it.

        Returns the new event id. Detectors should call this rather than
        touching the database and webhook separately.
        """
        ts = ts or time.time()
        meta = meta or {}
        label = label or _LABELS.get(type, type.title())
        event_id = self.db.add_event(
            camera_id, ts, type, label, score, json.dumps(meta)
        )
        if notify:
            self.notify({
                "type": type,
                "camera_id": camera_id,
                "camera_name": camera_name,
                "label": label,
                "ts": ts,
                "score": score,
                "meta": meta,
                "message": message,
            })
        return event_id

    def notify(self, event: dict[str, Any], *, bypass_cooldown: bool = False) -> bool:
        """POST one event to the webhook, subject to enabled/url and cooldown.
        Returns True only if a request was actually sent and accepted."""
        if not self.cfg.enabled or not self.cfg.webhook_url:
            return False
        key = f"{event.get('camera_id') or 'global'}:{event.get('type')}"
        if not bypass_cooldown and not self._within_cooldown_ok(key):
            return False
        return self._post(self._payload(event))

    def test(self) -> bool:
        """Fire a one-off test alert, ignoring enabled/cooldown but still
        needing a URL. Raises ValueError if no webhook is configured."""
        if not self.cfg.webhook_url:
            raise ValueError("no webhook_url configured")
        return self._post(self._payload({
            "type": "test",
            "label": _LABELS["test"],
            "message": "Sentry test alert — your webhook is working.",
            "ts": time.time(),
        }))

    # -- internals ----------------------------------------------------------

    def _within_cooldown_ok(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            last = self._last.get(key, 0.0)
            if now - last < self.cfg.cooldown_seconds:
                return False
            self._last[key] = now
            return True

    def _payload(self, event: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": "sentry",
            "type": event.get("type"),
            "camera_id": event.get("camera_id"),
            "camera": event.get("camera_name"),
            "label": event.get("label"),
            "score": event.get("score"),
            "ts": event.get("ts"),
            "meta": event.get("meta") or {},
            "message": event.get("message") or self._message(event),
        }

    def _message(self, event: dict[str, Any]) -> str:
        label = event.get("label") or _LABELS.get(event.get("type", ""), "Alert")
        where = event.get("camera_name")
        return f"{label} on {where}" if where else label

    def _post(self, payload: dict[str, Any]) -> bool:
        try:
            resp = httpx.post(self.cfg.webhook_url, json=payload, timeout=6.0)
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.warning("alert webhook failed: %s", exc)
            return False
