"""Regression tests for the pre-release audit (November 2026).

The audit replayed the real setup path and five-minute cycle against ten days
of recorder data from a live house and found defects the unit suite could not
see, because nothing in it drove a heat_cool thermostat, an away block, a
restart, or a monitor-only savings ledger. Each class below pins one of them.
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── Stub the climate component the thermostat adapter imports ────────

_climate = types.ModuleType("homeassistant.components.climate")
_climate.ATTR_CURRENT_HUMIDITY = "current_humidity"
_climate.ATTR_CURRENT_TEMPERATURE = "current_temperature"
_climate.ATTR_HVAC_ACTION = "hvac_action"
_climate.ATTR_MAX_TEMP = "max_temp"
_climate.ATTR_MIN_TEMP = "min_temp"
_climate.ATTR_TARGET_TEMP_HIGH = "target_temp_high"
_climate.ATTR_TARGET_TEMP_LOW = "target_temp_low"
_climate.SERVICE_SET_TEMPERATURE = "set_temperature"
_climate.HVACAction = SimpleNamespace(COOLING="cooling", HEATING="heating", IDLE="idle")
_climate.HVACMode = SimpleNamespace(
    COOL="cool", HEAT="heat", HEAT_COOL="heat_cool", AUTO="auto", OFF="off",
)
sys.modules.setdefault("homeassistant.components.climate", _climate)
_ha_const = sys.modules["homeassistant.const"]
if not hasattr(_ha_const, "ATTR_TEMPERATURE"):
    _ha_const.ATTR_TEMPERATURE = "temperature"

from custom_components.heatpump_optimizer.adapters.occupancy import (  # noqa: E402
    OccupancyAdapter,
    OccupancyMode,
)
from custom_components.heatpump_optimizer.adapters.thermostat import (  # noqa: E402
    MIN_DUAL_SETPOINT_GAP_F,
    ThermostatAdapter,
)
from custom_components.heatpump_optimizer.controllers.strategic import (  # noqa: E402
    StrategicPlanner,
)
from custom_components.heatpump_optimizer.engine.data_types import (  # noqa: E402
    ForecastPoint,
    OccupancyForecastPoint,
)
from custom_components.heatpump_optimizer.engine.optimizer import ScheduleOptimizer  # noqa: E402
from custom_components.heatpump_optimizer.engine.performance_model import (  # noqa: E402
    NOMINAL_INDOOR_F,
    PerformanceModel,
)
from custom_components.heatpump_optimizer.engine.thermal_simulator import ThermalSimulator  # noqa: E402
from custom_components.heatpump_optimizer.learning.energy_baseline import (  # noqa: E402
    DayEnergyRecord,
    EnergyBaseline,
)
from custom_components.heatpump_optimizer.learning.performance_profiler import (  # noqa: E402
    PerformanceProfiler,
)
from custom_components.heatpump_optimizer.savings_tracker import (  # noqa: E402
    LEDGER_VERSION,
    SavingsTracker,
)

NOW = datetime(2026, 11, 18, 13, 0, tzinfo=timezone.utc)


# ── Thermostat write path ────────────────────────────────────────────


def _thermostat(mode="heat_cool", indoor=63.0, action="idle", low=63.0, high=75.0):
    hass = MagicMock()
    hass.config.units.temperature_unit = "°F"
    attrs = {
        "current_temperature": indoor,
        "hvac_action": action,
        "target_temp_low": low if mode == "heat_cool" else None,
        "target_temp_high": high if mode == "heat_cool" else None,
        "temperature": None if mode == "heat_cool" else low,
    }
    hass.states.get.return_value = SimpleNamespace(state=mode, attributes=attrs)
    hass.services.async_call = AsyncMock()
    return ThermostatAdapter(hass, "climate.test"), hass


def _written(hass) -> dict:
    data = dict(hass.services.async_call.call_args.args[2])
    data.pop("entity_id", None)
    return data


class TestThermostatWritePath:
    """The heat setpoint once received 70-76 F on a 31 F morning."""

    def test_heat_target_goes_to_the_low_setpoint_and_leaves_the_high_alone(self):
        adapter, hass = _thermostat(low=63.0, high=75.0)
        ok = asyncio.run(adapter.async_set_temperature(60.0, 58.0, 66.0, side="heat"))
        assert ok
        assert _written(hass) == {"target_temp_low": 60.0, "target_temp_high": 75.0}

    def test_cool_target_goes_to_the_high_setpoint(self):
        adapter, hass = _thermostat(low=63.0, high=75.0)
        asyncio.run(adapter.async_set_temperature(76.0, 64.0, 76.0, side="cool"))
        assert _written(hass) == {"target_temp_low": 63.0, "target_temp_high": 76.0}

    def test_a_bare_number_is_refused_on_a_heat_cool_thermostat(self):
        """The old path guessed the side and put a cooling target on heat."""
        adapter, hass = _thermostat(indoor=63.0)
        ok = asyncio.run(adapter.async_set_temperature(76.0, 64.0, 76.0))
        assert ok is False
        hass.services.async_call.assert_not_called()

    def test_single_setpoint_thermostat_is_unchanged(self):
        adapter, hass = _thermostat(mode="heat", low=63.0)
        asyncio.run(adapter.async_set_temperature(61.0, 58.0, 66.0))
        assert _written(hass) == {"temperature": 61.0}

    def test_setpoints_are_rounded_to_what_a_thermostat_can_hold(self):
        adapter, hass = _thermostat()
        asyncio.run(adapter.async_set_temperature(60.53, 58.0, 66.0, side="heat"))
        assert _written(hass)["target_temp_low"] == 60.5

    def test_dual_write_keeps_the_pair_apart_and_remembers_both(self):
        adapter, hass = _thermostat()
        asyncio.run(adapter.async_set_dual_setpoints(65.0, 66.0, primary="heat"))
        written = _written(hass)
        assert written["target_temp_high"] - written["target_temp_low"] >= MIN_DUAL_SETPOINT_GAP_F
        assert adapter.last_written_setpoint == 65.0

    def test_idle_heat_cool_reports_the_nearer_side_not_always_cool(self):
        adapter, _ = _thermostat(indoor=63.5, low=63.0, high=75.0)
        assert adapter.get_active_mode() == "heat"
        adapter, _ = _thermostat(indoor=74.0, low=63.0, high=75.0)
        assert adapter.get_active_mode() == "cool"

    def test_override_detection_compares_like_with_like(self):
        """Heat 60 / cool 76 into a 70 F house is not a manual override."""
        adapter, hass = _thermostat(indoor=70.0)
        asyncio.run(adapter.async_set_dual_setpoints(60.0, 76.0, primary="heat"))
        hass.states.get.return_value = SimpleNamespace(state="heat_cool", attributes={
            "current_temperature": 70.0, "hvac_action": "idle",
            "target_temp_low": 60.0, "target_temp_high": 76.0, "temperature": None,
        })
        state = adapter.read_state()
        # effective_setpoint picks the cool side (76) for a 70 F house...
        assert state.effective_setpoint == 76.0
        # ...but the comparison used for override detection does not
        expected, actual = adapter.expected_and_actual(state)
        assert expected == actual

        # A real change to either side is still seen
        hass.states.get.return_value.attributes["target_temp_low"] = 64.0
        expected, actual = adapter.expected_and_actual(adapter.read_state())
        assert (expected, actual) == (60.0, 64.0)

    def test_flat_default_writes_the_edges_not_a_midpoint(self):
        adapter, hass = _thermostat()
        asyncio.run(adapter.async_write_flat_default(60.0, 76.0, "heat"))
        assert _written(hass) == {"target_temp_low": 60.0, "target_temp_high": 76.0}


# ── Occupancy ────────────────────────────────────────────────────────


class TestAwayBand:
    def test_away_delta_is_configurable(self):
        band = OccupancyAdapter.adjust_comfort_for_mode(
            (60.0, 66.0), "heat", OccupancyMode.AWAY, away_delta=2.0,
        )
        assert band == (58.0, 68.0)

    def test_away_and_vacation_never_pass_the_safety_limits(self):
        """60 - 4 = 56 was planned and written under a 58 F safety floor."""
        away = OccupancyAdapter.adjust_comfort_for_mode(
            (60.0, 66.0), "heat", OccupancyMode.AWAY, safety_limits=(58.0, 81.0),
        )
        assert away[0] == 58.0
        vacation = OccupancyAdapter.adjust_comfort_for_mode(
            (60.0, 66.0), "heat", OccupancyMode.VACATION, safety_limits=(58.0, 81.0),
        )
        assert vacation[0] == 58.0
        cool = OccupancyAdapter.adjust_comfort_for_mode(
            (64.0, 78.0), "cool", OccupancyMode.AWAY, safety_limits=(58.0, 81.0),
        )
        assert cool[1] == 81.0

    def _adapter(self, person_state: str | None):
        hass = MagicMock()
        if person_state is None:
            return OccupancyAdapter(hass, entity_ids=[])
        hass.states.get.return_value = SimpleNamespace(state=person_state)
        return OccupancyAdapter(hass, entity_ids=["person.a"], debounce_minutes=0)

    def _calendar(self, mode: str):
        now = datetime.now(timezone.utc)
        return [OccupancyForecastPoint(
            start_time=now - timedelta(hours=1), end_time=now + timedelta(hours=1),
            mode=mode, source="calendar",
        )]

    def test_calendar_home_does_not_fill_an_empty_house(self):
        """Nobody was home for 131 hours and the plan said home for all of them."""
        adapter = self._adapter("not_home")
        assert adapter.get_effective_mode(self._calendar("home")) == OccupancyMode.AWAY

    def test_presence_still_beats_a_calendar_that_says_away(self):
        adapter = self._adapter("home")
        assert adapter.get_effective_mode(self._calendar("away")) == OccupancyMode.HOME

    def test_calendar_can_refine_an_empty_house_to_vacation(self):
        adapter = self._adapter("not_home")
        assert adapter.get_effective_mode(self._calendar("vacation")) == OccupancyMode.VACATION

    def test_calendar_is_trusted_when_there_are_no_presence_entities(self):
        adapter = self._adapter(None)
        assert adapter.get_effective_mode(self._calendar("home")) == OccupancyMode.HOME
        assert adapter.get_effective_mode(self._calendar("away")) == OccupancyMode.AWAY


# ── Strategic planner ────────────────────────────────────────────────


def _forecast(temps, start=NOW):
    return [
        ForecastPoint(time=start + timedelta(hours=i), outdoor_temp=float(t))
        for i, t in enumerate(temps)
    ]


def _bins(lo, hi, slope, intercept):
    return {str(t): slope * t + intercept for t in range(lo, hi + 1)}


def _model(anchor: float | None = 62.0) -> PerformanceModel:
    def section(lo, hi, slope, intercept):
        out = {
            "deltas": _bins(lo, hi, slope, intercept),
            "linear_trendline": {"slope": slope, "intercept": intercept},
        }
        if anchor is not None:
            out["indoor_anchor_mean"] = anchor
        return out

    return PerformanceModel({
        "temperature": {
            "cool_1": section(55, 100, 0.05, -6.5),
            "heat_1": section(10, 55, 0.04, -0.6),
            "resist": section(10, 100, 0.03, -1.5),
            "auxiliary_heat_1": None,
        },
        "balance_point": {"heat_1": 15.0, "resist": 50.0},
        "differential": {"cool": 1.0, "heat": 1.0},
        "setpoint": {"cool": 74.0, "heat": 63.0},
    })


def _planner(**kwargs) -> StrategicPlanner:
    model = _model()
    return StrategicPlanner(
        optimizer=ScheduleOptimizer(model, ThermalSimulator(model)),
        resist_balance_point=54.0,
        **kwargs,
    )


class TestModeDetection:
    def test_frosty_morning_with_a_mild_afternoon_is_heat_not_cool(self):
        """31 F at 08:00 was planned as "cool" because 66 F came later."""
        planner = _planner()
        forecast = _forecast([31, 33, 38, 46, 55, 62, 66, 65])
        assert planner.detect_mode(forecast) == "heat"

    def test_warm_now_with_a_cold_night_ahead_is_cool(self):
        planner = _planner()
        forecast = _forecast([66, 67, 66, 62, 55, 48, 43, 40])
        assert planner.detect_mode(forecast) == "cool"

    def test_clear_cases_are_unchanged(self):
        planner = _planner()
        assert planner.detect_mode(_forecast([85] * 8)) == "cool"
        assert planner.detect_mode(_forecast([30] * 8)) == "heat"
        assert planner.detect_mode(_forecast([54] * 8)) == "off"


class TestHourBands:
    COOL = (64.0, 76.0)
    HEAT = (60.0, 66.0)

    def test_away_is_applied_once_and_stops_at_the_safety_floor(self):
        """Plans reached 52 F: the away delta went on twice, with no clamp."""
        planner = _planner(safety_limits=(58.0, 81.0), away_comfort_delta=4.0)
        forecast = _forecast([30] * 12)
        timeline = [OccupancyForecastPoint(
            start_time=NOW, end_time=NOW + timedelta(hours=12), mode="away", source="t",
        )]
        schedule = planner.optimize(
            62.0, forecast, self.COOL, self.HEAT,
            occupancy_timeline=timeline, current_occupancy=OccupancyMode.AWAY,
        )
        targets = {e.target_temp for e in schedule.entries}
        assert targets == {58.0}
        assert all(pt.comfort_min == 58.0 for pt in forecast)

    def test_without_a_safety_limit_away_is_still_only_applied_once(self):
        planner = _planner(away_comfort_delta=4.0)
        forecast = _forecast([30] * 6)
        timeline = [OccupancyForecastPoint(
            start_time=NOW, end_time=NOW + timedelta(hours=6), mode="away", source="t",
        )]
        planner.optimize(
            62.0, forecast, self.COOL, self.HEAT,
            occupancy_timeline=timeline, current_occupancy=OccupancyMode.AWAY,
        )
        assert {pt.comfort_min for pt in forecast} == {56.0}

    def test_current_hour_follows_presence_not_the_calendar(self):
        planner = _planner(safety_limits=(50.0, 85.0))
        forecast = _forecast([30] * 4, start=datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0))
        timeline = [OccupancyForecastPoint(
            start_time=forecast[0].time, end_time=forecast[-1].time + timedelta(hours=1),
            mode="away", source="t",
        )]
        planner.optimize(
            62.0, forecast, self.COOL, self.HEAT,
            occupancy_timeline=timeline, current_occupancy=OccupancyMode.HOME,
        )
        # Someone is home right now, whatever the calendar's away block says,
        # and the block is not believed for its remaining hours either.
        assert {pt.comfort_min for pt in forecast} == {60.0}

    def test_a_calendar_that_is_wrong_about_now_is_not_trusted_about_later(self):
        """Default "home" on the calendar, nobody home: the plan stays away.

        Otherwise every new hour began on the home band and flipped to the
        away band at the next re-plan, writing the thermostat twice an hour.
        """
        planner = _planner(safety_limits=(50.0, 85.0))
        start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        forecast = _forecast([30] * 8, start=start)
        timeline = [
            OccupancyForecastPoint(start_time=start, end_time=start + timedelta(hours=4),
                                   mode="home", source="default"),
            OccupancyForecastPoint(start_time=start + timedelta(hours=4),
                                   end_time=start + timedelta(hours=6), mode="away", source="event"),
            OccupancyForecastPoint(start_time=start + timedelta(hours=6),
                                   end_time=start + timedelta(hours=9), mode="home", source="default"),
        ]
        planner.optimize(
            62.0, forecast, self.COOL, self.HEAT,
            occupancy_timeline=timeline, current_occupancy=OccupancyMode.AWAY,
        )
        floors = [pt.comfort_min for pt in forecast]
        assert floors[:6] == [56.0] * 6          # still empty, then a real away event
        assert floors[6:] == [60.0, 60.0]        # the calendar changed its mind: trusted again

    def test_sleep_window_is_per_hour_even_without_a_calendar(self):
        planner = _planner(
            sleep_config={
                "enabled": True, "start": "22:00", "end": "06:00",
                "comfort_heat": (58.0, 62.0), "comfort_cool": (62.0, 68.0),
                "morning": None,
            },
        )
        start = datetime(2026, 11, 18, 20, 0, tzinfo=timezone.utc)
        forecast = _forecast([30] * 12, start=start)
        planner.optimize(62.0, forecast, self.COOL, self.HEAT)
        floors = {pt.time.hour: pt.comfort_min for pt in forecast}
        assert floors[20] == 60.0 and floors[21] == 60.0
        assert floors[22] == 58.0 and floors[5] == 58.0
        assert floors[6] == 60.0

    def test_band_at_reports_what_was_planned(self):
        planner = _planner(safety_limits=(58.0, 81.0))
        forecast = _forecast([30] * 4)
        planner.optimize(62.0, forecast, self.COOL, self.HEAT)
        assert planner.band_at(NOW + timedelta(minutes=20), "heat") == (60.0, 66.0)
        assert planner.band_at(NOW, "cool") == (64.0, 76.0)
        assert _planner().band_at(NOW, "heat") is None  # nothing planned yet


# ── Model: indoor anchor and noisy bins ──────────────────────────────


class TestIndoorAnchor:
    def test_drift_is_corrected_relative_to_where_the_table_was_recorded(self):
        """A table recorded at 62 F must return its own value at 62 F."""
        model = _model(anchor=62.0)
        assert model.passive_drift(30.0, indoor_temp=62.0) == pytest.approx(-0.6)
        # Colder house, smaller loss
        assert model.passive_drift(30.0, indoor_temp=60.0) == pytest.approx(-0.54)

    def test_a_cold_house_no_longer_warms_itself(self):
        """With a 72 F anchor, 30 F out and 60 F in predicted +0.06 F/hr."""
        assert _model(anchor=62.0).passive_drift(30.0, indoor_temp=60.0) < -0.4
        legacy = _model(anchor=None)
        assert legacy.indoor_anchor("resist", 30.0) == NOMINAL_INDOOR_F
        assert legacy.passive_drift(30.0, indoor_temp=60.0) > -0.3  # the old error

    def test_heating_rate_uses_its_own_anchor(self):
        model = _model(anchor=62.0)
        assert model.heating_delta(30.0, indoor_temp=62.0) == pytest.approx(0.6)
        assert model.heating_delta(30.0, indoor_temp=66.0) == pytest.approx(0.6 - 0.03 * 4)

    def test_per_bin_anchors_follow_the_season(self):
        profile = _model(anchor=None)._raw
        profile["temperature"]["resist"]["indoor_anchor"] = {"30": 62.0, "90": 72.0}
        model = PerformanceModel(profile)
        assert model.indoor_anchor("resist", 30.0) == 62.0
        assert model.indoor_anchor("resist", 90.0) == 72.0
        assert model.indoor_anchor("resist", 60.0) == pytest.approx(67.0)
        assert model.indoor_anchor("resist", 10.0) == 62.0   # clamped, not extrapolated


def _feed(profiler, outdoor, rate_per_hr, n, indoor=62.0, action="heating", start=NOW):
    """Feed n five-minute observations at a steady rate."""
    when = start
    temp = indoor
    profiler.record_observation(temp, outdoor, action, "heat_cool", now=when)
    for _ in range(n):
        when += timedelta(minutes=5)
        temp += rate_per_hr / 12.0
        profiler.record_observation(temp, outdoor, action, "heat_cool", now=when)
    return when


class TestProfilerAnchorsAndSmoothing:
    def test_bins_remember_the_indoor_temperature_they_were_recorded_at(self):
        profiler = PerformanceProfiler()
        _feed(profiler, 30, -0.6, 12, indoor=62.0, action="idle")
        data = profiler.to_profile_data()["temperature"]["resist"]
        assert data["indoor_anchor"]["30"] == pytest.approx(61.7, abs=0.2)
        assert data["indoor_anchor_mean"] == pytest.approx(61.7, abs=0.2)

        restored = PerformanceProfiler.from_dict(profiler.to_dict())
        again = restored.to_profile_data()["temperature"]["resist"]
        assert again["indoor_anchor"] == data["indoor_anchor"]

    def test_data_recorded_before_anchors_falls_back_to_the_last_indoor_temp(self):
        """Upgrading keeps 71,680 observations with no indoor temperature."""
        profiler = PerformanceProfiler()
        _feed(profiler, 30, -0.6, 12, indoor=62.0, action="idle")
        legacy = profiler.to_dict()
        for bins in legacy["bins"].values():
            for acc in bins.values():
                acc.pop("sum_indoor", None)
                acc.pop("indoor_count", None)
        legacy["previous_indoor_temp"] = 63.0

        data = PerformanceProfiler.from_dict(legacy).to_profile_data()
        resist = data["temperature"]["resist"]
        assert "indoor_anchor" not in resist
        assert resist["indoor_anchor_mean"] == 63.0

    def test_a_thin_outlier_bin_cannot_fake_a_capacity_cliff(self):
        """+0.12 F/hr at 46-47 F next to +2.18 at 48 F, from six samples."""
        profiler = PerformanceProfiler()
        when = NOW
        for outdoor in range(30, 54):
            rate = 0.04 * outdoor - 0.6          # the true curve
            n = 60
            if outdoor in (46, 47):
                rate, n = -0.28, 7               # thin, wrong
            when = _feed(profiler, outdoor, rate, n, start=when + timedelta(hours=1))

        raw = profiler.bin_coverage("heat_1")["bins"]
        assert raw[46]["mean_delta"] < 0 < raw[48]["mean_delta"]

        deltas = profiler.to_profile_data()["temperature"]["heat_1"]["deltas"]
        values = [deltas[str(t)] for t in range(30, 54)]
        jumps = [abs(b - a) for a, b in zip(values, values[1:])]
        assert max(jumps) < 0.5
        assert deltas["46"] > 0.5 and deltas["47"] > 0.5
        # Well-observed bins are left essentially alone
        assert deltas["35"] == pytest.approx(0.04 * 35 - 0.6, abs=0.1)


# ── Savings ledger ───────────────────────────────────────────────────


def _hour(tracker, hour, *, controlling, ratio_hours=1):
    base = datetime(2026, 11, 18, hour, 0, tzinfo=timezone.utc)
    for i in range(12):
        tracker.record_interval(
            now=base + timedelta(minutes=5 * i), hvac_running=True,
            interval_minutes=5.0, power_watts=4500.0, carbon_intensity=400.0,
            electricity_rate=0.18, mode="heat", controlling=controlling,
        )
    # Cross the boundary so the hour is finalized
    tracker.record_interval(
        now=base + timedelta(hours=1), hvac_running=False, interval_minutes=5.0,
        power_watts=0.0, carbon_intensity=400.0, electricity_rate=0.18,
        mode="heat", controlling=controlling,
    )


class TestSavingsLedger:
    def test_hours_the_optimizer_did_not_control_claim_nothing(self):
        """A monitor-only install accrued -1,565 kWh."""
        tracker = SavingsTracker()
        tracker.set_accuracy_tier("calibrated")
        tracker.set_baseline_ratio(baseline_runtime=30, optimized_runtime=60)  # "loses" 50%
        _hour(tracker, 10, controlling=False)

        record = tracker._hourly_records[-1]
        assert record.actual_kwh == pytest.approx(4.5)
        assert record.saved_kwh == 0.0
        assert record.saved_cost == 0.0 and record.saved_co2_grams == 0.0
        totals = tracker.cumulative_totals()
        assert totals["kwh_saved"] == 0.0
        assert totals["kwh_actual"] == pytest.approx(4.5)   # still metered

    def test_lifetime_saving_is_banked_from_measured_days_only(self):
        tracker = SavingsTracker()
        tracker.set_accuracy_tier("calibrated")
        baseline = EnergyBaseline()
        tracker.set_energy_baseline(baseline)
        _hour(tracker, 10, controlling=True)   # gives the day a rate and carbon figure

        def day(date, kwh, hdd, fraction):
            return DayEnergyRecord(
                date=date, kwh=kwh, mean_outdoor_f=65.0 - hdd, cdd=0.0, hdd=hdd,
                covered_minutes=1440.0, temp_covered_minutes=1440.0,
                optimizer_fraction=fraction, measured_fraction=1.0,
            )

        # Twelve baseline days at 1.0 kWh per heating degree-day
        for i in range(12):
            baseline._days.append(day(f"2026-11-{i + 1:02d}", 20.0 + i, 20.0 + i, 0.0))
        baseline._cached_fit = None
        assert baseline.fit().is_usable

        before = tracker.cumulative_totals()["kwh_saved"]
        tracker.record_measured_day(day("2026-11-20", 18.0, 20.0, 1.0))   # optimized
        saved = tracker.cumulative_totals()["kwh_saved"] - before
        assert saved == pytest.approx(2.0, abs=0.3)
        assert tracker.cumulative_totals()["cost_saved"] == pytest.approx(saved * 0.18, rel=0.01)

        # Baseline and mixed days are never banked
        again = tracker.cumulative_totals()["kwh_saved"]
        tracker.record_measured_day(day("2026-11-21", 10.0, 20.0, 0.0))
        tracker.record_measured_day(day("2026-11-22", 10.0, 20.0, 0.5))
        assert tracker.cumulative_totals()["kwh_saved"] == again

    def test_a_pre_gating_ledger_is_retired_once_on_upgrade(self):
        legacy = {
            "cumulative_kwh_saved": -1565.61,
            "cumulative_cost_saved": -231.92,
            "cumulative_co2_saved_grams": -678980.0,
            "cumulative_kwh_actual": 9000.0,
            "accuracy_tier": "calibrated",
        }
        tracker = SavingsTracker.from_dict(legacy)
        totals = tracker.cumulative_totals()
        assert totals["kwh_saved"] == 0.0 and totals["cost_saved"] == 0.0
        assert tracker.ledger_reset_at is not None

        # Saved again and reloaded, it is not reset a second time
        saved = tracker.to_dict()
        assert saved["ledger_version"] == LEDGER_VERSION
        tracker._cumulative_kwh_saved = 12.0
        reloaded = SavingsTracker.from_dict(tracker.to_dict())
        assert reloaded.cumulative_totals()["kwh_saved"] == 12.0
        assert reloaded.ledger_reset_at == tracker.ledger_reset_at

    def test_todays_hours_survive_a_restart(self):
        tracker = SavingsTracker()
        tracker.set_accuracy_tier("calibrated")
        _hour(tracker, 10, controlling=True)
        _hour(tracker, 12, controlling=True)
        restored = SavingsTracker.from_dict(tracker.to_dict())
        # 10:00, the idle 11:00 hour in between, and 12:00
        assert len(restored._hourly_records) == len(tracker._hourly_records) == 3
        assert [r.hour for r in restored._hourly_records] == [
            r.hour for r in tracker._hourly_records
        ]
        assert restored._hourly_records[0].actual_kwh == pytest.approx(4.5)
        assert restored.today_report().total_actual_kwh == pytest.approx(
            tracker.today_report().total_actual_kwh
        )
