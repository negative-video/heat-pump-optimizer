"""Config flow for Heat Pump Optimizer."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .adapters.area_occupancy import AreaOccupancyManager
from .adapters.entity_discovery import EntityDiscovery
from .const import (
    AGGRESSIVENESS_AGGRESSIVE,
    AGGRESSIVENESS_BALANCED,
    AGGRESSIVENESS_CONSERVATIVE,
    CONF_AREA_SENSOR_CONFIG,
    CONF_AUXILIARY_APPLIANCES,
    CONF_AWAY_COMFORT_DELTA,
    CONF_BAROMETRIC_PRESSURE_ENTITY,
    CONF_CALENDAR_AWAY_KEYWORDS,
    CONF_CALENDAR_DEFAULT_MODE,
    CONF_CALENDAR_ENTITIES,
    CONF_CALENDAR_ENTITY,
    CONF_CALENDAR_HOME_KEYWORDS,
    CONF_CARBON_WEIGHT,
    CONF_ATTIC_TEMP_ENTITY,
    CONF_CLIMATE_ENTITY,
    CONF_CO2_ENTITY,
    CONF_COMFORT_COOL_MAX,
    CONF_COMFORT_COOL_MIN,
    CONF_COMFORT_HEAT_MAX,
    CONF_COMFORT_HEAT_MIN,
    CONF_COST_WEIGHT,
    CONF_CRAWLSPACE_TEMP_ENTITY,
    CONF_DEMAND_RESPONSE_ENTITY,
    CONF_DEPARTURE_PROFILES,
    CONF_DEPARTURE_TRIGGER_WINDOW_MINUTES,
    CONF_DOOR_WINDOW_ENTITIES,
    CONF_DEPARTURE_ZONE,
    CONF_DEPARTURE_ZONES,
    CONF_ELECTRICITY_FLAT_RATE,
    CONF_ELECTRICITY_RATE_ENTITY,
    CONF_GRID_IMPORT_ENTITY,
    CONF_HVAC_POWER_DEFAULT_WATTS,
    CONF_HVAC_POWER_ENTITY,
    CONF_INDOOR_HUMIDITY_ENTITIES,
    CONF_INDOOR_TEMP_ENTITIES,
    CONF_INDOOR_WEIGHTING_MODE,
    CONF_INITIALIZATION_MODE,
    CONF_MAX_SETPOINT_CHANGE_PER_HOUR,
    CONF_MODEL_IMPORT_DATA,
    CONF_OCCUPANCY_DEBOUNCE_MINUTES,
    CONF_OCCUPANCY_ENTITIES,
    CONF_OCCUPIED_WEIGHT_MULTIPLIER,
    CONF_OPTIMIZATION_AGGRESSIVENESS,
    CONF_PRECONDITIONING_BUFFER_MINUTES,
    CONF_OUTDOOR_HUMIDITY_ENTITIES,
    CONF_OUTDOOR_TEMP_ENTITIES,
    CONF_OVERRIDE_GRACE_PERIOD_HOURS,
    CONF_PROFILE_PATH,
    CONF_REOPTIMIZE_INTERVAL_HOURS,
    CONF_SAFETY_COOL_MAX,
    CONF_SAFETY_HEAT_MIN,
    CONF_SOLAR_EXPORT_RATE_ENTITY,
    CONF_SOLAR_IRRADIANCE_ENTITY,
    CONF_SOLAR_PRODUCTION_ENTITY,
    CONF_SUN_ENTITY,
    CONF_TOU_SCHEDULE,
    CONF_ROOM_OCCUPANCY_DEBOUNCE_MINUTES,
    CONF_TRAVEL_TIME_SENSOR,
    CONF_TRAVEL_TIME_SENSORS,
    CONF_USE_ADAPTIVE_MODEL,
    CONF_USE_GREYBOX_MODEL,
    CONF_WEATHER_ENTITIES,
    CONF_WEATHER_ENTITY,
    CONF_WIND_SPEED_ENTITY,
    DEFAULT_AGGRESSIVENESS,
    DEFAULT_AWAY_COMFORT_DELTA,
    DEFAULT_CALENDAR_AWAY_KEYWORDS,
    DEFAULT_CALENDAR_DEFAULT_MODE,
    DEFAULT_CALENDAR_HOME_KEYWORDS,
    DEFAULT_CARBON_WEIGHT,
    DEFAULT_COMFORT_COOL_MAX,
    DEFAULT_COMFORT_COOL_MIN,
    DEFAULT_COMFORT_HEAT_MAX,
    DEFAULT_COMFORT_HEAT_MIN,
    DEFAULT_COST_WEIGHT,
    DEFAULT_DEPARTURE_TRIGGER_WINDOW_MINUTES,
    DEFAULT_HVAC_POWER_WATTS,
    DEFAULT_INDOOR_WEIGHTING_MODE,
    DEFAULT_MAX_SETPOINT_CHANGE_PER_HOUR,
    DEFAULT_OCCUPANCY_DEBOUNCE_MINUTES,
    DEFAULT_OCCUPIED_WEIGHT_MULTIPLIER,
    DEFAULT_OVERRIDE_GRACE_PERIOD_HOURS,
    DEFAULT_PRECONDITIONING_BUFFER_MINUTES,
    DEFAULT_REOPTIMIZE_INTERVAL_HOURS,
    DEFAULT_ROOM_OCCUPANCY_DEBOUNCE_MINUTES,
    DEFAULT_SAFETY_COOL_MAX,
    DEFAULT_SAFETY_HEAT_MIN,
    DEFAULT_SUN_ENTITY,
    DOMAIN,
    WEIGHTING_MODE_EQUAL,
    WEIGHTING_MODE_OCCUPIED_ONLY,
    WEIGHTING_MODE_WEIGHTED,
    INIT_MODE_BEESTAT,
    INIT_MODE_IMPORT,
    INIT_MODE_LEARNING,
)

_LOGGER = logging.getLogger(__name__)


def _validate_profile(path: str) -> str | None:
    """Validate a Beestat temperature profile JSON file.

    Returns None if valid, or an error key string matching strings.json.
    """
    if not os.path.isfile(path):
        _LOGGER.error("Beestat profile not found at path: %s", path)
        return "profile_not_found"
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as err:
        _LOGGER.error("Beestat profile parse error at %s: %s", path, err)
        return "profile_parse_error"

    # Check required keys that PerformanceModel.__init__ expects
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


def _validate_comfort_ranges(user_input: dict[str, Any]) -> dict[str, str]:
    """Validate that comfort/safety temperature ranges are logically consistent.

    Returns an empty dict if valid, or {field: error_key} for the first
    violation found (HA only shows one error per field).
    """
    errors: dict[str, str] = {}

    cool_min = user_input.get(CONF_COMFORT_COOL_MIN, DEFAULT_COMFORT_COOL_MIN)
    cool_max = user_input.get(CONF_COMFORT_COOL_MAX, DEFAULT_COMFORT_COOL_MAX)
    heat_min = user_input.get(CONF_COMFORT_HEAT_MIN, DEFAULT_COMFORT_HEAT_MIN)
    heat_max = user_input.get(CONF_COMFORT_HEAT_MAX, DEFAULT_COMFORT_HEAT_MAX)
    safety_heat = user_input.get(CONF_SAFETY_HEAT_MIN, DEFAULT_SAFETY_HEAT_MIN)
    safety_cool = user_input.get(CONF_SAFETY_COOL_MAX, DEFAULT_SAFETY_COOL_MAX)

    if cool_min >= cool_max:
        errors[CONF_COMFORT_COOL_MIN] = "cool_range_inverted"
    if heat_min >= heat_max:
        errors[CONF_COMFORT_HEAT_MIN] = "heat_range_inverted"
    if safety_heat >= heat_min:
        errors[CONF_SAFETY_HEAT_MIN] = "safety_above_comfort"
    if cool_max >= safety_cool:
        errors[CONF_COMFORT_COOL_MAX] = "comfort_above_safety"

    return errors


def _validate_model_import(data_str: str) -> str | None:
    """Validate imported model JSON data.

    Returns None if valid, error string if invalid.
    """
    try:
        data = json.loads(data_str)
        if not isinstance(data, dict):
            return "Model data must be a JSON object"
        if "estimator_state" not in data and "state_mean" not in data:
            return "Missing estimator state in model data"
    except (json.JSONDecodeError, TypeError) as err:
        return f"Invalid JSON: {err}"
    return None


# ─────────────────────────────────────────────────────────────────────
# Config Flow — 3-step initial setup
# ─────────────────────────────────────────────────────────────────────


class HeatPumpOptimizerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Heat Pump Optimizer."""

    VERSION = 1

    @staticmethod
    async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
        """Migrate old config entries to current version."""
        if config_entry.version > HeatPumpOptimizerConfigFlow.VERSION:
            # Downgrade not supported
            return False

        if config_entry.version == 1:
            # Current version — no migration needed.
            pass

        _LOGGER.info(
            "Migration of entry %s to version %s successful",
            config_entry.entry_id,
            HeatPumpOptimizerConfigFlow.VERSION,
        )
        return True

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._config_data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow handler."""
        return HeatPumpOptimizerOptionsFlow()

    # ── Step 1: Equipment ────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Select thermostat and weather source(s)."""
        if user_input is not None:
            # Store weather entities as list; keep singular for backward compat
            weather_entities = user_input.get(CONF_WEATHER_ENTITIES, [])
            if weather_entities:
                user_input[CONF_WEATHER_ENTITY] = weather_entities[0]
            self._config_data.update(user_input)
            return await self.async_step_air_sensors()

        # Auto-discover entities for smart defaults
        discovery = EntityDiscovery(self.hass)
        climate_suggestions = discovery.discover_climate_entities()
        weather_suggestions = discovery.discover_weather_entities()

        # Pre-select the highest-confidence climate entity
        suggested_climate = (
            climate_suggestions[0].entity_id if climate_suggestions else None
        )
        # Pre-select all weather entities (first = primary)
        suggested_weather = [s.entity_id for s in weather_suggestions]

        schema: dict[Any, Any] = {}

        if suggested_climate:
            schema[vol.Required(
                CONF_CLIMATE_ENTITY,
                description={"suggested_value": suggested_climate},
            )] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="climate"),
            )
        else:
            schema[vol.Required(CONF_CLIMATE_ENTITY)] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="climate"),
            )

        if suggested_weather:
            schema[vol.Required(
                CONF_WEATHER_ENTITIES,
                description={"suggested_value": suggested_weather},
            )] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="weather", multiple=True
                ),
            )
        else:
            schema[vol.Required(CONF_WEATHER_ENTITIES)] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="weather", multiple=True
                ),
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema),
        )

    # ── Step 2: Air Sensors ─────────────────────────────────────────

    async def async_step_air_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Optional indoor/outdoor air sensors (recommended)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = {k: v for k, v in user_input.items() if v != "" and v != []}

            # Prevent same sensor from being used as both indoor and outdoor
            outdoor_temps = set(user_input.get(CONF_OUTDOOR_TEMP_ENTITIES, []))
            indoor_temps = set(user_input.get(CONF_INDOOR_TEMP_ENTITIES, []))
            outdoor_hum = set(user_input.get(CONF_OUTDOOR_HUMIDITY_ENTITIES, []))
            indoor_hum = set(user_input.get(CONF_INDOOR_HUMIDITY_ENTITIES, []))
            if outdoor_temps & indoor_temps or outdoor_hum & indoor_hum:
                errors["base"] = "sensor_overlap"

            if not errors:
                self._config_data.update(user_input)
                return await self.async_step_presence_setup()

        # Auto-discover sensors for smart defaults
        discovery = EntityDiscovery(self.hass)

        def _suggest(conf_key, suggestions, max_count=2):
            existing = self._config_data.get(conf_key, [])
            if existing:
                return existing
            high = [s.entity_id for s in suggestions if s.confidence == "high"]
            return high[:max_count]

        indoor_temp_default = _suggest(
            CONF_INDOOR_TEMP_ENTITIES, discovery.discover_temp_sensors(outdoor=False)
        )
        indoor_humidity_default = _suggest(
            CONF_INDOOR_HUMIDITY_ENTITIES, discovery.discover_humidity_sensors(outdoor=False)
        )
        outdoor_temp_default = _suggest(
            CONF_OUTDOOR_TEMP_ENTITIES, discovery.discover_temp_sensors(outdoor=True)
        )
        outdoor_humidity_default = _suggest(
            CONF_OUTDOOR_HUMIDITY_ENTITIES, discovery.discover_humidity_sensors(outdoor=True)
        )

        return self.async_show_form(
            step_id="air_sensors",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_INDOOR_TEMP_ENTITIES,
                        default=indoor_temp_default,
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            device_class="temperature",
                            multiple=True,
                        ),
                    ),
                    vol.Optional(
                        CONF_INDOOR_HUMIDITY_ENTITIES,
                        default=indoor_humidity_default,
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            device_class="humidity",
                            multiple=True,
                        ),
                    ),
                    vol.Optional(
                        CONF_OUTDOOR_TEMP_ENTITIES,
                        default=outdoor_temp_default,
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            device_class="temperature",
                            multiple=True,
                        ),
                    ),
                    vol.Optional(
                        CONF_OUTDOOR_HUMIDITY_ENTITIES,
                        default=outdoor_humidity_default,
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            device_class="humidity",
                            multiple=True,
                        ),
                    ),
                }
            ),
        )

    # ── Step 3: Presence ──────────────────────────────────────────────

    async def async_step_presence_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: Optional presence/person entities for home/away (recommended)."""
        if user_input is not None:
            user_input = {k: v for k, v in user_input.items() if v != "" and v != []}
            self._config_data.update(user_input)
            return await self.async_step_thermal_profile()

        # Auto-discover person entities
        discovery = EntityDiscovery(self.hass)
        person_suggestions = discovery.discover_person_entities()
        occupancy_default = [
            s.entity_id for s in person_suggestions
            if s.confidence == "high"
        ]

        return self.async_show_form(
            step_id="presence_setup",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_OCCUPANCY_ENTITIES,
                        default=occupancy_default,
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=[
                                "person",
                                "binary_sensor",
                                "input_select",
                                "device_tracker",
                            ],
                            multiple=True,
                        ),
                    ),
                }
            ),
        )

    # ── Step 4: Thermal Profile ──────────────────────────────────────

    async def async_step_thermal_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2a: Choose initialization method for the thermal model."""
        if user_input is not None:
            mode = user_input.get(CONF_INITIALIZATION_MODE, INIT_MODE_LEARNING)
            self._config_data[CONF_INITIALIZATION_MODE] = mode

            if mode == INIT_MODE_BEESTAT:
                return await self.async_step_thermal_profile_beestat()
            if mode == INIT_MODE_IMPORT:
                return await self.async_step_thermal_profile_import()
            # Learning mode — no extra fields needed
            return await self.async_step_comfort()

        return self.async_show_form(
            step_id="thermal_profile",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_INITIALIZATION_MODE, default=INIT_MODE_LEARNING
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=INIT_MODE_LEARNING,
                                    label="Learn automatically (recommended)",
                                ),
                                selector.SelectOptionDict(
                                    value=INIT_MODE_BEESTAT,
                                    label="Import Beestat profile (faster startup)",
                                ),
                                selector.SelectOptionDict(
                                    value=INIT_MODE_IMPORT,
                                    label="Restore exported model",
                                ),
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        ),
                    ),
                }
            ),
        )

    async def async_step_thermal_profile_beestat(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2b: Provide Beestat temperature profile file path."""
        errors: dict[str, str] = {}

        if user_input is not None:
            profile_path = user_input.get(CONF_PROFILE_PATH, "").strip().strip("'\"")
            if not profile_path:
                errors[CONF_PROFILE_PATH] = "profile_not_found"
            else:
                validation_error = await self.hass.async_add_executor_job(
                    _validate_profile, profile_path
                )
                if validation_error:
                    errors[CONF_PROFILE_PATH] = validation_error
                    _LOGGER.error(
                        "Profile validation failed: %s", validation_error
                    )
                else:
                    self._config_data[CONF_PROFILE_PATH] = profile_path

            if not errors:
                return await self.async_step_comfort()

        return self.async_show_form(
            step_id="thermal_profile_beestat",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PROFILE_PATH): str,
                }
            ),
            errors=errors,
        )

    async def async_step_thermal_profile_import(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2c: Paste exported model JSON data."""
        errors: dict[str, str] = {}

        if user_input is not None:
            model_data_str = user_input.get(CONF_MODEL_IMPORT_DATA, "")
            if not model_data_str:
                errors[CONF_MODEL_IMPORT_DATA] = "invalid_model_data"
            else:
                validation_error = _validate_model_import(model_data_str)
                if validation_error:
                    errors[CONF_MODEL_IMPORT_DATA] = "invalid_model_data"
                    _LOGGER.error(
                        "Model import validation failed: %s", validation_error
                    )
                else:
                    self._config_data[CONF_MODEL_IMPORT_DATA] = model_data_str

            if not errors:
                return await self.async_step_comfort()

        return self.async_show_form(
            step_id="thermal_profile_import",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODEL_IMPORT_DATA): selector.TextSelector(
                        selector.TextSelectorConfig(multiline=True),
                    ),
                }
            ),
            errors=errors,
        )

    # ── Step 5: Temperature Boundaries ───────────────────────────────

    async def async_step_comfort(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 5: Configure safety limits and optimization range."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_comfort_ranges(user_input)
            if not errors:
                self._config_data.update(user_input)
                return self.async_create_entry(
                    title="Heat Pump Optimizer",
                    data=self._config_data,
                )

        return self.async_show_form(
            step_id="comfort",
            errors=errors,
            data_schema=vol.Schema(
                {
                    # Safety limits (absolute guardrails)
                    vol.Optional(
                        CONF_SAFETY_HEAT_MIN,
                        default=DEFAULT_SAFETY_HEAT_MIN,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=35, max=65, step=1,
                            unit_of_measurement="°F",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                    vol.Optional(
                        CONF_SAFETY_COOL_MAX,
                        default=DEFAULT_SAFETY_COOL_MAX,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=75, max=100, step=1,
                            unit_of_measurement="°F",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                    # Optimization range (where the optimizer works when home)
                    vol.Optional(
                        CONF_COMFORT_COOL_MIN,
                        default=DEFAULT_COMFORT_COOL_MIN,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=58, max=80, step=1,
                            unit_of_measurement="°F",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                    vol.Optional(
                        CONF_COMFORT_COOL_MAX,
                        default=DEFAULT_COMFORT_COOL_MAX,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=70, max=88, step=1,
                            unit_of_measurement="°F",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                    vol.Optional(
                        CONF_COMFORT_HEAT_MIN,
                        default=DEFAULT_COMFORT_HEAT_MIN,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=45, max=72, step=1,
                            unit_of_measurement="°F",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                    vol.Optional(
                        CONF_COMFORT_HEAT_MAX,
                        default=DEFAULT_COMFORT_HEAT_MAX,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=55, max=78, step=1,
                            unit_of_measurement="°F",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                }
            ),
        )


# ─────────────────────────────────────────────────────────────────────
# Options Flow — menu-based with 5 focused sub-steps
# ─────────────────────────────────────────────────────────────────────


class HeatPumpOptimizerOptionsFlow(OptionsFlow):
    """Options flow with categorized menu."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    @staticmethod
    def _strip_empty_strings(user_input: dict[str, Any]) -> dict[str, Any]:
        """Remove keys with empty string values so optional EntitySelectors don't reject them."""
        return {k: v for k, v in user_input.items() if v != ""}

    # ── Menu ─────────────────────────────────────────────────────────

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options menu."""
        if not self._options:
            self._options = dict(self.config_entry.options)
        return self.async_show_menu(
            step_id="init",
            menu_options=["equipment", "outdoor_sensors", "indoor_sensing", "presence", "energy", "behavior", "comfort", "schedule", "appliances"],
        )

    # ── Helpers ──────────────────────────────────────────────────────

    def _get_own_entity_ids(self) -> list[str]:
        """Return entity IDs created by this integration entry.

        Used to exclude our own sensors from entity pickers so the user
        doesn't accidentally select an integration output as an input.
        """
        from homeassistant.helpers import entity_registry
        ent_reg = entity_registry.async_get(self.hass)
        return [
            entry.entity_id
            for entry in ent_reg.entities.values()
            if entry.config_entry_id == self.config_entry.entry_id
        ]

    # ── Equipment ──────────────────────────────────────────────────

    async def async_step_equipment(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change thermostat or weather source without losing learned data."""
        if user_input is not None:
            user_input = self._strip_empty_strings(user_input)
            self._options.update(user_input)
            return self.async_create_entry(title="", data=self._options)

        # Read current values from entry data (initial setup)
        data = self.config_entry.data

        current_climate = self._options.get(
            CONF_CLIMATE_ENTITY, data.get(CONF_CLIMATE_ENTITY)
        )
        current_weather = self._options.get(
            CONF_WEATHER_ENTITIES,
            data.get(CONF_WEATHER_ENTITIES, [data.get(CONF_WEATHER_ENTITY, "")]),
        )

        return self.async_show_form(
            step_id="equipment",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CLIMATE_ENTITY,
                        default=current_climate,
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="climate"),
                    ),
                    vol.Required(
                        CONF_WEATHER_ENTITIES,
                        default=current_weather,
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="weather", multiple=True
                        ),
                    ),
                }
            ),
        )

    # ── Outdoor & Building Sensors ───────────────────────────────────

    async def async_step_outdoor_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Outdoor and building envelope sensor configuration."""
        if user_input is not None:
            user_input = self._strip_empty_strings(user_input)
            self._options.update(user_input)
            return self.async_create_entry(title="", data=self._options)

        # Run discovery for smart defaults on empty fields
        discovery = EntityDiscovery(self.hass)
        exclude = self._get_own_entity_ids()

        def _suggest_multi(conf_key, suggestions, max_count=2):
            """Return high-confidence entity IDs if the user hasn't configured any yet."""
            existing = self._options.get(conf_key, [])
            if existing:
                return existing
            high = [s.entity_id for s in suggestions if s.confidence == "high"]
            return high[:max_count]

        outdoor_temp_default = _suggest_multi(
            CONF_OUTDOOR_TEMP_ENTITIES, discovery.discover_temp_sensors(outdoor=True)
        )
        outdoor_humidity_default = _suggest_multi(
            CONF_OUTDOOR_HUMIDITY_ENTITIES, discovery.discover_humidity_sensors(outdoor=True)
        )

        return self.async_show_form(
            step_id="outdoor_sensors",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_OUTDOOR_TEMP_ENTITIES,
                        default=outdoor_temp_default,
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            device_class="temperature",
                            multiple=True,
                            exclude_entities=exclude,
                        ),
                    ),
                    vol.Optional(
                        CONF_OUTDOOR_HUMIDITY_ENTITIES,
                        default=outdoor_humidity_default,
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            device_class="humidity",
                            multiple=True,
                            exclude_entities=exclude,
                        ),
                    ),
                    vol.Optional(
                        CONF_WIND_SPEED_ENTITY,
                        description={"suggested_value": self._options.get(CONF_WIND_SPEED_ENTITY)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            exclude_entities=exclude,
                        ),
                    ),
                    vol.Optional(
                        CONF_SOLAR_IRRADIANCE_ENTITY,
                        description={"suggested_value": self._options.get(CONF_SOLAR_IRRADIANCE_ENTITY)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            device_class="irradiance",
                            exclude_entities=exclude,
                        ),
                    ),
                    vol.Optional(
                        CONF_BAROMETRIC_PRESSURE_ENTITY,
                        description={"suggested_value": self._options.get(CONF_BAROMETRIC_PRESSURE_ENTITY)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            device_class="atmospheric_pressure",
                            exclude_entities=exclude,
                        ),
                    ),
                    vol.Optional(
                        CONF_SUN_ENTITY,
                        default=self._options.get(CONF_SUN_ENTITY, DEFAULT_SUN_ENTITY),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sun"),
                    ),
                    vol.Optional(
                        CONF_DOOR_WINDOW_ENTITIES,
                        default=self._options.get(CONF_DOOR_WINDOW_ENTITIES, []),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="binary_sensor",
                            multiple=True,
                        ),
                    ),
                    vol.Optional(
                        CONF_ATTIC_TEMP_ENTITY,
                        description={"suggested_value": self._options.get(CONF_ATTIC_TEMP_ENTITY)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            device_class="temperature",
                            exclude_entities=exclude,
                        ),
                    ),
                    vol.Optional(
                        CONF_CRAWLSPACE_TEMP_ENTITY,
                        description={"suggested_value": self._options.get(CONF_CRAWLSPACE_TEMP_ENTITY)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            device_class="temperature",
                            exclude_entities=exclude,
                        ),
                    ),
                }
            ),
        )

    # ── Energy & Cost ────────────────────────────────────────────────

    async def async_step_energy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Power monitoring, CO2, electricity rates, optimization weights."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = self._strip_empty_strings(user_input)
            # Validate TOU schedule JSON if provided
            tou_raw = user_input.get(CONF_TOU_SCHEDULE, "").strip()
            if tou_raw:
                try:
                    tou_data = json.loads(tou_raw)
                    if not isinstance(tou_data, list):
                        errors[CONF_TOU_SCHEDULE] = "tou_not_array"
                    else:
                        for entry in tou_data:
                            if not isinstance(entry, dict):
                                errors[CONF_TOU_SCHEDULE] = "tou_invalid_entry"
                                break
                            if "rate" not in entry:
                                errors[CONF_TOU_SCHEDULE] = "tou_missing_rate"
                                break
                except json.JSONDecodeError:
                    errors[CONF_TOU_SCHEDULE] = "tou_invalid_json"
            if not errors:
                self._options.update(user_input)
                return self.async_create_entry(title="", data=self._options)

        # Discover power, solar, CO2, and rate sensors for smart defaults
        discovery = EntityDiscovery(self.hass)
        exclude = self._get_own_entity_ids()

        def _suggest_single(conf_key, suggestions):
            """Return first discovered entity_id if user hasn't configured one."""
            existing = self._options.get(conf_key)
            if existing:
                return existing
            return suggestions[0].entity_id if suggestions else None

        power_default = _suggest_single(
            CONF_HVAC_POWER_ENTITY, discovery.discover_power_sensors()
        )
        solar_default = _suggest_single(
            CONF_SOLAR_PRODUCTION_ENTITY, discovery.discover_solar_sensors()
        )
        co2_default = _suggest_single(
            CONF_CO2_ENTITY, discovery.discover_co2_sensors()
        )
        rate_default = _suggest_single(
            CONF_ELECTRICITY_RATE_ENTITY, discovery.discover_electricity_rate_sensors()
        )

        return self.async_show_form(
            step_id="energy",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_HVAC_POWER_ENTITY,
                        description={"suggested_value": power_default},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            exclude_entities=exclude,
                        ),
                    ),
                    vol.Optional(
                        CONF_HVAC_POWER_DEFAULT_WATTS,
                        default=self._options.get(
                            CONF_HVAC_POWER_DEFAULT_WATTS, DEFAULT_HVAC_POWER_WATTS
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=500, max=20000, step=100,
                            unit_of_measurement="W",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                    vol.Optional(
                        CONF_CO2_ENTITY,
                        description={"suggested_value": co2_default},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            exclude_entities=exclude,
                        ),
                    ),
                    vol.Optional(
                        CONF_ELECTRICITY_RATE_ENTITY,
                        description={"suggested_value": rate_default},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["sensor", "input_number"],
                            exclude_entities=exclude,
                        ),
                    ),
                    vol.Optional(
                        CONF_ELECTRICITY_FLAT_RATE,
                        default=self._options.get(CONF_ELECTRICITY_FLAT_RATE, 0.0),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.0, max=2.0, step=0.01,
                            unit_of_measurement="$/kWh",
                            mode=selector.NumberSelectorMode.BOX,
                        ),
                    ),
                    vol.Optional(
                        CONF_CARBON_WEIGHT,
                        default=self._options.get(
                            CONF_CARBON_WEIGHT, DEFAULT_CARBON_WEIGHT
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.0, max=1.0, step=0.1, mode=selector.NumberSelectorMode.SLIDER
                        ),
                    ),
                    vol.Optional(
                        CONF_COST_WEIGHT,
                        default=self._options.get(
                            CONF_COST_WEIGHT, DEFAULT_COST_WEIGHT
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.0, max=1.0, step=0.1, mode=selector.NumberSelectorMode.SLIDER
                        ),
                    ),
                    vol.Optional(
                        CONF_SOLAR_PRODUCTION_ENTITY,
                        description={"suggested_value": solar_default},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            device_class="power",
                            exclude_entities=exclude,
                        ),
                    ),
                    vol.Optional(
                        CONF_GRID_IMPORT_ENTITY,
                        description={"suggested_value": self._options.get(CONF_GRID_IMPORT_ENTITY)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            device_class="power",
                            exclude_entities=exclude,
                        ),
                    ),
                    vol.Optional(
                        CONF_SOLAR_EXPORT_RATE_ENTITY,
                        description={"suggested_value": self._options.get(CONF_SOLAR_EXPORT_RATE_ENTITY)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            exclude_entities=exclude,
                        ),
                    ),
                    vol.Optional(
                        CONF_TOU_SCHEDULE,
                        description={"suggested_value": self._options.get(CONF_TOU_SCHEDULE, "")},
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(multiline=True),
                    ),
                    vol.Optional(
                        CONF_DEMAND_RESPONSE_ENTITY,
                        description={"suggested_value": self._options.get(CONF_DEMAND_RESPONSE_ENTITY)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["input_boolean", "binary_sensor"],
                            exclude_entities=exclude,
                        ),
                    ),
                }
            ),
            errors=errors,
        )

    # ── Behavior ─────────────────────────────────────────────────────

    async def async_step_behavior(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Aggressiveness, override grace, reopt interval, model toggles."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if (
                user_input.get(CONF_USE_GREYBOX_MODEL)
                and not user_input.get(CONF_USE_ADAPTIVE_MODEL)
            ):
                errors[CONF_USE_GREYBOX_MODEL] = "greybox_requires_adaptive"
            if not errors:
                self._options.update(user_input)
                return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="behavior",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_OPTIMIZATION_AGGRESSIVENESS,
                        default=self._options.get(
                            CONF_OPTIMIZATION_AGGRESSIVENESS, DEFAULT_AGGRESSIVENESS
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=AGGRESSIVENESS_CONSERVATIVE,
                                    label="Conservative",
                                ),
                                selector.SelectOptionDict(
                                    value=AGGRESSIVENESS_BALANCED,
                                    label="Balanced (recommended)",
                                ),
                                selector.SelectOptionDict(
                                    value=AGGRESSIVENESS_AGGRESSIVE,
                                    label="Aggressive",
                                ),
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        ),
                    ),
                    vol.Optional(
                        CONF_OVERRIDE_GRACE_PERIOD_HOURS,
                        default=self._options.get(
                            CONF_OVERRIDE_GRACE_PERIOD_HOURS,
                            DEFAULT_OVERRIDE_GRACE_PERIOD_HOURS,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.5,
                            max=8.0,
                            step=0.5,
                            unit_of_measurement="hours",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                    vol.Optional(
                        CONF_REOPTIMIZE_INTERVAL_HOURS,
                        default=self._options.get(
                            CONF_REOPTIMIZE_INTERVAL_HOURS,
                            DEFAULT_REOPTIMIZE_INTERVAL_HOURS,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1,
                            max=8,
                            step=1,
                            unit_of_measurement="hours",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                    vol.Optional(
                        CONF_MAX_SETPOINT_CHANGE_PER_HOUR,
                        default=self._options.get(
                            CONF_MAX_SETPOINT_CHANGE_PER_HOUR,
                            DEFAULT_MAX_SETPOINT_CHANGE_PER_HOUR,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1.0,
                            max=6.0,
                            step=0.5,
                            unit_of_measurement="°F",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                    vol.Optional(
                        CONF_USE_ADAPTIVE_MODEL,
                        default=self._options.get(CONF_USE_ADAPTIVE_MODEL, True),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_USE_GREYBOX_MODEL,
                        default=self._options.get(CONF_USE_GREYBOX_MODEL, False),
                    ): selector.BooleanSelector(),
                }
            ),
        )

    # ── Comfort ──────────────────────────────────────────────────────

    async def async_step_comfort(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure safety limits and optimization range."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_comfort_ranges(user_input)
            if not errors:
                # Comfort settings go into config entry data, not options.
                # Use options as a transport mechanism; __init__.py merges them.
                self._options.update(user_input)
                return self.async_create_entry(title="", data=self._options)

        # Read current values from entry data (set during initial setup)
        data = self.config_entry.data

        return self.async_show_form(
            step_id="comfort",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SAFETY_HEAT_MIN,
                        default=self._options.get(
                            CONF_SAFETY_HEAT_MIN,
                            data.get(CONF_SAFETY_HEAT_MIN, DEFAULT_SAFETY_HEAT_MIN),
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=35, max=65, step=1,
                            unit_of_measurement="°F",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                    vol.Optional(
                        CONF_SAFETY_COOL_MAX,
                        default=self._options.get(
                            CONF_SAFETY_COOL_MAX,
                            data.get(CONF_SAFETY_COOL_MAX, DEFAULT_SAFETY_COOL_MAX),
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=75, max=100, step=1,
                            unit_of_measurement="°F",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                    vol.Optional(
                        CONF_COMFORT_COOL_MIN,
                        default=self._options.get(
                            CONF_COMFORT_COOL_MIN,
                            data.get(CONF_COMFORT_COOL_MIN, DEFAULT_COMFORT_COOL_MIN),
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=58, max=80, step=1,
                            unit_of_measurement="°F",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                    vol.Optional(
                        CONF_COMFORT_COOL_MAX,
                        default=self._options.get(
                            CONF_COMFORT_COOL_MAX,
                            data.get(CONF_COMFORT_COOL_MAX, DEFAULT_COMFORT_COOL_MAX),
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=70, max=88, step=1,
                            unit_of_measurement="°F",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                    vol.Optional(
                        CONF_COMFORT_HEAT_MIN,
                        default=self._options.get(
                            CONF_COMFORT_HEAT_MIN,
                            data.get(CONF_COMFORT_HEAT_MIN, DEFAULT_COMFORT_HEAT_MIN),
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=45, max=72, step=1,
                            unit_of_measurement="°F",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                    vol.Optional(
                        CONF_COMFORT_HEAT_MAX,
                        default=self._options.get(
                            CONF_COMFORT_HEAT_MAX,
                            data.get(CONF_COMFORT_HEAT_MAX, DEFAULT_COMFORT_HEAT_MAX),
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=55, max=78, step=1,
                            unit_of_measurement="°F",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                }
            ),
        )

    # ── Occupancy ────────────────────────────────────────────────────

    async def async_step_presence(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Presence entities, debounce, and away delta."""
        if user_input is not None:
            self._options.update(user_input)
            return self.async_create_entry(title="", data=self._options)

        # Discover person/presence entities for smart defaults
        existing_occupancy = self._options.get(CONF_OCCUPANCY_ENTITIES, [])
        if not existing_occupancy:
            discovery = EntityDiscovery(self.hass)
            person_suggestions = discovery.discover_person_entities()
            # Only auto-suggest "high" confidence (person entities)
            occupancy_default = [
                s.entity_id for s in person_suggestions
                if s.confidence == "high"
            ]
        else:
            occupancy_default = existing_occupancy

        return self.async_show_form(
            step_id="presence",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_OCCUPANCY_ENTITIES,
                        default=occupancy_default,
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=[
                                "person",
                                "binary_sensor",
                                "input_select",
                                "device_tracker",
                            ],
                            multiple=True,
                        ),
                    ),
                    vol.Optional(
                        CONF_OCCUPANCY_DEBOUNCE_MINUTES,
                        default=self._options.get(
                            CONF_OCCUPANCY_DEBOUNCE_MINUTES,
                            DEFAULT_OCCUPANCY_DEBOUNCE_MINUTES,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1,
                            max=30,
                            step=1,
                            unit_of_measurement="min",
                        ),
                    ),
                    vol.Optional(
                        CONF_AWAY_COMFORT_DELTA,
                        default=self._options.get(
                            CONF_AWAY_COMFORT_DELTA, DEFAULT_AWAY_COMFORT_DELTA
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=2.0,
                            max=8.0,
                            step=0.5,
                            unit_of_measurement="°F",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                }
            ),
        )

    # ── Schedule (calendar-based occupancy) ──────────────────────────

    async def async_step_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Calendar-based scheduling and pre-conditioning configuration."""
        if user_input is not None:
            user_input = self._strip_empty_strings(user_input)
            # Convert comma-separated away keywords to list
            val = user_input.get(CONF_CALENDAR_AWAY_KEYWORDS, "")
            if isinstance(val, str):
                user_input[CONF_CALENDAR_AWAY_KEYWORDS] = [
                    k.strip() for k in val.split(",") if k.strip()
                ]
            # Extract navigation flags before storing
            configure_departures = user_input.pop("configure_departures", False)
            show_advanced = user_input.pop("show_advanced", False)
            self._options.update(user_input)
            if configure_departures:
                return await self.async_step_schedule_departures()
            if show_advanced:
                return await self.async_step_schedule_advanced()
            return self.async_create_entry(title="", data=self._options)

        # Format keyword list as comma-separated for display
        away_kw = self._options.get(CONF_CALENDAR_AWAY_KEYWORDS, DEFAULT_CALENDAR_AWAY_KEYWORDS)
        if isinstance(away_kw, list):
            away_kw = ", ".join(away_kw)

        # Migrate singular → plural
        existing_calendars = self._options.get(CONF_CALENDAR_ENTITIES, [])
        if not existing_calendars:
            singular = self._options.get(CONF_CALENDAR_ENTITY)
            if singular:
                existing_calendars = [singular]

        # Discover calendar entities for smart defaults
        if not existing_calendars:
            discovery = EntityDiscovery(self.hass)
            calendar_suggestions = discovery.discover_calendar_entities()
            calendars_default = [
                s.entity_id for s in calendar_suggestions
                if s.confidence in ("high", "medium")
            ][:2]
        else:
            calendars_default = existing_calendars

        # Check if departure profiles already exist
        has_profiles = bool(self._options.get(CONF_DEPARTURE_PROFILES))

        return self.async_show_form(
            step_id="schedule",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_CALENDAR_ENTITIES,
                        default=calendars_default,
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="calendar",
                            multiple=True,
                        ),
                    ),
                    vol.Optional(
                        CONF_CALENDAR_AWAY_KEYWORDS,
                        default=away_kw,
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.TEXT,
                        ),
                    ),
                    vol.Optional(
                        CONF_PRECONDITIONING_BUFFER_MINUTES,
                        default=self._options.get(
                            CONF_PRECONDITIONING_BUFFER_MINUTES,
                            DEFAULT_PRECONDITIONING_BUFFER_MINUTES,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=60,
                            step=5,
                            unit_of_measurement="min",
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                    vol.Optional(
                        CONF_DEPARTURE_TRIGGER_WINDOW_MINUTES,
                        default=self._options.get(
                            CONF_DEPARTURE_TRIGGER_WINDOW_MINUTES,
                            DEFAULT_DEPARTURE_TRIGGER_WINDOW_MINUTES,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=15,
                            max=120,
                            step=15,
                            unit_of_measurement="min",
                        ),
                    ),
                    vol.Optional(
                        "configure_departures",
                        default=False,
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        "show_advanced",
                        default=False,
                    ): selector.BooleanSelector(),
                }
            ),
        )

    async def async_step_schedule_departures(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure per-person departure profiles (zone + travel sensor)."""
        if user_input is not None:
            # Build profiles from per-person fields
            profiles: list[dict[str, str]] = []
            person_entities = self._get_person_entities()
            for person_eid in person_entities:
                safe_key = person_eid.replace(".", "_")
                zone = user_input.get(f"zone_{safe_key}")
                travel = user_input.get(f"travel_{safe_key}")
                if zone or travel:
                    profile: dict[str, str] = {"person": person_eid}
                    if zone:
                        profile["zone"] = zone
                    if travel:
                        profile["travel_sensor"] = travel
                    profiles.append(profile)

            import json as _json
            self._options[CONF_DEPARTURE_PROFILES] = _json.dumps(profiles)
            return self.async_create_entry(title="", data=self._options)

        # Load existing profiles
        existing_profiles: dict[str, dict[str, str]] = {}
        raw = self._options.get(CONF_DEPARTURE_PROFILES)
        if raw:
            import json as _json
            try:
                for p in _json.loads(raw):
                    existing_profiles[p["person"]] = p
            except (ValueError, KeyError):
                pass

        # Migrate from legacy flat lists if no profiles exist
        if not existing_profiles:
            existing_profiles = self._migrate_legacy_departure_config()

        person_entities = self._get_person_entities()
        if not person_entities:
            # No person entities configured — skip departure profiles
            return self.async_create_entry(title="", data=self._options)

        # Build form with zone + travel sensor per person
        schema_dict: dict[Any, Any] = {}
        for person_eid in person_entities:
            safe_key = person_eid.replace(".", "_")
            profile = existing_profiles.get(person_eid, {})

            schema_dict[
                vol.Optional(
                    f"zone_{safe_key}",
                    description={"suggested_value": profile.get("zone")},
                )
            ] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="zone"),
            )
            schema_dict[
                vol.Optional(
                    f"travel_{safe_key}",
                    description={"suggested_value": profile.get("travel_sensor")},
                )
            ] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor"),
            )

        # Build description showing which person each pair belongs to
        person_labels = []
        for person_eid in person_entities:
            state = self.hass.states.get(person_eid)
            name = state.name if state else person_eid.split(".")[-1].replace("_", " ").title()
            person_labels.append(f"**{name}** ({person_eid})")

        return self.async_show_form(
            step_id="schedule_departures",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "person_list": "\n".join(person_labels),
            },
        )

    def _get_person_entities(self) -> list[str]:
        """Get person entities from occupancy config."""
        occupancy = self._options.get(CONF_OCCUPANCY_ENTITIES, [])
        return [eid for eid in occupancy if eid.startswith("person.")]

    def _migrate_legacy_departure_config(self) -> dict[str, dict[str, str]]:
        """Migrate legacy flat zone/travel lists into per-person profiles.

        Best-effort: pairs by index position if counts match,
        or assigns the single zone/sensor to the first person.
        """
        zones = self._options.get(CONF_DEPARTURE_ZONES, [])
        if not zones:
            singular = self._options.get(CONF_DEPARTURE_ZONE)
            if singular:
                zones = [singular]
        travel = self._options.get(CONF_TRAVEL_TIME_SENSORS, [])
        if not travel:
            singular = self._options.get(CONF_TRAVEL_TIME_SENSOR)
            if singular:
                travel = [singular]

        if not zones and not travel:
            return {}

        persons = self._get_person_entities()
        result: dict[str, dict[str, str]] = {}

        for i, person_eid in enumerate(persons):
            profile: dict[str, str] = {"person": person_eid}
            if i < len(zones):
                profile["zone"] = zones[i]
            if i < len(travel):
                profile["travel_sensor"] = travel[i]
            if len(profile) > 1:  # has more than just "person"
                result[person_eid] = profile

        return result

    async def async_step_schedule_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Advanced calendar settings — home keywords, default mode."""
        if user_input is not None:
            # Convert comma-separated home keywords to list
            val = user_input.get(CONF_CALENDAR_HOME_KEYWORDS, "")
            if isinstance(val, str):
                user_input[CONF_CALENDAR_HOME_KEYWORDS] = [
                    k.strip() for k in val.split(",") if k.strip()
                ]
            self._options.update(user_input)
            return self.async_create_entry(title="", data=self._options)

        home_kw = self._options.get(CONF_CALENDAR_HOME_KEYWORDS, DEFAULT_CALENDAR_HOME_KEYWORDS)
        if isinstance(home_kw, list):
            home_kw = ", ".join(home_kw)

        return self.async_show_form(
            step_id="schedule_advanced",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_CALENDAR_DEFAULT_MODE,
                        default=self._options.get(
                            CONF_CALENDAR_DEFAULT_MODE, DEFAULT_CALENDAR_DEFAULT_MODE
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["home", "away"],
                        ),
                    ),
                    vol.Optional(
                        CONF_CALENDAR_HOME_KEYWORDS,
                        default=home_kw,
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.TEXT,
                        ),
                    ),
                }
            ),
        )

    # ── Auxiliary Appliances ──────────────────────────────────────────

    async def async_step_appliances(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure auxiliary appliances that impact the thermal envelope."""
        if user_input is not None:
            appliances: list[dict[str, Any]] = []

            # Parse dynamic appliance fields (up to 5 appliances)
            for i in range(5):
                name = user_input.get(f"appliance_{i}_name")
                state_entity = user_input.get(f"appliance_{i}_state_entity")
                if not name or not state_entity:
                    continue

                active_states_raw = user_input.get(f"appliance_{i}_active_states", "on")
                active_states = [s.strip() for s in active_states_raw.split(",") if s.strip()]

                thermal_btu = user_input.get(f"appliance_{i}_thermal_btu", 0.0)
                estimated_watts = user_input.get(f"appliance_{i}_estimated_watts")
                power_entity = user_input.get(f"appliance_{i}_power_entity")

                # Generate a slug from the name
                slug = name.lower().replace(" ", "_").replace("-", "_")[:32]

                appliance: dict[str, Any] = {
                    "id": slug,
                    "name": name,
                    "state_entity": state_entity,
                    "active_states": active_states,
                    "thermal_impact_btu": float(thermal_btu),
                }
                if estimated_watts:
                    appliance["estimated_watts"] = float(estimated_watts)
                if power_entity:
                    appliance["power_entity"] = power_entity

                appliances.append(appliance)

            self._options[CONF_AUXILIARY_APPLIANCES] = json.dumps(appliances)
            return self.async_create_entry(title="", data=self._options)

        # Load existing appliances
        existing: list[dict[str, Any]] = []
        raw = self._options.get(CONF_AUXILIARY_APPLIANCES)
        if raw:
            try:
                existing = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass

        # Exclude our own entities from the picker
        own_eids = self._get_own_entity_ids()

        # Build form with up to 5 appliance slots (pre-filled from existing)
        schema_dict: dict[Any, Any] = {}
        for i in range(max(len(existing) + 1, 1)):
            if i >= 5:
                break
            app = existing[i] if i < len(existing) else {}

            schema_dict[vol.Optional(
                f"appliance_{i}_name",
                description={"suggested_value": app.get("name", "")},
            )] = selector.TextSelector()

            schema_dict[vol.Optional(
                f"appliance_{i}_state_entity",
                description={"suggested_value": app.get("state_entity", "")},
            )] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    exclude_entities=own_eids,
                ),
            )

            schema_dict[vol.Optional(
                f"appliance_{i}_active_states",
                description={"suggested_value": ", ".join(app.get("active_states", ["on"]))},
            )] = selector.TextSelector()

            schema_dict[vol.Optional(
                f"appliance_{i}_thermal_btu",
                description={"suggested_value": app.get("thermal_impact_btu", "")},
            )] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=-10000,
                    max=10000,
                    step=100,
                    unit_of_measurement="BTU/hr",
                    mode=selector.NumberSelectorMode.BOX,
                ),
            )

            schema_dict[vol.Optional(
                f"appliance_{i}_estimated_watts",
                description={"suggested_value": app.get("estimated_watts", "")},
            )] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=15000,
                    step=50,
                    unit_of_measurement="W",
                    mode=selector.NumberSelectorMode.BOX,
                ),
            )

            schema_dict[vol.Optional(
                f"appliance_{i}_power_entity",
                description={"suggested_value": app.get("power_entity", "")},
            )] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    exclude_entities=own_eids,
                ),
            )

        return self.async_show_form(
            step_id="appliances",
            data_schema=vol.Schema(schema_dict),
        )

    # ── Indoor Sensing ───────────────────────────────────────────────

    async def async_step_indoor_sensing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between simple indoor sensors or room-aware sensing."""
        if user_input is not None:
            mode = user_input.get("indoor_sensing_mode", "simple")
            if mode == "room_aware":
                return await self.async_step_indoor_rooms()
            return await self.async_step_indoor_simple()

        # Default: room_aware if rooms are already configured
        existing_config = self._options.get(CONF_AREA_SENSOR_CONFIG, "")
        has_rooms = bool(existing_config and existing_config != "[]")
        default_mode = "room_aware" if has_rooms else "simple"

        return self.async_show_form(
            step_id="indoor_sensing",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "indoor_sensing_mode",
                        default=default_mode,
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value="simple",
                                    label="Simple (flat sensor list)",
                                ),
                                selector.SelectOptionDict(
                                    value="room_aware",
                                    label="Room-Aware (per-room sensors with occupancy weighting)",
                                ),
                            ],
                        ),
                    ),
                }
            ),
        )

    async def async_step_indoor_simple(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Simple flat list of indoor temperature and humidity sensors."""
        if user_input is not None:
            user_input = self._strip_empty_strings(user_input)
            self._options.update(user_input)
            # Clear room config when switching to simple mode
            self._options[CONF_AREA_SENSOR_CONFIG] = ""
            self._options[CONF_INDOOR_WEIGHTING_MODE] = WEIGHTING_MODE_EQUAL
            return self.async_create_entry(title="", data=self._options)

        # Run discovery for smart defaults
        discovery = EntityDiscovery(self.hass)
        exclude = self._get_own_entity_ids()

        existing_temp = self._options.get(CONF_INDOOR_TEMP_ENTITIES, [])
        if not existing_temp:
            high = [s.entity_id for s in discovery.discover_temp_sensors(outdoor=False) if s.confidence == "high"]
            existing_temp = high[:2]

        existing_hum = self._options.get(CONF_INDOOR_HUMIDITY_ENTITIES, [])
        if not existing_hum:
            high = [s.entity_id for s in discovery.discover_humidity_sensors(outdoor=False) if s.confidence == "high"]
            existing_hum = high[:2]

        return self.async_show_form(
            step_id="indoor_simple",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_INDOOR_TEMP_ENTITIES,
                        default=existing_temp,
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            device_class="temperature",
                            multiple=True,
                            exclude_entities=exclude,
                        ),
                    ),
                    vol.Optional(
                        CONF_INDOOR_HUMIDITY_ENTITIES,
                        default=existing_hum,
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            device_class="humidity",
                            multiple=True,
                            exclude_entities=exclude,
                        ),
                    ),
                }
            ),
        )

    async def async_step_indoor_rooms(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Room-aware occupancy-weighted indoor sensing settings."""
        if user_input is not None:
            self._options.update(user_input)
            return await self.async_step_indoor_rooms_discover()

        return self.async_show_form(
            step_id="indoor_rooms",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_INDOOR_WEIGHTING_MODE,
                        default=self._options.get(
                            CONF_INDOOR_WEIGHTING_MODE,
                            DEFAULT_INDOOR_WEIGHTING_MODE,
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=WEIGHTING_MODE_EQUAL,
                                    label="Equal (all rooms weighted the same)",
                                ),
                                selector.SelectOptionDict(
                                    value=WEIGHTING_MODE_OCCUPIED_ONLY,
                                    label="Occupied rooms only",
                                ),
                                selector.SelectOptionDict(
                                    value=WEIGHTING_MODE_WEIGHTED,
                                    label="Weighted (occupied rooms count more)",
                                ),
                            ],
                        ),
                    ),
                    vol.Optional(
                        CONF_ROOM_OCCUPANCY_DEBOUNCE_MINUTES,
                        default=self._options.get(
                            CONF_ROOM_OCCUPANCY_DEBOUNCE_MINUTES,
                            DEFAULT_ROOM_OCCUPANCY_DEBOUNCE_MINUTES,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1,
                            max=30,
                            step=1,
                            unit_of_measurement="min",
                        ),
                    ),
                    vol.Optional(
                        CONF_OCCUPIED_WEIGHT_MULTIPLIER,
                        default=self._options.get(
                            CONF_OCCUPIED_WEIGHT_MULTIPLIER,
                            DEFAULT_OCCUPIED_WEIGHT_MULTIPLIER,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=2.0,
                            max=5.0,
                            step=0.5,
                            mode=selector.NumberSelectorMode.SLIDER,
                        ),
                    ),
                }
            ),
        )

    async def async_step_indoor_rooms_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Discover and select rooms from HA area registry."""
        if user_input is not None:
            selected_area_ids = user_input.get("selected_areas", [])
            self._selected_area_ids = selected_area_ids
            if selected_area_ids:
                return await self.async_step_indoor_rooms_edit()
            # No rooms selected — save empty config and return
            self._options[CONF_AREA_SENSOR_CONFIG] = (
                AreaOccupancyManager.serialize_area_config([])
            )
            return self.async_create_entry(title="", data=self._options)

        discovered = await AreaOccupancyManager.async_discover_areas(self.hass)
        self._discovered_areas = discovered

        if not discovered:
            return self.async_show_form(
                step_id="indoor_rooms_no_areas",
                data_schema=vol.Schema({}),
            )

        # Load existing config to pre-select previously configured areas
        existing_config = self._options.get(CONF_AREA_SENSOR_CONFIG)
        existing_area_ids: set[str] = set()
        if existing_config:
            for ac in AreaOccupancyManager.deserialize_area_config(existing_config):
                existing_area_ids.add(ac["area_id"])

        # Build options list from discovered areas
        area_options = []
        default_selected = []
        for area in discovered:
            n_temp = len(area.temp_entities)
            n_hum = len(area.humidity_entities)
            n_motion = len(area.motion_entities)
            parts = []
            if n_temp:
                parts.append(f"{n_temp} temp")
            if n_hum:
                parts.append(f"{n_hum} humidity")
            if n_motion:
                parts.append(f"{n_motion} motion")
            else:
                parts.append("no motion sensor")
            label = f"{area.area_name} ({', '.join(parts)})"
            area_options.append(
                selector.SelectOptionDict(value=area.area_id, label=label)
            )
            if existing_area_ids:
                if area.area_id in existing_area_ids:
                    default_selected.append(area.area_id)
            else:
                default_selected.append(area.area_id)

        return self.async_show_form(
            step_id="indoor_rooms_discover",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "selected_areas",
                        default=default_selected,
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=area_options,
                            multiple=True,
                        ),
                    ),
                }
            ),
        )

    async def async_step_indoor_rooms_no_areas(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle case where no areas with temp sensors were found."""
        if user_input is not None:
            self._options[CONF_AREA_SENSOR_CONFIG] = (
                AreaOccupancyManager.serialize_area_config([])
            )
            return self.async_create_entry(title="", data=self._options)
        return self.async_show_form(
            step_id="indoor_rooms_no_areas",
            data_schema=vol.Schema({}),
        )

    async def async_step_indoor_rooms_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit per-room temperature, humidity, and motion sensors."""
        from .engine.data_types import AreaSensorGroup

        areas = getattr(self, "_discovered_areas", [])
        selected_ids = getattr(self, "_selected_area_ids", [])

        if user_input is not None:
            # Build final area config from user edits
            final_areas = []
            all_temp_entities: list[str] = []
            all_humidity_entities: list[str] = []
            for area in areas:
                if area.area_id not in selected_ids:
                    continue
                temp_key = f"temp_{area.area_id}"
                hum_key = f"humidity_{area.area_id}"
                motion_key = f"motion_{area.area_id}"
                edited_temp = user_input.get(temp_key, area.temp_entities)
                edited_hum = user_input.get(hum_key, area.humidity_entities)
                edited_motion = user_input.get(motion_key, area.motion_entities)
                final_areas.append(AreaSensorGroup(
                    area_id=area.area_id,
                    area_name=area.area_name,
                    temp_entities=edited_temp,
                    humidity_entities=edited_hum,
                    motion_entities=edited_motion,
                ))
                # Collect for global derivation
                for e in edited_temp:
                    if e not in all_temp_entities:
                        all_temp_entities.append(e)
                for e in edited_hum:
                    if e not in all_humidity_entities:
                        all_humidity_entities.append(e)

            self._options[CONF_AREA_SENSOR_CONFIG] = (
                AreaOccupancyManager.serialize_area_config(final_areas)
            )
            # Derive global indoor entities from room union
            self._options[CONF_INDOOR_TEMP_ENTITIES] = all_temp_entities
            self._options[CONF_INDOOR_HUMIDITY_ENTITIES] = all_humidity_entities
            return self.async_create_entry(title="", data=self._options)

        # Load existing config for pre-filling
        existing_config_data: dict[str, dict[str, list[str]]] = {}
        existing_config = self._options.get(CONF_AREA_SENSOR_CONFIG)
        if existing_config:
            for ac in AreaOccupancyManager.deserialize_area_config(existing_config):
                existing_config_data[ac["area_id"]] = {
                    "temp_entities": ac.get("temp_entities", []),
                    "humidity_entities": ac.get("humidity_entities", []),
                    "motion_entities": ac.get("motion_entities", []),
                }

        exclude = self._get_own_entity_ids()
        schema_dict: dict[Any, Any] = {}
        for area in areas:
            if area.area_id not in selected_ids:
                continue
            existing = existing_config_data.get(area.area_id, {})

            temp_key = f"temp_{area.area_id}"
            default_temp = existing.get("temp_entities", area.temp_entities)
            schema_dict[
                vol.Optional(
                    temp_key,
                    description={"suggested_value": default_temp},
                )
            ] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    device_class="temperature",
                    multiple=True,
                    exclude_entities=exclude,
                ),
            )

            hum_key = f"humidity_{area.area_id}"
            default_hum = existing.get("humidity_entities", area.humidity_entities)
            schema_dict[
                vol.Optional(
                    hum_key,
                    description={"suggested_value": default_hum},
                )
            ] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    device_class="humidity",
                    multiple=True,
                    exclude_entities=exclude,
                ),
            )

            motion_key = f"motion_{area.area_id}"
            default_motion = existing.get("motion_entities", area.motion_entities)
            schema_dict[
                vol.Optional(
                    motion_key,
                    description={"suggested_value": default_motion},
                )
            ] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="binary_sensor",
                    multiple=True,
                ),
            )

        if not schema_dict:
            self._options[CONF_AREA_SENSOR_CONFIG] = (
                AreaOccupancyManager.serialize_area_config([])
            )
            return self.async_create_entry(title="", data=self._options)

        # Build description showing room names
        room_summaries = []
        for area in areas:
            if area.area_id not in selected_ids:
                continue
            room_summaries.append(f"**{area.area_name}**")

        return self.async_show_form(
            step_id="indoor_rooms_edit",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "room_details": ", ".join(room_summaries),
            },
        )
