"""Tests for options flow edge cases and HA-specific gotchas.

Covers bugs discovered during v0.1.1 alpha testing:
- Departure profile legacy migration (singular → plural → profiles)
- Area discovery with/without indoor entity filters
- Sensor reading edge cases (unknown, unavailable, out-of-range)
- Area config roundtrip with empty motion sensor lists
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from conftest import CC

# ── Load departure profile migration logic ────────────────────────
# Extract _load_departure_profiles as a pure function (avoids full
# coordinator import which pulls in the entire HA dependency tree).

_dp_globals: dict = {}
exec(
    compile(
        """
import json as _json

CONF_DEPARTURE_PROFILES = "departure_profiles"
CONF_DEPARTURE_ZONE = "departure_zone"
CONF_TRAVEL_TIME_SENSOR = "travel_time_sensor"
CONF_DEPARTURE_ZONES = "departure_zones"
CONF_TRAVEL_TIME_SENSORS = "travel_time_sensors"

def _load_departure_profiles(opts: dict) -> list[dict[str, str]]:
    raw = opts.get(CONF_DEPARTURE_PROFILES)
    if raw:
        try:
            profiles = _json.loads(raw)
            if isinstance(profiles, list):
                return profiles
        except (ValueError, TypeError):
            pass

    # Legacy migration: single zone/sensor → one profile
    zone = opts.get(CONF_DEPARTURE_ZONE)
    travel = opts.get(CONF_TRAVEL_TIME_SENSOR)
    if not zone and not travel:
        zones = opts.get(CONF_DEPARTURE_ZONES, [])
        travels = opts.get(CONF_TRAVEL_TIME_SENSORS, [])
        zone = zones[0] if zones else None
        travel = travels[0] if travels else None

    if zone or travel:
        profile: dict[str, str] = {}
        if zone:
            profile["zone"] = zone
        if travel:
            profile["travel_sensor"] = travel
        return [profile]

    return []
""",
        "<departure_profiles>",
        "exec",
    ),
    _dp_globals,
)

_load_departure_profiles = _dp_globals["_load_departure_profiles"]


# ═══════════════════════════════════════════════════════════════════
# Tests — Departure Profile Legacy Migration
# ═══════════════════════════════════════════════════════════════════


class TestDepartureProfileMigration:
    """Test backward-compatible loading from all legacy config formats."""

    def test_new_format_profiles_json(self):
        """New format: JSON-serialized list of profile dicts."""
        profiles = [
            {"person": "person.gerald", "zone": "zone.work", "travel_sensor": "sensor.gerald_travel"},
            {"person": "person.partner", "zone": "zone.office", "travel_sensor": "sensor.partner_travel"},
        ]
        opts = {"departure_profiles": json.dumps(profiles)}
        result = _load_departure_profiles(opts)
        assert len(result) == 2
        assert result[0]["person"] == "person.gerald"
        assert result[1]["travel_sensor"] == "sensor.partner_travel"

    def test_legacy_singular_zone_and_sensor(self):
        """Legacy v0.1.0: singular departure_zone + travel_time_sensor."""
        opts = {
            "departure_zone": "zone.work",
            "travel_time_sensor": "sensor.waze_home",
        }
        result = _load_departure_profiles(opts)
        assert len(result) == 1
        assert result[0]["zone"] == "zone.work"
        assert result[0]["travel_sensor"] == "sensor.waze_home"

    def test_legacy_plural_zones_and_sensors(self):
        """Legacy v0.1.1-early: plural departure_zones + travel_time_sensors."""
        opts = {
            "departure_zones": ["zone.work", "zone.gym"],
            "travel_time_sensors": ["sensor.waze_home", "sensor.waze_gym"],
        }
        result = _load_departure_profiles(opts)
        # Takes first of each — full multi-zone was never used in practice
        assert len(result) == 1
        assert result[0]["zone"] == "zone.work"
        assert result[0]["travel_sensor"] == "sensor.waze_home"

    def test_legacy_zone_only_no_sensor(self):
        """Legacy: zone configured but no travel sensor."""
        opts = {"departure_zone": "zone.work"}
        result = _load_departure_profiles(opts)
        assert len(result) == 1
        assert result[0]["zone"] == "zone.work"
        assert "travel_sensor" not in result[0]

    def test_legacy_sensor_only_no_zone(self):
        """Legacy: travel sensor configured but no zone."""
        opts = {"travel_time_sensor": "sensor.waze_home"}
        result = _load_departure_profiles(opts)
        assert len(result) == 1
        assert result[0]["travel_sensor"] == "sensor.waze_home"
        assert "zone" not in result[0]

    def test_no_departure_config_returns_empty(self):
        """No departure config at all → empty list."""
        assert _load_departure_profiles({}) == []

    def test_new_format_takes_priority_over_legacy(self):
        """New profiles JSON is used even if legacy keys also present."""
        profiles = [{"person": "person.gerald", "zone": "zone.new"}]
        opts = {
            "departure_profiles": json.dumps(profiles),
            "departure_zone": "zone.old",
            "travel_time_sensor": "sensor.old",
        }
        result = _load_departure_profiles(opts)
        assert len(result) == 1
        assert result[0]["zone"] == "zone.new"

    def test_malformed_json_falls_through_to_legacy(self):
        """If profiles JSON is corrupted, fall through to legacy migration."""
        opts = {
            "departure_profiles": "not valid json",
            "departure_zone": "zone.work",
        }
        result = _load_departure_profiles(opts)
        assert len(result) == 1
        assert result[0]["zone"] == "zone.work"

    def test_profiles_json_not_a_list_falls_through(self):
        """If profiles JSON is a dict (not list), fall through to legacy."""
        opts = {
            "departure_profiles": json.dumps({"zone": "zone.work"}),
            "departure_zone": "zone.fallback",
        }
        result = _load_departure_profiles(opts)
        assert len(result) == 1
        assert result[0]["zone"] == "zone.fallback"

    def test_empty_profiles_json_falls_through(self):
        """Empty JSON string falls through to legacy."""
        opts = {
            "departure_profiles": "",
            "departure_zone": "zone.work",
        }
        result = _load_departure_profiles(opts)
        assert len(result) == 1
        assert result[0]["zone"] == "zone.work"


# ═══════════════════════════════════════════════════════════════════
# Tests — Area Discovery Filtering
# ═══════════════════════════════════════════════════════════════════


# We need the real AreaOccupancyManager for these tests.
# test_area_occupancy.py already sets up the HA stubs, so import from there.
from test_area_occupancy import (
    AreaOccupancyManager,
    AreaSensorGroup,
    _make_hass,
    _make_state,
)


def _make_entity_entry(entity_id, area_id, domain, device_class=None):
    """Create a mock entity registry entry."""
    entry = MagicMock()
    entry.entity_id = entity_id
    entry.area_id = area_id
    entry.domain = domain
    entry.original_device_class = device_class
    return entry


def _make_area(area_id, name):
    """Create a mock area registry area."""
    area = MagicMock()
    area.id = area_id
    area.name = name
    return area


def _setup_discovery_mocks(hass, entities, areas):
    """Wire up entity and area registry mocks for async_discover_areas."""
    from homeassistant.helpers import area_registry, entity_registry

    # Entity registry
    ent_reg = MagicMock()
    ent_reg.entities = MagicMock()
    ent_reg.entities.values = MagicMock(return_value=entities)
    entity_registry.async_get = MagicMock(return_value=ent_reg)

    # Area registry
    area_reg = MagicMock()
    area_map = {a.id: a for a in areas}
    area_reg.async_get_area = MagicMock(side_effect=lambda aid: area_map.get(aid))
    area_registry.async_get = MagicMock(return_value=area_reg)


class TestAreaDiscoveryFiltering:
    """Test that area discovery finds the right rooms."""

    def test_unfiltered_discovers_all_areas_with_temp(self):
        """Without filter args, all areas with temp sensors are found."""
        hass = _make_hass()
        entities = [
            _make_entity_entry("sensor.lr_temp", "lr", "sensor", "temperature"),
            _make_entity_entry("sensor.br_temp", "br", "sensor", "temperature"),
            _make_entity_entry("sensor.garage_temp", "garage", "sensor", "temperature"),
        ]
        areas = [_make_area("lr", "Living Room"), _make_area("br", "Bedroom"), _make_area("garage", "Garage")]
        _setup_discovery_mocks(hass, entities, areas)

        result = asyncio.run(AreaOccupancyManager.async_discover_areas(hass))
        assert len(result) == 3
        names = {a.area_name for a in result}
        assert names == {"Living Room", "Bedroom", "Garage"}

    def test_filtered_only_returns_matching_entities(self):
        """With indoor_temp_entities filter, only matching areas are found."""
        hass = _make_hass()
        entities = [
            _make_entity_entry("sensor.lr_temp", "lr", "sensor", "temperature"),
            _make_entity_entry("sensor.br_temp", "br", "sensor", "temperature"),
            _make_entity_entry("sensor.garage_temp", "garage", "sensor", "temperature"),
        ]
        areas = [_make_area("lr", "Living Room"), _make_area("br", "Bedroom"), _make_area("garage", "Garage")]
        _setup_discovery_mocks(hass, entities, areas)

        result = asyncio.run(AreaOccupancyManager.async_discover_areas(
            hass, indoor_temp_entities=["sensor.lr_temp"]
        ))
        assert len(result) == 1
        assert result[0].area_name == "Living Room"

    def test_areas_without_temp_excluded(self):
        """Areas that only have motion sensors (no temp) are excluded."""
        hass = _make_hass()
        entities = [
            _make_entity_entry("sensor.lr_temp", "lr", "sensor", "temperature"),
            _make_entity_entry("binary_sensor.hall_motion", "hall", "binary_sensor", "motion"),
        ]
        areas = [_make_area("lr", "Living Room"), _make_area("hall", "Hallway")]
        _setup_discovery_mocks(hass, entities, areas)

        result = asyncio.run(AreaOccupancyManager.async_discover_areas(hass))
        assert len(result) == 1
        assert result[0].area_name == "Living Room"

    def test_motion_sensors_included_in_discovery(self):
        """Motion sensors in an area are included in the AreaSensorGroup."""
        hass = _make_hass()
        entities = [
            _make_entity_entry("sensor.lr_temp", "lr", "sensor", "temperature"),
            _make_entity_entry("binary_sensor.lr_motion", "lr", "binary_sensor", "motion"),
            _make_entity_entry("binary_sensor.lr_occupancy", "lr", "binary_sensor", "occupancy"),
        ]
        areas = [_make_area("lr", "Living Room")]
        _setup_discovery_mocks(hass, entities, areas)

        result = asyncio.run(AreaOccupancyManager.async_discover_areas(hass))
        assert len(result) == 1
        assert set(result[0].motion_entities) == {
            "binary_sensor.lr_motion",
            "binary_sensor.lr_occupancy",
        }

    def test_entities_without_area_ignored(self):
        """Entities not assigned to any area are skipped."""
        hass = _make_hass()
        entities = [
            _make_entity_entry("sensor.lr_temp", "lr", "sensor", "temperature"),
            _make_entity_entry("sensor.orphan_temp", None, "sensor", "temperature"),
        ]
        areas = [_make_area("lr", "Living Room")]
        _setup_discovery_mocks(hass, entities, areas)

        result = asyncio.run(AreaOccupancyManager.async_discover_areas(hass))
        assert len(result) == 1
        assert result[0].temp_entities == ["sensor.lr_temp"]

    def test_empty_registry_returns_empty(self):
        """No entities → no areas discovered."""
        hass = _make_hass()
        _setup_discovery_mocks(hass, [], [])

        result = asyncio.run(AreaOccupancyManager.async_discover_areas(hass))
        assert result == []


# ═══════════════════════════════════════════════════════════════════
# Tests — Sensor Reading Edge Cases
# ═══════════════════════════════════════════════════════════════════


class TestSensorReadingEdgeCases:
    """Test area sensor readings with HA-specific state quirks."""

    def _make_manager(self, state_map):
        hass = _make_hass(state_map)
        return AreaOccupancyManager(
            hass,
            weighting_mode="equal",
            area_config=[{
                "area_id": "lr",
                "area_name": "Living Room",
                "temp_entities": ["sensor.lr_temp"],
                "humidity_entities": ["sensor.lr_humidity"],
                "motion_entities": [],
            }],
        )

    def test_unknown_state_ignored(self):
        """HA returns 'unknown' for uninitialized sensors — should be skipped."""
        mgr = self._make_manager({
            "sensor.lr_temp": _make_state("unknown", unit="°F"),
        })
        mgr.update_readings()
        temp, _ = mgr.get_weighted_indoor_temp()
        assert temp is None

    def test_unavailable_state_ignored(self):
        """HA returns 'unavailable' when device is offline — should be skipped."""
        mgr = self._make_manager({
            "sensor.lr_temp": _make_state("unavailable", unit="°F"),
        })
        mgr.update_readings()
        temp, _ = mgr.get_weighted_indoor_temp()
        assert temp is None

    def test_extremely_high_temp_rejected(self):
        """Temps > 200°F (e.g., NAS/CPU sensors) should be rejected."""
        mgr = self._make_manager({
            "sensor.lr_temp": _make_state("250.0", unit="°F"),
        })
        mgr.update_readings()
        temp, _ = mgr.get_weighted_indoor_temp()
        assert temp is None

    def test_extremely_low_temp_rejected(self):
        """Temps < -80°F should be rejected (sensor error)."""
        mgr = self._make_manager({
            "sensor.lr_temp": _make_state("-100.0", unit="°F"),
        })
        mgr.update_readings()
        temp, _ = mgr.get_weighted_indoor_temp()
        assert temp is None

    def test_non_numeric_state_ignored(self):
        """Non-numeric states (e.g., 'on', 'off', 'charging') are skipped."""
        mgr = self._make_manager({
            "sensor.lr_temp": _make_state("on", unit="°F"),
        })
        mgr.update_readings()
        temp, _ = mgr.get_weighted_indoor_temp()
        assert temp is None

    def test_missing_entity_ignored(self):
        """Entity not in HA state machine → None from hass.states.get."""
        mgr = self._make_manager({})  # empty state map
        mgr.update_readings()
        temp, _ = mgr.get_weighted_indoor_temp()
        assert temp is None

    def test_humidity_over_100_rejected(self):
        """Humidity > 100% is physically impossible — reject."""
        mgr = self._make_manager({
            "sensor.lr_humidity": _make_state("110.0"),
        })
        mgr.update_readings()
        humidity, _ = mgr.get_weighted_indoor_humidity()
        assert humidity is None

    def test_humidity_negative_rejected(self):
        """Negative humidity is impossible — reject."""
        mgr = self._make_manager({
            "sensor.lr_humidity": _make_state("-5.0"),
        })
        mgr.update_readings()
        humidity, _ = mgr.get_weighted_indoor_humidity()
        assert humidity is None

    def test_valid_reading_among_bad_ones(self):
        """One valid sensor among invalid ones still produces a reading."""
        hass = _make_hass({
            "sensor.lr_temp": _make_state("72.0", unit="°F"),
            "sensor.bad_temp": _make_state("unknown", unit="°F"),
        })
        mgr = AreaOccupancyManager(
            hass,
            weighting_mode="equal",
            area_config=[{
                "area_id": "lr",
                "area_name": "Living Room",
                "temp_entities": ["sensor.lr_temp", "sensor.bad_temp"],
                "humidity_entities": [],
                "motion_entities": [],
            }],
        )
        mgr.update_readings()
        temp, _ = mgr.get_weighted_indoor_temp()
        # Only the valid sensor contributes
        assert temp == pytest.approx(72.0, abs=0.1)


# ═══════════════════════════════════════════════════════════════════
# Tests — Area Config Serialization Edge Cases
# ═══════════════════════════════════════════════════════════════════


class TestAreaConfigEdgeCases:
    """Test serialization/deserialization with edge-case data."""

    def test_roundtrip_empty_motion_list(self):
        """Rooms without motion sensors serialize and deserialize correctly."""
        areas = [
            AreaSensorGroup(
                area_id="lr",
                area_name="Living Room",
                temp_entities=["sensor.lr_temp"],
                humidity_entities=[],
                motion_entities=[],  # no motion sensors
            ),
        ]
        serialized = AreaOccupancyManager.serialize_area_config(areas)
        deserialized = AreaOccupancyManager.deserialize_area_config(serialized)
        assert deserialized[0]["motion_entities"] == []

    def test_roundtrip_empty_list(self):
        """Empty area list serializes to '[]' and back."""
        serialized = AreaOccupancyManager.serialize_area_config([])
        assert serialized == "[]"
        assert AreaOccupancyManager.deserialize_area_config(serialized) == []

    def test_special_characters_in_area_name(self):
        """Area names with special characters survive serialization."""
        areas = [
            AreaSensorGroup(
                area_id="kids_room",
                area_name="Kids' Room (2nd Floor)",
                temp_entities=["sensor.kids_temp"],
                humidity_entities=[],
                motion_entities=[],
            ),
        ]
        serialized = AreaOccupancyManager.serialize_area_config(areas)
        deserialized = AreaOccupancyManager.deserialize_area_config(serialized)
        assert deserialized[0]["area_name"] == "Kids' Room (2nd Floor)"
