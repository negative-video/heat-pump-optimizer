"""Adapter for reading and writing thermostat state via HomeKit climate entity."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from homeassistant.components.climate import (
    ATTR_CURRENT_HUMIDITY,
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_ACTION,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    SERVICE_SET_TEMPERATURE,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import TemperatureConverter

_LOGGER = logging.getLogger(__name__)

# Smallest heat/cool separation written to a dual-setpoint thermostat. Most
# reject a narrower pair, and some silently move the other setpoint instead.
MIN_DUAL_SETPOINT_GAP_F = 2.0


@dataclass
class ThermostatState:
    """Current thermostat state snapshot."""

    indoor_temp: float | None  # °F
    outdoor_temp: float | None  # °F (from weather, not thermostat)
    humidity: float | None  # %
    hvac_mode: str  # "cool", "heat", "heat_cool", "off"
    hvac_action: str | None  # "cooling", "heating", "idle", "off"
    target_temp: float | None  # °F (single setpoint)
    target_temp_high: float | None  # °F (dual setpoint - cool target)
    target_temp_low: float | None  # °F (dual setpoint - heat target)
    timestamp: datetime
    available: bool

    @property
    def effective_setpoint(self) -> float | None:
        """Return single effective setpoint, resolving dual-setpoint modes."""
        if self.target_temp is not None:
            return self.target_temp
        # Dual-setpoint: pick the active bound based on HVAC action
        if self.hvac_action == "heating":
            return self.target_temp_low
        if self.hvac_action == "cooling":
            return self.target_temp_high
        # Idle: use bound closest to current indoor temp
        if self.target_temp_low is not None and self.target_temp_high is not None:
            if self.indoor_temp is not None:
                mid = (self.target_temp_low + self.target_temp_high) / 2
                return self.target_temp_low if self.indoor_temp < mid else self.target_temp_high
            return self.target_temp_low
        return self.target_temp_low or self.target_temp_high


class ThermostatAdapter:
    """Read/write thermostat state with safety checks.

    Designed for HomeKit-connected Ecobee (local control, no rate limits).
    Tracks last-written setpoint to detect manual overrides.
    """

    def __init__(self, hass: HomeAssistant, climate_entity_id: str):
        self.hass = hass
        self.entity_id = climate_entity_id
        self._last_written_setpoint: float | None = None
        # On a heat_cool thermostat both setpoints are written; remember both
        # so override detection compares like with like.
        self._last_written_low: float | None = None
        self._last_written_high: float | None = None
        self._last_written_side: str | None = None
        self._last_write_time: datetime | None = None

    def read_state(self) -> ThermostatState:
        """Read current thermostat state from HA."""
        state = self.hass.states.get(self.entity_id)
        if state is None or state.state == "unavailable":
            return ThermostatState(
                indoor_temp=None,
                outdoor_temp=None,
                humidity=None,
                hvac_mode="off",
                hvac_action=None,
                target_temp=None,
                target_temp_high=None,
                target_temp_low=None,
                timestamp=datetime.now(timezone.utc),
                available=False,
            )

        attrs = state.attributes

        # Convert temps to °F
        indoor = self._to_fahrenheit(attrs.get(ATTR_CURRENT_TEMPERATURE))
        target = self._to_fahrenheit(attrs.get(ATTR_TEMPERATURE))
        target_high = self._to_fahrenheit(attrs.get(ATTR_TARGET_TEMP_HIGH))
        target_low = self._to_fahrenheit(attrs.get(ATTR_TARGET_TEMP_LOW))

        # Map HA HVAC mode to our mode strings
        hvac_mode = self._map_hvac_mode(state.state)
        hvac_action = attrs.get(ATTR_HVAC_ACTION)

        return ThermostatState(
            indoor_temp=indoor,
            outdoor_temp=None,  # Comes from weather entity, not thermostat
            humidity=attrs.get(ATTR_CURRENT_HUMIDITY),
            hvac_mode=hvac_mode,
            hvac_action=hvac_action,
            target_temp=target,
            target_temp_high=target_high,
            target_temp_low=target_low,
            timestamp=datetime.now(timezone.utc),
            available=True,
        )

    async def async_set_temperature(
        self,
        temperature: float,
        comfort_min: float,
        comfort_max: float,
        *,
        side: str | None = None,
        opposite: float | None = None,
    ) -> bool:
        """Set the thermostat target temperature with safety bounds.

        Args:
            temperature: Desired setpoint in °F.
            comfort_min: Absolute minimum allowed temperature.
            comfort_max: Absolute maximum allowed temperature.
            side: For a heat_cool thermostat, which setpoint ``temperature``
                is: "heat" (the low setpoint) or "cool" (the high one).
                Required there; the write is refused without it.
            opposite: For a heat_cool thermostat, the value for the other
                setpoint. When omitted the thermostat's existing value is
                left alone.

        Returns:
            True if the setpoint was written successfully.
        """
        # Clamp to comfort bounds
        clamped = max(comfort_min, min(comfort_max, temperature))
        if clamped != temperature:
            _LOGGER.info(
                "Clamped setpoint from %.1f°F to %.1f°F (bounds: %.1f-%.1f)",
                temperature, clamped, comfort_min, comfort_max,
            )
        clamped = self._round_setpoint(clamped)

        state = self.hass.states.get(self.entity_id)
        if state is None or state.state == "unavailable":
            _LOGGER.warning("Cannot write setpoint: thermostat unavailable")
            return False

        try:
            hvac_mode = self._map_hvac_mode(state.state)

            if hvac_mode == "heat_cool":
                # Dual-setpoint thermostat: write the side the plan means and
                # leave the other side where it is unless told otherwise.
                #
                # This used to guess the side from the indoor temperature
                # relative to the midpoint of whichever single comfort band
                # the caller passed, and then wrote that band's far edge as
                # the *other* setpoint. With a cool band in hand and a cold
                # house, that put the cool band's midpoint on the heat
                # setpoint: 70 F heat on a 31 F morning.
                attrs = state.attributes
                hvac_action = attrs.get(ATTR_HVAC_ACTION)
                low_now = self._to_fahrenheit(attrs.get(ATTR_TARGET_TEMP_LOW))
                high_now = self._to_fahrenheit(attrs.get(ATTR_TARGET_TEMP_HIGH))
                if side not in ("heat", "cool"):
                    # A bare number is ambiguous on a two-setpoint thermostat
                    # and guessing wrong puts a cooling target on the heat
                    # setpoint. Callers that mean a side must name it.
                    _LOGGER.error(
                        "Refusing to write %.1f°F to a heat_cool thermostat "
                        "without knowing which setpoint it is for",
                        clamped,
                    )
                    return False

                if side == "heat":
                    low = clamped
                    high = opposite if opposite is not None else high_now
                    if high is None:
                        high = max(comfort_max, low + MIN_DUAL_SETPOINT_GAP_F)
                    high = max(self._round_setpoint(high), low + MIN_DUAL_SETPOINT_GAP_F)
                else:
                    high = clamped
                    low = opposite if opposite is not None else low_now
                    if low is None:
                        low = min(comfort_min, high - MIN_DUAL_SETPOINT_GAP_F)
                    low = min(self._round_setpoint(low), high - MIN_DUAL_SETPOINT_GAP_F)

                service_data = {
                    "entity_id": self.entity_id,
                    ATTR_TARGET_TEMP_LOW: self._from_fahrenheit(low),
                    ATTR_TARGET_TEMP_HIGH: self._from_fahrenheit(high),
                }
                _LOGGER.debug(
                    "Dual-setpoint write: low=%.1f high=%.1f (side=%s, action=%s)",
                    low, high, side, hvac_action,
                )
            else:
                # Single-setpoint mode
                low = high = None
                side = None
                service_data = {
                    "entity_id": self.entity_id,
                    ATTR_TEMPERATURE: self._from_fahrenheit(clamped),
                }

            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                service_data,
                blocking=True,
            )
            self._last_written_setpoint = clamped
            self._last_written_low = low
            self._last_written_high = high
            self._last_written_side = side
            self._last_write_time = datetime.now(timezone.utc)
            _LOGGER.debug("Set thermostat to %.1f°F", clamped)
            return True
        except Exception:
            _LOGGER.error(
                "Failed to set thermostat temperature", exc_info=True
            )
            return False

    async def async_set_dual_setpoints(
        self,
        heat_floor: float,
        cool_ceiling: float,
        *,
        primary: str | None = None,
    ) -> bool:
        """Write an explicit heat floor / cool ceiling pair.

        Used for shoulder-season schedules, where the right answer is to hand
        the thermostat both bounds and let its own heat_cool arbitration
        decide, rather than re-deciding a single mode every cycle and writing
        a single setpoint that means different things at 06:00 and 15:00.

        ``primary`` ("heat" or "cool") names the side the plan is steering,
        which is the side ``last_written_setpoint`` reports.

        No-ops (returning False) on a thermostat that is not in heat_cool
        mode, since a single-setpoint device has nowhere to put the pair.
        """
        if cool_ceiling < heat_floor:
            heat_floor, cool_ceiling = cool_ceiling, heat_floor
        heat_floor = self._round_setpoint(heat_floor)
        cool_ceiling = max(
            self._round_setpoint(cool_ceiling), heat_floor + MIN_DUAL_SETPOINT_GAP_F
        )

        state = self.hass.states.get(self.entity_id)
        if state is None or state.state == "unavailable":
            _LOGGER.warning("Cannot write dual setpoints: thermostat unavailable")
            return False

        if self._map_hvac_mode(state.state) != "heat_cool":
            return False

        try:
            await self.hass.services.async_call(
                "climate",
                SERVICE_SET_TEMPERATURE,
                {
                    "entity_id": self.entity_id,
                    ATTR_TARGET_TEMP_LOW: self._from_fahrenheit(heat_floor),
                    ATTR_TARGET_TEMP_HIGH: self._from_fahrenheit(cool_ceiling),
                },
                blocking=True,
            )
        except Exception:
            _LOGGER.error("Failed to set dual setpoints", exc_info=True)
            return False

        if primary not in ("heat", "cool"):
            # Track the side the house is more likely to be held at.
            indoor = self._to_fahrenheit(state.attributes.get(ATTR_CURRENT_TEMPERATURE))
            mid = (heat_floor + cool_ceiling) / 2
            primary = "cool" if indoor is not None and indoor >= mid else "heat"
        self._last_written_setpoint = cool_ceiling if primary == "cool" else heat_floor
        self._last_written_low = heat_floor
        self._last_written_high = cool_ceiling
        self._last_written_side = primary
        self._last_write_time = datetime.now(timezone.utc)
        _LOGGER.debug(
            "Set dual setpoints: heat %.1f°F / cool %.1f°F", heat_floor, cool_ceiling,
        )
        return True

    async def async_write_flat_default(
        self,
        heat_floor: float,
        cool_ceiling: float,
        mode: str | None,
    ) -> bool:
        """Hand the thermostat back the plain, flat comfort edges.

        What a thermostat with no optimizer would be holding: the heat floor
        and, where it has one, the cool ceiling. Used when the optimizer
        stops steering (shutdown, forecast outage) so it does not leave a
        pre-conditioning setpoint behind.

        This replaces a "safe midpoint" write. The midpoint of a comfort band
        is not a neutral value on a dual-setpoint thermostat: written as the
        heat setpoint it is several degrees of heating nobody asked for.
        """
        state = self.hass.states.get(self.entity_id)
        if state is None or state.state == "unavailable":
            return False
        hvac_mode = self._map_hvac_mode(state.state)
        if hvac_mode == "heat_cool":
            return await self.async_set_dual_setpoints(
                heat_floor, cool_ceiling, primary=mode,
            )
        if hvac_mode == "heat":
            return await self.async_set_temperature(heat_floor, heat_floor, cool_ceiling)
        if hvac_mode == "cool":
            return await self.async_set_temperature(cool_ceiling, heat_floor, cool_ceiling)
        return False

    @staticmethod
    def _round_setpoint(temp_f: float) -> float:
        """Round to the half degree a thermostat can actually hold.

        A setpoint like 60.53 comes back from the device as 60.5 or 61, and
        the difference then reads as someone having changed it by hand.
        """
        return round(temp_f * 2) / 2

    @staticmethod
    def _infer_side(
        hvac_action: str | None,
        indoor: float | None,
        low: float | None,
        high: float | None,
    ) -> str:
        """Which setpoint of a heat_cool thermostat is in play right now."""
        if hvac_action == "heating":
            return "heat"
        if hvac_action == "cooling":
            return "cool"
        if indoor is not None and low is not None and high is not None:
            return "heat" if (indoor - low) <= (high - indoor) else "cool"
        return "heat" if low is not None and high is None else "cool"

    def expected_and_actual(
        self, state: ThermostatState,
    ) -> tuple[float | None, float | None]:
        """What we last wrote and what the thermostat shows, like for like.

        On a heat_cool thermostat ``effective_setpoint`` picks a side from the
        indoor temperature, which is not necessarily the side we wrote. Having
        written heat 60 / cool 76 into a 70 F house, it reports 76, and
        comparing that with the 60 we remember looks like a manual override.
        Compare low with low and high with high instead.
        """
        if (
            state.hvac_mode == "heat_cool"
            and self._last_written_low is not None
            and self._last_written_high is not None
            and state.target_temp_low is not None
            and state.target_temp_high is not None
        ):
            d_low = abs(state.target_temp_low - self._last_written_low)
            d_high = abs(state.target_temp_high - self._last_written_high)
            if d_high > d_low:
                return (self._last_written_high, state.target_temp_high)
            return (self._last_written_low, state.target_temp_low)
        return (self._last_written_setpoint, state.effective_setpoint)

    def detect_override(self) -> bool:
        """Check if someone manually changed the setpoint since our last write.

        Returns True if the thermostat's current setpoint differs from
        what we last wrote.
        """
        if self._last_written_setpoint is None:
            return False

        state = self.read_state()
        current = state.effective_setpoint
        if not state.available or current is None:
            return False

        # Allow 0.5°F tolerance for rounding
        diff = abs(current - self._last_written_setpoint)
        if diff > 0.5:
            _LOGGER.info(
                "Override detected: expected %.1f°F, thermostat shows %.1f°F",
                self._last_written_setpoint,
                current,
            )
            return True
        return False

    def get_active_mode(self) -> str:
        """Determine the current HVAC mode for optimization.

        Returns "cool", "heat", or "off".
        """
        state = self.read_state()
        if state.hvac_mode in ("cool", "heat"):
            return state.hvac_mode
        if state.hvac_mode == "heat_cool":
            # Dual mode — check what's actually running
            if state.hvac_action == HVACAction.COOLING:
                return "cool"
            if state.hvac_action == HVACAction.HEATING:
                return "heat"
            # Idle in auto mode: whichever setpoint the house is nearer to
            # is the side that will run next. This used to return "cool"
            # unconditionally, so every idle winter cycle selected the
            # cooling comfort band.
            return self._infer_side(
                None, state.indoor_temp, state.target_temp_low, state.target_temp_high,
            )
        return "off"

    @property
    def last_written_setpoint(self) -> float | None:
        """The last setpoint this adapter wrote to the thermostat."""
        return self._last_written_setpoint

    @property
    def last_write_time(self) -> datetime | None:
        """When the last setpoint was written."""
        return self._last_write_time

    def _to_fahrenheit(self, temp: float | None) -> float | None:
        """Convert a temperature from HA's unit system to °F."""
        if temp is None:
            return None
        if self.hass.config.units.temperature_unit == UnitOfTemperature.CELSIUS:
            return TemperatureConverter.convert(
                temp, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
            )
        return temp

    def _from_fahrenheit(self, temp_f: float) -> float:
        """Convert °F back to HA's unit system for service calls."""
        if self.hass.config.units.temperature_unit == UnitOfTemperature.CELSIUS:
            return TemperatureConverter.convert(
                temp_f, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
            )
        return temp_f

    @staticmethod
    def _map_hvac_mode(ha_state: str) -> str:
        """Map HA HVAC mode string to our internal mode."""
        mode_map = {
            HVACMode.COOL: "cool",
            HVACMode.HEAT: "heat",
            HVACMode.HEAT_COOL: "heat_cool",
            HVACMode.AUTO: "heat_cool",
            HVACMode.OFF: "off",
        }
        return mode_map.get(ha_state, "off")
