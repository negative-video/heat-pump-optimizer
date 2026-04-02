"""Adaptive performance model that computes HVAC deltas from Kalman-estimated parameters.

Implements the same interface as PerformanceModel (cooling_delta, heating_delta,
passive_drift, etc.) but derives values from the ThermalEstimator's learned
R, C, and Q_hvac parameters instead of static lookup tables.

In dual-mode operation, the coordinator chooses between this model and the
static PerformanceModel based on the estimator's confidence level.
"""

from __future__ import annotations

from ..learning.thermal_estimator import ThermalEstimator


class AdaptivePerformanceModel:
    """Computes HVAC performance deltas from learned thermal parameters.

    All methods return values in the same units as PerformanceModel:
    °F/hr of indoor temperature change.
    """

    def __init__(self, estimator: ThermalEstimator):
        self.estimator = estimator

    # ── Delta lookups (same interface as PerformanceModel) ──────────

    def passive_drift(self, outdoor_temp: float) -> float:
        """Indoor °F change per hour with HVAC off.

        Computed from learned R (envelope resistance) and C (air capacitance),
        including the effect of thermal mass coupling.

        Positive = house warming, negative = house cooling.
        """
        R_inv = self.estimator.R_inv
        R_int_inv = self.estimator.R_int_inv
        C_inv = self.estimator.C_inv
        T_air = self.estimator.T_air
        T_mass = self.estimator.T_mass

        Q_env = R_inv * (outdoor_temp - T_air)
        Q_int = R_int_inv * (T_mass - T_air)
        return C_inv * (Q_env + Q_int)

    def cooling_delta(self, outdoor_temp: float) -> float:
        """Indoor °F change per hour during cooling (negative = cooling).

        Net rate = passive drift - cooling capacity / C_air.
        This is what PerformanceModel.cooling_delta returns: the observed
        net change including both HVAC effect and envelope drift.
        """
        drift = self.passive_drift(outdoor_temp)
        Q_cool = self.estimator.cooling_capacity(outdoor_temp)
        return drift - self.estimator.C_inv * Q_cool

    def heating_delta(self, outdoor_temp: float) -> float:
        """Indoor °F change per hour during heating (positive = heating).

        Net rate = passive drift + heating capacity / C_air.
        """
        drift = self.passive_drift(outdoor_temp)
        Q_heat = self.estimator.heating_capacity(outdoor_temp)
        return drift + self.estimator.C_inv * Q_heat

    def aux_heating_delta(self, outdoor_temp: float) -> float:
        """Indoor °F change per hour during auxiliary heat.

        Not separately modeled by the Kalman filter — returns the standard
        heating delta as a conservative estimate.
        """
        return self.heating_delta(outdoor_temp)

    # ── Derived metrics (same interface as PerformanceModel) ────────

    def relative_efficiency(self, outdoor_temp: float, mode: str) -> float:
        """Relative efficiency vs. best conditions (0-1 scale)."""
        if mode == "cool":
            delta = abs(self.cooling_delta(outdoor_temp))
            # Best cooling: at lowest reasonable outdoor temp
            best = abs(self.cooling_delta(50.0))
            if best < 0.01:
                return 0.0
            return min(1.0, delta / best)
        elif mode == "heat":
            delta = self.heating_delta(outdoor_temp)
            best = self.heating_delta(65.0)
            if best < 0.01:
                return 0.0
            return min(1.0, delta / best)
        return 0.0

    def runtime_needed(self, outdoor_temp: float, mode: str, degrees: float) -> float:
        """Minutes of runtime to change indoor temp by `degrees` °F."""
        if mode == "cool":
            delta = self.cooling_delta(outdoor_temp)
            if delta >= 0:
                return float("inf")
            hours = degrees / abs(delta)
        elif mode == "heat":
            delta = self.heating_delta(outdoor_temp)
            if delta <= 0:
                return float("inf")
            hours = degrees / delta
        else:
            return float("inf")
        return hours * 60.0

    def coast_duration(self, outdoor_temp: float, mode: str, degrees: float) -> float:
        """Minutes the house can coast before drifting by `degrees` °F."""
        drift = self.passive_drift(outdoor_temp)
        if mode == "cool":
            if drift <= 0:
                return float("inf")
            hours = degrees / drift
        elif mode == "heat":
            if drift >= 0:
                return float("inf")
            hours = degrees / abs(drift)
        else:
            return float("inf")
        return hours * 60.0

    def net_cooling_rate(self, outdoor_temp: float) -> float:
        """Net indoor temp change per hour when cooling is running."""
        return self.cooling_delta(outdoor_temp)

    def net_heating_rate(self, outdoor_temp: float) -> float:
        """Net indoor temp change per hour when heating is running."""
        return self.heating_delta(outdoor_temp)

    # ── Properties for compatibility ────────────────────────────────

    @property
    def resist_balance_point(self) -> float:
        """Outdoor temp where passive drift is approximately zero.

        Solved from: R_inv * (T_out - T_air) + R_int_inv * (T_mass - T_air) ≈ 0
        When thermal mass is near air temp: T_balance ≈ T_air

        Clamped to 20-90°F: no residential home has a balance point outside
        this range.  The R_int_inv / R_inv ratio can amplify small T_mass-T_air
        gaps into absurd values when internal coupling is poorly learned.
        """
        T_air = self.estimator.T_air
        T_mass = self.estimator.T_mass
        R_inv = self.estimator.R_inv
        R_int_inv = self.estimator.R_int_inv

        if R_inv < 1e-6:
            return T_air

        # Solve: R_inv * (T_out - T_air) + R_int_inv * (T_mass - T_air) = 0
        # T_out = T_air - (R_int_inv / R_inv) * (T_mass - T_air)
        raw_bp = T_air - (R_int_inv / R_inv) * (T_mass - T_air)
        return max(20.0, min(90.0, raw_bp))

    @property
    def heat_balance_point(self) -> float | None:
        """Approximate heat balance point (where heating capacity = heat loss rate)."""
        # Not directly available from the Kalman filter parameters;
        # return None and let the caller use a default
        return None

    @property
    def cool_differential(self) -> float:
        """Thermostat deadband for cooling.

        Uses the profile value if available (passed during init),
        otherwise defaults to 1.0°F.
        """
        return getattr(self, "_cool_differential", 1.0)

    @cool_differential.setter
    def cool_differential(self, value: float) -> None:
        self._cool_differential = value

    @property
    def heat_differential(self) -> float:
        """Thermostat deadband for heating."""
        return getattr(self, "_heat_differential", 1.0)

    @heat_differential.setter
    def heat_differential(self, value: float) -> None:
        self._heat_differential = value

    @property
    def confidence(self) -> float:
        """Model confidence from estimator (0.0 to 1.0)."""
        return self.estimator.confidence

    def summary(self) -> str:
        """Human-readable summary of the adaptive model state."""
        est = self.estimator
        lines = [
            "=== Adaptive Performance Model (Kalman Filter) ===",
            "",
            f"Confidence: {est.confidence:.0%} ({est._n_obs} observations)",
            f"Envelope R: {est.R_value:.1f} °F·hr/BTU",
            f"Thermal mass: {est.thermal_mass:.0f} BTU/°F",
            f"Air T: {est.T_air:.1f}°F, Mass T: {est.T_mass:.1f}°F",
            f"Cooling capacity: {float(est.x[6]):.0f} BTU/hr (at {75}°F)",
            f"Heating capacity: {float(est.x[7]):.0f} BTU/hr (at {75}°F)",
            "",
            "Sample deltas:",
            f"  Cooling at 75°F: {self.cooling_delta(75):.2f}°F/hr",
            f"  Cooling at 95°F: {self.cooling_delta(95):.2f}°F/hr",
            f"  Heating at 35°F: {self.heating_delta(35):.2f}°F/hr",
            f"  Drift at 30°F:  {self.passive_drift(30):.2f}°F/hr",
            f"  Drift at 80°F:  {self.passive_drift(80):.2f}°F/hr",
            f"  Balance point: {self.resist_balance_point:.1f}°F",
        ]
        return "\n".join(lines)
