"""Daily coefficient calibrator — slow outer loop for the EKF.

Analyzes conditioned innovations (prediction errors tagged with environmental
context) to identify which hardcoded physics coefficients are biased for the
specific home, and applies bounded multiplicative corrections to the
CoefficientStore.

Architecture:
  Fast EKF (5-min) → conditioned innovations → Slow Calibrator (daily)
                                                   ↓
                                           CoefficientStore multipliers
                                                   ↓
                                           Fast EKF reads calibrated values

The calibrator never touches the EKF's internal state vector or covariance.
It adjusts the *environment* the EKF operates in, like weather data does.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import numpy as np

from .coefficient_store import ALL_COEFFICIENTS, TIER_1, CoefficientStore
from .sensitivity import COEFFICIENT_NAMES, compute_sensitivities

_LOGGER = logging.getLogger(__name__)

# ── Calibrator configuration ──────────────────────────────────────

# How often to run calibration
CALIBRATION_INTERVAL_HOURS = 24

# Minimum conditioned innovations before first calibration (~17 hours)
MIN_SAMPLES = 200

# Don't calibrate until this many hours of data are accumulated
MIN_HOURS_BEFORE_START = 72

# Don't run natural experiment detection until this many days
MIN_DAYS_FOR_EXPERIMENTS = 7

# Ridge regression regularization (Tikhonov parameter).
# Higher = more conservative (bias toward no change).
REGULARIZATION_LAMBDA = 1.0

# Multiplicative update rate: move 10% toward the regression target per day.
LEARNING_RATE = 0.1

# Maximum per-day change in any multiplier.
MAX_STEP_PER_DAY = 0.20

# EMA smoothing for multiplier updates.
EMA_BETA = 0.8

# If MAE increases by more than this fraction after calibration, revert.
MAE_REVERT_THRESHOLD = 0.25

# ── Natural experiment detection thresholds ───────────────────────

# Pure envelope: no solar, no occupancy, no wind, no HVAC
_PURE_ENV_MAX_WIND_MPH = 3.0
_PURE_ENV_MIN_DURATION_SAMPLES = 24  # 2 hours at 5-min intervals

# Wind isolation: strong wind vs. pure envelope baseline
_WIND_ISO_MIN_WIND_MPH = 8.0


class CoefficientCalibrator:
    """Slow outer-loop calibrator for thermal model coefficients.

    Call ``should_calibrate(now)`` every EKF cycle.  When it returns True,
    call ``calibrate(innovations)`` with the conditioned innovation buffer.
    """

    def __init__(self, coeff_store: CoefficientStore):
        self._store = coeff_store
        self._last_calibration: datetime | None = None
        self._first_innovation_time: datetime | None = None
        self._calibration_history: list[dict] = []
        self._pre_calibration_mae: float | None = None
        # Proposed multipliers from last dry-run (for diagnostics)
        self._proposed_multipliers: dict[str, float] = {}
        self._last_experiments: list[dict] = []

    def should_calibrate(self, now: datetime) -> bool:
        """Check whether it's time to run calibration."""
        if self._last_calibration is None:
            # Never calibrated — check cold-start holdoff
            if self._first_innovation_time is None:
                return False
            hours_since_start = (now - self._first_innovation_time).total_seconds() / 3600
            return hours_since_start >= MIN_HOURS_BEFORE_START
        hours_since_last = (now - self._last_calibration).total_seconds() / 3600
        return hours_since_last >= CALIBRATION_INTERVAL_HOURS

    def calibrate(
        self,
        innovations: list[dict],
        *,
        dry_run: bool = True,
    ) -> dict:
        """Run one calibration cycle.

        Args:
            innovations: Conditioned innovation records from the EKF
                (``ThermalEstimator.get_conditioned_innovations()``).
            dry_run: If True, compute and log proposed adjustments but don't
                apply them to the CoefficientStore.

        Returns:
            Diagnostic dict with keys: adjustments, experiments_detected,
            samples_used, regression_residual, dry_run.
        """
        now = datetime.now(timezone.utc)

        # Track first innovation time for cold-start holdoff
        if self._first_innovation_time is None and innovations:
            ts = innovations[0].get("timestamp", "")
            if ts:
                self._first_innovation_time = datetime.fromisoformat(ts)

        if len(innovations) < MIN_SAMPLES:
            _LOGGER.debug(
                "Calibrator: insufficient samples (%d < %d), skipping",
                len(innovations), MIN_SAMPLES,
            )
            return {"skipped": True, "reason": "insufficient_samples",
                    "samples": len(innovations)}

        # Build sensitivity matrix J (N × K) and innovation vector e (N,)
        n = len(innovations)
        k = len(COEFFICIENT_NAMES)
        J = np.zeros((n, k))
        e = np.zeros(n)

        for i, rec in enumerate(innovations):
            e[i] = rec.get("innovation", 0.0)
            sens = compute_sensitivities(rec)
            for j, name in enumerate(COEFFICIENT_NAMES):
                J[i, j] = sens.get(name, 0.0)

        # Ridge regression: δ = (JᵀJ + λI)⁻¹ Jᵀe
        JtJ = J.T @ J
        Jte = J.T @ e
        reg = REGULARIZATION_LAMBDA * np.eye(k)
        try:
            delta = np.linalg.solve(JtJ + reg, Jte)
        except np.linalg.LinAlgError:
            _LOGGER.warning("Calibrator: regression failed (singular matrix)")
            return {"skipped": True, "reason": "singular_matrix"}

        # Regression residual (diagnostic)
        residual = float(np.sqrt(np.mean((e - J @ delta) ** 2)))

        # Compute proposed multiplier updates
        adjustments: dict[str, dict] = {}
        for j, name in enumerate(COEFFICIENT_NAMES):
            current_mult = self._store.get_multiplier(name)
            # Sensitivity magnitude determines confidence
            col_norm = float(np.linalg.norm(J[:, j]))
            if col_norm < 1e-6:
                # No sensitivity for this coefficient — skip
                adjustments[name] = {
                    "current": current_mult,
                    "proposed": current_mult,
                    "delta": 0.0,
                    "confidence": 0.0,
                    "reason": "no_sensitivity",
                }
                continue

            # Fractional correction: delta[j] is in coefficient units,
            # but we need it as a fraction of the coefficient's scale.
            # Since J already has the coefficient's scale baked in, delta[j]
            # represents the absolute correction. The multiplier change is:
            raw_step = float(delta[j])
            clamped_step = max(-MAX_STEP_PER_DAY, min(MAX_STEP_PER_DAY, raw_step))

            # EMA smoothing + learning rate
            proposed = current_mult + LEARNING_RATE * clamped_step
            proposed = EMA_BETA * current_mult + (1 - EMA_BETA) * proposed

            # Confidence: higher column norm → more signal → higher confidence
            # Normalize by number of samples
            confidence = min(1.0, col_norm / (n * 0.01))

            adjustments[name] = {
                "current": round(current_mult, 4),
                "proposed": round(proposed, 4),
                "delta": round(proposed - current_mult, 6),
                "confidence": round(confidence, 3),
                "reason": "regression",
            }
            self._proposed_multipliers[name] = proposed

        # Detect natural experiments (supplementary)
        experiments = self._detect_natural_experiments(innovations)
        self._last_experiments = experiments

        # Apply updates (unless dry-run)
        applied = False
        if not dry_run:
            for name, adj in adjustments.items():
                if adj.get("reason") == "no_sensitivity":
                    continue
                self._store.set_multiplier(name, adj["proposed"])
                self._store.set_confidence(name, adj["confidence"])
            self._store.last_calibration = now
            self._store.calibration_count += 1
            applied = True
            _LOGGER.info(
                "Calibrator: applied coefficient updates (cycle %d, %d samples, "
                "residual=%.3f°F)",
                self._store.calibration_count, n, residual,
            )
        else:
            _LOGGER.info(
                "Calibrator: dry-run — proposed adjustments logged but not applied "
                "(%d samples, residual=%.3f°F)",
                n, residual,
            )

        self._last_calibration = now
        result = {
            "dry_run": dry_run,
            "applied": applied,
            "samples_used": n,
            "regression_residual": round(residual, 4),
            "adjustments": adjustments,
            "experiments_detected": len(experiments),
            "experiment_types": [ex["type"] for ex in experiments],
            "timestamp": now.isoformat(),
        }
        self._calibration_history.append(result)
        # Keep only last 30 calibration records
        if len(self._calibration_history) > 30:
            self._calibration_history = self._calibration_history[-30:]

        return result

    def _detect_natural_experiments(
        self, innovations: list[dict],
    ) -> list[dict]:
        """Scan for natural experiment windows in the innovation buffer.

        Returns a list of detected experiments, each with type, start/end
        timestamps, sample count, and mean innovation during the window.
        """
        if self._first_innovation_time is None:
            return []

        now = datetime.now(timezone.utc)
        hours_of_data = (now - self._first_innovation_time).total_seconds() / 3600
        if hours_of_data < MIN_DAYS_FOR_EXPERIMENTS * 24:
            return []

        experiments: list[dict] = []

        # ── Pure envelope detection ───────────────────────────────
        # Night, HVAC off, 0 occupants, low wind, no rain
        pure_env_window: list[dict] = []
        for rec in innovations:
            sun_elev = rec.get("sun_elevation")
            is_night = sun_elev is not None and sun_elev < -6
            hvac_off = not rec.get("hvac_running", False)
            no_people = rec.get("people_count") == 0
            low_wind = (
                rec.get("wind_speed_mph") is not None
                and rec.get("wind_speed_mph", 99) < _PURE_ENV_MAX_WIND_MPH
            )
            no_rain = not rec.get("precipitation", False)
            no_doors = rec.get("doors_windows_open", 0) == 0

            if is_night and hvac_off and no_people and low_wind and no_rain and no_doors:
                pure_env_window.append(rec)
            else:
                if len(pure_env_window) >= _PURE_ENV_MIN_DURATION_SAMPLES:
                    inns = [r.get("innovation", 0.0) for r in pure_env_window]
                    experiments.append({
                        "type": "pure_envelope",
                        "samples": len(pure_env_window),
                        "mean_innovation": round(sum(inns) / len(inns), 4),
                        "start": pure_env_window[0].get("timestamp", ""),
                        "end": pure_env_window[-1].get("timestamp", ""),
                    })
                pure_env_window = []

        # Flush remaining window
        if len(pure_env_window) >= _PURE_ENV_MIN_DURATION_SAMPLES:
            inns = [r.get("innovation", 0.0) for r in pure_env_window]
            experiments.append({
                "type": "pure_envelope",
                "samples": len(pure_env_window),
                "mean_innovation": round(sum(inns) / len(inns), 4),
                "start": pure_env_window[0].get("timestamp", ""),
                "end": pure_env_window[-1].get("timestamp", ""),
            })

        # ── Wind isolation detection ──────────────────────────────
        # Night, HVAC off, high wind — compare to pure envelope baseline
        windy_window: list[dict] = []
        for rec in innovations:
            sun_elev = rec.get("sun_elevation")
            is_night = sun_elev is not None and sun_elev < -6
            hvac_off = not rec.get("hvac_running", False)
            high_wind = (
                rec.get("wind_speed_mph") is not None
                and rec.get("wind_speed_mph", 0) >= _WIND_ISO_MIN_WIND_MPH
            )
            no_doors = rec.get("doors_windows_open", 0) == 0

            if is_night and hvac_off and high_wind and no_doors:
                windy_window.append(rec)
            else:
                if len(windy_window) >= 6:  # 30 minutes minimum
                    inns = [r.get("innovation", 0.0) for r in windy_window]
                    experiments.append({
                        "type": "wind_isolation",
                        "samples": len(windy_window),
                        "mean_innovation": round(sum(inns) / len(inns), 4),
                        "mean_wind_mph": round(
                            sum(r.get("wind_speed_mph", 0) for r in windy_window)
                            / len(windy_window), 1
                        ),
                        "start": windy_window[0].get("timestamp", ""),
                        "end": windy_window[-1].get("timestamp", ""),
                    })
                windy_window = []

        if len(windy_window) >= 6:
            inns = [r.get("innovation", 0.0) for r in windy_window]
            experiments.append({
                "type": "wind_isolation",
                "samples": len(windy_window),
                "mean_innovation": round(sum(inns) / len(inns), 4),
                "mean_wind_mph": round(
                    sum(r.get("wind_speed_mph", 0) for r in windy_window)
                    / len(windy_window), 1
                ),
                "start": windy_window[0].get("timestamp", ""),
                "end": windy_window[-1].get("timestamp", ""),
            })

        return experiments

    @property
    def proposed_multipliers(self) -> dict[str, float]:
        """Last proposed multipliers (from most recent calibration, live or dry-run)."""
        return dict(self._proposed_multipliers)

    @property
    def last_experiments(self) -> list[dict]:
        """Natural experiments detected in the most recent calibration."""
        return list(self._last_experiments)

    @property
    def calibration_history(self) -> list[dict]:
        """Last N calibration result dicts."""
        return list(self._calibration_history)

    def to_dict(self) -> dict:
        """Serialize calibrator state for persistence."""
        return {
            "last_calibration": (
                self._last_calibration.isoformat()
                if self._last_calibration else None
            ),
            "first_innovation_time": (
                self._first_innovation_time.isoformat()
                if self._first_innovation_time else None
            ),
            "proposed_multipliers": dict(self._proposed_multipliers),
            "calibration_history": self._calibration_history[-10:],
        }

    @classmethod
    def from_dict(
        cls, data: dict, coeff_store: CoefficientStore,
    ) -> CoefficientCalibrator:
        """Restore calibrator from persisted dict."""
        cal = cls(coeff_store)
        last = data.get("last_calibration")
        if last:
            cal._last_calibration = datetime.fromisoformat(last)
        first = data.get("first_innovation_time")
        if first:
            cal._first_innovation_time = datetime.fromisoformat(first)
        cal._proposed_multipliers = data.get("proposed_multipliers", {})
        cal._calibration_history = data.get("calibration_history", [])
        return cal
