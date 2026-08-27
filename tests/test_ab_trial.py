"""Tests for the A/B validation trial (item 10.4).

The trial is the only thing in this integration that can falsify its own
savings claim, so these tests care most about the ways it could quietly
stop being able to: contaminated populations, phantom verdicts from tiny
samples, and days counted by intent rather than by what actually happened.
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

ab_mod = load_module(
    "custom_components.heatpump_optimizer.learning.ab_trial",
    os.path.join(CC, "learning", "ab_trial.py"),
)
learning.ab_trial = ab_mod

ABTrial = ab_mod.ABTrial
EnergyBaseline = eb_mod.EnergyBaseline
DayEnergyRecord = eb_mod.DayEnergyRecord

START = datetime(2026, 7, 1, 0, 0)


def _record(
    day_offset: int,
    kwh: float,
    cdd: float = 10.0,
    hdd: float = 0.0,
    optimizer_fraction: float = 1.0,
    covered_minutes: float = 1440.0,
) -> DayEnergyRecord:
    day = (START + timedelta(days=day_offset)).strftime("%Y-%m-%d")
    return DayEnergyRecord(
        date=day,
        kwh=kwh,
        mean_outdoor_f=65.0 + cdd - hdd,
        cdd=cdd,
        hdd=hdd,
        covered_minutes=covered_minutes,
        temp_covered_minutes=covered_minutes,
        optimizer_fraction=optimizer_fraction,
        measured_fraction=1.0,
    )


# ── Scheduling ──────────────────────────────────────────────────────


class TestScheduling:
    def test_inactive_trial_has_no_opinion(self):
        trial = ABTrial()
        assert trial.should_optimize(START) is None
        assert trial.current_phase(START) is None

    def test_blocks_alternate_on_then_off(self):
        trial = ABTrial()
        trial.start(START, period_days=3, total_days=12)
        phases = [
            trial.current_phase(START + timedelta(days=d)) for d in range(12)
        ]
        assert phases[0:3] == ["on"] * 3
        assert phases[3:6] == ["off"] * 3
        assert phases[6:9] == ["on"] * 3
        assert phases[9:12] == ["off"] * 3

    def test_should_optimize_tracks_phase(self):
        trial = ABTrial()
        trial.start(START, period_days=2, total_days=8)
        assert trial.should_optimize(START) is True
        assert trial.should_optimize(START + timedelta(days=2)) is False
        assert trial.should_optimize(START + timedelta(days=4)) is True

    def test_single_day_blocks_alternate_daily(self):
        trial = ABTrial()
        trial.start(START, period_days=1, total_days=4)
        assert [
            trial.should_optimize(START + timedelta(days=d)) for d in range(4)
        ] == [True, False, True, False]

    def test_expires_after_total_days(self):
        trial = ABTrial()
        trial.start(START, period_days=3, total_days=6)
        assert trial.check_expiry(START + timedelta(days=5)) is False
        assert trial.check_expiry(START + timedelta(days=6)) is True
        assert trial.is_active is False
        assert trial.result()["completed"] is True

    def test_stop_is_idempotent(self):
        trial = ABTrial()
        trial.start(START)
        trial.stop()
        trial.stop()
        assert trial.is_active is False

    def test_out_of_range_parameters_are_clamped(self):
        trial = ABTrial()
        trial.start(START, period_days=999, total_days=99999)
        assert trial.period_days == ab_mod.MAX_PERIOD_DAYS
        assert trial.total_days == ab_mod.MAX_TOTAL_DAYS
        trial.start(START, period_days=0, total_days=0)
        assert trial.period_days == ab_mod.MIN_PERIOD_DAYS
        assert trial.total_days == ab_mod.MIN_TOTAL_DAYS


class TestPauseAction:
    """The trial drives pause/resume, but never fights the user."""

    def test_no_action_when_inactive(self):
        trial = ABTrial()
        assert trial.pause_action(START, currently_paused=False, trial_paused=False) is None

    def test_pauses_when_entering_an_off_block(self):
        trial = ABTrial()
        trial.start(START, period_days=2, total_days=8)
        at = START + timedelta(days=2)
        assert trial.pause_action(at, currently_paused=False, trial_paused=False) == "pause"

    def test_resumes_its_own_pause_when_the_on_block_returns(self):
        trial = ABTrial()
        trial.start(START, period_days=2, total_days=8)
        at = START + timedelta(days=4)
        assert trial.pause_action(at, currently_paused=True, trial_paused=True) == "resume"

    def test_does_not_resume_a_pause_the_user_asked_for(self):
        trial = ABTrial()
        trial.start(START, period_days=2, total_days=8)
        at = START + timedelta(days=4)
        assert trial.pause_action(at, currently_paused=True, trial_paused=False) is None

    def test_no_repeat_pause_while_already_off(self):
        trial = ABTrial()
        trial.start(START, period_days=2, total_days=8)
        at = START + timedelta(days=2)
        assert trial.pause_action(at, currently_paused=True, trial_paused=True) is None

    def test_no_action_during_a_normal_on_block(self):
        trial = ABTrial()
        trial.start(START, period_days=2, total_days=8)
        assert trial.pause_action(START, currently_paused=False, trial_paused=False) is None

    def test_expired_trial_stops_asking(self):
        trial = ABTrial()
        trial.start(START, period_days=2, total_days=4)
        at = START + timedelta(days=10)
        assert trial.pause_action(at, currently_paused=False, trial_paused=False) is None


# ── Population assignment ───────────────────────────────────────────


class TestPopulations:
    def test_days_land_in_the_arm_they_actually_ran_in(self):
        trial = ABTrial()
        trial.start(START, period_days=2, total_days=8)
        trial.record_day(_record(0, kwh=10.0, optimizer_fraction=1.0))
        trial.record_day(_record(2, kwh=12.0, optimizer_fraction=0.0))
        result = trial.result()
        assert result["optimizer_on_days"] == 1
        assert result["optimizer_off_days"] == 1

    def test_mixed_control_day_is_discarded(self):
        """A day the user took over is not evidence about either arm."""
        trial = ABTrial()
        trial.start(START, period_days=2, total_days=8)
        trial.record_day(_record(0, kwh=10.0, optimizer_fraction=0.5))
        assert trial.result()["optimizer_on_days"] == 0
        assert trial.result()["optimizer_off_days"] == 0

    def test_scheduled_arm_does_not_override_what_happened(self):
        """Day 0 is scheduled 'on', but the optimizer never controlled it."""
        trial = ABTrial()
        trial.start(START, period_days=3, total_days=9)
        trial.record_day(_record(0, kwh=10.0, optimizer_fraction=0.0))
        assert trial.result()["optimizer_off_days"] == 1
        assert trial.result()["optimizer_on_days"] == 0

    def test_poorly_covered_day_is_discarded(self):
        trial = ABTrial()
        trial.start(START, period_days=2, total_days=8)
        trial.record_day(_record(0, kwh=10.0, covered_minutes=600.0))
        assert trial.result()["optimizer_on_days"] == 0

    def test_mild_day_carries_no_degree_day_signal(self):
        trial = ABTrial()
        trial.start(START, period_days=2, total_days=8)
        trial.record_day(_record(0, kwh=2.0, cdd=0.2, hdd=0.0))
        assert trial.result()["optimizer_on_days"] == 0

    def test_days_outside_the_window_are_ignored(self):
        trial = ABTrial()
        trial.start(START, period_days=2, total_days=4)
        trial.record_day(_record(-1, kwh=10.0))
        trial.record_day(_record(10, kwh=10.0))
        assert trial.result()["optimizer_on_days"] == 0

    def test_duplicate_dates_are_not_double_counted(self):
        trial = ABTrial()
        trial.start(START, period_days=2, total_days=8)
        trial.record_day(_record(0, kwh=10.0))
        trial.record_day(_record(0, kwh=10.0))
        assert trial.result()["optimizer_on_days"] == 1

    def test_no_start_date_means_nothing_recorded(self):
        trial = ABTrial()
        trial.record_day(_record(0, kwh=10.0))
        assert trial.result()["optimizer_on_days"] == 0


# ── Verdict ─────────────────────────────────────────────────────────


class TestVerdict:
    def _run(self, on_kwh, off_kwh, period_days=1):
        trial = ABTrial()
        total = len(on_kwh) + len(off_kwh)
        trial.start(START, period_days=period_days, total_days=total + 2)
        offset = 0
        for kwh in on_kwh:
            trial.record_day(_record(offset, kwh=kwh, optimizer_fraction=1.0))
            offset += 1
        for kwh in off_kwh:
            trial.record_day(_record(offset, kwh=kwh, optimizer_fraction=0.0))
            offset += 1
        return trial.result()

    def test_one_day_each_is_never_conclusive(self):
        result = self._run([5.0], [10.0])
        assert result["conclusive"] is False
        assert result["verdict"] == "not enough data yet"

    def test_clear_saving_is_reported_as_a_saving(self):
        result = self._run(
            on_kwh=[8.0, 8.1, 7.9, 8.05, 8.0],
            off_kwh=[10.0, 10.1, 9.9, 10.05, 10.0],
        )
        assert result["conclusive"] is True
        assert result["verdict"] == "optimizer used less energy per degree-day"
        assert result["difference_pct"] == pytest.approx(20.0, abs=1.0)

    def test_clear_loss_is_reported_as_a_loss(self):
        """An honest test must be able to say the optimizer made it worse."""
        result = self._run(
            on_kwh=[12.0, 12.1, 11.9, 12.05, 12.0],
            off_kwh=[10.0, 10.1, 9.9, 10.05, 10.0],
        )
        assert result["conclusive"] is True
        assert result["verdict"] == "optimizer used more energy per degree-day"
        assert result["difference_kwh_per_degree_day"] < 0

    def test_noisy_wash_is_not_called_a_win(self):
        result = self._run(
            on_kwh=[10.0, 6.0, 14.0, 8.0, 12.0],
            off_kwh=[10.5, 14.0, 6.5, 12.0, 8.0],
        )
        assert result["conclusive"] is False
        assert result["verdict"] == "no measurable difference so far"

    def test_intensity_normalizes_away_weather(self):
        """A hotter off-block must not read as savings on its own."""
        trial = ABTrial()
        trial.start(START, period_days=1, total_days=12)
        # Same 1.0 kWh per degree-day on both arms, very different weather
        for i in range(5):
            trial.record_day(
                _record(i, kwh=10.0, cdd=10.0, optimizer_fraction=1.0)
            )
        for i in range(5, 10):
            trial.record_day(
                _record(i, kwh=25.0, cdd=25.0, optimizer_fraction=0.0)
            )
        result = trial.result()
        assert result["difference_kwh_per_degree_day"] == pytest.approx(0.0, abs=1e-6)
        assert result["conclusive"] is False


# ── Integration with the energy baseline ────────────────────────────


class TestBaselineIntegration:
    def test_trial_consumes_the_baseline_day_stream(self):
        baseline = EnergyBaseline()
        trial = ABTrial()
        trial.start(START, period_days=1, total_days=6)
        baseline.add_day_listener(trial.record_day)

        interval = 5.0
        n = int(1440 / interval)
        for d in range(4):
            day = START + timedelta(days=d)
            optimizer_active = d % 2 == 0
            watts = 8000.0 if optimizer_active else 10000.0
            for i in range(n):
                baseline.record_interval(
                    now=day + timedelta(minutes=interval * i),
                    interval_minutes=interval,
                    power_watts=watts,
                    outdoor_temp=85.0,
                    optimizer_active=optimizer_active,
                    power_measured=True,
                )
        baseline.force_close_day()

        result = trial.result()
        assert result["optimizer_on_days"] == 2
        assert result["optimizer_off_days"] == 2
        assert (
            result["optimizer_on_kwh_per_degree_day"]
            < result["optimizer_off_kwh_per_degree_day"]
        )


# ── Persistence ─────────────────────────────────────────────────────


class TestPersistence:
    def test_roundtrip_preserves_schedule_and_days(self):
        trial = ABTrial()
        trial.start(START, period_days=2, total_days=10)
        trial.record_day(_record(0, kwh=10.0, optimizer_fraction=1.0))
        trial.record_day(_record(2, kwh=12.0, optimizer_fraction=0.0))

        restored = ABTrial.from_dict(trial.to_dict())
        assert restored.is_active is True
        assert restored.period_days == 2
        assert restored.total_days == 10
        assert restored.current_phase(START + timedelta(days=2)) == "off"
        assert restored.result()["optimizer_on_days"] == 1
        assert restored.result()["optimizer_off_days"] == 1

    def test_corrupt_start_date_deactivates_rather_than_crashing(self):
        restored = ABTrial.from_dict({"active": True, "start_date": "not-a-date"})
        assert restored.is_active is False
        assert restored.should_optimize(START) is None

    def test_malformed_day_records_are_skipped(self):
        restored = ABTrial.from_dict(
            {
                "active": False,
                "start_date": "2026-07-01",
                "days": [{"bogus": True}, None],
            }
        )
        assert restored.days() == []
