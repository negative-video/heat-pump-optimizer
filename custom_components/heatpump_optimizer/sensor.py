"""Sensor entities for the Heat Pump Optimizer."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from datetime import datetime, timezone

from .const import DOMAIN
from .coordinator import HeatPumpOptimizerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities from a config entry."""
    coordinator: HeatPumpOptimizerCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        OptimizerPhaseSensor(coordinator, entry),
        TargetSetpointSensor(coordinator, entry),
        NextActionSensor(coordinator, entry),
        PredictedTempSensor(coordinator, entry),
        PredictionErrorSensor(coordinator, entry),
        ModelAccuracySensor(coordinator, entry),
        SavingsPercentSensor(coordinator, entry),
        # Kalman filter / adaptive model sensors
        ModelConfidenceSensor(coordinator, entry),
        EnvelopeRValueSensor(coordinator, entry),
        ThermalMassSensor(coordinator, entry),
        CoolingCapacitySensor(coordinator, entry),
        HeatingCapacitySensor(coordinator, entry),
        ThermalMassTempSensor(coordinator, entry),
        # Grey-box model sensor
        GreyBoxActiveSensor(coordinator, entry),
        # Diagnostic sensors
        TacticalCorrectionSensor(coordinator, entry),
        ForecastDeviationSensor(coordinator, entry),
        ScheduleSensor(coordinator, entry),
        # SensorHub diagnostic sensors
        OutdoorTempSourceSensor(coordinator, entry),
        IndoorTempSourceSensor(coordinator, entry),
        NetHvacPowerSensor(coordinator, entry),
        # Savings tracking sensors
        SavingsKwhTodaySensor(coordinator, entry),
        SavingsKwhCumulativeSensor(coordinator, entry),
        SavingsCostTodaySensor(coordinator, entry),
        SavingsCostCumulativeSensor(coordinator, entry),
        SavingsCO2TodaySensor(coordinator, entry),
        SavingsCO2CumulativeSensor(coordinator, entry),
        BaselineKwhTodaySensor(coordinator, entry),
        WorstCaseKwhTodaySensor(coordinator, entry),
        # Calendar occupancy / pre-conditioning sensors
        OccupancyForecastSensor(coordinator, entry),
        PreconditioningStatusSensor(coordinator, entry),
        # Comfort / humidity sensors
        ApparentTemperatureSensor(coordinator, entry),
        # Room-aware sensing (only meaningful when configured)
        OccupiedRoomsSensor(coordinator, entry),
        WeightedIndoorTempSensor(coordinator, entry),
        # Resilience diagnostics
        SourceHealthSensor(coordinator, entry),
        # Counterfactual digital twin savings sensors
        RuntimeSavingsTodaySensor(coordinator, entry),
        CopSavingsTodaySensor(coordinator, entry),
        RateSavingsTodaySensor(coordinator, entry),
        CarbonShiftSavingsTodaySensor(coordinator, entry),
        BaselineAvgCopSensor(coordinator, entry),
        OptimizedAvgCopSensor(coordinator, entry),
        CopImprovementPctSensor(coordinator, entry),
        ComfortHoursGainedSensor(coordinator, entry),
        BaselineComfortViolationsSensor(coordinator, entry),
        BaselineAvgIndoorTempSensor(coordinator, entry),
        BaselineConfidenceSensor(coordinator, entry),
        SavingsAccuracyTierSensor(coordinator, entry),
        ProfilerConfidenceSensor(coordinator, entry),
        ProfilerActiveSensor(coordinator, entry),
        ProfilerObservationsSensor(coordinator, entry),
        LearningProgressSensor(coordinator, entry),
    ]
    async_add_entities(entities)


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
            manufacturer="Heat Pump Optimizer",
            model="Optimizer",
            sw_version="0.1.0",
            entry_type=None,
        )
        self._key = key


class OptimizerPhaseSensor(OptimizerBaseSensor):
    """Current optimizer phase (pre-cooling, coasting, etc.)."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "current_phase", "Current Phase")
        self._attr_icon = "mdi:strategy"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("phase")


class TargetSetpointSensor(OptimizerBaseSensor):
    """Optimizer's current desired setpoint."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "target_setpoint", "Target Setpoint")
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:thermostat"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("target_setpoint")


class NextActionSensor(OptimizerBaseSensor):
    """Human-readable description of the next scheduled action."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "next_action", "Next Action")
        self._attr_icon = "mdi:clock-outline"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("next_action")


class PredictedTempSensor(OptimizerBaseSensor):
    """Model's predicted indoor temperature right now."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "predicted_indoor_temp", "Predicted Indoor Temp")
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:home-thermometer"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("predicted_indoor_temp")


class PredictionErrorSensor(OptimizerBaseSensor):
    """Difference between actual and predicted indoor temp."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "prediction_error", "Prediction Error")
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:chart-line-variant"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("prediction_error")


class ModelAccuracySensor(OptimizerBaseSensor):
    """Rolling mean absolute error of model predictions."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "model_accuracy", "Model Accuracy (MAE)")
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:bullseye-arrow"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("model_accuracy_mae")


class SavingsPercentSensor(OptimizerBaseSensor):
    """Estimated runtime savings percentage from current schedule."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "estimated_savings", "Estimated Savings")
        self._attr_native_unit_of_measurement = "%"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:leaf"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("savings_pct")


# ── Kalman filter / adaptive model sensors ──────────────────────────


class ModelConfidenceSensor(OptimizerBaseSensor):
    """Kalman filter model confidence (0-100%)."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "model_confidence", "Model Confidence")
        self._attr_native_unit_of_measurement = "%"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 0
        self._attr_icon = "mdi:brain"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get("kalman_confidence")
        return round(val * 100, 1) if val is not None else None


class EnvelopeRValueSensor(OptimizerBaseSensor):
    """Learned building envelope thermal resistance."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "envelope_r_value", "Envelope R-Value")
        self._attr_native_unit_of_measurement = "°F·hr/BTU"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:home-thermometer-outline"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("kalman_r_value")


class ThermalMassSensor(OptimizerBaseSensor):
    """Learned building thermal mass capacitance."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "thermal_mass", "Thermal Mass")
        self._attr_native_unit_of_measurement = "BTU/°F"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 0
        self._attr_icon = "mdi:wall"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("kalman_thermal_mass")


class CoolingCapacitySensor(OptimizerBaseSensor):
    """Learned cooling capacity at reference temperature."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "cooling_capacity", "Cooling Capacity")
        self._attr_native_unit_of_measurement = "BTU/hr"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 0
        self._attr_icon = "mdi:snowflake"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("kalman_cooling_capacity")


class HeatingCapacitySensor(OptimizerBaseSensor):
    """Learned heating capacity at reference temperature."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "heating_capacity", "Heating Capacity")
        self._attr_native_unit_of_measurement = "BTU/hr"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 0
        self._attr_icon = "mdi:fire"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("kalman_heating_capacity")


class ThermalMassTempSensor(OptimizerBaseSensor):
    """Hidden thermal mass temperature (wall/slab temperature estimate)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "thermal_mass_temp", "Thermal Mass Temperature")
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:thermometer-lines"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("kalman_mass_temp")


class GreyBoxActiveSensor(OptimizerBaseSensor):
    """Whether the grey-box LP optimizer is currently active."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "greybox_active", "Grey-Box Model Active")
        self._attr_icon = "mdi:chart-timeline-variant-shimmer"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        active = self.coordinator.data.get("using_greybox_model", False)
        return "Active" if active else "Inactive"

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "using_adaptive_model": self.coordinator.data.get("using_adaptive_model", False),
            "using_greybox_model": self.coordinator.data.get("using_greybox_model", False),
            "kalman_confidence": self.coordinator.data.get("kalman_confidence"),
            "kalman_observations": self.coordinator.data.get("kalman_observations"),
        }


# ── Diagnostic sensors ──────────────────────────────────────────────


class TacticalCorrectionSensor(OptimizerBaseSensor):
    """Current tactical correction being applied to the scheduled setpoint."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "tactical_correction", "Tactical Correction")
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:tune-vertical"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("tactical_correction")


class ForecastDeviationSensor(OptimizerBaseSensor):
    """Max deviation between current forecast and optimization snapshot."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "forecast_deviation", "Forecast Deviation")
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:weather-partly-cloudy"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("forecast_deviation")


class ScheduleSensor(OptimizerBaseSensor):
    """Current schedule entry count, with full schedule as attributes."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "schedule", "Optimization Schedule")
        self._attr_icon = "mdi:calendar-clock"

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("schedule_entries")

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        attrs = {}
        detail = self.coordinator.data.get("schedule_detail")
        if detail:
            attrs["entries"] = detail
        forecast = self.coordinator.data.get("forecast_detail")
        if forecast:
            attrs["forecast"] = forecast
        attrs["mode"] = self.coordinator.data.get("mode")
        attrs["last_optimization"] = self.coordinator.data.get("last_optimization")
        attrs["savings_pct"] = self.coordinator.data.get("savings_pct")
        attrs["baseline_runtime"] = self.coordinator.data.get("baseline_runtime")
        attrs["optimized_runtime"] = self.coordinator.data.get("optimized_runtime")
        return attrs


# ── SensorHub diagnostic sensors ────────────────────────────────────


class OutdoorTempSourceSensor(OptimizerBaseSensor):
    """Outdoor temperature currently used by the optimizer, with source info."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "outdoor_temp_source", "Outdoor Temp Source")
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:thermometer"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        info = self.coordinator.data.get("outdoor_temp_info", {})
        return info.get("value")

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        info = self.coordinator.data.get("outdoor_temp_info", {})
        return {
            "source": info.get("source", "unknown"),
            "stale": info.get("stale", False),
            "entity_count": info.get("entity_count", 0),
            "entities": info.get("entities", []),
        }


class IndoorTempSourceSensor(OptimizerBaseSensor):
    """Indoor temperature used by the optimizer (may average multiple sensors)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "indoor_temp_source", "Indoor Temp Source")
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:home-thermometer"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        info = self.coordinator.data.get("indoor_temp_info", {})
        return info.get("value")

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        info = self.coordinator.data.get("indoor_temp_info", {})
        return {
            "source": info.get("source", "unknown"),
            "stale": info.get("stale", False),
            "entity_count": info.get("entity_count", 0),
            "entities": info.get("entities", []),
        }


class NetHvacPowerSensor(OptimizerBaseSensor):
    """Net HVAC power draw after solar offset."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "net_hvac_power", "Net HVAC Power")
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_native_unit_of_measurement = "W"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:flash"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        # Read gross power from SensorHub, then compute net
        hub = self.coordinator.sensor_hub
        gross = hub.read_power_draw()
        if gross is None:
            return None
        return hub.read_net_power_draw(gross)

    @property
    def extra_state_attributes(self) -> dict:
        hub = self.coordinator.sensor_hub
        gross = hub.read_power_draw()
        solar = hub.read_solar_production()
        net = hub.read_net_power_draw(gross) if gross is not None else None
        return {
            "gross_power_watts": gross,
            "solar_production_watts": solar.value if solar else None,
            "net_power_watts": net,
            "solar_entity": hub._solar_production_entity,
        }


# ── Savings tracking sensors ────────────────────────────────────────


class _DailyResetMixin:
    """Mixin providing last_reset at midnight UTC for daily sensors."""

    @property
    def last_reset(self) -> datetime:
        return datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )


class SavingsKwhTodaySensor(_DailyResetMixin, OptimizerBaseSensor):
    """Energy saved today vs baseline (kWh)."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "savings_kwh_today", "Energy Saved Today")
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:lightning-bolt"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("savings_kwh_today")

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "runtime_component": self.coordinator.data.get("runtime_savings_kwh_today"),
            "cop_component": self.coordinator.data.get("cop_savings_kwh_today"),
            "source": self.coordinator.data.get("savings_accuracy_tier"),
        }


class SavingsKwhCumulativeSensor(OptimizerBaseSensor):
    """All-time cumulative energy saved (kWh)."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "savings_kwh_cumulative", "Energy Saved Total")
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:lightning-bolt"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("savings_kwh_cumulative")


class SavingsCostTodaySensor(_DailyResetMixin, OptimizerBaseSensor):
    """Money saved today vs baseline ($)."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "savings_cost_today", "Cost Saved Today")
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_native_unit_of_measurement = "$"
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:currency-usd"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("savings_cost_today")

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "runtime_component": self.coordinator.data.get("runtime_cost_savings_today"),
            "rate_arbitrage_component": self.coordinator.data.get("rate_arbitrage_savings_today"),
            "cop_component": self.coordinator.data.get("cop_cost_savings_today"),
        }


class SavingsCostCumulativeSensor(OptimizerBaseSensor):
    """All-time cumulative money saved ($)."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "savings_cost_cumulative", "Cost Saved Total")
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_native_unit_of_measurement = "$"
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:currency-usd"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("savings_cost_cumulative")


class SavingsCO2TodaySensor(_DailyResetMixin, OptimizerBaseSensor):
    """CO2 emissions avoided today (grams)."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "savings_co2_today", "CO2 Avoided Today")
        self._attr_native_unit_of_measurement = "g"
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 0
        self._attr_icon = "mdi:molecule-co2"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("savings_co2_today_grams")

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "runtime_component": self.coordinator.data.get("runtime_co2_savings_today"),
            "carbon_shift_component": self.coordinator.data.get("carbon_shift_savings_today"),
        }


class SavingsCO2CumulativeSensor(OptimizerBaseSensor):
    """All-time cumulative CO2 avoided (kg)."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "savings_co2_cumulative", "CO2 Avoided Total")
        self._attr_native_unit_of_measurement = "kg"
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:molecule-co2"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        grams = self.coordinator.data.get("savings_co2_cumulative_grams")
        if grams is None:
            return None
        return grams / 1000.0  # convert grams → kg


class BaselineKwhTodaySensor(_DailyResetMixin, OptimizerBaseSensor):
    """Estimated baseline energy usage today without optimizer (kWh)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "baseline_kwh_today", "Baseline Energy Today")
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:gauge"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("baseline_kwh_today")


class WorstCaseKwhTodaySensor(_DailyResetMixin, OptimizerBaseSensor):
    """Worst-case energy usage today if HVAC ran 24/7 (kWh)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "worst_case_kwh_today", "Worst Case Energy Today")
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:gauge-full"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("worst_case_kwh_today")


# ── Calendar Occupancy / Pre-conditioning ──────────────────────────


class OccupancyForecastSensor(OptimizerBaseSensor):
    """Predicted occupancy mode from calendar timeline."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "occupancy_forecast", "Occupancy Forecast")
        self._attr_icon = "mdi:calendar-account"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("occupancy_mode")

    @property
    def extra_state_attributes(self) -> dict | None:
        if self.coordinator.data is None:
            return None
        transition = self.coordinator.data.get("next_occupancy_transition")
        return {
            "source": self.coordinator.data.get("occupancy_forecast_source", "reactive"),
            "timeline_segments": self.coordinator.data.get("occupancy_timeline_segments", 0),
            "next_transition": transition.get("time") if transition else None,
            "next_transition_type": transition.get("type") if transition else None,
        }


class PreconditioningStatusSensor(OptimizerBaseSensor):
    """Pre-conditioning plan status and details."""

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry, "preconditioning_status", "Pre-conditioning Status"
        )
        self._attr_icon = "mdi:home-clock"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("precondition_status", "not_configured")

    @property
    def extra_state_attributes(self) -> dict | None:
        if self.coordinator.data is None:
            return None
        plan = self.coordinator.data.get("precondition_plan")
        if plan is None:
            return {"configured": self.coordinator.data.get(
                "occupancy_forecast_source", "reactive"
            ) == "calendar"}
        return {
            "scheduled_start": plan.get("scheduled_start"),
            "arrival_time": plan.get("arrival_time"),
            "arrival_source": plan.get("arrival_source"),
            "estimated_runtime_minutes": plan.get("estimated_runtime_minutes"),
            "estimated_energy_kwh": plan.get("estimated_energy_kwh"),
            "estimated_cost": plan.get("estimated_cost"),
            "temperature_gap": plan.get("temperature_gap"),
        }


class ApparentTemperatureSensor(OptimizerBaseSensor):
    """Indoor apparent (feels-like) temperature adjusted for humidity.

    Uses the NWS heat index formula to show what temperature occupants
    actually perceive, accounting for indoor humidity levels.
    """

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry, "apparent_temperature", "Apparent Temperature"
        )
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:thermometer-water"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("apparent_temperature")

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "raw_indoor_temp": self.coordinator.data.get("current_indoor_temp"),
            "indoor_humidity": self.coordinator.data.get("indoor_humidity"),
        }


class OccupiedRoomsSensor(OptimizerBaseSensor):
    """Count of currently occupied rooms when room-aware sensing is active."""

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry, "occupied_rooms", "Occupied Rooms"
        )
        self._attr_icon = "mdi:home-account"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        area_data = self.coordinator.data.get("area_occupancy")
        if area_data is None:
            return "Not configured"
        occupied = sum(1 for a in area_data if a.get("occupied"))
        total = len(area_data)
        return f"{occupied} of {total}"

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        area_data = self.coordinator.data.get("area_occupancy")
        if area_data is None:
            return {"configured": False}
        return {
            "configured": True,
            "rooms": [
                {
                    "name": a.get("area_name"),
                    "occupied": a.get("occupied"),
                    "weight": a.get("weight"),
                }
                for a in area_data
            ],
        }


class WeightedIndoorTempSensor(OptimizerBaseSensor):
    """Weighted indoor temperature used by the optimizer when room-aware sensing is active.

    Per-room apparent temperature, occupancy, and weight are exposed as attributes.
    """

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry, "weighted_indoor_temp", "Weighted Indoor Temperature"
        )
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:thermometer-lines"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        area_data = self.coordinator.data.get("area_occupancy")
        if area_data is None:
            # Not configured — show raw thermostat temp
            return self.coordinator.data.get("current_indoor_temp")
        # Compute weighted from area data
        total = 0.0
        total_weight = 0.0
        for a in area_data:
            temp = a.get("temp")
            weight = a.get("weight", 0.0)
            if temp is not None:
                total += temp * weight
                total_weight += weight
        if total_weight == 0.0:
            return self.coordinator.data.get("current_indoor_temp")
        return round(total / total_weight, 1)

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        area_data = self.coordinator.data.get("area_occupancy")
        if area_data is None:
            return {"room_aware": False}
        return {
            "room_aware": True,
            "rooms": [
                {
                    "name": a.get("area_name"),
                    "temp": a.get("temp"),
                    "humidity": a.get("humidity"),
                    "apparent_temp": a.get("apparent_temp"),
                    "occupied": a.get("occupied"),
                    "weight": a.get("weight"),
                }
                for a in area_data
            ],
        }


class SourceHealthSensor(OptimizerBaseSensor):
    """Shows overall data source health: 'N/M healthy' or 'N/M degraded'."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "source_health", "Source Health")
        self._attr_icon = "mdi:heart-pulse"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        health = self.coordinator.data.get("source_health")
        if health is None:
            return None
        healthy = health.get("healthy", 0)
        total = health.get("total", 0)
        status = health.get("status", "unknown")
        return f"{healthy}/{total} {status}"

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        health = self.coordinator.data.get("source_health")
        if health is None:
            return {}
        return health.get("sources", {})


# ── Counterfactual Digital Twin Savings Sensors ────────────────────


class RuntimeSavingsTodaySensor(_DailyResetMixin, OptimizerBaseSensor):
    """Energy saved today from running HVAC fewer total minutes (kWh)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "runtime_savings_today", "Runtime Savings Today")
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:timer-minus-outline"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("runtime_savings_kwh_today")


class CopSavingsTodaySensor(_DailyResetMixin, OptimizerBaseSensor):
    """Energy saved today from better compressor efficiency (kWh)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "cop_savings_today", "COP Savings Today")
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:speedometer"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("cop_savings_kwh_today")


class RateSavingsTodaySensor(_DailyResetMixin, OptimizerBaseSensor):
    """Cost saved today from running at cheaper electricity rates ($)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "rate_savings_today", "Rate Arbitrage Savings Today")
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_native_unit_of_measurement = "$"
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:cash-clock"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("rate_arbitrage_savings_today")


class CarbonShiftSavingsTodaySensor(_DailyResetMixin, OptimizerBaseSensor):
    """CO2 avoided today from running during cleaner grid hours (grams)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "carbon_shift_savings_today", "Carbon Shift Savings Today")
        self._attr_native_unit_of_measurement = "g"
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 0
        self._attr_icon = "mdi:leaf"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("carbon_shift_savings_today")


class BaselineAvgCopSensor(OptimizerBaseSensor):
    """Average COP the old routine would have achieved today."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "baseline_avg_cop", "Baseline Average COP")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:gauge-low"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("baseline_avg_cop")


class OptimizedAvgCopSensor(OptimizerBaseSensor):
    """Average COP the optimizer actually achieved today."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "optimized_avg_cop", "Optimized Average COP")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:gauge"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("optimized_avg_cop")


class CopImprovementPctSensor(OptimizerBaseSensor):
    """COP improvement percentage (optimized vs baseline)."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "cop_improvement_pct", "COP Improvement")
        self._attr_native_unit_of_measurement = "%"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 0
        self._attr_icon = "mdi:chart-line"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("cop_improvement_pct")


class ComfortHoursGainedSensor(_DailyResetMixin, OptimizerBaseSensor):
    """Hours where optimizer maintained comfort but baseline would have drifted."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "comfort_hours_gained", "Comfort Hours Gained")
        self._attr_native_unit_of_measurement = "h"
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:home-thermometer"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("comfort_hours_gained")


class BaselineComfortViolationsSensor(_DailyResetMixin, OptimizerBaseSensor):
    """Times the virtual house exceeded comfort bounds today."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "baseline_comfort_violations", "Baseline Comfort Violations")
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 0
        self._attr_icon = "mdi:thermometer-alert"

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("baseline_comfort_violations")


class BaselineAvgIndoorTempSensor(OptimizerBaseSensor):
    """Average indoor temp of the virtual house today."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "baseline_avg_indoor_temp", "Baseline Avg Indoor Temp")
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:home-thermometer-outline"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("baseline_avg_indoor_temp")


class BaselineConfidenceSensor(OptimizerBaseSensor):
    """How well we know the user's routine (0-100%)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "baseline_confidence", "Baseline Confidence")
        self._attr_native_unit_of_measurement = "%"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 0
        self._attr_icon = "mdi:shield-check"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("baseline_confidence")

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "sample_days": self.coordinator.data.get("baseline_sample_days"),
            "capture_method": self.coordinator.data.get("baseline_capture_method"),
            "days_remaining": self.coordinator.data.get("baseline_days_remaining"),
        }


class SavingsAccuracyTierSensor(OptimizerBaseSensor):
    """Current savings accuracy tier (learning/estimated/simulated/calibrated)."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "savings_accuracy_tier", "Savings Accuracy")
        self._attr_icon = "mdi:signal-cellular-3"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("savings_accuracy_tier")


# ── Performance Profiler sensors ─────────────────────────────────────


class ProfilerConfidenceSensor(OptimizerBaseSensor):
    """Overall profiler confidence (0-100%)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "profiler_confidence", "Profiler Confidence")
        self._attr_native_unit_of_measurement = "%"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 0
        self._attr_icon = "mdi:chart-bell-curve-cumulative"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("profiler_confidence")


class ProfilerActiveSensor(OptimizerBaseSensor):
    """Whether the profiler has replaced the default performance model."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "profiler_active", "Profiler Active")
        self._attr_icon = "mdi:chart-areaspline"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        active = self.coordinator.data.get("profiler_active", False)
        return "active" if active else "learning"


class ProfilerObservationsSensor(OptimizerBaseSensor):
    """Total number of observations accumulated by the profiler."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "profiler_observations", "Profiler Observations")
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_icon = "mdi:counter"

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("profiler_observations")


class LearningProgressSensor(OptimizerBaseSensor):
    """Human-readable learning status so users know what to expect."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "learning_progress", "Learning Progress")
        self._attr_icon = "mdi:progress-wrench"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None

        learning = self.coordinator.data.get("learning_active", True)
        tier = self.coordinator.data.get("savings_accuracy_tier", "learning")
        days = self.coordinator.data.get("baseline_sample_days", 0)
        confidence = self.coordinator.data.get("kalman_confidence")
        conf_pct = round(confidence * 100) if confidence is not None else 0

        if not learning and tier == "calibrated":
            return "Fully calibrated"
        if not learning:
            return f"Model ready ({conf_pct}% confidence)"
        if days < 7:
            return f"Day {days} of ~14: Capturing baseline"
        if days < 14:
            return f"Day {days}: Baseline ready, model learning ({conf_pct}%)"
        return f"Day {days}: Still learning ({conf_pct}% confidence)"

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "sample_days": self.coordinator.data.get("baseline_sample_days"),
            "model_confidence": self.coordinator.data.get("kalman_confidence"),
            "accuracy_tier": self.coordinator.data.get("savings_accuracy_tier"),
            "initialization_mode": self.coordinator.data.get("initialization_mode"),
        }
