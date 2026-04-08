"""Tests for Layer 2: TacticalController — 5-minute reality check and correction."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

# ── Bootstrap HA stubs (conftest handles this) ──────────────────────

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.conftest import load_module, CC

# Load the data_types module first (dependency)
data_types = load_module(
    "custom_components.heatpump_optimizer.engine.data_types",
    os.path.join(CC, "engine", "data_types.py"),
)
ScheduleEntry = data_types.ScheduleEntry
OptimizedSchedule = data_types.OptimizedSchedule
SimulationPoint = data_types.SimulationPoint

# Load the tactical module under test
tactical_mod = load_module(
    "custom_components.heatpump_optimizer.controllers.tactical",
    os.path.join(CC, "controllers", "tactical.py"),
)
TacticalController = tactical_mod.TacticalController
TacticalState = tactical_mod.TacticalState
TacticalResult = tactical_mod.TacticalResult
SMALL_ERROR_F = tactical_mod.SMALL_ERROR_F
LARGE_ERROR_F = tactical_mod.LARGE_ERROR_F
CORRECTION_DAMPING = tactical_mod.CORRECTION_DAMPING
MAX_CORRECTION_F = tactical_mod.MAX_CORRECTION_F
DISTURBED_RECOVERY_MINUTES = tactical_mod.DISTURBED_RECOVERY_MINUTES
DISTURBED_RECOVERY_THRESHOLD_F = tactical_mod.DISTURBED_RECOVERY_THRESHOLD_F
DISTURBED_MAX_HOURS = tactical_mod.DISTURBED_MAX_HOURS


# ── Helpers ─────────────────────────────────────────────────────────

NOW = datetime(2026, 4, 7, 12, 0, 0, tzinfo=timezone.utc)


def make_entry(target=72.0, mode="cool"):
    return ScheduleEntry(
        start_time=NOW - timedelta(hours=1),
        end_time=NOW + timedelta(hours=1),
        target_temp=target,
        mode=mode,
    )


def make_schedule(predicted_temp, time=None):
    t = time or NOW
    sim = [SimulationPoint(
        time=t,
        indoor_temp=predicted_temp,
        outdoor_temp=90.0,
        hvac_running=True,
        cumulative_runtime_minutes=30.0,
    )]
    return OptimizedSchedule(
        entries=[make_entry()],
        baseline_runtime_minutes=60.0,
        optimized_runtime_minutes=40.0,
        savings_pct=33.3,
        simulation=sim,
    )


# ── Nominal operation ──────────────────────────────────────────────


class TestNominalOperation:
    def test_on_track_small_error(self):
        """Error within 1F: stay nominal, no correction."""
        tc = TacticalController()
        result = tc.evaluate(72.3, make_schedule(72.0), make_entry(), now=NOW)
        assert result.state == TacticalState.NOMINAL
        assert result.should_write_setpoint is False
        assert result.setpoint_correction == 0.0
        assert abs(result.error - 0.3) < 0.01

    def test_exact_match(self):
        """Predicted == actual: nominal."""
        tc = TacticalController()
        result = tc.evaluate(72.0, make_schedule(72.0), make_entry(), now=NOW)
        assert result.state == TacticalState.NOMINAL
        assert result.error == 0.0

    def test_negative_small_error(self):
        """Actual slightly below predicted: still nominal."""
        tc = TacticalController()
        result = tc.evaluate(71.5, make_schedule(72.0), make_entry(), now=NOW)
        assert result.state == TacticalState.NOMINAL
        assert result.error == pytest.approx(-0.5)

    def test_boundary_exactly_1f(self):
        """Error of exactly 1.0F: still nominal (threshold is >1.0)."""
        tc = TacticalController()
        result = tc.evaluate(73.0, make_schedule(72.0), make_entry(), now=NOW)
        assert result.state == TacticalState.NOMINAL


# ── Correction ─────────────────────────────────────────────────────


class TestCorrection:
    def test_moderate_error_triggers_correction(self):
        """Error > 1F: enter CORRECTING, write setpoint."""
        tc = TacticalController()
        result = tc.evaluate(73.5, make_schedule(72.0), make_entry(72.0), now=NOW)
        assert result.state == TacticalState.CORRECTING
        assert result.should_write_setpoint is True
        # Error = +1.5, correction = -1.5 * 0.5 = -0.75
        assert result.setpoint_correction == pytest.approx(-0.75)
        assert result.corrected_setpoint == pytest.approx(72.0 - 0.75)

    def test_correction_damping(self):
        """Correction is damped by CORRECTION_DAMPING (0.5)."""
        tc = TacticalController()
        result = tc.evaluate(74.0, make_schedule(72.0), make_entry(72.0), now=NOW)
        # Error = +2.0, raw correction = -2.0 * 0.5 = -1.0
        assert result.setpoint_correction == pytest.approx(-1.0)

    def test_correction_capped_at_max(self):
        """Correction is capped at MAX_CORRECTION_F even with large error."""
        tc = TacticalController()
        # Error = 2.5 (just under LARGE_ERROR threshold)
        result = tc.evaluate(74.5, make_schedule(72.0), make_entry(72.0), now=NOW)
        # raw correction = -2.5 * 0.5 = -1.25, within cap of 2.0
        assert abs(result.setpoint_correction) <= MAX_CORRECTION_F

    def test_heating_correction_direction(self):
        """In heating mode, cooler-than-predicted raises setpoint."""
        tc = TacticalController()
        # Actual 66, predicted 68 => error = -2.0
        result = tc.evaluate(66.0, make_schedule(68.0), make_entry(68.0, "heat"), now=NOW)
        assert result.state == TacticalState.CORRECTING
        # correction = -(-2.0) * 0.5 = +1.0 (raises setpoint)
        assert result.setpoint_correction == pytest.approx(1.0)

    def test_negative_moderate_error(self):
        """Actual below predicted by > 1F: correction in positive direction."""
        tc = TacticalController()
        result = tc.evaluate(70.5, make_schedule(72.0), make_entry(72.0), now=NOW)
        assert result.state == TacticalState.CORRECTING
        assert result.setpoint_correction > 0  # raise setpoint


# ── Disturbed state ────────────────────────────────────────────────


class TestDisturbedState:
    def test_large_error_enters_disturbed(self):
        """Error > 3F: enter DISTURBED, no setpoint write."""
        tc = TacticalController()
        result = tc.evaluate(75.5, make_schedule(72.0), make_entry(), now=NOW)
        assert result.state == TacticalState.DISTURBED
        assert result.should_write_setpoint is False
        assert result.setpoint_correction == 0.0

    def test_disturbed_stays_disturbed_with_large_error(self):
        """Already disturbed and error still large: remain disturbed."""
        tc = TacticalController()
        tc.evaluate(75.5, make_schedule(72.0), make_entry(), now=NOW)
        result = tc.evaluate(
            75.0, make_schedule(72.0), make_entry(),
            now=NOW + timedelta(minutes=5),
        )
        assert result.state == TacticalState.DISTURBED

    def test_disturbed_recovery_after_sustained_small_errors(self):
        """Recover from disturbed when errors stay below threshold for enough time."""
        tc = TacticalController()
        # Enter disturbed
        tc.evaluate(76.0, make_schedule(72.0), make_entry(), now=NOW)
        assert tc.state == TacticalState.DISTURBED

        # Feed small errors for DISTURBED_RECOVERY_MINUTES + 1
        recovered = False
        for i in range(int(DISTURBED_RECOVERY_MINUTES) + 2):
            t = NOW + timedelta(minutes=i + 1)
            result = tc.evaluate(72.5, make_schedule(72.0), make_entry(), now=t)
            if tc.state == TacticalState.NOMINAL and not recovered:
                recovered = True
                # The recovery evaluation should offer to write the scheduled setpoint
                assert result.should_write_setpoint is True
                break

        assert recovered, "Controller did not recover from disturbed state"
        assert tc.state == TacticalState.NOMINAL

    def test_no_premature_recovery(self):
        """One small error followed by large: stay disturbed."""
        tc = TacticalController()
        tc.evaluate(76.0, make_schedule(72.0), make_entry(), now=NOW)

        # One small error
        tc.evaluate(72.5, make_schedule(72.0), make_entry(), now=NOW + timedelta(minutes=1))
        # Then a large error again
        result = tc.evaluate(
            75.0, make_schedule(72.0), make_entry(),
            now=NOW + timedelta(minutes=2),
        )
        assert result.state == TacticalState.DISTURBED

    def test_disturbed_timeout_recovery(self):
        """Auto-recover after DISTURBED_MAX_HOURS with reoptimization flag."""
        tc = TacticalController()
        tc.evaluate(76.0, make_schedule(72.0), make_entry(), now=NOW)
        assert tc.state == TacticalState.DISTURBED

        # Jump past the timeout
        result = tc.evaluate(
            74.5, make_schedule(72.0), make_entry(),
            now=NOW + timedelta(hours=DISTURBED_MAX_HOURS + 0.1),
        )
        assert result.state == TacticalState.NOMINAL
        assert tc.needs_reoptimization is True
        assert result.should_write_setpoint is True

    def test_clear_reoptimization_flag(self):
        """Flag clears after coordinator reads it."""
        tc = TacticalController()
        tc.evaluate(76.0, make_schedule(72.0), make_entry(), now=NOW)
        tc.evaluate(
            74.5, make_schedule(72.0), make_entry(),
            now=NOW + timedelta(hours=DISTURBED_MAX_HOURS + 0.1),
        )
        assert tc.needs_reoptimization is True
        tc.clear_reoptimization_flag()
        assert tc.needs_reoptimization is False


# ── No prediction / no entry ───────────────────────────────────────


class TestMissingData:
    def test_no_schedule(self):
        """No schedule: return gracefully, no correction."""
        tc = TacticalController()
        result = tc.evaluate(72.0, None, make_entry(), now=NOW)
        assert result.should_write_setpoint is False
        assert result.error == 0.0
        assert "No prediction" in result.reason

    def test_no_current_entry(self):
        """No current entry: return gracefully."""
        tc = TacticalController()
        result = tc.evaluate(72.0, make_schedule(72.0), None, now=NOW)
        assert result.should_write_setpoint is False
        assert result.error == 0.0

    def test_empty_simulation(self):
        """Schedule with no simulation points."""
        tc = TacticalController()
        schedule = OptimizedSchedule(
            entries=[], baseline_runtime_minutes=0,
            optimized_runtime_minutes=0, savings_pct=0,
        )
        result = tc.evaluate(72.0, schedule, make_entry(), now=NOW)
        assert result.should_write_setpoint is False


# ── Apparent temperature ───────────────────────────────────────────


class TestApparentTemperature:
    def test_cooling_uses_worse_value(self):
        """In cooling mode, use max(actual, apparent) for error."""
        tc = TacticalController()
        # Actual 73, apparent 75.5 (humid -> feels warmer) => effective = 75.5
        result = tc.evaluate(
            73.0, make_schedule(72.0), make_entry(72.0, "cool"),
            now=NOW, apparent_temp=75.5,
        )
        # Error = 75.5 - 72 = 3.5, which is > LARGE_ERROR_F (3.0)
        assert result.state == TacticalState.DISTURBED
        assert result.apparent_temp == 75.5

    def test_heating_uses_worse_value(self):
        """In heating mode, use min(actual, apparent) for error."""
        tc = TacticalController()
        # Actual 68, apparent 66 (dry -> feels cooler) => effective = 66
        result = tc.evaluate(
            68.0, make_schedule(68.0), make_entry(68.0, "heat"),
            now=NOW, apparent_temp=66.0,
        )
        # Error = 66 - 68 = -2.0, |error| > SMALL_ERROR_F
        assert result.state == TacticalState.CORRECTING

    def test_apparent_none_uses_actual(self):
        """No apparent temp: use actual directly."""
        tc = TacticalController()
        result = tc.evaluate(
            72.3, make_schedule(72.0), make_entry(), now=NOW, apparent_temp=None,
        )
        assert result.state == TacticalState.NOMINAL
        assert result.apparent_temp is None

    def test_cooling_apparent_lower_than_actual(self):
        """In cooling, if apparent < actual, use actual (it's the worse one)."""
        tc = TacticalController()
        # Actual 73.5 > apparent 72.5 => effective = 73.5
        result = tc.evaluate(
            73.5, make_schedule(72.0), make_entry(72.0, "cool"),
            now=NOW, apparent_temp=72.5,
        )
        assert result.error == pytest.approx(1.5)


# ── Error history ──────────────────────────────────────────────────


class TestErrorHistory:
    def test_error_recorded(self):
        tc = TacticalController()
        tc.evaluate(72.5, make_schedule(72.0), make_entry(), now=NOW)
        assert len(tc.error_history) == 1
        assert tc.error_history[0][1] == pytest.approx(0.5)

    def test_error_history_trimmed_at_24h(self):
        tc = TacticalController()
        # Add an old error
        old_time = NOW - timedelta(hours=25)
        tc.evaluate(72.5, make_schedule(72.0), make_entry(), now=old_time)
        # Add a new error, which should trigger trimming
        tc.evaluate(72.5, make_schedule(72.0), make_entry(), now=NOW)
        assert len(tc.error_history) == 1
        assert tc.error_history[0][0] == NOW

    def test_mean_absolute_error(self):
        tc = TacticalController()
        tc.evaluate(73.0, make_schedule(72.0), make_entry(), now=NOW)
        tc.evaluate(71.0, make_schedule(72.0), make_entry(), now=NOW + timedelta(minutes=5))
        assert tc.mean_absolute_error == pytest.approx(1.0)

    def test_mean_signed_error_detects_bias(self):
        tc = TacticalController()
        # All errors positive: warm bias
        for i in range(5):
            tc.evaluate(
                73.0, make_schedule(72.0), make_entry(),
                now=NOW + timedelta(minutes=i),
            )
        assert tc.mean_signed_error > 0

    def test_mae_none_when_empty(self):
        tc = TacticalController()
        assert tc.mean_absolute_error is None
        assert tc.mean_signed_error is None

    def test_get_recent_errors(self):
        """get_recent_errors uses datetime.now() internally, so use real time."""
        tc = TacticalController()
        real_now = datetime.now(timezone.utc)
        tc.evaluate(73.0, make_schedule(72.0), make_entry(), now=real_now)
        tc.evaluate(
            74.0, make_schedule(72.0), make_entry(),
            now=real_now + timedelta(minutes=1),
        )
        # Both should be within 60 min window
        recent = tc.get_recent_errors(minutes=60)
        assert len(recent) == 2


# ── State property ─────────────────────────────────────────────────


class TestStateProperty:
    def test_initial_state_nominal(self):
        tc = TacticalController()
        assert tc.state == TacticalState.NOMINAL

    def test_state_transitions(self):
        tc = TacticalController()
        # Nominal -> Correcting
        tc.evaluate(73.5, make_schedule(72.0), make_entry(), now=NOW)
        assert tc.state == TacticalState.CORRECTING
        # Back to nominal with small error
        tc.evaluate(72.3, make_schedule(72.0), make_entry(), now=NOW + timedelta(minutes=5))
        assert tc.state == TacticalState.NOMINAL


# ── Simulation point lookup ────────────────────────────────────────


class TestFindPredictedTemp:
    def test_closest_point_selected(self):
        """When multiple sim points, picks the one closest to 'now'."""
        sim = [
            SimulationPoint(
                time=NOW - timedelta(minutes=10),
                indoor_temp=71.0, outdoor_temp=90.0,
                hvac_running=True, cumulative_runtime_minutes=10.0,
            ),
            SimulationPoint(
                time=NOW + timedelta(minutes=2),
                indoor_temp=72.5, outdoor_temp=90.0,
                hvac_running=True, cumulative_runtime_minutes=30.0,
            ),
            SimulationPoint(
                time=NOW + timedelta(minutes=15),
                indoor_temp=73.0, outdoor_temp=90.0,
                hvac_running=True, cumulative_runtime_minutes=45.0,
            ),
        ]
        schedule = OptimizedSchedule(
            entries=[], baseline_runtime_minutes=60.0,
            optimized_runtime_minutes=40.0, savings_pct=33.0,
            simulation=sim,
        )
        tc = TacticalController()
        result = tc.evaluate(72.5, schedule, make_entry(), now=NOW)
        # Closest point is 2 min away -> predicted = 72.5
        assert result.predicted_temp == pytest.approx(72.5)
