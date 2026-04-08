"""Tests for the ProfileAdapter -- thermostat profile control."""

import asyncio
import importlib
import importlib.util
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Module loading (same pattern as other test files) ──────────────

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

# Stub homeassistant.core
ha_mod = types.ModuleType("homeassistant")
ha_mod.__path__ = ["homeassistant"]
sys.modules.setdefault("homeassistant", ha_mod)

ha_core = types.ModuleType("homeassistant.core")
ha_core.HomeAssistant = MagicMock
sys.modules.setdefault("homeassistant.core", ha_core)


def _load(full_name: str, path: str):
    spec = importlib.util.spec_from_file_location(full_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


profile_mod = _load(
    "custom_components.heatpump_optimizer.adapters.profile",
    os.path.join(CC, "adapters", "profile.py"),
)
adapters.profile = profile_mod

ProfileAdapter = profile_mod.ProfileAdapter


# ── Helpers ──────────────────────────────────────────────────────────


def _make_hass(entity_states: dict[str, str | dict]) -> MagicMock:
    """Create a mock hass with states.get and services.async_call."""
    hass = MagicMock()

    def _get_state(eid):
        if eid not in entity_states:
            return None
        val = entity_states[eid]
        state = MagicMock()
        if isinstance(val, dict):
            state.state = val.get("state", "")
            state.attributes = val.get("attributes", {})
        else:
            state.state = val
            state.attributes = {}
        return state

    hass.states.get.side_effect = _get_state
    hass.services.async_call = AsyncMock()
    return hass


# ── Tests ────────────────────────────────────────────────────────────


class TestProfileAdapterInit:
    """Constructor and configuration."""

    def test_default_profile_map(self):
        hass = _make_hass({})
        adapter = ProfileAdapter(hass)
        assert adapter._profile_map == {"home": "home", "away": "away", "sleep": "sleep"}

    def test_custom_profile_map(self):
        hass = _make_hass({})
        adapter = ProfileAdapter(
            hass,
            profile_map={"home": "Home", "away": "Away", "sleep": "Sleep"},
        )
        assert adapter._profile_map["home"] == "Home"
        assert adapter._reverse_map["home"] == "home"
        assert adapter._reverse_map["away"] == "away"

    def test_target_entity_id_select(self):
        hass = _make_hass({})
        adapter = ProfileAdapter(
            hass,
            entity_type="select",
            entity_id="select.my_ecobee_current_mode",
        )
        assert adapter.target_entity_id == "select.my_ecobee_current_mode"

    def test_target_entity_id_preset(self):
        hass = _make_hass({})
        adapter = ProfileAdapter(
            hass,
            entity_type="preset",
            climate_entity_id="climate.my_ecobee",
        )
        assert adapter.target_entity_id == "climate.my_ecobee"


class TestCurrentProfile:
    """Tests for reading current profile state."""

    def test_select_entity_current_profile(self):
        hass = _make_hass({"select.mode": "away"})
        adapter = ProfileAdapter(hass, entity_id="select.mode")
        assert adapter.current_profile == "away"

    def test_select_entity_custom_map(self):
        hass = _make_hass({"select.mode": "Away"})
        adapter = ProfileAdapter(
            hass,
            entity_id="select.mode",
            profile_map={"home": "Home", "away": "Away", "sleep": "Sleep"},
        )
        assert adapter.current_profile == "away"

    def test_preset_mode_current_profile(self):
        hass = _make_hass({
            "climate.therm": {"state": "heat", "attributes": {"preset_mode": "home"}},
        })
        adapter = ProfileAdapter(
            hass,
            entity_type="preset",
            climate_entity_id="climate.therm",
        )
        assert adapter.current_profile == "home"

    def test_unavailable_entity_returns_none(self):
        hass = _make_hass({"select.mode": "unavailable"})
        adapter = ProfileAdapter(hass, entity_id="select.mode")
        assert adapter.current_profile is None

    def test_missing_entity_returns_none(self):
        hass = _make_hass({})
        adapter = ProfileAdapter(hass, entity_id="select.missing")
        assert adapter.current_profile is None

    def test_unknown_profile_returns_none(self):
        hass = _make_hass({"select.mode": "custom_mode"})
        adapter = ProfileAdapter(hass, entity_id="select.mode")
        assert adapter.current_profile is None


class TestAvailability:
    """Tests for the available property."""

    def test_available_entity(self):
        hass = _make_hass({"select.mode": "home"})
        adapter = ProfileAdapter(hass, entity_id="select.mode")
        assert adapter.available is True

    def test_unavailable_entity(self):
        hass = _make_hass({"select.mode": "unavailable"})
        adapter = ProfileAdapter(hass, entity_id="select.mode")
        assert adapter.available is False

    def test_missing_entity(self):
        hass = _make_hass({})
        adapter = ProfileAdapter(hass, entity_id="select.missing")
        assert adapter.available is False

    def test_no_entity_configured(self):
        hass = _make_hass({})
        adapter = ProfileAdapter(hass)
        assert adapter.available is False


class TestSetProfile:
    """Tests for async_set_profile."""

    def test_select_entity_set_profile(self):
        hass = _make_hass({"select.mode": "home"})
        adapter = ProfileAdapter(hass, entity_id="select.mode")
        result = asyncio.run(adapter.async_set_profile("away"))
        assert result is True
        hass.services.async_call.assert_called_once_with(
            "select",
            "select_option",
            {"entity_id": "select.mode", "option": "away"},
            blocking=True,
        )

    def test_preset_mode_set_profile(self):
        hass = _make_hass({
            "climate.therm": {"state": "heat", "attributes": {"preset_mode": "home"}},
        })
        adapter = ProfileAdapter(
            hass,
            entity_type="preset",
            climate_entity_id="climate.therm",
        )
        result = asyncio.run(adapter.async_set_profile("sleep"))
        assert result is True
        hass.services.async_call.assert_called_once_with(
            "climate",
            "set_preset_mode",
            {"entity_id": "climate.therm", "preset_mode": "sleep"},
            blocking=True,
        )

    def test_custom_profile_map_used(self):
        hass = _make_hass({"select.mode": "Home"})
        adapter = ProfileAdapter(
            hass,
            entity_id="select.mode",
            profile_map={"home": "Home", "away": "Away", "sleep": "Sleep"},
        )
        result = asyncio.run(adapter.async_set_profile("away"))
        assert result is True
        hass.services.async_call.assert_called_once_with(
            "select",
            "select_option",
            {"entity_id": "select.mode", "option": "Away"},
            blocking=True,
        )

    def test_unavailable_entity_returns_false(self):
        hass = _make_hass({"select.mode": "unavailable"})
        adapter = ProfileAdapter(hass, entity_id="select.mode")
        result = asyncio.run(adapter.async_set_profile("away"))
        assert result is False
        hass.services.async_call.assert_not_called()

    def test_unknown_profile_name_returns_false(self):
        hass = _make_hass({"select.mode": "home"})
        adapter = ProfileAdapter(hass, entity_id="select.mode")
        result = asyncio.run(adapter.async_set_profile("unknown_mode"))
        assert result is False
        hass.services.async_call.assert_not_called()

    def test_service_call_failure_returns_false(self):
        hass = _make_hass({"select.mode": "home"})
        hass.services.async_call = AsyncMock(side_effect=Exception("Service error"))
        adapter = ProfileAdapter(hass, entity_id="select.mode")
        result = asyncio.run(adapter.async_set_profile("away"))
        assert result is False

    def test_tracks_last_set_profile(self):
        hass = _make_hass({"select.mode": "home"})
        adapter = ProfileAdapter(hass, entity_id="select.mode")
        assert adapter._last_set_profile is None
        asyncio.run(adapter.async_set_profile("away"))
        assert adapter._last_set_profile == "away"
        assert adapter._last_set_time is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
