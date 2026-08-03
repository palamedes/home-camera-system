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
QUIET_SECONDS = 10.0

MAX_BACKOFF = 30.0


class CameraRecorder:
    """Supervises the ffmpeg process for a single camera."""

    def __init__(self, camera: dict[str, Any], config: Any):
        self.camera_id = camera["id"]
        self.camera = camera
        self.config = config
        self.process: subprocess.Popen | None = None
        self.backoff = 1.0
        self.last_error: str | None = None
        self.started_at: float | None = None
        self.restarts = 0

    @property
    def directory(self) -> Path:
        return self.config.storage.recordings_dir / self.camera_id

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
    """Read a finished segment's duration and video codec."""
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
        payload = json.loads(result.stdout or "{}")
    except Exception:
        return None, None

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
    return duration, codec


class RecordingService:
    """Owns every camera recorder plus the segment indexer."""

    def __init__(self, config: Any, db: Any, go2rtc: Any):
        self.config = config
        self.db = db
        self.go2rtc = go2rtc
        self.recorders: dict[str, CameraRecorder] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._index_thread: threading.Thread | None = None

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

        for camera_id, camera in wanted.items():
            existing = self.recorders.get(camera_id)
            if existing is None:
                self.recorders[camera_id] = CameraRecorder(dict(camera), self.config)
            elif existing.camera.get("record_stream") != camera["record_stream"]:
                # Recording source changed under us; rebuild the process.
                existing.stop()
                self.recorders[camera_id] = CameraRecorder(dict(camera), self.config)

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
            self._stop.wait(15.0)

    def index_new_segments(self) -> int:
        """Add closed-but-unindexed segment files to the database."""
        added = 0
        now = time.time()
        for camera in self.db.cameras():
            camera_id = camera["id"]
            directory = self.config.storage.recordings_dir / camera_id
            if not directory.exists():
                continue
            known = self.db.known_paths(camera_id)
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

                duration, codec = probe_duration(path)
                if not duration or duration <= 0:
                    # Unreadable: usually a segment cut short by a crash.
                    # Leave it on disk but do not index it, or the timeline
                    # would advertise footage that cannot be played.
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
