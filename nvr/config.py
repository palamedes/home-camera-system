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
class StorageVolume:
    """One directory in the recordings pool, with its own capacity cap.

    cap is a share of that volume's filesystem ('80%') or an absolute size
    ('380G'). Volumes are written in list order: footage overflows to the next
    once one is at its cap.
    """

    path: Path
    cap: str = "80%"

    def cap_bytes(self) -> int:
        try:
            total = shutil.disk_usage(self.path).total
        except OSError:
            return 0
        try:
            return parse_size(self.cap, total)
        except (ValueError, TypeError):
            return 0

    def available(self) -> bool:
        """Mounted and writable right now. Volumes are expected to be fstab-
        mounted; an absent one is simply skipped for writing/pruning."""
        try:
            return self.path.is_dir() and os.access(self.path, os.W_OK)
        except OSError:
            return False


@dataclass
class StorageConfig:
    # Ordered recordings pool. The first volume is the primary; writes overflow
    # to later volumes as earlier ones fill.
    volumes: list[StorageVolume] = field(
        default_factory=lambda: [StorageVolume(ROOT / "recordings")]
    )
    # Saved clips live here — kept permanently, never touched by retention.
    clips_dir: Path = ROOT / "clips"
    max_age_days: int = 7
    segment_seconds: int = 60

    @property
    def recordings_dir(self) -> Path:
        """The primary volume's path. Kept for the many call sites that predate
        the multi-volume pool and just want 'where recordings live'."""
        return self.volumes[0].path if self.volumes else ROOT / "recordings"

    def volume_paths(self) -> list[Path]:
        return [v.path for v in self.volumes]

    def total_cap_bytes(self) -> int:
        """Combined capacity across all currently-available volumes."""
        return sum(v.cap_bytes() for v in self.volumes if v.available())

    def max_bytes(self) -> int:
        """Back-compat alias: the pool's total capacity."""
        return self.total_cap_bytes()


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
class WeatherConfig:
    """Dashboard weather + river-level card.

    Both feeds are free, keyless government/public APIs, fetched server-side and
    cached so browsers never reach the internet directly. Defaults point at
    Oriental, NC and the NWS river gauge ORLN7 (Neuse River at Oriental).
    """

    enabled: bool = True
    latitude: float = 35.0266
    longitude: float = -76.6952
    label: str = "Oriental, NC"
    temperature_unit: str = "fahrenheit"  # or "celsius"
    wind_unit: str = "mph"                 # mph | kmh | ms | kn
    precipitation_unit: str = "inch"       # inch | mm
    # NWS/NWPS gauge id (five-char handle, e.g. ORLN7). Empty disables the
    # water panel while leaving the weather panel intact.
    water_gauge: str = "ORLN7"
    water_label: str = "Neuse River at Oriental"
    refresh_seconds: int = 600
    # River-level alerting (uses the same gauge). water_alert_level is a stage
    # in feet; 0 disables the threshold. water_alert_on_action fires whenever
    # NWS reports any flood category past the normal "no_flooding".
    water_alert_level: float = 0.0
    water_alert_on_action: bool = True


@dataclass
class AlertsConfig:
    """Event/flood notifications, delivered by POSTing JSON to a webhook.

    Deliberately transport-agnostic: the webhook can point at Home Assistant, a
    Discord/Slack relay, ntfy, or your own script. Disabled until a URL is set.
    """

    enabled: bool = False
    webhook_url: str = ""
    # Don't re-notify the same (camera, kind) more often than this, seconds.
    cooldown_seconds: int = 120
    # Reolink AI object classes to raise events/alerts for.
    detect: list[str] = field(default_factory=lambda: ["person", "vehicle", "animal"])
    # How often to poll each Reolink camera's AI state, seconds.
    poll_seconds: float = 2.0


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    go2rtc: Go2rtcConfig = field(default_factory=Go2rtcConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    playback: PlaybackConfig = field(default_factory=PlaybackConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
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
    """Read config.yaml if present, else return defaults.

    Two environment overrides let the whole app be pointed elsewhere without
    editing the file — handy for a systemd unit with its state outside the repo,
    and essential for tests, which must never touch the real database:

      * SENTRY_CONFIG   — path to the config.yaml to read.
      * SENTRY_DATA_DIR — where the database, secret key, and go2rtc config live.
    """
    if path is None:
        env_path = os.environ.get("SENTRY_CONFIG")
        path = Path(env_path).expanduser() if env_path else ROOT / "config" / "config.yaml"
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
    raw_volumes = st.get("volumes")
    if raw_volumes:
        volumes = [
            StorageVolume(
                _resolve(ROOT, v["path"]),
                str(v.get("cap", v.get("max_usage", "80%"))),
            )
            for v in raw_volumes
            if v.get("path")
        ]
    else:
        # Back-compat: a single recordings_dir + max_usage becomes volume one.
        volumes = [StorageVolume(
            _resolve(ROOT, st.get("recordings_dir", "recordings")),
            str(st.get("max_usage", "80%")),
        )]
    cfg.storage = StorageConfig(
        volumes=volumes or [StorageVolume(ROOT / "recordings")],
        clips_dir=_resolve(ROOT, st.get("clips_dir", "clips")),
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

    w = raw.get("weather") or {}
    cfg.weather = WeatherConfig(
        enabled=bool(w.get("enabled", cfg.weather.enabled)),
        latitude=float(w.get("latitude", cfg.weather.latitude)),
        longitude=float(w.get("longitude", cfg.weather.longitude)),
        label=str(w.get("label", cfg.weather.label)),
        temperature_unit=str(w.get("temperature_unit", cfg.weather.temperature_unit)),
        wind_unit=str(w.get("wind_unit", cfg.weather.wind_unit)),
        precipitation_unit=str(w.get("precipitation_unit", cfg.weather.precipitation_unit)),
        water_gauge=str(w.get("water_gauge", cfg.weather.water_gauge)),
        water_label=str(w.get("water_label", cfg.weather.water_label)),
        refresh_seconds=int(w.get("refresh_seconds", cfg.weather.refresh_seconds)),
        water_alert_level=float(w.get("water_alert_level", cfg.weather.water_alert_level)),
        water_alert_on_action=bool(w.get("water_alert_on_action", cfg.weather.water_alert_on_action)),
    )

    a = raw.get("alerts") or {}
    cfg.alerts = AlertsConfig(
        enabled=bool(a.get("enabled", cfg.alerts.enabled)),
        webhook_url=str(a.get("webhook_url", cfg.alerts.webhook_url) or ""),
        cooldown_seconds=int(a.get("cooldown_seconds", cfg.alerts.cooldown_seconds)),
        detect=list(a.get("detect") or cfg.alerts.detect),
        poll_seconds=float(a.get("poll_seconds", cfg.alerts.poll_seconds)),
    )

    data_env = os.environ.get("SENTRY_DATA_DIR")
    if data_env:
        cfg.data_dir = Path(data_env).expanduser()

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.storage.recordings_dir.mkdir(parents=True, exist_ok=True)
    cfg.storage.clips_dir.mkdir(parents=True, exist_ok=True)
    return cfg
