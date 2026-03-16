"""Tests for Better Thermostat-inspired features:
- EMA temperature smoothing
- Debounced window/door events
- Setpoint switching penalty
"""

import importlib
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

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

if "custom_components.heatpump_optimizer.engine" not in sys.modules:
    engine_pkg = types.ModuleType("custom_components.heatpump_optimizer.engine")
    engine_pkg.__path__ = [os.path.join(CC, "engine")]
    sys.modules["custom_components.heatpump_optimizer.engine"] = engine_pkg
else:
    engine_pkg = sys.modules["custom_components.heatpump_optimizer.engine"]

# ── Stub homeassistant modules ─────────────────────────────────────

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
        "CELSIUS": "°C", "FAHRENHEIT": "°F",
    })
    ha_const.UnitOfSpeed = type("UnitOfSpeed", (), {
        "KILOMETERS_PER_HOUR": "km/h", "MILES_PER_HOUR": "mph",
        "METERS_PER_SECOND": "m/s",
    })
    ha_const.UnitOfPressure = type("UnitOfPressure", (), {
        "HPA": "hPa", "INHG": "inHg", "MBAR": "mbar", "PSI": "psi",
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


# ── Load actual modules via importlib ───────────────────────────────

def _load(full_name: str, path: str):
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


const_mod = _load(
    "custom_components.heatpump_optimizer.const",
    os.path.join(CC, "const.py"),
)
ho.const = const_mod

dt_mod = _load(
    "custom_components.heatpump_optimizer.engine.data_types",
    os.path.join(CC, "engine", "data_types.py"),
)
engine_pkg.data_types = dt_mod

# Stub performance_model and thermal_simulator for optimizer import
perf_mod = _load(
    "custom_components.heatpump_optimizer.engine.performance_model",
    os.path.join(CC, "engine", "performance_model.py"),
)
engine_pkg.performance_model = perf_mod

sim_mod = _load(
    "custom_components.heatpump_optimizer.engine.thermal_simulator",
    os.path.join(CC, "engine", "thermal_simulator.py"),
)
engine_pkg.thermal_simulator = sim_mod

sh_mod = _load(
    "custom_components.heatpump_optimizer.adapters.sensor_hub",
    os.path.join(CC, "adapters", "sensor_hub.py"),
)

opt_mod = _load(
    "custom_components.heatpump_optimizer.engine.optimizer",
    os.path.join(CC, "engine", "optimizer.py"),
)

SensorHub = sh_mod.SensorHub
SensorReading = sh_mod.SensorReading
ScheduleOptimizer = opt_mod.ScheduleOptimizer
ScheduleEntry = dt_mod.ScheduleEntry
HourScore = dt_mod.HourScore


# ── Helpers ─────────────────────────────────────────────────────────

def _make_state(value, unit=None, age_minutes=0, attributes=None):
    s = MagicMock()
    s.state = str(value)
    attrs = attributes or {}
    if unit is not None:
        attrs["unit_of_measurement"] = unit
    s.attributes = attrs
    s.last_updated = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return s


def _make_hass(state_map=None):
    hass = MagicMock()
    state_map = state_map or {}
    hass.states.get = MagicMock(side_effect=lambda eid: state_map.get(eid))
    return hass


def _make_hub(hass, **kwargs):
    return SensorHub(hass, **kwargs)


# ═══════════════════════════════════════════════════════════════════
# Feature 1: EMA Temperature Smoothing
# ═══════════════════════════════════════════════════════════════════

class TestEmaSmoothing:
    """EMA temperature smoothing on indoor and outdoor readings."""

    def test_apply_ema_cold_start(self):
        """First call returns the raw value (no prior state)."""
        result = SensorHub._apply_ema(72.0, None, 0.2)
        assert result == 72.0

    def test_apply_ema_convergence(self):
        """EMA converges toward step input over multiple calls."""
        prev = 70.0
        for _ in range(20):
            prev = SensorHub._apply_ema(75.0, prev, 0.2)
        # After 20 steps at alpha=0.2, should be very close to 75
        assert abs(prev - 75.0) < 0.1

    def test_apply_ema_smooths_noise(self):
        """EMA reduces variance of noisy signal."""
        import random
        random.seed(42)
        raw_values = [72.0 + random.gauss(0, 1.0) for _ in range(50)]
        raw_variance = sum((v - 72.0) ** 2 for v in raw_values) / len(raw_values)

        ema_values = []
        prev = None
        for v in raw_values:
            prev = SensorHub._apply_ema(v, prev, 0.2)
            ema_values.append(prev)

        ema_variance = sum((v - 72.0) ** 2 for v in ema_values) / len(ema_values)
        assert ema_variance < raw_variance

    def test_indoor_temp_ema_applied(self):
        """read_indoor_temp applies EMA across successive calls."""
        hass = _make_hass({"sensor.t": _make_state(72, unit="°F")})
        hub = _make_hub(hass, indoor_temp_entities=["sensor.t"])

        # First read: cold start, returns raw
        r1 = hub.read_indoor_temp()
        assert r1.value == pytest.approx(72.0)

        # Change sensor to 77°F — with EMA(0.2) the smoothed value should lag
        hass.states.get = MagicMock(
            side_effect=lambda eid: _make_state(77, unit="°F")
        )
        r2 = hub.read_indoor_temp()
        # EMA: 0.2 * 77 + 0.8 * 72 = 73.0
        assert r2.value == pytest.approx(73.0)

    def test_outdoor_temp_ema_applied(self):
        """read_outdoor_temp applies EMA to entity readings."""
        hass = _make_hass({"sensor.out": _make_state(55, unit="°F")})
        hub = _make_hub(hass, outdoor_temp_entities=["sensor.out"])

        r1 = hub.read_outdoor_temp()
        assert r1.value == pytest.approx(55.0)

        hass.states.get = MagicMock(
            side_effect=lambda eid: _make_state(60, unit="°F")
        )
        r2 = hub.read_outdoor_temp()
        # EMA: 0.2 * 60 + 0.8 * 55 = 56.0
        assert r2.value == pytest.approx(56.0)

    def test_outdoor_temp_forecast_bypasses_ema(self):
        """Forecast-sourced outdoor temps do NOT get EMA smoothed."""
        hass = _make_hass({})  # no outdoor entity
        hub = _make_hub(hass)

        now = datetime.now(timezone.utc)
        forecast = [dt_mod.ForecastPoint(time=now, outdoor_temp=65.0)]

        r1 = hub.read_outdoor_temp(forecast_snapshot=forecast)
        assert r1.value == pytest.approx(65.0)
        assert r1.source == "forecast"

    def test_indoor_temp_thermostat_fallback_ema(self):
        """Thermostat-only indoor reading also gets EMA smoothed."""
        hass = _make_hass({})  # no extra indoor sensors
        hub = _make_hub(hass)

        r1 = hub.read_indoor_temp(thermostat_temp=70.0)
        assert r1.value == pytest.approx(70.0)

        r2 = hub.read_indoor_temp(thermostat_temp=75.0)
        # EMA: 0.2 * 75 + 0.8 * 70 = 71.0
        assert r2.value == pytest.approx(71.0)


# ═══════════════════════════════════════════════════════════════════
# Feature 2: Debounced Window/Door Events
# ═══════════════════════════════════════════════════════════════════

class TestDoorWindowDebounce:
    """Debounced door/window open count."""

    def test_no_entities_returns_zero(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        assert hub.read_door_window_open_count() == (0, 0)

    def test_initial_state_captured(self):
        """First read captures current state without debounce delay."""
        hass = _make_hass({
            "binary_sensor.door": _make_state("on"),
        })
        hub = _make_hub(hass, door_window_entities=["binary_sensor.door"])
        open_count, total = hub.read_door_window_open_count()
        assert open_count == 1
        assert total == 1

    def test_brief_open_ignored(self):
        """A door that opens and closes within debounce period stays closed."""
        door_state = _make_state("off")
        hass = _make_hass({"binary_sensor.door": door_state})
        hub = _make_hub(hass, door_window_entities=["binary_sensor.door"])

        # Initial state: closed
        hub.read_door_window_open_count()
        assert hub._dw_debounced["binary_sensor.door"] is False

        # Door opens
        hass.states.get = MagicMock(
            side_effect=lambda eid: _make_state("on")
        )
        open_count, _ = hub.read_door_window_open_count()
        # Should still be 0 (debounce not elapsed)
        assert open_count == 0

        # Door closes before debounce expires
        hass.states.get = MagicMock(
            side_effect=lambda eid: _make_state("off")
        )
        open_count, _ = hub.read_door_window_open_count()
        assert open_count == 0

    def test_sustained_open_accepted(self):
        """A door open for > debounce seconds is accepted."""
        hass = _make_hass({"binary_sensor.door": _make_state("off")})
        hub = _make_hub(hass, door_window_entities=["binary_sensor.door"])
        hub._dw_debounce_seconds = 120

        # Initial: closed
        hub.read_door_window_open_count()

        # Door opens
        hass.states.get = MagicMock(
            side_effect=lambda eid: _make_state("on")
        )
        hub.read_door_window_open_count()

        # Simulate debounce timer having started 3 minutes ago
        hub._dw_last_raw["binary_sensor.door"] = (
            True,
            datetime.now(timezone.utc) - timedelta(seconds=180),
        )

        open_count, total = hub.read_door_window_open_count()
        assert open_count == 1
        assert total == 1

    def test_sustained_close_accepted(self):
        """A door closing for > debounce seconds transitions back."""
        hass = _make_hass({"binary_sensor.door": _make_state("on")})
        hub = _make_hub(hass, door_window_entities=["binary_sensor.door"])
        hub._dw_debounce_seconds = 120

        # Initial: open
        hub.read_door_window_open_count()
        assert hub._dw_debounced["binary_sensor.door"] is True

        # Door closes
        hass.states.get = MagicMock(
            side_effect=lambda eid: _make_state("off")
        )
        hub.read_door_window_open_count()

        # Fast-forward past debounce
        hub._dw_last_raw["binary_sensor.door"] = (
            False,
            datetime.now(timezone.utc) - timedelta(seconds=180),
        )

        open_count, _ = hub.read_door_window_open_count()
        assert open_count == 0

    def test_unavailable_sensor_excluded(self):
        """Unavailable sensors don't count toward total."""
        hass = _make_hass({
            "binary_sensor.door": _make_state("unavailable"),
        })
        hub = _make_hub(hass, door_window_entities=["binary_sensor.door"])
        open_count, total = hub.read_door_window_open_count()
        assert open_count == 0
        assert total == 0

    def test_multiple_sensors_independent(self):
        """Each sensor debounces independently."""
        hass = _make_hass({
            "binary_sensor.door1": _make_state("off"),
            "binary_sensor.door2": _make_state("on"),
        })
        hub = _make_hub(
            hass,
            door_window_entities=["binary_sensor.door1", "binary_sensor.door2"],
        )
        hub._dw_debounce_seconds = 120

        open_count, total = hub.read_door_window_open_count()
        # door1 initialized closed, door2 initialized open
        assert total == 2
        assert open_count == 1


# ═══════════════════════════════════════════════════════════════════
# Feature 3: Setpoint Switching Penalty
# ═══════════════════════════════════════════════════════════════════

class TestSwitchingPenalty:
    """_smooth_switching reduces gratuitous setpoint oscillation."""

    def _make_entries(self, temps):
        """Create schedule entries with given temperatures."""
        base = datetime(2024, 7, 1, tzinfo=timezone.utc)
        return [
            ScheduleEntry(
                start_time=base + timedelta(hours=i),
                end_time=base + timedelta(hours=i + 1),
                target_temp=t,
                mode="cool",
                reason="test",
            )
            for i, t in enumerate(temps)
        ]

    def test_no_smoothing_when_stable(self):
        """Entries within threshold are untouched."""
        entries = self._make_entries([72.0, 72.5, 72.0, 72.5])
        result = ScheduleOptimizer._smooth_switching(entries, weight=0.3)
        temps = [e.target_temp for e in result]
        assert temps == [72.0, 72.5, 72.0, 72.5]

    def test_oscillation_reduced(self):
        """Large oscillations are dampened."""
        entries = self._make_entries([70.0, 74.0, 70.0, 74.0])
        result = ScheduleOptimizer._smooth_switching(entries, weight=0.3)
        temps = [e.target_temp for e in result]

        # Each 4°F jump should be reduced
        for i in range(1, len(temps)):
            assert abs(temps[i] - temps[i - 1]) < 4.0

    def test_large_efficiency_change_passes_through(self):
        """Smoothing caps at 50% of delta, so large changes still happen."""
        entries = self._make_entries([70.0, 78.0])
        result = ScheduleOptimizer._smooth_switching(entries, weight=0.3)
        temps = [e.target_temp for e in result]

        # 8°F change: correction = min(0.3 * (8-1), 8*0.5) = min(2.1, 4.0) = 2.1
        # New temp: 78 - 2.1 = 75.9 → rounded to 76.0
        assert temps[1] == pytest.approx(76.0, abs=0.5)

    def test_weight_zero_no_smoothing(self):
        """Weight=0 disables smoothing entirely."""
        entries = self._make_entries([70.0, 78.0, 70.0])
        result = ScheduleOptimizer._smooth_switching(entries, weight=0.0)
        temps = [e.target_temp for e in result]
        assert temps == [70.0, 78.0, 70.0]

    def test_single_entry_unchanged(self):
        """Single entry list passes through."""
        entries = self._make_entries([72.0])
        result = ScheduleOptimizer._smooth_switching(entries, weight=0.3)
        assert len(result) == 1
        assert result[0].target_temp == 72.0

    def test_thermostat_resolution_preserved(self):
        """Output temps remain at 0.5°F resolution."""
        entries = self._make_entries([70.0, 73.0, 70.0])
        result = ScheduleOptimizer._smooth_switching(entries, weight=0.3)
        for e in result:
            assert e.target_temp * 2 == int(e.target_temp * 2)


# ═══════════════════════════════════════════════════════════════════
# Feature 4: Sensor Reading max_spread
# ═══════════════════════════════════════════════════════════════════

class TestSensorSpread:
    """max_spread field on SensorReading for divergence detection."""

    def test_single_sensor_zero_spread(self):
        hass = _make_hass({"sensor.t1": _make_state(72, unit="°F")})
        hub = _make_hub(hass)
        r = hub._read_multi_temp(["sensor.t1"], "Test")
        assert r.max_spread == 0.0

    def test_two_sensors_spread_computed(self):
        hass = _make_hass({
            "sensor.t1": _make_state(70, unit="°F"),
            "sensor.t2": _make_state(75, unit="°F"),
        })
        hub = _make_hub(hass)
        r = hub._read_multi_temp(["sensor.t1", "sensor.t2"], "Test")
        assert r.max_spread == pytest.approx(5.0)

    def test_three_sensors_max_spread(self):
        hass = _make_hass({
            "sensor.t1": _make_state(70, unit="°F"),
            "sensor.t2": _make_state(72, unit="°F"),
            "sensor.t3": _make_state(77, unit="°F"),
        })
        hub = _make_hub(hass)
        r = hub._read_multi_temp(["sensor.t1", "sensor.t2", "sensor.t3"], "Test")
        assert r.max_spread == pytest.approx(7.0)
