"""Tests for binary sensor entities — all 5 sensors, data keys, and attributes."""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import MagicMock

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

# Load binary_sensor module
bs_mod = load_module(
    "custom_components.heatpump_optimizer.binary_sensor",
    os.path.join(CC, "binary_sensor.py"),
)

OptimizerActiveSensor = bs_mod.OptimizerActiveSensor
OverrideDetectedSensor = bs_mod.OverrideDetectedSensor
StaleSensorDetectedSensor = bs_mod.StaleSensorDetectedSensor
AuxHeatActiveSensor = bs_mod.AuxHeatActiveSensor
LearningActiveSensor = bs_mod.LearningActiveSensor


# ── Helpers ──────────────────────────────────────────────────────────


def _entry(entry_id="test_entry"):
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


# ── OptimizerActiveSensor ────────────────────────────────────────────


class TestOptimizerActive:
    def test_is_on_true(self, mock_coordinator):
        mock_coordinator.data = {"active": True}
        sensor = OptimizerActiveSensor(mock_coordinator, _entry())
        assert sensor.is_on is True

    def test_is_on_false(self, mock_coordinator):
        mock_coordinator.data = {"active": False}
        sensor = OptimizerActiveSensor(mock_coordinator, _entry())
        assert sensor.is_on is False

    def test_data_none(self, mock_coordinator):
        mock_coordinator.data = None
        sensor = OptimizerActiveSensor(mock_coordinator, _entry())
        assert sensor.is_on is None

    def test_unique_id(self, mock_coordinator):
        sensor = OptimizerActiveSensor(mock_coordinator, _entry("abc"))
        assert sensor._attr_unique_id == "abc_active"

    def test_device_class(self, mock_coordinator):
        sensor = OptimizerActiveSensor(mock_coordinator, _entry())
        assert sensor._attr_device_class == "running"


# ── OverrideDetectedSensor ───────────────────────────────────────────


class TestOverrideDetected:
    def test_is_on_true(self, mock_coordinator):
        mock_coordinator.data = {"override_detected": True}
        sensor = OverrideDetectedSensor(mock_coordinator, _entry())
        assert sensor.is_on is True

    def test_is_on_false(self, mock_coordinator):
        mock_coordinator.data = {"override_detected": False}
        sensor = OverrideDetectedSensor(mock_coordinator, _entry())
        assert sensor.is_on is False

    def test_data_none(self, mock_coordinator):
        mock_coordinator.data = None
        sensor = OverrideDetectedSensor(mock_coordinator, _entry())
        assert sensor.is_on is None

    def test_unique_id(self, mock_coordinator):
        sensor = OverrideDetectedSensor(mock_coordinator, _entry("abc"))
        assert sensor._attr_unique_id == "abc_override_detected"

    def test_device_class(self, mock_coordinator):
        sensor = OverrideDetectedSensor(mock_coordinator, _entry())
        assert sensor._attr_device_class == "problem"


# ── StaleSensorDetectedSensor ────────────────────────────────────────


class TestStaleSensor:
    def test_is_on_true(self, mock_coordinator):
        mock_coordinator.data = {"sensor_stale": True}
        sensor = StaleSensorDetectedSensor(mock_coordinator, _entry())
        assert sensor.is_on is True

    def test_is_on_false(self, mock_coordinator):
        mock_coordinator.data = {"sensor_stale": False}
        sensor = StaleSensorDetectedSensor(mock_coordinator, _entry())
        assert sensor.is_on is False

    def test_data_none(self, mock_coordinator):
        mock_coordinator.data = None
        sensor = StaleSensorDetectedSensor(mock_coordinator, _entry())
        assert sensor.is_on is None

    def test_unique_id(self, mock_coordinator):
        sensor = StaleSensorDetectedSensor(mock_coordinator, _entry("abc"))
        assert sensor._attr_unique_id == "abc_sensor_stale"


# ── AuxHeatActiveSensor ─────────────────────────────────────────────


class TestAuxHeatActive:
    def test_is_on_true(self, mock_coordinator):
        mock_coordinator.data = {"aux_heat_active": True}
        sensor = AuxHeatActiveSensor(mock_coordinator, _entry())
        assert sensor.is_on is True

    def test_is_on_false(self, mock_coordinator):
        mock_coordinator.data = {"aux_heat_active": False}
        sensor = AuxHeatActiveSensor(mock_coordinator, _entry())
        assert sensor.is_on is False

    def test_data_none(self, mock_coordinator):
        mock_coordinator.data = None
        sensor = AuxHeatActiveSensor(mock_coordinator, _entry())
        assert sensor.is_on is None

    def test_unique_id(self, mock_coordinator):
        sensor = AuxHeatActiveSensor(mock_coordinator, _entry("abc"))
        assert sensor._attr_unique_id == "abc_aux_heat_active"

    def test_device_class(self, mock_coordinator):
        sensor = AuxHeatActiveSensor(mock_coordinator, _entry())
        assert sensor._attr_device_class == "heat"


# ── LearningActiveSensor ────────────────────────────────────────────


class TestLearningActive:
    def test_is_on_true(self, mock_coordinator):
        mock_coordinator.data = {"learning_active": True}
        sensor = LearningActiveSensor(mock_coordinator, _entry())
        assert sensor.is_on is True

    def test_is_on_false(self, mock_coordinator):
        mock_coordinator.data = {"learning_active": False}
        sensor = LearningActiveSensor(mock_coordinator, _entry())
        assert sensor.is_on is False

    def test_data_none(self, mock_coordinator):
        mock_coordinator.data = None
        sensor = LearningActiveSensor(mock_coordinator, _entry())
        assert sensor.is_on is None

    def test_unique_id(self, mock_coordinator):
        sensor = LearningActiveSensor(mock_coordinator, _entry("abc"))
        assert sensor._attr_unique_id == "abc_learning_active"

    def test_extra_state_attributes(self, mock_coordinator):
        mock_coordinator.data = {
            "learning_active": True,
            "kalman_confidence": 0.42,
            "kalman_observations": 200,
            "initialization_mode": "learning",
        }
        sensor = LearningActiveSensor(mock_coordinator, _entry())
        attrs = sensor.extra_state_attributes
        assert attrs["model_confidence"] == 0.42
        assert attrs["observations"] == 200
        assert attrs["initialization_mode"] == "learning"

    def test_extra_state_attributes_data_none(self, mock_coordinator):
        mock_coordinator.data = None
        sensor = LearningActiveSensor(mock_coordinator, _entry())
        assert sensor.extra_state_attributes == {}


# ── async_setup_entry ────────────────────────────────────────────────


class TestSetupEntry:
    def test_creates_all_five_sensors(self, mock_hass, mock_coordinator):
        mock_hass.data[DOMAIN] = {"test_entry": mock_coordinator}
        entry = _entry("test_entry")

        added = []
        async_add_entities = lambda entities: added.extend(entities)

        _loop.run_until_complete(
            bs_mod.async_setup_entry(mock_hass, entry, async_add_entities)
        )

        assert len(added) == 5
        types_found = {type(e).__name__ for e in added}
        assert "OptimizerActiveSensor" in types_found
        assert "OverrideDetectedSensor" in types_found
        assert "StaleSensorDetectedSensor" in types_found
        assert "AuxHeatActiveSensor" in types_found
        assert "LearningActiveSensor" in types_found
