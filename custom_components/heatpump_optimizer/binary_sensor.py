"""Binary sensor entities for the Heat Pump Optimizer."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, VERSION


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Heat Pump Optimizer",
        manufacturer="Gerald Burkett",
        model="Heat Pump Optimizer",
        sw_version=VERSION,
    )
from .coordinator import HeatPumpOptimizerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor entities from a config entry."""
    coordinator: HeatPumpOptimizerCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        OptimizerActiveSensor(coordinator, entry),
        OverrideDetectedSensor(coordinator, entry),
        StaleSensorDetectedSensor(coordinator, entry),
        AuxHeatActiveSensor(coordinator, entry),
        LearningActiveSensor(coordinator, entry),
        ThermostatBlendSuspectedSensor(coordinator, entry),
    ]
    async_add_entities(entities)


class OptimizerActiveSensor(CoordinatorEntity, BinarySensorEntity):
    """Whether the optimizer is actively controlling the thermostat."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HeatPumpOptimizerCoordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_active"
        self._attr_name = "Optimizer Active"
        self._attr_icon = "mdi:robot"
        self._attr_device_class = BinarySensorDeviceClass.RUNNING
        self._attr_device_info = _device_info(entry)

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("active", False)


class OverrideDetectedSensor(CoordinatorEntity, BinarySensorEntity):
    """Whether a manual override has been detected."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HeatPumpOptimizerCoordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_override_detected"
        self._attr_name = "Override Detected"
        self._attr_icon = "mdi:hand-back-right"
        self._attr_device_class = BinarySensorDeviceClass.PROBLEM
        self._attr_device_info = _device_info(entry)

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("override_detected", False)


class StaleSensorDetectedSensor(CoordinatorEntity, BinarySensorEntity):
    """Whether the thermostat sensor appears stuck/stale (identical readings for 24h+)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HeatPumpOptimizerCoordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_sensor_stale"
        self._attr_name = "Sensor Stale"
        self._attr_icon = "mdi:thermometer-alert"
        self._attr_device_class = BinarySensorDeviceClass.PROBLEM
        self._attr_device_info = _device_info(entry)

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("sensor_stale", False)


class AuxHeatActiveSensor(CoordinatorEntity, BinarySensorEntity):
    """Whether auxiliary/emergency heat is currently running."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HeatPumpOptimizerCoordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_aux_heat_active"
        self._attr_name = "Aux Heat Active"
        self._attr_icon = "mdi:radiator"
        self._attr_device_class = BinarySensorDeviceClass.HEAT
        self._attr_device_info = _device_info(entry)

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("aux_heat_active", False)


class LearningActiveSensor(CoordinatorEntity, BinarySensorEntity):
    """Whether the thermal model is still in learning mode (below confidence threshold)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HeatPumpOptimizerCoordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_learning_active"
        self._attr_name = "Learning Active"
        self._attr_icon = "mdi:school"
        self._attr_device_info = _device_info(entry)

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("learning_active", False)

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "model_confidence": self.coordinator.data.get("kalman_confidence"),
            "observations": self.coordinator.data.get("kalman_observations"),
            "initialization_mode": self.coordinator.data.get("initialization_mode"),
        }


class ThermostatBlendSuspectedSensor(CoordinatorEntity, BinarySensorEntity):
    """Whether the thermostat is suspected of blending toward a satellite sensor.

    Turns on when the configured blend mitigation mode detects that the
    thermostat's reported temperature is influenced by a remote occupancy/
    comfort sensor (e.g. Ecobee Follow Me, Nest satellite sensors). During
    this period the thermostat reading is excluded or down-weighted in the
    EKF thermal model to protect building parameter estimates.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HeatPumpOptimizerCoordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_thermostat_blend_suspected"
        self._attr_name = "Thermostat Blend Suspected"
        self._attr_icon = "mdi:thermometer-lines"
        self._attr_device_class = BinarySensorDeviceClass.PROBLEM
        self._attr_device_info = _device_info(entry)

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("thermostat_blend_suspected", False)

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "blend_mode": self.coordinator.data.get("thermostat_blend_mode"),
            "cross_sensor_spread_f": self.coordinator.data.get("cross_sensor_spread_f"),
            "indoor_thermo_excluded": self.coordinator.data.get("indoor_thermo_excluded"),
        }
