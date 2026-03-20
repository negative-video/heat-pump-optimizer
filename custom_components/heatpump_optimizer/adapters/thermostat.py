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
    ) -> bool:
        """Set the thermostat target temperature with safety bounds.

        Args:
            temperature: Desired setpoint in °F.
            comfort_min: Absolute minimum allowed temperature.
            comfort_max: Absolute maximum allowed temperature.

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

        state = self.hass.states.get(self.entity_id)
        if state is None or state.state == "unavailable":
            _LOGGER.warning("Cannot write setpoint: thermostat unavailable")
            return False

        # Convert back to HA's unit system if needed
        temp_value = self._from_fahrenheit(clamped)

        try:
            service_data = {
                "entity_id": self.entity_id,
                ATTR_TEMPERATURE: temp_value,
            }
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                service_data,
                blocking=True,
            )
            self._last_written_setpoint = clamped
            self._last_write_time = datetime.now(timezone.utc)
            _LOGGER.debug("Set thermostat to %.1f°F", clamped)
            return True
        except Exception:
            _LOGGER.error(
                "Failed to set thermostat temperature", exc_info=True
            )
            return False

    async def async_write_safe_default(
        self, comfort_min: float, comfort_max: float
    ) -> None:
        """Write comfort midpoint as a safe fallback (e.g., on shutdown)."""
        midpoint = (comfort_min + comfort_max) / 2
        _LOGGER.info("Writing safe default setpoint: %.1f°F", midpoint)
        await self.async_set_temperature(midpoint, comfort_min, comfort_max)

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
            # Idle in auto mode — use outdoor temp to guess
            return "cool"  # Default; coordinator should use weather to decide
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
