"""Tests for profiler-based counterfactual simulator.

Covers:
- Dual-setpoint heat_cool thermostat logic
- Profiler model-driven temperature evolution
- Solar-condition-aware passive drift in counterfactual
- Constructed baseline from comfort config (no 7-day capture needed)
- Rated capacity for COP/power calculations
- Serialization round-trip
- Savings decomposition
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from conftest import CC, load_module

dt_mod = load_module(
    "custom_components.heatpump_optimizer.engine.data_types",
    os.path.join(CC, "engine", "data_types.py"),
)
perf_mod = load_module(
    "custom_components.heatpump_optimizer.engine.performance_model",
    os.path.join(CC, "engine", "performance_model.py"),
)
cf_mod = load_module(
    "custom_components.heatpump_optimizer.engine.counterfactual_simulator",
    os.path.join(CC, "engine", "counterfactual_simulator.py"),
)

PerformanceModel = perf_mod.PerformanceModel
CounterfactualSimulator = cf_mod.CounterfactualSimulator
BaselineHourResult = dt_mod.BaselineHourResult

NOW = datetime(2026, 4, 7, 12, 0, 0, tzinfo=timezone.utc)


def _default_model():
    return PerformanceModel.from_defaults()


# ── Dual-setpoint thermostat ─────────────────────────────────────────


class TestDualSetpointThermostat:
    """Tests for the heat_cool dual-setpoint thermostat logic."""

    def test_heats_below_low_setpoint(self):
        sim = CounterfactualSimulator(initial_temp=60.0)
        mode, running = sim._dual_setpoint_decision(60.0, 65.0, 78.0)
        assert mode == "heat"
        assert running is True

    def test_cools_above_high_setpoint(self):
        sim = CounterfactualSimulator(initial_temp=80.0)
        mode, running = sim._dual_setpoint_decision(80.0, 65.0, 78.0)
        assert mode == "cool"
        assert running is True

    def test_idles_in_dead_band(self):
        sim = CounterfactualSimulator(initial_temp=72.0)
        mode, running = sim._dual_setpoint_decision(72.0, 65.0, 78.0)
        assert mode == "off"
        assert running is False

    def test_deadband_hysteresis(self):
        """HVAC doesn't turn on until past the deadband."""
        sim = CounterfactualSimulator(initial_temp=64.8, deadband=0.5)
        # 64.8 is above 65.0 - 0.5 = 64.5, so no heating
        mode, running = sim._dual_setpoint_decision(64.8, 65.0, 78.0)
        assert running is False

        # 64.4 is below 65.0 - 0.5 = 64.5, so heating kicks in
        mode, running = sim._dual_setpoint_decision(64.4, 65.0, 78.0)
        assert mode == "heat"
        assert running is True


# ── Profiler model physics ───────────────────────────────────────────


class TestCounterfactualPhysics:
    """Tests for profiler-model-driven temperature evolution."""

    def test_passive_cooling_when_cold_outside(self):
        """Virtual house should cool when outdoor temp is low and HVAC off."""
        sim = CounterfactualSimulator(initial_temp=72.0)
        model = _default_model()

        # Run 12 steps (1 hour) at 30F outdoor, no HVAC (setpoints far apart)
        for i in range(12):
            sim.step(
                now=NOW + timedelta(minutes=i * 5),
                outdoor_temp=30.0,
                setpoint_low=55.0,
                setpoint_high=85.0,
                model=model,
                dt_minutes=5.0,
            )

        # House should have cooled
        assert sim.virtual_indoor_temp < 72.0, (
            f"Virtual house should cool at 30F outdoor, got {sim.virtual_indoor_temp:.1f}"
        )

    def test_passive_warming_when_hot_outside(self):
        """Virtual house should warm when outdoor temp is high and HVAC off."""
        sim = CounterfactualSimulator(initial_temp=72.0)
        model = _default_model()

        for i in range(12):
            sim.step(
                now=NOW + timedelta(minutes=i * 5),
                outdoor_temp=95.0,
                setpoint_low=55.0,
                setpoint_high=85.0,
                model=model,
                dt_minutes=5.0,
            )

        assert sim.virtual_indoor_temp > 72.0

    def test_heating_activates_and_warms(self):
        """Virtual thermostat should activate heating when temp drops below setpoint_low."""
        sim = CounterfactualSimulator(initial_temp=62.0)
        model = _default_model()

        for i in range(24):  # 2 hours
            sim.step(
                now=NOW + timedelta(minutes=i * 5),
                outdoor_temp=30.0,
                setpoint_low=65.0,
                setpoint_high=78.0,
                model=model,
                dt_minutes=5.0,
            )

        # Heating should have warmed the house toward 65
        assert sim.virtual_indoor_temp > 62.0

    def test_cooling_activates_and_cools(self):
        """Virtual thermostat should activate cooling when temp rises above setpoint_high."""
        sim = CounterfactualSimulator(initial_temp=80.0)
        model = _default_model()

        for i in range(24):
            sim.step(
                now=NOW + timedelta(minutes=i * 5),
                outdoor_temp=95.0,
                setpoint_low=65.0,
                setpoint_high=78.0,
                model=model,
                dt_minutes=5.0,
            )

        # Cooling should have pulled temp down toward 78
        assert sim.virtual_indoor_temp < 80.0


# ── Solar condition awareness ────────────────────────────────────────


class TestCounterfactualSolar:
    """Tests for solar-condition-aware passive drift in counterfactual."""

    def test_classify_solar_night(self):
        result = CounterfactualSimulator._classify_solar(0.0, -5.0)
        assert result == "night"

    def test_classify_solar_sunny(self):
        result = CounterfactualSimulator._classify_solar(0.1, 40.0)
        assert result == "sunny"

    def test_classify_solar_cloudy(self):
        result = CounterfactualSimulator._classify_solar(0.8, 40.0)
        assert result == "cloudy"

    def test_solar_condition_passed_to_model(self):
        """When cloud cover is provided, passive drift uses solar condition."""
        model = MagicMock()
        model.passive_drift = MagicMock(return_value=0.5)
        model.cooling_delta = MagicMock(return_value=-1.0)
        model.heating_delta = MagicMock(return_value=1.0)

        sim = CounterfactualSimulator(initial_temp=72.0)
        sim.step(
            now=NOW,
            outdoor_temp=60.0,
            setpoint_low=55.0,
            setpoint_high=85.0,
            model=model,
            cloud_cover=0.1,
            sun_elevation=40.0,
        )

        # Should have called passive_drift with solar_condition="sunny"
        model.passive_drift.assert_called_once()
        call_kwargs = model.passive_drift.call_args
        assert call_kwargs.kwargs.get("solar_condition") == "sunny"


# ── Rated capacity and power ─────────────────────────────────────────


class TestRatedCapacity:
    """Tests for rated capacity affecting power/COP calculations."""

    def test_default_capacity(self):
        sim = CounterfactualSimulator()
        assert sim._rated_q_cool == 30000.0
        assert sim._rated_q_heat == 30000.0

    def test_set_rated_capacity(self):
        sim = CounterfactualSimulator()
        sim.set_rated_capacity(42000.0, 46200.0)
        assert sim._rated_q_cool == 42000.0
        assert sim._rated_q_heat == 46200.0

    def test_capacity_affects_power_calculation(self):
        sim = CounterfactualSimulator()
        small = sim._capacity_at_outdoor_temp(50.0, "heat")
        sim.set_rated_capacity(60000.0, 60000.0)
        large = sim._capacity_at_outdoor_temp(50.0, "heat")
        assert large > small


# ── Serialization ────────────────────────────────────────────────────


class TestCounterfactualSerialization:

    def test_round_trip(self):
        sim = CounterfactualSimulator(initial_temp=68.0, deadband=1.0)
        sim.set_rated_capacity(42000.0, 46200.0)
        sim._hours_since_reset = 42

        data = sim.to_dict()
        sim2 = CounterfactualSimulator.from_dict(data)

        assert abs(sim2._T_air - 68.0) < 0.01
        assert sim2._deadband == 1.0
        assert sim2._rated_q_cool == 42000.0
        assert sim2._rated_q_heat == 46200.0
        assert sim2._hours_since_reset == 42

    def test_legacy_data_migration(self):
        """Legacy data with T_mass should load without error."""
        data = {
            "T_air": 70.0,
            "T_mass": 69.0,  # legacy field -- should be ignored
            "deadband": 0.5,
            "hours_since_reset": 10,
        }
        sim = CounterfactualSimulator.from_dict(data)
        assert abs(sim._T_air - 70.0) < 0.01
        # virtual_mass_temp returns T_air in profiler-primary mode
        assert sim.virtual_mass_temp == sim.virtual_indoor_temp


# ── Savings decomposition ────────────────────────────────────────────


class TestSavingsDecomposition:

    def test_runtime_savings(self):
        sim = CounterfactualSimulator()
        baseline = BaselineHourResult(
            runtime_minutes=30.0,
            power_watts=2000.0,
            kwh=1.0,
        )
        result = sim.decompose_savings(
            baseline_result=baseline,
            actual_runtime_min=20.0,
            actual_power_watts=2000.0,
            actual_kwh=0.667,
            actual_cop=3.0,
            actual_rate=0.12,
            baseline_rate=0.12,
        )
        assert result["runtime_savings_kwh"] > 0
        assert result["cop_savings_kwh"] is not None

    def test_rate_arbitrage(self):
        sim = CounterfactualSimulator()
        baseline = BaselineHourResult(
            runtime_minutes=30.0,
            power_watts=2000.0,
            kwh=1.0,
        )
        result = sim.decompose_savings(
            baseline_result=baseline,
            actual_runtime_min=30.0,
            actual_power_watts=2000.0,
            actual_kwh=1.0,
            actual_cop=3.0,
            actual_rate=0.08,
            baseline_rate=0.15,
        )
        # Same runtime, same kWh, but cheaper rate
        assert result["rate_arbitrage_savings"] > 0


# ── Hour finalization ────────────────────────────────────────────────


class TestHourFinalization:
    """Tests that hour results are properly accumulated and finalized."""

    def test_hour_result_produced_on_boundary(self):
        sim = CounterfactualSimulator(initial_temp=60.0)
        model = _default_model()

        # Run through a full hour (12 steps of 5 min)
        for i in range(12):
            sim.step(
                now=NOW + timedelta(minutes=i * 5),
                outdoor_temp=30.0,
                setpoint_low=65.0,
                setpoint_high=78.0,
                model=model,
                dt_minutes=5.0,
            )

        # Cross into the next hour to finalize
        sim.step(
            now=NOW + timedelta(hours=1),
            outdoor_temp=30.0,
            setpoint_low=65.0,
            setpoint_high=78.0,
            model=model,
            dt_minutes=5.0,
        )

        result = sim.get_latest_hour_result()
        assert result is not None
        assert result.runtime_minutes >= 0
        assert result.kwh >= 0
