"""Tests for strategic controller with occupancy timeline integration.

Covers per-hour comfort stamping, timeline change detection, and
occupancy-aware re-optimization triggers.
"""

import importlib
import importlib.util
import json
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

adapters_pkg = types.ModuleType("custom_components.heatpump_optimizer.adapters")
adapters_pkg.__path__ = [os.path.join(CC, "adapters")]
sys.modules.setdefault("custom_components.heatpump_optimizer.adapters", adapters_pkg)

engine_pkg = types.ModuleType("custom_components.heatpump_optimizer.engine")
engine_pkg.__path__ = [os.path.join(CC, "engine")]
sys.modules.setdefault("custom_components.heatpump_optimizer.engine", engine_pkg)

controllers_pkg = types.ModuleType("custom_components.heatpump_optimizer.controllers")
controllers_pkg.__path__ = [os.path.join(CC, "controllers")]
sys.modules.setdefault("custom_components.heatpump_optimizer.controllers", controllers_pkg)

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
engine_pkg.data_types = dt_mod

# Load performance_model (needed by optimizer)
perf_mod = _load(
    "custom_components.heatpump_optimizer.engine.performance_model",
    os.path.join(CC, "engine", "performance_model.py"),
)
engine_pkg.performance_model = perf_mod

# Load optimizer module
opt_mod = _load(
    "custom_components.heatpump_optimizer.engine.optimizer",
    os.path.join(CC, "engine", "optimizer.py"),
)
engine_pkg.optimizer = opt_mod

# Load occupancy adapter
occ_mod = _load(
    "custom_components.heatpump_optimizer.adapters.occupancy",
    os.path.join(CC, "adapters", "occupancy.py"),
)
adapters_pkg.occupancy = occ_mod

# Load strategic controller
strat_mod = _load(
    "custom_components.heatpump_optimizer.controllers.strategic",
    os.path.join(CC, "controllers", "strategic.py"),
)
controllers_pkg.strategic = strat_mod

ForecastPoint = dt_mod.ForecastPoint
OccupancyForecastPoint = dt_mod.OccupancyForecastPoint
OptimizedSchedule = dt_mod.OptimizedSchedule
StrategicPlanner = strat_mod.StrategicPlanner
OccupancyMode = occ_mod.OccupancyMode

# ── Helpers ───────────────────────────────────────────────────────────

NOW = datetime(2026, 3, 12, 10, 0, 0, tzinfo=timezone.utc)


def _make_forecast(hours=24, base_temp=90.0):
    """Generate forecast points spanning multiple hours."""
    return [
        ForecastPoint(
            time=NOW + timedelta(hours=h),
            outdoor_temp=base_temp,
        )
        for h in range(hours)
    ]


def _make_timeline():
    """Create an away→home occupancy timeline: away 10:00-17:00, home 17:00-24:00."""
    return [
        OccupancyForecastPoint(
            start_time=NOW,
            end_time=NOW + timedelta(hours=7),
            mode="away",
            source="calendar",
        ),
        OccupancyForecastPoint(
            start_time=NOW + timedelta(hours=7),
            end_time=NOW + timedelta(hours=14),
            mode="home",
            source="calendar",
        ),
    ]


def _mock_optimizer():
    """Create a mock ScheduleOptimizer that returns a valid schedule."""
    optimizer = MagicMock()
    optimizer.optimize_setpoints.return_value = OptimizedSchedule(
        entries=[],
        baseline_runtime_minutes=120.0,
        optimized_runtime_minutes=80.0,
        savings_pct=33.3,
        comfort_violations=0,
    )
    return optimizer


# ── Tests: Per-Hour Comfort Stamping ──────────────────────────────────


class TestStampPerHourComfort:
    """Test that _stamp_per_hour_comfort sets correct comfort on ForecastPoints."""

    def test_away_hours_get_wider_comfort(self):
        optimizer = _mock_optimizer()
        planner = StrategicPlanner(optimizer=optimizer, resist_balance_point=50.2)

        forecast = _make_forecast(hours=14)
        timeline = _make_timeline()
        base_comfort = (70.0, 78.0)

        planner._stamp_per_hour_comfort(forecast, base_comfort, "cool", timeline)

        # First 7 hours are during "away" → wider comfort
        for i in range(7):
            assert forecast[i].comfort_min is not None
            assert forecast[i].comfort_max is not None
            # Away widens by AWAY_COMFORT_DELTA (4.0) in each direction
            assert forecast[i].comfort_min == pytest.approx(66.0)
            assert forecast[i].comfort_max == pytest.approx(82.0)

        # Hours 7-13 are "home" → base comfort
        for i in range(7, 14):
            assert forecast[i].comfort_min == pytest.approx(70.0)
            assert forecast[i].comfort_max == pytest.approx(78.0)

    def test_heat_mode_away(self):
        optimizer = _mock_optimizer()
        planner = StrategicPlanner(optimizer=optimizer, resist_balance_point=50.2)

        forecast = _make_forecast(hours=10, base_temp=30.0)
        timeline = [
            OccupancyForecastPoint(NOW, NOW + timedelta(hours=10), "away", "calendar"),
        ]
        base_comfort = (64.0, 70.0)

        planner._stamp_per_hour_comfort(forecast, base_comfort, "heat", timeline)

        for pt in forecast:
            assert pt.comfort_min == pytest.approx(60.0)  # 64 - 4
            assert pt.comfort_max == pytest.approx(74.0)  # 70 + 4

    def test_vacation_mode_sets_fixed_setpoints(self):
        optimizer = _mock_optimizer()
        planner = StrategicPlanner(optimizer=optimizer, resist_balance_point=50.2)

        forecast = _make_forecast(hours=5)
        timeline = [
            OccupancyForecastPoint(NOW, NOW + timedelta(hours=5), "vacation", "calendar"),
        ]
        base_comfort = (70.0, 78.0)

        planner._stamp_per_hour_comfort(forecast, base_comfort, "cool", timeline)

        for pt in forecast:
            assert pt.comfort_min == pytest.approx(70.0)
            assert pt.comfort_max == pytest.approx(82.0)  # VACATION_COOL_SETPOINT

    def test_no_timeline_coverage_defaults_to_home(self):
        """ForecastPoints outside the timeline default to HOME comfort."""
        optimizer = _mock_optimizer()
        planner = StrategicPlanner(optimizer=optimizer, resist_balance_point=50.2)

        forecast = _make_forecast(hours=24)
        # Timeline only covers first 14 hours
        timeline = _make_timeline()
        base_comfort = (70.0, 78.0)

        planner._stamp_per_hour_comfort(forecast, base_comfort, "cool", timeline)

        # Hours beyond timeline default to HOME
        for i in range(14, 24):
            assert forecast[i].comfort_min == pytest.approx(70.0)
            assert forecast[i].comfort_max == pytest.approx(78.0)


# ── Tests: Occupancy Timeline Hash ────────────────────────────────────


class TestOccupancyHash:
    def test_same_timeline_same_hash(self):
        tl1 = _make_timeline()
        tl2 = _make_timeline()
        h1 = StrategicPlanner._hash_occupancy_timeline(tl1)
        h2 = StrategicPlanner._hash_occupancy_timeline(tl2)
        assert h1 == h2

    def test_different_timeline_different_hash(self):
        tl1 = _make_timeline()
        tl2 = [
            OccupancyForecastPoint(NOW, NOW + timedelta(hours=24), "home", "calendar"),
        ]
        h1 = StrategicPlanner._hash_occupancy_timeline(tl1)
        h2 = StrategicPlanner._hash_occupancy_timeline(tl2)
        assert h1 != h2

    def test_empty_timeline(self):
        assert StrategicPlanner._hash_occupancy_timeline([]) == ""


# ── Tests: Should Reoptimize with Timeline ────────────────────────────


class TestShouldReoptimizeWithTimeline:
    def test_timeline_change_triggers_reoptimize(self):
        optimizer = _mock_optimizer()
        planner = StrategicPlanner(optimizer=optimizer, resist_balance_point=50.2)
        planner._last_optimization_time = NOW - timedelta(minutes=30)
        planner._last_occupancy_hash = "old_hash"

        timeline = _make_timeline()
        result = planner.should_reoptimize(occupancy_timeline=timeline)
        assert result is True

    def test_same_timeline_no_reoptimize(self):
        optimizer = _mock_optimizer()
        planner = StrategicPlanner(optimizer=optimizer, resist_balance_point=50.2)
        # Use real current time so elapsed check doesn't trigger
        real_now = datetime.now(timezone.utc)
        planner._last_optimization_time = real_now - timedelta(minutes=30)

        timeline = _make_timeline()
        planner._last_occupancy_hash = StrategicPlanner._hash_occupancy_timeline(timeline)

        result = planner.should_reoptimize(occupancy_timeline=timeline)
        assert result is False

    def test_none_timeline_no_trigger(self):
        optimizer = _mock_optimizer()
        planner = StrategicPlanner(optimizer=optimizer, resist_balance_point=50.2)
        real_now = datetime.now(timezone.utc)
        planner._last_optimization_time = real_now - timedelta(minutes=30)
        planner._last_occupancy_hash = "something"

        result = planner.should_reoptimize(occupancy_timeline=None)
        assert result is False


# ── Tests: Optimize with Timeline ─────────────────────────────────────


class TestOptimizeWithTimeline:
    def test_timeline_passed_stamps_forecast(self):
        """When timeline is provided, forecast points get per-hour comfort."""
        optimizer = _mock_optimizer()
        planner = StrategicPlanner(optimizer=optimizer, resist_balance_point=50.2)

        forecast = _make_forecast(hours=14)
        timeline = _make_timeline()

        schedule = planner.optimize(
            indoor_temp=76.0,
            forecast=forecast,
            comfort_cool=(70.0, 78.0),
            comfort_heat=(64.0, 70.0),
            occupancy_timeline=timeline,
        )

        assert schedule is not None
        # First point should have away comfort (during away hours)
        assert forecast[0].comfort_min is not None
        # Occupancy hash should be stored
        assert planner._last_occupancy_hash != ""

    def test_no_timeline_no_stamping(self):
        """Without timeline, forecast points remain unstamped."""
        optimizer = _mock_optimizer()
        planner = StrategicPlanner(optimizer=optimizer, resist_balance_point=50.2)

        forecast = _make_forecast(hours=14)

        schedule = planner.optimize(
            indoor_temp=76.0,
            forecast=forecast,
            comfort_cool=(70.0, 78.0),
            comfort_heat=(64.0, 70.0),
        )

        assert schedule is not None
        # No stamping without timeline
        assert forecast[0].comfort_min is None
        assert forecast[0].comfort_max is None


# ── Tests: Lookup Occupancy At ────────────────────────────────────────


class TestLookupOccupancyAt:
    def test_during_away(self):
        timeline = _make_timeline()
        mode = StrategicPlanner._lookup_occupancy_at(NOW + timedelta(hours=3), timeline)
        assert mode == OccupancyMode.AWAY

    def test_during_home(self):
        timeline = _make_timeline()
        mode = StrategicPlanner._lookup_occupancy_at(NOW + timedelta(hours=8), timeline)
        assert mode == OccupancyMode.HOME

    def test_outside_timeline_defaults_home(self):
        timeline = _make_timeline()
        mode = StrategicPlanner._lookup_occupancy_at(NOW + timedelta(hours=20), timeline)
        assert mode == OccupancyMode.HOME

    def test_boundary_start(self):
        """At the exact start of an away segment → AWAY."""
        timeline = _make_timeline()
        mode = StrategicPlanner._lookup_occupancy_at(NOW, timeline)
        assert mode == OccupancyMode.AWAY

    def test_boundary_transition(self):
        """At the exact transition point (end of away = start of home) → HOME."""
        timeline = _make_timeline()
        transition = NOW + timedelta(hours=7)
        mode = StrategicPlanner._lookup_occupancy_at(transition, timeline)
        assert mode == OccupancyMode.HOME


# ── Tests: Extended Occupancy Adapter Methods ─────────────────────────


class TestAdjustComfortForMode:
    """Test the static parameterized comfort adjustment."""

    def test_home_unchanged(self):
        result = occ_mod.OccupancyAdapter.adjust_comfort_for_mode(
            (70.0, 78.0), "cool", OccupancyMode.HOME
        )
        assert result == (70.0, 78.0)

    def test_away_widens(self):
        result = occ_mod.OccupancyAdapter.adjust_comfort_for_mode(
            (70.0, 78.0), "cool", OccupancyMode.AWAY
        )
        assert result == (66.0, 82.0)

    def test_vacation_cool(self):
        result = occ_mod.OccupancyAdapter.adjust_comfort_for_mode(
            (70.0, 78.0), "cool", OccupancyMode.VACATION
        )
        assert result == (70.0, 82.0)  # VACATION_COOL_SETPOINT

    def test_vacation_heat(self):
        result = occ_mod.OccupancyAdapter.adjust_comfort_for_mode(
            (64.0, 70.0), "heat", OccupancyMode.VACATION
        )
        assert result == (55.0, 70.0)  # VACATION_HEAT_SETPOINT


class TestGetEffectiveMode:
    """Test calendar-aware effective mode priority."""

    def test_forced_mode_highest_priority(self):
        hass = MagicMock()
        hass.states.get.return_value = None
        adapter = occ_mod.OccupancyAdapter(hass, entity_ids=[])
        adapter.force_mode(OccupancyMode.VACATION)

        timeline = [
            OccupancyForecastPoint(NOW, NOW + timedelta(hours=8), "home", "calendar"),
        ]
        result = adapter.get_effective_mode(calendar_timeline=timeline)
        assert result == OccupancyMode.VACATION

    def test_reactive_home_overrides_calendar_away(self):
        """Person came home early — reactive HOME trumps calendar AWAY."""
        hass = MagicMock()
        state = MagicMock()
        state.state = "home"
        hass.states.get.return_value = state

        adapter = occ_mod.OccupancyAdapter(hass, entity_ids=["person.alice"])
        timeline = [
            OccupancyForecastPoint(NOW, NOW + timedelta(hours=8), "away", "calendar"),
        ]
        result = adapter.get_effective_mode(calendar_timeline=timeline)
        assert result == OccupancyMode.HOME

    def test_no_timeline_falls_back_to_reactive(self):
        hass = MagicMock()
        state = MagicMock()
        state.state = "away"
        hass.states.get.return_value = state

        adapter = occ_mod.OccupancyAdapter(hass, entity_ids=["person.alice"])
        result = adapter.get_effective_mode(calendar_timeline=None)
        assert result == OccupancyMode.AWAY


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
