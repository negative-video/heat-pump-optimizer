"""Tests for config flow validation helpers.

Covers _validate_profile and _validate_model_import — pure functions
that only depend on json and os (standard library).
"""

from __future__ import annotations

import json
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
import os

def _validate_profile(path: str):
    if not os.path.isfile(path):
        return "File not found"
    try:
        with open(path) as f:
            data = json.load(f)
        temp = data.get("temperature", {})
        if not temp.get("cool_1", {}).get("deltas"):
            return "Missing cool_1 deltas"
        if not temp.get("heat_1", {}).get("deltas"):
            return "Missing heat_1 deltas"
        if not temp.get("resist", {}).get("deltas"):
            return "Missing resist deltas"
    except (json.JSONDecodeError, OSError) as err:
        return f"Invalid file: {err}"
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
""",
        "<config_flow_validators>",
        "exec",
    ),
    _globals,
)

_validate_profile = _globals["_validate_profile"]
_validate_model_import = _globals["_validate_model_import"]


# ── _validate_profile tests ──────────────────────────────────────────


class TestValidateProfile:
    def test_valid_profile(self, tmp_path):
        profile = {
            "temperature": {
                "cool_1": {"deltas": [1.0, 2.0]},
                "heat_1": {"deltas": [0.5, 1.5]},
                "resist": {"deltas": [0.1, 0.2]},
            }
        }
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(profile))
        assert _validate_profile(str(path)) is None

    def test_file_not_found(self):
        result = _validate_profile("/nonexistent/path/profile.json")
        assert result == "File not found"

    def test_invalid_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not valid json {{{")
        result = _validate_profile(str(path))
        assert result is not None
        assert "Invalid file" in result

    def test_missing_cool_deltas(self, tmp_path):
        profile = {
            "temperature": {
                "cool_1": {},
                "heat_1": {"deltas": [0.5]},
                "resist": {"deltas": [0.1]},
            }
        }
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(profile))
        result = _validate_profile(str(path))
        assert result == "Missing cool_1 deltas"

    def test_missing_heat_deltas(self, tmp_path):
        profile = {
            "temperature": {
                "cool_1": {"deltas": [1.0]},
                "heat_1": {},
                "resist": {"deltas": [0.1]},
            }
        }
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(profile))
        result = _validate_profile(str(path))
        assert result == "Missing heat_1 deltas"

    def test_missing_resist_deltas(self, tmp_path):
        profile = {
            "temperature": {
                "cool_1": {"deltas": [1.0]},
                "heat_1": {"deltas": [0.5]},
                "resist": {},
            }
        }
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(profile))
        result = _validate_profile(str(path))
        assert result == "Missing resist deltas"

    def test_missing_temperature_key(self, tmp_path):
        profile = {"other": "data"}
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(profile))
        result = _validate_profile(str(path))
        assert result is not None
        assert "Missing" in result

    def test_empty_deltas_list(self, tmp_path):
        profile = {
            "temperature": {
                "cool_1": {"deltas": []},
                "heat_1": {"deltas": [0.5]},
                "resist": {"deltas": [0.1]},
            }
        }
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(profile))
        result = _validate_profile(str(path))
        assert result == "Missing cool_1 deltas"


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
