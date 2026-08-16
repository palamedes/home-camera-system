"""Finding things on the LAN, and keeping hold of them when they move.

Every device Sentry talks to is stored by address, and a DHCP lease change
silently breaks the integration — the only symptom being "it stopped working".
The MAC is the stable identity, so this module does two jobs:

  * **re-find a known device** whose address moved, by sweeping the subnet and
    matching its MAC exactly;
  * **inventory the network**, so the twenty-odd unidentified boxes on the LAN
    can be named rather than guessed at.

Two deliberate constraints.

**Exact MAC match, never a heuristic.** Not by hostname, not by "the only other
thing answering that protocol". A near-miss here means commanding somebody
else's hardware, which is a far worse outcome than staying broken until a human
looks.

**Vendor lookup is offline.** The IEEE registry ships with the OS; sending a
list of the house's MAC addresses to a web service to find out what they are
would be a poor trade.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("nvr.netscan")

# Where the IEEE OUI registry tends to live. First one that exists wins; if none
# do, vendor lookup degrades to None rather than reaching for the network.
OUI_PATHS = (
    "/usr/share/hwdata/oui.txt",
    "/var/lib/ieee-data/oui.txt",
    "/usr/share/misc/oui.txt",
    "/usr/share/nmap/nmap-mac-prefixes",
)

# A sweep sends one datagram per address; this is the port it pokes. Nothing
# should be listening — the point is only to make the kernel do the ARP.
PROBE_PORT = 9

# What a thing on the network can be. Grouped the way somebody standing in the
# house thinks about it, not the way a network engineer does.
#
# Icons are emoji rather than an icon font or an SVG sprite: this project ships
# no external assets and no JS dependencies, and a glyph that renders everywhere
# without a download is worth more here than a prettier set that needs one.
DEVICE_KINDS: tuple[tuple[str, str, str], ...] = (
    # value, label, icon
    ("unknown", "Unidentified", "❓"),
    # Computers and handhelds
    ("desktop", "Desktop", "🖥️"),
    ("laptop", "Laptop", "💻"),
    ("smartphone", "Phone", "📱"),
    ("tablet", "Tablet", "📲"),
    ("wearable", "Wearable", "⌚"),
    ("server", "Server", "🗄️"),
    # Network gear
    ("router", "Router", "📶"),
    ("extender", "Mesh node / extender", "📡"),
    ("network", "Network gear", "🌐"),
    ("nas", "NAS / storage", "💾"),
    ("printer", "Printer", "🖨️"),
    ("ip_phone", "IP phone", "☎️"),
    # Entertainment
    ("tv", "TV", "📺"),
    ("media", "Media streamer", "🎬"),
    ("speaker", "Smart speaker", "🔊"),
    ("gaming", "Games console", "🎮"),
    ("frame", "Digital frame", "🖼️"),
    # Security — the things this house already cares about
    ("camera", "Camera", "📹"),
    ("doorbell", "Doorbell", "🔔"),
    ("lock", "Smart lock", "🔒"),
    ("sensor", "Sensor", "🛰️"),
    ("alarm", "Alarm / security", "🛡️"),
    # Home control
    ("thermostat", "Thermostat", "🌡️"),
    ("light", "Light", "💡"),
    ("plug", "Smart plug / relay", "🔌"),
    ("blinds", "Blinds / shades", "🪟"),
    ("garage", "Garage door", "🚪"),
    ("irrigation", "Irrigation", "💧"),
    ("hub", "Hub / bridge", "🧩"),
    ("iot", "Other smart device", "📟"),
    # Appliances
    ("fridge", "Fridge", "🧊"),
    ("dishwasher", "Dishwasher", "🍽️"),
    ("oven", "Oven / stove", "🍳"),
    ("microwave", "Microwave", "🍲"),
    ("washer", "Washing machine", "🧺"),
    ("dryer", "Dryer", "🌀"),
    ("vacuum", "Robot vacuum", "🧹"),
    ("water_heater", "Water heater", "🚿"),
    ("hvac", "HVAC / air handler", "❄️"),
    ("appliance", "Other appliance", "🔧"),
    # Outdoors / marine, since there is a boat and a dock
    ("boat", "Boat / marine", "⛵"),
    ("vehicle", "Vehicle / charger", "🚗"),
    ("solar", "Solar / battery", "🔋"),
    ("weather", "Weather station", "🌦️"),
)

KIND_VALUES = frozenset(value for value, _label, _icon in DEVICE_KINDS)
KIND_ICONS = {value: icon for value, _label, icon in DEVICE_KINDS}


def kind_choices() -> list[dict[str, str]]:
    return [{"value": v, "label": l, "icon": i} for v, l, i in DEVICE_KINDS]

_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def normalise_mac(value: Any) -> str | None:
    """Lower-case colon form, or None. Accepts the usual spellings."""
    if not value:
        return None
    text = str(value).strip().lower().replace("-", ":").replace(".", ":")
    if ":" not in text and len(text) == 12:
        text = ":".join(text[i:i + 2] for i in range(0, 12, 2))
    if not _MAC_RE.match(text):
        return None
    if text == "00:00:00:00:00:00":
        return None          # an unresolved ARP entry, not a device
    return text


def is_randomised(mac: str) -> bool:
    """Whether this is a privacy-randomised address rather than a real NIC.

    The second-least-significant bit of the first octet is the
    locally-administered flag. Phones and laptops rotate these per network, so
    they are noise in an inventory of things worth controlling.
    """
    try:
        return bool(int(mac.split(":")[0], 16) & 0b10)
    except (ValueError, IndexError):
        return False


# --- vendor lookup ---------------------------------------------------------

_oui_cache: dict[str, str] | None = None
_oui_lock = threading.Lock()


def _load_oui() -> dict[str, str]:
    global _oui_cache
    with _oui_lock:
        if _oui_cache is not None:
            return _oui_cache
        table: dict[str, str] = {}
        for candidate in OUI_PATHS:
            path = Path(candidate)
            if not path.is_file():
                continue
            try:
                table = _parse_oui(path)
            except OSError:
                continue
            if table:
                log.info("loaded %d OUI prefixes from %s", len(table), path)
                break
        _oui_cache = table
        return table


def _parse_oui(path: Path) -> dict[str, str]:
    table: dict[str, str] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            # IEEE format:  "5C-FC-E1   (hex)\t\tResideo"
            if "(hex)" in line:
                prefix, _, vendor = line.partition("(hex)")
                key = prefix.strip().lower().replace("-", "")
                name = vendor.strip()
                if len(key) == 6 and name:
                    table[key] = name
                continue
            # nmap format:  "5CFCE1 Resideo"
            parts = line.split(None, 1)
            if len(parts) == 2 and len(parts[0]) == 6:
                key = parts[0].strip().lower()
                if all(c in "0123456789abcdef" for c in key):
                    table.setdefault(key, parts[1].strip())
    return table


def vendor_for(mac: str | None) -> str | None:
    """Manufacturer for a MAC, from the local registry. Never hits the network."""
    mac = normalise_mac(mac)
    if not mac:
        return None
    if is_randomised(mac):
        return None          # a randomised address has no real vendor
    return _load_oui().get(mac.replace(":", "")[:6])


# --- the local network -----------------------------------------------------

def local_ipv4() -> str | None:
    """This host's LAN address, via the routing table. Sends nothing."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 53))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def local_network(prefix_len: int = 24) -> ipaddress.IPv4Network | None:
    address = local_ipv4()
    if not address:
        return None
    try:
        return ipaddress.ip_network(f"{address}/{prefix_len}", strict=False)
    except ValueError:
        return None


def arp_table() -> dict[str, str]:
    """Current IP -> MAC map from the kernel's ARP cache.

    Only holds recently-contacted hosts, which is why a sweep has to come
    first: the sweep is what puts entries in here.
    """
    table: dict[str, str] = {}
    try:
        lines = Path("/proc/net/arp").read_text().splitlines()[1:]
    except OSError:
        return table
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        mac = normalise_mac(parts[3])
        if mac:
            table[parts[0]] = mac
    return table


def sweep(network: ipaddress.IPv4Network | None = None,
          settle: float = 2.0) -> dict[str, str]:
    """Poke every address on the subnet, then read the ARP cache.

    One UDP datagram per host to a port nothing listens on; the reply does not
    matter, only that the kernel had to resolve the address to send it.
    """
    network = network or local_network()
    if network is None:
        return {}
    mine = local_ipv4()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        for host in network.hosts():
            address = str(host)
            if address == mine:
                continue
            try:
                sock.sendto(b"\x00", (address, PROBE_PORT))
            except OSError:
                # No route or the ARP queue is full; the next pass picks it up.
                continue
            # Paced so a few hundred sends do not outrun the ARP queue.
            time.sleep(0.002)
    finally:
        sock.close()
    # ARP replies come back asynchronously, so give the cache a moment.
    time.sleep(settle)
    return arp_table()


def find_by_mac(mac: str, *, network: ipaddress.IPv4Network | None = None
                ) -> str | None:
    """Current address of a known MAC, or None.

    Checks the ARP cache first — if the device has been talked to recently that
    is instant — and only sweeps when it has to.
    """
    wanted = normalise_mac(mac)
    if not wanted:
        return None

    def lookup(table: dict[str, str]) -> str | None:
        for address, found in table.items():
            if found == wanted:
                return address
        return None

    # Cache first, and RETURN on a hit rather than evaluating both sources.
    # Writing this as `for table in (arp_table(), sweep())` builds the tuple
    # eagerly, so the sweep runs every time and the cache saves nothing — a
    # recently-seen device would take thirty seconds to re-find instead of
    # being instant.
    hit = lookup(arp_table())
    if hit:
        return hit
    return lookup(sweep(network))


def mac_for(address: str, *, allow_sweep: bool = True) -> str | None:
    """The MAC currently at an address, so a device can learn its own identity
    the first time it is reached successfully."""
    mac = arp_table().get(address)
    if mac or not allow_sweep:
        return mac
    # Not in the cache: touch it, which is enough to populate the entry.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(b"\x00", (address, PROBE_PORT))
    except OSError:
        return None
    finally:
        sock.close()
    time.sleep(0.4)
    return arp_table().get(address)


# --- inventory -------------------------------------------------------------

def inventory(known: dict[str, dict[str, Any]] | None = None,
              network: ipaddress.IPv4Network | None = None) -> list[dict[str, Any]]:
    """Everything answering on the LAN, named where possible.

    `known` maps MAC -> {"kind": ..., "name": ...} for the things Sentry already
    manages, so the list separates "this is your driveway camera" from "no idea
    what this is".
    """
    known = known or {}
    found = sweep(network)
    rows = []
    for address, mac in sorted(found.items(), key=_address_key):
        entry = known.get(mac)
        rows.append({
            "address": address,
            "mac": mac,
            "vendor": vendor_for(mac),
            "randomised": is_randomised(mac),
            "known_kind": entry.get("kind") if entry else None,
            "known_name": entry.get("name") if entry else None,
        })
    return rows


def _address_key(item: tuple[str, str]) -> tuple:
    try:
        return (0, ipaddress.ip_address(item[0]))
    except ValueError:
        return (1, item[0])


def resolve_moved(stored_host: str, mac: str | None,
                  reachable: Any) -> tuple[str | None, bool]:
    """Find where a device went, after `stored_host` stopped answering.

    Returns (address, moved). Only ever returns an address whose MAC matches
    exactly — guessing here would mean commanding somebody else's hardware.
    """
    if not mac:
        return None, False
    address = find_by_mac(mac)
    if not address or address == stored_host:
        return None, False
    if reachable is not None and not reachable(address):
        # It answers ARP but not us; treat it as still lost rather than
        # rewriting the stored address to something that does not work.
        return None, False
    log.info("device %s moved from %s to %s", mac, stored_host, address)
    return address, True
