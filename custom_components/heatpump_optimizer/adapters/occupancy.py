"""Occupancy adapter — determines home/away/vacation from HA entities.

Supports multiple occupancy sources:
  - Person entities (person.*)
  - Binary sensors (e.g., Ecobee occupancy)
  - Input select for manual mode
  - Calendar-based occupancy timeline (schedule-aware optimization)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum

from homeassistant.core import HomeAssistant

from ..engine.data_types import OccupancyForecastPoint

_LOGGER = logging.getLogger(__name__)

# Comfort range widening when away
AWAY_COMFORT_DELTA = 4.0  # °F wider in each direction

# Vacation setpoints (energy saving)
VACATION_COOL_SETPOINT = 82.0
VACATION_HEAT_SETPOINT = 55.0


class OccupancyMode(str, Enum):
    """Occupancy states."""

    HOME = "home"
    AWAY = "away"
    VACATION = "vacation"


class OccupancyAdapter:
    """Determine occupancy state from various HA entity types."""

    def __init__(
        self,
        hass: HomeAssistant,
        entity_ids: list[str] | None = None,
        debounce_minutes: int = 5,
        *,
        entity_id: str | None = None,
        home_zone_states: list[str] | None = None,
    ):
        self.hass = hass
        # Backward compat: singular → list
        if entity_ids is None and entity_id is not None:
            entity_ids = [entity_id]
        self.entity_ids = entity_ids or []
        self.debounce_minutes = debounce_minutes
        self._forced_mode: OccupancyMode | None = None
        self._last_active: dict[str, datetime] = {}
        # Extra person entity states that count as "home" (e.g., zone names)
        self._home_zone_states = [
            s.lower().strip() for s in (home_zone_states or [])
        ]

    def get_people_home_count(self) -> int:
        """Count the number of tracked entities currently in HOME state.

        For person entities this is a direct headcount. For binary sensors
        (e.g., Ecobee occupancy) each "on" sensor counts as one.
        Returns 0 if no entities are configured.
        """
        if not self.entity_ids:
            return 0

        count = 0
        for eid in self.entity_ids:
            state = self.hass.states.get(eid)
            if state is None:
                continue
            if self._interpret_state(state.state) == OccupancyMode.HOME:
                count += 1
        return count

    def get_mode(self) -> OccupancyMode:
        """Determine current occupancy mode (reactive only, no calendar).

        Priority:
        1. Forced mode (set via service call)
        2. Entity states (any home/on → HOME, with debounce)
        3. If any entity reports vacation/extended_away → VACATION
        4. Otherwise → AWAY
        5. Default (no entities) → HOME
        """
        if self._forced_mode is not None:
            return self._forced_mode
        return self._get_reactive_mode()

    def force_mode(self, mode: OccupancyMode | None) -> None:
        """Force a specific occupancy mode (e.g., via service call).

        Pass None to clear the forced mode and return to entity-based detection.
        """
        self._forced_mode = mode
        if mode:
            _LOGGER.info("Occupancy forced to: %s", mode.value)
        else:
            _LOGGER.info("Occupancy force-mode cleared")

    def adjust_comfort_range(
        self,
        comfort: tuple[float, float],
        mode: str,
    ) -> tuple[float, float]:
        """Adjust comfort range based on current occupancy.

        Args:
            comfort: Base (min, max) comfort range in °F.
            mode: HVAC mode ("cool" or "heat").

        Returns:
            Adjusted (min, max) comfort range.
        """
        return self.adjust_comfort_for_mode(comfort, mode, self.get_mode())

    @staticmethod
    def adjust_comfort_for_mode(
        comfort: tuple[float, float],
        hvac_mode: str,
        occupancy_mode: OccupancyMode,
    ) -> tuple[float, float]:
        """Adjust comfort range for a specific occupancy mode.

        This is the parameterized version that accepts the occupancy mode
        directly, enabling per-hour comfort computation from a timeline.

        Args:
            comfort: Base (min, max) comfort range in °F.
            hvac_mode: HVAC mode ("cool" or "heat").
            occupancy_mode: The occupancy state to apply.

        Returns:
            Adjusted (min, max) comfort range.
        """
        if occupancy_mode == OccupancyMode.HOME:
            return comfort

        if occupancy_mode == OccupancyMode.VACATION:
            if hvac_mode == "cool":
                return (comfort[0], VACATION_COOL_SETPOINT)
            return (VACATION_HEAT_SETPOINT, comfort[1])

        # Away: widen by AWAY_COMFORT_DELTA in each direction
        return (
            comfort[0] - AWAY_COMFORT_DELTA,
            comfort[1] + AWAY_COMFORT_DELTA,
        )

    def get_effective_mode(
        self,
        calendar_timeline: list[OccupancyForecastPoint] | None = None,
    ) -> OccupancyMode:
        """Determine occupancy with calendar-aware fallback.

        Priority:
        1. Forced mode (highest — from service call)
        2. Reactive entities say HOME (person came home early, overrides calendar)
        3. Calendar timeline for current time
        4. Reactive entity-based detection (existing logic)

        This enables graceful override: if the calendar says AWAY but the
        reactive sensor detects someone home, the person wins.
        """
        if self._forced_mode is not None:
            return self._forced_mode

        # Reactive check: if entities detect someone is home, that trumps calendar
        reactive_mode = self._get_reactive_mode()
        if reactive_mode == OccupancyMode.HOME:
            return OccupancyMode.HOME

        # Calendar check: look up current time in the timeline
        if calendar_timeline:
            now = datetime.now(timezone.utc)
            for point in calendar_timeline:
                if point.start_time <= now < point.end_time:
                    mode_str = point.mode.lower()
                    if mode_str == "away":
                        return OccupancyMode.AWAY
                    if mode_str == "vacation":
                        return OccupancyMode.VACATION
                    if mode_str == "home":
                        return OccupancyMode.HOME
                    break

        # Fall back to reactive detection
        return reactive_mode

    def _get_reactive_mode(self) -> OccupancyMode:
        """Entity-based occupancy detection (existing logic, extracted)."""
        if not self.entity_ids:
            return OccupancyMode.HOME

        now = datetime.now(timezone.utc)
        any_home = False
        any_vacation = False

        for eid in self.entity_ids:
            state = self.hass.states.get(eid)
            if state is None:
                continue

            interpreted = self._interpret_state(state.state)

            if interpreted == OccupancyMode.HOME:
                self._last_active[eid] = now
                any_home = True
            elif interpreted == OccupancyMode.VACATION:
                any_vacation = True

        # Check debounce: recently-active entities count as home
        if not any_home:
            for eid, last in self._last_active.items():
                elapsed = (now - last).total_seconds() / 60.0
                if elapsed < self.debounce_minutes:
                    any_home = True
                    break

        if any_home:
            return OccupancyMode.HOME
        if any_vacation:
            return OccupancyMode.VACATION
        return OccupancyMode.AWAY

    def _interpret_state(self, state_value: str) -> OccupancyMode:
        """Map an entity state string to an occupancy mode.

        Handles:
        - person.* states: "home", "not_home", "away"
        - binary_sensor states: "on" (occupied), "off" (away)
        - input_select states: "home", "away", "vacation"
        - Configured zone states (e.g., "Lake Monticello") -> HOME
        - "unavailable"/"unknown": treated as AWAY (not home) to avoid
          phantom occupancy from entity hiccups inflating internal heat gain.
        """
        normalized = state_value.lower().strip()

        if normalized in ("home", "on"):
            return OccupancyMode.HOME

        # Check configured zone states (e.g., neighborhood zones)
        if self._home_zone_states and normalized in self._home_zone_states:
            return OccupancyMode.HOME

        if normalized in ("vacation", "extended_away"):
            return OccupancyMode.VACATION
        if normalized in ("not_home", "away", "off", "unavailable", "unknown"):
            return OccupancyMode.AWAY

        _LOGGER.debug("Unknown occupancy state '%s' -- defaulting to AWAY", state_value)
        return OccupancyMode.AWAY
