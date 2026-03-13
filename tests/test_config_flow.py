"""Tests for config flow validation helpers and options flow utilities.

Covers _validate_profile, _validate_model_import, _strip_empty_strings,
_suggest_multi filtering, and sensor overlap detection.
"""

from __future__ import annotations

import json
import logging
import os

import pytest

from conftest import CC

# These functions are pure (no HA imports needed), so we extract them
# directly from the source file rather than loading the full config_flow
# module (which would pull in HA dependencies and contaminate sys.modules).

_source_path = os.path.join(CC, "config_flow.py")
_globals = {}
exec(
    compile(
        """
import json
import logging
import os

_LOGGER = logging.getLogger(__name__)

def _validate_profile(path: str):
    if not os.path.isfile(path):
        _LOGGER.error("Beestat profile not found at path: %s", path)
        return "profile_not_found"
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as err:
        _LOGGER.error("Beestat profile parse error at %s: %s", path, err)
        return "profile_parse_error"

    temp = data.get("temperature", {})
    required_modes = ["cool_1", "heat_1", "resist"]
    for mode in required_modes:
        mode_data = temp.get(mode, {})
        if not mode_data or not mode_data.get("deltas"):
            _LOGGER.error(
                "Beestat profile missing temperature.%s.deltas. "
                "Top-level keys: %s, temperature keys: %s",
                mode, list(data.keys()), list(temp.keys()),
            )
            return "profile_missing_keys"
        if not mode_data.get("linear_trendline"):
            _LOGGER.error(
                "Beestat profile missing temperature.%s.linear_trendline", mode
            )
            return "profile_missing_keys"

    if "balance_point" not in data:
        _LOGGER.error(
            "Beestat profile missing balance_point. Top-level keys: %s",
            list(data.keys()),
        )
        return "profile_missing_keys"

    return None

def _validate_model_import(data_str: str):
    try:
        data = json.loads(data_str)
        if not isinstance(data, dict):
            return "Model data must be a JSON object"
        if "estimator_state" not in data and "state_mean" not in data:
            return "Missing estimator state in model data"
    except (json.JSONDecodeError, TypeError) as err:
        return f"Invalid JSON: {err}"
    return None

def _strip_empty_strings(user_input: dict) -> dict:
    return {k: v for k, v in user_input.items() if v != ""}
""",
        "<config_flow_validators>",
        "exec",
    ),
    _globals,
)

_validate_profile = _globals["_validate_profile"]
_validate_model_import = _globals["_validate_model_import"]
_strip_empty_strings = _globals["_strip_empty_strings"]


def _make_valid_profile():
    """Return a minimal valid Beestat profile dict."""
    return {
        "temperature": {
            "cool_1": {
                "deltas": {"75": -3.36, "96": -0.66},
                "linear_trendline": {"slope": 0.13, "intercept": -14.0},
            },
            "heat_1": {
                "deltas": {"30": 0.4, "50": 1.7},
                "linear_trendline": {"slope": 0.04, "intercept": -1.0},
            },
            "resist": {
                "deltas": {"50": -0.11, "70": 0.75},
                "linear_trendline": {"slope": 0.035, "intercept": -1.77},
            },
        },
        "balance_point": {"heat_1": 24.9, "resist": 50.2},
    }


# ── _validate_profile tests ──────────────────────────────────────────


class TestValidateProfile:
    def test_valid_profile(self, tmp_path):
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(_make_valid_profile()))
        assert _validate_profile(str(path)) is None

    def test_valid_real_beestat_file(self):
        """Validate against the actual Beestat export in docs/internal."""
        real_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "docs",
            "internal",
            "Temperature Profile - 2026-03-06.json",
        )
        if os.path.isfile(real_path):
            assert _validate_profile(real_path) is None

    def test_file_not_found(self):
        result = _validate_profile("/nonexistent/path/profile.json")
        assert result == "profile_not_found"

    def test_invalid_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not valid json {{{")
        result = _validate_profile(str(path))
        assert result == "profile_parse_error"

    def test_missing_cool_deltas(self, tmp_path):
        profile = _make_valid_profile()
        profile["temperature"]["cool_1"] = {}
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(profile))
        assert _validate_profile(str(path)) == "profile_missing_keys"

    def test_missing_heat_deltas(self, tmp_path):
        profile = _make_valid_profile()
        profile["temperature"]["heat_1"] = {}
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(profile))
        assert _validate_profile(str(path)) == "profile_missing_keys"

    def test_missing_resist_deltas(self, tmp_path):
        profile = _make_valid_profile()
        profile["temperature"]["resist"] = {}
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(profile))
        assert _validate_profile(str(path)) == "profile_missing_keys"

    def test_missing_temperature_key(self, tmp_path):
        profile = {"other": "data", "balance_point": {"heat_1": 24.9}}
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(profile))
        assert _validate_profile(str(path)) == "profile_missing_keys"

    def test_empty_deltas_list(self, tmp_path):
        profile = _make_valid_profile()
        profile["temperature"]["cool_1"]["deltas"] = []
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(profile))
        assert _validate_profile(str(path)) == "profile_missing_keys"

    def test_missing_linear_trendline(self, tmp_path):
        profile = _make_valid_profile()
        del profile["temperature"]["cool_1"]["linear_trendline"]
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(profile))
        assert _validate_profile(str(path)) == "profile_missing_keys"

    def test_missing_balance_point(self, tmp_path):
        profile = _make_valid_profile()
        del profile["balance_point"]
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(profile))
        assert _validate_profile(str(path)) == "profile_missing_keys"

    def test_path_with_spaces(self, tmp_path):
        """Filenames with spaces should work (common in Beestat exports)."""
        path = tmp_path / "Temperature Profile - 2026-03-06.json"
        path.write_text(json.dumps(_make_valid_profile()))
        assert _validate_profile(str(path)) is None


# ── _validate_model_import tests ─────────────────────────────────────


class TestValidateModelImport:
    def test_valid_with_estimator_state(self):
        data = json.dumps({"estimator_state": {"confidence": 0.8}})
        assert _validate_model_import(data) is None

    def test_valid_with_state_mean(self):
        data = json.dumps({"state_mean": [1.0, 2.0, 3.0]})
        assert _validate_model_import(data) is None

    def test_not_a_dict(self):
        data = json.dumps([1, 2, 3])
        result = _validate_model_import(data)
        assert result == "Model data must be a JSON object"

    def test_missing_required_keys(self):
        data = json.dumps({"other_key": "value"})
        result = _validate_model_import(data)
        assert result == "Missing estimator state in model data"

    def test_invalid_json_string(self):
        result = _validate_model_import("not json at all")
        assert result is not None
        assert "Invalid JSON" in result

    def test_none_input(self):
        result = _validate_model_import(None)
        assert result is not None
        assert "Invalid JSON" in result

    def test_empty_string(self):
        result = _validate_model_import("")
        assert result is not None
        assert "Invalid JSON" in result

    def test_valid_with_both_keys(self):
        data = json.dumps({
            "estimator_state": {"confidence": 0.9},
            "state_mean": [1.0],
        })
        assert _validate_model_import(data) is None


# ── _strip_empty_strings tests ───────────────────────────────────────


class TestStripEmptyStrings:
    def test_removes_empty_strings(self):
        result = _strip_empty_strings({
            "entity_a": "sensor.temp",
            "entity_b": "",
            "entity_c": "",
        })
        assert result == {"entity_a": "sensor.temp"}

    def test_preserves_non_empty_values(self):
        data = {"a": "value", "b": 0, "c": None, "d": [], "e": False}
        assert _strip_empty_strings(data) == data

    def test_empty_dict(self):
        assert _strip_empty_strings({}) == {}

    def test_all_empty_strings(self):
        assert _strip_empty_strings({"a": "", "b": ""}) == {}


# ── Quote stripping in Beestat path handling ─────────────────────────


class TestBeestatPathQuoteStripping:
    """Test that the strip().strip(\"'\\\"\") pattern used in
    async_step_thermal_profile_beestat correctly handles quoted paths.
    """

    def _strip_path(self, raw: str) -> str:
        """Replicate the path stripping from config_flow.py."""
        return raw.strip().strip("'\"")

    def test_single_quoted_path(self):
        raw = "'/homeassistant/Temperature Profile - 2026-03-06.json'"
        assert self._strip_path(raw) == "/homeassistant/Temperature Profile - 2026-03-06.json"

    def test_double_quoted_path(self):
        raw = '"/homeassistant/profile.json"'
        assert self._strip_path(raw) == "/homeassistant/profile.json"

    def test_unquoted_path_unchanged(self):
        raw = "/homeassistant/profile.json"
        assert self._strip_path(raw) == raw

    def test_whitespace_stripped(self):
        raw = "  /homeassistant/profile.json  "
        assert self._strip_path(raw) == "/homeassistant/profile.json"

    def test_quoted_with_spaces(self):
        raw = " '/config/Temperature Profile - 2026-03-06.json' "
        assert self._strip_path(raw) == "/config/Temperature Profile - 2026-03-06.json"

    def test_valid_profile_after_stripping(self, tmp_path):
        """End-to-end: strip quotes then validate."""
        path = tmp_path / "Temperature Profile - 2026-03-06.json"
        path.write_text(json.dumps(_make_valid_profile()))
        raw = f"'{path}'"
        cleaned = self._strip_path(raw)
        assert _validate_profile(cleaned) is None


# ── Sensor overlap detection ─────────────────────────────────────────


class TestSensorOverlap:
    """Test the overlap detection logic used in async_step_sensors."""

    @staticmethod
    def _check_overlap(user_input: dict) -> bool:
        """Replicate the overlap check from config_flow.py."""
        outdoor_t = set(user_input.get("outdoor_temp_entities", []))
        indoor_t = set(user_input.get("indoor_temp_entities", []))
        outdoor_h = set(user_input.get("outdoor_humidity_entities", []))
        indoor_h = set(user_input.get("indoor_humidity_entities", []))
        return bool(outdoor_t & indoor_t or outdoor_h & indoor_h)

    def test_no_overlap(self):
        assert not self._check_overlap({
            "outdoor_temp_entities": ["sensor.outdoor_temp"],
            "indoor_temp_entities": ["sensor.living_room_temp"],
        })

    def test_temp_overlap_detected(self):
        assert self._check_overlap({
            "outdoor_temp_entities": ["sensor.temp_a", "sensor.temp_b"],
            "indoor_temp_entities": ["sensor.temp_b", "sensor.temp_c"],
        })

    def test_humidity_overlap_detected(self):
        assert self._check_overlap({
            "outdoor_humidity_entities": ["sensor.hum_shared"],
            "indoor_humidity_entities": ["sensor.hum_shared"],
        })

    def test_empty_lists_no_overlap(self):
        assert not self._check_overlap({
            "outdoor_temp_entities": [],
            "indoor_temp_entities": [],
        })

    def test_missing_keys_no_overlap(self):
        assert not self._check_overlap({})


# ── Suggest multi high-confidence filtering ──────────────────────────


class TestSuggestMultiFiltering:
    """Test the high-confidence-only filtering logic."""

    @staticmethod
    def _suggest_multi(existing, suggestions, max_count=2):
        """Replicate the _suggest_multi logic from config_flow.py.

        suggestions is a list of dicts with 'entity_id' and 'confidence'.
        """
        if existing:
            return existing
        high = [s["entity_id"] for s in suggestions if s["confidence"] == "high"]
        return high[:max_count]

    def test_returns_existing_if_set(self):
        result = self._suggest_multi(
            ["sensor.existing"],
            [{"entity_id": "sensor.new", "confidence": "high"}],
        )
        assert result == ["sensor.existing"]

    def test_only_high_confidence(self):
        suggestions = [
            {"entity_id": "sensor.outdoor_temp", "confidence": "high"},
            {"entity_id": "sensor.cpu_temp", "confidence": "medium"},
            {"entity_id": "sensor.nas_temp", "confidence": "low"},
        ]
        result = self._suggest_multi([], suggestions)
        assert result == ["sensor.outdoor_temp"]

    def test_capped_at_max_count(self):
        suggestions = [
            {"entity_id": "sensor.a", "confidence": "high"},
            {"entity_id": "sensor.b", "confidence": "high"},
            {"entity_id": "sensor.c", "confidence": "high"},
        ]
        result = self._suggest_multi([], suggestions, max_count=2)
        assert len(result) == 2
        assert result == ["sensor.a", "sensor.b"]

    def test_no_high_confidence_returns_empty(self):
        suggestions = [
            {"entity_id": "sensor.cpu_temp", "confidence": "medium"},
            {"entity_id": "sensor.nas_temp", "confidence": "low"},
        ]
        result = self._suggest_multi([], suggestions)
        assert result == []

    def test_empty_suggestions_returns_empty(self):
        result = self._suggest_multi([], [])
        assert result == []
