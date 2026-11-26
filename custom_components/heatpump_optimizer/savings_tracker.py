"""Savings tracker — accumulates kWh, $, and CO2 savings over time.

Energy is integrated from the HVAC power sensor interval by interval, so
`actual_kwh` is metered energy whenever a power entity is configured and
available.  Every record carries an `energy_source` label saying whether the
number came from a meter, from rated-watts estimation, or from a mix, because
"2.4 kWh" from a CT clamp and "2.4 kWh" from tonnage-times-SEER deserve very
different amounts of trust.

Two different baselines are reported, and they are not interchangeable:

  1. **Weather-normalized baseline (headline).**  Metered kWh compared against
     a degree-day regression learned from the user's own days when the
     optimizer was not controlling.  Both sides are measured; the only model is
     a two-slope line.  See learning/energy_baseline.py.
  2. **Counterfactual digital twin (modeled attribution).**  Simulates the
     user's old thermostat schedule against actual weather and decomposes the
     difference into runtime, COP, and rate components.  This is useful for
     answering "why", but it shares a performance model with the optimizer it
     is grading, so its errors can cancel or compound invisibly.  It is
     reported as modeled attribution with an uncertainty label, never as the
     headline number.

Also tracked: worst case (HVAC always on, 60 min/hr) as a theoretical ceiling,
and a legacy runtime-ratio fallback for before the counterfactual has data.

Tracks per-hour records, produces daily reports, and maintains cumulative totals
that survive HA restarts via Store persistence.
"""

from __future__ import annotations

import dataclasses

import logging
from collections import deque
from datetime import datetime, timezone, tzinfo
from typing import Any

from .engine.counterfactual_simulator import CounterfactualSimulator
from .engine.data_types import BaselineHourResult, DailySavingsReport, HourlySavingsRecord
from .learning.energy_baseline import (
    LABEL_OPTIMIZED,
    MIN_DEGREE_DAYS_FOR_FIT,
    EnergyBaseline,
)

_LOGGER = logging.getLogger(__name__)

# Keep 7 days of hourly records in memory for diagnostics
MAX_HOURLY_RECORDS = 7 * 24

# Savings accuracy tiers
TIER_LEARNING = "learning"
TIER_PROJECTED = "projected"
TIER_ESTIMATED = "estimated"
TIER_SIMULATED = "simulated"
TIER_CALIBRATED = "calibrated"

# Where an hour's energy number came from
ENERGY_SOURCE_MEASURED = "measured"
ENERGY_SOURCE_ESTIMATED = "estimated"
ENERGY_SOURCE_MIXED = "mixed"
ENERGY_SOURCE_NONE = "none"

# Fraction of an hour's energy that must come from a real sensor before the
# hour is called measured rather than mixed.
MEASURED_ENERGY_THRESHOLD = 0.99


# Version of the lifetime ledger's meaning. 2 = savings are only claimed for
# hours the optimizer was actually in control.
LEDGER_VERSION = 2

# Hours of hourly records kept across a restart (today plus yesterday).
PERSISTED_HOURLY_RECORDS = 48


def _record_to_dict(record: HourlySavingsRecord) -> dict[str, Any]:
    data = dataclasses.asdict(record)
    data["hour"] = record.hour.isoformat()
    return data


def _record_from_dict(raw: dict[str, Any]) -> HourlySavingsRecord | None:
    """Rebuild a persisted hourly record, ignoring fields we no longer have."""
    try:
        known = {f.name for f in dataclasses.fields(HourlySavingsRecord)}
        data = {k: v for k, v in raw.items() if k in known}
        data["hour"] = datetime.fromisoformat(raw["hour"])
        return HourlySavingsRecord(**data)
    except (KeyError, TypeError, ValueError):
        return None


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
        # True energy integration: sum of power x interval over the hour,
        # tracked separately from runtime so the number is metered rather
        # than reconstructed from runtime x rated watts.
        self._hour_energy_kwh: float = 0.0
        self._hour_measured_energy_kwh: float = 0.0
        self._hour_powered_minutes: float = 0.0
        self._hour_co2_readings: list[float] = []
        self._hour_rate_readings: list[float] = []
        self._hour_cop_readings: list[float] = []
        self._hour_mode: str = "off"
        self._hour_solar_offset_kwh: float = 0.0
        self._hour_grid_import_kwh: float = 0.0
        self._hour_aux_kwh: float = 0.0  # incremental resistive kWh this hour
        # Minutes of this hour the optimizer was actually steering the
        # thermostat. An hour it did not steer has no savings to claim.
        self._hour_controlled_minutes: float = 0.0
        self._hour_observed_minutes: float = 0.0

        # When the lifetime ledger was last zeroed (None = never). Published
        # as last_reset so recorder statistics treat it as a reset, not as a
        # sudden gain.
        self._ledger_reset_at: datetime | None = None

        # Counterfactual simulator reference (set by coordinator)
        self._counterfactual: CounterfactualSimulator | None = None

        # Weather-normalized measured baseline (set by coordinator).
        # This owns the headline savings number; the counterfactual above is
        # demoted to modeled attribution.
        self._energy_baseline: EnergyBaseline | None = None

        # Legacy fallback ratio (used when counterfactual not yet available)
        self._baseline_to_optimized_ratio: float = 1.0

        # Current accuracy tier
        self._accuracy_tier: str = TIER_LEARNING

    def set_counterfactual(self, simulator: CounterfactualSimulator) -> None:
        """Set the counterfactual simulator reference."""
        self._counterfactual = simulator

    def set_energy_baseline(self, baseline: EnergyBaseline) -> None:
        """Set the weather-normalized measured baseline reference."""
        self._energy_baseline = baseline

    def set_accuracy_tier(self, tier: str) -> None:
        """Update the current accuracy tier."""
        if tier in (TIER_LEARNING, TIER_PROJECTED, TIER_ESTIMATED, TIER_SIMULATED, TIER_CALIBRATED):
            # Reset cumulative totals when upgrading from pre-baseline tiers
            # to prevent stale phantom data from polluting real savings.
            if (
                self._accuracy_tier in (TIER_LEARNING, TIER_PROJECTED)
                and tier not in (TIER_LEARNING, TIER_PROJECTED)
            ):
                self._reset_cumulative()
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
        power_measured: bool = False,
        controlling: bool = True,
    ) -> None:
        """Record a single update interval (typically 5 minutes).

        Called every coordinator update cycle. Accumulates data within the
        current hour, and finalizes the previous hour when the hour boundary
        is crossed.

        Args:
            power_measured: True when power_watts came from a configured power
                sensor rather than a rated-watts estimate. Drives the
                energy_source label on the resulting hourly record.
            controlling: True when the optimizer was writing setpoints during
                this interval. Energy is metered either way; savings are only
                claimed for hours it was mostly in control. Monitor-only,
                paused and overridden time is baseline by definition.
        """
        hour_key = int(now.timestamp()) // 3600

        # Hour boundary crossed — finalize the previous hour
        if self._current_hour is not None and hour_key != self._current_hour:
            self._finalize_hour()

        # Start new hour if needed
        if self._current_hour is None or hour_key != self._current_hour:
            self._current_hour = hour_key
            self._hour_actual_runtime_min = 0.0
            self._hour_energy_kwh = 0.0
            self._hour_measured_energy_kwh = 0.0
            self._hour_powered_minutes = 0.0
            self._hour_co2_readings = []
            self._hour_rate_readings = []
            self._hour_cop_readings = []
            self._hour_mode = mode
            self._hour_solar_offset_kwh = 0.0
            self._hour_grid_import_kwh = 0.0
            self._hour_aux_kwh = 0.0
            self._hour_controlled_minutes = 0.0
            self._hour_observed_minutes = 0.0

        self._hour_observed_minutes += interval_minutes
        if controlling:
            self._hour_controlled_minutes += interval_minutes

        # Accumulate this interval
        if hvac_running:
            self._hour_actual_runtime_min += interval_minutes
            if power_watts is not None:
                # Integrate energy directly rather than reconstructing it from
                # runtime x a single average at the end of the hour.
                interval_kwh = power_watts * (interval_minutes / 60.0) / 1000.0
                self._hour_energy_kwh += interval_kwh
                self._hour_powered_minutes += interval_minutes
                if power_measured:
                    self._hour_measured_energy_kwh += interval_kwh
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

        # Average power while running, derived from the integrated energy so
        # that it stays consistent with the metered total.
        avg_power = (
            self._hour_energy_kwh * 1000.0 / (self._hour_powered_minutes / 60.0)
            if self._hour_powered_minutes > 0
            else 0.0
        )

        # Average actual COP this hour
        avg_actual_cop = (
            sum(self._hour_cop_readings) / len(self._hour_cop_readings)
            if self._hour_cop_readings
            else None
        )

        # Actual energy: the integrated total when every running interval had a
        # power reading, extended at the observed average when some intervals
        # were missing one.
        if self._hour_powered_minutes >= actual_min or self._hour_powered_minutes <= 0:
            actual_kwh = self._hour_energy_kwh
        else:
            actual_kwh = (actual_min / 60.0) * (avg_power / 1000.0)
        worst_case_kwh = (worst_case_min / 60.0) * (avg_power / 1000.0)

        # Where the energy number came from
        if self._hour_energy_kwh <= 0:
            energy_source = ENERGY_SOURCE_NONE
        else:
            measured_fraction = self._hour_measured_energy_kwh / self._hour_energy_kwh
            if measured_fraction >= MEASURED_ENERGY_THRESHOLD:
                energy_source = ENERGY_SOURCE_MEASURED
            elif measured_fraction > 0:
                energy_source = ENERGY_SOURCE_MIXED
            else:
                energy_source = ENERGY_SOURCE_ESTIMATED

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

        # An hour the optimizer did not steer is a baseline hour: whatever
        # the house used is what it would have used. The modeled baseline is
        # discarded rather than compared, otherwise every model error in
        # monitor-only mode lands in the ledger as a saving or a loss. That
        # is how an install that never wrote a setpoint showed -1,565 kWh.
        controlled = (
            self._hour_observed_minutes <= 0
            or self._hour_controlled_minutes >= 0.5 * self._hour_observed_minutes
        )
        if not controlled:
            baseline_min = actual_min
            baseline_kwh = actual_kwh
            baseline_cost = None
            baseline_co2 = None
            runtime_savings_kwh = 0.0
            cop_savings_kwh = 0.0
            rate_arbitrage = None
            avoided_aux_kwh = 0.0

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

        if not controlled:
            # Solar self-consumption would otherwise show up as a "saving".
            if saved_co2 is not None:
                saved_co2 = 0.0
            if saved_cost is not None:
                saved_cost = 0.0

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
            energy_source=energy_source,
        )

        self._hourly_records.append(record)

        # Lifetime totals. Metered quantities accumulate here, hour by hour.
        # The lifetime *saving* does not: it is banked once per completed day
        # from the measured, weather-normalized baseline, in
        # record_measured_day(). The hourly saving above compares against the
        # digital twin, whose absolute energy is not calibrated (replayed
        # against ten real November days it predicted a quarter of the
        # house's actual heating energy), so summing it produced a lifetime
        # figure dominated by twin error. The twin's decomposition is kept as
        # modeled attribution only.
        if self._accuracy_tier not in (TIER_LEARNING, TIER_PROJECTED):
            # Until a day's measured saving is banked, the baseline is the
            # metered energy itself: no claim either way.
            self._cumulative_kwh_baseline += actual_kwh
            self._cumulative_kwh_actual += actual_kwh
            self._cumulative_kwh_worst_case += worst_case_kwh
            self._cumulative_aux_heat_kwh += aux_heat_kwh
            # Modeled attribution (diagnostic)
            self._cumulative_runtime_savings_kwh += runtime_savings_kwh
            self._cumulative_cop_savings_kwh += cop_savings_kwh
            if rate_arbitrage is not None:
                self._cumulative_rate_arbitrage_savings += rate_arbitrage
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

    def today_report(self, tz: tzinfo | None = None) -> DailySavingsReport:
        """Get savings report for the current calendar day.

        Args:
            tz: Timezone for determining "today". Defaults to UTC if not provided.
        """
        if tz is not None:
            today_str = datetime.now(tz).strftime("%Y-%m-%d")
        else:
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_hours = [
            r for r in self._hourly_records
            if r.hour.strftime("%Y-%m-%d") == today_str
        ]
        return DailySavingsReport(date=today_str, hours=today_hours)

    # ── Headline: measured vs weather-normalized baseline ────────────

    def measured_savings_today(self) -> dict[str, Any] | None:
        """Running metered-vs-baseline comparison for the current day.

        Returns None when no weather-normalized baseline has been learned yet,
        rather than falling back to a modeled number. A missing headline is
        more useful than a confident wrong one.
        """
        if self._energy_baseline is None:
            return None
        return self._energy_baseline.today_estimate()

    def measured_savings_window(self, days: int = 30) -> dict[str, Any] | None:
        """Metered-vs-baseline over recent whole days.

        Whole completed days only, so there is no partial-day extrapolation.
        This is the number to trust.
        """
        if self._energy_baseline is None:
            return None
        return self._energy_baseline.window_summary(days=days)

    def modeled_attribution(self, report: DailySavingsReport) -> dict[str, Any]:
        """Digital-twin decomposition, explicitly labelled as modeled.

        These components explain where the optimizer *thinks* the difference
        came from. They are produced by simulating the old schedule with the
        same performance model that drives the optimizer, so they inherit its
        errors and are not independent evidence of savings. Use them to answer
        "why", never "how much".
        """
        return {
            "method": "counterfactual digital twin",
            "confidence": "modeled, not measured",
            "uncertainty": (
                "Simulated with the same thermal model the optimizer uses, so "
                "these components cannot confirm the savings they describe. "
                "Check the weather-normalized measured savings instead."
            ),
            "runtime_component_kwh": report.total_runtime_savings_kwh,
            "modeled_cop_effect_kwh": report.total_cop_savings_kwh,
            "rate_arbitrage_component": report.total_rate_arbitrage_savings,
            "baseline_kwh": report.total_baseline_kwh,
            "actual_kwh": report.total_actual_kwh,
            "energy_source": report.energy_source,
        }

    def record_measured_day(self, record: Any) -> None:
        """Bank one completed day's measured saving into the lifetime ledger.

        Registered as a day listener on the energy baseline. Only days the
        optimizer controlled, with good sensor coverage, enough weather to
        normalize against and a usable baseline fit are counted. Both sides
        of the comparison are metered kWh: what the house used that day, and
        what the same weather cost on days the optimizer was not running.
        """
        baseline = self._energy_baseline
        if baseline is None or record is None:
            return
        if getattr(record, "label", None) != LABEL_OPTIMIZED or not record.usable:
            return
        if record.degree_days < MIN_DEGREE_DAYS_FOR_FIT:
            return
        fit = baseline.fit()
        if not fit.is_usable:
            return

        expected = fit.expected_kwh(record.cdd, record.hdd)
        saved = expected - record.kwh
        self._cumulative_kwh_saved += saved
        # Keeps baseline - actual == saved for the lifetime totals
        self._cumulative_kwh_baseline += saved

        recent = list(self._hourly_records)[-24:]
        rates = [r.electricity_rate for r in recent if r.electricity_rate is not None]
        carbon = [
            r.carbon_intensity_gco2_kwh for r in recent
            if r.carbon_intensity_gco2_kwh is not None
        ]
        if rates:
            self._cumulative_cost_saved += saved * (sum(rates) / len(rates))
        if carbon:
            self._cumulative_co2_saved_grams += saved * (sum(carbon) / len(carbon))
        _LOGGER.debug(
            "Lifetime ledger: %s measured %.2f kWh vs expected %.2f (saved %.2f)",
            record.date, record.kwh, expected, saved,
        )

    @property
    def ledger_reset_at(self) -> datetime | None:
        """When the lifetime totals were last zeroed, if ever."""
        return self._ledger_reset_at

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

    def _reset_cumulative(self) -> None:
        """Zero all cumulative totals (used on tier upgrade from pre-baseline)."""
        self._cumulative_kwh_saved = 0.0
        self._cumulative_kwh_baseline = 0.0
        self._cumulative_kwh_actual = 0.0
        self._cumulative_kwh_worst_case = 0.0
        self._cumulative_cost_saved = 0.0
        self._cumulative_co2_saved_grams = 0.0
        self._cumulative_runtime_savings_kwh = 0.0
        self._cumulative_cop_savings_kwh = 0.0
        self._cumulative_rate_arbitrage_savings = 0.0
        self._cumulative_comfort_violations = 0
        self._cumulative_aux_heat_kwh = 0.0
        self._cumulative_avoided_aux_kwh = 0.0
        _LOGGER.info("Cumulative savings totals reset")

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
            "ledger_version": LEDGER_VERSION,
            "ledger_reset_at": (
                self._ledger_reset_at.isoformat() if self._ledger_reset_at else None
            ),
            # Recent hours, so "today" survives a restart
            "hourly_records": [
                _record_to_dict(r) for r in list(self._hourly_records)[-PERSISTED_HOURLY_RECORDS:]
            ],
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

        reset_at = data.get("ledger_reset_at")
        if reset_at:
            try:
                tracker._ledger_reset_at = datetime.fromisoformat(reset_at)
            except (TypeError, ValueError):
                tracker._ledger_reset_at = None

        for raw in data.get("hourly_records") or []:
            record = _record_from_dict(raw)
            if record is not None:
                tracker._hourly_records.append(record)

        if data.get("ledger_version", 1) < LEDGER_VERSION:
            # Totals written before the ledger was gated on control. They
            # compared a model against a model whether or not the optimizer
            # was running, so they are not savings and cannot be corrected,
            # only retired. Recorder statistics keep the old history.
            had = tracker._cumulative_kwh_saved
            if any(abs(v) > 1e-9 for v in tracker.cumulative_totals().values()):
                tracker._reset_cumulative()
                tracker._ledger_reset_at = datetime.now(timezone.utc)
                _LOGGER.info(
                    "Lifetime savings ledger predates control gating "
                    "(%.1f kWh recorded); starting a fresh ledger",
                    had,
                )
        return tracker
