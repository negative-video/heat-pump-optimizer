"""Twin engine (engine/twin.py): arms, one-day runs, continuity, aux, import.

Phase 1 rows of the test table in TWIN_STATS_SPEC.md section 9.
"""

from __future__ import annotations

import io
import os
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.heatpump_optimizer.engine.data_types import ScheduleEntry
from custom_components.heatpump_optimizer.engine.performance_model import PerformanceModel
from custom_components.heatpump_optimizer.engine.thermal_simulator import ThermalSimulator
from custom_components.heatpump_optimizer.engine.twin import (
    DayInputs,
    DayResult,
    Watts,
    _DayBuilder,
    append_row,
    arm_schedules,
    build_typical_schedule,
    classify_aux,
    classify_aux_run,
    eligible_arms,
    energy_kwh,
    parse_beestat_csv,
    plan_row,
    plan_schedule,
    recovery_windows,
    reference_schedule,
    replay_schedule,
    resolve_watts,
    result_kwh,
    row_at,
    run_continuous,
    run_scenario,
    schedule_arm_schedule,
    schedule_entries,
    setpoint_row,
    warmer_schedule,
)

NY = ZoneInfo("America/New_York")
SAMPLE_CSV = os.path.join(os.path.dirname(__file__), "..", "docs", "internal", "Beestat Export - 531634979810.csv")
WATTS = Watts(heat=3480.0, cool=3480.0, aux=12500.0)


def _model() -> PerformanceModel:
    return PerformanceModel.from_defaults()


def _day(
    day: str = "2026-01-15",
    outdoor: float | list = 25.0,
    setpoints: list[list] | None = None,
    indoor_start: float = 62.0,
    controlled: float = 0.0,
    plan: list[list] | None = None,
    hours: int = 24,
) -> DayInputs:
    d = date.fromisoformat(day)
    start = datetime(d.year, d.month, d.day, tzinfo=UTC)
    out = outdoor if isinstance(outdoor, list) else [float(outdoor)] * hours
    return DayInputs(
        date=day,
        start_utc=start.isoformat(),
        hours=hours,
        outdoor=out,
        cloud=[None] * hours,
        sun_up=[None] * hours,
        indoor_start=indoor_start,
        indoor=[indoor_start] * hours,
        setpoints=setpoints if setpoints is not None else [[0, "heat_cool", 62.0, 74.0]],
        plan=plan if plan is not None else [],
        heat_min=[0.0] * hours,
        cool_min=[0.0] * hours,
        aux_min=[0.0] * hours,
        aux_runs=[],
        controlled=controlled,
        covered_min=hours * 60.0,
        source="live",
    )


# ── Identical schedules ────────────────────────────────────────────


class TestIdenticalSchedules:
    def test_copy_of_replay_schedule_gives_identical_result(self):
        model = _model()
        day = _day(setpoints=[[0, "heat_cool", 59.0, 74.0], [360, "heat_cool", 63.0, 74.0], [1320, "heat", 59.0, None]])
        replay = run_scenario(model, day, replay_schedule(day), 61.0)
        other = run_scenario(model, day, [list(r) for r in day.setpoints], 61.0)
        assert other == replay
        assert result_kwh(other, WATTS) - result_kwh(replay, WATTS) == 0.0

    def test_schedule_arm_matching_the_day_equals_replay(self):
        """A typical schedule equal to the day's own setpoints is a no-op arm."""
        model = _model()
        day = _day(setpoints=[[0, "heat", 60.0, None], [420, "heat", 63.0, None]], controlled=1.0)
        slots = [["heat", 60.0 if s * 15 < 420 else 63.0, None] for s in range(96)]
        typical = {"weekday": slots, "weekend": slots}
        sched = schedule_arm_schedule(day, typical, UTC)
        a = run_scenario(model, day, sched, 61.0)
        b = run_scenario(model, day, replay_schedule(day), 61.0)
        assert a == b


# ── Monotonic warmth ───────────────────────────────────────────────


class TestMonotonicWarmth:
    def test_warmer_uses_at_least_replay_on_cold_day(self):
        model = _model()
        day = _day(outdoor=25.0, setpoints=[[0, "heat_cool", 59.0, 70.0], [390, "heat_cool", 62.0, 74.0]])
        replay = run_scenario(model, day, replay_schedule(day), 61.0)
        warmer = run_scenario(model, day, warmer_schedule(day), 61.0)
        assert result_kwh(warmer, WATTS) >= result_kwh(replay, WATTS)
        assert warmer.indoor_mean > replay.indoor_mean

    def test_reference_72_uses_more_than_60_hold(self):
        model = _model()
        day = _day(outdoor=25.0, setpoints=[[0, "heat", 60.0, None]])
        hold60 = run_scenario(model, day, replay_schedule(day), 60.0)
        ref72 = run_scenario(model, day, reference_schedule(day, 72.0, 72.0), 60.0)
        assert result_kwh(ref72, WATTS) > result_kwh(hold60, WATTS)


# ── Run-length round trip ──────────────────────────────────────────


def _expected(sample):
    mode, heat, cool = sample
    return {"heat": ("heat", heat), "cool": ("cool", cool), "heat_cool": ("dual", heat, cool), "off": ("off",)}[mode]


def _observed(entry: ScheduleEntry):
    if entry.is_dual_setpoint:
        return ("dual", entry.target_temp_low, entry.target_temp_high)
    if entry.mode == "off":
        return ("off",)
    return (entry.mode, entry.target_temp)


def _round_trip(start: datetime, hours: int, sample_at):
    """Samples -> run-length rows -> ScheduleEntry; check every 5-minute step."""
    rows: list[list] = []
    samples = []
    for step in range(hours * 12):
        minute = step * 5
        sample = sample_at(start + timedelta(minutes=minute))
        samples.append(sample)
        append_row(rows, minute, *sample)
    entries = schedule_entries(rows, start, hours * 60)
    sim = ThermalSimulator(_model())
    assert entries[0].start_time == start
    assert entries[-1].end_time == start + timedelta(hours=hours)
    for step, sample in enumerate(samples):
        entry = sim._get_schedule_entry(start + timedelta(minutes=step * 5), entries)
        assert entry is not None
        assert _observed(entry) == _expected(sample), f"step {step}"
    return rows


class TestRunLengthRoundTrip:
    def test_mode_changes_and_setpoint_steps(self):
        start = datetime(2026, 4, 2, tzinfo=UTC)

        def sample_at(t):
            h = t.hour + t.minute / 60
            if h < 6:
                return ("heat", 59.0, None)
            if h < 9:
                return ("heat_cool", 63.0, 74.0)
            if h < 12.25:
                return ("off", None, None)
            if h < 20:
                return ("cool", None, 75.0)
            return ("heat_cool", 60.5, 71.0)

        rows = _round_trip(start, 24, sample_at)
        assert [r[1] for r in rows] == ["heat", "heat_cool", "off", "cool", "heat_cool"]
        assert rows[3][0] == 735

    def test_rows_only_on_change(self):
        rows: list[list] = []
        assert append_row(rows, 0, "heat", 60.0, None)
        assert not append_row(rows, 5, "heat", 60.0, None)
        assert append_row(rows, 10, "heat", 61.0, None)
        assert len(rows) == 2

    @pytest.mark.parametrize(("day", "hours"), [("2026-03-08", 23), ("2025-11-02", 25)])
    def test_dst_day(self, day, hours):
        d = date.fromisoformat(day)
        midnight = datetime(d.year, d.month, d.day, tzinfo=NY)
        start = midnight.astimezone(UTC)
        nxt = d + timedelta(days=1)
        assert (datetime(nxt.year, nxt.month, nxt.day, tzinfo=NY) - midnight) == timedelta(hours=24)
        assert round((datetime(nxt.year, nxt.month, nxt.day, tzinfo=NY).astimezone(UTC) - start).total_seconds()) == (
            hours * 3600
        )

        def sample_at(t):
            local = t.astimezone(NY)
            if local.hour < 6:
                return ("heat_cool", 59.0, 70.0)
            if local.hour < 22:
                return ("heat_cool", 62.0, 74.0)
            return ("heat", 60.0, None)

        rows = _round_trip(start, hours, sample_at)
        # The 06:00 local step lands at elapsed minute 300 +/- 60 on a DST day.
        expected = 5 * 60 if hours == 23 else 7 * 60
        assert rows[1][0] == expected

    def test_typical_schedule_follows_wall_clock_on_dst_day(self):
        day = _day(day="2026-03-08", hours=23)
        midnight = datetime(2026, 3, 8, tzinfo=NY).astimezone(UTC)
        day.start_utc = midnight.isoformat()
        slots = [["heat", 63.0 if s >= 24 else 59.0, None] for s in range(96)]  # 06:00 step
        rows = schedule_arm_schedule(day, {"weekday": slots, "weekend": slots}, NY)
        assert rows == [[0, "heat", 59.0, None], [300, "heat", 63.0, None]]


# ── Setpoint and plan row mapping ──────────────────────────────────


class TestRowMapping:
    def test_thermostat_state_mapping(self):
        assert setpoint_row("heat", 61.0, 60.0, 75.0) == ("heat", 61.0, None)
        assert setpoint_row("cool", 74.0, 60.0, 75.0) == ("cool", None, 74.0)
        assert setpoint_row("heat_cool", None, 60.0, 75.0) == ("heat_cool", 60.0, 75.0)
        assert setpoint_row("off", 70.0) == ("off", None, None)

    def test_plan_entry_mapping(self):
        t = datetime(2026, 1, 1, tzinfo=UTC)
        dual = ScheduleEntry(t, t, 62.0, "heat", target_temp_low=62.0, target_temp_high=75.0)
        assert plan_row(dual) == ("heat_cool", 62.0, 75.0)
        assert plan_row(ScheduleEntry(t, t, 61.0, "heat")) == ("heat", 61.0, None)
        assert plan_row(ScheduleEntry(t, t, 76.0, "cool")) == ("cool", None, 76.0)
        assert plan_row(None) == ("off", None, None)


# ── Arm schedules ──────────────────────────────────────────────────


class TestArmSchedules:
    def test_reference_copies_mode_and_merges(self):
        day = _day(
            setpoints=[
                [0, "heat", 59.0, None],
                [360, "heat", 63.0, None],
                [600, "heat_cool", 62.0, 70.0],
                [700, "cool", None, 76.0],
                [800, "off", None, None],
            ]
        )
        ref = reference_schedule(day, 72.0, 72.0)
        assert ref == [
            [0, "heat", 72.0, None],
            [600, "heat_cool", 72.0, 74.0],
            [700, "cool", None, 72.0],
            [800, "off", None, None],
        ]

    def test_warmer_shifts_each_side_present(self):
        day = _day(setpoints=[[0, "heat", 59.0, None], [100, "heat_cool", 62.0, 74.0], [200, "cool", None, 75.0]])
        assert warmer_schedule(day) == [
            [0, "heat", 60.0, None],
            [100, "heat_cool", 63.0, 73.0],
            [200, "cool", None, 74.0],
        ]

    def test_plan_skipped_when_uncovered_over_two_hours(self):
        covered = _day(plan=[[0, "heat", 60.0, None], [600, "off", None, None], [720, "heat", 62.0, None]])
        assert plan_schedule(covered) is not None
        gap = _day(plan=[[0, "heat", 60.0, None], [600, "off", None, None], [725, "heat", 62.0, None]])
        assert plan_schedule(gap) is None
        assert plan_schedule(_day(plan=[])) is None


# ── Arm eligibility ────────────────────────────────────────────────


class TestArmEligibility:
    PLAN = [[0, "heat", 60.0, None]]

    def _typical(self):
        slots = [["heat", 61.0, None]] * 96
        return {"weekday": slots, "weekend": slots}

    def test_monitor_only_day_gets_plan_not_schedule(self):
        day = _day(controlled=0.05, plan=self.PLAN)
        assert eligible_arms(day) == ["replay", "reference", "warmer", "plan"]
        arms = arm_schedules(day, typical=self._typical(), tz=UTC)
        assert "plan" in arms
        assert "schedule" not in arms

    def test_controlled_day_gets_schedule_not_plan(self):
        day = _day(controlled=0.95, plan=self.PLAN)
        assert eligible_arms(day) == ["replay", "reference", "warmer", "schedule"]
        arms = arm_schedules(day, typical=self._typical(), tz=UTC)
        assert "schedule" in arms
        assert "plan" not in arms

    def test_mixed_day_gets_neither(self):
        day = _day(controlled=0.5, plan=self.PLAN)
        assert eligible_arms(day) == ["replay", "reference", "warmer"]
        arms = arm_schedules(day, typical=self._typical(), tz=UTC)
        assert set(arms) == {"replay", "reference", "warmer"}

    def test_boundaries(self):
        assert "plan" in eligible_arms(_day(controlled=0.1))
        assert "schedule" in eligible_arms(_day(controlled=0.9))

    def test_schedule_unavailable_without_typical(self):
        arms = arm_schedules(_day(controlled=1.0), typical=None, tz=UTC)
        assert "schedule" not in arms


# ── Typical schedule ───────────────────────────────────────────────


class TestTypicalSchedule:
    def _days(self, n, start="2026-01-01"):
        out = []
        d0 = date.fromisoformat(start)
        for i in range(n):
            d = d0 + timedelta(days=i)
            heat = 63.0 if i % 2 else 61.0
            out.append(_day(day=d.isoformat(), setpoints=[[0, "heat", 59.0, None], [360, "heat", heat, None]]))
        return out

    def test_slot_medians(self):
        days = self._days(28)
        typ = build_typical_schedule(days, "2026-01-29", UTC)
        assert typ is not None
        assert typ["weekday"][0] == ["heat", 59.0, None]
        assert typ["weekday"][24][0] == "heat"
        assert 61.0 <= typ["weekday"][24][1] <= 63.0

    def test_unavailable_with_four_weekdays(self):
        # 2026-01-05 is a Monday: Mon-Thu plus a full weekend gives 4 weekdays.
        days = self._days(4, "2026-01-05") + self._days(2, "2026-01-10")
        assert build_typical_schedule(days, "2026-01-12", UTC) is None

    def test_controlled_days_excluded(self):
        days = self._days(28)
        for d in days:
            d.controlled = 1.0
        assert build_typical_schedule(days, "2026-01-29", UTC) is None


# ── Continuity and warm-up ─────────────────────────────────────────


class TestContinuity:
    def test_day_two_starts_at_day_one_end_temp(self):
        model = _model()
        sp = [[0, "heat_cool", 59.0, 74.0], [390, "heat_cool", 63.0, 74.0]]
        d1 = _day("2026-01-15", outdoor=30.0, setpoints=sp, indoor_start=62.0)
        d2 = _day("2026-01-16", outdoor=28.0, setpoints=sp, indoor_start=66.0)
        recs = run_continuous(model, [d1, d2], arms=("replay", "reference"))
        r1 = recs[0]["results"]
        r2 = recs[1]["results"]
        assert recs[0]["warmup"] is True
        assert recs[1]["warmup"] is False
        # Not re-anchored to the real indoor_start of day two.
        assert r2["replay"] == run_scenario(model, d2, replay_schedule(d2), r1["replay"].end_temp)
        assert r2["reference"] == run_scenario(model, d2, reference_schedule(d2), r1["reference"].end_temp)

    def test_missing_day_forces_warmup(self):
        model = _model()
        d1 = _day("2026-01-15")
        d3 = _day("2026-01-17", indoor_start=64.0)
        d4 = _day("2026-01-18")
        recs = run_continuous(model, [d1, d3, d4])
        assert [r["warmup"] for r in recs] == [True, True, False]
        assert recs[1]["results"]["replay"] == run_scenario(model, d3, replay_schedule(d3), 64.0)

    def test_unsimulated_day_forces_warmup(self):
        model = _model()
        d1 = _day("2026-01-15")
        d2 = _day("2026-01-16", outdoor=[30.0] * 19 + [None] * 5)
        d3 = _day("2026-01-17")
        recs = run_continuous(model, [d1, d2, d3])
        assert recs[1]["simulated"] is False
        assert recs[1]["results"] == {}
        assert recs[2]["warmup"] is True

    def test_outdoor_gaps_are_interpolated(self):
        out = [30.0] * 24
        out[5] = None
        out[6] = None
        out[0] = None
        day = _day(outdoor=out)
        res = run_scenario(_model(), day, replay_schedule(day), 62.0)
        assert res is not None
        assert len(res.indoor_hourly) == 24


# ── One-day result shape ───────────────────────────────────────────


class TestRunScenario:
    def test_hourly_shape_and_totals(self):
        day = _day(outdoor=20.0, setpoints=[[0, "heat", 64.0, None]])
        res = run_scenario(_model(), day, replay_schedule(day), 60.0)
        assert len(res.heat_min_hourly) == 24
        assert len(res.indoor_hourly) == 24
        assert res.heat_h == pytest.approx(sum(res.heat_min_hourly) / 60)
        assert res.cool_h == 0.0
        # Starts 4 F below a 64 setpoint: the first hour is off-setpoint.
        assert res.off_setpoint_h > 0

    def test_dst_day_hour_count(self):
        day = _day(day="2026-03-08", hours=23)
        res = run_scenario(_model(), day, replay_schedule(day), 62.0)
        assert len(res.heat_min_hourly) == 23

    def test_round_trip_dict(self):
        day = _day(outdoor=20.0)
        res = run_scenario(_model(), day, replay_schedule(day), 60.0)
        assert DayResult.from_dict(res.to_dict()) == res
        assert DayInputs.from_dict(day.to_dict()) == day
        assert DayInputs.from_dict({}).hours == 24


# ── Energy from runtime ────────────────────────────────────────────


class TestEnergy:
    def test_formula(self):
        assert energy_kwh(2.0, 1.0, 0.5, WATTS) == pytest.approx((2 * 3480 + 3480 + 0.5 * 12500) / 1000)

    def test_resolve_watts(self):
        w = resolve_watts(rated_heat=3500, rated_cool=3400, measured_heat_kwh=34.8, measured_heat_h=10.0)
        assert (w.heat, w.heat_source) == (pytest.approx(3480.0), "measured")
        assert (w.cool, w.cool_source) == (3400.0, "rated")
        assert (w.aux, w.aux_source) == (10000.0, "assumed")
        w = resolve_watts(rated_heat=3500, rated_cool=3400, measured_heat_h=9.0, aux_kw_configured=12.5)
        assert w.heat_source == "rated"
        assert (w.aux, w.aux_source) == (12500.0, "configured")


# ── Aux classes ────────────────────────────────────────────────────


class TestAuxClasses:
    STEP = [[0, "heat_cool", 59.0, 70.0], [390, "heat_cool", 63.0, 74.0]]  # 4 F step at 06:30

    def test_run_before_step_is_recovery(self):
        windows = recovery_windows(self.STEP)
        assert classify_aux_run([340, 360, 40.0], windows) == "recovery"  # ends 30 min before the step

    def test_cold_run_without_step(self):
        assert classify_aux_run([100, 130, 20.0], []) == "cold"

    def test_mild_run_without_step(self):
        assert classify_aux_run([100, 130, 45.0], []) == "other"

    def test_window_edges(self):
        windows = recovery_windows(self.STEP)
        assert windows == [(330.0, 570.0)]
        assert classify_aux_run([300, 330, 40.0], windows) == "other"
        assert classify_aux_run([570, 600, 20.0], windows) == "cold"

    def test_small_step_is_not_recovery(self):
        windows = recovery_windows([[0, "heat", 61.0, None], [390, "heat", 62.0, None]])
        assert windows == []

    def test_smart_recovery_ramp_counts_as_one_step(self):
        ramp = [[0, "heat_cool", 59.0, 70.0]]
        for i, sp in enumerate((59.6, 60.2, 60.8, 61.4, 62.0)):
            ramp.append([360 + i * 5, "heat_cool", sp, 74.0])
        windows = recovery_windows(ramp)
        assert windows == [(300.0, 560.0)]  # first rise at 360, last at 380

    def test_learned_threshold(self):
        assert classify_aux_run([100, 130, 30.0], [], threshold_f=25.0) == "other"
        assert classify_aux_run([100, 130, 30.0], [], threshold_f=None) == "cold"

    def test_classify_day_hours(self):
        day = _day(setpoints=self.STEP)
        day.aux_runs = [[340, 370, 30.0], [100, 130, 20.0], [900, 915, 45.0]]
        hours = classify_aux(day)
        assert hours == {"recovery": 0.5, "cold": 0.5, "other": 0.25}


# ── Import parser ──────────────────────────────────────────────────

HEADER = (
    'Timestamp,"Compressor 1","Compressor 2","Compressor Mode","Auxiliary Heat 1","Auxiliary Heat 2",Fan,'
    'Accessory,"Accessory Type","System Mode","Indoor Temperature","Indoor Humidity","Outdoor Temperature",'
    '"Outdoor Humidity",Event,Climate,"Setpoint Cool","Setpoint Heat"'
)


def _row(ts, comp=0, mode="off", aux=0, sysmode="auto", tin=62.0, tout=30.0, cool=74.0, heat=60.0):
    return f'"{ts}",{comp},0,{mode},{aux},0,0,0,off,{sysmode},{tin},50,{tout},60,,Home,{cool},{heat}'


def _csv(day: str, fn) -> list[str]:
    lines = [HEADER]
    t = datetime.fromisoformat(day)
    for i in range(288):
        ts = t + timedelta(minutes=5 * i)
        lines.append(fn(i, ts.strftime("%Y-%m-%d %H:%M:%S")))
    return lines


class TestImportParser:
    def test_comp_before_min_counts_unbroken_heating(self):
        def fn(i, ts):
            if 60 <= i < 72:  # 05:00-06:00 compressor heating, aux from 05:30
                return _row(ts, comp=300, mode="heat", aux=300 if i >= 66 else 0)
            if 100 <= i < 103:  # aux with a cold compressor
                return _row(ts, aux=300)
            return _row(ts)

        d = parse_beestat_csv(io.StringIO("\n".join(_csv("2026-01-15", fn))), UTC)[0]
        assert [r[3] for r in d.aux_runs] == [30.0, 0.0]

    def test_comp_before_min_carries_across_midnight(self):
        lines = [HEADER]
        t = datetime.fromisoformat("2026-01-15")
        for i in range(288 * 2):
            ts = (t + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M:%S")
            if 282 <= i < 294:  # 23:30 day 1 to 00:30 day 2, aux from 00:10
                lines.append(_row(ts, comp=300, mode="heat", aux=300 if i >= 290 else 0))
            else:
                lines.append(_row(ts))
        days = parse_beestat_csv(io.StringIO("\n".join(lines)), UTC)
        assert days[1].aux_runs[0][3] == 40.0

    def test_indoor_top_needs_a_sample_at_the_top_of_the_hour(self):
        def fn(i, ts):
            return _row(ts, tin=60.0 + i / 12)

        lines = _csv("2026-01-15", fn)
        del lines[1 + 36]  # drop 03:00
        d = parse_beestat_csv(io.StringIO("\n".join(lines)), UTC)[0]
        assert d.indoor_top[2] == pytest.approx(62.0)
        assert d.indoor_top[3] is None
        assert d.indoor_top[4] == pytest.approx(64.0)

    def test_seconds_become_minutes_and_hours(self):
        def fn(i, ts):
            if 72 <= i < 84:  # 06:00-07:00: full heating, aux for 3 rows of 120 s
                return _row(ts, comp=300, mode="heat", aux=120 if i < 75 else 0, heat=63.0)
            if 200 <= i < 206:
                return _row(ts, comp=150, mode="cool")
            return _row(ts)

        days = parse_beestat_csv(io.StringIO("\n".join(_csv("2026-01-15", fn))), UTC)
        assert len(days) == 1
        d = days[0]
        assert d.source == "import"
        assert d.hours == 24
        assert d.covered_min == 1440
        assert d.heat_h == pytest.approx(12 * 300 / 3600)
        assert d.aux_h == pytest.approx(3 * 120 / 3600)
        assert d.cool_h == pytest.approx(6 * 150 / 3600)
        assert d.heat_min[6] == pytest.approx(60.0)
        assert d.aux_runs == [[360, 375, 30.0, 0.0]]  # aux started with the compressor
        assert d.indoor_top == [62.0] * 24
        assert d.setpoints[0] == [0, "heat_cool", 60.0, 74.0]
        assert row_at(d.setpoints, 400)[2] == 63.0
        assert d.cloud == [None] * 24
        assert d.plan == []
        assert d.controlled == 0.0

    def test_blank_indoor_rows_skipped_and_short_days_dropped(self):
        def fn(i, ts):
            return _row(ts, tin="" if i >= 200 else 62.0)

        assert parse_beestat_csv(io.StringIO("\n".join(_csv("2026-01-15", fn))), UTC) == []

    def test_mode_mapping(self):
        def fn(i, ts):
            sysmode = "heat" if i < 100 else "cool" if i < 200 else "off"
            return _row(ts, sysmode=sysmode)

        d = parse_beestat_csv(io.StringIO("\n".join(_csv("2026-05-15", fn))), UTC)[0]
        assert d.setpoints == [[0, "heat", 60.0, None], [500, "cool", None, 74.0], [1000, "off", None, None]]

    def test_fall_back_day_has_25_hours(self):
        lines = [HEADER]
        t = datetime(2025, 11, 2)
        for i in range(288):
            ts = (t + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(_row(ts))
            if 12 <= i < 24:  # Beestat writes the repeated 01:00 hour as blank rows
                lines.append(_row(ts, tin=""))
        d = parse_beestat_csv(io.StringIO("\n".join(lines)), NY)[0]
        assert d.hours == 25
        assert len(d.outdoor) == 25
        assert d.indoor[2] is None  # the repeated hour has no samples
        assert d.indoor[3] is not None
        assert d.start_utc == "2025-11-02T04:00:00+00:00"

    @pytest.mark.skipif(not os.path.exists(SAMPLE_CSV), reason="Beestat sample lives in gitignored docs/internal")
    def test_sample_first_2000_lines(self):
        with open(SAMPLE_CSV, newline="") as fh:
            lines = [next(fh) for _ in range(2000)]
        days = parse_beestat_csv(lines, NY)
        # 1,999 rows: six whole days plus 271 rows (22.6 h) of the seventh, which passes the 20 h rule.
        assert len(days) == 7
        assert days[-1].covered_min == 271 * 5
        raw_heat = raw_cool = raw_aux = 0.0
        import csv

        for row in csv.DictReader(lines):
            if row["Timestamp"][:10] > days[-1].date or not row["Indoor Temperature"]:
                continue
            secs = float(row["Compressor 1"] or 0)
            if row["Compressor Mode"] == "heat":
                raw_heat += max(secs, float(row["Auxiliary Heat 1"] or 0))
            elif row["Compressor Mode"] == "cool":
                raw_cool += secs
            raw_aux += float(row["Auxiliary Heat 1"] or 0)
        assert sum(d.heat_h for d in days) == pytest.approx(raw_heat / 3600)
        assert sum(d.cool_h for d in days) == pytest.approx(raw_cool / 3600)
        assert sum(d.aux_h for d in days) == pytest.approx(raw_aux / 3600)
        for d in days:
            assert d.hours == 24
            assert d.covered_min >= 1200
            assert 0 <= d.heat_h + d.cool_h <= 24
            assert sum(v is not None for v in d.outdoor) >= 20
            assert 40 < d.indoor_mean < 85
            assert d.setpoints[0][0] == 0


def test_day_builder_ignores_rows_outside_day():
    b = _DayBuilder(date(2026, 1, 15), UTC)
    b.add(datetime(2026, 1, 16, 0, 5, tzinfo=UTC), {"Indoor Temperature": "62"})
    assert b.rows == 0
