"""Tests for AuxHeatLearner -- aux/emergency heat activation pattern learner.

Covers:
- Default state after construction
- record_interval with aux activation (rising edge)
- record_interval during normal heating (hp watts learning)
- Non-heating modes ignored for hp watts learning
- Threshold learning readiness (is_learned after MIN_EVENTS)
- HP watts learning readiness (hp_watts_learned after MIN_HP_SAMPLES)
- Event cap (50 max stored events)
- to_dict/from_dict roundtrip persistence
"""

from __future__ import annotations

import os
import sys

import pytest

from conftest import CC, load_module

# Load the module under test
aux_mod = load_module(
    "custom_components.heatpump_optimizer.learning.aux_heat_learner",
    os.path.join(CC, "learning", "aux_heat_learner.py"),
)

AuxHeatLearner = aux_mod.AuxHeatLearner
AuxHeatEvent = aux_mod.AuxHeatEvent
_DEFAULT_THRESHOLD_F = aux_mod._DEFAULT_THRESHOLD_F
_MIN_EVENTS = aux_mod._MIN_EVENTS
_MIN_HP_SAMPLES = aux_mod._MIN_HP_SAMPLES
_MAX_EVENTS = aux_mod._MAX_EVENTS
_EMA_ALPHA = aux_mod._EMA_ALPHA
_EMA_HP_ALPHA = aux_mod._EMA_HP_ALPHA


# ── Helpers ───────────────────────────────────────────────────────────


def _record_normal_heating(learner, power_watts=3000.0, dt_minutes=5.0):
    """Simulate one interval of normal (non-aux) heating."""
    learner.record_interval(
        aux_heat_active=False,
        outdoor_temp_f=40.0,
        effective_outdoor_temp_f=38.0,
        outdoor_humidity=50.0,
        setpoint_delta_f=2.0,
        dt_minutes=dt_minutes,
        hvac_running=True,
        hvac_mode="heat",
        power_watts=power_watts,
    )


def _record_aux_activation(learner, effective_outdoor_temp_f=15.0,
                           outdoor_temp_f=20.0, outdoor_humidity=80.0,
                           setpoint_delta_f=4.0, dt_minutes=5.0):
    """Simulate an interval where aux heat just activated (rising edge)."""
    learner.record_interval(
        aux_heat_active=True,
        outdoor_temp_f=outdoor_temp_f,
        effective_outdoor_temp_f=effective_outdoor_temp_f,
        outdoor_humidity=outdoor_humidity,
        setpoint_delta_f=setpoint_delta_f,
        dt_minutes=dt_minutes,
        hvac_running=True,
        hvac_mode="heat",
        power_watts=8000.0,
    )


def _record_idle(learner, dt_minutes=5.0):
    """Simulate an idle (HVAC off) interval."""
    learner.record_interval(
        aux_heat_active=False,
        outdoor_temp_f=50.0,
        effective_outdoor_temp_f=48.0,
        outdoor_humidity=40.0,
        setpoint_delta_f=0.0,
        dt_minutes=dt_minutes,
        hvac_running=False,
        hvac_mode="off",
        power_watts=None,
    )


# ── Tests ─────────────────────────────────────────────────────────────


class TestDefaultState:
    """Verify freshly-constructed learner state."""

    def test_default_threshold(self):
        learner = AuxHeatLearner()
        assert learner.threshold_f == _DEFAULT_THRESHOLD_F

    def test_default_hp_watts(self):
        learner = AuxHeatLearner(default_hp_watts=4000.0)
        assert learner.learned_hp_watts == 4000.0

    def test_default_hp_watts_standard(self):
        learner = AuxHeatLearner()
        assert learner.learned_hp_watts == 3500.0

    def test_not_learned_initially(self):
        learner = AuxHeatLearner()
        assert learner.is_learned is False

    def test_hp_watts_not_learned_initially(self):
        learner = AuxHeatLearner()
        assert learner.hp_watts_learned is False

    def test_no_events_initially(self):
        learner = AuxHeatLearner()
        assert learner.event_count == 0

    def test_last_event_none_initially(self):
        learner = AuxHeatLearner()
        assert learner.last_event is None


class TestRecordIntervalAuxActivation:
    """Aux heat rising-edge detection and event recording."""

    def test_first_aux_activation_records_event(self):
        learner = AuxHeatLearner()
        _record_aux_activation(learner, effective_outdoor_temp_f=18.0)
        assert learner.event_count == 1

    def test_first_aux_activation_sets_threshold_to_observation(self):
        learner = AuxHeatLearner()
        _record_aux_activation(learner, effective_outdoor_temp_f=18.0)
        # First event seeds threshold directly
        assert learner.threshold_f == 18.0

    def test_second_aux_activation_ema_updates_threshold(self):
        learner = AuxHeatLearner()
        _record_aux_activation(learner, effective_outdoor_temp_f=20.0)
        # Reset aux state so next call is a rising edge
        _record_idle(learner)
        _record_aux_activation(learner, effective_outdoor_temp_f=10.0)
        # EMA: 0.2 * 10 + 0.8 * 20 = 18
        assert learner.threshold_f == pytest.approx(18.0, abs=0.01)

    def test_continued_aux_does_not_double_count(self):
        """If aux stays on across two intervals, only the first counts."""
        learner = AuxHeatLearner()
        _record_aux_activation(learner, effective_outdoor_temp_f=15.0)
        # Second interval with aux still active -- not a rising edge
        learner.record_interval(
            aux_heat_active=True,
            outdoor_temp_f=20.0,
            effective_outdoor_temp_f=14.0,
            outdoor_humidity=80.0,
            setpoint_delta_f=4.0,
            dt_minutes=5.0,
            hvac_running=True,
            hvac_mode="heat",
            power_watts=8000.0,
        )
        assert learner.event_count == 1

    def test_event_captures_outdoor_conditions(self):
        learner = AuxHeatLearner()
        _record_aux_activation(
            learner,
            outdoor_temp_f=22.0,
            effective_outdoor_temp_f=16.0,
            outdoor_humidity=85.0,
            setpoint_delta_f=3.5,
        )
        ev = learner.last_event
        assert ev is not None
        assert ev.outdoor_temp_f == 22.0
        assert ev.effective_outdoor_temp_f == 16.0
        assert ev.outdoor_humidity == 85.0
        assert ev.setpoint_delta_f == 3.5

    def test_hp_runtime_tracked_before_aux(self):
        learner = AuxHeatLearner()
        # Normal heating for 3 intervals (15 min)
        for _ in range(3):
            _record_normal_heating(learner, dt_minutes=5.0)
        # Now aux kicks in
        _record_aux_activation(learner, dt_minutes=5.0)
        ev = learner.last_event
        assert ev.hp_runtime_before_min == pytest.approx(15.0)

    def test_runtime_resets_after_aux_activation(self):
        learner = AuxHeatLearner()
        _record_normal_heating(learner, dt_minutes=10.0)
        _record_aux_activation(learner)
        # After aux, idle resets, then start fresh
        _record_idle(learner)
        _record_normal_heating(learner, dt_minutes=7.0)
        _record_aux_activation(learner)
        ev = learner.last_event
        assert ev.hp_runtime_before_min == pytest.approx(7.0)


class TestRecordIntervalNormalHeating:
    """HP watts baseline learning during non-aux heating."""

    def test_first_sample_seeds_hp_watts(self):
        learner = AuxHeatLearner(default_hp_watts=3500.0)
        _record_normal_heating(learner, power_watts=2800.0)
        # First sample: direct assignment
        assert learner.learned_hp_watts == 2800.0

    def test_subsequent_samples_ema_update(self):
        learner = AuxHeatLearner(default_hp_watts=3500.0)
        _record_normal_heating(learner, power_watts=3000.0)
        # Second sample: EMA
        _record_normal_heating(learner, power_watts=4000.0)
        expected = _EMA_HP_ALPHA * 4000.0 + (1 - _EMA_HP_ALPHA) * 3000.0
        assert learner.learned_hp_watts == pytest.approx(expected, abs=0.1)

    def test_none_power_skipped(self):
        learner = AuxHeatLearner(default_hp_watts=3500.0)
        learner.record_interval(
            aux_heat_active=False,
            outdoor_temp_f=40.0,
            effective_outdoor_temp_f=38.0,
            outdoor_humidity=50.0,
            setpoint_delta_f=2.0,
            dt_minutes=5.0,
            hvac_running=True,
            hvac_mode="heat",
            power_watts=None,
        )
        # No update -- still default
        assert learner.learned_hp_watts == 3500.0

    def test_zero_power_skipped(self):
        learner = AuxHeatLearner(default_hp_watts=3500.0)
        learner.record_interval(
            aux_heat_active=False,
            outdoor_temp_f=40.0,
            effective_outdoor_temp_f=38.0,
            outdoor_humidity=50.0,
            setpoint_delta_f=2.0,
            dt_minutes=5.0,
            hvac_running=True,
            hvac_mode="heat",
            power_watts=0,
        )
        # 0 is falsy, so it's skipped
        assert learner.learned_hp_watts == 3500.0


class TestNonHeatingModeIgnored:
    """Non-heating modes should not contribute to hp watts learning."""

    def test_cooling_mode_ignored(self):
        learner = AuxHeatLearner(default_hp_watts=3500.0)
        learner.record_interval(
            aux_heat_active=False,
            outdoor_temp_f=90.0,
            effective_outdoor_temp_f=92.0,
            outdoor_humidity=60.0,
            setpoint_delta_f=1.0,
            dt_minutes=5.0,
            hvac_running=True,
            hvac_mode="cool",
            power_watts=2500.0,
        )
        assert learner.learned_hp_watts == 3500.0

    def test_off_mode_ignored(self):
        learner = AuxHeatLearner(default_hp_watts=3500.0)
        learner.record_interval(
            aux_heat_active=False,
            outdoor_temp_f=70.0,
            effective_outdoor_temp_f=70.0,
            outdoor_humidity=40.0,
            setpoint_delta_f=0.0,
            dt_minutes=5.0,
            hvac_running=False,
            hvac_mode="off",
            power_watts=100.0,
        )
        assert learner.learned_hp_watts == 3500.0

    def test_hvac_not_running_ignored(self):
        learner = AuxHeatLearner(default_hp_watts=3500.0)
        learner.record_interval(
            aux_heat_active=False,
            outdoor_temp_f=40.0,
            effective_outdoor_temp_f=38.0,
            outdoor_humidity=50.0,
            setpoint_delta_f=2.0,
            dt_minutes=5.0,
            hvac_running=False,
            hvac_mode="heat",
            power_watts=3000.0,
        )
        assert learner.learned_hp_watts == 3500.0

    def test_aux_active_ignored_for_hp_watts(self):
        """When aux is active, the power reading includes resistive strip -- skip it."""
        learner = AuxHeatLearner(default_hp_watts=3500.0)
        _record_aux_activation(learner)
        # HP watts should not have been updated from the aux-active interval
        assert learner.learned_hp_watts == 3500.0


class TestThresholdLearningReadiness:
    """is_learned requires MIN_EVENTS aux activation events."""

    def test_not_learned_with_fewer_than_min_events(self):
        learner = AuxHeatLearner()
        for i in range(_MIN_EVENTS - 1):
            _record_aux_activation(learner, effective_outdoor_temp_f=15.0 + i)
            _record_idle(learner)
        assert learner.is_learned is False
        assert learner.event_count == _MIN_EVENTS - 1

    def test_learned_at_exactly_min_events(self):
        learner = AuxHeatLearner()
        for i in range(_MIN_EVENTS):
            _record_aux_activation(learner, effective_outdoor_temp_f=15.0 + i)
            _record_idle(learner)
        assert learner.is_learned is True
        assert learner.event_count == _MIN_EVENTS

    def test_learned_above_min_events(self):
        learner = AuxHeatLearner()
        for i in range(_MIN_EVENTS + 5):
            _record_aux_activation(learner, effective_outdoor_temp_f=15.0)
            _record_idle(learner)
        assert learner.is_learned is True


class TestHPWattsLearningReadiness:
    """hp_watts_learned requires MIN_HP_SAMPLES non-aux heating samples."""

    def test_not_learned_with_fewer_than_min_samples(self):
        learner = AuxHeatLearner()
        for _ in range(_MIN_HP_SAMPLES - 1):
            _record_normal_heating(learner)
        assert learner.hp_watts_learned is False

    def test_learned_at_exactly_min_samples(self):
        learner = AuxHeatLearner()
        for _ in range(_MIN_HP_SAMPLES):
            _record_normal_heating(learner)
        assert learner.hp_watts_learned is True

    def test_learned_above_min_samples(self):
        learner = AuxHeatLearner()
        for _ in range(_MIN_HP_SAMPLES + 10):
            _record_normal_heating(learner)
        assert learner.hp_watts_learned is True


class TestEventCap:
    """Events list is capped at _MAX_EVENTS (50)."""

    def test_cap_at_max_events(self):
        learner = AuxHeatLearner()
        for i in range(_MAX_EVENTS + 10):
            _record_aux_activation(
                learner, effective_outdoor_temp_f=10.0 + (i * 0.1)
            )
            _record_idle(learner)
        assert learner.event_count == _MAX_EVENTS

    def test_oldest_events_evicted(self):
        learner = AuxHeatLearner()
        # Record MAX_EVENTS+1 events with distinguishable outdoor temps
        for i in range(_MAX_EVENTS + 1):
            _record_aux_activation(
                learner, outdoor_temp_f=float(i), effective_outdoor_temp_f=float(i)
            )
            _record_idle(learner)
        assert learner.event_count == _MAX_EVENTS
        # The first event (outdoor_temp_f=0.0) should have been evicted
        # The oldest remaining event should have outdoor_temp_f=1.0
        events_dict = learner.to_dict()["events"]
        assert events_dict[0]["outdoor_temp_f"] == 1.0
        assert events_dict[-1]["outdoor_temp_f"] == float(_MAX_EVENTS)


class TestToDictFromDictRoundtrip:
    """Persistence via to_dict/from_dict."""

    def test_empty_roundtrip(self):
        learner = AuxHeatLearner(default_hp_watts=4000.0)
        data = learner.to_dict()
        restored = AuxHeatLearner.from_dict(data, default_hp_watts=4000.0)
        assert restored.threshold_f == learner.threshold_f
        assert restored.learned_hp_watts == learner.learned_hp_watts
        assert restored.event_count == 0
        assert restored.hp_watts_learned is False
        assert restored.is_learned is False

    def test_roundtrip_with_events(self):
        learner = AuxHeatLearner()
        _record_normal_heating(learner, power_watts=2800.0)
        _record_aux_activation(learner, effective_outdoor_temp_f=18.0)
        _record_idle(learner)
        _record_aux_activation(learner, effective_outdoor_temp_f=12.0)

        data = learner.to_dict()
        restored = AuxHeatLearner.from_dict(data)

        assert restored.event_count == learner.event_count
        assert restored.threshold_f == pytest.approx(learner.threshold_f, abs=0.01)
        assert restored.learned_hp_watts == pytest.approx(
            learner.learned_hp_watts, abs=0.01
        )

    def test_roundtrip_preserves_hp_sample_count(self):
        learner = AuxHeatLearner()
        for _ in range(_MIN_HP_SAMPLES):
            _record_normal_heating(learner)
        assert learner.hp_watts_learned is True

        data = learner.to_dict()
        restored = AuxHeatLearner.from_dict(data)
        assert restored.hp_watts_learned is True

    def test_to_dict_structure(self):
        learner = AuxHeatLearner()
        _record_aux_activation(learner, effective_outdoor_temp_f=20.0)
        data = learner.to_dict()
        assert "events" in data
        assert "t_aux_threshold" in data
        assert "learned_hp_watts" in data
        assert "hp_sample_count" in data
        assert len(data["events"]) == 1
        ev = data["events"][0]
        assert "timestamp" in ev
        assert "outdoor_temp_f" in ev
        assert "effective_outdoor_temp_f" in ev

    def test_from_dict_handles_missing_keys(self):
        """from_dict should use defaults for missing keys."""
        restored = AuxHeatLearner.from_dict({})
        assert restored.threshold_f == _DEFAULT_THRESHOLD_F
        assert restored.learned_hp_watts == 3500.0
        assert restored.event_count == 0
        assert restored.hp_watts_learned is False

    def test_from_dict_skips_malformed_events(self):
        """Malformed event dicts should be silently skipped."""
        data = {
            "events": [
                {"bad_key": "not a real event"},
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "outdoor_temp_f": 20.0,
                    "outdoor_humidity": 80.0,
                    "effective_outdoor_temp_f": 15.0,
                    "setpoint_delta_f": 3.0,
                    "hp_runtime_before_min": 10.0,
                },
            ],
            "t_aux_threshold": 18.0,
            "learned_hp_watts": 3200.0,
            "hp_sample_count": 5,
        }
        restored = AuxHeatLearner.from_dict(data)
        # Malformed entry skipped, valid one loaded
        assert restored.event_count == 1
        assert restored.threshold_f == 18.0
        assert restored.learned_hp_watts == 3200.0

    def test_from_dict_custom_default_hp_watts(self):
        """from_dict respects the default_hp_watts parameter when key is missing."""
        restored = AuxHeatLearner.from_dict({}, default_hp_watts=5000.0)
        assert restored.learned_hp_watts == 5000.0


class TestHVACRuntimeReset:
    """Runtime counter behavior across HVAC on/off cycles."""

    def test_idle_resets_runtime(self):
        learner = AuxHeatLearner()
        _record_normal_heating(learner, dt_minutes=10.0)
        _record_normal_heating(learner, dt_minutes=10.0)
        # HVAC off -- should reset counter
        _record_idle(learner)
        _record_normal_heating(learner, dt_minutes=5.0)
        _record_aux_activation(learner)
        ev = learner.last_event
        # Only the 5 minutes since idle should count
        assert ev.hp_runtime_before_min == pytest.approx(5.0)
