"""Regression: a backwards solar drift fit must not make forecasts run away.

In November 2026 the live profiler learned a "cloudy" drift line with slope
-0.13 F/hr per F. ``passive_drift`` reused that slope for its indoor
correction, so a warmer house warmed faster and the plan forecast 95 F with
the heat at 63 and 45 F outside.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.heatpump_optimizer.engine.data_types import ForecastPoint, ScheduleEntry
from custom_components.heatpump_optimizer.engine.performance_model import MIN_DRIFT_SLOPE, PerformanceModel
from custom_components.heatpump_optimizer.engine.thermal_simulator import ThermalSimulator
from custom_components.heatpump_optimizer.learning.performance_profiler import (
    BinAccumulator,
    PerformanceProfiler,
)

BAD_CLOUDY = {"slope": -0.133829, "intercept": 6.3898}  # the live fit


def _model_with_bad_cloudy() -> PerformanceModel:
    m = PerformanceModel.from_defaults()
    m._solar_resist_trendlines["cloudy"] = dict(BAD_CLOUDY)
    return m


def _cloudy_day(model, start_temp=64.0, outdoor=45.0, target=63.0):
    t0 = datetime(2026, 11, 27, 2, tzinfo=UTC)
    fc = [
        ForecastPoint(time=t0 + timedelta(hours=h), outdoor_temp=outdoor, cloud_cover=0.9, sun_elevation=20.0)
        for h in range(25)
    ]
    sched = [ScheduleEntry(start_time=t0, end_time=t0 + timedelta(hours=24), target_temp=target, mode="heat")]
    return ThermalSimulator(model).simulate(start_temp, fc, sched)


class TestIndoorCorrection:
    def test_drift_falls_as_indoor_rises_under_bad_solar_fit(self):
        m = _model_with_bad_cloudy()
        drifts = [m.passive_drift(45.0, tin, solar_condition="cloudy") for tin in (60, 70, 80, 90)]
        assert all(a > b for a, b in zip(drifts, drifts[1:], strict=False))

    def test_negative_aggregate_slope_is_floored(self):
        m = PerformanceModel.from_defaults()
        m._resist_trendline = {"slope": -0.05, "intercept": 1.0}
        assert m._drift_slope() == MIN_DRIFT_SLOPE
        assert m.passive_drift(45.0, 90.0) < m.passive_drift(45.0, 60.0)

    def test_cloudy_day_does_not_run_away(self):
        pts = _cloudy_day(_model_with_bad_cloudy())
        assert max(p.indoor_temp for p in pts) < 66.0


class TestProfilerSolarFit:
    def _profiler_with_cloudy(self, slope: float) -> PerformanceProfiler:
        prof = PerformanceProfiler()
        bins = prof._solar_bins["resist"]["cloudy"]
        for t in range(40, 52):
            acc = BinAccumulator()
            delta = slope * t - 0.5
            acc.sum_delta = delta * 10
            acc.sum_sq_delta = delta * delta * 10
            acc.count = 10
            bins[t] = acc
        return prof

    def test_backwards_fit_rejected(self):
        assert self._profiler_with_cloudy(-0.13).solar_resist_trendline("cloudy") is None

    def test_physical_fit_kept(self):
        tl = self._profiler_with_cloudy(0.04).solar_resist_trendline("cloudy")
        assert tl is not None
        assert tl["slope"] == pytest.approx(0.04, abs=1e-6)
