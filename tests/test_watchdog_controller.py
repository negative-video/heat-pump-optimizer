"""Tests for Layer 3: WatchdogController -- override detection and mode change hysteresis."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.conftest import load_module, CC

# Load the watchdog module under test
watchdog_mod = load_module(
    "custom_components.heatpump_optimizer.controllers.watchdog",
    os.path.join(CC, "controllers", "watchdog.py"),
)
WatchdogController = watchdog_mod.WatchdogController
WatchdogState = watchdog_mod.WatchdogState
OverrideEvent = watchdog_mod.OverrideEvent


# ── Override detection ─────────────────────────────────────────────


class TestOverrideDetection:
    def test_no_override_when_setpoints_match(self):
        wd = WatchdogController()
        assert wd.check_override(70.0, 70.0) is False
        assert wd.state == WatchdogState.ACTIVE

    def test_no_override_within_tolerance(self):
        wd = WatchdogController()
        assert wd.check_override(70.0, 70.3) is False
        assert wd.state == WatchdogState.ACTIVE

    def test_override_at_tolerance_boundary(self):
        """Diff of exactly 0.5 is within tolerance (<=)."""
        wd = WatchdogController()
        assert wd.check_override(70.0, 70.5) is False

    def test_override_detected(self):
        wd = WatchdogController()
        result = wd.check_override(70.0, 73.0)
        assert result is True
        assert wd.state == WatchdogState.OVERRIDE_PAUSED
        assert len(wd.override_events) == 1

    def test_override_event_fields(self):
        wd = WatchdogController()
        wd.check_override(70.0, 73.0)
        event = wd.override_events[0]
        assert event.expected_setpoint == 70.0
        assert event.actual_setpoint == 73.0
        assert isinstance(event.timestamp, datetime)

    def test_already_paused_returns_true(self):
        """Second check while paused returns True without new event."""
        wd = WatchdogController()
        wd.check_override(70.0, 73.0)
        assert len(wd.override_events) == 1
        result = wd.check_override(70.0, 73.0)
        assert result is True
        assert len(wd.override_events) == 1  # No new event

    def test_none_expected_setpoint(self):
        wd = WatchdogController()
        assert wd.check_override(None, 70.0) is False

    def test_none_current_setpoint(self):
        wd = WatchdogController()
        assert wd.check_override(70.0, None) is False

    def test_both_none(self):
        wd = WatchdogController()
        assert wd.check_override(None, None) is False

    def test_custom_tolerance(self):
        wd = WatchdogController()
        assert wd.check_override(70.0, 71.0, tolerance=1.0) is False
        assert wd.check_override(70.0, 71.5, tolerance=1.0) is True


# ── Callbacks ──────────────────────────────────────────────────────


class TestCallbacks:
    def test_override_detected_callback(self):
        callback = MagicMock()
        wd = WatchdogController()
        wd.set_callbacks(on_override_detected=callback)
        wd.check_override(70.0, 73.0)
        callback.assert_called_once()
        event = callback.call_args[0][0]
        assert isinstance(event, OverrideEvent)

    def test_override_cleared_callback(self):
        callback = MagicMock()
        wd = WatchdogController()
        wd.set_callbacks(on_override_cleared=callback)
        wd.check_override(70.0, 73.0)
        wd.clear_override()
        callback.assert_called_once()

    def test_mode_change_callback(self):
        callback = MagicMock()
        wd = WatchdogController(mode_change_hysteresis_minutes=0)
        wd.set_callbacks(on_mode_change=callback)
        wd.check_mode_change("cool")  # First call sets mode
        wd.check_mode_change("heat")  # Detects change, starts timer
        wd.check_mode_change("heat")  # Timer already at 0 min, confirms
        callback.assert_called_once_with("heat")


# ── Grace period ───────────────────────────────────────────────────


class TestGracePeriod:
    def test_grace_period_not_expired(self):
        wd = WatchdogController(grace_period_hours=2.0)
        wd.check_override(70.0, 73.0)
        # Immediately after: not expired yet
        assert wd.check_grace_period() is False
        assert wd.state == WatchdogState.OVERRIDE_PAUSED

    def test_grace_period_not_applicable_when_active(self):
        """check_grace_period returns False when not in override."""
        wd = WatchdogController()
        assert wd.check_grace_period() is False

    def test_custom_grace_period(self):
        """Short grace period for testing."""
        wd = WatchdogController(grace_period_hours=0.0)
        wd.check_override(70.0, 73.0)
        # With 0-hour grace period, should expire immediately
        assert wd.check_grace_period() is True
        assert wd.state == WatchdogState.ACTIVE


# ── Clear override ─────────────────────────────────────────────────


class TestClearOverride:
    def test_clear_transitions_to_active(self):
        wd = WatchdogController()
        wd.check_override(70.0, 73.0)
        assert wd.state == WatchdogState.OVERRIDE_PAUSED
        wd.clear_override()
        assert wd.state == WatchdogState.ACTIVE
        assert wd.is_override_active is False

    def test_clear_when_not_overridden_is_noop(self):
        wd = WatchdogController()
        wd.clear_override()  # Should not raise
        assert wd.state == WatchdogState.ACTIVE

    def test_clear_fires_callback(self):
        callback = MagicMock()
        wd = WatchdogController()
        wd.set_callbacks(on_override_cleared=callback)
        wd.check_override(70.0, 73.0)
        wd.clear_override()
        callback.assert_called_once()

    def test_clear_noop_does_not_fire_callback(self):
        callback = MagicMock()
        wd = WatchdogController()
        wd.set_callbacks(on_override_cleared=callback)
        wd.clear_override()
        callback.assert_not_called()


# ── Mode change detection ──────────────────────────────────────────


class TestModeChange:
    def test_first_mode_sets_baseline(self):
        """First call just records the mode."""
        wd = WatchdogController()
        assert wd.check_mode_change("cool") is False

    def test_same_mode_no_change(self):
        wd = WatchdogController()
        wd.check_mode_change("cool")
        assert wd.check_mode_change("cool") is False

    def test_mode_change_starts_hysteresis(self):
        """Detecting a change returns False initially (hysteresis)."""
        wd = WatchdogController(mode_change_hysteresis_minutes=30)
        wd.check_mode_change("cool")
        assert wd.check_mode_change("heat") is False

    def test_mode_change_confirmed_after_hysteresis(self):
        """With 0-minute hysteresis, second detection confirms."""
        wd = WatchdogController(mode_change_hysteresis_minutes=0)
        wd.check_mode_change("cool")
        wd.check_mode_change("heat")  # Starts timer
        assert wd.check_mode_change("heat") is True  # Confirms (0 min elapsed)

    def test_mode_oscillation_resets(self):
        """Mode flips back before hysteresis: timer resets."""
        wd = WatchdogController(mode_change_hysteresis_minutes=30)
        wd.check_mode_change("cool")
        wd.check_mode_change("heat")  # Start timer
        wd.check_mode_change("cool")  # Flip back -- timer resets
        # Should not detect a confirmed change
        assert wd.check_mode_change("cool") is False


# ── Override event history ─────────────────────────────────────────


class TestOverrideHistory:
    def test_events_recorded(self):
        wd = WatchdogController()
        wd.check_override(70.0, 73.0)
        assert len(wd.override_events) == 1

    def test_event_trimming_90_days(self):
        """Events older than 90 days are dropped."""
        wd = WatchdogController()
        # Manually add an old event
        old_event = OverrideEvent(
            timestamp=datetime.now(timezone.utc) - timedelta(days=91),
            expected_setpoint=70.0,
            actual_setpoint=73.0,
            hour_of_day=14,
            day_of_week=0,
        )
        wd._override_events.append(old_event)
        # New override triggers trimming
        wd.check_override(70.0, 74.0)
        assert len(wd.override_events) == 1  # Old event trimmed


# ── Properties ─────────────────────────────────────────────────────


class TestProperties:
    def test_is_override_active(self):
        wd = WatchdogController()
        assert wd.is_override_active is False
        wd.check_override(70.0, 73.0)
        assert wd.is_override_active is True

    def test_override_time_remaining_none_when_active(self):
        wd = WatchdogController()
        assert wd.override_time_remaining is None

    def test_override_time_remaining_when_paused(self):
        wd = WatchdogController(grace_period_hours=2.0)
        wd.check_override(70.0, 73.0)
        remaining = wd.override_time_remaining
        assert remaining is not None
        # Should be close to 2 hours
        assert remaining.total_seconds() > 7100  # ~2hrs minus epsilon

    def test_override_frequency(self):
        wd = WatchdogController()
        # Add events at different hours
        for hour in [19, 19, 19, 7]:
            event = OverrideEvent(
                timestamp=datetime.now(timezone.utc),
                expected_setpoint=70.0,
                actual_setpoint=73.0,
                hour_of_day=hour,
                day_of_week=0,
            )
            wd._override_events.append(event)

        freq = wd.get_override_frequency(days=30)
        assert freq[19] == 3
        assert freq[7] == 1

    def test_override_frequency_empty(self):
        wd = WatchdogController()
        assert wd.get_override_frequency() == {}
