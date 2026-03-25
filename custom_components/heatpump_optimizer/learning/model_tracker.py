"""Model accuracy tracker — learns correction factors from prediction errors.

Tracks predicted vs actual indoor temperatures over time, binned by:
  - HVAC mode (cool, heat, resist/off)
  - Outdoor temperature range (5°F bins)

When the model consistently over- or under-predicts, applies correction factors
to the performance model's delta lookups. This handles:
  - Gradual system degradation (dirty filter, refrigerant leak)
  - Seasonal changes not captured in the base profile
  - Building envelope changes (new windows, insulation)

TODO: Per-mode error corrections are now largely superseded by the EKF's
online learning (thermal_estimator.py). The EKF continuously adapts all
parameters, making bin-based correction factors redundant for most users.
Consider deprecating once the EKF has proven reliable across a full
heating+cooling season.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

_LOGGER = logging.getLogger(__name__)

# Correction factor bounds
MIN_CORRECTION = 0.5
MAX_CORRECTION = 1.5
DEFAULT_CORRECTION = 1.0

# How much to adjust per weekly update (learning rate)
LEARNING_RATE = 0.1

# Minimum samples needed before applying a correction
MIN_SAMPLES_FOR_CORRECTION = 20

# Alert threshold: if correction drifts this far, warn the user
ALERT_THRESHOLD = 0.3  # 30% deviation from 1.0


@dataclass
class ErrorSample:
    """A single prediction error observation."""

    timestamp: datetime
    mode: str  # "cool", "heat", "resist"
    outdoor_temp: float
    predicted_delta: float  # °F change predicted
    actual_delta: float  # °F change observed
    error: float  # actual - predicted


@dataclass
class CorrectionBin:
    """Accumulated errors for a mode/temp bin."""

    mode: str
    temp_bin: int  # Lower bound of 5°F bin (e.g., 75 for 75-80°F)
    samples: list[ErrorSample] = field(default_factory=list)
    correction_factor: float = DEFAULT_CORRECTION

    @property
    def mean_error(self) -> float | None:
        if not self.samples:
            return None
        return sum(s.error for s in self.samples) / len(self.samples)

    @property
    def mean_absolute_error(self) -> float | None:
        if not self.samples:
            return None
        return sum(abs(s.error) for s in self.samples) / len(self.samples)

    def trim_old(self, max_age_days: int = 30) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        self.samples = [s for s in self.samples if s.timestamp > cutoff]


class ModelTracker:
    """Tracks model prediction accuracy and maintains correction factors."""

    def __init__(self, learning_rate: float = LEARNING_RATE):
        self._learning_rate = learning_rate
        # Bins keyed by (mode, temp_bin)
        self._bins: dict[tuple[str, int], CorrectionBin] = defaultdict(
            lambda: CorrectionBin(mode="", temp_bin=0)
        )
        # Global correction per mode (aggregated from bins)
        self._mode_corrections: dict[str, float] = {
            "cool": DEFAULT_CORRECTION,
            "heat": DEFAULT_CORRECTION,
            "resist": DEFAULT_CORRECTION,
        }
        self._last_update: datetime | None = None
        self._alert_triggered: dict[str, bool] = {}

    def record_observation(
        self,
        mode: str,
        outdoor_temp: float,
        predicted_delta: float,
        actual_delta: float,
        timestamp: datetime | None = None,
    ) -> None:
        """Record a prediction vs reality observation.

        Args:
            mode: "cool", "heat", or "resist"
            outdoor_temp: Outdoor temp during observation (°F)
            predicted_delta: Model's predicted °F change for the interval
            actual_delta: Observed °F change for the interval
            timestamp: When this was observed
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        error = actual_delta - predicted_delta
        sample = ErrorSample(
            timestamp=timestamp,
            mode=mode,
            outdoor_temp=outdoor_temp,
            predicted_delta=predicted_delta,
            actual_delta=actual_delta,
            error=error,
        )

        temp_bin = self._temp_to_bin(outdoor_temp)
        key = (mode, temp_bin)

        if key not in self._bins:
            self._bins[key] = CorrectionBin(mode=mode, temp_bin=temp_bin)

        self._bins[key].samples.append(sample)

    def update_corrections(self) -> dict[str, float]:
        """Recalculate correction factors from accumulated errors.

        Call this periodically (e.g., weekly or after N samples).
        Returns the updated mode corrections.
        """
        now = datetime.now(timezone.utc)

        for key, bin_data in self._bins.items():
            bin_data.trim_old(max_age_days=30)

            if len(bin_data.samples) < MIN_SAMPLES_FOR_CORRECTION:
                continue

            mean_error = bin_data.mean_error
            if mean_error is None:
                continue

            # If the model consistently over-predicts (positive delta too large),
            # mean_error will be negative → reduce the correction factor.
            # If under-predicts, mean_error will be positive → increase factor.
            #
            # We want: actual ≈ predicted * correction
            # So: correction = actual / predicted ≈ 1 + (mean_error / mean_predicted)
            mean_predicted = sum(s.predicted_delta for s in bin_data.samples) / len(bin_data.samples)
            if abs(mean_predicted) < 0.01:
                continue

            ideal_correction = (mean_predicted + mean_error) / mean_predicted
            # Smooth update: move toward ideal by learning rate
            lr = self._learning_rate
            new_correction = (
                bin_data.correction_factor * (1 - lr)
                + ideal_correction * lr
            )
            bin_data.correction_factor = max(
                MIN_CORRECTION, min(MAX_CORRECTION, new_correction)
            )

        # Aggregate per-mode corrections (weighted average across temp bins)
        for mode in ("cool", "heat", "resist"):
            mode_bins = [
                b for (m, _), b in self._bins.items()
                if m == mode and len(b.samples) >= MIN_SAMPLES_FOR_CORRECTION
            ]
            if not mode_bins:
                continue

            total_samples = sum(len(b.samples) for b in mode_bins)
            weighted_correction = sum(
                b.correction_factor * len(b.samples) for b in mode_bins
            ) / total_samples

            old = self._mode_corrections[mode]
            self._mode_corrections[mode] = weighted_correction

            if abs(weighted_correction - old) > 0.01:
                _LOGGER.info(
                    "Model correction [%s]: %.3f → %.3f (%d samples across %d bins)",
                    mode, old, weighted_correction, total_samples, len(mode_bins),
                )

            # Alert check
            deviation = abs(weighted_correction - DEFAULT_CORRECTION)
            if deviation > ALERT_THRESHOLD and not self._alert_triggered.get(mode):
                _LOGGER.warning(
                    "Model correction for %s has drifted to %.2f (%.0f%% from baseline). "
                    "Consider resetting learning to recalibrate.",
                    mode, weighted_correction, deviation * 100,
                )
                self._alert_triggered[mode] = True
            elif deviation <= ALERT_THRESHOLD * 0.8:
                self._alert_triggered[mode] = False

        self._last_update = now
        return dict(self._mode_corrections)

    def get_correction(self, mode: str) -> float:
        """Get the current correction factor for a mode."""
        return self._mode_corrections.get(mode, DEFAULT_CORRECTION)

    def get_bin_correction(self, mode: str, outdoor_temp: float) -> float:
        """Get temp-specific correction factor (more granular than mode-level)."""
        key = (mode, self._temp_to_bin(outdoor_temp))
        if key in self._bins and len(self._bins[key].samples) >= MIN_SAMPLES_FOR_CORRECTION:
            return self._bins[key].correction_factor
        return self._mode_corrections.get(mode, DEFAULT_CORRECTION)

    def get_accuracy_report(self) -> dict:
        """Generate accuracy stats for sensor entities."""
        report: dict[str, dict] = {}
        for mode in ("cool", "heat", "resist"):
            mode_bins = [
                b for (m, _), b in self._bins.items() if m == mode
            ]
            all_samples = [s for b in mode_bins for s in b.samples]
            if not all_samples:
                report[mode] = {
                    "samples": 0,
                    "mae": None,
                    "bias": None,
                    "correction": self._mode_corrections.get(mode, 1.0),
                    "alert": False,
                }
                continue

            mae = sum(abs(s.error) for s in all_samples) / len(all_samples)
            bias = sum(s.error for s in all_samples) / len(all_samples)
            report[mode] = {
                "samples": len(all_samples),
                "mae": round(mae, 3),
                "bias": round(bias, 3),
                "correction": round(self._mode_corrections[mode], 3),
                "alert": self._alert_triggered.get(mode, False),
            }

        return report

    @staticmethod
    def _temp_to_bin(outdoor_temp: float) -> int:
        """Round outdoor temp down to nearest 5°F bin."""
        return int(outdoor_temp // 5) * 5

    def to_dict(self) -> dict:
        """Serialize for persistence."""
        return {
            "mode_corrections": dict(self._mode_corrections),
            "alert_triggered": dict(self._alert_triggered),
            "last_update": (
                self._last_update.isoformat() if self._last_update else None
            ),
            # Don't persist individual samples — they rebuild from live observation
        }

    @classmethod
    def from_dict(cls, data: dict) -> ModelTracker:
        """Restore from persisted data."""
        tracker = cls()
        if "mode_corrections" in data:
            for mode, val in data["mode_corrections"].items():
                tracker._mode_corrections[mode] = max(
                    MIN_CORRECTION, min(MAX_CORRECTION, val)
                )
        if "alert_triggered" in data:
            tracker._alert_triggered = dict(data["alert_triggered"])
        if data.get("last_update"):
            try:
                tracker._last_update = datetime.fromisoformat(data["last_update"])
            except (ValueError, TypeError):
                pass
        return tracker
