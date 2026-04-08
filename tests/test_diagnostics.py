"""Tests for the diagnostics platform."""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import MagicMock

import pytest

from conftest import CC, load_module

# Stub coordinator module so diagnostics.py can import from the package
sys.modules.setdefault(
    "custom_components.heatpump_optimizer.coordinator", MagicMock()
)

const_mod = load_module(
    "custom_components.heatpump_optimizer.const",
    os.path.join(CC, "const.py"),
)
DOMAIN = const_mod.DOMAIN

diag_mod = load_module(
    "custom_components.heatpump_optimizer.diagnostics",
    os.path.join(CC, "diagnostics.py"),
)

_loop = asyncio.new_event_loop()


def _run(coro):
    return _loop.run_until_complete(coro)


def _make_entry(data=None, options=None):
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = data or {"climate_entity": "climate.thermostat", "weather_entity": "weather.home"}
    entry.options = options or {"comfort_min": 68, "comfort_max": 74}
    return entry


_DEFAULT_DATA = {"phase": "idle", "target_setpoint": 72}
_SENTINEL = object()


def _make_coordinator(data=_SENTINEL, export=None):
    coord = MagicMock()
    coord.data = _DEFAULT_DATA if data is _SENTINEL else data
    coord.export_model.return_value = export or {"confidence": 85.0, "observations": 200}
    return coord


class TestDiagnostics:
    def test_returns_expected_structure(self):
        hass = MagicMock()
        entry = _make_entry()
        coordinator = _make_coordinator()
        hass.data = {DOMAIN: {entry.entry_id: coordinator}}

        result = _run(diag_mod.async_get_config_entry_diagnostics(hass, entry))

        assert "version" in result
        assert "config_entry" in result
        assert "coordinator_data" in result
        assert "learned_model" in result
        assert result["version"] == const_mod.VERSION

    def test_config_entry_data_included(self):
        hass = MagicMock()
        entry = _make_entry()
        coordinator = _make_coordinator()
        hass.data = {DOMAIN: {entry.entry_id: coordinator}}

        result = _run(diag_mod.async_get_config_entry_diagnostics(hass, entry))

        assert result["config_entry"]["data"]["climate_entity"] == "climate.thermostat"
        assert result["config_entry"]["options"]["comfort_min"] == 68

    def test_redacts_sensitive_keys(self):
        hass = MagicMock()
        entry = _make_entry(data={"climate_entity": "climate.t", "password": "secret"})
        coordinator = _make_coordinator()
        hass.data = {DOMAIN: {entry.entry_id: coordinator}}

        result = _run(diag_mod.async_get_config_entry_diagnostics(hass, entry))

        assert result["config_entry"]["data"]["password"] == "**REDACTED**"
        assert result["config_entry"]["data"]["climate_entity"] == "climate.t"

    def test_coordinator_data_included(self):
        hass = MagicMock()
        entry = _make_entry()
        coordinator = _make_coordinator(data={"phase": "heating", "setpoint": 70})
        hass.data = {DOMAIN: {entry.entry_id: coordinator}}

        result = _run(diag_mod.async_get_config_entry_diagnostics(hass, entry))

        assert result["coordinator_data"]["phase"] == "heating"

    def test_learned_model_included(self):
        hass = MagicMock()
        entry = _make_entry()
        coordinator = _make_coordinator(export={"confidence": 92.5})
        hass.data = {DOMAIN: {entry.entry_id: coordinator}}

        result = _run(diag_mod.async_get_config_entry_diagnostics(hass, entry))

        assert result["learned_model"]["confidence"] == 92.5

    def test_coordinator_data_none_handled(self):
        hass = MagicMock()
        entry = _make_entry()
        coordinator = _make_coordinator(data=None)
        hass.data = {DOMAIN: {entry.entry_id: coordinator}}

        result = _run(diag_mod.async_get_config_entry_diagnostics(hass, entry))

        assert result["coordinator_data"] == {}

    def test_export_model_error_handled(self):
        hass = MagicMock()
        entry = _make_entry()
        coordinator = _make_coordinator()
        coordinator.export_model.side_effect = RuntimeError("boom")
        hass.data = {DOMAIN: {entry.entry_id: coordinator}}

        result = _run(diag_mod.async_get_config_entry_diagnostics(hass, entry))

        assert result["learned_model"] == {"error": "unavailable"}

    def test_coordinator_data_error_handled(self):
        hass = MagicMock()
        entry = _make_entry()
        coordinator = MagicMock()
        type(coordinator).data = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        coordinator.export_model.return_value = {"confidence": 50}
        hass.data = {DOMAIN: {entry.entry_id: coordinator}}

        result = _run(diag_mod.async_get_config_entry_diagnostics(hass, entry))

        assert result["coordinator_data"] == {"error": "unavailable"}
        assert result["learned_model"]["confidence"] == 50
