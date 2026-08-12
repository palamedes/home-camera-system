"""Control non-camera devices — relays and smart switches — over plain HTTP.

Sentry already knows things nothing else on the LAN knows (a person is on the
driveway; it is 20 minutes past sunset; someone pressed the porch switch). This
module is the other half of that: the ability to *act* on it without handing the
job to a separate home-automation stack.

Deliberately small. A device is an address plus a driver name; a driver knows
how to phrase "on", "off" and "what are you?" for its kind of hardware. Adding a
brand means adding one entry to DRIVERS, not touching anything else.

Everything here is local HTTP on the LAN — no cloud, no vendor account, no hub,
matching how the rest of Sentry talks to cameras.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

TIMEOUT = 5.0


class DeviceError(RuntimeError):
    """A device could not be reached or refused the command."""


def _auth(device: Any) -> httpx.DigestAuth | None:
    """Shelly uses digest auth, and only when a password has been set."""
    password = device["password"] if "password" in device.keys() else None
    if not password:
        return None
    user = (device["username"] if "username" in device.keys() else None) or "admin"
    return httpx.DigestAuth(user, password)


def _get(device: Any, path: str, params: dict[str, Any] | None = None) -> Any:
    url = f"http://{device['host']}{path}"
    try:
        response = httpx.get(url, params=params, auth=_auth(device), timeout=TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise DeviceError(f"{device['host']}: {exc}") from exc
    try:
        return response.json()
    except ValueError:
        return {}


# --- drivers ---------------------------------------------------------------
# Each driver exposes set_state(device, on) -> bool and get_state(device) -> bool
# | None. get_state returns None when the device does not report one.


class ShellyDriver:
    """Shelly Gen2+ (Plus/Pro/Gen3/Gen4) RPC API.

    Gen4 also speaks Matter and Zigbee, but those need a controller or a hub —
    the point of using HTTP is that Sentry can be the only brain on the network.
    """

    name = "shelly"
    label = "Shelly (Gen 2/3/4)"

    @staticmethod
    def set_state(device: Any, on: bool) -> bool:
        _get(device, "/rpc/Switch.Set",
             {"id": device["channel"], "on": "true" if on else "false"})
        return on

    @staticmethod
    def get_state(device: Any) -> bool | None:
        data = _get(device, "/rpc/Switch.GetStatus", {"id": device["channel"]})
        value = data.get("output") if isinstance(data, dict) else None
        return bool(value) if value is not None else None

    @staticmethod
    def identify(device: Any) -> dict[str, Any]:
        """Model/firmware/name, for confirming the right box answered."""
        data = _get(device, "/rpc/Shelly.GetDeviceInfo") or {}
        return {
            "model": data.get("model") or data.get("app"),
            "name": data.get("name"),
            "firmware": data.get("ver"),
            "mac": data.get("mac"),
            "generation": data.get("gen"),
        }


class ShellyGen1Driver:
    """Older Shelly 1/1PM (Gen1) used the /relay endpoints instead of RPC."""

    name = "shelly-gen1"
    label = "Shelly (Gen 1)"

    @staticmethod
    def set_state(device: Any, on: bool) -> bool:
        _get(device, f"/relay/{device['channel']}", {"turn": "on" if on else "off"})
        return on

    @staticmethod
    def get_state(device: Any) -> bool | None:
        data = _get(device, f"/relay/{device['channel']}")
        value = data.get("ison") if isinstance(data, dict) else None
        return bool(value) if value is not None else None

    @staticmethod
    def identify(device: Any) -> dict[str, Any]:
        data = _get(device, "/shelly") or {}
        return {
            "model": data.get("type"),
            "name": None,
            "firmware": data.get("fw"),
            "mac": data.get("mac"),
            "generation": 1,
        }


DRIVERS: dict[str, Any] = {
    ShellyDriver.name: ShellyDriver,
    ShellyGen1Driver.name: ShellyGen1Driver,
}


def driver_choices() -> list[dict[str, str]]:
    return [{"value": d.name, "label": d.label} for d in DRIVERS.values()]


def _driver(device: Any) -> Any:
    driver = DRIVERS.get(device["driver"])
    if driver is None:
        raise DeviceError(f"unknown driver {device['driver']!r}")
    return driver


# --- public API ------------------------------------------------------------

def set_state(device: Any, on: bool) -> bool:
    return _driver(device).set_state(device, bool(on))


def get_state(device: Any) -> bool | None:
    return _driver(device).get_state(device)


def identify(device: Any) -> dict[str, Any]:
    return _driver(device).identify(device)


def toggle(device: Any) -> bool:
    """Flip the device. Falls back to 'on' when it does not report a state, so a
    button still does something useful rather than failing."""
    current = get_state(device)
    want = not current if current is not None else True
    return set_state(device, want)
