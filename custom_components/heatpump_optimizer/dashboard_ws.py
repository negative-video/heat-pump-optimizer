"""Websocket command that serves the dashboard archive to the panel.

The series behind the panel's charts (a month of hourly rows, scored
forecasts, morning recoveries) are far too large for entity attributes, so
the panel asks for them directly.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

WS_TYPE_DASHBOARD = f"{DOMAIN}/dashboard"


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_DASHBOARD,
        vol.Optional("entry_id"): str,
    }
)
@callback
def ws_dashboard(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Return the dashboard payload for one loaded config entry."""
    wanted = msg.get("entry_id")
    for entry in hass.config_entries.async_entries(DOMAIN):
        if wanted and entry.entry_id != wanted:
            continue
        if entry.state is not ConfigEntryState.LOADED:
            continue
        coordinator = getattr(entry, "runtime_data", None)
        if coordinator is None:
            continue
        connection.send_result(msg["id"], coordinator.dashboard_payload())
        return
    connection.send_error(msg["id"], "not_found", "No loaded Heat Pump Optimizer entry")


@callback
def async_register_dashboard_ws(hass: HomeAssistant) -> None:
    """Register the command. Safe to call once per Home Assistant run."""
    websocket_api.async_register_command(hass, ws_dashboard)
