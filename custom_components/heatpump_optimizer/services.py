"""Service handlers for the Heat Pump Optimizer."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall

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


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register integration services."""

    async def _get_coordinator() -> HeatPumpOptimizerCoordinator | None:
        """Get the first (and typically only) coordinator instance."""
        entries = hass.data.get(DOMAIN, {})
        if not entries:
            _LOGGER.error("No Heat Pump Optimizer instances configured")
            return None
        # Return the first coordinator
        return next(iter(entries.values()))

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

    async def handle_set_constraint(call: ServiceCall) -> None:
        coordinator = await _get_coordinator()
        if coordinator:
            constraint_type = call.data["type"]
            value = call.data.get("value", 0)
            duration = call.data.get("duration_minutes", 60)
            source = call.data.get("source", "service_call")
            _LOGGER.info(
                "Service call: set_constraint type=%s value=%s duration=%d source=%s",
                constraint_type, value, duration, source,
            )
            await coordinator.async_set_constraint(
                constraint_type=constraint_type,
                value=value,
                duration_minutes=duration,
                source=source,
            )

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
        schema=vol.Schema({
            vol.Required("mode"): vol.In(["reduce", "restore"]),
            vol.Optional("duration_minutes", default=60): vol.Coerce(int),
        }),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_MODEL,
        handle_export_model,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_MODEL,
        handle_import_model,
        schema=vol.Schema({vol.Required("model_data"): dict}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CONSTRAINT,
        handle_set_constraint,
        schema=vol.Schema({
            vol.Required("type"): vol.In(["max_temp", "min_temp", "max_power", "pause_until"]),
            vol.Optional("value", default=0): vol.Coerce(float),
            vol.Optional("duration_minutes", default=60): vol.Coerce(int),
            vol.Optional("source", default="service_call"): str,
        }),
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
