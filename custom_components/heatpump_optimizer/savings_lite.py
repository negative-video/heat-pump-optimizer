"""Lightweight savings tracker for lite/minimal setup mode.

Estimates energy savings by comparing the efficiency of hours when the HVAC
actually ran (under optimizer control) vs the average efficiency of all
managed hours. No counterfactual simulation needed.

If the optimizer consistently shifts runtime to hours with better efficiency
scores, the delta between "when we ran" and "when we could have run" is
a reasonable proxy for energy saved.
"""

from __future__ import annotations

from datetime import datetime, timezone


# EPA average CO2 intensity for US electricity grid (lbs CO2 per kWh)
EPA_AVG_CO2_LBS_PER_KWH = 0.855


class LiteSavingsTracker:
    """Tracks efficiency-weighted HVAC runtime for lite mode savings estimation."""

    def __init__(self, default_power_watts: float = 3600.0) -> None:
        self._default_power_watts = default_power_watts

        # Cumulative counters
        self._runtime_intervals: int = 0          # 5-min intervals HVAC was running
        self._managed_intervals: int = 0          # total 5-min intervals managed
        self._runtime_score_sum: float = 0.0      # sum of efficiency scores during runtime
        self._total_score_sum: float = 0.0        # sum of efficiency scores across all hours
        self._coast_intervals_today: int = 0      # today's coast phase intervals
        self._last_reset_date: str = ""           # ISO date of last daily reset

    def record_interval(
        self,
        now: datetime,
        hvac_running: bool,
        efficiency_score: float,
        phase: str,
        interval_minutes: float = 5.0,
        power_watts: float | None = None,
    ) -> None:
        """Record a single 5-minute interval.

        Args:
            now: Current timestamp
            hvac_running: Whether HVAC is actively running (heating/cooling)
            efficiency_score: Current hour's efficiency rating (0-100, higher=better)
            phase: Current optimizer phase (pre-cooling, coasting, etc.)
            interval_minutes: Update interval in minutes
            power_watts: Actual measured power (or None for default)
        """
        # Daily reset
        today = now.strftime("%Y-%m-%d")
        if today != self._last_reset_date:
            self._coast_intervals_today = 0
            self._last_reset_date = today

        self._managed_intervals += 1
        self._total_score_sum += efficiency_score

        if hvac_running:
            self._runtime_intervals += 1
            self._runtime_score_sum += efficiency_score

        if "coast" in phase.lower():
            self._coast_intervals_today += 1

    @property
    def efficiency_gain_pct(self) -> float | None:
        """Percentage efficiency gain vs naive (unoptimized) scheduling.

        Compares average efficiency score during HVAC runtime vs average
        across all managed hours. Positive = optimizer shifted to better hours.
        """
        if self._runtime_intervals < 12 or self._managed_intervals < 24:
            return None  # Need at least 1 hour of runtime and 2 hours managed

        avg_runtime = self._runtime_score_sum / self._runtime_intervals
        avg_all = self._total_score_sum / self._managed_intervals

        if avg_all <= 0:
            return None
        return round((avg_runtime / avg_all - 1.0) * 100, 1)

    @property
    def estimated_kwh_saved(self) -> float | None:
        """Estimated cumulative kWh saved by running during better hours."""
        gain = self.efficiency_gain_pct
        if gain is None or gain <= 0:
            return 0.0

        runtime_hours = self._runtime_intervals * 5.0 / 60.0
        power_kw = self._default_power_watts / 1000.0
        # If we ran X% more efficiently, we used X/(100+X) less energy
        ratio = gain / (100.0 + gain)
        return round(runtime_hours * power_kw * ratio, 2)

    @property
    def estimated_co2_avoided_lbs(self) -> float | None:
        """Estimated cumulative CO2 avoided in lbs."""
        kwh = self.estimated_kwh_saved
        if kwh is None:
            return None
        return round(kwh * EPA_AVG_CO2_LBS_PER_KWH, 2)

    @property
    def estimated_cost_saved(self) -> float | None:
        """Estimated cost saved (requires rate — returns None without one)."""
        # This is populated externally if a rate entity is configured
        return None

    @property
    def coast_hours_today(self) -> float:
        """Hours spent in coast phase today."""
        return round(self._coast_intervals_today * 5.0 / 60.0, 1)

    def to_dict(self) -> dict:
        """Serialize for persistence."""
        return {
            "runtime_intervals": self._runtime_intervals,
            "managed_intervals": self._managed_intervals,
            "runtime_score_sum": self._runtime_score_sum,
            "total_score_sum": self._total_score_sum,
            "coast_intervals_today": self._coast_intervals_today,
            "last_reset_date": self._last_reset_date,
            "default_power_watts": self._default_power_watts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> LiteSavingsTracker:
        """Restore from persisted data."""
        tracker = cls(
            default_power_watts=data.get("default_power_watts", 3600.0),
        )
        tracker._runtime_intervals = data.get("runtime_intervals", 0)
        tracker._managed_intervals = data.get("managed_intervals", 0)
        tracker._runtime_score_sum = data.get("runtime_score_sum", 0.0)
        tracker._total_score_sum = data.get("total_score_sum", 0.0)
        tracker._coast_intervals_today = data.get("coast_intervals_today", 0)
        tracker._last_reset_date = data.get("last_reset_date", "")
        return tracker
