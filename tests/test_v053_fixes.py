"""v0.5.3: local-day energy, twin metered watts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.heatpump_optimizer.learning.twin_ledger import TwinLedger, replay_twin_history
from custom_components.heatpump_optimizer.savings_tracker import SavingsTracker

NY = ZoneInfo("America/New_York")


def _run_hour(tracker: SavingsTracker, start: datetime, watts: float) -> None:
    for i in range(12):
        tracker.record_interval(
            now=start + timedelta(minutes=5 * i),
            hvac_running=True,
            interval_minutes=5.0,
            power_watts=watts,
            carbon_intensity=None,
            electricity_rate=None,
            mode="heat",
        )


class TestTodayReportLocalDay:
    def test_evening_hours_count_toward_the_local_day(self):
        tracker = SavingsTracker()
        today = datetime.now(NY).replace(minute=0, second=0, microsecond=0)
        # 20:00 Eastern is 01:00 UTC the next day
        evening = today.replace(hour=20).astimezone(UTC)
        yesterday_evening = (today.replace(hour=20) - timedelta(days=1)).astimezone(UTC)
        _run_hour(tracker, yesterday_evening, 4500.0)
        _run_hour(tracker, evening, 4500.0)
        # Close the evening hour
        tracker.record_interval(
            now=evening + timedelta(hours=1),
            hvac_running=False,
            interval_minutes=5.0,
            power_watts=0.0,
            carbon_intensity=None,
            electricity_rate=None,
            mode="off",
        )
        report = tracker.today_report(tz=NY)
        assert [r.hour.astimezone(NY).hour for r in report.hours] == [20]
        assert report.total_actual_kwh == pytest.approx(4.5, abs=0.01)


def _series(days: int, power: float | None):
    start = datetime(2026, 11, 17, tzinfo=UTC)
    grid = [start + timedelta(minutes=5 * i) for i in range(days * 288 + 1)]
    n = len(grid)
    return {
        "grid": grid,
        "thermostat_indoor": [62.0] * n,
        "outdoor": [40.0] * n,
        "action": ["heating" if i % 2 == 0 else "idle" for i in range(n)],
        "aux": [False] * n,
        "mode": ["heat"] * n,
        "temperature": [63.0] * n,
        "target_low": [None] * n,
        "target_high": [None] * n,
        "power": None if power is None else [power] * n,
    }


class TestTwinMeteredWatts:
    def test_replay_reads_power(self):
        led = TwinLedger("UTC")
        replay_twin_history(led, _series(2, 4500.0))
        sums = led._pending_watts["2026-11-17"]
        assert sums["heat_h"] == pytest.approx(12.0)
        assert sums["heat_wh"] / sums["heat_h"] == pytest.approx(4500.0)

    def test_backfilled_days_get_measured_watts(self):
        led = TwinLedger("UTC")
        for day, nxt in replay_twin_history(led, _series(3, 4500.0)):
            led.close_day(day, next_top=nxt, rated_heat=3450.0, rated_cool=3000.0)
        assert led.records["2026-11-17"]["watts"]["heat_source"] == "measured"
        assert led.records["2026-11-17"]["watts"]["heat"] == pytest.approx(4500.0)

    def test_fill_metered_upgrades_stored_days(self):
        led = TwinLedger("UTC")
        for day, nxt in replay_twin_history(led, _series(3, None)):
            led.close_day(day, next_top=nxt, rated_heat=3450.0, rated_cool=3000.0)
        assert led.records["2026-11-18"]["watts"]["heat_source"] == "rated"

        scratch = TwinLedger("UTC")
        replay_twin_history(scratch, _series(3, 4500.0))
        filled = led.fill_metered(dict(scratch._pending_watts), rated_heat=3450.0, rated_cool=3000.0, aux_kw=12.5)
        assert filled == 3
        w = led.records["2026-11-18"]["watts"]
        assert (w["heat"], w["heat_source"]) == (pytest.approx(4500.0), "measured")
        assert (w["cool"], w["cool_source"]) == (3000.0, "rated")
        assert w["aux_source"] == "configured"
        # A second pass never overwrites days that already have sums
        assert led.fill_metered(dict(scratch._pending_watts)) == 0

    def test_flag_persists(self):
        led = TwinLedger("UTC")
        led.metered_backfilled = True
        assert TwinLedger.from_dict(led.to_dict(), "UTC").metered_backfilled is True

    def test_payload_watts_before_any_valid_day(self):
        led = TwinLedger("UTC")
        for day, nxt in replay_twin_history(led, _series(3, 4500.0)):
            led.close_day(day, next_top=nxt, rated_heat=3450.0, rated_cool=3000.0, aux_kw=12.5)
        assert not led.window()
        watts = led.payload(datetime(2026, 11, 20, 12, tzinfo=UTC))["watts"]
        assert (watts["heat"], watts["heat_source"]) == (4500, "measured")
        assert (watts["aux"], watts["aux_source"]) == (12500, "configured")


class TestSolarTrendlineRange:
    def test_night_line_only_near_its_fitted_range(self):
        from custom_components.heatpump_optimizer.engine.performance_model import PerformanceModel

        model = PerformanceModel.from_defaults()
        # A night line fitted on a week of 43-52 F nights
        model._solar_resist_trendlines["night"] = {"slope": 0.06, "intercept": -2.97, "temp_range": [43.0, 52.0]}
        inside = model.passive_drift(48.0, indoor_temp=None, solar_condition="night")
        assert inside == pytest.approx(0.06 * 48.0 - 2.97)
        # Outside the range the aggregate line takes over
        assert model.passive_drift(75.0, indoor_temp=None, solar_condition="night") == pytest.approx(
            model.passive_drift(75.0, indoor_temp=None)
        )

    def test_profiler_reports_fitted_range(self):
        from custom_components.heatpump_optimizer.learning.performance_profiler import (
            MIN_SAMPLES_PER_BIN,
            BinAccumulator,
            PerformanceProfiler,
        )

        prof = PerformanceProfiler()
        bins = prof._solar_bins["resist"]["night"]
        for t in range(43, 53):
            acc = BinAccumulator()
            for _ in range(MIN_SAMPLES_PER_BIN):
                acc.add(0.06 * t - 2.97)
            bins[t] = acc
        tl = prof.solar_resist_trendline("night")
        assert tl["temp_range"] == [43.0, 52.0]
