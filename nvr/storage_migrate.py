"""Move already-recorded footage into the current storage location.

After the storage path is switched, new footage writes to the new location but
old files stay where they were (still playable — the index holds absolute
paths). This optional, one-at-a-time background job relocates that old footage
into the current roots and updates the index.

It is deliberately *stateless* about the previous location: it moves any file
whose indexed path is not already under the current root, rebuilding the
destination from the database (camera id + day folder + filename for segments;
filename for clips). That makes it correct across several successive switches.

Safe alongside the live pipeline: files being written now already live under the
current root and are skipped; a file that retention prunes mid-move is counted
skipped, not failed. Cross-filesystem moves (internal disk -> NAS) fall back to
copy-then-delete via shutil.move.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("nvr.storage_migrate")


def _under(path: Path, root: Path) -> bool:
    try:
        return path.resolve(strict=False).is_relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return False


def _under_any(path: Path, roots: list[Path]) -> bool:
    return any(_under(path, r) for r in roots)


def _idle_status() -> dict[str, Any]:
    return {
        "state": "idle", "total": 0, "moved": 0, "failed": 0, "skipped": 0,
        "bytes_moved": 0, "error": None, "started": None, "finished": None,
    }


class StorageMigrator:
    def __init__(self, config: Any, db: Any):
        self.config = config
        self.db = db
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._status = _idle_status()

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def start(self) -> bool:
        """Launch a migration if one isn't already running. Returns False if a
        job is in progress."""
        with self._lock:
            if self.running():
                return False
            self._status = _idle_status()
            self._status.update(state="running", started=time.time())
            self._thread = threading.Thread(
                target=self._run, name="storage-migrate", daemon=True
            )
            self._thread.start()
            return True

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self._status.update(kw)

    def _inc(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._status[key] = self._status.get(key, 0) + n

    def _worklist(self) -> list[tuple[str, int, Path, Path]]:
        """(kind, row_id, source, destination) for every file that no longer
        lives on any pool volume — e.g. footage stranded on a volume you removed
        from the list. Such files are consolidated onto the primary volume."""
        roots = self.config.storage.volume_paths()
        primary = self.config.storage.recordings_dir
        clips_root = self.config.storage.clips_dir
        jobs: list[tuple[str, int, Path, Path]] = []
        for row in self.db.all_segments():
            src = Path(row["path"])
            if not _under_any(src, roots):
                # primary / <camera> / <day> / <file>
                dest = primary / row["camera_id"] / src.parent.name / src.name
                jobs.append(("segment", row["id"], src, dest))
        for row in self.db.clips():
            src = Path(row["path"])
            if not _under(src, clips_root):
                jobs.append(("clip", row["id"], src, clips_root / src.name))
        return jobs

    def _run(self) -> None:
        try:
            jobs = self._worklist()
            self._set(total=len(jobs))
            for kind, row_id, src, dest in jobs:
                self._move_one(kind, row_id, src, dest)
            self._set(state="done", finished=time.time())
            log.info(
                "storage migration done: %d moved, %d skipped, %d failed",
                self._status["moved"], self._status["skipped"], self._status["failed"],
            )
        except Exception as exc:  # noqa: BLE001 - report, don't crash the thread
            log.exception("storage migration crashed")
            self._set(state="error", error=str(exc), finished=time.time())

    def _move_one(self, kind: str, row_id: int, src: Path, dest: Path) -> None:
        try:
            if not src.exists():
                self._inc("skipped")  # pruned out from under us, or already moved
                return
            if src.resolve() == dest.resolve():
                self._inc("skipped")
                return
            dest.parent.mkdir(parents=True, exist_ok=True)
            size = src.stat().st_size
            shutil.move(str(src), str(dest))
            if kind == "segment":
                self.db.update_segment_path(row_id, str(dest))
            else:
                self.db.update_clip_path(row_id, str(dest))
            self._inc("moved")
            self._inc("bytes_moved", size)
        except Exception as exc:  # noqa: BLE001 - one bad file mustn't stop the run
            log.warning("could not migrate %s %s -> %s: %s", kind, src, dest, exc)
            self._inc("failed")
