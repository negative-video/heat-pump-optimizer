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

from .const import DOMAIN, VERSION
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
        # Diagnostic sensors
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
        ProfilerStatusSensor(coordinator, entry),
        LearningProgressSensor(coordinator, entry),
        # Auxiliary appliance sensors
        ApplianceThermalLoadSensor(coordinator, entry),
        ActiveAppliancesSensor(coordinator, entry),
        # Aux/emergency heat sensors
        AuxHeatThresholdSensor(coordinator, entry),
        AuxHeatKwhTodaySensor(coordinator, entry),
        AvoidedAuxHeatKwhSensor(coordinator, entry),
        # Thermostat blend mitigation diagnostics
        CrossSensorSpreadSensor(coordinator, entry),
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
            manufacturer="Gerald Burkett",
            model="Heat Pump Optimizer",
            sw_version=VERSION,
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

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        attrs = {}
        tc = self.coordinator.data.get("tactical_correction")
        if tc is not None:
            attrs["tactical_correction"] = tc
        ts = self.coordinator.data.get("tactical_state")
        if ts is not None:
            attrs["tactical_state"] = ts
        fd = self.coordinator.data.get("forecast_deviation")
        if fd is not None:
            attrs["forecast_deviation"] = fd
        return attrs


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

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        attrs = {}
        bias = self.coordinator.data.get("model_bias")
        if bias is not None:
            attrs["model_bias"] = bias
        age = self.coordinator.data.get("forecast_age_minutes")
        if age is not None:
            attrs["forecast_age_minutes"] = age
        solar = self.coordinator.data.get("solar_coefficient")
        if solar is not None:
            attrs["solar_coefficient"] = solar
        return attrs


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


class SavingsPercentSensor(_LearningNullMixin, OptimizerBaseSensor):
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
        return self._suppress_during_learning(
            self.coordinator.data.get("savings_pct")
        )


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

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "using_adaptive_model": self.coordinator.data.get("using_adaptive_model", False),
            "using_greybox_model": self.coordinator.data.get("using_greybox_model", False),
        }


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


# ── Diagnostic sensors ──────────────────────────────────────────────


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
        # Cap schedule/forecast entries to stay under HA's 16 KB attribute limit.
        # Full data remains available via coordinator.data for the sidebar panel.
        detail = self.coordinator.data.get("schedule_detail")
        if detail:
            attrs["entries"] = detail[:24]
        forecast = self.coordinator.data.get("forecast_detail")
        if forecast:
            attrs["forecast"] = forecast[:24]
        # Always include weather forecast — panel needs it for the weather chart
        weather = self.coordinator.data.get("weather_forecast")
        if weather:
            attrs["weather_forecast"] = weather[:24]
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
        attrs = {
            "source": info.get("source", "unknown"),
            "stale": info.get("stale", False),
            "entity_count": info.get("entity_count", 0),
            "entities": info.get("entities", []),
        }
        # Environment context for panel
        rate = self.coordinator.data.get("electricity_rate")
        if rate is not None:
            attrs["electricity_rate"] = round(rate, 4)
        co2 = self.coordinator.data.get("co2_intensity")
        if co2 is not None:
            attrs["co2_intensity"] = round(co2, 0)
        wind = self.coordinator.data.get("wind_speed_mph")
        if wind is not None:
            attrs["wind_speed_mph"] = round(wind, 1)
        solar = self.coordinator.data.get("solar_irradiance")
        if solar is not None:
            attrs["solar_irradiance"] = round(solar, 0)
        health = self.coordinator.data.get("source_health")
        if health is not None:
            attrs["source_health_status"] = health.get("status", "unknown")
            attrs["source_health_healthy"] = health.get("healthy", 0)
            attrs["source_health_total"] = health.get("total", 0)
            attrs["source_health_sources"] = health.get("sources", {})
        return attrs


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


class SavingsKwhTodaySensor(_LearningNullMixin, _DailyResetMixin, OptimizerBaseSensor):
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
        return self._suppress_during_learning(
            self.coordinator.data.get("savings_kwh_today")
        )

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "runtime_component": self.coordinator.data.get("runtime_savings_kwh_today"),
            "cop_component": self.coordinator.data.get("cop_savings_kwh_today"),
            "source": self.coordinator.data.get("savings_accuracy_tier"),
        }


class SavingsKwhCumulativeSensor(_LearningNullMixin, OptimizerBaseSensor):
    """All-time cumulative energy saved (kWh)."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "savings_kwh_cumulative", "Energy Saved Total")
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        # Use TOTAL (not TOTAL_INCREASING) because savings can go negative
        # during learning or shoulder seasons. TOTAL_INCREASING would cause
        # HA statistics to flag negative deltas as meter rollbacks.
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:lightning-bolt"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self._suppress_during_learning(
            self.coordinator.data.get("savings_kwh_cumulative")
        )


class SavingsCostTodaySensor(_LearningNullMixin, _DailyResetMixin, OptimizerBaseSensor):
    """Money saved today vs baseline ($)."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "savings_cost_today", "Cost Saved Today")
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_native_unit_of_measurement = coordinator.hass.config.currency or "USD"
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:currency-usd"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self._suppress_during_learning(
            self.coordinator.data.get("savings_cost_today")
        )

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "runtime_component": self.coordinator.data.get("runtime_cost_savings_today"),
            "rate_arbitrage_component": self.coordinator.data.get("rate_arbitrage_savings_today"),
            "cop_component": self.coordinator.data.get("cop_cost_savings_today"),
        }


class SavingsCostCumulativeSensor(_LearningNullMixin, OptimizerBaseSensor):
    """All-time cumulative money saved ($)."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "savings_cost_cumulative", "Cost Saved Total")
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_native_unit_of_measurement = coordinator.hass.config.currency or "USD"
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:currency-usd"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self._suppress_during_learning(
            self.coordinator.data.get("savings_cost_cumulative")
        )


class SavingsCO2TodaySensor(_LearningNullMixin, _DailyResetMixin, OptimizerBaseSensor):
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
        return self._suppress_during_learning(
            self.coordinator.data.get("savings_co2_today_grams")
        )

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "runtime_component": self.coordinator.data.get("runtime_co2_savings_today"),
            "carbon_shift_component": self.coordinator.data.get("carbon_shift_savings_today"),
        }


class SavingsCO2CumulativeSensor(_LearningNullMixin, OptimizerBaseSensor):
    """All-time cumulative CO2 avoided (kg)."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "savings_co2_cumulative", "CO2 Avoided Total")
        self._attr_native_unit_of_measurement = "kg"
        # Use TOTAL (not TOTAL_INCREASING) — savings can go negative.
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:molecule-co2"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        grams = self.coordinator.data.get("savings_co2_cumulative_grams")
        if grams is None:
            return None
        return self._suppress_during_learning(grams / 1000.0)


class BaselineKwhTodaySensor(_DailyResetMixin, OptimizerBaseSensor):
    """Estimated baseline energy usage today without optimizer (kWh)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

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
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        transition = self.coordinator.data.get("next_occupancy_transition")
        attrs = {
            "source": self.coordinator.data.get("occupancy_forecast_source", "reactive"),
            "timeline_segments": self.coordinator.data.get("occupancy_timeline_segments", 0),
            "next_transition": transition.get("time") if transition else None,
            "next_transition_type": transition.get("type") if transition else None,
        }
        # Expose occupancy timeline segments for panel rendering
        timeline = self.coordinator.occupancy_timeline
        if timeline:
            attrs["timeline"] = [
                {
                    "start": seg.start_time.isoformat(),
                    "end": seg.end_time.isoformat(),
                    "mode": seg.mode,
                }
                for seg in timeline[:24]
            ]
        return attrs


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
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
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
        # Use the effective indoor temp (multi-sensor averaged) from coordinator
        weighted = self.coordinator.data.get("weighted_indoor_temp")
        if weighted is not None:
            return round(weighted, 1)
        return self.coordinator.data.get("current_indoor_temp")

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


# ── Counterfactual Digital Twin Savings Sensors ────────────────────


class RuntimeSavingsTodaySensor(_LearningNullMixin, _DailyResetMixin, OptimizerBaseSensor):
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
        return self._suppress_during_learning(
            self.coordinator.data.get("runtime_savings_kwh_today")
        )


class CopSavingsTodaySensor(_LearningNullMixin, _DailyResetMixin, OptimizerBaseSensor):
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
        return self._suppress_during_learning(
            self.coordinator.data.get("cop_savings_kwh_today")
        )


class RateSavingsTodaySensor(_LearningNullMixin, _DailyResetMixin, OptimizerBaseSensor):
    """Cost saved today from running at cheaper electricity rates ($)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "rate_savings_today", "Rate Arbitrage Savings Today")
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_native_unit_of_measurement = coordinator.hass.config.currency or "USD"
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:cash-clock"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self._suppress_during_learning(
            self.coordinator.data.get("rate_arbitrage_savings_today")
        )


class CarbonShiftSavingsTodaySensor(_LearningNullMixin, _DailyResetMixin, OptimizerBaseSensor):
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
        return self._suppress_during_learning(
            self.coordinator.data.get("carbon_shift_savings_today")
        )


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


class ComfortHoursGainedSensor(_LearningNullMixin, _DailyResetMixin, OptimizerBaseSensor):
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
        return self._suppress_during_learning(
            self.coordinator.data.get("comfort_hours_gained")
        )


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


class ProfilerStatusSensor(OptimizerBaseSensor):
    """Profiler status with confidence, active state, and observation count as attributes."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "profiler_status", "Profiler")
        self._attr_icon = "mdi:list-status"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("profiler_status")

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        attrs = {}
        conf = self.coordinator.data.get("profiler_confidence")
        if conf is not None:
            attrs["confidence"] = conf
        attrs["active"] = self.coordinator.data.get("profiler_active", False)
        obs = self.coordinator.data.get("profiler_observations")
        if obs is not None:
            attrs["observations"] = obs
        # Override intelligence for panel diagnostics
        attrs["override_count_30d"] = self.coordinator.data.get(
            "override_count_30d", 0
        )
        override_pattern = self.coordinator.data.get("override_pattern")
        if override_pattern:
            attrs["override_pattern"] = override_pattern
        return attrs


class LearningProgressSensor(OptimizerBaseSensor):
    """Human-readable learning status so users know what to expect."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "learning_progress", "Learning Progress")
        self._attr_icon = "mdi:progress-wrench"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None

        baseline_only = self.coordinator.data.get("baseline_only_mode", True)
        tier = self.coordinator.data.get("savings_accuracy_tier", "learning")
        days = self.coordinator.data.get("baseline_sample_days", 0)
        baseline_ready = self.coordinator.data.get("baseline_ready", False)
        confidence = self.coordinator.data.get("kalman_confidence")
        conf_pct = round(confidence * 100) if confidence is not None else 0

        if not baseline_only and tier == "calibrated":
            return "Fully calibrated"
        if not baseline_only:
            return f"Optimizer active ({conf_pct}% confidence)"
        if not baseline_ready:
            return f"Day {days} of 7: Observing your schedule"
        return f"Baseline captured — model training ({conf_pct}%)"

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        baseline_only = self.coordinator.data.get("baseline_only_mode", True)
        attrs = {
            "sample_days": self.coordinator.data.get("baseline_sample_days"),
            "model_confidence": self.coordinator.data.get("kalman_confidence"),
            "accuracy_tier": self.coordinator.data.get("savings_accuracy_tier"),
            "initialization_mode": self.coordinator.data.get("initialization_mode"),
            "history_bootstrap_completed": self.coordinator.data.get(
                "history_bootstrap_completed"
            ),
            "history_bootstrap_result": self.coordinator.data.get(
                "history_bootstrap_result"
            ),
        }
        if baseline_only:
            attrs["comfort_band_note"] = (
                "The optimizer is passively observing your schedule to establish "
                "a baseline. It will not change your thermostat until baseline "
                "capture is complete (7 days) and model confidence is sufficient."
            )
        return attrs


class ApplianceThermalLoadSensor(OptimizerBaseSensor):
    """Net thermal load from auxiliary appliances in BTU/hr."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry, "appliance_thermal_load", "Appliance Thermal Load"
        )
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "BTU/hr"
        self._attr_icon = "mdi:heat-wave"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("appliance_thermal_load_btu")


class ActiveAppliancesSensor(OptimizerBaseSensor):
    """Names of currently active auxiliary appliances."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry, "active_appliances", "Active Appliances"
        )
        self._attr_icon = "mdi:washing-machine"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        diag = self.coordinator.data.get("appliance_diagnostics")
        if not diag:
            return None
        active = [a["name"] for a in diag.get("appliances", []) if a.get("active")]
        return ", ".join(active) if active else "None"

    @property
    def extra_state_attributes(self) -> dict | None:
        if self.coordinator.data is None:
            return None
        diag = self.coordinator.data.get("appliance_diagnostics")
        if not diag:
            return None
        return {
            "configured_count": diag.get("configured_count", 0),
            "active_count": diag.get("active_count", 0),
            "total_thermal_impact_btu": diag.get("total_thermal_impact_btu", 0),
            "appliances": diag.get("appliances", []),
        }


# ── Aux/emergency heat sensors ──────────────────────────────────────


class AuxHeatThresholdSensor(OptimizerBaseSensor):
    """Learned effective outdoor temp below which aux/emergency heat is likely to activate."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry, "aux_heat_threshold", "Aux Heat Threshold"
        )
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:thermometer-alert"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("aux_heat_threshold_f")

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "is_learned": self.coordinator.data.get("aux_heat_threshold_learned", False),
            "event_count": self.coordinator.data.get("aux_heat_event_count", 0),
            "learned_hp_watts": self.coordinator.data.get("aux_heat_learned_hp_watts"),
        }


class AuxHeatKwhTodaySensor(_DailyResetMixin, OptimizerBaseSensor):
    """Incremental resistive kWh consumed today above heat pump baseline draw."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry, "aux_heat_kwh_today", "Aux Heat kWh Today"
        )
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:lightning-bolt"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get("aux_heat_kwh_today", 0.0)
        return val if val > 0 else None


class AvoidedAuxHeatKwhSensor(_DailyResetMixin, OptimizerBaseSensor):
    """Estimated kWh saved today by pre-heating and avoiding aux heat activation."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry, "avoided_aux_heat_kwh_today", "Avoided Aux Heat kWh Today"
        )
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:shield-check-outline"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get("avoided_aux_heat_kwh_today", 0.0)
        return val if val > 0 else None


class CrossSensorSpreadSensor(OptimizerBaseSensor):
    """Temperature spread between the thermostat and additional indoor sensors.

    Reports the absolute difference between the primary thermostat reading and
    the average of configured indoor temperature entities. A persistently large
    spread (>2-3°F) at night when only one area is occupied is a sign that the
    thermostat is blending toward a remote satellite sensor. Useful for tuning
    the blend mitigation threshold in Multi-Sensor Median mode and for monitoring
    whether the Occupancy-Based or Schedule modes are activating correctly.

    This sensor is always present regardless of the selected blend mitigation mode.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "cross_sensor_spread", "Cross-Sensor Temp Spread")
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1
        self._attr_icon = "mdi:thermometer-lines"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        spread = self.coordinator.data.get("cross_sensor_spread_f", 0.0)
        # Only expose when indoor entities are configured (spread is meaningful)
        return round(spread, 1) if spread is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "blend_mode": self.coordinator.data.get("thermostat_blend_mode"),
            "blend_active": self.coordinator.data.get("thermostat_blend_suspected", False),
        }
