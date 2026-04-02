"""Coefficient sensitivity calculator for the slow calibration layer.

For each calibratable coefficient θ_k, computes ∂T_air_pred/∂θ_k — how much
the predicted air temperature would change per unit change in that coefficient.

These sensitivities are computed from values already cached in the conditioned
innovation record (``_last_thermal_loads`` snapshot), so they require no
additional physics computation.

The sensitivity matrix J (N × K) is used by the ``CoefficientCalibrator`` in
a ridge regression:  δ = (JᵀJ + λI)⁻¹ Jᵀe, where e is the innovation vector.
"""

from __future__ import annotations

import math

# Default constants (same as thermal_estimator.py module-level values)
_WIND_INFILTRATION_COEFF = 0.025
_STACK_EFFECT_COEFF = 0.02
_K_ATTIC = 50.0
_K_CRAWLSPACE = 25.0
_INTERNAL_GAIN_BASE_BTU = 800.0
_INTERNAL_GAIN_PER_PERSON_BTU = 350.0
_PRECIPITATION_OFFSET_F = 3.0
_ALPHA_COOL = 0.012
_ALPHA_HEAT = 0.015
_T_REF = 75.0
_SOLAR_MASS_FRACTION = 0.3

# Names of calibratable coefficients (must match coefficient_store.py)
COEFFICIENT_NAMES = (
    "wind_infiltration",
    "k_attic",
    "k_crawlspace",
    "internal_gain_base",
    "alpha_cool",
    "alpha_heat",
    "stack_effect",
    "internal_gain_per_person",
    "precipitation_offset",
    "solar_mass_fraction",
)


def compute_sensitivities(record: dict) -> dict[str, float]:
    """Compute per-coefficient sensitivities from a conditioned innovation record.

    Each sensitivity s_k = ∂T_air_pred / ∂θ_k, representing how much the
    predicted T_air would shift per unit change in coefficient θ_k.

    The record is a snapshot of ``_last_thermal_loads`` enriched with
    ``hvac_mode``, ``hvac_running``, ``outdoor_temp``, ``dt_hours``, etc.

    Returns:
        Dict mapping coefficient name to sensitivity value (°F per unit of θ).
        Coefficients with zero sensitivity (missing data, inactive) are included
        with value 0.0.
    """
    # Extract cached intermediate values
    ua_value = record.get("ua_value", 0.0)
    infiltration = record.get("infiltration_factor", 1.0)
    indoor_temp = record.get("indoor_temp", 72.0)
    effective_outdoor = record.get("effective_outdoor_temp", indoor_temp)
    outdoor_temp = record.get("outdoor_temp", effective_outdoor)
    wind_speed = record.get("wind_speed_mph")
    attic_temp = record.get("attic_temp")
    crawlspace_temp = record.get("crawlspace_temp")
    people_count = record.get("people_count")
    dt_hours = record.get("dt_hours", 5.0 / 60.0)
    hvac_mode = record.get("hvac_mode", "off")
    hvac_running = record.get("hvac_running", False)
    q_hvac = record.get("q_hvac", 0.0)
    precipitation = record.get("precipitation", False)

    # We need C_inv to convert BTU/hr → °F/step.  It's not directly in the
    # thermal loads dict, but we can infer it from the load components and
    # the observed temperature change.  For the sensitivity matrix we use
    # an approximate C_inv derived from the UA value and envelope area.
    #
    # Actually, the key insight is that we don't need absolute °F values —
    # the ridge regression regresses innovations (already in °F) against
    # sensitivities.  As long as the relative magnitudes are correct, the
    # solver produces correct multiplier corrections.  We set C_inv = 1
    # effectively, making sensitivities in BTU/hr units rather than °F.
    # The regression normalizes this out since all coefficients use the
    # same C_inv.
    #
    # Using dt_hours to convert per-step sensitivity.
    dt = dt_hours

    sensitivities: dict[str, float] = {}

    # ── wind_infiltration ─────────────────────────────────────────
    # Q_env = UA * infiltration * (T_out_eff - T_air)
    # infiltration includes wind_coeff * wind_speed
    # ∂Q_env/∂wind_coeff = UA * wind_speed * (T_out_eff - T_air)
    if wind_speed is not None and wind_speed > 0:
        sensitivities["wind_infiltration"] = (
            ua_value * wind_speed * (effective_outdoor - indoor_temp) * dt
        )
    else:
        sensitivities["wind_infiltration"] = 0.0

    # ── stack_effect ──────────────────────────────────────────────
    # infiltration includes stack_coeff * sqrt(|ΔT|)
    # ∂Q_env/∂stack_coeff = UA * sqrt(|ΔT|) * (T_out_eff - T_air)
    delta_t = abs(effective_outdoor - indoor_temp)
    sensitivities["stack_effect"] = (
        ua_value * math.sqrt(max(0.0, delta_t)) * (effective_outdoor - indoor_temp) * dt
    )

    # ── k_attic ───────────────────────────────────────────────────
    # Q_attic = k_attic * area_scale * (T_attic - T_air)
    # ∂Q/∂k_attic = area_scale * (T_attic - T_air)
    # area_scale is baked into the cached k_attic value; we extract it
    # from attic_contribution / k_attic / (T_attic - T_air) if available
    if attic_temp is not None:
        attic_contribution = record.get("attic_contribution_btu", 0.0)
        attic_delta = attic_temp - indoor_temp
        if abs(attic_delta) > 0.1:
            # area_scale factor = attic_contribution / (_K_ATTIC * attic_delta)
            # but we just need ∂T_air/∂(k_attic multiplier) which is
            # attic_contribution / _K_ATTIC (since contribution = k_attic_cal * area_scale * delta)
            sensitivities["k_attic"] = attic_contribution / _K_ATTIC * dt if _K_ATTIC > 0 else 0.0
        else:
            sensitivities["k_attic"] = 0.0
    else:
        sensitivities["k_attic"] = 0.0

    # ── k_crawlspace ──────────────────────────────────────────────
    if crawlspace_temp is not None:
        crawl_contribution = record.get("crawlspace_contribution_btu", 0.0)
        crawl_delta = crawlspace_temp - indoor_temp
        if abs(crawl_delta) > 0.1:
            sensitivities["k_crawlspace"] = crawl_contribution / _K_CRAWLSPACE * dt if _K_CRAWLSPACE > 0 else 0.0
        else:
            sensitivities["k_crawlspace"] = 0.0
    else:
        sensitivities["k_crawlspace"] = 0.0

    # ── internal_gain_base ────────────────────────────────────────
    # Q_internal = base + per_person * people
    # ∂Q/∂base = 1 (always, regardless of occupancy)
    sensitivities["internal_gain_base"] = dt

    # ── internal_gain_per_person ──────────────────────────────────
    # ∂Q/∂per_person = people_count (0 when unknown)
    if people_count is not None and people_count > 0:
        sensitivities["internal_gain_per_person"] = people_count * dt
    else:
        sensitivities["internal_gain_per_person"] = 0.0

    # ── alpha_cool ────────────────────────────────────────────────
    # Q_hvac_cool = -Q_cool_base * max(0.1, 1 - alpha_cool * (T_out - T_ref)) * ...
    # ∂Q_hvac/∂alpha_cool = -Q_cool_base * (-(T_out - T_ref)) * ... = Q_cool_base * (T_out - T_ref) * ...
    # Simplified: when cooling is active, sensitivity ≈ |q_hvac| * (T_out - T_ref) / alpha_cool_effective
    if hvac_running and hvac_mode == "cool":
        temp_deviation = outdoor_temp - _T_REF
        if abs(temp_deviation) > 0.1 and abs(q_hvac) > 0:
            # q_hvac is negative for cooling; sensitivity of q_hvac to alpha_cool multiplier
            sensitivities["alpha_cool"] = q_hvac * temp_deviation / max(0.1, 1.0 - _ALPHA_COOL * temp_deviation) * dt
        else:
            sensitivities["alpha_cool"] = 0.0
    else:
        sensitivities["alpha_cool"] = 0.0

    # ── alpha_heat ────────────────────────────────────────────────
    if hvac_running and hvac_mode == "heat":
        temp_deviation = _T_REF - outdoor_temp
        if abs(temp_deviation) > 0.1 and abs(q_hvac) > 0:
            sensitivities["alpha_heat"] = -q_hvac * temp_deviation / max(0.1, 1.0 - _ALPHA_HEAT * temp_deviation) * dt
        else:
            sensitivities["alpha_heat"] = 0.0
    else:
        sensitivities["alpha_heat"] = 0.0

    # ── precipitation_offset ──────────────────────────────────────
    # ∂Q_env/∂precip_offset = -UA * infiltration (only when raining)
    if precipitation:
        sensitivities["precipitation_offset"] = (
            -ua_value * infiltration * dt
        )
    else:
        sensitivities["precipitation_offset"] = 0.0

    # ── solar_mass_fraction ──────────────────────────────────────
    # Controls how much solar gain goes to thermal mass vs. air.
    # Increasing f_s means less solar to air (negative T_air sensitivity)
    # and more to mass (positive T_mass sensitivity, which indirectly
    # warms air via R_int coupling over time).
    # Net air sensitivity: ∂T_air/∂f_s = -Q_solar * dt
    # (immediate air temperature drops when more solar goes to mass)
    q_solar = record.get("q_solar", 0.0)
    if q_solar > 0:
        sensitivities["solar_mass_fraction"] = -q_solar * dt
    else:
        sensitivities["solar_mass_fraction"] = 0.0

    return sensitivities
