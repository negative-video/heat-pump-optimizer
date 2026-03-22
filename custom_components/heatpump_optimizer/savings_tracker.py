"""Savings tracker — accumulates kWh, $, and CO2 savings over time.

Compares actual HVAC runtime against counterfactual baselines:
  1. Counterfactual digital twin: simulates the user's old thermostat schedule
     against actual weather, capturing runtime reduction, COP improvement,
     and rate/carbon arbitrage savings.
  2. Worst case: HVAC always on (60 min/hr) — the theoretical ceiling.
  3. Legacy ratio-based: fallback when counterfactual data isn't available yet.

Tracks per-hour records, produces daily reports, and maintains cumulative totals
that survive HA restarts via Store persistence.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any

from .engine.counterfactual_simulator import CounterfactualSimulator
from .engine.data_types import BaselineHourResult, DailySavingsReport, HourlySavingsRecord

_LOGGER = logging.getLogger(__name__)

# Keep 7 days of hourly records in memory for diagnostics
MAX_HOURLY_RECORDS = 7 * 24

# Savings accuracy tiers
TIER_LEARNING = "learning"
TIER_PROJECTED = "projected"
TIER_ESTIMATED = "estimated"
TIER_SIMULATED = "simulated"
TIER_CALIBRATED = "calibrated"


class SavingsTracker:
    """Tracks energy, cost, and CO2 savings from optimizer vs baseline."""

    def __init__(self) -> None:
        self._hourly_records: deque[HourlySavingsRecord] = deque(maxlen=MAX_HOURLY_RECORDS)

        # Cumulative all-time totals (persisted to HA Store)
        self._cumulative_kwh_saved: float = 0.0
        self._cumulative_cost_saved: float = 0.0
        self._cumulative_co2_saved_grams: float = 0.0
        self._cumulative_kwh_baseline: float = 0.0
        self._cumulative_kwh_actual: float = 0.0
        self._cumulative_kwh_worst_case: float = 0.0

        # Decomposed cumulative totals
        self._cumulative_runtime_savings_kwh: float = 0.0
        self._cumulative_cop_savings_kwh: float = 0.0
        self._cumulative_rate_arbitrage_savings: float = 0.0

        # Comfort tracking
        self._cumulative_comfort_violations: int = 0

        # Aux heat tracking
        self._cumulative_aux_heat_kwh: float = 0.0
        self._cumulative_avoided_aux_kwh: float = 0.0

        # Intra-hour accumulator (5-min intervals within the current hour)
        self._current_hour: int | None = None  # hour key (Unix timestamp // 3600)
        self._hour_actual_runtime_min: float = 0.0
        self._hour_power_readings: list[float] = []
        self._hour_co2_readings: list[float] = []
        self._hour_rate_readings: list[float] = []
        self._hour_cop_readings: list[float] = []
        self._hour_mode: str = "off"
        self._hour_solar_offset_kwh: float = 0.0
        self._hour_aux_kwh: float = 0.0  # incremental resistive kWh this hour

        # Counterfactual simulator reference (set by coordinator)
        self._counterfactual: CounterfactualSimulator | None = None

        # Legacy fallback ratio (used when counterfactual not yet available)
        self._baseline_to_optimized_ratio: float = 1.0

        # Current accuracy tier
        self._accuracy_tier: str = TIER_LEARNING

    def set_counterfactual(self, simulator: CounterfactualSimulator) -> None:
        """Set the counterfactual simulator reference."""
        self._counterfactual = simulator

    def set_accuracy_tier(self, tier: str) -> None:
        """Update the current accuracy tier."""
        if tier in (TIER_LEARNING, TIER_PROJECTED, TIER_ESTIMATED, TIER_SIMULATED, TIER_CALIBRATED):
            self._accuracy_tier = tier

    @property
    def accuracy_tier(self) -> str:
        """Current savings accuracy tier."""
        return self._accuracy_tier

    def set_baseline_ratio(
        self,
        baseline_runtime: float,
        optimized_runtime: float,
    ) -> None:
        """Update the legacy baseline/optimized ratio (fallback method).

        Used when counterfactual simulator doesn't have data yet.
        """
        if optimized_runtime > 0:
            self._baseline_to_optimized_ratio = baseline_runtime / optimized_runtime
        else:
            self._baseline_to_optimized_ratio = 1.0

    def record_interval(
        self,
        now: datetime,
        hvac_running: bool,
        interval_minutes: float,
        power_watts: float | None,
        carbon_intensity: float | None,
        electricity_rate: float | None,
        mode: str,
        solar_production_watts: float | None = None,
        grid_import_watts: float | None = None,
        actual_cop: float | None = None,
        aux_heat_active: bool = False,
        hp_baseline_watts: float = 0.0,
    ) -> None:
        """Record a single update interval (typically 5 minutes).

        Called every coordinator update cycle. Accumulates data within the
        current hour, and finalizes the previous hour when the hour boundary
        is crossed.
        """
        hour_key = int(now.timestamp()) // 3600

        # Hour boundary crossed — finalize the previous hour
        if self._current_hour is not None and hour_key != self._current_hour:
            self._finalize_hour()

        # Start new hour if needed
        if self._current_hour is None or hour_key != self._current_hour:
            self._current_hour = hour_key
            self._hour_actual_runtime_min = 0.0
            self._hour_power_readings = []
            self._hour_co2_readings = []
            self._hour_rate_readings = []
            self._hour_cop_readings = []
            self._hour_mode = mode
            self._hour_solar_offset_kwh = 0.0
            self._hour_grid_import_kwh = 0.0
            self._hour_aux_kwh = 0.0

        # Accumulate this interval
        if hvac_running:
            self._hour_actual_runtime_min += interval_minutes
            if power_watts is not None:
                self._hour_power_readings.append(power_watts)
            if carbon_intensity is not None:
                self._hour_co2_readings.append(carbon_intensity)
            if electricity_rate is not None:
                self._hour_rate_readings.append(electricity_rate)
            if actual_cop is not None:
                self._hour_cop_readings.append(actual_cop)

            # Incremental resistive kWh: only the power above HP baseline draw
            if aux_heat_active and power_watts is not None and power_watts > hp_baseline_watts:
                resistive_watts = power_watts - hp_baseline_watts
                self._hour_aux_kwh += resistive_watts * (interval_minutes / 60.0) / 1000.0

            # Track solar offset — prefer grid import for accuracy when available
            if grid_import_watts is not None and power_watts is not None:
                # Self-consumption = total power - grid import
                self_consumption = max(0.0, power_watts - grid_import_watts)
                solar_offset_kwh = self_consumption * (interval_minutes / 60.0) / 1000.0
                self._hour_solar_offset_kwh += solar_offset_kwh
                self._hour_grid_import_kwh += (
                    grid_import_watts * (interval_minutes / 60.0) / 1000.0
                )
            elif solar_production_watts is not None and power_watts is not None:
                solar_offset_watts = min(solar_production_watts, power_watts)
                solar_offset_kwh = solar_offset_watts * (interval_minutes / 60.0) / 1000.0
                self._hour_solar_offset_kwh += solar_offset_kwh

        self._hour_mode = mode

    def _finalize_hour(self) -> None:
        """Convert accumulated interval data into an HourlySavingsRecord.

        Uses the counterfactual simulator when available for accurate baseline
        comparison, falls back to the legacy ratio-based approach otherwise.
        """
        if self._current_hour is None:
            return

        hour_dt = datetime.fromtimestamp(
            self._current_hour * 3600, tz=timezone.utc
        )

        actual_min = self._hour_actual_runtime_min
        worst_case_min = 60.0

        # Average actual power draw for this hour
        avg_power = (
            sum(self._hour_power_readings) / len(self._hour_power_readings)
            if self._hour_power_readings
            else 0.0
        )

        # Average actual COP this hour
        avg_actual_cop = (
            sum(self._hour_cop_readings) / len(self._hour_cop_readings)
            if self._hour_cop_readings
            else None
        )

        # Actual energy
        actual_kwh = (actual_min / 60.0) * (avg_power / 1000.0)
        worst_case_kwh = (worst_case_min / 60.0) * (avg_power / 1000.0)

        # Solar offset
        solar_offset_kwh = self._hour_solar_offset_kwh
        grid_kwh = max(0.0, actual_kwh - solar_offset_kwh)

        # Try counterfactual simulator first
        baseline_result = None
        if self._counterfactual is not None:
            baseline_result = self._counterfactual.get_hour_result(self._current_hour)

        avoided_aux_kwh = baseline_result.avoided_aux_heat_kwh if baseline_result is not None else 0.0
        aux_heat_kwh = self._hour_aux_kwh

        # ── Baseline values ──────────────────────────────────────────
        if baseline_result is not None:
            # Counterfactual simulation — the good stuff
            baseline_min = baseline_result.runtime_minutes
            baseline_kwh = baseline_result.kwh
            baseline_cop = baseline_result.cop
            baseline_indoor_temp = baseline_result.avg_indoor_temp

            # Baseline cost and CO2 come from the counterfactual (different rates/carbon)
            baseline_cost = baseline_result.cost
            baseline_co2 = baseline_result.co2_grams

            # Decompose savings
            avg_actual_rate = (
                sum(self._hour_rate_readings) / len(self._hour_rate_readings)
                if self._hour_rate_readings
                else None
            )
            decomposition = self._counterfactual.decompose_savings(
                baseline_result=baseline_result,
                actual_runtime_min=actual_min,
                actual_power_watts=avg_power,
                actual_kwh=actual_kwh,
                actual_cop=avg_actual_cop,
                actual_rate=avg_actual_rate,
                baseline_rate=(
                    baseline_result.cost / baseline_result.kwh
                    if baseline_result.cost is not None and baseline_result.kwh > 0
                    else None
                ),
            )
            runtime_savings_kwh = decomposition["runtime_savings_kwh"]
            cop_savings_kwh = decomposition["cop_savings_kwh"]
            rate_arbitrage = decomposition["rate_arbitrage_savings"]
        else:
            # Legacy ratio-based fallback
            baseline_min = actual_min * self._baseline_to_optimized_ratio
            baseline_kwh = (baseline_min / 60.0) * (avg_power / 1000.0)
            baseline_cop = None
            baseline_indoor_temp = None
            baseline_cost = None
            baseline_co2 = None
            runtime_savings_kwh = baseline_kwh - actual_kwh
            cop_savings_kwh = 0.0
            rate_arbitrage = None

        saved_kwh = baseline_kwh - actual_kwh

        # ── Carbon ───────────────────────────────────────────────────
        avg_co2: float | None = None
        actual_co2: float | None = None
        saved_co2: float | None = None
        worst_case_co2: float | None = None
        if self._hour_co2_readings:
            avg_co2 = sum(self._hour_co2_readings) / len(self._hour_co2_readings)
            actual_co2 = grid_kwh * avg_co2
            worst_case_co2 = worst_case_kwh * avg_co2
            if baseline_co2 is None:
                # Legacy: use same carbon intensity for baseline
                baseline_co2 = baseline_kwh * avg_co2
            saved_co2 = baseline_co2 - actual_co2

        # ── Cost ─────────────────────────────────────────────────────
        avg_rate: float | None = None
        actual_cost: float | None = None
        saved_cost: float | None = None
        worst_case_cost: float | None = None
        if self._hour_rate_readings:
            avg_rate = sum(self._hour_rate_readings) / len(self._hour_rate_readings)
            actual_cost = grid_kwh * avg_rate
            worst_case_cost = worst_case_kwh * avg_rate
            if baseline_cost is None:
                # Legacy: use same rate for baseline
                baseline_cost = baseline_kwh * avg_rate
            saved_cost = baseline_cost - actual_cost

        record = HourlySavingsRecord(
            hour=hour_dt,
            mode=self._hour_mode,
            baseline_runtime_minutes=baseline_min,
            actual_runtime_minutes=actual_min,
            worst_case_runtime_minutes=worst_case_min,
            power_draw_watts=avg_power,
            baseline_kwh=baseline_kwh,
            actual_kwh=actual_kwh,
            saved_kwh=saved_kwh,
            worst_case_kwh=worst_case_kwh,
            solar_offset_kwh=solar_offset_kwh,
            grid_kwh=grid_kwh,
            carbon_intensity_gco2_kwh=avg_co2,
            baseline_co2_grams=baseline_co2,
            actual_co2_grams=actual_co2,
            saved_co2_grams=saved_co2,
            worst_case_co2_grams=worst_case_co2,
            electricity_rate=avg_rate,
            baseline_cost=baseline_cost,
            actual_cost=actual_cost,
            saved_cost=saved_cost,
            worst_case_cost=worst_case_cost,
            # Counterfactual digital twin fields
            baseline_cop=baseline_cop if baseline_result else None,
            actual_cop=avg_actual_cop,
            baseline_indoor_temp=baseline_indoor_temp,
            runtime_savings_kwh=runtime_savings_kwh,
            cop_savings_kwh=cop_savings_kwh,
            rate_arbitrage_savings=rate_arbitrage,
            aux_heat_kwh=aux_heat_kwh,
            avoided_aux_heat_kwh=avoided_aux_kwh,
        )

        self._hourly_records.append(record)

        # Update cumulative totals
        self._cumulative_kwh_saved += saved_kwh
        self._cumulative_kwh_baseline += baseline_kwh
        self._cumulative_kwh_actual += actual_kwh
        self._cumulative_kwh_worst_case += worst_case_kwh
        self._cumulative_runtime_savings_kwh += runtime_savings_kwh
        self._cumulative_cop_savings_kwh += cop_savings_kwh
        if saved_cost is not None:
            self._cumulative_cost_saved += saved_cost
        if saved_co2 is not None:
            self._cumulative_co2_saved_grams += saved_co2
        if rate_arbitrage is not None:
            self._cumulative_rate_arbitrage_savings += rate_arbitrage
        self._cumulative_aux_heat_kwh += aux_heat_kwh
        self._cumulative_avoided_aux_kwh += avoided_aux_kwh

        # Comfort violation tracking
        if baseline_indoor_temp is not None and self._counterfactual is not None:
            if self._counterfactual.is_baseline_comfort_violation():
                self._cumulative_comfort_violations += 1

        source = "counterfactual" if baseline_result else "ratio"
        _LOGGER.debug(
            "Hour finalized [%s]: actual=%.1f min, baseline=%.1f min, "
            "saved=%.3f kWh (runtime=%.3f, cop=%.3f), mode=%s",
            source,
            actual_min,
            baseline_min,
            saved_kwh,
            runtime_savings_kwh,
            cop_savings_kwh,
            self._hour_mode,
        )

    def today_report(self) -> DailySavingsReport:
        """Get savings report for the current calendar day (UTC)."""
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_hours = [
            r for r in self._hourly_records
            if r.hour.strftime("%Y-%m-%d") == today_str
        ]
        return DailySavingsReport(date=today_str, hours=today_hours)

    def cumulative_totals(self) -> dict[str, float]:
        """Get all-time cumulative savings totals."""
        return {
            "kwh_saved": self._cumulative_kwh_saved,
            "kwh_baseline": self._cumulative_kwh_baseline,
            "kwh_actual": self._cumulative_kwh_actual,
            "kwh_worst_case": self._cumulative_kwh_worst_case,
            "cost_saved": self._cumulative_cost_saved,
            "co2_saved_grams": self._cumulative_co2_saved_grams,
            # Decomposed totals
            "runtime_savings_kwh": self._cumulative_runtime_savings_kwh,
            "cop_savings_kwh": self._cumulative_cop_savings_kwh,
            "rate_arbitrage_savings": self._cumulative_rate_arbitrage_savings,
            "comfort_violations": self._cumulative_comfort_violations,
            "aux_heat_kwh": self._cumulative_aux_heat_kwh,
            "avoided_aux_kwh": self._cumulative_avoided_aux_kwh,
        }

    # ── Persistence ─────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialize for HA Store persistence."""
        return {
            "cumulative_kwh_saved": self._cumulative_kwh_saved,
            "cumulative_cost_saved": self._cumulative_cost_saved,
            "cumulative_co2_saved_grams": self._cumulative_co2_saved_grams,
            "cumulative_kwh_baseline": self._cumulative_kwh_baseline,
            "cumulative_kwh_actual": self._cumulative_kwh_actual,
            "cumulative_kwh_worst_case": self._cumulative_kwh_worst_case,
            "cumulative_runtime_savings_kwh": self._cumulative_runtime_savings_kwh,
            "cumulative_cop_savings_kwh": self._cumulative_cop_savings_kwh,
            "cumulative_rate_arbitrage_savings": self._cumulative_rate_arbitrage_savings,
            "cumulative_comfort_violations": self._cumulative_comfort_violations,
            "cumulative_aux_heat_kwh": self._cumulative_aux_heat_kwh,
            "cumulative_avoided_aux_kwh": self._cumulative_avoided_aux_kwh,
            "baseline_to_optimized_ratio": self._baseline_to_optimized_ratio,
            "accuracy_tier": self._accuracy_tier,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SavingsTracker:
        """Restore from persisted data."""
        tracker = cls()
        tracker._cumulative_kwh_saved = data.get("cumulative_kwh_saved", 0.0)
        tracker._cumulative_cost_saved = data.get("cumulative_cost_saved", 0.0)
        tracker._cumulative_co2_saved_grams = data.get("cumulative_co2_saved_grams", 0.0)
        tracker._cumulative_kwh_baseline = data.get("cumulative_kwh_baseline", 0.0)
        tracker._cumulative_kwh_actual = data.get("cumulative_kwh_actual", 0.0)
        tracker._cumulative_kwh_worst_case = data.get("cumulative_kwh_worst_case", 0.0)
        tracker._cumulative_runtime_savings_kwh = data.get("cumulative_runtime_savings_kwh", 0.0)
        tracker._cumulative_cop_savings_kwh = data.get("cumulative_cop_savings_kwh", 0.0)
        tracker._cumulative_rate_arbitrage_savings = data.get("cumulative_rate_arbitrage_savings", 0.0)
        tracker._cumulative_comfort_violations = data.get("cumulative_comfort_violations", 0)
        tracker._cumulative_aux_heat_kwh = data.get("cumulative_aux_heat_kwh", 0.0)
        tracker._cumulative_avoided_aux_kwh = data.get("cumulative_avoided_aux_kwh", 0.0)
        tracker._baseline_to_optimized_ratio = data.get(
            "baseline_to_optimized_ratio", 1.0
        )
        tracker._accuracy_tier = data.get("accuracy_tier", TIER_LEARNING)
        return tracker
