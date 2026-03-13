"""Tests for the OccupancyAdapter — multi-entity, debounce, backward compat."""

import importlib
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

# ── Module loading (same pattern as test_thermal_estimator.py) ───────

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, PROJECT_ROOT)

CC = os.path.join(PROJECT_ROOT, "custom_components", "heatpump_optimizer")

# Create package stubs
pkg = types.ModuleType("custom_components")
pkg.__path__ = [os.path.join(PROJECT_ROOT, "custom_components")]
sys.modules.setdefault("custom_components", pkg)

ho = types.ModuleType("custom_components.heatpump_optimizer")
ho.__path__ = [CC]
sys.modules.setdefault("custom_components.heatpump_optimizer", ho)

adapters = types.ModuleType("custom_components.heatpump_optimizer.adapters")
adapters.__path__ = [os.path.join(CC, "adapters")]
sys.modules.setdefault("custom_components.heatpump_optimizer.adapters", adapters)

# Stub homeassistant.core so the import succeeds
ha_mod = types.ModuleType("homeassistant")
ha_mod.__path__ = ["homeassistant"]
sys.modules.setdefault("homeassistant", ha_mod)

ha_core = types.ModuleType("homeassistant.core")
ha_core.HomeAssistant = MagicMock  # placeholder class
sys.modules.setdefault("homeassistant.core", ha_core)


def _load(full_name: str, path: str):
    spec = importlib.util.spec_from_file_location(full_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


occ_mod = _load(
    "custom_components.heatpump_optimizer.adapters.occupancy",
    os.path.join(CC, "adapters", "occupancy.py"),
)
adapters.occupancy = occ_mod

OccupancyAdapter = occ_mod.OccupancyAdapter
OccupancyMode = occ_mod.OccupancyMode
AWAY_COMFORT_DELTA = occ_mod.AWAY_COMFORT_DELTA
VACATION_COOL_SETPOINT = occ_mod.VACATION_COOL_SETPOINT
VACATION_HEAT_SETPOINT = occ_mod.VACATION_HEAT_SETPOINT


# ── Helpers ──────────────────────────────────────────────────────────


def _make_state(value: str) -> MagicMock:
    """Create a mock HA State object with the given state string."""
    s = MagicMock()
    s.state = value
    return s


def _make_hass(entity_states: dict[str, str]) -> MagicMock:
    """Create a mock hass object whose states.get returns the right values.

    Args:
        entity_states: mapping of entity_id -> state string.
    """
    hass = MagicMock()
    hass.states.get.side_effect = lambda eid: (
        _make_state(entity_states[eid]) if eid in entity_states else None
    )
    return hass


# ── Tests ────────────────────────────────────────────────────────────


class TestOccupancyInit:
    """Constructor and entity_ids handling."""

    def test_entity_ids_list(self):
        hass = _make_hass({})
        adapter = OccupancyAdapter(hass, entity_ids=["person.alice", "person.bob"])
        assert adapter.entity_ids == ["person.alice", "person.bob"]

    def test_backward_compat_singular_entity_id(self):
        hass = _make_hass({})
        adapter = OccupancyAdapter(hass, entity_id="person.alice")
        assert adapter.entity_ids == ["person.alice"]

    def test_no_entities(self):
        hass = _make_hass({})
        adapter = OccupancyAdapter(hass)
        assert adapter.entity_ids == []

    def test_entity_ids_takes_precedence_over_entity_id(self):
        hass = _make_hass({})
        adapter = OccupancyAdapter(
            hass, entity_ids=["person.bob"], entity_id="person.alice"
        )
        # entity_ids is not None, so entity_id is ignored
        assert adapter.entity_ids == ["person.bob"]

    def test_debounce_default(self):
        hass = _make_hass({})
        adapter = OccupancyAdapter(hass)
        assert adapter.debounce_minutes == 5

    def test_debounce_custom(self):
        hass = _make_hass({})
        adapter = OccupancyAdapter(hass, debounce_minutes=10)
        assert adapter.debounce_minutes == 10


class TestInterpretState:
    """Unit tests for _interpret_state static method."""

    @pytest.mark.parametrize("value", ["home", "Home", "HOME", "on", "On", "ON"])
    def test_home_states(self, value):
        assert OccupancyAdapter._interpret_state(value) == OccupancyMode.HOME

    @pytest.mark.parametrize("value", ["not_home", "away", "off", "Away", "OFF"])
    def test_away_states(self, value):
        assert OccupancyAdapter._interpret_state(value) == OccupancyMode.AWAY

    @pytest.mark.parametrize("value", ["vacation", "extended_away", "Vacation", "EXTENDED_AWAY"])
    def test_vacation_states(self, value):
        assert OccupancyAdapter._interpret_state(value) == OccupancyMode.VACATION

    def test_unknown_defaults_to_home(self):
        assert OccupancyAdapter._interpret_state("unknown") == OccupancyMode.HOME
        assert OccupancyAdapter._interpret_state("unavailable") == OccupancyMode.HOME

    def test_whitespace_stripped(self):
        assert OccupancyAdapter._interpret_state("  home  ") == OccupancyMode.HOME
        assert OccupancyAdapter._interpret_state(" away ") == OccupancyMode.AWAY

    def test_case_insensitive(self):
        assert OccupancyAdapter._interpret_state("HoMe") == OccupancyMode.HOME
        assert OccupancyAdapter._interpret_state("VACATION") == OccupancyMode.VACATION


class TestGetMode:
    """Tests for get_mode logic, priority, and debounce."""

    def test_no_entities_returns_home(self):
        hass = _make_hass({})
        adapter = OccupancyAdapter(hass)
        assert adapter.get_mode() == OccupancyMode.HOME

    def test_forced_mode_overrides(self):
        hass = _make_hass({"person.alice": "away"})
        adapter = OccupancyAdapter(hass, entity_ids=["person.alice"])
        adapter.force_mode(OccupancyMode.HOME)
        assert adapter.get_mode() == OccupancyMode.HOME

    def test_any_home_means_home(self):
        hass = _make_hass({"person.alice": "home", "person.bob": "away"})
        adapter = OccupancyAdapter(hass, entity_ids=["person.alice", "person.bob"])
        assert adapter.get_mode() == OccupancyMode.HOME

    def test_all_away_returns_away(self):
        hass = _make_hass({"person.alice": "away", "person.bob": "not_home"})
        adapter = OccupancyAdapter(hass, entity_ids=["person.alice", "person.bob"])
        assert adapter.get_mode() == OccupancyMode.AWAY

    def test_any_vacation_returns_vacation(self):
        hass = _make_hass({"person.alice": "vacation", "person.bob": "away"})
        adapter = OccupancyAdapter(hass, entity_ids=["person.alice", "person.bob"])
        assert adapter.get_mode() == OccupancyMode.VACATION

    def test_home_takes_priority_over_vacation(self):
        """If someone is home and someone is on vacation, result is HOME."""
        hass = _make_hass({"person.alice": "home", "person.bob": "vacation"})
        adapter = OccupancyAdapter(hass, entity_ids=["person.alice", "person.bob"])
        assert adapter.get_mode() == OccupancyMode.HOME

    def test_debounce_keeps_home(self):
        """After going from home to away, debounce should keep HOME briefly."""
        hass = _make_hass({"person.alice": "home"})
        adapter = OccupancyAdapter(hass, entity_ids=["person.alice"], debounce_minutes=5)

        # First call: sees "home", records last_active timestamp
        assert adapter.get_mode() == OccupancyMode.HOME

        # Now person goes away
        hass.states.get.side_effect = lambda eid: _make_state("away")

        # Immediately after, debounce should keep HOME
        assert adapter.get_mode() == OccupancyMode.HOME

    def test_debounce_expires_to_away(self):
        """After debounce window expires, mode should transition to AWAY."""
        hass = _make_hass({"person.alice": "home"})
        adapter = OccupancyAdapter(hass, entity_ids=["person.alice"], debounce_minutes=5)

        # See home first
        assert adapter.get_mode() == OccupancyMode.HOME

        # Manually backdate the last_active to simulate time passing
        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        adapter._last_active["person.alice"] = old_time

        # Now person goes away
        hass.states.get.side_effect = lambda eid: _make_state("away")

        # Debounce expired, should be AWAY
        assert adapter.get_mode() == OccupancyMode.AWAY

    def test_missing_entity_ignored(self):
        """Entity that returns None from states.get should be skipped."""
        hass = _make_hass({"person.alice": "home"})
        adapter = OccupancyAdapter(
            hass, entity_ids=["person.alice", "person.missing"]
        )
        # person.missing returns None — should not cause error
        assert adapter.get_mode() == OccupancyMode.HOME

    def test_all_entities_missing_returns_away(self):
        """If all entities are missing, no one is home -> AWAY."""
        hass = _make_hass({})
        adapter = OccupancyAdapter(hass, entity_ids=["person.missing"])
        assert adapter.get_mode() == OccupancyMode.AWAY

    def test_binary_sensor_on_means_home(self):
        hass = _make_hass({"binary_sensor.occupancy": "on"})
        adapter = OccupancyAdapter(hass, entity_ids=["binary_sensor.occupancy"])
        assert adapter.get_mode() == OccupancyMode.HOME

    def test_binary_sensor_off_means_away(self):
        hass = _make_hass({"binary_sensor.occupancy": "off"})
        adapter = OccupancyAdapter(hass, entity_ids=["binary_sensor.occupancy"])
        assert adapter.get_mode() == OccupancyMode.AWAY


class TestForceMode:
    """Tests for force_mode method."""

    def test_force_away(self):
        hass = _make_hass({"person.alice": "home"})
        adapter = OccupancyAdapter(hass, entity_ids=["person.alice"])
        adapter.force_mode(OccupancyMode.AWAY)
        assert adapter.get_mode() == OccupancyMode.AWAY

    def test_force_vacation(self):
        hass = _make_hass({"person.alice": "home"})
        adapter = OccupancyAdapter(hass, entity_ids=["person.alice"])
        adapter.force_mode(OccupancyMode.VACATION)
        assert adapter.get_mode() == OccupancyMode.VACATION

    def test_clear_forced_mode(self):
        hass = _make_hass({"person.alice": "home"})
        adapter = OccupancyAdapter(hass, entity_ids=["person.alice"])
        adapter.force_mode(OccupancyMode.AWAY)
        assert adapter.get_mode() == OccupancyMode.AWAY

        adapter.force_mode(None)
        assert adapter.get_mode() == OccupancyMode.HOME


class TestAdjustComfortRange:
    """Tests for adjust_comfort_range."""

    def test_home_no_change(self):
        hass = _make_hass({"person.alice": "home"})
        adapter = OccupancyAdapter(hass, entity_ids=["person.alice"])
        result = adapter.adjust_comfort_range((70.0, 76.0), "cool")
        assert result == (70.0, 76.0)

    def test_away_widens_by_delta(self):
        hass = _make_hass({"person.alice": "away"})
        adapter = OccupancyAdapter(hass, entity_ids=["person.alice"])
        result = adapter.adjust_comfort_range((70.0, 76.0), "cool")
        assert result == (70.0 - AWAY_COMFORT_DELTA, 76.0 + AWAY_COMFORT_DELTA)

    def test_away_widens_heat_mode(self):
        hass = _make_hass({"person.alice": "away"})
        adapter = OccupancyAdapter(hass, entity_ids=["person.alice"])
        result = adapter.adjust_comfort_range((68.0, 72.0), "heat")
        assert result == (68.0 - AWAY_COMFORT_DELTA, 72.0 + AWAY_COMFORT_DELTA)

    def test_vacation_cool_mode(self):
        hass = _make_hass({"person.alice": "vacation"})
        adapter = OccupancyAdapter(hass, entity_ids=["person.alice"])
        result = adapter.adjust_comfort_range((70.0, 76.0), "cool")
        assert result == (70.0, VACATION_COOL_SETPOINT)

    def test_vacation_heat_mode(self):
        hass = _make_hass({"person.alice": "vacation"})
        adapter = OccupancyAdapter(hass, entity_ids=["person.alice"])
        result = adapter.adjust_comfort_range((68.0, 72.0), "heat")
        assert result == (VACATION_HEAT_SETPOINT, 72.0)

    def test_forced_away_adjusts(self):
        """Forced away mode should still widen the comfort range."""
        hass = _make_hass({"person.alice": "home"})
        adapter = OccupancyAdapter(hass, entity_ids=["person.alice"])
        adapter.force_mode(OccupancyMode.AWAY)
        result = adapter.adjust_comfort_range((70.0, 76.0), "cool")
        assert result == (70.0 - AWAY_COMFORT_DELTA, 76.0 + AWAY_COMFORT_DELTA)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
