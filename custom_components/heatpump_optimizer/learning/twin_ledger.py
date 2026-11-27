"""Twin ledger: day accumulation, per-day twin results and every twin statistic.

Pure Python, no Home Assistant imports. Design of record:
``docs/internal/TWIN_STATS_SPEC.md`` sections 3, 5 and 8.

The coordinator feeds ``record_cycle`` once per update. When the local date
changes the finished ``DayInputs`` comes back, and ``close_day`` folds it into
the twin house fit, simulates every arm with the model of the other fold,
classifies aux and stores a compact per-day record. All statistics read those
records. Nothing here changes control or feeds the lifetime savings ledger.

A caution on the reference figure in heating season (section 5.2): earlier
simulation work on this house found the sign of the night setback's effect is
not robust (between -8% and +13% of winter kWh across model variants), because
deep recoveries pull in strip heat. It is the least certain number here; the
band exists for that reason.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from ..dashboard_archive import fit_runtime_to_weather
from ..engine.twin import (
    DEFAULT_REFERENCE_COOL_F,
    DEFAULT_REFERENCE_HEAT_F,
    STEP_MINUTES,
    DayInputs,
    DayResult,
    Watts,
    append_row,
    arm_schedules,
    build_typical_schedule,
    can_simulate,
    classify_aux,
    energy_kwh,
    resolve_watts,
    run_scenario,
    setpoint_row,
)
from ..engine.twin_house import TwinFit

# ── Tunables (section 5) ─────────────────────────────────────────────

WINDOW_DAYS = 30
INPUTS_KEEP_DAYS = 60
RESULTS_KEEP_DAYS = 800
DAILY_DAYS = 60
VALID_COVERED_MIN = 1200
VALID_OUTDOOR_FRAC = 0.8

GRADE_MIN_DAYS = 7
GRADE_IDLE_KWH = 10.0
GRADE_GOOD_GAIN = 0.10
GRADE_GOOD_ERROR = 0.20
GRADE_FAIR_GAIN = 0.25
GRADE_FAIR_ERROR = 0.35
AUX_GAIN_MIN_H = 2.0
TRUSTED_GRADES = ("good", "fair")

GAIN_CLAMP = (0.67, 1.5)
BAND_MIN = 0.10
BAND_MAX = 0.50

DEGREE_FIT_DAYS = 120
DEGREE_BIN_F = 5
DEGREE_MIN_BIN_DAYS = 2
K_MIN_DAYS = 10
K_MIN_R2 = 0.5
HEAT_BALANCE_RANGE = range(45, 71)
COOL_BALANCE_RANGE = range(60, 81)

WATTS_WINDOW_DAYS = 30
CYCLE_MAX_GAP_MIN = 15.0
# Season to date counts from this month (July 1: a whole winter, then a
# whole summer up to the next July).
SEASON_START_MONTH = 7

YOY_MIN_DAYS = 10
YOY_MIN_PRIOR = 20
YOY_PRIOR_MIN_AGE = 300
YOY_PRIOR_MAX_AGE = 430
YOY_MIN_R2 = 0.6
YOY_D_RANGE = range(0, 21)

ARM_ORDER = ("reference", "schedule", "plan", "warmer", "replay")
ARM_LABELS = {
    "replay": "Twin, your setpoints",
    "reference": "Holding the reference",
    "warmer": "1 F warmer",
    "plan": "Optimizer plan",
    "schedule": "Your usual schedule",
}
ARM_SHORT = {
    "replay": "Twin",
    "reference": "Hold ref",
    "warmer": "1 F warmer",
    "plan": "Plan",
    "schedule": "Schedule",
}


def _arm_label(key: str, cooling: bool, ref_heat: float, ref_cool: float, short: bool = False) -> str:
    if key == "warmer" and cooling:
        return "1 F cooler"
    if key == "reference":
        ref = ref_cool if cooling else ref_heat
        return f"Hold {ref:g}" if short else f"Holding {ref:g} F"
    return (ARM_SHORT if short else ARM_LABELS)[key]


SCHEMA_VERSION = 1


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


# ── Live day accumulation (section 3.1) ──────────────────────────────


class _LiveDay:
    """One local day being accumulated from coordinator cycles."""

    def __init__(self, local_date: date, tz: tzinfo) -> None:
        midnight = datetime(local_date.year, local_date.month, local_date.day, tzinfo=tz)
        nxt = local_date + timedelta(days=1)
        next_midnight = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
        self.date = local_date.isoformat()
        self.start = midnight.astimezone(UTC)
        self.hours = max(1, round((next_midnight.astimezone(UTC) - self.start).total_seconds() / 3600))
        n = self.hours
        self.out_sum = [0.0] * n
        self.out_n = [0] * n
        self.in_sum = [0.0] * n
        self.in_n = [0] * n
        self.cloud_sum = [0.0] * n
        self.cloud_n = [0] * n
        self.sun_up: list[bool | None] = [None] * n
        self.sun_dist = [99.0] * n
        self.top: list[float | None] = [None] * n
        self.heat = [0.0] * n
        self.cool = [0.0] * n
        self.aux = [0.0] * n
        self.setpoints: list[list] = []
        self.plan: list[list] = []
        self.indoor_start: float | None = None
        self.covered = 0.0
        self.controlled = 0.0
        self.metered_wh = 0.0
        self.metered_any = False
        # Compressor-on, aux-off cycles with a real power reading (3.4).
        self.watt = {"heat_wh": 0.0, "heat_h": 0.0, "cool_wh": 0.0, "cool_h": 0.0}
        self.aux_runs: list[list] = []
        self.run: list | None = None  # [start, end, out_sum, out_n, comp_before]

    def add(
        self,
        now: datetime,
        dt: float,
        *,
        indoor: float | None,
        outdoor: float | None,
        mode: str,
        heat_sp: float | None,
        cool_sp: float | None,
        action: str | None,
        aux_active: bool,
        power_watts: float | None,
        power_measured: bool,
        controlled: bool,
        cloud: float | None,
        sun_elevation: float | None,
        plan: tuple | None,
        heat_run_min: float,
    ) -> None:
        minute = (now - self.start).total_seconds() / 60.0
        h = int(minute // 60)
        if h < 0 or h >= self.hours:
            return
        self.covered += dt
        if controlled:
            self.controlled += dt
        if indoor is not None:
            if self.indoor_start is None:
                self.indoor_start = indoor
            self.in_sum[h] += indoor
            self.in_n[h] += 1
            if self.top[h] is None and minute - h * 60 < STEP_MINUTES:
                self.top[h] = indoor
        if outdoor is not None:
            self.out_sum[h] += outdoor
            self.out_n[h] += 1
        if cloud is not None:
            self.cloud_sum[h] += cloud
            self.cloud_n[h] += 1
        if sun_elevation is not None:
            dist = abs(minute - h * 60 - 30)
            if dist < self.sun_dist[h]:
                self.sun_dist[h] = dist
                self.sun_up[h] = sun_elevation > 0

        heating = action == "heating" or (aux_active and action != "cooling")
        if heating:
            self.heat[h] += dt
        elif action == "cooling":
            self.cool[h] += dt
        if aux_active:
            self.aux[h] += dt
            if self.run is not None and minute - self.run[1] <= dt + 1e-6:
                self.run[1] = minute + dt
            else:
                self._close_run()
                self.run = [minute, minute + dt, 0.0, 0, heat_run_min]
            if outdoor is not None:
                self.run[2] += outdoor
                self.run[3] += 1
        else:
            self._close_run()

        if power_measured and power_watts is not None:
            self.metered_any = True
            self.metered_wh += power_watts * dt / 60.0
            if not aux_active and action in ("heating", "cooling"):
                key = "heat" if action == "heating" else "cool"
                self.watt[f"{key}_wh"] += power_watts * dt / 60.0
                self.watt[f"{key}_h"] += dt / 60.0

        single = cool_sp if mode == "cool" else heat_sp
        m, hsp, csp = setpoint_row(mode, single, heat_sp, cool_sp)
        append_row(self.setpoints, int(round(minute)), m, hsp, csp)
        if plan is not None:
            pm, phsp, pcsp = plan
            append_row(self.plan, int(round(minute)), pm, phsp, pcsp)

    def _close_run(self) -> None:
        if self.run is None:
            return
        start, end, osum, on, comp_before = self.run
        self.aux_runs.append(
            [int(round(start)), int(round(end)), round(osum / on, 2) if on else None, round(comp_before, 1)]
        )
        self.run = None

    def finish(self, hours: int | None = None) -> DayInputs:
        """The day as ``DayInputs``; ``hours`` truncates it (today so far)."""
        runs = list(self.aux_runs)
        if self.run is not None:
            start, end, osum, on, comp_before = self.run
            runs.append(
                [int(round(start)), int(round(end)), round(osum / on, 2) if on else None, round(comp_before, 1)]
            )
        n = self.hours if hours is None else max(1, min(hours, self.hours))
        minutes = n * 60
        return DayInputs(
            date=self.date,
            start_utc=self.start.isoformat(),
            hours=n,
            outdoor=[round(self.out_sum[h] / self.out_n[h], 2) if self.out_n[h] else None for h in range(n)],
            cloud=[round(self.cloud_sum[h] / self.cloud_n[h], 3) if self.cloud_n[h] else None for h in range(n)],
            sun_up=self.sun_up[:n],
            indoor_start=self.indoor_start,
            indoor=[round(self.in_sum[h] / self.in_n[h], 2) if self.in_n[h] else None for h in range(n)],
            setpoints=[list(r) for r in self.setpoints if r[0] < minutes] or [list(r) for r in self.setpoints[:1]],
            plan=[list(r) for r in self.plan if r[0] < minutes],
            heat_min=[round(v, 2) for v in self.heat[:n]],
            cool_min=[round(v, 2) for v in self.cool[:n]],
            aux_min=[round(v, 2) for v in self.aux[:n]],
            aux_runs=[r for r in runs if r[0] < minutes],
            controlled=round(self.controlled / self.covered, 3) if self.covered else 0.0,
            covered_min=round(self.covered, 1),
            metered_kwh=round(self.metered_wh / 1000.0, 3) if self.metered_any else None,
            source="live",
            indoor_top=self.top[:n],
        )

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("start",)}
        d["start"] = self.start.isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict, tz: tzinfo) -> _LiveDay:
        out = cls(date.fromisoformat(data["date"]), tz)
        for k, v in data.items():
            if k in ("date", "start", "hours"):
                continue
            if hasattr(out, k):
                setattr(out, k, v)
        return out


# ── Per-day record helpers ───────────────────────────────────────────


def _watts_of(rec: dict) -> Watts:
    w = rec.get("watts") or {}
    return Watts(
        float(w.get("heat", 3500.0)),
        float(w.get("cool", 3500.0)),
        float(w.get("aux", 10000.0)),
        w.get("heat_source", "rated"),
        w.get("cool_source", "rated"),
        w.get("aux_source", "assumed"),
    )


def actual_e(rec: dict, watts: Watts | None = None) -> float:
    a = rec["actual"]
    return energy_kwh(a["heat_h"], a["cool_h"], a["aux_h"], watts or _watts_of(rec))


def arm_e(rec: dict, arm: str, watts: Watts | None = None) -> float | None:
    r = rec.get("arms", {}).get(arm)
    if r is None:
        return None
    return energy_kwh(r["heat_h"], r["cool_h"], r["aux_h"], watts or _watts_of(rec))


def is_valid(rec: dict) -> bool:
    """Section 5 valid day."""
    if not rec.get("simulated") or rec.get("warmup"):
        return False
    a = rec["actual"]
    return a.get("covered", 0.0) >= VALID_COVERED_MIN and a.get("out_frac", 0.0) >= VALID_OUTDOOR_FRAC


def is_heating_day(rec: dict) -> bool:
    """Heating day: more heating than cooling runtime (section 5.3). A day
    with no runtime at all counts as heating when it averaged below 60 F."""
    a = rec["actual"]
    heat = a["heat_h"] + a["aux_h"]
    if heat == 0 and a["cool_h"] == 0:
        return (a.get("out") if a.get("out") is not None else 50.0) < 60.0
    return heat > a["cool_h"]


def heating_season(recs: list[dict]) -> bool:
    """Whether most of these days are heating days (ties go to heating)."""
    return sum(is_heating_day(r) for r in recs) * 2 >= len(recs) if recs else True


def grade(
    n: int, ea: float, g: float | None, w: float | None, comp: float | None = None, aux: float | None = None
) -> str:
    """Section 5.1 grade, with the compressor and aux gain conditions."""
    if n < GRADE_MIN_DAYS:
        return "warming_up"
    if ea < GRADE_IDLE_KWH or g is None or w is None:
        return "idle"
    gains = [x for x in (g, comp, aux) if x is not None]
    if all(abs(x - 1) <= GRADE_GOOD_GAIN for x in gains) and w <= GRADE_GOOD_ERROR:
        return "good"
    if all(abs(x - 1) <= GRADE_FAIR_GAIN for x in gains) and w <= GRADE_FAIR_ERROR:
        return "fair"
    return "poor"


def accuracy(recs: list[dict]) -> dict:
    """Section 5.1 over a list of valid day records."""
    ea = er = err = 0.0
    comp_a = comp_r = aux_a = aux_r = 0.0
    mae_sum = 0.0
    mae_n = 0
    for rec in recs:
        a = actual_e(rec)
        r = arm_e(rec, "replay") or 0.0
        ea += a
        er += r
        err += abs(r - a)
        act = rec["actual"]
        rep = rec["arms"]["replay"]
        comp_a += act["heat_h"] + act["cool_h"]
        comp_r += rep["heat_h"] + rep["cool_h"]
        aux_a += act["aux_h"]
        aux_r += rep["aux_h"]
        s, n = act.get("mae", [0.0, 0])
        mae_sum += s
        mae_n += n
    g = er / ea if ea > 0 else None
    w = err / ea if ea > 0 else None
    comp = comp_r / comp_a if comp_a > 0 else None
    aux = aux_r / aux_a if aux_a >= AUX_GAIN_MIN_H else None
    return {
        "days": len(recs),
        "e_actual": ea,
        "e_replay": er,
        "gain": g,
        "daily_error": w,
        "comp_gain": comp,
        "aux_gain": aux,
        "indoor_mae": mae_sum / mae_n if mae_n else None,
        "grade": grade(len(recs), ea, g, w, comp, aux),
    }


def _band(g: float | None, k: float | None) -> float:
    b = max(BAND_MIN, abs(g - 1) if g is not None else 0.0, abs(k - 1) if k is not None else 0.0)
    return min(b, BAND_MAX)


def _gain_clamped(g: float | None) -> float:
    if g is None:
        return 1.0
    return max(GAIN_CLAMP[0], min(GAIN_CLAMP[1], g))


# ── The ledger ───────────────────────────────────────────────────────


class TwinLedger:
    """Accumulates days, runs the twin at day close, computes the statistics."""

    def __init__(self, tz: tzinfo | str, lat: float = 0.0, lon: float = 0.0) -> None:
        self.tz = ZoneInfo(tz) if isinstance(tz, str) else tz
        self.inputs: dict[str, DayInputs] = {}
        self.records: dict[str, dict] = {}
        self.fit = TwinFit(lat, lon)
        self._open: _LiveDay | None = None
        self._last_sample: datetime | None = None
        self._heat_run_min = 0.0
        self._today_cache: tuple[str, dict] | None = None
        # Metered compressor watt sums of days finished but not yet closed.
        self._pending_watts: dict[str, dict[str, float]] = {}

    def set_location(self, lat: float, lon: float) -> None:
        self.fit.lat = float(lat)
        self.fit.lon = float(lon)

    # -- per-cycle feed ----------------------------------------------------

    def record_cycle(
        self,
        now: datetime,
        *,
        indoor_temp: float | None,
        outdoor_temp: float | None,
        hvac_mode: str | None,
        target_temp: float | None = None,
        target_low: float | None = None,
        target_high: float | None = None,
        hvac_action: str | None = None,
        aux_active: bool = False,
        power_watts: float | None = None,
        power_measured: bool = False,
        controlled: bool = False,
        cloud: float | None = None,
        sun_elevation: float | None = None,
        plan: tuple | None = None,
    ) -> tuple[DayInputs, float | None] | None:
        """Feed one coordinator cycle.

        Returns ``(finished_day, next_top)`` when this cycle starts a new
        local day: the previous day's inputs, and this sample's indoor
        temperature when it sits in the new day's first 5 minutes (the fit's
        last-hour end point). Otherwise None.
        """
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        local_day = now.astimezone(self.tz).date()
        finished: DayInputs | None = None
        if self._open is not None and self._open.date != local_day.isoformat():
            finished = self._open.finish()
            self._pending_watts[finished.date] = dict(self._open.watt)
            self._open = None
        if self._open is None:
            self._open = _LiveDay(local_day, self.tz)

        if self._last_sample is None:
            dt = float(STEP_MINUTES)
        else:
            dt = (now - self._last_sample).total_seconds() / 60.0
            if dt <= 0:
                return None
            dt = min(dt, CYCLE_MAX_GAP_MIN)
        self._last_sample = now

        mode = hvac_mode if hvac_mode in ("heat", "cool", "heat_cool", "off") else "off"
        if mode == "heat_cool":
            heat_sp, cool_sp = target_low, target_high
        elif mode == "cool":
            heat_sp, cool_sp = None, target_temp
        else:
            heat_sp, cool_sp = target_temp, None
        self._open.add(
            now,
            dt,
            indoor=indoor_temp,
            outdoor=outdoor_temp,
            mode=mode,
            heat_sp=heat_sp,
            cool_sp=cool_sp,
            action=hvac_action,
            aux_active=bool(aux_active),
            power_watts=power_watts,
            power_measured=bool(power_measured),
            controlled=bool(controlled),
            cloud=cloud,
            sun_elevation=sun_elevation,
            plan=plan,
            heat_run_min=self._heat_run_min,
        )
        self._heat_run_min = self._heat_run_min + dt if hvac_action == "heating" else 0.0
        if finished is None:
            return None
        minute = (now - self._open.start).total_seconds() / 60.0
        next_top = indoor_temp if minute < STEP_MINUTES else None
        return finished, next_top

    # -- day close ---------------------------------------------------------

    def _measured_watt_sums(self, before: str) -> dict[str, float]:
        cutoff = date.fromisoformat(before) - timedelta(days=WATTS_WINDOW_DAYS)
        sums = {"heat_wh": 0.0, "heat_h": 0.0, "cool_wh": 0.0, "cool_h": 0.0}
        for d, rec in self.records.items():
            if cutoff <= date.fromisoformat(d) <= date.fromisoformat(before):
                for k, v in (rec.get("metered") or {}).items():
                    if k in sums:
                        sums[k] += v
        return sums

    def close_day(
        self,
        inputs: DayInputs,
        *,
        base_model: Any = None,
        next_top: float | None = None,
        rated_heat: float = 3500.0,
        rated_cool: float = 3500.0,
        aux_kw: float | None = None,
        ref_heat: float = DEFAULT_REFERENCE_HEAT_F,
        ref_cool: float = DEFAULT_REFERENCE_COOL_F,
        aux_threshold: float | None = None,
        watt_sums: dict[str, float] | None = None,
        arms: tuple[str, ...] | None = None,
    ) -> dict:
        """Fold a finished day into the fit, simulate its arms, store a record."""
        prev = self.inputs.get((date.fromisoformat(inputs.date) - timedelta(days=1)).isoformat())
        self.inputs[inputs.date] = inputs
        self.fit.add_day(inputs, prev, next_top)
        if watt_sums is None:
            watt_sums = self._pending_watts.pop(inputs.date, None)
        sums = self._measured_watt_sums(inputs.date)
        if watt_sums:
            for k, v in watt_sums.items():
                sums[k] = sums.get(k, 0.0) + v
        watts = resolve_watts(
            rated_heat=rated_heat,
            rated_cool=rated_cool,
            measured_heat_kwh=sums["heat_wh"] / 1000.0,
            measured_heat_h=sums["heat_h"],
            measured_cool_kwh=sums["cool_wh"] / 1000.0,
            measured_cool_h=sums["cool_h"],
            aux_kw_configured=aux_kw,
            model_aux_watts=getattr(base_model, "aux_power_watts", 10000.0) if base_model is not None else 10000.0,
        )
        typical = build_typical_schedule(self.inputs.values(), inputs.date, self.tz)
        rec = self._simulate(inputs, prev, base_model, watts, ref_heat, ref_cool, aux_threshold, typical, arms)
        rec["metered"] = {k: round(v, 3) for k, v in (watt_sums or {}).items()}
        self.records[inputs.date] = rec
        self._today_cache = None
        self.prune()
        return rec

    def _simulate(
        self,
        inputs: DayInputs,
        prev: DayInputs | None,
        base_model: Any,
        watts: Watts,
        ref_heat: float,
        ref_cool: float,
        aux_threshold: float | None,
        typical: dict | None,
        arms: tuple[str, ...] | None,
    ) -> dict:
        out_frac = sum(1 for v in inputs.outdoor if v is not None) / inputs.hours if inputs.hours else 0.0
        aux_classes = classify_aux(inputs, aux_threshold)
        aux_bins: dict[str, float] = defaultdict(float)
        for run in inputs.aux_runs:
            if run[2] is not None:
                b = int(math.floor(float(run[2]) / DEGREE_BIN_F) * DEGREE_BIN_F)
                aux_bins[str(b)] += max(0.0, float(run[1]) - float(run[0])) / 60.0
        rec: dict[str, Any] = {
            "date": inputs.date,
            "source": inputs.source,
            "simulated": False,
            "warmup": False,
            "actual": {
                "heat_h": round(inputs.heat_h, 3),
                "cool_h": round(inputs.cool_h, 3),
                "aux_h": round(inputs.aux_h, 3),
                "out": _r(inputs.outdoor_mean),
                "in": _r(inputs.indoor_mean),
                "controlled": inputs.controlled,
                "covered": inputs.covered_min,
                "out_frac": round(out_frac, 3),
                "metered_kwh": inputs.metered_kwh,
                "mae": [0.0, 0],
            },
            "arms": {},
            "aux": {k: round(v, 3) for k, v in aux_classes.items()},
            "aux_bins": {k: round(v, 3) for k, v in aux_bins.items()},
            "watts": {
                "heat": round(watts.heat, 1),
                "cool": round(watts.cool, 1),
                "aux": round(watts.aux, 1),
                "heat_source": watts.heat_source,
                "cool_source": watts.cool_source,
                "aux_source": watts.aux_source,
            },
            "ref": [ref_heat, ref_cool],
        }
        model = self.fit.model_for(inputs, base_model)
        if model is None or not can_simulate(inputs) or inputs.indoor_start is None:
            return rec
        prev_date = (date.fromisoformat(inputs.date) - timedelta(days=1)).isoformat()
        prev_rec = self.records.get(prev_date)
        prev_ends = {a: r["end"] for a, r in prev_rec["arms"].items()} if prev_rec and prev_rec.get("simulated") else {}
        continuing = "replay" in prev_ends
        rec["warmup"] = not continuing
        sun = self.fit.sun_for(inputs, prev)
        schedules = arm_schedules(inputs, ref_heat=ref_heat, ref_cool=ref_cool, typical=typical, tz=self.tz)
        for arm, sched in schedules.items():
            if arms is not None and arm not in arms:
                continue
            start = prev_ends.get(arm, prev_ends.get("replay")) if continuing else None
            res = run_scenario(model, inputs, sched, inputs.indoor_start if start is None else start, sun)
            if res is None:
                continue
            rec["arms"][arm] = _arm_summary(res)
            if arm == "replay":
                rec["replay_hourly"] = {
                    "heat": [round(v, 1) for v in res.heat_min_hourly],
                    "cool": [round(v, 1) for v in res.cool_min_hourly],
                }
                mae_sum, mae_n = 0.0, 0
                for act, sim in zip(inputs.indoor, res.indoor_hourly, strict=False):
                    if act is not None:
                        mae_sum += abs(sim - act)
                        mae_n += 1
                rec["actual"]["mae"] = [round(mae_sum, 3), mae_n]
        rec["simulated"] = "replay" in rec["arms"]
        return rec

    def add_imported(
        self,
        days: list[DayInputs],
        *,
        base_model: Any = None,
        rated_heat: float = 3500.0,
        rated_cool: float = 3500.0,
        aux_kw: float | None = None,
        ref_heat: float = DEFAULT_REFERENCE_HEAT_F,
        ref_cool: float = DEFAULT_REFERENCE_COOL_F,
        aux_threshold: float | None = None,
    ) -> dict[str, int]:
        """Merge imported days (section 7). Never replaces a live or recorder day."""
        new = sorted((d for d in days if d.date not in self.records), key=lambda d: d.date)
        skipped = len(days) - len(new)
        self.fit.add_days(new)
        watts = resolve_watts(
            rated_heat=rated_heat,
            rated_cool=rated_cool,
            aux_kw_configured=aux_kw,
            model_aux_watts=getattr(base_model, "aux_power_watts", 10000.0) if base_model is not None else 10000.0,
        )
        simulated = 0
        prev: DayInputs | None = None
        for d in new:
            link = (
                prev
                if prev is not None and (date.fromisoformat(d.date) - date.fromisoformat(prev.date)).days == 1
                else None
            )
            rec = self._simulate(
                d, link, base_model, watts, ref_heat, ref_cool, aux_threshold, None, ("replay", "reference", "warmer")
            )
            rec["metered"] = {}
            self.records[d.date] = rec
            simulated += bool(rec["simulated"])
            prev = d
        self._today_cache = None
        self.prune()
        return {"read": len(days), "merged": len(new), "skipped": skipped, "simulated": simulated}

    def prune(self, today: str | None = None) -> None:
        if not self.records and not self.inputs:
            return
        latest = today or max([*self.records, *self.inputs])
        t = date.fromisoformat(latest)
        for d in list(self.inputs):
            if (t - date.fromisoformat(d)).days > INPUTS_KEEP_DAYS:
                del self.inputs[d]
        for d in list(self.records):
            age = (t - date.fromisoformat(d)).days
            if age > RESULTS_KEEP_DAYS:
                del self.records[d]
            elif age > INPUTS_KEEP_DAYS:
                self.records[d].pop("replay_hourly", None)

    def resimulate_reference(self, base_model: Any, ref_heat: float, ref_cool: float) -> int:
        """Section 4.4: rerun the reference arm where inputs remain; flag the rest."""
        n = 0
        for d, rec in sorted(self.records.items()):
            if rec.get("ref") == [ref_heat, ref_cool]:
                continue
            inputs = self.inputs.get(d)
            if inputs is None or not rec.get("simulated"):
                rec["stale_reference"] = True
                continue
            prev = self.inputs.get((date.fromisoformat(d) - timedelta(days=1)).isoformat())
            prev_rec = self.records.get((date.fromisoformat(d) - timedelta(days=1)).isoformat())
            model = self.fit.model_for(inputs, base_model)
            if model is None:
                rec["stale_reference"] = True
                continue
            start = None
            if prev_rec and prev_rec.get("simulated"):
                start = (prev_rec["arms"].get("reference") or prev_rec["arms"].get("replay") or {}).get("end")
            sched = arm_schedules(inputs, ref_heat=ref_heat, ref_cool=ref_cool).get("reference")
            res = run_scenario(
                model, inputs, sched, inputs.indoor_start if start is None else start, self.fit.sun_for(inputs, prev)
            )
            if res is not None:
                rec["arms"]["reference"] = _arm_summary(res)
                rec["ref"] = [ref_heat, ref_cool]
                rec.pop("stale_reference", None)
                n += 1
        return n

    # -- windows -----------------------------------------------------------

    def valid_records(self, include_import: bool = False) -> list[dict]:
        return [
            r
            for _, r in sorted(self.records.items())
            if is_valid(r) and (include_import or r.get("source") != "import")
        ]

    def window(self, n: int = WINDOW_DAYS) -> list[dict]:
        return self.valid_records()[-n:]

    def accuracy(self) -> dict:
        return accuracy(self.window())

    # -- section 5.2 -------------------------------------------------------

    def arm_stats(self, acc: dict | None = None, k: float | None = None) -> dict[str, dict]:
        acc = acc or self.accuracy()
        g = acc["gain"]
        gc = _gain_clamped(g)
        trusted = acc["grade"] in TRUSTED_GRADES
        win = self.window()
        out: dict[str, dict] = {}
        for arm in ARM_ORDER:
            recs = [r for r in win if arm in r["arms"]]
            if not recs:
                continue
            delta = est = 0.0
            aux_h = indoor = off = 0.0
            e_arm = 0.0
            for r in recs:
                ea = actual_e(r)
                es = arm_e(r, arm)
                er = arm_e(r, "replay")
                d = (es - er) / gc
                delta += d
                est += max(0.0, ea + d)
                e_arm += es
                s = r["arms"][arm]
                aux_h += s["aux_h"]
                indoor += s["in"]
                off += s["off_h"]
            n = len(recs)
            out[arm] = {
                "days": n,
                "delta_kwh": delta if trusted else None,
                "est_kwh": est if trusted else None,
                "kwh_day": (est / n) if trusted else None,
                "raw_kwh_day": e_arm / n,
                "aux_h": aux_h / n,
                "indoor_mean": indoor / n,
                "off_setpoint_h": off / n,
                "band": _band(g, k),
            }
        return out

    # -- section 5.3 -------------------------------------------------------

    def degree(self, today_outdoor: float | None, acc: dict | None = None) -> dict:
        acc = acc or self.accuracy()
        gc = _gain_clamped(acc["gain"])
        valid = self.valid_records()
        heating = heating_season(valid[-WINDOW_DAYS:])
        same = [r for r in valid if is_heating_day(r) == heating][-DEGREE_FIT_DAYS:]
        rows = [{"out": r["actual"]["out"], "e": actual_e(r)} for r in same if r["actual"]["out"] is not None]
        fit = fit_runtime_to_weather(
            rows,
            value=lambda d: d["e"],
            balance_range=HEAT_BALANCE_RANGE if heating else COOL_BALANCE_RANGE,
            cooling=not heating,
            active_threshold=0.5,
        )

        def c_reg(t_out: float) -> float | None:
            if fit is None:
                return None
            b, slope = fit["balance"], fit["slope"]
            if heating:
                return slope * (max(0.0, b + 1 - t_out) - max(0.0, b - t_out))
            return slope * (max(0.0, t_out - (b - 1)) - max(0.0, t_out - b))

        twin_days = []
        for r in same:
            if "warmer" not in r["arms"] or r["actual"]["out"] is None:
                continue
            c = (arm_e(r, "warmer") - arm_e(r, "replay")) / gc
            twin_days.append((r["actual"]["out"], c))
        both = [(c, c_reg(t)) for t, c in twin_days if c > 0 and (c_reg(t) or 0) > 0]
        k = None
        if fit is not None and fit["r2"] >= K_MIN_R2 and len(both) >= K_MIN_DAYS:
            k = sum(c for c, _ in both) / sum(cr for _, cr in both)
        bins: dict[int, list[float]] = defaultdict(list)
        for t, c in twin_days:
            bins[int(math.floor(t / DEGREE_BIN_F) * DEGREE_BIN_F)].append(c)
        bin_rows = [
            {"bin": b, "kwh": round(sum(v) / len(v), 3), "n": len(v)}
            for b, v in sorted(bins.items())
            if len(v) >= DEGREE_MIN_BIN_DAYS
        ]
        today_kwh = None
        source = None
        trusted = acc["grade"] in TRUSTED_GRADES
        if today_outdoor is not None:
            b = int(math.floor(today_outdoor / DEGREE_BIN_F) * DEGREE_BIN_F)
            hit = next((row for row in bin_rows if row["bin"] == b), None)
            if hit is not None and trusted:
                today_kwh, source = hit["kwh"], "twin"
            elif c_reg(today_outdoor) is not None:
                today_kwh, source = c_reg(today_outdoor), "regression"
        return {
            "season": "heat" if heating else "cool",
            "today_kwh": _r(today_kwh, 3),
            "source": source,
            "k": _r(k, 3),
            "bins": bin_rows if trusted else [],
            "fit": None
            if fit is None
            else {"balance": fit["balance"], "slope": fit["slope"], "intercept": fit["intercept"], "r2": fit["r2"]},
        }

    # -- section 5.4 -------------------------------------------------------

    def aux_stats(self, today: str | None = None) -> dict:
        win = self.window()
        valid = self.valid_records()
        t = date.fromisoformat(today) if today else (date.fromisoformat(valid[-1]["date"]) if valid else date.today())
        season_start = date(t.year if t.month >= SEASON_START_MONTH else t.year - 1, SEASON_START_MONTH, 1)
        season = [r for r in valid if date.fromisoformat(r["date"]) >= season_start]

        def block(recs: list[dict]) -> dict:
            classes = {"recovery": 0.0, "cold": 0.0, "other": 0.0}
            kwh = dict.fromkeys(classes, 0.0)
            aux_kwh = e_total = 0.0
            bins: dict[int, float] = defaultdict(float)
            days_with = 0
            for r in recs:
                w = _watts_of(r)
                for c in classes:
                    h = r.get("aux", {}).get(c, 0.0)
                    classes[c] += h
                    kwh[c] += h * w.aux / 1000.0
                aux_kwh += r["actual"]["aux_h"] * w.aux / 1000.0
                e_total += actual_e(r)
                for b, h in (r.get("aux_bins") or {}).items():
                    bins[int(b)] += h
                days_with += r["actual"]["aux_h"] > 0
            return {
                "classes": {c: round(v, 2) for c, v in classes.items()},
                "kwh": {c: round(v, 1) for c, v in kwh.items()},
                "share": round(aux_kwh / e_total, 3) if e_total > 0 else None,
                "bins": [{"bin": b, "hours": round(h, 2)} for b, h in sorted(bins.items())],
                "days_with_aux": days_with,
            }

        replay_aux = sum(r["arms"]["replay"]["aux_h"] for r in win)
        actual_aux = sum(r["actual"]["aux_h"] for r in win)
        plan_recs = [r for r in win if "plan" in r["arms"]]
        assumed = any((r.get("watts") or {}).get("aux_source") == "assumed" for r in win)
        return {
            "window": block(win),
            "season": block(season),
            "kwh_label": "assumed" if assumed else "configured",
            "days": [
                {"d": r["date"], **{c: round(r.get("aux", {}).get(c, 0.0), 2) for c in ("recovery", "cold", "other")}}
                for r in win
            ],
            "twin": {
                "replay_h": round(replay_aux, 2),
                "actual_h": round(actual_aux, 2),
                "plan_h": round(sum(r["arms"]["plan"]["aux_h"] for r in plan_recs), 2) if plan_recs else None,
                "plan_replay_h": round(sum(r["arms"]["replay"]["aux_h"] for r in plan_recs), 2) if plan_recs else None,
            },
        }

    # -- section 5.5 -------------------------------------------------------

    def year_over_year(self, watts: Watts) -> list[dict]:
        """Same weather, last year: one row per month with enough data."""
        all_valid = self.valid_records(include_import=True)
        daily = [
            {
                "date": date.fromisoformat(r["date"]),
                "out": r["actual"]["out"],
                "in": r["actual"]["in"],
                "e": energy_kwh(r["actual"]["heat_h"], r["actual"]["cool_h"], r["actual"]["aux_h"], watts),
                "heating": is_heating_day(r),
            }
            for r in all_valid
            if r["actual"]["out"] is not None and r["actual"]["in"] is not None
        ]
        if not daily:
            return []
        latest = max(d["date"] for d in daily)
        months: dict[str, list[dict]] = defaultdict(list)
        for d in daily:
            if (latest - d["date"]).days < 365:
                months[d["date"].strftime("%Y-%m")].append(d)
        rows = []
        for month, cur in sorted(months.items()):
            if len(cur) < YOY_MIN_DAYS:
                continue
            heating = sum(d["heating"] for d in cur) * 2 >= len(cur)
            mid = sorted(d["date"] for d in cur)[len(cur) // 2]
            prior = [
                d
                for d in daily
                if YOY_PRIOR_MIN_AGE <= (mid - d["date"]).days <= YOY_PRIOR_MAX_AGE and d["heating"] == heating
            ]
            if len(prior) < YOY_MIN_PRIOR:
                continue
            row = _yoy_month(month, cur, prior, heating)
            if row is not None:
                rows.append(row)
        return rows

    # -- today so far ------------------------------------------------------

    def today(self, now: datetime, base_model: Any = None, watts: Watts | None = None) -> dict | None:
        """Arms on the partial day from yesterday's end temperatures (cached per hour)."""
        if self._open is None:
            return None
        local = now.astimezone(self.tz)
        key = local.strftime("%Y-%m-%dT%H")
        if self._today_cache and self._today_cache[0] == key:
            return self._today_cache[1]
        elapsed = int((now - self._open.start).total_seconds() // 3600)
        if elapsed < 1:
            return None
        partial = self._open.finish(hours=elapsed)
        prev = self.inputs.get((date.fromisoformat(partial.date) - timedelta(days=1)).isoformat())
        rec = self._simulate(
            partial,
            prev,
            base_model,
            watts or Watts(3500.0, 3500.0, 10000.0),
            DEFAULT_REFERENCE_HEAT_F,
            DEFAULT_REFERENCE_COOL_F,
            None,
            build_typical_schedule(self.inputs.values(), partial.date, self.tz),
            None,
        )
        out = None
        if rec["simulated"]:
            out = {"elapsed_h": elapsed, "record": rec}
        self._today_cache = (key, out)
        return out

    # -- payload (section 8) -----------------------------------------------

    def payload(
        self,
        now: datetime,
        *,
        rate: float | None = None,
        today_outdoor: float | None = None,
        base_model: Any = None,
        ref_heat: float = DEFAULT_REFERENCE_HEAT_F,
        ref_cool: float = DEFAULT_REFERENCE_COOL_F,
    ) -> dict:
        """Everything the Twin tab and the twin sensors read."""
        acc = self.accuracy()
        trusted = acc["grade"] in TRUSTED_GRADES
        deg = self.degree(today_outdoor, acc)
        k = deg["k"]
        arms = self.arm_stats(acc, k)
        band = _band(acc["gain"], k)
        win = self.window()
        watts = _watts_of(win[-1]) if win else Watts(3500.0, 3500.0, 10000.0)

        def cost(kwh: float | None) -> float | None:
            return None if kwh is None or rate is None else round(kwh * rate, 2)

        ref = arms.get("reference")
        saved = ref["delta_kwh"] if ref else None
        pct = (saved / ref["est_kwh"]) if ref and saved is not None and ref["est_kwh"] else None
        effect = None
        if "plan" in arms:
            d = arms["plan"]["delta_kwh"]
            effect = {"key": "plan", "kwh": None if d is None else -d, "days": arms["plan"]["days"]}
        elif "schedule" in arms:
            d = arms["schedule"]["delta_kwh"]
            effect = {"key": "schedule", "kwh": d, "days": arms["schedule"]["days"]}
        if effect is not None:
            effect["cost"] = cost(effect["kwh"])
            effect["band"] = band
            effect["kwh"] = _r(effect["kwh"], 1)

        cooling = not heating_season(win)
        arm_rows = []
        for key in ARM_ORDER:
            st = arms.get(key)
            if st is None:
                continue
            arm_rows.append(
                {
                    "key": key,
                    "label": _arm_label(key, cooling, ref_heat, ref_cool),
                    "short": _arm_label(key, cooling, ref_heat, ref_cool, short=True),
                    "kwh_day": _r(st["kwh_day"], 2),
                    "delta_kwh": _r(st["delta_kwh"], 1) if key != "replay" else None,
                    "delta_cost": cost(st["delta_kwh"]) if key != "replay" else None,
                    "aux_h": _r(st["aux_h"], 2),
                    "indoor_mean": _r(st["indoor_mean"], 1),
                    "off_setpoint_h": _r(st["off_setpoint_h"], 2),
                    "days": st["days"],
                }
            )
        if win:
            n = len(win)
            arm_rows.append(
                {
                    "key": "actual",
                    "label": "Actual",
                    "short": "Actual",
                    "kwh_day": _r(sum(actual_e(r) for r in win) / n, 2),
                    "delta_kwh": None,
                    "delta_cost": None,
                    "aux_h": _r(sum(r["actual"]["aux_h"] for r in win) / n, 2),
                    "indoor_mean": _r(sum(r["actual"]["in"] or 0 for r in win) / n, 1),
                    "off_setpoint_h": None,
                    "days": n,
                }
            )

        imported = [r for r in self.valid_records(include_import=True) if r.get("source") == "import"]
        imp = accuracy(imported) if imported else None

        bins: dict[int, list] = defaultdict(lambda: [0.0, 0.0, 0])
        for r in win:
            if r["actual"]["out"] is None:
                continue
            b = int(math.floor(r["actual"]["out"] / 10) * 10)
            bins[b][0] += actual_e(r)
            bins[b][1] += arm_e(r, "replay")
            bins[b][2] += 1
        by_outdoor = [
            {"bin": b, "error": round((v[1] - v[0]) / v[0], 3) if v[0] > 0 else None, "n": v[2]}
            for b, v in sorted(bins.items())
        ]

        key = "heat" if heating_season(win) else "cool"
        act_h = [0.0] * 24
        rep_h = [0.0] * 24
        n_h = 0
        for r in win:
            inp = self.inputs.get(r["date"])
            hourly = r.get("replay_hourly")
            if inp is None or hourly is None or inp.hours != 24:
                continue
            src = inp.heat_min if key == "heat" else inp.cool_min
            for h in range(24):
                act_h[h] += src[h]
                rep_h[h] += hourly[key][h]
            n_h += 1
        by_hour = (
            [{"hour": h, "actual": round(act_h[h] / n_h, 1), "replay": round(rep_h[h] / n_h, 1)} for h in range(24)]
            if n_h
            else []
        )

        daily = []
        for d, r in sorted(self.records.items())[-DAILY_DAYS:]:
            daily.append(
                {
                    "d": d,
                    "actual": round(actual_e(r), 2),
                    "replay": _r(arm_e(r, "replay"), 2),
                    "out": r["actual"]["out"],
                    "controlled": r["actual"]["controlled"],
                    "warmup": bool(r.get("warmup")),
                    "valid": is_valid(r),
                    "source": r.get("source"),
                }
            )

        aux = self.aux_stats(now.astimezone(self.tz).date().isoformat())
        today_out = None
        today_sim = self.today(now, base_model, watts) if base_model is not None or self.fit.dates else None
        if today_sim is not None:
            rec = today_sim["record"]
            gc = _gain_clamped(acc["gain"])
            er = arm_e(rec, "replay")
            today_out = {
                "elapsed_h": today_sim["elapsed_h"],
                "actual_kwh": round(actual_e(rec), 2),
                "arms": [
                    {
                        "key": a,
                        "label": _arm_label(a, cooling, ref_heat, ref_cool),
                        "kwh": round(arm_e(rec, a), 2),
                        "delta_kwh": round((arm_e(rec, a) - er) / gc, 2) if trusted and a != "replay" else None,
                    }
                    for a in ARM_ORDER
                    if a in rec["arms"]
                ],
            }

        return {
            "grade": acc["grade"],
            "days": acc["days"],
            "days_needed": GRADE_MIN_DAYS,
            "gain": _r(acc["gain"], 3),
            "daily_error": _r(acc["daily_error"], 3),
            "indoor_mae": _r(acc["indoor_mae"], 2),
            "comp_gain": _r(acc["comp_gain"], 3),
            "aux_gain": _r(acc["aux_gain"], 3),
            "imported": None
            if imp is None
            else {"days": imp["days"], "gain": _r(imp["gain"], 3), "daily_error": _r(imp["daily_error"], 3)},
            "watts": {
                "heat": round(watts.heat),
                "cool": round(watts.cool),
                "aux": round(watts.aux),
                "heat_source": watts.heat_source,
                "cool_source": watts.cool_source,
                "aux_source": watts.aux_source,
            },
            "reference": {
                "heat_f": ref_heat,
                "cool_f": ref_cool,
                "saved_kwh": _r(saved, 1),
                "saved_cost": cost(saved),
                "pct": _r(pct, 3),
                "band": round(band, 3),
                "days": ref["days"] if ref else 0,
            },
            "effect": effect,
            "arms": arm_rows,
            "degree": {**deg, "today_cost": cost(deg["today_kwh"])},
            "aux": aux,
            "yoy": self.year_over_year(watts),
            "daily": daily,
            "by_outdoor": by_outdoor,
            "by_hour": by_hour,
            "by_hour_mode": key,
            "today": today_out,
            "model": self.fit.summary(),
            "rate": rate,
        }

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "inputs": {d: i.to_dict() for d, i in self.inputs.items()},
            "records": self.records,
            "fit": self.fit.to_dict(),
            "open": self._open.to_dict() if self._open is not None else None,
            "last_sample": self._last_sample.isoformat() if self._last_sample else None,
            "heat_run_min": self._heat_run_min,
        }

    @classmethod
    def from_dict(
        cls, data: dict | None, tz: tzinfo | str, lat: float | None = None, lon: float | None = None
    ) -> TwinLedger:
        data = data or {}
        fit = TwinFit.from_dict(data.get("fit") or {}, lat=lat, lon=lon)
        out = cls(tz, fit.lat, fit.lon)
        out.fit = fit
        out.inputs = {d: DayInputs.from_dict(v) for d, v in (data.get("inputs") or {}).items()}
        out.records = {d: dict(v) for d, v in (data.get("records") or {}).items()}
        if data.get("open"):
            try:
                out._open = _LiveDay.from_dict(data["open"], out.tz)
            except (KeyError, ValueError, TypeError):
                out._open = None
        if data.get("last_sample"):
            try:
                out._last_sample = datetime.fromisoformat(data["last_sample"])
            except ValueError:
                out._last_sample = None
        out._heat_run_min = float(data.get("heat_run_min", 0.0))
        return out


def _arm_summary(res: DayResult) -> dict:
    return {
        "heat_h": round(res.heat_h, 3),
        "cool_h": round(res.cool_h, 3),
        "aux_h": round(res.aux_h, 3),
        "in": round(res.indoor_mean, 2),
        "off_h": round(res.off_setpoint_h, 2),
        "end": round(res.end_temp, 3),
    }


def _yoy_month(month: str, cur: list[dict], prior: list[dict], heating: bool) -> dict | None:
    rng = range(45, 71) if heating else range(60, 81)
    fit_a = fit_runtime_to_weather(
        prior,
        value=lambda d: d["e"],
        balance_range=rng,
        cooling=not heating,
        min_days=YOY_MIN_PRIOR,
        active_threshold=0.5,
    )
    if fit_a is None or fit_a["r2"] < YOY_MIN_R2:
        return None

    def drv(d: dict, dd: float) -> float:
        bal = d["in"] - dd
        return max(0.0, bal - d["out"]) if heating else max(0.0, d["out"] - bal)

    fit_b = fit_runtime_to_weather(
        prior,
        value=lambda d: d["e"],
        balance_range=YOY_D_RANGE,
        driver=drv,
        min_days=YOY_MIN_PRIOR,
        active_threshold=0.5,
    )

    def pred_a(d: dict) -> float:
        x = max(0.0, fit_a["balance"] - d["out"]) if heating else max(0.0, d["out"] - fit_a["balance"])
        return max(0.0, fit_a["intercept"] + fit_a["slope"] * x)

    sum_e = sum(d["e"] for d in cur)
    sum_a = sum(pred_a(d) for d in cur)
    if sum_a <= 0:
        return None
    total = sum_e / sum_a - 1
    house = None
    if fit_b is not None:
        sum_b = sum(max(0.0, fit_b["intercept"] + fit_b["slope"] * drv(d, fit_b["balance"])) for d in cur)
        if sum_b > 0:
            house = sum_e / sum_b - 1
    # Band from fit A's residuals on the prior days, autocorrelation-inflated.
    pr = sorted(prior, key=lambda d: d["date"])
    res = [d["e"] - pred_a(d) for d in pr]
    n = len(cur)
    mean = sum(res) / len(res)
    sd = math.sqrt(sum((x - mean) ** 2 for x in res) / max(1, len(res) - 1))
    num = sum((res[i] - mean) * (res[i - 1] - mean) for i in range(1, len(res)))
    den = sum((x - mean) ** 2 for x in res)
    rho = max(0.0, min(0.8, num / den)) if den > 0 else 0.0
    band = 1.64 * sd * math.sqrt(n * (1 + rho) / (1 - rho)) / sum_a
    return {
        "month": month,
        "total": round(total, 4),
        "house": None if house is None else round(house, 4),
        "setpoints": None if house is None else round(total - house, 4),
        "band": round(band, 4),
        "within_noise": abs(total) < band,
        "n": n,
        "r2": fit_a["r2"],
    }


def replay_twin_history(ledger: TwinLedger, series: dict[str, Any]) -> list:
    """Feed recorder series through ``ledger.record_cycle``; return finished days.

    Uses the thermostat's own indoor reading (section 3.1). Days come back
    with ``source = "recorder"`` and no plan rows.
    """
    finished = []
    grid = series["grid"]
    for i, instant in enumerate(grid):
        indoor = series["thermostat_indoor"][i]
        action = series["action"][i]
        if indoor is None and action is None:
            continue
        mode = series["mode"][i]
        res = ledger.record_cycle(
            instant,
            indoor_temp=indoor,
            outdoor_temp=series["outdoor"][i],
            hvac_mode=mode,
            target_temp=series["temperature"][i],
            target_low=series["target_low"][i],
            target_high=series["target_high"][i],
            hvac_action=action,
            aux_active=bool(series["aux"][i]),
        )
        if res is not None:
            day, next_top = res
            day.source = "recorder"
            finished.append((day, next_top))
    return finished
