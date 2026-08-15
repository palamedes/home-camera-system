"""Motorized window coverings on a Connector / Motionblinds bridge.

The bridge (Dooya/Wintec DD7006 "Pro Hub" and its many rebadges — Connector+,
Motionblinds, Brel, SHC Budget Blinds) is a 433.92 MHz transmitter with a
network front end. It speaks JSON over UDP on the LAN, which is the whole
reason it is worth supporting: no cloud, no vendor account, same posture as the
cameras.

Wire protocol
-------------
* Commands go to the hub on port 32100, unicast or to multicast 238.0.0.18.
* The hub pushes unsolicited state changes and a 30-60s heartbeat on
  238.0.0.18:32101, which is how we learn that somebody used the wall remote.
* Three messages matter: GetDeviceList (enumerate), ReadDevice (poll one),
  WriteDevice (move one).

Authentication is the interesting part. GetDeviceList and ReadDevice are
unauthenticated — we can discover the hub and read every motor's position,
battery and signal with no credential at all. Only WriteDevice carries an
AccessToken, derived by AES-128-ECB encrypting the 16-character token the hub
hands out, keyed by the 16-character key from the vendor app.

Positions run 0 = fully open to 100 = fully closed, which is the protocol's own
convention and deliberately not flipped here — the UI does the translating, so
what goes on the wire always matches the vendor documentation.

There is no AES in the standard library and this needs exactly one block
encrypt, so `_aes_encrypt_block` below is a self-contained AES-128. It is
pinned to the FIPS-197 known-answer vector in the tests; hand-rolled crypto
without a known-answer test is how you get something that looks like it works.
"""

from __future__ import annotations

import json
import logging
import socket
import struct
import threading
import time
from typing import Any

log = logging.getLogger("nvr.shades")

MULTICAST_GROUP = "238.0.0.18"
COMMAND_PORT = 32100
PUSH_PORT = 32101

# The hub answers in well under a second on a healthy link; the generous margin
# is for the 433 MHz hop out to a motor, which is the slow part.
TIMEOUT = 5.0

# Protocol operation codes for WriteDevice.
OP_CLOSE = 0
OP_OPEN = 1
OP_STOP = 2

# deviceType of the bridge itself, as opposed to a motor hanging off it.
HUB_DEVICE_TYPE = "02000001"

# wirelessMode values that mean "this motor reports its real position back".
# 0 is transmit-only, so its position can only ever be dead reckoned.
BIDIRECTIONAL_MODES = (1, 2)


class ShadeError(RuntimeError):
    """A hub could not be reached, or refused a command."""


# ---------------------------------------------------------------------------
# AES-128, single block, encrypt only. See module docstring.
# ---------------------------------------------------------------------------

_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76"
    "ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d83115"
    "04c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f84"
    "53d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa8"
    "51a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d1973"
    "60814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479"
    "e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a"
    "703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df"
    "8ca1890dbfe6426841992d0fb054bb16"
)

_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(byte: int) -> int:
    """Multiply by x in GF(2^8), reducing by the AES polynomial."""
    byte <<= 1
    if byte & 0x100:
        byte ^= 0x11B
    return byte & 0xFF


def _expand_key(key: bytes) -> list[bytes]:
    """AES-128 key schedule: 11 round keys of 16 bytes."""
    words = [bytearray(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        temp = bytearray(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]                    # RotWord
            temp = bytearray(_SBOX[b] for b in temp)      # SubWord
            temp[0] ^= _RCON[i // 4 - 1]
        words.append(bytearray(a ^ b for a, b in zip(words[i - 4], temp)))
    return [bytes(b for word in words[r * 4:r * 4 + 4] for b in word)
            for r in range(11)]


def _aes_encrypt_block(key: bytes, block: bytes) -> bytes:
    """Encrypt exactly one 16-byte block with AES-128. No mode, no padding."""
    if len(key) != 16 or len(block) != 16:
        raise ValueError("AES-128 needs a 16-byte key and a 16-byte block")
    round_keys = _expand_key(key)
    state = bytearray(a ^ b for a, b in zip(block, round_keys[0]))

    for rnd in range(1, 11):
        state = bytearray(_SBOX[b] for b in state)                  # SubBytes
        # ShiftRows: state is column-major, so byte (row r, col c) is r + 4c.
        shifted = bytearray(16)
        for r in range(4):
            for c in range(4):
                shifted[r + 4 * c] = state[r + 4 * ((c + r) % 4)]
        state = shifted
        if rnd != 10:                                               # MixColumns
            mixed = bytearray(16)
            for c in range(4):
                s0, s1, s2, s3 = state[4 * c:4 * c + 4]
                t = s0 ^ s1 ^ s2 ^ s3
                mixed[4 * c + 0] = s0 ^ t ^ _xtime(s0 ^ s1)
                mixed[4 * c + 1] = s1 ^ t ^ _xtime(s1 ^ s2)
                mixed[4 * c + 2] = s2 ^ t ^ _xtime(s2 ^ s3)
                mixed[4 * c + 3] = s3 ^ t ^ _xtime(s3 ^ s0)
            state = mixed
        state = bytearray(a ^ b for a, b in zip(state, round_keys[rnd]))
    return bytes(state)


def access_token(api_key: str, hub_token: str) -> str:
    """Derive the AccessToken a WriteDevice must carry.

    `api_key` is the 16-character key from the vendor app (dashes included —
    they are part of the 16 characters, which is why stripping them yields the
    "truncated access code" complaints seen in the wild). `hub_token` is the
    16-character token from the most recent GetDeviceListAck; it changes when
    the hub restarts, so it is never cached for long.
    """
    key = (api_key or "").encode()
    token = (hub_token or "").encode()
    if len(key) != 16:
        raise ShadeError(
            f"the hub key must be exactly 16 characters (got {len(key)}); "
            "keep the dashes, e.g. 12ab345c-d67e-8f"
        )
    if len(token) != 16:
        raise ShadeError(f"hub token must be 16 characters (got {len(token)})")
    return _aes_encrypt_block(key, token).hex().upper()


# ---------------------------------------------------------------------------
# UDP transport
# ---------------------------------------------------------------------------

def _msg_id() -> str:
    """The protocol wants yyyyMMddHHmmssSSS. Only uniqueness actually matters."""
    now = time.time()
    return time.strftime("%Y%m%d%H%M%S", time.localtime(now)) + f"{int(now % 1 * 1000):03d}"


class _Transport:
    """One long-lived socket bound to the command port, serialised by a lock.

    Bound to 32100 rather than an ephemeral port on purpose: the hub replies to
    whatever source port asked, so a socket that listens on 32100 while sending
    from an ephemeral port never sees a single unicast answer.
    """

    def __init__(self, bind_port: int | None = None) -> None:
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        # The port we listen on, which is separate from the port we send to.
        # They are the same number in production; keeping them distinct lets a
        # test talk to a fake hub on an ephemeral port without trying to bind
        # the port that fake hub already owns.
        self.bind_port = COMMAND_PORT if bind_port is None else bind_port

    def _socket(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("", self.bind_port))
        except OSError as exc:
            sock.close()
            raise ShadeError(
                f"could not bind UDP {self.bind_port}: {exc}"
            ) from exc
        try:
            sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                struct.pack("4s4s", socket.inet_aton(MULTICAST_GROUP),
                            socket.inet_aton("0.0.0.0")),
            )
        except OSError:
            # Multicast is a bonus; unicast to a known host is the main path.
            log.debug("could not join %s", MULTICAST_GROUP, exc_info=True)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        self._sock = sock
        return sock

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                self._sock.close()
                self._sock = None

    def request(self, host: str, payload: dict[str, Any], *,
                expect: str, mac: str | None = None,
                timeout: float = TIMEOUT,
                port: int | None = None) -> dict[str, Any]:
        """Send one message and wait for the matching ack.

        Late acks from an earlier, timed-out call are still queued on the
        socket, so replies are matched on msgType and mac rather than simply
        taking the next packet — otherwise a slow motor poisons every
        subsequent read with stale data.
        """
        payload = {**payload, "msgID": _msg_id()}
        blob = json.dumps(payload).encode()
        with self._lock:
            sock = self._socket()
            self._drain(sock)
            try:
                sock.sendto(blob, (host, port or COMMAND_PORT))
            except OSError as exc:
                raise ShadeError(f"{host}: {exc}") from exc
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ShadeError(f"{host}: no answer to {payload['msgType']}")
                sock.settimeout(remaining)
                try:
                    data, _addr = sock.recvfrom(65535)
                except socket.timeout:
                    raise ShadeError(
                        f"{host}: no answer to {payload['msgType']}"
                    ) from None
                except OSError as exc:
                    raise ShadeError(f"{host}: {exc}") from exc
                try:
                    message = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("msgType") != expect:
                    continue
                if mac is not None and message.get("mac") != mac:
                    continue
                return message

    def broadcast(self, payload: dict[str, Any], *, expect: str,
                  targets: list[str], collect: float = 3.0) -> list[dict[str, Any]]:
        """Fire a message at several addresses and gather everything that answers."""
        payload = {**payload, "msgID": _msg_id()}
        blob = json.dumps(payload).encode()
        found: dict[str, dict[str, Any]] = {}
        with self._lock:
            sock = self._socket()
            self._drain(sock)
            for target in targets:
                try:
                    sock.sendto(blob, (target, COMMAND_PORT))
                except OSError:
                    # An unreachable address just means no ARP entry. Keep
                    # going: on a sweep most addresses are empty by definition.
                    continue
                # Paced so a few hundred sends do not outrun the ARP queue.
                time.sleep(0.003)
            deadline = time.monotonic() + collect
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    data, addr = sock.recvfrom(65535)
                except (socket.timeout, OSError):
                    break
                try:
                    message = json.loads(data)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("msgType") == expect:
                    message["_host"] = addr[0]
                    found[message.get("mac") or addr[0]] = message
        return list(found.values())

    @staticmethod
    def _drain(sock: socket.socket) -> None:
        """Throw away anything already queued, then restore blocking mode.

        The restore matters: draining puts the socket in non-blocking mode, and
        a non-blocking sendto fails with EAGAIN as soon as the kernel's ARP
        queue fills — which is exactly what a sweep across a mostly-empty /24
        does. Leaving it non-blocking silently drops the tail of every scan.
        """
        sock.settimeout(0)
        try:
            while True:
                try:
                    sock.recvfrom(65535)
                except (BlockingIOError, socket.timeout, OSError):
                    return
        finally:
            sock.settimeout(None)


_transport = _Transport()


# ---------------------------------------------------------------------------
# Protocol operations
# ---------------------------------------------------------------------------

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


def local_broadcast_targets(sweep: bool = True) -> list[str]:
    """Where to look for a hub.

    Multicast is the documented route and the subnet broadcast is the obvious
    fallback, but consumer wireless APs routinely filter BOTH to save airtime —
    measured on this network, where the hub answers unicast and ignores every
    broadcast form. So discovery also walks the local /24 one address at a
    time. It is 254 small datagrams on a button press, and it is the only
    method observed to work over WiFi.
    """
    targets = [MULTICAST_GROUP, "255.255.255.255"]
    local = local_ipv4()
    if not local:
        return targets
    octets = local.split(".")
    if len(octets) != 4:
        return targets
    prefix = ".".join(octets[:3])
    targets.append(f"{prefix}.255")
    if sweep:
        targets.extend(
            f"{prefix}.{host}" for host in range(1, 255)
            if f"{prefix}.{host}" != local
        )
    return targets


def discover(collect: float = 4.0, sweep: bool = True) -> list[dict[str, Any]]:
    """Find hubs on the LAN. Unauthenticated, and cannot move anything."""
    replies = _transport.broadcast(
        {"msgType": "GetDeviceList"}, expect="GetDeviceListAck",
        targets=local_broadcast_targets(sweep=sweep), collect=collect,
    )
    return [
        {
            "mac": r.get("mac"),
            "host": r.get("_host"),
            "device_type": r.get("deviceType"),
            "protocol": r.get("ProtocolVersion"),
            "token": r.get("token"),
            "devices": [d for d in r.get("data") or []
                        if d.get("deviceType") != HUB_DEVICE_TYPE],
        }
        for r in replies
    ]


def device_list(host: str) -> dict[str, Any]:
    """Enumerate one hub. Returns its token (needed for any later write)."""
    reply = _transport.request(
        host, {"msgType": "GetDeviceList"}, expect="GetDeviceListAck"
    )
    return {
        "mac": reply.get("mac"),
        "device_type": reply.get("deviceType"),
        "protocol": reply.get("ProtocolVersion"),
        "token": reply.get("token"),
        "devices": [d for d in reply.get("data") or []
                    if d.get("deviceType") != HUB_DEVICE_TYPE],
    }


def read_device(host: str, mac: str, device_type: str) -> dict[str, Any]:
    """Poll one motor. No AccessToken required."""
    reply = _transport.request(
        host,
        {"msgType": "ReadDevice", "mac": mac, "deviceType": device_type},
        expect="ReadDeviceAck", mac=mac,
    )
    return reply.get("data") or {}


def write_device(host: str, mac: str, device_type: str, data: dict[str, Any],
                 *, api_key: str | None, hub_token: str | None) -> dict[str, Any]:
    """Move one motor.

    When no key is configured the command is sent without an AccessToken. That
    is not wishful thinking: this firmware family is known to be inconsistent
    about enforcing it, and an unauthenticated write either works (in which
    case the vendor app is never needed again) or comes back refused, which is
    a clear, actionable error rather than a silent no-op.
    """
    message: dict[str, Any] = {
        "msgType": "WriteDevice", "mac": mac,
        "deviceType": device_type, "data": data,
    }
    if api_key:
        if not hub_token:
            raise ShadeError("no hub token yet — refresh the hub first")
        message["AccessToken"] = access_token(api_key, hub_token)
    reply = _transport.request(host, message, expect="WriteDeviceAck", mac=mac)
    actual = reply.get("actionResult")
    if actual:
        raise ShadeError(str(actual))
    return reply.get("data") or {}


def set_position(host: str, mac: str, device_type: str, position: int,
                 *, api_key: str | None, hub_token: str | None) -> dict[str, Any]:
    """Move to a position: 0 is fully open, 100 is fully closed."""
    position = max(0, min(100, int(position)))
    return write_device(host, mac, device_type, {"targetPosition": position},
                        api_key=api_key, hub_token=hub_token)


def operate(host: str, mac: str, device_type: str, action: str,
            *, api_key: str | None, hub_token: str | None) -> dict[str, Any]:
    """Run open / close / stop."""
    codes = {"open": OP_OPEN, "close": OP_CLOSE, "stop": OP_STOP}
    if action not in codes:
        raise ShadeError(f"unknown action {action!r}")
    return write_device(host, mac, device_type, {"operation": codes[action]},
                        api_key=api_key, hub_token=hub_token)


# ---------------------------------------------------------------------------
# Reading the telemetry
# ---------------------------------------------------------------------------

# The motors carry a 2-cell lithium pack: ~8.4 V charged, ~6.0 V flat. The
# protocol reports hundredths of a volt (784 -> 7.84 V). The percentage is an
# estimate off a linear curve and is labelled as such in the UI; the voltage is
# the number to trust.
BATTERY_FULL_MV = 840
BATTERY_EMPTY_MV = 600


def battery_percent(raw: Any) -> int | None:
    try:
        millivolts = int(raw)
    except (TypeError, ValueError):
        return None
    if millivolts <= 0:
        return None
    span = BATTERY_FULL_MV - BATTERY_EMPTY_MV
    pct = round((millivolts - BATTERY_EMPTY_MV) / span * 100)
    return max(0, min(100, pct))


def is_bidirectional(data: dict[str, Any]) -> bool:
    """Whether this motor reports real position, or can only be commanded."""
    try:
        return int(data.get("wirelessMode")) in BIDIRECTIONAL_MODES
    except (TypeError, ValueError):
        return False


def summarise(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten a ReadDeviceAck payload into the fields Sentry stores."""
    def as_int(key: str) -> int | None:
        try:
            return int(data[key])
        except (KeyError, TypeError, ValueError):
            return None

    return {
        "position": as_int("currentPosition"),
        "battery_mv": as_int("batteryLevel"),
        "battery_percent": battery_percent(data.get("batteryLevel")),
        "rssi": as_int("RSSI"),
        "bidirectional": is_bidirectional(data),
    }
