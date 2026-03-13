"""Auto-discovery of relevant HA entities for onboarding and options flow.

Queries the HA entity registry to find climate, weather, sensor, person,
calendar, and other entities that are likely relevant for heat pump optimization.
Results are ranked by confidence and presented as suggestions in the config flow.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from ..engine.data_types import EntitySuggestion

_LOGGER = logging.getLogger(__name__)

# Keywords for classifying indoor vs outdoor sensors
_OUTDOOR_KEYWORDS = {"outdoor", "outside", "exterior", "porch", "patio", "backyard", "garage", "deck"}
_INDOOR_KEYWORDS = {"indoor", "inside", "living", "bedroom", "kitchen", "bathroom", "office", "den", "nursery"}

# Keywords for HVAC-related power sensors
_HVAC_KEYWORDS = {"hvac", "heat pump", "heat_pump", "heatpump", "air conditioner", "ac", "furnace", "compressor"}

# Keywords for solar power sensors
_SOLAR_KEYWORDS = {"solar", "pv", "panel", "inverter", "enphase", "solaredge"}

# Area names that suggest outdoor
_OUTDOOR_AREA_KEYWORDS = {"outdoor", "outside", "porch", "patio", "backyard", "front yard", "garden", "deck", "garage"}


class EntityDiscovery:
    """Discovers and ranks HA entities relevant to heat pump optimization."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _get_registries(self):
        """Get area and entity registries."""
        from homeassistant.helpers import area_registry, entity_registry

        return (
            area_registry.async_get(self._hass),
            entity_registry.async_get(self._hass),
        )

    def _get_outdoor_area_ids(self, area_reg, entity_reg) -> set[str]:
        """Identify area IDs that are likely outdoor."""
        outdoor_ids: set[str] = set()
        for area in area_reg.async_list_areas():
            name_lower = area.name.lower()
            if any(kw in name_lower for kw in _OUTDOOR_AREA_KEYWORDS):
                outdoor_ids.add(area.id)
        return outdoor_ids

    def _entity_name(self, entry) -> str:
        """Get a human-readable name for an entity registry entry."""
        return entry.name or entry.original_name or entry.entity_id

    def _is_outdoor_by_name(self, name: str) -> bool:
        name_lower = name.lower()
        return any(kw in name_lower for kw in _OUTDOOR_KEYWORDS)

    def _is_indoor_by_name(self, name: str) -> bool:
        name_lower = name.lower()
        return any(kw in name_lower for kw in _INDOOR_KEYWORDS)

    def discover_climate_entities(self) -> list[EntitySuggestion]:
        """Find climate entities (thermostats)."""
        _, entity_reg = self._get_registries()
        results: list[EntitySuggestion] = []

        for entry in entity_reg.entities.values():
            if entry.domain != "climate" or entry.disabled:
                continue
            name = self._entity_name(entry)
            # Rank by platform — ecobee, nest, honeywell are high confidence
            platform = (entry.platform or "").lower()
            if platform in ("ecobee", "nest", "honeywell", "tado", "daikin"):
                confidence = "high"
                reason = f"Known HVAC platform ({platform})"
            else:
                confidence = "medium"
                reason = "Climate entity"
            results.append(EntitySuggestion(
                entity_id=entry.entity_id,
                friendly_name=name,
                confidence=confidence,
                reason=reason,
            ))

        # Sort high confidence first
        results.sort(key=lambda s: {"high": 0, "medium": 1, "low": 2}[s.confidence])
        return results

    def discover_weather_entities(self) -> list[EntitySuggestion]:
        """Find weather entities for forecasts."""
        _, entity_reg = self._get_registries()
        results: list[EntitySuggestion] = []

        for entry in entity_reg.entities.values():
            if entry.domain != "weather" or entry.disabled:
                continue
            name = self._entity_name(entry)
            platform = (entry.platform or "").lower()
            # Local weather stations are high confidence
            if platform in ("met", "openweathermap", "nws", "accuweather", "pirateweather"):
                confidence = "high"
                reason = f"Weather forecast provider ({platform})"
            else:
                confidence = "medium"
                reason = "Weather entity"
            results.append(EntitySuggestion(
                entity_id=entry.entity_id,
                friendly_name=name,
                confidence=confidence,
                reason=reason,
            ))

        results.sort(key=lambda s: {"high": 0, "medium": 1, "low": 2}[s.confidence])
        return results

    def discover_temp_sensors(self, outdoor: bool = False) -> list[EntitySuggestion]:
        """Find temperature sensors, classified as indoor or outdoor."""
        area_reg, entity_reg = self._get_registries()
        outdoor_area_ids = self._get_outdoor_area_ids(area_reg, entity_reg)
        results: list[EntitySuggestion] = []

        for entry in entity_reg.entities.values():
            if entry.domain != "sensor" or entry.disabled:
                continue
            if entry.original_device_class != "temperature":
                continue

            name = self._entity_name(entry)
            is_outdoor_name = self._is_outdoor_by_name(name)
            is_indoor_name = self._is_indoor_by_name(name)
            is_outdoor_area = entry.area_id in outdoor_area_ids if entry.area_id else False

            # Classify
            if outdoor:
                if is_outdoor_name or is_outdoor_area:
                    confidence = "high"
                    reason = (
                        "Name indicates outdoor"
                        if is_outdoor_name
                        else "Assigned to outdoor area"
                    )
                elif not is_indoor_name:
                    confidence = "low"
                    reason = "Unclassified temperature sensor"
                else:
                    continue  # Skip clearly indoor sensors when looking for outdoor
            else:
                if is_indoor_name:
                    confidence = "high"
                    reason = "Name indicates indoor"
                elif not is_outdoor_name and not is_outdoor_area:
                    # Not clearly outdoor — could be indoor
                    confidence = "medium"
                    reason = "Temperature sensor (location unknown)"
                else:
                    continue  # Skip clearly outdoor sensors when looking for indoor

            results.append(EntitySuggestion(
                entity_id=entry.entity_id,
                friendly_name=name,
                confidence=confidence,
                reason=reason,
            ))

        results.sort(key=lambda s: {"high": 0, "medium": 1, "low": 2}[s.confidence])
        return results

    def discover_humidity_sensors(self, outdoor: bool = False) -> list[EntitySuggestion]:
        """Find humidity sensors, classified as indoor or outdoor."""
        area_reg, entity_reg = self._get_registries()
        outdoor_area_ids = self._get_outdoor_area_ids(area_reg, entity_reg)
        results: list[EntitySuggestion] = []

        for entry in entity_reg.entities.values():
            if entry.domain != "sensor" or entry.disabled:
                continue
            if entry.original_device_class != "humidity":
                continue

            name = self._entity_name(entry)
            is_outdoor_name = self._is_outdoor_by_name(name)
            is_indoor_name = self._is_indoor_by_name(name)
            is_outdoor_area = entry.area_id in outdoor_area_ids if entry.area_id else False

            if outdoor:
                if is_outdoor_name or is_outdoor_area:
                    confidence = "high"
                    reason = (
                        "Name indicates outdoor"
                        if is_outdoor_name
                        else "Assigned to outdoor area"
                    )
                elif not is_indoor_name:
                    confidence = "low"
                    reason = "Unclassified humidity sensor"
                else:
                    continue
            else:
                if is_indoor_name:
                    confidence = "high"
                    reason = "Name indicates indoor"
                elif not is_outdoor_name and not is_outdoor_area:
                    confidence = "medium"
                    reason = "Humidity sensor (location unknown)"
                else:
                    continue

            results.append(EntitySuggestion(
                entity_id=entry.entity_id,
                friendly_name=name,
                confidence=confidence,
                reason=reason,
            ))

        results.sort(key=lambda s: {"high": 0, "medium": 1, "low": 2}[s.confidence])
        return results

    def discover_person_entities(self) -> list[EntitySuggestion]:
        """Find person entities and presence-related sensors."""
        _, entity_reg = self._get_registries()
        results: list[EntitySuggestion] = []

        for entry in entity_reg.entities.values():
            if entry.disabled:
                continue
            name = self._entity_name(entry)

            if entry.domain == "person":
                results.append(EntitySuggestion(
                    entity_id=entry.entity_id,
                    friendly_name=name,
                    confidence="high",
                    reason="Person entity",
                ))
            elif entry.domain == "device_tracker":
                results.append(EntitySuggestion(
                    entity_id=entry.entity_id,
                    friendly_name=name,
                    confidence="medium",
                    reason="Device tracker",
                ))
            elif (
                entry.domain == "binary_sensor"
                and entry.original_device_class in ("occupancy", "presence")
            ):
                results.append(EntitySuggestion(
                    entity_id=entry.entity_id,
                    friendly_name=name,
                    confidence="medium",
                    reason=f"Presence sensor ({entry.original_device_class})",
                ))

        results.sort(key=lambda s: {"high": 0, "medium": 1, "low": 2}[s.confidence])
        return results

    def discover_power_sensors(self) -> list[EntitySuggestion]:
        """Find HVAC-related power sensors."""
        _, entity_reg = self._get_registries()
        results: list[EntitySuggestion] = []

        for entry in entity_reg.entities.values():
            if entry.domain != "sensor" or entry.disabled:
                continue
            if entry.original_device_class != "power":
                continue

            name = self._entity_name(entry)
            name_lower = name.lower()
            eid_lower = entry.entity_id.lower()

            if any(kw in name_lower or kw in eid_lower for kw in _HVAC_KEYWORDS):
                confidence = "high"
                reason = "Name matches HVAC power sensor"
            elif any(kw in name_lower or kw in eid_lower for kw in _SOLAR_KEYWORDS):
                continue  # Skip solar sensors here
            else:
                confidence = "low"
                reason = "Power sensor (unclassified)"

            results.append(EntitySuggestion(
                entity_id=entry.entity_id,
                friendly_name=name,
                confidence=confidence,
                reason=reason,
            ))

        results.sort(key=lambda s: {"high": 0, "medium": 1, "low": 2}[s.confidence])
        return results

    def discover_solar_sensors(self) -> list[EntitySuggestion]:
        """Find solar production power sensors."""
        _, entity_reg = self._get_registries()
        results: list[EntitySuggestion] = []

        for entry in entity_reg.entities.values():
            if entry.domain != "sensor" or entry.disabled:
                continue
            if entry.original_device_class != "power":
                continue

            name = self._entity_name(entry)
            name_lower = name.lower()
            eid_lower = entry.entity_id.lower()

            if any(kw in name_lower or kw in eid_lower for kw in _SOLAR_KEYWORDS):
                confidence = "high"
                reason = "Name matches solar production"
            else:
                continue  # Only suggest clearly solar sensors

            results.append(EntitySuggestion(
                entity_id=entry.entity_id,
                friendly_name=name,
                confidence=confidence,
                reason=reason,
            ))

        results.sort(key=lambda s: {"high": 0, "medium": 1, "low": 2}[s.confidence])
        return results

    def discover_calendar_entities(self) -> list[EntitySuggestion]:
        """Find calendar entities for schedule-based occupancy."""
        _, entity_reg = self._get_registries()
        results: list[EntitySuggestion] = []

        for entry in entity_reg.entities.values():
            if entry.domain != "calendar" or entry.disabled:
                continue
            name = self._entity_name(entry)
            results.append(EntitySuggestion(
                entity_id=entry.entity_id,
                friendly_name=name,
                confidence="medium",
                reason="Calendar entity",
            ))

        return results

    def discover_wind_speed_sensors(self) -> list[EntitySuggestion]:
        """Find wind speed sensors."""
        _, entity_reg = self._get_registries()
        results: list[EntitySuggestion] = []

        for entry in entity_reg.entities.values():
            if entry.domain != "sensor" or entry.disabled:
                continue
            if entry.original_device_class != "wind_speed":
                continue
            name = self._entity_name(entry)
            results.append(EntitySuggestion(
                entity_id=entry.entity_id,
                friendly_name=name,
                confidence="high",
                reason="Wind speed sensor",
            ))

        return results

    def discover_irradiance_sensors(self) -> list[EntitySuggestion]:
        """Find solar irradiance sensors (W/m²)."""
        _, entity_reg = self._get_registries()
        results: list[EntitySuggestion] = []

        for entry in entity_reg.entities.values():
            if entry.domain != "sensor" or entry.disabled:
                continue
            if entry.original_device_class != "irradiance":
                continue
            name = self._entity_name(entry)
            results.append(EntitySuggestion(
                entity_id=entry.entity_id,
                friendly_name=name,
                confidence="high",
                reason="Solar irradiance sensor",
            ))

        return results

    def discover_co2_sensors(self) -> list[EntitySuggestion]:
        """Find CO2/carbon intensity sensors."""
        _, entity_reg = self._get_registries()
        results: list[EntitySuggestion] = []

        for entry in entity_reg.entities.values():
            if entry.domain != "sensor" or entry.disabled:
                continue
            name = self._entity_name(entry)
            name_lower = name.lower()
            eid_lower = entry.entity_id.lower()
            if any(kw in name_lower or kw in eid_lower for kw in (
                "co2", "carbon", "grid_intensity", "carbon_intensity"
            )):
                results.append(EntitySuggestion(
                    entity_id=entry.entity_id,
                    friendly_name=name,
                    confidence="medium",
                    reason="Carbon intensity sensor",
                ))

        return results

    def discover_electricity_rate_sensors(self) -> list[EntitySuggestion]:
        """Find electricity rate sensors."""
        _, entity_reg = self._get_registries()
        results: list[EntitySuggestion] = []

        for entry in entity_reg.entities.values():
            if entry.domain != "sensor" or entry.disabled:
                continue
            if entry.original_device_class != "monetary":
                continue
            name = self._entity_name(entry)
            name_lower = name.lower()
            eid_lower = entry.entity_id.lower()
            if any(kw in name_lower or kw in eid_lower for kw in (
                "electricity", "rate", "tariff", "price", "kwh"
            )):
                results.append(EntitySuggestion(
                    entity_id=entry.entity_id,
                    friendly_name=name,
                    confidence="high",
                    reason="Electricity rate sensor",
                ))

        return results
