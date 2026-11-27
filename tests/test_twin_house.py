"""Tests for the twin house model (TWIN_STATS_SPEC 3.5, 3.6; Phase 1b)."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.heatpump_optimizer.engine.data_types import ForecastPoint, ScheduleEntry
from custom_components.heatpump_optimizer.engine.performance_model import PerformanceModel
from custom_components.heatpump_optimizer.engine.thermal_simulator import ThermalSimulator
from custom_components.heatpump_optimizer.engine.twin import DayInputs, run_continuous, run_scenario
from custom_components.heatpump_optimizer.engine.twin_house import (
    N_COEF,
    TWIN_AUX_MIN_STARTS,
    TWIN_FIT_HALF_LIFE_DAYS,
    TWIN_FIT_MIN_DAYS,
    WALL_AZIMUTHS,
    Sums,
    TwinFit,
    TwinHouseModel,
    fit_rows,
    fold_of,
    learn_aux_rule,
    solve,
    sun_position,
    sun_series,
)

LAT, LON = 40.0, -80.0

# heat, heat*Tout, cool, cool*Tout, aux, k, const, roof, wall, roof_lag2, wall_lag2, all_lag8
TRUE_BETA = [0.4, 0.035, -6.0, 0.04, 3.0, 0.05, 0.5, 0.0, 1.2, 0.0, 0.0, 0.8]


def _blank_day(day: str, outdoor: list[float], **kw) -> DayInputs:
    start = datetime.fromisoformat(day).replace(tzinfo=UTC)
    n = len(outdoor)
    base = {
        "date": day,
        "start_utc": start.isoformat(),
        "hours": n,
        "outdoor": outdoor,
        "cloud": [None] * n,
        "sun_up": [None] * n,
        "indoor_start": 70.0,
        "indoor": [70.0] * n,
        "setpoints": [[0, "heat", 68.0, None]],
        "plan": [],
        "heat_min": [0.0] * n,
        "cool_min": [0.0] * n,
        "aux_min": [0.0] * n,
        "aux_runs": [],
        "covered_min": 1440.0,
        "indoor_top": [70.0] * n,
    }
    base.update(kw)
    return DayInputs(**base)


def _x(d: DayInputs, h: int, sun: list[tuple]) -> list[float]:
    seg = sun[h * 12 : (h + 1) * 12]
    sm = [sum(t[j] for t in seg) / 12 for j in range(5)]
    fh, fc, fa = d.heat_min[h] / 60, d.cool_min[h] / 60, d.aux_min[h] / 60
    tout = d.outdoor[h]
    return [fh, fh * tout, fc, fc * tout, fa, tout - d.indoor[h], 1.0, *sm]


def _synthetic_days(n_days: int, beta: list[float], wall_az: int, first: str = "2026-06-01", seed: int = 7):
    """Days whose hourly indoor changes are exactly ``x . beta``."""
    rng = random.Random(seed)
    days: list[DayInputs] = []
    top = 72.0
    prev = None
    d0 = date.fromisoformat(first)
    for k in range(n_days):
        day = (d0 + timedelta(days=k)).isoformat()
        outdoor = [75 + 12 * rng.random() + 8 * ((h - 6) % 24 > 8) for h in range(24)]
        cool = [rng.choice([0.0, 15.0, 30.0, 45.0, 60.0]) for _ in range(24)]
        heat = [0.0] * 24
        aux = [0.0] * 24
        if k % 3 == 0:  # some heating days so every column varies
            heat = [rng.choice([0.0, 20.0, 40.0]) for _ in range(24)]
            aux = [v / 2 if rng.random() < 0.3 else 0.0 for v in heat]
            cool = [0.0] * 24
        d = _blank_day(day, outdoor, heat_min=heat, cool_min=cool, aux_min=aux)
        sun = sun_series(d, prev, LAT, LON, wall_az)
        tops = [top]
        for h in range(24):
            d.indoor[h] = tops[h] + rng.uniform(-0.5, 0.5)
            tops.append(tops[h] + sum(a * b for a, b in zip(_x(d, h, sun), beta, strict=True)))
        d.indoor_top = tops[:24]
        d.indoor_start = tops[0]
        top = tops[24]
        days.append(d)
        prev = d
    return days


class TestSolve:
    def test_fit_recovers_parameters(self):
        rng = random.Random(1)
        s = Sums()
        for _ in range(2000):
            x = [rng.random(), rng.random() * 40, rng.random(), rng.random() * 90, rng.random()]
            x += [rng.uniform(-40, 30), 1.0] + [rng.random() for _ in range(5)]
            s.add(x, sum(a * b for a, b in zip(x, TRUE_BETA, strict=True)))
        beta = s.solve()
        for got, want in zip(beta, TRUE_BETA, strict=True):
            assert got == pytest.approx(want, rel=0.01, abs=1e-3)
        assert s.rss(beta) == pytest.approx(0.0, abs=1e-6)

    def test_singular_returns_none(self):
        assert solve([[0.0] * N_COEF for _ in range(N_COEF)], [0.0] * N_COEF) is None
        assert Sums().solve() is None

    def test_batches_equal_one_fit(self):
        rng = random.Random(2)
        rows = [([rng.random() for _ in range(N_COEF)], rng.random()) for _ in range(300)]
        one, two = Sums(), Sums()
        for x, y in rows:
            one.add(x, y)
        a, b = Sums(), Sums()
        for x, y in rows[:120]:
            a.add(x, y)
        for x, y in rows[120:]:
            b.add(x, y)
        two = a.plus(b)
        assert two.solve() == pytest.approx(one.solve())

    def test_round_trip(self):
        s = Sums()
        s.add([float(i) for i in range(N_COEF)], 2.0, 0.5)
        back = Sums.from_dict(s.to_dict())
        assert back.full() == s.full()
        assert back.xty == s.xty
        assert back.yty == s.yty and back.n == s.n


class TestSunPosition:
    def test_equinox_noon_and_midnight(self):
        # Solar noon at longitude 0 on the March equinox is about 12:07 UTC.
        elev, az = sun_position(datetime(2026, 3, 20, 12, 7, tzinfo=UTC), 40.0, 0.0)
        assert elev == pytest.approx(50.0, abs=1.0)
        assert az == pytest.approx(180.0, abs=1.0)
        assert sun_position(datetime(2026, 3, 20, 0, 7, tzinfo=UTC), 40.0, 0.0)[0] < 0

    def test_west_wall_lit_in_afternoon_only(self):
        d = _blank_day("2026-06-21", [80.0] * 24)
        sun = sun_series(d, None, LAT, LON, 270)
        local_noon_step = 17 * 12  # 12:00 at -80 lon is about 17:20 UTC
        assert sun[local_noon_step - 36][1] == 0.0  # morning: wall in shade
        assert sun[local_noon_step + 48][1] > 0.2  # late afternoon: lit


class TestSunSeries:
    def test_length_and_identical_per_call(self):
        d = _blank_day("2026-06-21", [80.0] * 24)
        a = sun_series(d, None, LAT, LON, 270)
        assert len(a) == 24 * 12 + 1
        assert a == sun_series(d, None, LAT, LON, 270)

    def test_continuous_across_days(self):
        d1 = _blank_day("2026-06-21", [80.0] * 24)
        d2 = _blank_day("2026-06-22", [80.0] * 24)
        s1 = sun_series(d1, None, LAT, LON, 270)
        s2 = sun_series(d2, d1, LAT, LON, 270)
        for a, b in zip(s1[-1], s2[0], strict=True):
            assert a == pytest.approx(b, abs=1e-3)

    def test_cloud_dims_the_sun(self):
        clear = _blank_day("2026-06-21", [80.0] * 24)
        cloudy = _blank_day("2026-06-21", [80.0] * 24, cloud=[1.0] * 24)
        a = sun_series(clear, None, LAT, LON, 270)
        b = sun_series(cloudy, None, LAT, LON, 270)
        assert sum(s[0] for s in b) < 0.3 * sum(s[0] for s in a)

    def test_arms_share_the_series_but_not_state(self):
        days = _synthetic_days(2, TRUE_BETA, 270)
        model = TwinHouseModel(TRUE_BETA, 270)
        sun = sun_series(days[1], days[0], LAT, LON, 270)
        a = model.with_sun(sun, days[1].start)
        b = model.with_sun(sun, days[1].start)
        a.begin_step(days[1].start + timedelta(hours=18))
        assert a._sun != b._sun
        assert model._series is None


class TestTwinFit:
    def test_wall_azimuth_and_coefficients(self):
        days = _synthetic_days(40, TRUE_BETA, 270)
        fit = TwinFit(LAT, LON)
        fit.add_days(days)
        assert fit.wall_az == 270
        beta = fit.coefficients()
        for got, want in zip(beta, TRUE_BETA, strict=True):
            assert got == pytest.approx(want, rel=0.02, abs=0.02)

    def test_duplicate_date_counted_once(self):
        days = _synthetic_days(3, TRUE_BETA, 270)
        fit = TwinFit(LAT, LON)
        fit.add_days(days)
        n = fit.sums[0][270].n + fit.sums[1][270].n
        assert fit.add_day(days[1]) is False
        assert fit.sums[0][270].n + fit.sums[1][270].n == n

    def test_half_life_decay(self):
        days = _synthetic_days(1, TRUE_BETA, 270)
        fit = TwinFit(LAT, LON)
        fit.add_day(days[0])
        f = fold_of(days[0].date)
        n0 = fit.sums[f][270].n
        later = DayInputs.from_dict(days[0].to_dict())
        later.date = (date.fromisoformat(days[0].date) + timedelta(days=int(TWIN_FIT_HALF_LIFE_DAYS))).isoformat()
        later.start_utc = datetime.fromisoformat(later.date).replace(tzinfo=UTC).isoformat()
        assert fold_of(later.date) != f
        fit.add_day(later)
        assert fit.sums[f][270].n == pytest.approx(n0 * 0.5, rel=1e-9)
        assert fit.days[f] == pytest.approx(0.5)

    def test_older_day_added_at_decayed_weight(self):
        days = _synthetic_days(2, TRUE_BETA, 270)
        fit = TwinFit(LAT, LON)
        fit.add_day(days[1])
        fit.add_day(days[0])
        f = fold_of(days[0].date)
        rows = len(fit_rows(days[0], sun_series(days[0], None, LAT, LON, 270), days[1].indoor_top[0]))
        assert fit.sums[f][270].n == pytest.approx((rows - 1) * 0.5 ** (1 / TWIN_FIT_HALF_LIFE_DAYS), rel=0.05)

    def test_fold_models_and_minimum_data(self):
        days = _synthetic_days(40, TRUE_BETA, 270)
        fit = TwinFit(LAT, LON)
        fit.add_days(days[:10])
        assert fit.model_for(days[11]) is None  # fewer than 14 days per fold
        fit.add_days(days[10:])
        day = days[20]
        model = fit.model_for(day)
        assert model is not None
        assert model.beta == fit.coefficients(1 - fold_of(day.date))
        assert model.beta != fit.coefficients(fold_of(day.date))

    def test_unseen_mode_not_simulated(self):
        days = [d for d in _synthetic_days(60, TRUE_BETA, 270) if d.heat_h == 0]  # cooling only
        fit = TwinFit(LAT, LON)
        fit.add_days(days)
        cold = _blank_day("2026-12-01", [20.0] * 24, heat_min=[30.0] * 24)
        assert fit.model_for(cold) is None
        assert fit.model_for(days[-1]) is not None

    def test_persistence(self):
        days = _synthetic_days(30, TRUE_BETA, 270)
        fit = TwinFit(LAT, LON)
        fit.add_days(days)
        back = TwinFit.from_dict(fit.to_dict())
        assert back.coefficients() == pytest.approx(fit.coefficients())
        assert back.wall_az == fit.wall_az
        assert back.dates == fit.dates
        empty = TwinFit.from_dict({}, lat=LAT, lon=LON)
        assert empty.coefficients() is None
        assert empty.summary()["coefficients"] is None

    def test_run_continuous_with_fold_models(self):
        days = _synthetic_days(40, TRUE_BETA, 270)
        fit = TwinFit(LAT, LON)
        fit.add_days(days)
        recs = run_continuous(None, days, arms=("replay", "warmer"), model_for=fit.model_for, sun_for=fit.sun_for)
        assert all(r["simulated"] for r in recs)
        assert recs[0]["warmup"] and not recs[1]["warmup"]
        recs = run_continuous(None, days, model_for=lambda d: None)
        assert not any(r["simulated"] for r in recs)

    def test_aux_starts_collected(self):
        d = _blank_day("2026-01-10", [20.0] * 24, aux_runs=[[300, 330, 18.0, 30.0], [400, 410, 25.0]])
        fit = TwinFit(LAT, LON)
        fit.add_day(d)
        assert fit.aux_starts == [[30.0, 18.0]]


class TestAuxRule:
    def test_learned_from_starts(self):
        starts = [[30.0, 10.0 + i] for i in range(20)] + [[180.0, 31.0]]
        rule = learn_aux_rule(starts)
        assert rule["runtime_min"] == 30.0
        assert rule["lockout_f"] == pytest.approx(29.0)  # index int(0.99 * 20) of 21 sorted values

    def test_too_few_starts(self):
        assert learn_aux_rule([[30.0, 20.0]] * (TWIN_AUX_MIN_STARTS - 1)) is None


def _cold_forecast(start: datetime, outdoor: float) -> list[ForecastPoint]:
    return [ForecastPoint(time=start + timedelta(minutes=30 + 60 * h), outdoor_temp=outdoor) for h in range(4)]


class TestSimulatorHooks:
    START = datetime(2026, 1, 10, tzinfo=UTC)

    def _run(self, model, outdoor=20.0, start_temp=60.0, target=68.0, hours=3):
        sched = [
            ScheduleEntry(
                start_time=self.START, end_time=self.START + timedelta(hours=hours), target_temp=target, mode="heat"
            )
        ]
        return ThermalSimulator(model).simulate(start_temp, _cold_forecast(self.START, outdoor), sched)

    def test_begin_step_once_per_step(self):
        calls = []

        class Counting(TwinHouseModel):
            def begin_step(self, time):
                calls.append(time)

        pts = self._run(Counting(TRUE_BETA, 270))
        assert calls == [p.time for p in pts]

    def test_runtime_aux_engages_at_r_minutes(self):
        slow = [0.1, 0.0, 0, 0, 0.5, 0.0, 0.0, 0, 0, 0, 0, 0]  # +0.1 F/hr heat, +0.6 with aux
        model = TwinHouseModel(slow, 270, aux_rule={"runtime_min": 30, "lockout_f": 32.0})
        pts = self._run(model)
        aux = [p.aux_running for p in pts]
        assert aux[:6] == [False] * 6
        assert all(aux[6:])

    def test_no_aux_above_lockout(self):
        slow = [0.1, 0.0, 0, 0, 0.5, 0.0, 0.0, 0, 0, 0, 0, 0]
        model = TwinHouseModel(slow, 270, aux_rule={"runtime_min": 30, "lockout_f": 32.0})
        assert not any(p.aux_running for p in self._run(model, outdoor=40.0))

    def test_aux_drops_at_target(self):
        fast = [1.0, 0.0, 0, 0, 20.0, 0.0, 0.0, 0, 0, 0, 0, 0]  # strips reach target fast
        model = TwinHouseModel(fast, 270, aux_rule={"runtime_min": 30, "lockout_f": 32.0})
        pts = self._run(model, start_temp=66.0)
        for p in pts:
            if p.aux_running:
                assert p.indoor_temp < 68.0
        assert any(p.aux_running for p in pts)
        assert any(p.hvac_running and not p.aux_running and p.indoor_temp >= 68.0 for p in pts)

    def test_performance_model_unchanged(self):
        model = PerformanceModel.from_defaults()
        pts = self._run(model)
        assert not hasattr(model, "aux_rule")
        assert len(pts) == 37

    def test_mock_models_ignore_hooks(self):
        from unittest.mock import MagicMock

        mock = MagicMock()
        mock.heat_differential = 1.0
        mock.cool_differential = 1.0
        mock.heat_balance_point = 25.0
        mock.heating_delta.return_value = 1.0
        mock.passive_drift.return_value = -0.5
        mock.aux_heating_delta.return_value = 1.0
        pts = self._run(mock)
        assert pts  # a MagicMock aux_rule is not a dict, so the droop rule runs


class TestRunScenarioWithSun:
    def test_sun_changes_drift_only_when_given(self):
        d = _blank_day("2026-06-21", [85.0] * 24, setpoints=[[0, "off", None, None]], indoor_start=75.0)
        model = TwinHouseModel(TRUE_BETA, 270)
        dark = run_scenario(model, d, d.setpoints, 75.0, None)
        lit = run_scenario(model, d, d.setpoints, 75.0, sun_series(d, None, LAT, LON, 270))
        assert lit.end_temp > dark.end_temp

    def test_every_azimuth_fitted(self):
        fit = TwinFit(LAT, LON)
        assert set(fit.sums[0]) == set(WALL_AZIMUTHS)
        assert TWIN_FIT_MIN_DAYS == 14
