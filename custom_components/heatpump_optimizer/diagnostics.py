"""Diagnostics support for Heat Pump Optimizer."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, VERSION

TO_REDACT = {"latitude", "longitude", "password", "token", "api_key"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

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

    return diag
