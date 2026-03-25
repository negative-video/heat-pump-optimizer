"""Tests for CoefficientCalibrator — daily slow-loop calibration."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.heatpump_optimizer.learning.coefficient_calibrator import (
    CALIBRATION_INTERVAL_HOURS,
    MIN_HOURS_BEFORE_START,
    MIN_SAMPLES,
    CoefficientCalibrator,
)
from custom_components.heatpump_optimizer.learning.coefficient_store import (
    CoefficientStore,
)
from custom_components.heatpump_optimizer.learning.sensitivity import (
    compute_sensitivities,
)


def _make_innovation_record(
    innovation: float = 0.0,
    wind_speed: float | None = 5.0,
    outdoor_temp: float = 50.0,
    indoor_temp: float = 72.0,
    ua_value: float = 200.0,
    infiltration_factor: float = 1.1,
    hvac_mode: str = "off",
    hvac_running: bool = False,
    q_hvac: float = 0.0,
    people_count: int | None = None,
    attic_temp: float | None = None,
    sun_elevation: float | None = -10.0,
    dt_hours: float = 5.0 / 60.0,
    timestamp: str = "2026-03-25T12:00:00+00:00",
    **kwargs,
) -> dict:
    """Build a minimal conditioned innovation record for testing."""
    rec = {
        "timestamp": timestamp,
        "innovation": innovation,
        "wind_speed_mph": wind_speed,
        "outdoor_temp": outdoor_temp,
        "indoor_temp": indoor_temp,
        "effective_outdoor_temp": outdoor_temp,
        "ua_value": ua_value,
        "infiltration_factor": infiltration_factor,
        "hvac_mode": hvac_mode,
        "hvac_running": hvac_running,
        "q_hvac": q_hvac,
        "people_count": people_count,
        "attic_temp": attic_temp,
        "attic_contribution_btu": 0.0,
        "crawlspace_temp": None,
        "crawlspace_contribution_btu": 0.0,
        "sun_elevation": sun_elevation,
        "precipitation": False,
        "doors_windows_open": 0,
        "dt_hours": dt_hours,
        "q_env": ua_value * infiltration_factor * (outdoor_temp - indoor_temp),
        "q_solar": 0.0,
        "q_internal": 800.0,
        "q_boundary": 0.0,
    }
    rec.update(kwargs)
    return rec


class TestSensitivityCalculator:
    """Test that sensitivities are computed correctly."""

    def test_wind_sensitivity_zero_when_no_wind(self):
        rec = _make_innovation_record(wind_speed=None)
        sens = compute_sensitivities(rec)
        assert sens["wind_infiltration"] == 0.0

    def test_wind_sensitivity_nonzero_when_windy(self):
        rec = _make_innovation_record(wind_speed=10.0)
        sens = compute_sensitivities(rec)
        assert sens["wind_infiltration"] != 0.0

    def test_attic_sensitivity_zero_when_no_sensor(self):
        rec = _make_innovation_record(attic_temp=None)
        sens = compute_sensitivities(rec)
        assert sens["k_attic"] == 0.0

    def test_alpha_cool_sensitivity_only_when_cooling(self):
        rec_off = _make_innovation_record(hvac_mode="off", hvac_running=False)
        rec_cool = _make_innovation_record(
            hvac_mode="cool", hvac_running=True, q_hvac=-20000, outdoor_temp=95,
        )
        sens_off = compute_sensitivities(rec_off)
        sens_cool = compute_sensitivities(rec_cool)
        assert sens_off["alpha_cool"] == 0.0
        assert sens_cool["alpha_cool"] != 0.0

    def test_precipitation_sensitivity_only_when_raining(self):
        rec_dry = _make_innovation_record(precipitation=False)
        rec_rain = _make_innovation_record(precipitation=True)
        sens_dry = compute_sensitivities(rec_dry)
        sens_rain = compute_sensitivities(rec_rain)
        assert sens_dry["precipitation_offset"] == 0.0
        assert sens_rain["precipitation_offset"] != 0.0

    def test_internal_gain_base_always_nonzero(self):
        rec = _make_innovation_record()
        sens = compute_sensitivities(rec)
        assert sens["internal_gain_base"] > 0.0


class TestCalibratorColdStart:
    """Calibrator should not run before enough data is accumulated."""

    def test_should_not_calibrate_immediately(self):
        store = CoefficientStore()
        cal = CoefficientCalibrator(store)
        now = datetime.now(timezone.utc)
        assert cal.should_calibrate(now) is False

    def test_should_not_calibrate_before_holdoff(self):
        store = CoefficientStore()
        cal = CoefficientCalibrator(store)
        cal._first_innovation_time = datetime.now(timezone.utc) - timedelta(hours=10)
        now = datetime.now(timezone.utc)
        assert cal.should_calibrate(now) is False

    def test_should_calibrate_after_holdoff(self):
        store = CoefficientStore()
        cal = CoefficientCalibrator(store)
        cal._first_innovation_time = datetime.now(timezone.utc) - timedelta(
            hours=MIN_HOURS_BEFORE_START + 1
        )
        now = datetime.now(timezone.utc)
        assert cal.should_calibrate(now) is True


class TestCalibratorRegression:
    """Test the core ridge regression calibration."""

    def test_insufficient_samples_skips(self):
        store = CoefficientStore()
        cal = CoefficientCalibrator(store)
        result = cal.calibrate([_make_innovation_record() for _ in range(10)])
        assert result.get("skipped") is True
        assert result["reason"] == "insufficient_samples"

    def test_white_noise_innovations_leave_multipliers_near_one(self):
        """When innovations are random noise, multipliers should stay ~1.0."""
        store = CoefficientStore()
        cal = CoefficientCalibrator(store)
        rng = np.random.default_rng(42)

        records = []
        base_time = datetime(2026, 3, 25, tzinfo=timezone.utc)
        for i in range(300):
            ts = (base_time + timedelta(minutes=5 * i)).isoformat()
            rec = _make_innovation_record(
                innovation=float(rng.normal(0, 0.3)),
                wind_speed=float(rng.uniform(0, 15)),
                outdoor_temp=float(rng.uniform(40, 90)),
                timestamp=ts,
            )
            records.append(rec)

        result = cal.calibrate(records, dry_run=False)
        assert not result.get("skipped")
        assert result["applied"] is True

        # All multipliers should remain very close to 1.0
        for name, adj in result["adjustments"].items():
            assert abs(adj["proposed"] - 1.0) < 0.05, (
                f"{name} drifted to {adj['proposed']}"
            )

    def test_known_wind_bias_detection(self):
        """Innovations correlated with wind speed → wind coefficient adjusts."""
        store = CoefficientStore()
        cal = CoefficientCalibrator(store)

        records = []
        base_time = datetime(2026, 3, 25, tzinfo=timezone.utc)
        for i in range(300):
            ts = (base_time + timedelta(minutes=5 * i)).isoformat()
            wind = 5.0 + 10.0 * (i % 2)  # alternates 5 and 15 mph
            # Positive bias correlated with wind → wind coeff too high
            innovation = 0.1 * wind + np.random.default_rng(i).normal(0, 0.1)
            rec = _make_innovation_record(
                innovation=float(innovation),
                wind_speed=wind,
                outdoor_temp=40.0,
                timestamp=ts,
            )
            records.append(rec)

        result = cal.calibrate(records, dry_run=True)
        assert not result.get("skipped")

        wind_adj = result["adjustments"]["wind_infiltration"]
        # Should propose a change (nonzero delta)
        assert abs(wind_adj["delta"]) > 1e-6, "Expected wind adjustment"

    def test_dry_run_does_not_modify_store(self):
        store = CoefficientStore()
        cal = CoefficientCalibrator(store)

        records = [
            _make_innovation_record(
                innovation=0.5,
                wind_speed=10.0,
                timestamp=(datetime(2026, 3, 25, tzinfo=timezone.utc) + timedelta(minutes=5 * i)).isoformat(),
            )
            for i in range(250)
        ]

        result = cal.calibrate(records, dry_run=True)
        assert result.get("dry_run") is True
        # Store should be unchanged
        assert store.get_multiplier("wind_infiltration") == 1.0
        assert store.calibration_count == 0


class TestCalibratorPersistence:
    """Round-trip serialization."""

    def test_round_trip(self):
        store = CoefficientStore()
        cal = CoefficientCalibrator(store)
        cal._last_calibration = datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc)
        cal._first_innovation_time = datetime(2026, 3, 20, 0, 0, tzinfo=timezone.utc)
        cal._proposed_multipliers = {"wind_infiltration": 0.85}

        data = cal.to_dict()
        restored = CoefficientCalibrator.from_dict(data, store)

        assert restored._last_calibration == cal._last_calibration
        assert restored._first_innovation_time == cal._first_innovation_time
        assert restored._proposed_multipliers == {"wind_infiltration": 0.85}
