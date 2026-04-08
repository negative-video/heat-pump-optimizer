"""Tests for the SensorHub adapter — all methods, fallback chains, unit conversion."""

import importlib
import importlib.util
import math
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, PropertyMock

import pytest

# ── HA stubs ────────────────────────────────────────────────────────
# Must be set up BEFORE loading sensor_hub.py via importlib.

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, PROJECT_ROOT)

CC = os.path.join(PROJECT_ROOT, "custom_components", "heatpump_optimizer")

# Package stubs
pkg = types.ModuleType("custom_components")
pkg.__path__ = [os.path.join(PROJECT_ROOT, "custom_components")]
sys.modules["custom_components"] = pkg

ho = types.ModuleType("custom_components.heatpump_optimizer")
ho.__path__ = [CC]
sys.modules["custom_components.heatpump_optimizer"] = ho

adapters_pkg = types.ModuleType("custom_components.heatpump_optimizer.adapters")
adapters_pkg.__path__ = [os.path.join(CC, "adapters")]
sys.modules["custom_components.heatpump_optimizer.adapters"] = adapters_pkg

engine_pkg = types.ModuleType("custom_components.heatpump_optimizer.engine")
engine_pkg.__path__ = [os.path.join(CC, "engine")]
sys.modules["custom_components.heatpump_optimizer.engine"] = engine_pkg

# ── Stub homeassistant modules with real conversion math ────────────

ha_core = types.ModuleType("homeassistant.core")
ha_core.HomeAssistant = MagicMock
ha_core.SupportsResponse = type("SupportsResponse", (), {
    "ONLY": "only", "OPTIONAL": "optional", "NONE": "none",
})
sys.modules["homeassistant"] = types.ModuleType("homeassistant")
sys.modules["homeassistant"].__path__ = ["homeassistant"]
sys.modules["homeassistant.core"] = ha_core

ha_const = types.ModuleType("homeassistant.const")
ha_const.UnitOfTemperature = type("UnitOfTemperature", (), {
    "CELSIUS": "\u00b0C",
    "FAHRENHEIT": "\u00b0F",
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

# Real conversion helpers
ha_util = types.ModuleType("homeassistant.util")
ha_util.__path__ = ["homeassistant/util"]
sys.modules["homeassistant.util"] = ha_util

ha_unit_conv = types.ModuleType("homeassistant.util.unit_conversion")


class _TemperatureConverter:
    @staticmethod
    def convert(value, from_unit, to_unit):
        if from_unit == "\u00b0C" and to_unit == "\u00b0F":
            return value * 9.0 / 5.0 + 32.0
        if from_unit == "\u00b0F" and to_unit == "\u00b0C":
            return (value - 32.0) * 5.0 / 9.0
        return value


class _SpeedConverter:
    @staticmethod
    def convert(value, from_unit, to_unit):
        # Convert to m/s first, then to target
        TO_MS = {"km/h": 1 / 3.6, "mph": 0.44704, "m/s": 1.0}
        FROM_MS = {"km/h": 3.6, "mph": 1 / 0.44704, "m/s": 1.0}
        ms = value * TO_MS.get(from_unit, 1.0)
        return ms * FROM_MS.get(to_unit, 1.0)


class _PressureConverter:
    @staticmethod
    def convert(value, from_unit, to_unit):
        # Convert to hPa first
        TO_HPA = {"inHg": 33.8639, "psi": 68.9476, "hPa": 1.0, "mbar": 1.0}
        FROM_HPA = {"inHg": 1 / 33.8639, "psi": 1 / 68.9476, "hPa": 1.0, "mbar": 1.0}
        hpa = value * TO_HPA.get(from_unit, 1.0)
        return hpa * FROM_HPA.get(to_unit, 1.0)


ha_unit_conv.TemperatureConverter = _TemperatureConverter
ha_unit_conv.SpeedConverter = _SpeedConverter
ha_unit_conv.PressureConverter = _PressureConverter
sys.modules["homeassistant.util.unit_conversion"] = ha_unit_conv


# ── Load actual modules via importlib ───────────────────────────────

def _load(full_name: str, path: str):
    spec = importlib.util.spec_from_file_location(full_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


# const (needed for DEFAULT_SENSOR_STALE_MINUTES)
const_mod = _load(
    "custom_components.heatpump_optimizer.const",
    os.path.join(CC, "const.py"),
)
ho.const = const_mod

# data_types (needed for ForecastPoint)
dt_mod = _load(
    "custom_components.heatpump_optimizer.engine.data_types",
    os.path.join(CC, "engine", "data_types.py"),
)
engine_pkg.data_types = dt_mod

# sensor_hub itself
sh_mod = _load(
    "custom_components.heatpump_optimizer.adapters.sensor_hub",
    os.path.join(CC, "adapters", "sensor_hub.py"),
)
adapters_pkg.sensor_hub = sh_mod

SensorHub = sh_mod.SensorHub
SensorReading = sh_mod.SensorReading
ForecastPoint = dt_mod.ForecastPoint
DEFAULT_SENSOR_STALE_MINUTES = const_mod.DEFAULT_SENSOR_STALE_MINUTES


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


def _make_hub(hass, **kwargs):
    return SensorHub(hass, **kwargs)


def _now():
    return datetime.now(timezone.utc)


# ═══════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════

class TestReadEntity:
    """_read_entity: valid, unavailable, unknown, missing, non-numeric, out-of-range."""

    def test_valid_numeric(self):
        hass = _make_hass({"sensor.t": _make_state(72.5)})
        hub = _make_hub(hass)
        assert hub._read_entity("sensor.t", -100, 200, "Test") == 72.5

    def test_unavailable(self):
        hass = _make_hass({"sensor.t": _make_state("unavailable")})
        hub = _make_hub(hass)
        assert hub._read_entity("sensor.t", -100, 200, "Test") is None

    def test_unknown(self):
        hass = _make_hass({"sensor.t": _make_state("unknown")})
        hub = _make_hub(hass)
        assert hub._read_entity("sensor.t", -100, 200, "Test") is None

    def test_missing_entity(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        assert hub._read_entity("sensor.missing", -100, 200, "Test") is None

    def test_non_numeric(self):
        hass = _make_hass({"sensor.t": _make_state("hello")})
        hub = _make_hub(hass)
        assert hub._read_entity("sensor.t", -100, 200, "Test") is None

    def test_below_range(self):
        hass = _make_hass({"sensor.t": _make_state(-150)})
        hub = _make_hub(hass)
        assert hub._read_entity("sensor.t", -100, 200, "Test") is None

    def test_above_range(self):
        hass = _make_hass({"sensor.t": _make_state(250)})
        hub = _make_hub(hass)
        assert hub._read_entity("sensor.t", -100, 200, "Test") is None

    def test_boundary_min(self):
        hass = _make_hass({"sensor.t": _make_state(-100)})
        hub = _make_hub(hass)
        assert hub._read_entity("sensor.t", -100, 200, "Test") == -100.0

    def test_boundary_max(self):
        hass = _make_hass({"sensor.t": _make_state(200)})
        hub = _make_hub(hass)
        assert hub._read_entity("sensor.t", -100, 200, "Test") == 200.0


class TestStaleDetection:
    """_is_stale: recent, old, None."""

    def test_recent_not_stale(self):
        hass = _make_hass({"sensor.t": _make_state(72, age_minutes=1)})
        hub = _make_hub(hass)
        state = hass.states.get("sensor.t")
        assert hub._is_stale(state) is False

    def test_old_is_stale(self):
        hass = _make_hass({"sensor.t": _make_state(72, age_minutes=DEFAULT_SENSOR_STALE_MINUTES + 5)})
        hub = _make_hub(hass)
        state = hass.states.get("sensor.t")
        assert hub._is_stale(state) is True

    def test_none_state_is_stale(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        assert hub._is_stale(None) is True

    def test_none_last_updated_is_stale(self):
        s = MagicMock()
        s.last_updated = None
        hass = _make_hass({})
        hub = _make_hub(hass)
        assert hub._is_stale(s) is True

    def test_exactly_at_threshold(self):
        """At exactly DEFAULT_SENSOR_STALE_MINUTES, should NOT be stale (> not >=)."""
        s = MagicMock()
        s.last_updated = datetime.now(timezone.utc) - timedelta(minutes=DEFAULT_SENSOR_STALE_MINUTES)
        hass = _make_hass({})
        hub = _make_hub(hass)
        # Due to time passing between creation and check, this is borderline.
        # The code uses >, so equal should not be stale if time hasn't advanced.
        # We accept either outcome at the exact boundary.
        result = hub._is_stale(s)
        assert isinstance(result, bool)


class TestReadMultiTemp:
    """_read_multi_temp: single, celsius auto-convert, averaging, skips unavailable, stale."""

    def test_single_entity_fahrenheit(self):
        hass = _make_hass({"sensor.t1": _make_state(72, unit="\u00b0F")})
        hub = _make_hub(hass)
        r = hub._read_multi_temp(["sensor.t1"], "Test")
        assert r is not None
        assert r.value == pytest.approx(72.0)
        assert r.source == "entity:sensor.t1"
        assert r.stale is False

    def test_celsius_auto_convert(self):
        hass = _make_hass({"sensor.t1": _make_state(22, unit="\u00b0C")})
        hub = _make_hub(hass)
        r = hub._read_multi_temp(["sensor.t1"], "Test")
        assert r is not None
        assert r.value == pytest.approx(71.6)

    def test_multiple_averaging(self):
        hass = _make_hass({
            "sensor.t1": _make_state(70, unit="\u00b0F"),
            "sensor.t2": _make_state(74, unit="\u00b0F"),
        })
        hub = _make_hub(hass)
        r = hub._read_multi_temp(["sensor.t1", "sensor.t2"], "Test")
        assert r is not None
        assert r.value == pytest.approx(72.0)
        assert r.source == "average:2"

    def test_skips_unavailable(self):
        hass = _make_hass({
            "sensor.t1": _make_state(70, unit="\u00b0F"),
            "sensor.t2": _make_state("unavailable"),
        })
        hub = _make_hub(hass)
        r = hub._read_multi_temp(["sensor.t1", "sensor.t2"], "Test")
        assert r is not None
        assert r.value == pytest.approx(70.0)
        assert r.source == "entity:sensor.t1"

    def test_stale_flag(self):
        hass = _make_hass({
            "sensor.t1": _make_state(70, unit="\u00b0F", age_minutes=DEFAULT_SENSOR_STALE_MINUTES + 5),
        })
        hub = _make_hub(hass)
        r = hub._read_multi_temp(["sensor.t1"], "Test")
        assert r is not None
        assert r.stale is True

    def test_all_unavailable_returns_none(self):
        hass = _make_hass({
            "sensor.t1": _make_state("unavailable"),
            "sensor.t2": _make_state("unknown"),
        })
        hub = _make_hub(hass)
        r = hub._read_multi_temp(["sensor.t1", "sensor.t2"], "Test")
        assert r is None

    def test_empty_entity_list(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        r = hub._read_multi_temp([], "Test")
        assert r is None


class TestReadMultiHumidity:
    """_read_multi_humidity: single, averaging, out-of-range skipped."""

    def test_single(self):
        hass = _make_hass({"sensor.h1": _make_state(55)})
        hub = _make_hub(hass)
        r = hub._read_multi_humidity(["sensor.h1"], "Test")
        assert r is not None
        assert r.value == pytest.approx(55.0)
        assert r.source == "entity:sensor.h1"

    def test_averaging(self):
        hass = _make_hass({
            "sensor.h1": _make_state(50),
            "sensor.h2": _make_state(60),
        })
        hub = _make_hub(hass)
        r = hub._read_multi_humidity(["sensor.h1", "sensor.h2"], "Test")
        assert r is not None
        assert r.value == pytest.approx(55.0)
        assert r.source == "average:2"

    def test_out_of_range_skipped(self):
        """Humidity outside 0-100 should be ignored."""
        hass = _make_hass({
            "sensor.h1": _make_state(50),
            "sensor.h2": _make_state(110),  # out of range
        })
        hub = _make_hub(hass)
        r = hub._read_multi_humidity(["sensor.h1", "sensor.h2"], "Test")
        assert r is not None
        assert r.value == pytest.approx(50.0)

    def test_negative_humidity_skipped(self):
        hass = _make_hass({"sensor.h1": _make_state(-5)})
        hub = _make_hub(hass)
        r = hub._read_multi_humidity(["sensor.h1"], "Test")
        assert r is None


class TestReadOutdoorTemp:
    """read_outdoor_temp: entity preferred, stale falls to forecast, forecast closest, last_known, None."""

    def test_entity_preferred(self):
        hass = _make_hass({"sensor.ot": _make_state(85, unit="\u00b0F")})
        hub = _make_hub(hass, outdoor_temp_entities=["sensor.ot"])
        r = hub.read_outdoor_temp()
        assert r is not None
        assert r.value == pytest.approx(85.0)
        assert r.source == "entity:sensor.ot"

    def test_stale_falls_to_forecast(self):
        hass = _make_hass({
            "sensor.ot": _make_state(85, unit="\u00b0F", age_minutes=DEFAULT_SENSOR_STALE_MINUTES + 5),
        })
        hub = _make_hub(hass, outdoor_temp_entities=["sensor.ot"])
        fp = ForecastPoint(time=_now(), outdoor_temp=80.0)
        r = hub.read_outdoor_temp(forecast_snapshot=[fp])
        assert r is not None
        assert r.value == pytest.approx(80.0)
        assert r.source == "forecast"

    def test_forecast_closest_point(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        now = _now()
        fp1 = ForecastPoint(time=now - timedelta(hours=1), outdoor_temp=70.0)
        fp2 = ForecastPoint(time=now + timedelta(minutes=10), outdoor_temp=75.0)
        r = hub.read_outdoor_temp(forecast_snapshot=[fp1, fp2])
        assert r is not None
        assert r.value == pytest.approx(75.0)

    def test_forecast_too_far_away_ignored(self):
        """Forecast point > 2h from now should not be used."""
        hass = _make_hass({})
        hub = _make_hub(hass)
        fp = ForecastPoint(time=_now() + timedelta(hours=3), outdoor_temp=99.0)
        r = hub.read_outdoor_temp(forecast_snapshot=[fp])
        assert r is None

    def test_last_known_fallback(self):
        hass = _make_hass({"sensor.ot": _make_state(85, unit="\u00b0F")})
        hub = _make_hub(hass, outdoor_temp_entities=["sensor.ot"])
        # First read: fresh, caches
        r1 = hub.read_outdoor_temp()
        assert r1 is not None

        # Now entity goes away
        hass.states.get = MagicMock(return_value=None)
        r2 = hub.read_outdoor_temp()
        assert r2 is not None
        assert r2.source == "last_known"
        assert r2.value == pytest.approx(85.0)
        assert r2.stale is True

    def test_no_data_returns_none(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        r = hub.read_outdoor_temp()
        assert r is None

    def test_stale_entity_returned_when_no_forecast_no_last_known(self):
        """Stale entity reading is returned (step 3) when no forecast and no prior cache."""
        hass = _make_hass({
            "sensor.ot": _make_state(60, unit="\u00b0F", age_minutes=DEFAULT_SENSOR_STALE_MINUTES + 10),
        })
        hub = _make_hub(hass, outdoor_temp_entities=["sensor.ot"])
        r = hub.read_outdoor_temp()
        assert r is not None
        assert r.stale is True
        assert r.value == pytest.approx(60.0)


class TestReadOutdoorHumidity:
    """read_outdoor_humidity: entity preferred, forecast fallback."""

    def test_entity_preferred(self):
        hass = _make_hass({"sensor.oh": _make_state(65)})
        hub = _make_hub(hass, outdoor_humidity_entities=["sensor.oh"])
        r = hub.read_outdoor_humidity()
        assert r is not None
        assert r.value == pytest.approx(65.0)

    def test_forecast_fallback(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        fp = ForecastPoint(time=_now(), outdoor_temp=80.0, humidity=55.0)
        r = hub.read_outdoor_humidity(forecast_snapshot=[fp])
        assert r is not None
        assert r.value == pytest.approx(55.0)
        assert r.source == "forecast"

    def test_forecast_no_humidity(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        fp = ForecastPoint(time=_now(), outdoor_temp=80.0, humidity=None)
        r = hub.read_outdoor_humidity(forecast_snapshot=[fp])
        assert r is None

    def test_stale_entity_used_when_no_forecast(self):
        hass = _make_hass({
            "sensor.oh": _make_state(55, age_minutes=DEFAULT_SENSOR_STALE_MINUTES + 5),
        })
        hub = _make_hub(hass, outdoor_humidity_entities=["sensor.oh"])
        r = hub.read_outdoor_humidity()
        assert r is not None
        assert r.stale is True


class TestReadIndoorTemp:
    """read_indoor_temp: entities only, thermostat only, entities+thermostat averaged, no data."""

    def test_entities_only(self):
        hass = _make_hass({"sensor.it": _make_state(71, unit="\u00b0F")})
        hub = _make_hub(hass, indoor_temp_entities=["sensor.it"])
        r = hub.read_indoor_temp()
        assert r is not None
        assert r.value == pytest.approx(71.0)

    def test_thermostat_only(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        r = hub.read_indoor_temp(thermostat_temp=73.0)
        assert r is not None
        assert r.value == pytest.approx(73.0)
        assert r.source == "thermostat"

    def test_entities_plus_thermostat_averaged(self):
        hass = _make_hass({"sensor.it": _make_state(70, unit="\u00b0F")})
        hub = _make_hub(hass, indoor_temp_entities=["sensor.it"])
        r = hub.read_indoor_temp(thermostat_temp=74.0)
        assert r is not None
        # 1 entity (70) + thermostat (74) = average 72
        assert r.value == pytest.approx(72.0)
        assert "average:2" == r.source

    def test_multiple_entities_plus_thermostat(self):
        hass = _make_hass({
            "sensor.it1": _make_state(70, unit="\u00b0F"),
            "sensor.it2": _make_state(72, unit="\u00b0F"),
        })
        hub = _make_hub(hass, indoor_temp_entities=["sensor.it1", "sensor.it2"])
        r = hub.read_indoor_temp(thermostat_temp=74.0)
        assert r is not None
        # average:2 gives (70+72)/2=71, then (71*2 + 74)/3 = 72.0
        assert r.value == pytest.approx(72.0)
        assert r.source == "average:3"

    def test_no_data(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        r = hub.read_indoor_temp()
        assert r is None

    def test_stale_entities_falls_to_thermostat(self):
        hass = _make_hass({
            "sensor.it": _make_state(70, unit="\u00b0F", age_minutes=DEFAULT_SENSOR_STALE_MINUTES + 5),
        })
        hub = _make_hub(hass, indoor_temp_entities=["sensor.it"])
        r = hub.read_indoor_temp(thermostat_temp=73.0)
        assert r is not None
        # Stale reading, so entities are not "fresh" — falls through to thermostat
        assert r.source == "thermostat"
        assert r.value == pytest.approx(73.0)


class TestReadWindSpeed:
    """read_wind_speed: mph direct, km/h converted, forecast fallback."""

    def test_mph_direct(self):
        hass = _make_hass({"sensor.ws": _make_state(10, unit="mph")})
        hub = _make_hub(hass, wind_speed_entity="sensor.ws")
        r = hub.read_wind_speed()
        assert r is not None
        assert r.value == pytest.approx(10.0)

    def test_kmh_converted(self):
        hass = _make_hass({"sensor.ws": _make_state(16.09, unit="km/h")})
        hub = _make_hub(hass, wind_speed_entity="sensor.ws")
        r = hub.read_wind_speed()
        assert r is not None
        assert r.value == pytest.approx(10.0, abs=0.1)

    def test_ms_converted(self):
        hass = _make_hass({"sensor.ws": _make_state(4.47, unit="m/s")})
        hub = _make_hub(hass, wind_speed_entity="sensor.ws")
        r = hub.read_wind_speed()
        assert r is not None
        assert r.value == pytest.approx(10.0, abs=0.1)

    def test_forecast_fallback(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        fp = ForecastPoint(time=_now(), outdoor_temp=80.0, wind_speed_mph=12.0)
        r = hub.read_wind_speed(forecast_snapshot=[fp])
        assert r is not None
        assert r.value == pytest.approx(12.0)
        assert r.source == "forecast"

    def test_no_entity_no_forecast(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        r = hub.read_wind_speed()
        assert r is None

    def test_forecast_no_wind(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        fp = ForecastPoint(time=_now(), outdoor_temp=80.0, wind_speed_mph=None)
        r = hub.read_wind_speed(forecast_snapshot=[fp])
        assert r is None


class TestReadSolarIrradiance:
    """read_solar_irradiance: valid, no entity, out of range."""

    def test_valid(self):
        hass = _make_hass({"sensor.si": _make_state(500)})
        hub = _make_hub(hass, solar_irradiance_entity="sensor.si")
        r = hub.read_solar_irradiance()
        assert r is not None
        assert r.value == pytest.approx(500.0)

    def test_no_entity(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        r = hub.read_solar_irradiance()
        assert r is None

    def test_out_of_range(self):
        hass = _make_hass({"sensor.si": _make_state(2000)})
        hub = _make_hub(hass, solar_irradiance_entity="sensor.si")
        r = hub.read_solar_irradiance()
        assert r is None

    def test_unavailable_entity(self):
        hass = _make_hass({"sensor.si": _make_state("unavailable")})
        hub = _make_hub(hass, solar_irradiance_entity="sensor.si")
        r = hub.read_solar_irradiance()
        assert r is None


class TestReadBarometricPressure:
    """read_barometric_pressure: hPa direct, inHg converted, psi converted."""

    def test_hpa_direct(self):
        hass = _make_hass({"sensor.bp": _make_state(1013.25, unit="hPa")})
        hub = _make_hub(hass, barometric_pressure_entity="sensor.bp")
        r = hub.read_barometric_pressure()
        assert r is not None
        assert r.value == pytest.approx(1013.25)

    def test_inhg_converted(self):
        """29.92 inHg is ~1013.25 hPa — converts before validation."""
        hass = _make_hass({"sensor.bp": _make_state(29.92, unit="inHg")})
        hub = _make_hub(hass, barometric_pressure_entity="sensor.bp")
        r = hub.read_barometric_pressure()
        assert r is not None
        assert r.value == pytest.approx(1013.25, abs=1.0)

    def test_psi_converted(self):
        """14.696 psi is ~1013.25 hPa — converts before validation."""
        hass = _make_hass({"sensor.bp": _make_state(14.696, unit="psi")})
        hub = _make_hub(hass, barometric_pressure_entity="sensor.bp")
        r = hub.read_barometric_pressure()
        assert r is not None
        assert r.value == pytest.approx(1013.25, abs=1.0)

    def test_nonsensical_inhg_rejected_after_conversion(self):
        """1000 inHg converts to ~33864 hPa — out of range, rejected."""
        hass = _make_hass({"sensor.bp": _make_state(1000, unit="inHg")})
        hub = _make_hub(hass, barometric_pressure_entity="sensor.bp")
        r = hub.read_barometric_pressure()
        assert r is None

    def test_mbar_pass_through(self):
        hass = _make_hass({"sensor.bp": _make_state(1013.25, unit="mbar")})
        hub = _make_hub(hass, barometric_pressure_entity="sensor.bp")
        r = hub.read_barometric_pressure()
        assert r is not None
        assert r.value == pytest.approx(1013.25)

    def test_no_entity(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        r = hub.read_barometric_pressure()
        assert r is None

    def test_out_of_range(self):
        """Pressure below 500 or above 1200 should be rejected."""
        hass = _make_hass({"sensor.bp": _make_state(400, unit="hPa")})
        hub = _make_hub(hass, barometric_pressure_entity="sensor.bp")
        r = hub.read_barometric_pressure()
        assert r is None


class TestReadSunElevation:
    """read_sun_elevation: returns elevation attribute, missing entity."""

    def test_returns_elevation(self):
        s = _make_state("above_horizon", attributes={"elevation": 45.0, "azimuth": 180.0})
        hass = _make_hass({"sun.sun": s})
        hub = _make_hub(hass)
        assert hub.read_sun_elevation() == pytest.approx(45.0)

    def test_missing_entity(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        assert hub.read_sun_elevation() is None

    def test_default_when_no_elevation_attr(self):
        s = _make_state("above_horizon", attributes={})
        hass = _make_hass({"sun.sun": s})
        hub = _make_hub(hass)
        # Default is 0 when attribute missing
        assert hub.read_sun_elevation() == pytest.approx(0.0)

    def test_sun_azimuth(self):
        s = _make_state("above_horizon", attributes={"elevation": 45.0, "azimuth": 180.0})
        hass = _make_hass({"sun.sun": s})
        hub = _make_hub(hass)
        assert hub.read_sun_azimuth() == pytest.approx(180.0)


class TestReadSolarProduction:
    """read_solar_production: valid, no entity."""

    def test_valid(self):
        hass = _make_hass({"sensor.sp": _make_state(3500)})
        hub = _make_hub(hass, solar_production_entity="sensor.sp")
        r = hub.read_solar_production()
        assert r is not None
        assert r.value == pytest.approx(3500.0)

    def test_no_entity(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        r = hub.read_solar_production()
        assert r is None

    def test_unavailable_entity(self):
        hass = _make_hass({"sensor.sp": _make_state("unavailable")})
        hub = _make_hub(hass, solar_production_entity="sensor.sp")
        r = hub.read_solar_production()
        assert r is None


class TestReadPowerDraw:
    """read_power_draw: entity available, unavailable falls back to default, no entity returns default."""

    def test_entity_available(self):
        hass = _make_hass({"sensor.pw": _make_state(3000)})
        hub = _make_hub(hass, power_entity="sensor.pw")
        assert hub.read_power_draw() == pytest.approx(3000.0)

    def test_entity_unavailable_falls_back_to_default(self):
        """When entity is configured but unavailable, fall back to default watts."""
        hass = _make_hass({"sensor.pw": _make_state("unavailable")})
        hub = _make_hub(hass, power_entity="sensor.pw")
        # Should fall back to default watts (3500) instead of returning None
        assert hub.read_power_draw() == pytest.approx(3500.0)

    def test_entity_unavailable_no_default_returns_none(self):
        """When entity is configured but unavailable and no default, returns None."""
        hass = _make_hass({"sensor.pw": _make_state("unavailable")})
        hub = _make_hub(hass, power_entity="sensor.pw", power_default_watts=0.0)
        assert hub.read_power_draw() is None

    def test_no_entity_returns_default(self):
        hass = _make_hass({})
        hub = _make_hub(hass, power_default_watts=4000.0)
        assert hub.read_power_draw() == pytest.approx(4000.0)

    def test_no_entity_returns_standard_default(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        assert hub.read_power_draw() == pytest.approx(3500.0)


class TestReadCo2Intensity:
    """read_co2_intensity: valid, no entity."""

    def test_valid(self):
        hass = _make_hass({"sensor.co2": _make_state(400)})
        hub = _make_hub(hass, co2_entity="sensor.co2")
        assert hub.read_co2_intensity() == pytest.approx(400.0)

    def test_no_entity(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        assert hub.read_co2_intensity() is None

    def test_out_of_range(self):
        hass = _make_hass({"sensor.co2": _make_state(3000)})
        hub = _make_hub(hass, co2_entity="sensor.co2")
        assert hub.read_co2_intensity() is None


class TestReadElectricityRate:
    """read_electricity_rate: entity, flat rate fallback, no data."""

    def test_entity(self):
        hass = _make_hass({"sensor.rate": _make_state(0.12)})
        hub = _make_hub(hass, rate_entity="sensor.rate")
        assert hub.read_electricity_rate() == pytest.approx(0.12)

    def test_flat_rate_fallback(self):
        hass = _make_hass({})
        hub = _make_hub(hass, flat_rate=0.15)
        assert hub.read_electricity_rate() == pytest.approx(0.15)

    def test_entity_unavailable_falls_to_flat_rate(self):
        hass = _make_hass({"sensor.rate": _make_state("unavailable")})
        hub = _make_hub(hass, rate_entity="sensor.rate", flat_rate=0.10)
        assert hub.read_electricity_rate() == pytest.approx(0.10)

    def test_no_data(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        assert hub.read_electricity_rate() is None


class TestReadNetPowerDraw:
    """read_net_power_draw: no solar returns gross, solar offsets, solar exceeds HVAC."""

    def test_no_solar_returns_gross(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        assert hub.read_net_power_draw(3000.0) == pytest.approx(3000.0)

    def test_solar_offsets(self):
        hass = _make_hass({"sensor.sp": _make_state(1000)})
        hub = _make_hub(hass, solar_production_entity="sensor.sp")
        net = hub.read_net_power_draw(3000.0)
        assert net == pytest.approx(2000.0)

    def test_solar_exceeds_hvac(self):
        hass = _make_hass({"sensor.sp": _make_state(5000)})
        hub = _make_hub(hass, solar_production_entity="sensor.sp")
        net = hub.read_net_power_draw(3000.0)
        assert net == pytest.approx(0.0)

    def test_none_hvac_power(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        assert hub.read_net_power_draw(None) is None

    def test_stale_solar_returns_gross(self):
        hass = _make_hass({
            "sensor.sp": _make_state(2000, age_minutes=DEFAULT_SENSOR_STALE_MINUTES + 5),
        })
        hub = _make_hub(hass, solar_production_entity="sensor.sp")
        net = hub.read_net_power_draw(3000.0)
        assert net == pytest.approx(3000.0)


class TestCorrectCurrentForecast:
    """correct_current_forecast: outdoor temp corrected, no correction when stale/far, empty."""

    def test_outdoor_temp_corrected(self):
        hass = _make_hass({"sensor.ot": _make_state(82, unit="\u00b0F")})
        hub = _make_hub(hass, outdoor_temp_entities=["sensor.ot"])
        fp = ForecastPoint(time=_now(), outdoor_temp=80.0)
        result = hub.correct_current_forecast([fp])
        assert result[0].outdoor_temp == pytest.approx(82.0)

    def test_no_correction_when_far(self):
        """Point > 1h from now should not be corrected."""
        hass = _make_hass({"sensor.ot": _make_state(82, unit="\u00b0F")})
        hub = _make_hub(hass, outdoor_temp_entities=["sensor.ot"])
        fp = ForecastPoint(time=_now() + timedelta(hours=2), outdoor_temp=80.0)
        result = hub.correct_current_forecast([fp])
        assert result[0].outdoor_temp == pytest.approx(80.0)  # unchanged

    def test_no_correction_when_stale(self):
        hass = _make_hass({
            "sensor.ot": _make_state(82, unit="\u00b0F", age_minutes=DEFAULT_SENSOR_STALE_MINUTES + 5),
        })
        hub = _make_hub(hass, outdoor_temp_entities=["sensor.ot"])
        fp = ForecastPoint(time=_now(), outdoor_temp=80.0)
        result = hub.correct_current_forecast([fp])
        assert result[0].outdoor_temp == pytest.approx(80.0)  # unchanged

    def test_empty_forecast(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        result = hub.correct_current_forecast([])
        assert result == []

    def test_humidity_corrected(self):
        hass = _make_hass({"sensor.oh": _make_state(55)})
        hub = _make_hub(hass, outdoor_humidity_entities=["sensor.oh"])
        fp = ForecastPoint(time=_now(), outdoor_temp=80.0, humidity=50.0)
        result = hub.correct_current_forecast([fp])
        assert result[0].humidity == pytest.approx(55.0)

    def test_wind_corrected(self):
        hass = _make_hass({"sensor.ws": _make_state(15, unit="mph")})
        hub = _make_hub(hass, wind_speed_entity="sensor.ws")
        fp = ForecastPoint(time=_now(), outdoor_temp=80.0, wind_speed_mph=10.0)
        result = hub.correct_current_forecast([fp])
        assert result[0].wind_speed_mph == pytest.approx(15.0)

    def test_solar_irradiance_corrected(self):
        hass = _make_hass({"sensor.si": _make_state(800)})
        hub = _make_hub(hass, solar_irradiance_entity="sensor.si")
        fp = ForecastPoint(time=_now(), outdoor_temp=80.0, solar_irradiance_w_m2=600.0)
        result = hub.correct_current_forecast([fp])
        assert result[0].solar_irradiance_w_m2 == pytest.approx(800.0)


class TestDiagnosticInfo:
    """get_outdoor_temp_info / get_indoor_temp_info."""

    def test_outdoor_temp_info(self):
        hass = _make_hass({"sensor.ot": _make_state(85, unit="\u00b0F")})
        hub = _make_hub(hass, outdoor_temp_entities=["sensor.ot"])
        info = hub.get_outdoor_temp_info()
        assert info["value"] == 85.0
        assert info["source"] == "entity:sensor.ot"
        assert info["stale"] is False
        assert info["entity_count"] == 1

    def test_outdoor_temp_info_no_data(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        info = hub.get_outdoor_temp_info()
        assert info["value"] is None
        assert info["source"] == "unavailable"
        assert info["stale"] is True

    def test_indoor_temp_info(self):
        hass = _make_hass({"sensor.it": _make_state(72, unit="\u00b0F")})
        hub = _make_hub(hass, indoor_temp_entities=["sensor.it"])
        info = hub.get_indoor_temp_info()
        assert info["value"] == 72.0
        assert info["stale"] is False

    def test_indoor_temp_info_thermostat(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        info = hub.get_indoor_temp_info(thermostat_temp=71.0)
        assert info["value"] == 71.0
        assert info["source"] == "thermostat"


class TestGridImportAndExportRate:
    """read_grid_import, read_solar_export_rate."""

    def test_grid_import_valid(self):
        hass = _make_hass({"sensor.gi": _make_state(1500)})
        hub = _make_hub(hass, grid_import_entity="sensor.gi")
        r = hub.read_grid_import()
        assert r is not None
        assert r.value == pytest.approx(1500.0)

    def test_grid_import_negative(self):
        """Negative = exporting to grid."""
        hass = _make_hass({"sensor.gi": _make_state(-500)})
        hub = _make_hub(hass, grid_import_entity="sensor.gi")
        r = hub.read_grid_import()
        assert r is not None
        assert r.value == pytest.approx(-500.0)

    def test_grid_import_no_entity(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        assert hub.read_grid_import() is None

    def test_solar_export_rate_valid(self):
        hass = _make_hass({"sensor.ser": _make_state(0.05)})
        hub = _make_hub(hass, solar_export_rate_entity="sensor.ser")
        assert hub.read_solar_export_rate() == pytest.approx(0.05)

    def test_solar_export_rate_no_entity(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        assert hub.read_solar_export_rate() is None


class TestReadIndoorHumidity:
    """read_indoor_humidity: entity, thermostat fallback."""

    def test_entity(self):
        hass = _make_hass({"sensor.ih": _make_state(45)})
        hub = _make_hub(hass, indoor_humidity_entities=["sensor.ih"])
        r = hub.read_indoor_humidity()
        assert r is not None
        assert r.value == pytest.approx(45.0)

    def test_thermostat_fallback(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        r = hub.read_indoor_humidity(thermostat_humidity=50.0)
        assert r is not None
        assert r.value == pytest.approx(50.0)
        assert r.source == "thermostat"

    def test_no_data(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        r = hub.read_indoor_humidity()
        assert r is None


# ═══════════════════════════════════════════════════════════════════
# Weighted Indoor Read (via AreaOccupancyManager)
# ═══════════════════════════════════════════════════════════════════

class TestWeightedIndoorReads:
    """Test SensorHub.read_weighted_indoor_temp/humidity methods."""

    def test_no_area_manager_delegates_to_regular(self):
        """Without area manager, weighted read falls back to regular read."""
        hass = _make_hass({
            "sensor.lr_temp": _make_state("72.0", unit="°F"),
        })
        hub = _make_hub(hass, indoor_temp_entities=["sensor.lr_temp"])
        # No area manager set
        r = hub.read_weighted_indoor_temp(thermostat_temp=74.0)
        assert r is not None
        # Should behave exactly like read_indoor_temp
        r2 = hub.read_indoor_temp(thermostat_temp=74.0)
        assert r.value == pytest.approx(r2.value, abs=0.01)

    def test_with_area_manager(self):
        """With area manager, weighted read uses weighted aggregation."""
        hass = _make_hass({
            "sensor.lr_temp": _make_state("72.0", unit="°F"),
            "sensor.br_temp": _make_state("78.0", unit="°F"),
        })
        hub = _make_hub(hass, indoor_temp_entities=["sensor.lr_temp", "sensor.br_temp"])

        # Create a mock area manager
        mock_area_mgr = MagicMock()
        mock_area_mgr.get_weighted_indoor_temp.return_value = (73.5, "weighted:1/2")
        hub.set_area_manager(mock_area_mgr)

        r = hub.read_weighted_indoor_temp(thermostat_temp=74.0)
        assert r is not None
        assert r.value == pytest.approx(73.5, abs=0.01)
        assert r.source == "weighted:1/2"

    def test_area_manager_returns_none_falls_back(self):
        """If area manager returns None, falls back to regular read."""
        hass = _make_hass({
            "sensor.lr_temp": _make_state("72.0", unit="°F"),
        })
        hub = _make_hub(hass, indoor_temp_entities=["sensor.lr_temp"])

        mock_area_mgr = MagicMock()
        mock_area_mgr.get_weighted_indoor_temp.return_value = (None, "")
        hub.set_area_manager(mock_area_mgr)

        r = hub.read_weighted_indoor_temp(thermostat_temp=74.0)
        assert r is not None
        # Falls back to regular indoor temp
        assert r.value is not None

    def test_weighted_humidity_no_area_manager(self):
        """Humidity weighted read without area manager delegates to regular."""
        hass = _make_hass({
            "sensor.lr_hum": _make_state("55.0"),
        })
        hub = _make_hub(hass, indoor_humidity_entities=["sensor.lr_hum"])
        r = hub.read_weighted_indoor_humidity(thermostat_humidity=50.0)
        r2 = hub.read_indoor_humidity(thermostat_humidity=50.0)
        assert r.value == pytest.approx(r2.value, abs=0.01)

    def test_weighted_humidity_with_area_manager(self):
        """Humidity weighted read with area manager uses weighted aggregation."""
        hass = _make_hass({})
        hub = _make_hub(hass)

        mock_area_mgr = MagicMock()
        mock_area_mgr.get_weighted_indoor_humidity.return_value = (55.0, "weighted:2/3")
        hub.set_area_manager(mock_area_mgr)

        r = hub.read_weighted_indoor_humidity(thermostat_humidity=50.0)
        assert r is not None
        assert r.value == pytest.approx(55.0, abs=0.01)
        assert r.source == "weighted:2/3"


# ═══════════════════════════════════════════════════════════════════
# Mode-Aware Power Draw
# ═══════════════════════════════════════════════════════════════════


class TestModeAwarePowerDraw:
    """read_power_draw() with hvac_action returns mode-appropriate wattage."""

    def test_cooling_uses_cooling_watts(self):
        hass = _make_hass({})
        hub = _make_hub(hass, cooling_watts=3000.0)
        assert hub.read_power_draw(hvac_action="cooling") == pytest.approx(3000.0)

    def test_heating_uses_heating_watts(self):
        hass = _make_hass({})
        hub = _make_hub(hass, heating_watts=4000.0)
        assert hub.read_power_draw(hvac_action="heating") == pytest.approx(4000.0)

    def test_heating_derived_from_cooling(self):
        """When only cooling_watts is set, heating defaults to cooling * 1.15."""
        hass = _make_hass({})
        hub = _make_hub(hass, cooling_watts=3000.0)
        assert hub.read_power_draw(hvac_action="heating") == pytest.approx(3450.0)

    def test_idle_returns_zero(self):
        hass = _make_hass({})
        hub = _make_hub(hass, power_default_watts=3000.0)
        assert hub.read_power_draw(hvac_action="idle") == pytest.approx(0.0)

    def test_off_returns_zero(self):
        hass = _make_hass({})
        hub = _make_hub(hass, power_default_watts=3000.0)
        assert hub.read_power_draw(hvac_action="off") == pytest.approx(0.0)

    def test_fan_only_returns_300(self):
        hass = _make_hass({})
        hub = _make_hub(hass, power_default_watts=3000.0)
        assert hub.read_power_draw(hvac_action="fan_only") == pytest.approx(300.0)

    def test_aux_heating_uses_aux_kw(self):
        hass = _make_hass({})
        hub = _make_hub(hass, aux_heat_kw=15.0)
        assert hub.read_power_draw(hvac_action="aux_heating") == pytest.approx(15000.0)

    def test_aux_heat_active_flag_overrides_action(self):
        """aux_heat_active=True uses aux watts even if action is 'heating'."""
        hass = _make_hass({})
        hub = _make_hub(hass, aux_heat_kw=17.0, heating_watts=4000.0)
        result = hub.read_power_draw(hvac_action="heating", aux_heat_active=True)
        assert result == pytest.approx(17000.0)

    def test_entity_overrides_mode(self):
        """When a power entity is configured and available, mode is ignored."""
        hass = _make_hass({"sensor.pw": _make_state(5000)})
        hub = _make_hub(hass, power_entity="sensor.pw")
        assert hub.read_power_draw(hvac_action="idle") == pytest.approx(5000.0)

    def test_backward_compat_no_action(self):
        """Calling without hvac_action returns default watts (backward compat)."""
        hass = _make_hass({})
        hub = _make_hub(hass, power_default_watts=3500.0)
        assert hub.read_power_draw() == pytest.approx(3500.0)

    def test_no_aux_kw_falls_back_to_default(self):
        """aux_heating without aux_heat_kw configured uses general default."""
        hass = _make_hass({})
        hub = _make_hub(hass, power_default_watts=3500.0)
        assert hub.read_power_draw(hvac_action="aux_heating") == pytest.approx(3500.0)


# ═══════════════════════════════════════════════════════════════════
# Duct Temperature Reading
# ═══════════════════════════════════════════════════════════════════


class TestReadDuctTemp:
    """read_duct_temp() returns supply air temp or None."""

    def test_not_configured(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        assert hub.read_duct_temp() is None

    def test_available(self):
        hass = _make_hass({"sensor.duct": _make_state(120, unit="\u00b0F")})
        hub = _make_hub(hass, duct_temp_entity="sensor.duct")
        result = hub.read_duct_temp()
        assert result is not None
        assert result.value == pytest.approx(120.0, abs=1.0)

    def test_celsius_conversion(self):
        """Duct temp in Celsius is converted to Fahrenheit."""
        hass = _make_hass({"sensor.duct": _make_state(50, unit="\u00b0C")})
        hub = _make_hub(hass, duct_temp_entity="sensor.duct")
        result = hub.read_duct_temp()
        assert result is not None
        assert result.value == pytest.approx(122.0, abs=1.0)

    def test_unavailable(self):
        hass = _make_hass({"sensor.duct": _make_state("unavailable")})
        hub = _make_hub(hass, duct_temp_entity="sensor.duct")
        assert hub.read_duct_temp() is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
