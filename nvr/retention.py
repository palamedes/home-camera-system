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
        """Per-camera (hard_cap, rolling) in seconds, plus the global cap.

        hard_cap: delete footage older than this (absolute, from now) no matter
        what. Falls back to the global limit when unset (NULL); 0 means "never
        delete by age".
        rolling: keep only this much of the most recent footage, measured from
        the newest segment. 0 means no rolling window (accumulate to the cap).
        """
        global_hard = self.config.storage.max_age_days * 86400
        windows: dict[str, tuple[int, int]] = {}
        # include_archived: a camera removed with "Keep footage" still owns its
        # retention override. Without this it drops out of `windows` entirely and
        # the orphan branch below prunes it at the global cap — silently deleting
        # footage the operator marked "Never (until space)" or gave a longer
        # override than the global limit.
        for c in self.db.cameras(include_archived=True):
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

        for cam_id, (hard, rolling) in windows.items():
            # 1. Rolling window — keep only the most recent `rolling` of footage,
            #    measured from the NEWEST segment rather than from now. Anchoring
            #    to the latest footage means the window stops advancing when a
            #    camera stops recording, so the last captured window is held
            #    (not deleted on a clock) until the hard cap below purges it.
            if rolling > 0:
                bounds = self.db.segment_bounds(cam_id)
                if bounds:
                    d, f = self._prune_older_than(cam_id, bounds[1] - rolling)
                    deleted += d
                    freed += f
            # 2. Hard cap — absolute max age from now; deletes no matter what.
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
            # Event markers are tiny, but there's no reason to keep them past
            # the footage they annotate.
            try:
                self.db.prune_events_older_than(now - global_hard)
            except Exception:
                log.debug("event prune skipped", exc_info=True)

        # 3. Size quota — oldest-first, a backstop for when even the rolling
        #    windows add up to more than the disk budget.
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
                for row in rows:
                    if total <= quota:
                        break
                    size = self._delete(row)
                    total -= size
                    freed += size
                    deleted += 1
                else:
                    continue
                break

        # 4. Free-space backstop — the hard floor that keeps each filesystem
        #    (and the OS, for the primary) alive. Applied per volume, pruning the
        #    oldest footage that actually lives on the volume that's low.
        for vol in self.config.storage.volumes:
            if not vol.available():
                continue
            try:
                usage = shutil.disk_usage(vol.path)
            except OSError:
                continue
            while usage.free < FREE_SPACE_FLOOR:
                rows = self.db.oldest_segments_under(str(vol.path), limit=BATCH)
                if not rows:
                    log.error(
                        "%s below %s free but no recordings left to prune there",
                        vol.path, FREE_SPACE_FLOOR,
                    )
                    break
                # Stop the moment the floor is met rather than running the whole
                # batch: dipping one byte under it used to cost up to BATCH (500)
                # segments — tens of GB of the oldest footage to reclaim almost
                # nothing. Re-stat every few deletes so the check is cheap.
                for i, row in enumerate(rows):
                    freed += self._delete(row)
                    deleted += 1
                    if i % 10 == 9:
                        try:
                            if shutil.disk_usage(vol.path).free >= FREE_SPACE_FLOOR:
                                break
                        except OSError:
                            break
                try:
                    usage = shutil.disk_usage(vol.path)
                except OSError:
                    break

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
        """Remove day folders left behind after their segments were deleted,
        across every pool volume."""
        for root in self.config.storage.volume_paths():
            if not root.exists():
                continue
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
        total = self.db.total_size()
        quota = self.config.storage.total_cap_bytes()

        # Per-volume breakdown, plus pooled disk free/total across available
        # volumes (distinct drives; two volumes on one filesystem double-count,
        # which is a fine approximation for the storage panel).
        volumes = []
        disk_free = disk_total = 0
        for vol in self.config.storage.volumes:
            try:
                usage = shutil.disk_usage(vol.path)
                free, vtotal = usage.free, usage.total
            except OSError:
                free = vtotal = None
            available = vol.available()
            volumes.append({
                "path": str(vol.path),
                "cap": vol.cap,
                "cap_bytes": vol.cap_bytes(),
                "used": self.db.recorded_bytes_under(str(vol.path)),
                "free": free,
                "total": vtotal,
                "available": available,
            })
            if available and free is not None:
                disk_free += free
                disk_total += vtotal

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
            "disk_free": disk_free,
            "disk_total": disk_total,
            "bytes_per_day": bytes_per_day,
            "projected_days": projected_days,
            "max_age_days": self.config.storage.max_age_days,
            "last_run": self.last_run,
            "volumes": volumes,
        }
