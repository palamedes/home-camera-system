"""Camera discovery.

Three signals, merged by IP:

  1. ONVIF WS-Discovery multicast — fast and authoritative, but plenty of
     cameras have it disabled by default or drop multicast on a VLAN.
  2. TCP sweep of the local subnets for camera-ish ports — catches everything
     reachable, including devices that ignore multicast.
  3. Vendor fingerprinting (Reolink HTTP API, ONVIF service probe, MAC OUI) to
     turn "something is listening on 554" into a named model.

Nothing here needs credentials: discovery lists what exists, and the user
supplies credentials when adding a specific camera.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from typing import Any

import httpx

from . import onvif, reolink

# Ports worth probing. 554 is RTSP; 8000/8899/2020/9000 are vendor control
# ports that distinguish a camera from a random web server on :80.
CAMERA_PORTS = (554, 80, 443, 8000, 8899, 2020, 9000, 8554)
STRONG_PORTS = (554, 8554, 8000, 8899, 2020, 9000)

# MAC prefixes seen on consumer camera gear. A hint for labelling only — never
# load-bearing, since vendors buy blocks constantly.
OUI_VENDORS = {
    "ec:71:db": "Reolink",
    "b0:c5:54": "Dahua",
    "3c:ef:8c": "Dahua",
    "44:47:cc": "Amcrest",
    "9c:8e:cd": "Amcrest",
    "c0:56:e3": "Hikvision",
    "44:19:b6": "Hikvision",
    "bc:ad:28": "Hikvision",
    "00:40:8c": "Axis",
    "ac:cc:8e": "Axis",
    "2c:aa:8e": "Wyze",
    "78:8b:2a": "Wyze",
    "b4:b0:24": "TP-Link",
    "78:8c:b5": "Ubiquiti",
    "24:5a:4c": "Ubiquiti",
}


@dataclass
class Candidate:
    host: str
    ports: list[int] = field(default_factory=list)
    mac: str | None = None
    vendor: str | None = None
    brand: str | None = None
    model: str | None = None
    name: str | None = None
    onvif_url: str | None = None
    onvif: bool = False
    rtsp: bool = False
    known: bool = False        # already added to the NVR
    camera_id: str | None = None
    confidence: str = "low"    # low | medium | high

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def local_subnets() -> list[str]:
    """IPv4 networks this box is on, excluding loopback.

    Capped at /24-sized sweeps: scanning a /16 would take minutes and home
    networks are never that wide in practice.
    """
    nets: list[str] = []
    try:
        output = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return nets

    for line in output.splitlines():
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", line)
        if not match:
            continue
        try:
            iface = ipaddress.ip_interface(match.group(1))
        except ValueError:
            continue
        if iface.ip.is_loopback:
            continue
        network = iface.network
        if network.num_addresses > 1024:
            network = ipaddress.ip_network(f"{iface.ip}/24", strict=False)
        nets.append(str(network))
    return nets


def arp_table() -> dict[str, str]:
    """IP -> MAC from the kernel neighbour table."""
    table: dict[str, str] = {}
    try:
        output = subprocess.run(
            ["ip", "neigh"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return table
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "lladdr" and ":" in parts[4]:
            table[parts[0]] = parts[4].lower()
    return table


def _check_port(host: str, port: int, timeout: float) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def sweep(subnets: list[str], timeout: float = 1.0, workers: int = 256) -> dict[str, list[int]]:
    """Concurrent TCP probe of camera ports across the given networks."""
    targets: list[tuple[str, int]] = []
    for cidr in subnets:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        hosts = network.hosts() if network.num_addresses > 2 else [network.network_address]
        for address in hosts:
            for port in CAMERA_PORTS:
                targets.append((str(address), port))

    open_ports: dict[str, list[int]] = {}
    if not targets:
        return open_ports

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda t: (t, _check_port(t[0], t[1], timeout)), targets)
        for (host, port), is_open in results:
            if is_open:
                open_ports.setdefault(host, []).append(port)
    for ports in open_ports.values():
        ports.sort()
    return open_ports


def _probe_onvif_endpoint(host: str, timeout: float = 3.0) -> str | None:
    """Find the ONVIF service URL by trying the conventional paths.

    Cameras that answered multicast already told us; this covers the ones that
    didn't but still speak ONVIF.
    """
    paths = ("/onvif/device_service", "/onvif/services", "/onvif/Device")
    for port in (80, 8000, 2020, 8899):
        for path in paths:
            url = f"http://{host}:{port}{path}"
            try:
                response = httpx.post(
                    url,
                    content=(
                        '<?xml version="1.0"?>'
                        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
                        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
                        "<s:Body><tds:GetSystemDateAndTime/></s:Body></s:Envelope>"
                    ).encode(),
                    headers={"Content-Type": "application/soap+xml; charset=utf-8"},
                    timeout=timeout,
                )
            except Exception:
                continue
            # A SOAP fault still proves ONVIF is listening.
            if b"Envelope" in response.content:
                return url
    return None


def _identify(candidate: Candidate, timeout: float) -> Candidate:
    """Enrich one candidate with vendor detail. Best-effort and never raises."""
    if candidate.mac:
        candidate.vendor = OUI_VENDORS.get(candidate.mac[:8])

    has_strong_port = any(p in STRONG_PORTS for p in candidate.ports)
    candidate.rtsp = 554 in candidate.ports or 8554 in candidate.ports

    if candidate.vendor == "Reolink" or 9000 in candidate.ports:
        try:
            if reolink.looks_like_reolink(candidate.host, timeout=timeout + 2):
                candidate.brand = "reolink"
                candidate.confidence = "high"
        except Exception:
            pass

    if not candidate.onvif_url:
        candidate.onvif_url = _probe_onvif_endpoint(candidate.host, timeout=timeout + 1)
    candidate.onvif = bool(candidate.onvif_url)

    if candidate.onvif:
        candidate.confidence = "high"
        if not candidate.brand and candidate.vendor:
            candidate.brand = candidate.vendor.lower()
    elif candidate.confidence != "high":
        if candidate.rtsp and has_strong_port:
            candidate.confidence = "medium"
        elif candidate.rtsp:
            candidate.confidence = "medium"

    if not candidate.brand and candidate.vendor:
        candidate.brand = candidate.vendor.lower()
    return candidate


def discover(
    subnets: list[str] | None = None,
    timeout: float = 1.0,
    onvif_wait: float = 3.0,
    known_hosts: dict[str, str] | None = None,
) -> list[Candidate]:
    """Full discovery pass. Returns likely cameras, best matches first."""
    subnets = subnets or local_subnets()
    known_hosts = known_hosts or {}

    candidates: dict[str, Candidate] = {}

    # Multicast first — it is cheap and gives us exact service URLs.
    try:
        for match in onvif.ws_discover(timeout=onvif_wait):
            entry = candidates.setdefault(match["host"], Candidate(host=match["host"]))
            entry.onvif_url = match["address"]
            entry.onvif = True
            entry.confidence = "high"
    except Exception:
        pass

    for host, ports in sweep(subnets, timeout=timeout).items():
        entry = candidates.setdefault(host, Candidate(host=host))
        entry.ports = ports

    # Drop hosts with only :80/:443 open and no ONVIF hint — routers, NAS boxes,
    # printers. Keeping them would bury the real cameras in noise.
    macs = arp_table()
    filtered: list[Candidate] = []
    for host, entry in candidates.items():
        entry.mac = macs.get(host)
        interesting = (
            entry.onvif
            or any(port in STRONG_PORTS for port in entry.ports)
            or OUI_VENDORS.get((entry.mac or "")[:8]) is not None
        )
        if interesting:
            filtered.append(entry)

    with ThreadPoolExecutor(max_workers=16) as pool:
        identified = list(pool.map(lambda c: _identify(c, timeout), filtered))

    for entry in identified:
        if entry.host in known_hosts:
            entry.known = True
            entry.camera_id = known_hosts[entry.host]

    rank = {"high": 0, "medium": 1, "low": 2}
    identified.sort(key=lambda c: (rank[c.confidence], c.host))
    return identified


def inspect(
    host: str, username: str, password: str, brand: str | None = None,
    onvif_url: str | None = None,
) -> dict[str, Any]:
    """Authenticate against one camera and report its streams.

    Tries the vendor API first when we recognise the brand, then ONVIF. Returns
    a dict the add-camera UI can render directly.
    """
    result: dict[str, Any] = {
        "host": host, "brand": brand, "model": None, "name": None,
        "serial": None, "firmware": None, "streams": [], "source": None,
        "error": None,
    }

    if brand == "reolink" or (brand is None and reolink.looks_like_reolink(host)):
        try:
            with reolink.ReolinkClient(host, username, password) as client:
                info = client.device_info()
                result.update(
                    brand="reolink", source="reolink-api", model=info.model,
                    name=info.name, serial=info.serial, firmware=info.firmware,
                )
                for stream in info.streams:
                    result["streams"].append({
                        "name": stream.name,
                        "codec": stream.codec,
                        "resolution": stream.resolution,
                        "fps": stream.fps,
                        "bitrate": stream.bitrate,
                        "url": reolink.rtsp_url(
                            host, username, password, stream.name, stream.codec
                        ),
                    })
                client.logout()
            if result["streams"]:
                return result
        except Exception as exc:
            result["error"] = f"Reolink API: {exc}"

    try:
        device = onvif.OnvifDevice(
            host, username, password, service_url=onvif_url
        )
        info = device.probe()
        result.update(
            source="onvif",
            brand=result["brand"] or (info.manufacturer or "").lower() or None,
            model=info.model or result["model"],
            serial=info.serial or result["serial"],
            firmware=info.firmware or result["firmware"],
            error=None if info.profiles else result["error"],
        )
        for profile in info.profiles:
            if not profile.stream_uri:
                continue
            result["streams"].append({
                "name": profile.name,
                "codec": (profile.encoding or "").lower() or None,
                "resolution": profile.resolution,
                "fps": profile.fps,
                "bitrate": profile.bitrate,
                "url": onvif.with_credentials(profile.stream_uri, username, password),
            })
    except Exception as exc:
        if not result["streams"]:
            result["error"] = f"{result['error'] + '; ' if result['error'] else ''}ONVIF: {exc}"

    return result
