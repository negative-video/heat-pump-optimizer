"""Tests for the PerformanceModel engine module.

Covers:
- from_defaults() creates a valid model with expected properties
- from_estimator() builds a model from ThermalEstimator state dict
- cooling_delta at various outdoor temps (negative = cooling)
- heating_delta at various outdoor temps (positive = warming)
- passive_drift negative below balance point, positive above
- passive_drift with solar_condition parameter
- passive_drift with indoor_temp adjustment
- runtime_needed returns float("inf") for impossible conditions
- runtime_needed returns sensible minutes for achievable conditions
- coast_duration for cool and heat modes
- relative_efficiency scaling (0-1 range)
- balance point properties
- aux_heating_delta when no aux data present
- _lookup_delta interpolation, trendline fallback, and exact match
- summary() produces readable string
"""

import os
import sys

import pytest

from conftest import CC, load_module

perf_mod = load_module(
    "custom_components.heatpump_optimizer.engine.performance_model",
    os.path.join(CC, "engine", "performance_model.py"),
)

PerformanceModel = perf_mod.PerformanceModel


# ── Helpers ──────────────────────────────────────────────────────────


def _make_custom_model(**overrides):
    """Build a PerformanceModel from custom profile data with sensible defaults."""
    data = {
        "temperature": {
            "cool_1": {
                "deltas": {"75": -3.0, "85": -2.0, "95": -1.0},
                "linear_trendline": {"slope": 0.1, "intercept": -10.5},
            },
            "heat_1": {
                "deltas": {"20": 0.5, "35": 1.0, "50": 1.5},
                "linear_trendline": {"slope": 0.033, "intercept": 0.0},
            },
            "resist": {
                "deltas": {"30": -0.6, "50": 0.0, "70": 0.6},
                "linear_trendline": {"slope": 0.03, "intercept": -1.5},
            },
            "auxiliary_heat_1": None,
        },
        "balance_point": {
            "heat_1": 25.0,
            "resist": 50.0,
        },
        "differential": {
            "cool": 1.0,
            "heat": 1.0,
        },
        "setpoint": {
            "cool": 74.0,
            "heat": 68.0,
        },
    }
    for key, val in overrides.items():
        # Support dotted keys like "balance_point.heat_1"
        parts = key.split(".")
        target = data
        for p in parts[:-1]:
            target = target[p]
        target[parts[-1]] = val
    return PerformanceModel(data)


# ── from_defaults() ──────────────────────────────────────────────────


class TestFromDefaults:
    """Tests for the from_defaults() class constructor."""

    def test_creates_valid_model(self):
        model = PerformanceModel.from_defaults()
        assert isinstance(model, PerformanceModel)

    def test_has_cooling_deltas(self):
        model = PerformanceModel.from_defaults()
        assert len(model._cool_deltas) > 0

    def test_has_heating_deltas(self):
        model = PerformanceModel.from_defaults()
        assert len(model._heat_deltas) > 0

    def test_has_resist_deltas(self):
        model = PerformanceModel.from_defaults()
        assert len(model._resist_deltas) > 0

    def test_balance_points_set(self):
        model = PerformanceModel.from_defaults()
        assert model.heat_balance_point == 25.0
        assert model.resist_balance_point == 50.0

    def test_setpoints_set(self):
        model = PerformanceModel.from_defaults()
        assert model.cool_setpoint == 74.0
        assert model.heat_setpoint == 68.0

    def test_differentials_set(self):
        model = PerformanceModel.from_defaults()
        assert model.cool_differential == 1.0
        assert model.heat_differential == 1.0

    def test_no_aux_heat(self):
        model = PerformanceModel.from_defaults()
        assert model._aux_heat_deltas == {}
        assert model._aux_heat_trendline is None

    def test_cooling_range_covers_65_to_105(self):
        model = PerformanceModel.from_defaults()
        assert 65 in model._cool_deltas
        assert 105 in model._cool_deltas

    def test_heating_range_covers_neg5_to_55(self):
        model = PerformanceModel.from_defaults()
        assert -5 in model._heat_deltas
        assert 55 in model._heat_deltas


# ── cooling_delta ────────────────────────────────────────────────────


class TestCoolingDelta:
    """Tests for cooling_delta at various outdoor temps."""

    def test_negative_at_mild_temps(self):
        """Cooling delta should be negative (lowering indoor temp)."""
        model = PerformanceModel.from_defaults()
        delta = model.cooling_delta(75.0)
        assert delta < 0, f"Expected negative, got {delta}"

    def test_negative_at_hot_temps(self):
        model = PerformanceModel.from_defaults()
        delta = model.cooling_delta(100.0)
        assert delta < 0, f"Expected negative, got {delta}"

    def test_more_effective_at_lower_outdoor_temps(self):
        """Cooling is more effective (more negative) at lower outdoor temps."""
        model = PerformanceModel.from_defaults()
        delta_75 = model.cooling_delta(75.0)
        delta_95 = model.cooling_delta(95.0)
        assert delta_75 < delta_95, (
            f"Expected cooling at 75F ({delta_75}) more effective than at 95F ({delta_95})"
        )

    def test_exact_match_uses_stored_delta(self):
        """When outdoor temp matches a stored key exactly, use that value."""
        model = _make_custom_model()
        delta = model.cooling_delta(85.0)
        assert delta == -2.0

    def test_interpolation_between_points(self):
        """Between two measured points, interpolate linearly."""
        model = _make_custom_model()
        delta = model.cooling_delta(80.0)
        # Midpoint between 75 (-3.0) and 85 (-2.0) => -2.5
        assert abs(delta - (-2.5)) < 0.01

    def test_trendline_extrapolation_below_range(self):
        """Below measured range, use trendline."""
        model = _make_custom_model()
        delta = model.cooling_delta(60.0)
        # trendline: 0.1 * 60 + (-10.5) = -4.5
        assert abs(delta - (-4.5)) < 0.01

    def test_trendline_extrapolation_above_range(self):
        """Above measured range, use trendline."""
        model = _make_custom_model()
        delta = model.cooling_delta(110.0)
        # trendline: 0.1 * 110 + (-10.5) = 0.5
        assert abs(delta - 0.5) < 0.01

    def test_indoor_temp_param_accepted_but_ignored(self):
        """indoor_temp is accepted for interface compatibility but not used."""
        model = PerformanceModel.from_defaults()
        delta_a = model.cooling_delta(80.0, indoor_temp=70.0)
        delta_b = model.cooling_delta(80.0, indoor_temp=80.0)
        assert delta_a == delta_b

    def test_defaults_capped_at_neg4(self):
        """Default model caps best cooling at -4.0 F/hr."""
        model = PerformanceModel.from_defaults()
        # At 65F outdoor, formula gives -(2.5 - 0.057*(65-70)) = -(2.5+0.285) = -2.785
        # But it's capped at max -4.0 (most effective end). Let's check the lowest outdoor.
        delta = model.cooling_delta(65.0)
        assert delta >= -4.0

    def test_defaults_floored_at_neg03(self):
        """Default model floors worst cooling at -0.3 F/hr."""
        model = PerformanceModel.from_defaults()
        delta = model.cooling_delta(105.0)
        assert delta <= -0.3


# ── heating_delta ────────────────────────────────────────────────────


class TestHeatingDelta:
    """Tests for heating_delta at various outdoor temps."""

    def test_positive_at_mild_temps(self):
        """Heating delta should be positive (raising indoor temp)."""
        model = PerformanceModel.from_defaults()
        delta = model.heating_delta(45.0)
        assert delta > 0, f"Expected positive, got {delta}"

    def test_positive_at_cold_temps(self):
        model = PerformanceModel.from_defaults()
        delta = model.heating_delta(0.0)
        assert delta > 0, f"Expected positive, got {delta}"

    def test_more_effective_at_warmer_outdoor_temps(self):
        """Heating is more effective (larger positive) at warmer outdoor temps."""
        model = PerformanceModel.from_defaults()
        delta_50 = model.heating_delta(50.0)
        delta_10 = model.heating_delta(10.0)
        assert delta_50 > delta_10, (
            f"Expected heating at 50F ({delta_50}) more effective than at 10F ({delta_10})"
        )

    def test_exact_match_uses_stored_delta(self):
        model = _make_custom_model()
        delta = model.heating_delta(35.0)
        assert delta == 1.0

    def test_interpolation_between_points(self):
        model = _make_custom_model()
        # Midpoint between 20 (0.5) and 35 (1.0)
        delta = model.heating_delta(27.5)
        expected = 0.5 + (1.0 - 0.5) * (27.5 - 20) / (35 - 20)
        assert abs(delta - expected) < 0.01

    def test_trendline_extrapolation_below_range(self):
        model = _make_custom_model()
        delta = model.heating_delta(-10.0)
        # trendline: 0.033 * (-10) + 0.0 = -0.33
        expected = 0.033 * (-10) + 0.0
        assert abs(delta - expected) < 0.01

    def test_indoor_temp_param_accepted_but_ignored(self):
        model = PerformanceModel.from_defaults()
        delta_a = model.heating_delta(30.0, indoor_temp=60.0)
        delta_b = model.heating_delta(30.0, indoor_temp=75.0)
        assert delta_a == delta_b

    def test_defaults_heating_capped_at_2(self):
        """Default model caps best heating at 2.0 F/hr."""
        model = PerformanceModel.from_defaults()
        delta = model.heating_delta(55.0)
        assert delta <= 2.0

    def test_defaults_heating_floored_at_02(self):
        """Default model floors worst heating at 0.2 F/hr."""
        model = PerformanceModel.from_defaults()
        delta = model.heating_delta(-5.0)
        assert delta >= 0.2


# ── passive_drift ────────────────────────────────────────────────────


class TestPassiveDrift:
    """Tests for passive_drift (resist) behavior."""

    def test_negative_below_balance_point(self):
        """Below resist balance point (~50F), house loses heat (negative drift)."""
        model = PerformanceModel.from_defaults()
        drift = model.passive_drift(30.0)
        assert drift < 0, f"Expected negative drift below balance, got {drift}"

    def test_positive_above_balance_point(self):
        """Above resist balance point (~50F), house gains heat (positive drift)."""
        model = PerformanceModel.from_defaults()
        drift = model.passive_drift(70.0)
        assert drift > 0, f"Expected positive drift above balance, got {drift}"

    def test_near_zero_at_balance_point(self):
        """At the resist balance point (~50F), drift should be near zero."""
        model = PerformanceModel.from_defaults()
        drift = model.passive_drift(50.0)
        assert abs(drift) < 0.1, f"Expected near-zero drift at balance point, got {drift}"

    def test_magnitude_increases_with_distance_from_balance(self):
        """Drift magnitude increases as outdoor temp moves from balance point."""
        model = PerformanceModel.from_defaults()
        drift_40 = model.passive_drift(40.0)
        drift_20 = model.passive_drift(20.0)
        assert abs(drift_20) > abs(drift_40), (
            f"Expected larger drift at 20F ({drift_20}) than at 40F ({drift_40})"
        )

    def test_solar_condition_sunny(self):
        """Solar-specific trendline used when available."""
        model = PerformanceModel.from_defaults()
        # Add solar-specific trendlines
        model._solar_resist_trendlines["sunny"] = {"slope": 0.04, "intercept": -1.2}
        model._solar_resist_balance_points["sunny"] = 30.0

        drift_sunny = model.passive_drift(60.0, solar_condition="sunny")
        drift_none = model.passive_drift(60.0, solar_condition=None)
        # sunny: 0.04 * 60 + (-1.2) = 1.2
        assert abs(drift_sunny - 1.2) < 0.01
        # They should differ since different trendlines
        assert drift_sunny != drift_none

    def test_solar_condition_falls_back_to_aggregate(self):
        """Unknown solar condition falls back to aggregate trendline."""
        model = PerformanceModel.from_defaults()
        drift_unknown = model.passive_drift(60.0, solar_condition="foggy")
        drift_none = model.passive_drift(60.0, solar_condition=None)
        assert drift_unknown == drift_none

    def test_indoor_temp_adjustment(self):
        """When indoor_temp is provided, drift is adjusted for temp difference."""
        model = PerformanceModel.from_defaults()
        drift_default = model.passive_drift(60.0)
        drift_warm = model.passive_drift(60.0, indoor_temp=80.0)
        drift_cool = model.passive_drift(60.0, indoor_temp=65.0)
        # Warmer indoor -> more heat loss -> lower drift
        # Cooler indoor -> less heat loss -> higher drift
        assert drift_cool > drift_warm

    def test_indoor_temp_72_equals_no_adjustment(self):
        """Indoor temp of 72F (nominal) should give same result as no indoor_temp."""
        model = PerformanceModel.from_defaults()
        drift_none = model.passive_drift(60.0)
        drift_72 = model.passive_drift(60.0, indoor_temp=72.0)
        assert abs(drift_none - drift_72) < 0.001


# ── runtime_needed ───────────────────────────────────────────────────


class TestRuntimeNeeded:
    """Tests for runtime_needed calculations."""

    def test_cooling_returns_finite_minutes(self):
        model = PerformanceModel.from_defaults()
        minutes = model.runtime_needed(85.0, "cool", 2.0)
        assert minutes > 0
        assert minutes < float("inf")

    def test_heating_returns_finite_minutes(self):
        model = PerformanceModel.from_defaults()
        minutes = model.runtime_needed(30.0, "heat", 2.0)
        assert minutes > 0
        assert minutes < float("inf")

    def test_cooling_inf_when_delta_positive(self):
        """If cooling delta is >= 0 (system cannot cool), return inf."""
        # Build a model where cooling delta is positive at extreme outdoor temp
        model = _make_custom_model()
        # At 110F, trendline gives 0.1*110 - 10.5 = 0.5 (positive!)
        minutes = model.runtime_needed(110.0, "cool", 2.0)
        assert minutes == float("inf")

    def test_heating_inf_when_delta_negative(self):
        """If heating delta is <= 0, return inf."""
        model = _make_custom_model()
        # At -10F, trendline gives 0.033*(-10) + 0.0 = -0.33 (negative!)
        minutes = model.runtime_needed(-10.0, "heat", 2.0)
        assert minutes == float("inf")

    def test_unknown_mode_returns_inf(self):
        model = PerformanceModel.from_defaults()
        minutes = model.runtime_needed(80.0, "ventilate", 2.0)
        assert minutes == float("inf")

    def test_larger_degrees_needs_more_time(self):
        model = PerformanceModel.from_defaults()
        time_2 = model.runtime_needed(85.0, "cool", 2.0)
        time_4 = model.runtime_needed(85.0, "cool", 4.0)
        assert time_4 > time_2

    def test_runtime_proportional_to_degrees(self):
        """Runtime should scale linearly with degrees requested."""
        model = PerformanceModel.from_defaults()
        time_1 = model.runtime_needed(85.0, "cool", 1.0)
        time_3 = model.runtime_needed(85.0, "cool", 3.0)
        assert abs(time_3 - 3 * time_1) < 0.01

    def test_harder_conditions_need_more_time(self):
        """Cooling at hotter outdoor temp needs more runtime."""
        model = PerformanceModel.from_defaults()
        time_80 = model.runtime_needed(80.0, "cool", 2.0)
        time_100 = model.runtime_needed(100.0, "cool", 2.0)
        assert time_100 > time_80


# ── coast_duration ───────────────────────────────────────────────────


class TestCoastDuration:
    """Tests for coast_duration (HVAC off drift time)."""

    def test_cool_mode_positive_drift(self):
        """In cool mode with positive drift (house warming), finite coast time."""
        model = PerformanceModel.from_defaults()
        # At 80F outdoor, drift is positive (house warming)
        minutes = model.coast_duration(80.0, "cool", 2.0)
        assert minutes > 0
        assert minutes < float("inf")

    def test_cool_mode_negative_drift(self):
        """In cool mode with negative drift (house cooling), infinite coast."""
        model = PerformanceModel.from_defaults()
        # At 30F outdoor, drift is negative (house losing heat)
        minutes = model.coast_duration(30.0, "cool", 2.0)
        assert minutes == float("inf")

    def test_heat_mode_negative_drift(self):
        """In heat mode with negative drift (house cooling), finite coast time."""
        model = PerformanceModel.from_defaults()
        # At 30F outdoor, drift is negative
        minutes = model.coast_duration(30.0, "heat", 2.0)
        assert minutes > 0
        assert minutes < float("inf")

    def test_heat_mode_positive_drift(self):
        """In heat mode with positive drift (house warming), infinite coast."""
        model = PerformanceModel.from_defaults()
        # At 80F outdoor, drift is positive
        minutes = model.coast_duration(80.0, "heat", 2.0)
        assert minutes == float("inf")

    def test_unknown_mode_returns_inf(self):
        model = PerformanceModel.from_defaults()
        minutes = model.coast_duration(80.0, "fan", 2.0)
        assert minutes == float("inf")

    def test_larger_degrees_allows_longer_coast(self):
        model = PerformanceModel.from_defaults()
        coast_1 = model.coast_duration(80.0, "cool", 1.0)
        coast_3 = model.coast_duration(80.0, "cool", 3.0)
        assert coast_3 > coast_1


# ── relative_efficiency ──────────────────────────────────────────────


class TestRelativeEfficiency:
    """Tests for relative_efficiency (0-1 scale)."""

    def test_cool_best_conditions_is_1(self):
        """At the coolest measured outdoor temp, cooling efficiency should be 1.0."""
        model = _make_custom_model()
        # 75F is the coolest measured point, delta = -3.0 (most negative)
        eff = model.relative_efficiency(75.0, "cool")
        assert abs(eff - 1.0) < 0.01

    def test_cool_worst_conditions_less_than_1(self):
        model = _make_custom_model()
        eff = model.relative_efficiency(95.0, "cool")
        assert 0 < eff < 1.0

    def test_heat_best_conditions_is_1(self):
        """At the warmest measured outdoor temp, heating efficiency should be 1.0."""
        model = _make_custom_model()
        # 50F is the warmest measured point, delta = 1.5 (most positive)
        eff = model.relative_efficiency(50.0, "heat")
        assert abs(eff - 1.0) < 0.01

    def test_heat_worst_conditions_less_than_1(self):
        model = _make_custom_model()
        eff = model.relative_efficiency(20.0, "heat")
        assert 0 < eff < 1.0

    def test_unknown_mode_returns_zero(self):
        model = _make_custom_model()
        eff = model.relative_efficiency(80.0, "dehumidify")
        assert eff == 0.0

    def test_cool_efficiency_decreases_with_higher_outdoor(self):
        model = PerformanceModel.from_defaults()
        eff_75 = model.relative_efficiency(75.0, "cool")
        eff_100 = model.relative_efficiency(100.0, "cool")
        assert eff_75 > eff_100

    def test_heat_efficiency_decreases_with_lower_outdoor(self):
        model = PerformanceModel.from_defaults()
        eff_50 = model.relative_efficiency(50.0, "heat")
        eff_10 = model.relative_efficiency(10.0, "heat")
        assert eff_50 > eff_10


# ── aux_heating_delta ────────────────────────────────────────────────


class TestAuxHeatingDelta:
    """Tests for auxiliary heat when absent and present."""

    def test_no_aux_data_returns_zero(self):
        model = PerformanceModel.from_defaults()
        delta = model.aux_heating_delta(20.0)
        assert delta == 0.0

    def test_with_aux_data_returns_value(self):
        """When aux heat data is present, returns a meaningful delta."""
        data = {
            "temperature": {
                "cool_1": {
                    "deltas": {"80": -2.0},
                    "linear_trendline": {"slope": 0.1, "intercept": -10.0},
                },
                "heat_1": {
                    "deltas": {"30": 1.0},
                    "linear_trendline": {"slope": 0.03, "intercept": 0.0},
                },
                "resist": {
                    "deltas": {"50": 0.0},
                    "linear_trendline": {"slope": 0.03, "intercept": -1.5},
                },
                "auxiliary_heat_1": {
                    "deltas": {"10": 2.0, "30": 3.0},
                    "linear_trendline": {"slope": 0.05, "intercept": 1.5},
                },
            },
            "balance_point": {"heat_1": 25.0, "resist": 50.0},
            "differential": {"cool": 1.0, "heat": 1.0},
            "setpoint": {"cool": 74.0, "heat": 68.0},
        }
        model = PerformanceModel(data)
        delta = model.aux_heating_delta(30.0)
        assert delta == 3.0

    def test_aux_extrapolation(self):
        """Aux heat uses trendline for temps outside measured range."""
        data = {
            "temperature": {
                "cool_1": {
                    "deltas": {"80": -2.0},
                    "linear_trendline": {"slope": 0.1, "intercept": -10.0},
                },
                "heat_1": {
                    "deltas": {"30": 1.0},
                    "linear_trendline": {"slope": 0.03, "intercept": 0.0},
                },
                "resist": {
                    "deltas": {"50": 0.0},
                    "linear_trendline": {"slope": 0.03, "intercept": -1.5},
                },
                "auxiliary_heat_1": {
                    "deltas": {"10": 2.0, "30": 3.0},
                    "linear_trendline": {"slope": 0.05, "intercept": 1.5},
                },
            },
            "balance_point": {"heat_1": 25.0, "resist": 50.0},
            "differential": {"cool": 1.0, "heat": 1.0},
            "setpoint": {"cool": 74.0, "heat": 68.0},
        }
        model = PerformanceModel(data)
        # Below range: trendline at -10F -> 0.05 * (-10) + 1.5 = 1.0
        delta = model.aux_heating_delta(-10.0)
        assert abs(delta - 1.0) < 0.01


# ── _lookup_delta edge cases ────────────────────────────────────────


class TestLookupDelta:
    """Tests for the _lookup_delta internal method edge cases."""

    def test_empty_deltas_uses_trendline(self):
        """When no measured points exist, use trendline."""
        model = PerformanceModel.from_defaults()
        trendline = {"slope": 0.05, "intercept": -2.0}
        result = model._lookup_delta({}, trendline, 40.0)
        expected = 0.05 * 40 + (-2.0)
        assert abs(result - expected) < 0.001

    def test_single_point_exact_match(self):
        model = PerformanceModel.from_defaults()
        result = model._lookup_delta({50: 1.5}, {"slope": 0.1, "intercept": 0}, 50.0)
        assert result == 1.5

    def test_rounding_to_nearest_int(self):
        """outdoor_temp is rounded to nearest int for exact lookup."""
        model = PerformanceModel.from_defaults()
        result = model._lookup_delta(
            {50: 1.5, 51: 1.6}, {"slope": 0.1, "intercept": 0}, 50.4
        )
        assert result == 1.5  # rounds to 50


# ── net_cooling_rate / net_heating_rate ──────────────────────────────


class TestNetRates:
    """Tests for net_cooling_rate and net_heating_rate wrappers."""

    def test_net_cooling_rate_matches_cooling_delta(self):
        model = PerformanceModel.from_defaults()
        assert model.net_cooling_rate(85.0) == model.cooling_delta(85.0)

    def test_net_heating_rate_matches_heating_delta(self):
        model = PerformanceModel.from_defaults()
        assert model.net_heating_rate(35.0) == model.heating_delta(35.0)


# ── from_estimator ───────────────────────────────────────────────────


class TestFromEstimator:
    """Tests for from_estimator() class constructor."""

    def _mock_estimator(self, **overrides):
        """Create a mock estimator with a state_dict method."""
        state = {
            "R_inv": 0.005,
            "Q_cool_base": 24000,
            "Q_heat_base": 20000,
            "C_inv": 1.0 / 2000.0,
        }
        state.update(overrides)

        class FakeEstimator:
            def state_dict(self):
                return state

        return FakeEstimator()

    def test_creates_valid_model(self):
        est = self._mock_estimator()
        model = PerformanceModel.from_estimator(est)
        assert isinstance(model, PerformanceModel)

    def test_has_cooling_deltas(self):
        est = self._mock_estimator()
        model = PerformanceModel.from_estimator(est)
        assert len(model._cool_deltas) > 0

    def test_has_heating_deltas(self):
        est = self._mock_estimator()
        model = PerformanceModel.from_estimator(est)
        assert len(model._heat_deltas) > 0

    def test_cooling_deltas_are_negative(self):
        est = self._mock_estimator()
        model = PerformanceModel.from_estimator(est)
        for temp, delta in model._cool_deltas.items():
            assert delta < 0, f"Cooling delta at {temp}F should be negative, got {delta}"

    def test_heating_deltas_are_positive(self):
        est = self._mock_estimator()
        model = PerformanceModel.from_estimator(est)
        for temp, delta in model._heat_deltas.items():
            assert delta > 0, f"Heating delta at {temp}F should be positive, got {delta}"

    def test_no_aux_heat(self):
        est = self._mock_estimator()
        model = PerformanceModel.from_estimator(est)
        assert model._aux_heat_deltas == {}

    def test_balance_points_set(self):
        est = self._mock_estimator()
        model = PerformanceModel.from_estimator(est)
        assert model.heat_balance_point == 25.0
        assert model.resist_balance_point == 50.0

    def test_larger_cooling_capacity_gives_more_negative_deltas(self):
        est_small = self._mock_estimator(Q_cool_base=12000)
        est_large = self._mock_estimator(Q_cool_base=36000)
        model_small = PerformanceModel.from_estimator(est_small)
        model_large = PerformanceModel.from_estimator(est_large)
        # At same temp, larger capacity should cool more
        delta_small = model_small.cooling_delta(85.0)
        delta_large = model_large.cooling_delta(85.0)
        assert delta_large < delta_small  # more negative


# ── summary ──────────────────────────────────────────────────────────


class TestSummary:
    """Tests for the summary() output."""

    def test_returns_string(self):
        model = PerformanceModel.from_defaults()
        result = model.summary()
        assert isinstance(result, str)

    def test_contains_key_sections(self):
        model = PerformanceModel.from_defaults()
        result = model.summary()
        assert "Cooling range" in result
        assert "Heating range" in result
        assert "Balance points" in result
        assert "Differentials" in result
        assert "setpoints" in result.lower()

    def test_contains_balance_point_values(self):
        model = PerformanceModel.from_defaults()
        result = model.summary()
        assert "25.0" in result
        assert "50.0" in result
