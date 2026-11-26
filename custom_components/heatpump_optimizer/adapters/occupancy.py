"""Occupancy adapter — determines home/away/vacation from HA entities.

Supports multiple occupancy sources:
  - Person entities (person.*)
  - Binary sensors (e.g., Ecobee occupancy)
  - Input select for manual mode
  - Calendar-based occupancy timeline (schedule-aware optimization)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
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
        # How far the comfort band widens when nobody is home. Configurable;
        # the module constant is only the default.
        self.away_comfort_delta: float = AWAY_COMFORT_DELTA
        self._forced_mode: OccupancyMode | None = None
        self._last_active: dict[str, datetime] = {}
        # Extra person entity states that count as "home" (e.g., zone names)
        self._home_zone_states = [s.lower().strip() for s in (home_zone_states or [])]

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
        return self.adjust_comfort_for_mode(comfort, mode, self.get_mode(), away_delta=self.away_comfort_delta)

    @staticmethod
    def adjust_comfort_for_mode(
        comfort: tuple[float, float],
        hvac_mode: str,
        occupancy_mode: OccupancyMode,
        away_delta: float = AWAY_COMFORT_DELTA,
        safety_limits: tuple[float, float] | None = None,
    ) -> tuple[float, float]:
        """Adjust comfort range for a specific occupancy mode.

        This is the parameterized version that accepts the occupancy mode
        directly, enabling per-hour comfort computation from a timeline.

        Args:
            comfort: Base (min, max) comfort range in °F.
            hvac_mode: HVAC mode ("cool" or "heat").
            occupancy_mode: The occupancy state to apply.
            away_delta: °F the band widens in each direction when away.
            safety_limits: Absolute (heat floor, cool ceiling). An away or
                vacation band is never allowed past these: an empty house
                still has pipes and plants in it.

        Returns:
            Adjusted (min, max) comfort range.
        """
        if occupancy_mode == OccupancyMode.HOME:
            adjusted = (comfort[0], comfort[1])
        elif occupancy_mode == OccupancyMode.VACATION:
            if hvac_mode == "cool":
                adjusted = (comfort[0], VACATION_COOL_SETPOINT)
            else:
                adjusted = (VACATION_HEAT_SETPOINT, comfort[1])
        else:
            # Away: widen in each direction
            adjusted = (comfort[0] - away_delta, comfort[1] + away_delta)

        if safety_limits is None:
            return adjusted
        lo = max(adjusted[0], safety_limits[0])
        hi = min(adjusted[1], safety_limits[1])
        if hi < lo:
            hi = lo
        return (lo, hi)

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

        The reverse does not hold. The calendar is a forecast, and its "home"
        is usually just the default for "no event right now". When presence
        entities are configured and none of them is home, the house is empty
        whatever the calendar says; the calendar may only refine that to
        vacation. Letting a default "home" win here reported a house as
        occupied through 131 hours in which nobody was in it.
        """
        if self._forced_mode is not None:
            return self._forced_mode

        # Reactive check: if entities detect someone is home, that trumps
        # calendar. With no presence entities the reactive answer is only a
        # default ("home"), not a detection, and must not outrank a calendar
        # event that says otherwise.
        reactive_mode = self._get_reactive_mode()
        if self.entity_ids and reactive_mode == OccupancyMode.HOME:
            return OccupancyMode.HOME

        # Calendar check: look up current time in the timeline
        if calendar_timeline:
            now = datetime.now(UTC)
            for point in calendar_timeline:
                if point.start_time <= now < point.end_time:
                    mode_str = point.mode.lower()
                    if mode_str == "vacation":
                        return OccupancyMode.VACATION
                    if mode_str == "away":
                        return OccupancyMode.AWAY
                    if mode_str == "home" and not self.entity_ids:
                        # No presence entities: the calendar is all we have.
                        return OccupancyMode.HOME
                    break

        # Fall back to reactive detection
        return reactive_mode

    def _get_reactive_mode(self) -> OccupancyMode:
        """Entity-based occupancy detection (existing logic, extracted)."""
        if not self.entity_ids:
            return OccupancyMode.HOME

        now = datetime.now(UTC)
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
            for last in self._last_active.values():
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
