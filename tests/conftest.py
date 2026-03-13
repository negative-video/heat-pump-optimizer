"""Shared test infrastructure for the Heat Pump Optimizer integration.

HA module stubs are registered at module level (not in fixtures) so they're
available during test collection when files do top-level imports.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── Path constants ──────────────────────────────────────────────────

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
CC = os.path.join(PROJECT_ROOT, "custom_components", "heatpump_optimizer")

sys.path.insert(0, PROJECT_ROOT)

# ── Unit conversion helpers (real math, not mocks) ──────────────────


class TemperatureConverter:
    @staticmethod
    def convert(value, from_unit, to_unit):
        if from_unit == "°C" and to_unit == "°F":
            return value * 9.0 / 5.0 + 32.0
        if from_unit == "°F" and to_unit == "°C":
            return (value - 32.0) * 5.0 / 9.0
        return value


class SpeedConverter:
    @staticmethod
    def convert(value, from_unit, to_unit):
        TO_MS = {"km/h": 1 / 3.6, "mph": 0.44704, "m/s": 1.0}
        FROM_MS = {"km/h": 3.6, "mph": 1 / 0.44704, "m/s": 1.0}
        ms = value * TO_MS.get(from_unit, 1.0)
        return ms * FROM_MS.get(to_unit, 1.0)


class PressureConverter:
    @staticmethod
    def convert(value, from_unit, to_unit):
        TO_HPA = {"inHg": 33.8639, "psi": 68.9476, "hPa": 1.0, "mbar": 1.0}
        FROM_HPA = {"inHg": 1 / 33.8639, "psi": 1 / 68.9476, "hPa": 1.0, "mbar": 1.0}
        hpa = value * TO_HPA.get(from_unit, 1.0)
        return hpa * FROM_HPA.get(to_unit, 1.0)


# ── Register HA module stubs at import time ─────────────────────────
# These must run before any test file tries to import from the integration.
# Uses setdefault so tests with their own stubs (legacy pattern) aren't clobbered.


def _register_stubs():
    """Register all homeassistant module stubs."""

    # -- Package stubs for custom_components --
    pkg = types.ModuleType("custom_components")
    pkg.__path__ = [os.path.join(PROJECT_ROOT, "custom_components")]
    sys.modules.setdefault("custom_components", pkg)

    ho = types.ModuleType("custom_components.heatpump_optimizer")
    ho.__path__ = [CC]
    sys.modules.setdefault("custom_components.heatpump_optimizer", ho)

    # Sub-package stubs (adapters, engine, learning, controllers) are NOT
    # registered here. Many test files import real modules from these packages,
    # and a stub package would prevent Python from finding the actual .py files.
    # Test files that need sub-package stubs for importlib loading create them
    # themselves (e.g., test_sensor_hub.py, test_occupancy.py).

    # -- homeassistant core stubs --
    ha_mod = types.ModuleType("homeassistant")
    ha_mod.__path__ = ["homeassistant"]
    sys.modules.setdefault("homeassistant", ha_mod)

    ha_core = types.ModuleType("homeassistant.core")
    ha_core.HomeAssistant = MagicMock
    ha_core.ServiceCall = MagicMock
    ha_core.callback = lambda f: f
    sys.modules.setdefault("homeassistant.core", ha_core)

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
    ha_const.EntityCategory = type("EntityCategory", (), {
        "DIAGNOSTIC": "diagnostic",
        "CONFIG": "config",
    })
    ha_const.CONF_NAME = "name"
    sys.modules.setdefault("homeassistant.const", ha_const)

    # -- homeassistant.util --
    ha_util = types.ModuleType("homeassistant.util")
    ha_util.__path__ = ["homeassistant/util"]
    sys.modules.setdefault("homeassistant.util", ha_util)

    ha_unit_conv = types.ModuleType("homeassistant.util.unit_conversion")
    ha_unit_conv.TemperatureConverter = TemperatureConverter
    ha_unit_conv.SpeedConverter = SpeedConverter
    ha_unit_conv.PressureConverter = PressureConverter
    sys.modules.setdefault("homeassistant.util.unit_conversion", ha_unit_conv)

    # -- homeassistant.helpers --
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_helpers.__path__ = ["homeassistant/helpers"]
    sys.modules.setdefault("homeassistant.helpers", ha_helpers)

    ha_device_reg = types.ModuleType("homeassistant.helpers.device_registry")
    ha_device_reg.DeviceInfo = dict
    ha_device_reg.async_get = MagicMock()
    sys.modules.setdefault("homeassistant.helpers.device_registry", ha_device_reg)

    ha_entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
    ha_entity_platform.AddEntitiesCallback = MagicMock
    sys.modules.setdefault("homeassistant.helpers.entity_platform", ha_entity_platform)

    ha_update_coord = types.ModuleType("homeassistant.helpers.update_coordinator")

    class _CoordinatorEntity:
        def __init__(self, coordinator=None, *args, **kwargs):
            self.coordinator = coordinator

        def async_write_ha_state(self):
            pass

    ha_update_coord.CoordinatorEntity = _CoordinatorEntity
    ha_update_coord.DataUpdateCoordinator = MagicMock
    sys.modules.setdefault("homeassistant.helpers.update_coordinator", ha_update_coord)

    # -- homeassistant.config_entries --
    ha_config_entries = types.ModuleType("homeassistant.config_entries")
    ha_config_entries.ConfigEntry = MagicMock
    ha_config_entries.ConfigFlow = type("ConfigFlow", (), {
        "__init_subclass__": classmethod(lambda cls, **kw: None),
        "async_set_unique_id": AsyncMock(),
        "async_show_form": AsyncMock(),
        "async_create_entry": AsyncMock(),
    })
    ha_config_entries.OptionsFlow = type("OptionsFlow", (), {})
    sys.modules.setdefault("homeassistant.config_entries", ha_config_entries)

    # -- homeassistant.components --
    ha_components = types.ModuleType("homeassistant.components")
    ha_components.__path__ = ["homeassistant/components"]
    sys.modules.setdefault("homeassistant.components", ha_components)

    ha_switch = types.ModuleType("homeassistant.components.switch")
    ha_switch.SwitchEntity = type("SwitchEntity", (), {})
    sys.modules.setdefault("homeassistant.components.switch", ha_switch)

    ha_binary_sensor = types.ModuleType("homeassistant.components.binary_sensor")
    ha_binary_sensor.BinarySensorEntity = type("BinarySensorEntity", (), {})
    ha_binary_sensor.BinarySensorDeviceClass = type("BinarySensorDeviceClass", (), {
        "RUNNING": "running",
        "PROBLEM": "problem",
        "HEAT": "heat",
    })
    sys.modules.setdefault("homeassistant.components.binary_sensor", ha_binary_sensor)

    ha_sensor = types.ModuleType("homeassistant.components.sensor")
    ha_sensor.SensorEntity = type("SensorEntity", (), {})
    ha_sensor.SensorDeviceClass = type("SensorDeviceClass", (), {
        "TEMPERATURE": "temperature",
        "POWER": "power",
        "ENERGY": "energy",
        "MONETARY": "monetary",
    })
    ha_sensor.SensorStateClass = type("SensorStateClass", (), {
        "MEASUREMENT": "measurement",
        "TOTAL": "total",
        "TOTAL_INCREASING": "total_increasing",
    })
    sys.modules.setdefault("homeassistant.components.sensor", ha_sensor)

    # -- voluptuous stub --
    vol_mod = types.ModuleType("voluptuous")
    vol_mod.Schema = lambda x: x
    vol_mod.Required = lambda x: x
    vol_mod.Optional = lambda x, **kw: x
    vol_mod.In = lambda x: x
    vol_mod.Coerce = lambda x: x
    vol_mod.All = lambda *x: x
    vol_mod.Any = lambda *x: x
    vol_mod.ALLOW_EXTRA = "ALLOW_EXTRA"
    sys.modules.setdefault("voluptuous", vol_mod)

    # -- homeassistant.helpers.storage --
    ha_storage = types.ModuleType("homeassistant.helpers.storage")
    ha_storage.Store = MagicMock
    sys.modules.setdefault("homeassistant.helpers.storage", ha_storage)


# Run stubs immediately at import time
_register_stubs()


# ── Module loader helper ────────────────────────────────────────────


def load_module(full_name: str, path: str):
    """Load a Python module by file path, registering it in sys.modules."""
    spec = importlib.util.spec_from_file_location(full_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Reusable helpers (importable, not fixtures) ─────────────────────


def make_state(value, attrs=None):
    """Create a mock HA state object."""
    state = SimpleNamespace()
    state.state = str(value)
    state.attributes = attrs or {}
    state.last_updated = datetime.now(timezone.utc)
    return state


# ── Pytest fixtures ─────────────────────────────────────────────────


@pytest.fixture
def mock_hass():
    """Create a minimal mock hass object."""
    hass = MagicMock()
    hass.data = {}
    hass.states.get.return_value = None
    hass.config.units.wind_speed_unit = "mph"
    hass.services.async_register = MagicMock()
    hass.services.async_remove = MagicMock()
    hass.bus.async_fire = MagicMock()
    return hass


@pytest.fixture
def mock_config_entry():
    """Create a mock ConfigEntry with sensible defaults."""
    entry = MagicMock()
    entry.entry_id = "test_entry_123"
    entry.data = {
        "climate_entity": "climate.thermostat",
        "weather_entity": "weather.home",
        "comfort_cool_min": 70.0,
        "comfort_cool_max": 78.0,
        "comfort_heat_min": 64.0,
        "comfort_heat_max": 70.0,
        "safety_cool_max": 85.0,
        "safety_heat_min": 50.0,
        "initialization_mode": "learning",
    }
    entry.options = {}
    entry.title = "Heat Pump Optimizer"
    return entry


@pytest.fixture
def mock_coordinator():
    """Create a mock coordinator with typical data."""
    coordinator = MagicMock()
    coordinator.data = {
        "active": True,
        "phase": "idle",
        "target_setpoint": 74.0,
        "override_detected": False,
        "sensor_stale": False,
        "aux_heat_active": False,
        "learning_active": False,
        "kalman_confidence": 0.75,
        "kalman_observations": 500,
        "initialization_mode": "learning",
    }
    coordinator.async_force_reoptimize = AsyncMock()
    coordinator.pause = MagicMock()
    coordinator.async_resume = AsyncMock()
    coordinator.set_occupancy = MagicMock()
    coordinator.async_demand_response = AsyncMock()
    coordinator.export_model = MagicMock(return_value={"estimator_state": {}})
    coordinator.import_model = MagicMock()
    coordinator.async_set_constraint = AsyncMock()
    return coordinator
