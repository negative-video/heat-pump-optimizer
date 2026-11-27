"""Service handlers for the Heat Pump Optimizer."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from .adapters.occupancy import OccupancyMode
from .const import DOMAIN
from .coordinator import HeatPumpOptimizerCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_FORCE_REOPTIMIZE = "force_reoptimize"
SERVICE_PAUSE = "pause"
SERVICE_RESUME = "resume"
SERVICE_SET_OCCUPANCY = "set_occupancy"
SERVICE_DEMAND_RESPONSE = "demand_response"
SERVICE_EXPORT_MODEL = "export_model"
SERVICE_IMPORT_MODEL = "import_model"
SERVICE_SET_CONSTRAINT = "set_constraint"
SERVICE_REBOOTSTRAP = "rebootstrap"
SERVICE_RESET_MODEL = "reset_model"
SERVICE_START_AB_TRIAL = "start_ab_trial"
SERVICE_STOP_AB_TRIAL = "stop_ab_trial"
SERVICE_AB_TRIAL_RESULT = "ab_trial_result"
SERVICE_IMPORT_THERMOSTAT_HISTORY = "import_thermostat_history"


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register integration services."""

    async def _get_coordinator() -> HeatPumpOptimizerCoordinator | None:
        """Get the first (and typically only) coordinator instance."""
        for entry in hass.config_entries.async_entries(DOMAIN):
            coord = getattr(entry, "runtime_data", None)
            if coord is not None:
                return coord
        _LOGGER.error("No Heat Pump Optimizer instances configured")
        return None

    async def handle_force_reoptimize(call: ServiceCall) -> None:
        coordinator = await _get_coordinator()
        if coordinator:
            _LOGGER.info("Service call: force re-optimization")
            await coordinator.async_force_reoptimize()

    async def handle_pause(call: ServiceCall) -> None:
        coordinator = await _get_coordinator()
        if coordinator:
            _LOGGER.info("Service call: pause optimization")
            coordinator.pause()

    async def handle_resume(call: ServiceCall) -> None:
        coordinator = await _get_coordinator()
        if coordinator:
            _LOGGER.info("Service call: resume optimization")
            await coordinator.async_resume()

    async def handle_set_occupancy(call: ServiceCall) -> None:
        coordinator = await _get_coordinator()
        if coordinator:
            mode_str = call.data.get("mode", "auto")
            _LOGGER.info("Service call: set occupancy to %s", mode_str)
            if mode_str == "auto":
                coordinator.set_occupancy(None)
            else:
                try:
                    mode = OccupancyMode(mode_str)
                    coordinator.set_occupancy(mode)
                except ValueError:
                    _LOGGER.error("Invalid occupancy mode: %s", mode_str)

    async def handle_demand_response(call: ServiceCall) -> None:
        coordinator = await _get_coordinator()
        if coordinator:
            mode = call.data.get("mode", "reduce")
            duration = call.data.get("duration_minutes", 60)
            _LOGGER.info("Service call: demand response %s for %d min", mode, duration)
            await coordinator.async_demand_response(mode, duration)

    async def handle_export_model(call: ServiceCall) -> dict:
        coordinator = await _get_coordinator()
        if coordinator:
            _LOGGER.info("Service call: export model")
            return coordinator.export_model()
        return {}

    async def handle_import_model(call: ServiceCall) -> None:
        coordinator = await _get_coordinator()
        if coordinator:
            model_data = call.data.get("model_data", {})
            _LOGGER.info("Service call: import model")
            coordinator.import_model(model_data)

    async def handle_import_thermostat_history(call: ServiceCall) -> dict:
        coordinator = await _get_coordinator()
        if not coordinator:
            return {}
        path = str(call.data["path"])
        if not hass.config.is_allowed_path(path):
            raise HomeAssistantError(f"Path is not allowed: {path} (add it to allowlist_external_dirs)")
        _LOGGER.info("Service call: import thermostat history from %s", path)
        try:
            return await coordinator.async_import_thermostat_history(path)
        except (OSError, UnicodeDecodeError) as err:
            raise HomeAssistantError(f"Could not read {path}: {err}") from err

    async def handle_set_constraint(call: ServiceCall) -> None:
        coordinator = await _get_coordinator()
        if coordinator:
            constraint_type = call.data["type"]
            value = call.data.get("value", 0)
            duration = call.data.get("duration_minutes", 60)
            source = call.data.get("source", "service_call")
            _LOGGER.info(
                "Service call: set_constraint type=%s value=%s duration=%d source=%s",
                constraint_type,
                value,
                duration,
                source,
            )
            await coordinator.async_set_constraint(
                constraint_type=constraint_type,
                value=value,
                duration_minutes=duration,
                source=source,
            )

    async def handle_reset_model(call: ServiceCall) -> None:
        coordinator = await _get_coordinator()
        if coordinator:
            _LOGGER.info("Service call: reset learned model")
            await coordinator.async_reset_model()

    async def handle_rebootstrap(call: ServiceCall) -> None:
        coordinator = await _get_coordinator()
        if coordinator:
            _LOGGER.info("Service call: rebootstrap from recorder history")
            coordinator._history_bootstrap_completed = False
            coordinator._bootstrap_retry_count = 0
            await coordinator._try_history_bootstrap()
            await coordinator.async_request_refresh()

    async def handle_start_ab_trial(call: ServiceCall) -> dict:
        coordinator = await _get_coordinator()
        if not coordinator:
            return {}
        period_days = call.data.get("period_days", 3)
        total_days = call.data.get("total_days", 28)
        _LOGGER.info(
            "Service call: start A/B trial (%d day blocks, %d days total)",
            period_days,
            total_days,
        )
        return await coordinator.async_start_ab_trial(
            period_days=period_days,
            total_days=total_days,
        )

    async def handle_stop_ab_trial(call: ServiceCall) -> dict:
        coordinator = await _get_coordinator()
        if not coordinator:
            return {}
        _LOGGER.info("Service call: stop A/B trial")
        return await coordinator.async_stop_ab_trial()

    async def handle_ab_trial_result(call: ServiceCall) -> dict:
        coordinator = await _get_coordinator()
        if not coordinator:
            return {}
        return coordinator.ab_trial_result()

    hass.services.async_register(DOMAIN, SERVICE_RESET_MODEL, handle_reset_model)
    hass.services.async_register(DOMAIN, SERVICE_REBOOTSTRAP, handle_rebootstrap)
    hass.services.async_register(DOMAIN, SERVICE_FORCE_REOPTIMIZE, handle_force_reoptimize)
    hass.services.async_register(DOMAIN, SERVICE_PAUSE, handle_pause)
    hass.services.async_register(DOMAIN, SERVICE_RESUME, handle_resume)
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_OCCUPANCY,
        handle_set_occupancy,
        schema=vol.Schema({vol.Required("mode"): str}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DEMAND_RESPONSE,
        handle_demand_response,
        schema=vol.Schema(
            {
                vol.Required("mode"): vol.In(["reduce", "restore"]),
                vol.Optional("duration_minutes", default=60): vol.Coerce(int),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_MODEL,
        handle_export_model,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_MODEL,
        handle_import_model,
        schema=vol.Schema({vol.Required("model_data"): dict}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_THERMOSTAT_HISTORY,
        handle_import_thermostat_history,
        schema=vol.Schema({vol.Required("path"): str}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CONSTRAINT,
        handle_set_constraint,
        schema=vol.Schema(
            {
                vol.Required("type"): vol.In(["max_temp", "min_temp", "max_power", "pause_until"]),
                vol.Optional("value", default=0): vol.Coerce(float),
                vol.Optional("duration_minutes", default=60): vol.Coerce(int),
                vol.Optional("source", default="service_call"): str,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_AB_TRIAL,
        handle_start_ab_trial,
        # Bounds are enforced in ABTrial.start(), which clamps rather than
        # rejecting, so a slightly out-of-range automation still runs a trial.
        schema=vol.Schema(
            {
                vol.Optional("period_days", default=3): vol.Coerce(int),
                vol.Optional("total_days", default=28): vol.Coerce(int),
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_AB_TRIAL,
        handle_stop_ab_trial,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_AB_TRIAL_RESULT,
        handle_ab_trial_result,
        supports_response=SupportsResponse.ONLY,
    )


async def async_unload_services(hass: HomeAssistant) -> None:
    """Unregister services."""
    hass.services.async_remove(DOMAIN, SERVICE_FORCE_REOPTIMIZE)
    hass.services.async_remove(DOMAIN, SERVICE_PAUSE)
    hass.services.async_remove(DOMAIN, SERVICE_RESUME)
    hass.services.async_remove(DOMAIN, SERVICE_SET_OCCUPANCY)
    hass.services.async_remove(DOMAIN, SERVICE_DEMAND_RESPONSE)
    hass.services.async_remove(DOMAIN, SERVICE_EXPORT_MODEL)
    hass.services.async_remove(DOMAIN, SERVICE_IMPORT_MODEL)
    hass.services.async_remove(DOMAIN, SERVICE_SET_CONSTRAINT)
    hass.services.async_remove(DOMAIN, SERVICE_RESET_MODEL)
    hass.services.async_remove(DOMAIN, SERVICE_REBOOTSTRAP)
    hass.services.async_remove(DOMAIN, SERVICE_START_AB_TRIAL)
    hass.services.async_remove(DOMAIN, SERVICE_STOP_AB_TRIAL)
    hass.services.async_remove(DOMAIN, SERVICE_AB_TRIAL_RESULT)
    hass.services.async_remove(DOMAIN, SERVICE_IMPORT_THERMOSTAT_HISTORY)
