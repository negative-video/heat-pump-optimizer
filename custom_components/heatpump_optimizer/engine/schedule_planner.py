"""Flat-default, gated, search-based setpoint planner.

This replaces the percentile-to-setpoint mapping that used to live in
``ScheduleOptimizer._build_schedule``. That mapping ranked the next 24 hours
by efficiency, converted rank to a percentile, and mapped the percentile
linearly onto the comfort band -- unconditionally. It never asked whether
moving the house was worth it, so it swung the full band every day in both
magnitude and direction. On a mild spring day that reads to a household as a
broken thermostat, and it costs energy rather than saving it.

The replacement has three parts:

1. **Flat is the default.** The comfort-band edge appropriate to the mode
   (ceiling for cooling, floor for heating). That is what a normal thermostat
   does and it is the correct answer on the large majority of days.

2. **Pre-conditioning is a gated event, not a continuous mapping.** The gates
   are deliberately narrow:

   * Cooling gate: **the unit will saturate.** Not "COP is better now". A
     ten-day closed-loop replay against this house's own July data found that
     deeper and longer pre-cooling *increased* total energy every time (the
     time constant is only about 5 hours, so overnight coolth has leaked away
     before the afternoon peak arrives), while a flat setpoint held the
     ceiling through 92-99F afternoons with essentially zero discomfort. So
     pre-cooling earns its keep only as comfort insurance on the rare days
     the compressor cannot hold the ceiling at 100% duty. Expect this gate to
     stay shut on normal days. That is the correct outcome, not a bug.
   * Heating gate: hours below the heat balance point are coming, where the
     compressor alone cannot hold setpoint and electric strip heat stages in.
     Compressor pre-heat that displaces resistance heat is a real, large win,
     and it is the one pre-conditioning case with a solid payoff here.

3. **When a gate opens, the window is searched, not derived.** A small
   candidate set (start 2-6 hours ahead of the event, depth 1-3F into the
   band) is simulated over the fixed stateful simulator and ranked by
   simulated comfort excursion first, then simulated total energy. The flat
   plan is always in the candidate set, so a search that finds nothing better
   ships flat.

Ratios of temperature deltas never decide anything here. Every comparison is
between two simulated trajectories priced in kWh.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .data_types import ForecastPoint, ScheduleEntry, SimulationPoint

_LOGGER = logging.getLogger(__name__)

# Candidate search space. Small on purpose: a handful of 5-minute-resolution
# 24h simulations is cheap and debuggable, and an optimizer library would buy
# nothing but opacity here.
CANDIDATE_LEAD_HOURS = (2, 3, 4, 5, 6)
CANDIDATE_DEPTHS_F = (1.0, 2.0, 3.0)

# Below this outdoor temperature a pre-heat is even worth considering. Above
# it, the repo's own moderate-cold-day finding applies: holding an elevated
# indoor temperature raises envelope losses faster than the COP advantage
# recovers them.
HEAT_PRECONDITION_OUTDOOR_MAX_F = 30.0

# How close to the heat balance point counts as "aux heat is coming".
AUX_LOOKAHEAD_MARGIN_F = 5.0

# Excursion below this is hysteresis noise, not saturation. 30 degree-minutes
# is half a degree for an hour.
SATURATION_EXCURSION_DEGREE_MINUTES = 30.0

# Candidates whose comfort outcomes are within this many degree-minutes of
# each other are treated as equally comfortable, and energy decides. Without
# a bucket this coarse, a rounding-level comfort difference could justify an
# arbitrarily large energy cost.
EXCURSION_EQUIVALENCE_DEGREE_MINUTES = 30.0

# Balance-point margin for labeling an hour as passive coasting.
BALANCE_POINT_MARGIN_F = 2.0


@dataclass
class PlanCandidate:
    """One simulated schedule candidate, priced in kWh."""

    label: str
    entries: list[ScheduleEntry]
    simulation: list[SimulationPoint] = field(default_factory=list)
    energy_kwh: float = 0.0
    runtime_minutes: float = 0.0
    aux_minutes: float = 0.0
    violations: int = 0
    excursion_degree_minutes: float = 0.0
    # Split by direction. Only one direction is ever actionable for a given
    # mode: cooling can fix an overshoot, heating can fix an undershoot, and
    # neither can fix the other. Gating on undirected excursion made a heat
    # plan think it should pre-heat its way out of a warm afternoon.
    excursion_over_degree_minutes: float = 0.0
    excursion_under_degree_minutes: float = 0.0
    # Simulation times where the trajectory really left the tolerated band.
    over_points: list[datetime] = field(default_factory=list)
    under_points: list[datetime] = field(default_factory=list)

    @property
    def excursion_points(self) -> list[datetime]:
        """All excursion times, in order."""
        return sorted(self.over_points + self.under_points)

    @property
    def rank_key(self) -> tuple[float, float]:
        """Comfort first (bucketed), energy second.

        Excursion is bucketed so that candidates within half a degree-hour of
        each other count as equally comfortable and energy breaks the tie. A
        finer comparison lets rounding-level comfort noise buy an unbounded
        amount of energy.
        """
        bucket = round(self.excursion_degree_minutes / EXCURSION_EQUIVALENCE_DEGREE_MINUTES)
        return (float(bucket), round(self.energy_kwh, 4))


@dataclass
class PlanResult:
    """What the planner decided, and what it decided against."""

    chosen: PlanCandidate
    flat: PlanCandidate
    gate_open: bool
    gate_reason: str
    candidates_evaluated: int = 1


class SchedulePlanner:
    """Builds a setpoint schedule: flat by default, searched when gated."""

    def __init__(self, model, simulator):
        self.model = model
        self.simulator = simulator

    # ── Entry point ────────────────────────────────────────────────

    def plan(
        self,
        indoor_temp: float,
        forecast: list[ForecastPoint],
        comfort_range: tuple[float, float],
        mode: str,
        hours: list[datetime] | None = None,
        per_hour_comfort: dict[datetime, tuple[float, float]] | None = None,
        per_hour_target: dict[datetime, float] | None = None,
    ) -> PlanResult:
        """Produce a schedule for the forecast window.

        ``per_hour_comfort`` is what the occupant is promised each hour and
        is what plans are scored against. ``per_hour_target`` is the heat
        setpoint for hours where it is deliberately above that promise (a
        morning recovery under way). Scoring a ramp against its destination
        would read every recovery as a comfort failure.
        """
        hour_keys = hours if hours is not None else self._forecast_hours(forecast)
        if not hour_keys:
            empty = PlanCandidate(label="flat", entries=[])
            return PlanResult(
                chosen=empty,
                flat=empty,
                gate_open=False,
                gate_reason="no forecast hours",
            )

        flat_entries = self.build_flat_schedule(
            hour_keys,
            forecast,
            comfort_range,
            mode,
            per_hour_comfort,
            per_hour_target,
        )
        flat = self._evaluate(
            "flat",
            flat_entries,
            indoor_temp,
            forecast,
            comfort_range,
            per_hour_comfort,
        )

        gate_open, event_hour, gate_reason = self._gate(
            mode,
            forecast,
            flat,
            hour_keys,
            comfort_range,
        )

        if not gate_open or event_hour is None:
            _LOGGER.debug(
                "Planner [%s]: flat schedule, gate closed (%s)",
                mode,
                gate_reason,
            )
            return PlanResult(
                chosen=flat,
                flat=flat,
                gate_open=False,
                gate_reason=gate_reason,
            )

        candidates = [flat]
        for lead in CANDIDATE_LEAD_HOURS:
            for depth in CANDIDATE_DEPTHS_F:
                entries = self._window_schedule(
                    hour_keys,
                    forecast,
                    comfort_range,
                    mode,
                    per_hour_comfort,
                    event_hour,
                    lead,
                    depth,
                    per_hour_target,
                )
                if entries is None:
                    continue
                label = f"{'pre-cool' if mode == 'cool' else 'pre-heat'} {lead}h @ {depth:.1f}F"
                candidates.append(
                    self._evaluate(
                        label,
                        entries,
                        indoor_temp,
                        forecast,
                        comfort_range,
                        per_hour_comfort,
                    )
                )

        best = min(candidates, key=lambda c: c.rank_key)
        _LOGGER.info(
            "Planner [%s]: gate open (%s), %d candidates, chose '%s' "
            "(%.2f kWh, %.0f degree-min excursion) vs flat (%.2f kWh, %.0f)",
            mode,
            gate_reason,
            len(candidates),
            best.label,
            best.energy_kwh,
            best.excursion_degree_minutes,
            flat.energy_kwh,
            flat.excursion_degree_minutes,
        )
        return PlanResult(
            chosen=best,
            flat=flat,
            gate_open=True,
            gate_reason=gate_reason,
            candidates_evaluated=len(candidates),
        )

    # ── Schedule construction ──────────────────────────────────────

    def build_flat_schedule(
        self,
        hour_keys: list[datetime],
        forecast: list[ForecastPoint],
        comfort_range: tuple[float, float],
        mode: str,
        per_hour_comfort: dict[datetime, tuple[float, float]] | None = None,
        per_hour_target: dict[datetime, float] | None = None,
    ) -> list[ScheduleEntry]:
        """A flat schedule at the comfort-band edge appropriate to the mode."""
        entries = []
        for hour in hour_keys:
            h_min, h_max = self._comfort_for_hour(hour, comfort_range, per_hour_comfort)
            outdoor = self._hour_outdoor_temp(hour, forecast)
            target = self._hour_target(hour, mode, h_min, h_max, per_hour_target)
            action = "coasting" if self._is_passive_hour(mode, outdoor) else "maintaining"
            if mode == "heat" and target > h_min + 1e-6:
                action = "recovering"
            entries.append(self._entry(hour, target, mode, outdoor, action))
        return entries

    def build_dual_setpoint_schedule(
        self,
        hour_keys: list[datetime],
        forecast: list[ForecastPoint],
        heat_floor: float,
        cool_ceiling: float,
        mode: str,
        per_hour_bounds: dict[datetime, tuple[float, float]] | None = None,
    ) -> list[ScheduleEntry]:
        """A heat floor / cool ceiling pair for every hour.

        The shoulder-season representation. A single-mode single-setpoint
        schedule cannot describe a day that needs heat at 06:00 and cooling at
        15:00, so the integration used to whipsaw between the two, producing
        heat targets and cool targets interleaved within the same day. A
        heat_cool thermostat arbitrates that natively and far better than a
        re-detected mode flag can.

        ``mode`` is the near-term detected mode; it only decides which side
        ``target_temp`` reports for the single-setpoint consumers (tactical
        layer, dashboards).

        ``per_hour_bounds`` maps an hour to its own ``(heat_floor,
        cool_ceiling)``. The pair is flat within an hour but not necessarily
        across the day: a sleep window, an away block and the morning
        recovery all move the floor. Hours without an entry use the flat pair.
        """
        entries = []
        flat_floor, flat_ceiling = heat_floor, cool_ceiling
        for hour in hour_keys:
            outdoor = self._hour_outdoor_temp(hour, forecast)
            heat_floor, cool_ceiling = (
                per_hour_bounds.get(hour, (flat_floor, flat_ceiling)) if per_hour_bounds else (flat_floor, flat_ceiling)
            )
            primary = heat_floor if mode == "heat" else cool_ceiling
            entries.append(
                ScheduleEntry(
                    start_time=hour,
                    end_time=hour + timedelta(hours=1),
                    target_temp=round(primary * 2) / 2,
                    mode=mode,
                    reason=(
                        f"{outdoor:.0f}°F outdoor: maintaining (heat {heat_floor:.1f}°F / cool {cool_ceiling:.1f}°F)"
                    ),
                    target_temp_low=round(heat_floor * 2) / 2,
                    target_temp_high=round(cool_ceiling * 2) / 2,
                )
            )
        return entries

    def evaluate(
        self,
        label: str,
        entries: list[ScheduleEntry],
        indoor_temp: float,
        forecast: list[ForecastPoint],
        comfort_range: tuple[float, float],
        per_hour_comfort: dict[datetime, tuple[float, float]] | None = None,
    ) -> PlanCandidate:
        """Simulate an externally-built schedule and price it in kWh."""
        return self._evaluate(label, entries, indoor_temp, forecast, comfort_range, per_hour_comfort)

    def _window_schedule(
        self,
        hour_keys: list[datetime],
        forecast: list[ForecastPoint],
        comfort_range: tuple[float, float],
        mode: str,
        per_hour_comfort: dict[datetime, tuple[float, float]] | None,
        event_hour: datetime,
        lead_hours: int,
        depth_f: float,
        per_hour_target: dict[datetime, float] | None = None,
    ) -> list[ScheduleEntry] | None:
        """Flat everywhere, except a pre-conditioning window before the event.

        Returns None when the window would fall entirely outside the planning
        horizon, or when the depth is unusable within the band.
        """
        window_start = event_hour - timedelta(hours=lead_hours)
        window = [h for h in hour_keys if window_start <= h < event_hour]
        if not window:
            return None

        entries = []
        moved = False
        for hour in hour_keys:
            h_min, h_max = self._comfort_for_hour(hour, comfort_range, per_hour_comfort)
            outdoor = self._hour_outdoor_temp(hour, forecast)
            flat_target = self._hour_target(hour, mode, h_min, h_max, per_hour_target)

            if hour in window:
                if mode == "cool":
                    target = max(h_min, flat_target - depth_f)
                    action = "pre-cooling"
                elif mode == "heat":
                    target = min(h_max, flat_target + depth_f)
                    action = "pre-heating"
                else:
                    target = flat_target
                    action = "maintaining"
                target = round(target * 2) / 2
                if abs(target - flat_target) > 1e-6:
                    moved = True
                else:
                    action = "maintaining"
            else:
                target = flat_target
                action = "coasting" if self._is_passive_hour(mode, outdoor) else "maintaining"

            entries.append(self._entry(hour, target, mode, outdoor, action))

        if not moved:
            return None
        return entries

    @staticmethod
    def _entry(
        hour: datetime,
        target: float,
        mode: str,
        outdoor: float,
        action: str,
    ) -> ScheduleEntry:
        target = round(target * 2) / 2
        return ScheduleEntry(
            start_time=hour,
            end_time=hour + timedelta(hours=1),
            target_temp=target,
            mode=mode,
            reason=f"{outdoor:.0f}°F outdoor: {action} (target {target:.1f}°F)",
        )

    @staticmethod
    def _flat_edge(mode: str, h_min: float, h_max: float) -> float:
        """The band edge a plain thermostat would sit at in this mode."""
        if mode == "cool":
            return h_max
        if mode == "heat":
            return h_min
        return (h_min + h_max) / 2

    @classmethod
    def _hour_target(
        cls,
        hour: datetime,
        mode: str,
        h_min: float,
        h_max: float,
        per_hour_target: dict[datetime, float] | None,
    ) -> float:
        """The default setpoint for an hour: the band edge, or a set target."""
        edge = cls._flat_edge(mode, h_min, h_max)
        if mode == "heat" and per_hour_target and hour in per_hour_target:
            return max(edge, per_hour_target[hour])
        return edge

    def _is_passive_hour(self, mode: str, outdoor_temp: float) -> bool:
        """Whether the envelope is already drifting the way HVAC would push.

        Purely a labeling aid now. With a flat schedule the target is the same
        either way; the distinction is what lets the dashboard say "coasting"
        instead of implying the compressor is working.
        """
        bp = getattr(self.model, "resist_balance_point", None) or 50.0
        if mode == "heat":
            return outdoor_temp > bp + BALANCE_POINT_MARGIN_F
        if mode == "cool":
            return outdoor_temp < bp - BALANCE_POINT_MARGIN_F
        return False

    # ── Gates ──────────────────────────────────────────────────────

    def _gate(
        self,
        mode: str,
        forecast: list[ForecastPoint],
        flat: PlanCandidate,
        hour_keys: list[datetime],
        comfort_range: tuple[float, float],
    ) -> tuple[bool, datetime | None, str]:
        """Decide whether pre-conditioning is even worth searching for."""
        if mode == "cool":
            return self._cooling_gate(flat, hour_keys)
        if mode == "heat":
            return self._heating_gate(forecast, flat, hour_keys)
        return (False, None, f"mode {mode} is not conditioned")

    def _cooling_gate(
        self,
        flat: PlanCandidate,
        hour_keys: list[datetime],
    ) -> tuple[bool, datetime | None, str]:
        """Open only when the compressor cannot hold the ceiling at 100% duty.

        The flat plan already runs the machine flat out against the comfort
        ceiling. If its simulated trajectory still climbs past the band, the
        unit saturates and there is genuinely nowhere to put the load but
        earlier. If it does not, pre-cooling can only add energy: a colder
        house gains heat faster all day, and the stored coolth leaks out
        before the peak.
        """
        # Only an *upward* excursion is a cooling saturation. A house drifting
        # below the floor in cool mode is not something more cooling can fix.
        if flat.excursion_over_degree_minutes <= SATURATION_EXCURSION_DEGREE_MINUTES:
            return (
                False,
                None,
                "flat plan holds the comfort ceiling; pre-cooling would only add load",
            )

        event_hour = self._first_excursion_hour(flat.over_points, hour_keys)
        if event_hour is None:
            return (False, None, "saturation predicted but no hour identified")
        return (
            True,
            event_hour,
            f"unit saturates from {event_hour.strftime('%H:%M')} "
            f"({flat.excursion_over_degree_minutes:.0f} degree-min over ceiling)",
        )

    def _heating_gate(
        self,
        forecast: list[ForecastPoint],
        flat: PlanCandidate,
        hour_keys: list[datetime],
    ) -> tuple[bool, datetime | None, str]:
        """Open when electric strip heat is coming, or the unit saturates.

        Displacing resistance heat with compressor runtime is the real
        heating-side lever: strip heat costs roughly three times as much per
        delivered degree. On a merely cold day (well above the balance point)
        the gate stays shut, matching the repo's own finding that pre-heating
        on moderate cold days increases energy use.
        """
        temps = [pt.outdoor_temp for pt in forecast]
        if not temps:
            return (False, None, "no forecast")

        bp = getattr(self.model, "heat_balance_point", None)
        if bp is None:
            bp = 25.0
        aux_threshold = bp + AUX_LOOKAHEAD_MARGIN_F

        if min(temps) < HEAT_PRECONDITION_OUTDOOR_MAX_F:
            aux_hour = self._first_hour_at_or_below(forecast, hour_keys, aux_threshold)
            if aux_hour is not None:
                return (
                    True,
                    aux_hour,
                    f"aux strip heat expected from {aux_hour.strftime('%H:%M')} "
                    f"(outdoor at or below {aux_threshold:.0f}F)",
                )

        # Only a *downward* excursion is a heating saturation. A house drifting
        # above the ceiling in heat mode is a warm afternoon, and no amount of
        # pre-heating fixes that; it means the day wanted cooling or a
        # dual-setpoint shoulder plan.
        if flat.excursion_under_degree_minutes > SATURATION_EXCURSION_DEGREE_MINUTES:
            # Below the floor *and not gaining on it* is saturation. Below a
            # floor that has just stepped up, and climbing, is a recovery
            # doing what it was asked to do; pre-heating ahead of it would
            # simply move the step earlier.
            stalled = self._losing_ground_points(flat.simulation, flat.under_points)
            event_hour = self._first_excursion_hour(stalled, hour_keys)
            if event_hour is not None:
                return (
                    True,
                    event_hour,
                    f"unit saturates from {event_hour.strftime('%H:%M')}",
                )

        return (
            False,
            None,
            "no aux hours forecast; pre-heating a moderate cold day costs energy",
        )

    @staticmethod
    def _losing_ground_points(
        simulation: list[SimulationPoint],
        under_points: list[datetime],
    ) -> list[datetime]:
        """Under-floor times at which the house is not warming.

        Requires the stall to last: a single flat step inside a climb (a
        thermostat cycling off at its differential) is not a stall. At least
        30 minutes of under-floor, non-rising points are needed.
        """
        if not simulation or not under_points:
            return []
        under = set(under_points)
        stalled: list[datetime] = []
        previous = None
        for point in simulation:
            if previous is not None and point.time in under and point.indoor_temp <= previous.indoor_temp + 1e-6:
                stalled.append(point.time)
            previous = point
        if len(simulation) > 1:
            step_min = (simulation[1].time - simulation[0].time).total_seconds() / 60.0
        else:
            step_min = 5.0
        if len(stalled) * step_min < 30.0:
            return []
        return stalled

    @staticmethod
    def _first_hour_at_or_below(
        forecast: list[ForecastPoint],
        hour_keys: list[datetime],
        threshold: float,
    ) -> datetime | None:
        """First scheduled hour whose forecast drops to or below a threshold."""
        cold = [pt.time for pt in forecast if pt.outdoor_temp <= threshold]
        if not cold:
            return None
        first_cold = min(cold)
        later = [h for h in hour_keys if h >= first_cold]
        if later:
            return min(later)
        return None

    @staticmethod
    def _first_excursion_hour(
        points: list[datetime],
        hour_keys: list[datetime],
    ) -> datetime | None:
        """The scheduled hour containing the first excursion of interest."""
        if not points or not hour_keys:
            return None
        first = min(points)
        candidates = [h for h in hour_keys if h <= first]
        if candidates:
            return max(candidates)
        return min(hour_keys)

    # ── Evaluation ─────────────────────────────────────────────────

    def _evaluate(
        self,
        label: str,
        entries: list[ScheduleEntry],
        indoor_temp: float,
        forecast: list[ForecastPoint],
        comfort_range: tuple[float, float],
        per_hour_comfort: dict[datetime, tuple[float, float]] | None,
    ) -> PlanCandidate:
        """Simulate a candidate and price it."""
        sim = self.simulator.simulate(indoor_temp, forecast, entries)
        metrics = self._comfort_metrics(sim, comfort_range, per_hour_comfort)
        return PlanCandidate(
            label=label,
            entries=entries,
            simulation=sim,
            energy_kwh=self.simulator.total_energy_kwh(sim),
            runtime_minutes=self.simulator.total_runtime(sim),
            aux_minutes=self.simulator.total_aux_runtime(sim),
            violations=metrics["violations"],
            excursion_degree_minutes=metrics["over"] + metrics["under"],
            excursion_over_degree_minutes=metrics["over"],
            excursion_under_degree_minutes=metrics["under"],
            over_points=metrics["over_points"],
            under_points=metrics["under_points"],
        )

    def _comfort_metrics(
        self,
        simulation: list[SimulationPoint],
        comfort_range: tuple[float, float],
        per_hour_comfort: dict[datetime, tuple[float, float]] | None,
    ) -> dict:
        """Violation count and directional degree-minutes.

        Uses each hour's own comfort band when the occupancy timeline stamped
        one, so an AWAY hour is not judged against a HOME band.
        """
        empty = {
            "violations": 0,
            "over": 0.0,
            "under": 0.0,
            "over_points": [],
            "under_points": [],
        }
        if len(simulation) < 2:
            return empty

        tolerance = self.simulator.violation_tolerance()
        dt_minutes = (simulation[1].time - simulation[0].time).total_seconds() / 60.0
        if dt_minutes <= 0:
            return empty

        violations = 0
        over = 0.0
        under = 0.0
        over_points: list[datetime] = []
        under_points: list[datetime] = []

        for point in simulation:
            h_min, h_max = self._comfort_for_hour(
                point.time.replace(minute=0, second=0, microsecond=0),
                comfort_range,
                per_hour_comfort,
            )
            lo = h_min - tolerance
            hi = h_max + tolerance
            if point.indoor_temp > hi:
                violations += 1
                over += (point.indoor_temp - hi) * dt_minutes
                over_points.append(point.time)
            elif point.indoor_temp < lo:
                violations += 1
                under += (lo - point.indoor_temp) * dt_minutes
                under_points.append(point.time)

        return {
            "violations": violations,
            "over": over,
            "under": under,
            "over_points": over_points,
            "under_points": under_points,
        }

    # ── Small helpers ──────────────────────────────────────────────

    @staticmethod
    def _comfort_for_hour(
        hour: datetime,
        comfort_range: tuple[float, float],
        per_hour_comfort: dict[datetime, tuple[float, float]] | None,
    ) -> tuple[float, float]:
        if per_hour_comfort and hour in per_hour_comfort:
            return per_hour_comfort[hour]
        return comfort_range

    @staticmethod
    def _forecast_hours(forecast: list[ForecastPoint]) -> list[datetime]:
        seen = set()
        for pt in forecast:
            seen.add(pt.time.replace(minute=0, second=0, microsecond=0))
        return sorted(seen)

    @staticmethod
    def _hour_outdoor_temp(hour: datetime, forecast: list[ForecastPoint]) -> float:
        temps = [pt.outdoor_temp for pt in forecast if pt.time.replace(minute=0, second=0, microsecond=0) == hour]
        if temps:
            return sum(temps) / len(temps)
        if forecast:
            closest = min(forecast, key=lambda pt: abs((pt.time - hour).total_seconds()))
            return closest.outdoor_temp
        return 70.0
