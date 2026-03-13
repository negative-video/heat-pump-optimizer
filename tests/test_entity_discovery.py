"""Tests for entity discovery utility.

Tests that EntityDiscovery correctly identifies and ranks HA entities
for auto-populating config flow fields.
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from custom_components.heatpump_optimizer.adapters.entity_discovery import (
    EntityDiscovery,
)


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════


def _make_entity(
    entity_id: str,
    domain: str | None = None,
    device_class: str | None = None,
    platform: str = "generic",
    area_id: str | None = None,
    name: str | None = None,
    disabled: bool = False,
):
    """Create a mock entity registry entry."""
    if domain is None:
        domain = entity_id.split(".")[0]
    return SimpleNamespace(
        entity_id=entity_id,
        domain=domain,
        original_device_class=device_class,
        platform=platform,
        area_id=area_id,
        name=name,
        original_name=name or entity_id.split(".")[-1].replace("_", " ").title(),
        device_id=None,
        disabled=disabled,
    )


def _make_area(area_id: str, name: str):
    return SimpleNamespace(id=area_id, name=name)


def _make_discovery(entities, areas=None):
    """Create an EntityDiscovery with mocked HA registries."""
    hass = MagicMock()

    area_reg = MagicMock()
    area_list = areas or []
    area_reg.async_list_areas.return_value = area_list
    area_reg.async_get_area.side_effect = lambda aid: next(
        (a for a in area_list if a.id == aid), None
    )

    entity_reg = MagicMock()
    entity_reg.entities.values.return_value = entities

    with patch(
        "custom_components.heatpump_optimizer.adapters.entity_discovery.EntityDiscovery._get_registries",
        return_value=(area_reg, entity_reg),
    ):
        discovery = EntityDiscovery(hass)
        # Patch again for method calls
        discovery._get_registries = lambda: (area_reg, entity_reg)
    return discovery


# ═══════════════════════════════════════════════════════════════════
# Climate Discovery Tests
# ═══════════════════════════════════════════════════════════════════


class TestDiscoverClimate:
    def test_finds_ecobee(self):
        entities = [
            _make_entity("climate.ecobee", platform="ecobee", name="Main Floor"),
        ]
        d = _make_discovery(entities)
        results = d.discover_climate_entities()
        assert len(results) == 1
        assert results[0].entity_id == "climate.ecobee"
        assert results[0].confidence == "high"

    def test_finds_generic_climate(self):
        entities = [
            _make_entity("climate.generic_thermostat", platform="generic_thermostat"),
        ]
        d = _make_discovery(entities)
        results = d.discover_climate_entities()
        assert len(results) == 1
        assert results[0].confidence == "medium"

    def test_excludes_disabled(self):
        entities = [
            _make_entity("climate.disabled_one", disabled=True),
        ]
        d = _make_discovery(entities)
        results = d.discover_climate_entities()
        assert len(results) == 0

    def test_high_confidence_first(self):
        entities = [
            _make_entity("climate.generic", platform="generic_thermostat"),
            _make_entity("climate.nest", platform="nest"),
        ]
        d = _make_discovery(entities)
        results = d.discover_climate_entities()
        assert results[0].entity_id == "climate.nest"
        assert results[0].confidence == "high"


# ═══════════════════════════════════════════════════════════════════
# Weather Discovery Tests
# ═══════════════════════════════════════════════════════════════════


class TestDiscoverWeather:
    def test_finds_weather_entities(self):
        entities = [
            _make_entity("weather.home", platform="met"),
            _make_entity("weather.owm", platform="openweathermap"),
        ]
        d = _make_discovery(entities)
        results = d.discover_weather_entities()
        assert len(results) == 2
        assert all(r.confidence == "high" for r in results)

    def test_unknown_platform_medium_confidence(self):
        entities = [
            _make_entity("weather.custom", platform="custom_weather"),
        ]
        d = _make_discovery(entities)
        results = d.discover_weather_entities()
        assert len(results) == 1
        assert results[0].confidence == "medium"


# ═══════════════════════════════════════════════════════════════════
# Temperature Sensor Discovery Tests
# ═══════════════════════════════════════════════════════════════════


class TestDiscoverTempSensors:
    def test_outdoor_by_name(self):
        entities = [
            _make_entity(
                "sensor.outdoor_temp", device_class="temperature",
                name="Outdoor Temperature"
            ),
        ]
        d = _make_discovery(entities)
        results = d.discover_temp_sensors(outdoor=True)
        assert len(results) == 1
        assert results[0].confidence == "high"

    def test_outdoor_by_area(self):
        """A sensor in an outdoor area should be classified as outdoor."""
        entities = [
            _make_entity(
                "sensor.zone_temp", device_class="temperature",
                area_id="backyard", name="Zone Temp"
            ),
        ]
        areas = [_make_area("backyard", "Backyard")]
        d = _make_discovery(entities, areas)
        results = d.discover_temp_sensors(outdoor=True)
        assert len(results) == 1
        assert results[0].confidence == "high"
        assert "outdoor area" in results[0].reason.lower()

    def test_indoor_by_name(self):
        entities = [
            _make_entity(
                "sensor.living_room_temp", device_class="temperature",
                name="Living Room Temperature"
            ),
        ]
        d = _make_discovery(entities)
        results = d.discover_temp_sensors(outdoor=False)
        assert len(results) == 1
        assert results[0].confidence == "high"

    def test_indoor_skips_outdoor_sensors(self):
        entities = [
            _make_entity(
                "sensor.outside_temp", device_class="temperature",
                name="Outside Temperature"
            ),
        ]
        d = _make_discovery(entities)
        results = d.discover_temp_sensors(outdoor=False)
        assert len(results) == 0

    def test_unclassified_sensor_included_for_indoor_as_medium(self):
        entities = [
            _make_entity(
                "sensor.temp_1", device_class="temperature",
                name="Temperature Sensor 1"
            ),
        ]
        d = _make_discovery(entities)
        results = d.discover_temp_sensors(outdoor=False)
        assert len(results) == 1
        assert results[0].confidence == "medium"


# ═══════════════════════════════════════════════════════════════════
# Person/Presence Discovery Tests
# ═══════════════════════════════════════════════════════════════════


class TestDiscoverPersonEntities:
    def test_finds_person_entities(self):
        entities = [
            _make_entity("person.john", name="John"),
            _make_entity("person.jane", name="Jane"),
        ]
        d = _make_discovery(entities)
        results = d.discover_person_entities()
        assert len(results) == 2
        assert all(r.confidence == "high" for r in results)

    def test_finds_device_trackers(self):
        entities = [
            _make_entity("device_tracker.phone", name="John's Phone"),
        ]
        d = _make_discovery(entities)
        results = d.discover_person_entities()
        assert len(results) == 1
        assert results[0].confidence == "medium"

    def test_finds_occupancy_binary_sensors(self):
        entities = [
            _make_entity(
                "binary_sensor.occupancy", device_class="occupancy",
                name="Home Occupancy"
            ),
        ]
        d = _make_discovery(entities)
        results = d.discover_person_entities()
        assert len(results) == 1
        assert results[0].confidence == "medium"

    def test_person_entities_ranked_first(self):
        entities = [
            _make_entity("device_tracker.phone", name="Phone"),
            _make_entity("person.john", name="John"),
        ]
        d = _make_discovery(entities)
        results = d.discover_person_entities()
        assert results[0].entity_id == "person.john"


# ═══════════════════════════════════════════════════════════════════
# Power Sensor Discovery Tests
# ═══════════════════════════════════════════════════════════════════


class TestDiscoverPowerSensors:
    def test_finds_hvac_power(self):
        entities = [
            _make_entity(
                "sensor.hvac_power", device_class="power",
                name="HVAC Power"
            ),
        ]
        d = _make_discovery(entities)
        results = d.discover_power_sensors()
        assert len(results) == 1
        assert results[0].confidence == "high"

    def test_skips_solar_sensors(self):
        entities = [
            _make_entity(
                "sensor.solar_production", device_class="power",
                name="Solar Production"
            ),
        ]
        d = _make_discovery(entities)
        results = d.discover_power_sensors()
        assert len(results) == 0

    def test_finds_heat_pump_by_entity_id(self):
        entities = [
            _make_entity(
                "sensor.heat_pump_power", device_class="power",
                name="Some Power Sensor"
            ),
        ]
        d = _make_discovery(entities)
        results = d.discover_power_sensors()
        assert len(results) == 1
        assert results[0].confidence == "high"


# ═══════════════════════════════════════════════════════════════════
# Solar Sensor Discovery Tests
# ═══════════════════════════════════════════════════════════════════


class TestDiscoverSolarSensors:
    def test_finds_solar_sensors(self):
        entities = [
            _make_entity(
                "sensor.solar_power", device_class="power",
                name="Solar Panel Production"
            ),
        ]
        d = _make_discovery(entities)
        results = d.discover_solar_sensors()
        assert len(results) == 1
        assert results[0].confidence == "high"

    def test_skips_non_solar(self):
        entities = [
            _make_entity(
                "sensor.total_power", device_class="power",
                name="Total House Power"
            ),
        ]
        d = _make_discovery(entities)
        results = d.discover_solar_sensors()
        assert len(results) == 0


# ═══════════════════════════════════════════════════════════════════
# Calendar Discovery Tests
# ═══════════════════════════════════════════════════════════════════


class TestDiscoverCalendarEntities:
    def test_finds_calendars(self):
        entities = [
            _make_entity("calendar.work", name="Work Schedule"),
            _make_entity("calendar.personal", name="Personal"),
        ]
        d = _make_discovery(entities)
        results = d.discover_calendar_entities()
        assert len(results) == 2
        assert all(r.confidence == "medium" for r in results)

    def test_excludes_disabled(self):
        entities = [
            _make_entity("calendar.old", name="Old Calendar", disabled=True),
        ]
        d = _make_discovery(entities)
        results = d.discover_calendar_entities()
        assert len(results) == 0


# ═══════════════════════════════════════════════════════════════════
# Humidity Sensor Discovery Tests
# ═══════════════════════════════════════════════════════════════════


class TestDiscoverHumiditySensors:
    def test_outdoor_humidity_by_name(self):
        entities = [
            _make_entity(
                "sensor.outdoor_humidity", device_class="humidity",
                name="Outdoor Humidity"
            ),
        ]
        d = _make_discovery(entities)
        results = d.discover_humidity_sensors(outdoor=True)
        assert len(results) == 1
        assert results[0].confidence == "high"

    def test_indoor_humidity_by_name(self):
        entities = [
            _make_entity(
                "sensor.bedroom_humidity", device_class="humidity",
                name="Bedroom Humidity"
            ),
        ]
        d = _make_discovery(entities)
        results = d.discover_humidity_sensors(outdoor=False)
        assert len(results) == 1
        assert results[0].confidence == "high"
