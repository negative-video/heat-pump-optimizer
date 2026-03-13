"""Layer 3: Reactive Watchdog — event-driven override and anomaly detection.

Listens to thermostat state changes and decides whether optimization should
pause, resume, or trigger re-optimization. Runs on HA's event loop via
state_changed callbacks (not periodic polling).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable

_LOGGER = logging.getLogger(__name__)


class WatchdogState(str, Enum):
    """Watchdog states."""

    ACTIVE = "active"
    OVERRIDE_PAUSED = "override_paused"
    MODE_CHANGE_PENDING = "mode_change_pending"


@dataclass
class OverrideEvent:
    """Record of a detected manual override."""

    timestamp: datetime
    expected_setpoint: float
    actual_setpoint: float
    hour_of_day: int
    day_of_week: int  # 0=Monday


@dataclass
class WatchdogController:
    """Detects overrides and anomalies, manages pause/resume lifecycle."""

    grace_period_hours: float = 2.0
    mode_change_hysteresis_minutes: float = 30.0

    # State
    _state: WatchdogState = WatchdogState.ACTIVE
    _override_detected_at: datetime | None = None
    _override_events: list[OverrideEvent] = field(default_factory=list)
    _last_mode: str | None = None
    _mode_change_at: datetime | None = None

    # Callbacks (set by coordinator)
    _on_override_detected: Callable | None = None
    _on_override_cleared: Callable | None = None
    _on_mode_change: Callable | None = None

    def set_callbacks(
        self,
        on_override_detected: Callable | None = None,
        on_override_cleared: Callable | None = None,
        on_mode_change: Callable | None = None,
    ) -> None:
        """Register callbacks for watchdog events."""
        self._on_override_detected = on_override_detected
        self._on_override_cleared = on_override_cleared
        self._on_mode_change = on_mode_change

    def check_override(
        self,
        last_written_setpoint: float | None,
        current_setpoint: float | None,
        tolerance: float = 0.5,
    ) -> bool:
        """Check if the thermostat setpoint was changed externally.

        Args:
            last_written_setpoint: What the optimizer last wrote.
            current_setpoint: What the thermostat currently reports.
            tolerance: °F tolerance for rounding differences.

        Returns:
            True if an override was detected.
        """
        if last_written_setpoint is None or current_setpoint is None:
            return False

        diff = abs(current_setpoint - last_written_setpoint)
        if diff <= tolerance:
            return False

        now = datetime.now(timezone.utc)

        # Already in override pause?
        if self._state == WatchdogState.OVERRIDE_PAUSED:
            return True  # Still overridden

        # New override
        _LOGGER.info(
            "Manual override detected: expected %.1f°F, got %.1f°F (diff %.1f°F)",
            last_written_setpoint, current_setpoint, diff,
        )
        self._state = WatchdogState.OVERRIDE_PAUSED
        self._override_detected_at = now

        event = OverrideEvent(
            timestamp=now,
            expected_setpoint=last_written_setpoint,
            actual_setpoint=current_setpoint,
            hour_of_day=now.hour,
            day_of_week=now.weekday(),
        )
        self._override_events.append(event)
        # Keep last 90 days of override events
        cutoff = now - timedelta(days=90)
        self._override_events = [e for e in self._override_events if e.timestamp > cutoff]

        if self._on_override_detected:
            self._on_override_detected(event)

        return True

    def check_grace_period(self) -> bool:
        """Check if the override grace period has expired.

        Returns:
            True if the grace period has expired and optimization can resume.
        """
        if self._state != WatchdogState.OVERRIDE_PAUSED:
            return False
        if self._override_detected_at is None:
            return False

        elapsed = (datetime.now(timezone.utc) - self._override_detected_at).total_seconds()
        if elapsed >= self.grace_period_hours * 3600:
            _LOGGER.info(
                "Override grace period expired after %.1f hours — resuming",
                elapsed / 3600,
            )
            self._state = WatchdogState.ACTIVE
            self._override_detected_at = None
            if self._on_override_cleared:
                self._on_override_cleared()
            return True
        return False

    def clear_override(self) -> None:
        """Manually clear the override state (e.g., thermostat resumed program)."""
        if self._state == WatchdogState.OVERRIDE_PAUSED:
            _LOGGER.info("Override manually cleared")
            self._state = WatchdogState.ACTIVE
            self._override_detected_at = None
            if self._on_override_cleared:
                self._on_override_cleared()

    def check_mode_change(self, current_mode: str) -> bool:
        """Detect HVAC mode changes with hysteresis.

        Returns True if the mode changed and enough time has passed to
        confirm it's not oscillating (hysteresis check).
        """
        if self._last_mode is None:
            self._last_mode = current_mode
            return False

        if current_mode == self._last_mode:
            self._mode_change_at = None
            return False

        now = datetime.now(timezone.utc)

        if self._mode_change_at is None:
            # First detection of change — start hysteresis timer
            self._mode_change_at = now
            _LOGGER.debug(
                "Mode change detected: %s → %s (waiting for hysteresis)",
                self._last_mode, current_mode,
            )
            return False

        # Check if enough time has passed
        elapsed_min = (now - self._mode_change_at).total_seconds() / 60
        if elapsed_min >= self.mode_change_hysteresis_minutes:
            _LOGGER.info(
                "Confirmed mode change: %s → %s (held for %.0f min)",
                self._last_mode, current_mode, elapsed_min,
            )
            self._last_mode = current_mode
            self._mode_change_at = None
            if self._on_mode_change:
                self._on_mode_change(current_mode)
            return True

        return False

    @property
    def state(self) -> WatchdogState:
        return self._state

    @property
    def is_override_active(self) -> bool:
        return self._state == WatchdogState.OVERRIDE_PAUSED

    @property
    def override_time_remaining(self) -> timedelta | None:
        """Time remaining in the override grace period."""
        if not self._override_detected_at:
            return None
        elapsed = datetime.now(timezone.utc) - self._override_detected_at
        remaining = timedelta(hours=self.grace_period_hours) - elapsed
        return max(remaining, timedelta(0))

    @property
    def override_events(self) -> list[OverrideEvent]:
        return list(self._override_events)

    def get_override_frequency(self, days: int = 30) -> dict[int, int]:
        """Count overrides by hour of day over the last N days.

        Returns: {hour_of_day: count} — useful for detecting patterns
        like "someone always overrides at 7pm".
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        recent = [e for e in self._override_events if e.timestamp > cutoff]
        frequency: dict[int, int] = {}
        for event in recent:
            frequency[event.hour_of_day] = frequency.get(event.hour_of_day, 0) + 1
        return frequency
