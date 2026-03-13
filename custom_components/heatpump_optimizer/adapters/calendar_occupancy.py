"""Calendar-based occupancy adapter — derives future occupancy from HA calendar events.

Reads events from a Home Assistant calendar entity (e.g., a "work location" calendar)
and builds a timeline of predicted occupancy states for the next 24-48 hours. This
enables the optimizer to use different comfort bands at different hours and plan
pre-conditioning for occupancy transitions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant

from ..engine.data_types import OccupancyForecastPoint

_LOGGER = logging.getLogger(__name__)

# Cache duration for calendar fetches
_CACHE_SECONDS = 900  # 15 minutes


class CalendarOccupancyAdapter:
    """Build an occupancy timeline from Home Assistant calendar events."""

    def __init__(
        self,
        hass: HomeAssistant,
        calendar_entity_id: str,
        home_keywords: list[str],
        away_keywords: list[str],
        default_when_no_event: str = "home",
    ):
        self.hass = hass
        self.calendar_entity_id = calendar_entity_id
        self.home_keywords = [kw.lower() for kw in home_keywords]
        self.away_keywords = [kw.lower() for kw in away_keywords]
        self.default_when_no_event = default_when_no_event

        self._cached_timeline: list[OccupancyForecastPoint] = []
        self._cache_time: datetime | None = None

    async def async_get_occupancy_timeline(
        self,
        hours_ahead: int = 48,
    ) -> list[OccupancyForecastPoint]:
        """Fetch calendar events and build an occupancy timeline.

        Returns a list of OccupancyForecastPoint covering the next `hours_ahead`
        hours. Gaps between events are filled with the default mode.
        Results are cached for 15 minutes.
        """
        now = datetime.now(timezone.utc)

        # Return cached result if fresh
        if (
            self._cache_time is not None
            and (now - self._cache_time).total_seconds() < _CACHE_SECONDS
            and self._cached_timeline
        ):
            return self._cached_timeline

        events = await self._fetch_events(now, hours_ahead)
        if events is None:
            _LOGGER.warning(
                "Calendar %s unavailable — returning empty timeline",
                self.calendar_entity_id,
            )
            return []

        timeline = self._build_timeline(events, now, hours_ahead)
        self._cached_timeline = timeline
        self._cache_time = now
        return timeline

    def get_next_transition(
        self,
        timeline: list[OccupancyForecastPoint],
        from_mode: str,
        to_mode: str,
    ) -> datetime | None:
        """Find the next transition from `from_mode` to `to_mode` in the timeline.

        Returns the start_time of the first segment matching `to_mode` that is
        immediately preceded by a segment matching `from_mode`, or None.
        """
        for i in range(1, len(timeline)):
            if timeline[i - 1].mode == from_mode and timeline[i].mode == to_mode:
                return timeline[i].start_time
        return None

    def invalidate_cache(self) -> None:
        """Force a fresh calendar fetch on next call."""
        self._cache_time = None

    async def _fetch_events(
        self,
        now: datetime,
        hours_ahead: int,
    ) -> list[dict[str, Any]] | None:
        """Fetch events from the HA calendar entity."""
        end_time = now + timedelta(hours=hours_ahead)
        try:
            result = await self.hass.services.async_call(
                "calendar",
                "get_events",
                {
                    "entity_id": self.calendar_entity_id,
                    "start_date_time": now.isoformat(),
                    "end_date_time": end_time.isoformat(),
                },
                blocking=True,
                return_response=True,
            )
            if result and self.calendar_entity_id in result:
                return result[self.calendar_entity_id].get("events", [])
        except Exception:
            _LOGGER.debug(
                "Failed to fetch events from %s",
                self.calendar_entity_id,
                exc_info=True,
            )
        return None

    def _build_timeline(
        self,
        events: list[dict[str, Any]],
        now: datetime,
        hours_ahead: int,
    ) -> list[OccupancyForecastPoint]:
        """Convert calendar events into a contiguous occupancy timeline.

        Events are matched against home/away keywords. Gaps between events
        are filled with the default mode.
        """
        horizon = now + timedelta(hours=hours_ahead)

        # Parse and classify events
        classified: list[tuple[datetime, datetime, str]] = []
        for event in events:
            start = self._parse_datetime(event.get("start"))
            end = self._parse_datetime(event.get("end"))
            if start is None or end is None:
                continue

            mode = self._classify_event(event.get("summary", ""))
            if mode is not None:
                classified.append((start, end, mode))

        # Sort by start time
        classified.sort(key=lambda x: x[0])

        # Build contiguous timeline with gap filling
        timeline: list[OccupancyForecastPoint] = []
        cursor = now

        for start, end, mode in classified:
            # Clamp to our window
            seg_start = max(start, now)
            seg_end = min(end, horizon)
            if seg_start >= seg_end:
                continue

            # Fill gap before this event with default mode
            if cursor < seg_start:
                timeline.append(
                    OccupancyForecastPoint(
                        start_time=cursor,
                        end_time=seg_start,
                        mode=self.default_when_no_event,
                        source="calendar_default",
                    )
                )

            timeline.append(
                OccupancyForecastPoint(
                    start_time=seg_start,
                    end_time=seg_end,
                    mode=mode,
                    source="calendar",
                )
            )
            cursor = seg_end

        # Fill trailing gap to horizon
        if cursor < horizon:
            timeline.append(
                OccupancyForecastPoint(
                    start_time=cursor,
                    end_time=horizon,
                    mode=self.default_when_no_event,
                    source="calendar_default",
                )
            )

        return timeline

    def _classify_event(self, summary: str) -> str | None:
        """Match event summary against home/away keywords.

        Returns "home", "away", or None if no keyword matches.
        """
        lower = summary.lower()

        for kw in self.away_keywords:
            if kw in lower:
                return "away"

        for kw in self.home_keywords:
            if kw in lower:
                return "home"

        return None

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        """Parse a datetime from a calendar event field."""
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                pass
        return None
