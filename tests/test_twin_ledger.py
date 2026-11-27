"""Tests for the twin ledger (TWIN_STATS_SPEC sections 3.1, 3.3, 5; Phase 2)."""

from __future__ import annotations

import json
import random
from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.heatpump_optimizer.engine.twin import DayInputs, Watts
from custom_components.heatpump_optimizer.learning.twin_ledger import (
    GRADE_MIN_DAYS,
    TwinLedger,
    accuracy,
    actual_e,
    grade,
    is_heating_day,
    is_valid,
)

W = {
    "heat": 3000.0,
    "cool": 3000.0,
    "aux": 10000.0,
    "heat_source": "rated",
    "cool_source": "rated",
    "aux_source": "configured",
}


def _rec(day: str, act=(10.0, 0.0, 0.0), replay=None, arms=None, out=30.0, tin=66.0, source="live", **extra):
    replay = replay or act
    rec = {
        "date": day,
        "source": source,
        "simulated": True,
        "warmup": False,
        "actual": {
            "heat_h": act[0],
            "cool_h": act[1],
            "aux_h": act[2],
            "out": out,
            "in": tin,
            "controlled": 0.0,
            "covered": 1440.0,
            "out_frac": 1.0,
            "metered_kwh": None,
            "mae": [12.0, 24],
        },
        "arms": {
            "replay": {
                "heat_h": replay[0],
                "cool_h": replay[1],
                "aux_h": replay[2],
                "in": tin,
                "off_h": 0.0,
                "end": tin,
            }
        },
        "aux": {"recovery": act[2], "cold": 0.0, "other": 0.0},
        "aux_bins": {},
        "watts": dict(W),
        "ref": [72.0, 72.0],
    }
    for name, hours in (arms or {}).items():
        rec["arms"][name] = {
            "heat_h": hours[0],
            "cool_h": hours[1],
            "aux_h": hours[2],
            "in": tin + 1,
            "off_h": 0.5,
            "end": tin,
        }
    rec.update(extra)
    return rec


def _days(n: int, first: str = "2026-01-01"):
    d0 = date.fromisoformat(first)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def _ledger_with(records: list[dict]) -> TwinLedger:
    led = TwinLedger("UTC", 40.0, -80.0)
    led.records = {r["date"]: r for r in records}
    return led


class TestGrade:
    def test_thresholds(self):
        assert grade(3, 100, 1.0, 0.1) == "warming_up"
        assert grade(10, 5, 1.0, 0.1) == "idle"
        assert grade(10, 100, 1.05, 0.15) == "good"
        assert grade(10, 100, 1.2, 0.3) == "fair"
        assert grade(10, 100, 1.3, 0.3) == "poor"

    def test_gain_split(self):
        assert grade(10, 100, 1.0, 0.15, comp=1.3, aux=0.7) == "poor"
        assert grade(10, 100, 1.0, 0.15, comp=1.2, aux=0.8) == "fair"
        assert grade(10, 100, 1.0, 0.15, comp=1.05, aux=None) == "good"


class TestGainCorrection:
    def test_replay_long_by_twenty_percent(self):
        recs = [
            _rec(d, act=(10.0, 0, 0), replay=(12.0, 0, 0), arms={"reference": (18.0, 0, 0)}, mae=None)
            for d in _days(30)
        ]
        led = _ledger_with(recs)
        acc = led.accuracy()
        assert acc["gain"] == pytest.approx(1.2)
        assert acc["daily_error"] == pytest.approx(0.2)
        assert acc["grade"] == "fair"
        arms = led.arm_stats(acc)
        # raw reference - replay is 6 h * 3 kW = 18 kWh a day, divided by 1.2
        assert arms["reference"]["delta_kwh"] == pytest.approx(30 * 18 / 1.2)
        assert arms["reference"]["est_kwh"] == pytest.approx(30 * (30 + 15))
        assert arms["reference"]["band"] == pytest.approx(0.2)

    def test_gain_clamped(self):
        recs = [_rec(d, act=(10.0, 0, 0), replay=(25.0, 0, 0), arms={"reference": (30.0, 0, 0)}) for d in _days(10)]
        led = _ledger_with(recs)
        acc = led.accuracy()
        assert acc["grade"] == "poor"
        assert led.arm_stats(acc)["reference"]["delta_kwh"] is None


class TestGradeGating:
    @pytest.mark.parametrize(
        ("n", "act", "rep", "want"),
        [
            (GRADE_MIN_DAYS - 1, (10.0, 0, 0), (10.0, 0, 0), "warming_up"),
            (10, (0.1, 0, 0), (0.1, 0, 0), "idle"),
            (10, (10.0, 0, 0), (16.0, 0, 0), "poor"),
        ],
    )
    def test_untrusted_grades_hide_deltas(self, n, act, rep, want):
        recs = [_rec(d, act=act, replay=rep, arms={"reference": (20.0, 0, 0)}) for d in _days(n)]
        led = _ledger_with(recs)
        p = led.payload(datetime(2026, 3, 1, tzinfo=UTC), rate=0.2)
        assert p["grade"] == want
        assert p["reference"]["saved_kwh"] is None
        assert all(a["delta_kwh"] is None for a in p["arms"])


class TestWindows:
    def test_invalid_and_imported_days_excluded(self):
        recs = [_rec(d) for d in _days(35)]
        recs[3]["warmup"] = True
        recs[4]["actual"]["covered"] = 600.0
        recs[5]["actual"]["out_frac"] = 0.5
        recs[6]["source"] = "import"
        recs[7]["simulated"] = False
        led = _ledger_with(recs)
        assert not any(is_valid(r) for r in recs[3:6])
        assert len(led.window()) == 30
        assert len(led.valid_records()) == 30
        assert len(led.valid_records(include_import=True)) == 31

    def test_imported_line_never_moves_grade(self):
        recs = [_rec(d) for d in _days(10)] + [
            _rec(d, replay=(20.0, 0, 0), source="import") for d in _days(20, "2025-01-01")
        ]
        led = _ledger_with(recs)
        p = led.payload(datetime(2026, 3, 1, tzinfo=UTC))
        assert p["grade"] == "good"
        assert p["imported"]["gain"] == pytest.approx(2.0)

    def test_heating_day(self):
        assert is_heating_day(_rec("2026-01-01", act=(2.0, 1.0, 0.0)))
        assert not is_heating_day(_rec("2026-07-01", act=(0.0, 3.0, 0.0)))
        assert is_heating_day(_rec("2026-04-01", act=(0.0, 0.0, 0.0), out=50.0))
        assert not is_heating_day(_rec("2026-06-01", act=(0.0, 0.0, 0.0), out=70.0))


class TestCostOfADegree:
    def test_regression_recovers_slope_and_k(self):
        rng = random.Random(3)
        b_true = 0.4  # heating hours per degree below 60
        recs = []
        for d in _days(60):
            out = rng.uniform(10, 70)
            heat = 2.0 + b_true * max(0.0, 60 - out)
            # Twin's warmer arm costs exactly one more degree of the same fit.
            warmer = heat + b_true * (max(0.0, 61 - out) - max(0.0, 60 - out))
            recs.append(_rec(d, act=(heat, 0, 0), replay=(heat, 0, 0), arms={"warmer": (warmer, 0, 0)}, out=out))
        led = _ledger_with(recs)
        deg = led.degree(30.0)
        assert deg["fit"]["balance"] == 60.0
        assert deg["fit"]["slope"] == pytest.approx(b_true * 3.0, rel=0.01)  # kWh per degree-day at 3 kW
        assert deg["k"] == pytest.approx(1.0, abs=0.02)
        assert deg["today_kwh"] == pytest.approx(b_true * 3.0, rel=0.05)
        assert deg["source"] == "twin"

    def test_falls_back_to_regression(self):
        recs = [_rec(d, act=(5.0 + i * 0.3, 0, 0), out=50.0 - i) for i, d in enumerate(_days(30))]
        led = _ledger_with(recs)
        deg = led.degree(15.0)
        assert deg["source"] == "regression"
        assert deg["today_kwh"] > 0


class TestYearOverYear:
    def _year(self, current_scale=1.0, current_indoor_drop=0.0):
        rng = random.Random(5)
        recs = []
        start = date(2025, 1, 1)
        for i in range(400):
            d = start + timedelta(days=i)
            out = 30 + 25 * (1 - abs(((d.timetuple().tm_yday - 15) % 365) - 182) / 182) * 0 + rng.uniform(-15, 15)
            out = min(out, 55)
            tin = 66.0
            current = d >= date(2026, 1, 1)
            if current:
                tin -= current_indoor_drop
            heat = 0.5 * max(0.0, tin - 8 - out)
            if current:
                heat *= current_scale
            recs.append(_rec(d.isoformat(), act=(heat, 0, 0), out=out, tin=tin))
        return _ledger_with(recs)

    def test_ten_percent_lower(self):
        led = self._year(current_scale=0.9)
        rows = led.year_over_year(Watts(3000, 3000, 10000))
        jan = next(r for r in rows if r["month"] == "2026-01")
        assert jan["total"] == pytest.approx(-0.10, abs=0.02)
        assert jan["house"] == pytest.approx(-0.10, abs=0.02)

    def test_indoor_lowered_shows_in_setpoints(self):
        led = self._year(current_indoor_drop=2.0)
        rows = led.year_over_year(Watts(3000, 3000, 10000))
        jan = next(r for r in rows if r["month"] == "2026-01")
        assert jan["total"] < -0.05
        assert jan["house"] == pytest.approx(0.0, abs=0.02)
        assert jan["setpoints"] == pytest.approx(jan["total"], abs=0.02)


class TestLiveAccumulation:
    def _feed(self, led, start, n, kw):
        out = None
        for i in range(n):
            res = led.record_cycle(start + timedelta(minutes=5 * i), **kw(i))
            out = res or out
        return out

    def test_day_close_and_runtime(self):
        led = TwinLedger("UTC")
        start = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)

        def kw(i):
            heating = 72 <= i < 84  # 06:00-07:00
            return {
                "indoor_temp": 64.0 + (i % 288) / 288,
                "outdoor_temp": 25.0,
                "hvac_mode": "heat",
                "target_temp": 68.0 if 72 <= i % 288 < 240 else 62.0,
                "hvac_action": "heating" if heating else "idle",
                "aux_active": 78 <= i < 84,
                "cloud": 0.5,
                "sun_elevation": 10.0,
            }

        res = self._feed(led, start, 289, kw)
        day, next_top = res
        assert day.date == "2026-01-10"
        assert day.hours == 24
        assert day.covered_min == pytest.approx(1440.0)
        assert day.heat_min[6] == pytest.approx(60.0)
        assert day.aux_min[6] == pytest.approx(30.0)
        assert day.aux_runs == [[390, 420, 25.0, 30.0]]
        assert day.indoor_top[0] == pytest.approx(64.0)
        assert next_top == pytest.approx(64.0)
        assert day.setpoints[0][:3] == [0, "heat", 62.0]
        assert [r[2] for r in day.setpoints] == [62.0, 68.0, 62.0]
        assert day.cloud[3] == 0.5

    def test_gap_capped(self):
        led = TwinLedger("UTC")
        t = datetime(2026, 1, 10, 1, tzinfo=UTC)
        base = {"indoor_temp": 65.0, "outdoor_temp": 30.0, "hvac_mode": "heat", "target_temp": 66.0}
        led.record_cycle(t, hvac_action="heating", **base)
        led.record_cycle(t + timedelta(hours=2), hvac_action="heating", **base)
        day = led._open.finish()
        assert sum(day.heat_min) == pytest.approx(5 + 15)

    def test_plan_rows_and_controlled(self):
        led = TwinLedger("UTC")
        t = datetime(2026, 1, 10, 1, tzinfo=UTC)
        for i in range(4):
            led.record_cycle(
                t + timedelta(minutes=5 * i),
                indoor_temp=65.0,
                outdoor_temp=30.0,
                hvac_mode="heat_cool",
                target_low=64.0,
                target_high=76.0,
                controlled=i < 2,
                plan=("heat", 66.0, None),
            )
        day = led._open.finish()
        assert day.setpoints[0][1:] == ["heat_cool", 64.0, 76.0]
        assert day.plan[0][1:] == ["heat", 66.0, None]
        assert day.controlled == pytest.approx(0.5)


def _synthetic_inputs(n: int, first: str = "2026-01-01"):
    """Real-looking inputs: a house with drift and heating that keeps 66 F."""
    rng = random.Random(9)
    out = []
    d0 = date.fromisoformat(first)
    tin = 66.0
    for k in range(n):
        day = (d0 + timedelta(days=k)).isoformat()
        start = datetime.fromisoformat(day).replace(tzinfo=UTC)
        outdoor = [25 + 10 * rng.random() for _ in range(24)]
        heat, tops, indoor = [], [], []
        for h in range(24):
            tops.append(tin)
            need = 0.05 * (tin - outdoor[h]) - 0.3
            frac = max(0.0, min(1.0, need / 1.5 + rng.uniform(-0.1, 0.1)))
            heat.append(60 * frac)
            tin = tin + frac * (0.3 + 0.036 * outdoor[h]) + 0.05 * (outdoor[h] - tin) + 0.5
            indoor.append((tops[-1] + tin) / 2)
        out.append(
            DayInputs(
                date=day,
                start_utc=start.isoformat(),
                hours=24,
                outdoor=outdoor,
                cloud=[None] * 24,
                sun_up=[None] * 24,
                indoor_start=tops[0],
                indoor=indoor,
                setpoints=[[0, "heat", 62.0, None], [360, "heat", 66.0, None], [1320, "heat", 62.0, None]],
                plan=[[0, "heat", 63.0, None]],
                heat_min=heat,
                cool_min=[0.0] * 24,
                aux_min=[0.0] * 24,
                aux_runs=[],
                covered_min=1440.0,
                indoor_top=tops,
            )
        )
    return out


class TestDayClose:
    def test_continuity_and_warmup(self):
        led = TwinLedger("UTC", 40.0, -80.0)
        days = _synthetic_inputs(40)
        led.fit.add_days(days[:30])  # seed the fit so the fold models exist
        for d in days[30:34]:
            led.close_day(d, rated_heat=3000, rated_cool=3000)
        missing = days[35]
        for d in [missing, *days[36:38]]:
            led.close_day(d, rated_heat=3000, rated_cool=3000)
        r = led.records
        assert r[days[30].date]["warmup"]
        assert not r[days[31].date]["warmup"]
        assert r[days[31].date]["simulated"]
        assert r[missing.date]["warmup"]  # the day before it is missing
        assert not is_valid(r[missing.date])
        assert set(r[days[31].date]["arms"]) >= {"replay", "reference", "warmer", "plan"}

    def test_not_simulated_until_fit_has_data(self):
        led = TwinLedger("UTC", 40.0, -80.0)
        d = _synthetic_inputs(1)[0]
        rec = led.close_day(d)
        assert rec["simulated"] is False
        assert led.inputs[d.date] is d

    def test_import_never_replaces_live(self):
        led = TwinLedger("UTC", 40.0, -80.0)
        days = _synthetic_inputs(40)
        led.records[days[5].date] = _rec(days[5].date)
        for d in days:
            d.source = "import"
        counts = led.add_imported(days, rated_heat=3000, rated_cool=3000)
        assert counts["read"] == 40 and counts["skipped"] == 1 and counts["merged"] == 39
        assert led.records[days[5].date]["source"] == "live"
        assert counts["simulated"] > 0
        sim = next(r for r in led.records.values() if r["source"] == "import" and r["simulated"])
        assert set(sim["arms"]) == {"replay", "reference", "warmer"}

    def test_resimulate_reference(self):
        led = TwinLedger("UTC", 40.0, -80.0)
        days = _synthetic_inputs(40)
        led.fit.add_days(days[:30])
        for d in days[30:36]:
            led.close_day(d, rated_heat=3000, rated_cool=3000, ref_heat=72.0)
        before = led.records[days[33].date]["arms"]["reference"]["heat_h"]
        n = led.resimulate_reference(None, 68.0, 72.0)
        assert n == 6
        assert led.records[days[33].date]["arms"]["reference"]["heat_h"] < before
        assert led.records[days[33].date]["ref"] == [68.0, 72.0]

    def test_payload_shape_and_today(self):
        led = TwinLedger("UTC", 40.0, -80.0)
        days = _synthetic_inputs(45)
        led.fit.add_days(days[:30])
        for d in days[30:45]:
            led.close_day(d, rated_heat=3000, rated_cool=3000)
        now = datetime.fromisoformat(days[-1].date).replace(tzinfo=UTC) + timedelta(days=1)
        for i in range(60):
            led.record_cycle(
                now + timedelta(minutes=5 * i),
                indoor_temp=66.0,
                outdoor_temp=30.0,
                hvac_mode="heat",
                target_temp=66.0,
                hvac_action="heating" if i % 3 == 0 else "idle",
            )
        p = led.payload(now + timedelta(hours=5), rate=0.18, today_outdoor=30.0)
        for key in (
            "grade",
            "days",
            "gain",
            "daily_error",
            "indoor_mae",
            "comp_gain",
            "aux_gain",
            "imported",
            "watts",
            "reference",
            "arms",
            "degree",
            "aux",
            "yoy",
            "daily",
            "by_outdoor",
            "by_hour",
            "today",
            "model",
        ):
            assert key in p
        assert len(p["daily"]) == 15
        assert len(p["by_hour"]) == 24
        assert p["today"]["elapsed_h"] == 5
        assert {a["key"] for a in p["today"]["arms"]} >= {"replay", "reference"}
        json.dumps(p)


class TestPersistence:
    def test_round_trip(self):
        led = TwinLedger("UTC", 40.0, -80.0)
        days = _synthetic_inputs(35)
        led.fit.add_days(days[:30])
        for d in days[30:]:
            led.close_day(d, rated_heat=3000, rated_cool=3000)
        led.record_cycle(datetime(2026, 2, 5, 1, tzinfo=UTC), indoor_temp=66.0, outdoor_temp=30.0, hvac_mode="heat")
        data = json.loads(json.dumps(led.to_dict()))
        back = TwinLedger.from_dict(data, "UTC")
        assert back.to_dict() == data
        assert back.accuracy() == led.accuracy()

    def test_from_empty(self):
        led = TwinLedger.from_dict({}, "UTC", 40.0, -80.0)
        assert led.records == {} and led.inputs == {}
        assert led.fit.lat == 40.0
        p = led.payload(datetime(2026, 1, 1, tzinfo=UTC))
        assert p["grade"] == "warming_up"
        assert TwinLedger.from_dict(None, "UTC").records == {}

    def test_accuracy_function_mae(self):
        recs = [_rec(d) for d in _days(8)]
        acc = accuracy(recs)
        assert acc["indoor_mae"] == pytest.approx(0.5)
        assert actual_e(recs[0]) == pytest.approx(30.0)
