"""Counterfactual simulator — the digital twin that answers "what if?"

Runs a parallel simulation of a "shadow house" following the user's old
thermostat schedule against real weather conditions. By comparing what this
virtual house would have consumed vs what the optimizer actually consumed,
we produce meaningful savings metrics that capture:

1. Runtime reduction — fewer total minutes of compressor operation
2. COP improvement — running at better outdoor temps (time-shifting)
3. Rate/carbon arbitrage — running at cheaper/cleaner times

The simulator maintains its own virtual indoor temperature state (T_air, T_mass)
that evolves independently from the real house using the same physics model
(the EKF's two-node RC thermal circuit) but with a standard thermostat controller
instead of the optimizer.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from datetime import datetime, timezone
from typing import Any

from ..engine.data_types import BaselineHourResult, BaselineScheduleTemplate
from ..learning.thermal_estimator import (
    ALPHA_COOL,
    ALPHA_HEAT,
    DEFAULT_INTERNAL_GAIN_BTU,
    DEFAULT_SOLAR_GAIN_BTU,
    IDX_C_INV,
    IDX_C_MASS_INV,
    IDX_Q_COOL,
    IDX_Q_HEAT,
    IDX_R_INT_INV,
    IDX_R_INV,
    T_REF_F,
    ThermalEstimator,
)

_LOGGER = logging.getLogger(__name__)

# Standard thermostat hysteresis (deadband)
THERMOSTAT_DEADBAND_F = 0.5

# Virtual state drift reset: blend toward real state weekly
DRIFT_RESET_INTERVAL_HOURS = 168  # 7 days
DRIFT_RESET_BLEND_FACTOR = 0.3  # 30% pull toward real state

# Maximum hourly records kept for reporting
MAX_HOUR_RECORDS = 7 * 24


class CounterfactualSimulator:
    """Simulates what would have happened under the user's old schedule.

    Maintains a virtual house with its own indoor temperature state,
    controlled by a simple thermostat following the baseline schedule.
    Uses the same physics model as the real optimizer (EKF parameters)
    but different control logic.
    """

    def __init__(self, initial_temp: float = 72.0) -> None:
        # Virtual house state
        self._T_air: float = initial_temp
        self._T_mass: float = initial_temp

        # Intra-hour accumulators
        self._current_hour_key: int | None = None
        self._hour_runtime_min: float = 0.0
        self._hour_power_readings: list[float] = []
        self._hour_cop_readings: list[float] = []
        self._hour_temp_readings: list[float] = []
        self._hour_co2_readings: list[float] = []
        self._hour_rate_readings: list[float] = []
        self._hour_mode: str = "off"

        # Completed hour results (rolling window)
        self._hour_results: deque[tuple[int, BaselineHourResult]] = deque(
            maxlen=MAX_HOUR_RECORDS
        )

        # Drift reset tracking
        self._hours_since_reset: int = 0

        # Comfort tracking: hours where baseline exceeded bounds
        self._comfort_bounds: tuple[float, float] = (64.0, 78.0)  # default

    def set_comfort_bounds(self, comfort_min: float, comfort_max: float) -> None:
        """Set the comfort bounds for tracking baseline comfort violations."""
        self._comfort_bounds = (comfort_min, comfort_max)

    def step(
        self,
        now: datetime,
        outdoor_temp: float,
        baseline_setpoint: float,
        baseline_mode: str,
        estimator: ThermalEstimator,
        dt_minutes: float = 5.0,
        cloud_cover: float | None = None,
        sun_elevation: float | None = None,
        carbon_intensity: float | None = None,
        electricity_rate: float | None = None,
        real_indoor_temp: float | None = None,
    ) -> None:
        """Advance the virtual house by one time step.

        Args:
            now: Current time (UTC).
            outdoor_temp: Actual outdoor temperature (°F).
            baseline_setpoint: What the user's old schedule would set (°F).
            baseline_mode: Mode from baseline schedule ("cool", "heat", "off").
            estimator: The EKF thermal estimator (for physics parameters).
            dt_minutes: Time step in minutes (default 5).
            cloud_cover: 0.0-1.0 cloud cover fraction.
            sun_elevation: Degrees above horizon.
            carbon_intensity: Grid carbon intensity (gCO2/kWh).
            electricity_rate: Electricity rate ($/kWh).
            real_indoor_temp: Actual house temp (for periodic drift reset).
        """
        hour_key = int(now.timestamp()) // 3600

        # Hour boundary crossed — finalize previous hour
        if self._current_hour_key is not None and hour_key != self._current_hour_key:
            self._finalize_hour()
            self._hours_since_reset += 1

            # Periodic drift reset
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
            self._hour_mode = baseline_mode

        # Determine if the virtual thermostat would run HVAC
        hvac_running = self._thermostat_decision(
            self._T_air, baseline_setpoint, baseline_mode
        )

        # Advance virtual house physics using EKF parameters
        dt_hours = dt_minutes / 60.0
        x = estimator.x
        R_inv = float(x[IDX_R_INV])
        R_int_inv = float(x[IDX_R_INT_INV])
        C_inv = float(x[IDX_C_INV])
        C_mass_inv = float(x[IDX_C_MASS_INV])
        Q_cool_base = float(x[IDX_Q_COOL])
        Q_heat_base = float(x[IDX_Q_HEAT])

        # Heat flows
        Q_env = R_inv * (outdoor_temp - self._T_air)
        Q_int = R_int_inv * (self._T_mass - self._T_air)
        Q_hvac = self._hvac_output(
            baseline_mode, hvac_running, outdoor_temp,
            Q_cool_base, Q_heat_base,
        )
        Q_solar = self._solar_gain(cloud_cover, sun_elevation)
        Q_internal = DEFAULT_INTERNAL_GAIN_BTU

        # Temperature updates (same physics as EKF _predict_state)
        dT_air = C_inv * (Q_env + Q_int + Q_hvac + Q_solar + Q_internal) * dt_hours
        dT_mass = C_mass_inv * (-Q_int) * dt_hours

        self._T_air += dT_air
        self._T_mass += dT_mass

        # Record this interval
        self._hour_temp_readings.append(self._T_air)

        if hvac_running:
            self._hour_runtime_min += dt_minutes

            # COP and power at this outdoor temp
            cop = self._cop_at_outdoor_temp(outdoor_temp, baseline_mode)
            capacity_btu = self._capacity_at_outdoor_temp(
                outdoor_temp, baseline_mode, Q_cool_base, Q_heat_base
            )
            # Power = capacity / COP (convert BTU/hr to watts: 1 BTU/hr = 0.293071 W)
            if cop > 0:
                power_watts = (capacity_btu / cop) * 0.293071
            else:
                power_watts = 0.0

            self._hour_power_readings.append(power_watts)
            self._hour_cop_readings.append(cop)

            if carbon_intensity is not None:
                self._hour_co2_readings.append(carbon_intensity)
            if electricity_rate is not None:
                self._hour_rate_readings.append(electricity_rate)

        self._hour_mode = baseline_mode

    def get_hour_result(self, hour_key: int) -> BaselineHourResult | None:
        """Get the counterfactual result for a specific hour.

        Args:
            hour_key: Unix timestamp // 3600 for the desired hour.

        Returns:
            BaselineHourResult or None if that hour hasn't been simulated.
        """
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
        """Current virtual house thermal mass temperature."""
        return self._T_mass

    def is_baseline_comfort_violation(self) -> bool:
        """Whether the virtual house is currently outside comfort bounds."""
        lo, hi = self._comfort_bounds
        return self._T_air < lo or self._T_air > hi

    # ── Internal Methods ─────────────────────────────────────────────

    def _thermostat_decision(
        self,
        indoor_temp: float,
        setpoint: float,
        mode: str,
    ) -> bool:
        """Simulate a standard thermostat with deadband hysteresis.

        A real thermostat turns on when the temperature deviates beyond
        the deadband from the setpoint, and turns off when it crosses
        back past the setpoint.
        """
        if mode == "cool":
            # Turn on cooling when temp rises above setpoint + deadband
            return indoor_temp > setpoint + THERMOSTAT_DEADBAND_F
        elif mode == "heat":
            # Turn on heating when temp drops below setpoint - deadband
            return indoor_temp < setpoint - THERMOSTAT_DEADBAND_F
        return False

    @staticmethod
    def _hvac_output(
        mode: str,
        running: bool,
        outdoor_temp: float,
        Q_cool_base: float,
        Q_heat_base: float,
    ) -> float:
        """Calculate HVAC heat flow (BTU/hr) with COP degradation.

        Same model as ThermalEstimator._hvac_output but without environmental
        adjustments (wind/humidity/pressure) for simplicity — the counterfactual
        doesn't need to be perfect, just representative.
        """
        if not running:
            return 0.0

        if mode == "cool":
            cop_factor = max(0.1, 1.0 - ALPHA_COOL * (outdoor_temp - T_REF_F))
            return -Q_cool_base * cop_factor
        elif mode == "heat":
            cop_factor = max(0.1, 1.0 - ALPHA_HEAT * (T_REF_F - outdoor_temp))
            return Q_heat_base * cop_factor

        return 0.0

    @staticmethod
    def _cop_at_outdoor_temp(outdoor_temp: float, mode: str) -> float:
        """Estimate COP at a given outdoor temperature.

        Uses simplified COP model:
        - Cooling COP degrades as outdoor temp rises (harder to reject heat)
        - Heating COP degrades as outdoor temp drops (less heat to extract)
        """
        if mode == "cool":
            # Typical air-source heat pump: COP ~3.5 at 75°F, degrades above
            base_cop = 3.5
            degradation = ALPHA_COOL * (outdoor_temp - T_REF_F)
            return max(1.0, base_cop * (1.0 - degradation))
        elif mode == "heat":
            # Heating COP ~3.0 at 50°F, degrades as it gets colder
            base_cop = 3.0
            degradation = ALPHA_HEAT * (T_REF_F - outdoor_temp)
            return max(1.0, base_cop * (1.0 - degradation))
        return 1.0

    @staticmethod
    def _capacity_at_outdoor_temp(
        outdoor_temp: float,
        mode: str,
        Q_cool_base: float,
        Q_heat_base: float,
    ) -> float:
        """HVAC capacity at given outdoor temp (BTU/hr, always positive)."""
        if mode == "cool":
            cop_factor = max(0.1, 1.0 - ALPHA_COOL * (outdoor_temp - T_REF_F))
            return Q_cool_base * cop_factor
        elif mode == "heat":
            cop_factor = max(0.1, 1.0 - ALPHA_HEAT * (T_REF_F - outdoor_temp))
            return Q_heat_base * cop_factor
        return 0.0

    @staticmethod
    def _solar_gain(
        cloud_cover: float | None,
        sun_elevation: float | None,
    ) -> float:
        """Estimate solar heat gain (BTU/hr) — mirrors ThermalEstimator."""
        if cloud_cover is None or sun_elevation is None or sun_elevation <= 0:
            return 0.0
        clear_sky = 1.0 - cloud_cover
        altitude_factor = math.sin(math.radians(max(0, min(90, sun_elevation))))
        return DEFAULT_SOLAR_GAIN_BTU * clear_sky * altitude_factor

    def _finalize_hour(self) -> None:
        """Convert accumulated interval data into a BaselineHourResult."""
        if self._current_hour_key is None:
            return

        runtime_min = self._hour_runtime_min

        # Average power and COP for this hour
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

        # Energy
        kwh = (runtime_min / 60.0) * (avg_power / 1000.0)

        # Cost
        cost = None
        if self._hour_rate_readings:
            avg_rate = sum(self._hour_rate_readings) / len(self._hour_rate_readings)
            cost = kwh * avg_rate

        # CO2
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
        )

        self._hour_results.append((self._current_hour_key, result))

        _LOGGER.debug(
            "Counterfactual hour finalized: runtime=%.1f min, kwh=%.3f, "
            "COP=%.2f, virtual_temp=%.1f°F",
            runtime_min,
            kwh,
            avg_cop if avg_cop else 0,
            avg_indoor_temp,
        )

    def _drift_reset(self, real_indoor_temp: float) -> None:
        """Blend virtual state toward real state to prevent unbounded drift.

        The virtual house may diverge from reality over time due to model
        errors. Periodically pulling it back keeps the counterfactual
        grounded without destroying the comparison value.
        """
        old_air = self._T_air
        self._T_air = (
            self._T_air * (1 - DRIFT_RESET_BLEND_FACTOR)
            + real_indoor_temp * DRIFT_RESET_BLEND_FACTOR
        )
        _LOGGER.debug(
            "Counterfactual drift reset: %.1f°F → %.1f°F (real=%.1f°F)",
            old_air,
            self._T_air,
            real_indoor_temp,
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
        """Decompose total savings into three components.

        Args:
            baseline_result: What the counterfactual house would have done.
            actual_runtime_min: How long the optimizer actually ran HVAC.
            actual_power_watts: Actual average power draw.
            actual_kwh: Actual energy consumption.
            actual_cop: COP the optimizer achieved.
            actual_rate: Electricity rate during optimized operation.
            baseline_rate: Electricity rate the baseline would have paid.

        Returns:
            Dict with runtime_savings_kwh, cop_savings_kwh, rate_arbitrage_savings.
        """
        total_saved_kwh = baseline_result.kwh - actual_kwh

        # Component 1: Runtime reduction
        # "If we ran at the BASELINE COP but for fewer minutes, how much would we save?"
        if baseline_result.power_watts > 0 and baseline_result.runtime_minutes > 0:
            runtime_diff_min = baseline_result.runtime_minutes - actual_runtime_min
            runtime_savings_kwh = (runtime_diff_min / 60.0) * (baseline_result.power_watts / 1000.0)
        else:
            runtime_savings_kwh = 0.0

        # Component 2: COP improvement
        # "For the minutes we DID run, how much did better COP save?"
        cop_savings_kwh = total_saved_kwh - runtime_savings_kwh

        # Component 3: Rate arbitrage (cost, not kWh)
        rate_arbitrage = None
        if actual_rate is not None and baseline_rate is not None:
            # Same kWh at different rates
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
            "T_mass": self._T_mass,
            "hours_since_reset": self._hours_since_reset,
            "comfort_bounds": list(self._comfort_bounds),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CounterfactualSimulator:
        """Restore from persisted data."""
        sim = cls(initial_temp=data.get("T_air", 72.0))
        sim._T_mass = data.get("T_mass", sim._T_air)
        sim._hours_since_reset = data.get("hours_since_reset", 0)
        bounds = data.get("comfort_bounds", [64.0, 78.0])
        if isinstance(bounds, list) and len(bounds) == 2:
            sim._comfort_bounds = (bounds[0], bounds[1])
        return sim
