"""Base class and shared mixins for Heat Pump Optimizer sensor entities.

Extracted from sensor.py so the ~60 concrete sensor classes sit on top of
a small, focused plumbing module instead of being interleaved with mixins.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DEFAULT_MODEL_CONFIDENCE_THRESHOLD, DOMAIN, VERSION
from .coordinator import HeatPumpOptimizerCoordinator


class OptimizerBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for optimizer sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HeatPumpOptimizerCoordinator,
        entry: ConfigEntry,
        key: str,
        name: str,
    ):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Heat Pump Optimizer",
            manufacturer="Gerald Burkett",
            model="Heat Pump Optimizer",
            sw_version=VERSION,
            entry_type=None,
        )
        self._key = key


class _LearningNullMixin:
    """Return None for savings values while in learning mode.

    During the learning tier, savings are meaningless (always zero).
    Returning None renders as 'Unknown' in HA, which is more honest
    than showing $0.00 / 0.00 kWh on day one.
    """

    def _suppress_during_learning(self, value):
        if value is None:
            return None
        tier = (self.coordinator.data or {}).get(
            "savings_accuracy_tier", "learning"
        )
        if tier == "learning":
            return None
        return value

    @property
    def _is_projected(self) -> bool:
        """Whether current savings values are projected (early estimates)."""
        tier = (self.coordinator.data or {}).get(
            "savings_accuracy_tier", "learning"
        )
        return tier == "projected"


class _DailyResetMixin:
    """Mixin providing last_reset at local midnight for daily sensors."""

    @property
    def last_reset(self) -> datetime:
        return dt_util.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        )


class _ThermalLoadMixin:
    """Shared helpers for thermal load component sensors."""

    def _get_components(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return self.coordinator.data.get("thermal_load_components") or {}

    def _confidence_attrs(self) -> dict:
        if self.coordinator.data is None:
            return {}
        confidence = self.coordinator.data.get("kalman_confidence", 0.0)
        return {
            "model_confidence": round(confidence, 2),
            "reliable": confidence >= DEFAULT_MODEL_CONFIDENCE_THRESHOLD,
        }
