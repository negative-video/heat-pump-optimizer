"""Tests for PerformanceProfiler — Beestat-equivalent live performance learning."""

import importlib
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone

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


# Load data_types first (dependency), then performance_model, then profiler
data_types_mod = _load(
    "custom_components.heatpump_optimizer.engine.data_types",
    os.path.join(CC, "engine", "data_types.py"),
)
perf_model_mod = _load(
    "custom_components.heatpump_optimizer.engine.performance_model",
    os.path.join(CC, "engine", "performance_model.py"),
)
profiler_mod = _load(
    "custom_components.heatpump_optimizer.learning.performance_profiler",
    os.path.join(CC, "learning", "performance_profiler.py"),
)

PerformanceProfiler = profiler_mod.PerformanceProfiler
BinAccumulator = profiler_mod.BinAccumulator
PerformanceModel = perf_model_mod.PerformanceModel


# ── Helpers ───────────────────────────────────────────────────────────

def _make_time(minutes_offset: int = 0) -> datetime:
    """Create a UTC datetime with an offset in minutes from a base time."""
    base = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(minutes=minutes_offset)


def _feed_cooling_data(profiler: PerformanceProfiler, n_hours: int = 4,
                       outdoor_temp: float = 85.0, delta_per_5min: float = -0.25):
    """Feed synthetic cooling observations for n_hours at a given outdoor temp."""
    intervals = int(n_hours * 12)  # 12 five-minute intervals per hour
    indoor = 75.0
    for i in range(intervals):
        profiler.record_observation(
            indoor_temp=indoor,
            outdoor_temp=outdoor_temp,
            hvac_action="cooling",
            hvac_mode="cool",
            now=_make_time(i * 5),
        )
        indoor += delta_per_5min  # indoor drops during cooling


def _feed_resist_data(profiler: PerformanceProfiler, n_hours: int = 4,
                      outdoor_temp: float = 70.0, delta_per_5min: float = 0.05):
    """Feed synthetic passive drift observations."""
    indoor = 72.0
    base_offset = 500  # avoid timestamp overlap with other feeds
    for i in range(int(n_hours * 12)):
        profiler.record_observation(
            indoor_temp=indoor,
            outdoor_temp=outdoor_temp,
            hvac_action="idle",
            hvac_mode="cool",
            now=_make_time(base_offset + i * 5),
        )
        indoor += delta_per_5min


# ── Tests ─────────────────────────────────────────────────────────────


class TestBinAccumulator:
    def test_empty(self):
        acc = BinAccumulator()
        assert acc.count == 0
        assert acc.mean_delta == 0.0
        assert acc.std_delta == 0.0

    def test_accumulation(self):
        acc = BinAccumulator()
        acc.add(3.0)
        acc.add(5.0)
        assert acc.count == 2
        assert acc.mean_delta == 4.0

    def test_solar_tracking(self):
        acc = BinAccumulator()
        acc.add(1.0, solar=500.0)
        acc.add(2.0, solar=300.0)
        assert acc.sum_solar == 800.0
        assert acc.count == 2


class TestModeClassification:
    def test_cooling(self):
        p = PerformanceProfiler()
        assert p._classify_mode("cooling", "cool", False) == "cool_1"

    def test_heating_compressor(self):
        p = PerformanceProfiler()
        assert p._classify_mode("heating", "heat", False) == "heat_1"

    def test_heating_aux(self):
        p = PerformanceProfiler()
        assert p._classify_mode("heating", "heat", True) == "auxiliary_heat_1"

    def test_aux_heating_action(self):
        p = PerformanceProfiler()
        assert p._classify_mode("aux_heating", "heat", False) == "auxiliary_heat_1"

    def test_emergency_heating(self):
        p = PerformanceProfiler()
        assert p._classify_mode("emergency_heating", "heat", False) == "auxiliary_heat_1"

    def test_idle_not_off(self):
        p = PerformanceProfiler()
        assert p._classify_mode("idle", "cool", False) == "resist"

    def test_idle_off(self):
        p = PerformanceProfiler()
        # hvac_mode == "off" is filtered before _classify_mode is called,
        # but if it reaches here, idle + off should return None
        assert p._classify_mode("idle", "off", False) is None

    def test_none_action_not_off(self):
        """hvac_action=None with active hvac_mode → resist (passive drift)."""
        p = PerformanceProfiler()
        assert p._classify_mode(None, "heat_cool", False) == "resist"

    def test_none_action_off(self):
        """hvac_action=None with hvac_mode='off' → None."""
        p = PerformanceProfiler()
        assert p._classify_mode(None, "off", False) is None

    def test_unknown_action_not_off(self):
        """Unrecognized hvac_action with active mode → resist (passive drift)."""
        p = PerformanceProfiler()
        assert p._classify_mode("drying", "cool", False) == "resist"

    def test_unknown_action_off(self):
        p = PerformanceProfiler()
        assert p._classify_mode("drying", "off", False) is None


class TestRecordObservation:
    def test_first_observation_skipped(self):
        """First observation has no previous, so no delta computed."""
        p = PerformanceProfiler()
        p.record_observation(72.0, 85.0, "cooling", "cool", now=_make_time(0))
        assert p.total_observations == 0

    def test_basic_cooling_recording(self):
        p = PerformanceProfiler()
        p.record_observation(72.0, 85.0, "cooling", "cool", now=_make_time(0))
        p.record_observation(71.5, 85.0, "cooling", "cool", now=_make_time(5))
        assert p.total_observations == 1
        # -0.5°F in 5 min = -6.0°F/hr
        bins = p._bins["cool_1"]
        assert 85 in bins
        assert abs(bins[85].mean_delta - (-6.0)) < 0.01

    def test_hvac_off_discarded(self):
        p = PerformanceProfiler()
        p.record_observation(72.0, 85.0, "cooling", "off", now=_make_time(0))
        p.record_observation(71.5, 85.0, "cooling", "off", now=_make_time(5))
        assert p.total_observations == 0

    def test_outlier_temp_change_rejected(self):
        """Temperature change > 2°F in 5 min is rejected."""
        p = PerformanceProfiler()
        p.record_observation(72.0, 85.0, "cooling", "cool", now=_make_time(0))
        p.record_observation(69.0, 85.0, "cooling", "cool", now=_make_time(5))
        assert p.total_observations == 0

    def test_outlier_delta_rejected(self):
        """Delta > 15°F/hr is rejected (but temp change might be <= 2°F)."""
        p = PerformanceProfiler()
        p.record_observation(72.0, 85.0, "cooling", "cool", now=_make_time(0))
        # 1.5°F change in 5 min = 18°F/hr > threshold
        p.record_observation(73.5, 85.0, "heating", "heat", now=_make_time(5))
        assert p.total_observations == 0

    def test_bad_interval_rejected(self):
        """Intervals outside 50% tolerance are rejected."""
        p = PerformanceProfiler()
        p.record_observation(72.0, 85.0, "cooling", "cool", now=_make_time(0))
        # 15 minutes later (3x expected) — rejected
        p.record_observation(71.5, 85.0, "cooling", "cool", now=_make_time(15))
        assert p.total_observations == 0

    def test_idle_resist_mode(self):
        p = PerformanceProfiler()
        p.record_observation(72.0, 80.0, "idle", "cool", now=_make_time(0))
        p.record_observation(72.1, 80.0, "idle", "cool", now=_make_time(5))
        assert p.total_observations == 1
        assert 80 in p._bins["resist"]


class TestTrendlineFitting:
    def test_single_bin_no_trendline(self):
        bins = {75: BinAccumulator()}
        for _ in range(10):
            bins[75].add(-3.0)
        result = PerformanceProfiler._fit_trendline(bins)
        # Only 1 temp bin — can't fit a line
        assert result["slope"] == 0.0

    def test_two_bins(self):
        bins = {
            70: BinAccumulator(),
            90: BinAccumulator(),
        }
        for _ in range(10):
            bins[70].add(-5.0)
            bins[90].add(-1.0)
        result = PerformanceProfiler._fit_trendline(bins)
        # slope should be positive (less negative cooling at higher temps)
        assert result["slope"] > 0
        # At 70°F: slope*70 + intercept ≈ -5.0
        predicted_70 = result["slope"] * 70 + result["intercept"]
        assert abs(predicted_70 - (-5.0)) < 0.1

    def test_weighted_by_count(self):
        bins = {
            70: BinAccumulator(),
            80: BinAccumulator(),
        }
        # 100 observations at 70°F, only 6 at 80°F
        for _ in range(100):
            bins[70].add(-4.0)
        for _ in range(6):
            bins[80].add(-2.0)
        result = PerformanceProfiler._fit_trendline(bins)
        # Trendline should be pulled toward the heavily-weighted 70°F point
        assert result["slope"] != 0


class TestBeestatFormat:
    def test_empty_profiler(self):
        p = PerformanceProfiler()
        data = p.to_beestat_format()
        assert data["temperature"]["cool_1"] is None
        assert data["temperature"]["resist"] is None

    def test_with_data(self):
        p = PerformanceProfiler()
        # Feed enough cooling data at multiple outdoor temps
        for outdoor in range(75, 96):
            delta_per_5min = -0.3 + (outdoor - 75) * 0.01  # less cooling at higher temps
            indoor = 72.0
            base = (outdoor - 75) * 100
            for i in range(8):  # 8 intervals = 40 min > 30 min minimum
                p.record_observation(
                    indoor_temp=indoor,
                    outdoor_temp=float(outdoor),
                    hvac_action="cooling",
                    hvac_mode="cool",
                    now=_make_time(base + i * 5),
                )
                indoor += delta_per_5min

        data = p.to_beestat_format()
        cool = data["temperature"]["cool_1"]
        assert cool is not None
        assert "deltas" in cool
        assert "linear_trendline" in cool
        assert "slope" in cool["linear_trendline"]
        # All deltas should be negative (cooling)
        for temp_str, delta in cool["deltas"].items():
            assert delta < 0, f"Expected negative delta at {temp_str}°F, got {delta}"

    def test_balance_point(self):
        p = PerformanceProfiler()
        # Feed resist data: negative below 50°F, positive above
        for outdoor in range(30, 80):
            drift = (outdoor - 50) * 0.03  # crosses zero at 50°F
            indoor = 70.0
            base = (outdoor - 30) * 100
            for i in range(8):
                p.record_observation(
                    indoor_temp=indoor,
                    outdoor_temp=float(outdoor),
                    hvac_action="idle",
                    hvac_mode="heat",
                    now=_make_time(base + i * 5),
                )
                indoor += drift * (5 / 60)  # convert °F/hr to °F per 5 min

        data = p.to_beestat_format()
        resist = data["temperature"]["resist"]
        assert resist is not None
        # Balance point should be near 50°F
        bp = data["balance_point"].get("resist")
        if bp is not None:
            assert 40 <= bp <= 60, f"Balance point {bp} not near 50°F"


class TestPerformanceModelOutput:
    def test_to_performance_model_insufficient(self):
        p = PerformanceProfiler()
        assert p.to_performance_model() is None

    def test_to_performance_model_with_data(self):
        p = PerformanceProfiler()
        # Feed cooling + resist data at multiple temps
        for outdoor in range(70, 100):
            indoor = 72.0
            base = (outdoor - 70) * 200
            for i in range(8):
                p.record_observation(
                    indoor_temp=indoor,
                    outdoor_temp=float(outdoor),
                    hvac_action="cooling",
                    hvac_mode="cool",
                    now=_make_time(base + i * 5),
                )
                indoor -= 0.2
            # Also idle/resist data
            indoor = 72.0
            for i in range(8):
                p.record_observation(
                    indoor_temp=indoor,
                    outdoor_temp=float(outdoor),
                    hvac_action="idle",
                    hvac_mode="cool",
                    now=_make_time(base + 100 + i * 5),
                )
                indoor += 0.05

        model = p.to_performance_model()
        assert model is not None
        # Model should return negative cooling deltas
        delta = model.cooling_delta(85.0)
        assert delta < 0


class TestConfidence:
    def test_empty_zero(self):
        p = PerformanceProfiler()
        assert p.confidence() == 0.0

    def test_mode_confidence_grows(self):
        p = PerformanceProfiler()
        # Feed a bit of data
        indoor = 72.0
        for i in range(12):  # 1 hour
            p.record_observation(72.0, 85.0, "cooling", "cool", now=_make_time(i * 5))
            indoor -= 0.2

        c1 = p.confidence("cool_1")
        # Now add more data at different outdoor temps
        for outdoor in range(75, 95):
            indoor = 72.0
            base = 200 + (outdoor - 75) * 100
            for i in range(12):
                p.record_observation(
                    indoor_temp=indoor,
                    outdoor_temp=float(outdoor),
                    hvac_action="cooling",
                    hvac_mode="cool",
                    now=_make_time(base + i * 5),
                )
                indoor -= 0.2

        c2 = p.confidence("cool_1")
        assert c2 > c1


class TestPersistence:
    def test_round_trip(self):
        p = PerformanceProfiler()
        # Feed some data
        indoor = 72.0
        for i in range(12):
            p.record_observation(
                indoor_temp=indoor,
                outdoor_temp=85.0,
                hvac_action="cooling",
                hvac_mode="cool",
                now=_make_time(i * 5),
            )
            indoor -= 0.2

        # Serialize and restore
        data = p.to_dict()
        p2 = PerformanceProfiler.from_dict(data)

        assert p2.total_observations == p.total_observations
        assert 85 in p2._bins["cool_1"]
        assert p2._bins["cool_1"][85].count == p._bins["cool_1"][85].count
        assert abs(p2._bins["cool_1"][85].mean_delta - p._bins["cool_1"][85].mean_delta) < 0.001

    def test_empty_round_trip(self):
        p = PerformanceProfiler()
        data = p.to_dict()
        p2 = PerformanceProfiler.from_dict(data)
        assert p2.total_observations == 0
        assert p2.confidence() == 0.0
