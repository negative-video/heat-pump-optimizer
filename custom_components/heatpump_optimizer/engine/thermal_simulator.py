"""Forward temperature simulation using the performance model.

Simulates indoor temperature over time given a schedule and weather forecast.
Uses 5-minute time steps to match the Beestat CSV resolution.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .data_types import ForecastPoint, ScheduleEntry, SimulationPoint
from .performance_model import PerformanceModel


class ThermalSimulator:
    """Simulates indoor temperature forward in time."""

    def __init__(self, model: PerformanceModel):
        self.model = model

    def simulate(
        self,
        initial_indoor_temp: float,
        forecast: list[ForecastPoint],
        schedule: list[ScheduleEntry],
        dt_minutes: float = 5.0,
    ) -> list[SimulationPoint]:
        """Simulate indoor temp over the schedule period.

        Args:
            initial_indoor_temp: Starting indoor temperature (°F)
            forecast: Outdoor temperature timeline
            schedule: Target temps and modes over time
            dt_minutes: Time step (default 5 min to match Beestat data)

        Returns:
            List of SimulationPoints at each time step
        """
        if not schedule or not forecast:
            return []

        start_time = schedule[0].start_time
        end_time = schedule[-1].end_time
        dt_hours = dt_minutes / 60.0

        indoor_temp = initial_indoor_temp
        cumulative_runtime = 0.0
        results: list[SimulationPoint] = []
        current_time = start_time

        while current_time <= end_time:
            outdoor_temp = self._interpolate_forecast(current_time, forecast)
            entry = self._get_schedule_entry(current_time, schedule)

            if entry is None or entry.mode == "off":
                # No HVAC - passive drift only
                hvac_running = False
                rate = self.model.passive_drift(outdoor_temp)
            elif entry.mode == "cool":
                hvac_running = self._should_cool(indoor_temp, entry.target_temp)
                if hvac_running:
                    # Beestat deltas represent the OBSERVED indoor temp change
                    # per hour of runtime, which already includes passive drift.
                    # Do NOT add passive_drift on top.
                    rate = self.model.cooling_delta(outdoor_temp)
                    cumulative_runtime += dt_minutes
                else:
                    rate = self.model.passive_drift(outdoor_temp)
            elif entry.mode == "heat":
                hvac_running = self._should_heat(indoor_temp, entry.target_temp)
                if hvac_running:
                    # Same: Beestat heating deltas already include drift.
                    rate = self.model.heating_delta(outdoor_temp)
                    cumulative_runtime += dt_minutes
                else:
                    rate = self.model.passive_drift(outdoor_temp)
            else:
                hvac_running = False
                rate = self.model.passive_drift(outdoor_temp)

            results.append(
                SimulationPoint(
                    time=current_time,
                    indoor_temp=round(indoor_temp, 2),
                    outdoor_temp=outdoor_temp,
                    hvac_running=hvac_running,
                    cumulative_runtime_minutes=cumulative_runtime,
                )
            )

            indoor_temp += rate * dt_hours
            current_time += timedelta(minutes=dt_minutes)

        return results

    def _should_cool(self, indoor_temp: float, target_temp: float) -> bool:
        """Determine if cooling should run based on thermostat logic.

        Uses the differential (hysteresis) to prevent short-cycling:
        - Turn ON when indoor temp exceeds target + differential/2
        - Turn OFF when indoor temp drops below target - differential/2
        """
        diff = self.model.cool_differential
        return indoor_temp > target_temp + diff / 2

    def _should_heat(self, indoor_temp: float, target_temp: float) -> bool:
        """Determine if heating should run based on thermostat logic."""
        diff = self.model.heat_differential
        return indoor_temp < target_temp - diff / 2

    def _interpolate_forecast(
        self, time: datetime, forecast: list[ForecastPoint]
    ) -> float:
        """Get outdoor temp at a given time by interpolating the forecast."""
        if not forecast:
            return 70.0  # fallback

        # Before first point
        if time <= forecast[0].time:
            return forecast[0].outdoor_temp

        # After last point
        if time >= forecast[-1].time:
            return forecast[-1].outdoor_temp

        # Find surrounding points and interpolate
        for i in range(len(forecast) - 1):
            if forecast[i].time <= time <= forecast[i + 1].time:
                span = (forecast[i + 1].time - forecast[i].time).total_seconds()
                if span == 0:
                    return forecast[i].outdoor_temp
                frac = (time - forecast[i].time).total_seconds() / span
                return (
                    forecast[i].outdoor_temp
                    + frac * (forecast[i + 1].outdoor_temp - forecast[i].outdoor_temp)
                )

        return forecast[-1].outdoor_temp

    def _get_schedule_entry(
        self, time: datetime, schedule: list[ScheduleEntry]
    ) -> ScheduleEntry | None:
        """Find the schedule entry covering the given time."""
        for entry in schedule:
            if entry.start_time <= time < entry.end_time:
                return entry
        # Check if time equals the last entry's end_time
        if schedule and time == schedule[-1].end_time:
            return schedule[-1]
        return None

    # ── Convenience methods ────────────────────────────────────────

    def simulate_constant_setpoint(
        self,
        initial_indoor_temp: float,
        forecast: list[ForecastPoint],
        setpoint: float,
        mode: str,
        dt_minutes: float = 5.0,
    ) -> list[SimulationPoint]:
        """Simulate with a single constant setpoint (baseline behavior)."""
        if not forecast:
            return []

        schedule = [
            ScheduleEntry(
                start_time=forecast[0].time,
                end_time=forecast[-1].time,
                target_temp=setpoint,
                mode=mode,
                reason="Constant setpoint (baseline)",
            )
        ]
        return self.simulate(initial_indoor_temp, forecast, schedule, dt_minutes)

    def total_runtime(self, simulation: list[SimulationPoint]) -> float:
        """Total HVAC runtime in minutes from a simulation result."""
        if not simulation:
            return 0.0
        return simulation[-1].cumulative_runtime_minutes

    def comfort_violations(
        self,
        simulation: list[SimulationPoint],
        comfort_min: float,
        comfort_max: float,
    ) -> int:
        """Count time steps where indoor temp is outside comfort bounds."""
        count = 0
        for point in simulation:
            if point.indoor_temp < comfort_min or point.indoor_temp > comfort_max:
                count += 1
        return count
