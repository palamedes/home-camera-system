"""Runtime-editable app settings, layered over config.yaml.

The `weather` and `alerts` config sections can be edited from the settings page
rather than the YAML file. Edits are validated here, applied to the live config
dataclasses *in place* (so the running services, which hold references to those
objects, pick them up), and persisted as one JSON blob per section in the
`app_settings` table. On startup load_overrides() replays the stored blobs over
whatever config.yaml provided — DB wins.

Infrastructure (server ports, go2rtc, paths, playback) stays file-only: it's
boot-time and can't meaningfully change without a restart.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from .config import ROOT, StorageVolume, parse_size

log = logging.getLogger("nvr.appsettings")

# Single-path storage dirs editable from the UI. Recordings are a multi-volume
# pool handled separately (see volume functions below); clips stay one folder.
STORAGE_KEYS = ("clips_dir",)

# Set this env var to skip DB-stored networking overrides at boot — the escape
# hatch if a bad host/port in the UI ever prevents startup.
IGNORE_NETWORK_ENV = "SENTRY_IGNORE_DB_NETWORK"


class SettingError(ValueError):
    """A submitted setting was invalid (bad type, out of range, unknown value)."""


# ---- coercers: each validates + normalises one field, raising SettingError --

def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    raise SettingError("expected a boolean")


def _as_str(v: Any) -> str:
    return str(v).strip()


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        raise SettingError("expected a number") from None


def _float_min(lo: float) -> Callable[[Any], float]:
    def coerce(v: Any) -> float:
        n = _num(v)
        if n < lo:
            raise SettingError(f"must be at least {lo}")
        return n
    return coerce


def _int_min(lo: int) -> Callable[[Any], int]:
    def coerce(v: Any) -> int:
        n = _num(v)
        if n < lo:
            raise SettingError(f"must be at least {lo}")
        return int(n)
    return coerce


def _enum(*allowed: str) -> Callable[[Any], str]:
    allowed_set = set(allowed)

    def coerce(v: Any) -> str:
        s = str(v).strip().lower()
        if s not in allowed_set:
            raise SettingError(f"must be one of {', '.join(sorted(allowed_set))}")
        return s
    return coerce


def _lat(v: Any) -> float:
    n = _num(v)
    if not -90 <= n <= 90:
        raise SettingError("latitude must be between -90 and 90")
    return n


def _lon(v: Any) -> float:
    n = _num(v)
    if not -180 <= n <= 180:
        raise SettingError("longitude must be between -180 and 180")
    return n


def _gauge(v: Any) -> str:
    return str(v).strip().upper()


def _port(v: Any) -> int:
    n = _num(v)
    if not 1 <= n <= 65535:
        raise SettingError("port must be between 1 and 65535")
    return int(n)


def _host(v: Any) -> str:
    s = str(v).strip()
    if not s:
        raise SettingError("host cannot be empty")
    return s


def _int_range(lo: int, hi: int) -> Callable[[Any], int]:
    def coerce(v: Any) -> int:
        n = _num(v)
        if not lo <= n <= hi:
            raise SettingError(f"must be between {lo} and {hi}")
        return int(n)
    return coerce


def _max_usage(v: Any) -> str:
    s = str(v).strip()
    try:
        parse_size(s, total=10 ** 15)
    except (ValueError, TypeError):
        raise SettingError("must look like '80%', '380G', or a byte count") from None
    return s


def _qsv_device(v: Any) -> str | None:
    s = str(v).strip()
    return s or None


def _subnet_list(v: Any) -> list[str]:
    if isinstance(v, str):
        v = [p for p in re.split(r"[\s,]+", v.strip()) if p]
    if not isinstance(v, (list, tuple)):
        raise SettingError("expected a list of CIDR subnets")
    out = []
    for item in v:
        s = str(item).strip()
        if not s:
            continue
        try:
            ipaddress.ip_network(s, strict=False)
        except ValueError:
            raise SettingError(f"invalid subnet {s!r}") from None
        out.append(s)
    return out


def _detect_list(v: Any) -> list[str]:
    allowed = {"person", "vehicle", "animal", "motion"}
    if isinstance(v, str):
        v = [p.strip() for p in v.split(",")]
    if not isinstance(v, (list, tuple)):
        raise SettingError("expected a list of detection kinds")
    out = []
    for item in v:
        s = str(item).strip().lower()
        if s and s in allowed and s not in out:
            out.append(s)
    return out


# ---- per-section field schemas --------------------------------------------

WEATHER_FIELDS: dict[str, Callable[[Any], Any]] = {
    "enabled": _as_bool,
    "latitude": _lat,
    "longitude": _lon,
    "label": _as_str,
    "temperature_unit": _enum("fahrenheit", "celsius"),
    "wind_unit": _enum("mph", "kmh", "ms", "kn"),
    "precipitation_unit": _enum("inch", "mm"),
    "water_gauge": _gauge,
    "water_label": _as_str,
    "refresh_seconds": _int_min(60),
    "water_alert_level": _float_min(0),
    "water_alert_on_action": _as_bool,
}

ALERTS_FIELDS: dict[str, Callable[[Any], Any]] = {
    "enabled": _as_bool,
    "webhook_url": _as_str,
    "cooldown_seconds": _int_min(0),
    "detect": _detect_list,
    "poll_seconds": _float_min(0.5),
}

SECTIONS: dict[str, tuple[str, dict[str, Callable[[Any], Any]]]] = {
    # section name -> (config attribute, field schema)
    "weather": ("weather", WEATHER_FIELDS),
    "alerts": ("alerts", ALERTS_FIELDS),
}


# ---- "advanced" sections: fields target nested config attrs and may need a
# restart to take effect. Each field is (dotted target, coercer, restart scope).
# restart scope is None (live), "recorder" (rebuild ffmpeg), or "app" (full
# process restart — host/port and go2rtc ports can't rebind live).

_LIVE, _RECORDER, _APP = None, "recorder", "app"

ADVANCED: dict[str, dict[str, tuple[str, Callable[[Any], Any], str | None]]] = {
    "storage_limits": {
        # Per-volume capacity lives on each volume (see the pool); these are the
        # pool-wide knobs.
        "max_age_days": ("storage.max_age_days", _int_min(0), _LIVE),
        "segment_seconds": ("storage.segment_seconds", _int_range(5, 3600), _RECORDER),
    },
    "network": {
        "host": ("server.host", _host, _APP),
        "port": ("server.port", _port, _APP),
        "session_days": ("server.session_days", _int_min(1), _LIVE),
        "secure_cookies": ("server.secure_cookies", _as_bool, _LIVE),
        "go2rtc_api_port": ("go2rtc.api_port", _port, _APP),
        "go2rtc_rtsp_port": ("go2rtc.rtsp_port", _port, _APP),
        "go2rtc_webrtc_port": ("go2rtc.webrtc_port", _port, _APP),
        "discovery_subnets": ("discovery.subnets", _subnet_list, _LIVE),
        "discovery_timeout": ("discovery.timeout", _float_min(0.1), _LIVE),
        "onvif_wait": ("discovery.onvif_wait", _float_min(0.1), _LIVE),
        "always_transcode": ("playback.always_transcode", _as_bool, _LIVE),
        "qsv_device": ("playback.qsv_device", _qsv_device, _LIVE),
    },
}


def _get_dotted(config: Any, path: str) -> Any:
    obj = config
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _set_dotted(config: Any, path: str, value: Any) -> None:
    obj = config
    parts = path.split(".")
    for part in parts[:-1]:
        obj = getattr(obj, part)
    setattr(obj, parts[-1], value)


def update_advanced(
    config: Any, db: Any, section: str, updates: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Validate + apply an advanced section. Returns (applied values, restart
    scopes that changed). Live fields take effect immediately; others need the
    caller to act on the returned scopes. Atomic: nothing changes on a bad field."""
    if section not in ADVANCED:
        raise SettingError(f"unknown settings section {section!r}")
    spec = ADVANCED[section]

    coerced: dict[str, tuple[str, Any, str | None]] = {}
    for key, value in updates.items():
        if key not in spec:
            raise SettingError(f"unknown field {key!r}")
        target, coerce, restart = spec[key]
        try:
            coerced[key] = (target, coerce(value), restart)
        except SettingError as exc:
            raise SettingError(f"{key}: {exc}") from None

    applied: dict[str, Any] = {}
    restarts: set[str] = set()
    for key, (target, value, restart) in coerced.items():
        old = _get_dotted(config, target)
        _set_dotted(config, target, value)
        applied[key] = value
        if restart and old != value:
            restarts.add(restart)

    stored = {}
    raw = db.get_setting(section)
    if raw:
        try:
            stored = json.loads(raw)
        except ValueError:
            stored = {}
    stored.update(applied)
    db.set_setting(section, json.dumps(stored))
    return applied, sorted(restarts)


# ---- public API ------------------------------------------------------------

def load_overrides(config: Any, db: Any) -> None:
    """Replay stored overrides over the config dataclasses (called at startup)."""
    for section, (attr, schema) in SECTIONS.items():
        raw = db.get_setting(section)
        if not raw:
            continue
        try:
            stored = json.loads(raw)
        except ValueError:
            log.warning("ignoring corrupt %s settings blob", section)
            continue
        target = getattr(config, attr)
        for key, value in stored.items():
            coerce = schema.get(key)
            if coerce is None:
                continue
            try:
                setattr(target, key, coerce(value))
            except SettingError:
                log.warning("ignoring bad stored %s.%s=%r", section, key, value)

    # Storage paths: apply leniently. Only switch to a stored path if it exists
    # and is writable *right now* — so a NAS that's unmounted at boot falls back
    # to the config.yaml default instead of writing into an empty mountpoint.
    raw = db.get_setting("storage")
    if raw:
        try:
            stored = json.loads(raw)
        except ValueError:
            stored = {}
        for key in STORAGE_KEYS:
            if key not in stored:
                continue
            path = _resolve_dir(stored[key])
            info = validate_storage_dir(path, create=False)
            if info["ok"]:
                setattr(config.storage, key, path)
            else:
                log.warning(
                    "stored %s=%s unusable (%s); keeping %s",
                    key, path, info["error"], getattr(config.storage, key),
                )

    # Recordings pool. Keep every listed volume even if one is momentarily
    # unmounted (fstab may bring it back) — availability is checked at write and
    # prune time, not here. Only replace the default if we parse ≥1 volume.
    raw = db.get_setting("volumes")
    if raw:
        try:
            stored = json.loads(raw)
        except ValueError:
            stored = None
        if isinstance(stored, list):
            vols = []
            for item in stored:
                p = (item or {}).get("path")
                if not p:
                    continue
                try:
                    cap = _max_usage((item or {}).get("cap", "80%"))
                except SettingError:
                    cap = "80%"
                vols.append(StorageVolume(_resolve_dir(p), cap))
            if vols:
                config.storage.volumes = vols

    # Advanced sections (storage limits, networking). Networking can be skipped
    # via the env hatch so a bad host/port never permanently blocks startup.
    skip_network = bool(os.environ.get(IGNORE_NETWORK_ENV))
    for section, spec in ADVANCED.items():
        if section == "network" and skip_network:
            continue
        raw = db.get_setting(section)
        if not raw:
            continue
        try:
            stored = json.loads(raw)
        except ValueError:
            log.warning("ignoring corrupt %s settings blob", section)
            continue
        for key, value in stored.items():
            entry = spec.get(key)
            if entry is None:
                continue
            target, coerce, _ = entry
            try:
                _set_dotted(config, target, coerce(value))
            except SettingError:
                log.warning("ignoring bad stored %s.%s=%r", section, key, value)


def update_section(config: Any, db: Any, section: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Validate and apply `updates` to one section: mutate the live dataclass in
    place, then merge + persist the section blob. Returns the applied values.
    Raises SettingError on the first invalid field, changing nothing."""
    if section not in SECTIONS:
        raise SettingError(f"unknown settings section {section!r}")
    attr, schema = SECTIONS[section]

    # Validate everything up front so a bad field leaves nothing half-applied.
    applied: dict[str, Any] = {}
    for key, value in updates.items():
        coerce = schema.get(key)
        if coerce is None:
            raise SettingError(f"unknown field {key!r}")
        try:
            applied[key] = coerce(value)
        except SettingError as exc:
            raise SettingError(f"{key}: {exc}") from None

    target = getattr(config, attr)
    for key, value in applied.items():
        setattr(target, key, value)

    stored = {}
    raw = db.get_setting(section)
    if raw:
        try:
            stored = json.loads(raw)
        except ValueError:
            stored = {}
    stored.update(applied)
    db.set_setting(section, json.dumps(stored))
    return applied


def current(config: Any) -> dict[str, dict[str, Any]]:
    """Snapshot of the editable sections' current values, for the API/UI."""
    out: dict[str, dict[str, Any]] = {}
    for section, (attr, schema) in SECTIONS.items():
        target = getattr(config, attr)
        out[section] = {key: getattr(target, key) for key in schema}
    for section, spec in ADVANCED.items():
        out[section] = {key: _get_dotted(config, target)
                        for key, (target, _c, _r) in spec.items()}
    return out


# ---- storage locations (relocatable, e.g. onto a NAS) ----------------------

def _resolve_dir(value: Any) -> Path:
    p = Path(str(value).strip()).expanduser()
    return p if p.is_absolute() else (ROOT / p)


def validate_storage_dir(path: Path, *, create: bool) -> dict[str, Any]:
    """Check a candidate storage directory is usable: exists (optionally create
    it), is a directory, and is writable. Returns disk free/total on success."""
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            return {"ok": False, "error": "path does not exist"}
        if not path.is_dir():
            return {"ok": False, "error": "not a directory"}
        probe = path / ".sentry_write_test"
        probe.write_text("ok")
        probe.unlink()
        usage = shutil.disk_usage(path)
        return {"ok": True, "error": None, "free": usage.free, "total": usage.total}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def storage_current(config: Any) -> dict[str, Any]:
    """Current storage paths + free/total space on each, for the UI."""
    out: dict[str, Any] = {}
    for key in STORAGE_KEYS:
        path = getattr(config.storage, key)
        info: dict[str, Any] = {"path": str(path)}
        try:
            usage = shutil.disk_usage(path)
            info["free"] = usage.free
            info["total"] = usage.total
        except OSError:
            info["free"] = info["total"] = None
        out[key] = info
    return out


def check_storage(updates: dict[str, Any]) -> dict[str, Any]:
    """Dry-run validation of candidate paths, without applying anything."""
    out: dict[str, Any] = {}
    for key in STORAGE_KEYS:
        if key not in updates:
            continue
        path = _resolve_dir(updates[key])
        info = validate_storage_dir(path, create=False)
        info["path"] = str(path)
        out[key] = info
    return out


def apply_storage(config: Any, db: Any, updates: dict[str, Any]) -> dict[str, str]:
    """Validate + relocate storage paths: create/validate each target, apply to
    the live config in place, and persist. Does NOT move existing footage — that
    is a separate, explicit migration. Raises SettingError on an unusable path."""
    resolved: dict[str, Path] = {}
    for key in STORAGE_KEYS:
        if key not in updates or str(updates[key]).strip() == "":
            continue
        path = _resolve_dir(updates[key])
        info = validate_storage_dir(path, create=True)
        if not info["ok"]:
            raise SettingError(f"{key}: {info['error']}")
        resolved[key] = path
    if not resolved:
        raise SettingError("no storage paths given")

    applied: dict[str, str] = {}
    for key, path in resolved.items():
        setattr(config.storage, key, path)
        applied[key] = str(path)

    stored = {}
    raw = db.get_setting("storage")
    if raw:
        try:
            stored = json.loads(raw)
        except ValueError:
            stored = {}
    stored.update(applied)
    db.set_setting("storage", json.dumps(stored))
    return applied


# ---- recordings pool (ordered volumes with per-volume caps) ----------------

def volumes_current(config: Any) -> list[dict[str, Any]]:
    return [{"path": str(v.path), "cap": v.cap} for v in config.storage.volumes]


def apply_volumes(config: Any, db: Any, items: Any) -> list[dict[str, Any]]:
    """Validate + apply the ordered recordings pool. Each item is {path, cap}.
    Every path is created/validated writable; caps must parse; at least one
    volume is required. Atomic: raises SettingError, changing nothing, on any
    problem."""
    if not isinstance(items, list) or not items:
        raise SettingError("at least one volume is required")
    resolved: list[StorageVolume] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        item = item or {}
        raw_path = str(item.get("path", "")).strip()
        if not raw_path:
            raise SettingError(f"volume {i + 1}: path is required")
        path = _resolve_dir(raw_path)
        if str(path) in seen:
            raise SettingError(f"duplicate volume {path}")
        seen.add(str(path))
        info = validate_storage_dir(path, create=True)
        if not info["ok"]:
            raise SettingError(f"volume {i + 1} ({path}): {info['error']}")
        try:
            cap = _max_usage(item.get("cap", "80%"))
        except SettingError as exc:
            raise SettingError(f"volume {i + 1}: {exc}") from None
        resolved.append(StorageVolume(path, cap))

    config.storage.volumes = resolved
    db.set_setting("volumes", json.dumps(
        [{"path": str(v.path), "cap": v.cap} for v in resolved]
    ))
    return volumes_current(config)
