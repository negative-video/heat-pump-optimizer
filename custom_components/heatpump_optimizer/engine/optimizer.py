"""Schedule optimizer that minimizes runtime by shifting HVAC load to efficient hours.

Two approaches:
1. Work-based analysis: Given actual runtime per hour, calculate how much less
   runtime would be needed if that work were done at a more efficient hour.
2. Simulation-based: Generate setpoint schedules and simulate forward.

The work-based approach is more reliable because it doesn't depend on simulation
accuracy. It directly uses the Beestat delta ratios.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .data_types import (
    ForecastPoint,
    HourScore,
    OptimizationWeights,
    OptimizedSchedule,
    ScheduleEntry,
)
from .performance_model import PerformanceModel
from .thermal_simulator import ThermalSimulator


class ScheduleOptimizer:
    """Finds optimal runtime distribution based on efficiency ratios."""

    def __init__(self, model: PerformanceModel, simulator: ThermalSimulator):
        self.model = model
        self.simulator = simulator

    def score_hours(
        self,
        forecast: list[ForecastPoint],
        mode: str,
        weights: OptimizationWeights | None = None,
    ) -> list[HourScore]:
        """Score each hour in the forecast by HVAC efficiency.

        Lower score = better time to run HVAC.

        For cooling: score = 1 / |cooling_delta(outdoor_temp)|
          75°F outdoor: score = 1/3.36 = 0.30 (good - run now)
          96°F outdoor: score = 1/0.66 = 1.52 (bad - avoid running)
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

            if mode == "cool":
                delta = abs(self.model.cooling_delta(avg_temp))
                efficiency_score = 1.0 / max(delta, 0.01)
            elif mode == "heat":
                delta = max(self.model.heating_delta(avg_temp), 0.01)
                efficiency_score = 1.0 / delta
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

    # ── Work-based optimizer ───────────────────────────────────────

    def analyze_runtime_efficiency(
        self,
        hourly_runtime: dict[int, float],
        hourly_outdoor_temp: dict[int, float],
        mode: str,
    ) -> dict:
        """Analyze actual runtime and calculate savings from optimal redistribution.

        This is the most reliable optimization method: it takes actual runtime
        per hour and calculates how much runtime would be needed if the same
        "cooling work" were done at the most efficient hours.

        Args:
            hourly_runtime: {hour_of_day: minutes_of_runtime}
            hourly_outdoor_temp: {hour_of_day: avg_outdoor_temp}
            mode: "cool" or "heat"

        Returns:
            Dict with actual_runtime, theoretical_min_runtime, savings_pct,
            and per-hour breakdown.
        """
        if mode == "cool":
            get_delta = lambda t: abs(self.model.cooling_delta(t))
        else:
            get_delta = lambda t: max(self.model.heating_delta(t), 0.01)

        # Calculate "cooling work units" done in each hour
        # Work = runtime * effectiveness (delta magnitude)
        # 1 hour at -3.36°F/hr delta = 3.36 work units
        # 1 hour at -0.66°F/hr delta = 0.66 work units
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
        """Generate an optimized setpoint schedule using simulation.

        This approach generates variable setpoints (pre-cool during efficient
        hours, coast during inefficient hours) and simulates forward to
        estimate runtime.

        If forecast points carry per-hour comfort_min/comfort_max (stamped by
        the strategic controller from a calendar occupancy timeline), those
        are used instead of the single comfort_range for each hour.
        """
        if weights is None:
            weights = OptimizationWeights()

        comfort_min, comfort_max = comfort_range
        hour_scores = self.score_hours(forecast, mode, weights)

        # Build per-hour comfort ranges from forecast annotations (if present)
        per_hour_comfort = self._extract_per_hour_comfort(forecast)

        # Baseline: constant midpoint (use per-hour midpoints when available)
        midpoint = (comfort_min + comfort_max) / 2
        baseline_sim = self.simulator.simulate_constant_setpoint(
            current_indoor_temp, forecast, midpoint, mode
        )
        baseline_runtime = self.simulator.total_runtime(baseline_sim)

        # Optimized: variable setpoints
        optimized_entries = self._build_schedule(
            hour_scores, comfort_range, mode, per_hour_comfort
        )
        optimized_sim = self.simulator.simulate(
            current_indoor_temp, forecast, optimized_entries
        )
        optimized_runtime = self.simulator.total_runtime(optimized_sim)

        violations = self.simulator.comfort_violations(
            optimized_sim, comfort_min, comfort_max
        )

        # Fallback if comfort violated
        if violations > 0:
            narrowed = (
                (comfort_min + midpoint) / 2,
                (comfort_max + midpoint) / 2,
            )
            optimized_entries = self._build_schedule(
                hour_scores, narrowed, mode
            )
            optimized_sim = self.simulator.simulate(
                current_indoor_temp, forecast, optimized_entries
            )
            optimized_runtime = self.simulator.total_runtime(optimized_sim)
            violations = self.simulator.comfort_violations(
                optimized_sim, comfort_min, comfort_max
            )

        savings_pct = 0.0
        if baseline_runtime > 0:
            savings_pct = (baseline_runtime - optimized_runtime) / baseline_runtime * 100

        return OptimizedSchedule(
            entries=optimized_entries,
            baseline_runtime_minutes=baseline_runtime,
            optimized_runtime_minutes=optimized_runtime,
            savings_pct=savings_pct,
            comfort_violations=violations,
            simulation=optimized_sim,
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

    def _build_schedule(
        self,
        hour_scores: list[HourScore],
        comfort_range: tuple[float, float],
        mode: str,
        per_hour_comfort: dict[datetime, tuple[float, float]] | None = None,
    ) -> list[ScheduleEntry]:
        """Build setpoint schedule from hour scores.

        When per_hour_comfort is provided, each hour uses its own comfort
        band (wider during AWAY, tighter during HOME). Otherwise, the single
        comfort_range applies uniformly.
        """
        if not hour_scores:
            return []

        comfort_min, comfort_max = comfort_range

        ranked = sorted(hour_scores, key=lambda s: s.combined_score)
        n = len(ranked)
        score_percentiles = {hs.hour: i / max(n - 1, 1) for i, hs in enumerate(ranked)}

        entries = []
        for hs in hour_scores:
            pct = score_percentiles[hs.hour]

            # Use per-hour comfort if available, otherwise global range
            if per_hour_comfort and hs.hour in per_hour_comfort:
                h_min, h_max = per_hour_comfort[hs.hour]
            else:
                h_min, h_max = comfort_min, comfort_max
            h_band = h_max - h_min

            if mode == "cool":
                target = h_min + pct * h_band
            elif mode == "heat":
                target = h_max - pct * h_band
            else:
                target = (h_min + h_max) / 2

            target = round(target * 2) / 2  # 0.5°F thermostat resolution

            if pct < 0.33:
                action = "pre-cooling" if mode == "cool" else "pre-heating"
            elif pct < 0.67:
                action = "maintaining"
            else:
                action = "coasting"

            entries.append(
                ScheduleEntry(
                    start_time=hs.hour,
                    end_time=hs.hour + timedelta(hours=1),
                    target_temp=target,
                    mode=mode,
                    reason=f"{hs.outdoor_temp:.0f}°F outdoor: {action} (target {target:.1f}°F)",
                )
            )

        entries.sort(key=lambda e: e.start_time)
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
