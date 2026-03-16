"""Performance profiler — builds Beestat-equivalent lookup tables from live data.

Accumulates observed HVAC performance (indoor °F/hr change vs outdoor temp)
into binned lookup tables for four modes: compressor cooling (cool_1),
compressor heating (heat_1), auxiliary heat (auxiliary_heat_1), and passive
drift (resist).

Over weeks/months of operation, this produces the same temperature profile
that Beestat generates from Ecobee data — but from any thermostat via
Home Assistant.  The profiler output is directly consumable by PerformanceModel,
replacing the hardcoded linear COP degradation constants (ALPHA_COOL/ALPHA_HEAT)
with measured nonlinear reality.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_LOGGER = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────
MIN_SAMPLES_PER_BIN = 6         # 30 min at a given outdoor temp
MIN_BINS_FOR_TRENDLINE = 5      # distinct temps for meaningful trendline
MIN_TOTAL_HOURS = 48.0          # total data before considering authoritative
OUTLIER_DELTA_THRESHOLD = 15.0  # max plausible °F/hr
OUTLIER_TEMP_CHANGE = 2.0       # max plausible °F change in one 5-min interval
INTERVAL_TOLERANCE = 0.5        # reject if interval deviates >50% from expected

# Expected outdoor temp ranges per mode (for confidence scoring)
EXPECTED_RANGES: dict[str, tuple[int, int]] = {
    "cool_1": (65, 100),
    "heat_1": (0, 55),
    "auxiliary_heat_1": (0, 35),
    "resist": (20, 90),
}

MODES = ("cool_1", "heat_1", "auxiliary_heat_1", "resist")


@dataclass
class BinAccumulator:
    """Running statistics for one (mode, outdoor_temp) bin."""

    sum_delta: float = 0.0
    sum_sq_delta: float = 0.0
    count: int = 0
    sum_solar: float = 0.0
    sum_sq_solar: float = 0.0

    @property
    def mean_delta(self) -> float:
        return self.sum_delta / self.count if self.count else 0.0

    @property
    def std_delta(self) -> float:
        if self.count < 2:
            return 0.0
        variance = (self.sum_sq_delta / self.count) - (self.mean_delta ** 2)
        return math.sqrt(max(0.0, variance))

    def add(self, delta: float, solar: float | None = None) -> None:
        self.sum_delta += delta
        self.sum_sq_delta += delta * delta
        self.count += 1
        if solar is not None:
            self.sum_solar += solar
            self.sum_sq_solar += solar * solar


class PerformanceProfiler:
    """Accumulates live HVAC performance into Beestat-equivalent lookup tables.

    Usage:
        1. Call record_observation() every coordinator update cycle (5 min)
        2. Check confidence() to see if enough data has been collected
        3. Call to_performance_model() to get a PerformanceModel backed by
           measured data (or None if insufficient)
    """

    def __init__(self, expected_interval_minutes: float = 5.0) -> None:
        self._bins: dict[str, dict[int, BinAccumulator]] = {
            m: {} for m in MODES
        }
        self._expected_interval = expected_interval_minutes
        self._previous_indoor_temp: float | None = None
        self._previous_timestamp: datetime | None = None
        self._total_observations: int = 0

    # ── Recording ─────────────────────────────────────────────────────

    def record_observation(
        self,
        indoor_temp: float,
        outdoor_temp: float,
        hvac_action: str | None,
        hvac_mode: str,
        aux_heat_active: bool = False,
        solar_irradiance: float | None = None,
        now: datetime | None = None,
    ) -> None:
        """Record a single observation from the coordinator update cycle.

        Args:
            indoor_temp: Current indoor temperature (°F).
            outdoor_temp: Current outdoor temperature (°F).
            hvac_action: Climate entity hvac_action ("cooling", "heating", "idle", etc).
            hvac_mode: Climate entity hvac_mode ("cool", "heat", "heat_cool", "off").
            aux_heat_active: Whether auxiliary/emergency heat is running.
            solar_irradiance: Solar irradiance (W/m²), if available.
            now: Current timestamp (UTC).
        """
        if now is None:
            now = datetime.now(timezone.utc)

        # Gate: discard when HVAC is off entirely
        if hvac_mode == "off":
            _LOGGER.debug("Profiler: skipped — hvac_mode is 'off'")
            self._previous_indoor_temp = indoor_temp
            self._previous_timestamp = now
            return

        # Need a previous reading to compute delta
        if self._previous_indoor_temp is None or self._previous_timestamp is None:
            _LOGGER.debug("Profiler: skipped — no previous reading (first observation)")
            self._previous_indoor_temp = indoor_temp
            self._previous_timestamp = now
            return

        # Compute interval
        interval_seconds = (now - self._previous_timestamp).total_seconds()
        interval_minutes = interval_seconds / 60.0

        # Reject bad intervals (restarts, gaps, etc.)
        min_interval = self._expected_interval * (1 - INTERVAL_TOLERANCE)
        max_interval = self._expected_interval * (1 + INTERVAL_TOLERANCE)
        if interval_minutes < min_interval or interval_minutes > max_interval:
            _LOGGER.debug(
                "Profiler: skipped — interval %.1f min outside [%.1f, %.1f]",
                interval_minutes, min_interval, max_interval,
            )
            self._previous_indoor_temp = indoor_temp
            self._previous_timestamp = now
            return

        # Compute delta
        temp_change = indoor_temp - self._previous_indoor_temp
        delta_f_per_hr = temp_change / (interval_minutes / 60.0)

        # Outlier rejection
        if abs(temp_change) > OUTLIER_TEMP_CHANGE:
            _LOGGER.debug(
                "Profiler: skipped — temp change %.2f°F exceeds threshold",
                temp_change,
            )
            self._previous_indoor_temp = indoor_temp
            self._previous_timestamp = now
            return
        if abs(delta_f_per_hr) > OUTLIER_DELTA_THRESHOLD:
            _LOGGER.debug(
                "Profiler: skipped — delta rate %.1f°F/hr exceeds threshold",
                delta_f_per_hr,
            )
            self._previous_indoor_temp = indoor_temp
            self._previous_timestamp = now
            return

        # Classify mode
        mode = self._classify_mode(hvac_action, hvac_mode, aux_heat_active)

        if mode is not None:
            temp_bin = round(outdoor_temp)
            if temp_bin not in self._bins[mode]:
                self._bins[mode][temp_bin] = BinAccumulator()
            self._bins[mode][temp_bin].add(delta_f_per_hr, solar_irradiance)
            self._total_observations += 1

        self._previous_indoor_temp = indoor_temp
        self._previous_timestamp = now

    @staticmethod
    def _classify_mode(
        hvac_action: str | None,
        hvac_mode: str,
        aux_heat_active: bool,
    ) -> str | None:
        """Classify observation into one of the four tracked modes."""
        if hvac_action == "cooling":
            return "cool_1"

        if hvac_action in ("heating", "aux_heating", "emergency_heating"):
            if aux_heat_active or hvac_action in ("aux_heating", "emergency_heating"):
                return "auxiliary_heat_1"
            return "heat_1"

        # idle, None, or any unrecognized action with HVAC not off → passive drift
        if hvac_mode != "off":
            return "resist"

        return None

    # ── Output ────────────────────────────────────────────────────────

    def to_beestat_format(self) -> dict[str, Any]:
        """Export profiler data in Beestat-compatible format.

        Produces the same dict structure consumed by PerformanceModel.__init__().
        """
        temperature: dict[str, Any] = {}
        balance_points: dict[str, float] = {}

        for mode in MODES:
            bins = self._bins[mode]
            qualified = {
                t: acc for t, acc in bins.items()
                if acc.count >= MIN_SAMPLES_PER_BIN
            }

            if not qualified:
                temperature[mode] = None
                continue

            deltas = {
                str(t): round(acc.mean_delta, 2)
                for t, acc in sorted(qualified.items())
            }
            durations = {
                str(t): round(acc.count * self._expected_interval / 60.0, 1)
                for t, acc in sorted(qualified.items())
            }
            trendline = self._fit_trendline(qualified)

            temperature[mode] = {
                "deltas": deltas,
                "durations": durations,
                "linear_trendline": trendline,
            }

            # Balance points from resist and heat_1 trendlines
            if mode in ("resist", "heat_1") and trendline["slope"] != 0:
                bp = -trendline["intercept"] / trendline["slope"]
                # Sanity check: balance point should be in a reasonable range
                if -20 <= bp <= 120:
                    balance_points[mode] = round(bp, 1)

        return {
            "temperature": temperature,
            "balance_point": balance_points,
            "differential": {"cool": 1.0, "heat": 1.0},
            "setpoint": {"cool": 74.0, "heat": 68.0},
        }

    def to_performance_model(self):
        """Build a PerformanceModel from accumulated data.

        Returns None if insufficient data for the required modes (cool_1 or
        heat_1 and resist).
        """
        # Need at least resist + one active mode
        has_cool = self.has_sufficient_data("cool_1")
        has_heat = self.has_sufficient_data("heat_1")
        has_resist = self.has_sufficient_data("resist")

        if not has_resist or not (has_cool or has_heat):
            return None

        # Lazy import to avoid circular dependency
        from ..engine.performance_model import PerformanceModel

        data = self.to_beestat_format()

        # PerformanceModel requires cool_1, heat_1, and resist to be non-None.
        # Fill missing modes from conservative defaults.
        defaults_data = None
        for required_mode in ("cool_1", "heat_1", "resist"):
            if data["temperature"][required_mode] is None:
                if defaults_data is None:
                    defaults_data = PerformanceModel.from_defaults()._raw
                data["temperature"][required_mode] = defaults_data["temperature"][required_mode]

        return PerformanceModel(data)

    # ── Trendline fitting ─────────────────────────────────────────────

    @staticmethod
    def _fit_trendline(
        bins: dict[int, BinAccumulator],
    ) -> dict[str, float]:
        """Weighted OLS linear regression: delta = slope * outdoor_temp + intercept.

        Weights each bin by its observation count.
        """
        if len(bins) < 2:
            return {"slope": 0.0, "intercept": 0.0}

        # Weighted least squares (no numpy dependency)
        sum_w = 0.0
        sum_wx = 0.0
        sum_wy = 0.0
        sum_wxx = 0.0
        sum_wxy = 0.0

        for temp, acc in bins.items():
            if acc.count < MIN_SAMPLES_PER_BIN:
                continue
            w = acc.count
            y = acc.mean_delta
            sum_w += w
            sum_wx += w * temp
            sum_wy += w * y
            sum_wxx += w * temp * temp
            sum_wxy += w * temp * y

        if sum_w == 0:
            return {"slope": 0.0, "intercept": 0.0}

        denom = sum_w * sum_wxx - sum_wx * sum_wx
        if abs(denom) < 1e-10:
            return {"slope": 0.0, "intercept": sum_wy / sum_w if sum_w else 0.0}

        slope = (sum_w * sum_wxy - sum_wx * sum_wy) / denom
        intercept = (sum_wy - slope * sum_wx) / sum_w

        return {"slope": round(slope, 6), "intercept": round(intercept, 4)}

    # ── Confidence ────────────────────────────────────────────────────

    def confidence(self, mode: str | None = None) -> float:
        """Confidence score (0.0 to 1.0).

        If mode is specified, returns confidence for that mode only.
        Otherwise returns the minimum confidence across modes that have data,
        or 0.0 if no modes have any data.
        """
        if mode is not None:
            return self._mode_confidence(mode)

        # Overall: min across modes that have any data
        confidences = []
        for m in MODES:
            if self._bins[m]:
                confidences.append(self._mode_confidence(m))

        if not confidences:
            return 0.0
        return min(confidences)

    def _mode_confidence(self, mode: str) -> float:
        """Confidence for a single mode.

        Geometric mean of:
        1. Coverage: fraction of expected temp range with sufficient data
        2. Depth: min(total_mode_hours / MIN_TOTAL_HOURS, 1.0)
        3. Quality: fraction of populated bins with count >= MIN_SAMPLES_PER_BIN
        """
        bins = self._bins.get(mode, {})
        if not bins:
            return 0.0

        expected_lo, expected_hi = EXPECTED_RANGES.get(mode, (20, 90))
        expected_span = expected_hi - expected_lo

        # Coverage: how much of the expected range has qualified bins
        qualified_temps = [
            t for t, acc in bins.items()
            if acc.count >= MIN_SAMPLES_PER_BIN and expected_lo <= t <= expected_hi
        ]
        if qualified_temps and expected_span > 0:
            observed_span = max(qualified_temps) - min(qualified_temps)
            coverage = min(1.0, observed_span / expected_span)
        else:
            coverage = 0.0

        # Depth: total observation hours
        total_obs = sum(acc.count for acc in bins.values())
        total_hours = total_obs * self._expected_interval / 60.0
        depth = min(1.0, total_hours / MIN_TOTAL_HOURS)

        # Quality: fraction of populated bins that are well-sampled
        populated = len(bins)
        qualified = sum(1 for acc in bins.values() if acc.count >= MIN_SAMPLES_PER_BIN)
        quality = qualified / populated if populated else 0.0

        # Geometric mean
        product = coverage * depth * quality
        if product <= 0:
            return 0.0
        return round(product ** (1.0 / 3.0), 3)

    def has_sufficient_data(self, mode: str) -> bool:
        """Whether a mode has enough data for reliable deltas and trendline."""
        bins = self._bins.get(mode, {})
        qualified = sum(
            1 for acc in bins.values() if acc.count >= MIN_SAMPLES_PER_BIN
        )
        return qualified >= MIN_BINS_FOR_TRENDLINE

    @property
    def total_observations(self) -> int:
        return self._total_observations

    def bin_coverage(self, mode: str) -> dict[str, Any]:
        """Diagnostic: which outdoor temps have data and how much."""
        bins = self._bins.get(mode, {})
        return {
            "bins": {
                t: {"count": acc.count, "mean_delta": round(acc.mean_delta, 2)}
                for t, acc in sorted(bins.items())
            },
            "qualified_bins": sum(
                1 for acc in bins.values() if acc.count >= MIN_SAMPLES_PER_BIN
            ),
            "total_observations": sum(acc.count for acc in bins.values()),
        }

    # ── Persistence ───────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialize for HA Store persistence."""
        bins_data: dict[str, dict[str, dict[str, float]]] = {}
        for mode in MODES:
            bins_data[mode] = {}
            for temp, acc in self._bins[mode].items():
                bins_data[mode][str(temp)] = {
                    "sum_delta": acc.sum_delta,
                    "sum_sq_delta": acc.sum_sq_delta,
                    "count": acc.count,
                    "sum_solar": acc.sum_solar,
                    "sum_sq_solar": acc.sum_sq_solar,
                }

        return {
            "bins": bins_data,
            "total_observations": self._total_observations,
            "expected_interval": self._expected_interval,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PerformanceProfiler:
        """Restore from persisted data."""
        profiler = cls(
            expected_interval_minutes=data.get("expected_interval", 5.0),
        )
        profiler._total_observations = data.get("total_observations", 0)

        for mode in MODES:
            mode_bins = data.get("bins", {}).get(mode, {})
            for temp_str, acc_data in mode_bins.items():
                try:
                    temp = int(temp_str)
                    acc = BinAccumulator(
                        sum_delta=acc_data["sum_delta"],
                        sum_sq_delta=acc_data["sum_sq_delta"],
                        count=acc_data["count"],
                        sum_solar=acc_data.get("sum_solar", 0.0),
                        sum_sq_solar=acc_data.get("sum_sq_solar", 0.0),
                    )
                    profiler._bins[mode][temp] = acc
                except (KeyError, ValueError):
                    continue

        return profiler
