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
