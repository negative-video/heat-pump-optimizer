"""Tests for OverrideTracker -- override pattern detection and comfort adjustments."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.conftest import load_module, CC

# Load the override_tracker module under test
ot_mod = load_module(
    "custom_components.heatpump_optimizer.learning.override_tracker",
    os.path.join(CC, "learning", "override_tracker.py"),
)
OverrideTracker = ot_mod.OverrideTracker
OverrideRecord = ot_mod.OverrideRecord
OverridePattern = ot_mod.OverridePattern
PATTERN_THRESHOLD = ot_mod.PATTERN_THRESHOLD
PATTERN_WINDOW_DAYS = ot_mod.PATTERN_WINDOW_DAYS
PREEMPTIVE_MIN_OCCURRENCES = ot_mod.PREEMPTIVE_MIN_OCCURRENCES
PREEMPTIVE_MAX_ADJUSTMENT_F = ot_mod.PREEMPTIVE_MAX_ADJUSTMENT_F

NOW = datetime(2026, 4, 7, 12, 0, 0, tzinfo=timezone.utc)


def _ts(days_ago: int = 0, hour: int = 12) -> datetime:
    """Return a UTC timestamp `days_ago` days before NOW at the given hour."""
    return NOW - timedelta(days=days_ago) + timedelta(hours=hour - 12)


# ── record_override ──────────────────────────────────────────────────


class TestRecordOverride:
    def test_warmer_direction(self):
        tracker = OverrideTracker()
        tracker.record_override(70.0, 73.0, timestamp=NOW)
        assert tracker.record_count == 1
        rec = tracker._records[0]
        assert rec.direction == "warmer"
        assert rec.expected_setpoint == 70.0
        assert rec.actual_setpoint == 73.0
        assert rec.hour_of_day == NOW.hour
        assert rec.day_of_week == NOW.weekday()

    def test_cooler_direction(self):
        tracker = OverrideTracker()
        tracker.record_override(74.0, 71.0, timestamp=NOW)
        assert tracker.record_count == 1
        rec = tracker._records[0]
        assert rec.direction == "cooler"
        assert rec.expected_setpoint == 74.0
        assert rec.actual_setpoint == 71.0

    def test_exact_match_is_cooler(self):
        """When actual == expected, diff is 0 which is not > 0, so direction is cooler."""
        tracker = OverrideTracker()
        tracker.record_override(70.0, 70.0, timestamp=NOW)
        assert tracker._records[0].direction == "cooler"

    def test_default_timestamp_is_utc(self):
        """When no timestamp is given, one is generated automatically."""
        tracker = OverrideTracker()
        tracker.record_override(70.0, 73.0)
        assert tracker.record_count == 1
        assert tracker._records[0].timestamp.tzinfo is not None

    def test_multiple_records_accumulate(self):
        tracker = OverrideTracker()
        for i in range(5):
            tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=i))
        assert tracker.record_count == 5


# ── 90-day trimming ──────────────────────────────────────────────────


class TestNinetyDayTrimming:
    def test_old_records_are_trimmed(self):
        tracker = OverrideTracker()
        # Add a record 100 days ago
        old_ts = NOW - timedelta(days=100)
        tracker.record_override(70.0, 73.0, timestamp=old_ts)
        assert tracker.record_count == 1

        # Adding a new record triggers trimming based on the new timestamp
        tracker.record_override(70.0, 73.0, timestamp=NOW)
        assert tracker.record_count == 1  # old record trimmed

    def test_records_within_90_days_kept(self):
        tracker = OverrideTracker()
        # Record at exactly 89 days ago
        recent_ts = NOW - timedelta(days=89)
        tracker.record_override(70.0, 73.0, timestamp=recent_ts)
        # Record now triggers trimming -- 89-day-old record should survive
        tracker.record_override(70.0, 73.0, timestamp=NOW)
        assert tracker.record_count == 2

    def test_record_at_exactly_90_days_is_trimmed(self):
        tracker = OverrideTracker()
        # Record at exactly 90 days ago
        boundary_ts = NOW - timedelta(days=90)
        tracker.record_override(70.0, 73.0, timestamp=boundary_ts)
        # Adding NOW triggers trim -- cutoff uses > so exactly 90 days is excluded
        tracker.record_override(70.0, 73.0, timestamp=NOW)
        assert tracker.record_count == 1  # boundary record trimmed

    def test_bulk_trimming(self):
        tracker = OverrideTracker()
        # Add 10 old records (95+ days ago)
        for i in range(10):
            old_ts = NOW - timedelta(days=95 + i)
            tracker.record_override(70.0, 73.0, timestamp=old_ts)
        assert tracker.record_count == 10

        # Now add a current record -- all old ones should be trimmed
        tracker.record_override(70.0, 73.0, timestamp=NOW)
        assert tracker.record_count == 1


# ── detect_patterns ──────────────────────────────────────────────────


class TestDetectPatterns:
    def _populate_pattern(
        self,
        tracker: OverrideTracker,
        hour: int,
        count: int,
        direction_up: bool = True,
    ) -> None:
        """Add `count` overrides at the given hour, spread over recent days."""
        for i in range(count):
            ts = _ts(days_ago=i, hour=hour)
            if direction_up:
                tracker.record_override(70.0, 73.0, timestamp=ts)
            else:
                tracker.record_override(74.0, 71.0, timestamp=ts)

    def test_no_records_no_patterns(self):
        tracker = OverrideTracker()
        assert tracker.detect_patterns() == []

    def test_below_threshold_no_pattern(self):
        tracker = OverrideTracker()
        # Add PATTERN_THRESHOLD - 1 records at hour 8
        self._populate_pattern(tracker, hour=8, count=PATTERN_THRESHOLD - 1)
        assert tracker.detect_patterns() == []

    def test_at_threshold_detects_pattern(self):
        tracker = OverrideTracker()
        self._populate_pattern(tracker, hour=8, count=PATTERN_THRESHOLD)
        patterns = tracker.detect_patterns()
        assert len(patterns) == 1
        assert patterns[0].hour_of_day == 8
        assert patterns[0].occurrences == PATTERN_THRESHOLD

    def test_above_threshold_detects_pattern(self):
        tracker = OverrideTracker()
        self._populate_pattern(tracker, hour=8, count=PATTERN_THRESHOLD + 5)
        patterns = tracker.detect_patterns()
        assert len(patterns) == 1
        assert patterns[0].occurrences == PATTERN_THRESHOLD + 5

    def test_warmer_direction_detected(self):
        tracker = OverrideTracker()
        self._populate_pattern(tracker, hour=8, count=5, direction_up=True)
        patterns = tracker.detect_patterns()
        assert len(patterns) == 1
        assert patterns[0].avg_direction == "warmer"
        assert patterns[0].avg_setpoint_change > 0

    def test_cooler_direction_detected(self):
        tracker = OverrideTracker()
        self._populate_pattern(tracker, hour=20, count=5, direction_up=False)
        patterns = tracker.detect_patterns()
        assert len(patterns) == 1
        assert patterns[0].avg_direction == "cooler"
        assert patterns[0].avg_setpoint_change < 0

    def test_warmer_suggestion_text(self):
        tracker = OverrideTracker()
        self._populate_pattern(tracker, hour=7, count=4, direction_up=True)
        patterns = tracker.detect_patterns()
        assert len(patterns) == 1
        assert "raising the comfort minimum" in patterns[0].suggestion
        assert "7:00" in patterns[0].suggestion

    def test_cooler_suggestion_text(self):
        tracker = OverrideTracker()
        self._populate_pattern(tracker, hour=22, count=4, direction_up=False)
        patterns = tracker.detect_patterns()
        assert len(patterns) == 1
        assert "lowering the comfort maximum" in patterns[0].suggestion
        assert "22:00" in patterns[0].suggestion

    def test_multiple_patterns_sorted_by_frequency(self):
        tracker = OverrideTracker()
        # 3 overrides at hour 10
        self._populate_pattern(tracker, hour=10, count=3)
        # 6 overrides at hour 18
        self._populate_pattern(tracker, hour=18, count=6)
        # 4 overrides at hour 14
        self._populate_pattern(tracker, hour=14, count=4)

        patterns = tracker.detect_patterns()
        assert len(patterns) == 3
        assert patterns[0].hour_of_day == 18  # most frequent first
        assert patterns[1].hour_of_day == 14
        assert patterns[2].hour_of_day == 10

    def test_records_outside_window_excluded(self):
        tracker = OverrideTracker()
        # Add records just outside the default 30-day window
        for i in range(5):
            ts = NOW - timedelta(days=PATTERN_WINDOW_DAYS + 1 + i)
            tracker.record_override(70.0, 73.0, timestamp=ts)
        # These are within 90 days (so kept in storage) but outside 30-day window
        assert tracker.record_count == 5
        assert tracker.detect_patterns() == []

    def test_custom_window_days(self):
        tracker = OverrideTracker()
        # Add records 40-45 days ago (outside 30-day default, inside 60-day window)
        for i in range(5):
            ts = NOW - timedelta(days=40 + i)
            tracker.record_override(70.0, 73.0, timestamp=ts)
        assert tracker.detect_patterns(window_days=30) == []
        patterns = tracker.detect_patterns(window_days=60)
        assert len(patterns) == 1


# ── dominant direction ────────────────────────────────────────────────


class TestDominantDirection:
    def test_mixed_direction_majority_warmer(self):
        tracker = OverrideTracker()
        # 3 warmer + 1 cooler at hour 9
        for i in range(3):
            tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=i, hour=9))
        tracker.record_override(74.0, 71.0, timestamp=_ts(days_ago=3, hour=9))

        patterns = tracker.detect_patterns()
        assert len(patterns) == 1
        assert patterns[0].avg_direction == "warmer"

    def test_mixed_direction_majority_cooler(self):
        tracker = OverrideTracker()
        # 1 warmer + 3 cooler at hour 15
        tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=0, hour=15))
        for i in range(1, 4):
            tracker.record_override(74.0, 71.0, timestamp=_ts(days_ago=i, hour=15))

        patterns = tracker.detect_patterns()
        assert len(patterns) == 1
        assert patterns[0].avg_direction == "cooler"

    def test_tie_defaults_to_cooler(self):
        """When warmer == cooler count, direction should be cooler (not >)."""
        tracker = OverrideTracker()
        # 2 warmer + 2 cooler = 4 total (above threshold of 3)
        tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=0, hour=11))
        tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=1, hour=11))
        tracker.record_override(74.0, 71.0, timestamp=_ts(days_ago=2, hour=11))
        tracker.record_override(74.0, 71.0, timestamp=_ts(days_ago=3, hour=11))

        patterns = tracker.detect_patterns()
        assert len(patterns) == 1
        assert patterns[0].avg_direction == "cooler"


# ── get_comfort_adjustment ────────────────────────────────────────────


class TestGetComfortAdjustment:
    def test_no_data_returns_zero(self):
        tracker = OverrideTracker()
        assert tracker.get_comfort_adjustment(8) == 0.0

    def test_below_min_occurrences_returns_zero(self):
        tracker = OverrideTracker()
        for i in range(PREEMPTIVE_MIN_OCCURRENCES - 1):
            tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=i, hour=8))
        assert tracker.get_comfort_adjustment(8) == 0.0

    def test_at_min_occurrences_returns_adjustment(self):
        tracker = OverrideTracker()
        for i in range(PREEMPTIVE_MIN_OCCURRENCES):
            tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=i, hour=8))
        adj = tracker.get_comfort_adjustment(8)
        # avg change is +3.0, adjustment is half = +1.5
        assert adj == pytest.approx(1.5)

    def test_warmer_adjustment_positive(self):
        tracker = OverrideTracker()
        for i in range(6):
            tracker.record_override(70.0, 74.0, timestamp=_ts(days_ago=i, hour=10))
        adj = tracker.get_comfort_adjustment(10)
        # avg change = +4.0, half = +2.0, capped at PREEMPTIVE_MAX_ADJUSTMENT_F
        assert adj == pytest.approx(PREEMPTIVE_MAX_ADJUSTMENT_F)

    def test_cooler_adjustment_negative(self):
        tracker = OverrideTracker()
        for i in range(6):
            tracker.record_override(74.0, 71.0, timestamp=_ts(days_ago=i, hour=20))
        adj = tracker.get_comfort_adjustment(20)
        # avg change = -3.0, half = -1.5
        assert adj == pytest.approx(-1.5)

    def test_capped_at_max_positive(self):
        tracker = OverrideTracker()
        for i in range(6):
            # Large override: +10 degrees
            tracker.record_override(65.0, 75.0, timestamp=_ts(days_ago=i, hour=6))
        adj = tracker.get_comfort_adjustment(6)
        # avg change = +10, half = +5.0, capped at +2.0
        assert adj == pytest.approx(PREEMPTIVE_MAX_ADJUSTMENT_F)

    def test_capped_at_max_negative(self):
        tracker = OverrideTracker()
        for i in range(6):
            # Large override: -10 degrees
            tracker.record_override(80.0, 70.0, timestamp=_ts(days_ago=i, hour=22))
        adj = tracker.get_comfort_adjustment(22)
        # avg change = -10, half = -5.0, capped at -2.0
        assert adj == pytest.approx(-PREEMPTIVE_MAX_ADJUSTMENT_F)

    def test_different_hours_independent(self):
        tracker = OverrideTracker()
        for i in range(6):
            tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=i, hour=8))
            tracker.record_override(74.0, 71.0, timestamp=_ts(days_ago=i, hour=20))
        assert tracker.get_comfort_adjustment(8) > 0
        assert tracker.get_comfort_adjustment(20) < 0
        assert tracker.get_comfort_adjustment(14) == 0.0  # no data at hour 14

    def test_old_records_outside_window_ignored(self):
        tracker = OverrideTracker()
        # Records just outside the PATTERN_WINDOW_DAYS window
        for i in range(6):
            ts = NOW - timedelta(days=PATTERN_WINDOW_DAYS + 1 + i)
            tracker.record_override(70.0, 73.0, timestamp=ts)
        # Still within 90-day storage but outside pattern window
        assert tracker.record_count == 6
        assert tracker.get_comfort_adjustment(12) == 0.0


# ── to_dict / from_dict roundtrip ─────────────────────────────────────


class TestSerialization:
    def test_empty_tracker_roundtrip(self):
        tracker = OverrideTracker()
        data = tracker.to_dict()
        restored = OverrideTracker.from_dict(data)
        assert restored.record_count == 0

    def test_full_roundtrip(self):
        tracker = OverrideTracker()
        tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=0, hour=8))
        tracker.record_override(74.0, 71.0, timestamp=_ts(days_ago=1, hour=20))
        tracker.record_override(68.0, 72.0, timestamp=_ts(days_ago=2, hour=14))

        data = tracker.to_dict()
        restored = OverrideTracker.from_dict(data)

        assert restored.record_count == 3
        # Verify all fields survived
        for orig, rest in zip(tracker._records, restored._records):
            assert rest.timestamp == orig.timestamp
            assert rest.hour_of_day == orig.hour_of_day
            assert rest.day_of_week == orig.day_of_week
            assert rest.expected_setpoint == orig.expected_setpoint
            assert rest.actual_setpoint == orig.actual_setpoint
            assert rest.direction == orig.direction

    def test_to_dict_structure(self):
        tracker = OverrideTracker()
        tracker.record_override(70.0, 73.0, timestamp=NOW)
        data = tracker.to_dict()
        assert "records" in data
        assert len(data["records"]) == 1
        rec = data["records"][0]
        assert "timestamp" in rec
        assert "hour_of_day" in rec
        assert "day_of_week" in rec
        assert "expected_setpoint" in rec
        assert "actual_setpoint" in rec
        assert "direction" in rec

    def test_from_dict_tolerates_empty_records(self):
        restored = OverrideTracker.from_dict({"records": []})
        assert restored.record_count == 0

    def test_from_dict_tolerates_missing_records_key(self):
        restored = OverrideTracker.from_dict({})
        assert restored.record_count == 0

    def test_from_dict_skips_malformed_records(self):
        data = {
            "records": [
                {
                    "timestamp": NOW.isoformat(),
                    "hour_of_day": 12,
                    "day_of_week": 0,
                    "expected_setpoint": 70.0,
                    "actual_setpoint": 73.0,
                    "direction": "warmer",
                },
                {
                    # Missing required fields
                    "timestamp": NOW.isoformat(),
                },
                {
                    # Invalid timestamp
                    "timestamp": "not-a-date",
                    "hour_of_day": 10,
                    "day_of_week": 2,
                    "expected_setpoint": 70.0,
                    "actual_setpoint": 73.0,
                    "direction": "warmer",
                },
            ]
        }
        restored = OverrideTracker.from_dict(data)
        assert restored.record_count == 1  # only first valid record

    def test_roundtrip_preserves_patterns(self):
        """Patterns detected before serialization should also be detected after."""
        tracker = OverrideTracker()
        for i in range(5):
            tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=i, hour=9))

        patterns_before = tracker.detect_patterns()
        data = tracker.to_dict()
        restored = OverrideTracker.from_dict(data)
        patterns_after = restored.detect_patterns()

        assert len(patterns_before) == len(patterns_after)
        assert patterns_before[0].hour_of_day == patterns_after[0].hour_of_day
        assert patterns_before[0].occurrences == patterns_after[0].occurrences


# ── get_stats ─────────────────────────────────────────────────────────


class TestGetStats:
    def test_empty_tracker_stats(self):
        tracker = OverrideTracker()
        stats = tracker.get_stats()
        assert stats["total_overrides_30d"] == 0
        assert stats["total_overrides_7d"] == 0
        assert stats["pattern_count"] == 0
        assert stats["top_pattern"] is None
        assert stats["by_hour_30d"] == {}

    def test_recent_overrides_counted(self):
        tracker = OverrideTracker()
        # 3 within 7 days
        for i in range(3):
            tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=i, hour=10))
        # 2 more between 7-30 days ago
        tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=15, hour=10))
        tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=20, hour=10))

        stats = tracker.get_stats()
        assert stats["total_overrides_30d"] == 5
        assert stats["total_overrides_7d"] == 3

    def test_pattern_count_in_stats(self):
        tracker = OverrideTracker()
        # Create a pattern at hour 10
        for i in range(4):
            tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=i, hour=10))
        # Create another at hour 18
        for i in range(4):
            tracker.record_override(74.0, 71.0, timestamp=_ts(days_ago=i, hour=18))

        stats = tracker.get_stats()
        assert stats["pattern_count"] == 2
        assert stats["top_pattern"] is not None

    def test_by_hour_distribution(self):
        tracker = OverrideTracker()
        tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=0, hour=8))
        tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=1, hour=8))
        tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=0, hour=20))

        stats = tracker.get_stats()
        assert stats["by_hour_30d"][8] == 2
        assert stats["by_hour_30d"][20] == 1

    def test_old_records_excluded_from_stats(self):
        tracker = OverrideTracker()
        # Record at 60 days ago (within 90-day storage, outside 30-day stats)
        tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=60, hour=10))
        # Record at 2 days ago
        tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=2, hour=10))

        stats = tracker.get_stats()
        assert stats["total_overrides_30d"] == 1
        assert stats["total_overrides_7d"] == 1


# ── record_count property ─────────────────────────────────────────────


class TestRecordCount:
    def test_initial_count_zero(self):
        tracker = OverrideTracker()
        assert tracker.record_count == 0

    def test_count_after_additions(self):
        tracker = OverrideTracker()
        tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=0))
        tracker.record_override(70.0, 73.0, timestamp=_ts(days_ago=1))
        assert tracker.record_count == 2
