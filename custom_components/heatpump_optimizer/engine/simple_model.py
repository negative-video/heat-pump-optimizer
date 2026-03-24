"""Simple performance model for lite/minimal setup mode.

Uses outdoor temperature as a direct efficiency proxy instead of
Beestat-measured deltas or EKF-learned parameters. Implements the same
interface as PerformanceModel so ScheduleOptimizer and ThermalSimulator
work without modification.

Physics basis: heat pump COP degrades as the outdoor-indoor temperature
differential increases. Since the thermostat holds indoor temp roughly
constant, outdoor temp alone is a strong proxy for efficiency (R > 0.95
correlation with Beestat delta magnitudes).
"""

from __future__ import annotations


class SimplePerformanceModel:
    """Outdoor-temp-only efficiency model for minimal setup mode.

    Generates synthetic delta curves that approximate the shape of real
    Beestat profiles without requiring measured data or learned parameters.

    The formulas are calibrated against the from_defaults() conservative
    curves in PerformanceModel:
      Cooling at 70°F → ~-3.0°F/hr, at 100°F → ~-0.5°F/hr
      Heating at 50°F → ~1.5°F/hr, at 0°F → ~0.3°F/hr
      Passive drift crosses zero near 50°F (resist balance point)
    """

    def __init__(self) -> None:
        # Match PerformanceModel interface
        self.heat_balance_point: float = 25.0
        self.resist_balance_point: float = 50.0
        self.cool_differential: float = 1.0
        self.heat_differential: float = 1.0
        self.cool_setpoint: float = 74.0
        self.heat_setpoint: float = 68.0

    # ── Delta lookups (match PerformanceModel interface) ─────────

    def cooling_delta(self, outdoor_temp: float) -> float:
        """Indoor °F change per hour of cooling at given outdoor temp.

        Returns negative values. More negative = more effective.
        Conservative curve: ~-3.0°F/hr at 70°F, ~-0.5°F/hr at 100°F.
        """
        # Clamp to reasonable cooling range
        t = max(outdoor_temp, 60.0)
        # Linear degradation matching PerformanceModel.from_defaults() shape
        delta = -(2.5 - 0.057 * (t - 70.0))
        return max(min(delta, -0.3), -4.0)

    def heating_delta(self, outdoor_temp: float) -> float:
        """Indoor °F change per hour of heating at given outdoor temp.

        Returns positive values. More positive = more effective.
        Conservative curve: ~1.5°F/hr at 50°F, ~0.3°F/hr at 0°F.
        """
        t = min(outdoor_temp, 55.0)
        delta = 0.3 + 0.024 * (t - 0.0)
        return max(min(delta, 2.0), 0.2)

    def aux_heating_delta(self, outdoor_temp: float) -> float:
        """No aux heat data in lite mode."""
        return 0.0

    def passive_drift(self, outdoor_temp: float) -> float:
        """Indoor °F change per hour with HVAC off.

        Negative below ~50°F (house cools), positive above (house warms).
        """
        return 0.03 * (outdoor_temp - 50.0)

    # ── Derived metrics (match PerformanceModel interface) ───────

    def relative_efficiency(self, outdoor_temp: float, mode: str) -> float:
        """Relative efficiency 0-1 vs best conditions."""
        if mode == "cool":
            delta = abs(self.cooling_delta(outdoor_temp))
            best = abs(self.cooling_delta(60.0))
            return delta / best if best > 0 else 0.0
        elif mode == "heat":
            delta = self.heating_delta(outdoor_temp)
            best = self.heating_delta(55.0)
            return delta / best if best > 0 else 0.0
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

    def summary(self) -> str:
        """Human-readable summary."""
        return (
            "=== Simple Performance Model (Lite Mode) ===\n"
            "\n"
            "Using weather-forecast-based efficiency estimation.\n"
            "No Beestat profile or learned parameters required.\n"
            f"\n"
            f"Cooling: ~{self.cooling_delta(70):.1f}°F/hr at 70°F, "
            f"~{self.cooling_delta(100):.1f}°F/hr at 100°F\n"
            f"Heating: ~{self.heating_delta(50):.1f}°F/hr at 50°F, "
            f"~{self.heating_delta(0):.1f}°F/hr at 0°F\n"
            f"Balance point: {self.resist_balance_point}°F"
        )
