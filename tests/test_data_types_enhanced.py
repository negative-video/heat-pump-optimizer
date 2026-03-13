"""Tests for enhanced data type fields.

Tests ForecastPoint.effective_outdoor_temp, HourlySavingsRecord new fields
(solar_offset_kwh, grid_kwh), and DailySavingsReport aggregations.
"""

import importlib
import os
import sys
import types
from datetime import datetime, timezone

import pytest

# ── Module loading (same pattern as test_greybox_optimizer.py) ──────

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, PROJECT_ROOT)

import importlib.util

CC = os.path.join(PROJECT_ROOT, "custom_components", "heatpump_optimizer")

# Create package stubs (idempotent)
if "custom_components" not in sys.modules:
    pkg = types.ModuleType("custom_components")
    pkg.__path__ = [os.path.join(PROJECT_ROOT, "custom_components")]
    sys.modules["custom_components"] = pkg

if "custom_components.heatpump_optimizer" not in sys.modules:
    ho = types.ModuleType("custom_components.heatpump_optimizer")
    ho.__path__ = [CC]
    sys.modules["custom_components.heatpump_optimizer"] = ho

if "custom_components.heatpump_optimizer.engine" not in sys.modules:
    engine = types.ModuleType("custom_components.heatpump_optimizer.engine")
    engine.__path__ = [os.path.join(CC, "engine")]
    sys.modules["custom_components.heatpump_optimizer.engine"] = engine


def _load(full_name: str, path: str):
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


dt_mod = _load(
    "custom_components.heatpump_optimizer.engine.data_types",
    os.path.join(CC, "engine", "data_types.py"),
)
sys.modules["custom_components.heatpump_optimizer.engine"].data_types = dt_mod

ForecastPoint = dt_mod.ForecastPoint
HourlySavingsRecord = dt_mod.HourlySavingsRecord
DailySavingsReport = dt_mod.DailySavingsReport


# ── Tests ──────────────────────────────────────────────────────────


class TestEffectiveOutdoorTemp:
    """Test ForecastPoint.effective_outdoor_temp property."""

    def test_no_wind_returns_outdoor(self):
        """With no wind data, effective temp equals outdoor temp."""
        fp = ForecastPoint(
            time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            outdoor_temp=40.0,
            wind_speed_mph=None,
        )
        assert fp.effective_outdoor_temp == 40.0

    def test_warm_weather_no_adjustment(self):
        """Above 50F, wind chill should not apply even with high wind."""
        fp = ForecastPoint(
            time=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            outdoor_temp=55.0,
            wind_speed_mph=25.0,
        )
        assert fp.effective_outdoor_temp == 55.0

    def test_cold_plus_wind_returns_lower(self):
        """Cold temp + wind should produce effective temp below actual."""
        fp = ForecastPoint(
            time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            outdoor_temp=30.0,
            wind_speed_mph=15.0,
        )
        eff = fp.effective_outdoor_temp
        assert eff < 30.0, f"Expected effective temp < 30, got {eff}"

    def test_low_wind_no_adjustment(self):
        """Wind at or below 3 mph should not trigger wind chill."""
        fp = ForecastPoint(
            time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            outdoor_temp=30.0,
            wind_speed_mph=3.0,
        )
        assert fp.effective_outdoor_temp == 30.0

        fp2 = ForecastPoint(
            time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            outdoor_temp=30.0,
            wind_speed_mph=2.0,
        )
        assert fp2.effective_outdoor_temp == 30.0

    def test_never_exceeds_actual(self):
        """Effective temp should never be higher than actual outdoor temp."""
        for temp in [10.0, 20.0, 30.0, 40.0, 49.0]:
            for wind in [5.0, 10.0, 20.0, 40.0]:
                fp = ForecastPoint(
                    time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
                    outdoor_temp=temp,
                    wind_speed_mph=wind,
                )
                assert fp.effective_outdoor_temp <= temp, (
                    f"Effective {fp.effective_outdoor_temp} > actual {temp} "
                    f"at wind {wind} mph"
                )

    def test_nws_formula_specific_values(self):
        """Verify specific NWS wind chill values."""
        # At 30F, 10 mph wind: NWS formula
        fp = ForecastPoint(
            time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            outdoor_temp=30.0,
            wind_speed_mph=10.0,
        )
        eff = fp.effective_outdoor_temp

        # Manual NWS: 35.74 + 0.6215*30 - 35.75*(10^0.16) + 0.4275*30*(10^0.16)
        expected = (
            35.74
            + 0.6215 * 30.0
            - 35.75 * (10.0 ** 0.16)
            + 0.4275 * 30.0 * (10.0 ** 0.16)
        )
        assert abs(eff - expected) < 0.01, f"Expected {expected:.2f}, got {eff:.2f}"

        # Should be approximately 21F
        assert 20.0 < eff < 25.0, f"Wind chill at 30F/10mph should be ~21F, got {eff:.1f}"


class TestHourlySavingsRecord:
    """Test HourlySavingsRecord new fields."""

    def test_solar_offset_kwh_field(self):
        """solar_offset_kwh should exist and default to 0.0."""
        record = HourlySavingsRecord(
            hour=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            mode="cool",
            baseline_runtime_minutes=30.0,
            actual_runtime_minutes=20.0,
        )
        assert record.solar_offset_kwh == 0.0

        # Can be set to a value
        record.solar_offset_kwh = 1.5
        assert record.solar_offset_kwh == 1.5

    def test_grid_kwh_field(self):
        """grid_kwh should exist and default to None."""
        record = HourlySavingsRecord(
            hour=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            mode="cool",
            baseline_runtime_minutes=30.0,
            actual_runtime_minutes=20.0,
        )
        assert record.grid_kwh is None

        # Can be set
        record.grid_kwh = 0.8
        assert record.grid_kwh == 0.8

    def test_solar_offset_with_grid_kwh(self):
        """When both are set, grid_kwh = actual_kwh - solar_offset_kwh."""
        record = HourlySavingsRecord(
            hour=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            mode="cool",
            baseline_runtime_minutes=30.0,
            actual_runtime_minutes=20.0,
            actual_kwh=2.0,
            solar_offset_kwh=0.5,
            grid_kwh=1.5,
        )
        assert record.actual_kwh == 2.0
        assert record.solar_offset_kwh == 0.5
        assert record.grid_kwh == 1.5


class TestDailySavingsReport:
    """Test DailySavingsReport aggregation properties."""

    def _make_hours(self) -> list[HourlySavingsRecord]:
        """Create sample hourly records for testing."""
        records = []
        for h in range(3):
            records.append(HourlySavingsRecord(
                hour=datetime(2026, 7, 15, h + 10, 0, tzinfo=timezone.utc),
                mode="cool",
                baseline_runtime_minutes=40.0,
                actual_runtime_minutes=25.0,
                baseline_kwh=2.0,
                actual_kwh=1.2,
                saved_kwh=0.8,
                saved_cost=0.10 if h < 2 else None,  # one None entry
            ))
        return records

    def test_total_saved_kwh(self):
        """total_saved_kwh should sum saved_kwh across all hours."""
        hours = self._make_hours()
        report = DailySavingsReport(date="2026-07-15", hours=hours)

        assert abs(report.total_saved_kwh - 2.4) < 0.01  # 0.8 * 3

    def test_total_saved_cost_with_none_entries(self):
        """total_saved_cost should skip None entries and sum the rest."""
        hours = self._make_hours()
        report = DailySavingsReport(date="2026-07-15", hours=hours)

        # Two records have saved_cost=0.10, one has None
        expected = 0.20
        assert report.total_saved_cost is not None
        assert abs(report.total_saved_cost - expected) < 0.01

    def test_total_saved_cost_all_none(self):
        """total_saved_cost should return None when all entries are None."""
        hours = []
        for h in range(3):
            hours.append(HourlySavingsRecord(
                hour=datetime(2026, 7, 15, h + 10, 0, tzinfo=timezone.utc),
                mode="cool",
                baseline_runtime_minutes=40.0,
                actual_runtime_minutes=25.0,
                saved_cost=None,
            ))
        report = DailySavingsReport(date="2026-07-15", hours=hours)
        assert report.total_saved_cost is None

    def test_empty_hours(self):
        """Report with no hours should return zero for kwh totals."""
        report = DailySavingsReport(date="2026-07-15", hours=[])
        assert report.total_saved_kwh == 0.0
        assert report.total_baseline_kwh == 0.0
        assert report.total_actual_kwh == 0.0
        assert report.total_saved_cost is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
