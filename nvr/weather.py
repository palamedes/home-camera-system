"""Dashboard weather + river-level feed.

One background thread refreshes two independent, keyless public APIs on a slow
cadence and caches the result, so page loads and the /api/weather endpoint are
instant and never block on the network — and browsers never talk to the
internet directly:

  * Open-Meteo  (api.open-meteo.com) — current conditions + today's high/low.
    Free, no key, no signup.
  * NWS/NWPS    (api.water.noaa.gov) — observed river stage for a gauge, plus a
    short time series for the trend arrow and sparkline. Same source the local
    TownDock page draws Oriental's Neuse River level from (gauge ORLN7).

Either feed can fail on its own; the other still renders. The last good value
is kept and served stale (with an age) rather than blanking the card on a
transient error.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from . import config

log = logging.getLogger("nvr.weather")

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
NWPS = "https://api.water.noaa.gov/nwps/v1/gauges"

# The Open-Meteo `current` variables we ask for, in one shot.
_CURRENT = ",".join((
    "temperature_2m",
    "relative_humidity_2m",
    "apparent_temperature",
    "dew_point_2m",
    "precipitation",
    "weather_code",
    "surface_pressure",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "uv_index",
    "is_day",
))

_COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")

# WMO weather codes -> (label, day emoji, night emoji). Open-Meteo reports the
# same numeric codes the WMO defines; we collapse the long tail to plain words.
_WMO: dict[int, tuple[str, str, str]] = {
    0: ("Clear", "☀️", "🌙"),
    1: ("Mainly clear", "🌤️", "🌙"),
    2: ("Partly cloudy", "⛅", "☁️"),
    3: ("Overcast", "☁️", "☁️"),
    45: ("Fog", "🌫️", "🌫️"),
    48: ("Rime fog", "🌫️", "🌫️"),
    51: ("Light drizzle", "🌦️", "🌧️"),
    53: ("Drizzle", "🌦️", "🌧️"),
    55: ("Heavy drizzle", "🌧️", "🌧️"),
    56: ("Freezing drizzle", "🌧️", "🌧️"),
    57: ("Freezing drizzle", "🌧️", "🌧️"),
    61: ("Light rain", "🌦️", "🌧️"),
    63: ("Rain", "🌧️", "🌧️"),
    65: ("Heavy rain", "🌧️", "🌧️"),
    66: ("Freezing rain", "🌧️", "🌧️"),
    67: ("Freezing rain", "🌧️", "🌧️"),
    71: ("Light snow", "🌨️", "🌨️"),
    73: ("Snow", "🌨️", "🌨️"),
    75: ("Heavy snow", "❄️", "❄️"),
    77: ("Snow grains", "🌨️", "🌨️"),
    80: ("Rain showers", "🌦️", "🌧️"),
    81: ("Rain showers", "🌧️", "🌧️"),
    82: ("Violent showers", "⛈️", "⛈️"),
    85: ("Snow showers", "🌨️", "🌨️"),
    86: ("Snow showers", "❄️", "❄️"),
    95: ("Thunderstorm", "⛈️", "⛈️"),
    96: ("Thunderstorm", "⛈️", "⛈️"),
    99: ("Thunderstorm", "⛈️", "⛈️"),
}

# NWPS floodCategory slugs -> human label. "no_flooding" is the normal state.
_FLOOD = {
    "no_flooding": "Normal",
    "action": "Action stage",
    "minor": "Minor flooding",
    "moderate": "Moderate flooding",
    "major": "Major flooding",
}


def _compass(degrees: float | None) -> str | None:
    if degrees is None:
        return None
    return _COMPASS[round(degrees / 22.5) % 16]


def _parse_iso(text: str | None) -> float | None:
    """ISO-8601 (often Z-suffixed) -> epoch seconds, or None."""
    if not text:
        return None
    try:
        cleaned = text.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).timestamp()
    except ValueError:
        return None


class WeatherService:
    def __init__(self, config: Any, alerts: Any = None):
        self.cfg = config.weather
        # Optional alert dispatcher; when present, river-level thresholds and
        # NWS flood-stage changes raise notifications.
        self.alerts = alerts
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None
        # Flood-alert edge state: whether we're currently above the user's
        # level threshold, and the last NWS category we alerted on.
        self._flood_above = False
        self._flood_category = "no_flooding"

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        # Always run the loop; refresh() self-guards when disabled. This lets the
        # settings page toggle weather on/off live without restarting a thread.
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="weather", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                log.exception("weather refresh failed")
            # See events.py: the sleep is outside the try, so a bad interval
            # here would take the whole weather service down without a trace.
            self._stop.wait(
                config.safe_interval(self.cfg.refresh_seconds,
                                     default=600.0, minimum=60.0)
            )

    # -- public read ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Last cached payload (never blocks). Empty-but-shaped before the
        first refresh so the frontend can render a loading state."""
        with self._lock:
            if self._data is None:
                return {
                    "enabled": self.cfg.enabled,
                    "location": self.cfg.label,
                    "updated": None,
                    "weather": None,
                    "water": None,
                }
            return self._data

    # -- refresh -------------------------------------------------------------

    def refresh(self) -> dict[str, Any]:
        if not self.cfg.enabled:
            # Disabled: don't touch the network; present an empty, shaped payload.
            disabled = {
                "enabled": False, "location": self.cfg.label,
                "updated": None, "weather": None, "water": None,
            }
            with self._lock:
                self._data = disabled
            return disabled
        weather = self._fetch_weather()
        water = self._fetch_water()
        if water and self.alerts:
            # Check the freshly-fetched reading (not a held stale one).
            self._check_flood(water)
        payload = {
            "enabled": True,
            "location": self.cfg.label,
            "updated": time.time(),
            "weather": weather,
            "water": water,
        }
        with self._lock:
            # Keep the last good sub-feed if this pass couldn't fetch it, rather
            # than flipping a working panel to empty on one bad request.
            if self._data:
                if weather is None:
                    payload["weather"] = self._data.get("weather")
                if water is None:
                    payload["water"] = self._data.get("water")
            self._data = payload
        return payload

    def _check_flood(self, water: dict[str, Any]) -> None:
        """Raise a flood alert on a rising edge — crossing the user's level
        threshold, or NWS moving to a new non-normal flood category. Edge-
        tracked so a river that simply stays high is one alert, not one per
        refresh; the threshold re-arms once the level drops back below it."""
        level = (water.get("level") or {}).get("value")
        category = water.get("flood_category") or "no_flooding"
        label = water.get("label") or "River"

        # 1. User level threshold.
        threshold = self.cfg.water_alert_level
        if threshold and level is not None:
            above = float(level) >= float(threshold)
            if above and not self._flood_above:
                self._emit_flood(
                    label, level, water,
                    f"{label} at {float(level):.2f} ft — above your "
                    f"{float(threshold):.2f} ft alert level",
                )
            self._flood_above = above

        # 2. NWS flood-stage change (normal -> action/minor/... or worsening).
        if self.cfg.water_alert_on_action:
            if category != "no_flooding" and category != self._flood_category:
                stage = water.get("flood_label") or category
                lvl = f"{float(level):.2f} ft" if level is not None else "n/a"
                self._emit_flood(label, level, water, f"{label}: NWS {stage} ({lvl})")
            self._flood_category = category

    def _emit_flood(
        self, label: str, level: Any, water: dict[str, Any], message: str
    ) -> None:
        try:
            self.alerts.emit(
                type="flood",
                camera_id=None,
                camera_name=label,
                label=message,
                message=message,
                score=float(level) if level is not None else None,
                meta={
                    "gauge": water.get("gauge"),
                    "level": level,
                    "unit": (water.get("level") or {}).get("unit"),
                    "flood_category": water.get("flood_category"),
                },
            )
        except Exception:
            log.exception("failed to emit flood alert")

    def _fetch_weather(self) -> dict[str, Any] | None:
        params = {
            "latitude": self.cfg.latitude,
            "longitude": self.cfg.longitude,
            "current": _CURRENT,
            "daily": "temperature_2m_max,temperature_2m_min,weather_code",
            "temperature_unit": self.cfg.temperature_unit,
            "wind_speed_unit": self.cfg.wind_unit,
            "precipitation_unit": self.cfg.precipitation_unit,
            "timezone": "auto",
            "forecast_days": 1,
        }
        try:
            resp = httpx.get(OPEN_METEO, params=params, timeout=8.0)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("open-meteo fetch failed: %s", exc)
            return None

        cur = data.get("current") or {}
        units = data.get("current_units") or {}
        daily = data.get("daily") or {}
        code = int(cur.get("weather_code", 0) or 0)
        is_day = bool(cur.get("is_day", 1))
        label, day_icon, night_icon = _WMO.get(code, ("—", "🌡️", "🌡️"))

        def metric(key: str) -> dict[str, Any]:
            return {"value": cur.get(key), "unit": units.get(key, "")}

        def wind(key: str) -> dict[str, Any]:
            # Open-Meteo writes mph as the awkward "mp/h"; tidy it.
            m = metric(key)
            if m["unit"] == "mp/h":
                m["unit"] = "mph"
            return m

        def pressure_inhg() -> dict[str, Any]:
            # Open-Meteo only reports pressure in hPa; convert to inHg, which is
            # what a US audience reads a barometer in.
            raw = cur.get("surface_pressure")
            if raw is None:
                return {"value": None, "unit": "inHg"}
            return {"value": round(float(raw) * 0.02953, 2), "unit": "inHg"}

        return {
            "temperature": metric("temperature_2m"),
            "apparent": metric("apparent_temperature"),
            "humidity": metric("relative_humidity_2m"),
            "dew_point": metric("dew_point_2m"),
            "precipitation": metric("precipitation"),
            "pressure": pressure_inhg(),
            "wind_speed": wind("wind_speed_10m"),
            "wind_gust": wind("wind_gusts_10m"),
            "uv_index": metric("uv_index"),
            "wind_direction": {
                "deg": cur.get("wind_direction_10m"),
                "compass": _compass(cur.get("wind_direction_10m")),
            },
            "condition": {
                "code": code,
                "text": label,
                "icon": day_icon if is_day else night_icon,
                "is_day": is_day,
            },
            "high": _first(daily.get("temperature_2m_max")),
            "low": _first(daily.get("temperature_2m_min")),
            "temp_unit": units.get("temperature_2m", "°"),
        }

    def _fetch_water(self) -> dict[str, Any] | None:
        gauge = (self.cfg.water_gauge or "").strip().upper()
        if not gauge:
            return None
        try:
            resp = httpx.get(f"{NWPS}/{gauge}", timeout=8.0)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("nwps gauge %s fetch failed: %s", gauge, exc)
            return None

        observed = ((data.get("status") or {}).get("observed") or {})
        level = observed.get("primary")
        if level is None or level == -999:
            return None
        flood = str(observed.get("floodCategory") or "no_flooding")

        series, trend = self._water_series(gauge)
        return {
            "gauge": gauge,
            "label": self.cfg.water_label,
            "level": {"value": level, "unit": observed.get("primaryUnit", "ft")},
            "flood_category": flood,
            "flood_label": _FLOOD.get(flood, flood.replace("_", " ").title()),
            "observed_at": _parse_iso(observed.get("validTime")),
            "trend": trend,
            "series": series,
        }

    def _water_series(self, gauge: str) -> tuple[list[float], str | None]:
        """Recent observed stage values (for a sparkline) and a rise/fall/steady
        trend. Best-effort: a failure here just drops the sparkline, not the
        current reading."""
        try:
            resp = httpx.get(f"{NWPS}/{gauge}/stageflow/observed", timeout=8.0)
            resp.raise_for_status()
            points = (resp.json() or {}).get("data") or []
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("nwps series %s fetch failed: %s", gauge, exc)
            return [], None

        values = [
            float(p["primary"])
            for p in points
            if p.get("primary") is not None and p.get("primary") != -999
        ]
        # Keep the tail (~last day at ~15-min cadence) so the sparkline stays legible.
        values = values[-96:]
        if len(values) < 2:
            return values, None

        # Trend from the recent slope: compare the latest reading against the
        # value ~3h back (12 points), with a small deadband so noise reads flat.
        earlier = values[-12] if len(values) >= 12 else values[0]
        delta = values[-1] - earlier
        if delta > 0.05:
            trend = "rising"
        elif delta < -0.05:
            trend = "falling"
        else:
            trend = "steady"
        return values, trend


def _first(seq: Any) -> Any:
    return seq[0] if isinstance(seq, list) and seq else None
