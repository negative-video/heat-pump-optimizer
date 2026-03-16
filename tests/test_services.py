"""Tests for service handlers — all 9 services, coordinator dispatch, and unload."""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import CC, load_module

# Ensure homeassistant.core has ServiceCall (may be overwritten by other test files)
_ha_core = sys.modules.get("homeassistant.core")
if _ha_core and not hasattr(_ha_core, "ServiceCall"):
    _ha_core.ServiceCall = MagicMock

# Load the modules we need
const_mod = load_module(
    "custom_components.heatpump_optimizer.const",
    os.path.join(CC, "const.py"),
)
DOMAIN = const_mod.DOMAIN

# Load occupancy adapter for OccupancyMode
occ_mod = load_module(
    "custom_components.heatpump_optimizer.adapters.occupancy",
    os.path.join(CC, "adapters", "occupancy.py"),
)
OccupancyMode = occ_mod.OccupancyMode

# Stub the coordinator module so services.py can import it
sys.modules.setdefault(
    "custom_components.heatpump_optimizer.coordinator", MagicMock()
)

# Now load services
services_mod = load_module(
    "custom_components.heatpump_optimizer.services",
    os.path.join(CC, "services.py"),
)


# ── Helpers ──────────────────────────────────────────────────────────

_loop = asyncio.new_event_loop()


def _run(coro):
    return _loop.run_until_complete(coro)


def _call(data=None):
    call = MagicMock()
    call.data = data or {}
    return call


def _setup(hass, coordinator):
    hass.data[DOMAIN] = {"entry_1": coordinator}
    _run(services_mod.async_setup_services(hass))


def _handler(hass, name):
    for call_args in hass.services.async_register.call_args_list:
        args = call_args[0]
        if args[1] == name:
            return args[2]
    raise ValueError(f"Service {name} not registered")


# ── Registration / Unload ────────────────────────────────────────────


class TestServiceRegistration:
    def test_registers_all_services(self, mock_hass, mock_coordinator):
        _setup(mock_hass, mock_coordinator)
        registered = [a[0][1] for a in mock_hass.services.async_register.call_args_list]
        expected = [
            "rebootstrap", "force_reoptimize", "pause", "resume",
            "set_occupancy", "demand_response", "export_model",
            "import_model", "set_constraint",
        ]
        for name in expected:
            assert name in registered
        assert len(registered) == 9

    def test_unload_removes_all(self, mock_hass):
        _run(services_mod.async_unload_services(mock_hass))
        removed = [a[0][1] for a in mock_hass.services.async_remove.call_args_list]
        assert len(removed) == 9


# ── rebootstrap ──────────────────────────────────────────────────────


class TestRebootstrap:
    def test_calls_coordinator_bootstrap(self, mock_hass, mock_coordinator):
        mock_coordinator._history_bootstrap_completed = True
        mock_coordinator._try_history_bootstrap = AsyncMock()
        mock_coordinator.async_request_refresh = AsyncMock()
        _setup(mock_hass, mock_coordinator)
        _run(_handler(mock_hass, "rebootstrap")(_call()))
        # Should reset the flag and trigger bootstrap
        assert mock_coordinator._history_bootstrap_completed is False
        mock_coordinator._try_history_bootstrap.assert_called_once()
        mock_coordinator.async_request_refresh.assert_called_once()


# ── force_reoptimize ─────────────────────────────────────────────────


class TestForceReoptimize:
    def test_calls_coordinator(self, mock_hass, mock_coordinator):
        _setup(mock_hass, mock_coordinator)
        _run(_handler(mock_hass, "force_reoptimize")(_call()))
        mock_coordinator.async_force_reoptimize.assert_called_once()


# ── pause / resume ───────────────────────────────────────────────────


class TestPause:
    def test_calls_coordinator(self, mock_hass, mock_coordinator):
        _setup(mock_hass, mock_coordinator)
        _run(_handler(mock_hass, "pause")(_call()))
        mock_coordinator.pause.assert_called_once()


class TestResume:
    def test_calls_coordinator(self, mock_hass, mock_coordinator):
        _setup(mock_hass, mock_coordinator)
        _run(_handler(mock_hass, "resume")(_call()))
        mock_coordinator.async_resume.assert_called_once()


# ── set_occupancy ────────────────────────────────────────────────────


class TestSetOccupancy:
    def test_mode_home(self, mock_hass, mock_coordinator):
        _setup(mock_hass, mock_coordinator)
        _run(_handler(mock_hass, "set_occupancy")(_call({"mode": "home"})))
        mock_coordinator.set_occupancy.assert_called_once_with(OccupancyMode.HOME)

    def test_mode_away(self, mock_hass, mock_coordinator):
        _setup(mock_hass, mock_coordinator)
        _run(_handler(mock_hass, "set_occupancy")(_call({"mode": "away"})))
        mock_coordinator.set_occupancy.assert_called_once_with(OccupancyMode.AWAY)

    def test_mode_vacation(self, mock_hass, mock_coordinator):
        _setup(mock_hass, mock_coordinator)
        _run(_handler(mock_hass, "set_occupancy")(_call({"mode": "vacation"})))
        mock_coordinator.set_occupancy.assert_called_once_with(OccupancyMode.VACATION)

    def test_mode_auto_passes_none(self, mock_hass, mock_coordinator):
        _setup(mock_hass, mock_coordinator)
        _run(_handler(mock_hass, "set_occupancy")(_call({"mode": "auto"})))
        mock_coordinator.set_occupancy.assert_called_once_with(None)

    def test_invalid_mode_not_called(self, mock_hass, mock_coordinator):
        _setup(mock_hass, mock_coordinator)
        _run(_handler(mock_hass, "set_occupancy")(_call({"mode": "bogus"})))
        mock_coordinator.set_occupancy.assert_not_called()


# ── demand_response ──────────────────────────────────────────────────


class TestDemandResponse:
    def test_reduce_with_duration(self, mock_hass, mock_coordinator):
        _setup(mock_hass, mock_coordinator)
        _run(_handler(mock_hass, "demand_response")(_call({"mode": "reduce", "duration_minutes": 120})))
        mock_coordinator.async_demand_response.assert_called_once_with("reduce", 120)

    def test_restore_default_duration(self, mock_hass, mock_coordinator):
        _setup(mock_hass, mock_coordinator)
        _run(_handler(mock_hass, "demand_response")(_call({"mode": "restore"})))
        mock_coordinator.async_demand_response.assert_called_once_with("restore", 60)


# ── export_model / import_model ──────────────────────────────────────


class TestExportModel:
    def test_returns_model_data(self, mock_hass, mock_coordinator):
        _setup(mock_hass, mock_coordinator)
        result = _run(_handler(mock_hass, "export_model")(_call()))
        assert result == {"estimator_state": {}}

    def test_no_coordinator_returns_empty(self, mock_hass):
        mock_hass.data[DOMAIN] = {}
        _run(services_mod.async_setup_services(mock_hass))
        result = _run(_handler(mock_hass, "export_model")(_call()))
        assert result == {}


class TestImportModel:
    def test_passes_model_data(self, mock_hass, mock_coordinator):
        _setup(mock_hass, mock_coordinator)
        model = {"estimator_state": {"confidence": 0.8}}
        _run(_handler(mock_hass, "import_model")(_call({"model_data": model})))
        mock_coordinator.import_model.assert_called_once_with(model)


# ── set_constraint ───────────────────────────────────────────────────


class TestSetConstraint:
    def test_full_params(self, mock_hass, mock_coordinator):
        _setup(mock_hass, mock_coordinator)
        _run(_handler(mock_hass, "set_constraint")(_call({
            "type": "max_temp", "value": 76, "duration_minutes": 120, "source": "grid",
        })))
        mock_coordinator.async_set_constraint.assert_called_once_with(
            constraint_type="max_temp", value=76, duration_minutes=120, source="grid",
        )

    def test_defaults(self, mock_hass, mock_coordinator):
        _setup(mock_hass, mock_coordinator)
        _run(_handler(mock_hass, "set_constraint")(_call({"type": "pause_until"})))
        mock_coordinator.async_set_constraint.assert_called_once_with(
            constraint_type="pause_until", value=0, duration_minutes=60, source="service_call",
        )


# ── No coordinator edge cases ────────────────────────────────────────


class TestNoCoordinator:
    def test_empty_entries(self, mock_hass):
        mock_hass.data[DOMAIN] = {}
        _run(services_mod.async_setup_services(mock_hass))
        _run(_handler(mock_hass, "force_reoptimize")(_call()))  # should not raise

    def test_domain_missing(self, mock_hass):
        mock_hass.data = {}
        _run(services_mod.async_setup_services(mock_hass))
        _run(_handler(mock_hass, "force_reoptimize")(_call()))  # should not raise
