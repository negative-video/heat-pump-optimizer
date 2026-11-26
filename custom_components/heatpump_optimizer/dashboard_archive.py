"""Rolling records behind the dashboard panel.

Home Assistant's recorder keeps about ten days of states and nothing about
what the optimizer predicted. The panel needs three things beyond that:

* how wrong each plan's indoor forecast turned out, by lead time, next to
  the error of simply assuming the temperature would not change;
* an hourly log of temperatures and equipment minutes, kept for two months,
  and a daily roll-up kept for two years;
* each morning's climb from the night setpoint to the daytime one.

Everything here is plain Python with no Home Assistant imports. The
coordinator feeds it once per cycle and persists ``to_dict()``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

# Lead times, in hours, at which each plan's forecast is scored.
LEADS_HOURS = (1, 3, 6, 12, 24)

MAX_SAMPLES = 4500  # about 37 days of five leads an hour
MAX_PENDING = 400
MAX_HOURS = 24 * 62
MAX_DAYS = 730
MAX_RECOVERIES = 120

# How far a reading may sit from the forecast instant and still score it.
MATCH_TOLERANCE = timedelta(minutes=12)

# A day enters the daily roll-up only when most of it was logged.
MIN_DAY_HOURS = 20

# Morning recovery detection
RECOVERY_STEP_F = 1.5  # a heat setpoint rise at least this large
RECOVERY_ARRIVE_TOLERANCE_F = 0.5
RECOVERY_FIRST_HOUR = 3  # local hours in which a rise counts as "morning"
RECOVERY_LAST_HOUR = 13
RECOVERY_GIVE_UP = timedelta(hours=8)

# Runtime-versus-weather fit
FIT_BALANCE_RANGE_F = range(48, 67)
FIT_MIN_DAYS = 10
FIT_MIN_HEATING_DAYS = 6
AUX_DAY_HOURS = 0.25  # strip heat for 15 minutes marks an "aux day"


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = max(0, min(len(sorted_values) - 1, math.ceil(fraction * len(sorted_values)) - 1))
    return sorted_values[index]


def fit_runtime_to_weather(days: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Least-squares fit of daily heating hours against heating degrees.

    ``hours = intercept + slope * max(0, balance - mean_outdoor)``, with the
    balance temperature chosen by grid search. Returns None until there are
    enough days, and enough of them with heating, for the slope to mean
    anything.
    """
    points = [(d["out"], d["heat"] + d["aux"]) for d in days if d.get("out") is not None]
    if len(points) < FIT_MIN_DAYS or sum(1 for _, y in points if y > 0.25) < FIT_MIN_HEATING_DAYS:
        return None
    mean_y = sum(y for _, y in points) / len(points)
    total = sum((y - mean_y) ** 2 for _, y in points)
    if total <= 0:
        return None

    best: dict[str, Any] | None = None
    for balance in FIT_BALANCE_RANGE_F:
        xs = [max(0.0, balance - t) for t, _ in points]
        mean_x = sum(xs) / len(xs)
        spread = sum((x - mean_x) ** 2 for x in xs)
        if spread <= 0:
            continue
        slope = sum((x - mean_x) * (y - mean_y) for x, (_, y) in zip(xs, points, strict=True)) / spread
        if slope <= 0:
            continue
        intercept = mean_y - slope * mean_x
        residuals = [y - max(0.0, intercept + slope * x) for x, (_, y) in zip(xs, points, strict=True)]
        sse = sum(r * r for r in residuals)
        r2 = 1.0 - sse / total
        if best is None or r2 > best["r2"]:
            best = {
                "balance": float(balance),
                "slope": round(slope, 4),
                "intercept": round(intercept, 3),
                "r2": round(r2, 3),
                "sd": round(math.sqrt(sse / len(points)), 2),
                "n": len(points),
            }
    return best


class DashboardArchive:
    """Forecast scoring, hourly log and morning recoveries for the panel."""

    def __init__(self) -> None:
        self._pending: list[dict[str, Any]] = []
        # [target epoch minute, lead h, error, no-change error, outdoor, local hour, controlled]
        self._samples: list[list[Any]] = []
        self._last_plan_hour: str | None = None

        self._bucket: dict[str, Any] | None = None
        # [local hour "YYYY-MM-DDTHH", indoor, outdoor, heat min, cool min, aux min, heat setpoint, controlled]
        self._hours: list[list[Any]] = []
        self._days: list[dict[str, Any]] = []

        self._last_heat_setpoint: float | None = None
        self._open_recovery: dict[str, Any] | None = None
        self._recoveries: list[dict[str, Any]] = []

    # ── Forecast scoring ─────────────────────────────────────────────

    def record_plan(
        self,
        made_at: datetime,
        simulation: list[Any] | None,
        indoor_now: float | None,
        controlled: bool,
    ) -> None:
        """Remember what a fresh plan predicts at each lead time.

        One plan per clock hour is enough; re-plans inside the hour would
        only add correlated samples.
        """
        if not simulation or indoor_now is None:
            return
        hour_key = made_at.strftime("%Y-%m-%dT%H")
        if hour_key == self._last_plan_hour:
            return
        self._last_plan_hour = hour_key

        for lead in LEADS_HOURS:
            target = made_at + timedelta(hours=lead)
            point = min(simulation, key=lambda p: abs((p.time - target).total_seconds()))
            if abs(point.time - target) > MATCH_TOLERANCE:
                continue
            self._pending.append(
                {
                    "t": target.isoformat(),
                    "lead": lead,
                    "pred": round(float(point.indoor_temp), 2),
                    "at": round(float(indoor_now), 2),
                    "ctl": bool(controlled),
                }
            )
        if len(self._pending) > MAX_PENDING:
            self._pending = self._pending[-MAX_PENDING:]

    def _score_pending(self, now: datetime, local: datetime, indoor: float | None, outdoor: float | None) -> None:
        still_waiting = []
        for item in self._pending:
            try:
                target = datetime.fromisoformat(item["t"])
            except (TypeError, ValueError):
                continue
            if now < target:
                still_waiting.append(item)
                continue
            if indoor is None or now - target > MATCH_TOLERANCE:
                continue  # missed the instant; an old reading would not be honest
            self._samples.append(
                [
                    int(target.timestamp() // 60),
                    int(item["lead"]),
                    round(indoor - float(item["pred"]), 2),
                    round(indoor - float(item["at"]), 2),
                    _round(outdoor, 1),
                    local.hour,
                    1 if item.get("ctl") else 0,
                ]
            )
        self._pending = still_waiting
        if len(self._samples) > MAX_SAMPLES:
            self._samples = self._samples[-MAX_SAMPLES:]

    def verification(self, now: datetime, window_days: int = 30) -> dict[str, Any]:
        """Forecast error by lead time, by hour of day and by outdoor temperature."""
        cutoff = int((now - timedelta(days=window_days)).timestamp() // 60)
        samples = [s for s in self._samples if s[0] >= cutoff]

        leads = []
        for lead in LEADS_HOURS:
            rows = [s for s in samples if s[1] == lead]
            if not rows:
                continue
            errors = sorted(abs(s[2]) for s in rows)
            leads.append(
                {
                    "lead": lead,
                    "n": len(rows),
                    "mae": round(sum(errors) / len(rows), 2),
                    "bias": round(sum(s[2] for s in rows) / len(rows), 2),
                    "p90": round(_percentile(errors, 0.9), 2),
                    "no_change_mae": round(sum(abs(s[3]) for s in rows) / len(rows), 2),
                }
            )

        shortest = [s for s in samples if s[1] == LEADS_HOURS[0]]
        by_hour_rows: dict[int, list[float]] = defaultdict(list)
        by_outdoor_rows: dict[int, list[float]] = defaultdict(list)
        for s in shortest:
            by_hour_rows[int(s[5])].append(s[2])
            if s[4] is not None:
                by_outdoor_rows[int(math.floor(s[4] / 5.0) * 5)].append(s[2])

        def _summary(key_name: str, groups: dict[int, list[float]]) -> list[dict[str, Any]]:
            return [
                {
                    key_name: key,
                    "n": len(values),
                    "bias": round(sum(values) / len(values), 2),
                    "mae": round(sum(abs(v) for v in values) / len(values), 2),
                }
                for key, values in sorted(groups.items())
            ]

        span_days = 0.0
        if samples:
            span_days = (samples[-1][0] - samples[0][0]) / 1440.0
        return {
            "leads": leads,
            "by_hour": _summary("hour", by_hour_rows),
            "by_outdoor": _summary("bin", by_outdoor_rows),
            "samples": len(samples),
            "span_days": round(span_days, 1),
            "controlled_fraction": round(sum(s[6] for s in samples) / len(samples), 2) if samples else 0.0,
            "window_days": window_days,
        }

    # ── Per-cycle feed ───────────────────────────────────────────────

    def record_cycle(
        self,
        now: datetime,
        local: datetime,
        *,
        indoor: float | None,
        outdoor: float | None,
        hvac_action: str | None,
        aux_active: bool,
        heat_setpoint: float | None,
        interval_minutes: float,
        controlled: bool,
        planned_start: str | None = None,
        deadline: str | None = None,
        forecast_indoor: float | None = None,
    ) -> None:
        """Feed one control cycle. ``now`` is UTC, ``local`` the same instant locally.

        ``indoor`` is the temperature the panel shows. ``forecast_indoor`` is
        the reading the plan was simulated from (the thermostat's own), so
        forecasts are scored against the quantity they predicted.
        """
        self._score_pending(now, local, indoor if forecast_indoor is None else forecast_indoor, outdoor)
        self._log_hour(local, indoor, outdoor, hvac_action, aux_active, heat_setpoint, interval_minutes, controlled)
        self._track_recovery(
            local,
            indoor,
            outdoor,
            hvac_action,
            aux_active,
            heat_setpoint,
            interval_minutes,
            controlled,
            planned_start,
            deadline,
        )
        if heat_setpoint is not None:
            self._last_heat_setpoint = heat_setpoint

    # ── Hourly log and daily roll-up ─────────────────────────────────

    def _log_hour(
        self,
        local: datetime,
        indoor: float | None,
        outdoor: float | None,
        hvac_action: str | None,
        aux_active: bool,
        heat_setpoint: float | None,
        minutes: float,
        controlled: bool,
    ) -> None:
        key = local.strftime("%Y-%m-%dT%H")
        if self._bucket is not None and self._bucket["key"] != key:
            self._close_hour()
        if self._bucket is None:
            self._bucket = {
                "key": key, "in": 0.0, "n_in": 0, "out": 0.0, "n_out": 0,
                "heat": 0.0, "cool": 0.0, "aux": 0.0, "ctl": 0.0, "min": 0.0, "sp": None,
            }  # fmt: skip
        bucket = self._bucket
        if indoor is not None:
            bucket["in"] += indoor
            bucket["n_in"] += 1
        if outdoor is not None:
            bucket["out"] += outdoor
            bucket["n_out"] += 1
        if hvac_action == "heating":
            bucket["heat"] += minutes
        elif hvac_action == "cooling":
            bucket["cool"] += minutes
        if aux_active:
            bucket["aux"] += minutes
        if controlled:
            bucket["ctl"] += minutes
        bucket["min"] += minutes
        if heat_setpoint is not None:
            bucket["sp"] = heat_setpoint

    def _close_hour(self) -> None:
        bucket, self._bucket = self._bucket, None
        if not bucket or bucket["min"] <= 0:
            return
        record = [
            bucket["key"],
            _round(bucket["in"] / bucket["n_in"], 1) if bucket["n_in"] else None,
            _round(bucket["out"] / bucket["n_out"], 1) if bucket["n_out"] else None,
            round(min(60.0, bucket["heat"]), 1),
            round(min(60.0, bucket["cool"]), 1),
            round(min(60.0, bucket["aux"]), 1),
            _round(bucket["sp"], 1),
            round(min(1.0, bucket["ctl"] / bucket["min"]), 2),
        ]
        previous_date = self._hours[-1][0][:10] if self._hours else None
        self._hours.append(record)
        # A day closes with its last hour. The date-change check covers a day
        # whose 23:00 hour was never logged (restart, outage).
        if previous_date is not None and previous_date != record[0][:10]:
            self._close_day(previous_date)
        if record[0][11:13] == "23":
            self._close_day(record[0][:10])
        if len(self._hours) > MAX_HOURS:
            self._hours = self._hours[-MAX_HOURS:]

    def _close_day(self, date: str) -> None:
        if any(d["d"] == date for d in self._days[-3:]):
            return
        summary = self._summarize_day(date)
        if summary is not None:
            self._days.append(summary)
            if len(self._days) > MAX_DAYS:
                self._days = self._days[-MAX_DAYS:]

    def _summarize_day(self, date: str, minimum_hours: int = MIN_DAY_HOURS) -> dict[str, Any] | None:
        rows = [h for h in self._hours if h[0][:10] == date]
        if len(rows) < minimum_hours:
            return None
        outdoor = [h[2] for h in rows if h[2] is not None]
        return {
            "d": date,
            "out": round(sum(outdoor) / len(outdoor), 1) if outdoor else None,
            "heat": round(sum(h[3] for h in rows) / 60.0, 2),
            "cool": round(sum(h[4] for h in rows) / 60.0, 2),
            "aux": round(sum(h[5] for h in rows) / 60.0, 2),
            "ctl": round(sum(h[7] for h in rows) / len(rows), 2),
            "n": len(rows),
        }

    # ── Morning recoveries ───────────────────────────────────────────

    def _track_recovery(
        self,
        local: datetime,
        indoor: float | None,
        outdoor: float | None,
        hvac_action: str | None,
        aux_active: bool,
        heat_setpoint: float | None,
        minutes: float,
        controlled: bool,
        planned_start: str | None,
        deadline: str | None,
    ) -> None:
        stamp = local.strftime("%Y-%m-%dT%H:%M")
        previous = self._last_heat_setpoint
        open_ = self._open_recovery

        if open_ is None:
            rose = heat_setpoint is not None and previous is not None and heat_setpoint - previous >= RECOVERY_STEP_F
            if not rose or not RECOVERY_FIRST_HOUR <= local.hour < RECOVERY_LAST_HOUR:
                return
            open_ = self._open_recovery = {
                "start": stamp,
                "from": _round(previous, 1),
                "to": _round(heat_setpoint, 1),
                "in": _round(indoor, 1),
                "out": _round(outdoor, 1),
                "heat": 0.0,
                "aux": 0.0,
                "ctl": bool(controlled),
                "planned_start": planned_start,
                "deadline": deadline,
            }
        else:
            if hvac_action == "heating":
                open_["heat"] += minutes
            if aux_active:
                open_["aux"] += minutes
            if heat_setpoint is not None and heat_setpoint > open_["to"]:
                open_["to"] = _round(heat_setpoint, 1)

        started = datetime.strptime(open_["start"], "%Y-%m-%dT%H:%M")
        elapsed = local.replace(tzinfo=None) - started
        if indoor is not None and indoor >= open_["to"] - RECOVERY_ARRIVE_TOLERANCE_F:
            self._finish_recovery(stamp, int(round(elapsed.total_seconds() / 60.0)))
        elif elapsed > RECOVERY_GIVE_UP or (heat_setpoint is not None and heat_setpoint < open_["to"] - 1.0):
            self._finish_recovery(None, None)

    def _finish_recovery(self, arrived: str | None, minutes: int | None) -> None:
        record, self._open_recovery = self._open_recovery, None
        if record is None:
            return
        record["arrived"] = arrived
        record["minutes"] = minutes
        record["heat"] = round(record["heat"], 1)
        record["aux"] = round(record["aux"], 1)
        self._recoveries.append(record)
        if len(self._recoveries) > MAX_RECOVERIES:
            self._recoveries = self._recoveries[-MAX_RECOVERIES:]

    # ── Output ───────────────────────────────────────────────────────

    @property
    def hour_count(self) -> int:
        return len(self._hours)

    def payload(self, now: datetime, local: datetime) -> dict[str, Any]:
        """Everything the panel draws from this archive."""
        days = list(self._days)
        today = self._summarize_day(local.strftime("%Y-%m-%d"), minimum_hours=1)
        return {
            "verification": self.verification(now),
            "hours": self._hours[-24 * 35 :],
            "days": days,
            "today": today,
            "fit": fit_runtime_to_weather(days),
            "aux_day_hours": AUX_DAY_HOURS,
            "recoveries": self._recoveries[-40:],
            "open_recovery": self._open_recovery,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending": self._pending,
            "samples": self._samples,
            "last_plan_hour": self._last_plan_hour,
            "bucket": self._bucket,
            "hours": self._hours,
            "days": self._days,
            "last_heat_setpoint": self._last_heat_setpoint,
            "open_recovery": self._open_recovery,
            "recoveries": self._recoveries,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> DashboardArchive:
        archive = cls()
        if not isinstance(data, dict):
            return archive
        archive._pending = [p for p in data.get("pending") or [] if isinstance(p, dict)][-MAX_PENDING:]
        archive._samples = [s for s in data.get("samples") or [] if isinstance(s, list) and len(s) == 7][-MAX_SAMPLES:]
        archive._last_plan_hour = data.get("last_plan_hour")
        bucket = data.get("bucket")
        archive._bucket = bucket if isinstance(bucket, dict) and "key" in bucket else None
        archive._hours = [h for h in data.get("hours") or [] if isinstance(h, list) and len(h) == 8][-MAX_HOURS:]
        archive._days = [d for d in data.get("days") or [] if isinstance(d, dict) and "d" in d][-MAX_DAYS:]
        setpoint = data.get("last_heat_setpoint")
        archive._last_heat_setpoint = float(setpoint) if isinstance(setpoint, (int, float)) else None
        open_ = data.get("open_recovery")
        archive._open_recovery = open_ if isinstance(open_, dict) and "start" in open_ else None
        archive._recoveries = [r for r in data.get("recoveries") or [] if isinstance(r, dict)][-MAX_RECOVERIES:]
        return archive
