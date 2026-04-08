"""ApplianceManager — centralized reader for auxiliary appliances that impact the thermal envelope.

Tracks the on/off state and power draw of configured appliances (e.g., heat pump
water heater, dryer, oven) and computes the net thermal impact in BTU/hr for the
EKF thermal estimator and performance profiler.

Follows the SensorHub pattern: reads entity states via hass.states.get(), handles
unavailable/unknown states gracefully, and returns 0 when no appliances are configured.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant

from ..engine.data_types import ApplianceConfig, ApplianceState

_LOGGER = logging.getLogger(__name__)


class ApplianceManager:
    """Manages auxiliary appliances that impact the thermal envelope."""

    def __init__(self, hass: HomeAssistant, config_json: str | None) -> None:
        self._hass = hass
        self._configs: list[ApplianceConfig] = []
        self._states: list[ApplianceState] = []

        if config_json:
            self._configs = self.deserialize(config_json)
            self._states = [ApplianceState(config=c) for c in self._configs]

    def update(self) -> None:
        """Read all appliance entity states from HA. Called each coordinator cycle."""
        for app_state in self._states:
            cfg = app_state.config

            # Read state entity
            state_obj = self._hass.states.get(cfg.state_entity)
            if state_obj is None or state_obj.state in ("unavailable", "unknown"):
                app_state.is_active = False
                app_state.current_power_watts = None
                continue

            # Check if the entity's state matches any of the configured active states
            app_state.is_active = state_obj.state.lower() in [
                s.lower() for s in cfg.active_states
            ]

            # Determine power draw
            app_state.current_power_watts = self._read_power(cfg, app_state.is_active)

    def _read_power(
        self, cfg: ApplianceConfig, is_active: bool
    ) -> float | None:
        """Read or estimate instantaneous power draw in watts."""
        if not is_active:
            return 0.0

        # Priority 1: Real-time power entity reporting W or kW
        if cfg.power_entity:
            state_obj = self._hass.states.get(cfg.power_entity)
            if state_obj is not None and state_obj.state not in (
                "unavailable",
                "unknown",
            ):
                try:
                    value = float(state_obj.state)
                    uom = state_obj.attributes.get("unit_of_measurement", "")
                    device_class = state_obj.attributes.get("device_class", "")

                    # Only use if it's a real-time power sensor (W/kW)
                    if uom in ("W", "w"):
                        return value
                    if uom in ("kW", "kw"):
                        return value * 1000.0
                    # device_class=power without explicit unit — assume W
                    if device_class == "power":
                        return value

                    # If it's an energy sensor (kWh), skip — too stale for real-time
                    # Fall through to estimated_watts
                except (ValueError, TypeError):
                    pass

        # Priority 2: Configured estimated wattage
        if cfg.estimated_watts is not None:
            return cfg.estimated_watts

        # Priority 3: No power data available
        return None

    def _thermal_impact_btu_for(self, state: ApplianceState) -> float:
        """Compute real-time BTU/hr for a single appliance."""
        if not state.is_active:
            return 0.0
        cfg = state.config

        # Mode A: watts-based — thermal_factor converts watts to BTU/hr
        if cfg.thermal_factor is not None:
            if state.current_power_watts is not None and state.current_power_watts > 0:
                return state.current_power_watts * cfg.thermal_factor
            if cfg.estimated_watts is not None and cfg.estimated_watts > 0:
                return cfg.estimated_watts * cfg.thermal_factor

        # Mode B: static BTU/hr (HPWH-style or legacy configs)
        return cfg.thermal_impact_btu

    def total_thermal_impact_btu(self) -> float:
        """Sum of BTU/hr from all currently active appliances."""
        return sum(self._thermal_impact_btu_for(s) for s in self._states)

    def total_humidity_impact(self) -> float | None:
        """Sum of %RH/hr from all currently active appliances, or None if none configured."""
        impacts = [
            s.config.humidity_impact
            for s in self._states
            if s.is_active and s.config.humidity_impact is not None
        ]
        return sum(impacts) if impacts else None

    def get_appliance_states(self) -> list[ApplianceState]:
        """Return current state of all configured appliances."""
        return list(self._states)

    def get_active_appliances(self) -> list[ApplianceState]:
        """Return only currently active appliances."""
        return [s for s in self._states if s.is_active]

    def get_diagnostics(self) -> dict[str, Any]:
        """Per-appliance status dict for sensor/panel exposure."""
        appliances = []
        for s in self._states:
            appliances.append({
                "id": s.config.id,
                "name": s.config.name,
                "active": s.is_active,
                "thermal_impact_btu": self._thermal_impact_btu_for(s),
                "power_watts": s.current_power_watts,
                "state_entity": s.config.state_entity,
            })
        return {
            "appliances": appliances,
            "total_thermal_impact_btu": self.total_thermal_impact_btu(),
            "active_count": len(self.get_active_appliances()),
            "configured_count": len(self._configs),
        }

    @property
    def configured(self) -> bool:
        """Whether any appliances are configured."""
        return len(self._configs) > 0

    @staticmethod
    def serialize(configs: list[dict[str, Any]]) -> str:
        """Serialize a list of appliance config dicts to JSON string for options storage."""
        return json.dumps(configs)

    @staticmethod
    def deserialize(raw: str) -> list[ApplianceConfig]:
        """Parse JSON string back to list of ApplianceConfig."""
        try:
            items = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            _LOGGER.warning("Failed to parse auxiliary appliances config: %s", raw)
            return []

        configs = []
        for item in items:
            try:
                raw_factor = item.get("thermal_factor")
                if raw_factor is not None:
                    thermal_factor = float(raw_factor)
                else:
                    # Legacy config without thermal_factor — back-calculate it
                    btu = float(item.get("thermal_impact_btu", 0))
                    watts = item.get("estimated_watts")
                    if watts and float(watts) > 0:
                        thermal_factor = btu / float(watts)
                    elif item.get("power_entity"):
                        thermal_factor = 3.412
                    else:
                        thermal_factor = None
                configs.append(ApplianceConfig(
                    id=item["id"],
                    name=item["name"],
                    state_entity=item["state_entity"],
                    active_states=item.get("active_states", ["on"]),
                    thermal_impact_btu=float(item.get("thermal_impact_btu", 0)),
                    thermal_factor=thermal_factor,
                    power_entity=item.get("power_entity"),
                    estimated_watts=item.get("estimated_watts"),
                    humidity_impact=item.get("humidity_impact"),
                    controllable=item.get("controllable", False),
                    control_entity=item.get("control_entity"),
                ))
            except (KeyError, ValueError, TypeError) as exc:
                _LOGGER.warning(
                    "Skipping invalid appliance config %s: %s", item, exc
                )
        return configs
