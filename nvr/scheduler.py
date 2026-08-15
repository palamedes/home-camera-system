"""Camera schedules: time-of-day rules that drive per-camera actions.

Each row in the `schedules` table is one rule — "on these weekdays, between
these two minutes-of-day, hold this action". A background thread (shaped like
RetentionService) wakes on an interval, works out the current weekday and
minute in *server local time*, and applies whatever is in force:

  * record      — within an active window a camera records; when a camera has
                  record-schedules and none is active, it stops. Only cameras
                  that own at least one record-schedule are touched, so manual
                  recording control on unscheduled cameras is never fought.
  * light       — spotlight on inside the window, off outside (edge-triggered).
  * nightvision — the configured mode is applied when a window *begins*
                  (edge-triggered), not re-sent every tick.

Windows wrap past midnight when end_min < start_min. A wrapping window's
weekday mask is attributed to the day the window *starts*: a Monday 22:00–06:00
rule is in force from Monday 22:00 through Tuesday 06:00.

Light and night-vision are carried out by nvr.camera_control, which is authored
separately. It is imported softly so this module stays importable (and the loop
stays alive) whether or not that module exists yet.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

log = logging.getLogger("nvr.scheduler")

# Camera hardware control (spotlight / IR-cut) lives in a sibling module built
# on another branch. Soft-import so this file is self-contained and green even
# when that module is absent; the loop degrades to a logged warning.
try:  # pragma: no cover - trivial import guard
    from . import camera_control
except Exception:  # noqa: BLE001 - any import failure means "not available"
    camera_control = None  # type: ignore[assignment]

from . import devices, shades

VALID_ACTIONS = frozenset({"record", "light", "nightvision", "power", "cover"})
VALID_NV_MODES = frozenset({"auto", "color", "bw"})

# How often the loop re-evaluates. Minute-resolution windows don't need finer.
INTERVAL = 30.0


def day_enabled(days: int, weekday: int) -> bool:
    """Whether `weekday` (0=Mon .. 6=Sun) is set in the 7-bit `days` mask."""
    return bool(days & (1 << weekday))


def in_window(
    days: int, start_min: int, end_min: int, weekday: int, minute: int
) -> bool:
    """Is (weekday, minute) inside this schedule's window?

    weekday is 0=Mon .. 6=Sun; minute is 0..1439 (minutes past midnight).

    * start_min < end_min  -> a same-day window [start, end) on an enabled day.
    * end_min   < start_min -> wraps midnight: [start, 1440) on an enabled day
      plus [0, end) on the following day, which belongs to the *start* day's
      mask bit.
    * start_min == end_min -> empty (never active).
    """
    if start_min == end_min:
        return False
    if start_min < end_min:
        return day_enabled(days, weekday) and start_min <= minute < end_min
    # Wrapping window.
    if minute >= start_min:
        return day_enabled(days, weekday)
    if minute < end_min:
        prev = (weekday - 1) % 7  # the tail belongs to the day it started on
        return day_enabled(days, prev)
    return False


class SchedulerService:
    def __init__(self, config: Any, db: Any, recording: Any = None):
        self.config = config
        self.db = db
        # Optional: needed to make record on/off take effect. When absent the
        # DB flag is still set but the recorder isn't nudged (fine for tests).
        self.recording = recording
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run: float | None = None
        # Last-applied desired state, to avoid redundant work each tick.
        self._record_state: dict[str, int] = {}
        self._light_state: dict[str, bool] = {}
        self._nv_inside: dict[int, bool] = {}
        # Devices record their state only after the relay actually accepted the
        # command, so an unreachable relay is retried on the next tick instead
        # of being assumed applied.
        self._device_state: dict[str, bool] = {}
        # Covering schedules fire once when their window opens, so all we track
        # is whether each rule was inside its window on the previous pass.
        self._cover_inside: dict[int, bool] = {}

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("scheduler pass failed")
            self._stop.wait(INTERVAL)

    def run_once(self) -> None:
        """Evaluate every enabled schedule against 'now' (server local time)."""
        lt = time.localtime()
        self.apply(lt.tm_wday, lt.tm_hour * 60 + lt.tm_min)
        self.last_run = time.time()

    # ------------------------------------------------------------------ apply

    def apply(self, weekday: int, minute: int) -> None:
        """Apply all schedules for a given weekday/minute. Pure w.r.t. clock —
        split out from run_once so tests can drive it deterministically."""
        rows = [s for s in self.db.schedules() if s["enabled"]]
        self._apply_record(rows, weekday, minute)
        self._apply_light(rows, weekday, minute)
        self._apply_nightvision(rows, weekday, minute)
        self._apply_device_power(rows, weekday, minute)
        self._apply_coverings(rows, weekday, minute)

    def _apply_coverings(self, rows: list[Any], weekday: int, minute: int) -> None:
        """Move window coverings when a schedule's window opens.

        Rising edge only, unlike relays. A relay schedule is authoritative
        ("on 18:00-23:00" also means "off otherwise"), but a shade is not: if
        you raise it by hand at noon it should stay up, not be dragged back
        down every thirty seconds until the window ends. "Open at seven, close
        at sunset" is naturally two rules, which is also how a person says it.
        """
        for s in rows:
            if s["action"] != "cover":
                continue
            active = in_window(
                s["days"], s["start_min"], s["end_min"], weekday, minute
            )
            sid = s["id"]
            was_inside = self._cover_inside.get(sid, False)
            self._cover_inside[sid] = active
            if not active or was_inside:
                continue
            try:
                position = max(0, min(100, int(s["value"])))
            except (TypeError, ValueError):
                log.warning("covering schedule %s has a bad position %r",
                            sid, s["value"])
                continue
            for covering in self._covering_targets(s):
                self._move_covering(covering, position)

    def _covering_targets(self, schedule: Any) -> list[Any]:
        """Resolve a schedule to the coverings it should move.

        Either one named covering, or a group: NULL room means every room and
        NULL layer means both layers, so a rule with neither set is "the whole
        house".
        """
        if schedule["covering_id"]:
            covering = self.db.covering(schedule["covering_id"])
            return [covering] if covering and covering["enabled"] else []
        room_id = schedule["covering_room_id"]
        rows = (self.db.coverings(room_id=room_id, enabled_only=True)
                if room_id is not None else self.db.coverings(enabled_only=True))
        layer = schedule["covering_layer"]
        if layer:
            rows = [r for r in rows if r["layer"] == layer]
        return rows

    def _move_covering(self, covering: Any, position: int) -> None:
        """One covering, best effort — a motor out of radio range must not stop
        the rest of the group, nor kill the scheduler loop."""
        hub = self.db.shade_hub(covering["hub_id"])
        if hub is None or not hub["enabled"]:
            return
        try:
            shades.set_position(
                hub["host"], covering["id"], covering["device_type"], position,
                api_key=hub["api_key"], hub_token=hub["token"],
            )
        except Exception as exc:  # noqa: BLE001 - one bad motor must not stop the pass
            log.warning("schedule could not move %s: %s", covering["id"], exc)
            self.db.update_covering(
                covering["id"], last_error=str(exc), last_seen=time.time()
            )
            return
        self.db.update_covering(
            covering["id"], last_position=position, last_seen=time.time(),
            last_error=None,
        )
        log.info("schedule moved %s to %s", covering["id"], position)

    def _apply_device_power(self, rows: list[Any], weekday: int, minute: int) -> None:
        """Switch relays on/off for their windows.

        Same aggregate-then-edge-trigger shape as lights: a device is on if ANY
        of its windows is active, and we only talk to it when that flips. The
        difference is that the new state is recorded only once the device has
        accepted it, so a relay that was unplugged gets retried next tick rather
        than being silently written off.

        A power schedule is authoritative, like any timer: "on 18:00-23:00" also
        means "off the rest of the time", so a scheduled device is asserted off
        outside its windows. Because it is edge-triggered, that assertion happens
        only when the desired state changes (or on the first pass after a
        restart) — so switching the light on by hand at midnight sticks until the
        next window boundary rather than being fought every 30 seconds.
        """
        wants: dict[str, bool] = {}
        for s in rows:
            if s["action"] != "power" or not s["device_id"]:
                continue
            active = in_window(
                s["days"], s["start_min"], s["end_min"], weekday, minute
            )
            wants[s["device_id"]] = wants.get(s["device_id"], False) or active

        for device_id, want in wants.items():
            if self._device_state.get(device_id) == want:
                continue
            device = self.db.device(device_id)
            if device is None or not device["enabled"]:
                continue
            try:
                devices.set_state(device, want)
            except Exception as exc:
                # Left out of _device_state, so the next pass tries again.
                log.warning("schedule could not switch %s: %s", device_id, exc)
                self.db.update_device(
                    device_id, last_error=str(exc), last_seen=time.time()
                )
                continue
            self._device_state[device_id] = want
            self.db.update_device(
                device_id, last_state=1 if want else 0,
                last_seen=time.time(), last_error=None,
            )
            log.info("schedule switched %s %s", device_id, "on" if want else "off")

    def _apply_record(self, rows: list[Any], weekday: int, minute: int) -> None:
        # Aggregate per camera: a camera records if ANY of its record windows is
        # active now. Cameras with no record-schedule are absent from this map
        # and therefore never touched.
        wants: dict[str, int] = {}
        for s in rows:
            if s["action"] != "record":
                continue
            active = in_window(
                s["days"], s["start_min"], s["end_min"], weekday, minute
            )
            cam_id = s["camera_id"]
            wants[cam_id] = 1 if (wants.get(cam_id) or active) else 0

        changed = False
        for cam_id, want in wants.items():
            cam = self.db.camera(cam_id)
            if cam is None or cam["archived"]:
                # db.camera() resolves archived rows on purpose (History needs
                # them), so a removed camera's schedules would otherwise keep
                # firing — flipping its record flag so a later Restore starts
                # recording unasked, with no way to see or delete the rule.
                continue
            if int(cam["record"] or 0) != want:
                # Re-asserts the schedule even against a manual toggle, and
                # clears any bounded record_until window.
                self.db.update_camera(cam_id, record=want, record_until=None)
                changed = True
                log.info(
                    "schedule set %s recording %s", cam_id, "on" if want else "off"
                )
            self._record_state[cam_id] = want

        if changed and self.recording is not None:
            try:
                self.recording.sync()
            except Exception:
                log.exception("recording.sync() after schedule change failed")

    def _apply_light(self, rows: list[Any], weekday: int, minute: int) -> None:
        wants: dict[str, bool] = {}
        for s in rows:
            if s["action"] != "light":
                continue
            active = in_window(
                s["days"], s["start_min"], s["end_min"], weekday, minute
            )
            cam_id = s["camera_id"]
            wants[cam_id] = wants.get(cam_id, False) or active

        for cam_id, want in wants.items():
            if self._light_state.get(cam_id) == want:
                continue  # edge-triggered: only act when the state flips
            self._light_state[cam_id] = want
            self._call_control(
                "set_light", cam_id, lambda cam: camera_control.set_light(cam, want)
            )

    def _apply_nightvision(self, rows: list[Any], weekday: int, minute: int) -> None:
        for s in rows:
            if s["action"] != "nightvision":
                continue
            active = in_window(
                s["days"], s["start_min"], s["end_min"], weekday, minute
            )
            sid = s["id"]
            was_inside = self._nv_inside.get(sid, False)
            self._nv_inside[sid] = active
            if active and not was_inside:
                # Rising edge only — apply the mode once when the window opens.
                mode = s["value"] or "auto"
                self._call_control(
                    "set_night_vision", s["camera_id"],
                    lambda cam, m=mode: camera_control.set_night_vision(cam, mode=m),
                )

    def _call_control(self, what: str, camera_id: str, fn: Any) -> None:
        """Run a camera_control action, swallowing (logging) any failure so one
        misbehaving camera or a missing module never kills the loop."""
        if camera_control is None:
            log.warning(
                "camera_control unavailable; skipping %s for %s", what, camera_id
            )
            return
        cam = self.db.camera(camera_id)
        if cam is None or cam["archived"]:
            # A camera removed with "Keep footage" must be inert: without this
            # its floodlight still switches on every evening.
            return
        try:
            fn(cam)
        except Exception:
            log.warning("camera_control.%s failed for %s", what, camera_id, exc_info=True)
