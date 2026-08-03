"""Ring-buffer retention.

Three independent limits, whichever bites first:

  * max_age_days  — footage older than this is gone regardless of space.
  * max_usage     — total recorded bytes, as a size or a share of the disk.
  * a hard free-space floor — the backstop. Filling the root filesystem on this
    box would take down the database and the OS with it, so this one is not
    configurable and always wins.

Deletes oldest-first and removes the index row in the same pass, so the
timeline never advertises footage that is no longer on disk.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("nvr.retention")

# Never let the filesystem get closer to full than this.
FREE_SPACE_FLOOR = 5 * 1024**3  # 5 GB

# Deleted per pass. Bounded so a wildly over-quota system prunes gradually
# instead of blocking for minutes on one enormous unlink storm.
BATCH = 500


class RetentionService:
    def __init__(self, config: Any, db: Any):
        self.config = config
        self.db = db
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run: float | None = None
        self.last_deleted = 0

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="retention", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # Let the indexer get a pass in before the first prune.
        self._stop.wait(60.0)
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("retention pass failed")
            self._stop.wait(300.0)

    def _delete(self, row: Any) -> int:
        """Remove one segment from disk and index. Returns bytes freed."""
        path = Path(row["path"])
        size = int(row["size"] or 0)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not delete %s: %s", path, exc)
            return 0
        self.db.delete_segment(row["id"])
        return size

    def _camera_windows(self) -> tuple[dict[str, tuple[int, int]], int]:
        """Per-camera (hard_cap, rolling_min) in seconds, plus the global cap.

        hard_cap: delete footage older than this no matter what. Falls back to
        the global limit when unset (NULL); 0 means "never delete by age".
        rolling_min: the recent window protected from space-based pruning.
        """
        global_hard = self.config.storage.max_age_days * 86400
        windows: dict[str, tuple[int, int]] = {}
        for c in self.db.cameras():
            rs = c["retention_seconds"]
            hard = global_hard if rs is None else int(rs)
            rolling = int(c["rolling_keep_seconds"] or 0)
            windows[c["id"]] = (hard, rolling)
        return windows, global_hard

    def run_once(self) -> dict[str, Any]:
        deleted = 0
        freed = 0
        now = time.time()
        windows, global_hard = self._camera_windows()

        # 1. Hard age cap — per camera. Footage older than the cap is deleted
        #    regardless of free space (this is the "delete no matter what" tier).
        for cam_id, (hard, _rolling) in windows.items():
            if hard > 0:
                d, f = self._prune_older_than(cam_id, now - hard)
                deleted += d
                freed += f

        # Segments left behind by a removed camera fall back to the global cap.
        if global_hard > 0:
            present = {r["camera_id"] for r in self.db.query(
                "SELECT DISTINCT camera_id FROM segments"
            )}
            for orphan_id in present - set(windows):
                d, f = self._prune_older_than(orphan_id, now - global_hard)
                deleted += d
                freed += f

        # A segment is protected from the space tiers while it is newer than its
        # camera's rolling-keep window. Recent footage (the rolling minimum) thus
        # survives space pressure — only the free-space safety floor overrides it.
        def protected(row: Any) -> bool:
            rolling = windows.get(row["camera_id"], (0, 0))[1]
            return rolling > 0 and (now - float(row["start_ts"])) < rolling

        # 2. Size quota — delete the oldest UNPROTECTED footage first, and stop
        #    rather than eat into a rolling window even if that leaves us over
        #    quota (the free-space floor below is the real backstop).
        try:
            quota = self.config.storage.max_bytes()
        except (OSError, ValueError):
            quota = 0
        if quota > 0:
            total = self.db.total_size()
            while total > quota:
                rows = self.db.oldest_segments(limit=BATCH)
                if not rows:
                    break
                progressed = False
                for row in rows:
                    if total <= quota:
                        break
                    if protected(row):
                        continue
                    size = self._delete(row)
                    total -= size
                    freed += size
                    deleted += 1
                    progressed = True
                if not progressed:
                    break  # only protected footage left; respect it over quota

        # 3. Free-space backstop. First reclaim unprotected footage; only if the
        #    disk is still dangerously full do we override rolling protection —
        #    filling the root filesystem would take down the OS and database.
        usage = shutil.disk_usage(self.config.storage.recordings_dir)
        if usage.free < FREE_SPACE_FLOOR:
            while usage.free < FREE_SPACE_FLOOR:
                rows = self.db.oldest_segments(limit=BATCH)
                unprotected = [r for r in rows if not protected(r)]
                if not unprotected:
                    break
                for row in unprotected:
                    freed += self._delete(row)
                    deleted += 1
                usage = shutil.disk_usage(self.config.storage.recordings_dir)

            while usage.free < FREE_SPACE_FLOOR:
                rows = self.db.oldest_segments(limit=BATCH)
                if not rows:
                    log.error(
                        "disk below %s free but no recordings left to prune",
                        FREE_SPACE_FLOOR,
                    )
                    break
                log.warning(
                    "disk below %s free; overriding rolling-keep protection",
                    FREE_SPACE_FLOOR,
                )
                for row in rows:
                    freed += self._delete(row)
                    deleted += 1
                usage = shutil.disk_usage(self.config.storage.recordings_dir)

        self.prune_empty_directories()
        self.last_run = time.time()
        self.last_deleted = deleted
        if deleted:
            log.info("retention removed %d segments (%.1f GB)", deleted, freed / 1024**3)
        return {"deleted": deleted, "freed": freed}

    def _prune_older_than(self, camera_id: str, cutoff: float) -> tuple[int, int]:
        """Delete a camera's segments older than cutoff. Returns (deleted, freed)."""
        deleted = 0
        freed = 0
        while True:
            rows = self.db.segments_older_than_for_camera(camera_id, cutoff, limit=BATCH)
            if not rows:
                break
            for row in rows:
                freed += self._delete(row)
                deleted += 1
            if len(rows) < BATCH:
                break
        return deleted, freed

    def prune_empty_directories(self) -> None:
        """Remove day folders left behind after their segments were deleted."""
        root = self.config.storage.recordings_dir
        if not root.exists():
            return
        for camera_dir in root.iterdir():
            if not camera_dir.is_dir():
                continue
            for day_dir in camera_dir.iterdir():
                if not day_dir.is_dir():
                    continue
                try:
                    next(day_dir.iterdir())
                except StopIteration:
                    day_dir.rmdir()
                except OSError:
                    pass

    def estimate(self) -> dict[str, Any]:
        """Projected retention, for the storage panel in the UI.

        Rate is measured from what has actually been written rather than from
        the camera's configured bitrate, which cameras routinely overshoot.
        """
        usage = shutil.disk_usage(self.config.storage.recordings_dir)
        total = self.db.total_size()
        try:
            quota = self.config.storage.max_bytes()
        except (OSError, ValueError):
            quota = 0

        span_seconds = 0.0
        for camera in self.db.cameras():
            bounds = self.db.segment_bounds(camera["id"])
            if bounds:
                span_seconds = max(span_seconds, bounds[1] - bounds[0])

        bytes_per_day = (total / span_seconds * 86400) if span_seconds > 60 else 0
        projected_days = (quota / bytes_per_day) if bytes_per_day > 0 else None

        return {
            "used_bytes": total,
            "quota_bytes": quota,
            "disk_free": usage.free,
            "disk_total": usage.total,
            "bytes_per_day": bytes_per_day,
            "projected_days": projected_days,
            "max_age_days": self.config.storage.max_age_days,
            "last_run": self.last_run,
        }
