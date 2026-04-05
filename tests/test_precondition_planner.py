"""Tests for PreconditionPlanner — candidate evaluation, edge cases, cost optimization."""

import importlib
import importlib.util
import math
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# ── Module loading ────────────────────────────────────────────────────

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, PROJECT_ROOT)

CC = os.path.join(PROJECT_ROOT, "custom_components", "heatpump_optimizer")

pkg = types.ModuleType("custom_components")
pkg.__path__ = [os.path.join(PROJECT_ROOT, "custom_components")]
sys.modules.setdefault("custom_components", pkg)

ho = types.ModuleType("custom_components.heatpump_optimizer")
ho.__path__ = [CC]
sys.modules.setdefault("custom_components.heatpump_optimizer", ho)

engine = types.ModuleType("custom_components.heatpump_optimizer.engine")
engine.__path__ = [os.path.join(CC, "engine")]
sys.modules.setdefault("custom_components.heatpump_optimizer.engine", engine)

# Stub homeassistant
ha_mod = types.ModuleType("homeassistant")
ha_mod.__path__ = ["homeassistant"]
sys.modules.setdefault("homeassistant", ha_mod)
ha_core = types.ModuleType("homeassistant.core")
ha_core.HomeAssistant = MagicMock
sys.modules.setdefault("homeassistant.core", ha_core)


def _load(full_name: str, path: str):
    spec = importlib.util.spec_from_file_location(full_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


dt_mod = _load(
    "custom_components.heatpump_optimizer.engine.data_types",
    os.path.join(CC, "engine", "data_types.py"),
)
engine.data_types = dt_mod

perf_mod = _load(
    "custom_components.heatpump_optimizer.engine.performance_model",
    os.path.join(CC, "engine", "performance_model.py"),
)
engine.performance_model = perf_mod

planner_mod = _load(
    "custom_components.heatpump_optimizer.engine.precondition_planner",
    os.path.join(CC, "engine", "precondition_planner.py"),
)
engine.precondition_planner = planner_mod

ForecastPoint = dt_mod.ForecastPoint
PreconditionPlan = dt_mod.PreconditionPlan
PreconditionPlanner = planner_mod.PreconditionPlanner

# ── Helpers ───────────────────────────────────────────────────────────

NOW = datetime(2026, 3, 12, 10, 0, 0, tzinfo=timezone.utc)


class MockPerformanceModel:
    """Simple mock for PerformanceModel with controllable behavior."""

    def __init__(self, drift_per_hour=0.5, runtime_per_degree=30.0):
        self._drift_per_hour = drift_per_hour  # °F/hr passive warming
        self._runtime_per_degree = runtime_per_degree  # minutes per °F

    def passive_drift(self, outdoor_temp: float, indoor_temp: float | None = None) -> float:
        return self._drift_per_hour

    def runtime_needed(self, outdoor_temp: float, mode: str, gap: float) -> float:
        if gap <= 0:
            return 0.0
        return gap * self._runtime_per_degree


def _make_forecast(start: datetime, hours: int, outdoor_temp: float = 85.0, rate=None):
    """Generate hourly forecast points."""
    return [
        ForecastPoint(
            time=start + timedelta(hours=h),
            outdoor_temp=outdoor_temp,
            electricity_rate=rate,
        )
        for h in range(hours)
    ]


def _make_planner(drift=0.5, runtime_per_deg=30.0):
    model = MockPerformanceModel(drift_per_hour=drift, runtime_per_degree=runtime_per_deg)
    return PreconditionPlanner(model)


# ── Tests: Target Temperature ─────────────────────────────────────────


class TestTargetTemp:
    def test_cool_target_is_max(self):
        assert PreconditionPlanner._target_temp("cool", (70.0, 78.0)) == 78.0

    def test_heat_target_is_min(self):
        assert PreconditionPlanner._target_temp("heat", (64.0, 70.0)) == 64.0


# ── Tests: Temperature Gap ────────────────────────────────────────────


class TestTemperatureGap:
    def test_cool_gap_above_target(self):
        # Current 82, target 78 → 4°F gap
        assert PreconditionPlanner._temperature_gap(82.0, 78.0, "cool") == 4.0

    def test_cool_no_gap(self):
        # Current 76, target 78 → no gap
        assert PreconditionPlanner._temperature_gap(76.0, 78.0, "cool") == 0.0

    def test_heat_gap_below_target(self):
        # Current 58, target 64 → 6°F gap
        assert PreconditionPlanner._temperature_gap(58.0, 64.0, "heat") == 6.0

    def test_heat_no_gap(self):
        # Current 66, target 64 → no gap
        assert PreconditionPlanner._temperature_gap(66.0, 64.0, "heat") == 0.0


# ── Tests: Plan Method ────────────────────────────────────────────────


class TestPlan:
    def test_returns_none_when_already_comfortable(self):
        """If indoor is already within HOME comfort, returns None."""
        planner = _make_planner()
        forecast = _make_forecast(NOW, 12)
        arrival = NOW + timedelta(hours=8)

        with patch.object(planner_mod, "datetime") as mock_dt:
            mock_dt.now.return_value = NOW
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            result = planner.plan(
                arrival_time=arrival,
                current_indoor_temp=77.0,  # within (70, 78) for cool
                forecast=forecast,
                mode="cool",
                home_comfort=(70.0, 78.0),
                away_comfort=(66.0, 82.0),
            )
        assert result is None

    def test_creates_plan_for_cooling(self):
        """When house is warm, should create a plan to pre-cool."""
        planner = _make_planner(drift=0.3, runtime_per_deg=20.0)
        forecast = _make_forecast(NOW, 12, outdoor_temp=90.0)
        arrival = NOW + timedelta(hours=8)

        with patch.object(planner_mod, "datetime") as mock_dt:
            mock_dt.now.return_value = NOW
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            result = planner.plan(
                arrival_time=arrival,
                current_indoor_temp=82.0,
                forecast=forecast,
                mode="cool",
                home_comfort=(70.0, 78.0),
                away_comfort=(66.0, 82.0),
            )

        assert result is not None
        assert isinstance(result, PreconditionPlan)
        assert result.arrival_time == arrival
        assert result.temperature_gap > 0
        assert result.estimated_runtime_minutes > 0
        assert result.estimated_energy_kwh > 0

    def test_creates_plan_for_heating(self):
        """When house is cold, should create a plan to pre-heat."""
        planner = _make_planner(drift=-0.3, runtime_per_deg=25.0)
        forecast = _make_forecast(NOW, 12, outdoor_temp=30.0)
        arrival = NOW + timedelta(hours=8)

        with patch.object(planner_mod, "datetime") as mock_dt:
            mock_dt.now.return_value = NOW
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            result = planner.plan(
                arrival_time=arrival,
                current_indoor_temp=58.0,
                forecast=forecast,
                mode="heat",
                home_comfort=(64.0, 70.0),
                away_comfort=(60.0, 74.0),
            )

        assert result is not None
        assert result.temperature_gap > 0
        assert result.estimated_runtime_minutes > 0

    def test_should_start_now_when_deadline_passed(self):
        """When arrival is imminent, should_start_now = True."""
        planner = _make_planner(drift=0.2, runtime_per_deg=30.0)
        forecast = _make_forecast(NOW, 4, outdoor_temp=90.0)
        arrival = NOW + timedelta(minutes=10)  # almost here

        with patch.object(planner_mod, "datetime") as mock_dt:
            mock_dt.now.return_value = NOW
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            result = planner.plan(
                arrival_time=arrival,
                current_indoor_temp=82.0,
                forecast=forecast,
                mode="cool",
                home_comfort=(70.0, 78.0),
                away_comfort=(66.0, 82.0),
            )

        assert result is not None
        assert result.should_start_now is True

    def test_arrival_source_propagated(self):
        planner = _make_planner(drift=0.2, runtime_per_deg=20.0)
        forecast = _make_forecast(NOW, 12, outdoor_temp=90.0)
        arrival = NOW + timedelta(hours=6)

        with patch.object(planner_mod, "datetime") as mock_dt:
            mock_dt.now.return_value = NOW
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            result = planner.plan(
                arrival_time=arrival,
                current_indoor_temp=83.0,
                forecast=forecast,
                mode="cool",
                home_comfort=(70.0, 78.0),
                away_comfort=(66.0, 82.0),
                arrival_source="travel_sensor",
            )

        assert result is not None
        assert result.arrival_source == "travel_sensor"

    def test_cost_uses_electricity_rate(self):
        """When forecast has electricity rates, cost should be computed."""
        planner = _make_planner(drift=0.2, runtime_per_deg=20.0)
        forecast = _make_forecast(NOW, 12, outdoor_temp=90.0, rate=0.15)
        arrival = NOW + timedelta(hours=8)

        with patch.object(planner_mod, "datetime") as mock_dt:
            mock_dt.now.return_value = NOW
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            result = planner.plan(
                arrival_time=arrival,
                current_indoor_temp=82.0,
                forecast=forecast,
                mode="cool",
                home_comfort=(70.0, 78.0),
                away_comfort=(66.0, 82.0),
            )

        assert result is not None
        assert result.estimated_cost is not None
        assert result.estimated_cost > 0

    def test_no_rate_cost_is_none(self):
        """When no electricity rate, estimated_cost should be None."""
        planner = _make_planner(drift=0.2, runtime_per_deg=20.0)
        forecast = _make_forecast(NOW, 12, outdoor_temp=90.0)
        arrival = NOW + timedelta(hours=8)

        with patch.object(planner_mod, "datetime") as mock_dt:
            mock_dt.now.return_value = NOW
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            result = planner.plan(
                arrival_time=arrival,
                current_indoor_temp=82.0,
                forecast=forecast,
                mode="cool",
                home_comfort=(70.0, 78.0),
                away_comfort=(66.0, 82.0),
            )

        assert result is not None
        assert result.estimated_cost is None


# ── Tests: Drift Simulation ──────────────────────────────────────────


class TestDriftSimulation:
    def test_drift_trajectory_length(self):
        planner = _make_planner(drift=0.5)
        end = NOW + timedelta(hours=4)
        lookup = {int(NOW.timestamp()) // 3600: 85.0}
        trajectory = planner._simulate_drift(75.0, NOW, end, lookup)
        # 4 hours / 15-min intervals = 16 steps + 1 initial
        assert len(trajectory) >= 16

    def test_drift_increases_temp(self):
        """Positive drift should increase temperature over time."""
        planner = _make_planner(drift=0.5)
        end = NOW + timedelta(hours=2)
        lookup = {int((NOW + timedelta(hours=h)).timestamp()) // 3600: 85.0 for h in range(3)}
        trajectory = planner._simulate_drift(75.0, NOW, end, lookup)
        # Temperature should be higher at end
        assert trajectory[-1][1] > trajectory[0][1]


# ── Tests: Lookup Drift ──────────────────────────────────────────────


class TestLookupDrift:
    def test_exact_time(self):
        trajectory = [(NOW, 75.0), (NOW + timedelta(minutes=15), 75.2)]
        assert PreconditionPlanner._lookup_drift(trajectory, NOW) == 75.0

    def test_between_points(self):
        trajectory = [(NOW, 75.0), (NOW + timedelta(minutes=30), 76.0)]
        result = PreconditionPlanner._lookup_drift(trajectory, NOW + timedelta(minutes=15))
        assert result == 75.0  # returns closest at or before

    def test_empty_trajectory(self):
        assert PreconditionPlanner._lookup_drift([], NOW) == 75.0


# ── Tests: Build Temp Lookup ─────────────────────────────────────────


class TestBuildTempLookup:
    def test_maps_forecast_to_hours(self):
        forecast = _make_forecast(NOW, 3, outdoor_temp=85.0)
        lookup = PreconditionPlanner._build_temp_lookup(forecast)
        assert len(lookup) == 3
        for v in lookup.values():
            assert v == 85.0


# ── Tests: Rate At ───────────────────────────────────────────────────


class TestRateAt:
    def test_with_rates(self):
        forecast = _make_forecast(NOW, 4, rate=0.12)
        rate = PreconditionPlanner._rate_at(NOW, 60, forecast)
        assert rate == pytest.approx(0.12)

    def test_no_rates(self):
        forecast = _make_forecast(NOW, 4)
        rate = PreconditionPlanner._rate_at(NOW, 60, forecast)
        assert rate is None

    def test_empty_forecast(self):
        rate = PreconditionPlanner._rate_at(NOW, 60, [])
        assert rate is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
