"""Adapter to convert Home Assistant weather entity forecasts to ForecastPoints."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from homeassistant.components.weather import (
    ATTR_FORECAST_CLOUD_COVERAGE,
    ATTR_FORECAST_TEMP,
    ATTR_FORECAST_TIME,
    ATTR_FORECAST_WIND_SPEED,
    WeatherEntityFeature,
)
from homeassistant.const import UnitOfSpeed, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import SpeedConverter, TemperatureConverter

from ..engine.data_types import ForecastPoint

_LOGGER = logging.getLogger(__name__)


async def async_get_forecast(
    hass: HomeAssistant,
    weather_entity_id: str,
) -> list[ForecastPoint]:
    """Fetch hourly forecast from a weather entity and convert to ForecastPoints.

    Prefers hourly forecast; falls back to twice-daily if hourly unavailable.
    All temperatures are converted to °F for the engine.
    """
    # Check if entity exists — during early boot it may not be registered yet.
    if hass.states.get(weather_entity_id) is None:
        _LOGGER.debug("Weather entity %s not in state machine yet", weather_entity_id)
        return []

    forecast_data = await _fetch_forecast(hass, weather_entity_id, "hourly")
    if not forecast_data:
        forecast_data = await _fetch_forecast(hass, weather_entity_id, "twice_daily")
    if not forecast_data:
        _LOGGER.warning("No forecast data from %s", weather_entity_id)
        return []

    points: list[ForecastPoint] = []
    for entry in forecast_data:
        time_str = entry.get(ATTR_FORECAST_TIME)
        temp = entry.get(ATTR_FORECAST_TEMP)
        if time_str is None or temp is None:
            continue

        # Parse time
        if isinstance(time_str, str):
            forecast_time = datetime.fromisoformat(time_str)
        elif isinstance(time_str, datetime):
            forecast_time = time_str
        else:
            continue

        # Ensure timezone-aware
        if forecast_time.tzinfo is None:
            forecast_time = forecast_time.replace(tzinfo=timezone.utc)

        # Convert temperature to °F if needed
        temp_f = _ensure_fahrenheit(hass, weather_entity_id, temp)

        # Cloud cover (0-100 from HA, convert to 0-1 fraction)
        cloud_cover = entry.get(ATTR_FORECAST_CLOUD_COVERAGE)
        cloud_fraction = cloud_cover / 100.0 if cloud_cover is not None else None

        # Wind speed (HA provides in km/h or mph depending on unit system)
        wind_speed = entry.get(ATTR_FORECAST_WIND_SPEED)
        wind_speed_mph = _ensure_mph(hass, weather_entity_id, wind_speed) if wind_speed is not None else None

        # Humidity
        humidity = entry.get("humidity")

        # Weather condition and precipitation flag
        condition = entry.get("condition")
        _PRECIP_CONDITIONS = {"rainy", "pouring", "snowy", "lightning-rainy", "hail"}
        is_precip = condition in _PRECIP_CONDITIONS if condition else False

        points.append(
            ForecastPoint(
                time=forecast_time,
                outdoor_temp=temp_f,
                carbon_intensity=None,
                electricity_rate=None,
                wind_speed_mph=wind_speed_mph,
                humidity=humidity,
                cloud_cover=cloud_fraction,
                weather_condition=condition,
                precipitation=is_precip,
            )
        )

    _LOGGER.debug("Fetched %d forecast points from %s", len(points), weather_entity_id)
    return points


async def async_get_forecast_multi(
    hass: HomeAssistant,
    weather_entity_ids: list[str],
) -> tuple[list[ForecastPoint], str | None]:
    """Try each weather entity in order, return first successful result.

    Returns:
        Tuple of (forecast_points, source_entity_id). If all fail, returns ([], None).
    """
    for entity_id in weather_entity_ids:
        points = await async_get_forecast(hass, entity_id)
        if points:
            if len(weather_entity_ids) > 1:
                _LOGGER.debug(
                    "Forecast from %s (%d points, %d entities available)",
                    entity_id,
                    len(points),
                    len(weather_entity_ids),
                )
            return points, entity_id

    # Only warn if entities exist (if they don't, it's a boot timing issue)
    entities_exist = any(hass.states.get(eid) for eid in weather_entity_ids)
    if entities_exist:
        _LOGGER.warning(
            "All %d weather entities failed to provide forecast: %s",
            len(weather_entity_ids),
            weather_entity_ids,
        )
    return [], None


async def _fetch_forecast(
    hass: HomeAssistant,
    entity_id: str,
    forecast_type: str,
) -> list[dict] | None:
    """Fetch forecast data using the weather.get_forecasts service."""
    try:
        result = await hass.services.async_call(
            "weather",
            "get_forecasts",
            {"entity_id": entity_id, "type": forecast_type},
            blocking=True,
            return_response=True,
        )
        if result and entity_id in result:
            return result[entity_id].get("forecast", [])
        _LOGGER.debug(
            "weather.get_forecasts(%s, %s) returned: %s",
            entity_id, forecast_type, result,
        )
    except Exception as err:
        _LOGGER.warning(
            "Failed to get %s forecast from %s: %s",
            forecast_type, entity_id, err,
        )
    return None


def _ensure_fahrenheit(
    hass: HomeAssistant, entity_id: str, temp: float
) -> float:
    """Convert temperature to °F if the weather entity reports in °C."""
    state = hass.states.get(entity_id)
    if state is not None:
        unit = state.attributes.get("temperature_unit")
    else:
        # Entity state not available yet — fall back to HA unit system
        unit = hass.config.units.temperature_unit

    if unit == UnitOfTemperature.CELSIUS:
        return TemperatureConverter.convert(
            temp, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
        )
    return temp


def _ensure_mph(hass: HomeAssistant, entity_id: str, wind_speed: float) -> float:
    """Convert wind speed to mph based on the weather entity's reported unit."""
    state = hass.states.get(entity_id)
    if state is not None:
        unit = state.attributes.get("wind_speed_unit", UnitOfSpeed.MILES_PER_HOUR)
    else:
        # Fall back to HA unit system — metric uses km/h
        unit = hass.config.units.wind_speed_unit
    if unit in (UnitOfSpeed.KILOMETERS_PER_HOUR, "km/h"):
        return SpeedConverter.convert(
            wind_speed, UnitOfSpeed.KILOMETERS_PER_HOUR, UnitOfSpeed.MILES_PER_HOUR
        )
    if unit in (UnitOfSpeed.METERS_PER_SECOND, "m/s"):
        return SpeedConverter.convert(
            wind_speed, UnitOfSpeed.METERS_PER_SECOND, UnitOfSpeed.MILES_PER_HOUR
        )
    return wind_speed


async def enrich_forecast_with_grid_data(
    hass: HomeAssistant,
    forecast: list[ForecastPoint],
    co2_entity_id: str | None = None,
    rate_entity_id: str | None = None,
    flat_rate: float | None = None,
    tou_schedule: list[dict] | None = None,
) -> list[ForecastPoint]:
    """Enrich forecast points with carbon intensity and electricity rate data.

    Reads current values from configured HA sensor entities and applies them
    to forecast points. If the CO2 sensor exposes a 'forecast' attribute with
    hourly data, uses per-hour values; otherwise applies current value to all hours.
    """
    # ── Carbon intensity ────────────────────────────────────────────
    co2_current: float | None = None
    co2_by_hour: dict[int, float] = {}

    if co2_entity_id:
        co2_state = hass.states.get(co2_entity_id)
        if co2_state and co2_state.state not in ("unknown", "unavailable"):
            try:
                co2_current = float(co2_state.state)
            except (ValueError, TypeError):
                _LOGGER.debug("Could not parse CO2 intensity from %s", co2_entity_id)

            # Check for forecast attribute (some integrations expose hourly forecasts)
            co2_forecast = co2_state.attributes.get("forecast")
            if isinstance(co2_forecast, list):
                for entry in co2_forecast:
                    time_str = entry.get("datetime") or entry.get("time")
                    intensity = entry.get("carbon_intensity") or entry.get("value")
                    if time_str and intensity is not None:
                        try:
                            if isinstance(time_str, str):
                                dt = datetime.fromisoformat(time_str)
                            else:
                                dt = time_str
                            hour_key = int(dt.timestamp()) // 3600
                            co2_by_hour[hour_key] = float(intensity)
                        except (ValueError, TypeError):
                            continue

    # ── Electricity rate ────────────────────────────────────────────
    rate_current: float | None = None

    if rate_entity_id:
        rate_state = hass.states.get(rate_entity_id)
        if rate_state and rate_state.state not in ("unknown", "unavailable"):
            try:
                rate_current = float(rate_state.state)
            except (ValueError, TypeError):
                _LOGGER.debug("Could not parse electricity rate from %s", rate_entity_id)

    # Fall back to flat rate from config
    if rate_current is None and flat_rate is not None:
        rate_current = flat_rate

    # ── Apply to forecast points ────────────────────────────────────
    for pt in forecast:
        hour_key = int(pt.time.timestamp()) // 3600

        # CO2: prefer per-hour forecast, fall back to current reading
        if hour_key in co2_by_hour:
            pt.carbon_intensity = co2_by_hour[hour_key]
        elif co2_current is not None:
            pt.carbon_intensity = co2_current

        # Rate: TOU schedule takes priority, then entity, then flat rate
        tou_rate = _lookup_tou_rate(pt.time, tou_schedule) if tou_schedule else None
        if tou_rate is not None:
            pt.electricity_rate = tou_rate
        elif rate_current is not None:
            pt.electricity_rate = rate_current

    enriched_count = sum(
        1 for pt in forecast
        if pt.carbon_intensity is not None or pt.electricity_rate is not None
    )
    _LOGGER.debug(
        "Enriched %d/%d forecast points with grid data (CO2=%s, rate=%s)",
        enriched_count,
        len(forecast),
        co2_entity_id or "none",
        rate_entity_id or f"flat={flat_rate}" if flat_rate else "none",
    )

    return forecast


def _lookup_tou_rate(
    forecast_time: datetime,
    tou_schedule: list[dict],
) -> float | None:
    """Look up the electricity rate from a TOU schedule for a given time.

    Schedule format: [{"days": [0-6], "start_hour": 0-23, "end_hour": 0-23, "rate": float}]
    where days uses Monday=0 convention.
    """
    day_of_week = forecast_time.weekday()
    hour = forecast_time.hour

    for period in tou_schedule:
        days = period.get("days", [])
        start = period.get("start_hour", 0)
        end = period.get("end_hour", 24)
        rate = period.get("rate")

        if rate is None:
            continue
        if day_of_week not in days:
            continue
        if start <= hour < end:
            return float(rate)

    return None


def has_forecast_deviated(
    old_forecast: list[ForecastPoint],
    new_forecast: list[ForecastPoint],
    lookahead_hours: int = 6,
    threshold_f: float = 5.0,
) -> bool:
    """Check if the forecast has changed enough to warrant re-optimization.

    Compares temperatures in the next `lookahead_hours` between old and new
    forecasts. Returns True if any hour deviates by more than `threshold_f`.
    """
    if not old_forecast or not new_forecast:
        return True  # No old forecast = always re-optimize

    now = datetime.now(timezone.utc)
    cutoff = now.replace(hour=now.hour + lookahead_hours)

    # Build lookup from old forecast: hour -> temp
    old_by_hour: dict[int, float] = {}
    for pt in old_forecast:
        if pt.time <= cutoff:
            hour_key = int(pt.time.timestamp()) // 3600
            old_by_hour[hour_key] = pt.outdoor_temp

    for pt in new_forecast:
        if pt.time > cutoff:
            continue
        hour_key = int(pt.time.timestamp()) // 3600
        old_temp = old_by_hour.get(hour_key)
        if old_temp is not None and abs(pt.outdoor_temp - old_temp) > threshold_f:
            _LOGGER.info(
                "Forecast deviation: %s was %.1f°F, now %.1f°F (threshold %.1f°F)",
                pt.time.isoformat(),
                old_temp,
                pt.outdoor_temp,
                threshold_f,
            )
            return True

    return False


def estimate_solar_elevation(
    latitude: float,
    longitude: float,
    dt: datetime,
) -> float:
    """Estimate solar elevation angle from latitude, longitude, and UTC time.

    Uses a simplified solar position algorithm. Returns degrees above horizon
    (negative = below horizon / nighttime).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    # Convert to UTC for calculation
    utc = dt.utctimetuple() if hasattr(dt, "utctimetuple") else dt.timetuple()
    utc_hour = utc.tm_hour + utc.tm_min / 60.0
    day_of_year = utc.tm_yday

    # Solar declination (approximate)
    declination = 23.45 * math.sin(math.radians(360.0 / 365.0 * (day_of_year - 81)))

    # Hour angle: incorporate longitude for correct solar noon
    hour_angle = (utc_hour - 12.0 + longitude / 15.0) * 15.0

    # Solar elevation
    lat_rad = math.radians(latitude)
    dec_rad = math.radians(declination)
    ha_rad = math.radians(hour_angle)

    sin_elevation = (
        math.sin(lat_rad) * math.sin(dec_rad)
        + math.cos(lat_rad) * math.cos(dec_rad) * math.cos(ha_rad)
    )

    return math.degrees(math.asin(max(-1.0, min(1.0, sin_elevation))))


def populate_sun_elevation(
    forecast: list[ForecastPoint],
    latitude: float,
    longitude: float,
) -> None:
    """Fill in sun_elevation for all forecast points that don't already have it."""
    for pt in forecast:
        if pt.sun_elevation is None:
            pt.sun_elevation = estimate_solar_elevation(latitude, longitude, pt.time)
