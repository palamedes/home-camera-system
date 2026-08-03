"""Camera device control: spotlight and night-vision toggles.

These are per-camera *device* properties (a white-LED floodlight, and the
day/night colour mode plus IR illuminator), not stream settings — so they live
apart from streams.py and go2rtc. Only Reolink is implemented; every other
brand reports "unsupported" (None) rather than erroring, so callers can probe
any camera uniformly.

The public surface is deliberately small and stable — a scheduler soft-imports
it — so keep these names and signatures:

    get_controls(camera) -> dict
    set_light(camera, on: bool) -> None
    set_night_vision(camera, *, mode=None, ir=None) -> None
    class CameraControlError(Exception)

Reads are best-effort: a camera that doesn't answer a given query yields None
for that control instead of raising, so an unsupported/older model still
returns a usable dict. Writes are strict: any failure raises
CameraControlError with a human-readable message.

Reuses the native Reolink HTTP client (reolink.py) for login/token/command
POSTing rather than opening a second, divergent client.
"""

from __future__ import annotations

from typing import Any

from .reolink import ReolinkClient


class CameraControlError(Exception):
    """A device-control command failed (bad credentials, unsupported command,
    the camera returned an error code, or it was unreachable)."""


# ---------------------------------------------------------------------------
# Value mapping: our stable public vocabulary <-> Reolink's API strings.
# Kept as module-level dicts so they can be unit-tested without a camera.
# ---------------------------------------------------------------------------

# Day/night colour mode (Isp.dayNight).
_MODE_TO_REOLINK = {"auto": "Auto", "color": "Color", "bw": "Black&White"}
_MODE_FROM_REOLINK = {
    "auto": "auto",
    "color": "color",
    "black&white": "bw",
    "blackwhite": "bw",
    "bw": "bw",
}

# IR illuminator (IrLights.state). Firmware has used both "Off" and "Close"
# for the disabled state; accept either on read.
_IR_TO_REOLINK = {"auto": "Auto", "on": "On", "off": "Off"}
_IR_FROM_REOLINK = {
    "auto": "auto",
    "on": "on",
    "off": "off",
    "close": "off",
}


def _mode_to_reolink(mode: str) -> str:
    try:
        return _MODE_TO_REOLINK[mode]
    except KeyError:
        raise CameraControlError(
            f"unknown day/night mode {mode!r} (want auto|color|bw)"
        ) from None


def _mode_from_reolink(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return _MODE_FROM_REOLINK.get(value.strip().lower())


def _ir_to_reolink(ir: str) -> str:
    try:
        return _IR_TO_REOLINK[ir]
    except KeyError:
        raise CameraControlError(
            f"unknown IR setting {ir!r} (want auto|on|off)"
        ) from None


def _ir_from_reolink(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return _IR_FROM_REOLINK.get(value.strip().lower())


# ---------------------------------------------------------------------------
# Camera-row helpers
# ---------------------------------------------------------------------------

# Reolink models use a single sensor on channel 0. NVRs would need a per-camera
# channel; that's out of scope for the direct-attached cameras here.
_CHANNEL = 0


def _field(camera: Any, name: str, default: Any = None) -> Any:
    """Read a column from a sqlite3.Row (or a plain dict) without raising."""
    try:
        value = camera[name]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _is_reolink(camera: Any) -> bool:
    return str(_field(camera, "brand", "")).strip().lower() == "reolink"


def _require_reolink(camera: Any) -> None:
    if not _is_reolink(camera):
        brand = _field(camera, "brand") or "unknown"
        raise CameraControlError(
            f"device control is only supported on Reolink cameras (this is {brand!r})"
        )


def _client(camera: Any) -> ReolinkClient:
    return ReolinkClient(
        host=str(_field(camera, "host", "")),
        username=str(_field(camera, "username", "") or ""),
        password=str(_field(camera, "password", "") or ""),
    )


# ---------------------------------------------------------------------------
# Reolink reads (best-effort; return None on any failure)
# ---------------------------------------------------------------------------


def _read_light(client: ReolinkClient) -> bool | None:
    try:
        data = client._call(
            [{"cmd": "GetWhiteLed", "action": 0, "param": {"channel": _CHANNEL}}]
        )
        state = data[0]["value"]["WhiteLed"]["state"]
        return bool(int(state))
    except Exception:
        return None


def _read_day_night(client: ReolinkClient) -> str | None:
    try:
        data = client._call(
            [{"cmd": "GetIsp", "action": 1, "param": {"channel": _CHANNEL}}]
        )
        return _mode_from_reolink(data[0]["value"]["Isp"].get("dayNight"))
    except Exception:
        return None


def _read_ir(client: ReolinkClient) -> str | None:
    try:
        data = client._call(
            [{"cmd": "GetIrLights", "action": 0, "param": {"channel": _CHANNEL}}]
        )
        return _ir_from_reolink(data[0]["value"]["IrLights"].get("state"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_controls(camera: Any) -> dict[str, Any]:
    """Current device-control state.

    Returns {"light": bool|None, "night_vision": {"mode", "ir"}|None}. A value
    of None means "unsupported or currently unknown" — either the brand isn't
    handled, the camera didn't answer, or that specific query failed. Never
    raises; a failed probe simply yields Nones.
    """
    result: dict[str, Any] = {"light": None, "night_vision": None}
    if not _is_reolink(camera):
        return result
    try:
        with _client(camera) as client:
            client.login()
            result["light"] = _read_light(client)
            mode = _read_day_night(client)
            ir = _read_ir(client)
            if mode is not None or ir is not None:
                result["night_vision"] = {"mode": mode, "ir": ir}
    except Exception:
        # Unreachable / bad credentials: report everything as unknown rather
        # than surfacing an error on a read.
        return result
    return result


def set_light(camera: Any, on: bool) -> None:
    """Turn the white-LED spotlight/floodlight on or off.

    Raises CameraControlError on any failure.
    """
    _require_reolink(camera)
    try:
        with _client(camera) as client:
            client.login()
            client._call(
                [
                    {
                        "cmd": "SetWhiteLed",
                        "param": {
                            "WhiteLed": {"channel": _CHANNEL, "state": 1 if on else 0}
                        },
                    }
                ]
            )
    except CameraControlError:
        raise
    except Exception as exc:
        raise CameraControlError(f"could not set spotlight: {exc}") from exc


def set_night_vision(
    camera: Any, *, mode: str | None = None, ir: str | None = None
) -> None:
    """Set the day/night colour mode and/or the IR illuminator.

    `mode` is one of "auto" | "color" | "bw" (maps to Isp.dayNight).
    `ir`   is one of "auto" | "on"    | "off" (maps to IrLights.state).
    Pass either or both; passing neither is an error. Raises
    CameraControlError on any failure.
    """
    _require_reolink(camera)
    if mode is None and ir is None:
        raise CameraControlError("set_night_vision: nothing to set")

    # Validate/translate up front so a bad argument fails before we touch the
    # network (and never leaves a half-applied change).
    isp_value = _mode_to_reolink(mode) if mode is not None else None
    ir_value = _ir_to_reolink(ir) if ir is not None else None

    try:
        with _client(camera) as client:
            client.login()
            if isp_value is not None:
                client._call(
                    [
                        {
                            "cmd": "SetIsp",
                            "param": {
                                "Isp": {"channel": _CHANNEL, "dayNight": isp_value}
                            },
                        }
                    ]
                )
            if ir_value is not None:
                client._call(
                    [
                        {
                            "cmd": "SetIrLights",
                            "param": {
                                "IrLights": {"channel": _CHANNEL, "state": ir_value}
                            },
                        }
                    ]
                )
    except CameraControlError:
        raise
    except Exception as exc:
        raise CameraControlError(f"could not set night vision: {exc}") from exc
