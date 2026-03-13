"""Switch entity for enabling/disabling the Heat Pump Optimizer."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HeatPumpOptimizerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities from a config entry."""
    coordinator: HeatPumpOptimizerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([OptimizerEnabledSwitch(coordinator, entry)])


class OptimizerEnabledSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable the optimizer.

    When off, the optimizer stops writing setpoints (same as pause service).
    When on, optimization resumes (same as resume service).
    Visible in dashboards and controllable by automations/scenes.
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: HeatPumpOptimizerCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_enabled"
        self._attr_name = "Optimizer Enabled"
        self._attr_icon = "mdi:robot"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Heat Pump Optimizer",
            manufacturer="Heat Pump Optimizer",
            model="Optimizer",
            sw_version="0.1.0",
        )

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("active", False)

    async def async_turn_on(self, **kwargs) -> None:
        """Resume optimization."""
        await self._coordinator.async_resume()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Pause optimization."""
        self._coordinator.pause()
        self.async_write_ha_state()
