"""Tests for Layer 1: StrategicPlanner -- schedule optimization lifecycle."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.conftest import load_module, CC

# Load dependencies
data_types = load_module(
    "custom_components.heatpump_optimizer.engine.data_types",
    os.path.join(CC, "engine", "data_types.py"),
)
ForecastPoint = data_types.ForecastPoint
OccupancyForecastPoint = data_types.OccupancyForecastPoint
OptimizedSchedule = data_types.OptimizedSchedule
ScheduleEntry = data_types.ScheduleEntry
SimulationPoint = data_types.SimulationPoint

# Load comfort first (needed by strategic)
comfort_mod = load_module(
    "custom_components.heatpump_optimizer.engine.comfort",
    os.path.join(CC, "engine", "comfort.py"),
)

# Load occupancy adapter (needed by strategic)
occupancy_mod = load_module(
    "custom_components.heatpump_optimizer.adapters.occupancy",
    os.path.join(CC, "adapters", "occupancy.py"),
)
OccupancyMode = occupancy_mod.OccupancyMode

# Stub the optimizer module to avoid circular engine/__init__.py import
import types
optimizer_stub = types.ModuleType("custom_components.heatpump_optimizer.engine.optimizer")

class _ScheduleOptimizer:
    def optimize_setpoints(self, *args, **kwargs):
        pass

optimizer_stub.ScheduleOptimizer = _ScheduleOptimizer
sys.modules["custom_components.heatpump_optimizer.engine.optimizer"] = optimizer_stub
ScheduleOptimizer = _ScheduleOptimizer

strategic_mod = load_module(
    "custom_components.heatpump_optimizer.controllers.strategic",
    os.path.join(CC, "controllers", "strategic.py"),
)
StrategicPlanner = strategic_mod.StrategicPlanner
SHOULDER_MARGIN_F = strategic_mod.SHOULDER_MARGIN_F
MODE_SWITCH_HYSTERESIS_F = strategic_mod.MODE_SWITCH_HYSTERESIS_F

# ── Helpers ─────────────────────────────────────────────────────────

NOW = datetime(2026, 4, 7, 12, 0, 0, tzinfo=timezone.utc)
BALANCE_POINT = 50.0
COMFORT_COOL = (72.0, 78.0)
COMFORT_HEAT = (62.0, 70.0)


def make_forecast(temps, start=None):
    """Create ForecastPoints from a list of outdoor temps (one per hour)."""
    base = start or NOW
    return [
        ForecastPoint(
            time=base + timedelta(hours=i),
            outdoor_temp=t,
            carbon_intensity=None,
            electricity_rate=None,
        )
        for i, t in enumerate(temps)
    ]


def make_mock_optimizer(schedule=None):
    """Create a mock ScheduleOptimizer that returns a canned schedule."""
    mock = MagicMock(spec=ScheduleOptimizer)
    if schedule is None:
        schedule = OptimizedSchedule(
            entries=[ScheduleEntry(
                start_time=NOW, end_time=NOW + timedelta(hours=1),
                target_temp=74.0, mode="cool",
            )],
            baseline_runtime_minutes=60.0,
            optimized_runtime_minutes=40.0,
            savings_pct=33.3,
        )
    mock.optimize_setpoints.return_value = schedule
    return mock


def make_planner(optimizer=None, balance_point=BALANCE_POINT):
    return StrategicPlanner(
        optimizer=optimizer or make_mock_optimizer(),
        resist_balance_point=balance_point,
    )


def make_occupancy_timeline(segments):
    """Create OccupancyForecastPoints from (offset_hours, duration_hours, mode) tuples."""
    return [
        OccupancyForecastPoint(
            start_time=NOW + timedelta(hours=s),
            end_time=NOW + timedelta(hours=s + d),
            mode=m,
            source="test",
        )
        for s, d, m in segments
    ]


# ── should_reoptimize ──────────────────────────────────────────────


class TestShouldReoptimize:
    def test_never_optimized(self):
        sp = make_planner()
        assert sp.should_reoptimize() is True

    def test_force_flag(self):
        sp = make_planner()
        sp._last_optimization_time = datetime.now(timezone.utc)
        assert sp.should_reoptimize(force=True) is True

    def test_time_elapsed(self):
        sp = make_planner()
        sp._last_optimization_time = datetime.now(timezone.utc) - timedelta(hours=5)
        assert sp.should_reoptimize() is True

    def test_not_yet_time(self):
        sp = make_planner()
        sp._last_optimization_time = datetime.now(timezone.utc) - timedelta(hours=1)
        assert sp.should_reoptimize() is False

    def test_forecast_deviation(self):
        sp = make_planner()
        sp._last_optimization_time = datetime.now(timezone.utc) - timedelta(minutes=30)
        now = datetime.now(timezone.utc)
        old_forecast = [
            ForecastPoint(time=now + timedelta(hours=i), outdoor_temp=80.0,
                         carbon_intensity=None, electricity_rate=None)
            for i in range(6)
        ]
        new_forecast = [
            ForecastPoint(time=now + timedelta(hours=i), outdoor_temp=90.0,
                         carbon_intensity=None, electricity_rate=None)
            for i in range(6)
        ]
        sp._last_forecast_snapshot = old_forecast
        assert sp.should_reoptimize(new_forecast=new_forecast, forecast_threshold_f=5.0) is True

    def test_forecast_stable(self):
        sp = make_planner()
        sp._last_optimization_time = datetime.now(timezone.utc) - timedelta(minutes=30)
        now = datetime.now(timezone.utc)
        forecast = [
            ForecastPoint(time=now + timedelta(hours=i), outdoor_temp=80.0,
                         carbon_intensity=None, electricity_rate=None)
            for i in range(6)
        ]
        sp._last_forecast_snapshot = forecast
        # Same forecast -- no deviation
        assert sp.should_reoptimize(new_forecast=forecast, forecast_threshold_f=5.0) is False

    def test_occupancy_timeline_changed(self):
        sp = make_planner()
        sp._last_optimization_time = datetime.now(timezone.utc) - timedelta(minutes=30)
        sp._last_occupancy_hash = "old_hash"
        timeline = make_occupancy_timeline([(0, 8, "home")])
        assert sp.should_reoptimize(occupancy_timeline=timeline) is True

    def test_occupancy_timeline_same(self):
        sp = make_planner()
        sp._last_optimization_time = datetime.now(timezone.utc) - timedelta(minutes=30)
        timeline = make_occupancy_timeline([(0, 8, "home")])
        sp._last_occupancy_hash = sp._hash_occupancy_timeline(timeline)
        assert sp.should_reoptimize(occupancy_timeline=timeline) is False


# ── detect_mode ────────────────────────────────────────────────────


class TestDetectMode:
    def test_clearly_cooling(self):
        """Average well above balance + hysteresis."""
        sp = make_planner(balance_point=50.0)
        forecast = make_forecast([85, 88, 90, 87, 85, 83, 82, 80])
        assert sp.detect_mode(forecast) == "cool"

    def test_clearly_heating(self):
        """Average well below balance - hysteresis."""
        sp = make_planner(balance_point=50.0)
        forecast = make_forecast([30, 32, 35, 33, 31, 30, 28, 29])
        assert sp.detect_mode(forecast) == "heat"

    def test_near_balance_high_peak(self):
        """Avg near balance but peak > bp+10 => cool."""
        sp = make_planner(balance_point=50.0)
        forecast = make_forecast([48, 50, 52, 55, 62, 58, 55, 50])
        mode = sp.detect_mode(forecast)
        assert mode == "cool"

    def test_near_balance_low_trough(self):
        """Avg near balance but trough < bp-10 => heat."""
        sp = make_planner(balance_point=50.0)
        forecast = make_forecast([52, 50, 48, 45, 38, 42, 45, 48])
        mode = sp.detect_mode(forecast)
        assert mode == "heat"

    def test_truly_mild(self):
        """All temps near balance point: off."""
        sp = make_planner(balance_point=50.0)
        forecast = make_forecast([49, 50, 51, 50, 49, 50, 51, 50])
        assert sp.detect_mode(forecast) == "off"

    def test_empty_forecast(self):
        sp = make_planner()
        assert sp.detect_mode([]) == "off"

    def test_balance_point_sanity_clamp(self):
        """Out-of-range balance point falls back to 50."""
        sp = make_planner(balance_point=200.0)
        forecast = make_forecast([80, 85, 90, 88])
        mode = sp.detect_mode(forecast)
        assert mode == "cool"

    def test_near_term_only(self):
        """Only first 8 hours used for mode detection."""
        sp = make_planner(balance_point=50.0)
        # First 8 hours cold, hours 9-24 hot
        temps = [30] * 8 + [90] * 16
        forecast = make_forecast(temps)
        # Near-term is cold => heat
        assert sp.detect_mode(forecast) == "heat"


# ── optimize ───────────────────────────────────────────────────────


class TestOptimize:
    def test_empty_forecast_returns_none(self):
        sp = make_planner()
        result = sp.optimize(72.0, [], COMFORT_COOL, COMFORT_HEAT)
        assert result is None

    def test_successful_cooling(self):
        mock_opt = make_mock_optimizer()
        sp = make_planner(optimizer=mock_opt, balance_point=50.0)
        forecast = make_forecast([85, 88, 90, 87, 85, 83, 82, 80])
        result = sp.optimize(72.0, forecast, COMFORT_COOL, COMFORT_HEAT)
        assert result is not None
        assert sp.mode == "cool"
        assert sp.schedule is result
        mock_opt.optimize_setpoints.assert_called_once()

    def test_successful_heating(self):
        mock_opt = make_mock_optimizer()
        sp = make_planner(optimizer=mock_opt, balance_point=50.0)
        forecast = make_forecast([30, 32, 35, 33, 31, 30, 28, 29])
        result = sp.optimize(68.0, forecast, COMFORT_COOL, COMFORT_HEAT)
        assert result is not None
        assert sp.mode == "heat"

    def test_off_mode_returns_none(self):
        """When detect_mode returns off, optimize returns None."""
        sp = make_planner(balance_point=50.0)
        forecast = make_forecast([49, 50, 51, 50, 49, 50, 51, 50])
        result = sp.optimize(70.0, forecast, COMFORT_COOL, COMFORT_HEAT)
        assert result is None
        assert sp.mode is None

    def test_optimizer_exception_returns_none(self):
        mock_opt = make_mock_optimizer()
        mock_opt.optimize_setpoints.side_effect = ValueError("test error")
        sp = make_planner(optimizer=mock_opt, balance_point=50.0)
        forecast = make_forecast([85, 88, 90, 87, 85, 83, 82, 80])
        result = sp.optimize(72.0, forecast, COMFORT_COOL, COMFORT_HEAT)
        assert result is None

    def test_stores_forecast_snapshot(self):
        sp = make_planner(balance_point=50.0)
        forecast = make_forecast([85, 88, 90, 87, 85, 83, 82, 80])
        sp.optimize(72.0, forecast, COMFORT_COOL, COMFORT_HEAT)
        assert len(sp.forecast_snapshot) == len(forecast)

    def test_last_optimization_time_set(self):
        sp = make_planner(balance_point=50.0)
        assert sp.last_optimization_time is None
        forecast = make_forecast([85, 88, 90, 87, 85, 83, 82, 80])
        sp.optimize(72.0, forecast, COMFORT_COOL, COMFORT_HEAT)
        assert sp.last_optimization_time is not None


# ── Shoulder day ───────────────────────────────────────────────────


class TestShoulderDay:
    def test_shoulder_day_detected(self):
        """Cold morning + hot afternoon: shoulder day."""
        sp = make_planner(balance_point=50.0)
        # Min 35 < comfort_heat[0] - SHOULDER_MARGIN = 60
        # Max 82 > comfort_cool[1] + SHOULDER_MARGIN = 80
        forecast = make_forecast([35, 40, 50, 60, 70, 80, 82, 78])
        assert sp._is_shoulder_day(forecast, COMFORT_COOL, COMFORT_HEAT) is True

    def test_not_shoulder_single_mode(self):
        """All hot: not a shoulder day."""
        sp = make_planner(balance_point=50.0)
        forecast = make_forecast([80, 82, 85, 88, 90, 88, 85, 82])
        assert sp._is_shoulder_day(forecast, COMFORT_COOL, COMFORT_HEAT) is False

    def test_balance_point_crossing(self):
        """Cold morning + warm afternoon crossing balance point."""
        sp = make_planner(balance_point=50.0)
        # min 40 < bp-5=45, max 60 > bp+5=55
        forecast = make_forecast([40, 42, 45, 50, 55, 58, 60, 58])
        assert sp._is_shoulder_day(forecast, COMFORT_COOL, COMFORT_HEAT) is True

    def test_empty_forecast(self):
        sp = make_planner()
        assert sp._is_shoulder_day([], COMFORT_COOL, COMFORT_HEAT) is False

    def test_shoulder_narrows_comfort(self):
        """On shoulder days, comfort range is narrowed."""
        mock_opt = make_mock_optimizer()
        sp = make_planner(optimizer=mock_opt, balance_point=50.0)
        forecast = make_forecast([35, 40, 50, 60, 70, 80, 82, 78])
        sp.optimize(72.0, forecast, COMFORT_COOL, COMFORT_HEAT)
        # Optimizer should be called with narrowed comfort
        call_args = mock_opt.optimize_setpoints.call_args
        comfort_used = call_args[0][2]
        # Narrowed from (72,78) to midpoint +/- 1.5 = (73.5, 76.5)
        assert comfort_used[1] - comfort_used[0] == pytest.approx(3.0)


# ── Humidity correction ────────────────────────────────────────────


class TestHumidityCorrection:
    def test_cooling_high_humidity_lowers_max(self):
        """High humidity makes it feel warmer, so lower comfort max."""
        result = StrategicPlanner._apply_humidity_correction(
            (72.0, 78.0), 80.0, "cool"
        )
        assert result[1] < 78.0
        assert result[0] == 72.0  # min unchanged

    def test_cooling_low_humidity_no_change(self):
        """Low humidity in cooling: no adjustment."""
        result = StrategicPlanner._apply_humidity_correction(
            (72.0, 78.0), 30.0, "cool"
        )
        assert result == (72.0, 78.0)

    def test_heating_dry_air_lifts_range(self):
        """Low humidity in heating: lift comfort range."""
        result = StrategicPlanner._apply_humidity_correction(
            (62.0, 70.0), 20.0, "heat"
        )
        assert result[0] > 62.0
        assert result[1] > 70.0

    def test_heating_normal_humidity_no_change(self):
        """Normal humidity in heating: no change."""
        result = StrategicPlanner._apply_humidity_correction(
            (62.0, 70.0), 50.0, "heat"
        )
        assert result == (62.0, 70.0)

    def test_cooling_max_doesnt_go_below_min_plus_2(self):
        """Humidity correction won't squeeze range below 2F."""
        result = StrategicPlanner._apply_humidity_correction(
            (76.0, 78.0), 95.0, "cool"
        )
        assert result[1] >= result[0] + 2.0


# ── Sleep window ───────────────────────────────────────────────────


class TestSleepWindow:
    """Sleep window checks use local time, so we use the system's local tz."""

    def _make_local_time(self, hour, minute=0):
        """Create a datetime with hour/minute in local time."""
        # Build a naive datetime then localize to system tz
        from datetime import timezone as _tz
        local_tz = datetime.now().astimezone().tzinfo
        return datetime(2026, 4, 7, hour, minute, tzinfo=local_tz)

    def test_overnight_window_in_range(self):
        """22:00-07:00, local time 23:00 -> True."""
        cfg = {"start": "22:00", "end": "07:00"}
        assert StrategicPlanner._is_in_sleep_window(self._make_local_time(23), cfg) is True

    def test_overnight_window_out_of_range(self):
        """22:00-07:00, local time 08:00 -> False."""
        cfg = {"start": "22:00", "end": "07:00"}
        assert StrategicPlanner._is_in_sleep_window(self._make_local_time(8), cfg) is False

    def test_same_day_window_in_range(self):
        """01:00-06:00, local time 03:00 -> True."""
        cfg = {"start": "01:00", "end": "06:00"}
        assert StrategicPlanner._is_in_sleep_window(self._make_local_time(3), cfg) is True

    def test_same_day_window_out_of_range(self):
        """01:00-06:00, local time 07:00 -> False."""
        cfg = {"start": "01:00", "end": "06:00"}
        assert StrategicPlanner._is_in_sleep_window(self._make_local_time(7), cfg) is False


# ── Occupancy lookup ───────────────────────────────────────────────


class TestOccupancyLookup:
    def test_covered_by_timeline(self):
        timeline = make_occupancy_timeline([(0, 4, "home"), (4, 4, "away")])
        mode = StrategicPlanner._lookup_occupancy_at(NOW + timedelta(hours=5), timeline)
        assert mode == OccupancyMode.AWAY

    def test_not_covered_defaults_home(self):
        timeline = make_occupancy_timeline([(0, 2, "away")])
        mode = StrategicPlanner._lookup_occupancy_at(NOW + timedelta(hours=5), timeline)
        assert mode == OccupancyMode.HOME

    def test_vacation_mode(self):
        timeline = make_occupancy_timeline([(0, 24, "vacation")])
        mode = StrategicPlanner._lookup_occupancy_at(NOW + timedelta(hours=12), timeline)
        assert mode == OccupancyMode.VACATION

    def test_home_mode(self):
        timeline = make_occupancy_timeline([(0, 24, "home")])
        mode = StrategicPlanner._lookup_occupancy_at(NOW + timedelta(hours=6), timeline)
        assert mode == OccupancyMode.HOME


# ── Occupancy hash ─────────────────────────────────────────────────


class TestOccupancyHash:
    def test_same_timeline_same_hash(self):
        t1 = make_occupancy_timeline([(0, 8, "home")])
        t2 = make_occupancy_timeline([(0, 8, "home")])
        assert StrategicPlanner._hash_occupancy_timeline(t1) == \
               StrategicPlanner._hash_occupancy_timeline(t2)

    def test_different_timeline_different_hash(self):
        t1 = make_occupancy_timeline([(0, 8, "home")])
        t2 = make_occupancy_timeline([(0, 4, "home"), (4, 4, "away")])
        assert StrategicPlanner._hash_occupancy_timeline(t1) != \
               StrategicPlanner._hash_occupancy_timeline(t2)


# ── Properties ─────────────────────────────────────────────────────


class TestProperties:
    def test_schedule_none_initially(self):
        sp = make_planner()
        assert sp.schedule is None

    def test_mode_none_initially(self):
        sp = make_planner()
        assert sp.mode is None

    def test_last_optimization_time_none_initially(self):
        sp = make_planner()
        assert sp.last_optimization_time is None

    def test_forecast_snapshot_empty_initially(self):
        sp = make_planner()
        assert sp.forecast_snapshot == []
