"""Forward temperature simulation using the performance model.

Simulates indoor temperature over time given a schedule and weather forecast.
Uses 5-minute time steps to match the thermostat update interval.

The simulated thermostat is *stateful*: it latches on above
``target + differential/2`` and stays on until ``target - differential/2``
(mirrored for heat).  A stateless predicate makes the simulated indoor temp
ride permanently at the ON threshold, which manufactures a comfort violation
for every schedule that legitimately uses a comfort band edge.

Schedule entries may carry a dual setpoint pair (``target_temp_low`` /
``target_temp_high``).  When both are present the simulated thermostat
arbitrates like a real heat_cool thermostat: heat below the low setpoint,
cool above the high one, coast in between.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .data_types import ForecastPoint, ScheduleEntry, SimulationPoint
from .performance_model import PerformanceModel

# Floor on the simulated thermostat differential.  A zero differential
# degenerates into a stateless predicate and reintroduces the ride-at-target
# artifact this module exists to avoid.
MIN_DIFFERENTIAL_F = 0.2

# Auxiliary (electric resistance) strip heat engages when the compressor is
# running and the house is still this far below target.  Mirrors the droop
# staging a real heat pump thermostat uses.
AUX_DROOP_F = 1.5

# Aux is only considered plausible within this margin of the heat balance
# point; above it the compressor has capacity to spare.
AUX_BALANCE_MARGIN_F = 5.0

# Two opt-in model hooks (docs/internal/TWIN_STATS_SPEC.md 3.6). A model that
# defines neither, such as PerformanceModel, simulates exactly as before.
#
# * ``model.begin_step(time)`` is called at the top of every step, before any
#   rate is read, for models whose rates vary with the time of day (the twin
#   house model's delayed sun).
# * ``model.aux_rule`` as ``{"runtime_min": R, "lockout_f": L}`` replaces the
#   droop rule with runtime staging: strips come on once the compressor has
#   been heating without a break for R minutes, only at or below L outdoor,
#   and only while the house is below target.


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
        passive_only: bool = False,
        initial_hvac_on: bool = False,
    ) -> list[SimulationPoint]:
        """Simulate indoor temp over the schedule period.

        Args:
            initial_indoor_temp: Starting indoor temperature (°F)
            forecast: Outdoor temperature timeline
            schedule: Target temps and modes over time
            dt_minutes: Time step (default 5 min to match thermostat interval)
            passive_only: If True, use passive_drift() for all steps regardless
                of schedule (for learning mode where HVAC is not controlled)
            initial_hvac_on: Whether the compressor is already running at the
                start of the window (seeds the hysteresis state)

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
        cumulative_aux = 0.0
        hvac_on = bool(initial_hvac_on)
        active_mode = "off"
        results: list[SimulationPoint] = []
        current_time = start_time
        begin_step = getattr(self.model, "begin_step", None)
        aux_rule = getattr(self.model, "aux_rule", None)
        if not isinstance(aux_rule, dict):
            aux_rule = None
        heat_run_minutes = 0.0  # compressor heating without a break, this call

        while current_time <= end_time:
            if begin_step is not None:
                begin_step(current_time)
            outdoor_temp = self._interpolate_forecast(current_time, forecast)
            entry = self._get_schedule_entry(current_time, schedule)

            # Solar condition for this time step (from forecast cloud cover)
            solar_cond = self._interpolate_solar_condition(current_time, forecast)

            aux_running = False

            if passive_only or entry is None or entry.mode == "off":
                # No HVAC - passive drift only
                hvac_on = False
                active_mode = "off"
                rate = self.model.passive_drift(outdoor_temp, indoor_temp, solar_condition=solar_cond)
            else:
                step_mode, target, hvac_on = self._thermostat_step(entry, indoor_temp, hvac_on, active_mode)
                active_mode = step_mode
                if not hvac_on or step_mode == "off":
                    rate = self.model.passive_drift(outdoor_temp, indoor_temp, solar_condition=solar_cond)
                elif step_mode == "cool":
                    # Profile deltas represent the OBSERVED indoor temp change
                    # per hour of runtime, which already includes passive drift.
                    # Do NOT add passive_drift on top.
                    rate = self.model.cooling_delta(outdoor_temp, indoor_temp)
                    cumulative_runtime += dt_minutes
                elif step_mode == "heat":
                    if aux_rule is not None:
                        aux_running = self._aux_runtime_engaged(
                            aux_rule, outdoor_temp, indoor_temp, target, heat_run_minutes
                        )
                    else:
                        aux_running = self._aux_engaged(outdoor_temp, indoor_temp, target)
                    # Same: Profile heating deltas already include drift.
                    rate = self.model.heating_delta(outdoor_temp, indoor_temp)
                    if aux_running:
                        aux_rate = self.model.aux_heating_delta(outdoor_temp, indoor_temp)
                        if aux_rate > rate:
                            rate = aux_rate
                        cumulative_aux += dt_minutes
                    cumulative_runtime += dt_minutes
                    heat_run_minutes += dt_minutes
                else:
                    hvac_on = False
                    rate = self.model.passive_drift(outdoor_temp, indoor_temp, solar_condition=solar_cond)

            if not (hvac_on and active_mode == "heat"):
                heat_run_minutes = 0.0

            results.append(
                SimulationPoint(
                    time=current_time,
                    indoor_temp=round(indoor_temp, 2),
                    outdoor_temp=outdoor_temp,
                    hvac_running=hvac_on,
                    cumulative_runtime_minutes=cumulative_runtime,
                    hvac_mode=active_mode if hvac_on else "off",
                    aux_running=aux_running,
                    cumulative_aux_minutes=cumulative_aux,
                )
            )

            indoor_temp += rate * dt_hours
            current_time += timedelta(minutes=dt_minutes)

        return results

    # ── Stateful thermostat ────────────────────────────────────────

    def _thermostat_step(
        self,
        entry: ScheduleEntry,
        indoor_temp: float,
        hvac_on: bool,
        previous_mode: str,
    ) -> tuple[str, float, bool]:
        """Decide (mode, target, running) for one step of a stateful thermostat.

        Handles both single-setpoint entries and dual-setpoint (heat_cool)
        entries.  ``previous_mode`` carries the latched side across steps so a
        dual-setpoint thermostat does not flip sides mid-cycle.
        """
        low = entry.target_temp_low
        high = entry.target_temp_high

        if low is not None and high is not None:
            # Dual setpoint: whichever side is latched keeps its own hysteresis
            # until it releases, then the other side may pick up.
            if hvac_on and previous_mode == "heat":
                if self._should_heat(indoor_temp, low, currently_on=True):
                    return "heat", low, True
            elif hvac_on and previous_mode == "cool":
                if self._should_cool(indoor_temp, high, currently_on=True):
                    return "cool", high, True

            if self._should_heat(indoor_temp, low, currently_on=False):
                return "heat", low, True
            if self._should_cool(indoor_temp, high, currently_on=False):
                return "cool", high, True
            return "off", indoor_temp, False

        target = entry.target_temp
        if entry.mode == "cool":
            running = self._should_cool(indoor_temp, target, currently_on=hvac_on and previous_mode == "cool")
            return "cool", target, running
        if entry.mode == "heat":
            running = self._should_heat(indoor_temp, target, currently_on=hvac_on and previous_mode == "heat")
            return "heat", target, running
        return "off", target, False

    def _should_cool(
        self,
        indoor_temp: float,
        target_temp: float,
        currently_on: bool = False,
    ) -> bool:
        """Stateful cooling thermostat decision.

        Uses the differential (hysteresis) to prevent short-cycling:
        - Turn ON when indoor temp exceeds target + differential/2
        - Turn OFF when indoor temp drops below target - differential/2

        ``currently_on`` carries the compressor state from the previous step.
        Called without it, this answers "would a resting thermostat start?",
        which is the conservative reading and matches the historical
        stateless behavior.
        """
        diff = max(self.model.cool_differential, MIN_DIFFERENTIAL_F)
        if currently_on:
            return indoor_temp > target_temp - diff / 2
        return indoor_temp > target_temp + diff / 2

    def _should_heat(
        self,
        indoor_temp: float,
        target_temp: float,
        currently_on: bool = False,
    ) -> bool:
        """Stateful heating thermostat decision (mirror of _should_cool)."""
        diff = max(self.model.heat_differential, MIN_DIFFERENTIAL_F)
        if currently_on:
            return indoor_temp < target_temp + diff / 2
        return indoor_temp < target_temp - diff / 2

    def _aux_engaged(
        self,
        outdoor_temp: float,
        indoor_temp: float,
        target_temp: float,
    ) -> bool:
        """Whether electric strip heat would be staged in for this step.

        Real thermostats stage aux on droop: the compressor is running but the
        house is still meaningfully below target.  That only happens near or
        below the heat balance point, where compressor capacity has collapsed.
        Modeling it matters because displacing aux runtime is the one heating
        lever with a large, real energy payoff.
        """
        bp = self.model.heat_balance_point
        if bp is None:
            bp = 25.0
        if outdoor_temp > bp + AUX_BALANCE_MARGIN_F:
            return False
        return indoor_temp < target_temp - AUX_DROOP_F

    @staticmethod
    def _aux_runtime_engaged(
        rule: dict,
        outdoor_temp: float,
        indoor_temp: float,
        target_temp: float,
        heat_run_minutes: float,
    ) -> bool:
        """Runtime staging (``model.aux_rule``), as an Ecobee stages strips.

        ``heat_run_minutes`` is how long the compressor had been heating
        without a break before this step.
        """
        lockout = rule.get("lockout_f")
        runtime = rule.get("runtime_min")
        if lockout is None or runtime is None:
            return False
        return outdoor_temp <= lockout and heat_run_minutes >= runtime - 1e-9 and indoor_temp < target_temp

    def _interpolate_forecast(self, time: datetime, forecast: list[ForecastPoint]) -> float:
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
                return forecast[i].outdoor_temp + frac * (forecast[i + 1].outdoor_temp - forecast[i].outdoor_temp)

        return forecast[-1].outdoor_temp

    def _get_schedule_entry(self, time: datetime, schedule: list[ScheduleEntry]) -> ScheduleEntry | None:
        """Find the schedule entry covering the given time."""
        for entry in schedule:
            if entry.start_time <= time < entry.end_time:
                return entry
        # Check if time equals the last entry's end_time
        if schedule and time == schedule[-1].end_time:
            return schedule[-1]
        return None

    @staticmethod
    def _interpolate_solar_condition(
        time: datetime,
        forecast: list[ForecastPoint],
    ) -> str | None:
        """Determine solar condition from the nearest forecast point's cloud cover.

        Uses the profiler's forecast classification: cloud_cover < 0.3 = sunny,
        > 0.7 = cloudy, night if sun_elevation <= 0.  Returns None if no data.
        """
        if not forecast:
            return None

        # Find the closest forecast point
        closest = min(forecast, key=lambda pt: abs((pt.time - time).total_seconds()))
        if abs((closest.time - time).total_seconds()) > 7200:
            return None  # too far from any forecast point

        sun_elev = closest.sun_elevation
        if sun_elev is not None and sun_elev <= 0:
            return "night"

        cloud = closest.cloud_cover
        if cloud is not None:
            if cloud < 0.3:
                return "sunny"
            return "cloudy"

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

    def total_aux_runtime(self, simulation: list[SimulationPoint]) -> float:
        """Total auxiliary strip-heat runtime in minutes."""
        if not simulation:
            return 0.0
        return simulation[-1].cumulative_aux_minutes

    def total_energy_kwh(self, simulation: list[SimulationPoint]) -> float:
        """Total electrical energy for a simulated plan, in kWh.

        This is the currency every plan comparison uses.  Ratios of °F/hr
        deltas are never allowed to decide anything on their own -- see the
        gross-vs-net decomposition in PerformanceModel.gross_hvac_rate.
        """
        if len(simulation) < 2:
            return 0.0

        dt_hours = (simulation[1].time - simulation[0].time).total_seconds() / 3600.0
        if dt_hours <= 0:
            return 0.0

        total = 0.0
        for point in simulation:
            if not point.hvac_running:
                continue
            watts = self.model.hvac_power_watts(
                point.hvac_mode or "cool",
                point.outdoor_temp,
                aux_active=point.aux_running,
            )
            total += watts * dt_hours / 1000.0
        return total

    def comfort_violations(
        self,
        simulation: list[SimulationPoint],
        comfort_min: float,
        comfort_max: float,
        tolerance: float | None = None,
    ) -> int:
        """Count time steps meaningfully outside the comfort bounds.

        A thermostat holding a band edge necessarily overshoots it by half the
        differential every cycle; that is normal hardware behavior, not a
        planning failure.  Counting those steps as violations flags every
        schedule that uses the band, which is every useful schedule.  So the
        bounds are widened by ``tolerance`` (default: the larger configured
        differential) before counting.
        """
        if tolerance is None:
            tolerance = self.violation_tolerance()

        lo = comfort_min - tolerance
        hi = comfort_max + tolerance

        count = 0
        for point in simulation:
            if point.indoor_temp < lo or point.indoor_temp > hi:
                count += 1
        return count

    def violation_tolerance(self) -> float:
        """Excursion beyond the comfort band that counts as normal cycling."""
        return max(
            self.model.cool_differential,
            self.model.heat_differential,
            MIN_DIFFERENTIAL_F,
        )

    def comfort_excursion_degree_minutes(
        self,
        simulation: list[SimulationPoint],
        comfort_min: float,
        comfort_max: float,
        tolerance: float | None = None,
    ) -> float:
        """Severity of comfort excursions, in degree-minutes beyond tolerance.

        A graded measure so plan candidates can be ranked by how badly they
        miss, not merely whether they miss.  Returns 0.0 for a plan that stays
        within the tolerated band.
        """
        if len(simulation) < 2:
            return 0.0
        if tolerance is None:
            tolerance = self.violation_tolerance()

        dt_minutes = (simulation[1].time - simulation[0].time).total_seconds() / 60.0
        if dt_minutes <= 0:
            return 0.0

        lo = comfort_min - tolerance
        hi = comfort_max + tolerance

        total = 0.0
        for point in simulation:
            if point.indoor_temp > hi:
                total += (point.indoor_temp - hi) * dt_minutes
            elif point.indoor_temp < lo:
                total += (lo - point.indoor_temp) * dt_minutes
        return total
