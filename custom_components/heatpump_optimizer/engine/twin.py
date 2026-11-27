"""Digital twin engine: day inputs, scenario arms, one-day simulation.

Pure Python, no Home Assistant imports (like ``dashboard_archive.py``).
See ``docs/internal/TWIN_STATS_SPEC.md`` for the design of record.

The design rule everything here serves: a simulated house is only ever
compared against another run of the same simulated house. The ``replay``
arm follows the real setpoints and is both the control for every other arm
and the accuracy test (graded against real runtime). Energy comes from
runtime times fixed watt constants that are the same on every side of every
comparison, so a wrong constant cancels.

The model is normally the fitted ``TwinHouseModel`` (``twin_house.py``);
any ``PerformanceModel`` also works. Known limits (stated, not fixed here):

* One thermal node: stored heat appears only through the twin house model's
  delayed sun terms.
* Sun is clear-sky scaled by cloud cover.
* A compressor run that crosses midnight restarts the aux runtime count.
"""

from __future__ import annotations

import csv
import math
import statistics
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from .data_types import ForecastPoint, ScheduleEntry
from .performance_model import PerformanceModel
from .thermal_simulator import ThermalSimulator

STEP_MINUTES = 5
STEPS_PER_HOUR = 60 // STEP_MINUTES

# A day with fewer hourly outdoor readings than this is not simulated.
MIN_OUTDOOR_HOURS = 20

# Minimum covered minutes for an imported day to be kept (20 hours of rows).
MIN_IMPORT_COVERED_MIN = 1200

# Arm eligibility on how much of the day the optimizer was writing setpoints.
PLAN_MAX_CONTROLLED = 0.1
SCHEDULE_MIN_CONTROLLED = 0.9

# The plan arm is skipped when "no plan entry" rows cover more than this.
PLAN_MAX_UNCOVERED_MIN = 120

# Reference hold defaults (options twin_reference_heat_f / _cool_f).
DEFAULT_REFERENCE_HEAT_F = 72.0
DEFAULT_REFERENCE_COOL_F = 72.0
# Minimum gap between heat and cool in a heat_cool reference hold.
REFERENCE_DEADBAND_F = 2.0

# Comfort yardstick: hours more than this beyond the household's setpoint.
OFF_SETPOINT_MARGIN_F = 1.0

# Typical schedule (section 4.2).
TYPICAL_SLOT_MINUTES = 15
TYPICAL_SLOTS = 24 * 60 // TYPICAL_SLOT_MINUTES
TYPICAL_MAX_DAYS = 28
TYPICAL_MAX_AGE_DAYS = 60
TYPICAL_MIN_WEEKDAYS = 5
TYPICAL_MIN_WEEKEND = 2

# Aux run classification (section 5.4).
RECOVERY_STEP_F = 1.5
RECOVERY_BEFORE_MIN = 60
RECOVERY_AFTER_MIN = 180
DEFAULT_AUX_THRESHOLD_F = 35.0
# Smart recovery ramps the heat setpoint up in small increments (0.1 to
# 0.9 F every 5 minutes on an Ecobee) instead of one jump. Rises separated by
# no more than this many minutes are merged into one step before testing it
# against RECOVERY_STEP_F; a single jump is the degenerate one-row case.
RECOVERY_RAMP_GAP_MIN = 60

AUX_CLASSES = ("recovery", "cold", "other")

MODES = ("heat", "cool", "heat_cool", "off")

# Beestat "System Mode" to thermostat hvac_mode.
_BEESTAT_MODES = {"heat": "heat", "cool": "cool", "auto": "heat_cool", "off": "off"}


# ── Data records ───────────────────────────────────────────────────


@dataclass
class DayInputs:
    """Compact record of one local day, enough to simulate every arm.

    Run-length rows are ``[minute, mode, heat_sp, cool_sp]`` with ``minute``
    counted in elapsed minutes from ``start_utc`` (so a DST day runs 0..1380
    or 0..1500). A new row is appended only when the mode or either setpoint
    changes. The first row applies from minute 0 whatever its own minute.
    """

    date: str  # local "YYYY-MM-DD"
    start_utc: str  # ISO instant of local midnight
    hours: int  # 24 (23 or 25 on DST days)
    outdoor: list[float | None]  # hourly mean F, len == hours
    cloud: list[float | None]  # hourly mean cloud cover 0-1, None if unknown
    sun_up: list[bool | None]  # hourly: sun elevation > 0 at mid-hour
    indoor_start: float | None  # thermostat indoor temp at the first sample
    indoor: list[float | None]  # hourly mean thermostat indoor temp
    setpoints: list[list]  # run-length: [minute, mode, heat_sp, cool_sp]
    plan: list[list]  # run-length: [minute, mode, heat_sp, cool_sp] of the plan
    heat_min: list[float]  # hourly compressor-heating minutes, actual
    cool_min: list[float]  # hourly cooling minutes, actual
    aux_min: list[float]  # hourly aux minutes, actual
    aux_runs: list[list]  # [start_minute, end_minute, mean_outdoor, comp_before_min]
    controlled: float = 0.0  # fraction of the day the optimizer wrote setpoints
    covered_min: float = 0.0  # minutes with a valid sample
    metered_kwh: float | None = None  # integral of the power sensor
    source: str = "live"  # "live" | "recorder" | "import"
    # Thermostat indoor temp at the first sample of each hour, when that
    # sample falls in the hour's first 5 minutes (fit input, spec 3.5).
    indoor_top: list[float | None] = field(default_factory=list)

    @property
    def start(self) -> datetime:
        """Local midnight as an aware UTC datetime."""
        return datetime.fromisoformat(self.start_utc)

    @property
    def minutes(self) -> int:
        return self.hours * 60

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> DayInputs:
        hours = int(data.get("hours", 24))
        empty_f = [None] * hours
        zeros = [0.0] * hours
        return cls(
            date=data.get("date", ""),
            start_utc=data.get("start_utc", ""),
            hours=hours,
            outdoor=list(data.get("outdoor", empty_f)),
            cloud=list(data.get("cloud", empty_f)),
            sun_up=list(data.get("sun_up", empty_f)),
            indoor_start=data.get("indoor_start"),
            indoor=list(data.get("indoor", empty_f)),
            setpoints=[list(r) for r in data.get("setpoints", [])],
            plan=[list(r) for r in data.get("plan", [])],
            heat_min=list(data.get("heat_min", zeros)),
            cool_min=list(data.get("cool_min", zeros)),
            aux_min=list(data.get("aux_min", zeros)),
            aux_runs=[list(r) for r in data.get("aux_runs", [])],
            controlled=float(data.get("controlled", 0.0)),
            covered_min=float(data.get("covered_min", 0.0)),
            metered_kwh=data.get("metered_kwh"),
            source=data.get("source", "live"),
            indoor_top=list(data["indoor_top"]) if "indoor_top" in data else list(empty_f),
        )

    # Actual runtime totals, hours.
    @property
    def heat_h(self) -> float:
        return sum(self.heat_min) / 60.0

    @property
    def cool_h(self) -> float:
        return sum(self.cool_min) / 60.0

    @property
    def aux_h(self) -> float:
        return sum(self.aux_min) / 60.0

    @property
    def outdoor_mean(self) -> float | None:
        vals = [v for v in self.outdoor if v is not None]
        return sum(vals) / len(vals) if vals else None

    @property
    def indoor_mean(self) -> float | None:
        vals = [v for v in self.indoor if v is not None]
        return sum(vals) / len(vals) if vals else None


@dataclass
class DayResult:
    """One arm simulated over one day."""

    heat_h: float
    cool_h: float
    aux_h: float
    indoor_mean: float
    indoor_hourly: list[float]
    heat_min_hourly: list[float]  # for the by-hour accuracy view
    off_setpoint_h: float  # comfort yardstick, section 4.3
    end_temp: float
    cool_min_hourly: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> DayResult:
        return cls(
            heat_h=float(data.get("heat_h", 0.0)),
            cool_h=float(data.get("cool_h", 0.0)),
            aux_h=float(data.get("aux_h", 0.0)),
            indoor_mean=float(data.get("indoor_mean", 0.0)),
            indoor_hourly=list(data.get("indoor_hourly", [])),
            heat_min_hourly=list(data.get("heat_min_hourly", [])),
            off_setpoint_h=float(data.get("off_setpoint_h", 0.0)),
            end_temp=float(data.get("end_temp", 0.0)),
            cool_min_hourly=list(data.get("cool_min_hourly", [])),
        )


@dataclass
class Watts:
    """Watt constants for converting runtime to energy (section 3.4)."""

    heat: float
    cool: float
    aux: float
    heat_source: str = "rated"  # "measured" | "rated"
    cool_source: str = "rated"
    aux_source: str = "assumed"  # "configured" | "assumed"


# ── Energy from runtime (section 3.4) ──────────────────────────────

# Measured compressor watts need this many hours of compressor-on, aux-off
# cycles with a real power reading before they replace the rated figure.
MEASURED_WATTS_MIN_H = 10.0


def energy_kwh(heat_h: float, cool_h: float, aux_h: float, watts: Watts) -> float:
    """``E = heat_h * P_heat + cool_h * P_cool + aux_h * P_aux`` in kWh.

    ``heat_h`` already includes the hours aux ran alongside the compressor;
    ``P_aux`` is the strip draw on top.
    """
    return (heat_h * watts.heat + cool_h * watts.cool + aux_h * watts.aux) / 1000.0


def result_kwh(result: DayResult, watts: Watts) -> float:
    return energy_kwh(result.heat_h, result.cool_h, result.aux_h, watts)


def actual_kwh(inputs: DayInputs, watts: Watts) -> float:
    """Actual energy: the same formula applied to actual runtime."""
    return energy_kwh(inputs.heat_h, inputs.cool_h, inputs.aux_h, watts)


def resolve_watts(
    *,
    rated_heat: float,
    rated_cool: float,
    measured_heat_kwh: float = 0.0,
    measured_heat_h: float = 0.0,
    measured_cool_kwh: float = 0.0,
    measured_cool_h: float = 0.0,
    aux_kw_configured: float | None = None,
    model_aux_watts: float = 10000.0,
) -> Watts:
    """Pick watt constants per the first-choice / fallback table in 3.4.

    The measured sums are over the last 30 days of compressor-on, aux-off
    cycles in that mode with a real power reading.
    """
    if measured_heat_h >= MEASURED_WATTS_MIN_H:
        heat, heat_src = measured_heat_kwh * 1000.0 / measured_heat_h, "measured"
    else:
        heat, heat_src = float(rated_heat), "rated"
    if measured_cool_h >= MEASURED_WATTS_MIN_H:
        cool, cool_src = measured_cool_kwh * 1000.0 / measured_cool_h, "measured"
    else:
        cool, cool_src = float(rated_cool), "rated"
    if aux_kw_configured:
        aux, aux_src = float(aux_kw_configured) * 1000.0, "configured"
    else:
        aux, aux_src = float(model_aux_watts), "assumed"
    return Watts(heat, cool, aux, heat_src, cool_src, aux_src)


# ── Run-length schedules ───────────────────────────────────────────


def setpoint_row(
    hvac_mode: str | None,
    target_temp: float | None,
    target_temp_low: float | None = None,
    target_temp_high: float | None = None,
) -> tuple[str, float | None, float | None]:
    """Map a thermostat state to ``(mode, heat_sp, cool_sp)`` (table in 3.1)."""
    if hvac_mode == "heat":
        return ("heat", target_temp, None)
    if hvac_mode == "cool":
        return ("cool", None, target_temp)
    if hvac_mode == "heat_cool":
        return ("heat_cool", target_temp_low, target_temp_high)
    return ("off", None, None)


def plan_row(entry: ScheduleEntry | None) -> tuple[str, float | None, float | None]:
    """Map the plan's current ``ScheduleEntry`` to ``(mode, heat_sp, cool_sp)``."""
    if entry is None:
        return ("off", None, None)
    if entry.is_dual_setpoint:
        return ("heat_cool", entry.target_temp_low, entry.target_temp_high)
    if entry.mode == "heat":
        return ("heat", entry.target_temp, None)
    if entry.mode == "cool":
        return ("cool", None, entry.target_temp)
    return ("off", None, None)


def append_row(rows: list[list], minute: float, mode: str, heat_sp: float | None, cool_sp: float | None) -> bool:
    """Append ``[minute, mode, heat_sp, cool_sp]`` only if it differs from the last row."""
    if rows:
        last = rows[-1]
        if last[1] == mode and last[2] == heat_sp and last[3] == cool_sp:
            return False
    rows.append([minute, mode, heat_sp, cool_sp])
    return True


def merge_rows(rows: list[list]) -> list[list]:
    """Drop rows that repeat the previous row's mode and setpoints."""
    out: list[list] = []
    for row in rows:
        append_row(out, row[0], row[1], row[2], row[3])
    return out


def row_at(rows: list[list], minute: float) -> list | None:
    """The row in force at ``minute`` (the first row covers from 0)."""
    if not rows:
        return None
    current = rows[0]
    for row in rows[1:]:
        if row[0] <= minute:
            current = row
        else:
            break
    return current


def _effective_row(mode: str, heat_sp: float | None, cool_sp: float | None) -> tuple[str, float | None, float | None]:
    """Normalize a row whose needed setpoint is missing to ``off``."""
    if mode == "heat" and heat_sp is not None:
        return ("heat", heat_sp, None)
    if mode == "cool" and cool_sp is not None:
        return ("cool", None, cool_sp)
    if mode == "heat_cool":
        if heat_sp is not None and cool_sp is not None:
            return ("heat_cool", heat_sp, cool_sp)
        if heat_sp is not None:
            return ("heat", heat_sp, None)
        if cool_sp is not None:
            return ("cool", None, cool_sp)
    return ("off", None, None)


def schedule_entries(rows: list[list], start: datetime, minutes: int) -> list[ScheduleEntry]:
    """Build ``ScheduleEntry``s covering ``[start, start + minutes)`` from run-length rows."""
    end = start + timedelta(minutes=minutes)
    if not rows:
        return [ScheduleEntry(start_time=start, end_time=end, target_temp=0.0, mode="off", reason="twin")]
    entries: list[ScheduleEntry] = []
    for i, row in enumerate(rows):
        m0 = 0.0 if i == 0 else max(0.0, float(row[0]))
        m1 = float(rows[i + 1][0]) if i + 1 < len(rows) else float(minutes)
        m1 = min(m1, float(minutes))
        if m1 <= m0:
            continue
        mode, heat_sp, cool_sp = _effective_row(row[1], row[2], row[3])
        t0 = start + timedelta(minutes=m0)
        t1 = start + timedelta(minutes=m1)
        if mode == "heat_cool":
            entry = ScheduleEntry(
                start_time=t0,
                end_time=t1,
                target_temp=heat_sp,
                mode="heat",
                reason="twin",
                target_temp_low=heat_sp,
                target_temp_high=cool_sp,
            )
        elif mode == "heat":
            entry = ScheduleEntry(start_time=t0, end_time=t1, target_temp=heat_sp, mode="heat", reason="twin")
        elif mode == "cool":
            entry = ScheduleEntry(start_time=t0, end_time=t1, target_temp=cool_sp, mode="cool", reason="twin")
        else:
            entry = ScheduleEntry(start_time=t0, end_time=t1, target_temp=0.0, mode="off", reason="twin")
        entries.append(entry)
    return entries


# ── Arm schedule builders (section 4) ──────────────────────────────


def replay_schedule(inputs: DayInputs) -> list[list]:
    """The day's real setpoints."""
    return [list(r) for r in inputs.setpoints]


def reference_schedule(
    inputs: DayInputs,
    ref_heat: float = DEFAULT_REFERENCE_HEAT_F,
    ref_cool: float = DEFAULT_REFERENCE_COOL_F,
) -> list[list]:
    """Constant hold that copies each real row's mode (section 4.1).

    This mostly measures the household's own setpoint choices against a
    72 F hold. Label it "vs holding 72", never "optimizer savings".
    """
    out: list[list] = []
    for row in inputs.setpoints:
        mode = row[1]
        if mode == "heat":
            append_row(out, row[0], "heat", ref_heat, None)
        elif mode == "cool":
            append_row(out, row[0], "cool", None, ref_cool)
        elif mode == "heat_cool":
            append_row(out, row[0], "heat_cool", ref_heat, max(ref_cool, ref_heat + REFERENCE_DEADBAND_F))
        else:
            append_row(out, row[0], "off", None, None)
    return out


def warmer_schedule(inputs: DayInputs, step: float = 1.0) -> list[list]:
    """Real setpoints with heat +1 F and cool -1 F wherever each is set."""
    return [
        [
            row[0],
            row[1],
            row[2] + step if row[2] is not None else None,
            row[3] - step if row[3] is not None else None,
        ]
        for row in inputs.setpoints
    ]


def plan_uncovered_minutes(inputs: DayInputs) -> float:
    """Minutes of the day covered by "no plan entry" rows (mode off, both None)."""
    rows = inputs.plan
    if not rows:
        return float(inputs.minutes)
    total = 0.0
    for i, row in enumerate(rows):
        m0 = 0.0 if i == 0 else float(row[0])
        m1 = float(rows[i + 1][0]) if i + 1 < len(rows) else float(inputs.minutes)
        if row[1] == "off" and row[2] is None and row[3] is None:
            total += max(0.0, min(m1, inputs.minutes) - m0)
    return total


def plan_schedule(inputs: DayInputs) -> list[list] | None:
    """The optimizer's plan as recorded, or None when it does not cover the day."""
    if not inputs.plan or plan_uncovered_minutes(inputs) > PLAN_MAX_UNCOVERED_MIN:
        return None
    return [list(r) for r in inputs.plan]


def _local_slot(start: datetime, minute: float, tz: tzinfo) -> int:
    """Typical-schedule slot of the local wall time at ``minute`` into the day."""
    local = (start + timedelta(minutes=minute)).astimezone(tz)
    return (local.hour * 60 + local.minute) // TYPICAL_SLOT_MINUTES


def _is_weekend(day: str) -> bool:
    return date.fromisoformat(day).weekday() >= 5


def build_typical_schedule(days: Iterable[DayInputs], today: str, tz: tzinfo) -> dict | None:
    """Household's typical schedule from recent uncontrolled days (section 4.2).

    Most recent ``TYPICAL_MAX_DAYS`` days before ``today`` with
    ``controlled <= PLAN_MAX_CONTROLLED``, none older than
    ``TYPICAL_MAX_AGE_DAYS``, split weekday / weekend. Per 15-minute slot:
    the most common mode, then the median heat and cool setpoints among days
    in that mode. Returns ``{"weekday": slots, "weekend": slots}`` with each
    slot ``[mode, heat_sp, cool_sp]``, or None when there are fewer than 5
    weekdays or 2 weekend days.
    """
    t = date.fromisoformat(today)
    pool = [
        d
        for d in days
        if d.controlled <= PLAN_MAX_CONTROLLED
        and d.setpoints
        and 0 < (t - date.fromisoformat(d.date)).days <= TYPICAL_MAX_AGE_DAYS
    ]
    pool.sort(key=lambda d: d.date, reverse=True)
    pool = pool[:TYPICAL_MAX_DAYS]
    weekday = [d for d in pool if not _is_weekend(d.date)]
    weekend = [d for d in pool if _is_weekend(d.date)]
    if len(weekday) < TYPICAL_MIN_WEEKDAYS or len(weekend) < TYPICAL_MIN_WEEKEND:
        return None
    return {"weekday": _typical_slots(weekday, tz), "weekend": _typical_slots(weekend, tz)}


def _typical_slots(days: list[DayInputs], tz: tzinfo) -> list[list]:
    # Per day, the row in force at each local slot's midpoint.
    per_slot: list[list[list]] = [[] for _ in range(TYPICAL_SLOTS)]
    for d in days:
        start = d.start
        for minute in range(TYPICAL_SLOT_MINUTES // 2, d.minutes, TYPICAL_SLOT_MINUTES):
            row = row_at(d.setpoints, minute)
            if row is None:
                continue
            slot = _local_slot(start, minute, tz)
            if 0 <= slot < TYPICAL_SLOTS:
                per_slot[slot].append(row)
    slots: list[list] = []
    prev: list = ["off", None, None]
    for rows in per_slot:
        if not rows:
            slots.append(list(prev))
            continue
        counts = Counter(r[1] for r in rows)
        top = max(counts.values())
        mode = next(m for m in MODES if counts.get(m) == top)
        in_mode = [r for r in rows if r[1] == mode]
        heats = [r[2] for r in in_mode if r[2] is not None]
        cools = [r[3] for r in in_mode if r[3] is not None]
        heat = statistics.median(heats) if heats and mode in ("heat", "heat_cool") else None
        cool = statistics.median(cools) if cools and mode in ("cool", "heat_cool") else None
        prev = [mode, heat, cool]
        slots.append(prev)
    return slots


def schedule_arm_schedule(inputs: DayInputs, typical: dict | None, tz: tzinfo) -> list[list] | None:
    """The household's typical schedule laid onto this day's wall clock."""
    if not typical:
        return None
    slots = typical["weekend" if _is_weekend(inputs.date) else "weekday"]
    start = inputs.start
    out: list[list] = []
    for minute in range(0, inputs.minutes, STEP_MINUTES):
        mode, heat, cool = slots[_local_slot(start, minute, tz)]
        append_row(out, minute, mode, heat, cool)
    return out


def eligible_arms(inputs: DayInputs) -> list[str]:
    """Arms computed for this day (section 4 table), before data availability.

    ``plan`` needs ``controlled <= 0.1``; ``schedule`` needs
    ``controlled >= 0.9``; mixed days get neither. Whether the plan rows or a
    typical schedule actually exist is checked by the builders.
    """
    arms = ["replay", "reference", "warmer"]
    if inputs.controlled <= PLAN_MAX_CONTROLLED:
        arms.append("plan")
    if inputs.controlled >= SCHEDULE_MIN_CONTROLLED:
        arms.append("schedule")
    return arms


def arm_schedules(
    inputs: DayInputs,
    *,
    ref_heat: float = DEFAULT_REFERENCE_HEAT_F,
    ref_cool: float = DEFAULT_REFERENCE_COOL_F,
    typical: dict | None = None,
    tz: tzinfo | None = None,
) -> dict[str, list[list]]:
    """Every arm this day gets, mapped to its run-length schedule."""
    out: dict[str, list[list]] = {}
    for arm in eligible_arms(inputs):
        if arm == "replay":
            sched = replay_schedule(inputs)
        elif arm == "reference":
            sched = reference_schedule(inputs, ref_heat, ref_cool)
        elif arm == "warmer":
            sched = warmer_schedule(inputs)
        elif arm == "plan":
            sched = plan_schedule(inputs)
        else:
            sched = schedule_arm_schedule(inputs, typical, tz) if tz is not None else None
        if sched is not None:
            out[arm] = sched
    return out


# ── Running one scenario for one day (section 3.2) ─────────────────


def can_simulate(inputs: DayInputs) -> bool:
    """Whether the day has enough outdoor readings to simulate.

    A whole day needs ``MIN_OUTDOOR_HOURS``; a partial day (today so far,
    under 23 hours) needs 80% of its hours.
    """
    if inputs.hours <= 0:
        return False
    need = MIN_OUTDOOR_HOURS if inputs.hours >= 23 else math.ceil(0.8 * inputs.hours)
    return sum(1 for v in inputs.outdoor if v is not None) >= need


def _fill_gaps(values: list[float | None]) -> list[float]:
    """Linear interpolation over None, edges held at the nearest value."""
    known = [(i, v) for i, v in enumerate(values) if v is not None]
    if not known:
        return []
    out: list[float] = []
    k = 0
    for i in range(len(values)):
        if values[i] is not None:
            out.append(float(values[i]))
            continue
        while k + 1 < len(known) and known[k + 1][0] < i:
            k += 1
        left = known[k] if known[k][0] < i else None
        right = next((p for p in known[k:] if p[0] > i), None)
        if left and right:
            frac = (i - left[0]) / (right[0] - left[0])
            out.append(left[1] + frac * (right[1] - left[1]))
        else:
            out.append(float((left or right)[1]))
    return out


def build_forecast(inputs: DayInputs) -> list[ForecastPoint]:
    """``ForecastPoint``s at each mid-hour from the day's hourly weather."""
    outdoor = _fill_gaps(inputs.outdoor)
    start = inputs.start
    points: list[ForecastPoint] = []
    for h, temp in enumerate(outdoor):
        cloud = inputs.cloud[h] if h < len(inputs.cloud) else None
        sun = inputs.sun_up[h] if h < len(inputs.sun_up) else None
        # ThermalSimulator only tests the sign of sun_elevation.
        elev = None if sun is None else (10.0 if sun else -10.0)
        points.append(
            ForecastPoint(
                time=start + timedelta(minutes=h * 60 + 30),
                outdoor_temp=temp,
                cloud_cover=cloud,
                sun_elevation=elev,
            )
        )
    return points


def off_setpoint_hours(indoor_hourly: list[float | None], setpoints: list[list], hours: int) -> float:
    """Comfort yardstick (section 4.3).

    Hours in which the indoor temperature is more than 1 F below the
    household's real heat setpoint in force, or more than 1 F above the real
    cool setpoint. Evaluated per 5-minute step against that step's setpoint,
    using the hour's mean indoor temperature for both simulated arms and the
    measured house, so every row of the comparison uses the same yardstick.
    """
    total_steps = 0
    for step in range(hours * STEPS_PER_HOUR):
        h = step // STEPS_PER_HOUR
        temp = indoor_hourly[h] if h < len(indoor_hourly) else None
        if temp is None:
            continue
        row = row_at(setpoints, step * STEP_MINUTES)
        if row is None:
            continue
        mode, heat_sp, cool_sp = _effective_row(row[1], row[2], row[3])
        too_cold = heat_sp is not None and temp < heat_sp - OFF_SETPOINT_MARGIN_F
        too_warm = cool_sp is not None and temp > cool_sp + OFF_SETPOINT_MARGIN_F
        if too_cold or too_warm:
            total_steps += 1
    return total_steps * STEP_MINUTES / 60.0


def run_scenario(
    model: PerformanceModel,
    inputs: DayInputs,
    schedule: list[list],
    start_temp: float,
    sun: list[tuple] | None = None,
) -> DayResult | None:
    """Simulate one arm's schedule over one day.

    ``sun`` is the day's sun series (``twin_house.sun_series``), shared by
    every arm. A model with ``with_sun`` gets its own copy reading from it,
    so arms never share mutable state.

    Returns None when the day cannot be simulated (fewer than 20 hourly
    outdoor readings).
    """
    if not can_simulate(inputs):
        return None
    start = inputs.start
    if hasattr(model, "with_sun"):
        model = model.with_sun(sun, start)
    forecast = build_forecast(inputs)
    entries = schedule_entries(schedule, start, inputs.minutes)
    points = ThermalSimulator(model).simulate(start_temp, forecast, entries, dt_minutes=float(STEP_MINUTES))
    end_temp = points[-1].indoor_temp if points else start_temp
    # The last point sits on the end instant; it belongs to the next day.
    points = points[:-1]

    hours = inputs.hours
    heat = [0.0] * hours
    cool = [0.0] * hours
    aux = [0.0] * hours
    temp_sum = [0.0] * hours
    temp_n = [0] * hours
    for i, p in enumerate(points):
        h = min(i // STEPS_PER_HOUR, hours - 1)
        if p.hvac_running and p.hvac_mode == "heat":
            heat[h] += STEP_MINUTES
        elif p.hvac_running and p.hvac_mode == "cool":
            cool[h] += STEP_MINUTES
        if p.aux_running:
            aux[h] += STEP_MINUTES
        temp_sum[h] += p.indoor_temp
        temp_n[h] += 1
    indoor_hourly = [round(temp_sum[h] / temp_n[h], 3) if temp_n[h] else start_temp for h in range(hours)]
    indoor_mean = sum(p.indoor_temp for p in points) / len(points) if points else start_temp
    return DayResult(
        heat_h=sum(heat) / 60.0,
        cool_h=sum(cool) / 60.0,
        aux_h=sum(aux) / 60.0,
        indoor_mean=round(indoor_mean, 3),
        indoor_hourly=indoor_hourly,
        heat_min_hourly=heat,
        off_setpoint_h=off_setpoint_hours(indoor_hourly, inputs.setpoints, hours),
        end_temp=end_temp,
        cool_min_hourly=cool,
    )


# ── Continuous multi-day runs (section 3.3) ────────────────────────


def _is_next_day(prev: DayInputs, cur: DayInputs) -> bool:
    return (date.fromisoformat(cur.date) - date.fromisoformat(prev.date)).days == 1


def run_continuous(
    model: PerformanceModel | None,
    days: list[DayInputs],
    arms: Iterable[str] = ("replay",),
    *,
    model_for: Callable[[DayInputs], object] | None = None,
    sun_for: Callable[[DayInputs, DayInputs | None], list[tuple] | None] | None = None,
    **schedule_kwargs,
) -> list[dict]:
    """Simulate arms day after day, each from its own previous end temperature.

    The replay arm is never re-anchored to the real indoor temperature at
    midnight; doing so would start it from a different kind of state than
    the other arms and the deltas would pick up the difference. When the
    previous day is missing or was not simulated every arm restarts from
    ``indoor_start`` and the day is flagged ``warmup`` (excluded from every
    statistic).

    ``model_for(day)``, when given, picks each day's model (the twin house
    model of the other fold); a None from it leaves the day unsimulated.
    ``sun_for(day, prev_day)`` builds the day's sun series.

    Returns one dict per input day: ``{"date", "simulated", "warmup",
    "results": {arm: DayResult}}``.
    """
    wanted = list(arms)
    out: list[dict] = []
    prev_day: DayInputs | None = None
    prev_end: dict[str, float] = {}
    for d in sorted(days, key=lambda x: x.date):
        record: dict = {"date": d.date, "simulated": False, "warmup": False, "results": {}}
        out.append(record)
        day_model = model_for(d) if model_for is not None else model
        if not can_simulate(d) or d.indoor_start is None or day_model is None:
            prev_day, prev_end = d, {}
            continue
        continuing = prev_day is not None and _is_next_day(prev_day, d) and "replay" in prev_end
        record["warmup"] = not continuing
        sun = sun_for(d, prev_day) if sun_for is not None else None
        schedules = arm_schedules(d, **schedule_kwargs)
        ends: dict[str, float] = {}
        for arm in wanted:
            sched = schedules.get(arm)
            if sched is None:
                continue
            start_temp = prev_end.get(arm, prev_end.get("replay")) if continuing else None
            if start_temp is None:
                start_temp = d.indoor_start
            res = run_scenario(day_model, d, sched, start_temp, sun)
            if res is None:
                continue
            record["results"][arm] = res
            ends[arm] = res.end_temp
        record["simulated"] = "replay" in record["results"]
        prev_day, prev_end = d, ends if record["simulated"] else {}
    return out


# ── Aux run classification (section 5.4) ───────────────────────────


def recovery_windows(setpoints: list[list]) -> list[tuple[float, float]]:
    """Windows around heat setpoint increases of at least ``RECOVERY_STEP_F``.

    Each window runs from ``RECOVERY_BEFORE_MIN`` before the increase to
    ``RECOVERY_AFTER_MIN`` after it (the thermostat starts recovering ahead
    of the scheduled step). A smart-recovery ramp, a chain of small rises no
    more than ``RECOVERY_RAMP_GAP_MIN`` apart, counts as one increase from
    its first rise to its last.
    """
    rises: list[tuple[float, float]] = []  # (minute, rise F)
    prev_heat: float | None = None
    for row in setpoints:
        heat = row[2] if row[1] in ("heat", "heat_cool") else None
        if heat is not None and prev_heat is not None and heat > prev_heat:
            rises.append((float(row[0]), heat - prev_heat))
        if heat is not None:
            prev_heat = heat
    windows: list[tuple[float, float]] = []
    i = 0
    while i < len(rises):
        first = last = rises[i][0]
        total = rises[i][1]
        j = i + 1
        while j < len(rises) and rises[j][0] - last <= RECOVERY_RAMP_GAP_MIN:
            last = rises[j][0]
            total += rises[j][1]
            j += 1
        if total >= RECOVERY_STEP_F - 1e-9:
            windows.append((first - RECOVERY_BEFORE_MIN, last + RECOVERY_AFTER_MIN))
        i = j
    return windows


def classify_aux_run(
    run: list,
    windows: list[tuple[float, float]],
    threshold_f: float | None = None,
) -> str:
    """``recovery``, ``cold`` or ``other`` for one ``[start, end, mean_outdoor, ...]`` run."""
    start, end, outdoor = float(run[0]), float(run[1]), run[2]
    for w0, w1 in windows:
        if start < w1 and end > w0:
            return "recovery"
    limit = DEFAULT_AUX_THRESHOLD_F if threshold_f is None else threshold_f
    if outdoor is not None and outdoor < limit:
        return "cold"
    return "other"


def classify_aux(inputs: DayInputs, threshold_f: float | None = None) -> dict[str, float]:
    """Aux hours per class for one day.

    ``threshold_f`` is ``aux_heat_learner.threshold_f`` when learned; None
    uses the 35 F default.
    """
    windows = recovery_windows(inputs.setpoints)
    hours = dict.fromkeys(AUX_CLASSES, 0.0)
    for run in inputs.aux_runs:
        cls = classify_aux_run(run, windows, threshold_f)
        hours[cls] += max(0.0, float(run[1]) - float(run[0])) / 60.0
    return hours


# ── Beestat history import (section 7) ─────────────────────────────


def _num(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


class _DayBuilder:
    """Accumulates 5-minute rows of one local day into a ``DayInputs``."""

    def __init__(self, local_date: date, tz: tzinfo):
        midnight = datetime(local_date.year, local_date.month, local_date.day, tzinfo=tz)
        nxt = local_date + timedelta(days=1)
        next_midnight = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
        self.date = local_date.isoformat()
        self.start = midnight.astimezone(UTC)
        self.hours = round((next_midnight.astimezone(UTC) - self.start).total_seconds() / 3600)
        n = self.hours
        self.out_sum = [0.0] * n
        self.out_n = [0] * n
        self.in_sum = [0.0] * n
        self.in_n = [0] * n
        self.top: list[float | None] = [None] * n
        self.heat = [0.0] * n
        self.cool = [0.0] * n
        self.aux = [0.0] * n
        self.setpoints: list[list] = []
        self.indoor_start: float | None = None
        self.rows = 0
        self.aux_runs: list[list] = []
        self._run: list | None = None  # [start, end, outdoor_sum, outdoor_n]
        self._heat_sp: float | None = None
        self._cool_sp: float | None = None
        # Minutes the compressor has been heating without a break, carried
        # across midnight by the parser when rows are contiguous.
        self.heat_run_min = 0.0
        self.last_utc: datetime | None = None

    def add(self, when_utc: datetime, row: dict) -> None:
        minute = (when_utc - self.start).total_seconds() / 60.0
        h = int(minute // 60)
        if h < 0 or h >= self.hours:
            return
        indoor = _num(row.get("Indoor Temperature"))
        outdoor = _num(row.get("Outdoor Temperature"))
        self.rows += 1
        if self.indoor_start is None:
            self.indoor_start = indoor
        self.in_sum[h] += indoor
        self.in_n[h] += 1
        if self.top[h] is None and minute - h * 60 < STEP_MINUTES:
            self.top[h] = indoor
        if self.last_utc is not None and when_utc - self.last_utc != timedelta(minutes=STEP_MINUTES):
            self.heat_run_min = 0.0
        self.last_utc = when_utc
        if outdoor is not None:
            self.out_sum[h] += outdoor
            self.out_n[h] += 1

        comp = (_num(row.get("Compressor 1")) or 0.0) / 60.0
        aux = (_num(row.get("Auxiliary Heat 1")) or 0.0) / 60.0
        comp_mode = (row.get("Compressor Mode") or "").strip()
        if comp_mode == "cool":
            self.cool[h] += comp
        elif comp_mode == "heat":
            self.heat[h] += max(comp, aux)
        elif aux > 0:
            # Aux without compressor heating still counts as heating (3.1).
            self.heat[h] += aux
        self.aux[h] += aux

        if aux > 0:
            if self._run is not None and minute - self._run[1] <= 1e-6:
                self._run[1] = minute + STEP_MINUTES
            else:
                self._close_run()
                self._run = [minute, minute + STEP_MINUTES, 0.0, 0, self.heat_run_min]
            if outdoor is not None:
                self._run[2] += outdoor
                self._run[3] += 1
        else:
            self._close_run()
        heating = comp_mode == "heat" and comp > 0
        self.heat_run_min = self.heat_run_min + STEP_MINUTES if heating else 0.0

        sys_mode = _BEESTAT_MODES.get((row.get("System Mode") or "").strip(), "off")
        heat_sp = _num(row.get("Setpoint Heat"))
        cool_sp = _num(row.get("Setpoint Cool"))
        # Carry the last known setpoint across a blank cell.
        if heat_sp is not None:
            self._heat_sp = heat_sp
        if cool_sp is not None:
            self._cool_sp = cool_sp
        single = self._cool_sp if sys_mode == "cool" else self._heat_sp
        mode, hsp, csp = setpoint_row(sys_mode, single, self._heat_sp, self._cool_sp)
        append_row(self.setpoints, int(round(minute)), mode, hsp, csp)

    def _close_run(self) -> None:
        if self._run is None:
            return
        start, end, osum, on, comp_before = self._run
        mean_out = round(osum / on, 2) if on else None
        self.aux_runs.append([int(round(start)), int(round(end)), mean_out, comp_before])
        self._run = None

    def finish(self) -> DayInputs:
        self._close_run()
        n = self.hours
        return DayInputs(
            date=self.date,
            start_utc=self.start.isoformat(),
            hours=n,
            outdoor=[round(self.out_sum[h] / self.out_n[h], 2) if self.out_n[h] else None for h in range(n)],
            cloud=[None] * n,
            sun_up=[None] * n,
            indoor_start=self.indoor_start,
            indoor=[round(self.in_sum[h] / self.in_n[h], 2) if self.in_n[h] else None for h in range(n)],
            setpoints=self.setpoints,
            plan=[],
            heat_min=[round(v, 3) for v in self.heat],
            cool_min=[round(v, 3) for v in self.cool],
            aux_min=[round(v, 3) for v in self.aux],
            aux_runs=self.aux_runs,
            controlled=0.0,
            covered_min=float(self.rows * STEP_MINUTES),
            metered_kwh=None,
            source="import",
            indoor_top=list(self.top),
        )


def parse_beestat_csv(lines: Iterable[str], tz: tzinfo | str) -> list[DayInputs]:
    """Parse a Beestat 5-minute export into one ``DayInputs`` per local day.

    ``Timestamp`` is the thermostat's naive local time; ``Compressor 1`` and
    ``Auxiliary Heat 1`` are seconds of runtime in the 5-minute row (0-300).
    Rows with an empty indoor temperature are skipped (Beestat writes the
    repeated fall-back hour as empty rows). Only days with at least 20 hours
    of rows are returned. Cloud and sun are unknown. Each aux run records how
    long the compressor had been heating without a break when it started.
    """
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    reader = csv.DictReader(lines)
    days: list[DayInputs] = []
    builder: _DayBuilder | None = None
    last_naive: datetime | None = None
    fold = 0
    for row in reader:
        if _num(row.get("Indoor Temperature")) is None:
            continue
        stamp = (row.get("Timestamp") or "").strip()
        try:
            naive = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        # A wall-clock time that repeats or goes backwards is the second
        # pass through a fall-back hour.
        if last_naive is None or naive.date() != last_naive.date():
            fold = 0
        elif naive <= last_naive:
            fold = 1
        elif not (fold == 1 and naive.hour == last_naive.hour):
            fold = 0
        last_naive = naive
        when = naive.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if builder is None or builder.date != naive.date().isoformat():
            prev_builder = builder
            if builder is not None:
                _keep(days, builder)
            builder = _DayBuilder(naive.date(), zone)
            if prev_builder is not None:
                builder.heat_run_min = prev_builder.heat_run_min
                builder.last_utc = prev_builder.last_utc
        builder.add(when, row)
    if builder is not None:
        _keep(days, builder)
    return days


def _keep(days: list[DayInputs], builder: _DayBuilder) -> None:
    if builder.rows * STEP_MINUTES >= MIN_IMPORT_COVERED_MIN:
        days.append(builder.finish())
