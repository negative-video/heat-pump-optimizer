"""Coarse thermal characterisation derived from measured profiler data.

This replaces the 9-state EKF (audit item 8). The EKF tried to identify R,
two capacitances, an internal coupling, two HVAC capacities, a solar gain and
two temperatures from a single observed indoor temperature. That is
structurally unobservable: one measurement cannot pin seven-plus parameters,
so the parameter that absorbed residual error changed with conditions and the
estimate random-walked (R-value 12.7 -> 36.2 -> 5.2 -> 15 over three days on
the maintainer's own install). Each of the ten hand-tuned safeguards in the
old module was a prior in disguise.

Control never needed those nine states. It needs three numbers, and each one
is separately observable from data the profiler already collects:

* **drift slope k** -- how fast the house tracks outdoor temperature, the
  inverse of its time constant. This is the slope of the measured passive
  drift trendline, an insulation proxy.
* **gross HVAC rate** -- the equipment's own forcing. Already available from
  ``PerformanceModel.gross_hvac_rate``, envelope load removed.
* **thermal mass** -- seeded from floor area rather than estimated. A live
  estimate was never identifiable, and the only thing it feeds (converting
  appliance BTU to degrees per hour) is insensitive to the error a seed
  carries.

Everything here is deliberately a documented approximation. The house is
treated as a single node, which it is not; the maintainer's own data fits a
two-node model (indoor coupled to both outdoor and attic) noticeably better.
The single-node numbers are honest as coarse diagnostics and as a sanity
bound on the planner, and they are not represented as anything more.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# BTU per degree F per square foot of conditioned floor area. Rule-of-thumb
# seed for air plus furnishings plus the fraction of structure that responds
# on the timescales the optimizer plans over (hours, not days).
THERMAL_MASS_BTU_PER_SQFT = 0.6

# Fallback floor area when the user has not supplied one.
DEFAULT_SQFT = 2000.0

# A drift slope below this is treated as unmeasured rather than as a
# superinsulated house. Slopes this small usually mean too few passive
# observations, not a passivhaus.
MIN_CREDIBLE_DRIFT_SLOPE = 0.005

# Above this the fit is more likely dominated by a window left open or by
# HVAC observations misclassified as passive drift.
MAX_CREDIBLE_DRIFT_SLOPE = 0.50


@dataclass(frozen=True)
class ThermalProfile:
    """Coarse, measured thermal characterisation of the house.

    Every field is None when the profiler lacks the observations to support
    it. None means "not measured yet", never "zero" -- reporting a confident
    number from absent data is the specific failure this module exists to
    end.
    """

    drift_slope_per_hour: float | None
    time_constant_hours: float | None
    thermal_mass_btu_per_f: float
    envelope_loss_btu_per_hour_f: float | None
    observations: int
    source: str

    @property
    def is_measured(self) -> bool:
        return self.drift_slope_per_hour is not None

    def as_dict(self) -> dict[str, Any]:
        """Diagnostic payload. Keys are stable; values may be None."""
        return {
            "drift_slope_per_hour": (
                round(self.drift_slope_per_hour, 4) if self.drift_slope_per_hour is not None else None
            ),
            "time_constant_hours": (
                round(self.time_constant_hours, 1) if self.time_constant_hours is not None else None
            ),
            "thermal_mass_btu_per_f": round(self.thermal_mass_btu_per_f, 0),
            "envelope_loss_btu_per_hour_f": (
                round(self.envelope_loss_btu_per_hour_f, 1) if self.envelope_loss_btu_per_hour_f is not None else None
            ),
            "observations": self.observations,
            "source": self.source,
            "model": "single-node approximation from measured passive drift",
        }


def thermal_mass_for(sqft: float | None) -> float:
    """Seed thermal mass from conditioned floor area.

    Deliberately not learned. The old code derived this from the EKF's C_inv
    state, which meant the profiler's appliance-BTU conversion moved whenever
    the filter wandered -- a live dependency on the least trustworthy number
    in the system, feeding the data used to build its replacement.
    """
    area = sqft if sqft and sqft > 0 else DEFAULT_SQFT
    return THERMAL_MASS_BTU_PER_SQFT * area


def derive_thermal_profile(profiler, sqft: float | None = None) -> ThermalProfile:
    """Derive the coarse thermal profile from measured profiler data.

    The drift slope comes from the passive-drift ("resist") trendline, which
    is fitted to observations where the HVAC was not running. With the house
    modelled as one node, ``dT_in/dt = k * (T_out - T_in)``, so the slope of
    drift against outdoor temperature is k directly, and the time constant is
    its reciprocal.
    """
    mass = thermal_mass_for(sqft)
    observations = 0
    slope: float | None = None

    # This feeds the whole sensor payload, so a diagnostic derivation must
    # never be the thing that takes the integration down.
    try:
        observations = int(profiler.mode_observation_count("resist"))
    except Exception:  # noqa: BLE001
        observations = 0

    try:
        profile = profiler.to_profile_data()
        resist = (profile.get("temperature") or {}).get("resist")
        trendline = (resist or {}).get("linear_trendline") or {}
        candidate = trendline.get("slope")
        if candidate is not None:
            candidate = float(candidate)
            if MIN_CREDIBLE_DRIFT_SLOPE <= abs(candidate) <= MAX_CREDIBLE_DRIFT_SLOPE:
                slope = abs(candidate)
    except Exception:  # noqa: BLE001
        slope = None

    if slope is None:
        return ThermalProfile(
            drift_slope_per_hour=None,
            time_constant_hours=None,
            thermal_mass_btu_per_f=mass,
            envelope_loss_btu_per_hour_f=None,
            observations=observations,
            source="unmeasured",
        )

    # tau = 1/k, and the envelope conductance follows from mass / tau.
    tau = 1.0 / slope
    return ThermalProfile(
        drift_slope_per_hour=slope,
        time_constant_hours=tau,
        thermal_mass_btu_per_f=mass,
        envelope_loss_btu_per_hour_f=mass * slope,
        observations=observations,
        source="measured",
    )
