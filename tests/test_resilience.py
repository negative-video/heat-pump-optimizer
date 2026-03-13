"""Tests for resilience hardening features.

Covers:
- Multi-weather entity fallback (async_get_forecast_multi)
- Outdoor humidity last-known cache fallback
- Power entity fallback to default watts
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.heatpump_optimizer.adapters.sensor_hub import SensorHub


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════


def _make_state(value, attrs=None):
    """Create a mock HA state."""
    state = SimpleNamespace()
    state.state = str(value)
    state.attributes = attrs or {}
    state.last_updated = datetime.now(timezone.utc)
    return state


def _make_hass(states=None):
    """Create a minimal hass mock."""
    hass = MagicMock()
    state_dict = states or {}
    hass.states.get = lambda eid: state_dict.get(eid)
    hass.config.units.wind_speed_unit = "mph"
    return hass


def _make_hub(hass, **kwargs):
    return SensorHub(hass, **kwargs)


# ═══════════════════════════════════════════════════════════════════
# Multi-Weather Fallback Tests
# ═══════════════════════════════════════════════════════════════════


def _try_import_forecast():
    """Try to import forecast module; returns None if HA deps unavailable."""
    try:
        from custom_components.heatpump_optimizer.adapters import forecast
        return forecast
    except (ImportError, ModuleNotFoundError):
        return None


_forecast_mod = _try_import_forecast()
_skip_forecast = pytest.mark.skipif(
    _forecast_mod is None,
    reason="Forecast module requires full HA component dependencies",
)


@_skip_forecast
class TestAsyncGetForecastMulti:
    """async_get_forecast_multi: primary succeeds, primary fails → fallback, all fail."""

    @pytest.mark.asyncio
    async def test_primary_succeeds(self):
        with patch.object(
            _forecast_mod, "async_get_forecast",
            new_callable=AsyncMock,
            return_value=[SimpleNamespace(time=datetime.now(timezone.utc), outdoor_temp=72.0)],
        ) as mock_get:
            points, source = await _forecast_mod.async_get_forecast_multi(
                MagicMock(), ["weather.primary", "weather.fallback"]
            )
            assert len(points) == 1
            assert source == "weather.primary"
            mock_get.assert_called_once()

    @pytest.mark.asyncio
    async def test_primary_fails_fallback_succeeds(self):
        async def mock_get(hass, entity_id):
            if entity_id == "weather.primary":
                return []
            return [SimpleNamespace(time=datetime.now(timezone.utc), outdoor_temp=72.0)]

        with patch.object(_forecast_mod, "async_get_forecast", side_effect=mock_get):
            points, source = await _forecast_mod.async_get_forecast_multi(
                MagicMock(), ["weather.primary", "weather.fallback"]
            )
            assert len(points) == 1
            assert source == "weather.fallback"

    @pytest.mark.asyncio
    async def test_all_fail(self):
        with patch.object(
            _forecast_mod, "async_get_forecast",
            new_callable=AsyncMock, return_value=[],
        ):
            points, source = await _forecast_mod.async_get_forecast_multi(
                MagicMock(), ["weather.a", "weather.b"]
            )
            assert points == []
            assert source is None

    @pytest.mark.asyncio
    async def test_single_entity(self):
        with patch.object(
            _forecast_mod, "async_get_forecast",
            new_callable=AsyncMock,
            return_value=[SimpleNamespace(time=datetime.now(timezone.utc), outdoor_temp=72.0)],
        ):
            points, source = await _forecast_mod.async_get_forecast_multi(
                MagicMock(), ["weather.only"]
            )
            assert len(points) == 1
            assert source == "weather.only"

    @pytest.mark.asyncio
    async def test_empty_list(self):
        points, source = await _forecast_mod.async_get_forecast_multi(MagicMock(), [])
        assert points == []
        assert source is None


# ═══════════════════════════════════════════════════════════════════
# Outdoor Humidity Last-Known Cache Tests
# ═══════════════════════════════════════════════════════════════════


class TestOutdoorHumidityLastKnown:
    """read_outdoor_humidity should fall back to last-known value."""

    def test_fresh_reading_cached(self):
        hass = _make_hass({"sensor.h": _make_state(55.0)})
        hub = _make_hub(hass, outdoor_humidity_entities=["sensor.h"])
        reading = hub.read_outdoor_humidity()
        assert reading is not None
        assert reading.value == pytest.approx(55.0)
        assert not reading.stale

    def test_last_known_fallback_after_all_fail(self):
        hass = _make_hass({"sensor.h": _make_state(55.0)})
        hub = _make_hub(hass, outdoor_humidity_entities=["sensor.h"])

        # First read — caches the value
        reading1 = hub.read_outdoor_humidity()
        assert reading1 is not None

        # Make sensor unavailable
        hass.states.get = lambda eid: _make_state("unavailable")

        # Second read — should fall back to last-known
        reading2 = hub.read_outdoor_humidity()
        assert reading2 is not None
        assert reading2.value == pytest.approx(55.0)
        assert reading2.stale
        assert reading2.source == "last_known"

    def test_no_last_known_returns_none(self):
        hass = _make_hass({})
        hub = _make_hub(hass)
        reading = hub.read_outdoor_humidity()
        assert reading is None


# ═══════════════════════════════════════════════════════════════════
# Power Entity Fallback Tests
# ═══════════════════════════════════════════════════════════════════


class TestPowerEntityFallback:
    """read_power_draw: entity unavailable should fall back to default watts."""

    def test_entity_available_uses_entity(self):
        hass = _make_hass({"sensor.pw": _make_state(5000)})
        hub = _make_hub(hass, power_entity="sensor.pw")
        assert hub.read_power_draw() == pytest.approx(5000.0)

    def test_entity_unavailable_falls_back(self):
        hass = _make_hass({"sensor.pw": _make_state("unavailable")})
        hub = _make_hub(hass, power_entity="sensor.pw", power_default_watts=3500.0)
        assert hub.read_power_draw() == pytest.approx(3500.0)

    def test_entity_unavailable_no_default(self):
        hass = _make_hass({"sensor.pw": _make_state("unavailable")})
        hub = _make_hub(hass, power_entity="sensor.pw", power_default_watts=0.0)
        assert hub.read_power_draw() is None

    def test_no_entity_uses_default(self):
        hass = _make_hass({})
        hub = _make_hub(hass, power_default_watts=4000.0)
        assert hub.read_power_draw() == pytest.approx(4000.0)
