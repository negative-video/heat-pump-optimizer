"""Tests for the Extended Kalman Filter thermal estimator.

Tests convergence, persistence, adaptive model interface,
and Beestat-primed initialization.
"""

import importlib
import json
import math
import sys
import os
import types

import numpy as np
import pytest

# Add project root to path
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, PROJECT_ROOT)

# Set up minimal package structure so relative imports work,
# without pulling in HA-dependent modules (__init__.py, coordinator, etc.)
from unittest.mock import MagicMock
import importlib.util

CC = os.path.join(PROJECT_ROOT, "custom_components", "heatpump_optimizer")

# Create package stubs
pkg = types.ModuleType("custom_components")
pkg.__path__ = [os.path.join(PROJECT_ROOT, "custom_components")]
sys.modules["custom_components"] = pkg

ho = types.ModuleType("custom_components.heatpump_optimizer")
ho.__path__ = [CC]
sys.modules["custom_components.heatpump_optimizer"] = ho

learning = types.ModuleType("custom_components.heatpump_optimizer.learning")
learning.__path__ = [os.path.join(CC, "learning")]
sys.modules["custom_components.heatpump_optimizer.learning"] = learning

engine = types.ModuleType("custom_components.heatpump_optimizer.engine")
engine.__path__ = [os.path.join(CC, "engine")]
sys.modules["custom_components.heatpump_optimizer.engine"] = engine

# Now load the actual modules using importlib
def _load(full_name: str, path: str):
    spec = importlib.util.spec_from_file_location(full_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod

te_mod = _load(
    "custom_components.heatpump_optimizer.learning.thermal_estimator",
    os.path.join(CC, "learning", "thermal_estimator.py"),
)
learning.thermal_estimator = te_mod

apm_mod = _load(
    "custom_components.heatpump_optimizer.engine.adaptive_performance_model",
    os.path.join(CC, "engine", "adaptive_performance_model.py"),
)
engine.adaptive_performance_model = apm_mod

ThermalEstimator = te_mod.ThermalEstimator
DT_HOURS = te_mod.DT_HOURS
IDX_R_INV = te_mod.IDX_R_INV
IDX_C_INV = te_mod.IDX_C_INV
IDX_Q_COOL = te_mod.IDX_Q_COOL
IDX_Q_HEAT = te_mod.IDX_Q_HEAT
T_REF_F = te_mod.T_REF_F
ALPHA_COOL = te_mod.ALPHA_COOL
ALPHA_HEAT = te_mod.ALPHA_HEAT
AdaptivePerformanceModel = apm_mod.AdaptivePerformanceModel


# ── Synthetic data generator ────────────────────────────────────────


def simulate_building(
    R: float,
    C: float,
    Q_cool: float,
    Q_heat: float,
    outdoor_temps: list[float],
    hvac_modes: list[str],
    hvac_running: list[bool],
    initial_indoor: float = 72.0,
    dt_hours: float = DT_HOURS,
    noise_std: float = 0.1,
    envelope_area: float = 2000.0,
) -> list[float]:
    """Generate synthetic indoor temperature readings from known parameters.

    Uses a simplified single-node RC model (no thermal mass) for clarity.
    R is per-area R-value (°F·hr·ft²/BTU), matching the EKF's convention.
    Total conductance UA = (1/R) * envelope_area.
    """
    R_inv = 1.0 / R
    UA = R_inv * envelope_area
    C_inv = 1.0 / C
    indoor = initial_indoor
    readings = []

    for i in range(len(outdoor_temps)):
        # Add measurement noise
        readings.append(indoor + np.random.normal(0, noise_std))

        # Physics update (UA = R_inv * area, matching EKF)
        Q_env = UA * (outdoor_temps[i] - indoor)

        Q_hvac = 0.0
        if hvac_running[i]:
            if hvac_modes[i] == "cool":
                cop = max(0.1, 1.0 - ALPHA_COOL * (outdoor_temps[i] - T_REF_F))
                Q_hvac = -Q_cool * cop
            elif hvac_modes[i] == "heat":
                cop = max(0.1, 1.0 - ALPHA_HEAT * (T_REF_F - outdoor_temps[i]))
                Q_hvac = Q_heat * cop

        indoor += C_inv * (Q_env + Q_hvac) * dt_hours

    return readings


# ── Tests ───────────────────────────────────────────────────────────


class TestColdStart:
    """Test cold start initialization."""

    def test_cold_start_creates_valid_state(self):
        est = ThermalEstimator.cold_start(indoor_temp=70.0)
        assert est.x[0] == 70.0  # T_air
        assert est.x[1] == 70.0  # T_mass
        assert est.P.shape == (9, 9)
        assert est.confidence == 0.0  # No observations yet
        assert est._n_obs == 0

    def test_cold_start_parameters_in_bounds(self):
        est = ThermalEstimator.cold_start()
        assert est.R_inv > 0
        assert est.C_inv > 0
        assert est.R_value > 0
        assert est.thermal_mass > 0


class TestEKFConvergence:
    """Test that the EKF converges to true parameters from synthetic data."""

    def test_passive_drift_convergence(self):
        """EKF should learn R from passive drift (HVAC off) periods."""
        np.random.seed(42)

        # True building parameters
        TRUE_R = 8.0       # °F·hr/BTU
        TRUE_C = 1000.0    # BTU/°F
        TRUE_Q_COOL = 24000.0
        TRUE_Q_HEAT = 20000.0

        # Generate 2 days of HVAC-off data with varying outdoor temps
        n_steps = 576  # 2 days at 5-min intervals
        outdoor = [60 + 15 * math.sin(2 * math.pi * i / 288) for i in range(n_steps)]
        modes = ["off"] * n_steps
        running = [False] * n_steps

        readings = simulate_building(
            TRUE_R, TRUE_C, TRUE_Q_COOL, TRUE_Q_HEAT,
            outdoor, modes, running,
            initial_indoor=72.0, noise_std=0.1,
        )

        est = ThermalEstimator.cold_start(indoor_temp=72.0)

        for i in range(n_steps):
            est.update(
                observed_temp=readings[i],
                outdoor_temp=outdoor[i],
                hvac_mode="off",
                hvac_running=False,
            )

        # R_inv should converge toward 1/TRUE_R = 0.125
        learned_R = est.R_value
        assert 3.0 < learned_R < 20.0, f"R={learned_R} not in reasonable range"

    def test_cooling_capacity_convergence(self):
        """EKF should learn Q_cool from cooling periods."""
        np.random.seed(123)

        TRUE_R = 8.0
        TRUE_C = 1000.0
        TRUE_Q_COOL = 24000.0
        TRUE_Q_HEAT = 20000.0

        # 3 days: mix of cooling and off periods
        n_steps = 864
        outdoor = [85 + 10 * math.sin(2 * math.pi * i / 288) for i in range(n_steps)]
        # Cooling during the day (when outdoor > 80), off at night
        modes = ["cool" if outdoor[i] > 80 else "off" for i in range(n_steps)]
        running = [modes[i] == "cool" for i in range(n_steps)]

        readings = simulate_building(
            TRUE_R, TRUE_C, TRUE_Q_COOL, TRUE_Q_HEAT,
            outdoor, modes, running,
            initial_indoor=72.0, noise_std=0.1,
        )

        est = ThermalEstimator.cold_start(indoor_temp=72.0)

        for i in range(n_steps):
            est.update(
                observed_temp=readings[i],
                outdoor_temp=outdoor[i],
                hvac_mode=modes[i],
                hvac_running=running[i],
            )

        # Q_cool should move toward TRUE_Q_COOL
        learned_Q = float(est.x[IDX_Q_COOL])
        assert 10000 < learned_Q < 50000, f"Q_cool={learned_Q} not in reasonable range"
        assert est.confidence > 0.0, "Confidence should be above zero after data"

    def test_confidence_increases_with_observations(self):
        """Confidence should increase as more data comes in."""
        np.random.seed(456)
        est = ThermalEstimator.cold_start(indoor_temp=72.0)

        confidences = []
        for i in range(500):
            outdoor = 75 + 10 * math.sin(2 * math.pi * i / 288)
            est.update(
                observed_temp=72.0 + np.random.normal(0, 0.1),
                outdoor_temp=outdoor,
                hvac_mode="off",
                hvac_running=False,
            )
            if i % 50 == 0:
                confidences.append(est.confidence)

        # Confidence should generally increase
        assert confidences[-1] > confidences[0], (
            f"Confidence should increase: {confidences}"
        )


class TestBeestatPriming:
    """Test initialization from Beestat profile data."""

    @pytest.fixture
    def beestat_profile(self):
        """Load the actual Beestat profile if available."""
        profile_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "Temperature Profile - 2026-03-06.json",
        )
        if not os.path.exists(profile_path):
            pytest.skip("Beestat profile not found")
        with open(profile_path) as f:
            return json.load(f)

    def test_from_beestat_creates_valid_state(self, beestat_profile):
        est = ThermalEstimator.from_beestat(beestat_profile)
        assert est.R_inv > 0
        assert est.C_inv > 0
        assert float(est.x[IDX_Q_COOL]) > 5000
        assert float(est.x[IDX_Q_HEAT]) > 5000

    def test_from_beestat_lower_uncertainty(self, beestat_profile):
        cold = ThermalEstimator.cold_start()
        primed = ThermalEstimator.from_beestat(beestat_profile)

        cold_trace = np.trace(cold.P)
        primed_trace = np.trace(primed.P)
        assert primed_trace < cold_trace, (
            "Beestat-primed should have lower uncertainty"
        )


class TestPersistence:
    """Test state serialization and restoration."""

    def test_round_trip(self):
        """State should survive serialization → deserialization."""
        np.random.seed(789)
        est = ThermalEstimator.cold_start(indoor_temp=71.5)

        # Feed some data
        for i in range(50):
            est.update(
                observed_temp=71.5 + np.random.normal(0, 0.1),
                outdoor_temp=80.0,
                hvac_mode="cool",
                hvac_running=True,
            )

        # Serialize
        data = est.to_dict()

        # Restore
        restored = ThermalEstimator.from_dict(data)

        # Verify
        np.testing.assert_array_almost_equal(est.x, restored.x, decimal=10)
        np.testing.assert_array_almost_equal(est.P, restored.P, decimal=10)
        assert est._n_obs == restored._n_obs
        assert est.R_meas == restored.R_meas

    def test_persistence_no_sample_loss(self):
        """Unlike ModelTracker, the Kalman state persists fully."""
        est = ThermalEstimator.cold_start()
        for i in range(100):
            est.update(72.0, 80.0, "off", False)

        original_R_inv = est.R_inv
        original_confidence = est.confidence

        # Simulate restart
        data = est.to_dict()
        restored = ThermalEstimator.from_dict(data)

        assert restored.R_inv == original_R_inv
        # Confidence preserved (innovations are lost but n_obs is kept)
        assert restored._n_obs == 100


class TestAdaptivePerformanceModel:
    """Test the AdaptivePerformanceModel interface."""

    def test_interface_compatibility(self):
        """Should implement same methods as PerformanceModel."""
        est = ThermalEstimator.cold_start()
        model = AdaptivePerformanceModel(est)

        # All these should work without error
        drift = model.passive_drift(80.0)
        cool = model.cooling_delta(80.0)
        heat = model.heating_delta(30.0)
        aux = model.aux_heating_delta(20.0)
        eff = model.relative_efficiency(80.0, "cool")
        rt = model.runtime_needed(80.0, "cool", 3.0)
        coast = model.coast_duration(80.0, "cool", 2.0)
        bp = model.resist_balance_point

        # Basic physics checks
        assert cool < 0, "Cooling delta should be negative"
        assert heat > 0, "Heating delta should be positive"
        assert rt > 0, "Runtime should be positive"

    def test_cooling_delta_degrades_with_heat(self):
        """Cooling should be less effective at higher outdoor temps."""
        est = ThermalEstimator.cold_start()
        model = AdaptivePerformanceModel(est)

        cool_75 = model.cooling_delta(75.0)
        cool_95 = model.cooling_delta(95.0)

        # At 95°F, cooling is less effective (less negative) than at 75°F
        assert cool_95 > cool_75, (
            f"Cooling at 95°F ({cool_95}) should be less negative than at 75°F ({cool_75})"
        )

    def test_passive_drift_direction(self):
        """Drift should be positive when hot outside, negative when cold."""
        est = ThermalEstimator.cold_start(indoor_temp=72.0)
        model = AdaptivePerformanceModel(est)

        drift_hot = model.passive_drift(95.0)
        drift_cold = model.passive_drift(30.0)

        assert drift_hot > 0, "Hot outdoor should cause positive drift"
        assert drift_cold < 0, "Cold outdoor should cause negative drift"

    def test_confidence_property(self):
        """Confidence should be accessible via the model."""
        est = ThermalEstimator.cold_start()
        model = AdaptivePerformanceModel(est)
        assert 0.0 <= model.confidence <= 1.0

    def test_summary(self):
        """Summary should produce readable output."""
        est = ThermalEstimator.cold_start()
        model = AdaptivePerformanceModel(est)
        summary = model.summary()
        assert "Kalman Filter" in summary
        assert "Confidence" in summary
        assert "Cooling" in summary


class TestNumericalStability:
    """Test that the EKF doesn't diverge under edge cases."""

    def test_constant_temperature(self):
        """Filter should handle constant readings without divergence."""
        est = ThermalEstimator.cold_start(indoor_temp=72.0)

        for _ in range(1000):
            est.update(72.0, 72.0, "off", False)

        assert np.all(np.isfinite(est.x)), "State should remain finite"
        assert np.all(np.isfinite(est.P)), "Covariance should remain finite"
        assert np.all(np.diag(est.P) >= 0), "Covariance diagonal should be non-negative"

    def test_extreme_temperatures(self):
        """Filter should handle extreme outdoor temps without divergence."""
        est = ThermalEstimator.cold_start()

        temps = [0, 10, -10, 110, 105, 5, 100, -5]
        for t in temps:
            est.update(72.0, t, "off", False)

        assert np.all(np.isfinite(est.x)), "State should remain finite"
        assert np.all(np.isfinite(est.P)), "Covariance should remain finite"

    def test_rapid_mode_switching(self):
        """Filter should handle rapid HVAC mode changes."""
        est = ThermalEstimator.cold_start()

        for i in range(200):
            mode = ["cool", "heat", "off"][i % 3]
            running = mode != "off"
            est.update(72.0, 80.0, mode, running)

        assert np.all(np.isfinite(est.x))
        assert np.all(np.isfinite(est.P))

    def test_parameter_bounds_enforced(self):
        """Parameters should stay within physical bounds."""
        est = ThermalEstimator.cold_start()

        # Feed deliberately misleading data
        for _ in range(100):
            est.update(
                observed_temp=72.0,
                outdoor_temp=72.0,
                hvac_mode="cool",
                hvac_running=True,
            )

        # All parameters should be clamped
        assert est.R_inv > 0
        assert est.C_inv > 0
        assert float(est.x[IDX_Q_COOL]) >= 5000
        assert float(est.x[IDX_Q_HEAT]) >= 5000


class TestAccuracyReporting:
    """Test accuracy stats and innovation tracking."""

    def test_innovation_tracking(self):
        est = ThermalEstimator.cold_start(indoor_temp=72.0)

        innovations = []
        for i in range(50):
            inn = est.update(72.0 + 0.1 * np.sin(i), 80.0, "off", False)
            innovations.append(inn)

        mae = est.mean_absolute_error
        bias = est.mean_signed_error
        assert mae is not None
        assert bias is not None
        assert mae >= 0

    def test_accuracy_report_format(self):
        est = ThermalEstimator.cold_start()
        est.update(72.0, 80.0, "off", False)

        report = est.get_accuracy_report()
        for mode in ("cool", "heat", "resist"):
            assert mode in report
            assert "samples" in report[mode]
            assert "mae" in report[mode]
            assert "correction" in report[mode]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
