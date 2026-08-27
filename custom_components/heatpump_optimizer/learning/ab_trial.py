"""A/B validation trial — the only measurement that can prove the thesis.

Alternates the optimizer between on and off in fixed-length blocks and
accumulates the two resulting populations of metered kWh per degree-day.  No
thermal model, no counterfactual simulation, no COP assumption: two sets of
real days, weather-normalized the same way, compared directly.

The trial drives the same pause/resume path the Optimizer Enabled switch uses,
and consumes the same DayEnergyRecord stream the weather-normalized baseline
builds, so it adds a scheduler and a statistic rather than a second accounting
system.

Blocks alternate on/off so that seasonal drift affects both populations
roughly equally.  Days are assigned to a population by what the optimizer
actually did that day, not by what the schedule intended, so a day the user
took manual control of is discarded instead of contaminating a population.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .energy_baseline import (
    LABEL_BASELINE,
    LABEL_OPTIMIZED,
    MIN_DEGREE_DAYS_FOR_FIT,
    DayEnergyRecord,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_PERIOD_DAYS = 3
DEFAULT_TOTAL_DAYS = 28

MIN_PERIOD_DAYS = 1
MAX_PERIOD_DAYS = 14
MIN_TOTAL_DAYS = 2
MAX_TOTAL_DAYS = 180

PHASE_ON = "on"
PHASE_OFF = "off"

# A verdict needs both populations to have real days and the difference to
# clear roughly two standard errors.  Below that the honest answer is
# "not enough data yet".
MIN_DAYS_PER_POPULATION = 4
CONCLUSIVE_T = 2.0


@dataclass
class TrialDay:
    """One completed day assigned to a trial population."""

    date: str
    population: str  # PHASE_ON | PHASE_OFF
    kwh: float
    degree_days: float
    kwh_per_degree_day: float
    mean_outdoor_f: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "population": self.population,
            "kwh": round(self.kwh, 4),
            "degree_days": round(self.degree_days, 3),
            "kwh_per_degree_day": round(self.kwh_per_degree_day, 5),
            "mean_outdoor_f": round(self.mean_outdoor_f, 2),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrialDay:
        return cls(
            date=str(data["date"]),
            population=str(data.get("population", PHASE_ON)),
            kwh=float(data.get("kwh", 0.0)),
            degree_days=float(data.get("degree_days", 0.0)),
            kwh_per_degree_day=float(data.get("kwh_per_degree_day", 0.0)),
            mean_outdoor_f=float(data.get("mean_outdoor_f", 0.0)),
        )


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _sample_variance(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = _mean(values)
    return sum((v - m) ** 2 for v in values) / (n - 1)


class ABTrial:
    """Schedules alternating optimizer-on/off blocks and scores the result."""

    def __init__(self) -> None:
        self._active: bool = False
        self._start_date: date | None = None
        self._period_days: int = DEFAULT_PERIOD_DAYS
        self._total_days: int = DEFAULT_TOTAL_DAYS
        self._days: list[TrialDay] = []
        self._completed: bool = False
        self._stopped_reason: str | None = None

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(
        self,
        now: datetime,
        period_days: int = DEFAULT_PERIOD_DAYS,
        total_days: int = DEFAULT_TOTAL_DAYS,
        reset: bool = True,
    ) -> None:
        """Begin a trial starting today (local calendar)."""
        self._period_days = max(MIN_PERIOD_DAYS, min(MAX_PERIOD_DAYS, int(period_days)))
        self._total_days = max(MIN_TOTAL_DAYS, min(MAX_TOTAL_DAYS, int(total_days)))
        self._start_date = now.date()
        self._active = True
        self._completed = False
        self._stopped_reason = None
        if reset:
            self._days = []
        _LOGGER.info(
            "A/B validation trial started: %d day blocks for %d days total",
            self._period_days,
            self._total_days,
        )

    def stop(self, reason: str = "stopped by user") -> None:
        """End the trial and hand control back to normal operation."""
        if not self._active:
            return
        self._active = False
        self._stopped_reason = reason
        _LOGGER.info("A/B validation trial ended (%s)", reason)

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def period_days(self) -> int:
        return self._period_days

    @property
    def total_days(self) -> int:
        return self._total_days

    @property
    def start_date(self) -> date | None:
        return self._start_date

    # ── Scheduling ───────────────────────────────────────────────────

    def elapsed_days(self, now: datetime) -> int:
        if self._start_date is None:
            return 0
        return max(0, (now.date() - self._start_date).days)

    def current_phase(self, now: datetime) -> str | None:
        """Which arm the trial is in right now, or None when inactive."""
        if not self._active or self._start_date is None:
            return None
        elapsed = self.elapsed_days(now)
        if elapsed >= self._total_days:
            return None
        block = elapsed // self._period_days
        return PHASE_ON if block % 2 == 0 else PHASE_OFF

    def should_optimize(self, now: datetime) -> bool | None:
        """True/False when the trial wants control, None when it does not.

        None means "no opinion": the trial is inactive or has run its course,
        and normal pause/resume semantics apply.
        """
        phase = self.current_phase(now)
        if phase is None:
            return None
        return phase == PHASE_ON

    def pause_action(
        self,
        now: datetime,
        currently_paused: bool,
        trial_paused: bool,
    ) -> str | None:
        """What the trial wants done to the optimizer right now.

        Returns "pause", "resume", or None. Kept as a pure function so the
        interaction with a user's own pause is testable without a coordinator.

        A pause the user asked for always wins: the trial will not resume an
        optimizer it did not pause itself, and once the user has taken over,
        the trial stops touching pause state for the rest of the run.
        """
        want_optimize = self.should_optimize(now)
        if want_optimize is None:
            return None
        if not want_optimize and not currently_paused:
            return "pause"
        if want_optimize and currently_paused and trial_paused:
            return "resume"
        return None

    def check_expiry(self, now: datetime) -> bool:
        """End the trial once total_days have elapsed. Returns True if ended."""
        if not self._active or self._start_date is None:
            return False
        if self.elapsed_days(now) >= self._total_days:
            self._completed = True
            self.stop(reason="completed")
            return True
        return False

    # ── Data intake ──────────────────────────────────────────────────

    def record_day(self, record: DayEnergyRecord) -> None:
        """Assign one completed day to a population, if it qualifies.

        Called for every day the energy baseline closes.  Days outside the
        trial window, days with poor sensor coverage, mild days that carry no
        degree-day signal, and days where control was mixed are all skipped.
        """
        if self._start_date is None:
            return
        if not record.usable:
            return
        try:
            day = datetime.strptime(record.date, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return
        if day < self._start_date:
            return
        if (day - self._start_date).days >= self._total_days:
            return
        intensity = record.kwh_per_degree_day
        if intensity is None or record.degree_days < MIN_DEGREE_DAYS_FOR_FIT:
            return

        if record.label == LABEL_OPTIMIZED:
            population = PHASE_ON
        elif record.label == LABEL_BASELINE:
            population = PHASE_OFF
        else:
            _LOGGER.debug(
                "A/B trial skipping %s: mixed control (%.0f%% optimized)",
                record.date,
                record.optimizer_fraction * 100,
            )
            return

        if any(d.date == record.date for d in self._days):
            return

        self._days.append(
            TrialDay(
                date=record.date,
                population=population,
                kwh=record.kwh,
                degree_days=record.degree_days,
                kwh_per_degree_day=intensity,
                mean_outdoor_f=record.mean_outdoor_f,
            )
        )
        _LOGGER.debug(
            "A/B trial recorded %s to %s arm: %.3f kWh/degree-day",
            record.date,
            population,
            intensity,
        )

    # ── Result ───────────────────────────────────────────────────────

    def _population(self, name: str) -> list[float]:
        return [d.kwh_per_degree_day for d in self._days if d.population == name]

    def result(self) -> dict[str, Any]:
        """Current standing of the trial, in plain measured terms."""
        on = self._population(PHASE_ON)
        off = self._population(PHASE_OFF)

        on_mean = _mean(on)
        off_mean = _mean(off)

        summary: dict[str, Any] = {
            "active": self._active,
            "completed": self._completed,
            "stopped_reason": self._stopped_reason,
            "period_days": self._period_days,
            "total_days": self._total_days,
            "start_date": self._start_date.isoformat() if self._start_date else None,
            "optimizer_on_days": len(on),
            "optimizer_off_days": len(off),
            "optimizer_on_kwh_per_degree_day": round(on_mean, 4) if on else None,
            "optimizer_off_kwh_per_degree_day": round(off_mean, 4) if off else None,
            "difference_kwh_per_degree_day": None,
            "difference_pct": None,
            "t_statistic": None,
            "conclusive": False,
            "verdict": "not enough data yet",
        }

        if not on or not off:
            return summary

        difference = off_mean - on_mean  # positive means the optimizer saved
        summary["difference_kwh_per_degree_day"] = round(difference, 4)
        if off_mean > 0:
            summary["difference_pct"] = round(difference / off_mean * 100.0, 1)

        var_on = _sample_variance(on)
        var_off = _sample_variance(off)
        se = math.sqrt(var_on / len(on) + var_off / len(off)) if (on and off) else 0.0
        if se > 0:
            t_stat = difference / se
            summary["t_statistic"] = round(t_stat, 2)
        else:
            t_stat = 0.0

        enough = (
            len(on) >= MIN_DAYS_PER_POPULATION
            and len(off) >= MIN_DAYS_PER_POPULATION
        )
        if enough and se > 0 and abs(t_stat) >= CONCLUSIVE_T:
            summary["conclusive"] = True
            if difference > 0:
                summary["verdict"] = "optimizer used less energy per degree-day"
            else:
                summary["verdict"] = "optimizer used more energy per degree-day"
        elif enough:
            summary["verdict"] = "no measurable difference so far"
        return summary

    def days(self) -> list[dict[str, Any]]:
        """Per-day detail, for diagnostics download."""
        return [d.to_dict() for d in self._days]

    # ── Persistence ──────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "completed": self._completed,
            "stopped_reason": self._stopped_reason,
            "start_date": self._start_date.isoformat() if self._start_date else None,
            "period_days": self._period_days,
            "total_days": self._total_days,
            "days": [d.to_dict() for d in self._days],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ABTrial:
        trial = cls()
        trial._active = bool(data.get("active", False))
        trial._completed = bool(data.get("completed", False))
        trial._stopped_reason = data.get("stopped_reason")
        raw_start = data.get("start_date")
        if raw_start:
            try:
                trial._start_date = date.fromisoformat(str(raw_start))
            except (TypeError, ValueError):
                trial._start_date = None
                trial._active = False
        trial._period_days = max(
            MIN_PERIOD_DAYS,
            min(MAX_PERIOD_DAYS, int(data.get("period_days", DEFAULT_PERIOD_DAYS))),
        )
        trial._total_days = max(
            MIN_TOTAL_DAYS,
            min(MAX_TOTAL_DAYS, int(data.get("total_days", DEFAULT_TOTAL_DAYS))),
        )
        for raw in data.get("days", []) or []:
            try:
                trial._days.append(TrialDay.from_dict(raw))
            except (KeyError, TypeError, ValueError):
                _LOGGER.debug("Skipping malformed A/B trial day record")
        return trial
