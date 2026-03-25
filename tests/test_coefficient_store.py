"""Tests for CoefficientStore — per-home coefficient multipliers."""

from __future__ import annotations

import os
import sys

# Ensure project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.heatpump_optimizer.learning.coefficient_store import (
    ALL_COEFFICIENTS,
    MAX_MULTIPLIER,
    MIN_MULTIPLIER,
    CoefficientStore,
)


class TestCoefficientStoreDefaults:
    """Default multipliers should return original constants unchanged."""

    def test_all_multipliers_default_to_one(self):
        store = CoefficientStore()
        for name in ALL_COEFFICIENTS:
            assert store.get_multiplier(name) == 1.0

    def test_effective_returns_default_when_multiplier_is_one(self):
        store = CoefficientStore()
        assert store.effective("wind_infiltration", 0.025) == 0.025
        assert store.effective("k_attic", 50.0) == 50.0
        assert store.effective("alpha_cool", 0.012) == 0.012

    def test_effective_applies_multiplier(self):
        store = CoefficientStore()
        store.set_multiplier("wind_infiltration", 0.5)
        assert store.effective("wind_infiltration", 0.025) == 0.025 * 0.5

    def test_effective_unknown_coefficient_returns_default(self):
        store = CoefficientStore()
        assert store.effective("nonexistent", 42.0) == 42.0


class TestCoefficientStoreBounds:
    """Multipliers should be clamped to [MIN_MULTIPLIER, MAX_MULTIPLIER]."""

    def test_clamp_below_minimum(self):
        store = CoefficientStore()
        store.set_multiplier("wind_infiltration", 0.01)  # below 0.2
        assert store.get_multiplier("wind_infiltration") == MIN_MULTIPLIER

    def test_clamp_above_maximum(self):
        store = CoefficientStore()
        store.set_multiplier("wind_infiltration", 100.0)  # above 5.0
        assert store.get_multiplier("wind_infiltration") == MAX_MULTIPLIER

    def test_valid_multiplier_not_clamped(self):
        store = CoefficientStore()
        store.set_multiplier("k_attic", 2.3)
        assert store.get_multiplier("k_attic") == 2.3


class TestCoefficientStorePersistence:
    """Round-trip serialization."""

    def test_round_trip(self):
        store = CoefficientStore()
        store.set_multiplier("wind_infiltration", 0.7)
        store.set_multiplier("alpha_cool", 1.3)
        store.set_confidence("wind_infiltration", 0.85)
        store.calibration_count = 5

        data = store.to_dict()
        restored = CoefficientStore.from_dict(data)

        assert restored.get_multiplier("wind_infiltration") == 0.7
        assert restored.get_multiplier("alpha_cool") == 1.3
        assert restored.get_confidence("wind_infiltration") == 0.85
        assert restored.calibration_count == 5
        # Unmodified coefficients should still be 1.0
        assert restored.get_multiplier("k_crawlspace") == 1.0

    def test_from_dict_tolerates_missing_keys(self):
        """Partial data should not crash — missing coefficients default to 1.0."""
        data = {
            "multipliers": {"wind_infiltration": 0.6},
            "confidence": {},
            "calibration_count": 2,
        }
        restored = CoefficientStore.from_dict(data)
        assert restored.get_multiplier("wind_infiltration") == 0.6
        assert restored.get_multiplier("k_attic") == 1.0
        assert restored.calibration_count == 2

    def test_from_dict_clamps_out_of_range_values(self):
        data = {
            "multipliers": {"wind_infiltration": 99.0},
            "confidence": {},
        }
        restored = CoefficientStore.from_dict(data)
        assert restored.get_multiplier("wind_infiltration") == MAX_MULTIPLIER
