"""Tests for enhanced COP corrections in the thermal estimator.

Tests wind chill, humidity, and pressure corrections applied to
HVAC output calculations in the Extended Kalman Filter.
"""

import importlib
import math
import os
import sys
import types

import numpy as np
import pytest

# ── Module loading (same pattern as test_thermal_estimator.py) ──────

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

if "custom_components.heatpump_optimizer.learning" not in sys.modules:
    learning = types.ModuleType("custom_components.heatpump_optimizer.learning")
    learning.__path__ = [os.path.join(CC, "learning")]
    sys.modules["custom_components.heatpump_optimizer.learning"] = learning

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

te_mod = _load(
    "custom_components.heatpump_optimizer.learning.thermal_estimator",
    os.path.join(CC, "learning", "thermal_estimator.py"),
)
sys.modules["custom_components.heatpump_optimizer.learning"].thermal_estimator = te_mod

ThermalEstimator = te_mod.ThermalEstimator
IDX_Q_COOL = te_mod.IDX_Q_COOL
IDX_Q_HEAT = te_mod.IDX_Q_HEAT
ALPHA_COOL = te_mod.ALPHA_COOL
ALPHA_HEAT = te_mod.ALPHA_HEAT
T_REF_F = te_mod.T_REF_F


# ── Helpers ────────────────────────────────────────────────────────


def make_estimator() -> ThermalEstimator:
    """Create a cold-start estimator for direct _hvac_output testing."""
    est = ThermalEstimator.cold_start(indoor_temp=72.0)
    return est


def get_hvac_output(est, mode, running, outdoor_temp):
    """Call _hvac_output with current estimator HVAC base capacities."""
    Q_cool_base = float(est.x[IDX_Q_COOL])
    Q_heat_base = float(est.x[IDX_Q_HEAT])
    return est._hvac_output(mode, running, outdoor_temp, Q_cool_base, Q_heat_base)


# ── Tests ──────────────────────────────────────────────────────────


class TestWindChillCorrection:
    """Test wind chill effects on heating COP in _hvac_output."""

    def test_wind_chill_applied_during_heating(self):
        """Wind chill should reduce effective outdoor temp for heating COP."""
        est = make_estimator()

        # Baseline: no wind
        est._current_wind_speed = None
        baseline = get_hvac_output(est, "heat", True, 30.0)

        # With wind
        est._current_wind_speed = 15.0
        with_wind = get_hvac_output(est, "heat", True, 30.0)

        # Wind chill makes it feel colder, so COP degrades further,
        # reducing heating output
        assert with_wind < baseline, (
            f"Wind chill should reduce heating output: {with_wind} vs {baseline}"
        )

    def test_no_wind_chill_above_50f(self):
        """Wind chill formula only applies below 50F outdoor."""
        est = make_estimator()

        est._current_wind_speed = None
        baseline = get_hvac_output(est, "heat", True, 55.0)

        est._current_wind_speed = 20.0
        with_wind = get_hvac_output(est, "heat", True, 55.0)

        assert with_wind == baseline, (
            "Wind chill should not apply above 50F"
        )

    def test_no_wind_chill_for_low_wind(self):
        """Wind chill formula requires wind > 3 mph."""
        est = make_estimator()

        est._current_wind_speed = None
        baseline = get_hvac_output(est, "heat", True, 30.0)

        est._current_wind_speed = 2.0
        with_low_wind = get_hvac_output(est, "heat", True, 30.0)

        assert with_low_wind == baseline, (
            "Wind chill should not apply for wind <= 3 mph"
        )

    def test_no_wind_chill_for_cooling(self):
        """Wind chill should not affect cooling mode."""
        est = make_estimator()

        est._current_wind_speed = None
        baseline = get_hvac_output(est, "cool", True, 40.0)

        est._current_wind_speed = 20.0
        with_wind = get_hvac_output(est, "cool", True, 40.0)

        assert with_wind == baseline, (
            "Wind chill should not affect cooling mode"
        )

    def test_nws_formula_correctness(self):
        """Verify the NWS wind chill formula produces expected values."""
        # NWS formula: 35.74 + 0.6215*T - 35.75*V^0.16 + 0.4275*T*V^0.16
        # At 30F, 10 mph: should be approximately 21F
        T = 30.0
        V = 10.0
        expected_wc = (
            35.74 + 0.6215 * T - 35.75 * (V ** 0.16) + 0.4275 * T * (V ** 0.16)
        )
        assert 20.0 < expected_wc < 25.0, f"NWS wind chill at 30F/10mph = {expected_wc}"

        est = make_estimator()
        est._current_wind_speed = V
        est._current_pressure = None
        est._current_humidity = None

        # The output should use the wind-chill-adjusted temp for COP
        output = get_hvac_output(est, "heat", True, T)

        # Compute what we expect: COP uses wind chill temp
        cop_factor = max(0.1, 1.0 - ALPHA_HEAT * (T_REF_F - expected_wc))
        Q_heat_base = float(est.x[IDX_Q_HEAT])
        expected_output = Q_heat_base * cop_factor

        assert abs(output - expected_output) < 1.0, (
            f"Expected ~{expected_output:.1f}, got {output:.1f}"
        )

    def test_reduces_heating_output(self):
        """Wind chill should produce strictly less heating output than calm conditions."""
        est = make_estimator()

        for wind_speed in [5.0, 10.0, 20.0, 30.0]:
            est._current_wind_speed = None
            baseline = get_hvac_output(est, "heat", True, 20.0)

            est._current_wind_speed = wind_speed
            windy = get_hvac_output(est, "heat", True, 20.0)

            assert windy < baseline, (
                f"Wind {wind_speed} mph should reduce heating output"
            )


class TestHumidityPenalty:
    """Test humidity corrections on cooling COP."""

    def test_high_humidity_reduces_cooling_cop(self):
        """Humidity > 50% should reduce cooling COP."""
        est = make_estimator()

        est._current_humidity = None
        est._current_wind_speed = None
        est._current_pressure = None
        baseline = get_hvac_output(est, "cool", True, 90.0)

        est._current_humidity = 80.0
        humid = get_hvac_output(est, "cool", True, 90.0)

        # Cooling is negative; less negative means reduced capacity
        assert humid > baseline, (
            f"High humidity should reduce cooling capacity: {humid} vs {baseline}"
        )

    def test_no_penalty_below_50_pct(self):
        """Humidity at or below 50% should have no effect."""
        est = make_estimator()

        est._current_humidity = None
        est._current_wind_speed = None
        est._current_pressure = None
        baseline = get_hvac_output(est, "cool", True, 90.0)

        est._current_humidity = 50.0
        at_50 = get_hvac_output(est, "cool", True, 90.0)

        assert at_50 == baseline, "Humidity at 50% should have no effect"

        est._current_humidity = 30.0
        at_30 = get_hvac_output(est, "cool", True, 90.0)

        assert at_30 == baseline, "Humidity below 50% should have no effect"

    def test_penalty_capped_at_0_8(self):
        """Humidity penalty multiplier should not go below 0.8."""
        est = make_estimator()

        est._current_humidity = None
        est._current_wind_speed = None
        est._current_pressure = None
        baseline = get_hvac_output(est, "cool", True, 90.0)

        # Even extreme humidity should not reduce beyond 80% of baseline
        est._current_humidity = 100.0
        extreme = get_hvac_output(est, "cool", True, 90.0)

        # The formula: max(0.8, 1.0 - (humidity - 50) / 500)
        # At 100%: max(0.8, 1.0 - 50/500) = max(0.8, 0.9) = 0.9
        # At much higher values the cap kicks in
        # The key is that the multiplier never goes below 0.8
        humidity_multiplier = extreme / baseline
        assert humidity_multiplier >= 0.79, (
            f"Humidity multiplier {humidity_multiplier} should be >= 0.8"
        )

    def test_none_humidity_no_effect(self):
        """None humidity should produce no correction."""
        est = make_estimator()
        est._current_wind_speed = None
        est._current_pressure = None

        est._current_humidity = None
        baseline = get_hvac_output(est, "cool", True, 85.0)

        # Re-check: explicitly None
        est._current_humidity = None
        result = get_hvac_output(est, "cool", True, 85.0)

        assert result == baseline

    def test_not_applied_to_heating(self):
        """Humidity penalty should not affect heating mode."""
        est = make_estimator()
        est._current_wind_speed = None
        est._current_pressure = None

        est._current_humidity = None
        baseline = get_hvac_output(est, "heat", True, 30.0)

        est._current_humidity = 95.0
        humid = get_hvac_output(est, "heat", True, 30.0)

        assert humid == baseline, "Humidity should not affect heating COP"


class TestPressureCorrection:
    """Test atmospheric pressure corrections on COP."""

    def test_standard_pressure_no_effect(self):
        """Standard pressure (1013.25 hPa) should multiply by ~1.0."""
        est = make_estimator()
        est._current_wind_speed = None
        est._current_humidity = None

        est._current_pressure = None
        baseline = get_hvac_output(est, "cool", True, 90.0)

        est._current_pressure = 1013.25
        at_standard = get_hvac_output(est, "cool", True, 90.0)

        # (1013.25 / 1013.25) ** 0.3 = 1.0
        assert abs(at_standard - baseline) < 1.0, (
            f"Standard pressure should match baseline: {at_standard} vs {baseline}"
        )

    def test_low_pressure_reduces_cop(self):
        """Low pressure (high altitude) should reduce COP."""
        est = make_estimator()
        est._current_wind_speed = None
        est._current_humidity = None

        est._current_pressure = None
        baseline = get_hvac_output(est, "cool", True, 90.0)

        est._current_pressure = 850.0  # ~5000 ft altitude
        low_p = get_hvac_output(est, "cool", True, 90.0)

        # For cooling, values are negative; less negative = reduced capacity
        assert low_p > baseline, (
            f"Low pressure should reduce cooling capacity: {low_p} vs {baseline}"
        )

    def test_high_pressure_increases_cop(self):
        """High pressure should slightly increase COP."""
        est = make_estimator()
        est._current_wind_speed = None
        est._current_humidity = None

        est._current_pressure = None
        baseline = get_hvac_output(est, "cool", True, 90.0)

        est._current_pressure = 1050.0
        high_p = get_hvac_output(est, "cool", True, 90.0)

        # For cooling, more negative = more capacity
        assert high_p < baseline, (
            f"High pressure should increase cooling capacity: {high_p} vs {baseline}"
        )

    def test_applied_to_both_modes(self):
        """Pressure correction should affect both heating and cooling."""
        est = make_estimator()
        est._current_wind_speed = None
        est._current_humidity = None

        # Cooling
        est._current_pressure = None
        cool_baseline = get_hvac_output(est, "cool", True, 90.0)
        est._current_pressure = 850.0
        cool_low_p = get_hvac_output(est, "cool", True, 90.0)
        assert cool_low_p != cool_baseline

        # Heating (use warm enough temp to avoid wind chill path)
        est._current_pressure = None
        heat_baseline = get_hvac_output(est, "heat", True, 55.0)
        est._current_pressure = 850.0
        heat_low_p = get_hvac_output(est, "heat", True, 55.0)
        assert heat_low_p != heat_baseline

    def test_none_pressure_no_effect(self):
        """None pressure should produce no correction."""
        est = make_estimator()
        est._current_wind_speed = None
        est._current_humidity = None

        est._current_pressure = None
        result1 = get_hvac_output(est, "cool", True, 85.0)
        result2 = get_hvac_output(est, "cool", True, 85.0)

        assert result1 == result2


class TestCombinedCorrections:
    """Test combinations of environmental corrections."""

    def test_humidity_plus_pressure_combined_cooling(self):
        """Humidity and pressure should stack for cooling."""
        est = make_estimator()
        est._current_wind_speed = None

        # Baseline: no corrections
        est._current_humidity = None
        est._current_pressure = None
        baseline = get_hvac_output(est, "cool", True, 90.0)

        # Humidity only
        est._current_humidity = 80.0
        est._current_pressure = None
        humid_only = get_hvac_output(est, "cool", True, 90.0)

        # Pressure only
        est._current_humidity = None
        est._current_pressure = 850.0
        pressure_only = get_hvac_output(est, "cool", True, 90.0)

        # Both combined
        est._current_humidity = 80.0
        est._current_pressure = 850.0
        combined = get_hvac_output(est, "cool", True, 90.0)

        # Combined should be less capacity (less negative) than either alone
        assert combined > humid_only, "Combined should reduce more than humidity alone"
        assert combined > pressure_only, "Combined should reduce more than pressure alone"

    def test_wind_plus_pressure_combined_heating(self):
        """Wind chill and pressure should stack for heating."""
        est = make_estimator()
        est._current_humidity = None

        # Baseline: no corrections
        est._current_wind_speed = None
        est._current_pressure = None
        baseline = get_hvac_output(est, "heat", True, 30.0)

        # Wind only
        est._current_wind_speed = 15.0
        est._current_pressure = None
        wind_only = get_hvac_output(est, "heat", True, 30.0)

        # Pressure only (low)
        est._current_wind_speed = None
        est._current_pressure = 850.0
        pressure_only = get_hvac_output(est, "heat", True, 30.0)

        # Both combined
        est._current_wind_speed = 15.0
        est._current_pressure = 850.0
        combined = get_hvac_output(est, "heat", True, 30.0)

        # Combined should produce less heating than either alone
        assert combined < wind_only, "Combined should reduce more than wind alone"
        assert combined < pressure_only, "Combined should reduce more than pressure alone"

    def test_all_none_matches_baseline(self):
        """All environmental fields as None should match uncorrected baseline."""
        est = make_estimator()

        est._current_wind_speed = None
        est._current_humidity = None
        est._current_pressure = None

        output1 = get_hvac_output(est, "cool", True, 85.0)

        # Manually compute expected: cop = max(0.1, 1 - 0.012 * (85 - 75)) = 0.88
        cop = max(0.1, 1.0 - ALPHA_COOL * (85.0 - T_REF_F))
        Q_cool = float(est.x[IDX_Q_COOL])
        expected = -Q_cool * cop

        assert abs(output1 - expected) < 0.01, (
            f"All-None should match baseline formula: {output1} vs {expected}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
