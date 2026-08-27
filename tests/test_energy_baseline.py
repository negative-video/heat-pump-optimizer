"""Tests for the weather-normalized energy baseline (item 10.2).

The point of this module is that savings stop being model-vs-model fiction, so
these tests check the two things that could quietly reintroduce fiction:
that the slopes really come from optimizer-off days, and that a missing
baseline reports nothing instead of guessing.
"""

import os
import sys
import types
from datetime import datetime, timedelta

import pytest

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, PROJECT_ROOT)

CC = os.path.join(PROJECT_ROOT, "custom_components", "heatpump_optimizer")

pkg = types.ModuleType("custom_components")
pkg.__path__ = [os.path.join(PROJECT_ROOT, "custom_components")]
sys.modules.setdefault("custom_components", pkg)

ho = types.ModuleType("custom_components.heatpump_optimizer")
ho.__path__ = [CC]
sys.modules.setdefault("custom_components.heatpump_optimizer", ho)

learning = types.ModuleType("custom_components.heatpump_optimizer.learning")
learning.__path__ = [os.path.join(CC, "learning")]
sys.modules.setdefault("custom_components.heatpump_optimizer.learning", learning)

from conftest import load_module  # noqa: E402

eb_mod = load_module(
    "custom_components.heatpump_optimizer.learning.energy_baseline",
    os.path.join(CC, "learning", "energy_baseline.py"),
)
learning.energy_baseline = eb_mod

EnergyBaseline = eb_mod.EnergyBaseline
DayEnergyRecord = eb_mod.DayEnergyRecord
BaselineFit = eb_mod.BaselineFit
DEGREE_DAY_BASE_F = eb_mod.DEGREE_DAY_BASE_F


# ── Helpers ─────────────────────────────────────────────────────────

INTERVAL = 5.0
INTERVALS_PER_DAY = int(1440 / INTERVAL)


def _feed_day(
    baseline: EnergyBaseline,
    day: datetime,
    mean_outdoor: float,
    kwh: float,
    optimizer_active: bool,
    power_measured: bool = True,
    coverage: float = 1.0,
):
    """Feed one synthetic day at constant power and constant outdoor temp."""
    n = int(INTERVALS_PER_DAY * coverage)
    watts = kwh * 1000.0 / (n * INTERVAL / 60.0) if n else 0.0
    for i in range(n):
        baseline.record_interval(
            now=day + timedelta(minutes=INTERVAL * i),
            interval_minutes=INTERVAL,
            power_watts=watts,
            outdoor_temp=mean_outdoor,
            optimizer_active=optimizer_active,
            power_measured=power_measured,
        )


def _day(n: int) -> datetime:
    return datetime(2026, 7, 1) + timedelta(days=n)


# ── Degree-day accounting ───────────────────────────────────────────


class TestDegreeDays:
    def test_cooling_day_produces_cdd_only(self):
        b = EnergyBaseline()
        _feed_day(b, _day(0), mean_outdoor=85.0, kwh=20.0, optimizer_active=False)
        b.force_close_day()
        rec = b.days[0]
        assert rec.cdd == pytest.approx(20.0)
        assert rec.hdd == 0.0

    def test_heating_day_produces_hdd_only(self):
        b = EnergyBaseline()
        _feed_day(b, _day(0), mean_outdoor=35.0, kwh=30.0, optimizer_active=False)
        b.force_close_day()
        rec = b.days[0]
        assert rec.hdd == pytest.approx(30.0)
        assert rec.cdd == 0.0

    def test_energy_is_integrated_not_assumed(self):
        b = EnergyBaseline()
        _feed_day(b, _day(0), mean_outdoor=80.0, kwh=12.5, optimizer_active=False)
        b.force_close_day()
        assert b.days[0].kwh == pytest.approx(12.5, rel=1e-6)

    def test_day_rolls_over_on_calendar_change(self):
        b = EnergyBaseline()
        _feed_day(b, _day(0), 80.0, 10.0, optimizer_active=False)
        _feed_day(b, _day(1), 80.0, 10.0, optimizer_active=False)
        # First day closed automatically when the second began
        assert len(b.days) == 1
        assert b.days[0].date == "2026-07-01"

    def test_partial_day_is_not_usable(self):
        b = EnergyBaseline()
        _feed_day(b, _day(0), 80.0, 5.0, optimizer_active=False, coverage=0.5)
        b.force_close_day()
        assert b.days[0].usable is False

    def test_missing_outdoor_temp_reduces_coverage(self):
        b = EnergyBaseline()
        day = _day(0)
        for i in range(INTERVALS_PER_DAY):
            # Half the day has no outdoor reading at all
            temp = 85.0 if i < INTERVALS_PER_DAY // 2 else None
            b.record_interval(
                now=day + timedelta(minutes=INTERVAL * i),
                interval_minutes=INTERVAL,
                power_watts=1000.0,
                outdoor_temp=temp,
                optimizer_active=False,
                power_measured=True,
            )
        b.force_close_day()
        assert b.days[0].usable is False


# ── Day labelling ───────────────────────────────────────────────────


class TestDayLabelling:
    def test_optimizer_off_day_is_baseline(self):
        b = EnergyBaseline()
        _feed_day(b, _day(0), 85.0, 20.0, optimizer_active=False)
        b.force_close_day()
        assert b.days[0].label == "baseline"

    def test_optimizer_on_day_is_optimized(self):
        b = EnergyBaseline()
        _feed_day(b, _day(0), 85.0, 20.0, optimizer_active=True)
        b.force_close_day()
        assert b.days[0].label == "optimized"

    def test_half_controlled_day_is_mixed_and_excluded(self):
        b = EnergyBaseline()
        day = _day(0)
        for i in range(INTERVALS_PER_DAY):
            b.record_interval(
                now=day + timedelta(minutes=INTERVAL * i),
                interval_minutes=INTERVAL,
                power_watts=1000.0,
                outdoor_temp=85.0,
                optimizer_active=(i % 2 == 0),
                power_measured=True,
            )
        b.force_close_day()
        assert b.days[0].label == "mixed"
        assert b.population("baseline") == []
        assert b.population("optimized") == []


# ── Regression ──────────────────────────────────────────────────────


class TestFit:
    def test_no_data_reports_no_baseline(self):
        b = EnergyBaseline()
        fit = b.fit()
        assert fit.source == "none"
        assert fit.is_usable is False
        assert b.today_estimate() is None
        assert b.window_summary() is None

    def test_four_days_is_not_enough(self):
        b = EnergyBaseline()
        for i in range(4):
            _feed_day(b, _day(i), 65.0 + 10 + i, kwh=8.0 + i, optimizer_active=False)
        b.force_close_day()
        assert b.fit().source == "none"

    def test_recovers_a_known_cooling_slope(self):
        """Synthetic house at exactly 0.6 kWh per cooling degree-day."""
        b = EnergyBaseline()
        slope = 0.6
        for i in range(8):
            cdd = 5.0 + 3.0 * i
            _feed_day(
                b,
                _day(i),
                mean_outdoor=DEGREE_DAY_BASE_F + cdd,
                kwh=slope * cdd,
                optimizer_active=False,
            )
        b.force_close_day()
        fit = b.fit()
        assert fit.source == "measured"
        assert fit.kwh_per_cdd == pytest.approx(slope, rel=0.02)
        assert fit.cooling_days == 8

    def test_recovers_a_known_heating_slope(self):
        b = EnergyBaseline()
        slope = 0.4
        for i in range(8):
            hdd = 6.0 + 2.5 * i
            _feed_day(
                b,
                _day(i),
                mean_outdoor=DEGREE_DAY_BASE_F - hdd,
                kwh=slope * hdd,
                optimizer_active=False,
            )
        b.force_close_day()
        fit = b.fit()
        assert fit.kwh_per_hdd == pytest.approx(slope, rel=0.02)
        assert fit.kwh_per_cdd == 0.0

    def test_optimized_days_do_not_move_the_baseline(self):
        """The whole point: the baseline must be innocent of the optimizer."""
        b = EnergyBaseline()
        slope = 0.6
        for i in range(8):
            cdd = 5.0 + 3.0 * i
            _feed_day(
                b, _day(i), DEGREE_DAY_BASE_F + cdd, slope * cdd, optimizer_active=False
            )
        baseline_only = b.fit().kwh_per_cdd

        # Now a run of very efficient optimized days
        for i in range(8, 20):
            cdd = 5.0 + 3.0 * (i - 8)
            _feed_day(
                b,
                _day(i),
                DEGREE_DAY_BASE_F + cdd,
                slope * cdd * 0.5,
                optimizer_active=True,
            )
        b.force_close_day()
        assert b.fit().kwh_per_cdd == pytest.approx(baseline_only, rel=1e-6)

    def test_negative_slope_sample_is_clamped_to_zero(self):
        b = EnergyBaseline()
        # Backwards house: more degree-days, less energy. Physically impossible,
        # so the fit must not publish a negative slope.
        for i in range(8):
            cdd = 5.0 + 3.0 * i
            _feed_day(
                b, _day(i), DEGREE_DAY_BASE_F + cdd, 40.0 - cdd, optimizer_active=False
            )
        b.force_close_day()
        assert b.fit().kwh_per_cdd >= 0.0


# ── Savings reporting ───────────────────────────────────────────────


class TestSavingsReporting:
    def _seeded(self) -> EnergyBaseline:
        b = EnergyBaseline()
        slope = 0.6
        for i in range(8):
            cdd = 5.0 + 3.0 * i
            _feed_day(
                b, _day(i), DEGREE_DAY_BASE_F + cdd, slope * cdd, optimizer_active=False
            )
        b.force_close_day()
        return b

    def test_window_summary_measures_a_real_reduction(self):
        b = self._seeded()
        for i in range(8, 18):
            cdd = 10.0
            _feed_day(
                b,
                _day(i),
                DEGREE_DAY_BASE_F + cdd,
                0.6 * cdd * 0.8,  # 20% less than baseline
                optimizer_active=True,
            )
        b.force_close_day()
        summary = b.window_summary()
        assert summary["days"] == 10
        assert summary["saved_pct"] == pytest.approx(20.0, abs=1.0)
        assert summary["source"] == "measured"
        assert summary["measured_fraction"] == pytest.approx(1.0)

    def test_window_summary_can_report_a_loss(self):
        """An honest baseline must be able to say the optimizer lost."""
        b = self._seeded()
        for i in range(8, 18):
            cdd = 10.0
            _feed_day(
                b,
                _day(i),
                DEGREE_DAY_BASE_F + cdd,
                0.6 * cdd * 1.15,
                optimizer_active=True,
            )
        b.force_close_day()
        summary = b.window_summary()
        assert summary["saved_kwh"] < 0
        assert summary["saved_pct"] < 0

    def test_today_estimate_scales_degree_days_by_elapsed_time(self):
        b = self._seeded()
        # Six hours into a hot day
        day = _day(20)
        for i in range(int(6 * 60 / INTERVAL)):
            b.record_interval(
                now=day + timedelta(minutes=INTERVAL * i),
                interval_minutes=INTERVAL,
                power_watts=1000.0,
                outdoor_temp=DEGREE_DAY_BASE_F + 20.0,
                optimizer_active=True,
                power_measured=True,
            )
        est = b.today_estimate()
        assert est["elapsed_fraction"] == pytest.approx(0.25, abs=0.01)
        # 20 CDD scaled to a quarter day = 5 CDD, baseline = 0.6 * 5 = 3.0 kWh
        assert est["baseline_kwh"] == pytest.approx(3.0, rel=0.05)
        assert est["actual_kwh"] == pytest.approx(6.0, rel=0.01)
        assert est["saved_kwh"] < 0

    def test_estimated_power_is_flagged_not_hidden(self):
        b = EnergyBaseline()
        slope = 0.6
        for i in range(8):
            cdd = 5.0 + 3.0 * i
            _feed_day(
                b,
                _day(i),
                DEGREE_DAY_BASE_F + cdd,
                slope * cdd,
                optimizer_active=False,
                power_measured=False,
            )
        for i in range(8, 14):
            _feed_day(
                b,
                _day(i),
                DEGREE_DAY_BASE_F + 10,
                5.0,
                optimizer_active=True,
                power_measured=False,
            )
        b.force_close_day()
        assert b.window_summary()["measured_fraction"] == pytest.approx(0.0)


# ── Profile seeding ─────────────────────────────────────────────────


class TestProfileSeed:
    def test_seed_converts_runtime_per_degree_day_to_kwh(self):
        b = EnergyBaseline()
        # Synthetic profile in the Beestat shape: minutes of runtime per
        # degree-day, per stage.
        ok = b.seed_from_profile(
            runtime_per_degree_day={
                "cool_1": 60.0,
                "cool_2": None,
                "heat_1": 30.0,
                "heat_2": None,
                "auxiliary_heat_1": 6.0,
                "auxiliary_heat_2": None,
            },
            cooling_watts=3000.0,
            heating_watts=3000.0,
            aux_heat_watts=10000.0,
        )
        assert ok is True
        fit = b.fit()
        assert fit.source == "profile_seed"
        # 60 min/CDD at 3 kW = 3.0 kWh/CDD
        assert fit.kwh_per_cdd == pytest.approx(3.0)
        # 30 min at 3 kW + 6 min at 10 kW = 1.5 + 1.0 = 2.5 kWh/HDD
        assert fit.kwh_per_hdd == pytest.approx(2.5)

    def test_measured_days_supersede_the_seed(self):
        b = EnergyBaseline()
        b.seed_from_profile({"cool_1": 60.0}, cooling_watts=3000.0, heating_watts=0.0)
        for i in range(8):
            cdd = 5.0 + 3.0 * i
            _feed_day(
                b, _day(i), DEGREE_DAY_BASE_F + cdd, 0.6 * cdd, optimizer_active=False
            )
        b.force_close_day()
        fit = b.fit()
        assert fit.source == "measured"
        assert fit.kwh_per_cdd == pytest.approx(0.6, rel=0.02)

    def test_empty_profile_is_rejected(self):
        b = EnergyBaseline()
        assert b.seed_from_profile({}, 3000.0, 3000.0) is False
        assert b.seed_from_profile({"cool_1": None}, 3000.0, 3000.0) is False
        assert b.fit().source == "none"


# ── Persistence ─────────────────────────────────────────────────────


class TestPersistence:
    def test_roundtrip_preserves_fit_and_open_day(self):
        b = EnergyBaseline()
        for i in range(8):
            cdd = 5.0 + 3.0 * i
            _feed_day(
                b, _day(i), DEGREE_DAY_BASE_F + cdd, 0.6 * cdd, optimizer_active=False
            )
        # leave day 8 open mid-flight
        _feed_day(b, _day(8), DEGREE_DAY_BASE_F + 10, 6.0, True, coverage=0.5)

        restored = EnergyBaseline.from_dict(b.to_dict())
        assert len(restored.days) == len(b.days)
        assert restored.fit().kwh_per_cdd == pytest.approx(b.fit().kwh_per_cdd, rel=1e-3)
        assert restored.today_partial()["kwh"] == pytest.approx(
            b.today_partial()["kwh"], rel=1e-3
        )

    def test_malformed_records_are_skipped(self):
        restored = EnergyBaseline.from_dict(
            {"days": [{"nope": 1}, None, {"date": "2026-07-01", "kwh": 3.0}]}
        )
        assert len(restored.days) == 1

    def test_day_listener_fires_once_per_closed_day(self):
        b = EnergyBaseline()
        seen = []
        b.add_day_listener(seen.append)
        _feed_day(b, _day(0), 85.0, 10.0, optimizer_active=False)
        _feed_day(b, _day(1), 85.0, 10.0, optimizer_active=False)
        b.force_close_day()
        assert len(seen) == 2
        assert [d.date for d in seen] == ["2026-07-01", "2026-07-02"]

    def test_failing_listener_does_not_break_accounting(self):
        b = EnergyBaseline()

        def boom(_record):
            raise RuntimeError("listener blew up")

        b.add_day_listener(boom)
        _feed_day(b, _day(0), 85.0, 10.0, optimizer_active=False)
        b.force_close_day()
        assert len(b.days) == 1
