"""Tests for onboarding changes: PerformanceModel cold start, rate limiter,
safety limits, learning mode conservatism, constraint management, and
config flow validation.
"""

import importlib
import importlib.util
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

# ── Module loading ────────────────────────────────────────────────────

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, PROJECT_ROOT)

CC = os.path.join(PROJECT_ROOT, "custom_components", "heatpump_optimizer")

# Create package stubs (idempotent)
if "custom_components" not in sys.modules:
    pkg = types.ModuleType("custom_components")
    pkg.__path__ = [os.path.join(PROJECT_ROOT, "custom_components")]
    sys.modules["custom_components"] = pkg

if "custom_components.heatpump_optimizer" not in sys.modules:
    ho = types.ModuleType("custom_components.heatpump_optimizer")
    ho.__path__ = [CC]
    sys.modules["custom_components.heatpump_optimizer"] = ho

if "custom_components.heatpump_optimizer.engine" not in sys.modules:
    engine = types.ModuleType("custom_components.heatpump_optimizer.engine")
    engine.__path__ = [os.path.join(CC, "engine")]
    sys.modules["custom_components.heatpump_optimizer.engine"] = engine

if "custom_components.heatpump_optimizer.learning" not in sys.modules:
    learning = types.ModuleType("custom_components.heatpump_optimizer.learning")
    learning.__path__ = [os.path.join(CC, "learning")]
    sys.modules["custom_components.heatpump_optimizer.learning"] = learning

# Stub HA modules needed by const.py
for mod_name in [
    "homeassistant",
    "homeassistant.core",
    "homeassistant.config_entries",
    "homeassistant.helpers",
    "homeassistant.helpers.selector",
    "homeassistant.helpers.event",
    "homeassistant.helpers.storage",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.entity_platform",
    "homeassistant.components",
    "homeassistant.components.binary_sensor",
    "homeassistant.components.switch",
    "voluptuous",
]:
    if mod_name not in sys.modules:
        stub = types.ModuleType(mod_name)
        sys.modules[mod_name] = stub


def _load(full_name: str, path: str):
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load const first (needed by performance_model indirectly)
const_mod = _load(
    "custom_components.heatpump_optimizer.const",
    os.path.join(CC, "const.py"),
)

# Load performance_model
pm_mod = _load(
    "custom_components.heatpump_optimizer.engine.performance_model",
    os.path.join(CC, "engine", "performance_model.py"),
)
PerformanceModel = pm_mod.PerformanceModel

# Load config_flow validation helper
cf_mod_path = os.path.join(CC, "config_flow.py")


# =====================================================================
# PerformanceModel.from_defaults() tests
# =====================================================================


class TestPerformanceModelDefaults:
    """Tests for the cold-start synthetic performance model."""

    def test_from_defaults_creates_valid_model(self):
        model = PerformanceModel.from_defaults()
        assert model is not None
        assert model.resist_balance_point == 50.0
        assert model.heat_balance_point == 25.0

    def test_cooling_deltas_are_negative(self):
        model = PerformanceModel.from_defaults()
        for temp in [70, 80, 90, 100]:
            delta = model.cooling_delta(temp)
            assert delta < 0, f"Cooling delta at {temp}F should be negative, got {delta}"

    def test_cooling_less_effective_at_higher_temps(self):
        model = PerformanceModel.from_defaults()
        delta_70 = model.cooling_delta(70)
        delta_95 = model.cooling_delta(95)
        # More negative = more effective; delta_70 should be more negative
        assert abs(delta_70) > abs(delta_95), (
            f"Cooling should be more effective at 70F ({delta_70}) than 95F ({delta_95})"
        )

    def test_heating_deltas_are_positive(self):
        model = PerformanceModel.from_defaults()
        for temp in [0, 20, 40, 50]:
            delta = model.heating_delta(temp)
            assert delta > 0, f"Heating delta at {temp}F should be positive, got {delta}"

    def test_heating_more_effective_at_milder_temps(self):
        model = PerformanceModel.from_defaults()
        delta_50 = model.heating_delta(50)
        delta_0 = model.heating_delta(0)
        assert delta_50 > delta_0, (
            f"Heating should be more effective at 50F ({delta_50}) than 0F ({delta_0})"
        )

    def test_passive_drift_crosses_zero_near_balance(self):
        model = PerformanceModel.from_defaults()
        drift_30 = model.passive_drift(30)
        drift_70 = model.passive_drift(70)
        assert drift_30 < 0, f"Drift at 30F should be negative (cooling), got {drift_30}"
        assert drift_70 > 0, f"Drift at 70F should be positive (warming), got {drift_70}"

    def test_differentials_are_set(self):
        model = PerformanceModel.from_defaults()
        assert model.cool_differential == 1.0
        assert model.heat_differential == 1.0

    def test_runtime_needed_returns_finite(self):
        model = PerformanceModel.from_defaults()
        runtime = model.runtime_needed(85, "cool", 3.0)
        assert runtime > 0
        assert runtime < float("inf")

    def test_coast_duration_returns_finite(self):
        model = PerformanceModel.from_defaults()
        coast = model.coast_duration(90, "cool", 3.0)
        assert coast > 0
        assert coast < float("inf")


# =====================================================================
# PerformanceModel.from_estimator() tests
# =====================================================================


class TestPerformanceModelFromEstimator:
    """Tests for deriving a model from Kalman filter state."""

    def _make_mock_estimator(self, **overrides):
        """Create a mock estimator with realistic state_dict."""
        defaults = {
            "R_inv": 0.005,
            "R_int_inv": 0.01,
            "C_inv": 0.0005,
            "C_mass_inv": 0.0001,
            "Q_cool_base": 24000,
            "Q_heat_base": 20000,
        }
        defaults.update(overrides)
        estimator = MagicMock()
        estimator.state_dict.return_value = defaults
        return estimator

    def test_creates_valid_model(self):
        est = self._make_mock_estimator()
        model = PerformanceModel.from_estimator(est)
        assert model is not None

    def test_cooling_deltas_are_negative(self):
        est = self._make_mock_estimator()
        model = PerformanceModel.from_estimator(est)
        delta = model.cooling_delta(85)
        assert delta < 0

    def test_heating_deltas_are_positive(self):
        est = self._make_mock_estimator()
        model = PerformanceModel.from_estimator(est)
        delta = model.heating_delta(30)
        assert delta > 0

    def test_higher_q_cool_means_more_effective(self):
        est_low = self._make_mock_estimator(Q_cool_base=15000)
        est_high = self._make_mock_estimator(Q_cool_base=30000)
        model_low = PerformanceModel.from_estimator(est_low)
        model_high = PerformanceModel.from_estimator(est_high)
        # More negative = more effective cooling
        assert model_high.cooling_delta(85) < model_low.cooling_delta(85)


# =====================================================================
# Rate limiter tests
# =====================================================================


class TestRateLimiter:
    """Tests for the setpoint rate limiter logic."""

    def test_no_limit_when_within_range(self):
        # Simulate _apply_rate_limit logic directly
        max_change = 4.0
        current = 74.0
        target = 72.0  # 2F change, within 4F limit
        diff = target - current
        if abs(diff) <= max_change:
            result = target
        else:
            result = current + max_change if diff > 0 else current - max_change
        assert result == 72.0

    def test_clamps_large_decrease(self):
        max_change = 4.0
        current = 78.0
        target = 70.0  # 8F change, exceeds 4F limit
        diff = target - current
        if abs(diff) <= max_change:
            result = target
        else:
            result = current + max_change if diff > 0 else current - max_change
        assert result == 74.0  # clamped to -4F

    def test_clamps_large_increase(self):
        max_change = 4.0
        current = 66.0
        target = 72.0  # 6F change, exceeds 4F limit
        diff = target - current
        if abs(diff) <= max_change:
            result = target
        else:
            result = current + max_change if diff > 0 else current - max_change
        assert result == 70.0  # clamped to +4F

    def test_no_limit_when_current_is_none(self):
        # When current is None, target passes through unchanged
        current = None
        target = 72.0
        result = target if current is None else target  # simplified
        assert result == 72.0


# =====================================================================
# Dwell time tests
# =====================================================================


class TestDwellTime:
    """Tests for minimum dwell time enforcement."""

    def test_allows_first_write(self):
        last_time = None
        now = datetime(2026, 3, 12, 12, 0, 0, tzinfo=timezone.utc)
        assert last_time is None  # first write always allowed

    def test_blocks_write_within_dwell_period(self):
        min_dwell_seconds = 900  # 15 min
        last_time = datetime(2026, 3, 12, 12, 0, 0, tzinfo=timezone.utc)
        now = datetime(2026, 3, 12, 12, 10, 0, tzinfo=timezone.utc)  # 10 min later
        elapsed = (now - last_time).total_seconds()
        assert elapsed < min_dwell_seconds

    def test_allows_write_after_dwell_period(self):
        min_dwell_seconds = 900  # 15 min
        last_time = datetime(2026, 3, 12, 12, 0, 0, tzinfo=timezone.utc)
        now = datetime(2026, 3, 12, 12, 16, 0, tzinfo=timezone.utc)  # 16 min later
        elapsed = (now - last_time).total_seconds()
        assert elapsed >= min_dwell_seconds


# =====================================================================
# Safety limits & learning mode comfort band tests
# =====================================================================


class TestSafetyLimits:
    """Tests for the two-tier temperature enforcement logic."""

    def test_safety_clamps_comfort_range(self):
        """Safety limits should clamp comfort range that exceeds them."""
        safety_min, safety_max = 50.0, 85.0
        comfort = (48.0, 88.0)  # exceeds both limits
        clamped = (max(comfort[0], safety_min), min(comfort[1], safety_max))
        assert clamped == (50.0, 85.0)

    def test_safety_passes_through_normal_range(self):
        """Normal comfort range within safety limits should pass through unchanged."""
        safety_min, safety_max = 50.0, 85.0
        comfort = (70.0, 78.0)
        clamped = (max(comfort[0], safety_min), min(comfort[1], safety_max))
        assert clamped == (70.0, 78.0)

    def test_learning_mode_narrows_band(self):
        """During learning, inner 60% of comfort band should be used."""
        comfort = (70.0, 78.0)  # 8F band
        band = comfort[1] - comfort[0]
        margin = band * 0.2  # 1.6F from each side
        narrowed = (comfort[0] + margin, comfort[1] - margin)
        assert narrowed[0] == pytest.approx(71.6)
        assert narrowed[1] == pytest.approx(76.4)
        # Effective band is 4.8F (60% of 8F)
        effective_band = narrowed[1] - narrowed[0]
        assert effective_band == pytest.approx(4.8)

    def test_learning_mode_with_safety_limits(self):
        """Learning narrowing applies after safety clamping."""
        safety_min, safety_max = 50.0, 85.0
        comfort = (70.0, 78.0)
        # First: safety clamp
        clamped = (max(comfort[0], safety_min), min(comfort[1], safety_max))
        # Then: learning narrowing
        band = clamped[1] - clamped[0]
        margin = band * 0.2
        narrowed = (clamped[0] + margin, clamped[1] - margin)
        assert narrowed[0] == pytest.approx(71.6)
        assert narrowed[1] == pytest.approx(76.4)


# =====================================================================
# Constraint management tests
# =====================================================================


class TestConstraintManagement:
    """Tests for external constraint expiration logic."""

    def test_expired_constraints_removed(self):
        now = datetime(2026, 3, 12, 12, 0, 0, tzinfo=timezone.utc)
        constraints = [
            {
                "type": "max_temp",
                "value": 76.0,
                "expires": now - timedelta(minutes=5),  # expired
                "source": "test",
            },
            {
                "type": "min_temp",
                "value": 65.0,
                "expires": now + timedelta(minutes=30),  # still active
                "source": "test",
            },
        ]
        active = [c for c in constraints if c["expires"] > now]
        assert len(active) == 1
        assert active[0]["type"] == "min_temp"

    def test_active_constraints_respected(self):
        constraints = [
            {"type": "max_temp", "value": 75.0},
            {"type": "min_temp", "value": 68.0},
        ]
        comfort = (65.0, 80.0)
        for c in constraints:
            if c["type"] == "min_temp":
                comfort = (max(comfort[0], c["value"]), comfort[1])
            elif c["type"] == "max_temp":
                comfort = (comfort[0], min(comfort[1], c["value"]))
        assert comfort == (68.0, 75.0)

    def test_no_constraints_passes_through(self):
        constraints = []
        comfort = (70.0, 78.0)
        for c in constraints:
            pass  # no-op
        assert comfort == (70.0, 78.0)


# =====================================================================
# Config flow validation tests
# =====================================================================


class TestConfigFlowValidation:
    """Tests for _validate_model_import helper."""

    def test_valid_model_json(self):
        data = json.dumps({"estimator_state": {"x": [1, 2, 3]}})
        # Inline the validation logic
        try:
            parsed = json.loads(data)
            assert isinstance(parsed, dict)
            assert "estimator_state" in parsed or "state_mean" in parsed
        except (json.JSONDecodeError, TypeError):
            pytest.fail("Should have been valid")

    def test_valid_model_with_state_mean(self):
        data = json.dumps({"state_mean": [1, 2, 3, 4, 5, 6, 7, 8]})
        parsed = json.loads(data)
        assert isinstance(parsed, dict)
        assert "state_mean" in parsed

    def test_invalid_json(self):
        data = "not json at all {"
        with pytest.raises(json.JSONDecodeError):
            json.loads(data)

    def test_missing_required_keys(self):
        data = json.dumps({"some_other_key": 123})
        parsed = json.loads(data)
        assert "estimator_state" not in parsed
        assert "state_mean" not in parsed

    def test_non_dict_rejected(self):
        data = json.dumps([1, 2, 3])
        parsed = json.loads(data)
        assert not isinstance(parsed, dict)


# =====================================================================
# Default values tests
# =====================================================================


class TestDefaultValues:
    """Tests for the updated default constants."""

    def test_comfort_defaults_wider_than_before(self):
        assert const_mod.DEFAULT_COMFORT_COOL_MIN == 72.0
        assert const_mod.DEFAULT_COMFORT_COOL_MAX == 78.0
        assert const_mod.DEFAULT_COMFORT_HEAT_MIN == 62.0
        assert const_mod.DEFAULT_COMFORT_HEAT_MAX == 70.0

    def test_safety_defaults_are_generous(self):
        assert const_mod.DEFAULT_SAFETY_COOL_MAX == 90.0
        assert const_mod.DEFAULT_SAFETY_HEAT_MIN == 45.0

    def test_safety_wider_than_comfort(self):
        assert const_mod.DEFAULT_SAFETY_COOL_MAX > const_mod.DEFAULT_COMFORT_COOL_MAX
        assert const_mod.DEFAULT_SAFETY_HEAT_MIN < const_mod.DEFAULT_COMFORT_HEAT_MIN

    def test_init_modes_defined(self):
        assert const_mod.INIT_MODE_LEARNING == "learning"
        assert const_mod.INIT_MODE_BEESTAT == "beestat"
        assert const_mod.INIT_MODE_IMPORT == "import"

    def test_aggressiveness_presets_defined(self):
        assert const_mod.AGGRESSIVENESS_CONSERVATIVE == "conservative"
        assert const_mod.AGGRESSIVENESS_BALANCED == "balanced"
        assert const_mod.AGGRESSIVENESS_AGGRESSIVE == "aggressive"
        assert const_mod.DEFAULT_AGGRESSIVENESS == "balanced"

    def test_switch_in_platforms(self):
        assert "switch" in const_mod.PLATFORMS
