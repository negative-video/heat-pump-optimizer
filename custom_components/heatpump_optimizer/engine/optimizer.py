"""Schedule optimizer: energy-scored hours, flat-default gated planning.

Planning lives in :mod:`.schedule_planner`. This module owns hour scoring,
the public ``optimize_setpoints`` entry point, and display helpers.

Two historical mistakes are corrected here and should not be reintroduced:

* Hours used to be scored as ``1 / |net_delta|``. The net delta conflates the
  machine's output with the envelope load arriving at the same time, so it
  overstates the spread between mild and extreme hours by roughly the ratio of
  the net spread to the gross spread. Scoring now uses
  ``PerformanceModel.energy_per_degree_kwh`` -- kWh per degree of delivered
  forcing -- which is an energy quantity, not a needle-speed ratio.
* Setpoints used to be derived by mapping each hour's efficiency percentile
  onto the comfort band. See :mod:`.schedule_planner` for why that was wrong
  and what replaced it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..const import DEFAULT_SWITCHING_PENALTY_WEIGHT
from .data_types import (
    ForecastPoint,
    HourScore,
    OptimizationWeights,
    OptimizedSchedule,
    ScheduleEntry,
)
from .performance_model import PerformanceModel
from .schedule_planner import SchedulePlanner
from .thermal_simulator import ThermalSimulator

# Stand-in score for an hour where the equipment delivers no usable forcing.
# Large enough to rank last, finite so min-max normalization still works.
_UNUSABLE_HOUR_SCORE = 1e6

_WORK_REDISTRIBUTION_DISCLAIMER = (
    "theoretical upper bound, not achievable: assumes HVAC work is conserved "
    "when moved between hours, which it is not"
)


class ScheduleOptimizer:
    """Scores hours by energy cost and plans schedules over the simulator."""

    def __init__(self, model: PerformanceModel, simulator: ThermalSimulator):
        self.model = model
        self.simulator = simulator

    @property
    def planner(self) -> SchedulePlanner:
        """Planner bound to the optimizer's *current* model and simulator.

        The coordinator hot-swaps ``self.model`` when the active model
        changes, so the planner is built per call rather than cached.
        """
        return SchedulePlanner(self.model, self.simulator)

    def score_hours(
        self,
        forecast: list[ForecastPoint],
        mode: str,
        weights: OptimizationWeights | None = None,
    ) -> list[HourScore]:
        """Score each hour in the forecast by the energy cost of running it.

        Lower score = better time to run HVAC.

        ``efficiency_score`` is kWh per °F of delivered indoor forcing:
        ``power_kW / |gross_hvac_rate|``. Because the envelope load has been
        subtracted out of the rate, a hot afternoon hour is only penalized to
        the extent the equipment actually costs more to run, not to the extent
        the house is fighting back.
        """
        if weights is None:
            weights = OptimizationWeights()

        # Group forecast points into hourly bins
        hourly_temps: dict[datetime, list[float]] = {}
        hourly_carbon: dict[datetime, list[float]] = {}
        hourly_cost: dict[datetime, list[float]] = {}

        for pt in forecast:
            hour_key = pt.time.replace(minute=0, second=0, microsecond=0)
            hourly_temps.setdefault(hour_key, []).append(pt.outdoor_temp)
            if pt.carbon_intensity is not None:
                hourly_carbon.setdefault(hour_key, []).append(pt.carbon_intensity)
            if pt.electricity_rate is not None:
                hourly_cost.setdefault(hour_key, []).append(pt.electricity_rate)

        # First pass: compute raw scores per hour
        raw_scores: list[dict] = []

        for hour in sorted(hourly_temps.keys()):
            avg_temp = sum(hourly_temps[hour]) / len(hourly_temps[hour])

            if mode in ("cool", "heat"):
                efficiency_score = self.model.energy_per_degree_kwh(mode, avg_temp)
                if efficiency_score == float("inf"):
                    # No usable forcing at this temperature. Rank it last
                    # rather than poisoning the min-max normalization.
                    efficiency_score = _UNUSABLE_HOUR_SCORE
            else:
                efficiency_score = 1.0

            carbon_score = None
            if hour in hourly_carbon:
                carbon_score = sum(hourly_carbon[hour]) / len(hourly_carbon[hour])

            cost_score = None
            if hour in hourly_cost:
                cost_score = sum(hourly_cost[hour]) / len(hourly_cost[hour])

            raw_scores.append({
                "hour": hour,
                "outdoor_temp": avg_temp,
                "efficiency": efficiency_score,
                "carbon": carbon_score,
                "cost": cost_score,
            })

        # Second pass: normalize each dimension to 0-1 via min-max scaling.
        # This makes weights comparable across dimensions with different units
        # (efficiency ~0.1-2.0, carbon ~50-500 g/kWh, cost ~0.05-0.50 $/kWh).
        eff_vals = [s["efficiency"] for s in raw_scores]
        carbon_vals = [s["carbon"] for s in raw_scores if s["carbon"] is not None]
        cost_vals = [s["cost"] for s in raw_scores if s["cost"] is not None]

        def _normalize(value: float, vals: list[float]) -> float:
            min_v, max_v = min(vals), max(vals)
            range_v = max_v - min_v
            if range_v < 1e-9:
                return 0.5  # all values identical
            return (value - min_v) / range_v

        scores: list[HourScore] = []
        for s in raw_scores:
            norm_eff = _normalize(s["efficiency"], eff_vals) if eff_vals else 0.5

            norm_carbon = None
            if s["carbon"] is not None and carbon_vals:
                norm_carbon = _normalize(s["carbon"], carbon_vals)

            norm_cost = None
            if s["cost"] is not None and cost_vals:
                norm_cost = _normalize(s["cost"], cost_vals)

            combined = weights.energy_efficiency * norm_eff
            if norm_carbon is not None and weights.carbon_intensity > 0:
                combined += weights.carbon_intensity * norm_carbon
            if norm_cost is not None and weights.electricity_cost > 0:
                combined += weights.electricity_cost * norm_cost

            scores.append(
                HourScore(
                    hour=s["hour"],
                    outdoor_temp=s["outdoor_temp"],
                    efficiency_score=s["efficiency"],
                    carbon_score=s["carbon"],
                    cost_score=s["cost"],
                    combined_score=combined,
                )
            )

        return scores

    # ── Work-redistribution analysis (DIAGNOSTICS ONLY) ────────────

    def analyze_runtime_efficiency_theoretical_bound(
        self,
        hourly_runtime: dict[int, float],
        hourly_outdoor_temp: dict[int, float],
        mode: str,
    ) -> dict:
        """Theoretical upper bound on redistribution savings. NOT achievable.

        Do not wire this into savings displays, sensors, or any control
        decision. It was the project's headline savings number and it is
        physically unsound in two independent ways:

        1. It measures "work" as ``runtime x net_delta``. The net delta
           includes the envelope load, so hours where the house is fighting
           back score as low-capacity hours and the spread is inflated.
        2. Work is not conserved under redistribution. The afternoon envelope
           gain arrives whether or not the compressor ran that morning. Only
           the fraction the thermal mass can buffer inside the comfort band is
           shiftable, and pre-conditioning *adds* load, because a colder or
           warmer house exchanges heat with outside faster all day.

        A closed-loop replay against this house's own data showed trajectory
        simulation losing energy exactly where this arithmetic predicts large
        wins. Every real benefit decision compares simulated total energy
        instead -- see :mod:`.schedule_planner`.

        Args:
            hourly_runtime: {hour_of_day: minutes_of_runtime}
            hourly_outdoor_temp: {hour_of_day: avg_outdoor_temp}
            mode: "cool" or "heat"

        Returns:
            Dict with the bound plus a ``disclaimer`` key.
        """
        if mode == "cool":
            get_delta = lambda t: abs(self.model.cooling_delta(t))
        else:
            get_delta = lambda t: max(self.model.heating_delta(t), 0.01)

        # "Work units" = runtime * net delta magnitude.
        #
        # Retained for diagnostics only. The quantity is not conserved under
        # redistribution: the afternoon envelope gain arrives whether or not
        # the compressor ran earlier, and pre-conditioning *adds* load, since
        # a colder house gains heat faster all day. Treating it as a movable
        # budget is what produced the discredited 25.9% annual savings
        # figure. Nothing user-facing may consume this.
        hourly_work: dict[int, float] = {}
        hourly_delta: dict[int, float] = {}
        total_work = 0.0
        total_actual_runtime = 0.0

        for hour in sorted(hourly_runtime.keys()):
            runtime_min = hourly_runtime[hour]
            if runtime_min <= 0:
                continue
            outdoor_temp = hourly_outdoor_temp.get(hour, 70.0)
            delta = get_delta(outdoor_temp)
            work = (runtime_min / 60.0) * delta  # work units = hours * delta
            hourly_work[hour] = work
            hourly_delta[hour] = delta
            total_work += work
            total_actual_runtime += runtime_min

        if total_work == 0 or total_actual_runtime == 0:
            return {
                "disclaimer": _WORK_REDISTRIBUTION_DISCLAIMER,
                "actual_runtime_minutes": 0,
                "theoretical_min_runtime_minutes": 0,
                "savings_pct": 0,
                "savings_minutes": 0,
                "hourly_breakdown": [],
            }

        # Find the best delta available across all hours of the day
        all_deltas = {
            hour: get_delta(hourly_outdoor_temp.get(hour, 70.0))
            for hour in hourly_outdoor_temp
        }
        best_delta = max(all_deltas.values()) if all_deltas else 1.0

        # Theoretical minimum: do ALL the work at the best efficiency
        theoretical_min_hours = total_work / best_delta
        theoretical_min_minutes = theoretical_min_hours * 60

        # More realistic: redistribute work to the most efficient hours available,
        # respecting that each hour only has 60 minutes of capacity
        sorted_hours = sorted(all_deltas.keys(), key=lambda h: all_deltas[h], reverse=True)
        remaining_work = total_work
        redistributed_runtime = 0.0
        redistribution_plan: list[dict] = []

        for hour in sorted_hours:
            if remaining_work <= 0:
                break
            delta = all_deltas[hour]
            # Max work this hour can do = 60 min * delta
            max_work_this_hour = (60.0 / 60.0) * delta  # 1 hour * delta
            work_assigned = min(remaining_work, max_work_this_hour)
            runtime_this_hour = (work_assigned / delta) * 60  # minutes
            remaining_work -= work_assigned
            redistributed_runtime += runtime_this_hour
            redistribution_plan.append({
                "hour": hour,
                "outdoor_temp": hourly_outdoor_temp.get(hour, 0),
                "delta": round(delta, 2),
                "runtime_minutes": round(runtime_this_hour, 1),
                "work_units": round(work_assigned, 2),
            })

        savings_minutes = total_actual_runtime - redistributed_runtime
        savings_pct = (savings_minutes / total_actual_runtime * 100) if total_actual_runtime > 0 else 0

        # Per-hour breakdown of actual efficiency
        hourly_breakdown = []
        for hour in sorted(hourly_runtime.keys()):
            if hourly_runtime[hour] > 0:
                delta = hourly_delta.get(hour, 0)
                efficiency_vs_best = (delta / best_delta * 100) if best_delta > 0 else 0
                hourly_breakdown.append({
                    "hour": hour,
                    "outdoor_temp": hourly_outdoor_temp.get(hour, 0),
                    "runtime_minutes": round(hourly_runtime[hour], 1),
                    "delta": round(delta, 2),
                    "work_units": round(hourly_work.get(hour, 0), 2),
                    "efficiency_vs_best": round(efficiency_vs_best, 1),
                })

        return {
            "disclaimer": _WORK_REDISTRIBUTION_DISCLAIMER,
            "actual_runtime_minutes": round(total_actual_runtime, 1),
            "theoretical_min_runtime_minutes": round(theoretical_min_minutes, 1),
            "redistributed_runtime_minutes": round(redistributed_runtime, 1),
            "savings_minutes": round(savings_minutes, 1),
            "savings_pct": round(savings_pct, 1),
            "total_work_units": round(total_work, 2),
            "best_delta": round(best_delta, 2),
            "hourly_breakdown": hourly_breakdown,
            "redistribution_plan": redistribution_plan,
        }

    # ── Setpoint-based optimizer (simulation) ──────────────────────

    def optimize_setpoints(
        self,
        current_indoor_temp: float,
        forecast: list[ForecastPoint],
        comfort_range: tuple[float, float],
        mode: str,
        weights: OptimizationWeights | None = None,
    ) -> OptimizedSchedule:
        """Plan a setpoint schedule and price it against the flat baseline.

        The baseline is the flat schedule -- what a plain thermostat sitting
        at the comfort-band edge would have done -- not a midpoint setpoint.
        That is the only comparison that means anything, and since the planner
        ships flat on most days, the honestly reported saving on most days is
        0.0%. A scheduler that always claims a win is a scheduler nobody can
        audit.

        There is no narrow-the-band-and-retry fallback any more. Overshoot is
        handled inside the planner's candidate search: if a plan leaves the
        band, a candidate with earlier or deeper pre-conditioning outranks it.
        If nothing avoids a real excursion (undersized equipment, extreme
        weather) the least-bad plan ships and says so through
        ``comfort_violations`` / ``comfort_excursion_degree_minutes``, instead
        of silently redefining what the occupant asked for.

        If forecast points carry per-hour comfort_min/comfort_max (stamped by
        the strategic controller from a calendar occupancy timeline), those
        are used instead of the single comfort_range for each hour.
        """
        if weights is None:
            weights = OptimizationWeights()

        per_hour_comfort = self._extract_per_hour_comfort(forecast)

        # Hour scoring still runs: it drives the diagnostics/display path and
        # defines the schedule's hour grid. It no longer *derives* setpoints.
        hour_scores = self.score_hours(forecast, mode, weights)
        hour_keys = [hs.hour for hs in hour_scores]

        result = self.planner.plan(
            current_indoor_temp,
            forecast,
            comfort_range,
            mode,
            hours=hour_keys,
            per_hour_comfort=per_hour_comfort,
        )
        chosen = result.chosen
        flat = result.flat

        savings_pct = 0.0
        if flat.energy_kwh > 0:
            savings_pct = (
                (flat.energy_kwh - chosen.energy_kwh) / flat.energy_kwh * 100
            )

        return OptimizedSchedule(
            entries=chosen.entries,
            baseline_runtime_minutes=flat.runtime_minutes,
            optimized_runtime_minutes=chosen.runtime_minutes,
            savings_pct=savings_pct,
            comfort_violations=chosen.violations,
            simulation=chosen.simulation,
            comfort_excursion_degree_minutes=chosen.excursion_degree_minutes,
            plan_label=chosen.label,
            baseline_kwh=flat.energy_kwh,
            optimized_kwh=chosen.energy_kwh,
        )

    @staticmethod
    def _extract_per_hour_comfort(
        forecast: list[ForecastPoint],
    ) -> dict[datetime, tuple[float, float]]:
        """Extract per-hour comfort bounds from forecast point annotations.

        Returns a dict mapping hour_key → (comfort_min, comfort_max) for
        hours that have per-hour comfort set. Empty dict if no annotations.
        """
        per_hour: dict[datetime, tuple[float, float]] = {}
        for pt in forecast:
            if pt.comfort_min is not None and pt.comfort_max is not None:
                hour_key = pt.time.replace(minute=0, second=0, microsecond=0)
                # Use the first point's comfort for each hour
                if hour_key not in per_hour:
                    per_hour[hour_key] = (pt.comfort_min, pt.comfort_max)
        return per_hour

    # ``_build_schedule`` used to live here. It ranked hours by efficiency,
    # converted rank to a percentile, and mapped the percentile linearly onto
    # the comfort band -- unconditionally, every day, in both directions. It
    # has been deleted rather than demoted to a candidate generator, because
    # its output was never a hypothesis worth testing: a full-band swing on a
    # moderate day loses energy and reads as a broken thermostat. See
    # engine/schedule_planner.py for what replaced it.

    @staticmethod
    def _smooth_switching(
        entries: list[ScheduleEntry],
        weight: float = DEFAULT_SWITCHING_PENALTY_WEIGHT,
        threshold: float = 1.0,
    ) -> list[ScheduleEntry]:
        """Smooth consecutive setpoint changes to reduce compressor cycling.

        For each consecutive pair of hours, if the setpoint delta exceeds
        ``threshold`` (°F), the later entry is nudged toward the earlier
        one proportionally to ``weight``. This is a soft penalty — large
        efficiency-driven changes still pass through.

        No longer applied to planner output. A searched pre-conditioning
        window is a single deliberate step in and a single step back out;
        smoothing it would blunt exactly the depth the search chose. Kept
        because a future planner variant with finer-grained entries may want
        it, and because it is cheap and self-contained.
        """
        if len(entries) < 2 or weight <= 0:
            return entries

        for i in range(1, len(entries)):
            delta = entries[i].target_temp - entries[i - 1].target_temp
            abs_delta = abs(delta)
            if abs_delta > threshold:
                # Correction is proportional to overshoot beyond threshold,
                # but capped so we never override a >2°F efficiency gap
                correction = weight * (abs_delta - threshold)
                correction = min(correction, abs_delta * 0.5)  # never more than half
                if delta > 0:
                    entries[i].target_temp -= correction
                else:
                    entries[i].target_temp += correction
                # Re-round to thermostat resolution
                entries[i].target_temp = round(entries[i].target_temp * 2) / 2

        return entries

    # ── Display helpers ────────────────────────────────────────────

    def score_summary(self, hour_scores: list[HourScore]) -> str:
        """Human-readable summary of hour scores."""
        if not hour_scores:
            return "No scores available"

        lines = ["Hour  | Outdoor | Efficiency | Combined | Rank"]
        lines.append("-" * 55)

        ranked = sorted(hour_scores, key=lambda s: s.combined_score)
        rank_map = {hs.hour: i + 1 for i, hs in enumerate(ranked)}

        for hs in hour_scores:
            rank = rank_map[hs.hour]
            lines.append(
                f"{hs.hour.strftime('%H:%M')} | "
                f"{hs.outdoor_temp:5.1f}°F | "
                f"{hs.efficiency_score:10.3f} | "
                f"{hs.combined_score:8.3f} | "
                f"#{rank}"
            )

        return "\n".join(lines)
