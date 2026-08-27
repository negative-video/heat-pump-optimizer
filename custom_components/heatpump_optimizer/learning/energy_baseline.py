"""Weather-normalized energy baseline — the honest savings denominator.

The counterfactual digital twin answers "what would my thermal model have done
under the old schedule?".  That is a model-vs-model comparison: every error in
the performance model appears on both sides of the subtraction, where it can
cancel or compound invisibly.  It cannot be audited against a meter.

This module answers a different, checkable question:

    "How much energy does this house use per degree-day when the optimizer is
     NOT running, and how much does it use per degree-day when it is?"

Both halves are metered.  The only model involved is a two-slope linear
regression the user can verify by hand:

    expected_kwh(day) = kwh_per_cdd * CDD + kwh_per_hdd * HDD + base_kwh

Degree-days use the standard 65 F base and the day's mean outdoor temperature,
matching the convention Beestat and utilities use, so a `runtime_per_degree_day`
figure from an external profile can seed the slopes before enough local days
have been observed.

Day classification comes from the optimizer's own control state, not from a
model: a day where the optimizer never wrote a setpoint is a baseline day, a
day where it controlled throughout is an optimized day, and anything in between
is discarded rather than fudged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Standard degree-day base temperature (F).  Utilities, Beestat, and NOAA all
# use 65 F, so slopes learned here are comparable with published figures.
DEGREE_DAY_BASE_F = 65.0

MINUTES_PER_DAY = 1440.0

# A day only enters the regression when the sensors covered most of it.
MIN_DAY_COVERAGE_MINUTES = 1200.0  # 20 h of 24
MIN_TEMP_COVERAGE_FRACTION = 0.8

# A day informs a slope only when the weather actually asked for that mode.
MIN_DEGREE_DAYS_FOR_FIT = 1.0

# Fit requirements
MIN_BASELINE_DAYS = 5  # per-mode minimum before a measured slope is published
MIN_DAYS_FOR_INTERCEPT = 10  # below this, fit through the origin (more stable)

# Day labelling thresholds (fraction of the day the optimizer was controlling)
OPTIMIZED_DAY_THRESHOLD = 0.9
BASELINE_DAY_THRESHOLD = 0.1

# Rolling history retained (persisted; ~2 years of daily records is tiny)
MAX_DAY_RECORDS = 730

LABEL_BASELINE = "baseline"
LABEL_OPTIMIZED = "optimized"
LABEL_MIXED = "mixed"

SOURCE_NONE = "none"
SOURCE_PROFILE_SEED = "profile_seed"
SOURCE_MEASURED = "measured"


@dataclass
class DayEnergyRecord:
    """One calendar day of metered HVAC energy alongside its weather."""

    date: str  # "2026-07-04" (local calendar day)
    kwh: float
    mean_outdoor_f: float
    cdd: float
    hdd: float
    covered_minutes: float
    temp_covered_minutes: float
    optimizer_fraction: float
    measured_fraction: float  # fraction of energy from a real power sensor

    @property
    def label(self) -> str:
        """Which population this day belongs to."""
        if self.optimizer_fraction >= OPTIMIZED_DAY_THRESHOLD:
            return LABEL_OPTIMIZED
        if self.optimizer_fraction <= BASELINE_DAY_THRESHOLD:
            return LABEL_BASELINE
        return LABEL_MIXED

    @property
    def degree_days(self) -> float:
        """Total degree-days, either side of the base temperature."""
        return self.cdd + self.hdd

    @property
    def kwh_per_degree_day(self) -> float | None:
        """Weather-normalized daily intensity, or None on a mild day."""
        dd = self.degree_days
        if dd < MIN_DEGREE_DAYS_FOR_FIT:
            return None
        return self.kwh / dd

    @property
    def usable(self) -> bool:
        """Whether sensor coverage was good enough to trust this day."""
        if self.covered_minutes < MIN_DAY_COVERAGE_MINUTES:
            return False
        if self.covered_minutes <= 0:
            return False
        return (
            self.temp_covered_minutes / self.covered_minutes
        ) >= MIN_TEMP_COVERAGE_FRACTION

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "kwh": round(self.kwh, 4),
            "mean_outdoor_f": round(self.mean_outdoor_f, 2),
            "cdd": round(self.cdd, 3),
            "hdd": round(self.hdd, 3),
            "covered_minutes": round(self.covered_minutes, 1),
            "temp_covered_minutes": round(self.temp_covered_minutes, 1),
            "optimizer_fraction": round(self.optimizer_fraction, 4),
            "measured_fraction": round(self.measured_fraction, 4),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DayEnergyRecord:
        return cls(
            date=str(data["date"]),
            kwh=float(data.get("kwh", 0.0)),
            mean_outdoor_f=float(data.get("mean_outdoor_f", DEGREE_DAY_BASE_F)),
            cdd=float(data.get("cdd", 0.0)),
            hdd=float(data.get("hdd", 0.0)),
            covered_minutes=float(data.get("covered_minutes", 0.0)),
            temp_covered_minutes=float(data.get("temp_covered_minutes", 0.0)),
            optimizer_fraction=float(data.get("optimizer_fraction", 0.0)),
            measured_fraction=float(data.get("measured_fraction", 0.0)),
        )


@dataclass
class BaselineFit:
    """The learned weather-normalization slopes and how much to trust them."""

    kwh_per_cdd: float = 0.0
    kwh_per_hdd: float = 0.0
    base_kwh: float = 0.0
    source: str = SOURCE_NONE
    cooling_days: int = 0
    heating_days: int = 0
    # Mean absolute residual of the fit, in kWh/day.  This is the honest
    # uncertainty on any single day's baseline number.
    residual_kwh: float | None = None

    @property
    def is_usable(self) -> bool:
        return self.source != SOURCE_NONE and (
            self.kwh_per_cdd > 0.0 or self.kwh_per_hdd > 0.0
        )

    def expected_kwh(self, cdd: float, hdd: float) -> float:
        """Baseline energy for a day with the given degree-days."""
        return max(
            0.0,
            self.kwh_per_cdd * max(0.0, cdd)
            + self.kwh_per_hdd * max(0.0, hdd)
            + self.base_kwh,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kwh_per_cdd": self.kwh_per_cdd,
            "kwh_per_hdd": self.kwh_per_hdd,
            "base_kwh": self.base_kwh,
            "source": self.source,
            "cooling_days": self.cooling_days,
            "heating_days": self.heating_days,
            "residual_kwh": self.residual_kwh,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BaselineFit:
        return cls(
            kwh_per_cdd=float(data.get("kwh_per_cdd", 0.0)),
            kwh_per_hdd=float(data.get("kwh_per_hdd", 0.0)),
            base_kwh=float(data.get("base_kwh", 0.0)),
            source=str(data.get("source", SOURCE_NONE)),
            cooling_days=int(data.get("cooling_days", 0)),
            heating_days=int(data.get("heating_days", 0)),
            residual_kwh=(
                float(data["residual_kwh"])
                if data.get("residual_kwh") is not None
                else None
            ),
        )


def _fit_slope(
    points: list[tuple[float, float]],
    allow_intercept: bool,
) -> tuple[float, float, float | None]:
    """Least-squares fit of kwh = slope * degree_days (+ intercept).

    Returns (slope, intercept, mean_absolute_residual).  Slope is clamped
    non-negative: a house cannot use less energy the harder the weather gets,
    and a negative slope only ever means the sample is too small.
    """
    n = len(points)
    if n == 0:
        return 0.0, 0.0, None

    if not allow_intercept or n < 2:
        # Through the origin: slope = sum(x*y) / sum(x^2)
        sxx = sum(x * x for x, _ in points)
        if sxx <= 0:
            return 0.0, 0.0, None
        slope = sum(x * y for x, y in points) / sxx
        intercept = 0.0
    else:
        sx = sum(x for x, _ in points)
        sy = sum(y for _, y in points)
        sxx = sum(x * x for x, _ in points)
        sxy = sum(x * y for x, y in points)
        denom = n * sxx - sx * sx
        if abs(denom) < 1e-9:
            sxx_only = sxx
            if sxx_only <= 0:
                return 0.0, 0.0, None
            slope = sxy / sxx_only
            intercept = 0.0
        else:
            slope = (n * sxy - sx * sy) / denom
            intercept = (sy - slope * sx) / n

    if slope < 0.0:
        # Degenerate sample. Fall back to the origin fit, still clamped.
        sxx = sum(x * x for x, _ in points)
        slope = max(0.0, sum(x * y for x, y in points) / sxx) if sxx > 0 else 0.0
        intercept = 0.0
    if intercept < 0.0:
        intercept = 0.0

    residual = sum(abs(y - (slope * x + intercept)) for x, y in points) / n
    return slope, intercept, residual


class EnergyBaseline:
    """Accumulates metered HVAC energy per degree-day and fits the baseline.

    Usage:
        1. Call record_interval() every coordinator cycle, whether or not the
           optimizer is currently controlling.  It must keep running while the
           optimizer is paused: paused days are exactly the baseline days.
        2. Read fit() for the learned slopes and today_estimate() for the
           running measured-vs-baseline comparison.
    """

    def __init__(self) -> None:
        self._days: list[DayEnergyRecord] = []

        # Current (incomplete) day accumulator
        self._current_date: str | None = None
        self._day_kwh: float = 0.0
        self._day_measured_kwh: float = 0.0
        self._day_minutes: float = 0.0
        self._day_temp_minutes: float = 0.0
        self._day_temp_weighted_sum: float = 0.0
        self._day_optimizer_minutes: float = 0.0

        # Seed slopes from an external profile (used until measured days exist)
        self._seed: BaselineFit | None = None

        # Cached fit, invalidated whenever a day closes
        self._cached_fit: BaselineFit | None = None

        # Callback invoked with each completed DayEnergyRecord (A/B trial hook)
        self._day_listeners: list[Any] = []

    # ── Wiring ───────────────────────────────────────────────────────

    def add_day_listener(self, callback: Any) -> None:
        """Register a callable invoked with every completed DayEnergyRecord."""
        if callback not in self._day_listeners:
            self._day_listeners.append(callback)

    def seed_from_profile(
        self,
        runtime_per_degree_day: dict[str, Any],
        cooling_watts: float,
        heating_watts: float,
        aux_heat_watts: float | None = None,
    ) -> bool:
        """Seed the slopes from a thermostat profile's runtime per degree-day.

        `runtime_per_degree_day` is the Beestat-format mapping of stage name to
        *minutes of runtime per degree-day* (e.g. {"cool_1": 46.55,
        "heat_1": 11.35, "auxiliary_heat_1": 2.08}).  Multiplying by the stage's
        power draw converts it to kWh per degree-day.

        Returns True when a usable seed was produced.  The seed is only ever
        used while there is not enough measured data for a real fit.
        """
        if not isinstance(runtime_per_degree_day, dict):
            return False

        def _minutes(key: str) -> float:
            value = runtime_per_degree_day.get(key)
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                return 0.0

        cool_kwh = _minutes("cool_1") / 60.0 * (cooling_watts / 1000.0)
        cool_kwh += _minutes("cool_2") / 60.0 * (cooling_watts / 1000.0)
        heat_kwh = _minutes("heat_1") / 60.0 * (heating_watts / 1000.0)
        heat_kwh += _minutes("heat_2") / 60.0 * (heating_watts / 1000.0)
        if aux_heat_watts:
            heat_kwh += _minutes("auxiliary_heat_1") / 60.0 * (aux_heat_watts / 1000.0)
            heat_kwh += _minutes("auxiliary_heat_2") / 60.0 * (aux_heat_watts / 1000.0)

        if cool_kwh <= 0.0 and heat_kwh <= 0.0:
            return False

        self._seed = BaselineFit(
            kwh_per_cdd=cool_kwh,
            kwh_per_hdd=heat_kwh,
            base_kwh=0.0,
            source=SOURCE_PROFILE_SEED,
        )
        self._cached_fit = None
        _LOGGER.info(
            "Energy baseline seeded from profile: %.3f kWh/CDD, %.3f kWh/HDD",
            cool_kwh,
            heat_kwh,
        )
        return True

    # ── Accumulation ─────────────────────────────────────────────────

    def record_interval(
        self,
        now: datetime,
        interval_minutes: float,
        power_watts: float | None,
        outdoor_temp: float | None,
        optimizer_active: bool,
        power_measured: bool = False,
    ) -> None:
        """Accumulate one coordinator cycle into the current day.

        Args:
            now: Local-time timestamp (the calendar day comes from this).
            interval_minutes: Length of the cycle.
            power_watts: HVAC power draw for the cycle.  Integrated over the
                whole cycle including idle time, so the result is metered HVAC
                energy rather than compressor-only energy.
            outdoor_temp: Outdoor temperature (F) for the degree-day mean.
            optimizer_active: Whether the optimizer was actively controlling
                the thermostat during this cycle.
            power_measured: True when power_watts came from a real power sensor
                rather than a rated-watts estimate.
        """
        if interval_minutes <= 0:
            return

        date_key = now.strftime("%Y-%m-%d")
        if self._current_date is not None and date_key != self._current_date:
            self._close_day()
        if self._current_date is None:
            self._start_day(date_key)

        hours = interval_minutes / 60.0
        kwh = max(0.0, (power_watts or 0.0)) * hours / 1000.0

        self._day_kwh += kwh
        if power_measured:
            self._day_measured_kwh += kwh
        self._day_minutes += interval_minutes
        if outdoor_temp is not None:
            self._day_temp_minutes += interval_minutes
            self._day_temp_weighted_sum += outdoor_temp * interval_minutes
        if optimizer_active:
            self._day_optimizer_minutes += interval_minutes

    def _start_day(self, date_key: str) -> None:
        self._current_date = date_key
        self._day_kwh = 0.0
        self._day_measured_kwh = 0.0
        self._day_minutes = 0.0
        self._day_temp_minutes = 0.0
        self._day_temp_weighted_sum = 0.0
        self._day_optimizer_minutes = 0.0

    def _close_day(self) -> None:
        """Finalize the current day into a DayEnergyRecord."""
        if self._current_date is None or self._day_minutes <= 0:
            self._current_date = None
            return

        mean_temp = (
            self._day_temp_weighted_sum / self._day_temp_minutes
            if self._day_temp_minutes > 0
            else DEGREE_DAY_BASE_F
        )
        record = DayEnergyRecord(
            date=self._current_date,
            kwh=self._day_kwh,
            mean_outdoor_f=mean_temp,
            cdd=max(0.0, mean_temp - DEGREE_DAY_BASE_F),
            hdd=max(0.0, DEGREE_DAY_BASE_F - mean_temp),
            covered_minutes=self._day_minutes,
            temp_covered_minutes=self._day_temp_minutes,
            optimizer_fraction=self._day_optimizer_minutes / self._day_minutes,
            measured_fraction=(
                self._day_measured_kwh / self._day_kwh if self._day_kwh > 0 else 0.0
            ),
        )

        self._days.append(record)
        if len(self._days) > MAX_DAY_RECORDS:
            del self._days[: len(self._days) - MAX_DAY_RECORDS]

        self._current_date = None
        self._cached_fit = None

        _LOGGER.debug(
            "Energy baseline day closed: %s %s kwh=%.2f CDD=%.1f HDD=%.1f "
            "coverage=%.0f min",
            record.date,
            record.label,
            record.kwh,
            record.cdd,
            record.hdd,
            record.covered_minutes,
        )

        for listener in list(self._day_listeners):
            try:
                listener(record)
            except Exception:  # noqa: BLE001 - a listener must never break accounting
                _LOGGER.warning("Energy baseline day listener failed", exc_info=True)

    def force_close_day(self) -> None:
        """Close the in-progress day immediately (used on shutdown/reset)."""
        self._close_day()

    # ── Fitting ──────────────────────────────────────────────────────

    @property
    def days(self) -> list[DayEnergyRecord]:
        """All completed day records, oldest first."""
        return list(self._days)

    def population(self, label: str) -> list[DayEnergyRecord]:
        """Usable completed days belonging to one population."""
        return [d for d in self._days if d.usable and d.label == label]

    def fit(self) -> BaselineFit:
        """Learned baseline slopes, preferring measured days over the seed."""
        if self._cached_fit is not None:
            return self._cached_fit

        baseline_days = self.population(LABEL_BASELINE)
        cooling = [
            (d.cdd, d.kwh)
            for d in baseline_days
            if d.cdd >= MIN_DEGREE_DAYS_FOR_FIT
        ]
        heating = [
            (d.hdd, d.kwh)
            for d in baseline_days
            if d.hdd >= MIN_DEGREE_DAYS_FOR_FIT
        ]

        seed = self._seed or BaselineFit()
        kwh_per_cdd = seed.kwh_per_cdd
        kwh_per_hdd = seed.kwh_per_hdd
        base_kwh = 0.0
        residuals: list[float] = []
        measured = False

        if len(cooling) >= MIN_BASELINE_DAYS:
            slope, intercept, residual = _fit_slope(
                cooling, allow_intercept=len(cooling) >= MIN_DAYS_FOR_INTERCEPT
            )
            kwh_per_cdd = slope
            base_kwh = max(base_kwh, intercept)
            if residual is not None:
                residuals.append(residual)
            measured = True

        if len(heating) >= MIN_BASELINE_DAYS:
            slope, intercept, residual = _fit_slope(
                heating, allow_intercept=len(heating) >= MIN_DAYS_FOR_INTERCEPT
            )
            kwh_per_hdd = slope
            base_kwh = max(base_kwh, intercept)
            if residual is not None:
                residuals.append(residual)
            measured = True

        if measured:
            source = SOURCE_MEASURED
        elif self._seed is not None:
            source = SOURCE_PROFILE_SEED
        else:
            source = SOURCE_NONE

        result = BaselineFit(
            kwh_per_cdd=kwh_per_cdd,
            kwh_per_hdd=kwh_per_hdd,
            base_kwh=base_kwh,
            source=source,
            cooling_days=len(cooling),
            heating_days=len(heating),
            residual_kwh=(sum(residuals) / len(residuals)) if residuals else None,
        )
        self._cached_fit = result
        return result

    # ── Reporting ────────────────────────────────────────────────────

    def today_partial(self) -> dict[str, float]:
        """Degree-days and metered energy for the in-progress day so far.

        Degree-days are scaled by the fraction of the day observed, so the
        comparison against the baseline slope stays dimensionally honest at
        any hour: at 06:00 both sides describe a quarter of a day.
        """
        elapsed = (
            min(1.0, self._day_minutes / MINUTES_PER_DAY)
            if self._day_minutes > 0
            else 0.0
        )
        mean_temp = (
            self._day_temp_weighted_sum / self._day_temp_minutes
            if self._day_temp_minutes > 0
            else DEGREE_DAY_BASE_F
        )
        return {
            "elapsed_fraction": elapsed,
            "mean_outdoor_f": mean_temp,
            "cdd": max(0.0, mean_temp - DEGREE_DAY_BASE_F) * elapsed,
            "hdd": max(0.0, DEGREE_DAY_BASE_F - mean_temp) * elapsed,
            "kwh": self._day_kwh,
            "measured_fraction": (
                self._day_measured_kwh / self._day_kwh if self._day_kwh > 0 else 0.0
            ),
            "optimizer_fraction": (
                self._day_optimizer_minutes / self._day_minutes
                if self._day_minutes > 0
                else 0.0
            ),
        }

    def today_estimate(self) -> dict[str, Any] | None:
        """Running measured-vs-baseline comparison for the current day.

        Returns None while there is no usable baseline fit, rather than
        inventing a number.
        """
        fit = self.fit()
        if not fit.is_usable:
            return None
        partial = self.today_partial()
        # Degree-days are already scaled to the elapsed fraction; scale the
        # non-weather intercept the same way so both sides cover the same span.
        baseline_kwh = max(
            0.0,
            fit.kwh_per_cdd * partial["cdd"]
            + fit.kwh_per_hdd * partial["hdd"]
            + fit.base_kwh * partial["elapsed_fraction"],
        )
        actual_kwh = partial["kwh"]
        return {
            "baseline_kwh": baseline_kwh,
            "actual_kwh": actual_kwh,
            "saved_kwh": baseline_kwh - actual_kwh,
            "cdd": partial["cdd"],
            "hdd": partial["hdd"],
            "elapsed_fraction": partial["elapsed_fraction"],
            "measured_fraction": partial["measured_fraction"],
            "uncertainty_kwh": fit.residual_kwh,
            "source": fit.source,
        }

    def window_summary(self, days: int = 30) -> dict[str, Any] | None:
        """Measured vs baseline over the most recent completed optimized days.

        This is the trustworthy headline: whole days only, no partial-day
        extrapolation, and both sides in metered kWh.
        """
        fit = self.fit()
        if not fit.is_usable:
            return None
        optimized = [
            d
            for d in self.population(LABEL_OPTIMIZED)
            if d.degree_days >= MIN_DEGREE_DAYS_FOR_FIT
        ][-days:]
        if not optimized:
            return None

        actual = sum(d.kwh for d in optimized)
        baseline = sum(fit.expected_kwh(d.cdd, d.hdd) for d in optimized)
        measured_kwh = sum(d.kwh * d.measured_fraction for d in optimized)
        uncertainty = (
            fit.residual_kwh * len(optimized) if fit.residual_kwh is not None else None
        )
        return {
            "days": len(optimized),
            "actual_kwh": actual,
            "baseline_kwh": baseline,
            "saved_kwh": baseline - actual,
            "saved_pct": ((baseline - actual) / baseline * 100.0) if baseline > 0 else None,
            "uncertainty_kwh": uncertainty,
            "measured_fraction": (measured_kwh / actual) if actual > 0 else 0.0,
            "source": fit.source,
        }

    def diagnostics(self) -> dict[str, Any]:
        """Everything a user needs to audit the baseline by hand."""
        fit = self.fit()
        baseline_days = self.population(LABEL_BASELINE)
        optimized_days = self.population(LABEL_OPTIMIZED)
        return {
            "fit": fit.to_dict(),
            "baseline_days": len(baseline_days),
            "optimized_days": len(optimized_days),
            "mixed_days": sum(1 for d in self._days if d.label == LABEL_MIXED),
            "unusable_days": sum(1 for d in self._days if not d.usable),
            "degree_day_base_f": DEGREE_DAY_BASE_F,
            "days_until_measured_fit": max(
                0, MIN_BASELINE_DAYS - max(fit.cooling_days, fit.heating_days)
            ),
        }

    # ── Persistence ──────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "days": [d.to_dict() for d in self._days],
            "seed": self._seed.to_dict() if self._seed is not None else None,
            "current": {
                "date": self._current_date,
                "kwh": self._day_kwh,
                "measured_kwh": self._day_measured_kwh,
                "minutes": self._day_minutes,
                "temp_minutes": self._day_temp_minutes,
                "temp_weighted_sum": self._day_temp_weighted_sum,
                "optimizer_minutes": self._day_optimizer_minutes,
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EnergyBaseline:
        baseline = cls()
        for raw in data.get("days", []) or []:
            try:
                baseline._days.append(DayEnergyRecord.from_dict(raw))
            except (KeyError, TypeError, ValueError):
                _LOGGER.debug("Skipping malformed energy baseline day record")
        seed = data.get("seed")
        if seed:
            try:
                baseline._seed = BaselineFit.from_dict(seed)
            except (KeyError, TypeError, ValueError):
                baseline._seed = None
        current = data.get("current") or {}
        baseline._current_date = current.get("date")
        baseline._day_kwh = float(current.get("kwh", 0.0))
        baseline._day_measured_kwh = float(current.get("measured_kwh", 0.0))
        baseline._day_minutes = float(current.get("minutes", 0.0))
        baseline._day_temp_minutes = float(current.get("temp_minutes", 0.0))
        baseline._day_temp_weighted_sum = float(current.get("temp_weighted_sum", 0.0))
        baseline._day_optimizer_minutes = float(current.get("optimizer_minutes", 0.0))
        return baseline
