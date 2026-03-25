"""Solar gain correction for passive thermal drift predictions.

The resist profile averages across all sky conditions at each outdoor
temperature. A sunny 85°F day heats the house faster than a cloudy 85°F day
due to solar radiation through windows.

This module applies a correction factor to passive drift based on:
  - Cloud cover (from weather forecast)
  - Solar altitude (from sun.sun entity or calculated from lat/time)
  - A learned solar gain coefficient (calibrated from prediction errors)

TODO: This module's cloud-cover coefficient could be absorbed into the EKF's
solar_gain_btu parameter (IDX_SOLAR_GAIN), which now performs the same role
via online Kalman learning. Consider deprecating in favor of the EKF path.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)

# Default solar gain coefficient (learned over time)
DEFAULT_SOLAR_COEFFICIENT = 0.3
MIN_SOLAR_COEFFICIENT = 0.0
MAX_SOLAR_COEFFICIENT = 1.0


@dataclass
class SolarAdjuster:
    """Adjusts passive drift rate for solar radiation effects."""

    latitude: float  # Degrees (e.g., 37.9 for Virginia)
    solar_coefficient: float = DEFAULT_SOLAR_COEFFICIENT

    def adjustment_factor(
        self,
        now: datetime,
        cloud_cover: float | None,
        sun_elevation: float | None = None,
        irradiance_w_m2: float | None = None,
    ) -> float:
        """Calculate the solar adjustment multiplier for passive drift.

        Returns a factor to multiply passive_drift() by:
          >1.0 = house gains/loses heat faster than the average profile
          <1.0 = house gains/loses heat slower than the average profile
          1.0  = no adjustment (average conditions or nighttime)

        Args:
            now: Current time (timezone-aware).
            cloud_cover: 0.0 (clear) to 1.0 (overcast). None = no adjustment.
            sun_elevation: Degrees above horizon. None = calculated from lat/time.
            irradiance_w_m2: Direct solar irradiance measurement (W/m²). When
                provided, computes solar gain directly instead of using the
                cloud_cover * altitude model.
        """
        # Direct irradiance model: compute Q_solar and derive adjustment factor
        if irradiance_w_m2 is not None:
            Q_solar = irradiance_w_m2 * self.solar_coefficient * 3.412
            # Average solar gain as baseline reference
            average_solar = self.solar_coefficient * 0.5 * 0.5
            # Scale relative to average conditions
            # At average: Q_solar_avg ≈ average_solar * 3.412 * some_reference_irradiance
            # Simpler: use the irradiance directly relative to a typical clear-sky ~500 W/m²
            typical_clear_sky = 500.0
            extra = self.solar_coefficient * (irradiance_w_m2 / typical_clear_sky)
            adjustment = 1.0 + (extra - average_solar)
            return max(0.5, min(2.0, adjustment))

        if cloud_cover is None:
            return 1.0

        # Get solar altitude
        if sun_elevation is None:
            sun_elevation = self._estimate_solar_elevation(now)

        # No solar effect at night
        if sun_elevation <= 0:
            return 1.0

        # Solar altitude factor: peaks at solar noon, zero at horizon
        # Normalize to 0-1 range (90° is directly overhead, rare at 37.9°N)
        altitude_factor = math.sin(math.radians(sun_elevation))
        altitude_factor = max(0.0, min(1.0, altitude_factor))

        # Clear sky = more solar gain, cloudy = less
        clear_sky = 1.0 - cloud_cover

        # The adjustment: how much extra drift beyond the average
        # Average conditions assumed to be ~50% cloud cover
        # Clear sky adds solar gain, overcast reduces it
        extra = self.solar_coefficient * clear_sky * altitude_factor

        # The base profile was measured under average conditions,
        # so we adjust relative to that average
        # Average clear_sky * altitude contribution ≈ 0.25 (rough midpoint)
        average_solar = self.solar_coefficient * 0.5 * 0.5
        adjustment = 1.0 + (extra - average_solar)

        return max(0.5, min(2.0, adjustment))  # Clamp to reasonable range

    def adjust_drift_rate(
        self,
        base_drift: float,
        outdoor_temp: float,
        resist_balance_point: float,
        now: datetime,
        cloud_cover: float | None,
        sun_elevation: float | None = None,
        irradiance_w_m2: float | None = None,
    ) -> float:
        """Apply solar correction to a passive drift rate.

        Only applies when outdoor conditions cause heat gain (above balance point
        in summer) or heat loss reduction (below balance point in winter).
        """
        factor = self.adjustment_factor(now, cloud_cover, sun_elevation, irradiance_w_m2)

        if factor == 1.0:
            return base_drift

        # Above balance point: solar increases heat gain (drift more positive)
        # Below balance point: solar reduces heat loss (drift less negative)
        # In both cases, more sun = drift moves toward positive
        adjusted = base_drift * factor

        _LOGGER.debug(
            "Solar adjustment: base_drift=%.3f, factor=%.2f, adjusted=%.3f "
            "(cloud=%.0f%%, elevation=%.1f°)",
            base_drift, factor, adjusted,
            (cloud_cover or 0) * 100,
            sun_elevation or 0,
        )

        return adjusted

    def update_coefficient(self, new_value: float) -> None:
        """Update the learned solar coefficient (called by model tracker)."""
        clamped = max(MIN_SOLAR_COEFFICIENT, min(MAX_SOLAR_COEFFICIENT, new_value))
        if clamped != self.solar_coefficient:
            _LOGGER.info(
                "Solar coefficient updated: %.3f → %.3f",
                self.solar_coefficient, clamped,
            )
            self.solar_coefficient = clamped

    def _estimate_solar_elevation(self, now: datetime) -> float:
        """Estimate solar elevation angle from latitude and time.

        Uses a simplified solar position calculation. For production,
        the sun.sun entity provides this directly.
        """
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        day_of_year = now.timetuple().tm_yday

        # Solar declination (approximate)
        declination = 23.45 * math.sin(math.radians(360 / 365 * (day_of_year - 81)))

        # Hour angle (15° per hour from solar noon)
        # Approximate solar noon at longitude 0 for UTC
        hour_angle = (now.hour + now.minute / 60 - 12) * 15

        # Solar elevation
        lat_rad = math.radians(self.latitude)
        dec_rad = math.radians(declination)
        ha_rad = math.radians(hour_angle)

        sin_elevation = (
            math.sin(lat_rad) * math.sin(dec_rad)
            + math.cos(lat_rad) * math.cos(dec_rad) * math.cos(ha_rad)
        )

        return math.degrees(math.asin(max(-1, min(1, sin_elevation))))

    def to_dict(self) -> dict:
        """Serialize for persistence."""
        return {
            "latitude": self.latitude,
            "solar_coefficient": self.solar_coefficient,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SolarAdjuster:
        """Restore from persisted data."""
        return cls(
            latitude=data.get("latitude", 37.9),
            solar_coefficient=data.get("solar_coefficient", DEFAULT_SOLAR_COEFFICIENT),
        )
