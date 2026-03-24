"""SensorHub — centralized sensor reads with fallback chains and validation.

Provides a single point of access for all environmental, indoor, and energy
sensor data. Each read method implements a fallback chain (e.g., standalone
sensor → forecast → last known) so the optimizer degrades gracefully when
optional sensors are unavailable.

All temperatures are normalized to °F. Wind speeds to mph. Pressure to hPa.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

from homeassistant.const import (
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import (
    PressureConverter,
    SpeedConverter,
    TemperatureConverter,
)

from ..const import (
    BLEND_MODE_MEDIAN,
    BLEND_MODE_NONE,
    DEFAULT_BLEND_OUTLIER_THRESHOLD_F,
    DEFAULT_DOOR_WINDOW_DEBOUNCE_SECONDS,
    DEFAULT_EMA_ALPHA,
    DEFAULT_HUMIDITY_SQUELCH_OFF,
    DEFAULT_HUMIDITY_SQUELCH_ON,
    DEFAULT_SENSOR_STALE_MINUTES,
)
from ..engine.data_types import ForecastPoint

# Avoid circular import — AreaOccupancyManager is only used for type hints
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .area_occupancy import AreaOccupancyManager

_LOGGER = logging.getLogger(__name__)


@dataclass
class SensorReading:
    """A validated sensor reading with provenance."""

    value: float
    source: str  # e.g. "entity:sensor.outdoor_temp", "forecast", "average:3"
    timestamp: datetime
    stale: bool = False  # True if > DEFAULT_SENSOR_STALE_MINUTES old
    sensor_count: int = 1  # Number of sensors contributing to this reading
    max_spread: float = 0.0  # Max difference between sensors (for divergence detection)


class SensorHub:
    """Centralized sensor reads with fallback chains and multi-sensor averaging."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        outdoor_temp_entities: list[str] | None = None,
        outdoor_humidity_entities: list[str] | None = None,
        indoor_temp_entities: list[str] | None = None,
        indoor_humidity_entities: list[str] | None = None,
        wind_speed_entity: str | None = None,
        solar_irradiance_entity: str | None = None,
        uv_index_entity: str | None = None,
        barometric_pressure_entity: str | None = None,
        sun_entity: str = "sun.sun",
        solar_production_entity: str | None = None,
        grid_import_entity: str | None = None,
        solar_export_rate_entity: str | None = None,
        # Door/window contact sensors (optional)
        door_window_entities: list[str] | None = None,
        # Buffer zone temperature sensors (optional)
        attic_temp_entity: str | None = None,
        crawlspace_temp_entity: str | None = None,
        # Existing sensor config (migrated from coordinator)
        power_entity: str | None = None,
        power_default_watts: float = 3500.0,
        co2_entity: str | None = None,
        rate_entity: str | None = None,
        flat_rate: float | None = None,
        # Thermostat satellite blend mitigation (optional)
        blend_mode: str = BLEND_MODE_NONE,
        blend_outlier_threshold_f: float = DEFAULT_BLEND_OUTLIER_THRESHOLD_F,
        # Humidity-gated sensor squelching (wet rooms)
        humidity_squelch_pairs: list[dict] | None = None,
    ) -> None:
        self.hass = hass

        # Environmental
        self._outdoor_temp_entities = outdoor_temp_entities or []
        self._outdoor_humidity_entities = outdoor_humidity_entities or []
        self._wind_speed_entity = wind_speed_entity
        self._solar_irradiance_entity = solar_irradiance_entity
        self._uv_index_entity = uv_index_entity
        self._barometric_pressure_entity = barometric_pressure_entity
        self._sun_entity = sun_entity

        # Indoor
        self._indoor_temp_entities = indoor_temp_entities or []
        self._indoor_humidity_entities = indoor_humidity_entities or []

        # Energy/solar
        self._solar_production_entity = solar_production_entity
        self._grid_import_entity = grid_import_entity
        self._solar_export_rate_entity = solar_export_rate_entity

        # Room-aware area occupancy manager (optional)
        self._area_manager: AreaOccupancyManager | None = None

        # Door/window contact sensors (optional)
        self._door_window_entities: list[str] = door_window_entities or []

        # Buffer zone temperature sensors (optional)
        self._attic_temp_entity = attic_temp_entity
        self._crawlspace_temp_entity = crawlspace_temp_entity

        # Existing (migrated from coordinator)
        self._power_entity = power_entity
        self._power_default_watts = power_default_watts
        self._co2_entity = co2_entity
        self._rate_entity = rate_entity
        self._flat_rate = flat_rate

        # Last-known caches for fallback
        self._last_outdoor_temp: SensorReading | None = None
        self._last_outdoor_humidity: SensorReading | None = None

        # EMA smoothing state
        self._ema_indoor_temp: float | None = None
        self._ema_outdoor_temp: float | None = None
        self._ema_alpha: float = DEFAULT_EMA_ALPHA

        # Door/window debounce state (per-entity)
        self._dw_debounce_seconds: int = DEFAULT_DOOR_WINDOW_DEBOUNCE_SECONDS
        self._dw_last_raw: dict[str, tuple[bool, datetime]] = {}
        self._dw_debounced: dict[str, bool] = {}

        # Thermostat satellite blend mitigation (median mode)
        self._blend_mode: str = blend_mode
        self._blend_outlier_threshold_f: float = blend_outlier_threshold_f
        # Public diagnostic state (updated by read_indoor_temp each cycle)
        self.thermostat_blend_suspected: bool = False
        self.cross_sensor_spread_f: float = 0.0

        # Humidity-gated sensor squelching (wet rooms)
        self._humidity_squelch_pairs: list[dict] = humidity_squelch_pairs or []
        self._squelch_active: dict[str, bool] = {}  # hysteresis state per pair
        self._current_squelched: set[str] = set()    # cached per cycle
        self.squelched_entities: list[str] = []       # public diagnostic

    # ── Core validation ─────────────────────────────────────────────

    def _read_entity(
        self,
        entity_id: str,
        min_val: float,
        max_val: float,
        label: str,
    ) -> float | None:
        """Read and validate a numeric sensor value within expected bounds."""
        state = self.hass.states.get(entity_id)
        if not state or state.state in ("unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        if value < min_val or value > max_val:
            _LOGGER.warning(
                "%s sensor %s value %.2f outside valid range [%.1f, %.1f] — ignoring",
                label, entity_id, value, min_val, max_val,
            )
            return None
        return value

    def _read_entity_with_unit(
        self,
        entity_id: str,
        min_val: float,
        max_val: float,
        label: str,
    ) -> tuple[float | None, str | None]:
        """Read a numeric sensor value and its unit_of_measurement."""
        state = self.hass.states.get(entity_id)
        if not state or state.state in ("unknown", "unavailable"):
            return None, None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None, None
        unit = state.attributes.get("unit_of_measurement")
        if value < min_val or value > max_val:
            _LOGGER.warning(
                "%s sensor %s value %.2f outside valid range [%.1f, %.1f] — ignoring",
                label, entity_id, value, min_val, max_val,
            )
            return None, unit
        return value, unit

    def _is_stale(self, state) -> bool:
        """Check if a state object's last_updated is too old."""
        if state is None or state.last_updated is None:
            return True
        age_min = (datetime.now(timezone.utc) - state.last_updated).total_seconds() / 60
        return age_min > DEFAULT_SENSOR_STALE_MINUTES

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    # ── EMA smoothing ────────────────────────────────────────────────

    @staticmethod
    def _apply_ema(current: float, previous: float | None, alpha: float) -> float:
        """Apply exponential moving average. Cold-starts on first call."""
        if previous is None:
            return current
        return alpha * current + (1.0 - alpha) * previous

    # ── Humidity-gated squelching ──────────────────────────────────

    def _get_squelched_entities(self) -> set[str]:
        """Check humidity squelch pairs and return entity IDs to exclude.

        Uses hysteresis: activates at DEFAULT_HUMIDITY_SQUELCH_ON (65%),
        deactivates at DEFAULT_HUMIDITY_SQUELCH_OFF (55%).
        """
        squelched: set[str] = set()
        for pair in self._humidity_squelch_pairs:
            hum_eid = pair.get("humidity_entity", "")
            temp_eid = pair.get("temp_entity", "")
            if not hum_eid or not temp_eid:
                continue

            humidity = self._read_entity(hum_eid, 0.0, 100.0, "Squelch humidity")
            if humidity is None:
                continue  # Can't read sensor — don't squelch

            was_active = self._squelch_active.get(hum_eid, False)
            if was_active:
                now_active = humidity >= DEFAULT_HUMIDITY_SQUELCH_OFF
            else:
                now_active = humidity >= DEFAULT_HUMIDITY_SQUELCH_ON

            if now_active != was_active:
                _LOGGER.info(
                    "Humidity squelch %s for %s (humidity=%.1f%% via %s)",
                    "activated" if now_active else "deactivated",
                    temp_eid,
                    humidity,
                    hum_eid,
                )
            self._squelch_active[hum_eid] = now_active

            if now_active:
                squelched.add(temp_eid)
                squelched.add(hum_eid)

        self._current_squelched = squelched
        self.squelched_entities = sorted(squelched)
        return squelched

    # ── Multi-sensor averaging ─────────────────────────────────────

    def _read_multi_temp(
        self,
        entity_ids: list[str],
        label: str,
        exclude_entities: set[str] | None = None,
    ) -> SensorReading | None:
        """Read multiple temperature sensors, average valid ones, auto-convert to °F.

        Fresh readings are preferred. Stale readings are only included if no
        fresh readings are available, preventing old values from dominating.
        """
        if not entity_ids:
            return None

        fresh_values: list[float] = []
        fresh_entities: list[str] = []
        stale_values: list[float] = []
        stale_entities: list[str] = []

        for eid in entity_ids:
            if exclude_entities and eid in exclude_entities:
                continue
            value, unit = self._read_entity_with_unit(eid, -80.0, 200.0, label)
            if value is None:
                continue
            # Convert to °F if needed
            if unit == UnitOfTemperature.CELSIUS or unit == "°C":
                value = TemperatureConverter.convert(
                    value, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
                )
            state = self.hass.states.get(eid)
            if self._is_stale(state):
                stale_values.append(value)
                stale_entities.append(eid)
            else:
                fresh_values.append(value)
                fresh_entities.append(eid)

        # Prefer fresh readings; fall back to stale if no fresh available
        if fresh_values:
            values, entities_used, is_stale = fresh_values, fresh_entities, False
        elif stale_values:
            values, entities_used, is_stale = stale_values, stale_entities, True
        else:
            return None

        avg = sum(values) / len(values)
        spread = (max(values) - min(values)) if len(values) > 1 else 0.0
        source = (
            f"entity:{entities_used[0]}" if len(values) == 1
            else f"average:{len(values)}"
        )
        return SensorReading(
            value=avg,
            source=source,
            timestamp=self._now(),
            stale=is_stale,
            sensor_count=len(values),
            max_spread=spread,
        )

    def _read_multi_humidity(
        self,
        entity_ids: list[str],
        label: str,
        exclude_entities: set[str] | None = None,
    ) -> SensorReading | None:
        """Read multiple humidity sensors, average valid ones.

        Fresh readings preferred; stale only used as fallback.
        """
        if not entity_ids:
            return None

        fresh_values: list[float] = []
        fresh_entities: list[str] = []
        stale_values: list[float] = []
        stale_entities: list[str] = []

        for eid in entity_ids:
            if exclude_entities and eid in exclude_entities:
                continue
            value = self._read_entity(eid, 0.0, 100.0, label)
            if value is None:
                continue
            state = self.hass.states.get(eid)
            if self._is_stale(state):
                stale_values.append(value)
                stale_entities.append(eid)
            else:
                fresh_values.append(value)
                fresh_entities.append(eid)

        if fresh_values:
            values, entities_used, is_stale = fresh_values, fresh_entities, False
        elif stale_values:
            values, entities_used, is_stale = stale_values, stale_entities, True
        else:
            return None

        avg = sum(values) / len(values)
        source = (
            f"entity:{entities_used[0]}" if len(values) == 1
            else f"average:{len(values)}"
        )
        return SensorReading(
            value=avg,
            source=source,
            timestamp=self._now(),
            stale=is_stale,
            sensor_count=len(values),
        )

    # ── Outdoor temperature ──────────────────────────────────────────

    def read_outdoor_temp(
        self,
        forecast_snapshot: list[ForecastPoint] | None = None,
    ) -> SensorReading | None:
        """Outdoor temp: entity average → forecast current hour → last known.

        Applies EMA smoothing to reduce noise-driven setpoint corrections.
        """
        # 1. Standalone sensor entities
        reading = self._read_multi_temp(self._outdoor_temp_entities, "Outdoor temp")
        if reading and not reading.stale:
            self._ema_outdoor_temp = self._apply_ema(
                reading.value, self._ema_outdoor_temp, self._ema_alpha
            )
            reading = SensorReading(
                value=self._ema_outdoor_temp,
                source=reading.source,
                timestamp=reading.timestamp,
                stale=False,
                sensor_count=reading.sensor_count,
            )
            self._last_outdoor_temp = reading
            return reading

        # 2. Current-hour forecast
        if forecast_snapshot:
            now = self._now()
            closest = min(
                forecast_snapshot,
                key=lambda pt: abs((pt.time - now).total_seconds()),
            )
            if abs((closest.time - now).total_seconds()) < 7200:  # within 2h
                reading = SensorReading(
                    value=closest.outdoor_temp,
                    source="forecast",
                    timestamp=closest.time,
                    stale=False,
                )
                self._last_outdoor_temp = reading
                return reading

        # 3. Stale entity reading (better than nothing)
        if reading and reading.stale:
            self._last_outdoor_temp = reading
            return reading

        # 4. Last known
        if self._last_outdoor_temp is not None:
            return SensorReading(
                value=self._last_outdoor_temp.value,
                source="last_known",
                timestamp=self._last_outdoor_temp.timestamp,
                stale=True,
            )

        return None

    # ── Outdoor humidity ─────────────────────────────────────────────

    def read_outdoor_humidity(
        self,
        thermostat_humidity: float | None = None,
        forecast_snapshot: list[ForecastPoint] | None = None,
    ) -> SensorReading | None:
        """Outdoor humidity: entity average → forecast → None."""
        # 1. Standalone sensor entities
        reading = self._read_multi_humidity(
            self._outdoor_humidity_entities, "Outdoor humidity"
        )
        if reading and not reading.stale:
            self._last_outdoor_humidity = reading
            return reading

        # 2. Current-hour forecast humidity
        if forecast_snapshot:
            now = self._now()
            closest = min(
                forecast_snapshot,
                key=lambda pt: abs((pt.time - now).total_seconds()),
            )
            if (
                closest.humidity is not None
                and abs((closest.time - now).total_seconds()) < 7200
            ):
                return SensorReading(
                    value=closest.humidity,
                    source="forecast",
                    timestamp=closest.time,
                    stale=False,
                )

        # 3. Stale entity reading
        if reading and reading.stale:
            return reading

        # 4. Last-known value (any provenance)
        if self._last_outdoor_humidity is not None:
            return SensorReading(
                value=self._last_outdoor_humidity.value,
                source="last_known",
                timestamp=self._last_outdoor_humidity.timestamp,
                stale=True,
            )

        return None

    # ── Indoor temperature ────────────────────────────────────────────

    def _get_fresh_indoor_entity_values(
        self, exclude_entities: set[str] | None = None,
    ) -> list[float]:
        """Return fresh °F readings from each configured indoor temp entity.

        Used by the median outlier filter to get individual values before
        they are averaged together.  Stale values are excluded so that
        offline sensors don't influence the pool.
        """
        values: list[float] = []
        for eid in self._indoor_temp_entities:
            if exclude_entities and eid in exclude_entities:
                continue
            value, unit = self._read_entity_with_unit(eid, -80.0, 200.0, "Indoor temp")
            if value is None:
                continue
            if unit == UnitOfTemperature.CELSIUS or unit == "°C":
                value = TemperatureConverter.convert(
                    value, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
                )
            state = self.hass.states.get(eid)
            if not self._is_stale(state):
                values.append(value)
        return values

    def _median_filter_indoor_temps(
        self,
        entity_values: list[float],
        thermostat_temp: float | None,
        threshold_f: float,
    ) -> tuple[float | None, bool, str]:
        """Pool all indoor sensors, exclude outliers > threshold from median, average remainder.

        Returns (filtered_avg, thermostat_was_excluded, source_string).

        Requires ≥3 sensors for reliable outlier rejection; with fewer sensors,
        all values are returned unchanged so no sensor is wrongly discarded.

        Kitchen/stove note: a sensor near an active oven spikes as a HIGH outlier
        and is excluded by the same mechanism as thermostat blending — correct
        behaviour, since the spike is localised and should not bias the house average.
        """
        pool: list[float] = list(entity_values)
        if thermostat_temp is not None:
            pool.append(thermostat_temp)

        if not pool:
            return None, False, "no_sensors"

        if len(pool) < 3:
            # Insufficient sensors for majority-vote outlier rejection — use all.
            avg = sum(pool) / len(pool)
            return avg, False, f"average:{len(pool)}"

        median_val = statistics.median(pool)
        filtered = [v for v in pool if abs(v - median_val) <= threshold_f]
        if not filtered:
            filtered = pool  # safety fallback: never discard everything

        thermostat_excluded = (
            thermostat_temp is not None
            and abs(thermostat_temp - median_val) > threshold_f
        )
        avg = sum(filtered) / len(filtered)
        excluded_count = len(pool) - len(filtered)
        source = f"median_filtered:{len(filtered)}"
        if excluded_count:
            source += f"(-{excluded_count})"
        return avg, thermostat_excluded, source

    def read_indoor_temp(
        self,
        thermostat_temp: float | None = None,
    ) -> SensorReading | None:
        """Indoor temp: entity average → thermostat.

        Applies EMA smoothing to reduce noise-driven setpoint corrections.
        """
        raw_value: float | None = None
        source = "thermostat"
        sensor_count = 1

        # ── Humidity-gated squelching (wet rooms)
        squelched = self._get_squelched_entities()

        # ── Median-filter mode (requires ≥3 sensors for reliable outlier rejection)
        if self._blend_mode == BLEND_MODE_MEDIAN and self._indoor_temp_entities:
            entity_values = self._get_fresh_indoor_entity_values(
                exclude_entities=squelched,
            )
            filtered_avg, excluded, filter_source = self._median_filter_indoor_temps(
                entity_values, thermostat_temp, self._blend_outlier_threshold_f
            )
            self.thermostat_blend_suspected = excluded
            if excluded:
                _LOGGER.debug(
                    "Thermostat excluded by median filter: "
                    "thermostat=%.1f°F, median=%.1f°F, threshold=%.1f°F",
                    thermostat_temp,
                    statistics.median(entity_values + ([thermostat_temp] if thermostat_temp is not None else [])),
                    self._blend_outlier_threshold_f,
                )
            if filtered_avg is not None:
                self._ema_indoor_temp = self._apply_ema(
                    filtered_avg, self._ema_indoor_temp, self._ema_alpha
                )
                total_sensors = len(entity_values) + (1 if thermostat_temp is not None else 0)
                return SensorReading(
                    value=self._ema_indoor_temp,
                    source=filter_source,
                    timestamp=self._now(),
                    stale=False,
                    sensor_count=total_sensors,
                )
            # Fall through if no sensors returned

        # ── Standard mode (occupancy/schedule exclusion handled by caller via thermostat_temp=None)
        # 1. Additional room sensors
        reading = self._read_multi_temp(
            self._indoor_temp_entities, "Indoor temp",
            exclude_entities=squelched,
        )
        if reading and not reading.stale:
            # If thermostat also available, include it in the average
            if thermostat_temp is not None:
                count = reading.sensor_count
                total = reading.value * count + thermostat_temp
                raw_value = total / (count + 1)
                source = f"average:{count + 1}"
                sensor_count = count + 1
            else:
                raw_value = reading.value
                source = reading.source
                sensor_count = reading.sensor_count
        elif thermostat_temp is not None:
            # 2. Thermostat only
            raw_value = thermostat_temp

        if raw_value is None:
            return None

        # Apply EMA smoothing
        self._ema_indoor_temp = self._apply_ema(
            raw_value, self._ema_indoor_temp, self._ema_alpha
        )

        return SensorReading(
            value=self._ema_indoor_temp,
            source=source,
            timestamp=self._now(),
            stale=False,
            sensor_count=sensor_count,
        )

    # ── Indoor humidity ───────────────────────────────────────────────

    def read_indoor_humidity(
        self,
        thermostat_humidity: float | None = None,
    ) -> SensorReading | None:
        """Indoor humidity: entity average → thermostat."""
        squelched = self._current_squelched or self._get_squelched_entities()
        reading = self._read_multi_humidity(
            self._indoor_humidity_entities, "Indoor humidity",
            exclude_entities=squelched,
        )
        if reading and not reading.stale:
            return reading

        if thermostat_humidity is not None:
            return SensorReading(
                value=thermostat_humidity,
                source="thermostat",
                timestamp=self._now(),
                stale=False,
            )

        return None

    # ── Room-aware weighted reads ──────────────────────────────────

    def set_area_manager(self, manager: AreaOccupancyManager | None) -> None:
        """Attach an AreaOccupancyManager for weighted indoor reads."""
        self._area_manager = manager

    def read_weighted_indoor_temp(
        self,
        thermostat_temp: float | None = None,
    ) -> SensorReading | None:
        """Indoor temp using occupancy-weighted area averaging.

        Falls back to read_indoor_temp() if no area manager is set or
        if the weighted computation fails.
        """
        if self._area_manager is None:
            return self.read_indoor_temp(thermostat_temp)

        value, source = self._area_manager.get_weighted_indoor_temp(thermostat_temp)
        if value is None:
            return self.read_indoor_temp(thermostat_temp)

        return SensorReading(
            value=value,
            source=source,
            timestamp=self._now(),
            stale=False,
        )

    def read_weighted_indoor_humidity(
        self,
        thermostat_humidity: float | None = None,
    ) -> SensorReading | None:
        """Indoor humidity using occupancy-weighted area averaging.

        Falls back to read_indoor_humidity() if no area manager is set or
        if the weighted computation fails.
        """
        if self._area_manager is None:
            return self.read_indoor_humidity(thermostat_humidity)

        value, source = self._area_manager.get_weighted_indoor_humidity(
            thermostat_humidity
        )
        if value is None:
            return self.read_indoor_humidity(thermostat_humidity)

        return SensorReading(
            value=value,
            source=source,
            timestamp=self._now(),
            stale=False,
        )

    # ── Wind speed ───────────────────────────────────────────────────

    def read_wind_speed(
        self,
        forecast_snapshot: list[ForecastPoint] | None = None,
    ) -> SensorReading | None:
        """Wind speed in mph: entity → forecast."""
        if self._wind_speed_entity:
            value, unit = self._read_entity_with_unit(
                self._wind_speed_entity, 0.0, 200.0, "Wind speed"
            )
            if value is not None:
                # Convert to mph
                if unit in (
                    UnitOfSpeed.KILOMETERS_PER_HOUR,
                    "km/h",
                ):
                    value = SpeedConverter.convert(
                        value,
                        UnitOfSpeed.KILOMETERS_PER_HOUR,
                        UnitOfSpeed.MILES_PER_HOUR,
                    )
                elif unit in (UnitOfSpeed.METERS_PER_SECOND, "m/s"):
                    value = SpeedConverter.convert(
                        value,
                        UnitOfSpeed.METERS_PER_SECOND,
                        UnitOfSpeed.MILES_PER_HOUR,
                    )
                state = self.hass.states.get(self._wind_speed_entity)
                return SensorReading(
                    value=value,
                    source=f"entity:{self._wind_speed_entity}",
                    timestamp=self._now(),
                    stale=self._is_stale(state),
                )

        # Fallback to forecast
        if forecast_snapshot:
            now = self._now()
            closest = min(
                forecast_snapshot,
                key=lambda pt: abs((pt.time - now).total_seconds()),
            )
            if (
                closest.wind_speed_mph is not None
                and abs((closest.time - now).total_seconds()) < 7200
            ):
                return SensorReading(
                    value=closest.wind_speed_mph,
                    source="forecast",
                    timestamp=closest.time,
                    stale=False,
                )

        return None

    # ── Solar irradiance ──────────────────────────────────────────────

    def read_solar_irradiance(self) -> SensorReading | None:
        """Direct solar irradiance in W/m². No fallback — entity or None."""
        if not self._solar_irradiance_entity:
            return None
        value = self._read_entity(
            self._solar_irradiance_entity, 0.0, 1500.0, "Solar irradiance"
        )
        if value is None:
            return None
        state = self.hass.states.get(self._solar_irradiance_entity)
        return SensorReading(
            value=value,
            source=f"entity:{self._solar_irradiance_entity}",
            timestamp=self._now(),
            stale=self._is_stale(state),
        )

    # ── UV index ────────────────────────────────────────────────────────

    def read_uv_index(self) -> SensorReading | None:
        """UV index from weather integration (e.g. OpenWeatherMap). No fallback."""
        if not self._uv_index_entity:
            return None
        value = self._read_entity(
            self._uv_index_entity, 0.0, 15.0, "UV index"
        )
        if value is None:
            return None
        state = self.hass.states.get(self._uv_index_entity)
        return SensorReading(
            value=value,
            source=f"entity:{self._uv_index_entity}",
            timestamp=self._now(),
            stale=self._is_stale(state),
        )

    # ── Barometric pressure ───────────────────────────────────────────

    def read_barometric_pressure(self) -> SensorReading | None:
        """Barometric pressure in hPa. Auto-converts inHg/mbar/psi."""
        if not self._barometric_pressure_entity:
            return None
        state = self.hass.states.get(self._barometric_pressure_entity)
        if not state or state.state in ("unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        unit = state.attributes.get("unit_of_measurement")

        # Convert to hPa before validation
        if unit in (UnitOfPressure.INHG, "inHg"):
            value = PressureConverter.convert(
                value, UnitOfPressure.INHG, UnitOfPressure.HPA
            )
        elif unit in (UnitOfPressure.MBAR, "mbar"):
            pass  # mbar == hPa
        elif unit in (UnitOfPressure.PSI, "psi"):
            value = PressureConverter.convert(
                value, UnitOfPressure.PSI, UnitOfPressure.HPA
            )
        # else assume hPa

        # Validate after conversion to hPa
        if value < 500.0 or value > 1200.0:
            _LOGGER.warning(
                "Barometric pressure sensor %s value %.2f hPa outside valid range "
                "[500.0, 1200.0] — ignoring",
                self._barometric_pressure_entity, value,
            )
            return None

        return SensorReading(
            value=value,
            source=f"entity:{self._barometric_pressure_entity}",
            timestamp=self._now(),
            stale=self._is_stale(state),
        )

    # ── Sun position ──────────────────────────────────────────────────

    def read_sun_elevation(self) -> float | None:
        """Sun elevation in degrees from configured entity."""
        state = self.hass.states.get(self._sun_entity)
        if state is None:
            return None
        try:
            return float(state.attributes.get("elevation", 0))
        except (ValueError, TypeError):
            return None

    def read_sun_azimuth(self) -> float | None:
        """Sun azimuth in degrees from configured entity."""
        state = self.hass.states.get(self._sun_entity)
        if state is None:
            return None
        try:
            return float(state.attributes.get("azimuth", 0))
        except (ValueError, TypeError):
            return None

    # ── Door/window contact sensors ─────────────────────────────────────

    def read_door_window_open_count(self) -> tuple[int, int]:
        """Count open doors/windows with debouncing.

        A door/window must remain in its new state for at least
        ``_dw_debounce_seconds`` before the change is accepted. This
        prevents brief events (letting the dog out, passing through)
        from triggering infiltration penalties or pausing EKF learning.

        Returns:
            Tuple of (debounced_open_count, total_configured).
            If no entities configured, returns (0, 0).
        """
        if not self._door_window_entities:
            return 0, 0

        now = self._now()
        open_count = 0
        total = 0

        for eid in self._door_window_entities:
            state = self.hass.states.get(eid)
            if not state or state.state in ("unknown", "unavailable"):
                continue

            total += 1
            raw_open = state.state == "on"  # binary_sensor: on = open

            # Initialize debounced state on first encounter
            if eid not in self._dw_debounced:
                self._dw_debounced[eid] = raw_open

            if raw_open != self._dw_debounced[eid]:
                # State differs from debounced — track when it first changed
                if eid not in self._dw_last_raw or self._dw_last_raw[eid][0] != raw_open:
                    # First time seeing this new state — start debounce timer
                    self._dw_last_raw[eid] = (raw_open, now)
                else:
                    # Still in the new state — check if debounce period elapsed
                    _, change_time = self._dw_last_raw[eid]
                    elapsed = (now - change_time).total_seconds()
                    if elapsed >= self._dw_debounce_seconds:
                        _LOGGER.debug(
                            "Door/window %s confirmed %s after %ds debounce",
                            eid, "open" if raw_open else "closed", int(elapsed),
                        )
                        self._dw_debounced[eid] = raw_open
                        del self._dw_last_raw[eid]
            else:
                # Raw matches debounced — clear any pending timer
                self._dw_last_raw.pop(eid, None)

            if self._dw_debounced[eid]:
                open_count += 1

        return open_count, total

    # ── Buffer zone temperatures ──────────────────────────────────────

    def read_attic_temp(self) -> SensorReading | None:
        """Attic temperature in °F. Returns None if not configured or unavailable."""
        if not self._attic_temp_entity:
            return None
        reading = self._read_multi_temp([self._attic_temp_entity], "Attic temp")
        return reading

    def read_crawlspace_temp(self) -> SensorReading | None:
        """Crawlspace temperature in °F. Returns None if not configured or unavailable."""
        if not self._crawlspace_temp_entity:
            return None
        reading = self._read_multi_temp([self._crawlspace_temp_entity], "Crawlspace temp")
        return reading

    # ── Energy / solar production ─────────────────────────────────────

    def read_solar_production(self) -> SensorReading | None:
        """Solar panel production in watts."""
        if not self._solar_production_entity:
            return None
        value = self._read_entity(
            self._solar_production_entity, 0.0, 100000.0, "Solar production"
        )
        if value is None:
            return None
        state = self.hass.states.get(self._solar_production_entity)
        return SensorReading(
            value=value,
            source=f"entity:{self._solar_production_entity}",
            timestamp=self._now(),
            stale=self._is_stale(state),
        )

    def read_grid_import(self) -> SensorReading | None:
        """Net grid import in watts (negative = exporting)."""
        if not self._grid_import_entity:
            return None
        value = self._read_entity(
            self._grid_import_entity, -100000.0, 100000.0, "Grid import"
        )
        if value is None:
            return None
        state = self.hass.states.get(self._grid_import_entity)
        return SensorReading(
            value=value,
            source=f"entity:{self._grid_import_entity}",
            timestamp=self._now(),
            stale=self._is_stale(state),
        )

    def read_solar_export_rate(self) -> float | None:
        """Solar export/feed-in tariff rate in $/kWh."""
        if not self._solar_export_rate_entity:
            return None
        return self._read_entity(
            self._solar_export_rate_entity, 0.0, 10.0, "Solar export rate"
        )

    def derive_solar_irradiance_from_panels(
        self,
        panel_area_m2: float | None,
        panel_efficiency: float | None,
    ) -> float | None:
        """Derive solar irradiance (W/m²) from panel production, area, and efficiency.

        Returns None if production is unavailable or panel specs are missing.
        Formula: irradiance = production_watts / (panel_area_m2 * efficiency)
        """
        if not panel_area_m2 or not panel_efficiency or panel_area_m2 <= 0:
            return None
        production = self.read_solar_production()
        if production is None or production.stale or production.value <= 0:
            return None
        irradiance = production.value / (panel_area_m2 * panel_efficiency)
        # Clamp to reasonable range (0-1500 W/m²)
        return min(1500.0, irradiance)

    # ── Migrated sensor reads (from coordinator) ──────────────────────

    def read_power_draw(self) -> float | None:
        """HVAC power draw. Entity → default watts fallback.

        When the configured power entity is unavailable, falls back to default
        watts so savings tracking continues during transient sensor outages.
        """
        if self._power_entity:
            value = self._read_entity(
                self._power_entity, 0.0, 100000.0, "HVAC power"
            )
            if value is not None:
                return value
            # Entity configured but unavailable — fall back to default
            if self._power_default_watts:
                _LOGGER.debug(
                    "HVAC power entity unavailable, using default %dW",
                    self._power_default_watts,
                )
                return self._power_default_watts
            return None
        return self._power_default_watts

    def read_co2_intensity(self) -> float | None:
        """CO2 grid intensity in gCO2/kWh."""
        if not self._co2_entity:
            return None
        return self._read_entity(self._co2_entity, 0.0, 2000.0, "CO2 intensity")

    def read_electricity_rate(self) -> float | None:
        """Electricity rate in $/kWh. Entity → flat rate."""
        if self._rate_entity:
            value = self._read_entity(
                self._rate_entity, 0.0, 10.0, "Electricity rate"
            )
            if value is not None:
                return value
        return self._flat_rate

    def read_net_power_draw(self, hvac_power: float | None) -> float | None:
        """Net HVAC power draw after solar offset.

        If solar production is available, reduces the HVAC power by surplus solar
        (assumes solar offsets HVAC consumption first). Returns gross power if
        no solar data available.
        """
        if hvac_power is None:
            return None

        solar = self.read_solar_production()
        if solar is None or solar.stale:
            return hvac_power

        # Solar surplus = production that exceeds non-HVAC household load
        # Simplified: assume all solar can offset HVAC (conservative estimate)
        net = max(0.0, hvac_power - solar.value)
        return net

    # ── Forecast correction ───────────────────────────────────────────

    def correct_current_forecast(
        self,
        forecast: list[ForecastPoint],
    ) -> list[ForecastPoint]:
        """Replace current hour's forecast with ground-truth sensor readings.

        Only modifies the closest forecast point to now. Returns the same list
        (mutated). No-op if no standalone sensors are configured.
        """
        if not forecast:
            return forecast

        now = self._now()
        closest_idx = min(
            range(len(forecast)),
            key=lambda i: abs((forecast[i].time - now).total_seconds()),
        )
        closest = forecast[closest_idx]

        # Only correct if within 1 hour of now
        if abs((closest.time - now).total_seconds()) > 3600:
            return forecast

        # Outdoor temperature
        temp_reading = self._read_multi_temp(
            self._outdoor_temp_entities, "Outdoor temp"
        )
        if temp_reading and not temp_reading.stale:
            closest.outdoor_temp = temp_reading.value

        # Outdoor humidity
        humidity_reading = self._read_multi_humidity(
            self._outdoor_humidity_entities, "Outdoor humidity"
        )
        if humidity_reading and not humidity_reading.stale:
            closest.humidity = humidity_reading.value

        # Wind speed
        if self._wind_speed_entity:
            wind = self.read_wind_speed()
            if wind and not wind.stale and wind.source.startswith("entity:"):
                closest.wind_speed_mph = wind.value

        # Solar irradiance
        irradiance = self.read_solar_irradiance()
        if irradiance and not irradiance.stale:
            closest.solar_irradiance_w_m2 = irradiance.value

        return forecast

    # ── Diagnostic info ──────────────────────────────────────────────

    def get_outdoor_temp_info(
        self,
        forecast_snapshot: list[ForecastPoint] | None = None,
    ) -> dict:
        """Diagnostic info about the outdoor temperature reading."""
        reading = self.read_outdoor_temp(forecast_snapshot)
        return {
            "value": round(reading.value, 1) if reading else None,
            "source": reading.source if reading else "unavailable",
            "stale": reading.stale if reading else True,
            "entity_count": len(self._outdoor_temp_entities),
            "entities": self._outdoor_temp_entities,
        }

    def get_indoor_temp_info(
        self,
        thermostat_temp: float | None = None,
    ) -> dict:
        """Diagnostic info about the indoor temperature reading."""
        reading = self.read_indoor_temp(thermostat_temp)
        return {
            "value": round(reading.value, 1) if reading else None,
            "source": reading.source if reading else "unavailable",
            "stale": reading.stale if reading else True,
            "entity_count": len(self._indoor_temp_entities),
            "entities": self._indoor_temp_entities,
        }
