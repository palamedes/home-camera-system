"""Continuous recording.

One ffmpeg process per camera, pulling from go2rtc's loopback RTSP and writing
fixed-length MP4 segments with `-c copy`. No re-encoding: the camera already
produced a compressed stream, and decoding 6 MP fisheye around the clock would
saturate this CPU for no benefit. Recording is therefore nearly free — it is
disk-bound, not CPU-bound.

Segments are indexed after they close rather than as they are created. A file
whose mtime has stopped advancing is one ffmpeg has finished with; anything
newer is still being written and must not be indexed, because its duration is
not yet known.

Segment start times come from `mtime - duration`, not from the filename. The
filename is local time, which is ambiguous for one hour every autumn; mtime is
an epoch and never is.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import streams

log = logging.getLogger("nvr.recorder")

# A segment is considered closed once untouched for this long. Must exceed the
# interval at which ffmpeg flushes, or we would index a half-written file.
# How long a segment file must sit untouched before it's indexed. ffmpeg closes
# each file the instant it rolls to the next, so mtime stops moving immediately;
# this only guards against a half-flushed file. Kept short because every second
# here is a second the history timeline trails live. Indexing a still-open file
# is self-correcting anyway: an unfinalised MP4 has no moov atom, so the probe
# below fails and it's simply retried next pass.
QUIET_SECONDS = 3.0

# How old an unreadable segment must be before the indexer deletes it. Far
# longer than QUIET_SECONDS because deleting is irreversible: this is the margin
# against ever removing a file that is somehow still being written. A segment in
# flight is at most segment_seconds old (60s by default) and always reads as
# "unreadable" — an MP4 has no moov atom until it's closed — so the only thing
# standing between a live recording and deletion is this number. An hour is ~60x
# the worst legitimate case, and these files are rare enough that cleaning them
# up an hour later costs nothing.
CORRUPT_MAX_AGE = 3600.0  # 1 hour

MAX_BACKOFF = 30.0

# Never write onto a volume with less than this free — the free-space backstop
# that keeps a filesystem (and the OS, for the primary) alive.
WRITE_FREE_FLOOR = 5 * 1024**3  # 5 GB


class CameraRecorder:
    """Supervises the ffmpeg process for a single camera."""

    def __init__(self, camera: dict[str, Any], config: Any, base_dir: Path | None = None):
        self.camera_id = camera["id"]
        self.camera = camera
        self.config = config
        # The pool volume this recorder currently writes to. Defaults to the
        # primary; RecordingService picks the active one and rebuilds the
        # recorder when overflow moves it to a different volume.
        self.base_dir = base_dir or config.storage.recordings_dir
        self.process: subprocess.Popen | None = None
        self.backoff = 1.0
        self.last_error: str | None = None
        self.started_at: float | None = None
        self.restarts = 0

    @property
    def directory(self) -> Path:
        return self.base_dir / self.camera_id

    def ensure_directories(self) -> None:
        """Pre-create today's and tomorrow's day folders.

        ffmpeg's segment muxer will not create directories, and it rolls over
        the date in the output path at midnight — so tomorrow's folder has to
        exist before midnight or recording stops for the night.
        """
        today = datetime.now()
        for offset in (0, 1):
            day = (today + timedelta(days=offset)).strftime("%Y-%m-%d")
            (self.directory / day).mkdir(parents=True, exist_ok=True)

    def _command(self) -> list[str]:
        stream = (
            streams.sub_stream_name(self.camera_id)
            if self.camera["record_stream"] == "sub"
            else streams.main_stream_name(self.camera_id)
        )
        source = self.config.go2rtc.local_rtsp(stream)
        pattern = str(self.directory / "%Y-%m-%d" / "%H-%M-%S.mp4")

        return [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            # TCP: UDP RTSP drops packets under WiFi contention, and a torn
            # H.264 GOP corrupts the whole segment rather than one frame.
            "-rtsp_transport", "tcp",
            "-timeout", "10000000",
            "-i", source,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy",
            # Audio is re-encoded because cameras commonly emit G.711, which
            # MP4 cannot carry. Costs almost nothing.
            "-c:a", "aac", "-b:a", "64k",
            "-f", "segment",
            "-segment_time", str(self.config.storage.segment_seconds),
            "-segment_atclocktime", "1",
            "-segment_format", "mp4",
            "-reset_timestamps", "1",
            "-strftime", "1",
            pattern,
        ]

    def start(self) -> None:
        self.ensure_directories()
        self.process = subprocess.Popen(
            self._command(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.started_at = time.time()
        log.info("recording %s -> %s", self.camera_id, self.directory)

    def stop(self) -> None:
        if not self.process:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None
        self.started_at = None

    def check(self) -> None:
        """Restart the process if it died, with backoff.

        A camera that is unplugged should not spin ffmpeg in a tight loop, but
        a camera that blipped should come back fast — hence exponential
        backoff that resets once a run survives a full minute.
        """
        if self.process is None:
            self.start()
            return
        if self.process.poll() is None:
            # Healthy for a while: forgive earlier failures. Clear last_error too,
            # or a one-off blip (e.g. go2rtc restarting) keeps showing on the
            # camera page long after recording recovered.
            if self.started_at and time.time() - self.started_at > 60:
                self.backoff = 1.0
                self.last_error = None
            return

        stderr = ""
        if self.process.stderr:
            try:
                stderr = self.process.stderr.read() or ""
            except Exception:
                pass
        self.last_error = stderr.strip().splitlines()[-1] if stderr.strip() else None
        self.restarts += 1
        log.warning(
            "recorder for %s exited (%s); retrying in %.0fs",
            self.camera_id, self.last_error or "no error output", self.backoff,
        )
        self.process = None
        time.sleep(self.backoff)
        self.backoff = min(self.backoff * 2, MAX_BACKOFF)
        self.start()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None


def probe_duration(path: Path) -> tuple[float | None, str | None]:
    """Read a finished segment's duration and video codec.

    Returns (duration, codec); either may be None. See probe_segment() when you
    need to know *why* a probe came back empty.
    """
    duration, codec, _ = probe_segment(path)
    return duration, codec


def probe_segment(path: Path) -> tuple[float | None, str | None, bool]:
    """As probe_duration, plus whether ffprobe actually reached a verdict.

    The third element is True when ffprobe ran to completion — so a None
    duration means the file genuinely is unreadable — and False when ffprobe
    itself could not run (missing binary, timeout, killed). Deleting a file is
    only ever safe on a True verdict: if ffprobe were broken, a False verdict
    would otherwise condemn every segment on disk.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-show_entries", "stream=codec_name,codec_type",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return None, None, False        # ffprobe unavailable — no verdict
    try:
        payload = json.loads(result.stdout or "{}")
    except Exception:
        # ffprobe ran but emitted nothing parseable: that is a verdict on the
        # file (a truncated MP4 with no moov atom prints an error and no JSON).
        return None, None, True

    duration = None
    raw = (payload.get("format") or {}).get("duration")
    if raw:
        try:
            duration = float(raw)
        except ValueError:
            duration = None

    codec = None
    for stream in payload.get("streams") or []:
        if stream.get("codec_type") == "video":
            codec = stream.get("codec_name")
            break
    return duration, codec, True


class RecordingService:
    """Owns every camera recorder plus the segment indexer."""

    def __init__(self, config: Any, db: Any, go2rtc: Any):
        self.config = config
        self.db = db
        self.go2rtc = go2rtc
        self.recorders: dict[str, CameraRecorder] = {}
        self.active_dir: Path | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._index_thread: threading.Thread | None = None

    def choose_volume(self) -> Path:
        """Which pool volume new footage should land on: the first available
        volume still under its cap with free space. When every volume is full,
        fall back to the first available one (ring-buffer mode — retention then
        prunes oldest across the pool). Absent volumes are skipped."""
        volumes = self.config.storage.volumes
        available = []
        for vol in volumes:
            if not vol.available():
                continue
            available.append(vol.path)
            try:
                free = shutil.disk_usage(vol.path).free
            except OSError:
                continue
            used = self.db.recorded_bytes_under(str(vol.path))
            if used < vol.cap_bytes() and free > WRITE_FREE_FLOOR:
                return vol.path
        if available:
            return available[0]
        # Nothing mounted: last resort is the primary path (may fail to write,
        # which the recorder reports and retries — better than crashing).
        return self.config.storage.recordings_dir

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._supervise, name="recorder-supervisor", daemon=True
        )
        self._thread.start()
        self._index_thread = threading.Thread(
            target=self._index_loop, name="segment-indexer", daemon=True
        )
        self._index_thread.start()

    def stop(self) -> None:
        self._stop.set()
        for recorder in list(self.recorders.values()):
            recorder.stop()
        self.recorders.clear()

    def sync(self) -> None:
        """Reconcile running recorders against the camera table."""
        now = time.time()

        # Expire bounded recording windows. Once record_until passes we clear
        # the flag in the database rather than merely skipping it, so a timed
        # recording does not silently resume after a restart.
        for camera in self.db.cameras(enabled_only=True):
            until = camera["record_until"]
            if camera["record"] and until is not None and until <= now:
                log.info("recording window elapsed for %s; stopping", camera["id"])
                self.db.update_camera(camera["id"], record=0, record_until=None)

        wanted = {
            camera["id"]: camera
            for camera in self.db.cameras(enabled_only=True)
            if camera["record"]
            and camera["main_url"]
            and (camera["record_until"] is None or camera["record_until"] > now)
        }

        for camera_id in list(self.recorders):
            if camera_id not in wanted:
                log.info("stopping recorder for removed camera %s", camera_id)
                self.recorders.pop(camera_id).stop()

        # Pick the pool volume new footage should land on right now. Shared by
        # every camera (the pool overflows as a whole), so it's computed once —
        # but a camera pinned to a specific drive overrides it.
        active = self.choose_volume()
        self.active_dir = active
        for camera_id, camera in wanted.items():
            existing = self.recorders.get(camera_id)
            base = self._volume_for(camera, active)
            # Rebuild when the recorder is new, the record stream changed, or the
            # target volume moved (overflow / pin change) — makes fail-over work.
            if (existing is None
                    or existing.camera.get("record_stream") != camera["record_stream"]
                    or existing.base_dir != base):
                if existing is not None:
                    existing.stop()
                self.recorders[camera_id] = CameraRecorder(dict(camera), self.config, base)

    def _volume_for(self, camera: Any, fallback: Path) -> Path:
        """Honour a camera's pinned volume when it's mounted with room to spare;
        otherwise fall back to the pool's normal overflow choice. Pruning is
        per-volume, so a pinned camera simply shares its drive's age/space cap.

        `camera` may be a sqlite3.Row (no .get), so read defensively."""
        try:
            pref = camera["preferred_volume"]
        except (IndexError, KeyError):
            pref = None
        if not pref:
            return fallback
        for vol in self.config.storage.volumes:
            if str(vol.path) == pref and vol.available():
                try:
                    if shutil.disk_usage(vol.path).free > WRITE_FREE_FLOOR:
                        return vol.path
                except OSError:
                    pass
                break
        return fallback

    def restart(self, camera_id: str) -> None:
        """Drop a camera's recorder so the supervisor rebuilds it fresh.

        Used after a camera-side encoder change: the stream's resolution shifted
        under a -c copy process, and the clean fix is a new ffmpeg on a new
        segment. The next supervisor tick re-creates the recorder from the
        current camera row.
        """
        recorder = self.recorders.pop(camera_id, None)
        if recorder is not None:
            recorder.stop()

    def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                self.go2rtc.ensure_running()
                self.sync()
                for recorder in list(self.recorders.values()):
                    recorder.ensure_directories()
                    recorder.check()
            except Exception:
                log.exception("recorder supervisor iteration failed")
            self._stop.wait(5.0)

    # ---- indexing --------------------------------------------------------

    def _index_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.index_new_segments()
            except Exception:
                log.exception("segment indexing failed")
            # Poll often: this interval is dead time between a segment closing
            # and it appearing on the history timeline. The scan is cheap —
            # already-known paths are skipped by a set lookup, so a pass is one
            # readdir per camera per volume.
            self._stop.wait(5.0)

    def index_new_segments(self) -> int:
        """Add closed-but-unindexed segment files to the database."""
        added = 0
        now = time.time()
        for camera in self.db.cameras():
            camera_id = camera["id"]
            known = self.db.known_paths(camera_id)
            # Footage for one camera can be spread across every pool volume, so
            # scan them all.
            for base in self.config.storage.volume_paths():
                directory = base / camera_id
                if not directory.exists():
                    continue
                for path in sorted(directory.glob("*/*.mp4")):
                    key = str(path)
                    if key in known:
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    if now - stat.st_mtime < QUIET_SECONDS:
                        continue  # ffmpeg is still writing this one
                    if stat.st_size == 0:
                        path.unlink(missing_ok=True)
                        continue

                    duration, codec, probed = probe_segment(path)
                    if not duration or duration <= 0:
                        # Unreadable: usually a segment cut short by a crash.
                        # Never index it, or the timeline would advertise
                        # footage that cannot be played.
                        #
                        # These used to be left on disk forever: retention only
                        # prunes *indexed* segments, so they leaked and made the
                        # storage figures under-report real usage. Delete them —
                        # but only on a positive ffprobe verdict (never when
                        # ffprobe itself failed to run, which would condemn every
                        # segment at once) and only once the file is far too old
                        # to still be mid-write. A rebuilt/restored database is
                        # safe under this rule: valid footage probes fine and is
                        # simply re-indexed, never deleted.
                        if probed and now - stat.st_mtime > CORRUPT_MAX_AGE:
                            log.warning(
                                "deleting unreadable segment %s (%d bytes)",
                                path, stat.st_size,
                            )
                            path.unlink(missing_ok=True)
                        continue
                    self.db.add_segment(
                        camera_id=camera_id,
                        path=key,
                        start_ts=stat.st_mtime - duration,
                        duration=duration,
                        size=stat.st_size,
                        codec=codec,
                    )
                    added += 1
        if added:
            log.debug("indexed %d new segments", added)
        return added

    def status(self) -> dict[str, dict[str, Any]]:
        return {
            camera_id: {
                "running": recorder.running,
                "restarts": recorder.restarts,
                "last_error": recorder.last_error,
                "uptime": (
                    time.time() - recorder.started_at if recorder.started_at else 0
                ),
            }
            for camera_id, recorder in self.recorders.items()
        }
