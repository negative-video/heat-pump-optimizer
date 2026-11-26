"""Diagnostics support for Heat Pump Optimizer."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import VERSION

TO_REDACT = {"latitude", "longitude", "password", "token", "api_key"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    diag: dict[str, Any] = {
        "version": VERSION,
        "config_entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
    }

    try:
        diag["coordinator_data"] = dict(coordinator.data or {})
    except Exception:  # noqa: BLE001
        diag["coordinator_data"] = {"error": "unavailable"}

    try:
        diag["learned_model"] = coordinator.export_model()
    except Exception:  # noqa: BLE001
        diag["learned_model"] = {"error": "unavailable"}

    # Measured savings evidence: the day-by-day degree-day records behind the
    # headline number, so the whole chain is auditable from a diagnostics dump.
    try:
        diag["energy_baseline"] = {
            **coordinator.energy_baseline.diagnostics(),
            "days": [d.to_dict() for d in coordinator.energy_baseline.days],
        }
    except Exception:  # noqa: BLE001
        diag["energy_baseline"] = {"error": "unavailable"}

    try:
        diag["ab_trial"] = coordinator.ab_trial_result()
    except Exception:  # noqa: BLE001
        diag["ab_trial"] = {"error": "unavailable"}

    return diag
