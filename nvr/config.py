"""Configuration loading.

System settings come from config/config.yaml; cameras live in the database
because the UI has to write them. Anything missing from the file falls back to
the defaults here, so a half-written config still boots.
"""

from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def parse_size(value: Any, total: int | None = None) -> int:
    """Turn '80%', '380G', or a raw byte count into bytes.

    Percentages are taken against `total`, which callers pass as the size of
    the filesystem the recordings live on.
    """
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().upper()
    if not text:
        raise ValueError("empty size")
    if text.endswith("%"):
        if total is None:
            raise ValueError(f"percentage size {value!r} needs a total to scale against")
        return int(total * float(text[:-1]) / 100.0)
    if text.endswith("B"):
        text = text[:-1]
    if text and text[-1] in _UNITS:
        return int(float(text[:-1]) * _UNITS[text[-1]])
    return int(float(text))


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    session_days: int = 30
    secure_cookies: bool = False


@dataclass
class StorageConfig:
    recordings_dir: Path = ROOT / "recordings"
    max_usage: str = "80%"
    max_age_days: int = 7
    segment_seconds: int = 60

    def max_bytes(self) -> int:
        """Resolve max_usage against the filesystem holding the recordings."""
        total = shutil.disk_usage(self.recordings_dir).total
        return parse_size(self.max_usage, total)


@dataclass
class Go2rtcConfig:
    binary: Path = ROOT / "bin" / "go2rtc"
    api_port: int = 1984
    rtsp_port: int = 8554
    webrtc_port: int = 8555

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    def local_rtsp(self, stream: str) -> str:
        """RTSP URL for a stream go2rtc is publishing, on loopback."""
        return f"rtsp://127.0.0.1:{self.rtsp_port}/{stream}"


@dataclass
class DiscoveryConfig:
    subnets: list[str] = field(default_factory=list)
    timeout: float = 1.0
    onvif_wait: float = 3.0


@dataclass
class PlaybackConfig:
    always_transcode: bool = False
    qsv_device: str | None = "/dev/dri/renderD128"

    def hardware_available(self) -> bool:
        return bool(self.qsv_device) and os.path.exists(self.qsv_device)


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    go2rtc: Go2rtcConfig = field(default_factory=Go2rtcConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    playback: PlaybackConfig = field(default_factory=PlaybackConfig)
    data_dir: Path = ROOT / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nvr.db"

    @property
    def go2rtc_config_path(self) -> Path:
        return self.data_dir / "go2rtc.yaml"

    def secret_key(self) -> bytes:
        """Persistent key for signing session cookies.

        Generated on first run. Losing it just logs everyone out.
        """
        path = self.data_dir / "secret.key"
        if not path.exists():
            path.write_bytes(secrets.token_bytes(32))
            path.chmod(0o600)
        return path.read_bytes()


def _resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path)


def load(path: Path | None = None) -> Config:
    """Read config.yaml if present, else return defaults."""
    path = path or ROOT / "config" / "config.yaml"
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}

    cfg = Config()

    s = raw.get("server") or {}
    cfg.server = ServerConfig(
        host=s.get("host", cfg.server.host),
        port=int(s.get("port", cfg.server.port)),
        session_days=int(s.get("session_days", cfg.server.session_days)),
        secure_cookies=bool(s.get("secure_cookies", cfg.server.secure_cookies)),
    )

    st = raw.get("storage") or {}
    cfg.storage = StorageConfig(
        recordings_dir=_resolve(ROOT, st.get("recordings_dir", "recordings")),
        max_usage=str(st.get("max_usage", cfg.storage.max_usage)),
        max_age_days=int(st.get("max_age_days", cfg.storage.max_age_days)),
        segment_seconds=int(st.get("segment_seconds", cfg.storage.segment_seconds)),
    )

    g = raw.get("go2rtc") or {}
    cfg.go2rtc = Go2rtcConfig(
        binary=_resolve(ROOT, g.get("binary", "bin/go2rtc")),
        api_port=int(g.get("api_port", cfg.go2rtc.api_port)),
        rtsp_port=int(g.get("rtsp_port", cfg.go2rtc.rtsp_port)),
        webrtc_port=int(g.get("webrtc_port", cfg.go2rtc.webrtc_port)),
    )

    d = raw.get("discovery") or {}
    cfg.discovery = DiscoveryConfig(
        subnets=list(d.get("subnets") or []),
        timeout=float(d.get("timeout", cfg.discovery.timeout)),
        onvif_wait=float(d.get("onvif_wait", cfg.discovery.onvif_wait)),
    )

    p = raw.get("playback") or {}
    cfg.playback = PlaybackConfig(
        always_transcode=bool(p.get("always_transcode", False)),
        qsv_device=p.get("qsv_device", cfg.playback.qsv_device),
    )

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.storage.recordings_dir.mkdir(parents=True, exist_ok=True)
    return cfg
