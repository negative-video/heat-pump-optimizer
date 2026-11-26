"""Morning recovery: the two occupant preferences, end to end.

Covers the pure window logic in engine/morning_recovery.py and its use by
the strategic planner over the real simulator, including the two properties
that are easy to lose:

* the recovery ramp is scored against what the occupant was promised, not
  against its destination, so the planner does not pre-heat ahead of it; and
* a recovery that has begun is not un-begun by a later re-plan.
"""

from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta, timezone
from unittest.mock import patch

import pytest

from custom_components.heatpump_optimizer.adapters.occupancy import OccupancyMode
from custom_components.heatpump_optimizer.controllers import strategic as strategic_mod
from custom_components.heatpump_optimizer.controllers.strategic import StrategicPlanner
from custom_components.heatpump_optimizer.engine.data_types import ForecastPoint
from custom_components.heatpump_optimizer.engine.morning_recovery import (
    RECOVERY_AT_SLEEP_END,
    RECOVERY_WAIT_FOR_WARMTH,
    RECOVERY_WARM_BY,
    HourFacts,
    MorningRecoveryConfig,
    find_morning_windows,
    hold_floor,
    parse_time,
    recovery_floors,
    wait_for_warmth_start,
    warm_by_candidates,
)
from custom_components.heatpump_optimizer.engine.optimizer import ScheduleOptimizer
from custom_components.heatpump_optimizer.engine.performance_model import PerformanceModel
from custom_components.heatpump_optimizer.engine.thermal_simulator import ThermalSimulator

# Wednesday 2026-01-14, 02:00. The test stubs make local time == UTC.
NOW = datetime(2026, 1, 14, 2, 0, tzinfo=timezone.utc)
SLEEP = {
    "enabled": True,
    "start": "22:00",
    "end": "06:00",
    "comfort_heat": (59.0, 64.0),
    "comfort_cool": (62.0, 68.0),
}
COMFORT_HEAT = (63.0, 67.0)
COMFORT_COOL = (64.0, 76.0)


def _bins(lo: int, hi: int, slope: float, intercept: float) -> dict:
    return {str(t): slope * t + intercept for t in range(lo, hi + 1)}


def _model() -> PerformanceModel:
    """A house recorded at 62 F indoors, like a real winter one.

    Net heating: +0.6 F/hr at 30 F outdoor, +1.4 at 50 F.
    Drift:       -0.6 F/hr at 30 F outdoor,  0.0 at 50 F.
    """
    heat = (0.04, -0.6)
    resist = (0.03, -1.5)
    cool = (0.05, -6.5)
    return PerformanceModel({
        "temperature": {
            "cool_1": {
                "deltas": _bins(55, 100, *cool),
                "linear_trendline": {"slope": cool[0], "intercept": cool[1]},
                "indoor_anchor_mean": 72.0,
            },
            "heat_1": {
                "deltas": _bins(10, 55, *heat),
                "linear_trendline": {"slope": heat[0], "intercept": heat[1]},
                "indoor_anchor_mean": 62.0,
            },
            "resist": {
                "deltas": _bins(10, 100, *resist),
                "linear_trendline": {"slope": resist[0], "intercept": resist[1]},
                "indoor_anchor_mean": 62.0,
            },
            "auxiliary_heat_1": None,
        },
        "balance_point": {"heat_1": 15.0, "resist": 50.0},
        "differential": {"cool": 1.0, "heat": 1.0},
        "setpoint": {"cool": 74.0, "heat": 63.0},
    })


def _forecast(outdoor, hours: int = 24, start: datetime = NOW) -> list[ForecastPoint]:
    """``outdoor`` is a constant or a function of the hour offset."""
    temp = outdoor if callable(outdoor) else (lambda _i: outdoor)
    return [
        ForecastPoint(time=start + timedelta(hours=i), outdoor_temp=float(temp(i)))
        for i in range(hours)
    ]


def _planner(config: MorningRecoveryConfig | None) -> StrategicPlanner:
    model = _model()
    simulator = ThermalSimulator(model)
    return StrategicPlanner(
        optimizer=ScheduleOptimizer(model, simulator),
        resist_balance_point=50.0,
        sleep_config=dict(SLEEP),
        safety_limits=(58.0, 81.0),
        morning_recovery=config,
    )


class _Clock(datetime):
    """datetime whose now() is pinned; the planner reads the wall clock."""

    pinned = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.pinned


def _optimize(planner, forecast, indoor=59.5, now=NOW, occupancy=OccupancyMode.HOME):
    _Clock.pinned = now
    with patch.object(strategic_mod, "datetime", _Clock):
        return planner.optimize(
            indoor_temp=indoor,
            forecast=forecast,
            comfort_cool=COMFORT_COOL,
            comfort_heat=COMFORT_HEAT,
            current_occupancy=occupancy,
        )


def _targets(schedule) -> dict[int, float]:
    """Heat setpoint by hour of day for the first day of the schedule."""
    return {e.start_time.hour: e.target_temp for e in schedule.entries[:24]}


def _hour(h: int) -> datetime:
    return NOW.replace(hour=h)


# ── Pure window logic ────────────────────────────────────────────────


def _facts(outdoor=lambda h: 30.0, home=True) -> list[HourFacts]:
    facts = []
    for h in range(24):
        t = NOW.replace(hour=h)
        facts.append(HourFacts(
            hour=t, local=t, in_sleep=(h >= 22 or h < 6), home=home,
            day_floor=63.0, outdoor_temp=outdoor(h),
        ))
    return facts


class TestWindows:
    def test_weekday_window_runs_from_sleep_end_to_target(self):
        config = MorningRecoveryConfig()  # warm by 09:00 weekdays
        [window] = find_morning_windows(_facts(), 59.0, dt_time(6, 0), config)
        assert window.deadline == _hour(9)
        assert window.hold_hours == [_hour(6), _hour(7), _hour(8)]
        # Sleep hours within six hours of the target may host an early start
        assert window.early_hours == [_hour(3), _hour(4), _hour(5)]
        assert (window.night_floor, window.day_floor) == (59.0, 63.0)

    def test_weekend_uses_the_weekend_time(self):
        saturday = datetime(2026, 1, 17, 0, 0, tzinfo=timezone.utc)
        facts = [
            HourFacts(hour=saturday + timedelta(hours=h), local=saturday + timedelta(hours=h),
                      in_sleep=(h >= 22 or h < 6), home=True, day_floor=63.0, outdoor_temp=30.0)
            for h in range(24)
        ]
        [window] = find_morning_windows(facts, 59.0, dt_time(6, 0), MorningRecoveryConfig())
        assert window.deadline == saturday.replace(hour=12)

    def test_no_window_when_there_is_nothing_to_recover(self):
        config = MorningRecoveryConfig()
        # Day floor not above the night floor
        assert find_morning_windows(_facts(), 63.0, dt_time(6, 0), config) == []
        # Target time not after the end of sleep
        early = MorningRecoveryConfig(weekday_time=dt_time(6, 0))
        assert find_morning_windows(_facts(), 59.0, dt_time(6, 0), early) == []
        # Nobody home
        assert find_morning_windows(_facts(home=False), 59.0, dt_time(6, 0), config) == []
        # Feature switched to the plain schedule
        off = MorningRecoveryConfig(mode=RECOVERY_AT_SLEEP_END)
        assert find_morning_windows(_facts(), 59.0, dt_time(6, 0), off) == []

    def test_warm_by_holds_the_night_floor_until_the_start(self):
        config = MorningRecoveryConfig(mode=RECOVERY_WARM_BY)
        [window] = find_morning_windows(_facts(), 59.0, dt_time(6, 0), config)
        floors = recovery_floors(window, config, _hour(7))
        assert floors[_hour(6)] == 59.0
        assert floors[_hour(7)] == 63.0 and floors[_hour(8)] == 63.0
        assert _hour(5) not in floors  # sleep hours before the start are untouched

    def test_warm_by_may_start_inside_the_sleep_window(self):
        config = MorningRecoveryConfig(mode=RECOVERY_WARM_BY)
        [window] = find_morning_windows(_facts(), 59.0, dt_time(6, 0), config)
        floors = recovery_floors(window, config, _hour(4))
        assert _hour(3) not in floors
        assert floors[_hour(4)] == 63.0 and floors[_hour(5)] == 63.0

    def test_candidates_are_latest_first_and_never_in_the_past(self):
        config = MorningRecoveryConfig()
        [window] = find_morning_windows(_facts(), 59.0, dt_time(6, 0), config)
        assert warm_by_candidates(window, config, _hour(2)) == [
            _hour(8), _hour(7), _hour(6), _hour(5), _hour(4), _hour(3)
        ]
        assert warm_by_candidates(window, config, _hour(7)) == [_hour(8), _hour(7)]

    def test_wait_holds_the_interim_floor_until_it_is_warm_outside(self):
        config = MorningRecoveryConfig(mode=RECOVERY_WAIT_FOR_WARMTH, warm_outdoor_temp=35.0)
        warming = lambda h: 30.0 + 3.0 * max(0, h - 6)  # 30, 33, 36 at 06/07/08
        [window] = find_morning_windows(_facts(warming), 59.0, dt_time(6, 0), config)
        assert hold_floor(window, config) == 61.0  # halfway by default
        start = wait_for_warmth_start(window, config)
        assert start == _hour(8)
        floors = recovery_floors(window, config, start)
        assert floors[_hour(6)] == 61.0 and floors[_hour(7)] == 61.0
        assert floors[_hour(8)] == 63.0

    def test_wait_starts_at_the_latest_time_when_it_never_warms_up(self):
        config = MorningRecoveryConfig(mode=RECOVERY_WAIT_FOR_WARMTH)
        [window] = find_morning_windows(_facts(), 59.0, dt_time(6, 0), config)
        assert wait_for_warmth_start(window, config) == window.deadline
        floors = recovery_floors(window, config, window.deadline)
        assert set(floors.values()) == {61.0}

    def test_explicit_interim_temperature_is_clamped_between_the_floors(self):
        [window] = find_morning_windows(
            _facts(), 59.0, dt_time(6, 0), MorningRecoveryConfig(mode=RECOVERY_WAIT_FOR_WARMTH)
        )
        low = MorningRecoveryConfig(mode=RECOVERY_WAIT_FOR_WARMTH, hold_temp=50.0)
        high = MorningRecoveryConfig(mode=RECOVERY_WAIT_FOR_WARMTH, hold_temp=70.0)
        assert hold_floor(window, low) == 59.0
        assert hold_floor(window, high) == 63.0

    def test_parse_time_accepts_both_selector_formats(self):
        assert parse_time("09:00", dt_time(1, 0)) == dt_time(9, 0)
        assert parse_time("12:30:00", dt_time(1, 0)) == dt_time(12, 30)
        assert parse_time("nonsense", dt_time(1, 0)) == dt_time(1, 0)
        assert parse_time(None, dt_time(1, 0)) == dt_time(1, 0)


# ── Through the planner and the real simulator ───────────────────────


class TestWarmBy:
    def test_starts_just_in_time_and_arrives(self):
        planner = _planner(MorningRecoveryConfig(mode=RECOVERY_WARM_BY))
        schedule = _optimize(planner, _forecast(40.0))
        targets = _targets(schedule)
        [plan] = planner.recovery_plan[:1]
        start = datetime.fromisoformat(plan["recovery_start"])

        # Night floor before the start, daytime floor from the start on
        assert all(targets[h] == 59.0 for h in range(2, start.hour))
        assert all(targets[h] == 63.0 for h in range(start.hour, 12))
        # Not at the first opportunity, and not left until the deadline
        assert _hour(3) < start < _hour(9)

        at_deadline = [p for p in schedule.simulation if p.time <= _hour(9)][-1]
        assert at_deadline.indoor_temp >= 62.5

    def test_colder_mornings_start_earlier(self):
        mild = _planner(MorningRecoveryConfig(mode=RECOVERY_WARM_BY))
        cold = _planner(MorningRecoveryConfig(mode=RECOVERY_WARM_BY))
        _optimize(mild, _forecast(45.0))
        _optimize(cold, _forecast(32.0))
        mild_start = mild.recovery_plan[0]["recovery_start"]
        cold_start = cold.recovery_plan[0]["recovery_start"]
        assert cold_start < mild_start

    def test_the_ramp_is_not_a_comfort_failure(self):
        """Promise = night floor until the target; setpoint = daytime floor."""
        planner = _planner(MorningRecoveryConfig(mode=RECOVERY_WARM_BY))
        forecast = _forecast(40.0)
        schedule = _optimize(planner, forecast)
        start = datetime.fromisoformat(planner.recovery_plan[0]["recovery_start"])
        by_hour = {pt.time: pt for pt in forecast}

        ramp_hour = by_hour[start]
        assert ramp_hour.comfort_min == 59.0
        assert ramp_hour.heat_target == 63.0
        assert by_hour[_hour(9)].comfort_min == 63.0
        assert by_hour[_hour(9)].heat_target is None
        # A plain flat plan: nothing was pre-heated ahead of the recovery
        assert schedule.plan_label == "flat"
        assert planner.band_at(start, "heat")[0] == 59.0
        assert planner.heat_target_at(start) == 63.0

    def test_a_started_recovery_is_not_undone_by_a_replan(self):
        planner = _planner(MorningRecoveryConfig(mode=RECOVERY_WARM_BY))
        _optimize(planner, _forecast(40.0))
        start = datetime.fromisoformat(planner.recovery_plan[0]["recovery_start"])

        # Re-plan after the start with a house that is suddenly warmer than
        # the model expected, so a fresh search would choose a later start.
        later = start + timedelta(minutes=30)
        schedule = _optimize(
            planner, _forecast(40.0, start=start), indoor=62.0, now=later,
        )
        assert planner.recovery_plan[0]["recovery_start"] == start.isoformat()
        assert schedule.entries[0].target_temp == 63.0

    def test_recovery_state_round_trips_for_persistence(self):
        planner = _planner(MorningRecoveryConfig(mode=RECOVERY_WARM_BY))
        _optimize(planner, _forecast(40.0))
        saved = planner.recovery_state()
        assert saved

        restored = _planner(MorningRecoveryConfig(mode=RECOVERY_WARM_BY))
        restored.restore_recovery_state(saved)
        assert restored.recovery_state() == saved
        restored.restore_recovery_state({"bad": "not a time"})
        assert restored.recovery_state() == {}


class TestWaitForWarmth:
    def test_holds_interim_then_finishes_when_warm_enough(self):
        config = MorningRecoveryConfig(mode=RECOVERY_WAIT_FOR_WARMTH, warm_outdoor_temp=35.0)
        planner = _planner(config)
        # 30 F overnight, reaching 36 F in the 08:00 hour
        outdoor = lambda i: 30.0 if i < 6 else 36.0
        schedule = _optimize(planner, _forecast(outdoor))
        targets = _targets(schedule)

        assert all(targets[h] == 59.0 for h in range(2, 6))       # asleep
        assert targets[6] == 61.0 and targets[7] == 61.0           # interim
        assert targets[8] == 63.0                                  # warm enough
        assert planner.recovery_plan[0]["recovery_start"] == _hour(8).isoformat()

    def test_never_pre_heats_ahead_of_the_wait(self):
        """A cold morning must not turn the wait into an earlier start."""
        config = MorningRecoveryConfig(mode=RECOVERY_WAIT_FOR_WARMTH)
        planner = _planner(config)
        schedule = _optimize(planner, _forecast(31.0))  # never reaches 35 F
        targets = _targets(schedule)

        assert all(targets[h] == 59.0 for h in range(2, 6))
        assert all(targets[h] == 61.0 for h in range(6, 9))
        assert targets[9] == 63.0  # the latest time
        assert "pre-heat" not in schedule.plan_label


class TestPlainSchedule:
    def test_sleep_end_mode_steps_up_when_sleep_ends(self):
        planner = _planner(MorningRecoveryConfig(mode=RECOVERY_AT_SLEEP_END))
        schedule = _optimize(planner, _forecast(40.0))
        targets = _targets(schedule)
        assert targets[5] == 59.0 and targets[6] == 63.0
        assert planner.recovery_plan == []

    def test_no_sleep_schedule_means_no_recovery(self):
        planner = _planner(MorningRecoveryConfig(mode=RECOVERY_WARM_BY))
        planner.sleep_config = {"enabled": False}
        schedule = _optimize(planner, _forecast(40.0))
        assert set(_targets(schedule).values()) == {63.0}
        assert planner.recovery_plan == []

    def test_an_empty_house_gets_the_away_band_not_a_recovery(self):
        planner = _planner(MorningRecoveryConfig(mode=RECOVERY_WARM_BY))
        schedule = _optimize(planner, _forecast(40.0), occupancy=OccupancyMode.AWAY)
        # 63 - 4 = 59 all day; no hour is "home", so nothing to recover
        assert set(_targets(schedule).values()) == {59.0}
        assert planner.recovery_plan == []


@pytest.mark.parametrize("mode", [RECOVERY_WARM_BY, RECOVERY_WAIT_FOR_WARMTH])
def test_shoulder_day_recovery_moves_the_heat_floor_of_the_pair(mode):
    """On a dual-setpoint day the recovery lives on the heat side."""
    planner = _planner(MorningRecoveryConfig(mode=mode, warm_outdoor_temp=35.0))
    # Cold morning, warm afternoon: crosses the balance point by 5 F each way
    outdoor = lambda i: 34.0 if i < 8 else 60.0
    schedule = _optimize(planner, _forecast(outdoor))
    assert schedule.dual_setpoint
    lows = {e.start_time.hour: e.target_temp_low for e in schedule.entries[:24]}
    highs = {e.target_temp_high for e in schedule.entries[:24]}

    assert lows[2] == 59.0          # asleep
    assert lows[12] == 63.0         # after the target time
    assert min(lows.values()) >= 58.0 and max(highs) <= 81.0
    assert planner.recovery_plan
