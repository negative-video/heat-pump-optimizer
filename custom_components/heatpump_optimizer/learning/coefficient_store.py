"""Mutable store for calibratable thermal model coefficients.

Each coefficient is stored as a multiplier on the original hardcoded default.
The effective value consumed by the EKF and optimizer is:

    effective = default_value * multiplier

Multipliers are bounded to [MIN_MULTIPLIER, MAX_MULTIPLIER] and default to 1.0
(i.e. no change from the original constant).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

_LOGGER = logging.getLogger(__name__)

# Hard bounds on any multiplier — a coefficient can't go below 20% or above
# 500% of its textbook default.  This prevents the calibrator from producing
# physically nonsensical values even with bad data.
MIN_MULTIPLIER = 0.2
MAX_MULTIPLIER = 5.0

# Names of all calibratable coefficients, grouped by tier.
TIER_1 = (
    "wind_infiltration",
    "k_attic",
    "k_crawlspace",
    "internal_gain_base",
    "alpha_cool",
    "alpha_heat",
)
TIER_2 = (
    "stack_effect",
    "internal_gain_per_person",
    "precipitation_offset",
    "solar_mass_fraction",
)
ALL_COEFFICIENTS = TIER_1 + TIER_2


@dataclass
class CoefficientStore:
    """Per-home correction multipliers for thermal model coefficients.

    Usage in the EKF / optimizer::

        wind_coeff = store.effective("wind_infiltration", _WIND_INFILTRATION_COEFF)
    """

    # Multiplier for each calibratable coefficient (default 1.0 = no change)
    _multipliers: dict[str, float] = field(default_factory=lambda: {
        name: 1.0 for name in ALL_COEFFICIENTS
    })

    # Per-coefficient confidence from the last calibration (0.0–1.0)
    _confidence: dict[str, float] = field(default_factory=lambda: {
        name: 0.0 for name in ALL_COEFFICIENTS
    })

    # Metadata
    last_calibration: datetime | None = None
    calibration_count: int = 0

    def effective(self, name: str, default: float) -> float:
        """Return the calibrated value: ``default * multiplier``."""
        return default * self._multipliers.get(name, 1.0)

    def get_multiplier(self, name: str) -> float:
        """Return the raw multiplier for a coefficient."""
        return self._multipliers.get(name, 1.0)

    def set_multiplier(self, name: str, value: float) -> None:
        """Set a multiplier, clamping to [MIN_MULTIPLIER, MAX_MULTIPLIER]."""
        clamped = max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, value))
        if clamped != value:
            _LOGGER.debug(
                "Coefficient %s multiplier clamped: %.4f → %.4f",
                name, value, clamped,
            )
        self._multipliers[name] = clamped

    def get_confidence(self, name: str) -> float:
        """Return calibration confidence for a coefficient (0.0–1.0)."""
        return self._confidence.get(name, 0.0)

    def set_confidence(self, name: str, value: float) -> None:
        """Set calibration confidence, clamped to [0, 1]."""
        self._confidence[name] = max(0.0, min(1.0, value))

    @property
    def multipliers(self) -> dict[str, float]:
        """Read-only copy of all multipliers."""
        return dict(self._multipliers)

    @property
    def all_confidence(self) -> dict[str, float]:
        """Read-only copy of all confidence values."""
        return dict(self._confidence)

    def to_dict(self) -> dict:
        """Serialize for HA persistent storage."""
        return {
            "multipliers": dict(self._multipliers),
            "confidence": dict(self._confidence),
            "last_calibration": (
                self.last_calibration.isoformat() if self.last_calibration else None
            ),
            "calibration_count": self.calibration_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> CoefficientStore:
        """Restore from persisted dict, tolerant of missing/extra keys."""
        store = cls()

        raw_mult = data.get("multipliers", {})
        for name in ALL_COEFFICIENTS:
            if name in raw_mult:
                store.set_multiplier(name, float(raw_mult[name]))

        raw_conf = data.get("confidence", {})
        for name in ALL_COEFFICIENTS:
            if name in raw_conf:
                store.set_confidence(name, float(raw_conf[name]))

        last_cal = data.get("last_calibration")
        if last_cal:
            store.last_calibration = datetime.fromisoformat(last_cal)
        store.calibration_count = int(data.get("calibration_count", 0))

        return store
