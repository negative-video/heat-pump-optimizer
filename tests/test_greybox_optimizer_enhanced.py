"""Tests for enhanced features in the Grey-Box LP optimizer.

Tests effective outdoor temperature, direct irradiance solar gain,
humidity/pressure HVAC capacity corrections, and forecast binning
of new environmental fields.
"""

import importlib
import math
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

# ── Module loading (same pattern as test_greybox_optimizer.py) ──────

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

sa_mod = _load(
    "custom_components.heatpump_optimizer.learning.solar_adjuster",
    os.path.join(CC, "learning", "solar_adjuster.py"),
)
sys.modules["custom_components.heatpump_optimizer.learning"].solar_adjuster = sa_mod

ThermalEstimator = te_mod.ThermalEstimator
GreyBoxOptimizer = gb_mod.GreyBoxOptimizer
ForecastPoint = dt_mod.ForecastPoint
SolarAdjuster = sa_mod.SolarAdjuster


# ── Helpers ────────────────────────────────────────────────────────


def make_estimator(
    indoor_temp: float = 72.0,
    R_inv: float = 2.0,
    C_inv: float = 0.001,
    Q_cool: float = 5000.0,
    Q_heat: float = 5000.0,
    n_obs: int = 500,
) -> ThermalEstimator:
    """Create an estimator with known parameters for testing."""
    est = ThermalEstimator.cold_start(indoor_temp)
    est.x[te_mod.IDX_R_INV] = R_inv
    est.x[te_mod.IDX_C_INV] = C_inv
    est.x[te_mod.IDX_Q_COOL] = Q_cool
    est.x[te_mod.IDX_Q_HEAT] = Q_heat
    est._n_obs = n_obs
    est.P = np.diag([
        0.001, 0.1, 1e-5, 1e-4, 1e-9, 1e-11, 100, 100,
    ])
    return est


def make_forecast_with_wind(
    n_hours: int = 12,
    outdoor_temp: float = 30.0,
    wind_speed: float = 15.0,
) -> list[ForecastPoint]:
    """Create a forecast with wind speed for cold conditions."""
    start = datetime(2026, 1, 15, 6, 0, 0, tzinfo=timezone.utc)
    return [
        ForecastPoint(
            time=start + timedelta(hours=h),
            outdoor_temp=outdoor_temp,
            wind_speed_mph=wind_speed,
        )
        for h in range(n_hours)
    ]


def make_forecast_with_irradiance(
    n_hours: int = 12,
    outdoor_temp: float = 85.0,
    irradiance: float = 600.0,
) -> list[ForecastPoint]:
    """Create a forecast with direct irradiance measurements."""
    start = datetime(2026, 7, 15, 6, 0, 0, tzinfo=timezone.utc)
    return [
        ForecastPoint(
            time=start + timedelta(hours=h),
            outdoor_temp=outdoor_temp,
            solar_irradiance_w_m2=irradiance,
        )
        for h in range(n_hours)
    ]


def make_forecast_with_humidity_pressure(
    n_hours: int = 12,
    outdoor_temp: float = 90.0,
    humidity: float = 80.0,
    pressure: float = 850.0,
) -> list[ForecastPoint]:
    """Create a forecast with humidity and pressure data."""
    start = datetime(2026, 7, 15, 6, 0, 0, tzinfo=timezone.utc)
    return [
        ForecastPoint(
            time=start + timedelta(hours=h),
            outdoor_temp=outdoor_temp,
            humidity=humidity,
            pressure_hpa=pressure,
        )
        for h in range(n_hours)
    ]


# ── Tests ──────────────────────────────────────────────────────────


class TestEffectiveOutdoorTemp:
    """Test that forecast effective_outdoor_temp flows into COP calculations."""

    def test_effective_temp_used_for_cop(self):
        """The optimizer should use effective_temp (wind-adjusted) for COP."""
        est = make_estimator()
        opt = GreyBoxOptimizer(est)

        # With wind: effective temp is lower than actual
        forecast_windy = make_forecast_with_wind(6, outdoor_temp=30.0, wind_speed=20.0)
        hourly_windy = opt._bin_forecast_hourly(forecast_windy)

        # Verify the binned forecast has effective_temp different from temp
        for h in hourly_windy:
            assert h["effective_temp"] is not None
            assert h["effective_temp"] < h["temp"], (
                f"Effective temp {h['effective_temp']} should be < outdoor {h['temp']} with wind"
            )

    def test_no_wind_equals_outdoor(self):
        """Without wind, effective_temp should equal outdoor temp."""
        est = make_estimator()
        opt = GreyBoxOptimizer(est)

        start = datetime(2026, 7, 15, 6, 0, 0, tzinfo=timezone.utc)
        forecast = [
            ForecastPoint(
                time=start + timedelta(hours=h),
                outdoor_temp=85.0,
            )
            for h in range(6)
        ]

        hourly = opt._bin_forecast_hourly(forecast)
        for h in hourly:
            assert abs(h["effective_temp"] - h["temp"]) < 0.01, (
                "No-wind effective temp should equal outdoor temp"
            )

    def test_binned_in_hourly_forecast(self):
        """Effective temp should be properly averaged in hourly bins."""
        est = make_estimator()
        opt = GreyBoxOptimizer(est)

        start = datetime(2026, 1, 15, 6, 0, 0, tzinfo=timezone.utc)
        # Two points in the same hour with different wind speeds
        forecast = [
            ForecastPoint(
                time=start,
                outdoor_temp=30.0,
                wind_speed_mph=10.0,
            ),
            ForecastPoint(
                time=start + timedelta(minutes=30),
                outdoor_temp=30.0,
                wind_speed_mph=20.0,
            ),
        ]

        hourly = opt._bin_forecast_hourly(forecast)
        assert len(hourly) == 1

        # Effective temp should be average of the two effective temps
        eff1 = forecast[0].effective_outdoor_temp
        eff2 = forecast[1].effective_outdoor_temp
        expected_avg = (eff1 + eff2) / 2.0
        assert abs(hourly[0]["effective_temp"] - expected_avg) < 0.01


class TestDirectIrradianceSolarGain:
    """Test direct irradiance measurement overrides the cloud model."""

    def test_irradiance_overrides_cloud_model(self):
        """When irradiance is provided, cloud_cover should be ignored."""
        opt_class = GreyBoxOptimizer

        # With irradiance: should use direct conversion
        gain_irr = opt_class._solar_gain(
            cloud_cover=0.8,  # very cloudy
            sun_elevation=45.0,
            irradiance_w_m2=600.0,
        )

        # Without irradiance, same cloud cover: cloud model
        gain_cloud = opt_class._solar_gain(
            cloud_cover=0.8,
            sun_elevation=45.0,
            irradiance_w_m2=None,
        )

        # Direct irradiance at 600 W/m2 should give more solar than 80% cloud
        assert gain_irr > gain_cloud, (
            f"Irradiance 600 W/m2 ({gain_irr:.0f}) should exceed 80% cloud ({gain_cloud:.0f})"
        )

    def test_conversion_to_btu(self):
        """Irradiance W/m2 should be converted to BTU/hr via 3.412 factor."""
        gain = GreyBoxOptimizer._solar_gain(
            cloud_cover=None,
            sun_elevation=None,
            irradiance_w_m2=100.0,
        )
        expected = 100.0 * 3.412
        assert abs(gain - expected) < 0.01, (
            f"Expected {expected}, got {gain}"
        )

    def test_no_irradiance_falls_back(self):
        """Without irradiance, should fall back to cloud cover model."""
        gain = GreyBoxOptimizer._solar_gain(
            cloud_cover=0.0,  # clear sky
            sun_elevation=45.0,
            irradiance_w_m2=None,
        )
        # clear_sky=1.0, altitude_factor=sin(45)~0.707
        expected = 3000.0 * 1.0 * math.sin(math.radians(45.0))
        assert abs(gain - expected) < 1.0

    def test_zero_irradiance(self):
        """Zero irradiance should produce zero solar gain."""
        gain = GreyBoxOptimizer._solar_gain(
            cloud_cover=0.0,
            sun_elevation=45.0,
            irradiance_w_m2=0.0,
        )
        assert gain == 0.0


class TestHvacCapacityHumidityPressure:
    """Test humidity and pressure corrections in _hvac_capacity."""

    def test_humidity_reduces_cooling(self):
        """High humidity should reduce cooling capacity."""
        est = make_estimator()
        opt = GreyBoxOptimizer(est)
        params = opt._extract_params()

        baseline = opt._hvac_capacity("cool", 90.0, params)
        humid = opt._hvac_capacity("cool", 90.0, params, humidity=80.0)

        # Both negative; humid should be less negative
        assert humid > baseline, (
            f"Humidity should reduce cooling: {humid} vs {baseline}"
        )

    def test_pressure_affects_capacity(self):
        """Low pressure should reduce capacity."""
        est = make_estimator()
        opt = GreyBoxOptimizer(est)
        params = opt._extract_params()

        baseline = opt._hvac_capacity("cool", 90.0, params)
        low_p = opt._hvac_capacity("cool", 90.0, params, pressure_hpa=850.0)

        # Low pressure = less capacity (less negative for cooling)
        assert low_p > baseline, (
            f"Low pressure should reduce cooling: {low_p} vs {baseline}"
        )

    def test_baseline_without_corrections(self):
        """No humidity/pressure should match the basic COP formula."""
        est = make_estimator()
        opt = GreyBoxOptimizer(est)
        params = opt._extract_params()

        result = opt._hvac_capacity("cool", 90.0, params)

        # Manual calculation
        cop = max(0.1, 1.0 - 0.012 * (90.0 - 75.0))
        expected = -params["Q_cool_base"] * cop

        assert abs(result - expected) < 0.01


class TestBinForecastHourly:
    """Test that new environmental fields are properly binned."""

    def test_humidity_averaged(self):
        """Humidity values should be averaged across sub-hourly points."""
        est = make_estimator()
        opt = GreyBoxOptimizer(est)

        start = datetime(2026, 7, 15, 6, 0, 0, tzinfo=timezone.utc)
        forecast = [
            ForecastPoint(time=start, outdoor_temp=85.0, humidity=60.0),
            ForecastPoint(time=start + timedelta(minutes=30), outdoor_temp=85.0, humidity=80.0),
        ]

        hourly = opt._bin_forecast_hourly(forecast)
        assert len(hourly) == 1
        assert abs(hourly[0]["humidity"] - 70.0) < 0.01

    def test_irradiance_averaged(self):
        """Irradiance values should be averaged across sub-hourly points."""
        est = make_estimator()
        opt = GreyBoxOptimizer(est)

        start = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
        forecast = [
            ForecastPoint(time=start, outdoor_temp=85.0, solar_irradiance_w_m2=400.0),
            ForecastPoint(time=start + timedelta(minutes=30), outdoor_temp=85.0, solar_irradiance_w_m2=600.0),
        ]

        hourly = opt._bin_forecast_hourly(forecast)
        assert abs(hourly[0]["solar_irradiance"] - 500.0) < 0.01

    def test_pressure_averaged(self):
        """Pressure values should be averaged across sub-hourly points."""
        est = make_estimator()
        opt = GreyBoxOptimizer(est)

        start = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
        forecast = [
            ForecastPoint(time=start, outdoor_temp=85.0, pressure_hpa=1010.0),
            ForecastPoint(time=start + timedelta(minutes=30), outdoor_temp=85.0, pressure_hpa=1020.0),
        ]

        hourly = opt._bin_forecast_hourly(forecast)
        assert abs(hourly[0]["pressure_hpa"] - 1015.0) < 0.01

    def test_effective_temp_averaged(self):
        """Effective outdoor temp should be averaged across sub-hourly points."""
        est = make_estimator()
        opt = GreyBoxOptimizer(est)

        start = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        forecast = [
            ForecastPoint(time=start, outdoor_temp=30.0, wind_speed_mph=10.0),
            ForecastPoint(time=start + timedelta(minutes=30), outdoor_temp=30.0, wind_speed_mph=20.0),
        ]

        hourly = opt._bin_forecast_hourly(forecast)
        eff1 = forecast[0].effective_outdoor_temp
        eff2 = forecast[1].effective_outdoor_temp
        expected = (eff1 + eff2) / 2.0
        assert abs(hourly[0]["effective_temp"] - expected) < 0.01

    def test_none_values_excluded(self):
        """None values should be excluded from averages (not zero)."""
        est = make_estimator()
        opt = GreyBoxOptimizer(est)

        start = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
        forecast = [
            ForecastPoint(time=start, outdoor_temp=85.0, humidity=80.0, pressure_hpa=1010.0),
            ForecastPoint(time=start + timedelta(minutes=30), outdoor_temp=85.0, humidity=None, pressure_hpa=None),
        ]

        hourly = opt._bin_forecast_hourly(forecast)
        # Only the first point has humidity and pressure
        assert abs(hourly[0]["humidity"] - 80.0) < 0.01
        assert abs(hourly[0]["pressure_hpa"] - 1010.0) < 0.01


class TestSolarAdjusterIrradiance:
    """Test solar adjuster irradiance path."""

    def test_irradiance_overrides_cloud_model(self):
        """When irradiance_w_m2 is provided, cloud_cover is ignored."""
        sa = SolarAdjuster(latitude=37.9, solar_coefficient=0.3)
        now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

        # With irradiance, regardless of cloud cover
        factor_irr = sa.adjustment_factor(now, cloud_cover=0.9, irradiance_w_m2=600.0)

        # Without irradiance, same cloudy conditions
        factor_cloud = sa.adjustment_factor(now, cloud_cover=0.9, sun_elevation=45.0)

        # 600 W/m2 is bright; should produce a higher factor than 90% cloud
        assert factor_irr > factor_cloud, (
            f"Irradiance factor {factor_irr} should exceed cloudy factor {factor_cloud}"
        )

    def test_high_irradiance_increases_factor(self):
        """High irradiance should increase the adjustment factor above 1.0."""
        sa = SolarAdjuster(latitude=37.9, solar_coefficient=0.3)
        now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

        factor = sa.adjustment_factor(now, cloud_cover=None, irradiance_w_m2=800.0)
        assert factor > 1.0, f"High irradiance should produce factor > 1.0, got {factor}"

    def test_low_irradiance_decreases_factor(self):
        """Very low irradiance should decrease the adjustment factor below 1.0."""
        sa = SolarAdjuster(latitude=37.9, solar_coefficient=0.3)
        now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

        factor = sa.adjustment_factor(now, cloud_cover=None, irradiance_w_m2=50.0)
        assert factor < 1.0, f"Low irradiance should produce factor < 1.0, got {factor}"

    def test_zero_irradiance(self):
        """Zero irradiance should produce a factor at or near the lower end."""
        sa = SolarAdjuster(latitude=37.9, solar_coefficient=0.3)
        now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

        factor = sa.adjustment_factor(now, cloud_cover=None, irradiance_w_m2=0.0)
        assert factor < 1.0, f"Zero irradiance should give factor < 1.0, got {factor}"

    def test_clamped_range(self):
        """Adjustment factor should be clamped between 0.5 and 2.0."""
        sa = SolarAdjuster(latitude=37.9, solar_coefficient=0.3)
        now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

        # Extremely high irradiance
        factor_high = sa.adjustment_factor(now, cloud_cover=None, irradiance_w_m2=10000.0)
        assert factor_high <= 2.0, f"Factor should be capped at 2.0, got {factor_high}"

        # Zero irradiance
        factor_low = sa.adjustment_factor(now, cloud_cover=None, irradiance_w_m2=0.0)
        assert factor_low >= 0.5, f"Factor should be floored at 0.5, got {factor_low}"

    def test_none_uses_cloud_model(self):
        """None irradiance should fall through to cloud cover model."""
        sa = SolarAdjuster(latitude=37.9, solar_coefficient=0.3)
        now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

        factor_cloud = sa.adjustment_factor(now, cloud_cover=0.2, sun_elevation=45.0, irradiance_w_m2=None)
        factor_no_irr = sa.adjustment_factor(now, cloud_cover=0.2, sun_elevation=45.0)

        assert factor_cloud == factor_no_irr, "None irradiance should use cloud model"

    def test_adjust_drift_rate_passes_irradiance(self):
        """adjust_drift_rate should forward irradiance to adjustment_factor."""
        sa = SolarAdjuster(latitude=37.9, solar_coefficient=0.3)
        now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

        base_drift = 0.5
        adjusted_irr = sa.adjust_drift_rate(
            base_drift, 85.0, 50.0, now,
            cloud_cover=None, irradiance_w_m2=700.0,
        )
        adjusted_none = sa.adjust_drift_rate(
            base_drift, 85.0, 50.0, now,
            cloud_cover=None, irradiance_w_m2=None,
        )

        # With high irradiance, drift should be amplified (factor > 1)
        # Without irradiance and None cloud_cover, factor=1.0 so drift unchanged
        assert adjusted_none == base_drift, "None irradiance+cloud should leave drift unchanged"
        assert adjusted_irr != base_drift, "Irradiance should modify drift"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
