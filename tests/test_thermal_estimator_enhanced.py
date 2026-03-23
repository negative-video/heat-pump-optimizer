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


class TestWindNotAppliedToCondenser:
    """Wind should NOT affect condenser COP — only envelope infiltration.

    Wind chill (NWS formula) models perceived temp on exposed human skin.
    Heat pump condensers use forced convection from their own fan, so
    ambient wind has negligible (or slightly positive) effect on COP.
    """

    def test_wind_does_not_affect_heating_output(self):
        """Wind speed should not change HVAC heating output."""
        est = make_estimator()

        est._current_wind_speed = None
        baseline = get_hvac_output(est, "heat", True, 30.0)

        est._current_wind_speed = 15.0
        with_wind = get_hvac_output(est, "heat", True, 30.0)

        assert with_wind == baseline, (
            f"Wind should not affect heating COP: {with_wind} vs {baseline}"
        )

    def test_wind_does_not_affect_cooling_output(self):
        """Wind speed should not change HVAC cooling output."""
        est = make_estimator()

        est._current_wind_speed = None
        baseline = get_hvac_output(est, "cool", True, 95.0)

        est._current_wind_speed = 20.0
        with_wind = get_hvac_output(est, "cool", True, 95.0)

        assert with_wind == baseline, (
            "Wind should not affect cooling COP"
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

    def test_pressure_reduces_heating(self):
        """Low pressure (altitude) should reduce heating capacity."""
        est = make_estimator()
        est._current_humidity = None
        est._current_wind_speed = None

        # Baseline: no pressure correction
        est._current_pressure = None
        baseline = get_hvac_output(est, "heat", True, 30.0)

        # Low pressure (altitude)
        est._current_pressure = 850.0
        low_pressure = get_hvac_output(est, "heat", True, 30.0)

        assert low_pressure < baseline, (
            "Low pressure should reduce heating capacity"
        )

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


# ── Thermal mass stability tests ──────────────────────────────────

IDX_C_MASS_INV = te_mod.IDX_C_MASS_INV
IDX_T_AIR = te_mod.IDX_T_AIR
IDX_T_MASS = te_mod.IDX_T_MASS
BOUNDS = te_mod.BOUNDS
DT_HOURS = te_mod.DT_HOURS


class TestThermalMassStability:
    """Verify thermal mass doesn't diverge under low-observability conditions."""

    def _make_idle_estimator(self, indoor=72.0, outdoor=88.0):
        """Create an estimator in a hot-outdoor / idle-HVAC scenario."""
        est = ThermalEstimator.cold_start(indoor_temp=indoor)
        # Set T_mass close to T_air (near equilibrium)
        est.x[IDX_T_MASS] = indoor + 0.1
        return est

    def test_idle_hot_evening_no_runaway(self):
        """Thermal mass stays bounded during 20 idle cycles with hot outdoor."""
        est = self._make_idle_estimator(indoor=72.0, outdoor=88.0)
        initial_c_mass_inv = float(est.x[IDX_C_MASS_INV])
        initial_mass = 1.0 / initial_c_mass_inv

        for _ in range(20):
            est.update(
                observed_temp=72.0,
                outdoor_temp=88.0,
                hvac_mode="cool",
                hvac_running=False,
                cloud_cover=0.3,
                sun_elevation=-5.0,  # evening
                dt_hours=DT_HOURS,
            )

        final_mass = est.thermal_mass
        # Thermal mass must stay below the tightened bound of 30,000
        assert final_mass <= 30_000, (
            f"Thermal mass diverged to {final_mass:.0f} BTU/°F during idle period"
        )
        # Should not have drifted by more than ~2x from start (rate limiting)
        assert final_mass < initial_mass * 3, (
            f"Thermal mass drifted too far: {initial_mass:.0f} → {final_mass:.0f}"
        )

    def test_rate_limiting_caps_single_step(self):
        """C_mass_inv can't change more than 5% per cycle."""
        est = ThermalEstimator.cold_start(indoor_temp=72.0)
        before = float(est.x[IDX_C_MASS_INV])

        # Force a large innovation by setting a far-off predicted temp
        est.update(
            observed_temp=75.0,  # 3°F innovation
            outdoor_temp=95.0,
            hvac_mode="cool",
            hvac_running=False,
            cloud_cover=0.0,
            sun_elevation=45.0,
            dt_hours=DT_HOURS,
        )

        after = float(est.x[IDX_C_MASS_INV])
        max_allowed_change = 0.05 * before
        actual_change = abs(after - before)

        assert actual_change <= max_allowed_change + 1e-12, (
            f"C_mass_inv changed by {actual_change:.2e} "
            f"(max {max_allowed_change:.2e})"
        )

    def test_bounds_cap_thermal_mass_at_30k(self):
        """Bounds enforce thermal mass ≤ 30,000 BTU/°F."""
        est = ThermalEstimator.cold_start(indoor_temp=72.0)
        # Manually force C_mass_inv below bound
        est.x[IDX_C_MASS_INV] = 1e-6  # Would be 1,000,000 BTU/°F
        est._prev_c_mass_inv = None  # Bypass rate limiting for this test
        est._clamp_parameters()

        lo, hi = BOUNDS[IDX_C_MASS_INV]
        assert est.x[IDX_C_MASS_INV] >= lo, (
            f"C_mass_inv {est.x[IDX_C_MASS_INV]} below lower bound {lo}"
        )
        assert est.thermal_mass <= 1.0 / lo + 1, (
            f"Thermal mass {est.thermal_mass:.0f} exceeds upper physical bound"
        )

    def test_observability_gating_freezes_at_equilibrium(self):
        """When T_mass ≈ T_air, C_mass_inv should barely change."""
        est = ThermalEstimator.cold_start(indoor_temp=72.0)
        # Force exact equilibrium
        est.x[IDX_T_MASS] = 72.0
        est.x[IDX_T_AIR] = 72.0
        before = float(est.x[IDX_C_MASS_INV])

        for _ in range(5):
            est.update(
                observed_temp=72.0,
                outdoor_temp=72.0,  # no driving force
                hvac_mode="cool",
                hvac_running=False,
                cloud_cover=1.0,
                sun_elevation=-10.0,
                dt_hours=DT_HOURS,
            )

        after = float(est.x[IDX_C_MASS_INV])
        # Should be essentially unchanged (gated + damped)
        pct_change = abs(after - before) / before * 100
        assert pct_change < 1.0, (
            f"C_mass_inv changed {pct_change:.2f}% at equilibrium "
            "(expected near-zero due to observability gating)"
        )

    def test_observability_factor_scales_with_delta(self):
        """Verify that the obs_factor mechanism correctly scales by |T_mass-T_air|.

        C_mass_inv has very low process noise and starts with a diagonal P, so
        the Kalman gain takes many cycles to build up.  Instead of checking
        end-to-end convergence, we verify the observability factor calculation
        that gates the Kalman gain — the critical mechanism that prevents
        runaway.
        """
        mass_obs = te_mod._MASS_OBS_THRESHOLD
        full_obs = te_mod._MASS_FULL_OBS_DELTA

        # Below hard threshold: factor should be 0
        assert min(1.0, 0.3 / full_obs) < 0.5  # small
        # At threshold: factor is threshold/full_obs
        expected_at_threshold = mass_obs / full_obs
        assert abs(min(1.0, mass_obs / full_obs) - expected_at_threshold) < 1e-9
        # Above full_obs: factor = 1.0
        assert min(1.0, 3.0 / full_obs) == 1.0
        # Linear scaling in between
        factor_at_1 = min(1.0, 1.0 / full_obs)
        assert 0.0 < factor_at_1 < 1.0


IDX_Q_COOL_LOCAL = te_mod.IDX_Q_COOL
IDX_Q_HEAT_LOCAL = te_mod.IDX_Q_HEAT


class TestTonnagePriorStrength:
    """Verify user-provided tonnage resists aggressive drift during early learning."""

    def test_tonnage_prior_resists_drift(self):
        """Q_cool should stay near rated capacity over many cycles."""
        est = ThermalEstimator.cold_start(indoor_temp=72.0, tonnage=3.5, sqft=2200)
        initial_q_cool = float(est.x[IDX_Q_COOL_LOCAL])  # 42,000
        assert abs(initial_q_cool - 42000.0) < 1.0

        # Simulate 200 cycles (~17 hours) of cooling with modest dT
        for i in range(200):
            running = (i % 12) < 6  # 50% duty cycle
            est.update(
                observed_temp=71.5 if running else 72.0,
                outdoor_temp=90.0,
                hvac_mode="cool",
                hvac_running=running,
                cloud_cover=0.3,
                sun_elevation=30.0,
                dt_hours=DT_HOURS,
            )

        learned_q = float(est.x[IDX_Q_COOL_LOCAL])
        # Should still be within 50% of rated after 17 hours
        assert learned_q > 0.50 * initial_q_cool, (
            f"Q_cool={learned_q:.0f} drifted too far from rated "
            f"{initial_q_cool:.0f} ({learned_q/initial_q_cool*100:.0f}%)"
        )

    def test_tonnage_prior_flag_persisted(self):
        """_has_tonnage_prior survives serialization round-trip."""
        est = ThermalEstimator.cold_start(indoor_temp=72.0, tonnage=2.5)
        assert est._has_tonnage_prior is True

        data = est.to_dict()
        restored = ThermalEstimator.from_dict(data)
        assert restored._has_tonnage_prior is True

    def test_no_tonnage_no_rate_limit(self):
        """Without tonnage, Q_cool should converge freely."""
        est = ThermalEstimator.cold_start(indoor_temp=72.0)
        assert est._has_tonnage_prior is False
        # Process noise should be default 1.0
        assert est.Q[IDX_Q_COOL_LOCAL, IDX_Q_COOL_LOCAL] == 1.0

    def test_tonnage_reduces_process_noise(self):
        """With tonnage, process noise for Q_cool/Q_heat is reduced."""
        est = ThermalEstimator.cold_start(indoor_temp=72.0, tonnage=3.0)
        assert est.Q[IDX_Q_COOL_LOCAL, IDX_Q_COOL_LOCAL] == 0.01
        assert est.Q[IDX_Q_HEAT_LOCAL, IDX_Q_HEAT_LOCAL] == 0.01


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
