"""Tests for CalendarOccupancyAdapter — keyword classification, timeline building, caching."""

import importlib
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Module loading (same pattern as test_occupancy.py) ────────────────

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, PROJECT_ROOT)

CC = os.path.join(PROJECT_ROOT, "custom_components", "heatpump_optimizer")

# Create package stubs
pkg = types.ModuleType("custom_components")
pkg.__path__ = [os.path.join(PROJECT_ROOT, "custom_components")]
sys.modules.setdefault("custom_components", pkg)

ho = types.ModuleType("custom_components.heatpump_optimizer")
ho.__path__ = [CC]
sys.modules.setdefault("custom_components.heatpump_optimizer", ho)

adapters = types.ModuleType("custom_components.heatpump_optimizer.adapters")
adapters.__path__ = [os.path.join(CC, "adapters")]
sys.modules.setdefault("custom_components.heatpump_optimizer.adapters", adapters)

engine = types.ModuleType("custom_components.heatpump_optimizer.engine")
engine.__path__ = [os.path.join(CC, "engine")]
sys.modules.setdefault("custom_components.heatpump_optimizer.engine", engine)

# Stub homeassistant.core
ha_mod = types.ModuleType("homeassistant")
ha_mod.__path__ = ["homeassistant"]
sys.modules.setdefault("homeassistant", ha_mod)

ha_core = types.ModuleType("homeassistant.core")
ha_core.HomeAssistant = MagicMock
sys.modules.setdefault("homeassistant.core", ha_core)


def _load(full_name: str, path: str):
    spec = importlib.util.spec_from_file_location(full_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


dt_mod = _load(
    "custom_components.heatpump_optimizer.engine.data_types",
    os.path.join(CC, "engine", "data_types.py"),
)
engine.data_types = dt_mod

cal_mod = _load(
    "custom_components.heatpump_optimizer.adapters.calendar_occupancy",
    os.path.join(CC, "adapters", "calendar_occupancy.py"),
)
adapters.calendar_occupancy = cal_mod

CalendarOccupancyAdapter = cal_mod.CalendarOccupancyAdapter
OccupancyForecastPoint = dt_mod.OccupancyForecastPoint

# ── Helpers ───────────────────────────────────────────────────────────

NOW = datetime(2026, 3, 12, 14, 0, 0, tzinfo=timezone.utc)

HOME_KW = ["wfh", "work from home", "home", "remote"]
AWAY_KW = ["office", "in-person", "on-site", "work"]


def _make_adapter(hass=None, default="home"):
    if hass is None:
        hass = MagicMock()
    return CalendarOccupancyAdapter(
        hass=hass,
        calendar_entity_ids=["calendar.work_location"],
        home_keywords=HOME_KW,
        away_keywords=AWAY_KW,
        default_when_no_event=default,
    )


# ── Tests: Keyword Classification ─────────────────────────────────────


class TestClassifyEvent:
    """Tests for _classify_event keyword matching."""

    def test_away_keyword_in_person(self):
        adapter = _make_adapter()
        assert adapter._classify_event("In-Person") == "away"

    def test_away_keyword_office(self):
        adapter = _make_adapter()
        assert adapter._classify_event("Office day") == "away"

    def test_home_keyword_remote(self):
        adapter = _make_adapter()
        assert adapter._classify_event("Remote") == "home"

    def test_home_keyword_wfh(self):
        adapter = _make_adapter()
        assert adapter._classify_event("WFH") == "home"

    def test_case_insensitive(self):
        adapter = _make_adapter()
        assert adapter._classify_event("IN-PERSON") == "away"
        assert adapter._classify_event("remote") == "home"

    def test_no_match_returns_none(self):
        adapter = _make_adapter()
        assert adapter._classify_event("Doctor appointment") is None

    def test_away_takes_priority_over_home(self):
        """If summary matches both away and home keywords, away wins (checked first)."""
        adapter = _make_adapter()
        # "Work from home" contains "work" (away) — away is checked first
        assert adapter._classify_event("Work from home") == "away"

    def test_partial_match(self):
        adapter = _make_adapter()
        assert adapter._classify_event("Working from office today") == "away"

    def test_empty_summary(self):
        adapter = _make_adapter()
        assert adapter._classify_event("") is None


# ── Tests: Timeline Building ──────────────────────────────────────────


class TestBuildTimeline:
    """Tests for _build_timeline — gap filling, clamping, ordering."""

    def test_single_event_fills_gaps(self):
        adapter = _make_adapter()
        events = [
            {
                "summary": "In-Person",
                "start": (NOW + timedelta(hours=2)).isoformat(),
                "end": (NOW + timedelta(hours=10)).isoformat(),
            }
        ]
        timeline = adapter._build_timeline(events, NOW, hours_ahead=24)

        assert len(timeline) == 3  # gap before + event + gap after
        assert timeline[0].mode == "home"  # default fills gap
        assert timeline[0].source == "calendar_default"
        assert timeline[1].mode == "away"
        assert timeline[1].source == "calendar"
        assert timeline[2].mode == "home"

    def test_contiguous_events_no_gaps(self):
        adapter = _make_adapter()
        mid = NOW + timedelta(hours=8)
        events = [
            {"summary": "In-Person", "start": NOW.isoformat(), "end": mid.isoformat()},
            {"summary": "Remote", "start": mid.isoformat(), "end": (NOW + timedelta(hours=24)).isoformat()},
        ]
        timeline = adapter._build_timeline(events, NOW, hours_ahead=24)

        assert len(timeline) == 2
        assert timeline[0].mode == "away"
        assert timeline[1].mode == "home"

    def test_events_clamped_to_window(self):
        """Events outside the planning window are clamped."""
        adapter = _make_adapter()
        events = [
            {
                "summary": "In-Person",
                "start": (NOW - timedelta(hours=2)).isoformat(),  # before now
                "end": (NOW + timedelta(hours=5)).isoformat(),
            }
        ]
        timeline = adapter._build_timeline(events, NOW, hours_ahead=8)

        # Event starts at NOW (clamped), not 2 hours ago
        assert timeline[0].start_time == NOW
        assert timeline[0].mode == "away"

    def test_unclassifiable_event_skipped(self):
        adapter = _make_adapter()
        events = [
            {
                "summary": "Doctor appointment",
                "start": (NOW + timedelta(hours=1)).isoformat(),
                "end": (NOW + timedelta(hours=2)).isoformat(),
            }
        ]
        timeline = adapter._build_timeline(events, NOW, hours_ahead=8)

        # Only the default gap-fill segment
        assert len(timeline) == 1
        assert timeline[0].mode == "home"

    def test_trailing_gap_filled(self):
        adapter = _make_adapter()
        events = [
            {
                "summary": "Remote",
                "start": NOW.isoformat(),
                "end": (NOW + timedelta(hours=4)).isoformat(),
            }
        ]
        timeline = adapter._build_timeline(events, NOW, hours_ahead=8)

        assert len(timeline) == 2
        assert timeline[1].start_time == NOW + timedelta(hours=4)
        assert timeline[1].mode == "home"  # default

    def test_default_away(self):
        """When default_when_no_event is 'away', gaps filled with away."""
        adapter = _make_adapter(default="away")
        events = []
        timeline = adapter._build_timeline(events, NOW, hours_ahead=8)

        assert len(timeline) == 1
        assert timeline[0].mode == "away"

    def test_events_sorted_by_start(self):
        """Events given out of order are sorted correctly."""
        adapter = _make_adapter()
        events = [
            {"summary": "Remote", "start": (NOW + timedelta(hours=10)).isoformat(), "end": (NOW + timedelta(hours=18)).isoformat()},
            {"summary": "In-Person", "start": (NOW + timedelta(hours=2)).isoformat(), "end": (NOW + timedelta(hours=10)).isoformat()},
        ]
        timeline = adapter._build_timeline(events, NOW, hours_ahead=24)

        modes = [seg.mode for seg in timeline]
        # gap(home) -> away -> home -> gap(home)
        assert modes[0] == "home"  # gap before
        assert modes[1] == "away"  # In-Person
        assert modes[2] == "home"  # Remote

    def test_invalid_datetime_skipped(self):
        adapter = _make_adapter()
        events = [
            {"summary": "In-Person", "start": "not-a-date", "end": (NOW + timedelta(hours=4)).isoformat()},
        ]
        timeline = adapter._build_timeline(events, NOW, hours_ahead=8)
        # Invalid event skipped, only default fill
        assert len(timeline) == 1
        assert timeline[0].mode == "home"


# ── Tests: Transition Detection ───────────────────────────────────────


class TestGetNextTransition:
    def test_away_to_home(self):
        adapter = _make_adapter()
        t1 = NOW
        t2 = NOW + timedelta(hours=8)
        t3 = NOW + timedelta(hours=24)
        timeline = [
            OccupancyForecastPoint(t1, t2, "away", "calendar"),
            OccupancyForecastPoint(t2, t3, "home", "calendar"),
        ]
        result = adapter.get_next_transition(timeline, "away", "home")
        assert result == t2

    def test_no_matching_transition(self):
        adapter = _make_adapter()
        t1 = NOW
        t2 = NOW + timedelta(hours=24)
        timeline = [
            OccupancyForecastPoint(t1, t2, "home", "calendar"),
        ]
        result = adapter.get_next_transition(timeline, "away", "home")
        assert result is None

    def test_home_to_away(self):
        adapter = _make_adapter()
        t1 = NOW
        t2 = NOW + timedelta(hours=6)
        t3 = NOW + timedelta(hours=16)
        timeline = [
            OccupancyForecastPoint(t1, t2, "home", "calendar"),
            OccupancyForecastPoint(t2, t3, "away", "calendar"),
        ]
        result = adapter.get_next_transition(timeline, "home", "away")
        assert result == t2


# ── Tests: Parse Datetime ─────────────────────────────────────────────


class TestParseDatetime:
    def test_iso_string(self):
        result = CalendarOccupancyAdapter._parse_datetime("2026-03-12T14:00:00+00:00")
        assert result == NOW

    def test_naive_datetime_gets_utc(self):
        result = CalendarOccupancyAdapter._parse_datetime("2026-03-12T14:00:00")
        assert result.tzinfo == timezone.utc

    def test_datetime_object(self):
        result = CalendarOccupancyAdapter._parse_datetime(NOW)
        assert result == NOW

    def test_naive_datetime_object(self):
        naive = datetime(2026, 3, 12, 14, 0, 0)
        result = CalendarOccupancyAdapter._parse_datetime(naive)
        assert result.tzinfo == timezone.utc

    def test_none(self):
        assert CalendarOccupancyAdapter._parse_datetime(None) is None

    def test_invalid_string(self):
        assert CalendarOccupancyAdapter._parse_datetime("not-a-date") is None


# ── Tests: Cache ──────────────────────────────────────────────────────


class TestCache:
    def test_cache_returns_same_result(self):
        import asyncio

        hass = MagicMock()
        hass.services.async_call = AsyncMock(
            return_value={
                "calendar.work_location": {
                    "events": [
                        {
                            "summary": "In-Person",
                            "start": NOW.isoformat(),
                            "end": (NOW + timedelta(hours=8)).isoformat(),
                        }
                    ]
                }
            }
        )
        adapter = _make_adapter(hass)

        async def _run():
            r1 = await adapter.async_get_occupancy_timeline()
            r2 = await adapter.async_get_occupancy_timeline()
            return r1, r2

        result1, result2 = asyncio.run(_run())

        # Should only have called the service once (cached on second call)
        assert hass.services.async_call.call_count == 1
        assert result1 is result2

    def test_invalidate_cache(self):
        adapter = _make_adapter()
        adapter._cache_time = NOW
        adapter._cached_timeline = [MagicMock()]
        adapter.invalidate_cache()
        assert adapter._cache_time is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
