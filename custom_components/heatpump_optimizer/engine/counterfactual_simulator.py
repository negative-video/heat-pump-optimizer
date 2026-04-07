"""Counterfactual simulator -- the digital twin that answers "what if?"

Runs a parallel simulation of a "shadow house" following the user's
configured thermostat schedule (dual-setpoint heat_cool) against real
weather conditions.  By comparing what this virtual house would have
consumed vs what the optimizer actually consumed, we produce meaningful
savings metrics that capture:

1. Runtime reduction -- fewer total minutes of compressor operation
2. COP improvement -- running at better outdoor temps (time-shifting)
3. Rate/carbon arbitrage -- running at cheaper/cleaner times

The simulator uses the profiler-measured PerformanceModel for passive
drift and HVAC deltas, making the digital twin as accurate as the
profiler's empirical trendlines (4x more accurate than EKF-based physics).
"""

from __future__ import annotations

import logging
import math
from collections import deque
from datetime import datetime, timezone
from typing import Any

from ..engine.data_types import BaselineHourResult

# COP model constants (textbook, independent of thermal model)
ALPHA_COOL = 0.012   # COP degradation per degree above 75F
ALPHA_HEAT = 0.015   # COP degradation per degree below 75F
T_REF_F = 75.0

_LOGGER = logging.getLogger(__name__)

# Default thermostat hysteresis (deadband)
THERMOSTAT_DEADBAND_F = 0.5

# Virtual state drift reset: blend toward real state weekly
DRIFT_RESET_INTERVAL_HOURS = 168  # 7 days
DRIFT_RESET_BLEND_FACTOR = 0.3  # 30% pull toward real state

# Maximum hourly records kept for reporting
MAX_HOUR_RECORDS = 7 * 24

# Default HVAC capacity when no model/tonnage available (BTU/hr)
_DEFAULT_Q_COOL = 30000.0
_DEFAULT_Q_HEAT = 30000.0


class CounterfactualSimulator:
    """Simulates what would have happened under the user's normal thermostat.

    Uses a dual-setpoint heat_cool thermostat model: heats when indoor temp
    drops below setpoint_low, cools when it rises above setpoint_high,
    idles in between.  This matches how most modern thermostats operate.
    """

    def __init__(
        self, initial_temp: float = 72.0, deadband: float = THERMOSTAT_DEADBAND_F
    ) -> None:
        # Virtual house state (single-node -- no mass tracking needed
        # with profiler model, which captures mass effects in aggregate)
        self._T_air: float = initial_temp
        self._deadband: float = deadband

        # Intra-hour accumulators
        self._current_hour_key: int | None = None
        self._hour_runtime_min: float = 0.0
        self._hour_power_readings: list[float] = []
        self._hour_cop_readings: list[float] = []
        self._hour_temp_readings: list[float] = []
        self._hour_co2_readings: list[float] = []
        self._hour_rate_readings: list[float] = []
        self._hour_mode: str = "off"
        self._hour_avoided_aux_kwh: float = 0.0

        # Completed hour results (rolling window)
        self._hour_results: deque[tuple[int, BaselineHourResult]] = deque(
            maxlen=MAX_HOUR_RECORDS
        )

        # Drift reset tracking
        self._hours_since_reset: int = 0

        # Comfort tracking
        self._comfort_bounds: tuple[float, float] = (64.0, 78.0)

        # Rated HVAC capacity for power/COP calculations (set from tonnage)
        self._rated_q_cool: float = _DEFAULT_Q_COOL
        self._rated_q_heat: float = _DEFAULT_Q_HEAT

    def set_comfort_bounds(self, comfort_min: float, comfort_max: float) -> None:
        """Set the comfort bounds for tracking baseline comfort violations."""
        self._comfort_bounds = (comfort_min, comfort_max)

    def set_rated_capacity(self, q_cool: float, q_heat: float) -> None:
        """Set rated HVAC capacity from user's tonnage config."""
        self._rated_q_cool = q_cool
        self._rated_q_heat = q_heat

    def step(
        self,
        now: datetime,
        outdoor_temp: float,
        setpoint_low: float,
        setpoint_high: float,
        model: object,
        dt_minutes: float = 5.0,
        cloud_cover: float | None = None,
        sun_elevation: float | None = None,
        carbon_intensity: float | None = None,
        electricity_rate: float | None = None,
        real_indoor_temp: float | None = None,
        aux_threshold_f: float | None = None,
    ) -> None:
        """Advance the virtual house by one time step.

        Args:
            now: Current time (UTC).
            outdoor_temp: Actual outdoor temperature (F).
            setpoint_low: Heating setpoint (heat when below).
            setpoint_high: Cooling setpoint (cool when above).
            model: PerformanceModel (profiler-measured) for drift/delta.
            dt_minutes: Time step in minutes (default 5).
            cloud_cover: 0.0-1.0 cloud cover fraction.
            sun_elevation: Degrees above horizon.
            carbon_intensity: Grid carbon intensity (gCO2/kWh).
            electricity_rate: Electricity rate ($/kWh).
            real_indoor_temp: Actual house temp (for periodic drift reset).
            aux_threshold_f: Learned outdoor temp below which aux heat triggers.
        """
        hour_key = int(now.timestamp()) // 3600

        # Hour boundary crossed -- finalize previous hour
        if self._current_hour_key is not None and hour_key != self._current_hour_key:
            self._finalize_hour()
            self._hours_since_reset += 1

            if (
                self._hours_since_reset >= DRIFT_RESET_INTERVAL_HOURS
                and real_indoor_temp is not None
            ):
                self._drift_reset(real_indoor_temp)
                self._hours_since_reset = 0

        # Start new hour if needed
        if self._current_hour_key is None or hour_key != self._current_hour_key:
            self._current_hour_key = hour_key
            self._hour_runtime_min = 0.0
            self._hour_power_readings = []
            self._hour_cop_readings = []
            self._hour_temp_readings = []
            self._hour_co2_readings = []
            self._hour_rate_readings = []
            self._hour_mode = "off"
            self._hour_avoided_aux_kwh = 0.0

        # Dual-setpoint thermostat decision
        hvac_mode, hvac_running = self._dual_setpoint_decision(
            self._T_air, setpoint_low, setpoint_high,
        )

        # Determine solar condition for profiler model
        solar_cond = self._classify_solar(cloud_cover, sun_elevation)

        # Advance virtual house using profiler model
        dt_hours = dt_minutes / 60.0
        if hvac_running and hvac_mode == "cool":
            rate = model.cooling_delta(outdoor_temp, self._T_air)
        elif hvac_running and hvac_mode == "heat":
            rate = model.heating_delta(outdoor_temp, self._T_air)
        else:
            rate = model.passive_drift(
                outdoor_temp, self._T_air, solar_condition=solar_cond,
            )

        self._T_air += rate * dt_hours
        self._hour_temp_readings.append(self._T_air)

        if hvac_running:
            self._hour_runtime_min += dt_minutes
            self._hour_mode = hvac_mode

            cop = self._cop_at_outdoor_temp(outdoor_temp, hvac_mode)
            capacity_btu = self._capacity_at_outdoor_temp(outdoor_temp, hvac_mode)
            power_watts = (capacity_btu / cop) * 0.293071 if cop > 0 else 0.0

            self._hour_power_readings.append(power_watts)
            self._hour_cop_readings.append(cop)

            if carbon_intensity is not None:
                self._hour_co2_readings.append(carbon_intensity)
            if electricity_rate is not None:
                self._hour_rate_readings.append(electricity_rate)

        # Shadow aux heat detection
        if (
            aux_threshold_f is not None
            and hvac_mode == "heat"
            and outdoor_temp < aux_threshold_f
            and self._T_air < setpoint_low - 1.0
        ):
            aux_strip_watts = self._rated_q_heat * 0.293071
            avoided_kwh = aux_strip_watts * (dt_minutes / 60.0) / 1000.0
            self._hour_avoided_aux_kwh += avoided_kwh

    def get_hour_result(self, hour_key: int) -> BaselineHourResult | None:
        """Get the counterfactual result for a specific hour."""
        for key, result in self._hour_results:
            if key == hour_key:
                return result
        return None

    def get_latest_hour_result(self) -> BaselineHourResult | None:
        """Get the most recently finalized hour result."""
        if self._hour_results:
            return self._hour_results[-1][1]
        return None

    @property
    def virtual_indoor_temp(self) -> float:
        """Current virtual house indoor temperature."""
        return self._T_air

    @property
    def virtual_mass_temp(self) -> float:
        """Current virtual house thermal mass temperature.

        With profiler-primary model, mass is not tracked separately.
        Return T_air for backward compatibility with sensors/dashboard.
        """
        return self._T_air

    def is_baseline_comfort_violation(self) -> bool:
        """Whether the virtual house is currently outside comfort bounds."""
        lo, hi = self._comfort_bounds
        return self._T_air < lo or self._T_air > hi

    # ── Internal Methods ─────────────────────────────────────────────

    def _dual_setpoint_decision(
        self,
        indoor_temp: float,
        setpoint_low: float,
        setpoint_high: float,
    ) -> tuple[str, bool]:
        """Dual-setpoint heat_cool thermostat logic.

        Returns (mode, running):
        - ("heat", True) when indoor drops below setpoint_low - deadband
        - ("cool", True) when indoor rises above setpoint_high + deadband
        - ("off", False) when indoor is in the dead band between setpoints
        """
        if indoor_temp < setpoint_low - self._deadband:
            return "heat", True
        if indoor_temp > setpoint_high + self._deadband:
            return "cool", True
        return "off", False

    @staticmethod
    def _classify_solar(
        cloud_cover: float | None,
        sun_elevation: float | None,
    ) -> str | None:
        """Classify solar condition from current weather data."""
        if sun_elevation is not None and sun_elevation <= 0:
            return "night"
        if cloud_cover is not None:
            if cloud_cover < 0.3:
                return "sunny"
            return "cloudy"
        return None

    def _cop_at_outdoor_temp(self, outdoor_temp: float, mode: str) -> float:
        """Estimate COP at a given outdoor temperature."""
        if mode == "cool":
            base_cop = 3.5
            degradation = ALPHA_COOL * (outdoor_temp - T_REF_F)
            return max(1.0, base_cop * (1.0 - degradation))
        elif mode == "heat":
            base_cop = 3.0
            degradation = ALPHA_HEAT * (T_REF_F - outdoor_temp)
            return max(1.0, base_cop * (1.0 - degradation))
        return 1.0

    def _capacity_at_outdoor_temp(self, outdoor_temp: float, mode: str) -> float:
        """HVAC capacity at given outdoor temp (BTU/hr, always positive)."""
        if mode == "cool":
            cop_factor = max(0.1, 1.0 - ALPHA_COOL * (outdoor_temp - T_REF_F))
            return self._rated_q_cool * cop_factor
        elif mode == "heat":
            cop_factor = max(0.1, 1.0 - ALPHA_HEAT * (T_REF_F - outdoor_temp))
            return self._rated_q_heat * cop_factor
        return 0.0

    def _finalize_hour(self) -> None:
        """Convert accumulated interval data into a BaselineHourResult."""
        if self._current_hour_key is None:
            return

        runtime_min = self._hour_runtime_min

        avg_power = (
            sum(self._hour_power_readings) / len(self._hour_power_readings)
            if self._hour_power_readings
            else 0.0
        )
        avg_cop = (
            sum(self._hour_cop_readings) / len(self._hour_cop_readings)
            if self._hour_cop_readings
            else None
        )
        avg_indoor_temp = (
            sum(self._hour_temp_readings) / len(self._hour_temp_readings)
            if self._hour_temp_readings
            else self._T_air
        )

        kwh = (runtime_min / 60.0) * (avg_power / 1000.0)

        cost = None
        if self._hour_rate_readings:
            avg_rate = sum(self._hour_rate_readings) / len(self._hour_rate_readings)
            cost = kwh * avg_rate

        co2_grams = None
        if self._hour_co2_readings:
            avg_co2 = sum(self._hour_co2_readings) / len(self._hour_co2_readings)
            co2_grams = kwh * avg_co2

        result = BaselineHourResult(
            runtime_minutes=runtime_min,
            power_watts=avg_power,
            kwh=kwh,
            cost=cost,
            co2_grams=co2_grams,
            cop=avg_cop,
            avg_indoor_temp=round(avg_indoor_temp, 1),
            avoided_aux_heat_kwh=self._hour_avoided_aux_kwh,
        )

        self._hour_results.append((self._current_hour_key, result))

    def _drift_reset(self, real_indoor_temp: float) -> None:
        """Blend virtual T_air toward real state to prevent unbounded drift."""
        old = self._T_air
        self._T_air = (
            self._T_air * (1 - DRIFT_RESET_BLEND_FACTOR)
            + real_indoor_temp * DRIFT_RESET_BLEND_FACTOR
        )
        _LOGGER.debug(
            "Counterfactual drift reset: %.1fF -> %.1fF (real=%.1fF)",
            old, self._T_air, real_indoor_temp,
        )

    # ── Savings Decomposition ────────────────────────────────────────

    def decompose_savings(
        self,
        baseline_result: BaselineHourResult,
        actual_runtime_min: float,
        actual_power_watts: float,
        actual_kwh: float,
        actual_cop: float | None,
        actual_rate: float | None,
        baseline_rate: float | None,
    ) -> dict[str, float]:
        """Decompose total savings into three components."""
        total_saved_kwh = baseline_result.kwh - actual_kwh

        if baseline_result.power_watts > 0 and baseline_result.runtime_minutes > 0:
            runtime_diff_min = baseline_result.runtime_minutes - actual_runtime_min
            runtime_savings_kwh = (runtime_diff_min / 60.0) * (baseline_result.power_watts / 1000.0)
        else:
            runtime_savings_kwh = 0.0

        cop_savings_kwh = total_saved_kwh - runtime_savings_kwh

        rate_arbitrage = None
        if actual_rate is not None and baseline_rate is not None:
            rate_arbitrage = actual_kwh * (baseline_rate - actual_rate)

        return {
            "runtime_savings_kwh": max(0.0, runtime_savings_kwh),
            "cop_savings_kwh": cop_savings_kwh,
            "rate_arbitrage_savings": rate_arbitrage,
        }

    # ── Persistence ─────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialize for HA Store persistence."""
        return {
            "T_air": self._T_air,
            "deadband": self._deadband,
            "hours_since_reset": self._hours_since_reset,
            "comfort_bounds": list(self._comfort_bounds),
            "rated_q_cool": self._rated_q_cool,
            "rated_q_heat": self._rated_q_heat,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CounterfactualSimulator:
        """Restore from persisted data."""
        sim = cls(
            initial_temp=data.get("T_air", 72.0),
            deadband=data.get("deadband", THERMOSTAT_DEADBAND_F),
        )
        sim._hours_since_reset = data.get("hours_since_reset", 0)
        bounds = data.get("comfort_bounds", [64.0, 78.0])
        if isinstance(bounds, list) and len(bounds) == 2:
            sim._comfort_bounds = (bounds[0], bounds[1])
        sim._rated_q_cool = data.get("rated_q_cool", _DEFAULT_Q_COOL)
        sim._rated_q_heat = data.get("rated_q_heat", _DEFAULT_Q_HEAT)
        # Migration: ignore legacy T_mass field
        return sim
