"""Finding devices by MAC, and recovering when one moves.

The dangerous failure here is not "did not find it" — it is "found the wrong
one". A device that answers to somebody else's hardware is far worse than one
that stays broken until a human looks, so most of these tests are about
refusing to guess.
"""

import ipaddress

import pytest

from nvr import netscan


# --- MAC handling ----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("5c:fc:e1:23:82:d4", "5c:fc:e1:23:82:d4"),
    ("5C:FC:E1:23:82:D4", "5c:fc:e1:23:82:d4"),
    ("5c-fc-e1-23-82-d4", "5c:fc:e1:23:82:d4"),
    ("5CFCE12382D4", "5c:fc:e1:23:82:d4"),
    ("  5c:fc:e1:23:82:d4  ", "5c:fc:e1:23:82:d4"),
])
def test_macs_normalise_to_one_spelling(raw, expected):
    assert netscan.normalise_mac(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "not a mac", "5c:fc:e1", "5c:fc:e1:23:82:d4:99", 12345,
    "00:00:00:00:00:00",     # an unresolved ARP entry, not a device
    "zz:fc:e1:23:82:d4",
])
def test_junk_is_not_a_mac(raw):
    assert netscan.normalise_mac(raw) is None


@pytest.mark.parametrize("mac,randomised", [
    ("34:98:b5:9e:77:13", False),   # NETGEAR, a real NIC
    ("ec:71:db:2c:0c:44", False),   # Reolink
    ("3e:5d:fa:b5:0a:6d", True),    # locally administered
    ("c6:60:08:18:3d:73", True),
    ("46:32:c1:e5:1d:41", True),
    ("72:7d:1f:44:b6:69", True),
])
def test_randomised_addresses_are_recognised(mac, randomised):
    """Phones rotate these per network, so they are noise in an inventory of
    things worth controlling."""
    assert netscan.is_randomised(mac) is randomised


def test_a_randomised_address_reports_no_vendor():
    assert netscan.vendor_for("3e:5d:fa:b5:0a:6d") is None


def test_vendor_lookup_is_offline_and_works(monkeypatch):
    monkeypatch.setattr(netscan, "_oui_cache", {"5cfce1": "Resideo"})
    assert netscan.vendor_for("5c:fc:e1:23:82:d4") == "Resideo"
    assert netscan.vendor_for("aa:bb:cc:00:00:01") is None


def test_vendor_lookup_survives_a_missing_registry(monkeypatch):
    monkeypatch.setattr(netscan, "_oui_cache", {})
    assert netscan.vendor_for("5c:fc:e1:23:82:d4") is None


# --- the ARP table ---------------------------------------------------------

ARP_SAMPLE = """IP address       HW type     Flags       HW address            Mask     Device
192.168.1.21     0x1         0x2         dc:71:96:6a:97:08     *        wlan0
192.168.1.4      0x1         0x0         00:00:00:00:00:00     *        wlan0
192.168.1.25     0x1         0x2         78:EE:4C:DD:CF:10     *        wlan0
malformed line
"""


@pytest.fixture
def arp(monkeypatch, tmp_path):
    path = tmp_path / "arp"
    path.write_text(ARP_SAMPLE)
    import nvr.netscan as module
    real_path = module.Path

    class FakePath:
        def __new__(cls, value):
            return real_path(path) if value == "/proc/net/arp" else real_path(value)

    monkeypatch.setattr(module, "Path", FakePath)
    return path


def test_the_arp_table_is_parsed(arp):
    table = netscan.arp_table()
    assert table["192.168.1.21"] == "dc:71:96:6a:97:08"
    # Case is normalised so a comparison never fails on spelling.
    assert table["192.168.1.25"] == "78:ee:4c:dd:cf:10"


def test_unresolved_and_malformed_entries_are_dropped(arp):
    table = netscan.arp_table()
    assert "192.168.1.4" not in table       # all-zero MAC is not a device
    assert len(table) == 2


def test_a_missing_arp_file_is_not_fatal(monkeypatch, tmp_path):
    """Not every system exposes /proc/net/arp; that must degrade, not crash."""
    import nvr.netscan as module
    real_path = module.Path
    missing = tmp_path / "no-such-arp"

    class FakePath:
        def __new__(cls, value):
            return real_path(missing) if value == "/proc/net/arp" else real_path(value)

    monkeypatch.setattr(module, "Path", FakePath)
    assert netscan.arp_table() == {}


# --- finding, and refusing to guess ----------------------------------------

def test_find_by_mac_uses_the_cache_before_sweeping(monkeypatch, arp):
    swept = []
    monkeypatch.setattr(netscan, "sweep", lambda *a, **k: swept.append(1) or {})
    assert netscan.find_by_mac("78:ee:4c:dd:cf:10") == "192.168.1.25"
    assert not swept, "swept when the answer was already in the cache"


def test_find_by_mac_sweeps_when_it_has_to(monkeypatch, arp):
    monkeypatch.setattr(netscan, "sweep",
                        lambda *a, **k: {"192.168.1.99": "aa:bb:cc:dd:ee:ff"})
    assert netscan.find_by_mac("aa:bb:cc:dd:ee:ff") == "192.168.1.99"


def test_find_by_mac_gives_up_rather_than_guessing(monkeypatch, arp):
    monkeypatch.setattr(netscan, "sweep", lambda *a, **k: {})
    assert netscan.find_by_mac("aa:bb:cc:dd:ee:ff") is None


def test_find_by_mac_needs_a_real_mac(monkeypatch):
    monkeypatch.setattr(netscan, "sweep", lambda *a, **k: {})
    assert netscan.find_by_mac("nonsense") is None
    assert netscan.find_by_mac(None) is None


# --- resolve_moved ---------------------------------------------------------

def test_a_moved_device_is_found(monkeypatch):
    monkeypatch.setattr(netscan, "find_by_mac", lambda mac, **k: "192.168.1.31")
    address, moved = netscan.resolve_moved("192.168.1.25", "aa:bb:cc:dd:ee:ff", None)
    assert (address, moved) == ("192.168.1.31", True)


def test_a_device_that_has_not_moved_reports_no_move(monkeypatch):
    monkeypatch.setattr(netscan, "find_by_mac", lambda mac, **k: "192.168.1.25")
    assert netscan.resolve_moved("192.168.1.25", "aa:bb:cc:dd:ee:ff", None) \
        == (None, False)


def test_without_a_known_mac_nothing_is_attempted(monkeypatch):
    """No stable identity means no safe way to search — so do not search."""
    called = []
    monkeypatch.setattr(netscan, "find_by_mac", lambda *a, **k: called.append(1))
    assert netscan.resolve_moved("192.168.1.25", None, None) == (None, False)
    assert not called


def test_an_address_that_answers_arp_but_not_us_is_not_adopted(monkeypatch):
    """Rewriting the stored address to something that does not actually work
    would turn a recoverable outage into a confusing one."""
    monkeypatch.setattr(netscan, "find_by_mac", lambda mac, **k: "192.168.1.31")
    address, moved = netscan.resolve_moved(
        "192.168.1.25", "aa:bb:cc:dd:ee:ff", reachable=lambda _addr: False
    )
    assert (address, moved) == (None, False)


def test_a_reachable_new_address_is_adopted(monkeypatch):
    monkeypatch.setattr(netscan, "find_by_mac", lambda mac, **k: "192.168.1.31")
    address, moved = netscan.resolve_moved(
        "192.168.1.25", "aa:bb:cc:dd:ee:ff", reachable=lambda _addr: True
    )
    assert (address, moved) == ("192.168.1.31", True)


# --- inventory -------------------------------------------------------------

def test_inventory_names_what_sentry_already_manages(monkeypatch):
    monkeypatch.setattr(netscan, "sweep", lambda *a, **k: {
        "192.168.1.53": "ec:71:db:2c:0c:44",
        "192.168.1.99": "aa:bb:cc:dd:ee:ff",
    })
    monkeypatch.setattr(netscan, "_oui_cache", {"ec71db": "Reolink"})
    rows = netscan.inventory({"ec:71:db:2c:0c:44": {"kind": "camera",
                                                    "name": "Driveway"}})
    by_address = {r["address"]: r for r in rows}
    assert by_address["192.168.1.53"]["known_name"] == "Driveway"
    assert by_address["192.168.1.53"]["vendor"] == "Reolink"
    assert by_address["192.168.1.99"]["known_kind"] is None


def test_inventory_sorts_numerically_not_lexically(monkeypatch):
    """.9 before .10, or the list reads as nonsense."""
    monkeypatch.setattr(netscan, "sweep", lambda *a, **k: {
        "192.168.1.10": "aa:bb:cc:00:00:01",
        "192.168.1.9": "aa:bb:cc:00:00:02",
        "192.168.1.100": "aa:bb:cc:00:00:03",
    })
    order = [r["address"] for r in netscan.inventory({})]
    assert order == ["192.168.1.9", "192.168.1.10", "192.168.1.100"]


def test_local_network_is_a_subnet():
    network = netscan.local_network()
    assert network is None or isinstance(network, ipaddress.IPv4Network)


# --- the app-level API -----------------------------------------------------

def test_the_network_page_is_admin_only(viewer_client, admin_client):
    assert viewer_client.get("/network", follow_redirects=False).status_code == 303
    assert admin_client.get("/network").status_code == 200


def test_the_scan_endpoint_is_admin_only(viewer_client):
    assert viewer_client.post("/api/network/scan").status_code == 403


def test_the_scan_names_devices_sentry_manages(
        admin_client, db, monkeypatch, app_module):
    from conftest import add_camera
    add_camera(db, "front", "Front Door")
    db.execute("UPDATE cameras SET mac = ? WHERE id = ?",
               ("ec:71:db:2c:0c:44", "front"))
    db.add_shade_hub(id="78ee4cddcf10", name="Shade hub", host="192.168.1.25")
    monkeypatch.setattr(app_module.netscan, "sweep", lambda *a, **k: {
        "192.168.1.53": "ec:71:db:2c:0c:44",
        "192.168.1.25": "78:ee:4c:dd:cf:10",
        # Deliberately NOT aa:bb:...— 0xaa has the locally-administered bit
        # set, so it would be classed as a randomised phone address and left
        # out of the unidentified count. This one looks like real hardware.
        "192.168.1.99": "ac:bb:cc:dd:ee:ff",
    })
    body = admin_client.post("/api/network/scan").json()
    named = {d["address"]: d["known_name"] for d in body["devices"]}
    assert named["192.168.1.53"] == "Front Door"
    # The hub is identifiable from its protocol id even before anyone has
    # reached it by address and learned a MAC the usual way.
    assert named["192.168.1.25"] == "Shade hub"
    assert named["192.168.1.99"] is None
    assert body["unknown"] == 1


def test_a_hub_that_stops_answering_is_re_found_by_mac(
        admin_client, db, monkeypatch, app_module):
    """The whole point: a DHCP lease change should not need a human."""
    db.add_shade_hub(id="hub1", name="Hub", host="192.168.1.25",
                     mac="aa:bb:cc:dd:ee:ff", token="T" * 16)
    from nvr import shades

    seen = []

    def device_list(host):
        seen.append(host)
        if host == "192.168.1.25":
            raise shades.ShadeError("no answer to GetDeviceList")
        return {"mac": "hub1", "token": "U" * 16, "devices": []}

    monkeypatch.setattr(app_module.shadelib, "device_list", device_list)
    monkeypatch.setattr(app_module.shadelib, "read_device",
                        lambda *a, **k: {"currentPosition": 0})
    monkeypatch.setattr(app_module.netscan, "resolve_moved",
                        lambda host, mac, reachable: ("192.168.1.31", True))

    r = admin_client.post("/api/blinds/hubs/hub1/refresh")
    assert r.status_code == 200, r.text
    assert seen == ["192.168.1.25", "192.168.1.31"]
    assert db.shade_hub("hub1")["host"] == "192.168.1.31"


def test_a_hub_with_no_known_mac_is_not_hunted_for(
        admin_client, db, monkeypatch, app_module):
    """No stable identity means no safe way to search."""
    db.add_shade_hub(id="hub1", name="Hub", host="192.168.1.25", token="T" * 16)
    from nvr import shades

    def boom(host):
        raise shades.ShadeError("no answer")

    monkeypatch.setattr(app_module.shadelib, "device_list", boom)
    hunted = []
    monkeypatch.setattr(app_module.netscan, "find_by_mac",
                        lambda *a, **k: hunted.append(1))
    assert admin_client.post("/api/blinds/hubs/hub1/refresh").status_code == 502
    assert not hunted
    assert db.shade_hub("hub1")["host"] == "192.168.1.25"


# --- saying what a device is -----------------------------------------------

def test_a_label_is_stored_against_the_mac(admin_client, db):
    r = admin_client.patch("/api/network/devices/fc:b9:7e:5f:fd:b5", json={
        "label": "Kitchen dishwasher", "kind": "dishwasher",
        "notes": "under the counter", "address": "192.168.1.10",
    })
    assert r.status_code == 200
    assert r.json()["icon"] == "🍽️"
    entry = db.lan_device("fc:b9:7e:5f:fd:b5")
    assert entry["label"] == "Kitchen dishwasher"
    assert entry["kind"] == "dishwasher"


def test_a_label_survives_the_device_changing_address(
        admin_client, db, monkeypatch, app_module):
    """The entire reason labels are keyed by MAC and not by address."""
    admin_client.patch("/api/network/devices/fc:b9:7e:5f:fd:b5",
                       json={"label": "Kitchen dishwasher", "kind": "dishwasher",
                             "address": "192.168.1.10"})
    monkeypatch.setattr(app_module.netscan, "sweep",
                        lambda *a, **k: {"192.168.1.77": "fc:b9:7e:5f:fd:b5"})
    body = admin_client.post("/api/network/scan").json()
    moved = body["devices"][0]
    assert moved["address"] == "192.168.1.77"
    assert moved["display"] == "Kitchen dishwasher"


def test_a_persons_label_beats_the_vendor_guess(
        admin_client, monkeypatch, app_module):
    """"GE Appliances" is a guess; only a person knows which appliance."""
    monkeypatch.setattr(app_module.netscan, "sweep",
                        lambda *a, **k: {"192.168.1.10": "fc:b9:7e:5f:fd:b5"})
    before = admin_client.post("/api/network/scan").json()["devices"][0]
    assert before["display"] == before["vendor"]
    admin_client.patch("/api/network/devices/fc:b9:7e:5f:fd:b5",
                       json={"label": "Kitchen dishwasher"})
    after = admin_client.post("/api/network/scan").json()["devices"][0]
    assert after["display"] == "Kitchen dishwasher"


def test_the_mac_is_normalised_before_storing(admin_client, db):
    """So FC-B9-7E... and fc:b9:7e... are the same device, not two."""
    admin_client.patch("/api/network/devices/FC-B9-7E-5F-FD-B5",
                       json={"label": "Stove"})
    admin_client.patch("/api/network/devices/fc:b9:7e:5f:fd:b5",
                       json={"notes": "double oven"})
    assert len(db.lan_devices()) == 1
    entry = db.lan_device("fc:b9:7e:5f:fd:b5")
    assert entry["label"] == "Stove" and entry["notes"] == "double oven"


def test_an_unknown_kind_is_refused(admin_client):
    r = admin_client.patch("/api/network/devices/fc:b9:7e:5f:fd:b5",
                           json={"kind": "teleporter"})
    assert r.status_code == 400


def test_a_bad_mac_is_refused(admin_client):
    assert admin_client.patch("/api/network/devices/not-a-mac",
                              json={"label": "x"}).status_code == 400


def test_labelling_something_switched_off_still_works(admin_client, db):
    """A device can be unplugged when you happen to be labelling things."""
    r = admin_client.patch("/api/network/devices/fc:b9:7e:4d:41:a2",
                           json={"label": "Stove", "kind": "oven"})
    assert r.status_code == 200
    assert db.lan_device("fc:b9:7e:4d:41:a2")["label"] == "Stove"


def test_identifying_a_device_removes_it_from_the_unknown_count(
        admin_client, monkeypatch, app_module):
    monkeypatch.setattr(app_module.netscan, "sweep",
                        lambda *a, **k: {"192.168.1.10": "fc:b9:7e:5f:fd:b5"})
    assert admin_client.post("/api/network/scan").json()["unknown"] == 1
    admin_client.patch("/api/network/devices/fc:b9:7e:5f:fd:b5",
                       json={"kind": "dishwasher"})
    assert admin_client.post("/api/network/scan").json()["unknown"] == 0


def test_dismissing_clears_it_without_naming_it(
        admin_client, monkeypatch, app_module):
    monkeypatch.setattr(app_module.netscan, "sweep",
                        lambda *a, **k: {"192.168.1.10": "fc:b9:7e:5f:fd:b5"})
    admin_client.patch("/api/network/devices/fc:b9:7e:5f:fd:b5",
                       json={"dismissed": True})
    assert admin_client.post("/api/network/scan").json()["unknown"] == 0


def test_first_seen_is_not_moved_by_later_sightings(
        admin_client, db, monkeypatch, app_module):
    """A genuinely new arrival should read as new; if first_seen kept being
    refreshed, nothing would ever look new."""
    monkeypatch.setattr(app_module.netscan, "sweep",
                        lambda *a, **k: {"192.168.1.10": "fc:b9:7e:5f:fd:b5"})
    admin_client.post("/api/network/scan")
    first = db.lan_device("fc:b9:7e:5f:fd:b5")["first_seen"]
    admin_client.post("/api/network/scan")
    entry = db.lan_device("fc:b9:7e:5f:fd:b5")
    assert entry["first_seen"] == first
    assert entry["last_seen"] >= first


def test_the_known_list_is_available_without_scanning(
        admin_client, monkeypatch, app_module):
    """So the page shows something immediately instead of a blank panel and a
    thirty-second wait."""
    monkeypatch.setattr(app_module.netscan, "sweep",
                        lambda *a, **k: {"192.168.1.10": "fc:b9:7e:5f:fd:b5"})
    admin_client.post("/api/network/scan")
    swept = []
    monkeypatch.setattr(app_module.netscan, "sweep",
                        lambda *a, **k: swept.append(1) or {})
    body = admin_client.get("/api/network/devices").json()
    assert not swept
    assert [d["mac"] for d in body["devices"]] == ["fc:b9:7e:5f:fd:b5"]
    assert body["stale"] is True


def test_forgetting_a_device(admin_client, db):
    admin_client.patch("/api/network/devices/fc:b9:7e:5f:fd:b5",
                       json={"label": "Old thing"})
    assert admin_client.delete(
        "/api/network/devices/fc:b9:7e:5f:fd:b5").status_code == 200
    assert db.lan_devices() == []


def test_annotation_is_admin_only(viewer_client):
    assert viewer_client.patch("/api/network/devices/fc:b9:7e:5f:fd:b5",
                               json={"label": "x"}).status_code == 403
    assert viewer_client.get("/api/network/devices").status_code == 403


def test_the_kind_catalogue_covers_the_appliances(admin_client):
    values = {k["value"] for k in
              admin_client.get("/api/network/devices").json()["kinds"]}
    for expected in ("dishwasher", "oven", "microwave", "fridge", "washer",
                     "vacuum", "thermostat", "camera", "blinds", "plug"):
        assert expected in values, expected


def test_every_kind_has_an_icon():
    from nvr import netscan as ns
    for value, label, icon in ns.DEVICE_KINDS:
        assert value and label and icon, value
    assert len(ns.KIND_VALUES) == len(ns.DEVICE_KINDS), "duplicate kind value"
