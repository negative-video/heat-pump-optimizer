"""Dashboard archive: forecast scoring, hourly log, daily roll-up, recoveries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from custom_components.heatpump_optimizer.dashboard_archive import (
    LEADS_HOURS,
    DashboardArchive,
    fit_runtime_to_weather,
)

UTC = timezone.utc
START = datetime(2026, 1, 14, 0, 0, tzinfo=UTC)


@dataclass
class _Point:
    time: datetime
    indoor_temp: float


def _feed(archive, when, **kw):
    """One 5-minute cycle. Tests treat local time as UTC."""
    defaults = dict(
        indoor=62.0, outdoor=35.0, hvac_action="idle", aux_active=False,
        heat_setpoint=59.0, interval_minutes=5.0, controlled=False,
    )
    defaults.update(kw)
    archive.record_cycle(when, when, **defaults)


def _run(archive, start, hours, fn=lambda t: {}):
    for step in range(int(hours * 12)):
        when = start + timedelta(minutes=5 * step)
        _feed(archive, when, **fn(when))


class TestForecastScoring:
    def _plan(self, made_at, slope=0.0, base=62.0):
        return [
            _Point(made_at + timedelta(minutes=5 * i), base + slope * (5 * i / 60.0))
            for i in range(24 * 12 + 1)
        ]

    def test_each_lead_is_scored_against_the_reading_at_that_instant(self):
        archive = DashboardArchive()
        # Plan says flat 62; the house actually cools 0.5 F per hour.
        archive.record_plan(START, self._plan(START), 62.0, controlled=False)
        _run(archive, START, 25, lambda t: {"indoor": 62.0 - 0.5 * (t - START).total_seconds() / 3600})

        result = archive.verification(START + timedelta(hours=25))
        by_lead = {row["lead"]: row for row in result["leads"]}
        assert set(by_lead) == set(LEADS_HOURS)
        assert by_lead[6]["bias"] == -3.0  # measured minus predicted
        assert by_lead[6]["mae"] == 3.0
        # "No change" predicts 62 as well here, so it is exactly as wrong
        assert by_lead[6]["no_change_mae"] == 3.0
        assert by_lead[1]["n"] == 1

    def test_a_good_forecast_beats_no_change(self):
        archive = DashboardArchive()
        archive.record_plan(START, self._plan(START, slope=-0.5), 62.0, controlled=True)
        _run(archive, START, 13, lambda t: {"indoor": 62.0 - 0.5 * (t - START).total_seconds() / 3600})
        by_lead = {row["lead"]: row for row in archive.verification(START + timedelta(hours=13))["leads"]}
        assert by_lead[12]["mae"] == 0.0
        assert by_lead[12]["no_change_mae"] == 6.0

    def test_one_plan_per_clock_hour(self):
        archive = DashboardArchive()
        archive.record_plan(START, self._plan(START), 62.0, False)
        archive.record_plan(START + timedelta(minutes=20), self._plan(START), 62.0, False)
        assert len(archive.to_dict()["pending"]) == len(LEADS_HOURS)

    def test_a_missed_instant_is_dropped_not_scored_late(self):
        archive = DashboardArchive()
        archive.record_plan(START, self._plan(START), 62.0, False)
        # Nothing fed for two hours, then a reading: the 1 h forecast is stale
        _feed(archive, START + timedelta(hours=2))
        leads = [row["lead"] for row in archive.verification(START + timedelta(hours=3))["leads"]]
        assert 1 not in leads

    def test_error_is_split_by_hour_and_outdoor_bin(self):
        archive = DashboardArchive()
        for h in range(6):
            made = START + timedelta(hours=h)
            archive.record_plan(made, self._plan(made), 62.0, False)
            _run(archive, made, 1, lambda t: {"indoor": 61.0, "outdoor": 33.0})
        _feed(archive, START + timedelta(hours=6), indoor=61.0, outdoor=33.0)
        result = archive.verification(START + timedelta(hours=7))
        assert [row["hour"] for row in result["by_hour"]] == [1, 2, 3, 4, 5, 6]
        assert result["by_outdoor"] == [{"bin": 30, "n": 6, "bias": -1.0, "mae": 1.0}]


class TestHourlyLog:
    def test_minutes_and_means_per_hour(self):
        archive = DashboardArchive()

        def heating_first_half(t):
            return {"hvac_action": "heating" if t.minute < 30 else "idle", "indoor": 60.0, "outdoor": 30.0}

        _run(archive, START, 2.1, heating_first_half)
        first = archive.to_dict()["hours"][0]
        assert first[0] == "2026-01-14T00"
        assert first[1:6] == [60.0, 30.0, 30.0, 0.0, 0.0]

    def test_a_full_day_rolls_up(self):
        archive = DashboardArchive()
        _run(archive, START, 49, lambda t: {"hvac_action": "heating" if t.hour < 6 else "idle",
                                           "aux_active": t.hour == 5, "outdoor": 28.0})
        days = archive.to_dict()["days"]
        assert [d["d"] for d in days] == ["2026-01-14", "2026-01-15"]
        assert days[0]["heat"] == 6.0 and days[0]["aux"] == 1.0 and days[0]["out"] == 28.0

    def test_a_partial_day_is_not_rolled_up(self):
        archive = DashboardArchive()
        _run(archive, START + timedelta(hours=14), 12)
        assert archive.to_dict()["days"] == []

    def test_round_trip(self):
        archive = DashboardArchive()
        archive.record_plan(START, [_Point(START + timedelta(hours=h), 62.0) for h in range(25)], 62.0, True)
        _run(archive, START, 30)
        restored = DashboardArchive.from_dict(archive.to_dict())
        assert restored.to_dict() == archive.to_dict()
        assert DashboardArchive.from_dict(None).hour_count == 0
        assert DashboardArchive.from_dict({"hours": "nonsense", "samples": [[1, 2]]}).hour_count == 0


class TestRecoveries:
    def _morning(self, archive, rate_per_hour=1.0, step_hour=6, step_minute=30):
        state = {"indoor": 60.0}

        def cycle(t):
            stepped = (t.hour, t.minute) >= (step_hour, step_minute) and t.hour < 22
            if stepped and state["indoor"] < 63.0:
                state["indoor"] += rate_per_hour / 12.0
            return {
                "indoor": round(state["indoor"], 2),
                "heat_setpoint": 63.0 if stepped else 59.0,
                "hvac_action": "heating" if stepped and state["indoor"] < 63.0 else "idle",
                "planned_start": "2026-01-14T06:30",
                "deadline": "2026-01-14T09:00",
            }

        _run(archive, START, 14, cycle)

    def test_a_morning_climb_is_timed(self):
        archive = DashboardArchive()
        self._morning(archive, rate_per_hour=1.0)
        [event] = archive.to_dict()["recoveries"]
        assert event["start"] == "2026-01-14T06:30"
        assert (event["from"], event["to"]) == (59.0, 63.0)
        # 60 -> 62.5 at 1 F/hr is 2.5 h
        assert 145 <= event["minutes"] <= 155
        assert event["arrived"] in ("2026-01-14T08:55", "2026-01-14T09:00")
        assert event["heat"] >= 140
        assert event["deadline"] == "2026-01-14T09:00"

    def test_already_warm_is_zero_minutes(self):
        archive = DashboardArchive()
        _run(archive, START, 8, lambda t: {"indoor": 64.0, "heat_setpoint": 63.0 if t.hour >= 6 else 59.0})
        [event] = archive.to_dict()["recoveries"]
        assert event["minutes"] == 0

    def test_an_evening_setpoint_rise_is_not_a_morning(self):
        archive = DashboardArchive()
        _run(archive, START + timedelta(hours=15), 4,
             lambda t: {"indoor": 60.0, "heat_setpoint": 63.0 if t.hour >= 17 else 59.0})
        assert archive.to_dict()["recoveries"] == []

    def test_a_climb_that_never_arrives_is_closed_unresolved(self):
        archive = DashboardArchive()
        _run(archive, START, 16, lambda t: {"indoor": 60.0, "heat_setpoint": 63.0 if t.hour >= 6 else 59.0})
        [event] = archive.to_dict()["recoveries"]
        assert event["arrived"] is None and event["minutes"] is None


class TestRuntimeFit:
    def test_recovers_a_known_relationship(self):
        days = [
            {"d": f"d{i}", "out": float(t), "heat": max(0.0, 0.5 * (55 - t) - 1.0), "aux": 0.0}
            for i, t in enumerate(range(20, 64, 2))
        ]
        fit = fit_runtime_to_weather(days)
        assert fit is not None
        assert 53 <= fit["balance"] <= 57
        assert abs(fit["slope"] - 0.5) < 0.06
        assert fit["r2"] > 0.98

    def test_too_few_days_gives_no_fit(self):
        days = [{"d": "a", "out": 30.0, "heat": 10.0, "aux": 0.0}] * 5
        assert fit_runtime_to_weather(days) is None

    def test_a_summer_with_no_heating_gives_no_fit(self):
        days = [{"d": f"d{i}", "out": 75.0 + i, "heat": 0.0, "aux": 0.0} for i in range(20)]
        assert fit_runtime_to_weather(days) is None


def test_payload_shape():
    archive = DashboardArchive()
    _run(archive, START, 30)
    payload = archive.payload(START + timedelta(hours=30), START + timedelta(hours=30))
    assert set(payload) == {
        "verification", "hours", "days", "today", "fit", "aux_day_hours", "recoveries", "open_recovery",
    }
    assert payload["today"]["d"] == "2026-01-15"
    assert len(payload["hours"]) == 29


class TestHistoryReplay:
    """The startup backfill: aligned recorder series fed through the archive."""

    def test_fill_forward_holds_the_last_known_value(self):
        from custom_components.heatpump_optimizer.coordinator_dashboard import _fill_forward

        grid = [START + timedelta(minutes=5 * i) for i in range(5)]
        series = [(START + timedelta(minutes=4), 1.0), (START + timedelta(minutes=11), None),
                  (START + timedelta(minutes=16), 3.0)]
        assert _fill_forward(series, grid) == [None, 1.0, 1.0, 1.0, 3.0]

    def test_replay_builds_hours_days_and_a_recovery(self):
        from custom_components.heatpump_optimizer.coordinator_dashboard import replay_history

        grid = [START + timedelta(minutes=5 * i) for i in range(26 * 12)]
        hours = [(t - START).total_seconds() / 3600 for t in grid]
        stepped = [6.5 <= h < 22 for h in hours]
        indoor = [60.0 if h < 6.5 else min(63.0, 60.0 + (h - 6.5)) for h in hours]
        archive = DashboardArchive()
        fed = replay_history(
            archive,
            grid,
            indoor=indoor,
            outdoor=[32.0] * len(grid),
            action=["heating" if s and t < 63.0 else "idle" for s, t in zip(stepped, indoor)],
            aux=[None] * len(grid),
            heat_setpoint=[63.0 if s else 59.0 for s in stepped],
        )
        state = archive.to_dict()
        assert fed == len(grid)
        assert [d["d"] for d in state["days"]] == ["2026-01-14"]
        assert state["days"][0]["heat"] == 3.0
        [event] = state["recoveries"]
        assert event["start"] == "2026-01-14T06:30" and event["minutes"] == 150
        assert event["ctl"] is False
