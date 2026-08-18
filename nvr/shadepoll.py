"""Keeping Sentry's idea of where the blinds are honest.

Nothing refreshed covering state on a timer, so using the wall remote left the
page showing the last position Sentry ITSELF commanded — a confidently wrong
number, which is worse than showing none.

The hub is documented as pushing state changes on a multicast group, which
would make this unnecessary. It does not reach us: ninety seconds of listening
on that group heard nothing, because the access point filters multicast in both
directions. Polling is the only option on this network.

Two rates, for two different reasons:

  * **Hourly**, for the ordinary case. Each read is a round trip over 433 MHz
    and ten motors is real airtime, spent on information that changes when
    somebody touches a remote — rarely, and not urgently.

  * **A short burst after Sentry moves something**, because a motor takes
    seconds to travel and the value recorded at command time is the *target*.
    Without this the position is aspirational until the next hour, and it is
    also the only way to find out whether a shade actually arrived.

A motor that keeps failing is backed off rather than retried every cycle: at
the far end of a weak link, one dead shade should not soak the radio the other
nine are sharing.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from . import config, shades

log = logging.getLogger("nvr.shadepoll")

# The ordinary rate. Hourly is plenty for something that only changes when a
# person touches a remote.
POLL_INTERVAL = 3600.0

# After Sentry issues a move: check this often, for this long. Long enough to
# outlast a full-height traverse, short enough not to matter.
BURST_INTERVAL = 6.0
BURST_SECONDS = 42.0

# How long the loop sleeps between wake-ups. Short relative to POLL_INTERVAL so
# a burst can be serviced promptly without the hourly pass being early.
TICK = 3.0

# Pacing between motors within one pass, so a sweep of ten does not monopolise
# the radio.
BETWEEN_COVERINGS = 0.4

# Consecutive failures before a covering is rested, and for how many passes.
BACKOFF_AFTER = 3
BACKOFF_PASSES = 6


class ShadePollService:
    def __init__(self, config_obj: Any, db: Any):
        self.config = config_obj
        self.db = db
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run: float | None = None
        self._next_full = 0.0
        # covering_id -> consecutive failures, and passes still to skip.
        self._failures: dict[str, int] = {}
        self._resting: dict[str, int] = {}
        # covering_id -> when its burst ends.
        self._burst_until: dict[str, float] = {}
        self._burst_next: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="shadepoll", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # Let the rest of the system settle before touching the radio.
        self._stop.wait(30.0)
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("shade poll failed")
            # Guarded: this sleep sits after the try, so anything raised here
            # would kill the thread silently and permanently.
            self._stop.wait(config.safe_interval(TICK, default=3.0, minimum=1.0))

    # -- the passes ---------------------------------------------------------

    def watch(self, covering_id: str) -> None:
        """Sentry just moved this one — watch it until it settles.

        Called from the request that issued the move, so it must not block or
        talk to anything.
        """
        now = time.time()
        with self._lock:
            self._burst_until[covering_id] = now + BURST_SECONDS
            self._burst_next[covering_id] = now + BURST_INTERVAL
            # A move proves the link works, so forgive earlier failures rather
            # than leaving it rested when it is plainly reachable.
            self._failures.pop(covering_id, None)
            self._resting.pop(covering_id, None)

    def run_once(self) -> None:
        now = time.time()
        self._run_bursts(now)
        if now >= self._next_full:
            self._next_full = now + config.safe_interval(
                POLL_INTERVAL, default=3600.0, minimum=60.0
            )
            self.poll_all()
        self.last_run = time.time()

    def _run_bursts(self, now: float) -> None:
        with self._lock:
            due = [cid for cid, until in self._burst_until.items()
                   if now < until and now >= self._burst_next.get(cid, 0.0)]
            for cid in due:
                self._burst_next[cid] = now + BURST_INTERVAL
            expired = [cid for cid, until in self._burst_until.items() if now >= until]
            for cid in expired:
                self._burst_until.pop(cid, None)
                self._burst_next.pop(cid, None)
        for covering_id in due:
            covering = self.db.covering(covering_id)
            if covering is not None:
                self._poll(covering, count_failures=False)

    def poll_all(self) -> int:
        """Read every enabled covering on every enabled hub."""
        polled = 0
        for covering in self.db.coverings(enabled_only=True):
            if self._stop.is_set():
                break
            if self._rested(covering["id"]):
                continue
            if self._poll(covering):
                polled += 1
            self._stop.wait(BETWEEN_COVERINGS)
        if polled:
            log.info("polled %d covering(s)", polled)
        return polled

    def _rested(self, covering_id: str) -> bool:
        """Whether this one is being skipped after repeated failures."""
        left = self._resting.get(covering_id, 0)
        if left <= 0:
            return False
        self._resting[covering_id] = left - 1
        return True

    def _poll(self, covering: Any, count_failures: bool = True) -> bool:
        hub = self.db.shade_hub(covering["hub_id"])
        if hub is None or not hub["enabled"]:
            return False
        try:
            data = shades.read_device(
                hub["host"], covering["id"], covering["device_type"]
            )
        except Exception as exc:  # noqa: BLE001 - one bad motor must not stop the pass
            if count_failures:
                self._note_failure(covering["id"], exc)
            return False

        summary = shades.summarise(data)
        fields: dict[str, Any] = {"last_seen": time.time(), "last_error": None}
        if summary.get("position") is not None:
            fields["last_position"] = summary["position"]
        if summary.get("battery_mv") is not None:
            fields["battery_mv"] = summary["battery_mv"]
        if summary.get("rssi") is not None:
            fields["rssi"] = summary["rssi"]
        fields["bidirectional"] = 1 if summary.get("bidirectional") else 0
        self.db.update_covering(covering["id"], **fields)
        self._failures.pop(covering["id"], None)
        return True

    def _note_failure(self, covering_id: str, exc: Exception) -> None:
        count = self._failures.get(covering_id, 0) + 1
        self._failures[covering_id] = count
        self.db.update_covering(
            covering_id, last_error=str(exc), last_seen=time.time()
        )
        if count >= BACKOFF_AFTER:
            # At the far end of a weak link, one dead shade should not soak the
            # radio the other nine are sharing.
            self._resting[covering_id] = BACKOFF_PASSES
            self._failures[covering_id] = 0
            log.warning("resting %s after %d failures", covering_id, count)
