"""Layer 2: Tactical Controller — 5-minute reality check and correction.

Compares the model's predicted indoor temperature against actual readings.
When reality diverges from the plan, applies corrections:
  - Small drift (<1°F): adjust setpoint by the error amount
  - Moderate drift (1-2°F): flag for monitoring, apply correction
  - Large drift (>2°F): enter "disturbed" state (window open, oven, party)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from ..engine.data_types import OptimizedSchedule, ScheduleEntry, SimulationPoint

_LOGGER = logging.getLogger(__name__)

# Thresholds
SMALL_ERROR_F = 1.0
MODERATE_ERROR_F = 2.0
LARGE_ERROR_F = 3.0
DISTURBED_RECOVERY_MINUTES = 15
DISTURBED_RECOVERY_THRESHOLD_F = 2.0
CORRECTION_DAMPING = 0.5  # Apply only 50% of error as correction to avoid oscillation
MAX_CORRECTION_F = 2.0
DISTURBED_MAX_HOURS = 4.0  # Auto-recover from disturbed state after this many hours


class TacticalState(str, Enum):
    """Tactical controller state."""

    NOMINAL = "nominal"
    CORRECTING = "correcting"
    DISTURBED = "disturbed"


@dataclass
class TacticalResult:
    """Result of a tactical evaluation."""

    state: TacticalState
    setpoint_correction: float  # °F to add to scheduled setpoint (can be 0)
    predicted_temp: float | None
    actual_temp: float
    error: float  # actual - predicted (uses effective temp when apparent temp available)
    should_write_setpoint: bool
    corrected_setpoint: float | None
    reason: str
    apparent_temp: float | None = None  # humidity-adjusted feels-like temperature


@dataclass
class TacticalController:
    """Evaluates model accuracy in real-time and applies corrections."""

    # Recent prediction errors: (timestamp, error_°F)
    _error_history: list[tuple[datetime, float]] = field(default_factory=list)
    _state: TacticalState = TacticalState.NOMINAL
    _disturbed_since: datetime | None = None
    _consecutive_small_errors: int = 0
    _needs_reoptimization: bool = False

    def evaluate(
        self,
        actual_indoor_temp: float,
        schedule: OptimizedSchedule | None,
        current_entry: ScheduleEntry | None,
        now: datetime | None = None,
        apparent_temp: float | None = None,
    ) -> TacticalResult:
        """Run the 5-minute reality check.

        Args:
            actual_indoor_temp: Current indoor temp from thermostat (°F).
            schedule: The active optimized schedule (with simulation points).
            current_entry: The schedule entry for the current hour.
            now: Current time (defaults to utcnow).
            apparent_temp: Humidity-adjusted feels-like temp (°F), or None.

        Returns:
            TacticalResult with correction advice.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        predicted = self._find_predicted_temp(schedule, now)

        if predicted is None or current_entry is None:
            return TacticalResult(
                state=self._state,
                setpoint_correction=0.0,
                predicted_temp=predicted,
                actual_temp=actual_indoor_temp,
                error=0.0,
                should_write_setpoint=False,
                corrected_setpoint=None,
                reason="No prediction available",
                apparent_temp=apparent_temp,
            )

        # Use effective temp that accounts for humidity's impact on perceived comfort
        # Cooling: high humidity makes it feel warmer → use the worse (higher) value
        # Heating: dry air makes it feel cooler → use the worse (lower) value
        effective_temp = actual_indoor_temp
        if apparent_temp is not None:
            if current_entry.mode == "cool":
                effective_temp = max(actual_indoor_temp, apparent_temp)
            elif current_entry.mode == "heat":
                effective_temp = min(actual_indoor_temp, apparent_temp)

        error = effective_temp - predicted
        self._record_error(now, error)

        # Check disturbed state recovery
        if self._state == TacticalState.DISTURBED:
            result = self._handle_disturbed(
                actual_indoor_temp, predicted, error, current_entry, now
            )
            result.apparent_temp = apparent_temp
            return result

        # Classify error magnitude
        abs_error = abs(error)

        if abs_error > LARGE_ERROR_F:
            result = self._enter_disturbed(
                actual_indoor_temp, predicted, error, now
            )
            result.apparent_temp = apparent_temp
            return result

        if abs_error > SMALL_ERROR_F:
            result = self._apply_correction(
                actual_indoor_temp, predicted, error, current_entry
            )
            result.apparent_temp = apparent_temp
            return result

        # Small or no error — nominal operation
        self._consecutive_small_errors = 0
        self._state = TacticalState.NOMINAL
        return TacticalResult(
            state=TacticalState.NOMINAL,
            setpoint_correction=0.0,
            predicted_temp=predicted,
            actual_temp=actual_indoor_temp,
            error=error,
            should_write_setpoint=False,
            corrected_setpoint=None,
            reason=f"On track (error {error:+.1f}°F)",
            apparent_temp=apparent_temp,
        )

    def _apply_correction(
        self,
        actual: float,
        predicted: float,
        error: float,
        entry: ScheduleEntry,
    ) -> TacticalResult:
        """Apply a damped correction to the scheduled setpoint."""
        self._state = TacticalState.CORRECTING

        # Damped correction: move setpoint in the direction that counteracts the error
        # If actual is warmer than predicted (error > 0) during cooling, lower setpoint
        # If actual is cooler than predicted (error < 0) during heating, raise setpoint
        raw_correction = -error * CORRECTION_DAMPING
        correction = max(-MAX_CORRECTION_F, min(MAX_CORRECTION_F, raw_correction))
        corrected = entry.target_temp + correction

        return TacticalResult(
            state=TacticalState.CORRECTING,
            setpoint_correction=correction,
            predicted_temp=predicted,
            actual_temp=actual,
            error=error,
            should_write_setpoint=True,
            corrected_setpoint=corrected,
            reason=f"Correcting: error {error:+.1f}°F, adjusting setpoint by {correction:+.1f}°F",
        )

    def _enter_disturbed(
        self,
        actual: float,
        predicted: float,
        error: float,
        now: datetime,
    ) -> TacticalResult:
        """Enter disturbed state — large divergence from model."""
        self._state = TacticalState.DISTURBED
        self._disturbed_since = now
        _LOGGER.warning(
            "Entering disturbed state: actual=%.1f°F, predicted=%.1f°F, error=%+.1f°F",
            actual, predicted, error,
        )
        return TacticalResult(
            state=TacticalState.DISTURBED,
            setpoint_correction=0.0,
            predicted_temp=predicted,
            actual_temp=actual,
            error=error,
            should_write_setpoint=False,
            corrected_setpoint=None,
            reason=f"Disturbed: error {error:+.1f}°F exceeds threshold — pausing setpoint writes",
        )

    def _handle_disturbed(
        self,
        actual: float,
        predicted: float,
        error: float,
        entry: ScheduleEntry,
        now: datetime,
    ) -> TacticalResult:
        """Check if we can recover from disturbed state."""
        abs_error = abs(error)

        # Max timeout: auto-recover after DISTURBED_MAX_HOURS to prevent indefinite lock
        if self._disturbed_since is not None:
            disturbed_hours = (now - self._disturbed_since).total_seconds() / 3600
            if disturbed_hours >= DISTURBED_MAX_HOURS:
                _LOGGER.warning(
                    "Disturbed state exceeded %.0f-hour timeout (error %+.1f°F) "
                    "— forcing recovery and re-optimization",
                    DISTURBED_MAX_HOURS, error,
                )
                self._state = TacticalState.NOMINAL
                self._disturbed_since = None
                self._needs_reoptimization = True
                return TacticalResult(
                    state=TacticalState.NOMINAL,
                    setpoint_correction=0.0,
                    predicted_temp=predicted,
                    actual_temp=actual,
                    error=error,
                    should_write_setpoint=True,
                    corrected_setpoint=entry.target_temp,
                    reason=f"Forced recovery after {DISTURBED_MAX_HOURS:.0f}h timeout — resuming scheduled setpoint",
                )

        if abs_error < DISTURBED_RECOVERY_THRESHOLD_F:
            # Check if we've been below threshold long enough
            recent = [
                e for t, e in self._error_history
                if (now - t).total_seconds() < DISTURBED_RECOVERY_MINUTES * 60
            ]
            if recent and all(abs(e) < DISTURBED_RECOVERY_THRESHOLD_F for e in recent):
                _LOGGER.info("Recovering from disturbed state — errors normalized")
                self._state = TacticalState.NOMINAL
                self._disturbed_since = None
                return TacticalResult(
                    state=TacticalState.NOMINAL,
                    setpoint_correction=0.0,
                    predicted_temp=predicted,
                    actual_temp=actual,
                    error=error,
                    should_write_setpoint=True,
                    corrected_setpoint=entry.target_temp,
                    reason="Recovered from disturbed state — resuming scheduled setpoint",
                )

        return TacticalResult(
            state=TacticalState.DISTURBED,
            setpoint_correction=0.0,
            predicted_temp=predicted,
            actual_temp=actual,
            error=error,
            should_write_setpoint=False,
            corrected_setpoint=None,
            reason=f"Still disturbed: error {error:+.1f}°F (waiting for recovery)",
        )

    def _record_error(self, now: datetime, error: float) -> None:
        """Store error and trim history to 24 hours."""
        self._error_history.append((now, error))
        cutoff = now - timedelta(hours=24)
        self._error_history = [
            (t, e) for t, e in self._error_history if t > cutoff
        ]

    def _find_predicted_temp(
        self, schedule: OptimizedSchedule | None, now: datetime
    ) -> float | None:
        """Find the simulation's predicted indoor temp for the current time."""
        if not schedule or not schedule.simulation:
            return None

        closest = min(
            schedule.simulation,
            key=lambda pt: abs((pt.time - now).total_seconds()),
        )
        # Only valid if within 10 minutes
        if abs((closest.time - now).total_seconds()) > 600:
            return None
        return closest.indoor_temp

    @property
    def state(self) -> TacticalState:
        return self._state

    @property
    def error_history(self) -> list[tuple[datetime, float]]:
        return list(self._error_history)

    @property
    def mean_absolute_error(self) -> float | None:
        """Rolling MAE over recent history."""
        if not self._error_history:
            return None
        return sum(abs(e) for _, e in self._error_history) / len(self._error_history)

    @property
    def mean_signed_error(self) -> float | None:
        """Rolling mean signed error (bias detection)."""
        if not self._error_history:
            return None
        return sum(e for _, e in self._error_history) / len(self._error_history)

    def get_recent_errors(self, minutes: int = 60) -> list[float]:
        """Get errors from the last N minutes."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        return [e for t, e in self._error_history if t > cutoff]

    @property
    def needs_reoptimization(self) -> bool:
        """True if disturbed-state timeout triggered a forced recovery."""
        return self._needs_reoptimization

    def clear_reoptimization_flag(self) -> None:
        """Clear the flag after the coordinator has acted on it."""
        self._needs_reoptimization = False
