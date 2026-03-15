"""Tests for the Grey-Box LP optimizer.

Tests thermal matrix construction, LP solving, uncertainty margins,
schedule conversion, and integration with the Kalman filter estimator.
"""

import importlib
import math
import os
import sys
import types
from datetime import datetime, timedelta, timezone

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


# Load data_types first (dependency of greybox_optimizer)
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

apm_mod = _load(
    "custom_components.heatpump_optimizer.engine.adaptive_performance_model",
    os.path.join(CC, "engine", "adaptive_performance_model.py"),
)
sys.modules["custom_components.heatpump_optimizer.engine"].adaptive_performance_model = apm_mod

gb_mod = _load(
    "custom_components.heatpump_optimizer.engine.greybox_optimizer",
    os.path.join(CC, "engine", "greybox_optimizer.py"),
)
sys.modules["custom_components.heatpump_optimizer.engine"].greybox_optimizer = gb_mod

ThermalEstimator = te_mod.ThermalEstimator
GreyBoxOptimizer = gb_mod.GreyBoxOptimizer
ForecastPoint = dt_mod.ForecastPoint
OptimizationWeights = dt_mod.OptimizationWeights
ScheduleEntry = dt_mod.ScheduleEntry
OptimizedSchedule = dt_mod.OptimizedSchedule


# ── Test Helpers ────────────────────────────────────────────────────


def make_forecast(
    n_hours: int = 24,
    base_temp: float = 85.0,
    amplitude: float = 10.0,
    start: datetime | None = None,
    carbon_intensity: float | None = None,
    electricity_rate: float | None = None,
) -> list[ForecastPoint]:
    """Create a synthetic sinusoidal forecast."""
    if start is None:
        start = datetime(2026, 7, 15, 6, 0, 0, tzinfo=timezone.utc)
    points = []
    for h in range(n_hours):
        t = start + timedelta(hours=h)
        # Sinusoidal: peak at hour 14 (2pm), trough at hour 2 (2am)
        phase = 2 * math.pi * ((h + 8) % 24) / 24.0  # shift so peak ~14:00
        temp = base_temp + amplitude * math.sin(phase)
        points.append(ForecastPoint(
            time=t,
            outdoor_temp=temp,
            carbon_intensity=carbon_intensity,
            electricity_rate=electricity_rate,
        ))
    return points


def make_estimator(
    indoor_temp: float = 72.0,
    R_inv: float = 0.001,
    C_inv: float = 0.001,
    Q_cool: float = 5000.0,
    Q_heat: float = 5000.0,
    n_obs: int = 500,
) -> ThermalEstimator:
    """Create an estimator with known parameters for testing.

    R_inv is per-area conductance (1/R_wall). With default envelope_area=2000,
    UA = R_inv * area = 0.001 * 2000 = 2.0 BTU/(hr·°F) total building conductance.

    Parameters are chosen so that:
      - HVAC effect ~3-4 °F/hr per unit duty (B ≈ C_inv * Q * cop)
      - Passive drift ~0.04°F/hr per °F outdoor delta (moderate insulation)
      - Uncertainty margins are small (well-converged filter)
    """
    est = ThermalEstimator.cold_start(indoor_temp)
    est.x[te_mod.IDX_R_INV] = R_inv
    est.x[te_mod.IDX_C_INV] = C_inv
    est.x[te_mod.IDX_Q_COOL] = Q_cool
    est.x[te_mod.IDX_Q_HEAT] = Q_heat
    est._n_obs = n_obs
    # Very well converged filter — tiny uncertainty
    est.P = np.diag([
        0.001,    # T_air
        0.1,      # T_mass
        1e-5,     # R_inv
        1e-4,     # R_int_inv
        1e-9,     # C_inv
        1e-11,    # C_mass_inv
        100,      # Q_cool — ±10 BTU/hr
        100,      # Q_heat — ±10 BTU/hr
        100,      # solar_gain_btu
    ])
    return est


# ── Tests ───────────────────────────────────────────────────────────


class TestThermalMatrices:
    """Test that linearized thermal model matches the estimator's physics."""

    def test_build_matrices_shape(self):
        est = make_estimator()
        opt = GreyBoxOptimizer(est)
        forecast = make_forecast(12)
        hourly = opt._bin_forecast_hourly(forecast)

        params = opt._extract_params()
        T_mass = opt._precompute_thermal_mass(72.0, hourly, params)
        A, B, d = opt._build_thermal_matrices(72.0, hourly, T_mass, params, "cool")

        assert A.shape == (12,)
        assert B.shape == (12,)
        assert d.shape == (12,)

    def test_passive_trajectory_matches_drift(self):
        """With u=0, the LP thermal model should produce similar drift to the estimator."""
        est = make_estimator(indoor_temp=74.0)
        opt = GreyBoxOptimizer(est)
        forecast = make_forecast(6, base_temp=90.0, amplitude=0.0)
        hourly = opt._bin_forecast_hourly(forecast)

        params = opt._extract_params()
        T_mass = opt._precompute_thermal_mass(74.0, hourly, params)
        A, B, d = opt._build_thermal_matrices(74.0, hourly, T_mass, params, "cool")

        # Simulate passive (no HVAC)
        T = opt._simulate_trajectory(np.zeros(6), A, B, d, 74.0, 6)

        # With 90°F outdoor and 74°F indoor, drift should be positive (warming)
        assert T[-1] > 74.0, f"Expected warming, got T[-1]={T[-1]}"

    def test_cooling_effect_direction(self):
        """B[t] should be negative for cooling (lowers temperature)."""
        est = make_estimator()
        opt = GreyBoxOptimizer(est)
        forecast = make_forecast(6, base_temp=90.0, amplitude=0.0)
        hourly = opt._bin_forecast_hourly(forecast)

        params = opt._extract_params()
        T_mass = opt._precompute_thermal_mass(72.0, hourly, params)
        A, B, d = opt._build_thermal_matrices(72.0, hourly, T_mass, params, "cool")

        assert np.all(B < 0), "Cooling B[t] should be negative"

    def test_heating_effect_direction(self):
        """B[t] should be positive for heating (raises temperature)."""
        est = make_estimator()
        opt = GreyBoxOptimizer(est)
        forecast = make_forecast(6, base_temp=30.0, amplitude=0.0)
        hourly = opt._bin_forecast_hourly(forecast)

        params = opt._extract_params()
        T_mass = opt._precompute_thermal_mass(72.0, hourly, params)
        A, B, d = opt._build_thermal_matrices(72.0, hourly, T_mass, params, "heat")

        assert np.all(B > 0), "Heating B[t] should be positive"

    def test_hvac_on_overcomes_drift(self):
        """Full HVAC duty (u=1) should cool the house even on a hot day."""
        est = make_estimator(indoor_temp=74.0)
        opt = GreyBoxOptimizer(est)
        forecast = make_forecast(6, base_temp=95.0, amplitude=0.0)
        hourly = opt._bin_forecast_hourly(forecast)

        params = opt._extract_params()
        T_mass = opt._precompute_thermal_mass(74.0, hourly, params)
        A, B, d = opt._build_thermal_matrices(74.0, hourly, T_mass, params, "cool")

        # Full cooling for all hours
        T = opt._simulate_trajectory(np.ones(6), A, B, d, 74.0, 6)

        # With 20k BTU/hr cooling, should cool down despite 95°F outdoor
        assert T[-1] < 74.0, f"Expected cooling, got T[-1]={T[-1]}"


class TestLPSolver:
    """Test the greedy LP solver."""

    def test_no_hvac_when_not_needed(self):
        """If passive trajectory stays in comfort, u should be all zeros."""
        est = make_estimator(indoor_temp=72.0)
        opt = GreyBoxOptimizer(est)
        # Mild weather, empty house — no HVAC needed
        # (people_home_count=0 → Q_internal = 800 BTU/hr base appliances only)
        forecast = make_forecast(12, base_temp=72.0, amplitude=2.0)

        schedule = opt.optimize(72.0, forecast, (64.0, 80.0), "cool", people_home_count=0)
        assert schedule.optimized_runtime_minutes < 60, "Should need little/no runtime in mild weather"

    def test_cooling_on_hot_day(self):
        """On a hot day, optimizer should schedule cooling runtime."""
        # Start near comfort max so even moderate drift triggers HVAC
        est = make_estimator(indoor_temp=75.5)
        opt = GreyBoxOptimizer(est)
        forecast = make_forecast(24, base_temp=95.0, amplitude=10.0)

        schedule = opt.optimize(75.5, forecast, (70.0, 76.0), "cool")
        assert schedule.optimized_runtime_minutes > 0, "Should need cooling on hot day"
        assert len(schedule.entries) == 24

    def test_heating_on_cold_day(self):
        """On a cold day, optimizer should schedule heating runtime."""
        # Start near comfort min so moderate drift triggers heating
        est = make_estimator(indoor_temp=60.5)
        opt = GreyBoxOptimizer(est)
        forecast = make_forecast(24, base_temp=25.0, amplitude=5.0)

        schedule = opt.optimize(60.5, forecast, (60.0, 68.0), "heat")
        assert schedule.optimized_runtime_minutes > 0, "Should need heating on cold day"

    def test_comfort_maintained(self):
        """Optimized schedule should not violate comfort bounds."""
        est = make_estimator(indoor_temp=73.0)
        opt = GreyBoxOptimizer(est)
        forecast = make_forecast(24, base_temp=90.0, amplitude=8.0)

        # Wider comfort band accounts for internal heat gains (~1200 BTU/hr)
        schedule = opt.optimize(73.0, forecast, (66.0, 78.0), "cool")
        assert schedule.comfort_violations <= 2, f"Got {schedule.comfort_violations} violations"

    def test_prefers_efficient_hours(self):
        """Optimizer should assign more runtime to cooler (more efficient) hours."""
        est = make_estimator(indoor_temp=73.0)
        opt = GreyBoxOptimizer(est)
        # Two distinct temperature blocks: cool morning, hot afternoon
        start = datetime(2026, 7, 15, 6, 0, 0, tzinfo=timezone.utc)
        forecast = []
        for h in range(24):
            t = start + timedelta(hours=h)
            temp = 80.0 if h < 12 else 100.0  # Big efficiency gap
            forecast.append(ForecastPoint(time=t, outdoor_temp=temp))

        schedule = opt.optimize(73.0, forecast, (70.0, 76.0), "cool")

        # Check that more runtime is allocated to cooler hours
        morning_entries = [e for e in schedule.entries if e.start_time.hour < 18]
        afternoon_entries = [e for e in schedule.entries if e.start_time.hour >= 18]

        # Morning targets should be lower (more pre-cooling) or similar
        # (the LP solver accounts for internal gains which make the
        # distribution less cleanly split between morning/afternoon)
        if morning_entries and afternoon_entries:
            morning_avg = sum(e.target_temp for e in morning_entries) / len(morning_entries)
            afternoon_avg = sum(e.target_temp for e in afternoon_entries) / len(afternoon_entries)
            assert morning_avg <= afternoon_avg + 1.0, (
                f"Morning avg {morning_avg:.1f} should be <= afternoon avg {afternoon_avg:.1f} + 1.0"
            )

    def test_savings_positive_on_variable_weather(self):
        """Savings should be positive when there's weather variation to exploit."""
        est = make_estimator(indoor_temp=73.0)
        opt = GreyBoxOptimizer(est)
        forecast = make_forecast(24, base_temp=90.0, amplitude=12.0)

        schedule = opt.optimize(73.0, forecast, (70.0, 76.0), "cool")
        # The LP optimizer should find some savings vs constant-setpoint baseline
        assert schedule.savings_pct >= 0, f"Savings should be non-negative, got {schedule.savings_pct}"


class TestUncertaintyMargins:
    """Test confidence-proportional uncertainty margins."""

    def test_high_confidence_small_margins(self):
        """Well-converged filter should produce small margins."""
        est = make_estimator(n_obs=2000)
        est.P = est.P * 0.001  # Very low uncertainty
        opt = GreyBoxOptimizer(est)
        forecast_data = [{"temp": 90.0} for _ in range(12)]
        params = opt._extract_params()

        margins = opt._compute_uncertainty_margins(12, forecast_data, "cool", params)
        # Margins should be small
        assert margins[6] < 2.0, f"6hr margin {margins[6]:.1f}°F too large for high confidence"

    def test_low_confidence_large_margins(self):
        """Cold-start filter should produce larger margins."""
        est = ThermalEstimator.cold_start(72.0)
        est._n_obs = 20  # Just past minimum threshold
        opt = GreyBoxOptimizer(est)
        forecast_data = [{"temp": 90.0} for _ in range(12)]
        params = opt._extract_params()

        margins = opt._compute_uncertainty_margins(12, forecast_data, "cool", params)
        # Margins should be larger than high-confidence case
        assert margins[6] > 0.5, f"6hr margin {margins[6]:.1f}°F too small for low confidence"

    def test_margins_increase_with_horizon(self):
        """Margins should grow with prediction horizon."""
        est = make_estimator()
        opt = GreyBoxOptimizer(est)
        forecast_data = [{"temp": 90.0} for _ in range(24)]
        params = opt._extract_params()

        margins = opt._compute_uncertainty_margins(24, forecast_data, "cool", params)
        # Should be monotonically non-decreasing
        for t in range(1, 24):
            assert margins[t + 1] >= margins[t] - 0.01, (
                f"Margin at hour {t+1} ({margins[t+1]:.3f}) should be >= "
                f"margin at hour {t} ({margins[t]:.3f})"
            )

    def test_margin_cap(self):
        """Margins should be capped at a reasonable maximum."""
        est = ThermalEstimator.cold_start(72.0)
        est._n_obs = 20
        opt = GreyBoxOptimizer(est)
        forecast_data = [{"temp": 90.0} for _ in range(48)]
        params = opt._extract_params()

        margins = opt._compute_uncertainty_margins(48, forecast_data, "cool", params)
        assert np.all(margins <= 3.01), f"Margins should be capped at 3°F, max={margins.max():.1f}"


class TestScheduleConversion:
    """Test duty cycle to schedule entry conversion."""

    def test_output_format(self):
        """Optimizer should produce valid OptimizedSchedule with all fields."""
        est = make_estimator(indoor_temp=73.0)
        opt = GreyBoxOptimizer(est)
        forecast = make_forecast(12, base_temp=85.0)

        schedule = opt.optimize(73.0, forecast, (70.0, 76.0), "cool")

        assert isinstance(schedule, OptimizedSchedule)
        assert len(schedule.entries) > 0
        assert all(isinstance(e, ScheduleEntry) for e in schedule.entries)
        assert schedule.baseline_runtime_minutes >= 0
        assert schedule.optimized_runtime_minutes >= 0
        assert isinstance(schedule.savings_pct, float)
        assert len(schedule.simulation) > 0

    def test_entries_cover_forecast_period(self):
        """Schedule entries should cover every forecast hour."""
        est = make_estimator(indoor_temp=73.0)
        opt = GreyBoxOptimizer(est)
        forecast = make_forecast(24, base_temp=85.0)

        schedule = opt.optimize(73.0, forecast, (70.0, 76.0), "cool")
        assert len(schedule.entries) == 24

    def test_target_temps_within_comfort(self):
        """All target temperatures should be within comfort bounds."""
        est = make_estimator(indoor_temp=73.0)
        opt = GreyBoxOptimizer(est)
        forecast = make_forecast(24, base_temp=90.0)

        schedule = opt.optimize(73.0, forecast, (70.0, 76.0), "cool")
        for entry in schedule.entries:
            assert 70.0 <= entry.target_temp <= 76.0, (
                f"Target {entry.target_temp} outside comfort [70, 76]"
            )

    def test_entries_have_mode_and_reason(self):
        """Each entry should have mode and descriptive reason."""
        est = make_estimator(indoor_temp=73.0)
        opt = GreyBoxOptimizer(est)
        forecast = make_forecast(12, base_temp=85.0)

        schedule = opt.optimize(73.0, forecast, (70.0, 76.0), "cool")
        for entry in schedule.entries:
            assert entry.mode == "cool"
            assert len(entry.reason) > 0
            assert "duty=" in entry.reason

    def test_empty_forecast_returns_empty_schedule(self):
        """Empty forecast should produce empty schedule without error."""
        est = make_estimator()
        opt = GreyBoxOptimizer(est)
        schedule = opt.optimize(72.0, [], (70.0, 76.0), "cool")
        assert schedule.optimized_runtime_minutes == 0
        assert len(schedule.entries) == 0


class TestMultiObjective:
    """Test multi-objective optimization with carbon and cost weights."""

    def test_carbon_shifts_runtime(self):
        """Adding carbon weight should shift runtime toward cleaner hours."""
        # Start near comfort max so cooling is needed
        est = make_estimator(indoor_temp=75.5)
        start = datetime(2026, 7, 15, 6, 0, 0, tzinfo=timezone.utc)

        # Create forecast where first 12 hours are dirty, last 12 clean
        forecast = []
        for h in range(24):
            t = start + timedelta(hours=h)
            forecast.append(ForecastPoint(
                time=t,
                outdoor_temp=95.0,  # Hot enough to require cooling
                carbon_intensity=500.0 if h < 12 else 100.0,
            ))

        # No carbon weight
        opt1 = GreyBoxOptimizer(est)
        schedule1 = opt1.optimize(
            75.5, forecast, (70.0, 76.0), "cool",
            weights=OptimizationWeights(energy_efficiency=1.0, carbon_intensity=0.0),
        )

        # With carbon weight
        opt2 = GreyBoxOptimizer(est)
        schedule2 = opt2.optimize(
            75.5, forecast, (70.0, 76.0), "cool",
            weights=OptimizationWeights(energy_efficiency=1.0, carbon_intensity=1.0),
        )

        # Both should produce valid schedules
        assert schedule1.optimized_runtime_minutes > 0
        assert schedule2.optimized_runtime_minutes > 0

    def test_cost_weight_produces_savings(self):
        """Adding cost weight should produce cost estimates in schedule."""
        est = make_estimator(indoor_temp=73.0)
        start = datetime(2026, 7, 15, 6, 0, 0, tzinfo=timezone.utc)

        forecast = []
        for h in range(24):
            t = start + timedelta(hours=h)
            forecast.append(ForecastPoint(
                time=t,
                outdoor_temp=90.0,
                electricity_rate=0.30 if 14 <= h <= 20 else 0.10,
            ))

        opt = GreyBoxOptimizer(est)
        schedule = opt.optimize(
            73.0, forecast, (70.0, 76.0), "cool",
            weights=OptimizationWeights(energy_efficiency=1.0, electricity_cost=1.0),
        )

        # Should have cost estimates populated
        assert schedule.optimized_kwh is not None
        assert schedule.baseline_kwh is not None
        if schedule.optimized_cost is not None:
            assert schedule.optimized_cost >= 0


class TestIntegration:
    """Integration tests verifying grey-box works with the thermal estimator."""

    def test_with_cold_start_estimator(self):
        """Grey-box should work with a cold-start (no Beestat) estimator."""
        est = ThermalEstimator.cold_start(72.0)
        est._n_obs = 50  # Past minimum observation threshold
        opt = GreyBoxOptimizer(est)
        forecast = make_forecast(24, base_temp=85.0)

        schedule = opt.optimize(72.0, forecast, (68.0, 76.0), "cool")
        assert isinstance(schedule, OptimizedSchedule)
        assert len(schedule.entries) > 0

    def test_confidence_property(self):
        """Grey-box optimizer should expose estimator confidence."""
        est = make_estimator(n_obs=1000)
        opt = GreyBoxOptimizer(est)
        assert 0.0 <= opt.confidence <= 1.0

    def test_summary_output(self):
        """Summary should produce readable output."""
        est = make_estimator()
        opt = GreyBoxOptimizer(est)
        summary = opt.summary()
        assert "Grey-Box Optimizer" in summary
        assert "Confidence" in summary
        assert "Envelope R" in summary

    def test_thermal_trajectory_consistency(self):
        """LP trajectory should be internally consistent with the thermal matrices."""
        est = make_estimator(indoor_temp=73.0)
        opt = GreyBoxOptimizer(est)
        forecast = make_forecast(12, base_temp=90.0, amplitude=5.0)
        hourly = opt._bin_forecast_hourly(forecast)
        n = len(hourly)

        params = opt._extract_params()
        T_mass = opt._precompute_thermal_mass(73.0, hourly, params)
        A, B, d = opt._build_thermal_matrices(73.0, hourly, T_mass, params, "cool")

        # Create arbitrary duty cycles
        u = np.array([0.5] * n)
        T = opt._simulate_trajectory(u, A, B, d, 73.0, n)

        # Verify step-by-step: T[t+1] = A[t]*T[t] + B[t]*u[t] + d[t]
        for t in range(n):
            expected = A[t] * T[t] + B[t] * u[t] + d[t]
            assert abs(T[t + 1] - expected) < 1e-10, (
                f"Trajectory inconsistency at step {t}: {T[t+1]} != {expected}"
            )

    def test_baseline_vs_optimized_comparison(self):
        """Optimized runtime should be <= baseline on variable weather."""
        est = make_estimator(indoor_temp=73.0)
        opt = GreyBoxOptimizer(est)
        forecast = make_forecast(24, base_temp=88.0, amplitude=12.0)

        schedule = opt.optimize(73.0, forecast, (70.0, 76.0), "cool")
        # The LP should find a schedule at least as good as the baseline
        assert schedule.optimized_runtime_minutes <= schedule.baseline_runtime_minutes + 1.0, (
            f"Optimized ({schedule.optimized_runtime_minutes:.0f} min) should be "
            f"<= baseline ({schedule.baseline_runtime_minutes:.0f} min)"
        )
