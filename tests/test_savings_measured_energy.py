"""Tests for measured-energy grounding in SavingsTracker (item 10.1 and 10.3).

Covers the two claims the savings numbers now rest on: that actual_kwh is an
integration of the power sensor rather than runtime times a rated constant, and
that a number derived from rated watts is labelled as such rather than passed
off as a measurement.
"""

import os
import sys
import types
from datetime import datetime, timedelta, timezone

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

engine = types.ModuleType("custom_components.heatpump_optimizer.engine")
engine.__path__ = [os.path.join(CC, "engine")]
sys.modules.setdefault("custom_components.heatpump_optimizer.engine", engine)

learning = types.ModuleType("custom_components.heatpump_optimizer.learning")
learning.__path__ = [os.path.join(CC, "learning")]
sys.modules.setdefault("custom_components.heatpump_optimizer.learning", learning)

from conftest import load_module  # noqa: E402

dt_mod = load_module(
    "custom_components.heatpump_optimizer.engine.data_types",
    os.path.join(CC, "engine", "data_types.py"),
)
engine.data_types = dt_mod

eb_mod = load_module(
    "custom_components.heatpump_optimizer.learning.energy_baseline",
    os.path.join(CC, "learning", "energy_baseline.py"),
)
learning.energy_baseline = eb_mod

st_mod = load_module(
    "custom_components.heatpump_optimizer.savings_tracker",
    os.path.join(CC, "savings_tracker.py"),
)
ho.savings_tracker = st_mod

SavingsTracker = st_mod.SavingsTracker
EnergyBaseline = eb_mod.EnergyBaseline
DailySavingsReport = dt_mod.DailySavingsReport
HourlySavingsRecord = dt_mod.HourlySavingsRecord

BASE = datetime(2026, 7, 4, 10, 0, tzinfo=timezone.utc)


def _feed(tracker, powers, measured=True, minutes=5.0, start=BASE):
    """Feed one hour of running intervals at the given per-interval powers."""
    for i, watts in enumerate(powers):
        tracker.record_interval(
            now=start + timedelta(minutes=minutes * i),
            hvac_running=True,
            interval_minutes=minutes,
            power_watts=watts,
            carbon_intensity=None,
            electricity_rate=None,
            mode="cool",
            power_measured=measured,
        )
    # Cross the hour boundary to finalize
    tracker.record_interval(
        now=start + timedelta(hours=1),
        hvac_running=False,
        interval_minutes=minutes,
        power_watts=0.0,
        carbon_intensity=None,
        electricity_rate=None,
        mode="off",
        power_measured=measured,
    )
    return tracker._hourly_records[-1]


# ── Energy integration ──────────────────────────────────────────────


class TestMeasuredEnergyIntegration:
    def test_constant_power_matches_the_simple_product(self):
        tracker = SavingsTracker()
        record = _feed(tracker, [2400.0] * 12)
        assert record.actual_runtime_minutes == 60.0
        assert record.actual_kwh == pytest.approx(2.4, rel=1e-6)

    def test_varying_power_is_integrated_not_sampled(self):
        """A ramp must give the true integral, not first or last reading."""
        tracker = SavingsTracker()
        powers = [1000.0 * (i + 1) for i in range(12)]  # 1kW ... 12kW
        record = _feed(tracker, powers)
        expected = sum(powers) * (5.0 / 60.0) / 1000.0
        assert record.actual_kwh == pytest.approx(expected, rel=1e-9)
        assert record.actual_kwh == pytest.approx(6.5, rel=1e-9)

    def test_energy_survives_a_spiky_hour(self):
        tracker = SavingsTracker()
        powers = [0.0, 9000.0, 0.0, 9000.0, 0.0, 9000.0]
        record = _feed(tracker, powers)
        expected = sum(powers) * (5.0 / 60.0) / 1000.0
        assert record.actual_kwh == pytest.approx(expected, rel=1e-9)

    def test_avg_power_stays_consistent_with_the_energy(self):
        tracker = SavingsTracker()
        powers = [1000.0 * (i + 1) for i in range(12)]
        record = _feed(tracker, powers)
        hours = record.actual_runtime_minutes / 60.0
        assert record.power_draw_watts / 1000.0 * hours == pytest.approx(
            record.actual_kwh, rel=1e-9
        )

    def test_missing_readings_extend_at_the_observed_average(self):
        tracker = SavingsTracker()
        for i in range(12):
            tracker.record_interval(
                now=BASE + timedelta(minutes=5 * i),
                hvac_running=True,
                interval_minutes=5.0,
                # Sensor drops out for the second half of the hour
                power_watts=3000.0 if i < 6 else None,
                carbon_intensity=None,
                electricity_rate=None,
                mode="cool",
                power_measured=True,
            )
        tracker.record_interval(
            now=BASE + timedelta(hours=1),
            hvac_running=False,
            interval_minutes=5.0,
            power_watts=0.0,
            carbon_intensity=None,
            electricity_rate=None,
            mode="off",
        )
        record = tracker._hourly_records[-1]
        assert record.actual_runtime_minutes == 60.0
        # 3 kW held across the whole running hour
        assert record.actual_kwh == pytest.approx(3.0, rel=1e-6)


# ── Provenance labelling ────────────────────────────────────────────


class TestEnergyProvenance:
    def test_power_sensor_energy_is_labelled_measured(self):
        tracker = SavingsTracker()
        record = _feed(tracker, [2400.0] * 12, measured=True)
        assert record.energy_source == "measured"

    def test_rated_watts_energy_is_labelled_estimated(self):
        tracker = SavingsTracker()
        record = _feed(tracker, [2400.0] * 12, measured=False)
        assert record.energy_source == "estimated"

    def test_sensor_dropout_produces_mixed(self):
        tracker = SavingsTracker()
        for i in range(12):
            tracker.record_interval(
                now=BASE + timedelta(minutes=5 * i),
                hvac_running=True,
                interval_minutes=5.0,
                power_watts=2400.0,
                carbon_intensity=None,
                electricity_rate=None,
                mode="cool",
                power_measured=(i < 6),
            )
        tracker.record_interval(
            now=BASE + timedelta(hours=1),
            hvac_running=False,
            interval_minutes=5.0,
            power_watts=0.0,
            carbon_intensity=None,
            electricity_rate=None,
            mode="off",
        )
        assert tracker._hourly_records[-1].energy_source == "mixed"

    def test_idle_hour_has_no_energy_source(self):
        tracker = SavingsTracker()
        for i in range(13):
            tracker.record_interval(
                now=BASE + timedelta(minutes=5 * i),
                hvac_running=False,
                interval_minutes=5.0,
                power_watts=0.0,
                carbon_intensity=None,
                electricity_rate=None,
                mode="off",
            )
        assert tracker._hourly_records[-1].energy_source == "none"

    def test_daily_report_rolls_up_provenance(self):
        report = DailySavingsReport(
            date="2026-07-04",
            hours=[
                HourlySavingsRecord(
                    hour=BASE,
                    mode="cool",
                    baseline_runtime_minutes=0.0,
                    actual_runtime_minutes=60.0,
                    energy_source="measured",
                ),
                HourlySavingsRecord(
                    hour=BASE + timedelta(hours=1),
                    mode="cool",
                    baseline_runtime_minutes=0.0,
                    actual_runtime_minutes=60.0,
                    energy_source="estimated",
                ),
            ],
        )
        assert report.energy_source == "mixed"

    def test_default_provenance_is_not_measured(self):
        """A record that never says where its energy came from must not claim
        to be measured."""
        record = HourlySavingsRecord(
            hour=BASE,
            mode="cool",
            baseline_runtime_minutes=0.0,
            actual_runtime_minutes=0.0,
        )
        assert record.energy_source == "none"


# ── Headline vs modeled attribution ─────────────────────────────────


class TestHeadlineVsModeled:
    def test_no_baseline_means_no_headline_number(self):
        tracker = SavingsTracker()
        assert tracker.measured_savings_today() is None
        assert tracker.measured_savings_window() is None

    def test_headline_comes_from_the_energy_baseline(self):
        tracker = SavingsTracker()
        baseline = EnergyBaseline()
        tracker.set_energy_baseline(baseline)

        interval = 5.0
        n = int(1440 / interval)
        day0 = datetime(2026, 7, 1)
        # Five optimizer-off days at 1.0 kWh per cooling degree-day
        for d in range(5):
            cdd = 10.0 + d
            watts = (cdd * 1.0) * 1000.0 / (n * interval / 60.0)
            for i in range(n):
                baseline.record_interval(
                    now=day0 + timedelta(days=d, minutes=interval * i),
                    interval_minutes=interval,
                    power_watts=watts,
                    outdoor_temp=65.0 + cdd,
                    optimizer_active=False,
                    power_measured=True,
                )
        # Then five optimizer-on days using 30% less
        for d in range(5, 10):
            cdd = 12.0
            watts = (cdd * 0.7) * 1000.0 / (n * interval / 60.0)
            for i in range(n):
                baseline.record_interval(
                    now=day0 + timedelta(days=d, minutes=interval * i),
                    interval_minutes=interval,
                    power_watts=watts,
                    outdoor_temp=65.0 + cdd,
                    optimizer_active=True,
                    power_measured=True,
                )
        baseline.force_close_day()

        window = tracker.measured_savings_window()
        assert window is not None
        assert window["days"] == 5
        assert window["saved_pct"] == pytest.approx(30.0, abs=2.0)
        assert window["source"] == "measured"

    def test_modeled_attribution_is_labelled_as_modeled(self):
        tracker = SavingsTracker()
        report = DailySavingsReport(
            date="2026-07-04",
            hours=[
                HourlySavingsRecord(
                    hour=BASE,
                    mode="cool",
                    baseline_runtime_minutes=60.0,
                    actual_runtime_minutes=40.0,
                    baseline_kwh=3.0,
                    actual_kwh=2.0,
                    saved_kwh=1.0,
                    runtime_savings_kwh=0.8,
                    cop_savings_kwh=0.2,
                    energy_source="measured",
                )
            ],
        )
        attribution = tracker.modeled_attribution(report)
        assert attribution["confidence"] == "modeled, not measured"
        assert "modeled_cop_effect_kwh" in attribution
        assert attribution["modeled_cop_effect_kwh"] == pytest.approx(0.2)
        assert "uncertainty" in attribution
        assert attribution["uncertainty"]

    def test_no_emdash_in_user_facing_attribution_text(self):
        tracker = SavingsTracker()
        attribution = tracker.modeled_attribution(
            DailySavingsReport(date="2026-07-04", hours=[])
        )
        for value in attribution.values():
            if isinstance(value, str):
                assert "—" not in value
                assert "–" not in value
