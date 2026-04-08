"""Tests for HVAC energy consumption sensors and duct-based aux heat detection."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

_loop = asyncio.new_event_loop()

from conftest import CC, load_module

# Load const for device classes
const_mod = load_module(
    "custom_components.heatpump_optimizer.const",
    os.path.join(CC, "const.py"),
)
DOMAIN = const_mod.DOMAIN

# Stub the coordinator module
sys.modules.setdefault(
    "custom_components.heatpump_optimizer.coordinator", MagicMock()
)

# Load sensor module
sensor_mod = load_module(
    "custom_components.heatpump_optimizer.sensor",
    os.path.join(CC, "sensor.py"),
)

HvacEnergyTodaySensor = sensor_mod.HvacEnergyTodaySensor
HvacEnergyCumulativeSensor = sensor_mod.HvacEnergyCumulativeSensor


# ── Helpers ──────────────────────────────────────────────────────────


def _entry(entry_id="test_entry"):
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _get_class_value(attr):
    """Get the string value from an enum or string attribute."""
    return attr.value if hasattr(attr, "value") else str(attr)


# ═══════════════════════════════════════════════════════════════════
# HVAC Energy Today Sensor
# ═══════════════════════════════════════════════════════════════════


class TestHvacEnergyToday:
    def test_returns_actual_kwh(self, mock_coordinator):
        mock_coordinator.data = {"actual_kwh_today": 12.5}
        sensor = HvacEnergyTodaySensor(mock_coordinator, _entry())
        assert sensor.native_value == pytest.approx(12.5)

    def test_returns_zero_when_no_usage(self, mock_coordinator):
        mock_coordinator.data = {"actual_kwh_today": 0.0}
        sensor = HvacEnergyTodaySensor(mock_coordinator, _entry())
        assert sensor.native_value == pytest.approx(0.0)

    def test_returns_none_when_no_data(self, mock_coordinator):
        mock_coordinator.data = None
        sensor = HvacEnergyTodaySensor(mock_coordinator, _entry())
        assert sensor.native_value is None

    def test_device_class_is_energy(self, mock_coordinator):
        sensor = HvacEnergyTodaySensor(mock_coordinator, _entry())
        assert _get_class_value(sensor._attr_device_class) == "energy"

    def test_state_class_is_total(self, mock_coordinator):
        sensor = HvacEnergyTodaySensor(mock_coordinator, _entry())
        assert _get_class_value(sensor._attr_state_class) == "total"

    def test_unit_is_kwh(self, mock_coordinator):
        sensor = HvacEnergyTodaySensor(mock_coordinator, _entry())
        assert sensor._attr_native_unit_of_measurement == "kWh"


# ═══════════════════════════════════════════════════════════════════
# HVAC Energy Cumulative Sensor
# ═══════════════════════════════════════════════════════════════════


class TestHvacEnergyCumulative:
    def test_returns_cumulative_kwh(self, mock_coordinator):
        mock_coordinator.data = {"actual_kwh_cumulative": 1234.5}
        sensor = HvacEnergyCumulativeSensor(mock_coordinator, _entry())
        assert sensor.native_value == pytest.approx(1234.5)

    def test_returns_zero_initially(self, mock_coordinator):
        mock_coordinator.data = {"actual_kwh_cumulative": 0.0}
        sensor = HvacEnergyCumulativeSensor(mock_coordinator, _entry())
        assert sensor.native_value == pytest.approx(0.0)

    def test_returns_none_when_no_data(self, mock_coordinator):
        mock_coordinator.data = None
        sensor = HvacEnergyCumulativeSensor(mock_coordinator, _entry())
        assert sensor.native_value is None

    def test_device_class_is_energy(self, mock_coordinator):
        sensor = HvacEnergyCumulativeSensor(mock_coordinator, _entry())
        assert _get_class_value(sensor._attr_device_class) == "energy"

    def test_state_class_is_total_increasing(self, mock_coordinator):
        sensor = HvacEnergyCumulativeSensor(mock_coordinator, _entry())
        assert _get_class_value(sensor._attr_state_class) == "total_increasing"

    def test_unit_is_kwh(self, mock_coordinator):
        sensor = HvacEnergyCumulativeSensor(mock_coordinator, _entry())
        assert sensor._attr_native_unit_of_measurement == "kWh"
