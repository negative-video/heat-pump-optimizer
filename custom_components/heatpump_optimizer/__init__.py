"""Heat Pump Optimizer — HACS integration for efficiency-optimized HVAC scheduling.

Uses measured or learned thermal characteristics to shift HVAC runtime to hours
when the heat pump operates most efficiently, reducing energy consumption while
maintaining comfort bounds.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import async_register_built_in_panel, async_remove_panel
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.start import async_at_started

from .const import (
    CONF_AWAY_COMFORT_DELTA,
    CONF_CLIMATE_ENTITY,
    CONF_SETUP_COMPLEXITY,
    CONF_COMFORT_COOL_MAX,
    CONF_COMFORT_COOL_MIN,
    CONF_COMFORT_HEAT_MAX,
    CONF_COMFORT_HEAT_MIN,
    CONF_INDOOR_HUMIDITY_ENTITIES,
    CONF_INDOOR_TEMP_ENTITIES,
    CONF_INITIALIZATION_MODE,
    CONF_MAX_SETPOINT_CHANGE_PER_HOUR,
    CONF_MODEL_IMPORT_DATA,
    CONF_MONITOR_ONLY,
    CONF_OCCUPANCY_ENTITIES,
    CONF_OCCUPANCY_ENTITY,
    CONF_OPTIMIZATION_AGGRESSIVENESS,
    CONF_OUTDOOR_HUMIDITY_ENTITIES,
    CONF_OUTDOOR_TEMP_ENTITIES,
    CONF_OVERRIDE_GRACE_PERIOD_HOURS,
    CONF_PROFILE_PATH,
    CONF_REOPTIMIZE_INTERVAL_HOURS,
    CONF_SAFETY_COOL_MAX,
    CONF_SAFETY_HEAT_MIN,
    CONF_SLEEP_COMFORT_COOL_MAX,
    CONF_SLEEP_COMFORT_COOL_MIN,
    CONF_SLEEP_COMFORT_HEAT_MAX,
    CONF_SLEEP_COMFORT_HEAT_MIN,
    CONF_SLEEP_SCHEDULE_ENABLED,
    CONF_SLEEP_SCHEDULE_END,
    CONF_SLEEP_SCHEDULE_START,
    CONF_DWELL_TIME_MINUTES,
    CONF_THERMOSTAT_DEADBAND,
    CONF_WEATHER_ENTITIES,
    CONF_WEATHER_ENTITY,
    CONF_HOME_SQFT,
    CONF_HVAC_TONNAGE,
    CONF_HVAC_SEER,
    CONF_AUX_HEAT_TYPE,
    CONF_AUX_HEAT_KW,
    DEFAULT_AGGRESSIVENESS,
    DEFAULT_AWAY_COMFORT_DELTA,
    DEFAULT_COMFORT_COOL_MAX,
    DEFAULT_COMFORT_COOL_MIN,
    DEFAULT_COMFORT_HEAT_MAX,
    DEFAULT_COMFORT_HEAT_MIN,
    DEFAULT_MAX_SETPOINT_CHANGE_PER_HOUR,
    DEFAULT_OVERRIDE_GRACE_PERIOD_HOURS,
    DEFAULT_REOPTIMIZE_INTERVAL_HOURS,
    DEFAULT_SAFETY_COOL_MAX,
    DEFAULT_SAFETY_HEAT_MIN,
    DEFAULT_SLEEP_COMFORT_COOL_MAX,
    DEFAULT_SLEEP_COMFORT_COOL_MIN,
    DEFAULT_SLEEP_COMFORT_HEAT_MAX,
    DEFAULT_SLEEP_COMFORT_HEAT_MIN,
    DEFAULT_SLEEP_SCHEDULE_ENABLED,
    DEFAULT_SLEEP_SCHEDULE_END,
    DEFAULT_SLEEP_SCHEDULE_START,
    DEFAULT_DWELL_TIME_MINUTES,
    DEFAULT_THERMOSTAT_DEADBAND,
    DOMAIN,
    INIT_MODE_BEESTAT,
    INIT_MODE_LEARNING,
    PLATFORMS,
    VERSION,
)
from .coordinator import HeatPumpOptimizerCoordinator
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Heat Pump Optimizer from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Extract config — options can override data for equipment & comfort values
    climate_entity = (
        entry.options.get(CONF_CLIMATE_ENTITY)
        or entry.data[CONF_CLIMATE_ENTITY]
    )
    weather_entity = entry.data.get(CONF_WEATHER_ENTITY)

    # Weather: migrate singular → plural for backward compat
    weather_entities = (
        entry.options.get(CONF_WEATHER_ENTITIES)
        or entry.data.get(CONF_WEATHER_ENTITIES)
    )
    if not weather_entities and weather_entity:
        weather_entities = [weather_entity]
    weather_entities = weather_entities or []
    # Keep singular for backward compat with coordinator interface
    if not weather_entity and weather_entities:
        weather_entity = weather_entities[0]

    # Setup complexity (default to "full" for backward compat with existing entries)
    setup_complexity = entry.data.get(CONF_SETUP_COMPLEXITY, "full")

    # Initialization mode (default to beestat for backward compat with existing entries)
    init_mode = entry.data.get(CONF_INITIALIZATION_MODE, INIT_MODE_BEESTAT)
    profile_path = entry.data.get(CONF_PROFILE_PATH)
    model_import_data = entry.data.get(CONF_MODEL_IMPORT_DATA)

    # Occupancy: migrate singular → plural for backward compat
    occupancy_entities = (
        entry.options.get(CONF_OCCUPANCY_ENTITIES)
        or entry.data.get(CONF_OCCUPANCY_ENTITIES)
    )
    if not occupancy_entities:
        singular = entry.data.get(CONF_OCCUPANCY_ENTITY)
        if singular:
            occupancy_entities = [singular]
    # Comfort ranges — options flow overrides take precedence over initial data
    opts = dict(entry.options)

    # Merge onboarding data into opts (options flow overrides take precedence)
    for _key in (
        CONF_OUTDOOR_TEMP_ENTITIES, CONF_OUTDOOR_HUMIDITY_ENTITIES,
        CONF_INDOOR_TEMP_ENTITIES, CONF_INDOOR_HUMIDITY_ENTITIES,
        # System specs set during initial setup — editable later via options flow
        CONF_HOME_SQFT, CONF_HVAC_TONNAGE, CONF_AUX_HEAT_TYPE, CONF_AUX_HEAT_KW,
    ):
        if _key not in opts:
            _val = entry.data.get(_key)
            if _val is not None:
                opts[_key] = _val
    comfort_cool = (
        opts.get(CONF_COMFORT_COOL_MIN, entry.data.get(CONF_COMFORT_COOL_MIN, DEFAULT_COMFORT_COOL_MIN)),
        opts.get(CONF_COMFORT_COOL_MAX, entry.data.get(CONF_COMFORT_COOL_MAX, DEFAULT_COMFORT_COOL_MAX)),
    )
    comfort_heat = (
        opts.get(CONF_COMFORT_HEAT_MIN, entry.data.get(CONF_COMFORT_HEAT_MIN, DEFAULT_COMFORT_HEAT_MIN)),
        opts.get(CONF_COMFORT_HEAT_MAX, entry.data.get(CONF_COMFORT_HEAT_MAX, DEFAULT_COMFORT_HEAT_MAX)),
    )

    # Safety limits — options flow overrides take precedence
    safety_limits = (
        opts.get(CONF_SAFETY_HEAT_MIN, entry.data.get(CONF_SAFETY_HEAT_MIN, DEFAULT_SAFETY_HEAT_MIN)),
        opts.get(CONF_SAFETY_COOL_MAX, entry.data.get(CONF_SAFETY_COOL_MAX, DEFAULT_SAFETY_COOL_MAX)),
    )

    # Behavior parameters from options
    behavior = {
        "aggressiveness": opts.get(CONF_OPTIMIZATION_AGGRESSIVENESS, DEFAULT_AGGRESSIVENESS),
        "override_grace_hours": opts.get(CONF_OVERRIDE_GRACE_PERIOD_HOURS, DEFAULT_OVERRIDE_GRACE_PERIOD_HOURS),
        "reoptimize_interval_hours": opts.get(CONF_REOPTIMIZE_INTERVAL_HOURS, DEFAULT_REOPTIMIZE_INTERVAL_HOURS),
        "max_setpoint_change_per_hour": opts.get(CONF_MAX_SETPOINT_CHANGE_PER_HOUR, DEFAULT_MAX_SETPOINT_CHANGE_PER_HOUR),
        "away_comfort_delta": opts.get(CONF_AWAY_COMFORT_DELTA, DEFAULT_AWAY_COMFORT_DELTA),
        "thermostat_deadband": opts.get(CONF_THERMOSTAT_DEADBAND, DEFAULT_THERMOSTAT_DEADBAND),
        "dwell_time_minutes": opts.get(CONF_DWELL_TIME_MINUTES, DEFAULT_DWELL_TIME_MINUTES),
    }

    # Sleep schedule (optional — tighter comfort during sleeping hours)
    sleep_config = {
        "enabled": opts.get(CONF_SLEEP_SCHEDULE_ENABLED, DEFAULT_SLEEP_SCHEDULE_ENABLED),
        "start": opts.get(CONF_SLEEP_SCHEDULE_START, DEFAULT_SLEEP_SCHEDULE_START),
        "end": opts.get(CONF_SLEEP_SCHEDULE_END, DEFAULT_SLEEP_SCHEDULE_END),
        "comfort_cool": (
            opts.get(CONF_SLEEP_COMFORT_COOL_MIN, DEFAULT_SLEEP_COMFORT_COOL_MIN),
            opts.get(CONF_SLEEP_COMFORT_COOL_MAX, DEFAULT_SLEEP_COMFORT_COOL_MAX),
        ),
        "comfort_heat": (
            opts.get(CONF_SLEEP_COMFORT_HEAT_MIN, DEFAULT_SLEEP_COMFORT_HEAT_MIN),
            opts.get(CONF_SLEEP_COMFORT_HEAT_MAX, DEFAULT_SLEEP_COMFORT_HEAT_MAX),
        ),
    }

    # Pre-read beestat profile off the event loop (blocking I/O)
    profile_json: str | None = None
    if profile_path and init_mode == INIT_MODE_BEESTAT:
        try:
            profile_json = await hass.async_add_executor_job(
                Path(profile_path).read_text
            )
        except (FileNotFoundError, OSError) as err:
            _LOGGER.error("Cannot read Beestat profile '%s': %s", profile_path, err)

    # Monitor-only mode: run full pipeline without writing setpoints
    monitor_only = opts.get(CONF_MONITOR_ONLY, entry.data.get(CONF_MONITOR_ONLY, False))

    # Create coordinator
    coordinator = HeatPumpOptimizerCoordinator(
        hass,
        profile_path=profile_path,
        profile_json=profile_json,
        climate_entity_id=climate_entity,
        weather_entity_id=weather_entity,
        weather_entity_ids=weather_entities,
        comfort_cool=comfort_cool,
        comfort_heat=comfort_heat,
        safety_limits=safety_limits,
        occupancy_entity_ids=occupancy_entities,
        options=opts,
        initialization_mode=init_mode,
        model_import_data=model_import_data,
        behavior=behavior,
        sleep_config=sleep_config,
        monitor_only=monitor_only,
        setup_complexity=setup_complexity,
    )

    # Initialize (loads persisted state, starts watchdog, runs first optimization)
    await coordinator.async_setup()

    # First data refresh
    await coordinator.async_config_entry_first_refresh()

    # Guard: if the climate entity isn't available yet, defer setup so HA
    # retries with exponential backoff rather than running with broken metrics.
    thermo_state = coordinator.thermostat.read_state()
    if not thermo_state.available:
        raise ConfigEntryNotReady(
            f"Climate entity '{climate_entity}' not yet available — "
            "will retry automatically"
        )

    # Once HA is fully started (all integrations loaded), trigger a refresh
    # so the coordinator can pick up weather entities that weren't ready yet,
    # and attempt history bootstrap (recorder guaranteed ready at this point).
    async def _on_ha_started(_hass):
        await coordinator.async_try_history_bootstrap_if_needed()
        await coordinator.async_request_refresh()

    async_at_started(hass, _on_ha_started)

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Register services and sidebar panel (only once for the domain)
    if len(hass.data[DOMAIN]) == 1:
        await async_setup_services(hass)
        await _async_register_panel(hass)

    # Forward setup to sensor, binary_sensor, and switch platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry — write safe defaults and clean up."""
    coordinator: HeatPumpOptimizerCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Graceful shutdown: write safe setpoint, persist state
    await coordinator.async_shutdown()

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    # Unregister services and panel if no more instances
    if not hass.data[DOMAIN]:
        await async_unload_services(hass)
        async_remove_panel(hass, DOMAIN)

    return unload_ok


async def _async_register_panel(hass: HomeAssistant) -> None:
    """Register the sidebar dashboard panel."""
    frontend_path = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(f"/api/{DOMAIN}/frontend", str(frontend_path), cache_headers=False)]
    )
    async_register_built_in_panel(
        hass,
        component_name="custom",
        sidebar_title="Heat Pump",
        sidebar_icon="mdi:heat-pump-outline",
        frontend_url_path=DOMAIN,
        config={
            "_panel_custom": {
                "name": "heatpump-optimizer-panel",
                "module_url": f"/api/{DOMAIN}/frontend/panel.js?v={VERSION}",
            }
        },
    )
