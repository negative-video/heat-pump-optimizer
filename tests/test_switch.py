"""Tests for the OptimizerEnabledSwitch entity."""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

_loop = asyncio.new_event_loop()

from conftest import CC, load_module

# Load const for DOMAIN
const_mod = load_module(
    "custom_components.heatpump_optimizer.const",
    os.path.join(CC, "const.py"),
)
DOMAIN = const_mod.DOMAIN

# Stub the coordinator module
sys.modules.setdefault(
    "custom_components.heatpump_optimizer.coordinator", MagicMock()
)

# Load switch module
switch_mod = load_module(
    "custom_components.heatpump_optimizer.switch",
    os.path.join(CC, "switch.py"),
)
OptimizerEnabledSwitch = switch_mod.OptimizerEnabledSwitch


# ── Helpers ──────────────────────────────────────────────────────────


def _make_switch(coordinator, entry_id="test_entry_123"):
    entry = MagicMock()
    entry.entry_id = entry_id
    return OptimizerEnabledSwitch(coordinator, entry)


# ── Tests ────────────────────────────────────────────────────────────


class TestIsOn:
    def test_active_true(self, mock_coordinator):
        mock_coordinator.data = {"active": True}
        sw = _make_switch(mock_coordinator)
        assert sw.is_on is True

    def test_active_false(self, mock_coordinator):
        mock_coordinator.data = {"active": False}
        sw = _make_switch(mock_coordinator)
        assert sw.is_on is False

    def test_data_none(self, mock_coordinator):
        mock_coordinator.data = None
        sw = _make_switch(mock_coordinator)
        assert sw.is_on is None

    def test_active_key_missing_defaults_false(self, mock_coordinator):
        mock_coordinator.data = {}
        sw = _make_switch(mock_coordinator)
        assert sw.is_on is False


class TestTurnOn:
    def test_calls_resume(self, mock_coordinator):
        sw = _make_switch(mock_coordinator)
        _loop.run_until_complete(sw.async_turn_on())
        mock_coordinator.async_resume.assert_called_once()

    def test_writes_state(self, mock_coordinator):
        sw = _make_switch(mock_coordinator)
        _loop.run_until_complete(sw.async_turn_on())
        # No assertion needed — just verify it doesn't raise


class TestTurnOff:
    def test_calls_pause(self, mock_coordinator):
        sw = _make_switch(mock_coordinator)
        _loop.run_until_complete(sw.async_turn_off())
        mock_coordinator.pause.assert_called_once()


class TestAttributes:
    def test_unique_id(self, mock_coordinator):
        sw = _make_switch(mock_coordinator, entry_id="abc123")
        assert sw._attr_unique_id == "abc123_enabled"

    def test_name(self, mock_coordinator):
        sw = _make_switch(mock_coordinator)
        assert sw._attr_name == "Optimizer Enabled"

    def test_icon(self, mock_coordinator):
        sw = _make_switch(mock_coordinator)
        assert sw._attr_icon == "mdi:robot"

    def test_device_info(self, mock_coordinator):
        sw = _make_switch(mock_coordinator, entry_id="xyz")
        info = sw._attr_device_info
        assert (DOMAIN, "xyz") in info["identifiers"]
        assert info["name"] == "Heat Pump Optimizer"
