"""Tests for apparent temperature (heat index) and humidity-aware optimization.

Covers:
  - Heat index calculation (NWS formula edge cases)
  - Strategic humidity correction using heat index
  - Tactical controller with apparent temperature
"""

import importlib
import importlib.util
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

pkg = types.ModuleType("custom_components")
pkg.__path__ = [os.path.join(PROJECT_ROOT, "custom_components")]
sys.modules.setdefault("custom_components", pkg)

ho = types.ModuleType("custom_components.heatpump_optimizer")
ho.__path__ = [CC]
sys.modules.setdefault("custom_components.heatpump_optimizer", ho)

adapters_pkg = types.ModuleType("custom_components.heatpump_optimizer.adapters")
adapters_pkg.__path__ = [os.path.join(CC, "adapters")]
sys.modules.setdefault("custom_components.heatpump_optimizer.adapters", adapters_pkg)

engine_pkg = types.ModuleType("custom_components.heatpump_optimizer.engine")
engine_pkg.__path__ = [os.path.join(CC, "engine")]
sys.modules.setdefault("custom_components.heatpump_optimizer.engine", engine_pkg)

controllers_pkg = types.ModuleType("custom_components.heatpump_optimizer.controllers")
controllers_pkg.__path__ = [os.path.join(CC, "controllers")]
sys.modules.setdefault("custom_components.heatpump_optimizer.controllers", controllers_pkg)

# Stub homeassistant
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


# Load modules
dt_mod = _load(
    "custom_components.heatpump_optimizer.engine.data_types",
    os.path.join(CC, "engine", "data_types.py"),
)
engine_pkg.data_types = dt_mod

comfort_mod = _load(
    "custom_components.heatpump_optimizer.engine.comfort",
    os.path.join(CC, "engine", "comfort.py"),
)
engine_pkg.comfort = comfort_mod

perf_mod = _load(
    "custom_components.heatpump_optimizer.engine.performance_model",
    os.path.join(CC, "engine", "performance_model.py"),
)
engine_pkg.performance_model = perf_mod

opt_mod = _load(
    "custom_components.heatpump_optimizer.engine.optimizer",
    os.path.join(CC, "engine", "optimizer.py"),
)
engine_pkg.optimizer = opt_mod

occ_mod = _load(
    "custom_components.heatpump_optimizer.adapters.occupancy",
    os.path.join(CC, "adapters", "occupancy.py"),
)
adapters_pkg.occupancy = occ_mod

strat_mod = _load(
    "custom_components.heatpump_optimizer.controllers.strategic",
    os.path.join(CC, "controllers", "strategic.py"),
)
controllers_pkg.strategic = strat_mod

tact_mod = _load(
    "custom_components.heatpump_optimizer.controllers.tactical",
    os.path.join(CC, "controllers", "tactical.py"),
)
controllers_pkg.tactical = tact_mod

# Imports
calculate_apparent_temperature = comfort_mod.calculate_apparent_temperature
ForecastPoint = dt_mod.ForecastPoint
OptimizedSchedule = dt_mod.OptimizedSchedule
ScheduleEntry = dt_mod.ScheduleEntry
SimulationPoint = dt_mod.SimulationPoint
StrategicPlanner = strat_mod.StrategicPlanner
TacticalController = tact_mod.TacticalController
TacticalState = tact_mod.TacticalState

NOW = datetime(2026, 3, 12, 10, 0, 0, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════════════════
# Heat Index Calculation Tests
# ═══════════════════════════════════════════════════════════════════════


class TestCalculateApparentTemperature:
    """Tests for the NWS heat index calculation."""

    def test_below_70f_returns_unchanged(self):
        """Below 70°F, heat index is not meaningful."""
        assert calculate_apparent_temperature(65.0, 80.0) == 65.0
        assert calculate_apparent_temperature(50.0, 90.0) == 50.0

    def test_low_humidity_returns_unchanged(self):
        """Below 40% humidity (above 30%), no correction applied."""
        result = calculate_apparent_temperature(75.0, 35.0)
        assert result == 75.0

    def test_dry_air_cooling_correction(self):
        """Low humidity (<30%) makes it feel slightly cooler."""
        # At 20% humidity: correction = (30 - 20) * 0.05 = 0.5°F
        result = calculate_apparent_temperature(72.0, 20.0)
        assert result == pytest.approx(71.5, abs=0.01)

    def test_dry_air_max_correction(self):
        """At 0% humidity, max correction is 1.5°F."""
        result = calculate_apparent_temperature(72.0, 0.0)
        assert result == pytest.approx(70.5, abs=0.01)

    def test_dry_air_below_65f_no_correction(self):
        """Dry air correction only applies at 65°F+."""
        result = calculate_apparent_temperature(60.0, 10.0)
        assert result == 60.0

    def test_moderate_humidity_at_75f(self):
        """75°F at 50% humidity — simple Steadman should be < 80."""
        result = calculate_apparent_temperature(75.0, 50.0)
        # Steadman: 0.5 * (75 + 61 + (7*1.2) + (50*0.094)) = 0.5 * (75+61+8.4+4.7) = 74.55
        assert result < 80.0
        assert result > 70.0

    def test_high_humidity_at_78f(self):
        """78°F at 70% humidity — should feel noticeably warmer."""
        result = calculate_apparent_temperature(78.0, 70.0)
        assert result > 78.0  # humidity makes it feel warmer

    def test_high_humidity_at_80f(self):
        """80°F at 80% humidity — well into heat index territory."""
        result = calculate_apparent_temperature(80.0, 80.0)
        # NWS tables say ~84°F heat index
        assert result > 82.0
        assert result < 90.0

    def test_very_high_humidity_at_85f(self):
        """85°F at 90% humidity — significant heat index."""
        result = calculate_apparent_temperature(85.0, 90.0)
        # NWS tables: ~101°F
        assert result > 95.0

    def test_low_humidity_high_temp(self):
        """95°F at 10% humidity — low-humidity adjustment applies."""
        result = calculate_apparent_temperature(95.0, 10.0)
        # Low humidity actually makes hot temps feel slightly less bad
        # The NWS adjustment should reduce the heat index
        assert result < 100.0

    def test_steadman_to_rothfusz_transition(self):
        """Around the 80°F Steadman threshold, should switch to Rothfusz."""
        # At these conditions, simple Steadman gives ~80, so Rothfusz kicks in
        result = calculate_apparent_temperature(82.0, 60.0)
        assert result > 82.0  # humidity effect at 82°F/60% is noticeable

    def test_boundary_humidity_40_percent(self):
        """Exactly 40% humidity at 75°F — just at the threshold."""
        result = calculate_apparent_temperature(75.0, 40.0)
        # Should compute heat index (not return unchanged)
        assert isinstance(result, float)

    def test_boundary_humidity_30_percent(self):
        """Exactly 30% humidity — no dry-air correction."""
        result = calculate_apparent_temperature(72.0, 30.0)
        # Not < 30, so no dry-air correction; not >= 40, so no heat index
        assert result == 72.0

    def test_monotonic_with_humidity(self):
        """At constant temp, increasing humidity should increase apparent temp."""
        results = [
            calculate_apparent_temperature(80.0, h)
            for h in [40, 50, 60, 70, 80, 90]
        ]
        for i in range(len(results) - 1):
            assert results[i] <= results[i + 1]


# ═══════════════════════════════════════════════════════════════════════
# Strategic Humidity Correction Tests
# ═══════════════════════════════════════════════════════════════════════


class TestStrategicHumidityCorrection:
    """Tests for the heat-index-based strategic comfort correction."""

    def test_cooling_no_correction_at_low_humidity(self):
        """50% humidity at comfort max 78°F — no meaningful correction."""
        comfort = (70.0, 78.0)
        result = StrategicPlanner._apply_humidity_correction(comfort, 50.0, "cool")
        # At 78°F/50%, Steadman: 0.5*(78+61+12+4.7) ≈ 77.85, below 80 → small/no delta
        assert result[0] == 70.0
        # Max should be close to original or equal
        assert result[1] >= 76.0

    def test_cooling_correction_at_high_humidity(self):
        """70% humidity at comfort max 78°F — should tighten max."""
        comfort = (70.0, 78.0)
        result = StrategicPlanner._apply_humidity_correction(comfort, 70.0, "cool")
        assert result[0] == 70.0
        assert result[1] < 78.0  # max lowered

    def test_cooling_correction_capped_by_min_band(self):
        """Very high humidity shouldn't collapse the band below 2°F."""
        comfort = (74.0, 78.0)
        result = StrategicPlanner._apply_humidity_correction(comfort, 95.0, "cool")
        assert result[1] >= comfort[0] + 2.0  # min 2°F band maintained

    def test_heating_dry_air_lifts_comfort(self):
        """Low humidity in heating mode lifts comfort range."""
        comfort = (64.0, 70.0)
        result = StrategicPlanner._apply_humidity_correction(comfort, 20.0, "heat")
        # lift = (30 - 20) * 0.05 = 0.5
        assert result[0] == pytest.approx(64.5, abs=0.01)
        assert result[1] == pytest.approx(70.5, abs=0.01)

    def test_heating_normal_humidity_no_change(self):
        """Normal humidity in heating mode — no correction."""
        comfort = (64.0, 70.0)
        result = StrategicPlanner._apply_humidity_correction(comfort, 40.0, "heat")
        assert result == comfort

    def test_off_mode_no_change(self):
        """Off mode returns comfort unchanged."""
        comfort = (64.0, 70.0)
        result = StrategicPlanner._apply_humidity_correction(comfort, 80.0, "off")
        assert result == comfort


# ═══════════════════════════════════════════════════════════════════════
# Tactical Controller with Apparent Temperature Tests
# ═══════════════════════════════════════════════════════════════════════


def _make_schedule(target_temp=76.0, predicted_temp=76.0, mode="cool"):
    """Create a minimal schedule with simulation for tactical testing."""
    entry = ScheduleEntry(
        start_time=NOW,
        end_time=NOW + timedelta(hours=1),
        target_temp=target_temp,
        mode=mode,
    )
    sim = SimulationPoint(
        time=NOW,
        indoor_temp=predicted_temp,
        outdoor_temp=90.0,
        hvac_running=True,
        cumulative_runtime_minutes=30.0,
    )
    schedule = OptimizedSchedule(
        entries=[entry],
        baseline_runtime_minutes=480.0,
        optimized_runtime_minutes=360.0,
        savings_pct=25.0,
        simulation=[sim],
    )
    return schedule, entry


class TestTacticalApparentTemp:
    """Tests for tactical controller with humidity-adjusted apparent temperature."""

    def test_no_apparent_temp_uses_raw(self):
        """Without apparent_temp, behavior is identical to before."""
        tc = TacticalController()
        schedule, entry = _make_schedule(predicted_temp=76.0)

        result = tc.evaluate(
            actual_indoor_temp=76.0,
            schedule=schedule,
            current_entry=entry,
            now=NOW,
            apparent_temp=None,
        )
        assert result.state == TacticalState.NOMINAL
        assert abs(result.error) < 0.5

    def test_cooling_apparent_higher_triggers_correction(self):
        """In cooling mode, if apparent > actual, error should reflect apparent."""
        tc = TacticalController()
        # Predicted 76, actual 76, but apparent 79 (high humidity)
        schedule, entry = _make_schedule(predicted_temp=76.0, mode="cool")

        result = tc.evaluate(
            actual_indoor_temp=76.0,
            schedule=schedule,
            current_entry=entry,
            now=NOW,
            apparent_temp=79.0,
        )
        # effective_temp = max(76, 79) = 79; error = 79 - 76 = +3.0
        assert result.error == pytest.approx(3.0, abs=0.1)
        # This should trigger at least a correction (>1°F error)
        assert result.state in (TacticalState.CORRECTING, TacticalState.DISTURBED)

    def test_cooling_apparent_lower_ignored(self):
        """In cooling mode, apparent < actual is not worse, so use raw."""
        tc = TacticalController()
        schedule, entry = _make_schedule(predicted_temp=76.0, mode="cool")

        result = tc.evaluate(
            actual_indoor_temp=76.0,
            schedule=schedule,
            current_entry=entry,
            now=NOW,
            apparent_temp=74.0,  # lower than actual
        )
        # effective_temp = max(76, 74) = 76; error = 76 - 76 = 0
        assert abs(result.error) < 0.5
        assert result.state == TacticalState.NOMINAL

    def test_heating_apparent_lower_triggers_correction(self):
        """In heating mode, if apparent < actual (dry air), error reflects apparent."""
        tc = TacticalController()
        schedule, entry = _make_schedule(predicted_temp=68.0, mode="heat")

        result = tc.evaluate(
            actual_indoor_temp=68.0,
            schedule=schedule,
            current_entry=entry,
            now=NOW,
            apparent_temp=66.0,  # dry air feels cooler
        )
        # effective_temp = min(68, 66) = 66; error = 66 - 68 = -2.0
        assert result.error == pytest.approx(-2.0, abs=0.1)
        assert result.state == TacticalState.CORRECTING

    def test_heating_apparent_higher_ignored(self):
        """In heating mode, apparent > actual is not worse, so use raw."""
        tc = TacticalController()
        schedule, entry = _make_schedule(predicted_temp=68.0, mode="heat")

        result = tc.evaluate(
            actual_indoor_temp=68.0,
            schedule=schedule,
            current_entry=entry,
            now=NOW,
            apparent_temp=70.0,  # higher than actual
        )
        # effective_temp = min(68, 70) = 68; error = 68 - 68 = 0
        assert abs(result.error) < 0.5
        assert result.state == TacticalState.NOMINAL

    def test_apparent_temp_stored_in_result(self):
        """TacticalResult should carry the apparent_temp value."""
        tc = TacticalController()
        schedule, entry = _make_schedule(predicted_temp=76.0)

        result = tc.evaluate(
            actual_indoor_temp=76.0,
            schedule=schedule,
            current_entry=entry,
            now=NOW,
            apparent_temp=79.5,
        )
        assert result.apparent_temp == 79.5

    def test_actual_temp_unchanged_in_result(self):
        """TacticalResult.actual_temp should always be the raw reading."""
        tc = TacticalController()
        schedule, entry = _make_schedule(predicted_temp=76.0, mode="cool")

        result = tc.evaluate(
            actual_indoor_temp=76.0,
            schedule=schedule,
            current_entry=entry,
            now=NOW,
            apparent_temp=80.0,
        )
        # actual_temp stays raw, even though error uses effective_temp
        assert result.actual_temp == 76.0

    def test_no_prediction_with_apparent_temp(self):
        """When no prediction available, apparent temp is stored but no error."""
        tc = TacticalController()

        result = tc.evaluate(
            actual_indoor_temp=76.0,
            schedule=None,
            current_entry=None,
            now=NOW,
            apparent_temp=79.0,
        )
        assert result.error == 0.0
        assert result.apparent_temp == 79.0
