"""go2rtc supervision.

go2rtc owns the hard part: maintaining RTSP connections to cameras and
re-serving them as WebRTC/MSE that a browser can actually play, with
sub-second latency and no transcoding. We generate its config from the camera
table, run it as a child process, and restart it when cameras change.

It listens on loopback only. The browser never talks to it directly — requests
are proxied through this app so that live video sits behind the same login as
everything else. WebRTC is the exception: its UDP port must be reachable from
the viewing device, which is fine on a LAN and is the piece to revisit if the
system is ever exposed remotely.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

log = logging.getLogger("nvr.streams")

# Health-check tuning for a go2rtc that's alive but not actually serving (it
# started while its ports were still held and bound nothing). Give a fresh
# process time to bind before probing, then require a few consecutive failures
# — at the supervisor's 5s cadence — before deciding it's wedged.
HEALTH_GRACE_SECONDS = 20.0
HEALTH_FAIL_THRESHOLD = 3


def stream_online(info: dict[str, Any] | None) -> bool:
    """Whether a go2rtc stream has a producer that is actually connected.

    go2rtc producers carry no "state" field. A configured-but-idle producer is
    reported as just {"url": ...} — go2rtc connects to cameras on demand — while
    a live one gains "bytes_recv" and a populated "receivers" list once it is
    pulling from the camera. Presence of either is the reliable liveness signal;
    an earlier check for a non-existent "state" key marked every healthy camera
    offline.
    """
    if not info:
        return False
    for producer in info.get("producers") or []:
        if producer.get("bytes_recv") or (producer.get("receivers") or []):
            return True
    return False


def main_stream_name(camera_id: str) -> str:
    return camera_id


def sub_stream_name(camera_id: str) -> str:
    return f"{camera_id}_sub"


def _lan_ip() -> str | None:
    """Best-guess LAN address, used as a WebRTC ICE candidate."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1; no packets are sent
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


class Go2rtcManager:
    def __init__(self, config: Any, db: Any):
        self.config = config
        self.db = db
        self.process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._last_config: str | None = None
        self._spawned_at: float = 0.0
        self._health_failures = 0

    # ---- config ----------------------------------------------------------

    def build_config(self) -> dict[str, Any]:
        streams: dict[str, Any] = {}
        for camera in self.db.cameras(enabled_only=True):
            if camera["main_url"]:
                streams[main_stream_name(camera["id"])] = self._sources(
                    main_stream_name(camera["id"]), camera["main_url"]
                )
            if camera["sub_url"]:
                streams[sub_stream_name(camera["id"])] = self._sources(
                    sub_stream_name(camera["id"]), camera["sub_url"]
                )

        webrtc: dict[str, Any] = {"listen": f":{self.config.go2rtc.webrtc_port}"}
        ip = _lan_ip()
        if ip:
            webrtc["candidates"] = [f"{ip}:{self.config.go2rtc.webrtc_port}"]

        return {
            "api": {"listen": f"127.0.0.1:{self.config.go2rtc.api_port}"},
            "rtsp": {"listen": f"127.0.0.1:{self.config.go2rtc.rtsp_port}"},
            "webrtc": webrtc,
            "srtp": {"listen": ""},
            "streams": streams,
            "log": {"level": "warn"},
        }

    def _sources(self, name: str, url: str) -> list[str]:
        """Stream sources: the raw RTSP, plus an on-demand Opus audio track.

        Cameras emit AAC, which WebRTC cannot carry. The `ffmpeg:...#audio=opus`
        source transcodes AAC->Opus, but go2rtc only spawns it when a consumer
        actually requests the audio track — muted viewers and grid tiles pay
        nothing, and the transcoder dies within ~1s of the last listener
        leaving. It pulls audio from go2rtc's own loopback, not a second camera
        connection.
        """
        return [url, f"ffmpeg:{name}#audio=opus"]

    def write_config(self) -> bool:
        """Write go2rtc.yaml. Returns True if the contents changed."""
        text = yaml.safe_dump(self.build_config(), sort_keys=False)
        path = self.config.go2rtc_config_path
        if self._last_config == text and path.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o600)  # contains RTSP URLs with camera passwords
        self._last_config = text
        return True

    # ---- process ---------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            self.write_config()
            self._spawn()

    def _spawn(self) -> None:
        binary = self.config.go2rtc.binary
        if not binary.exists():
            log.error("go2rtc binary missing at %s — run scripts/setup.sh", binary)
            return
        if not shutil.which("ffmpeg"):
            log.warning("ffmpeg not on PATH; go2rtc fallbacks will fail")
        # Capture go2rtc's own logs (truncated each start) so WebRTC/ICE
        # problems are diagnosable instead of vanishing into /dev/null.
        log_path = self.config.data_dir / "go2rtc.log"
        self._log_handle = open(log_path, "w")
        self.process = subprocess.Popen(
            [str(binary), "-config", str(self.config.go2rtc_config_path)],
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            cwd=str(self.config.data_dir),
        )
        self._spawned_at = time.monotonic()
        self._health_failures = 0
        log.info("go2rtc started (pid %s), logging to %s", self.process.pid, log_path)

    def reload(self) -> None:
        """Regenerate config and restart if the stream set changed.

        go2rtc has no config-reload signal, so a restart is the honest option.
        It reconnects in well under a second; the recorder's own retry loop
        absorbs the gap.
        """
        with self._lock:
            if not self.write_config():
                return
            self._stop_locked()
            self._spawn()

    def _stop_locked(self) -> None:
        if not self.process:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def ensure_running(self) -> None:
        """Restart go2rtc if it died — or if it's alive but not serving.

        Called by the supervisor loop. A liveness check alone isn't enough: a
        go2rtc that started while its ports were still held by a previous
        instance stays alive but binds nothing — no API, no RTSP, no streams —
        and would otherwise wedge there forever. So once a process is past its
        startup grace period we also probe the API and restart a hollow one.
        """
        with self._lock:
            if self.process is None or self.process.poll() is not None:
                log.warning("go2rtc not running; restarting")
                self.process = None
                self._spawn()
                return

            # Alive — but give it time to bind before holding it to answering.
            if time.monotonic() - self._spawned_at < HEALTH_GRACE_SECONDS:
                return
            if self._api_healthy():
                self._health_failures = 0
                return
            self._health_failures += 1
            if self._health_failures >= HEALTH_FAIL_THRESHOLD:
                log.warning(
                    "go2rtc alive but API unresponsive (%d checks); restarting",
                    self._health_failures,
                )
                self._stop_locked()
                self._spawn()

    def _api_healthy(self) -> bool:
        try:
            resp = httpx.get(f"{self.config.go2rtc.api_base}/api", timeout=1.0)
            return resp.status_code < 500
        except Exception:
            return False

    # ---- status ----------------------------------------------------------

    def wait_ready(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                httpx.get(f"{self.config.go2rtc.api_base}/api", timeout=1.0)
                return True
            except Exception:
                time.sleep(0.3)
        return False

    def stream_status(self) -> dict[str, Any]:
        try:
            response = httpx.get(
                f"{self.config.go2rtc.api_base}/api/streams", timeout=3.0
            )
            return response.json() or {}
        except Exception:
            return {}

    def is_online(self, camera_id: str) -> bool:
        """Whether go2rtc currently has a live producer for the main stream."""
        return stream_online(self.stream_status().get(main_stream_name(camera_id)))

    def snapshot(self, camera_id: str, prefer_sub: bool = True) -> bytes | None:
        """Single JPEG frame, for camera tiles.

        Prefers the substream: a still from a 6 MP main stream costs far more
        to decode than the thumbnail needs.
        """
        names = (
            [sub_stream_name(camera_id), main_stream_name(camera_id)]
            if prefer_sub
            else [main_stream_name(camera_id)]
        )
        for name in names:
            try:
                response = httpx.get(
                    f"{self.config.go2rtc.api_base}/api/frame.jpeg",
                    params={"src": name},
                    timeout=12.0,
                )
                if response.status_code == 200 and response.content[:2] == b"\xff\xd8":
                    return response.content
            except Exception:
                continue
        return None


def probe_rtsp(url: str, timeout: int = 12) -> dict[str, Any]:
    """Validate an RTSP URL with ffprobe before we commit it to the database.

    Catches wrong credentials, wrong codec in the path, and unreachable hosts
    at add-camera time rather than silently producing an empty recording
    directory hours later.
    """
    import json

    command = [
        "ffprobe", "-v", "error",
        "-rtsp_transport", "tcp",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,avg_frame_rate",
        "-show_entries", "format=format_name",
        "-of", "json",
        "-timeout", str(timeout * 1_000_000),
        url,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout + 5
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timed out connecting to the camera"}
    except FileNotFoundError:
        return {"ok": False, "error": "ffprobe not installed"}

    if result.returncode != 0:
        message = (result.stderr or "").strip().splitlines()
        detail = message[-1] if message else "could not open stream"
        if "401" in detail or "Unauthorized" in detail:
            detail = "authentication failed — check username and password"
        return {"ok": False, "error": detail}

    try:
        payload = json.loads(result.stdout)
        stream = (payload.get("streams") or [{}])[0]
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "error": "unreadable stream"}

    fps = None
    raw_fps = stream.get("avg_frame_rate") or ""
    if "/" in raw_fps:
        num, _, den = raw_fps.partition("/")
        try:
            fps = round(int(num) / int(den), 1) if int(den) else None
        except (ValueError, ZeroDivisionError):
            fps = None

    return {
        "ok": True,
        "codec": stream.get("codec_name"),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "fps": fps,
    }
