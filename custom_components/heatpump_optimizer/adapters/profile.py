"""Thermostat profile adapter -- controls home/away/sleep comfort profiles.

Supports two mechanisms:
  - select entity: calls select.select_option on a select.* entity
    (e.g., Ecobee via HomeKit exposes select.my_ecobee_current_mode)
  - preset mode: calls climate.set_preset_mode on the climate entity
    (for thermostats using HA's built-in preset system)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class ProfileAdapter:
    """Control thermostat comfort profile (home/away/sleep)."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        entity_type: str = "select",
        entity_id: str | None = None,
        climate_entity_id: str | None = None,
        profile_map: dict[str, str] | None = None,
    ):
        """Initialize the profile adapter.

        Args:
            hass: Home Assistant instance.
            entity_type: "select" for select.* entities, "preset" for
                climate preset_mode.
            entity_id: The select.* entity ID (required for "select" type).
            climate_entity_id: The climate.* entity ID (required for
                "preset" type).
            profile_map: Maps canonical names to entity-specific values,
                e.g. {"home": "Home", "away": "Away", "sleep": "Sleep"}.
        """
        self.hass = hass
        self._entity_type = entity_type
        self._entity_id = entity_id
        self._climate_entity_id = climate_entity_id
        self._profile_map = profile_map or {
            "home": "home",
            "away": "away",
            "sleep": "sleep",
        }
        # Reverse map for reading current state back to canonical name
        self._reverse_map = {
            v.lower(): k for k, v in self._profile_map.items()
        }
        self._last_set_profile: str | None = None
        self._last_set_time: datetime | None = None

    @property
    def target_entity_id(self) -> str | None:
        """The entity ID being controlled."""
        if self._entity_type == "select":
            return self._entity_id
        return self._climate_entity_id

    @property
    def current_profile(self) -> str | None:
        """Read current profile, return canonical name (home/away/sleep)."""
        target_id = self.target_entity_id
        if not target_id:
            return None

        state = self.hass.states.get(target_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None

        if self._entity_type == "select":
            return self._reverse_map.get(state.state.lower())

        # Preset mode: read from climate entity attribute
        preset = state.attributes.get("preset_mode", "")
        if preset:
            return self._reverse_map.get(preset.lower())
        return None

    @property
    def available(self) -> bool:
        """Whether the profile entity exists and is available."""
        target_id = self.target_entity_id
        if not target_id:
            return False
        state = self.hass.states.get(target_id)
        return state is not None and state.state not in (
            "unavailable",
            "unknown",
        )

    async def async_set_profile(self, profile: str) -> bool:
        """Set the thermostat profile.

        Args:
            profile: Canonical profile name ("home", "away", or "sleep").

        Returns:
            True on success, False on failure.
        """
        mapped = self._profile_map.get(profile)
        if mapped is None:
            _LOGGER.warning(
                "Unknown profile '%s' -- no mapping configured", profile
            )
            return False

        if not self.available:
            _LOGGER.warning(
                "Profile entity %s unavailable, cannot set '%s'",
                self.target_entity_id,
                profile,
            )
            return False

        try:
            if self._entity_type == "select":
                await self.hass.services.async_call(
                    "select",
                    "select_option",
                    {
                        "entity_id": self._entity_id,
                        "option": mapped,
                    },
                    blocking=True,
                )
            else:
                await self.hass.services.async_call(
                    "climate",
                    "set_preset_mode",
                    {
                        "entity_id": self._climate_entity_id,
                        "preset_mode": mapped,
                    },
                    blocking=True,
                )

            self._last_set_profile = profile
            self._last_set_time = datetime.now(timezone.utc)
            _LOGGER.info(
                "Set thermostat profile to '%s' (mapped: '%s') via %s",
                profile,
                mapped,
                self.target_entity_id,
            )
            return True

        except Exception:
            _LOGGER.exception(
                "Failed to set profile '%s' on %s",
                profile,
                self.target_entity_id,
            )
            return False
