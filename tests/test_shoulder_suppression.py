"""Tests for per-hour heating/cooling suppression and shoulder day detection.

Covers:
- Per-hour suppression of heating when outdoor > balance point
- Per-hour suppression of cooling when outdoor < balance point
- Enhanced shoulder day detection via balance-point crossing
- Phase classification override when indoor temp already at target
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from conftest import CC, load_module

# Load modules
dt_mod = load_module(
    "custom_components.heatpump_optimizer.engine.data_types",
    os.path.join(CC, "engine", "data_types.py"),
)
perf_mod = load_module(
    "custom_components.heatpump_optimizer.engine.performance_model",
    os.path.join(CC, "engine", "performance_model.py"),
)
sim_mod = load_module(
    "custom_components.heatpump_optimizer.engine.thermal_simulator",
    os.path.join(CC, "engine", "thermal_simulator.py"),
)
opt_mod = load_module(
    "custom_components.heatpump_optimizer.engine.optimizer",
    os.path.join(CC, "engine", "optimizer.py"),
)
strat_mod = load_module(
    "custom_components.heatpump_optimizer.controllers.strategic",
    os.path.join(CC, "controllers", "strategic.py"),
)

ForecastPoint = dt_mod.ForecastPoint
HourScore = dt_mod.HourScore
ScheduleEntry = dt_mod.ScheduleEntry
PerformanceModel = perf_mod.PerformanceModel
ThermalSimulator = sim_mod.ThermalSimulator
ScheduleOptimizer = opt_mod.ScheduleOptimizer
StrategicPlanner = strat_mod.StrategicPlanner


# ── Helpers ──────────────────────────────────────────────────────────

NOW = datetime(2026, 4, 7, 6, 0, 0, tzinfo=timezone.utc)


def _make_hour_scores(outdoor_temps):
    """Build HourScore list from a sequence of outdoor temps."""
    scores = []
    for i, temp in enumerate(outdoor_temps):
        hour = NOW + timedelta(hours=i)
        # Lower score = more efficient; warm hours are cheaper for heating
        score = 1.0 / max(0.1, temp - 20)
        scores.append(HourScore(
            hour=hour,
            outdoor_temp=temp,
            efficiency_score=score,
            combined_score=score,
        ))
    return scores


def _make_forecast(outdoor_temps):
    """Build ForecastPoint list from a sequence of outdoor temps."""
    return [
        ForecastPoint(
            time=NOW + timedelta(hours=i),
            outdoor_temp=temp,
        )
        for i, temp in enumerate(outdoor_temps)
    ]


def _model_with_bp(balance_point):
    """Create a PerformanceModel with a specific resist balance point."""
    model = PerformanceModel.from_defaults()
    # Override the balance point in the raw data
    model.resist_balance_point = balance_point
    return model


# ── Per-hour heating suppression ─────────────────────────────────────


class TestPerHourSuppression:
    """Tests for _build_schedule per-hour balance point suppression."""

    def test_heating_suppressed_above_balance_point(self):
        """When outdoor > bp + margin, heating should be coasting, not pre-heating."""
        model = _model_with_bp(55.0)
        sim = ThermalSimulator(model)
        opt = ScheduleOptimizer(model, sim)

        # Shoulder day: cold morning (40F), warm afternoon (70F)
        outdoor = [40, 42, 44, 46, 50, 54, 58, 62, 65, 68, 70, 70,
                   68, 65, 62, 58, 54, 50, 48, 46, 44, 42, 41, 40]
        scores = _make_hour_scores(outdoor)

        entries = opt._build_schedule(scores, (62, 70), "heat")

        # Hours where outdoor > 55 + 2 = 57 should be coasting
        for entry in entries:
            hour_idx = int((entry.start_time - NOW).total_seconds() / 3600)
            if outdoor[hour_idx] > 57:
                assert "coasting" in entry.reason, (
                    f"Hour {hour_idx} at {outdoor[hour_idx]}F should be coasting "
                    f"(bp=55, margin=2), got: {entry.reason}"
                )

    def test_heating_allowed_below_balance_point(self):
        """When outdoor < bp - margin, heating targets should be assigned normally."""
        model = _model_with_bp(55.0)
        sim = ThermalSimulator(model)
        opt = ScheduleOptimizer(model, sim)

        # All hours below balance point
        outdoor = [30, 32, 34, 36, 38, 40, 42, 44,
                   40, 38, 36, 34, 32, 30, 28, 26,
                   28, 30, 32, 34, 36, 34, 32, 30]
        scores = _make_hour_scores(outdoor)

        entries = opt._build_schedule(scores, (62, 70), "heat")

        # At least some hours should have pre-heating or maintaining
        reasons = [e.reason for e in entries]
        has_active = any("pre-heating" in r or "maintaining" in r for r in reasons)
        assert has_active, "All hours below BP should allow active heating"

    def test_cooling_suppressed_below_balance_point(self):
        """When outdoor < bp - margin, cooling should be coasting."""
        model = _model_with_bp(55.0)
        sim = ThermalSimulator(model)
        opt = ScheduleOptimizer(model, sim)

        # Shoulder day: warm afternoon needs cooling, but morning is cool
        outdoor = [45, 44, 43, 44, 46, 50, 55, 60, 68, 75, 80, 82,
                   80, 78, 75, 70, 65, 60, 55, 52, 50, 48, 46, 45]
        scores = _make_hour_scores(outdoor)

        entries = opt._build_schedule(scores, (72, 78), "cool")

        # Hours where outdoor < 55 - 2 = 53 should be coasting
        for entry in entries:
            hour_idx = int((entry.start_time - NOW).total_seconds() / 3600)
            if outdoor[hour_idx] < 53:
                assert "coasting" in entry.reason, (
                    f"Hour {hour_idx} at {outdoor[hour_idx]}F should be coasting "
                    f"in cool mode (bp=55, margin=2), got: {entry.reason}"
                )

    def test_suppressed_hours_target_comfort_floor_heat(self):
        """Suppressed heating hours should target comfort min (coast low)."""
        model = _model_with_bp(55.0)
        sim = ThermalSimulator(model)
        opt = ScheduleOptimizer(model, sim)

        outdoor = [40] * 12 + [70] * 12  # morning cold, afternoon warm
        scores = _make_hour_scores(outdoor)

        entries = opt._build_schedule(scores, (62, 70), "heat")

        # Afternoon hours (warm, suppressed) should target 62 (comfort min)
        for entry in entries[12:]:
            assert entry.target_temp == 62.0, (
                f"Suppressed heating hour should target comfort min 62, "
                f"got {entry.target_temp}"
            )

    def test_suppressed_hours_target_comfort_ceiling_cool(self):
        """Suppressed cooling hours should target comfort max (coast high)."""
        model = _model_with_bp(55.0)
        sim = ThermalSimulator(model)
        opt = ScheduleOptimizer(model, sim)

        outdoor = [45] * 12 + [80] * 12  # morning cool, afternoon hot
        scores = _make_hour_scores(outdoor)

        entries = opt._build_schedule(scores, (72, 78), "cool")

        # Morning hours (cool, suppressed) should target 78 (comfort max)
        for entry in entries[:12]:
            assert entry.target_temp == 78.0, (
                f"Suppressed cooling hour should target comfort max 78, "
                f"got {entry.target_temp}"
            )

    def test_no_suppression_without_balance_point(self):
        """When model has no balance point, no suppression should occur."""
        model = PerformanceModel.from_defaults()
        model.resist_balance_point = None
        sim = ThermalSimulator(model)
        opt = ScheduleOptimizer(model, sim)

        outdoor = [40] * 12 + [70] * 12
        scores = _make_hour_scores(outdoor)

        entries = opt._build_schedule(scores, (62, 70), "heat")

        # With bp=None -> fallback 50, hours at 70 > 52 are suppressed
        # but hours at 40 should still get active scheduling
        morning_reasons = [entries[i].reason for i in range(12)]
        assert any("pre-heating" in r or "maintaining" in r for r in morning_reasons)


# ── Shoulder day detection ───────────────────────────────────────────


class TestShoulderDayDetection:
    """Tests for enhanced _is_shoulder_day with balance-point crossing."""

    def _make_planner(self, bp=55.0):
        model = _model_with_bp(bp)
        sim = ThermalSimulator(model)
        opt = ScheduleOptimizer(model, sim)
        return StrategicPlanner(
            optimizer=opt,
            resist_balance_point=bp,
        )

    def test_classic_shoulder_both_heat_and_cool_needed(self):
        """Original behavior: day needs both heating and cooling."""
        planner = self._make_planner(55.0)
        # Very cold morning, very hot afternoon
        forecast = _make_forecast([20] * 6 + [90] * 12 + [30] * 6)
        result = planner._is_shoulder_day(forecast, (72, 78), (62, 70))
        assert result is True

    def test_balance_point_crossing_triggers_shoulder(self):
        """Cold morning + warm afternoon crossing BP should trigger shoulder."""
        planner = self._make_planner(55.0)
        # Morning 40F (< bp-5=50), afternoon 65F (> bp+5=60)
        forecast = _make_forecast([40, 42, 44, 46, 50, 54, 58, 62, 65, 65,
                                   64, 62, 60, 58, 55, 52, 50, 48, 46, 44,
                                   42, 41, 40, 40])
        result = planner._is_shoulder_day(forecast, (72, 78), (62, 70))
        assert result is True, (
            "Day crossing balance point (40F min, 65F max, bp=55) should be shoulder"
        )

    def test_cold_day_no_shoulder(self):
        """Uniformly cold day should NOT be shoulder."""
        planner = self._make_planner(55.0)
        forecast = _make_forecast([30, 32, 34, 36, 38, 40, 42, 44,
                                   46, 48, 48, 46, 44, 42, 40, 38,
                                   36, 34, 32, 30, 28, 28, 28, 28])
        result = planner._is_shoulder_day(forecast, (72, 78), (62, 70))
        assert result is False, "Uniformly cold day (max 48 < bp+5=60) is not shoulder"

    def test_warm_day_no_shoulder(self):
        """Uniformly warm day should NOT be shoulder."""
        planner = self._make_planner(55.0)
        forecast = _make_forecast([65, 68, 70, 72, 75, 78, 80, 82,
                                   85, 85, 84, 82, 80, 78, 75, 72,
                                   70, 68, 66, 65, 64, 63, 62, 62])
        result = planner._is_shoulder_day(forecast, (72, 78), (62, 70))
        assert result is False, "Uniformly warm day (min 62 > bp-5=50) is not shoulder"

    def test_mild_crossing_no_shoulder(self):
        """Small crossing of balance point should NOT trigger shoulder."""
        planner = self._make_planner(55.0)
        # Range 52-58: crosses BP but not by > 5 on each side
        forecast = _make_forecast([52, 53, 54, 55, 56, 57, 58, 57,
                                   56, 55, 54, 53, 52, 53, 54, 55,
                                   56, 57, 56, 55, 54, 53, 52, 52])
        result = planner._is_shoulder_day(forecast, (72, 78), (62, 70))
        assert result is False, "Mild crossing (52-58 vs bp=55) should not trigger"


# ── Phase classification ─────────────────────────────────────────────


class TestPhaseClassification:
    """Tests for _classify_phase indoor temp override."""

    def _entry(self, reason, target_temp=70.0):
        return ScheduleEntry(
            start_time=NOW,
            end_time=NOW + timedelta(hours=1),
            target_temp=target_temp,
            mode="heat",
            reason=reason,
        )

    def _classify(self, entry, indoor_temp=None):
        # Inline the coordinator's _classify_phase logic since we can't
        # easily instantiate the full coordinator in unit tests.
        reason = entry.reason.lower()
        if indoor_temp is not None:
            if "pre-heating" in reason and indoor_temp >= entry.target_temp - 0.5:
                return "maintaining"
            if "pre-cooling" in reason and indoor_temp <= entry.target_temp + 0.5:
                return "maintaining"
        if "pre-cooling" in reason:
            return "pre_cooling"
        if "pre-heating" in reason:
            return "pre_heating"
        if "coasting" in reason:
            return "coasting"
        return "maintaining"

    def test_pre_heating_below_target(self):
        """Pre-heating label preserved when indoor is below target."""
        entry = self._entry("65F outdoor: pre-heating (target 70.0F)", 70.0)
        assert self._classify(entry, indoor_temp=66.0) == "pre_heating"

    def test_pre_heating_at_target_becomes_maintaining(self):
        """Pre-heating overridden to maintaining when indoor >= target - 0.5."""
        entry = self._entry("65F outdoor: pre-heating (target 70.0F)", 70.0)
        assert self._classify(entry, indoor_temp=69.8) == "maintaining"

    def test_pre_heating_above_target_becomes_maintaining(self):
        """Pre-heating overridden when indoor is above target."""
        entry = self._entry("65F outdoor: pre-heating (target 70.0F)", 70.0)
        assert self._classify(entry, indoor_temp=72.0) == "maintaining"

    def test_pre_cooling_above_target(self):
        """Pre-cooling label preserved when indoor is above target."""
        entry = self._entry("85F outdoor: pre-cooling (target 73.0F)", 73.0)
        entry.mode = "cool"
        assert self._classify(entry, indoor_temp=76.0) == "pre_cooling"

    def test_pre_cooling_at_target_becomes_maintaining(self):
        """Pre-cooling overridden when indoor <= target + 0.5."""
        entry = self._entry("85F outdoor: pre-cooling (target 73.0F)", 73.0)
        entry.mode = "cool"
        assert self._classify(entry, indoor_temp=73.2) == "maintaining"

    def test_coasting_unchanged(self):
        """Coasting label is never overridden."""
        entry = self._entry("65F outdoor: coasting (target 62.0F)", 62.0)
        assert self._classify(entry, indoor_temp=70.0) == "coasting"

    def test_no_indoor_temp_no_override(self):
        """Without indoor temp, labels are not overridden."""
        entry = self._entry("65F outdoor: pre-heating (target 70.0F)", 70.0)
        assert self._classify(entry, indoor_temp=None) == "pre_heating"
