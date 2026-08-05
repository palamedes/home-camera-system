"""App settings: validation/coercion, DB overlay on load, and PATCH endpoints."""

from __future__ import annotations

import json

import pytest

from nvr import appsettings
from nvr.appsettings import SettingError
from nvr.config import (
    AlertsConfig, DiscoveryConfig, Go2rtcConfig, PlaybackConfig, ServerConfig,
    StorageConfig, WeatherConfig,
)


class _Cfg:
    def __init__(self):
        self.weather = WeatherConfig()
        self.alerts = AlertsConfig()
        self.server = ServerConfig()
        self.storage = StorageConfig()
        self.go2rtc = Go2rtcConfig()
        self.discovery = DiscoveryConfig()
        self.playback = PlaybackConfig()


@pytest.fixture(autouse=True)
def _restore_cfg(app_module):
    """Endpoint tests mutate the shared app config in place; snapshot each
    section and restore it so nothing leaks between tests."""
    import copy
    c = app_module.cfg
    names = ("server", "storage", "go2rtc", "discovery", "playback",
             "weather", "alerts")
    saved = {n: copy.copy(getattr(c, n)) for n in names}
    yield
    for n, val in saved.items():
        setattr(c, n, val)


# --- coercion / validation -------------------------------------------------

def test_update_weather_coerces_types(app_module, db):
    cfg = _Cfg()
    applied = appsettings.update_section(cfg, db, "weather", {
        "latitude": "35.5", "longitude": "-76.7", "enabled": "true",
        "refresh_seconds": "900", "water_alert_level": "3.5",
    })
    assert applied["latitude"] == 35.5
    assert cfg.weather.latitude == 35.5           # applied in place
    assert cfg.weather.enabled is True
    assert cfg.weather.refresh_seconds == 900
    assert cfg.weather.water_alert_level == 3.5


def test_update_rejects_bad_values_atomically(app_module, db):
    cfg = _Cfg()
    with pytest.raises(SettingError):
        appsettings.update_section(cfg, db, "weather", {"latitude": 999})
    # Nothing applied and nothing persisted on failure.
    assert cfg.weather.latitude == WeatherConfig().latitude
    assert db.get_setting("weather") is None


def test_update_rejects_unknown_field(app_module, db):
    cfg = _Cfg()
    with pytest.raises(SettingError):
        appsettings.update_section(cfg, db, "weather", {"nope": 1})


def test_update_bad_enum(app_module, db):
    cfg = _Cfg()
    with pytest.raises(SettingError):
        appsettings.update_section(cfg, db, "weather", {"temperature_unit": "kelvin"})


def test_detect_list_filters_to_known(app_module, db):
    cfg = _Cfg()
    applied = appsettings.update_section(cfg, db, "alerts", {
        "detect": ["person", "unicorn", "vehicle", "person"],
    })
    assert applied["detect"] == ["person", "vehicle"]


# --- persistence + overlay -------------------------------------------------

def test_persisted_then_loaded_back(app_module, db):
    cfg = _Cfg()
    appsettings.update_section(cfg, db, "weather", {"label": "Beaufort, NC", "latitude": 34.72})
    # Stored as a JSON blob.
    stored = json.loads(db.get_setting("weather"))
    assert stored["label"] == "Beaufort, NC"

    # A fresh config picks the overrides back up.
    fresh = _Cfg()
    appsettings.load_overrides(fresh, db)
    assert fresh.weather.label == "Beaufort, NC"
    assert fresh.weather.latitude == 34.72


def test_load_overrides_ignores_corrupt_blob(app_module, db):
    db.set_setting("weather", "{not json")
    cfg = _Cfg()
    appsettings.load_overrides(cfg, db)  # must not raise
    assert cfg.weather.label == WeatherConfig().label


def test_current_snapshot_shape(app_module, db):
    cfg = _Cfg()
    snap = appsettings.current(cfg)
    assert set(snap) == {"weather", "alerts", "storage_limits", "network"}
    assert "latitude" in snap["weather"]
    assert "webhook_url" in snap["alerts"]
    assert "max_age_days" in snap["storage_limits"]
    assert "port" in snap["network"]


# --- endpoints -------------------------------------------------------------

def test_patch_weather_endpoint(app_module, admin_client, monkeypatch):
    # Don't hit the real weather API on the post-save refresh.
    monkeypatch.setattr(app_module.weather, "refresh", lambda *a, **k: None)
    r = admin_client.patch("/api/settings/weather", json={"label": "New Bern, NC"})
    assert r.status_code == 200
    assert app_module.cfg.weather.label == "New Bern, NC"
    assert r.json()["settings"]["weather"]["label"] == "New Bern, NC"


def test_patch_alerts_endpoint_reconfigures(app_module, admin_client, monkeypatch):
    applied_called = {"n": 0}
    monkeypatch.setattr(app_module.events, "apply",
                        lambda: applied_called.__setitem__("n", applied_called["n"] + 1))
    r = admin_client.patch("/api/settings/alerts",
                           json={"enabled": True, "detect": ["person"]})
    assert r.status_code == 200
    assert app_module.cfg.alerts.enabled is True
    assert applied_called["n"] == 1


def test_patch_validation_error_returns_400(app_module, admin_client):
    r = admin_client.patch("/api/settings/weather", json={"latitude": 999})
    assert r.status_code == 400
    assert "error" in r.json()


def test_settings_endpoints_admin_only(app_module, viewer_client):
    assert viewer_client.get("/api/settings").status_code == 403
    assert viewer_client.patch("/api/settings/weather", json={}).status_code == 403


# --- advanced sections: storage limits + network ---------------------------

def test_storage_limits_live_vs_recorder_restart(app_module, db):
    cfg = _Cfg()
    applied, restarts = appsettings.update_advanced(cfg, db, "storage_limits", {
        "max_age_days": "14", "segment_seconds": "30",
    })
    assert cfg.storage.max_age_days == 14
    assert cfg.storage.segment_seconds == 30
    assert "recorder" in restarts        # segment length changed


def test_storage_limits_rejects_unknown_field(app_module, db):
    cfg = _Cfg()
    # max_usage is now per-volume, not a pool-wide limit.
    with pytest.raises(SettingError):
        appsettings.update_advanced(cfg, db, "storage_limits", {"max_usage": "80%"})


def test_network_targets_nested_attrs(app_module, db):
    cfg = _Cfg()
    applied, restarts = appsettings.update_advanced(cfg, db, "network", {
        "port": "8080", "host": "0.0.0.0", "go2rtc_rtsp_port": "9554",
        "discovery_subnets": "192.168.1.0/24, 10.0.0.0/8",
        "qsv_device": "", "session_days": "10",
    })
    assert cfg.server.port == 8080
    assert cfg.server.host == "0.0.0.0"
    assert cfg.go2rtc.rtsp_port == 9554
    assert cfg.discovery.subnets == ["192.168.1.0/24", "10.0.0.0/8"]
    assert cfg.playback.qsv_device is None      # blank -> software
    assert cfg.server.session_days == 10
    assert "app" in restarts                    # host/port/go2rtc need restart


def test_network_live_fields_have_no_restart(app_module, db):
    cfg = _Cfg()
    _applied, restarts = appsettings.update_advanced(cfg, db, "network", {
        "session_days": "45", "discovery_timeout": "2.5",
    })
    assert restarts == []                       # both are live


def test_network_rejects_bad_port_and_subnet(app_module, db):
    cfg = _Cfg()
    with pytest.raises(SettingError):
        appsettings.update_advanced(cfg, db, "network", {"port": 70000})
    with pytest.raises(SettingError):
        appsettings.update_advanced(cfg, db, "network", {"discovery_subnets": "not-a-cidr"})


def test_network_overlay_applied_on_load(app_module, db):
    cfg = _Cfg()
    appsettings.update_advanced(cfg, db, "network", {"port": 8123})
    fresh = _Cfg()
    appsettings.load_overrides(fresh, db)
    assert fresh.server.port == 8123


def test_network_overlay_skipped_by_env(app_module, db, monkeypatch):
    cfg = _Cfg()
    appsettings.update_advanced(cfg, db, "network", {"port": 9999})
    monkeypatch.setenv(appsettings.IGNORE_NETWORK_ENV, "1")
    fresh = _Cfg()
    appsettings.load_overrides(fresh, db)
    assert fresh.server.port == ServerConfig().port      # default, not 9999


def test_patch_network_endpoint_reports_restart(app_module, admin_client):
    r = admin_client.patch("/api/settings/network", json={"port": 8090})
    assert r.status_code == 200
    body = r.json()
    assert "app" in body["restart_required"]
    assert app_module.cfg.server.port == 8090


def test_patch_storage_limits_endpoint(app_module, admin_client):
    r = admin_client.patch("/api/settings/storage_limits", json={"max_age_days": 21})
    assert r.status_code == 200
    assert app_module.cfg.storage.max_age_days == 21


def test_geocode_endpoint(app_module, admin_client, monkeypatch):
    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"results": [
                {"name": "Oriental", "admin1": "North Carolina",
                 "country_code": "US", "latitude": 35.03, "longitude": -76.7},
            ]}
    monkeypatch.setattr(app_module.httpx, "get", lambda *a, **k: _Resp())
    r = admin_client.get("/api/settings/geocode?q=Oriental")
    assert r.status_code == 200
    results = r.json()["results"]
    assert results[0]["latitude"] == 35.03
    assert "Oriental" in results[0]["label"]
