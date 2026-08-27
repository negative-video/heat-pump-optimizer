"""Cost and carbon weights actually reach the scheduler (audit item 5).

`OptimizationWeights` was normalized and blended inside `score_hours` from the
beginning, but every call path constructed it with defaults
(carbon_intensity=0.0, electricity_cost=0.0), and `StrategicPlanner.optimize`
did not take a weights argument at all. So a price could never influence a
schedule, while the README advertised rate arbitrage, the config flow
collected rate entities and TOU schedules, and the savings tracker reported
"rate savings". Unit tests passed weights explicitly, so coverage looked
green.

The load-bearing test here is the flat-rate one. Enabling cost weighting by
default is only safe if a flat tariff produces exactly the schedule it did
before, and that is a property worth pinning rather than assuming.
"""

import io
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.conftest import CC, load_module
from tests.test_schedule_planner_rework import (
    HEAT_WAVE,
    MODERATE_SPRING,
    make_forecast,
    make_model,
)

dt_mod = load_module(
    "custom_components.heatpump_optimizer.engine.data_types",
    os.path.join(CC, "engine", "data_types.py"),
)
sim_mod = load_module(
    "custom_components.heatpump_optimizer.engine.thermal_simulator",
    os.path.join(CC, "engine", "thermal_simulator.py"),
)
opt_mod = load_module(
    "custom_components.heatpump_optimizer.engine.optimizer",
    os.path.join(CC, "engine", "optimizer.py"),
)

OptimizationWeights = dt_mod.OptimizationWeights
ThermalSimulator = sim_mod.ThermalSimulator
ScheduleOptimizer = opt_mod.ScheduleOptimizer


def _plan(forecast, weights=None, band=(72.0, 78.0), mode="cool"):
    model = make_model()
    opt = ScheduleOptimizer(model, ThermalSimulator(model))
    return opt.optimize_setpoints(75.0, forecast, band, mode, weights=weights)


def _targets(schedule):
    return [e.target_temp for e in schedule.entries]


def _with_rates(temps, rates):
    """Attach a per-hour electricity rate to each forecast point."""
    forecast = make_forecast(temps)
    for point, rate in zip(forecast, rates):
        point.electricity_rate = rate
    return forecast


class TestFlatRateChangesNothing:
    """The maintainer is on a flat tariff. Wiring cost must be a no-op there."""

    def test_flat_rate_matches_no_rate_on_a_moderate_day(self):
        flat = [0.148] * 24
        without = _plan(make_forecast(MODERATE_SPRING))
        with_flat = _plan(
            _with_rates(MODERATE_SPRING, flat),
            OptimizationWeights(energy_efficiency=1.0, electricity_cost=1.0),
        )
        assert _targets(with_flat) == _targets(without)

    def test_flat_rate_matches_no_rate_on_a_heat_wave(self):
        flat = [0.148] * 24
        without = _plan(make_forecast(HEAT_WAVE))
        with_flat = _plan(
            _with_rates(HEAT_WAVE, flat),
            OptimizationWeights(energy_efficiency=1.0, electricity_cost=1.0),
        )
        assert _targets(with_flat) == _targets(without)

    def test_zero_weight_ignores_even_a_wildly_variable_rate(self):
        spiky = [0.05 if h < 12 else 2.50 for h in range(24)]
        without = _plan(make_forecast(MODERATE_SPRING))
        ignored = _plan(
            _with_rates(MODERATE_SPRING, spiky),
            OptimizationWeights(energy_efficiency=1.0, electricity_cost=0.0),
        )
        assert _targets(ignored) == _targets(without)


class TestWeightsReachTheScorer:
    def test_variable_rate_moves_the_hour_scores(self):
        """The point of item 5: a price must be able to change something.

        Asserted on hour scores rather than setpoints. The planner ships flat
        on a moderate day regardless of price, which is correct -- shifting a
        setpoint to chase a tariff still costs more energy than it saves on a
        house with this time constant. Cost belongs in the ranking, not in a
        licence to swing the band.
        """
        spiky = [0.05 if h < 12 else 2.50 for h in range(24)]
        forecast = _with_rates(MODERATE_SPRING, spiky)

        model = make_model()
        opt = ScheduleOptimizer(model, ThermalSimulator(model))
        neutral = opt.score_hours(
            forecast, "cool", OptimizationWeights(energy_efficiency=1.0)
        )
        priced = opt.score_hours(
            forecast, "cool",
            OptimizationWeights(energy_efficiency=1.0, electricity_cost=1.0),
        )

        neutral_rank = [h.hour for h in sorted(neutral, key=lambda h: h.combined_score)]
        priced_rank = [h.hour for h in sorted(priced, key=lambda h: h.combined_score)]
        assert priced_rank != neutral_rank, "electricity cost did not reach the ranking"

    def test_cheap_hours_outrank_expensive_ones_when_cost_dominates(self):
        cheap_first = [0.05 if h < 12 else 2.50 for h in range(24)]
        forecast = _with_rates(MODERATE_SPRING, cheap_first)

        model = make_model()
        opt = ScheduleOptimizer(model, ThermalSimulator(model))
        scored = opt.score_hours(
            forecast, "cool",
            OptimizationWeights(energy_efficiency=0.0, electricity_cost=1.0),
        )
        best = min(scored, key=lambda h: h.combined_score)
        assert best.hour.hour < 12, "cost-dominated ranking picked an expensive hour"


class TestCoordinatorBuildsWeights:
    """Wiring, not scoring: the coordinator must construct non-defaults."""

    def test_helper_enables_cost_only_when_a_rate_source_exists(self):
        # Read the source rather than import it: coordinator.py pulls in Home
        # Assistant, which is not installed in this environment.
        src = io.open(os.path.join(CC, "coordinator.py"), encoding="utf-8").read()
        assert "_optimization_weights" in src
        assert "electricity_cost=1.0 if self._rate_entity_id else 0.0" in src, (
            "cost weight must be gated on an actual configured rate source"
        )

    def test_strategic_optimize_accepts_weights(self):
        import inspect
        from custom_components.heatpump_optimizer.controllers import strategic

        sig = inspect.signature(strategic.StrategicPlanner.optimize)
        assert "weights" in sig.parameters, (
            "StrategicPlanner.optimize still cannot receive weights"
        )
