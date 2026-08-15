"""The Connector / Motionblinds wire protocol.

The AES here is hand-rolled (no crypto dependency, and the AccessToken needs
exactly one block), so it is pinned to the published FIPS known-answer vectors.
A cipher that is subtly wrong still produces 32 plausible hex characters, and
the only thing that would ever tell us is a hub refusing every write.
"""

import json
import socket
import threading

import pytest

from nvr import shades


# --- AES ------------------------------------------------------------------

@pytest.mark.parametrize("key,plain,cipher", [
    # FIPS-197 Appendix C.1
    ("000102030405060708090a0b0c0d0e0f",
     "00112233445566778899aabbccddeeff",
     "69c4e0d86a7b0430d8cdb78070b4c55a"),
    # NIST SP 800-38A F.1.1, ECB-AES128 blocks 1 and 2
    ("2b7e151628aed2a6abf7158809cf4f3c",
     "6bc1bee22e409f96e93d7e117393172a",
     "3ad77bb40d7a3660a89ecaf32466ef97"),
    ("2b7e151628aed2a6abf7158809cf4f3c",
     "ae2d8a571e03ac9c9eb76fac45af8e51",
     "f5d3d58503b9699de785895a96fdbaaf"),
])
def test_aes_matches_the_published_vectors(key, plain, cipher):
    got = shades._aes_encrypt_block(bytes.fromhex(key), bytes.fromhex(plain))
    assert got.hex() == cipher


def test_aes_rejects_wrong_sizes():
    with pytest.raises(ValueError):
        shades._aes_encrypt_block(b"short", b"0" * 16)
    with pytest.raises(ValueError):
        shades._aes_encrypt_block(b"0" * 16, b"short")


def test_access_token_is_hex_upper_and_stable():
    token = shades.access_token("12ab345c-d67e-8f", "0123456789ABCDEF")
    assert len(token) == 32
    assert token == token.upper()
    assert all(c in "0123456789ABCDEF" for c in token)
    # Same inputs, same answer — the hub would reject a moving target.
    assert token == shades.access_token("12ab345c-d67e-8f", "0123456789ABCDEF")


def test_a_different_key_gives_a_different_token():
    a = shades.access_token("12ab345c-d67e-8f", "0123456789ABCDEF")
    b = shades.access_token("12ab345c-d67e-80", "0123456789ABCDEF")
    assert a != b


def test_a_key_without_its_dashes_is_rejected_with_a_useful_message():
    """Stripping the dashes leaves 14 characters. This is THE common mistake,
    and silently padding it would produce a token the hub quietly refuses."""
    with pytest.raises(shades.ShadeError) as exc:
        shades.access_token("12ab345cd67e8f", "0123456789ABCDEF")
    assert "16 characters" in str(exc.value)
    assert "dashes" in str(exc.value)


def test_a_missing_token_is_rejected():
    with pytest.raises(shades.ShadeError):
        shades.access_token("12ab345c-d67e-8f", "")


# --- telemetry ------------------------------------------------------------

def test_bidirectional_detection():
    assert shades.is_bidirectional({"wirelessMode": 1})
    assert shades.is_bidirectional({"wirelessMode": 2})
    assert not shades.is_bidirectional({"wirelessMode": 0})
    assert not shades.is_bidirectional({})
    assert not shades.is_bidirectional({"wirelessMode": "nonsense"})


@pytest.mark.parametrize("raw,expected", [
    (840, 100),
    (600, 0),
    (720, 50),
    (900, 100),   # clamped, never 125%
    (100, 0),     # clamped, never negative
])
def test_battery_percent(raw, expected):
    assert shades.battery_percent(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "abc", 0, -5])
def test_battery_percent_survives_junk(raw):
    assert shades.battery_percent(raw) is None


def test_summarise_reads_a_real_payload():
    """This is a verbatim ReadDeviceAck from the DD7006 on the bench."""
    data = {
        "type": 1, "operation": 5, "currentPosition": 2, "targetPosition": 2,
        "currentAngle": 73, "targetAngle": 73, "currentState": 3, "state": 0,
        "voltageMode": 1, "batteryLevel": 784, "chargingState": 0,
        "wirelessMode": 1, "speedLevel": 0, "RSSI": -105,
    }
    assert shades.summarise(data) == {
        "position": 2, "battery_mv": 784, "battery_percent": 77,
        "rssi": -105, "bidirectional": True,
    }


def test_summarise_tolerates_a_sparse_payload():
    """A motor that answers with almost nothing must not crash the poller."""
    assert shades.summarise({}) == {
        "position": None, "battery_mv": None, "battery_percent": None,
        "rssi": None, "bidirectional": False,
    }


# --- transport ------------------------------------------------------------

class FakeHub:
    """A UDP hub on the loopback that answers whatever we tell it to."""

    def __init__(self, responder):
        self.responder = responder
        self.received = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            message = json.loads(data)
            self.received.append(message)
            reply = self.responder(message)
            if reply is not None:
                self.sock.sendto(json.dumps(reply).encode(), addr)

    def close(self):
        self._stop.set()
        self.thread.join(timeout=2)
        self.sock.close()


@pytest.fixture
def transport():
    """A transport listening on an ephemeral port, talking to a fake hub.

    In production it binds 32100 so the hub's unicast replies land somewhere it
    is listening. Binding that here would collide with a running Sentry, so the
    listen port floats and the destination port is passed per call.
    """
    t = shades._Transport(bind_port=0)
    yield t
    t.close()


def test_request_returns_the_matching_ack(transport):
    hub = FakeHub(lambda m: {"msgType": "GetDeviceListAck", "mac": "abc",
                             "token": "T" * 16, "data": []})
    try:
        monkey_port = hub.port
        reply = _request_to(transport, "127.0.0.1", monkey_port,
                            {"msgType": "GetDeviceList"}, "GetDeviceListAck")
        assert reply["mac"] == "abc"
        assert hub.received[0]["msgType"] == "GetDeviceList"
        # Every message carries an id, which is how acks are correlated.
        assert hub.received[0]["msgID"]
    finally:
        hub.close()


def test_request_ignores_a_stale_ack_for_another_device(transport):
    """A slow motor's late reply must not be handed back as this one's answer.

    Without mac matching, one timed-out poll poisons every later read with
    another shade's position — which would show the wrong number and, worse,
    could be written to the wrong row.
    """
    replies = [
        {"msgType": "ReadDeviceAck", "mac": "other", "data": {"currentPosition": 99}},
        {"msgType": "ReadDeviceAck", "mac": "wanted", "data": {"currentPosition": 7}},
    ]

    def responder(message):
        # Answer twice: the wrong device first, then the right one.
        return replies.pop(0) if replies else None

    hub = FakeHub(responder)
    try:
        # Two sends so the fake emits both replies; the second call must skip
        # the queued 'other' packet rather than returning it.
        _request_to(transport, "127.0.0.1", hub.port,
                    {"msgType": "ReadDevice", "mac": "other"},
                    "ReadDeviceAck", mac="other")
        reply = _request_to(transport, "127.0.0.1", hub.port,
                            {"msgType": "ReadDevice", "mac": "wanted"},
                            "ReadDeviceAck", mac="wanted")
        assert reply["data"]["currentPosition"] == 7
    finally:
        hub.close()


def test_a_silent_hub_raises_rather_than_hanging(transport):
    hub = FakeHub(lambda m: None)
    try:
        with pytest.raises(shades.ShadeError) as exc:
            _request_to(transport, "127.0.0.1", hub.port,
                        {"msgType": "GetDeviceList"}, "GetDeviceListAck",
                        timeout=0.4)
        assert "no answer" in str(exc.value)
    finally:
        hub.close()


def test_garbage_on_the_wire_is_skipped_not_parsed(transport):
    """Something else on the LAN using this port must not crash a poll."""
    sent = []

    def responder(message):
        sent.append(message)
        return None

    hub = FakeHub(responder)
    try:
        # Blast a non-JSON datagram at our socket first.
        noise = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock = transport._socket()
        noise.sendto(b"not json at all", sock.getsockname())
        noise.close()
        with pytest.raises(shades.ShadeError):
            _request_to(transport, "127.0.0.1", hub.port,
                        {"msgType": "GetDeviceList"}, "GetDeviceListAck",
                        timeout=0.4)
    finally:
        hub.close()


def _request_to(transport, host, port, payload, expect, mac=None, timeout=3.0):
    """Point the transport at a fake hub's ephemeral port."""
    return transport.request(host, payload, expect=expect, mac=mac,
                             timeout=timeout, port=port)


# --- command construction -------------------------------------------------

def test_write_device_without_a_key_omits_the_access_token(monkeypatch):
    """No key configured means we still try — this firmware family is known to
    be inconsistent about enforcing the token, and a refusal is a clear error
    rather than a silent no-op."""
    seen = {}

    def fake_request(host, payload, *, expect, mac=None, timeout=shades.TIMEOUT):
        seen.update(payload)
        return {"msgType": "WriteDeviceAck", "mac": mac, "data": {}}

    monkeypatch.setattr(shades._transport, "request", fake_request)
    shades.set_position("10.0.0.1", "aa01", "10000000", 40,
                        api_key=None, hub_token="T" * 16)
    assert "AccessToken" not in seen
    assert seen["data"] == {"targetPosition": 40}


def test_write_device_with_a_key_carries_the_access_token(monkeypatch):
    seen = {}

    def fake_request(host, payload, *, expect, mac=None, timeout=shades.TIMEOUT):
        seen.update(payload)
        return {"msgType": "WriteDeviceAck", "mac": mac, "data": {}}

    monkeypatch.setattr(shades._transport, "request", fake_request)
    shades.set_position("10.0.0.1", "aa01", "10000000", 40,
                        api_key="12ab345c-d67e-8f", hub_token="0123456789ABCDEF")
    assert seen["AccessToken"] == shades.access_token(
        "12ab345c-d67e-8f", "0123456789ABCDEF"
    )


def test_a_key_with_no_hub_token_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(shades._transport, "request",
                        lambda *a, **k: {"msgType": "WriteDeviceAck"})
    with pytest.raises(shades.ShadeError) as exc:
        shades.set_position("10.0.0.1", "aa01", "10000000", 40,
                            api_key="12ab345c-d67e-8f", hub_token=None)
    assert "token" in str(exc.value)


@pytest.mark.parametrize("position,expected", [
    (-10, 0), (0, 0), (55, 55), (100, 100), (250, 100),
])
def test_positions_are_clamped(monkeypatch, position, expected):
    seen = {}
    monkeypatch.setattr(
        shades._transport, "request",
        lambda host, payload, **k: (seen.update(payload),
                                    {"msgType": "WriteDeviceAck", "data": {}})[1],
    )
    shades.set_position("10.0.0.1", "aa01", "10000000", position,
                        api_key=None, hub_token=None)
    assert seen["data"]["targetPosition"] == expected


@pytest.mark.parametrize("action,code", [("close", 0), ("open", 1), ("stop", 2)])
def test_operate_sends_the_documented_codes(monkeypatch, action, code):
    seen = {}
    monkeypatch.setattr(
        shades._transport, "request",
        lambda host, payload, **k: (seen.update(payload),
                                    {"msgType": "WriteDeviceAck", "data": {}})[1],
    )
    shades.operate("10.0.0.1", "aa01", "10000000", action,
                   api_key=None, hub_token=None)
    assert seen["data"] == {"operation": code}


def test_an_unknown_action_is_refused(monkeypatch):
    with pytest.raises(shades.ShadeError):
        shades.operate("10.0.0.1", "aa01", "10000000", "wiggle",
                       api_key=None, hub_token=None)


def test_a_refused_write_becomes_an_error(monkeypatch):
    """The hub reports refusal in actionResult while still returning an ack —
    treating that as success is how a UI ends up claiming a shade moved."""
    monkeypatch.setattr(
        shades._transport, "request",
        lambda *a, **k: {"msgType": "WriteDeviceAck", "mac": "aa01",
                         "actionResult": "AccessToken error"},
    )
    with pytest.raises(shades.ShadeError) as exc:
        shades.set_position("10.0.0.1", "aa01", "10000000", 40,
                            api_key=None, hub_token=None)
    assert "AccessToken error" in str(exc.value)


def test_device_list_drops_the_hub_from_its_own_device_list(monkeypatch):
    """The bridge lists itself alongside the motors; treating it as a covering
    would create a phantom shade that never answers."""
    monkeypatch.setattr(
        shades._transport, "request",
        lambda *a, **k: {
            "msgType": "GetDeviceListAck", "mac": "aabbccddeeff",
            "deviceType": "02000001", "ProtocolVersion": "0.9",
            "token": "0123456789ABCDEF",
            "data": [
                {"mac": "aabbccddeeff", "deviceType": "02000001"},
                {"mac": "aabbccddeeff0001", "deviceType": "10000000"},
                {"mac": "aabbccddeeff0002", "deviceType": "10000000"},
            ],
        },
    )
    info = shades.device_list("192.168.1.50")
    assert info["token"] == "0123456789ABCDEF"
    assert [d["mac"] for d in info["devices"]] == [
        "aabbccddeeff0001", "aabbccddeeff0002"
    ]


def test_discovery_targets_include_multicast_and_a_subnet_broadcast():
    targets = shades.local_broadcast_targets()
    assert shades.MULTICAST_GROUP in targets
    assert any(t.endswith(".255") for t in targets)


def test_discovery_sweeps_the_subnet_by_unicast():
    """Measured on the real network: this hub answers unicast and ignores
    multicast AND every broadcast form, because the AP filters them. Without
    the sweep, discovery finds nothing over WiFi."""
    targets = shades.local_broadcast_targets(sweep=True)
    unicast = [t for t in targets
               if not t.endswith(".255") and t != shades.MULTICAST_GROUP]
    assert len(unicast) > 200


def test_the_sweep_skips_our_own_address():
    local = shades.local_ipv4()
    if local is None:
        pytest.skip("no routable address on this host")
    assert local not in shades.local_broadcast_targets(sweep=True)


def test_the_sweep_can_be_turned_off():
    assert len(shades.local_broadcast_targets(sweep=False)) <= 3


def test_draining_leaves_the_socket_blocking(transport):
    """Draining switches the socket non-blocking; failing to switch it back
    makes sendto fail with EAGAIN partway through a 254-address sweep, so the
    tail of every scan vanishes silently."""
    sock = transport._socket()
    transport._drain(sock)
    assert sock.gettimeout() is None
