"""Layer 1: Strategic Planner — periodic schedule optimization.

Manages the optimization lifecycle: when to re-optimize, mode detection,
shoulder day handling, and comfort range adjustments.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta, timezone

from ..adapters.occupancy import OccupancyAdapter, OccupancyMode
from ..engine.comfort import calculate_apparent_temperature
from ..engine.data_types import (
    ForecastPoint,
    OccupancyForecastPoint,
    OptimizationWeights,
    OptimizedSchedule,
)
from ..engine.optimizer import ScheduleOptimizer

_LOGGER = logging.getLogger(__name__)

# Shoulder day detection
SHOULDER_MARGIN_F = 2.0
MODE_SWITCH_HYSTERESIS_F = 5.0


@dataclass
class StrategicPlanner:
    """Decides when and how to re-optimize the HVAC schedule."""

    optimizer: ScheduleOptimizer
    resist_balance_point: float  # From performance model (~50.2°F)
    reoptimize_interval_hours: float = 4.0

    # Optional grey-box optimizer (LP-based, set by coordinator)
    greybox_optimizer: object | None = None
    sleep_config: dict | None = None
    _use_greybox: bool = False

    _last_optimization_time: datetime | None = None
    _last_forecast_snapshot: list[ForecastPoint] = field(default_factory=list)
    _current_schedule: OptimizedSchedule | None = None
    _current_mode: str | None = None
    _last_occupancy_hash: str = ""

    def should_reoptimize(
        self,
        new_forecast: list[ForecastPoint] | None = None,
        forecast_threshold_f: float = 5.0,
        force: bool = False,
        occupancy_timeline: list[OccupancyForecastPoint] | None = None,
    ) -> bool:
        """Determine whether a strategic re-optimization is needed."""
        if force:
            return True

        now = datetime.now(timezone.utc)

        # Never optimized
        if self._last_optimization_time is None:
            return True

        # Time-based
        elapsed_hrs = (now - self._last_optimization_time).total_seconds() / 3600
        if elapsed_hrs >= self.reoptimize_interval_hours:
            _LOGGER.debug("Re-optimize: %.1f hrs since last run", elapsed_hrs)
            return True

        # Forecast deviation
        if new_forecast and self._last_forecast_snapshot:
            if self._forecast_deviated(new_forecast, forecast_threshold_f):
                _LOGGER.debug("Re-optimize: forecast deviated beyond threshold")
                return True

        # Dynamic shoulder day / mode conflict detection
        if new_forecast and self._current_mode:
            new_mode = self.detect_mode(new_forecast)
            if new_mode != self._current_mode and new_mode != "off":
                _LOGGER.debug(
                    "Re-optimize: mode conflict detected (current=%s, forecast suggests=%s)",
                    self._current_mode, new_mode,
                )
                return True

        # Occupancy timeline changed
        if occupancy_timeline is not None:
            new_hash = self._hash_occupancy_timeline(occupancy_timeline)
            if new_hash != self._last_occupancy_hash:
                _LOGGER.debug("Re-optimize: occupancy timeline changed")
                return True

        return False

    def optimize(
        self,
        indoor_temp: float,
        forecast: list[ForecastPoint],
        comfort_cool: tuple[float, float],
        comfort_heat: tuple[float, float],
        humidity: float | None = None,
        humidity_correction: bool = False,
        occupancy_timeline: list[OccupancyForecastPoint] | None = None,
        people_home_count: int | None = None,
        indoor_humidity: float | None = None,
        appliance_btu: float = 0.0,
        aux_threshold_f: float | None = None,
        aux_heat_active: bool = False,
    ) -> OptimizedSchedule | None:
        """Run the optimizer and update internal state.

        Handles mode detection, shoulder days, humidity correction, and
        per-hour comfort stamping from the occupancy timeline.

        Returns the new schedule, or None if optimization is not needed/possible.
        """
        if not forecast:
            _LOGGER.warning("No forecast data — cannot optimize")
            return None

        # Determine mode
        mode = self.detect_mode(forecast)
        if mode == "off":
            _LOGGER.debug("Near balance point — no optimization needed")
            self._current_schedule = None
            self._current_mode = None
            return None

        # Select base HOME comfort range for this mode
        comfort = comfort_cool if mode == "cool" else comfort_heat

        # Apply humidity correction to comfort range
        if humidity is not None and humidity_correction:
            comfort = self._apply_humidity_correction(comfort, humidity, mode)

        # Check for shoulder day (needs both heat and cool)
        is_shoulder = self._is_shoulder_day(forecast, comfort_cool, comfort_heat)
        if is_shoulder:
            _LOGGER.info("Shoulder day detected — using conservative optimization")
            midpoint = (comfort[0] + comfort[1]) / 2
            comfort = (midpoint - 1.5, midpoint + 1.5)

        # Stamp per-hour comfort on forecast points from occupancy timeline
        if occupancy_timeline and not is_shoulder:
            self._stamp_per_hour_comfort(forecast, comfort, mode, occupancy_timeline)
            _LOGGER.debug(
                "Stamped per-hour comfort from occupancy timeline (%d segments)",
                len(occupancy_timeline),
            )

        # Run optimizer (grey-box LP or heuristic)
        try:
            if self._use_greybox and self.greybox_optimizer is not None:
                schedule = self.greybox_optimizer.optimize(
                    indoor_temp, forecast, comfort, mode,
                    people_home_count=people_home_count,
                    indoor_humidity=indoor_humidity,
                    appliance_btu=appliance_btu,
                    aux_threshold_f=aux_threshold_f,
                    aux_heat_active=aux_heat_active,
                )
            else:
                schedule = self.optimizer.optimize_setpoints(
                    indoor_temp, forecast, comfort, mode
                )
        except Exception:
            _LOGGER.error("Optimization failed", exc_info=True)
            # If grey-box failed, fall back to heuristic
            if self._use_greybox and self.greybox_optimizer is not None:
                _LOGGER.info("Falling back to heuristic optimizer")
                try:
                    schedule = self.optimizer.optimize_setpoints(
                        indoor_temp, forecast, comfort, mode
                    )
                except Exception:
                    _LOGGER.error("Heuristic fallback also failed", exc_info=True)
                    return None
            else:
                return None

        # Store state
        self._current_schedule = schedule
        self._current_mode = mode
        self._last_forecast_snapshot = list(forecast)
        self._last_optimization_time = datetime.now(timezone.utc)
        if occupancy_timeline is not None:
            self._last_occupancy_hash = self._hash_occupancy_timeline(
                occupancy_timeline
            )

        _LOGGER.info(
            "Optimization complete [%s]: baseline=%.1f min, optimized=%.1f min, "
            "savings=%.1f%%, violations=%d",
            mode,
            schedule.baseline_runtime_minutes,
            schedule.optimized_runtime_minutes,
            schedule.savings_pct,
            schedule.comfort_violations,
        )

        return schedule

    def detect_mode(self, forecast: list[ForecastPoint]) -> str:
        """Determine HVAC mode from near-term forecast temperatures.

        Returns "cool", "heat", or "off" (near balance point).

        Uses only the first 8 hours of the forecast for mode determination.
        The optimizer re-runs every 1-4 hours, so it only needs to pick the
        right mode for the immediate future.  Using the full 24-hour peak
        caused "cool" selection on shoulder mornings that need heat because
        a warm afternoon peak exceeded the balance point threshold.
        """
        if not forecast:
            return "off"

        # Near-term focus: commit to a mode for the next few hours only.
        near_term = forecast[:8] if len(forecast) > 8 else forecast
        temps = [pt.outdoor_temp for pt in near_term]
        avg = sum(temps) / len(temps)
        max_temp = max(temps)
        min_temp = min(temps)

        # Sanity-check balance point: if it drifted outside a reasonable
        # residential range (20-90°F), fall back to a safe default.
        bp = self.resist_balance_point
        if bp < 20 or bp > 90:
            _LOGGER.warning(
                "Balance point %.1f°F out of range, using default 50°F", bp,
            )
            bp = 50.0

        # Well above balance point -> cooling
        if avg > bp + MODE_SWITCH_HYSTERESIS_F:
            return "cool"

        # Well below balance point -> heating
        if avg < bp - MODE_SWITCH_HYSTERESIS_F:
            return "heat"

        # Near balance point -- use peak/trough to decide
        if max_temp > bp + 10:
            return "cool"
        if min_temp < bp - 10:
            return "heat"

        # Truly mild -- HVAC probably not needed
        return "off"

    def _is_shoulder_day(
        self,
        forecast: list[ForecastPoint],
        comfort_cool: tuple[float, float],
        comfort_heat: tuple[float, float],
    ) -> bool:
        """Detect a shoulder day that might need both heating and cooling.

        Also triggers on days where outdoor temps cross the balance point
        significantly (cold morning + warm afternoon), even if the
        afternoon isn't hot enough to need active cooling.  These days
        need conservative scheduling to avoid pre-heating during warm hours.
        """
        temps = [pt.outdoor_temp for pt in forecast]
        if not temps:
            return False

        max_temp = max(temps)
        min_temp = min(temps)

        needs_cool = max_temp > comfort_cool[1] + SHOULDER_MARGIN_F
        needs_heat = min_temp < comfort_heat[0] - SHOULDER_MARGIN_F

        if needs_cool and needs_heat:
            return True

        # Balance-point crossing: cold morning + warm afternoon where
        # passive drift reverses direction during the day.
        bp = self.resist_balance_point
        crosses_balance = min_temp < bp - 5 and max_temp > bp + 5
        return crosses_balance

    @staticmethod
    def _apply_humidity_correction(
        comfort: tuple[float, float],
        humidity: float,
        mode: str = "cool",
    ) -> tuple[float, float]:
        """Adjust comfort range based on humidity's effect on perceived temperature.

        Cooling: high humidity makes it feel warmer, so lower the comfort max
        by the heat index overshoot at the comfort ceiling.
        Heating: low humidity makes it feel cooler, so raise the comfort min
        slightly to compensate for dry air.
        """
        if mode == "cool":
            apparent_at_max = calculate_apparent_temperature(comfort[1], humidity)
            delta = apparent_at_max - comfort[1]
            if delta <= 0:
                return comfort
            new_max = comfort[1] - delta
            # Don't let max go below min + 2°F
            new_max = max(new_max, comfort[0] + 2.0)
            return (comfort[0], new_max)

        if mode == "heat" and humidity < 30:
            # Dry air makes it feel cooler — lift comfort range slightly
            lift = (30.0 - humidity) * 0.05  # max ~1.5°F at 0% RH
            return (comfort[0] + lift, comfort[1] + lift)

        return comfort

    def _forecast_deviated(
        self,
        new_forecast: list[ForecastPoint],
        threshold_f: float,
    ) -> bool:
        """Check if new forecast deviates significantly from the snapshot."""
        now = datetime.now(timezone.utc)
        lookahead = now + timedelta(hours=6)

        old_by_hour: dict[int, float] = {}
        for pt in self._last_forecast_snapshot:
            if now <= pt.time <= lookahead:
                key = int(pt.time.timestamp()) // 3600
                old_by_hour[key] = pt.outdoor_temp

        for pt in new_forecast:
            if pt.time > lookahead:
                continue
            key = int(pt.time.timestamp()) // 3600
            old_temp = old_by_hour.get(key)
            if old_temp is not None and abs(pt.outdoor_temp - old_temp) > threshold_f:
                return True

        return False

    def _stamp_per_hour_comfort(
        self,
        forecast: list[ForecastPoint],
        base_comfort: tuple[float, float],
        mode: str,
        timeline: list[OccupancyForecastPoint],
    ) -> None:
        """Set comfort_min/comfort_max on each ForecastPoint from the timeline."""
        for pt in forecast:
            occ_mode = self._lookup_occupancy_at(pt.time, timeline)
            adjusted = OccupancyAdapter.adjust_comfort_for_mode(
                base_comfort, mode, occ_mode
            )
            # Apply sleep bounds when HOME and within sleep window
            if (
                self.sleep_config
                and self.sleep_config.get("enabled")
                and occ_mode == OccupancyMode.HOME
                and self._is_in_sleep_window(pt.time, self.sleep_config)
            ):
                sleep_comfort = self.sleep_config.get(
                    "comfort_cool" if mode == "cool" else "comfort_heat"
                )
                if sleep_comfort:
                    adjusted = sleep_comfort
            pt.comfort_min = adjusted[0]
            pt.comfort_max = adjusted[1]

    @staticmethod
    def _is_in_sleep_window(time: datetime, sleep_config: dict) -> bool:
        """Check if a time falls within the sleep window (handles overnight)."""
        start_str = sleep_config.get("start", "22:00")
        end_str = sleep_config.get("end", "07:00")
        start_parts = start_str.split(":")
        end_parts = end_str.split(":")
        start_h, start_m = int(start_parts[0]), int(start_parts[1])
        end_h, end_m = int(end_parts[0]), int(end_parts[1])

        try:
            from homeassistant.util import dt as dt_util
            local_time = dt_util.as_local(time).time()
        except ImportError:
            local_time = time.astimezone().time()

        start = dt_time(start_h, start_m)
        end = dt_time(end_h, end_m)

        if start <= end:  # same-day window (e.g., 01:00-06:00)
            return start <= local_time < end
        else:  # overnight window (e.g., 22:00-07:00)
            return local_time >= start or local_time < end

    @staticmethod
    def _lookup_occupancy_at(
        time: datetime,
        timeline: list[OccupancyForecastPoint],
    ) -> OccupancyMode:
        """Find the occupancy mode at a given time from the timeline."""
        for seg in timeline:
            if seg.start_time <= time < seg.end_time:
                mode_str = seg.mode.lower()
                if mode_str == "away":
                    return OccupancyMode.AWAY
                if mode_str == "vacation":
                    return OccupancyMode.VACATION
                return OccupancyMode.HOME
        return OccupancyMode.HOME  # default when time not covered

    @staticmethod
    def _hash_occupancy_timeline(
        timeline: list[OccupancyForecastPoint],
    ) -> str:
        """Produce a simple hash to detect timeline changes."""
        parts = []
        for seg in timeline:
            parts.append(
                f"{seg.start_time.isoformat()}:{seg.end_time.isoformat()}:{seg.mode}"
            )
        return "|".join(parts)

    @property
    def schedule(self) -> OptimizedSchedule | None:
        return self._current_schedule

    @property
    def mode(self) -> str | None:
        return self._current_mode

    @property
    def last_optimization_time(self) -> datetime | None:
        return self._last_optimization_time

    @property
    def forecast_snapshot(self) -> list[ForecastPoint]:
        return list(self._last_forecast_snapshot)
