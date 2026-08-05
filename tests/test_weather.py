"""Weather + river-level service: parsing, trend, resilience, endpoint.

The two upstream APIs (Open-Meteo, NWS/NWPS) are stubbed at nvr.weather.httpx
so nothing touches the network. We assert we shape their responses correctly,
compute the water trend, keep the last good value when a feed blips, and expose
it all through /api/weather.
"""

from __future__ import annotations

import pytest

from nvr import weather as weather_mod
from nvr.config import WeatherConfig
from nvr.weather import WeatherService


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


_OPEN_METEO = {
    "current_units": {
        "temperature_2m": "°F", "relative_humidity_2m": "%",
        "apparent_temperature": "°F", "dew_point_2m": "°F",
        "precipitation": "inch", "surface_pressure": "hPa",
        "wind_speed_10m": "mp/h", "wind_gusts_10m": "mp/h", "uv_index": "",
    },
    "current": {
        "temperature_2m": 81.3, "relative_humidity_2m": 74,
        "apparent_temperature": 88.0, "dew_point_2m": 71.5,
        "precipitation": 0.0, "weather_code": 2, "surface_pressure": 1015,
        "wind_speed_10m": 9.4, "wind_direction_10m": 45,
        "wind_gusts_10m": 15.0, "uv_index": 6.2, "is_day": 1,
    },
    "daily": {
        "temperature_2m_max": [90.1], "temperature_2m_min": [72.0],
        "weather_code": [2],
    },
}

_GAUGE = {
    "status": {
        "observed": {
            "primary": 0.74, "primaryUnit": "ft",
            "secondary": -999, "secondaryUnit": "kcfs",
            "floodCategory": "no_flooding",
            "validTime": "2026-08-04T17:24:00Z",
        },
    },
}

# A rising series: last value well above the value 12 points back.
_SERIES_RISING = {
    "data": [{"validTime": f"t{i}", "primary": 0.40 + i * 0.02, "secondary": -999}
             for i in range(20)]
}


def _fake_get_factory(gauge=_GAUGE, series=_SERIES_RISING, meteo=_OPEN_METEO):
    def _get(url, **kwargs):
        if "open-meteo" in url:
            return _FakeResp(meteo)
        if url.endswith("/stageflow/observed"):
            return _FakeResp(series)
        return _FakeResp(gauge)
    return _get


@pytest.fixture()
def svc(app_module):
    return WeatherService(app_module.cfg)


# --- helpers ---------------------------------------------------------------

def test_compass_cardinals():
    assert weather_mod._compass(0) == "N"
    assert weather_mod._compass(45) == "NE"
    assert weather_mod._compass(90) == "E"
    assert weather_mod._compass(None) is None


def test_parse_iso_z_suffix():
    ts = weather_mod._parse_iso("2026-08-04T17:24:00Z")
    assert ts and ts > 0
    assert weather_mod._parse_iso(None) is None
    assert weather_mod._parse_iso("nonsense") is None


# --- weather feed ----------------------------------------------------------

def test_fetch_weather_shapes_all_fields(svc, monkeypatch):
    monkeypatch.setattr(weather_mod.httpx, "get", _fake_get_factory())
    w = svc._fetch_weather()
    assert w["temperature"] == {"value": 81.3, "unit": "°F"}
    assert w["humidity"]["value"] == 74
    assert w["dew_point"]["value"] == 71.5
    # hPa -> inHg: 1015 * 0.02953 ≈ 29.97
    assert w["pressure"] == {"value": 29.97, "unit": "inHg"}
    assert w["wind_speed"]["unit"] == "mph"  # tidied from Open-Meteo's "mp/h"
    assert w["uv_index"]["value"] == 6.2
    assert w["wind_direction"]["compass"] == "NE"
    assert w["condition"]["text"] == "Partly cloudy"
    assert w["condition"]["is_day"] is True
    assert w["high"] == 90.1 and w["low"] == 72.0


def test_fetch_weather_night_icon(svc, monkeypatch):
    payload = {**_OPEN_METEO, "current": {**_OPEN_METEO["current"], "is_day": 0}}
    monkeypatch.setattr(weather_mod.httpx, "get", _fake_get_factory(meteo=payload))
    w = svc._fetch_weather()
    assert w["condition"]["is_day"] is False


def test_fetch_weather_network_error_returns_none(svc, monkeypatch):
    def boom(*a, **k):
        raise weather_mod.httpx.ConnectError("no network")
    monkeypatch.setattr(weather_mod.httpx, "get", boom)
    assert svc._fetch_weather() is None


# --- water feed ------------------------------------------------------------

def test_fetch_water_level_and_rising_trend(svc, monkeypatch):
    monkeypatch.setattr(weather_mod.httpx, "get", _fake_get_factory())
    water = svc._fetch_water()
    assert water["level"] == {"value": 0.74, "unit": "ft"}
    assert water["flood_label"] == "Normal"
    assert water["trend"] == "rising"
    assert len(water["series"]) >= 2
    assert water["observed_at"] and water["observed_at"] > 0


def test_fetch_water_missing_reading_returns_none(svc, monkeypatch):
    empty = {"status": {"observed": {"primary": -999}}}
    monkeypatch.setattr(weather_mod.httpx, "get", _fake_get_factory(gauge=empty))
    assert svc._fetch_water() is None


def test_fetch_water_disabled_when_no_gauge(app_module, monkeypatch):
    cfg = app_module.cfg
    svc = WeatherService(cfg)
    svc.cfg = WeatherConfig(water_gauge="")
    assert svc._fetch_water() is None


def test_water_trend_steady_within_deadband(svc, monkeypatch):
    flat = {"data": [{"primary": 0.50, "secondary": -999} for _ in range(20)]}
    monkeypatch.setattr(weather_mod.httpx, "get",
                        _fake_get_factory(series=flat))
    water = svc._fetch_water()
    assert water["trend"] == "steady"


# --- refresh resilience ----------------------------------------------------

def test_refresh_keeps_last_good_when_a_feed_blips(svc, monkeypatch):
    # First pass: everything works.
    monkeypatch.setattr(weather_mod.httpx, "get", _fake_get_factory())
    first = svc.refresh()
    assert first["weather"] and first["water"]

    # Second pass: every request fails. Both panels should retain prior values.
    def boom(*a, **k):
        raise weather_mod.httpx.ConnectError("down")
    monkeypatch.setattr(weather_mod.httpx, "get", boom)
    second = svc.refresh()
    assert second["weather"] == first["weather"]
    assert second["water"] == first["water"]


def test_snapshot_before_refresh_is_shaped_but_empty(svc):
    snap = svc.snapshot()
    assert snap["weather"] is None and snap["water"] is None
    assert "location" in snap


# --- flood alerting --------------------------------------------------------

class _RecordingAlerts:
    def __init__(self):
        self.emitted = []

    def emit(self, **kwargs):
        self.emitted.append(kwargs)


def _water(level, category="no_flooding"):
    return {
        "gauge": "ORLN7", "label": "Neuse River at Oriental",
        "level": {"value": level, "unit": "ft"},
        "flood_category": category,
        "flood_label": {"no_flooding": "Normal"}.get(category, category.title()),
    }


def test_flood_level_threshold_edge(app_module):
    sink = _RecordingAlerts()
    svc = WeatherService(app_module.cfg, alerts=sink)
    svc.cfg = WeatherConfig(water_alert_level=3.0, water_alert_on_action=False)

    svc._check_flood(_water(2.0))          # below -> nothing
    assert sink.emitted == []
    svc._check_flood(_water(3.2))          # crosses -> one alert
    assert len(sink.emitted) == 1
    assert sink.emitted[0]["type"] == "flood"
    svc._check_flood(_water(3.5))          # still above -> no repeat
    assert len(sink.emitted) == 1
    svc._check_flood(_water(1.0))          # drops below -> re-arms
    svc._check_flood(_water(3.1))          # crosses again -> second alert
    assert len(sink.emitted) == 2


def test_flood_category_change_alerts_once(app_module):
    sink = _RecordingAlerts()
    svc = WeatherService(app_module.cfg, alerts=sink)
    svc.cfg = WeatherConfig(water_alert_level=0.0, water_alert_on_action=True)

    svc._check_flood(_water(1.0, "no_flooding"))   # normal -> nothing
    assert sink.emitted == []
    svc._check_flood(_water(5.0, "action"))        # -> alert
    svc._check_flood(_water(5.1, "action"))        # same category -> no repeat
    assert len(sink.emitted) == 1
    svc._check_flood(_water(7.0, "minor"))         # worsens -> alert
    assert len(sink.emitted) == 2


# --- endpoint --------------------------------------------------------------

def test_api_weather_endpoint(app_module, admin_client, monkeypatch):
    monkeypatch.setattr(weather_mod.httpx, "get", _fake_get_factory())
    app_module.weather.refresh()
    r = admin_client.get("/api/weather")
    assert r.status_code == 200
    body = r.json()
    assert body["weather"]["temperature"]["value"] == 81.3
    assert body["water"]["level"]["value"] == 0.74
