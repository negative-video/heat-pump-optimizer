"""Pre-conditioning planner — finds the cost/efficiency-optimal time to start
transitioning from AWAY comfort to HOME comfort before the user arrives.

Two-stage operation:
  Stage 1 (proactive): Uses calendar event end time as estimated arrival.
  Stage 2 (reactive):  Refines with zone departure + travel time sensor.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from .data_types import ForecastPoint, PreconditionPlan
from .performance_model import PerformanceModel

_LOGGER = logging.getLogger(__name__)

# Resolution for candidate start times
_CANDIDATE_INTERVAL_MINUTES = 15


class PreconditionPlanner:
    """Find the optimal time to begin HVAC pre-conditioning before arrival."""

    def __init__(self, model: PerformanceModel):
        self.model = model

    def plan(
        self,
        arrival_time: datetime,
        current_indoor_temp: float,
        forecast: list[ForecastPoint],
        mode: str,
        home_comfort: tuple[float, float],
        away_comfort: tuple[float, float],
        power_watts: float = 3500.0,
        buffer_minutes: int = 15,
        arrival_source: str = "calendar",
    ) -> PreconditionPlan | None:
        """Determine the cost/efficiency-optimal pre-conditioning start time.

        Args:
            arrival_time: When the user is expected home.
            current_indoor_temp: Current indoor temperature (°F).
            forecast: Weather forecast covering the planning window.
            mode: "cool" or "heat".
            home_comfort: HOME comfort range (min, max) in °F.
            away_comfort: AWAY comfort range (min, max) in °F.
            power_watts: HVAC power draw estimate.
            buffer_minutes: Safety buffer before arrival (finish early).
            arrival_source: "calendar" or "travel_sensor".

        Returns:
            PreconditionPlan, or None if no pre-conditioning is needed.
        """
        now = datetime.now(timezone.utc)
        deadline = arrival_time - timedelta(minutes=buffer_minutes)

        if deadline <= now:
            # No time left — must start immediately if needed
            return self._immediate_plan(
                now, arrival_time, current_indoor_temp, forecast, mode,
                home_comfort, power_watts, arrival_source,
            )

        # Determine the target temp we need to reach
        target = self._target_temp(mode, home_comfort)

        # Check if already at target
        gap = self._temperature_gap(current_indoor_temp, target, mode)
        if gap <= 0.5:
            _LOGGER.debug("Already within HOME comfort — no pre-conditioning needed")
            return None

        # Build outdoor temp lookup from forecast
        temp_at_time = self._build_temp_lookup(forecast)

        # Simulate passive drift from now to deadline
        drift_trajectory = self._simulate_drift(
            current_indoor_temp, now, deadline, temp_at_time
        )

        # Evaluate candidate start times
        best_plan: PreconditionPlan | None = None
        best_cost = float("inf")

        cursor = now
        while cursor <= deadline:
            indoor_at_cursor = self._lookup_drift(drift_trajectory, cursor)
            gap_at_cursor = self._temperature_gap(indoor_at_cursor, target, mode)

            if gap_at_cursor <= 0.5:
                # No conditioning needed at this start time
                cursor += timedelta(minutes=_CANDIDATE_INTERVAL_MINUTES)
                continue

            # Estimate runtime needed
            outdoor_temp = self._outdoor_temp_at(cursor, temp_at_time, forecast)
            runtime_min = self.model.runtime_needed(outdoor_temp, mode, gap_at_cursor)

            if math.isinf(runtime_min):
                cursor += timedelta(minutes=_CANDIDATE_INTERVAL_MINUTES)
                continue

            # Check if HVAC can finish before deadline
            finish_time = cursor + timedelta(minutes=runtime_min)
            if finish_time > deadline + timedelta(minutes=buffer_minutes):
                cursor += timedelta(minutes=_CANDIDATE_INTERVAL_MINUTES)
                continue

            # Compute energy and cost
            runtime_hours = runtime_min / 60.0
            energy_kwh = runtime_hours * power_watts / 1000.0

            # Cost: use electricity rate from forecast if available
            rate = self._rate_at(cursor, runtime_min, forecast)
            cost = energy_kwh * rate if rate is not None else energy_kwh

            if cost < best_cost:
                best_cost = cost
                best_plan = PreconditionPlan(
                    start_time=cursor,
                    arrival_time=arrival_time,
                    estimated_runtime_minutes=round(runtime_min, 1),
                    estimated_energy_kwh=round(energy_kwh, 2),
                    estimated_cost=round(energy_kwh * rate, 3) if rate is not None else None,
                    temperature_gap=round(gap_at_cursor, 1),
                    should_start_now=False,
                    arrival_source=arrival_source,
                )

            cursor += timedelta(minutes=_CANDIDATE_INTERVAL_MINUTES)

        if best_plan is not None:
            # Check if the optimal start time is now or in the past
            if best_plan.start_time <= now + timedelta(minutes=5):
                best_plan.should_start_now = True

            _LOGGER.info(
                "Pre-conditioning plan: start %s for %s arrival "
                "(%.0f min runtime, %.1f°F gap, %.2f kWh)",
                best_plan.start_time.strftime("%H:%M"),
                arrival_time.strftime("%H:%M"),
                best_plan.estimated_runtime_minutes,
                best_plan.temperature_gap,
                best_plan.estimated_energy_kwh,
            )

        return best_plan

    def _immediate_plan(
        self,
        now: datetime,
        arrival_time: datetime,
        current_indoor_temp: float,
        forecast: list[ForecastPoint],
        mode: str,
        home_comfort: tuple[float, float],
        power_watts: float,
        arrival_source: str,
    ) -> PreconditionPlan | None:
        """Create a plan when there's no time to optimize — start now."""
        target = self._target_temp(mode, home_comfort)
        gap = self._temperature_gap(current_indoor_temp, target, mode)
        if gap <= 0.5:
            return None

        outdoor_temp = self._outdoor_temp_at(now, {}, forecast)
        runtime_min = self.model.runtime_needed(outdoor_temp, mode, gap)
        if math.isinf(runtime_min):
            runtime_min = 120.0  # fallback estimate

        energy_kwh = (runtime_min / 60.0) * power_watts / 1000.0
        rate = self._rate_at(now, runtime_min, forecast)

        return PreconditionPlan(
            start_time=now,
            arrival_time=arrival_time,
            estimated_runtime_minutes=round(runtime_min, 1),
            estimated_energy_kwh=round(energy_kwh, 2),
            estimated_cost=round(energy_kwh * rate, 3) if rate is not None else None,
            temperature_gap=round(gap, 1),
            should_start_now=True,
            arrival_source=arrival_source,
        )

    @staticmethod
    def _target_temp(mode: str, home_comfort: tuple[float, float]) -> float:
        """Get the target temperature for pre-conditioning."""
        if mode == "cool":
            # Cool to the HOME max (e.g., 78°F) — just inside comfort
            return home_comfort[1]
        # Heat to the HOME min (e.g., 64°F) — just inside comfort
        return home_comfort[0]

    @staticmethod
    def _temperature_gap(current: float, target: float, mode: str) -> float:
        """Calculate degrees that need to be recovered."""
        if mode == "cool":
            return max(0.0, current - target)  # e.g., 82 - 78 = 4°F to cool
        return max(0.0, target - current)  # e.g., 64 - 58 = 6°F to heat

    def _simulate_drift(
        self,
        start_temp: float,
        start_time: datetime,
        end_time: datetime,
        temp_lookup: dict[int, float],
    ) -> list[tuple[datetime, float]]:
        """Simulate passive thermal drift from start to end."""
        trajectory: list[tuple[datetime, float]] = []
        dt_minutes = 15
        current = start_temp
        t = start_time

        while t <= end_time:
            trajectory.append((t, current))
            # Get outdoor temp for this time
            hour_key = int(t.timestamp()) // 3600
            outdoor = temp_lookup.get(hour_key, 75.0)
            drift_per_hour = self.model.passive_drift(outdoor, current)
            current += drift_per_hour * (dt_minutes / 60.0)
            t += timedelta(minutes=dt_minutes)

        return trajectory

    @staticmethod
    def _lookup_drift(
        trajectory: list[tuple[datetime, float]], time: datetime
    ) -> float:
        """Find indoor temp at a given time from the drift trajectory."""
        if not trajectory:
            return 75.0
        # Find the closest point at or before the given time
        for i in range(len(trajectory) - 1, -1, -1):
            if trajectory[i][0] <= time:
                return trajectory[i][1]
        return trajectory[0][1]

    @staticmethod
    def _build_temp_lookup(forecast: list[ForecastPoint]) -> dict[int, float]:
        """Build hour_key → outdoor_temp lookup from forecast."""
        lookup: dict[int, float] = {}
        for pt in forecast:
            hour_key = int(pt.time.timestamp()) // 3600
            lookup[hour_key] = pt.outdoor_temp
        return lookup

    @staticmethod
    def _outdoor_temp_at(
        time: datetime,
        temp_lookup: dict[int, float],
        forecast: list[ForecastPoint],
    ) -> float:
        """Get outdoor temp at a given time."""
        hour_key = int(time.timestamp()) // 3600
        if hour_key in temp_lookup:
            return temp_lookup[hour_key]
        # Fallback: find closest forecast point
        if forecast:
            closest = min(forecast, key=lambda p: abs((p.time - time).total_seconds()))
            return closest.outdoor_temp
        return 75.0

    @staticmethod
    def _rate_at(
        start_time: datetime,
        runtime_minutes: float,
        forecast: list[ForecastPoint],
    ) -> float | None:
        """Get weighted average electricity rate during the runtime window."""
        if not forecast:
            return None

        end_time = start_time + timedelta(minutes=runtime_minutes)
        rates = []
        for pt in forecast:
            if pt.electricity_rate is not None and start_time <= pt.time <= end_time:
                rates.append(pt.electricity_rate)

        if not rates:
            # Try any forecast point with a rate
            all_rates = [pt.electricity_rate for pt in forecast if pt.electricity_rate is not None]
            if all_rates:
                return sum(all_rates) / len(all_rates)
            return None

        return sum(rates) / len(rates)
