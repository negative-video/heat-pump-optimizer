"""Room-aware occupancy-weighted indoor sensing.

Tracks per-room occupancy via motion/occupancy sensors assigned to HA areas,
then provides weighted temperature and humidity readings that reflect what
occupants actually feel rather than a flat whole-house average.

This adapter works alongside (not replaces) the whole-home OccupancyAdapter.
Whole-home occupancy controls comfort range widening (home/away/vacation);
room-level occupancy controls sensor weighting within the "home" state.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import TemperatureConverter

from ..const import (
    CONF_AREA_SENSOR_CONFIG,
    CONF_INDOOR_HUMIDITY_ENTITIES,
    CONF_INDOOR_TEMP_ENTITIES,
    DEFAULT_OCCUPIED_WEIGHT_MULTIPLIER,
    DEFAULT_ROOM_OCCUPANCY_DEBOUNCE_MINUTES,
    DEFAULT_SENSOR_STALE_MINUTES,
    DEFAULT_SPIKE_HISTORY_MINUTES,
    DEFAULT_SPIKE_HUMIDITY_THRESHOLD,
    DEFAULT_SPIKE_TEMP_THRESHOLD,
    DEFAULT_SPIKE_WINDOW_MINUTES,
    WEIGHTING_MODE_EQUAL,
    WEIGHTING_MODE_OCCUPIED_ONLY,
    WEIGHTING_MODE_WEIGHTED,
)
from ..engine.comfort import calculate_apparent_temperature
from ..engine.data_types import AreaSensorGroup, IndoorWeightingMode

_LOGGER = logging.getLogger(__name__)


@dataclass
class _SensorSnapshot:
    """A timestamped sensor reading for spike detection."""

    timestamp: datetime
    value: float


@dataclass
class _SensorHistory:
    """Rolling history for one sensor, used for spike detection."""

    readings: deque[_SensorSnapshot] = field(
        default_factory=lambda: deque(maxlen=120)  # ~30 min at 15s intervals
    )
    dampened: bool = False


class AreaOccupancyManager:
    """Manages per-room occupancy tracking and weighted sensor aggregation."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        weighting_mode: str = WEIGHTING_MODE_EQUAL,
        area_config: list[dict] | None = None,
        debounce_minutes: float = DEFAULT_ROOM_OCCUPANCY_DEBOUNCE_MINUTES,
        occupied_weight_multiplier: float = DEFAULT_OCCUPIED_WEIGHT_MULTIPLIER,
    ) -> None:
        self.hass = hass
        self._mode = IndoorWeightingMode(weighting_mode)
        self._debounce_minutes = debounce_minutes
        self._occupied_weight_multiplier = occupied_weight_multiplier

        # Build AreaSensorGroups from stored config
        self._areas: list[AreaSensorGroup] = []
        if area_config:
            for ac in area_config:
                self._areas.append(
                    AreaSensorGroup(
                        area_id=ac["area_id"],
                        area_name=ac["area_name"],
                        temp_entities=ac.get("temp_entities", []),
                        humidity_entities=ac.get("humidity_entities", []),
                        motion_entities=ac.get("motion_entities", []),
                    )
                )

        # Spike detection: keyed by entity_id
        self._sensor_history: dict[str, _SensorHistory] = {}

    @property
    def mode(self) -> IndoorWeightingMode:
        return self._mode

    @property
    def areas(self) -> list[AreaSensorGroup]:
        return list(self._areas)

    # ── Area discovery ───────────────────────────────────────────────

    @staticmethod
    async def async_discover_areas(
        hass: HomeAssistant,
        indoor_temp_entities: list[str] | None = None,
        indoor_humidity_entities: list[str] | None = None,
    ) -> list[AreaSensorGroup]:
        """Discover areas from HA registry that have relevant sensors.

        Cross-references the HA area and entity registries to find areas
        containing temperature/humidity sensors and motion/occupancy sensors.
        Only includes temperature/humidity entities that are already in the
        user's configured sensor lists (if provided).

        Returns a list of AreaSensorGroup with entities populated but no
        occupancy state (that's set at runtime via update_occupancy).
        """
        from homeassistant.helpers import (
            area_registry,
            device_registry,
            entity_registry,
        )

        area_reg = area_registry.async_get(hass)
        entity_reg = entity_registry.async_get(hass)
        device_reg = device_registry.async_get(hass)

        temp_set = set(indoor_temp_entities or [])
        humidity_set = set(indoor_humidity_entities or [])

        # Group entities by area
        # Entities can have a direct area_id, or inherit from their parent device
        area_entities: dict[str, dict[str, list[str]]] = {}
        for entry in entity_reg.entities.values():
            area_id = entry.area_id
            if area_id is None and entry.device_id:
                device = device_reg.async_get(entry.device_id)
                if device is not None:
                    area_id = device.area_id
            if area_id is None:
                continue
            if area_id not in area_entities:
                area_entities[area_id] = {
                    "temp": [],
                    "humidity": [],
                    "motion": [],
                }

            eid = entry.entity_id

            # Temperature sensors
            if (
                entry.domain == "sensor"
                and entry.original_device_class == "temperature"
                and (not temp_set or eid in temp_set)
            ):
                area_entities[area_id]["temp"].append(eid)

            # Humidity sensors
            elif (
                entry.domain == "sensor"
                and entry.original_device_class == "humidity"
                and (not humidity_set or eid in humidity_set)
            ):
                area_entities[area_id]["humidity"].append(eid)

            # Motion/occupancy sensors
            elif entry.domain == "binary_sensor" and entry.original_device_class in (
                "motion",
                "occupancy",
            ):
                area_entities[area_id]["motion"].append(eid)

        # Build AreaSensorGroups for areas that have at least a temp sensor
        result: list[AreaSensorGroup] = []
        for area_id, entities in area_entities.items():
            if not entities["temp"]:
                continue
            area = area_reg.async_get_area(area_id)
            if area is None:
                continue
            result.append(
                AreaSensorGroup(
                    area_id=area_id,
                    area_name=area.name,
                    temp_entities=entities["temp"],
                    humidity_entities=entities["humidity"],
                    motion_entities=entities["motion"],
                )
            )

        _LOGGER.info(
            "Discovered %d areas with temperature sensors: %s",
            len(result),
            [a.area_name for a in result],
        )
        return result

    # ── Occupancy tracking ───────────────────────────────────────────

    def update_occupancy(self) -> None:
        """Update per-room occupancy state from motion sensors.

        Called each coordinator cycle (every 5 minutes). Rules:
        - Any motion sensor 'on' -> room is occupied
        - All motion sensors 'off' but last_motion within debounce -> still occupied
        - Stale/unavailable motion sensor -> treated as occupied (graceful degradation)
        - No motion sensors for a room -> always treated as occupied
        """
        now = datetime.now(timezone.utc)

        for area in self._areas:
            if not area.motion_entities:
                # No motion sensors: include at baseline weight (treated as occupied)
                area.occupied = True
                continue

            any_on = False
            any_stale = False

            for eid in area.motion_entities:
                state = self.hass.states.get(eid)
                if state is None or state.state in ("unknown", "unavailable"):
                    any_stale = True
                    continue
                if state.state == "on":
                    any_on = True
                    area.last_motion = now

            if any_on:
                area.occupied = True
            elif any_stale:
                # Graceful degradation: stale sensor -> treat as occupied
                area.occupied = True
                _LOGGER.debug(
                    "Area '%s' has stale motion sensor — treating as occupied",
                    area.area_name,
                )
            elif area.last_motion is not None:
                # Debounce: keep occupied for N minutes after last motion
                elapsed = (now - area.last_motion).total_seconds() / 60.0
                area.occupied = elapsed < self._debounce_minutes
            else:
                area.occupied = False

    # ── Per-area sensor readings ─────────────────────────────────────

    def update_readings(self) -> None:
        """Read temperature and humidity for each area and compute apparent temp."""
        now = datetime.now(timezone.utc)

        for area in self._areas:
            area.current_temp = self._read_area_temp(area)
            area.current_humidity = self._read_area_humidity(area)

            # Record readings for spike detection
            for eid in area.temp_entities:
                val = self._read_single_temp(eid)
                if val is not None:
                    self._record_reading(eid, val, now)
            for eid in area.humidity_entities:
                val = self._read_single_humidity(eid)
                if val is not None:
                    self._record_reading(eid, val, now)

            # Compute apparent temp if both readings available
            if area.current_temp is not None and area.current_humidity is not None:
                area.current_apparent_temp = calculate_apparent_temperature(
                    area.current_temp, area.current_humidity
                )
            elif area.current_temp is not None:
                area.current_apparent_temp = area.current_temp
            else:
                area.current_apparent_temp = None

    def _read_single_temp(self, entity_id: str) -> float | None:
        """Read a single temperature entity, converting to °F."""
        state = self.hass.states.get(entity_id)
        if not state or state.state in ("unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        if value < -80.0 or value > 200.0:
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit == UnitOfTemperature.CELSIUS or unit == "°C":
            value = TemperatureConverter.convert(
                value, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
            )
        return value

    def _read_single_humidity(self, entity_id: str) -> float | None:
        """Read a single humidity entity."""
        state = self.hass.states.get(entity_id)
        if not state or state.state in ("unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        if value < 0.0 or value > 100.0:
            return None
        return value

    def _read_area_temp(self, area: AreaSensorGroup) -> float | None:
        """Average all valid temperature sensors in an area."""
        values = []
        for eid in area.temp_entities:
            val = self._read_single_temp(eid)
            if val is not None:
                values.append(val)
        return sum(values) / len(values) if values else None

    def _read_area_humidity(self, area: AreaSensorGroup) -> float | None:
        """Average all valid humidity sensors in an area."""
        values = []
        for eid in area.humidity_entities:
            val = self._read_single_humidity(eid)
            if val is not None:
                values.append(val)
        return sum(values) / len(values) if values else None

    # ── Spike detection ──────────────────────────────────────────────

    def _record_reading(
        self, entity_id: str, value: float, now: datetime
    ) -> None:
        """Record a sensor reading for spike detection history."""
        if entity_id not in self._sensor_history:
            self._sensor_history[entity_id] = _SensorHistory()
        history = self._sensor_history[entity_id]

        # Prune old readings beyond history window
        cutoff = now.timestamp() - DEFAULT_SPIKE_HISTORY_MINUTES * 60
        while history.readings and history.readings[0].timestamp.timestamp() < cutoff:
            history.readings.popleft()

        history.readings.append(_SensorSnapshot(timestamp=now, value=value))

    def _is_sensor_spiking(self, entity_id: str, now: datetime) -> bool:
        """Check if a sensor has experienced a rapid spike.

        Looks at readings within the spike window (default 10 min) and checks
        if the change exceeds thresholds for temp or humidity.
        """
        history = self._sensor_history.get(entity_id)
        if not history or len(history.readings) < 2:
            return False

        current = history.readings[-1]
        window_start = now.timestamp() - DEFAULT_SPIKE_WINDOW_MINUTES * 60

        # Find the oldest reading within the spike window
        oldest_in_window = None
        for reading in history.readings:
            if reading.timestamp.timestamp() >= window_start:
                oldest_in_window = reading
                break

        if oldest_in_window is None or oldest_in_window is current:
            return False

        change = abs(current.value - oldest_in_window.value)

        # Determine threshold: humidity sensors are 0-100, temp sensors are higher
        # Use humidity threshold if value is in humidity range
        if oldest_in_window.value <= 100.0 and current.value <= 100.0:
            # Could be humidity — check both thresholds, use the more lenient one
            # We can't definitively distinguish here, so check against both
            return change >= DEFAULT_SPIKE_HUMIDITY_THRESHOLD
        return change >= DEFAULT_SPIKE_TEMP_THRESHOLD

    def _check_all_spikes(self) -> dict[str, bool]:
        """Check all tracked sensors for spikes. Returns entity_id -> is_spiking."""
        now = datetime.now(timezone.utc)
        result: dict[str, bool] = {}
        for entity_id in self._sensor_history:
            result[entity_id] = self._is_sensor_spiking(entity_id, now)
        return result

    def _is_area_spiking(self, area: AreaSensorGroup, spike_map: dict[str, bool]) -> bool:
        """Check if any sensor in an area is currently spiking."""
        all_entities = area.temp_entities + area.humidity_entities
        return any(spike_map.get(eid, False) for eid in all_entities)

    # ── Weighted aggregation ─────────────────────────────────────────

    def get_area_weights(self) -> dict[str, float]:
        """Compute per-area weights based on occupancy, mode, and spike state.

        Returns a dict mapping area_id -> weight. A weight of 0.0 means the
        area is excluded from the average.
        """
        spike_map = self._check_all_spikes()
        weights: dict[str, float] = {}

        for area in self._areas:
            # Start with mode-based weight
            if self._mode == IndoorWeightingMode.EQUAL:
                weight = 1.0
            elif self._mode == IndoorWeightingMode.OCCUPIED_ONLY:
                weight = 1.0 if area.occupied else 0.0
            else:  # WEIGHTED
                weight = self._occupied_weight_multiplier if area.occupied else 1.0

            # Dampen spiking areas
            if self._is_area_spiking(area, spike_map):
                weight *= 0.05  # near-zero but not exactly zero
                _LOGGER.debug(
                    "Area '%s' has spiking sensor — dampening weight to %.2f",
                    area.area_name,
                    weight,
                )

            weights[area.area_id] = weight

        # Fallback: if all weights are zero (occupied_only with no one detected),
        # revert to equal weighting to avoid no-data situation
        if all(w == 0.0 for w in weights.values()) and weights:
            _LOGGER.debug(
                "All area weights are zero — falling back to equal weighting"
            )
            for area_id in weights:
                weights[area_id] = 1.0

        return weights

    def get_weighted_indoor_temp(
        self, thermostat_temp: float | None = None
    ) -> tuple[float | None, str]:
        """Compute weighted indoor temperature across all areas.

        Args:
            thermostat_temp: The thermostat's own temperature reading.
                Included at weight 1.0 as it represents the zone the HVAC
                directly controls.

        Returns:
            (weighted_temp, source_string) or (None, "") if no data.
        """
        weights = self.get_area_weights()

        total = 0.0
        total_weight = 0.0
        n_occupied = 0
        n_total = 0

        for area in self._areas:
            if area.current_temp is None:
                continue
            w = weights.get(area.area_id, 0.0)
            total += area.current_temp * w
            total_weight += w
            n_total += 1
            if area.occupied:
                n_occupied += 1

        # Include thermostat reading at weight 1.0
        if thermostat_temp is not None:
            total += thermostat_temp * 1.0
            total_weight += 1.0

        if total_weight == 0.0:
            return None, ""

        avg = total / total_weight
        source = f"weighted:{n_occupied}/{n_total}"
        return avg, source

    def get_weighted_indoor_humidity(
        self, thermostat_humidity: float | None = None
    ) -> tuple[float | None, str]:
        """Compute weighted indoor humidity across all areas.

        Args:
            thermostat_humidity: The thermostat's own humidity reading.

        Returns:
            (weighted_humidity, source_string) or (None, "") if no data.
        """
        weights = self.get_area_weights()

        total = 0.0
        total_weight = 0.0
        n_occupied = 0
        n_total = 0

        for area in self._areas:
            if area.current_humidity is None:
                continue
            w = weights.get(area.area_id, 0.0)
            total += area.current_humidity * w
            total_weight += w
            n_total += 1
            if area.occupied:
                n_occupied += 1

        if thermostat_humidity is not None:
            total += thermostat_humidity * 1.0
            total_weight += 1.0

        if total_weight == 0.0:
            return None, ""

        avg = total / total_weight
        source = f"weighted:{n_occupied}/{n_total}"
        return avg, source

    def get_diagnostics(self) -> list[dict]:
        """Return per-area diagnostic info for sensor attributes."""
        weights = self.get_area_weights()
        return [
            {
                "area_id": area.area_id,
                "area_name": area.area_name,
                "occupied": area.occupied,
                "weight": weights.get(area.area_id, 0.0),
                "temp": area.current_temp,
                "humidity": area.current_humidity,
                "apparent_temp": area.current_apparent_temp,
                "motion_entities": area.motion_entities,
                "temp_entities": area.temp_entities,
                "humidity_entities": area.humidity_entities,
            }
            for area in self._areas
        ]

    @staticmethod
    def serialize_area_config(areas: list[AreaSensorGroup]) -> str:
        """Serialize area config to JSON string for storage in config entry."""
        return json.dumps(
            [
                {
                    "area_id": a.area_id,
                    "area_name": a.area_name,
                    "temp_entities": a.temp_entities,
                    "humidity_entities": a.humidity_entities,
                    "motion_entities": a.motion_entities,
                }
                for a in areas
            ]
        )

    @staticmethod
    def deserialize_area_config(config_json: str) -> list[dict]:
        """Deserialize area config from JSON string."""
        return json.loads(config_json) if config_json else []
