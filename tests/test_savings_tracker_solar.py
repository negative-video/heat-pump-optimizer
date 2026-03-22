"""Tests for SavingsTracker — solar production offset, grid_kwh, CO2/cost."""

import importlib
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

# ── Module loading (same pattern as test_thermal_estimator.py) ───────

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, PROJECT_ROOT)

CC = os.path.join(PROJECT_ROOT, "custom_components", "heatpump_optimizer")

# Create package stubs
pkg = types.ModuleType("custom_components")
pkg.__path__ = [os.path.join(PROJECT_ROOT, "custom_components")]
sys.modules.setdefault("custom_components", pkg)

ho = types.ModuleType("custom_components.heatpump_optimizer")
ho.__path__ = [CC]
sys.modules.setdefault("custom_components.heatpump_optimizer", ho)

engine = types.ModuleType("custom_components.heatpump_optimizer.engine")
engine.__path__ = [os.path.join(CC, "engine")]
sys.modules.setdefault("custom_components.heatpump_optimizer.engine", engine)


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

st_mod = _load(
    "custom_components.heatpump_optimizer.savings_tracker",
    os.path.join(CC, "savings_tracker.py"),
)
ho.savings_tracker = st_mod

SavingsTracker = st_mod.SavingsTracker
HourlySavingsRecord = dt_mod.HourlySavingsRecord
DailySavingsReport = dt_mod.DailySavingsReport


# ── Helpers ──────────────────────────────────────────────────────────


def _make_time(hour: int = 10, minute: int = 0) -> datetime:
    """Create a UTC datetime on a fixed date with the given hour/minute."""
    return datetime(2026, 3, 12, hour, minute, 0, tzinfo=timezone.utc)


def _record_full_hour(
    tracker: SavingsTracker,
    base_hour: int = 10,
    hvac_running: bool = True,
    power_watts: float = 3000.0,
    carbon_intensity: float = 400.0,
    electricity_rate: float = 0.12,
    mode: str = "cool",
    solar_production_watts: float | None = None,
    interval_minutes: float = 5.0,
) -> None:
    """Record 12 intervals of 5 minutes to fill one hour, then cross boundary."""
    for i in range(12):
        t = _make_time(base_hour, i * 5)
        tracker.record_interval(
            now=t,
            hvac_running=hvac_running,
            interval_minutes=interval_minutes,
            power_watts=power_watts,
            carbon_intensity=carbon_intensity,
            electricity_rate=electricity_rate,
            mode=mode,
            solar_production_watts=solar_production_watts,
        )
    # Cross hour boundary to finalize
    tracker.record_interval(
        now=_make_time(base_hour + 1, 0),
        hvac_running=False,
        interval_minutes=interval_minutes,
        power_watts=0,
        carbon_intensity=carbon_intensity,
        electricity_rate=electricity_rate,
        mode="off",
    )


# ── Tests ────────────────────────────────────────────────────────────


class TestSolarOffset:
    """Solar production should offset grid consumption."""

    def test_solar_offsets_grid(self):
        """With partial solar, grid_kwh = actual_kwh - solar_offset_kwh."""
        tracker = SavingsTracker()
        # HVAC: 3000W, Solar: 1000W -> solar covers 1/3 of consumption
        _record_full_hour(
            tracker,
            power_watts=3000.0,
            solar_production_watts=1000.0,
        )
        record = tracker._hourly_records[-1]

        # actual_kwh = (60 min / 60) * (3000 / 1000) = 3.0 kWh
        assert abs(record.actual_kwh - 3.0) < 0.01

        # solar_offset_kwh = 1000 * (60/60) / 1000 = 1.0 kWh
        assert abs(record.solar_offset_kwh - 1.0) < 0.01

        # grid_kwh = 3.0 - 1.0 = 2.0
        assert abs(record.grid_kwh - 2.0) < 0.01

    def test_solar_exceeds_hvac(self):
        """Solar > HVAC power: solar_offset capped at HVAC, grid_kwh = 0."""
        tracker = SavingsTracker()
        # HVAC: 2000W, Solar: 5000W
        _record_full_hour(
            tracker,
            power_watts=2000.0,
            solar_production_watts=5000.0,
        )
        record = tracker._hourly_records[-1]

        # actual_kwh = 2.0
        assert abs(record.actual_kwh - 2.0) < 0.01

        # solar_offset capped at HVAC power: 2000 * 1hr / 1000 = 2.0
        assert abs(record.solar_offset_kwh - 2.0) < 0.01

        # grid_kwh = max(0, 2.0 - 2.0) = 0.0
        assert record.grid_kwh == 0.0

    def test_no_solar_means_grid_equals_actual(self):
        """Without solar, grid_kwh should equal actual_kwh."""
        tracker = SavingsTracker()
        _record_full_hour(
            tracker,
            power_watts=3000.0,
            solar_production_watts=None,
        )
        record = tracker._hourly_records[-1]

        assert abs(record.solar_offset_kwh) < 0.001
        assert abs(record.grid_kwh - record.actual_kwh) < 0.01

    def test_solar_not_accumulated_when_hvac_off(self):
        """Solar offset should only accumulate when HVAC is running."""
        tracker = SavingsTracker()
        _record_full_hour(
            tracker,
            hvac_running=False,
            power_watts=3000.0,
            solar_production_watts=2000.0,
        )
        record = tracker._hourly_records[-1]

        assert record.solar_offset_kwh == 0.0
        assert record.actual_kwh == 0.0
        assert record.grid_kwh == 0.0


class TestGridKwhInRecord:
    """grid_kwh should be used for CO2 and cost calculations."""

    def test_grid_kwh_populated_correctly(self):
        tracker = SavingsTracker()
        _record_full_hour(
            tracker,
            power_watts=4000.0,
            solar_production_watts=1000.0,
        )
        record = tracker._hourly_records[-1]

        # actual_kwh = 4.0, solar_offset = 1.0, grid = 3.0
        assert abs(record.grid_kwh - 3.0) < 0.01

    def test_grid_kwh_non_negative(self):
        """grid_kwh should never go negative even with huge solar."""
        tracker = SavingsTracker()
        _record_full_hour(
            tracker,
            power_watts=1000.0,
            solar_production_watts=10000.0,
        )
        record = tracker._hourly_records[-1]
        assert record.grid_kwh >= 0.0

    def test_co2_uses_grid_kwh(self):
        """Actual CO2 should be based on grid_kwh, not actual_kwh."""
        tracker = SavingsTracker()
        _record_full_hour(
            tracker,
            power_watts=3000.0,
            solar_production_watts=1000.0,
            carbon_intensity=500.0,
        )
        record = tracker._hourly_records[-1]

        # grid_kwh = 2.0, CO2 = 2.0 * 500 = 1000 grams
        expected_co2 = record.grid_kwh * 500.0
        assert abs(record.actual_co2_grams - expected_co2) < 0.1

        # Baseline CO2 uses baseline_kwh (no solar offset)
        expected_baseline_co2 = record.baseline_kwh * 500.0
        assert abs(record.baseline_co2_grams - expected_baseline_co2) < 0.1

    def test_cost_uses_grid_kwh(self):
        """Actual cost should be based on grid_kwh, not actual_kwh."""
        tracker = SavingsTracker()
        _record_full_hour(
            tracker,
            power_watts=3000.0,
            solar_production_watts=1000.0,
            electricity_rate=0.15,
        )
        record = tracker._hourly_records[-1]

        # grid_kwh = 2.0, cost = 2.0 * 0.15 = 0.30
        expected_cost = record.grid_kwh * 0.15
        assert abs(record.actual_cost - expected_cost) < 0.001

        # Baseline cost uses baseline_kwh (no solar offset)
        expected_baseline_cost = record.baseline_kwh * 0.15
        assert abs(record.baseline_cost - expected_baseline_cost) < 0.001

    def test_saved_co2_benefits_from_solar(self):
        """With solar, saved_co2 should be higher than without (lower actual)."""
        # With solar
        tracker_solar = SavingsTracker()
        tracker_solar.set_baseline_ratio(baseline_runtime=90, optimized_runtime=60)
        _record_full_hour(
            tracker_solar,
            power_watts=3000.0,
            solar_production_watts=1500.0,
            carbon_intensity=400.0,
        )
        record_solar = tracker_solar._hourly_records[-1]

        # Without solar
        tracker_no_solar = SavingsTracker()
        tracker_no_solar.set_baseline_ratio(baseline_runtime=90, optimized_runtime=60)
        _record_full_hour(
            tracker_no_solar,
            power_watts=3000.0,
            solar_production_watts=None,
            carbon_intensity=400.0,
        )
        record_no_solar = tracker_no_solar._hourly_records[-1]

        # Solar user has lower actual_co2 -> higher saved_co2
        assert record_solar.saved_co2_grams > record_no_solar.saved_co2_grams


class TestSavingsTrackerPersistence:
    """Serialize / deserialize round-trip."""

    def test_round_trip(self):
        tracker = SavingsTracker()
        tracker._cumulative_kwh_saved = 12.5
        tracker._cumulative_cost_saved = 1.50
        tracker._cumulative_co2_saved_grams = 5000.0
        tracker._cumulative_kwh_baseline = 50.0
        tracker._cumulative_kwh_actual = 37.5
        tracker._cumulative_kwh_worst_case = 100.0

        data = tracker.to_dict()
        restored = SavingsTracker.from_dict(data)

        assert restored._cumulative_kwh_saved == 12.5
        assert restored._cumulative_cost_saved == 1.50
        assert restored._cumulative_co2_saved_grams == 5000.0
        assert restored._cumulative_kwh_baseline == 50.0
        assert restored._cumulative_kwh_actual == 37.5
        assert restored._cumulative_kwh_worst_case == 100.0

    def test_baseline_ratio_persisted(self):
        tracker = SavingsTracker()
        tracker.set_baseline_ratio(baseline_runtime=120, optimized_runtime=80)
        expected_ratio = 120.0 / 80.0

        data = tracker.to_dict()
        restored = SavingsTracker.from_dict(data)

        assert abs(restored._baseline_to_optimized_ratio - expected_ratio) < 0.001

    def test_from_dict_defaults(self):
        """Missing keys should default to zero / 1.0."""
        restored = SavingsTracker.from_dict({})
        assert restored._cumulative_kwh_saved == 0.0
        assert restored._cumulative_cost_saved == 0.0
        assert restored._baseline_to_optimized_ratio == 1.0


class TestSavingsTrackerHourBoundary:
    """Hour boundary crossing and finalization."""

    def test_boundary_finalizes_previous(self):
        """Crossing an hour boundary should finalize the previous hour."""
        tracker = SavingsTracker()

        # Record intervals in hour 10
        for i in range(6):
            tracker.record_interval(
                now=_make_time(10, i * 5),
                hvac_running=True,
                interval_minutes=5.0,
                power_watts=2000.0,
                carbon_intensity=300.0,
                electricity_rate=0.10,
                mode="cool",
            )

        # No finalized record yet (still within hour 10)
        assert len(tracker._hourly_records) == 0

        # Cross into hour 11
        tracker.record_interval(
            now=_make_time(11, 0),
            hvac_running=False,
            interval_minutes=5.0,
            power_watts=0,
            carbon_intensity=300.0,
            electricity_rate=0.10,
            mode="off",
        )

        # Now hour 10 should be finalized
        assert len(tracker._hourly_records) == 1
        record = tracker._hourly_records[0]
        assert record.actual_runtime_minutes == 30.0  # 6 * 5 min
        assert abs(record.actual_kwh - 1.0) < 0.01  # 30/60 * 2000/1000

    def test_multiple_intervals_accumulated(self):
        """Multiple intervals within the same hour should accumulate."""
        tracker = SavingsTracker()
        tracker.set_baseline_ratio(baseline_runtime=60, optimized_runtime=30)

        # 12 intervals of 5 minutes = 60 minutes runtime
        _record_full_hour(tracker, power_watts=2400.0)

        record = tracker._hourly_records[-1]
        assert record.actual_runtime_minutes == 60.0
        # actual_kwh = (60/60) * (2400/1000) = 2.4
        assert abs(record.actual_kwh - 2.4) < 0.01
        # baseline = 60 * (60/30) = 120 min -> baseline_kwh = (120/60)*(2400/1000) = 4.8
        assert abs(record.baseline_kwh - 4.8) < 0.01
        assert abs(record.saved_kwh - 2.4) < 0.01

    def test_today_report(self):
        """today_report should include only hours from the current UTC day."""
        tracker = SavingsTracker()

        # Record an hour today (use a time that matches "today" in UTC)
        now = datetime.now(timezone.utc)
        hour = now.hour
        # If we are at hour 23 the boundary cross will go to next day,
        # so use an earlier hour to be safe
        if hour >= 23:
            hour = 10

        for i in range(6):
            t = now.replace(hour=hour, minute=i * 5, second=0, microsecond=0)
            tracker.record_interval(
                now=t,
                hvac_running=True,
                interval_minutes=5.0,
                power_watts=2000.0,
                carbon_intensity=None,
                electricity_rate=None,
                mode="cool",
            )

        # Cross boundary
        t_next = now.replace(hour=hour + 1, minute=0, second=0, microsecond=0)
        tracker.record_interval(
            now=t_next,
            hvac_running=False,
            interval_minutes=5.0,
            power_watts=0,
            carbon_intensity=None,
            electricity_rate=None,
            mode="off",
        )

        report = tracker.today_report()
        assert isinstance(report, DailySavingsReport)
        assert len(report.hours) >= 1
        assert report.total_actual_kwh > 0

    def test_cumulative_totals_update(self):
        """Cumulative totals should accumulate across multiple hours."""
        tracker = SavingsTracker()
        tracker.set_accuracy_tier("estimated")  # cumulative only accumulates at ESTIMATED+
        tracker.set_baseline_ratio(baseline_runtime=90, optimized_runtime=60)

        _record_full_hour(tracker, base_hour=10, power_watts=3000.0)
        _record_full_hour(tracker, base_hour=12, power_watts=3000.0)

        totals = tracker.cumulative_totals()
        assert totals["kwh_actual"] > 0
        assert totals["kwh_baseline"] > totals["kwh_actual"]
        assert totals["kwh_saved"] > 0

    def test_set_baseline_ratio_zero_optimized(self):
        """Zero optimized runtime should default ratio to 1.0."""
        tracker = SavingsTracker()
        tracker.set_baseline_ratio(baseline_runtime=60, optimized_runtime=0)
        assert tracker._baseline_to_optimized_ratio == 1.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
