"""Automations: bind something happening to something being done.

Sentry already knows things nothing else on the LAN knows — a person is on the
driveway, the river is rising, it is twenty past sunset. This is the other half:
turning that into an action without handing the job to a separate
home-automation stack.

An automation is three parts:

  * a **trigger** — either an event Sentry raised ('person on the driveway'),
    or nothing at all, in which case it only ever runs when its URL is called;
  * a **window** — optional days and time-of-day, so "porch light on" can mean
    "after dark" rather than "at noon as well";
  * a list of **actions** — switch a relay, move window coverings, call an
    outside URL.

Every automation gets a URL regardless of its trigger:

    /api/hook/run/<slug>?token=<t>

which is the generic "poke Sentry" endpoint: a Shelly's own input button, a
phone shortcut, a scene controller, a cron job on another machine.

Two design points worth stating.

**Actions run on a worker thread, never on the caller's.** Events arrive on the
camera-polling thread; running a slow relay call inline would stall detection
for every other camera. The queue also means an automation that jams cannot
take the detector down with it.

**Every automation has a cooldown.** A person loitering in frame raises an
event every poll, and without this the porch light would be commanded dozens of
times a minute. Same reasoning as the alert dispatcher's, and the default is
deliberately not zero.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any

import httpx

log = logging.getLogger("nvr.automations")

# How long an outbound webhook may take before we give up on it.
WEBHOOK_TIMEOUT = 8.0

# Actions an automation can take. Kept small and explicit: a driver-style
# registry would be more general, but this is a list a person reads.
ACTION_KINDS = ("device", "covering", "webhook")

TRIGGER_KINDS = ("event", "hook")


class AutomationError(RuntimeError):
    """An automation could not be run, or is not valid."""


def _day_enabled(days: int, weekday: int) -> bool:
    return bool(days & (1 << weekday))


def in_window(days: int, start_min: int | None, end_min: int | None,
              weekday: int, minute: int) -> bool:
    """Whether now falls inside an automation's window.

    An automation with no times set runs whenever its trigger fires; that is
    the common case, so it must be the cheap one.
    """
    if start_min is None or end_min is None:
        return _day_enabled(days, weekday) if days != 127 else True
    if start_min == end_min:
        return False
    if start_min < end_min:
        return _day_enabled(days, weekday) and start_min <= minute < end_min
    # Wraps midnight; the tail belongs to the day the window started on.
    if minute >= start_min:
        return _day_enabled(days, weekday)
    if minute < end_min:
        return _day_enabled(days, (weekday - 1) % 7)
    return False


def validate_actions(raw: Any) -> list[dict[str, Any]]:
    """Check an action list before it is stored, not when it fires.

    A typo that only surfaces at 2am when the driveway camera sees somebody is
    a bad way to find out the automation was never going to work.
    """
    if not isinstance(raw, list):
        raise AutomationError("actions must be a list")
    if not raw:
        raise AutomationError("add at least one action")
    checked = []
    for index, action in enumerate(raw):
        where = f"action {index + 1}"
        if not isinstance(action, dict):
            raise AutomationError(f"{where} must be an object")
        kind = action.get("kind")
        if kind not in ACTION_KINDS:
            raise AutomationError(
                f"{where}: kind must be one of {', '.join(ACTION_KINDS)}"
            )
        if kind == "device":
            if not action.get("device_id"):
                raise AutomationError(f"{where}: needs a device")
            state = (action.get("state") or "on").lower()
            if state not in ("on", "off", "toggle"):
                raise AutomationError(f"{where}: state must be on, off or toggle")
            action = {"kind": "device", "device_id": str(action["device_id"]),
                      "state": state}
            seconds = raw[index].get("for_seconds")
            if seconds not in (None, "", 0):
                try:
                    seconds = int(seconds)
                except (TypeError, ValueError):
                    raise AutomationError(f"{where}: for_seconds must be a number")
                if not 1 <= seconds <= 86400:
                    raise AutomationError(f"{where}: for_seconds must be 1..86400")
                action["for_seconds"] = seconds
        elif kind == "covering":
            position = action.get("position")
            try:
                position = int(position)
            except (TypeError, ValueError):
                raise AutomationError(f"{where}: position must be a number")
            if not 0 <= position <= 100:
                raise AutomationError(f"{where}: position must be 0..100")
            action = {"kind": "covering", "position": position,
                      "layer": action.get("layer") or None,
                      "room_id": action.get("room_id")}
        elif kind == "webhook":
            url = (action.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                raise AutomationError(f"{where}: url must start with http:// or https://")
            method = (action.get("method") or "POST").upper()
            if method not in ("GET", "POST"):
                raise AutomationError(f"{where}: method must be GET or POST")
            action = {"kind": "webhook", "url": url, "method": method,
                      "body": action.get("body") or {}}
        checked.append(action)
    return checked


def validate_match(raw: Any) -> dict[str, Any]:
    """The event pattern. Absent keys match anything, which is the point:
    'any person, any camera' should be the easiest thing to express."""
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise AutomationError("match must be an object")
    out: dict[str, Any] = {}
    for key in ("event_type", "camera_id"):
        value = raw.get(key)
        if value not in (None, "", "any"):
            out[key] = str(value)
    return out


def matches(pattern: dict[str, Any], event: dict[str, Any]) -> bool:
    for key, wanted in pattern.items():
        if str(event.get(key) or "") != wanted:
            return False
    return True


class AutomationService:
    """Runs automations off a queue, on its own thread."""

    def __init__(self, config: Any, db: Any, devices: Any = None,
                 shades: Any = None):
        self.config = config
        self.db = db
        # Injected so tests can watch what an automation actually did without
        # touching a relay or transmitting on 433 MHz.
        self.devices = devices
        self.shades = shades
        self._queue: queue.Queue = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_run: dict[int, float] = {}
        # (deadline, device_id, state) for "on for five minutes" actions.
        self._reverts: list[tuple[float, str, bool]] = []
        self._reverts_lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="automations", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                item = None
            if item is not None:
                automation_id, context = item
                try:
                    self.run_now(automation_id, context)
                except Exception:
                    log.exception("automation %s failed", automation_id)
            self._apply_due_reverts()

    # -- triggering ---------------------------------------------------------

    def handle_event(self, event: dict[str, Any]) -> None:
        """Called by the alert dispatcher for every event it records.

        Deliberately does no work beyond matching: anything slow happens on the
        worker thread, because this runs on whichever detector raised the event.
        """
        try:
            rows = self.db.automations(enabled_only=True)
        except Exception:
            log.exception("could not load automations")
            return
        now = time.localtime()
        for row in rows:
            if row["trigger_kind"] != "event":
                continue
            try:
                pattern = json.loads(row["match"] or "{}")
            except ValueError:
                continue
            if not matches(pattern, event):
                continue
            if not in_window(row["days"], row["start_min"], row["end_min"],
                             now.tm_wday, now.tm_hour * 60 + now.tm_min):
                continue
            if not self._cooldown_ok(row):
                continue
            self.enqueue(row["id"], {"event": event})

    def enqueue(self, automation_id: int, context: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait((automation_id, context))
        except queue.Full:
            # Dropping is better than blocking a detector thread. Loud, because
            # a full queue means something is wedged.
            log.warning("automation queue full; dropped %s", automation_id)

    def _cooldown_ok(self, row: Any) -> bool:
        cooldown = row["cooldown_seconds"] or 0
        if cooldown <= 0:
            return True
        now = time.time()
        last = self._last_run.get(row["id"], 0.0)
        if now - last < cooldown:
            return False
        self._last_run[row["id"]] = now
        return True

    # -- running ------------------------------------------------------------

    def run_now(self, automation_id: int, context: dict[str, Any] | None = None
                ) -> dict[str, Any]:
        """Run every action, recording what happened. One failing action does
        not stop the rest — half a scene is better than none, and the error is
        surfaced rather than swallowed."""
        row = self.db.automation(automation_id)
        if row is None:
            raise AutomationError("automation not found")
        try:
            actions = json.loads(row["actions"] or "[]")
        except ValueError:
            raise AutomationError("automation has unreadable actions")

        errors: list[str] = []
        for action in actions:
            try:
                self._run_action(action, context or {})
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                log.warning("automation %s action failed: %s", row["slug"], exc)
                errors.append(str(exc))
        self.db.update_automation(
            automation_id,
            last_run=time.time(),
            last_error="; ".join(errors) if errors else None,
            run_count=(row["run_count"] or 0) + 1,
        )
        return {"ran": len(actions), "errors": errors}

    def _run_action(self, action: dict[str, Any], context: dict[str, Any]) -> None:
        kind = action.get("kind")
        if kind == "device":
            self._run_device(action)
        elif kind == "covering":
            self._run_covering(action)
        elif kind == "webhook":
            self._run_webhook(action, context)
        else:
            raise AutomationError(f"unknown action kind {kind!r}")

    def _run_device(self, action: dict[str, Any]) -> None:
        if self.devices is None:
            raise AutomationError("device control unavailable")
        device = self.db.device(action["device_id"])
        if device is None or not device["enabled"]:
            raise AutomationError(f"device {action['device_id']} unavailable")
        state = action.get("state", "on")
        if state == "toggle":
            result = self.devices.toggle(device)
        else:
            result = self.devices.set_state(device, state == "on")
        self.db.update_device(
            device["id"], last_state=1 if result else 0,
            last_seen=time.time(), last_error=None,
        )
        seconds = action.get("for_seconds")
        if seconds:
            # "On for five minutes." Scheduled rather than slept, so the worker
            # stays free for whatever else fires in the meantime.
            with self._reverts_lock:
                self._reverts.append(
                    (time.time() + seconds, device["id"], not result)
                )

    def _apply_due_reverts(self) -> None:
        now = time.time()
        with self._reverts_lock:
            due = [r for r in self._reverts if r[0] <= now]
            self._reverts = [r for r in self._reverts if r[0] > now]
        for _deadline, device_id, state in due:
            device = self.db.device(device_id)
            if device is None or not device["enabled"] or self.devices is None:
                continue
            try:
                self.devices.set_state(device, state)
                self.db.update_device(
                    device_id, last_state=1 if state else 0,
                    last_seen=time.time(), last_error=None,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("automation could not revert %s: %s", device_id, exc)

    def _run_covering(self, action: dict[str, Any]) -> None:
        if self.shades is None:
            raise AutomationError("covering control unavailable")
        room_id = action.get("room_id")
        rows = (self.db.coverings(room_id=int(room_id), enabled_only=True)
                if room_id not in (None, "", 0)
                else self.db.coverings(enabled_only=True))
        layer = action.get("layer")
        if layer:
            rows = [r for r in rows if r["layer"] == layer]
        if not rows:
            raise AutomationError("no coverings match that group")
        position = int(action["position"])
        failures = []
        for covering in rows:
            hub = self.db.shade_hub(covering["hub_id"])
            if hub is None or not hub["enabled"]:
                continue
            try:
                self.shades.set_position(
                    hub["host"], covering["id"], covering["device_type"],
                    position, api_key=hub["api_key"], hub_token=hub["token"],
                )
                self.db.update_covering(
                    covering["id"], last_position=position,
                    last_seen=time.time(), last_error=None,
                )
            except Exception as exc:  # noqa: BLE001
                self.db.update_covering(
                    covering["id"], last_error=str(exc), last_seen=time.time()
                )
                failures.append(f"{covering['name']}: {exc}")
        if failures:
            raise AutomationError("; ".join(failures))

    def _run_webhook(self, action: dict[str, Any], context: dict[str, Any]) -> None:
        payload = dict(action.get("body") or {})
        event = context.get("event")
        if event:
            payload.setdefault("event", event)
        payload.setdefault("source", "sentry")
        try:
            if action.get("method") == "GET":
                response = httpx.get(action["url"], timeout=WEBHOOK_TIMEOUT)
            else:
                response = httpx.post(action["url"], json=payload,
                                      timeout=WEBHOOK_TIMEOUT)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AutomationError(f"webhook failed: {exc}") from exc
