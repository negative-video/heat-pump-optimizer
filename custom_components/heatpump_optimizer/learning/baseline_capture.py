"""Baseline schedule capture — learns the user's pre-optimization thermostat routine.

During the INIT_MODE_LEARNING phase (when the optimizer is NOT writing setpoints),
this module records what the thermostat is actually doing every 5 minutes. After 7
days of observation, it aggregates the data into a BaselineScheduleTemplate that
represents the user's "normal" routine.

This template powers the counterfactual simulator, enabling meaningful savings
comparisons: "what would have happened if you kept your old routine" vs "what the
optimizer actually did."

After optimization starts, override patterns from OverrideTracker can continuously
refine the baseline — if the user consistently overrides to 72°F at 7pm, that
updates the baseline for 7pm.
"""

from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..engine.data_types import BaselineScheduleTemplate

_LOGGER = logging.getLogger(__name__)

# Minimum observation requirements
MIN_OBSERVATION_DAYS = 7
MIN_WEEKDAY_DAYS = 5
MIN_WEEKEND_DAYS = 2
MIN_SAMPLES_PER_BUCKET = 3  # at least 3 readings per hour bucket

# Override refinement thresholds
OVERRIDE_REFINEMENT_MIN_OCCURRENCES = 5
OVERRIDE_REFINEMENT_WEIGHT = 0.3  # blend 30% override, 70% original


@dataclass
class SetpointObservation:
    """A single observation of the thermostat's actual behavior."""
    timestamp: datetime
    hour_of_day: int  # 0-23
    day_of_week: int  # 0=Monday, 6=Sunday
    setpoint: float  # °F
    mode: str  # "cool", "heat", "off"


class BaselineCapture:
    """Captures and aggregates the user's pre-optimization thermostat schedule.

    Usage:
        1. During learning phase, call record_observation() every 5 minutes
        2. Check is_ready to see if enough data has been collected
        3. Call build_template() to produce the BaselineScheduleTemplate
        4. Optionally refine with override data via refine_from_overrides()
    """

    def __init__(self) -> None:
        self._observations: list[SetpointObservation] = []
        self._template: BaselineScheduleTemplate | None = None
        self._observed_dates: set[str] = set()  # "YYYY-MM-DD" strings

    def record_observation(
        self,
        now: datetime,
        setpoint: float,
        mode: str,
    ) -> None:
        """Record a thermostat observation during the learning period.

        Args:
            now: Current time (local timezone).
            setpoint: Current thermostat target temperature (°F).
            mode: Current HVAC mode ("cool", "heat", "off").
        """
        obs = SetpointObservation(
            timestamp=now,
            hour_of_day=now.hour,
            day_of_week=now.weekday(),
            setpoint=setpoint,
            mode=mode,
        )
        self._observations.append(obs)
        self._observed_dates.add(now.strftime("%Y-%m-%d"))

    @property
    def sample_days(self) -> int:
        """Number of unique calendar days observed."""
        return len(self._observed_dates)

    @property
    def is_ready(self) -> bool:
        """Whether enough data has been collected to build a baseline template."""
        if self.sample_days < MIN_OBSERVATION_DAYS:
            return False

        weekday_dates = set()
        weekend_dates = set()
        for obs in self._observations:
            date_str = obs.timestamp.strftime("%Y-%m-%d")
            if obs.day_of_week < 5:
                weekday_dates.add(date_str)
            else:
                weekend_dates.add(date_str)

        return len(weekday_dates) >= MIN_WEEKDAY_DAYS and len(weekend_dates) >= MIN_WEEKEND_DAYS

    @property
    def days_remaining(self) -> int:
        """Estimated days remaining before baseline is ready."""
        if self.is_ready:
            return 0
        return max(0, MIN_OBSERVATION_DAYS - self.sample_days)

    @property
    def confidence(self) -> float:
        """Confidence in the baseline schedule (0.0 to 1.0).

        Based on:
        - Number of observation days (more days = more confidence)
        - Coverage of hour buckets (all 48 slots filled = higher confidence)
        - Consistency of observations (low variance = higher confidence)
        """
        if not self._observations:
            return 0.0

        if self._template is None and not self.is_ready:
            # Linear ramp from 0 to 0.3 over the observation period
            return min(0.3, self.sample_days / MIN_OBSERVATION_DAYS * 0.3)

        if self._template is not None:
            return self._template.confidence

        return 0.0

    def build_template(self) -> BaselineScheduleTemplate:
        """Aggregate observations into a baseline schedule template.

        Groups observations by (hour, is_weekend) and computes median setpoint
        and dominant mode for each bucket.
        """
        if not self._observations:
            _LOGGER.warning("No observations to build baseline template from")
            return BaselineScheduleTemplate()

        # Separate weekday vs weekend observations
        weekday_obs: dict[int, list[SetpointObservation]] = defaultdict(list)
        weekend_obs: dict[int, list[SetpointObservation]] = defaultdict(list)

        for obs in self._observations:
            if obs.day_of_week < 5:
                weekday_obs[obs.hour_of_day].append(obs)
            else:
                weekend_obs[obs.hour_of_day].append(obs)

        weekday_setpoints = {}
        weekday_modes = {}
        weekend_setpoints = {}
        weekend_modes = {}

        # Fill weekday buckets
        for hour in range(24):
            obs_list = weekday_obs.get(hour, [])
            if obs_list:
                weekday_setpoints[hour] = round(
                    statistics.median(o.setpoint for o in obs_list), 1
                )
                weekday_modes[hour] = self._dominant_mode(obs_list)
            else:
                # Interpolate from nearest neighbor
                weekday_setpoints[hour] = self._interpolate_setpoint(
                    hour, weekday_obs
                )
                weekday_modes[hour] = "off"

        # Fill weekend buckets
        for hour in range(24):
            obs_list = weekend_obs.get(hour, [])
            if obs_list:
                weekend_setpoints[hour] = round(
                    statistics.median(o.setpoint for o in obs_list), 1
                )
                weekend_modes[hour] = self._dominant_mode(obs_list)
            else:
                weekend_setpoints[hour] = self._interpolate_setpoint(
                    hour, weekend_obs
                )
                weekend_modes[hour] = "off"

        # Compute confidence based on coverage and consistency
        filled_buckets = sum(
            1 for h in range(24)
            if weekday_obs.get(h) and len(weekday_obs[h]) >= MIN_SAMPLES_PER_BUCKET
        ) + sum(
            1 for h in range(24)
            if weekend_obs.get(h) and len(weekend_obs[h]) >= MIN_SAMPLES_PER_BUCKET
        )
        coverage = filled_buckets / 48.0

        # Consistency: average coefficient of variation across buckets
        cvs = []
        for obs_dict in [weekday_obs, weekend_obs]:
            for hour, obs_list in obs_dict.items():
                if len(obs_list) >= MIN_SAMPLES_PER_BUCKET:
                    setpoints = [o.setpoint for o in obs_list]
                    mean = statistics.mean(setpoints)
                    if mean > 0:
                        cv = statistics.stdev(setpoints) / mean if len(setpoints) > 1 else 0
                        cvs.append(cv)

        avg_cv = statistics.mean(cvs) if cvs else 1.0
        consistency = max(0.0, 1.0 - avg_cv * 10)  # low CV = high consistency

        confidence = min(1.0, coverage * 0.6 + consistency * 0.4)

        self._template = BaselineScheduleTemplate(
            weekday_setpoints=weekday_setpoints,
            weekend_setpoints=weekend_setpoints,
            weekday_modes=weekday_modes,
            weekend_modes=weekend_modes,
            capture_method="learning_period",
            capture_date=datetime.now(timezone.utc),
            confidence=round(confidence, 3),
            sample_days=self.sample_days,
        )

        _LOGGER.info(
            "Baseline schedule captured: %d days, %.0f%% confidence, "
            "coverage=%.0f%%, consistency=%.0f%%",
            self.sample_days,
            confidence * 100,
            coverage * 100,
            consistency * 100,
        )

        return self._template

    @property
    def template(self) -> BaselineScheduleTemplate | None:
        """Get the current baseline template, if built."""
        return self._template

    def get_baseline_setpoint(self, now: datetime) -> float | None:
        """Get the baseline setpoint for the current time.

        Returns None if no template has been built yet.
        """
        if self._template is None:
            return None

        hour = now.hour
        is_weekend = now.weekday() >= 5

        if is_weekend:
            return self._template.weekend_setpoints.get(hour)
        return self._template.weekday_setpoints.get(hour)

    def get_baseline_mode(self, now: datetime) -> str | None:
        """Get the baseline HVAC mode for the current time."""
        if self._template is None:
            return None

        hour = now.hour
        is_weekend = now.weekday() >= 5

        if is_weekend:
            return self._template.weekend_modes.get(hour, "off")
        return self._template.weekday_modes.get(hour, "off")

    def refine_from_overrides(
        self,
        override_records: list[dict],
    ) -> None:
        """Refine baseline schedule using override patterns.

        When users consistently override the optimizer to a certain setpoint
        at a given hour, that's a signal about what they actually want —
        and therefore what their baseline routine would have been.

        Args:
            override_records: List of dicts with hour_of_day, actual_setpoint,
                and count fields from OverrideTracker patterns.
        """
        if self._template is None:
            return

        for record in override_records:
            hour = record.get("hour_of_day")
            setpoint = record.get("actual_setpoint")
            count = record.get("count", 0)

            if hour is None or setpoint is None:
                continue
            if count < OVERRIDE_REFINEMENT_MIN_OCCURRENCES:
                continue

            # Blend override setpoint into baseline
            weight = OVERRIDE_REFINEMENT_WEIGHT
            for setpoints in [
                self._template.weekday_setpoints,
                self._template.weekend_setpoints,
            ]:
                if hour in setpoints:
                    original = setpoints[hour]
                    setpoints[hour] = round(
                        original * (1 - weight) + setpoint * weight, 1
                    )

            _LOGGER.debug(
                "Baseline refined from overrides: hour=%d, setpoint=%.1f°F (%d occurrences)",
                hour, setpoint, count,
            )

        self._template.capture_method = "override_inferred"

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _dominant_mode(obs_list: list[SetpointObservation]) -> str:
        """Return the most common HVAC mode in a list of observations."""
        mode_counts: dict[str, int] = defaultdict(int)
        for obs in obs_list:
            mode_counts[obs.mode] += 1
        return max(mode_counts, key=mode_counts.get)  # type: ignore[arg-type]

    @staticmethod
    def _interpolate_setpoint(
        hour: int,
        obs_by_hour: dict[int, list[SetpointObservation]],
    ) -> float:
        """Interpolate setpoint from nearest hour that has observations."""
        # Search outward from the target hour
        for offset in range(1, 13):
            for neighbor in [(hour - offset) % 24, (hour + offset) % 24]:
                if neighbor in obs_by_hour and obs_by_hour[neighbor]:
                    return round(
                        statistics.median(
                            o.setpoint for o in obs_by_hour[neighbor]
                        ),
                        1,
                    )
        return 72.0  # absolute fallback

    # ── Persistence ─────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialize for HA Store persistence."""
        data: dict[str, Any] = {
            "observed_dates": sorted(self._observed_dates),
            "observations": [
                {
                    "timestamp": obs.timestamp.isoformat(),
                    "hour_of_day": obs.hour_of_day,
                    "day_of_week": obs.day_of_week,
                    "setpoint": obs.setpoint,
                    "mode": obs.mode,
                }
                for obs in self._observations[-2016:]  # keep last 7 days at 5-min intervals
            ],
        }

        if self._template is not None:
            data["template"] = {
                "weekday_setpoints": {str(k): v for k, v in self._template.weekday_setpoints.items()},
                "weekend_setpoints": {str(k): v for k, v in self._template.weekend_setpoints.items()},
                "weekday_modes": {str(k): v for k, v in self._template.weekday_modes.items()},
                "weekend_modes": {str(k): v for k, v in self._template.weekend_modes.items()},
                "capture_method": self._template.capture_method,
                "capture_date": self._template.capture_date.isoformat() if self._template.capture_date else None,
                "confidence": self._template.confidence,
                "sample_days": self._template.sample_days,
            }

        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BaselineCapture:
        """Restore from persisted data."""
        capture = cls()
        capture._observed_dates = set(data.get("observed_dates", []))

        for item in data.get("observations", []):
            try:
                capture._observations.append(
                    SetpointObservation(
                        timestamp=datetime.fromisoformat(item["timestamp"]),
                        hour_of_day=item["hour_of_day"],
                        day_of_week=item["day_of_week"],
                        setpoint=item["setpoint"],
                        mode=item["mode"],
                    )
                )
            except (KeyError, ValueError):
                continue

        template_data = data.get("template")
        if template_data:
            try:
                capture_date = None
                if template_data.get("capture_date"):
                    capture_date = datetime.fromisoformat(template_data["capture_date"])

                capture._template = BaselineScheduleTemplate(
                    weekday_setpoints={int(k): v for k, v in template_data.get("weekday_setpoints", {}).items()},
                    weekend_setpoints={int(k): v for k, v in template_data.get("weekend_setpoints", {}).items()},
                    weekday_modes={int(k): v for k, v in template_data.get("weekday_modes", {}).items()},
                    weekend_modes={int(k): v for k, v in template_data.get("weekend_modes", {}).items()},
                    capture_method=template_data.get("capture_method", "learning_period"),
                    capture_date=capture_date,
                    confidence=template_data.get("confidence", 0.0),
                    sample_days=template_data.get("sample_days", 0),
                )
            except (KeyError, ValueError) as exc:
                _LOGGER.warning("Failed to restore baseline template: %s", exc)

        return capture
