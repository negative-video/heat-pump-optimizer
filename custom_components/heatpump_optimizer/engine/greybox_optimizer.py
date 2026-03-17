"""Grey-Box Model optimizer using Linear Programming with learned thermal parameters.

Combines Bayesian priors (from Beestat or conservative defaults) with online
Kalman filter estimation and LP-based optimal scheduling. This is the architecture
used in commercial building energy optimization systems.

Key differences from the heuristic ScheduleOptimizer:
  1. Uses the physical thermal model directly (not efficiency scoring)
  2. LP formulation finds globally optimal HVAC duty cycles
  3. Parameter uncertainty from the EKF covariance matrix tightens comfort
     margins when confidence is low → conservative when uncertain,
     aggressive when well-calibrated
  4. Two-node model (air + thermal mass) captures lag effects

The LP is solved via a greedy thermal-constrained assignment algorithm
(no scipy dependency). For 24 hourly decision variables, this produces
near-optimal solutions in microseconds.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np

from .data_types import (
    ForecastPoint,
    OptimizationWeights,
    OptimizedSchedule,
    ScheduleEntry,
    SimulationPoint,
)

if TYPE_CHECKING:
    from ..learning.thermal_estimator import ThermalEstimator

_LOGGER = logging.getLogger(__name__)

# Thermal estimator state indices (duplicated to avoid import at module level)
_IDX_R_INV = 2
_IDX_R_INT_INV = 3
_IDX_C_INV = 4
_IDX_C_MASS_INV = 5
_IDX_Q_COOL = 6
_IDX_Q_HEAT = 7
_IDX_SOLAR_GAIN = 8

# COP degradation slopes (must match thermal_estimator.py)
_ALPHA_COOL = 0.012
_ALPHA_HEAT = 0.015
_T_REF = 75.0

# Fallback solar gain constant (BTU/hr at peak clear sky) — used only
# when the estimator doesn't have the solar gain parameter yet
_SOLAR_GAIN_BTU_FALLBACK = 3000.0

# Internal heat gain constants (must match thermal_estimator.py)
_INTERNAL_GAIN_BASE_BTU = 800.0      # appliances/electronics (always present)
_INTERNAL_GAIN_PER_PERSON_BTU = 350.0  # ~350 BTU/hr per occupant
_DEFAULT_INTERNAL_GAIN_BTU = 1200.0    # fallback when occupancy unknown

# Precipitation evaporative cooling offset (°F)
_PRECIPITATION_OFFSET_F = 3.0

# Default time step for the LP (1 hour)
_LP_DT_HOURS = 1.0


class GreyBoxOptimizer:
    """LP-based schedule optimizer using learned thermal parameters.

    Uses the ThermalEstimator's state vector and covariance matrix to:
    1. Build a linearized thermal model of the building
    2. Find optimal HVAC duty cycles via greedy LP relaxation
    3. Apply uncertainty-aware comfort margins
    4. Convert duty cycles to a ScheduleEntry-based schedule
    """

    def __init__(self, estimator: ThermalEstimator):
        self.estimator = estimator

    def optimize(
        self,
        current_indoor_temp: float,
        forecast: list[ForecastPoint],
        comfort_range: tuple[float, float],
        mode: str,
        weights: OptimizationWeights | None = None,
        people_home_count: int | None = None,
        indoor_humidity: float | None = None,
        appliance_btu: float = 0.0,
    ) -> OptimizedSchedule:
        """Find optimal HVAC schedule using linear programming.

        Args:
            current_indoor_temp: Current indoor temperature (°F).
            forecast: Weather forecast points.
            comfort_range: (min_temp, max_temp) in °F.
            mode: "cool" or "heat".
            weights: Multi-objective weights (energy, carbon, cost).
            people_home_count: Current occupant count (for internal gain scaling).
            indoor_humidity: Current indoor relative humidity (0-100).
            appliance_btu: Net BTU/hr from auxiliary appliances (negative = cooling).

        Returns:
            OptimizedSchedule with entries, savings estimate, and simulation.
        """
        if weights is None:
            weights = OptimizationWeights()

        # Store environmental context for use in thermal model
        self._people_home_count = people_home_count
        self._indoor_humidity = indoor_humidity
        self._appliance_btu = appliance_btu

        # Group forecast into hourly bins
        hourly_forecast = self._bin_forecast_hourly(forecast)
        n_hours = len(hourly_forecast)
        if n_hours == 0:
            return self._empty_schedule()

        # Extract thermal parameters from estimator
        params = self._extract_params()

        # Pre-compute thermal mass trajectory (treat as exogenous)
        T_mass_trajectory = self._precompute_thermal_mass(
            current_indoor_temp, hourly_forecast, params
        )

        # Build linearized thermal matrices
        A, B, d = self._build_thermal_matrices(
            current_indoor_temp, hourly_forecast, T_mass_trajectory, params, mode
        )

        # Compute cost vector for objective function
        cost = self._compute_cost_vector(hourly_forecast, mode, params, weights)

        # Compute uncertainty-aware comfort margins
        comfort_min, comfort_max = comfort_range
        margins = self._compute_uncertainty_margins(n_hours, hourly_forecast, mode, params)
        effective_min = np.array([comfort_min + margins[t] for t in range(n_hours + 1)])
        effective_max = np.array([comfort_max - margins[t] for t in range(n_hours + 1)])

        # Overlay per-hour comfort from calendar occupancy timeline (if present)
        for t, hf in enumerate(hourly_forecast):
            if hf.get("comfort_min") is not None and hf.get("comfort_max") is not None:
                effective_min[t + 1] = hf["comfort_min"] + margins[t + 1]
                effective_max[t + 1] = hf["comfort_max"] - margins[t + 1]

        # Ensure effective band is at least 1°F wide
        for t in range(n_hours + 1):
            if effective_max[t] - effective_min[t] < 1.0:
                mid_t = (effective_min[t] + effective_max[t]) / 2
                effective_min[t] = mid_t - 0.5
                effective_max[t] = mid_t + 0.5

        # Solve LP: find optimal duty cycles
        u_opt = self._solve_lp(
            cost, A, B, d, effective_min, effective_max, current_indoor_temp, n_hours
        )

        # Re-iterate: recompute thermal mass trajectory using optimized duty
        # cycles, then rebuild matrices and re-solve. This corrects the
        # frozen-mass approximation for cases where aggressive pre-heating/
        # cooling shifts T_mass significantly (e.g. lightweight construction).
        T_mass_trajectory = self._recompute_thermal_mass_with_duty(
            current_indoor_temp, hourly_forecast, params, u_opt, mode
        )
        A, B, d = self._build_thermal_matrices(
            current_indoor_temp, hourly_forecast, T_mass_trajectory, params, mode
        )
        u_opt = self._solve_lp(
            cost, A, B, d, effective_min, effective_max, current_indoor_temp, n_hours
        )

        # Simulate forward with optimal duty cycles
        T_trajectory = self._simulate_trajectory(
            u_opt, A, B, d, current_indoor_temp, n_hours
        )

        # Count comfort violations (against original bounds, not effective)
        violations = sum(
            1 for t in range(1, n_hours + 1)
            if T_trajectory[t] < comfort_min - 0.5 or T_trajectory[t] > comfort_max + 0.5
        )

        # Compute baseline (constant midpoint, u=1 whenever needed)
        midpoint = (comfort_min + comfort_max) / 2
        u_baseline = self._compute_baseline_duty(
            A, B, d, midpoint, current_indoor_temp, n_hours, mode
        )

        # Convert to schedule entries
        entries = self._duty_to_schedule(u_opt, hourly_forecast, mode, comfort_range)

        # Build simulation points
        sim_points = self._build_simulation_points(
            T_trajectory, u_opt, hourly_forecast
        )

        # Calculate runtimes (minutes)
        optimized_runtime = float(np.sum(u_opt)) * 60.0
        baseline_runtime = float(np.sum(u_baseline)) * 60.0

        savings_pct = 0.0
        if baseline_runtime > 0:
            savings_pct = max(0.0, (baseline_runtime - optimized_runtime) / baseline_runtime * 100)

        # Compute energy/cost/carbon estimates (per-hour COP-aware)
        schedule = OptimizedSchedule(
            entries=entries,
            baseline_runtime_minutes=baseline_runtime,
            optimized_runtime_minutes=optimized_runtime,
            savings_pct=savings_pct,
            comfort_violations=violations,
            simulation=sim_points,
        )

        self._add_energy_estimates(schedule, u_opt, u_baseline, hourly_forecast, mode)

        _LOGGER.info(
            "Grey-box LP optimization [%s]: %d hours, baseline=%.0f min, "
            "optimized=%.0f min, savings=%.1f%%, confidence=%.0f%%",
            mode, n_hours, baseline_runtime, optimized_runtime,
            savings_pct, self.estimator.confidence * 100,
        )

        return schedule

    # ── Thermal Model Construction ──────────────────────────────────

    def _extract_params(self) -> dict:
        """Extract current thermal parameters from estimator."""
        x = self.estimator.x
        solar_gain = float(x[_IDX_SOLAR_GAIN]) if len(x) > _IDX_SOLAR_GAIN else _SOLAR_GAIN_BTU_FALLBACK
        return {
            "R_inv": float(x[_IDX_R_INV]),
            "R_int_inv": float(x[_IDX_R_INT_INV]),
            "C_inv": float(x[_IDX_C_INV]),
            "C_mass_inv": float(x[_IDX_C_MASS_INV]),
            "Q_cool_base": float(x[_IDX_Q_COOL]),
            "Q_heat_base": float(x[_IDX_Q_HEAT]),
            "T_air": self.estimator.T_air,
            "T_mass": self.estimator.T_mass,
            "solar_gain_btu": solar_gain,
            "envelope_area": self.estimator.envelope_area,
        }

    def _precompute_thermal_mass(
        self,
        T_air_init: float,
        hourly_forecast: list[dict],
        params: dict,
    ) -> np.ndarray:
        """Pre-compute thermal mass trajectory assuming no HVAC.

        Thermal mass changes slowly (C_mass >> C_air), so treating it as
        exogenous in the LP is a good approximation for 24-hour horizons.
        """
        n = len(hourly_forecast)
        T_mass = np.zeros(n + 1)
        T_mass[0] = params["T_mass"]
        T_air = T_air_init

        R_int_inv = params["R_int_inv"]
        C_mass_inv = params["C_mass_inv"]
        R_inv = params["R_inv"]
        C_inv = params["C_inv"]
        UA = R_inv * params.get("envelope_area", 2000.0)

        # Internal heat gain (use current occupancy as best estimate for horizon)
        people = getattr(self, "_people_home_count", None)
        if people is not None:
            Q_internal = _INTERNAL_GAIN_BASE_BTU + _INTERNAL_GAIN_PER_PERSON_BTU * people
        else:
            Q_internal = _DEFAULT_INTERNAL_GAIN_BTU

        for t in range(n):
            T_out = hourly_forecast[t]["temp"]
            # Precipitation: evaporative cooling reduces effective outdoor temp
            if hourly_forecast[t].get("precipitation", False):
                T_out = T_out - _PRECIPITATION_OFFSET_F
            # Approximate air temp with passive drift
            Q_env = UA * (T_out - T_air)
            Q_int = R_int_inv * (T_mass[t] - T_air)
            Q_appliances = getattr(self, "_appliance_btu", 0.0)
            T_air += C_inv * (Q_env + Q_int + Q_internal + Q_appliances) * _LP_DT_HOURS

            # Mass temp update
            Q_int_mass = R_int_inv * (T_air - T_mass[t])
            T_mass[t + 1] = T_mass[t] + C_mass_inv * Q_int_mass * _LP_DT_HOURS

        return T_mass

    def _recompute_thermal_mass_with_duty(
        self,
        T_air_init: float,
        hourly_forecast: list[dict],
        params: dict,
        u: np.ndarray,
        mode: str,
    ) -> np.ndarray:
        """Re-compute thermal mass trajectory including HVAC duty cycles.

        After the first LP solve, we know u[t]. Simulating T_air with that
        duty and recomputing T_mass coupling gives a more accurate mass
        trajectory for a second LP pass.
        """
        n = len(hourly_forecast)
        T_mass = np.zeros(n + 1)
        T_mass[0] = params["T_mass"]
        T_air = T_air_init

        R_int_inv = params["R_int_inv"]
        C_mass_inv = params["C_mass_inv"]
        R_inv = params["R_inv"]
        C_inv = params["C_inv"]
        UA = R_inv * params.get("envelope_area", 2000.0)

        people = getattr(self, "_people_home_count", None)
        if people is not None:
            Q_internal = _INTERNAL_GAIN_BASE_BTU + _INTERNAL_GAIN_PER_PERSON_BTU * people
        else:
            Q_internal = _DEFAULT_INTERNAL_GAIN_BTU

        for t in range(n):
            T_out = hourly_forecast[t]["temp"]
            if hourly_forecast[t].get("precipitation", False):
                T_out = T_out - _PRECIPITATION_OFFSET_F

            Q_env = UA * (T_out - T_air)
            Q_int = R_int_inv * (T_mass[t] - T_air)
            # Include HVAC contribution from optimized duty
            Q_hvac = self._hvac_capacity(mode, hourly_forecast[t]["temp"], params)
            Q_appliances = getattr(self, "_appliance_btu", 0.0)
            T_air += C_inv * (Q_env + Q_int + Q_internal + u[t] * Q_hvac + Q_appliances) * _LP_DT_HOURS

            Q_int_mass = R_int_inv * (T_air - T_mass[t])
            T_mass[t + 1] = T_mass[t] + C_mass_inv * Q_int_mass * _LP_DT_HOURS

        return T_mass

    def _build_thermal_matrices(
        self,
        T_air_init: float,
        hourly_forecast: list[dict],
        T_mass: np.ndarray,
        params: dict,
        mode: str,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Build linearized thermal model matrices.

        T_air[t+1] = A[t] * T_air[t] + B[t] * u[t] + d[t]

        where u[t] is the HVAC duty cycle (0 to 1) for hour t.

        Returns:
            A: (n,) array — state transition coefficients
            B: (n,) array — HVAC effect per unit duty cycle
            d: (n,) array — exogenous inputs (outdoor temp, thermal mass, solar)
        """
        n = len(hourly_forecast)
        R_inv = params["R_inv"]
        R_int_inv = params["R_int_inv"]
        C_inv = params["C_inv"]
        UA = R_inv * params.get("envelope_area", 2000.0)

        A = np.zeros(n)
        B = np.zeros(n)
        d = np.zeros(n)

        dt = _LP_DT_HOURS

        # Internal heat gain (use current occupancy as best estimate for horizon)
        people = getattr(self, "_people_home_count", None)
        if people is not None:
            Q_internal = _INTERNAL_GAIN_BASE_BTU + _INTERNAL_GAIN_PER_PERSON_BTU * people
        else:
            Q_internal = _DEFAULT_INTERNAL_GAIN_BTU

        # Indoor humidity for SHR correction
        indoor_hum = getattr(self, "_indoor_humidity", None)

        for t in range(n):
            T_out = hourly_forecast[t]["temp"]
            T_eff = hourly_forecast[t].get("effective_temp") or T_out
            cloud = hourly_forecast[t].get("cloud_cover")
            sun_elev = hourly_forecast[t].get("sun_elevation")
            humidity = hourly_forecast[t].get("humidity")
            pressure = hourly_forecast[t].get("pressure_hpa")
            irradiance = hourly_forecast[t].get("solar_irradiance")
            is_precip = hourly_forecast[t].get("precipitation", False)

            # Precipitation: evaporative cooling reduces effective envelope temp
            env_T_out = T_out - _PRECIPITATION_OFFSET_F if is_precip else T_out

            # State transition: how much of current T_air carries forward
            A[t] = 1.0 - C_inv * (UA + R_int_inv) * dt

            # HVAC effect: use effective temp for COP degradation
            Q_hvac = self._hvac_capacity(
                mode, T_eff, params, humidity=humidity, pressure_hpa=pressure,
                indoor_humidity=indoor_hum,
            )
            B[t] = C_inv * Q_hvac * dt
            # For cooling: Q_hvac is negative → B[t] is negative (lowers temp)
            # For heating: Q_hvac is positive → B[t] is positive (raises temp)

            # Exogenous: outdoor temp (with precip correction) for envelope + thermal mass + solar + internal gain
            Q_env = UA * env_T_out  # partial: UA * T_out (the -UA*T_air part is in A)
            Q_int = R_int_inv * T_mass[t]  # partial: coupling from mass
            Q_solar = self._solar_gain(
                cloud, sun_elev, irradiance_w_m2=irradiance,
                solar_gain_btu=params.get("solar_gain_btu", _SOLAR_GAIN_BTU_FALLBACK),
            )

            Q_appliances = getattr(self, "_appliance_btu", 0.0)
            d[t] = C_inv * (Q_env + Q_int + Q_solar + Q_internal + Q_appliances) * dt
            # Note: A already accounts for -UA*T_air and -R_int_inv*T_air

        return A, B, d

    def _hvac_capacity(
        self,
        mode: str,
        T_out: float,
        params: dict,
        humidity: float | None = None,
        pressure_hpa: float | None = None,
        indoor_humidity: float | None = None,
    ) -> float:
        """HVAC heat output in BTU/hr (negative for cooling, positive for heating).

        Args:
            mode: "cool" or "heat".
            T_out: Outdoor temperature (or effective temp) for COP calculation.
            params: Thermal parameters dict.
            humidity: Outdoor relative humidity 0-100, or None.
            pressure_hpa: Atmospheric pressure in hPa, or None.
            indoor_humidity: Indoor relative humidity 0-100 (for SHR correction).
        """
        if mode == "cool":
            raw_factor = 1.0 - _ALPHA_COOL * (T_out - _T_REF)
            cop_factor = max(0.1, raw_factor)
            if raw_factor <= 0.1:
                _LOGGER.warning(
                    "Grey-box COP at floor for cooling: outdoor=%.1f°F", T_out
                )
            # Outdoor humidity correction: high humidity reduces cooling COP
            if humidity is not None and humidity > 50.0:
                cop_factor *= max(0.8, 1.0 - (humidity - 50.0) / 500.0)
            # Indoor humidity SHR: high indoor RH means more latent cooling
            # (dehumidification) and less sensible cooling (temperature change)
            if indoor_humidity is not None and indoor_humidity > 50.0:
                shr = max(0.65, 1.0 - (indoor_humidity - 50.0) / 100.0)
                cop_factor *= shr
            # Pressure correction
            if pressure_hpa is not None:
                cop_factor *= (pressure_hpa / 1013.25) ** 0.1
            return -params["Q_cool_base"] * cop_factor
        elif mode == "heat":
            raw_factor = 1.0 - _ALPHA_HEAT * (_T_REF - T_out)
            cop_factor = max(0.1, raw_factor)
            if raw_factor <= 0.1:
                _LOGGER.warning(
                    "Grey-box COP at floor for heating: outdoor=%.1f°F", T_out
                )
            # Pressure correction
            if pressure_hpa is not None:
                cop_factor *= (pressure_hpa / 1013.25) ** 0.1
            return params["Q_heat_base"] * cop_factor
        return 0.0

    @staticmethod
    def _solar_gain(
        cloud_cover: float | None,
        sun_elevation: float | None,
        irradiance_w_m2: float | None = None,
        solar_gain_btu: float = _SOLAR_GAIN_BTU_FALLBACK,
    ) -> float:
        """Solar heat gain in BTU/hr.

        When direct irradiance measurement is available, use it directly.
        Otherwise fall back to the cloud_cover * elevation model using the
        learned solar gain parameter from the EKF.
        """
        if irradiance_w_m2 is not None:
            return irradiance_w_m2 * 3.412  # W/m² → BTU/hr/m² (solar_coefficient absorbs area)
        if cloud_cover is None or sun_elevation is None or sun_elevation <= 0:
            return 0.0
        clear_sky = 1.0 - cloud_cover
        altitude_factor = math.sin(math.radians(max(0, min(90, sun_elevation))))
        return solar_gain_btu * clear_sky * altitude_factor

    # ── Cost Vector ─────────────────────────────────────────────────

    def _compute_cost_vector(
        self,
        hourly_forecast: list[dict],
        mode: str,
        params: dict,
        weights: OptimizationWeights,
    ) -> np.ndarray:
        """Build per-hour cost for the LP objective.

        cost[t] = w_energy * efficiency_cost[t]
                + w_carbon * carbon[t]
                + w_cost * rate[t]

        All dimensions normalized to [0, 1] for comparability.
        """
        n = len(hourly_forecast)
        cost = np.ones(n)  # base: uniform energy cost

        # Energy efficiency dimension: inverse of HVAC effectiveness
        # Lower |delta| = less effective = higher cost to run
        eff_raw = np.zeros(n)
        for t in range(n):
            T_out = hourly_forecast[t]["temp"]
            Q_hvac = abs(self._hvac_capacity(mode, T_out, params))
            C_inv = params["C_inv"]
            # Degrees of temperature change per hour of runtime
            delta_per_hour = C_inv * Q_hvac
            eff_raw[t] = 1.0 / max(delta_per_hour, 0.001)

        eff_norm = self._normalize(eff_raw)

        # Carbon dimension
        carbon_raw = np.array([
            h.get("carbon_intensity", 0.0) or 0.0 for h in hourly_forecast
        ])
        carbon_norm = self._normalize(carbon_raw)

        # Cost dimension
        cost_raw = np.array([
            h.get("electricity_rate", 0.0) or 0.0 for h in hourly_forecast
        ])
        cost_norm = self._normalize(cost_raw)

        # Weighted combination
        cost = weights.energy_efficiency * eff_norm
        if weights.carbon_intensity > 0 and np.any(carbon_raw > 0):
            cost = cost + weights.carbon_intensity * carbon_norm
        if weights.electricity_cost > 0 and np.any(cost_raw > 0):
            cost = cost + weights.electricity_cost * cost_norm

        return cost

    @staticmethod
    def _normalize(arr: np.ndarray) -> np.ndarray:
        """Min-max normalize to [0, 1]."""
        mn, mx = arr.min(), arr.max()
        rng = mx - mn
        if rng < 1e-9:
            return np.full_like(arr, 0.5)
        return (arr - mn) / rng

    # ── Uncertainty Margins ─────────────────────────────────────────

    def _compute_uncertainty_margins(
        self,
        n_hours: int,
        hourly_forecast: list[dict],
        mode: str,
        params: dict,
    ) -> np.ndarray:
        """Propagate parameter uncertainty to temperature prediction uncertainty.

        Uses the EKF covariance matrix to compute how much the temperature
        prediction could drift due to parameter errors. Higher uncertainty
        → wider safety margins → more conservative optimization.

        Returns margin[t] for t = 0..n_hours (°F to subtract from each
        comfort bound).
        """
        P = self.estimator.P
        confidence = self.estimator.confidence
        dt = _LP_DT_HOURS

        # Uncertainty scaling factor: aggressive when confident, conservative when not
        # k = 0.2 at confidence=1.0, k = 1.5 at confidence=0.0
        k = 1.5 * (1.0 - confidence) + 0.2

        # Parameter standard deviations
        sigma_R_inv = math.sqrt(max(0, P[_IDX_R_INV, _IDX_R_INV]))
        sigma_C_inv = math.sqrt(max(0, P[_IDX_C_INV, _IDX_C_INV]))
        if mode == "cool":
            sigma_Q = math.sqrt(max(0, P[_IDX_Q_COOL, _IDX_Q_COOL]))
        else:
            sigma_Q = math.sqrt(max(0, P[_IDX_Q_HEAT, _IDX_Q_HEAT]))

        margins = np.zeros(n_hours + 1)
        margins[0] = 0.0  # current temp is known

        # Accumulate uncertainty forward through the horizon
        sigma_T_sq = P[0, 0]  # initial temperature uncertainty

        T_air = params["T_air"]
        area = params.get("envelope_area", 2000.0)
        UA = params["R_inv"] * area

        for t in range(n_hours):
            T_out = hourly_forecast[t]["temp"]
            Q_hvac_abs = abs(self._hvac_capacity(mode, T_out, params))

            # Sensitivity of T_air[t+1] to each parameter (R_inv enters as UA = R_inv * area)
            J_R = params["C_inv"] * area * (T_out - T_air) * dt  # ∂T/∂R_inv
            J_C = (UA * (T_out - T_air) + Q_hvac_abs) * dt  # ∂T/∂C_inv
            J_Q = params["C_inv"] * dt  # ∂T/∂Q_hvac

            # Propagated variance (sum of parameter contributions)
            A_coeff = 1.0 - params["C_inv"] * (UA + params["R_int_inv"]) * dt
            sigma_T_sq = (
                A_coeff**2 * sigma_T_sq
                + J_R**2 * sigma_R_inv**2
                + J_C**2 * sigma_C_inv**2
                + J_Q**2 * sigma_Q**2
            )

            margins[t + 1] = k * math.sqrt(max(0, sigma_T_sq))

        # Cap margins at half the comfort band to prevent infeasibility
        max_margin = 3.0
        margins = np.minimum(margins, max_margin)

        return margins

    # ── LP Solver (Greedy Thermal-Constrained Assignment) ───────────

    def _solve_lp(
        self,
        cost: np.ndarray,
        A: np.ndarray,
        B: np.ndarray,
        d: np.ndarray,
        T_min: np.ndarray,
        T_max: np.ndarray,
        T_init: float,
        n: int,
    ) -> np.ndarray:
        """Solve the thermal-constrained LP via greedy assignment.

        Algorithm:
        1. Compute passive trajectory (u=0)
        2. Identify hours where comfort would be violated
        3. Sort candidate hours by marginal cost (cost per degree of change)
        4. Greedily assign runtime to cheapest hours, using binary search
           to find the maximum feasible duty at each hour
        5. Refine — reduce duty on most expensive hours to minimum needed

        This exploits the chain structure of the thermal dynamics to avoid
        a general LP solver.
        """
        # Step 1: Passive trajectory (no HVAC)
        T_passive = self._simulate_trajectory(
            np.zeros(n), A, B, d, T_init, n
        )

        # Step 2: Check if HVAC is needed at all
        needs_hvac = False
        for t in range(1, n + 1):
            if T_passive[t] < T_min[t] or T_passive[t] > T_max[t]:
                needs_hvac = True
                break

        if not needs_hvac:
            return np.zeros(n)

        # Step 3: Marginal cost per degree of temperature effect
        eps = 1e-6
        marginal_cost = cost / (np.abs(B) + eps)

        # Step 4: Sort hours by marginal cost (cheapest first)
        sorted_hours = np.argsort(marginal_cost)

        # Step 5: Greedy assignment — assign duty chronologically first
        # to handle cascading thermal dynamics, then refine by cost.
        #
        # The chain structure T[t+1] = A[t]*T[t] + B[t]*u[t] + d[t] means
        # duty at hour t only affects hours t+1 onward. We first walk
        # forward in time assigning minimum duty to keep within bounds,
        # then redistribute from expensive to cheap hours.
        u = np.zeros(n)

        # Forward pass: assign minimum duty to keep trajectory in bounds
        for t in range(n):
            # Simulate trajectory with current u
            T_current = self._simulate_trajectory(u, A, B, d, T_init, n)

            # Check if hour t+1 would violate comfort without duty at t
            if T_min[t + 1] <= T_current[t + 1] <= T_max[t + 1]:
                continue  # No duty needed this hour

            # Binary search for minimum duty at hour t to fix violation
            lo, hi = 0.0, 1.0
            for _ in range(12):
                mid = (lo + hi) / 2
                u_test = u.copy()
                u_test[t] = mid
                T_test = self._simulate_trajectory(u_test, A, B, d, T_init, n)
                # Check if this fixes the immediate violation and doesn't
                # create a new one (e.g., overcooling below T_min)
                ok = T_min[t + 1] - 0.1 <= T_test[t + 1] <= T_max[t + 1] + 0.1
                if ok:
                    hi = mid  # Can use less
                else:
                    lo = mid  # Need more
            u[t] = hi

        # Greedy refinement: try to shift duty from expensive to cheap hours
        all_satisfied = False
        for _iteration in range(3):
            for t in sorted_hours:
                if u[t] >= 1.0:
                    continue

                # Binary search for maximum feasible duty at this hour
                max_feasible = self._find_max_feasible_duty(
                    u, t, A, B, d, T_min, T_max, T_init, n
                )

                if max_feasible > u[t] + 0.01:
                    u[t] = max_feasible

                # Check if all constraints are now satisfied
                T_current = self._simulate_trajectory(u, A, B, d, T_init, n)
                all_satisfied = True
                for s in range(1, n + 1):
                    if T_current[s] < T_min[s] - 0.1 or T_current[s] > T_max[s] + 0.1:
                        all_satisfied = False
                        break
                if all_satisfied:
                    break

            if all_satisfied:
                break

        # Step 6: Refine — reduce duty on most expensive hours to minimum
        reverse_sorted = sorted_hours[::-1]
        for t in reverse_sorted:
            if u[t] <= 0.01:
                continue

            # Binary search for minimum needed duty at this hour
            lo, hi = 0.0, u[t]
            for _ in range(12):
                mid = (lo + hi) / 2
                u_test = u.copy()
                u_test[t] = mid
                T_test = self._simulate_trajectory(u_test, A, B, d, T_init, n)

                feasible = True
                for s in range(1, n + 1):
                    if T_test[s] < T_min[s] - 0.1 or T_test[s] > T_max[s] + 0.1:
                        feasible = False
                        break

                if feasible:
                    hi = mid
                else:
                    lo = mid

            u[t] = hi

        return u

    def _find_max_feasible_duty(
        self,
        u_current: np.ndarray,
        t: int,
        A: np.ndarray,
        B: np.ndarray,
        d: np.ndarray,
        T_min: np.ndarray,
        T_max: np.ndarray,
        T_init: float,
        n: int,
    ) -> float:
        """Binary search for maximum duty at hour t that doesn't violate constraints."""
        lo, hi = 0.0, 1.0

        # Quick check: is u=1 feasible?
        u_test = u_current.copy()
        u_test[t] = 1.0
        T_test = self._simulate_trajectory(u_test, A, B, d, T_init, n)
        if self._is_feasible(T_test, T_min, T_max, n):
            return 1.0

        # Quick check: is any duty feasible?
        u_test[t] = 0.01
        T_test = self._simulate_trajectory(u_test, A, B, d, T_init, n)
        if not self._is_feasible(T_test, T_min, T_max, n):
            # Even tiny duty is infeasible — check if it at least helps
            T_without = self._simulate_trajectory(u_current, A, B, d, T_init, n)
            worst_without = self._worst_violation(T_without, T_min, T_max, n)
            worst_with = self._worst_violation(T_test, T_min, T_max, n)
            if worst_with >= worst_without:
                return 0.0  # Doesn't help

        # Binary search for max feasible duty
        for _ in range(12):
            mid = (lo + hi) / 2
            u_test = u_current.copy()
            u_test[t] = mid
            T_test = self._simulate_trajectory(u_test, A, B, d, T_init, n)

            if self._is_feasible(T_test, T_min, T_max, n):
                lo = mid  # Can go higher
            else:
                hi = mid  # Must go lower

        return lo

    @staticmethod
    def _is_feasible(
        T: np.ndarray, T_min: np.ndarray, T_max: np.ndarray, n: int
    ) -> bool:
        """Check if a temperature trajectory satisfies all comfort constraints."""
        for s in range(1, n + 1):
            if T[s] < T_min[s] - 0.1 or T[s] > T_max[s] + 0.1:
                return False
        return True

    @staticmethod
    def _worst_violation(
        T: np.ndarray, T_min: np.ndarray, T_max: np.ndarray, n: int
    ) -> float:
        """Return the magnitude of the worst comfort violation."""
        worst = 0.0
        for s in range(1, n + 1):
            if T[s] < T_min[s]:
                worst = max(worst, T_min[s] - T[s])
            if T[s] > T_max[s]:
                worst = max(worst, T[s] - T_max[s])
        return worst

    def _simulate_trajectory(
        self,
        u: np.ndarray,
        A: np.ndarray,
        B: np.ndarray,
        d: np.ndarray,
        T_init: float,
        n: int,
    ) -> np.ndarray:
        """Forward simulate temperature trajectory from duty cycles.

        T[t+1] = A[t] * T[t] + B[t] * u[t] + d[t]
        """
        T = np.zeros(n + 1)
        T[0] = T_init
        for t in range(n):
            T[t + 1] = A[t] * T[t] + B[t] * u[t] + d[t]
        return T

    def _compute_baseline_duty(
        self,
        A: np.ndarray,
        B: np.ndarray,
        d: np.ndarray,
        setpoint: float,
        T_init: float,
        n: int,
        mode: str,
    ) -> np.ndarray:
        """Compute baseline duty cycles for constant-setpoint thermostat.

        Simulates a simple thermostat that turns HVAC on when temperature
        deviates from setpoint by more than 0.5°F (hysteresis).
        """
        u = np.zeros(n)
        T = T_init
        differential = 0.5

        for t in range(n):
            if mode == "cool":
                # Turn on cooling when temp exceeds setpoint + differential
                if T > setpoint + differential:
                    u[t] = 1.0
                elif T > setpoint:
                    # Proportional in the hysteresis band
                    u[t] = (T - setpoint) / differential
            elif mode == "heat":
                if T < setpoint - differential:
                    u[t] = 1.0
                elif T < setpoint:
                    u[t] = (setpoint - T) / differential

            T = A[t] * T + B[t] * u[t] + d[t]

        return u

    # ── Forecast Binning ────────────────────────────────────────────

    def _bin_forecast_hourly(
        self, forecast: list[ForecastPoint]
    ) -> list[dict]:
        """Group forecast points into hourly bins with average values."""
        if not forecast:
            return []

        bins: dict[datetime, list[ForecastPoint]] = {}
        for pt in forecast:
            hour_key = pt.time.replace(minute=0, second=0, microsecond=0)
            bins.setdefault(hour_key, []).append(pt)

        result = []
        for hour in sorted(bins.keys()):
            pts = bins[hour]
            entry = {
                "hour": hour,
                "temp": sum(p.outdoor_temp for p in pts) / len(pts),
                "carbon_intensity": None,
                "electricity_rate": None,
                "cloud_cover": None,
                "sun_elevation": None,
                "effective_temp": None,
                "humidity": None,
                "solar_irradiance": None,
                "pressure_hpa": None,
                "precipitation": False,
            }
            # Average optional fields
            ci = [p.carbon_intensity for p in pts if p.carbon_intensity is not None]
            if ci:
                entry["carbon_intensity"] = sum(ci) / len(ci)
            er = [p.electricity_rate for p in pts if p.electricity_rate is not None]
            if er:
                entry["electricity_rate"] = sum(er) / len(er)
            # Cloud cover and sun elevation
            cc = [p.cloud_cover for p in pts if p.cloud_cover is not None]
            if cc:
                entry["cloud_cover"] = sum(cc) / len(cc)
            se = [p.sun_elevation for p in pts if p.sun_elevation is not None]
            if se:
                entry["sun_elevation"] = sum(se) / len(se)
            # Effective outdoor temp (wind chill adjusted)
            eff = [p.effective_outdoor_temp for p in pts]
            entry["effective_temp"] = sum(eff) / len(eff)
            # Humidity
            hum = [p.humidity for p in pts if p.humidity is not None]
            if hum:
                entry["humidity"] = sum(hum) / len(hum)
            # Solar irradiance
            irr = [p.solar_irradiance_w_m2 for p in pts if p.solar_irradiance_w_m2 is not None]
            if irr:
                entry["solar_irradiance"] = sum(irr) / len(irr)
            # Atmospheric pressure
            prs = [p.pressure_hpa for p in pts if p.pressure_hpa is not None]
            if prs:
                entry["pressure_hpa"] = sum(prs) / len(prs)
            # Precipitation: True if any point in this hour has precipitation
            entry["precipitation"] = any(p.precipitation for p in pts)
            # Per-hour comfort bounds (from calendar occupancy timeline)
            cmin = [p.comfort_min for p in pts if p.comfort_min is not None]
            cmax = [p.comfort_max for p in pts if p.comfort_max is not None]
            entry["comfort_min"] = sum(cmin) / len(cmin) if cmin else None
            entry["comfort_max"] = sum(cmax) / len(cmax) if cmax else None

            result.append(entry)

        return result

    # ── Schedule Conversion ─────────────────────────────────────────

    def _duty_to_schedule(
        self,
        u: np.ndarray,
        hourly_forecast: list[dict],
        mode: str,
        comfort_range: tuple[float, float],
    ) -> list[ScheduleEntry]:
        """Convert duty cycle vector to ScheduleEntry list."""
        comfort_min, comfort_max = comfort_range
        comfort_band = comfort_max - comfort_min
        entries = []

        for t, hf in enumerate(hourly_forecast):
            hour = hf["hour"]
            duty = u[t]

            # Map duty cycle to target temperature
            if mode == "cool":
                # High duty → lower target (pre-cooling)
                # Low duty → higher target (coasting)
                target = comfort_max - duty * comfort_band
            elif mode == "heat":
                # High duty → higher target (pre-heating)
                target = comfort_min + duty * comfort_band
            else:
                target = (comfort_min + comfort_max) / 2

            # Round to thermostat resolution
            target = round(target * 2) / 2

            # Clamp to comfort bounds
            target = max(comfort_min, min(comfort_max, target))

            # Classify action
            if duty > 0.66:
                action = "pre-cooling" if mode == "cool" else "pre-heating"
            elif duty > 0.33:
                action = "maintaining"
            else:
                action = "coasting"

            entries.append(ScheduleEntry(
                start_time=hour,
                end_time=hour + timedelta(hours=1),
                target_temp=target,
                mode=mode,
                reason=(
                    f"{hf['temp']:.0f}°F outdoor: {action} "
                    f"(duty={duty:.0%}, target {target:.1f}°F)"
                ),
            ))

        return entries

    def _build_simulation_points(
        self,
        T_trajectory: np.ndarray,
        u: np.ndarray,
        hourly_forecast: list[dict],
    ) -> list[SimulationPoint]:
        """Build simulation point list from LP solution."""
        points = []
        cumulative_runtime = 0.0

        for t, hf in enumerate(hourly_forecast):
            runtime_this_hour = u[t] * 60.0
            cumulative_runtime += runtime_this_hour

            points.append(SimulationPoint(
                time=hf["hour"],
                indoor_temp=T_trajectory[t],
                outdoor_temp=hf["temp"],
                hvac_running=u[t] > 0.1,
                cumulative_runtime_minutes=cumulative_runtime,
            ))

        # Final point
        if hourly_forecast:
            last = hourly_forecast[-1]
            points.append(SimulationPoint(
                time=last["hour"] + timedelta(hours=1),
                indoor_temp=T_trajectory[len(hourly_forecast)],
                outdoor_temp=last["temp"],
                hvac_running=False,
                cumulative_runtime_minutes=cumulative_runtime,
            ))

        return points

    # ── Energy Estimates ────────────────────────────────────────────

    def _cop_at_outdoor_temp(self, outdoor_temp: float, mode: str) -> float:
        """Estimate COP at a given outdoor temperature.

        COP degrades independently from capacity: as conditions worsen the
        compressor works harder (lower COP) while also delivering less output
        (lower capacity). Both effects must be modelled separately to get
        accurate electrical power estimates.
        """
        if mode == "cool":
            base_cop = 3.5
            degradation = _ALPHA_COOL * (outdoor_temp - _T_REF)
            return max(1.0, base_cop * (1.0 - degradation))
        elif mode == "heat":
            base_cop = 3.0
            degradation = _ALPHA_HEAT * (_T_REF - outdoor_temp)
            return max(1.0, base_cop * (1.0 - degradation))
        return 1.0

    def _power_watts_at_outdoor_temp(
        self, outdoor_temp: float, mode: str, params: dict,
    ) -> float:
        """Estimate electrical power draw (W) at a given outdoor temperature.

        Power = capacity_delivered / COP, converted from BTU/hr to watts.
        Capacity and COP degrade independently with outdoor temperature.
        """
        capacity_btu = abs(self._hvac_capacity(mode, outdoor_temp, params))
        cop = self._cop_at_outdoor_temp(outdoor_temp, mode)
        # 1 BTU/hr = 0.293071 W
        return (capacity_btu / cop) * 0.293071

    def _add_energy_estimates(
        self,
        schedule: OptimizedSchedule,
        u_opt: np.ndarray,
        u_baseline: np.ndarray,
        hourly_forecast: list[dict],
        mode: str,
    ) -> None:
        """Add kWh, CO2, and cost estimates to the schedule.

        Uses per-hour outdoor temperature to compute COP and power draw,
        so that time-shifting HVAC to milder hours is properly credited.
        """
        params = self._extract_params()

        opt_kwh_total = 0.0
        base_kwh_total = 0.0
        opt_co2 = 0.0
        base_co2 = 0.0
        opt_cost = 0.0
        base_cost = 0.0
        has_carbon = any(h.get("carbon_intensity") for h in hourly_forecast)
        has_cost = any(h.get("electricity_rate") for h in hourly_forecast)

        for t, hf in enumerate(hourly_forecast):
            T_out = hf["temp"]
            power_w = self._power_watts_at_outdoor_temp(T_out, mode, params)
            kwh_opt = u_opt[t] * power_w / 1000.0
            kwh_base = (
                u_baseline[t] * power_w / 1000.0 if t < len(u_baseline) else 0.0
            )
            opt_kwh_total += kwh_opt
            base_kwh_total += kwh_base

            if has_carbon:
                ci = hf.get("carbon_intensity") or 0.0
                opt_co2 += kwh_opt * ci
                base_co2 += kwh_base * ci

            if has_cost:
                rate = hf.get("electricity_rate") or 0.0
                opt_cost += kwh_opt * rate
                base_cost += kwh_base * rate

        schedule.optimized_kwh = opt_kwh_total
        schedule.baseline_kwh = base_kwh_total

        if has_carbon:
            schedule.optimized_co2_grams = opt_co2
            schedule.baseline_co2_grams = base_co2

        if has_cost:
            schedule.optimized_cost = opt_cost
            schedule.baseline_cost = base_cost

    def _empty_schedule(self) -> OptimizedSchedule:
        """Return an empty schedule when no forecast data available."""
        return OptimizedSchedule(
            entries=[],
            baseline_runtime_minutes=0,
            optimized_runtime_minutes=0,
            savings_pct=0,
        )

    # ── Public Accessors ────────────────────────────────────────────

    @property
    def confidence(self) -> float:
        """Model confidence from the underlying estimator."""
        return self.estimator.confidence

    def summary(self) -> str:
        """Human-readable summary of the grey-box optimizer state."""
        est = self.estimator
        params = self._extract_params()
        lines = [
            "=== Grey-Box Optimizer (LP + Kalman Filter) ===",
            "",
            f"Confidence: {est.confidence:.0%} ({est._n_obs} observations)",
            f"Envelope R: {est.R_value:.1f} °F·hr/BTU",
            f"Thermal mass: {est.thermal_mass:.0f} BTU/°F",
            f"Cooling capacity: {params['Q_cool_base']:.0f} BTU/hr (at {_T_REF}°F)",
            f"Heating capacity: {params['Q_heat_base']:.0f} BTU/hr (at {_T_REF}°F)",
            "",
            f"Uncertainty margins at 12hr horizon:",
        ]

        # Show example margins
        mock_forecast = [{"temp": 85.0} for _ in range(12)]
        margins = self._compute_uncertainty_margins(12, mock_forecast, "cool", params)
        lines.append(f"  Cooling at 85°F: ±{margins[6]:.1f}°F at 6hr, ±{margins[12]:.1f}°F at 12hr")

        mock_forecast = [{"temp": 30.0} for _ in range(12)]
        margins = self._compute_uncertainty_margins(12, mock_forecast, "heat", params)
        lines.append(f"  Heating at 30°F: ±{margins[6]:.1f}°F at 6hr, ±{margins[12]:.1f}°F at 12hr")

        return "\n".join(lines)
