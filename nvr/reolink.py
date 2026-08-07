"""Reolink HTTP API.

Reolink's ONVIF support exists but is thin — it reports fewer profiles than the
camera actually serves, and on the fisheye models it omits stream variants
entirely. The native API is more honest about what a given camera can do, so we
prefer it when we recognise a Reolink and fall back to ONVIF otherwise.

Cameras ship self-signed certs, so TLS verification is off. That is acceptable
only because this traffic never leaves the LAN; if cameras ever sit on an
untrusted network this needs revisiting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx


@dataclass
class ReolinkStream:
    name: str          # "main" | "sub" | "ext"
    codec: str         # "h264" | "h265"
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    bitrate: int | None = None

    @property
    def resolution(self) -> str | None:
        return f"{self.width}x{self.height}" if self.width and self.height else None


@dataclass
class ReolinkInfo:
    model: str | None = None
    name: str | None = None
    firmware: str | None = None
    serial: str | None = None
    hardware: str | None = None
    channels: int = 1
    streams: list[ReolinkStream] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def looks_like_reolink(host: str, timeout: float = 4.0) -> bool:
    """Fingerprint without credentials.

    An unauthenticated api.cgi call returns a JSON error rather than HTML,
    which no other vendor on the LAN does. Reolink redirects :80 to HTTPS, so
    try both.
    """
    for base in (f"https://{host}", f"http://{host}"):
        try:
            response = httpx.get(
                f"{base}/cgi-bin/api.cgi",
                params={"cmd": "GetDevInfo"},
                timeout=timeout,
                verify=False,
                follow_redirects=True,
            )
            payload = response.json()
        except Exception:
            continue
        if isinstance(payload, list) and payload and "cmd" in payload[0]:
            return True
    return False


class ReolinkClient:
    def __init__(self, host: str, username: str, password: str, timeout: float = 8.0):
        self.host = host
        self.username = username
        self.password = password
        self.timeout = timeout
        self.token: str | None = None
        # Firmware from ~2023 on refuses plain HTTP for login.
        self.base = f"https://{host}"
        self._client = httpx.Client(
            timeout=timeout, verify=False, follow_redirects=True
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ReolinkClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        # Release the camera-side session before closing the socket. Reolink caps
        # concurrent logins; without this the session lingers until the camera
        # times it out, and repeated short-lived clients hit "Login: max session"
        # (which then starves other API calls like the light/night-vision reads).
        try:
            self.logout()
        except Exception:
            pass
        self.close()

    def _call(self, commands: list[dict[str, Any]], authed: bool = True) -> list[dict]:
        params: dict[str, Any] = {"cmd": commands[0]["cmd"]}
        if authed:
            if self.token is None:
                self.login()
            params["token"] = self.token
        response = self._client.post(
            f"{self.base}/cgi-bin/api.cgi", params=params, json=commands
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError(f"unexpected Reolink response: {payload!r}")
        for entry in payload:
            if entry.get("code") not in (0, None):
                detail = (entry.get("error") or {}).get("detail", "unknown error")
                raise RuntimeError(f"{entry.get('cmd')}: {detail}")
        return payload

    def login(self) -> None:
        payload = self._call(
            [
                {
                    "cmd": "Login",
                    "param": {
                        "User": {
                            "Version": "0",
                            "userName": self.username,
                            "password": self.password,
                        }
                    },
                }
            ],
            authed=False,
        )
        self.token = payload[0]["value"]["Token"]["name"]

    def logout(self) -> None:
        if self.token:
            try:
                self._call([{"cmd": "Logout", "param": {}}])
            except Exception:
                pass
            self.token = None

    def ai_state(self, channel: int = 0) -> dict[str, bool]:
        """Current onboard-AI alarm state, normalised to our vocabulary.

        Reolink reports each class as {alarm_state, support}; we map
        people->person, vehicle->vehicle, dog_cat->animal and only include
        classes the camera actually supports. An unsupported camera (older
        firmware, no AI) returns an empty dict.
        """
        data = self._call(
            [{"cmd": "GetAiState", "action": 0, "param": {"channel": channel}}]
        )
        value = data[0].get("value") or {}
        mapping = {"people": "person", "vehicle": "vehicle", "dog_cat": "animal"}
        out: dict[str, bool] = {}
        for reo_key, our_key in mapping.items():
            entry = value.get(reo_key)
            if isinstance(entry, dict) and int(entry.get("support", 0)):
                out[our_key] = bool(int(entry.get("alarm_state", 0)))
        return out

    def motion_state(self, channel: int = 0) -> bool | None:
        """Plain motion-detection state, or None if the camera won't report it."""
        try:
            data = self._call(
                [{"cmd": "GetMdState", "action": 0, "param": {"channel": channel}}]
            )
            return bool(int(data[0]["value"]["state"]))
        except Exception:
            return None

    def device_info(self) -> ReolinkInfo:
        info = ReolinkInfo()
        data = self._call([{"cmd": "GetDevInfo", "action": 0, "param": {}}])
        dev = data[0]["value"]["DevInfo"]
        info.raw = dev
        info.model = dev.get("model")
        info.name = dev.get("name")
        info.firmware = dev.get("firmVer")
        info.serial = dev.get("serial")
        info.hardware = dev.get("hardVer")
        info.channels = int(dev.get("channelNum") or 1)

        try:
            enc = self._call(
                [{"cmd": "GetEnc", "action": 1, "param": {"channel": 0}}]
            )[0]["value"]["Enc"]
        except Exception:
            return info

        for key in ("mainStream", "subStream", "extStream"):
            stream = enc.get(key)
            if not stream:
                continue
            size = str(stream.get("size") or "")
            width = height = None
            if "*" in size:
                parts = size.split("*")
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    width, height = int(parts[0]), int(parts[1])
            info.streams.append(
                ReolinkStream(
                    name=key.replace("Stream", ""),
                    codec=(stream.get("vType") or "h264").lower(),
                    width=width,
                    height=height,
                    fps=int(stream["frameRate"]) if stream.get("frameRate") else None,
                    bitrate=int(stream["bitRate"]) if stream.get("bitRate") else None,
                )
            )
        return info

    # ---- encoder control (GetEnc / SetEnc) -------------------------------

    def encoder_options(self, channel: int = 0) -> dict[str, Any]:
        """Resolutions and bitrates the camera advertises for each stream.

        GetEnc with action=1 returns both the current `value` and a `range` of
        acceptable settings. The range's shape varies across firmware, so the
        actual flattening lives in streamprobe.normalise_enc_range; here we just
        pull the per-stream blocks out. Returns {} on any failure — encoder
        control then degrades to read-only.
        """
        from . import streamprobe

        try:
            data = self._call(
                [{"cmd": "GetEnc", "action": 1, "param": {"channel": channel}}]
            )[0]
        except Exception:
            return {}
        rng = (data.get("range") or {})
        enc_range = rng.get("Enc") if isinstance(rng, dict) else None
        # Some firmware nests the per-stream ranges under a list.
        if isinstance(enc_range, list):
            enc_range = enc_range[0] if enc_range else None
        if not isinstance(enc_range, dict):
            return {}
        out: dict[str, Any] = {}
        for key, name in (("mainStream", "main"), ("subStream", "sub")):
            opts = streamprobe.normalise_enc_range(enc_range.get(key))
            if opts:
                out[name] = opts
        return out

    def set_encoding(
        self, stream: str, size: str | None = None,
        bitrate: int | None = None, channel: int = 0,
    ) -> None:
        """Change one stream's resolution and/or bitrate via SetEnc.

        SetEnc replaces the whole Enc block, so we read the current one, patch
        the requested stream in place, and write it all back — leaving every
        other setting (the other streams, audio, GOP, profile) untouched.
        Raises RuntimeError on an unsupported stream or a camera-side rejection.
        """
        key = {"main": "mainStream", "sub": "subStream", "ext": "extStream"}.get(stream)
        if not key:
            raise ValueError(f"unknown stream {stream!r}")
        enc = self._call(
            [{"cmd": "GetEnc", "action": 0, "param": {"channel": channel}}]
        )[0]["value"]["Enc"]
        block = dict(enc.get(key) or {})
        if not block:
            raise RuntimeError(f"camera has no {stream} stream to configure")
        if size is not None:
            block["size"] = str(size)
        if bitrate is not None:
            block["bitRate"] = int(bitrate)
        new_enc = dict(enc)
        new_enc["channel"] = int(enc.get("channel", channel))
        new_enc[key] = block
        self._call([{"cmd": "SetEnc", "param": {"Enc": new_enc}}])


def rtsp_url(
    host: str, username: str, password: str, stream: str = "main",
    codec: str = "h264", channel: int = 0, port: int = 554,
) -> str:
    """Reolink's RTSP path scheme.

    Path encodes the codec, so it must match how the camera is actually
    configured — requesting h264Preview on a stream set to H.265 fails rather
    than transcoding.
    """
    user = quote(username, safe="")
    pw = quote(password, safe="")
    channel_str = f"{channel + 1:02d}"
    return f"rtsp://{user}:{pw}@{host}:{port}/{codec}Preview_{channel_str}_{stream}"
