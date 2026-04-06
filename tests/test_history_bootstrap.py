"""Tests for the history bootstrap module."""

import importlib
import importlib.util
import os
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

# ── Module loading (same pattern as other tests) ─────────────────────

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

learning = types.ModuleType("custom_components.heatpump_optimizer.learning")
learning.__path__ = [os.path.join(CC, "learning")]
sys.modules.setdefault("custom_components.heatpump_optimizer.learning", learning)


def _load(full_name: str, path: str):
    spec = importlib.util.spec_from_file_location(full_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Mock HA dependencies before loading our modules
ha_const = types.ModuleType("homeassistant.const")
ha_const.UnitOfTemperature = type("UnitOfTemperature", (), {
    "CELSIUS": "°C",
    "FAHRENHEIT": "°F",
})
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
sys.modules["homeassistant"].__path__ = []  # type: ignore[attr-defined]
sys.modules.setdefault("homeassistant.const", ha_const)

ha_core = types.ModuleType("homeassistant.core")
ha_core.HomeAssistant = MagicMock  # type: ignore[attr-defined]
sys.modules.setdefault("homeassistant.core", ha_core)

ha_util = types.ModuleType("homeassistant.util")
ha_util.__path__ = []  # type: ignore[attr-defined]
sys.modules.setdefault("homeassistant.util", ha_util)

ha_unit_conversion = types.ModuleType("homeassistant.util.unit_conversion")


class _MockTempConverter:
    @staticmethod
    def convert(value, from_unit, to_unit):
        if from_unit == "°C" and to_unit == "°F":
            return value * 9 / 5 + 32
        return value


ha_unit_conversion.TemperatureConverter = _MockTempConverter  # type: ignore[attr-defined]
sys.modules.setdefault("homeassistant.util.unit_conversion", ha_unit_conversion)

# Load data_types (dependency of baseline_capture)
data_types_mod = _load(
    "custom_components.heatpump_optimizer.engine.data_types",
    os.path.join(CC, "engine", "data_types.py"),
)

# Load thermal_estimator
te_mod = _load(
    "custom_components.heatpump_optimizer.learning.thermal_estimator",
    os.path.join(CC, "learning", "thermal_estimator.py"),
)
learning.thermal_estimator = te_mod

# Load baseline_capture
bc_mod = _load(
    "custom_components.heatpump_optimizer.learning.baseline_capture",
    os.path.join(CC, "learning", "baseline_capture.py"),
)
learning.baseline_capture = bc_mod

# Load performance_profiler
pp_mod = _load(
    "custom_components.heatpump_optimizer.learning.performance_profiler",
    os.path.join(CC, "learning", "performance_profiler.py"),
)
learning.performance_profiler = pp_mod

# Load history_bootstrap
hb_mod = _load(
    "custom_components.heatpump_optimizer.learning.history_bootstrap",
    os.path.join(CC, "learning", "history_bootstrap.py"),
)
learning.history_bootstrap = hb_mod

ThermalEstimator = te_mod.ThermalEstimator
BaselineCapture = bc_mod.BaselineCapture
PerformanceProfiler = pp_mod.PerformanceProfiler
HistoryDataPoint = hb_mod.HistoryDataPoint
BootstrapResult = hb_mod.BootstrapResult
_batch_feed_estimator = hb_mod._batch_feed_estimator
_bootstrap_baseline = hb_mod._bootstrap_baseline
_bootstrap_profiler = hb_mod._bootstrap_profiler
_build_aligned_timeline = hb_mod._build_aligned_timeline
_interpolate_numeric = hb_mod._interpolate_numeric
_forward_fill_str = hb_mod._forward_fill_str
_forward_fill_numeric = hb_mod._forward_fill_numeric
GRID_INTERVAL_MINUTES = hb_mod.GRID_INTERVAL_MINUTES


# ── Mock HA state objects ────────────────────────────────────────────────


@dataclass
class MockState:
    """Mimics homeassistant.core.State for testing."""

    state: str
    attributes: dict[str, Any]
    last_changed: datetime


def _make_climate_states(
    start: datetime,
    hours: float = 24.0,
    interval_minutes: int = 5,
    indoor_temp_f: float = 72.0,
    setpoint_f: float = 74.0,
    hvac_mode: str = "cool",
    hvac_action: str = "idle",
) -> list[MockState]:
    """Generate a sequence of mock climate entity states."""
    states = []
    t = start
    end = start + timedelta(hours=hours)
    while t <= end:
        attrs = {
            "current_temperature": indoor_temp_f,
            "temperature": setpoint_f,
            "hvac_action": hvac_action,
        }
        states.append(MockState(state=hvac_mode, attributes=attrs, last_changed=t))
        t += timedelta(minutes=interval_minutes)
    return states


# ── Interpolation tests ─────────────────────────────────────────────────


class TestInterpolation:
    """Test numeric interpolation and forward-fill helpers."""

    def test_interpolate_exact_match(self):
        t0 = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        timeline = [(t0, 72.0)]
        assert _interpolate_numeric(timeline, t0, 15) == 72.0

    def test_interpolate_between_points(self):
        t0 = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(minutes=10)
        timeline = [(t0, 70.0), (t1, 80.0)]
        mid = t0 + timedelta(minutes=5)
        result = _interpolate_numeric(timeline, mid, 15)
        assert result == pytest.approx(75.0)

    def test_interpolate_empty_timeline(self):
        t = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        assert _interpolate_numeric([], t, 15) is None

    def test_interpolate_beyond_gap(self):
        t0 = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        timeline = [(t0, 72.0)]
        target = t0 + timedelta(minutes=20)
        assert _interpolate_numeric(timeline, target, 15) is None

    def test_interpolate_within_gap_from_end(self):
        t0 = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        timeline = [(t0, 72.0)]
        target = t0 + timedelta(minutes=10)
        assert _interpolate_numeric(timeline, target, 15) == 72.0

    def test_forward_fill_str(self):
        t0 = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        timeline = [
            (t0, "cool"),
            (t0 + timedelta(hours=2), "heat"),
        ]
        assert _forward_fill_str(timeline, t0 + timedelta(hours=1)) == "cool"
        assert _forward_fill_str(timeline, t0 + timedelta(hours=3)) == "heat"
        assert _forward_fill_str(timeline, t0 - timedelta(hours=1)) is None

    def test_forward_fill_numeric(self):
        t0 = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        timeline = [(t0, 74.0), (t0 + timedelta(hours=1), 72.0)]
        assert _forward_fill_numeric(timeline, t0 + timedelta(minutes=30)) == 74.0
        assert _forward_fill_numeric([], t0) is None


# ── Timeline building tests ──────────────────────────────────────────────


class TestBuildAlignedTimeline:
    """Test the timeline alignment and resampling."""

    def test_basic_alignment(self):
        start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)

        climate_states = _make_climate_states(
            start, hours=1, indoor_temp_f=72.0, hvac_action="cooling"
        )
        outdoor_states = [(start, 85.0), (end, 85.0)]

        points = _build_aligned_timeline(
            climate_states=climate_states,
            outdoor_states=outdoor_states,
            indoor_sensor_timelines=[],
            humidity_states=[],
            wind_states=[],
            start_time=start,
            end_time=end,
        )

        # 1 hour / 5 min = 12 intervals + 1 (inclusive) = 13 grid points
        assert len(points) == 13

        valid = [p for p in points if p.valid]
        assert len(valid) == 13

        assert points[0].indoor_temp == pytest.approx(72.0)
        assert points[0].outdoor_temp == pytest.approx(85.0)
        assert points[0].hvac_mode == "cool"
        assert points[0].hvac_running is True
        assert points[0].hvac_action == "cooling"

    def test_missing_outdoor_temp(self):
        start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)

        climate_states = _make_climate_states(start, hours=1)

        points = _build_aligned_timeline(
            climate_states=climate_states,
            outdoor_states=[],
            indoor_sensor_timelines=[],
            humidity_states=[],
            wind_states=[],
            start_time=start,
            end_time=end,
        )

        valid = [p for p in points if p.valid]
        assert len(valid) == 0

    def test_setpoint_captured(self):
        start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(minutes=10)

        climate_states = _make_climate_states(
            start, hours=0.17, setpoint_f=74.0
        )
        outdoor_states = [(start, 85.0), (end, 85.0)]

        points = _build_aligned_timeline(
            climate_states=climate_states,
            outdoor_states=outdoor_states,
            indoor_sensor_timelines=[],
            humidity_states=[],
            wind_states=[],
            start_time=start,
            end_time=end,
        )

        assert points[0].setpoint == pytest.approx(74.0)


# ── EKF batch feeding tests ─────────────────────────────────────────────


class TestBatchFeedEstimator:
    """Test batch-feeding historical data through the EKF."""

    def test_basic_batch_feed(self):
        estimator = ThermalEstimator.cold_start()
        initial_obs = estimator._n_obs

        start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
        points = []
        for i in range(288):
            t = start + timedelta(minutes=5 * i)
            points.append(
                HistoryDataPoint(
                    timestamp=t,
                    indoor_temp=72.0 + 0.1 * (i % 12),
                    outdoor_temp=85.0,
                    hvac_mode="cool",
                    hvac_running=i % 3 == 0,
                    hvac_action="cooling" if i % 3 == 0 else "idle",
                    setpoint=74.0,
                    humidity=50.0,
                    wind_speed_mph=5.0,
                )
            )

        valid, skipped = _batch_feed_estimator(estimator, points)

        assert valid == 288
        assert skipped == 0
        assert estimator._n_obs == initial_obs + 288

    def test_skips_invalid_points(self):
        estimator = ThermalEstimator.cold_start()

        start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
        points = [
            HistoryDataPoint(
                timestamp=start,
                indoor_temp=72.0,
                outdoor_temp=85.0,
                hvac_mode="cool",
                hvac_running=False,
                hvac_action="idle",
                setpoint=74.0,
                humidity=None,
                wind_speed_mph=None,
            ),
            HistoryDataPoint(
                timestamp=start + timedelta(minutes=5),
                indoor_temp=None,
                outdoor_temp=85.0,
                hvac_mode="cool",
                hvac_running=False,
                hvac_action="idle",
                setpoint=74.0,
                humidity=None,
                wind_speed_mph=None,
            ),
            HistoryDataPoint(
                timestamp=start + timedelta(minutes=10),
                indoor_temp=72.0,
                outdoor_temp=None,
                hvac_mode="cool",
                hvac_running=False,
                hvac_action="idle",
                setpoint=74.0,
                humidity=None,
                wind_speed_mph=None,
            ),
        ]

        valid, skipped = _batch_feed_estimator(estimator, points)
        assert valid == 1
        assert skipped == 2

    def test_convergence_improves_with_data(self):
        """EKF confidence should increase with more observations."""
        estimator = ThermalEstimator.cold_start()
        initial_confidence = estimator.confidence

        start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
        points = []
        for i in range(500):
            t = start + timedelta(minutes=5 * i)
            running = i % 4 < 2
            temp = 73.0 - (0.05 * (i % 20)) if running else 73.0 + (0.02 * (i % 20))
            points.append(
                HistoryDataPoint(
                    timestamp=t,
                    indoor_temp=temp,
                    outdoor_temp=90.0,
                    hvac_mode="cool",
                    hvac_running=running,
                    hvac_action="cooling" if running else "idle",
                    setpoint=74.0,
                    humidity=None,
                    wind_speed_mph=None,
                )
            )

        _batch_feed_estimator(estimator, points)

        assert estimator.confidence > initial_confidence


# ── Baseline bootstrap tests ────────────────────────────────────────────


class TestBootstrapBaseline:
    """Test baseline capture from historical data."""

    def test_baseline_populated(self):
        baseline = BaselineCapture()
        start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)  # Monday

        points = []
        for i in range(2880):
            t = start + timedelta(minutes=5 * i)
            points.append(
                HistoryDataPoint(
                    timestamp=t,
                    indoor_temp=72.0,
                    outdoor_temp=85.0,
                    hvac_mode="cool",
                    hvac_running=False,
                    hvac_action="idle",
                    setpoint=74.0,
                    humidity=None,
                    wind_speed_mph=None,
                )
            )

        count = _bootstrap_baseline(baseline, points)

        assert count == 2880
        assert baseline.sample_days >= 7
        assert baseline.is_ready

    def test_baseline_skips_missing_setpoint(self):
        baseline = BaselineCapture()
        start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)

        points = [
            HistoryDataPoint(
                timestamp=start,
                indoor_temp=72.0,
                outdoor_temp=85.0,
                hvac_mode="cool",
                hvac_running=False,
                hvac_action="idle",
                setpoint=None,
                humidity=None,
                wind_speed_mph=None,
            ),
        ]

        count = _bootstrap_baseline(baseline, points)
        assert count == 0


# ── Profiler bootstrap tests ────────────────────────────────────────────


class TestBootstrapProfiler:
    """Test performance profiler from historical data."""

    def test_profiler_populated(self):
        profiler = PerformanceProfiler()
        start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)

        points = []
        for i in range(24):
            t = start + timedelta(minutes=5 * i)
            points.append(
                HistoryDataPoint(
                    timestamp=t,
                    indoor_temp=72.0 - 0.05 * i,
                    outdoor_temp=90.0,
                    hvac_mode="cool",
                    hvac_running=True,
                    hvac_action="cooling",
                    setpoint=74.0,
                    humidity=None,
                    wind_speed_mph=None,
                )
            )

        count = _bootstrap_profiler(profiler, points)

        assert count == 24
        assert profiler.total_observations > 0

    def test_profiler_resets_on_gaps(self):
        profiler = PerformanceProfiler()
        start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)

        points = [
            HistoryDataPoint(
                timestamp=start,
                indoor_temp=72.0,
                outdoor_temp=90.0,
                hvac_mode="cool",
                hvac_running=True,
                hvac_action="cooling",
                setpoint=74.0,
                humidity=None,
                wind_speed_mph=None,
            ),
            HistoryDataPoint(
                timestamp=start + timedelta(minutes=5),
                indoor_temp=None,
                outdoor_temp=None,
                hvac_mode="cool",
                hvac_running=False,
                hvac_action="idle",
                setpoint=74.0,
                humidity=None,
                wind_speed_mph=None,
            ),
            HistoryDataPoint(
                timestamp=start + timedelta(minutes=10),
                indoor_temp=71.5,
                outdoor_temp=90.0,
                hvac_mode="cool",
                hvac_running=True,
                hvac_action="cooling",
                setpoint=74.0,
                humidity=None,
                wind_speed_mph=None,
            ),
        ]

        _bootstrap_profiler(profiler, points)

        assert profiler._previous_indoor_temp == 71.5


# ── HistoryDataPoint tests ──────────────────────────────────────────────


class TestHistoryDataPoint:
    """Test the data point validity logic."""

    def test_valid_point(self):
        p = HistoryDataPoint(
            timestamp=datetime.now(timezone.utc),
            indoor_temp=72.0,
            outdoor_temp=85.0,
            hvac_mode="cool",
            hvac_running=False,
            hvac_action="idle",
            setpoint=74.0,
            humidity=None,
            wind_speed_mph=None,
        )
        assert p.valid is True

    def test_invalid_no_indoor(self):
        p = HistoryDataPoint(
            timestamp=datetime.now(timezone.utc),
            indoor_temp=None,
            outdoor_temp=85.0,
            hvac_mode="cool",
            hvac_running=False,
            hvac_action="idle",
            setpoint=74.0,
            humidity=None,
            wind_speed_mph=None,
        )
        assert p.valid is False

    def test_invalid_no_outdoor(self):
        p = HistoryDataPoint(
            timestamp=datetime.now(timezone.utc),
            indoor_temp=72.0,
            outdoor_temp=None,
            hvac_mode="cool",
            hvac_running=False,
            hvac_action="idle",
            setpoint=74.0,
            humidity=None,
            wind_speed_mph=None,
        )
        assert p.valid is False
