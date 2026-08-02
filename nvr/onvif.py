"""Minimal ONVIF client.

Speaks SOAP directly instead of using zeep. ONVIF's WSDLs are enormous and zeep
parses them at runtime; for the four calls we actually need (device info,
profiles, stream URIs, plus the unauthenticated clock read) hand-rolled XML is
smaller, faster to start, and one less dependency to keep patched.

The clock read is not optional politeness: WS-Security digests embed a
timestamp, and cameras reject requests whose clock is skewed from theirs. We
read the device's own time first and apply the offset, which is why cameras
with a dead RTC still authenticate.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "d": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
}

_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
_PASSWORD_DIGEST = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)


@dataclass
class Profile:
    token: str
    name: str
    encoding: str | None = None
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    bitrate: int | None = None
    stream_uri: str | None = None

    @property
    def resolution(self) -> str | None:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return None


@dataclass
class DeviceInfo:
    manufacturer: str | None = None
    model: str | None = None
    firmware: str | None = None
    serial: str | None = None
    hardware: str | None = None
    profiles: list[Profile] = field(default_factory=list)


def _security_header(username: str, password: str, clock_offset: float) -> str:
    """WS-Security UsernameToken with a password digest."""
    nonce = secrets.token_bytes(16)
    created = (
        datetime.now(timezone.utc) + timedelta(seconds=clock_offset)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = hashlib.sha1(nonce + created.encode() + password.encode()).digest()
    return (
        f'<s:Header><Security s:mustUnderstand="1" xmlns="{_WSSE}">'
        f"<UsernameToken><Username>{_esc(username)}</Username>"
        f'<Password Type="{_PASSWORD_DIGEST}">{base64.b64encode(digest).decode()}</Password>'
        f'<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f"{base64.b64encode(nonce).decode()}</Nonce>"
        f'<Created xmlns="{_WSU}">{created}</Created>'
        f"</UsernameToken></Security></s:Header>"
    )


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _envelope(body: str, header: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{NS["s"]}" xmlns:tds="{NS["tds"]}" '
        f'xmlns:trt="{NS["trt"]}" xmlns:tt="{NS["tt"]}">'
        f"{header}<s:Body>{body}</s:Body></s:Envelope>"
    )


def _text(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(path, NS)
    return found.text if found is not None and found.text else None


def _int(node: ET.Element | None, path: str) -> int | None:
    raw = _text(node, path)
    try:
        return int(float(raw)) if raw is not None else None
    except ValueError:
        return None


class OnvifDevice:
    """One camera's ONVIF endpoints."""

    def __init__(
        self,
        host: str,
        username: str = "",
        password: str = "",
        port: int = 80,
        service_url: str | None = None,
        timeout: float = 6.0,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self.device_url = service_url or f"http://{host}:{port}/onvif/device_service"
        self.media_url = self.device_url.replace("device_service", "media_service")
        self.clock_offset = 0.0

    def _post(self, url: str, body: str, action: str, authed: bool = True) -> ET.Element:
        header = (
            _security_header(self.username, self.password, self.clock_offset)
            if authed and self.username
            else ""
        )
        response = httpx.post(
            url,
            content=_envelope(body, header).encode(),
            headers={
                "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'
            },
            timeout=self.timeout,
            verify=False,
        )
        response.raise_for_status()
        return ET.fromstring(response.content)

    def sync_clock(self) -> None:
        """Measure skew against the camera. Unauthenticated per the ONVIF spec."""
        try:
            root = self._post(
                self.device_url,
                "<tds:GetSystemDateAndTime/>",
                "http://www.onvif.org/ver10/device/wsdl/GetSystemDateAndTime",
                authed=False,
            )
        except Exception:
            return
        utc = root.find(".//tt:UTCDateTime", NS)
        if utc is None:
            return
        try:
            device_time = datetime(
                _int(utc, "tt:Date/tt:Year") or 1970,
                _int(utc, "tt:Date/tt:Month") or 1,
                _int(utc, "tt:Date/tt:Day") or 1,
                _int(utc, "tt:Time/tt:Hour") or 0,
                _int(utc, "tt:Time/tt:Minute") or 0,
                _int(utc, "tt:Time/tt:Second") or 0,
                tzinfo=timezone.utc,
            )
        except ValueError:
            return
        self.clock_offset = (device_time - datetime.now(timezone.utc)).total_seconds()

    def device_information(self) -> DeviceInfo:
        root = self._post(
            self.device_url,
            "<tds:GetDeviceInformation/>",
            "http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation",
        )
        body = root.find(".//tds:GetDeviceInformationResponse", NS)
        return DeviceInfo(
            manufacturer=_text(body, "tds:Manufacturer"),
            model=_text(body, "tds:Model"),
            firmware=_text(body, "tds:FirmwareVersion"),
            serial=_text(body, "tds:SerialNumber"),
            hardware=_text(body, "tds:HardwareId"),
        )

    def profiles(self) -> list[Profile]:
        root = self._post(
            self.media_url,
            "<trt:GetProfiles/>",
            "http://www.onvif.org/ver10/media/wsdl/GetProfiles",
        )
        out: list[Profile] = []
        for node in root.findall(".//trt:Profiles", NS):
            enc = node.find(".//tt:VideoEncoderConfiguration", NS)
            out.append(
                Profile(
                    token=node.get("token", ""),
                    name=_text(node, "tt:Name") or node.get("token", ""),
                    encoding=_text(enc, "tt:Encoding"),
                    width=_int(enc, "tt:Resolution/tt:Width"),
                    height=_int(enc, "tt:Resolution/tt:Height"),
                    fps=_int(enc, "tt:RateControl/tt:FrameRateLimit"),
                    bitrate=_int(enc, "tt:RateControl/tt:BitrateLimit"),
                )
            )
        return out

    def stream_uri(self, profile_token: str) -> str | None:
        body = (
            "<trt:GetStreamUri>"
            "<trt:StreamSetup>"
            "<tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
            "</trt:StreamSetup>"
            f"<trt:ProfileToken>{_esc(profile_token)}</trt:ProfileToken>"
            "</trt:GetStreamUri>"
        )
        root = self._post(
            self.media_url, body, "http://www.onvif.org/ver10/media/wsdl/GetStreamUri"
        )
        return _text(root, ".//tt:Uri")

    def probe(self) -> DeviceInfo:
        """Everything we need to add the camera, in one call."""
        self.sync_clock()
        info = self.device_information()
        try:
            profiles = self.profiles()
        except Exception:
            profiles = []
        for profile in profiles:
            try:
                profile.stream_uri = self.stream_uri(profile.token)
            except Exception:
                pass
        # Highest resolution first, so "main" and "sub" fall out naturally.
        profiles.sort(key=lambda p: (p.width or 0) * (p.height or 0), reverse=True)
        info.profiles = profiles
        return info


def with_credentials(uri: str, username: str, password: str) -> str:
    """Inject credentials into an RTSP URL.

    Cameras usually return stream URIs without them, and ffmpeg/go2rtc need
    them inline. Percent-encodes so passwords containing '@' or ':' survive.
    """
    if not username or "@" in uri.split("//", 1)[-1].split("/", 1)[0]:
        return uri
    from urllib.parse import quote

    scheme, _, rest = uri.partition("://")
    if not rest:
        return uri
    return f"{scheme}://{quote(username, safe='')}:{quote(password, safe='')}@{rest}"


def ws_discover(timeout: float = 3.0) -> list[dict[str, str]]:
    """WS-Discovery multicast probe for ONVIF devices on the local segment.

    Returns dicts with 'address' (the ONVIF service URL) and 'host'. Devices
    that ignore multicast are still found by the TCP sweep in discovery.py.
    """
    import socket
    import uuid

    message = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        f"<e:Header><w:MessageID>uuid:{uuid.uuid4()}</w:MessageID>"
        "<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
        "<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>"
        "</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types>"
        "</d:Probe></e:Body></e:Envelope>"
    ).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    found: dict[str, dict[str, str]] = {}
    try:
        sock.sendto(message, ("239.255.255.250", 3702))
        deadline = timeout
        import time as _time

        started = _time.monotonic()
        while _time.monotonic() - started < deadline:
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            for url in re.findall(rb"http[s]?://[^\s<]+", data):
                address = url.decode(errors="ignore")
                host = address.split("//", 1)[-1].split("/", 1)[0].split(":")[0]
                found.setdefault(address, {"address": address, "host": host})
    finally:
        sock.close()
    return list(found.values())
