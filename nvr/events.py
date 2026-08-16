"""Smart-event ingestion from camera onboard AI.

Reolink cameras run person/vehicle/animal detection on-device; this service
polls that state and turns each *rising edge* (nothing -> detected) into an
event via the alert dispatcher, which records it (for the timeline) and
notifies. Polling the camera's own AI is free, needs no local ML, and reuses
the native Reolink client.

Shaped like the other background services (RetentionService/SchedulerService):
a daemon thread on a fixed interval, resilient to any single camera being
unreachable. Only runs when alerts are enabled and there's something to detect;
non-Reolink cameras are skipped (their buttons already degrade to no-ops).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from . import config
from .reolink import ReolinkClient

log = logging.getLogger("nvr.events")


class EventService:
    def __init__(self, config: Any, db: Any, alerts: Any):
        self.config = config
        self.db = db
        self.alerts = alerts
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run: float | None = None
        # Last-seen alarm state per "<camera>:<kind>", to fire on rising edge only.
        self._state: dict[str, bool] = {}
        # One long-lived Reolink client per camera, so we log in once and reuse
        # the token instead of a fresh login every poll (see _client_for).
        self._clients: dict[str, ReolinkClient] = {}

    @property
    def cfg(self) -> Any:
        return self.config.alerts

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _wanted(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.detect and self.cfg.poll_seconds > 0)

    def start(self) -> None:
        # No point polling cameras if the feature is off or nothing is wanted.
        if not self._wanted() or self.running():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="events", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for cid in list(self._clients):
            self._drop_client(cid)

    def apply(self) -> None:
        """Start or stop the poller to match the current config — called after a
        live settings change toggles alerts on or off."""
        if self._wanted():
            self.start()
        elif self.running():
            self.stop()

    def _loop(self) -> None:
        # Give go2rtc/recorders a moment before we start hammering camera APIs.
        self._stop.wait(10.0)
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("event poll failed")
            # Coerced defensively: this sleep sits AFTER the try, so anything
            # thrown here would kill the poller outright — silently, permanently,
            # and with the UI still reporting detection as enabled.
            self._stop.wait(
                config.safe_interval(self.cfg.poll_seconds, default=2.0, minimum=0.5)
            )

    def poll_once(self) -> None:
        detect = {d.strip().lower() for d in (self.cfg.detect or []) if d}
        for cam in self.db.cameras(enabled_only=True):
            if not _is_reolink(cam):
                continue
            self._poll_camera(cam, detect)
        self.last_run = time.time()

    def _poll_camera(self, cam: Any, detect: set[str]) -> None:
        states = self._read_states(cam, detect)
        if states is None:
            return  # unreachable / no AI — leave prior edge state untouched
        for kind, active in states.items():
            if kind not in detect:
                continue
            key = f"{cam['id']}:{kind}"
            was = self._state.get(key, False)
            self._state[key] = active
            if active and not was:
                self._raise(cam, kind)

    def _client_for(self, cam: Any) -> ReolinkClient:
        """One long-lived client per camera: log in once (lazily, on first API
        call) and reuse the token across polls. Reolink caps concurrent sessions,
        so a fresh login every poll quickly hits "Login: max session" and starves
        other API use (the light / night-vision reads). Recreated if the camera's
        IP changed under it."""
        cid = cam["id"]
        host = str(cam["host"])
        client = self._clients.get(cid)
        if client is not None and client.host != host:
            self._drop_client(cid)
            client = None
        if client is None:
            client = ReolinkClient(
                host=host,
                username=str(cam["username"] or ""),
                password=str(cam["password"] or ""),
            )
            self._clients[cid] = client
        return client

    def _drop_client(self, cid: str) -> None:
        client = self._clients.pop(cid, None)
        if client is None:
            return
        try:
            client.logout()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass

    def _read_states(self, cam: Any, detect: set[str]) -> dict[str, bool] | None:
        # Reuse the camera's session; on any failure (expired token, a blip) drop
        # the client and retry once with a fresh login before giving up.
        for attempt in (0, 1):
            try:
                client = self._client_for(cam)
                states = dict(client.ai_state())
                if "motion" in detect:
                    motion = client.motion_state()
                    if motion is not None:
                        states["motion"] = motion
                return states
            except Exception as exc:
                self._drop_client(cam["id"])
                if attempt:
                    log.debug("AI poll failed for %s: %s", cam["id"], exc)
        return None

    def _raise(self, cam: Any, kind: str) -> None:
        log.info("event: %s on %s", kind, cam["id"])
        try:
            self.alerts.emit(
                type=kind,
                camera_id=cam["id"],
                camera_name=cam["name"],
            )
        except Exception:
            log.exception("failed to raise %s event for %s", kind, cam["id"])


def _is_reolink(cam: Any) -> bool:
    try:
        return str(cam["brand"] or "").strip().lower() == "reolink"
    except (KeyError, IndexError, TypeError):
        return False
