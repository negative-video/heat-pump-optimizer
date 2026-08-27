"""Apparent temperature (heat index) calculations for comfort-aware optimization.

Uses the NWS heat index formula to calculate how temperature "feels" given
indoor humidity. This allows the optimizer to proactively cool more aggressively
when high humidity makes the air feel warmer than the thermometer reads, reducing
manual thermostat overrides from uncomfortable occupants.
"""

from __future__ import annotations


def calculate_apparent_temperature(temp_f: float, humidity_pct: float) -> float:
    """Calculate the apparent (feels-like) temperature given temp and humidity.

    Uses the NWS Steadman/Rothfusz heat index formula for warm conditions
    (temp >= 70°F, humidity >= 40%) and a small dry-air correction for
    heating scenarios (humidity < 30%).

    Args:
        temp_f: Indoor air temperature in °F.
        humidity_pct: Relative humidity as a percentage (0-100).

    Returns:
        Apparent temperature in °F. Returns temp_f unchanged when
        humidity effects are negligible.
    """
    # Dry air makes it feel slightly cooler (relevant for heating)
    if humidity_pct < 30 and temp_f >= 65.0:
        correction = (30.0 - humidity_pct) * 0.05  # max ~1.5°F at 0% RH
        return temp_f - correction

    # Heat index only meaningful at higher temps and moderate+ humidity
    if temp_f < 70.0 or humidity_pct < 40.0:
        return temp_f

    return _nws_heat_index(temp_f, humidity_pct)


def _nws_heat_index(temp_f: float, humidity_pct: float) -> float:
    """NWS heat index using Steadman simple formula with Rothfusz regression.

    Follows the NWS algorithm:
    1. Try simple Steadman formula first.
    2. If result >= 80°F, use full Rothfusz regression.
    3. Apply low-humidity and high-humidity adjustments as needed.

    Reference: https://www.wpc.ncep.noaa.gov/html/heatindex_equation.shtml
    """
    t = temp_f
    r = humidity_pct

    # Step 1: Simple Steadman formula
    hi = 0.5 * (t + 61.0 + ((t - 68.0) * 1.2) + (r * 0.094))

    if hi < 80.0:
        return hi

    # Step 2: Full Rothfusz regression
    hi = (
        -42.379
        + 2.04901523 * t
        + 10.14333127 * r
        - 0.22475541 * t * r
        - 6.83783e-3 * t * t
        - 5.481717e-2 * r * r
        + 1.22874e-3 * t * t * r
        + 8.5282e-4 * t * r * r
        - 1.99e-6 * t * t * r * r
    )

    # Step 3: Low-humidity adjustment (RH < 13% and 80 < T < 112)
    if r < 13.0 and 80.0 < t < 112.0:
        adjustment = ((13.0 - r) / 4.0) * ((17.0 - abs(t - 95.0)) / 17.0) ** 0.5
        hi -= adjustment

    # Step 4: High-humidity adjustment (RH > 85% and 80 < T < 87)
    if r > 85.0 and 80.0 < t < 87.0:
        adjustment = ((r - 85.0) / 10.0) * ((87.0 - t) / 5.0)
        hi += adjustment

    return hi


# ── Effective comfort band derivation ────────────────────────────────

# Absolute floor on the optimizer's working band. Below this the planner has
# no room to do anything but chatter, and every "safety" narrowing layer that
# pushed it lower was making control worse, not safer.
COMFORT_BAND_MIN_WIDTH_F = 2.0

# Fraction shrunk from EACH side per activation tier. These used to be
# scattered across three modules and compounded silently.
TIER_NARROWING = {
    "learning": 0.20,       # inner 60%
    "conservative": 0.10,   # inner 80%
    "standard": 0.0,
    "confident": 0.0,
}


def derive_effective_comfort_band(
    base: tuple[float, float],
    *,
    override_shift: float = 0.0,
    demand_response_delta: float = 0.0,
    constraints=(),
    safety_limits: tuple[float, float] | None = None,
    humidity_band: tuple[float, float] | None = None,
    tier: str | None = None,
    min_width: float = COMFORT_BAND_MIN_WIDTH_F,
) -> tuple[float, float]:
    """Derive the band the optimizer may actually work in, in one place.

    The narrowing layers used to live in three modules (coordinator comfort
    selection, shoulder-day handling, and an optimizer retry fallback) and
    compounded invisibly, so a nominal 6F band could collapse to 1-2F without
    anything logging that it had happened. This function fixes the ordering
    and enforces a floor.

    Order of application, widest influence first:

    1. ``override_shift`` -- learned occupant preference, shifts both edges.
    2. ``demand_response_delta`` -- widens both edges (a grid event buys
       tolerance, it does not remove it). The band after steps 1-2 is the
       occupant-sanctioned outer bound; nothing later may exceed it.
    3. ``humidity_band`` -- a pre-computed apparent-temperature correction,
       applied as an intersection with the current band.
    4. ``tier`` -- activation-tier narrowing.
    5. Floor: if steps 3-4 left the band narrower than ``min_width``, it is
       reopened symmetrically toward the step-2 bounds. Heuristic caution
       layers do not get to strangle the planner.
    6. ``constraints`` -- explicit external min_temp/max_temp requests.
       These are deliberate and *are* allowed to narrow past ``min_width``.
    7. ``safety_limits`` -- absolute guardrails, never exceeded.

    Returns the effective ``(min, max)``. If the inputs are contradictory the
    band is returned collapsed rather than inverted.
    """
    lo, hi = float(base[0]), float(base[1])

    if override_shift:
        lo += override_shift
        hi += override_shift

    if demand_response_delta:
        lo -= demand_response_delta
        hi += demand_response_delta

    outer_lo, outer_hi = lo, hi

    if humidity_band is not None:
        lo = max(lo, humidity_band[0])
        hi = min(hi, humidity_band[1])

    fraction = TIER_NARROWING.get(tier or "", 0.0)
    if fraction > 0 and hi > lo:
        margin = (hi - lo) * fraction
        lo += margin
        hi -= margin

    # Reopen toward the occupant-sanctioned bounds if the heuristic layers
    # compounded into an unplannable sliver. This is the check the old
    # scattered narrowing had nowhere to live.
    if hi - lo < min_width:
        deficit = min_width - (hi - lo)
        room_lo = lo - outer_lo
        room_hi = outer_hi - hi
        take_lo = min(deficit / 2.0, max(0.0, room_lo))
        take_hi = min(deficit - take_lo, max(0.0, room_hi))
        # Anything the high side could not absorb goes back to the low side.
        take_lo = min(deficit - take_hi, max(0.0, room_lo))
        lo -= take_lo
        hi += take_hi

    # Explicit constraints are honored even when they squeeze hard: an
    # external integration asking for "never above 74" means it.
    for constraint in constraints or ():
        ctype = constraint.get("type")
        value = constraint.get("value")
        if value is None:
            continue
        if ctype == "min_temp":
            lo = max(lo, float(value))
        elif ctype == "max_temp":
            hi = min(hi, float(value))

    if safety_limits is not None:
        lo = max(lo, safety_limits[0])
        hi = min(hi, safety_limits[1])

    if hi < lo:
        return (lo, lo)

    return (lo, hi)
