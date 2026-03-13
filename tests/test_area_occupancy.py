"""Tests for the AreaOccupancyManager — room-level occupancy and weighted sensing."""

import importlib
import importlib.util
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

# ── HA stubs ────────────────────────────────────────────────────────
# Must be set up BEFORE loading modules via importlib.

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, PROJECT_ROOT)

CC = os.path.join(PROJECT_ROOT, "custom_components", "heatpump_optimizer")

# Package stubs
if "custom_components" not in sys.modules:
    pkg = types.ModuleType("custom_components")
    pkg.__path__ = [os.path.join(PROJECT_ROOT, "custom_components")]
    sys.modules["custom_components"] = pkg

if "custom_components.heatpump_optimizer" not in sys.modules:
    ho = types.ModuleType("custom_components.heatpump_optimizer")
    ho.__path__ = [CC]
    sys.modules["custom_components.heatpump_optimizer"] = ho
else:
    ho = sys.modules["custom_components.heatpump_optimizer"]

if "custom_components.heatpump_optimizer.adapters" not in sys.modules:
    adapters_pkg = types.ModuleType("custom_components.heatpump_optimizer.adapters")
    adapters_pkg.__path__ = [os.path.join(CC, "adapters")]
    sys.modules["custom_components.heatpump_optimizer.adapters"] = adapters_pkg
else:
    adapters_pkg = sys.modules["custom_components.heatpump_optimizer.adapters"]

if "custom_components.heatpump_optimizer.engine" not in sys.modules:
    engine_pkg = types.ModuleType("custom_components.heatpump_optimizer.engine")
    engine_pkg.__path__ = [os.path.join(CC, "engine")]
    sys.modules["custom_components.heatpump_optimizer.engine"] = engine_pkg
else:
    engine_pkg = sys.modules["custom_components.heatpump_optimizer.engine"]

# ── Stub homeassistant modules ──────────────────────────────────────

if "homeassistant" not in sys.modules:
    sys.modules["homeassistant"] = types.ModuleType("homeassistant")
    sys.modules["homeassistant"].__path__ = ["homeassistant"]

if "homeassistant.core" not in sys.modules:
    ha_core = types.ModuleType("homeassistant.core")
    ha_core.HomeAssistant = MagicMock
    sys.modules["homeassistant.core"] = ha_core

if "homeassistant.const" not in sys.modules:
    ha_const = types.ModuleType("homeassistant.const")
    ha_const.UnitOfTemperature = type("UnitOfTemperature", (), {
        "CELSIUS": "°C",
        "FAHRENHEIT": "°F",
    })
    ha_const.UnitOfSpeed = type("UnitOfSpeed", (), {
        "KILOMETERS_PER_HOUR": "km/h",
        "MILES_PER_HOUR": "mph",
        "METERS_PER_SECOND": "m/s",
    })
    ha_const.UnitOfPressure = type("UnitOfPressure", (), {
        "HPA": "hPa",
        "INHG": "inHg",
        "MBAR": "mbar",
        "PSI": "psi",
    })
    sys.modules["homeassistant.const"] = ha_const

if "homeassistant.util" not in sys.modules:
    ha_util = types.ModuleType("homeassistant.util")
    ha_util.__path__ = ["homeassistant/util"]
    sys.modules["homeassistant.util"] = ha_util

if "homeassistant.util.unit_conversion" not in sys.modules:
    ha_unit_conv = types.ModuleType("homeassistant.util.unit_conversion")

    class _TemperatureConverter:
        @staticmethod
        def convert(value, from_unit, to_unit):
            if from_unit == "°C" and to_unit == "°F":
                return value * 9.0 / 5.0 + 32.0
            if from_unit == "°F" and to_unit == "°C":
                return (value - 32.0) * 5.0 / 9.0
            return value

    class _SpeedConverter:
        @staticmethod
        def convert(value, from_unit, to_unit):
            TO_MS = {"km/h": 1 / 3.6, "mph": 0.44704, "m/s": 1.0}
            FROM_MS = {"km/h": 3.6, "mph": 1 / 0.44704, "m/s": 1.0}
            ms = value * TO_MS.get(from_unit, 1.0)
            return ms * FROM_MS.get(to_unit, 1.0)

    class _PressureConverter:
        @staticmethod
        def convert(value, from_unit, to_unit):
            TO_HPA = {"inHg": 33.8639, "psi": 68.9476, "hPa": 1.0, "mbar": 1.0}
            FROM_HPA = {"inHg": 1 / 33.8639, "psi": 1 / 68.9476, "hPa": 1.0, "mbar": 1.0}
            hpa = value * TO_HPA.get(from_unit, 1.0)
            return hpa * FROM_HPA.get(to_unit, 1.0)

    ha_unit_conv.TemperatureConverter = _TemperatureConverter
    ha_unit_conv.SpeedConverter = _SpeedConverter
    ha_unit_conv.PressureConverter = _PressureConverter
    sys.modules["homeassistant.util.unit_conversion"] = ha_unit_conv

# Stub homeassistant.helpers modules (needed for area/entity registry)
if "homeassistant.helpers" not in sys.modules:
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_helpers.__path__ = ["homeassistant/helpers"]
    sys.modules["homeassistant.helpers"] = ha_helpers

if "homeassistant.helpers.area_registry" not in sys.modules:
    ha_area_reg = types.ModuleType("homeassistant.helpers.area_registry")
    ha_area_reg.async_get = MagicMock()
    sys.modules["homeassistant.helpers.area_registry"] = ha_area_reg

if "homeassistant.helpers.entity_registry" not in sys.modules:
    ha_entity_reg = types.ModuleType("homeassistant.helpers.entity_registry")
    ha_entity_reg.async_get = MagicMock()
    sys.modules["homeassistant.helpers.entity_registry"] = ha_entity_reg

if "homeassistant.helpers.device_registry" not in sys.modules:
    ha_device_reg = types.ModuleType("homeassistant.helpers.device_registry")
    ha_device_reg.async_get = MagicMock()
    sys.modules["homeassistant.helpers.device_registry"] = ha_device_reg


# ── Load actual modules via importlib ───────────────────────────────

def _load(full_name: str, path: str):
    spec = importlib.util.spec_from_file_location(full_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


# const
const_mod = _load(
    "custom_components.heatpump_optimizer.const",
    os.path.join(CC, "const.py"),
)
ho.const = const_mod

# comfort (needed for apparent temp)
comfort_mod = _load(
    "custom_components.heatpump_optimizer.engine.comfort",
    os.path.join(CC, "engine", "comfort.py"),
)
engine_pkg.comfort = comfort_mod

# data_types
dt_mod = _load(
    "custom_components.heatpump_optimizer.engine.data_types",
    os.path.join(CC, "engine", "data_types.py"),
)
engine_pkg.data_types = dt_mod

# area_occupancy itself
ao_mod = _load(
    "custom_components.heatpump_optimizer.adapters.area_occupancy",
    os.path.join(CC, "adapters", "area_occupancy.py"),
)
adapters_pkg.area_occupancy = ao_mod

AreaOccupancyManager = ao_mod.AreaOccupancyManager
AreaSensorGroup = dt_mod.AreaSensorGroup
IndoorWeightingMode = dt_mod.IndoorWeightingMode


# ── Helpers ─────────────────────────────────────────────────────────

def _make_state(value, unit=None, age_minutes=0, attributes=None):
    """Create a mock HA state object."""
    s = MagicMock()
    s.state = str(value)
    attrs = attributes or {}
    if unit is not None:
        attrs["unit_of_measurement"] = unit
    s.attributes = attrs
    s.last_updated = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return s


def _make_hass(state_map=None):
    """Create a mock hass where hass.states.get(eid) returns from state_map."""
    hass = MagicMock()
    state_map = state_map or {}
    hass.states.get = MagicMock(side_effect=lambda eid: state_map.get(eid))
    return hass


def _make_area_config(areas):
    """Build area config dicts from simple specs."""
    result = []
    for a in areas:
        result.append({
            "area_id": a["id"],
            "area_name": a["name"],
            "temp_entities": a.get("temp", []),
            "humidity_entities": a.get("humidity", []),
            "motion_entities": a.get("motion", []),
        })
    return result


# ═══════════════════════════════════════════════════════════════════
# Tests — Occupancy Tracking
# ═══════════════════════════════════════════════════════════════════

class TestOccupancyTracking:
    """Test per-room occupancy detection."""

    def test_motion_on_marks_occupied(self):
        """A room with active motion sensor is marked occupied."""
        hass = _make_hass({
            "binary_sensor.living_room_motion": _make_state("on"),
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="weighted",
            area_config=_make_area_config([{
                "id": "lr", "name": "Living Room",
                "temp": ["sensor.lr_temp"],
                "motion": ["binary_sensor.living_room_motion"],
            }]),
        )
        mgr.update_occupancy()
        assert mgr.areas[0].occupied is True

    def test_motion_off_marks_unoccupied_after_debounce(self):
        """A room with all motion sensors off and no recent motion is unoccupied."""
        hass = _make_hass({
            "binary_sensor.bedroom_motion": _make_state("off"),
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="weighted",
            area_config=_make_area_config([{
                "id": "br", "name": "Bedroom",
                "temp": ["sensor.br_temp"],
                "motion": ["binary_sensor.bedroom_motion"],
            }]),
            debounce_minutes=5,
        )
        mgr.update_occupancy()
        assert mgr.areas[0].occupied is False

    def test_debounce_keeps_room_occupied(self):
        """A room stays occupied during the debounce window after motion stops."""
        hass = _make_hass({
            "binary_sensor.bedroom_motion": _make_state("on"),
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="weighted",
            area_config=_make_area_config([{
                "id": "br", "name": "Bedroom",
                "temp": ["sensor.br_temp"],
                "motion": ["binary_sensor.bedroom_motion"],
            }]),
            debounce_minutes=10,
        )
        # First: motion detected
        mgr.update_occupancy()
        assert mgr.areas[0].occupied is True

        # Now motion goes off, but within debounce
        hass.states.get = MagicMock(side_effect=lambda eid: _make_state("off"))
        mgr.update_occupancy()
        assert mgr.areas[0].occupied is True  # still within debounce

    def test_stale_motion_sensor_treated_as_occupied(self):
        """Unavailable motion sensor → room treated as occupied (graceful degradation)."""
        hass = _make_hass({
            "binary_sensor.kitchen_motion": _make_state("unavailable"),
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="weighted",
            area_config=_make_area_config([{
                "id": "kit", "name": "Kitchen",
                "temp": ["sensor.kit_temp"],
                "motion": ["binary_sensor.kitchen_motion"],
            }]),
        )
        mgr.update_occupancy()
        assert mgr.areas[0].occupied is True

    def test_no_motion_sensors_treated_as_occupied(self):
        """Rooms without motion sensors are always treated as occupied."""
        hass = _make_hass({})
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="weighted",
            area_config=_make_area_config([{
                "id": "hall", "name": "Hallway",
                "temp": ["sensor.hall_temp"],
                "motion": [],
            }]),
        )
        mgr.update_occupancy()
        assert mgr.areas[0].occupied is True

    def test_multiple_motion_sensors_any_on(self):
        """If any motion sensor in a room is on, the room is occupied."""
        hass = _make_hass({
            "binary_sensor.lr_motion_1": _make_state("off"),
            "binary_sensor.lr_motion_2": _make_state("on"),
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="weighted",
            area_config=_make_area_config([{
                "id": "lr", "name": "Living Room",
                "temp": ["sensor.lr_temp"],
                "motion": ["binary_sensor.lr_motion_1", "binary_sensor.lr_motion_2"],
            }]),
        )
        mgr.update_occupancy()
        assert mgr.areas[0].occupied is True


# ═══════════════════════════════════════════════════════════════════
# Tests — Weighting Modes
# ═══════════════════════════════════════════════════════════════════

class TestWeightingModes:
    """Test area weight calculations for all three modes."""

    def _two_room_manager(self, mode, occupied_rooms=None):
        """Create a manager with 2 rooms, with specified rooms occupied."""
        hass = _make_hass({})
        area_config = _make_area_config([
            {"id": "lr", "name": "Living Room", "temp": ["sensor.lr_temp"], "motion": ["binary_sensor.lr_motion"]},
            {"id": "br", "name": "Bedroom", "temp": ["sensor.br_temp"], "motion": ["binary_sensor.br_motion"]},
        ])
        mgr = AreaOccupancyManager(hass, weighting_mode=mode, area_config=area_config)
        # Manually set occupancy
        occupied_rooms = occupied_rooms or []
        for area in mgr.areas:
            area.occupied = area.area_id in occupied_rooms
        return mgr

    def test_equal_mode_all_weight_1(self):
        """EQUAL mode: all rooms get weight 1.0 regardless of occupancy."""
        mgr = self._two_room_manager("equal", ["lr"])
        weights = mgr.get_area_weights()
        assert weights["lr"] == 1.0
        assert weights["br"] == 1.0

    def test_occupied_only_mode(self):
        """OCCUPIED_ONLY mode: occupied=1.0, unoccupied=0.0."""
        mgr = self._two_room_manager("occupied_only", ["lr"])
        weights = mgr.get_area_weights()
        assert weights["lr"] == 1.0
        assert weights["br"] == 0.0

    def test_occupied_only_fallback_when_all_empty(self):
        """OCCUPIED_ONLY: if no rooms occupied, fallback to all 1.0."""
        mgr = self._two_room_manager("occupied_only", [])
        weights = mgr.get_area_weights()
        assert weights["lr"] == 1.0
        assert weights["br"] == 1.0

    def test_weighted_mode(self):
        """WEIGHTED mode: occupied rooms get multiplier, unoccupied get 1.0."""
        mgr = self._two_room_manager("weighted", ["lr"])
        mgr._occupied_weight_multiplier = 3.0
        weights = mgr.get_area_weights()
        assert weights["lr"] == 3.0
        assert weights["br"] == 1.0


# ═══════════════════════════════════════════════════════════════════
# Tests — Weighted Temperature Aggregation
# ═══════════════════════════════════════════════════════════════════

class TestWeightedAggregation:
    """Test weighted temperature and humidity calculations."""

    def test_weighted_temp_occupied_only(self):
        """Only occupied room contributes in occupied_only mode."""
        hass = _make_hass({
            "sensor.lr_temp": _make_state("72.0", unit="°F"),
            "sensor.br_temp": _make_state("78.0", unit="°F"),
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="occupied_only",
            area_config=_make_area_config([
                {"id": "lr", "name": "Living Room", "temp": ["sensor.lr_temp"], "motion": ["binary_sensor.lr"]},
                {"id": "br", "name": "Bedroom", "temp": ["sensor.br_temp"], "motion": ["binary_sensor.br"]},
            ]),
        )
        # Set occupancy manually
        mgr._areas[0].occupied = True  # Living Room
        mgr._areas[1].occupied = False  # Bedroom
        mgr.update_readings()

        temp, source = mgr.get_weighted_indoor_temp()
        # Only living room contributes (72°F)
        assert temp == pytest.approx(72.0, abs=0.1)
        assert "weighted:1/2" in source

    def test_weighted_temp_with_thermostat(self):
        """Thermostat reading is included at weight 1.0."""
        hass = _make_hass({
            "sensor.lr_temp": _make_state("72.0", unit="°F"),
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="occupied_only",
            area_config=_make_area_config([
                {"id": "lr", "name": "Living Room", "temp": ["sensor.lr_temp"], "motion": []},
            ]),
        )
        mgr._areas[0].occupied = True
        mgr.update_readings()

        temp, source = mgr.get_weighted_indoor_temp(thermostat_temp=74.0)
        # (72 * 1.0 + 74 * 1.0) / (1 + 1) = 73.0
        assert temp == pytest.approx(73.0, abs=0.1)

    def test_weighted_mode_gives_occupied_more_weight(self):
        """In weighted mode, occupied rooms count more."""
        hass = _make_hass({
            "sensor.lr_temp": _make_state("72.0", unit="°F"),
            "sensor.br_temp": _make_state("78.0", unit="°F"),
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="weighted",
            area_config=_make_area_config([
                {"id": "lr", "name": "Living Room", "temp": ["sensor.lr_temp"], "motion": ["binary_sensor.lr"]},
                {"id": "br", "name": "Bedroom", "temp": ["sensor.br_temp"], "motion": ["binary_sensor.br"]},
            ]),
            occupied_weight_multiplier=3.0,
        )
        mgr._areas[0].occupied = True  # Living Room (weight 3.0)
        mgr._areas[1].occupied = False  # Bedroom (weight 1.0)
        mgr.update_readings()

        temp, _ = mgr.get_weighted_indoor_temp()
        # (72 * 3 + 78 * 1) / (3 + 1) = (216 + 78) / 4 = 73.5
        assert temp == pytest.approx(73.5, abs=0.1)

    def test_equal_mode_simple_average(self):
        """Equal mode produces simple average regardless of occupancy."""
        hass = _make_hass({
            "sensor.lr_temp": _make_state("70.0", unit="°F"),
            "sensor.br_temp": _make_state("80.0", unit="°F"),
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="equal",
            area_config=_make_area_config([
                {"id": "lr", "name": "Living Room", "temp": ["sensor.lr_temp"], "motion": ["binary_sensor.lr"]},
                {"id": "br", "name": "Bedroom", "temp": ["sensor.br_temp"], "motion": ["binary_sensor.br"]},
            ]),
        )
        mgr._areas[0].occupied = True
        mgr._areas[1].occupied = False
        mgr.update_readings()

        temp, _ = mgr.get_weighted_indoor_temp()
        # (70 * 1 + 80 * 1) / (1 + 1) = 75.0
        assert temp == pytest.approx(75.0, abs=0.1)

    def test_weighted_humidity(self):
        """Humidity weighting works the same as temperature."""
        hass = _make_hass({
            "sensor.lr_humidity": _make_state("50.0"),
            "sensor.br_humidity": _make_state("70.0"),
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="occupied_only",
            area_config=_make_area_config([
                {"id": "lr", "name": "Living Room", "humidity": ["sensor.lr_humidity"], "motion": ["binary_sensor.lr"]},
                {"id": "br", "name": "Bedroom", "humidity": ["sensor.br_humidity"], "motion": ["binary_sensor.br"]},
            ]),
        )
        mgr._areas[0].occupied = True
        mgr._areas[1].occupied = False
        mgr.update_readings()

        humidity, source = mgr.get_weighted_indoor_humidity()
        assert humidity == pytest.approx(50.0, abs=0.1)

    def test_no_data_returns_none(self):
        """If no area has valid data, returns None."""
        hass = _make_hass({})  # No states
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="weighted",
            area_config=_make_area_config([
                {"id": "lr", "name": "Living Room", "temp": ["sensor.lr_temp"], "motion": []},
            ]),
        )
        mgr.update_readings()
        temp, source = mgr.get_weighted_indoor_temp()
        assert temp is None

    def test_celsius_conversion(self):
        """Celsius sensors are converted to Fahrenheit."""
        hass = _make_hass({
            "sensor.lr_temp": _make_state("22.0", unit="°C"),  # 71.6°F
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="equal",
            area_config=_make_area_config([
                {"id": "lr", "name": "Living Room", "temp": ["sensor.lr_temp"], "motion": []},
            ]),
        )
        mgr.update_readings()
        temp, _ = mgr.get_weighted_indoor_temp()
        assert temp == pytest.approx(71.6, abs=0.1)


# ═══════════════════════════════════════════════════════════════════
# Tests — Spike Detection
# ═══════════════════════════════════════════════════════════════════

class TestSpikeDetection:
    """Test transient spike dampening for showers, cooking, etc."""

    def test_humidity_spike_dampens_weight(self):
        """A rapid humidity spike dampens the area's weight to near-zero."""
        hass = _make_hass({
            "sensor.bath_temp": _make_state("72.0", unit="°F"),
            "sensor.bath_humidity": _make_state("50.0"),
            "sensor.lr_temp": _make_state("72.0", unit="°F"),
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="equal",
            area_config=_make_area_config([
                {"id": "bath", "name": "Bathroom", "temp": ["sensor.bath_temp"], "humidity": ["sensor.bath_humidity"], "motion": []},
                {"id": "lr", "name": "Living Room", "temp": ["sensor.lr_temp"], "motion": []},
            ]),
        )
        mgr.update_readings()

        # Simulate humidity jumping from 50 to 90 (shower)
        hass.states.get = MagicMock(side_effect=lambda eid: {
            "sensor.bath_temp": _make_state("76.0", unit="°F"),
            "sensor.bath_humidity": _make_state("90.0"),
            "sensor.lr_temp": _make_state("72.0", unit="°F"),
        }.get(eid))
        mgr.update_readings()

        # The bathroom should be spiking
        weights = mgr.get_area_weights()
        assert weights["bath"] < 0.1  # dampened to near-zero
        assert weights["lr"] == 1.0  # unaffected

    def test_no_spike_normal_readings(self):
        """Normal readings don't trigger dampening."""
        hass = _make_hass({
            "sensor.lr_temp": _make_state("72.0", unit="°F"),
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="equal",
            area_config=_make_area_config([
                {"id": "lr", "name": "Living Room", "temp": ["sensor.lr_temp"], "motion": []},
            ]),
        )
        # Several normal readings
        mgr.update_readings()
        hass.states.get = MagicMock(
            side_effect=lambda eid: _make_state("72.5", unit="°F")
        )
        mgr.update_readings()

        weights = mgr.get_area_weights()
        assert weights["lr"] == 1.0


# ═══════════════════════════════════════════════════════════════════
# Tests — Per-Area Apparent Temperature
# ═══════════════════════════════════════════════════════════════════

class TestApparentTemperature:
    """Test per-area apparent temperature computation."""

    def test_apparent_temp_with_high_humidity(self):
        """High humidity increases apparent temperature."""
        hass = _make_hass({
            "sensor.br_temp": _make_state("78.0", unit="°F"),
            "sensor.br_humidity": _make_state("65.0"),
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="equal",
            area_config=_make_area_config([
                {"id": "br", "name": "Bedroom", "temp": ["sensor.br_temp"], "humidity": ["sensor.br_humidity"], "motion": []},
            ]),
        )
        mgr.update_readings()

        area = mgr.areas[0]
        assert area.current_apparent_temp is not None
        # At 78°F and 65% humidity, apparent temp should be higher than 78
        assert area.current_apparent_temp > 78.0

    def test_apparent_temp_without_humidity(self):
        """Without humidity data, apparent temp equals raw temp."""
        hass = _make_hass({
            "sensor.lr_temp": _make_state("72.0", unit="°F"),
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="equal",
            area_config=_make_area_config([
                {"id": "lr", "name": "Living Room", "temp": ["sensor.lr_temp"], "motion": []},
            ]),
        )
        mgr.update_readings()
        area = mgr.areas[0]
        assert area.current_apparent_temp == pytest.approx(72.0, abs=0.1)


# ═══════════════════════════════════════════════════════════════════
# Tests — Serialization
# ═══════════════════════════════════════════════════════════════════

class TestSerialization:
    """Test config serialization and deserialization."""

    def test_roundtrip(self):
        """Serialized config can be deserialized back."""
        areas = [
            AreaSensorGroup(
                area_id="lr",
                area_name="Living Room",
                temp_entities=["sensor.lr_temp"],
                humidity_entities=["sensor.lr_humidity"],
                motion_entities=["binary_sensor.lr_motion"],
            ),
            AreaSensorGroup(
                area_id="br",
                area_name="Bedroom",
                temp_entities=["sensor.br_temp"],
                humidity_entities=[],
                motion_entities=["binary_sensor.br_motion", "binary_sensor.br_motion_2"],
            ),
        ]
        serialized = AreaOccupancyManager.serialize_area_config(areas)
        deserialized = AreaOccupancyManager.deserialize_area_config(serialized)

        assert len(deserialized) == 2
        assert deserialized[0]["area_id"] == "lr"
        assert deserialized[0]["area_name"] == "Living Room"
        assert deserialized[0]["temp_entities"] == ["sensor.lr_temp"]
        assert deserialized[1]["motion_entities"] == ["binary_sensor.br_motion", "binary_sensor.br_motion_2"]

    def test_deserialize_empty(self):
        """Empty string deserializes to empty list."""
        assert AreaOccupancyManager.deserialize_area_config("") == []


# ═══════════════════════════════════════════════════════════════════
# Tests — Diagnostics
# ═══════════════════════════════════════════════════════════════════

class TestDiagnostics:
    """Test diagnostic output."""

    def test_diagnostics_structure(self):
        """Diagnostics return proper per-area breakdown."""
        hass = _make_hass({
            "sensor.lr_temp": _make_state("72.0", unit="°F"),
            "sensor.lr_humidity": _make_state("50.0"),
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="weighted",
            area_config=_make_area_config([
                {"id": "lr", "name": "Living Room",
                 "temp": ["sensor.lr_temp"],
                 "humidity": ["sensor.lr_humidity"],
                 "motion": ["binary_sensor.lr_motion"]},
            ]),
        )
        mgr._areas[0].occupied = True
        mgr.update_readings()

        diag = mgr.get_diagnostics()
        assert len(diag) == 1
        assert diag[0]["area_name"] == "Living Room"
        assert diag[0]["occupied"] is True
        assert diag[0]["temp"] == pytest.approx(72.0, abs=0.1)
        assert diag[0]["humidity"] == pytest.approx(50.0, abs=0.1)
        assert diag[0]["weight"] == 3.0  # occupied in weighted mode


# ═══════════════════════════════════════════════════════════════════
# Tests — Backward Compatibility
# ═══════════════════════════════════════════════════════════════════

class TestBackwardCompat:
    """Ensure equal mode produces identical behavior to no room-aware sensing."""

    def test_equal_mode_is_simple_average(self):
        """Equal mode with 3 rooms produces the same result as averaging all sensors."""
        hass = _make_hass({
            "sensor.a_temp": _make_state("70.0", unit="°F"),
            "sensor.b_temp": _make_state("74.0", unit="°F"),
            "sensor.c_temp": _make_state("76.0", unit="°F"),
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="equal",
            area_config=_make_area_config([
                {"id": "a", "name": "A", "temp": ["sensor.a_temp"], "motion": []},
                {"id": "b", "name": "B", "temp": ["sensor.b_temp"], "motion": []},
                {"id": "c", "name": "C", "temp": ["sensor.c_temp"], "motion": []},
            ]),
        )
        mgr.update_readings()
        temp, _ = mgr.get_weighted_indoor_temp()
        expected = (70.0 + 74.0 + 76.0) / 3.0
        assert temp == pytest.approx(expected, abs=0.01)
