"""Override pattern tracker — learns when humans override the optimizer.

Tracks manual override events by time of day and day of week to detect
repeating patterns. If someone consistently overrides at the same time,
the integration can surface a suggestion to adjust comfort ranges or
exclude that time window from optimization.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

_LOGGER = logging.getLogger(__name__)

# Minimum occurrences to call it a pattern
PATTERN_THRESHOLD = 3
PATTERN_WINDOW_DAYS = 30
# Preemptive comfort adjustment
PREEMPTIVE_MIN_OCCURRENCES = 5
PREEMPTIVE_MAX_ADJUSTMENT_F = 2.0  # cap how much we shift comfort bounds


@dataclass
class OverrideRecord:
    """A single override event."""

    timestamp: datetime
    hour_of_day: int
    day_of_week: int  # 0=Monday
    expected_setpoint: float
    actual_setpoint: float
    direction: str  # "warmer" or "cooler"


@dataclass
class OverridePattern:
    """A detected repeating override pattern."""

    hour_of_day: int
    occurrences: int
    avg_direction: str  # "warmer" or "cooler"
    avg_setpoint_change: float  # °F
    suggestion: str


class OverrideTracker:
    """Tracks override history and detects patterns."""

    def __init__(self):
        self._records: list[OverrideRecord] = []

    def record_override(
        self,
        expected_setpoint: float,
        actual_setpoint: float,
        timestamp: datetime | None = None,
    ) -> None:
        """Record a new override event."""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        diff = actual_setpoint - expected_setpoint
        direction = "warmer" if diff > 0 else "cooler"

        record = OverrideRecord(
            timestamp=timestamp,
            hour_of_day=timestamp.hour,
            day_of_week=timestamp.weekday(),
            expected_setpoint=expected_setpoint,
            actual_setpoint=actual_setpoint,
            direction=direction,
        )
        self._records.append(record)

        # Trim to last 90 days
        cutoff = timestamp - timedelta(days=90)
        self._records = [r for r in self._records if r.timestamp > cutoff]

        _LOGGER.debug(
            "Override recorded: %s at hour %d (%+.1f°F %s)",
            timestamp.strftime("%a"), record.hour_of_day, diff, direction,
        )

    def detect_patterns(self, window_days: int = PATTERN_WINDOW_DAYS) -> list[OverridePattern]:
        """Analyze override history for repeating time-of-day patterns.

        Returns patterns sorted by frequency (most common first).
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        recent = [r for r in self._records if r.timestamp > cutoff]

        if not recent:
            return []

        # Group by hour of day
        by_hour: dict[int, list[OverrideRecord]] = defaultdict(list)
        for r in recent:
            by_hour[r.hour_of_day].append(r)

        patterns: list[OverridePattern] = []
        for hour, records in by_hour.items():
            if len(records) < PATTERN_THRESHOLD:
                continue

            # Determine dominant direction
            warmer = sum(1 for r in records if r.direction == "warmer")
            cooler = len(records) - warmer
            direction = "warmer" if warmer > cooler else "cooler"

            avg_change = sum(
                r.actual_setpoint - r.expected_setpoint for r in records
            ) / len(records)

            # Generate suggestion
            if direction == "warmer":
                suggestion = (
                    f"Consider raising the comfort minimum around {hour}:00 — "
                    f"overrides average {avg_change:+.1f}°F ({len(records)} times in {window_days} days)"
                )
            else:
                suggestion = (
                    f"Consider lowering the comfort maximum around {hour}:00 — "
                    f"overrides average {avg_change:+.1f}°F ({len(records)} times in {window_days} days)"
                )

            patterns.append(
                OverridePattern(
                    hour_of_day=hour,
                    occurrences=len(records),
                    avg_direction=direction,
                    avg_setpoint_change=round(avg_change, 1),
                    suggestion=suggestion,
                )
            )

        patterns.sort(key=lambda p: p.occurrences, reverse=True)
        return patterns

    def get_comfort_adjustment(self, hour: int) -> float:
        """Get the preemptive comfort shift for a given hour of day.

        Returns a signed °F value: positive means users tend to override warmer,
        negative means cooler. Returns 0.0 if no strong pattern exists.

        The adjustment is half the average override magnitude (conservative),
        capped at ±PREEMPTIVE_MAX_ADJUSTMENT_F.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=PATTERN_WINDOW_DAYS)
        recent = [
            r for r in self._records
            if r.timestamp > cutoff and r.hour_of_day == hour
        ]

        if len(recent) < PREEMPTIVE_MIN_OCCURRENCES:
            return 0.0

        avg_change = sum(
            r.actual_setpoint - r.expected_setpoint for r in recent
        ) / len(recent)

        # Conservative: apply half the average, capped
        adjustment = avg_change * 0.5
        return max(-PREEMPTIVE_MAX_ADJUSTMENT_F, min(PREEMPTIVE_MAX_ADJUSTMENT_F, adjustment))

    def get_stats(self) -> dict:
        """Summary statistics for sensor attributes."""
        now = datetime.now(timezone.utc)
        last_30 = [r for r in self._records if r.timestamp > now - timedelta(days=30)]
        last_7 = [r for r in self._records if r.timestamp > now - timedelta(days=7)]

        patterns = self.detect_patterns()

        return {
            "total_overrides_30d": len(last_30),
            "total_overrides_7d": len(last_7),
            "pattern_count": len(patterns),
            "top_pattern": patterns[0].suggestion if patterns else None,
            "by_hour_30d": self._count_by_hour(last_30),
        }

    @staticmethod
    def _count_by_hour(records: list[OverrideRecord]) -> dict[int, int]:
        counts: dict[int, int] = {}
        for r in records:
            counts[r.hour_of_day] = counts.get(r.hour_of_day, 0) + 1
        return counts

    @property
    def record_count(self) -> int:
        return len(self._records)

    def to_dict(self) -> dict:
        """Serialize for persistence."""
        return {
            "records": [
                {
                    "timestamp": r.timestamp.isoformat(),
                    "hour_of_day": r.hour_of_day,
                    "day_of_week": r.day_of_week,
                    "expected_setpoint": r.expected_setpoint,
                    "actual_setpoint": r.actual_setpoint,
                    "direction": r.direction,
                }
                for r in self._records
            ]
        }

    @classmethod
    def from_dict(cls, data: dict) -> OverrideTracker:
        """Restore from persisted data."""
        tracker = cls()
        for item in data.get("records", []):
            try:
                tracker._records.append(
                    OverrideRecord(
                        timestamp=datetime.fromisoformat(item["timestamp"]),
                        hour_of_day=item["hour_of_day"],
                        day_of_week=item["day_of_week"],
                        expected_setpoint=item["expected_setpoint"],
                        actual_setpoint=item["actual_setpoint"],
                        direction=item["direction"],
                    )
                )
            except (KeyError, ValueError):
                continue
        return tracker
