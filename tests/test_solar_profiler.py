"""Tests for solar-aware profiler binning.

Covers:
- Solar condition classification from attic delta and sun elevation
- Solar condition classification from forecast cloud cover
- Solar-aware 2D binning: separate trendlines for sunny/cloudy/night
- Backward compatibility: aggregate bins still work without attic data
- Serialization round-trip preserves solar bins
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

from conftest import CC, load_module

pp_mod = load_module(
    "custom_components.heatpump_optimizer.learning.performance_profiler",
    os.path.join(CC, "learning", "performance_profiler.py"),
)

PerformanceProfiler = pp_mod.PerformanceProfiler
ATTIC_DELTA_SUNNY_THRESHOLD = pp_mod.ATTIC_DELTA_SUNNY_THRESHOLD

# ── Helpers ──────────────────────────────────────────────────────────

NOW = datetime(2026, 4, 7, 12, 0, 0, tzinfo=timezone.utc)


def _record(profiler, indoor, outdoor, action="idle", mode="heat",
            attic=None, sun_elev=30.0, ts_offset_min=0):
    """Helper to record a single observation."""
    ts = NOW + timedelta(minutes=ts_offset_min)
    return profiler.record_observation(
        indoor_temp=indoor,
        outdoor_temp=outdoor,
        hvac_action=action,
        hvac_mode=mode,
        now=ts,
        attic_temp=attic,
        sun_elevation=sun_elev,
    )


def _build_profiler_with_solar_data():
    """Build a profiler with enough solar-binned resist data for trendlines.

    Simulates: sunny and cloudy observations at various outdoor temps.
    Sunny: attic = outdoor + 20 (delta > 10 threshold)
    Cloudy: attic = outdoor + 5 (delta < 10 threshold)
    Night: sun_elevation = -5
    """
    p = PerformanceProfiler()
    offset = 0

    # We need sequential pairs (previous + current) for delta computation.
    # Each pair produces one observation. We need 6+ per bin for trendline.

    # Sunny observations at outdoor 50, 55, 60, 65, 70 (resist mode)
    for outdoor in [50, 55, 60, 65, 70]:
        attic = outdoor + 20  # sunny
        for i in range(8):  # 8 observations per bin
            indoor_prev = 70.0 + outdoor * 0.02  # mild upward trend with outdoor
            indoor_curr = indoor_prev + 0.02  # slight warming
            # First: set baseline
            p.record_observation(
                indoor_temp=indoor_prev, outdoor_temp=outdoor,
                hvac_action="idle", hvac_mode="heat",
                now=NOW + timedelta(minutes=offset),
                attic_temp=attic, sun_elevation=40.0,
            )
            offset += 5
            # Second: actual observation (delta computed from pair)
            p.record_observation(
                indoor_temp=indoor_curr, outdoor_temp=outdoor,
                hvac_action="idle", hvac_mode="heat",
                now=NOW + timedelta(minutes=offset),
                attic_temp=attic, sun_elevation=40.0,
            )
            offset += 5

    # Cloudy observations at same outdoor temps
    for outdoor in [50, 55, 60, 65, 70]:
        attic = outdoor + 5  # cloudy
        for i in range(8):
            indoor_prev = 70.0 - outdoor * 0.01  # mild cooling
            indoor_curr = indoor_prev - 0.02  # slight cooling
            p.record_observation(
                indoor_temp=indoor_prev, outdoor_temp=outdoor,
                hvac_action="idle", hvac_mode="heat",
                now=NOW + timedelta(minutes=offset),
                attic_temp=attic, sun_elevation=30.0,
            )
            offset += 5
            p.record_observation(
                indoor_temp=indoor_curr, outdoor_temp=outdoor,
                hvac_action="idle", hvac_mode="heat",
                now=NOW + timedelta(minutes=offset),
                attic_temp=attic, sun_elevation=30.0,
            )
            offset += 5

    # Night observations
    for outdoor in [45, 50, 55, 60, 65]:
        for i in range(8):
            indoor_prev = 70.0 - outdoor * 0.02
            indoor_curr = indoor_prev - 0.05  # cooling at night
            p.record_observation(
                indoor_temp=indoor_prev, outdoor_temp=outdoor,
                hvac_action="idle", hvac_mode="heat",
                now=NOW + timedelta(minutes=offset),
                attic_temp=outdoor - 2,  # attic near outdoor at night
                sun_elevation=-5.0,
            )
            offset += 5
            p.record_observation(
                indoor_temp=indoor_curr, outdoor_temp=outdoor,
                hvac_action="idle", hvac_mode="heat",
                now=NOW + timedelta(minutes=offset),
                attic_temp=outdoor - 2,
                sun_elevation=-5.0,
            )
            offset += 5

    return p


# ── Solar condition classification ───────────────────────────────────


class TestSolarConditionClassification:
    """Tests for _classify_solar_condition."""

    def test_night_below_horizon(self):
        result = PerformanceProfiler._classify_solar_condition(
            attic_temp=60.0, outdoor_temp=55.0, sun_elevation=-5.0,
        )
        assert result == "night"

    def test_night_at_horizon(self):
        result = PerformanceProfiler._classify_solar_condition(
            attic_temp=60.0, outdoor_temp=55.0, sun_elevation=0.0,
        )
        assert result == "night"

    def test_sunny_high_attic_delta(self):
        result = PerformanceProfiler._classify_solar_condition(
            attic_temp=80.0, outdoor_temp=65.0, sun_elevation=40.0,
        )
        assert result == "sunny"  # delta = 15 > 10

    def test_cloudy_low_attic_delta(self):
        result = PerformanceProfiler._classify_solar_condition(
            attic_temp=70.0, outdoor_temp=65.0, sun_elevation=40.0,
        )
        assert result == "cloudy"  # delta = 5 < 10

    def test_sunny_at_threshold(self):
        result = PerformanceProfiler._classify_solar_condition(
            attic_temp=75.0, outdoor_temp=64.9, sun_elevation=30.0,
        )
        assert result == "sunny"  # delta = 10.1 > 10

    def test_cloudy_at_threshold(self):
        result = PerformanceProfiler._classify_solar_condition(
            attic_temp=75.0, outdoor_temp=65.1, sun_elevation=30.0,
        )
        assert result == "cloudy"  # delta = 9.9 < 10

    def test_no_attic_sensor_returns_none(self):
        result = PerformanceProfiler._classify_solar_condition(
            attic_temp=None, outdoor_temp=65.0, sun_elevation=30.0,
        )
        assert result is None

    def test_no_sun_elevation_daytime(self):
        """Without sun elevation but with attic, can still classify sunny/cloudy."""
        result = PerformanceProfiler._classify_solar_condition(
            attic_temp=80.0, outdoor_temp=65.0, sun_elevation=None,
        )
        assert result == "sunny"


class TestForecastSolarClassification:
    """Tests for classify_solar_from_forecast."""

    def test_night(self):
        assert PerformanceProfiler.classify_solar_from_forecast(0.0, -5.0) == "night"

    def test_clear_sky(self):
        assert PerformanceProfiler.classify_solar_from_forecast(0.1, 30.0) == "sunny"

    def test_cloudy(self):
        assert PerformanceProfiler.classify_solar_from_forecast(0.8, 30.0) == "cloudy"

    def test_partial_cloud_treated_as_cloudy(self):
        assert PerformanceProfiler.classify_solar_from_forecast(0.5, 30.0) == "cloudy"

    def test_no_cloud_data(self):
        assert PerformanceProfiler.classify_solar_from_forecast(None, 30.0) is None


# ── Solar-aware binning ──────────────────────────────────────────────


class TestSolarAwareBinning:
    """Tests for 2D binning: observations split by solar condition."""

    def test_sunny_observations_go_to_sunny_bins(self):
        p = PerformanceProfiler()
        # First observation (baseline)
        p.record_observation(
            indoor_temp=70.0, outdoor_temp=60.0,
            hvac_action="idle", hvac_mode="heat",
            now=NOW, attic_temp=80.0, sun_elevation=40.0,
        )
        # Second observation (produces delta)
        p.record_observation(
            indoor_temp=70.5, outdoor_temp=60.0,
            hvac_action="idle", hvac_mode="heat",
            now=NOW + timedelta(minutes=5),
            attic_temp=80.0, sun_elevation=40.0,
        )
        # Check solar bins
        sunny_bins = p._solar_bins["resist"]["sunny"]
        assert 60 in sunny_bins
        assert sunny_bins[60].count == 1

    def test_night_observations_go_to_night_bins(self):
        p = PerformanceProfiler()
        p.record_observation(
            indoor_temp=70.0, outdoor_temp=50.0,
            hvac_action="idle", hvac_mode="heat",
            now=NOW, attic_temp=48.0, sun_elevation=-10.0,
        )
        p.record_observation(
            indoor_temp=69.5, outdoor_temp=50.0,
            hvac_action="idle", hvac_mode="heat",
            now=NOW + timedelta(minutes=5),
            attic_temp=48.0, sun_elevation=-10.0,
        )
        night_bins = p._solar_bins["resist"]["night"]
        assert 50 in night_bins
        assert night_bins[50].count == 1

    def test_no_attic_skips_solar_bins(self):
        """Without attic sensor, solar bins should remain empty."""
        p = PerformanceProfiler()
        p.record_observation(
            indoor_temp=70.0, outdoor_temp=60.0,
            hvac_action="idle", hvac_mode="heat",
            now=NOW,
        )
        p.record_observation(
            indoor_temp=70.5, outdoor_temp=60.0,
            hvac_action="idle", hvac_mode="heat",
            now=NOW + timedelta(minutes=5),
        )
        # Aggregate bin should have data
        assert 60 in p._bins["resist"]
        # Solar bins should be empty (no attic or sun data)
        for cond in ("sunny", "cloudy", "night"):
            assert len(p._solar_bins["resist"][cond]) == 0

    def test_aggregate_bins_still_populated(self):
        """Solar binning should not affect the existing aggregate bins."""
        p = PerformanceProfiler()
        p.record_observation(
            indoor_temp=70.0, outdoor_temp=60.0,
            hvac_action="idle", hvac_mode="heat",
            now=NOW, attic_temp=80.0, sun_elevation=40.0,
        )
        p.record_observation(
            indoor_temp=70.5, outdoor_temp=60.0,
            hvac_action="idle", hvac_mode="heat",
            now=NOW + timedelta(minutes=5),
            attic_temp=80.0, sun_elevation=40.0,
        )
        # Both aggregate and solar bins have the observation
        assert p._bins["resist"][60].count == 1
        assert p._solar_bins["resist"]["sunny"][60].count == 1


# ── Solar trendlines ─────────────────────────────────────────────────


class TestSolarTrendlines:
    """Tests for solar-condition-specific trendline output."""

    def test_solar_profile_data_has_conditions(self):
        p = _build_profiler_with_solar_data()
        data = p.to_solar_profile_data()
        assert "sunny" in data
        assert "cloudy" in data
        assert "night" in data

    def test_sunny_resist_trendline_exists(self):
        p = _build_profiler_with_solar_data()
        data = p.to_solar_profile_data()
        resist = data["sunny"]["temperature"].get("resist")
        assert resist is not None, "Sunny resist trendline should exist"
        assert "linear_trendline" in resist

    def test_solar_resist_trendline_method(self):
        p = _build_profiler_with_solar_data()
        sunny_tl = p.solar_resist_trendline("sunny")
        assert sunny_tl is not None
        assert "slope" in sunny_tl
        assert "intercept" in sunny_tl

    def test_insufficient_data_returns_none(self):
        """Mode/condition with insufficient data returns None trendline."""
        p = PerformanceProfiler()
        assert p.solar_resist_trendline("sunny") is None


# ── Serialization ────────────────────────────────────────────────────


class TestSolarSerialization:
    """Tests for solar bins persistence."""

    def test_round_trip_preserves_solar_bins(self):
        p = _build_profiler_with_solar_data()
        data = p.to_dict()
        p2 = PerformanceProfiler.from_dict(data)

        # Compare solar bin counts
        for mode in ("resist",):
            for cond in ("sunny", "cloudy", "night"):
                orig_count = sum(
                    acc.count for acc in p._solar_bins[mode][cond].values()
                )
                restored_count = sum(
                    acc.count for acc in p2._solar_bins[mode][cond].values()
                )
                assert orig_count == restored_count, (
                    f"{mode}/{cond}: {orig_count} vs {restored_count}"
                )

    def test_from_dict_without_solar_bins(self):
        """Legacy data without solar_bins should load gracefully."""
        p = PerformanceProfiler()
        p.record_observation(
            indoor_temp=70.0, outdoor_temp=60.0,
            hvac_action="idle", hvac_mode="heat", now=NOW,
        )
        data = p.to_dict()
        # Remove solar_bins to simulate legacy format
        data.pop("solar_bins", None)
        p2 = PerformanceProfiler.from_dict(data)
        # Solar bins should be empty but not cause errors
        for cond in ("sunny", "cloudy", "night"):
            assert len(p2._solar_bins["resist"][cond]) == 0
        # Aggregate bins should be preserved
        assert p2._total_observations == p._total_observations
