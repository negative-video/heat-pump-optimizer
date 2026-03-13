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
    CONF_AWAY_COMFORT_DELTA,
    CONF_BAROMETRIC_PRESSURE_ENTITY,
    CONF_CALENDAR_AWAY_KEYWORDS,
    CONF_CALENDAR_DEFAULT_MODE,
    CONF_CALENDAR_ENTITY,
    CONF_CALENDAR_HOME_KEYWORDS,
    CONF_CARBON_WEIGHT,
    CONF_CLIMATE_ENTITY,
    CONF_CO2_ENTITY,
    CONF_COMFORT_COOL_MAX,
    CONF_COMFORT_COOL_MIN,
    CONF_COMFORT_HEAT_MAX,
    CONF_COMFORT_HEAT_MIN,
    CONF_COST_WEIGHT,
    CONF_DEPARTURE_TRIGGER_WINDOW_MINUTES,
    CONF_DEPARTURE_ZONE,
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
    CONF_ROOM_OCCUPANCY_DEBOUNCE_MINUTES,
    CONF_TRAVEL_TIME_SENSOR,
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
            return await self.async_step_thermal_profile()

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

    # ── Step 2: Thermal Profile ──────────────────────────────────────

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

    # ── Step 3: Temperature Boundaries ───────────────────────────────

    async def async_step_comfort(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: Configure safety limits and optimization range."""
        if user_input is not None:
            self._config_data.update(user_input)
            return self.async_create_entry(
                title="Heat Pump Optimizer",
                data=self._config_data,
            )

        return self.async_show_form(
            step_id="comfort",
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
            menu_options=["sensors", "energy", "behavior", "comfort", "occupancy", "schedule", "rooms"],
        )

    # ── Sensors ──────────────────────────────────────────────────────

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Environmental and indoor sensor configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = self._strip_empty_strings(user_input)

            # Prevent the same sensor from being used as both indoor and outdoor
            outdoor_temps = set(user_input.get(CONF_OUTDOOR_TEMP_ENTITIES, []))
            indoor_temps = set(user_input.get(CONF_INDOOR_TEMP_ENTITIES, []))
            outdoor_hum = set(user_input.get(CONF_OUTDOOR_HUMIDITY_ENTITIES, []))
            indoor_hum = set(user_input.get(CONF_INDOOR_HUMIDITY_ENTITIES, []))
            if outdoor_temps & indoor_temps or outdoor_hum & indoor_hum:
                errors["base"] = "sensor_overlap"

            if not errors:
                self._options.update(user_input)
                return self.async_create_entry(title="", data=self._options)

        # Run discovery for smart defaults on empty fields
        discovery = EntityDiscovery(self.hass)

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
        indoor_temp_default = _suggest_multi(
            CONF_INDOOR_TEMP_ENTITIES, discovery.discover_temp_sensors(outdoor=False)
        )
        indoor_humidity_default = _suggest_multi(
            CONF_INDOOR_HUMIDITY_ENTITIES, discovery.discover_humidity_sensors(outdoor=False)
        )

        return self.async_show_form(
            step_id="sensors",
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
                    vol.Optional(
                        CONF_WIND_SPEED_ENTITY,
                        description={"suggested_value": self._options.get(CONF_WIND_SPEED_ENTITY)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor"),
                    ),
                    vol.Optional(
                        CONF_SOLAR_IRRADIANCE_ENTITY,
                        description={"suggested_value": self._options.get(CONF_SOLAR_IRRADIANCE_ENTITY)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor", device_class="irradiance"
                        ),
                    ),
                    vol.Optional(
                        CONF_BAROMETRIC_PRESSURE_ENTITY,
                        description={"suggested_value": self._options.get(CONF_BAROMETRIC_PRESSURE_ENTITY)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
                            device_class="atmospheric_pressure",
                        ),
                    ),
                    vol.Optional(
                        CONF_SUN_ENTITY,
                        default=self._options.get(CONF_SUN_ENTITY, DEFAULT_SUN_ENTITY),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sun"),
                    ),
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
                }
            ),
            errors=errors,
        )

    # ── Energy & Cost ────────────────────────────────────────────────

    async def async_step_energy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Power monitoring, CO2, electricity rates, optimization weights."""
        if user_input is not None:
            user_input = self._strip_empty_strings(user_input)
            self._options.update(user_input)
            return self.async_create_entry(title="", data=self._options)

        # Discover power, solar, CO2, and rate sensors for smart defaults
        discovery = EntityDiscovery(self.hass)

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
                        selector.EntitySelectorConfig(domain="sensor"),
                    ),
                    vol.Optional(
                        CONF_HVAC_POWER_DEFAULT_WATTS,
                        default=self._options.get(
                            CONF_HVAC_POWER_DEFAULT_WATTS, DEFAULT_HVAC_POWER_WATTS
                        ),
                    ): vol.Coerce(float),
                    vol.Optional(
                        CONF_CO2_ENTITY,
                        description={"suggested_value": co2_default},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor"),
                    ),
                    vol.Optional(
                        CONF_ELECTRICITY_RATE_ENTITY,
                        description={"suggested_value": rate_default},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["sensor", "input_number"],
                        ),
                    ),
                    vol.Optional(
                        CONF_ELECTRICITY_FLAT_RATE,
                        default=self._options.get(CONF_ELECTRICITY_FLAT_RATE, 0.0),
                    ): vol.Coerce(float),
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
                            domain="sensor", device_class="power"
                        ),
                    ),
                    vol.Optional(
                        CONF_GRID_IMPORT_ENTITY,
                        description={"suggested_value": self._options.get(CONF_GRID_IMPORT_ENTITY)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor", device_class="power"
                        ),
                    ),
                    vol.Optional(
                        CONF_SOLAR_EXPORT_RATE_ENTITY,
                        description={"suggested_value": self._options.get(CONF_SOLAR_EXPORT_RATE_ENTITY)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor"),
                    ),
                }
            ),
        )

    # ── Behavior ─────────────────────────────────────────────────────

    async def async_step_behavior(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Aggressiveness, override grace, reopt interval, model toggles."""
        if user_input is not None:
            self._options.update(user_input)
            return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="behavior",
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
        if user_input is not None:
            # Comfort settings go into config entry data, not options.
            # Use options as a transport mechanism; __init__.py merges them.
            self._options.update(user_input)
            return self.async_create_entry(title="", data=self._options)

        # Read current values from entry data (set during initial setup)
        data = self.config_entry.data

        return self.async_show_form(
            step_id="comfort",
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

    async def async_step_occupancy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Occupancy entities, debounce, and away delta."""
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
            step_id="occupancy",
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
            # Convert comma-separated keywords to lists
            for key in (CONF_CALENDAR_HOME_KEYWORDS, CONF_CALENDAR_AWAY_KEYWORDS):
                val = user_input.get(key, "")
                if isinstance(val, str):
                    user_input[key] = [k.strip() for k in val.split(",") if k.strip()]
            self._options.update(user_input)
            return self.async_create_entry(title="", data=self._options)

        # Format keyword lists as comma-separated for display
        home_kw = self._options.get(CONF_CALENDAR_HOME_KEYWORDS, DEFAULT_CALENDAR_HOME_KEYWORDS)
        away_kw = self._options.get(CONF_CALENDAR_AWAY_KEYWORDS, DEFAULT_CALENDAR_AWAY_KEYWORDS)
        if isinstance(home_kw, list):
            home_kw = ", ".join(home_kw)
        if isinstance(away_kw, list):
            away_kw = ", ".join(away_kw)

        # Discover calendar entities for smart defaults
        existing_calendar = self._options.get(CONF_CALENDAR_ENTITY, "")
        if not existing_calendar:
            discovery = EntityDiscovery(self.hass)
            calendar_suggestions = discovery.discover_calendar_entities()
            calendar_default = (
                calendar_suggestions[0].entity_id if calendar_suggestions else ""
            )
        else:
            calendar_default = existing_calendar

        return self.async_show_form(
            step_id="schedule",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_CALENDAR_ENTITY,
                        description={"suggested_value": calendar_default or None},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="calendar"),
                    ),
                    vol.Optional(
                        CONF_CALENDAR_HOME_KEYWORDS,
                        default=home_kw,
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.TEXT,
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
                        CONF_CALENDAR_DEFAULT_MODE,
                        default=self._options.get(
                            CONF_CALENDAR_DEFAULT_MODE, DEFAULT_CALENDAR_DEFAULT_MODE
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["home", "away"],
                            translation_key="calendar_default_mode",
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
                        CONF_DEPARTURE_ZONE,
                        description={"suggested_value": self._options.get(CONF_DEPARTURE_ZONE)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="zone"),
                    ),
                    vol.Optional(
                        CONF_TRAVEL_TIME_SENSOR,
                        description={"suggested_value": self._options.get(CONF_TRAVEL_TIME_SENSOR)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor"),
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
                }
            ),
        )

    # ── Room-aware sensing ────────────────────────────────────────────

    async def async_step_rooms(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Room-aware occupancy-weighted indoor sensing configuration."""
        if user_input is not None:
            # If mode changed to non-equal and no area config exists, run discovery
            mode = user_input.get(CONF_INDOOR_WEIGHTING_MODE, WEIGHTING_MODE_EQUAL)
            if mode != WEIGHTING_MODE_EQUAL and not self._options.get(CONF_AREA_SENSOR_CONFIG):
                # Store the mode settings and proceed to room discovery
                self._options.update(user_input)
                return await self.async_step_rooms_discover()

            self._options.update(user_input)
            return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="rooms",
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
                                    label="Equal (current behavior)",
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
                            translation_key="indoor_weighting_mode",
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

    async def async_step_rooms_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Discover and select rooms from HA area registry."""
        if user_input is not None:
            selected_area_ids = user_input.get("selected_areas", [])
            # Filter discovered areas to only selected ones
            all_areas = await AreaOccupancyManager.async_discover_areas(
                self.hass,
                indoor_temp_entities=self._options.get(CONF_INDOOR_TEMP_ENTITIES),
                indoor_humidity_entities=self._options.get(CONF_INDOOR_HUMIDITY_ENTITIES),
            )
            selected = [a for a in all_areas if a.area_id in selected_area_ids]
            self._options[CONF_AREA_SENSOR_CONFIG] = (
                AreaOccupancyManager.serialize_area_config(selected)
            )
            return self.async_create_entry(title="", data=self._options)

        # Run discovery
        discovered = await AreaOccupancyManager.async_discover_areas(
            self.hass,
            indoor_temp_entities=self._options.get(CONF_INDOOR_TEMP_ENTITIES),
            indoor_humidity_entities=self._options.get(CONF_INDOOR_HUMIDITY_ENTITIES),
        )

        if not discovered:
            # No areas found — return to rooms step with a note
            return self.async_show_form(
                step_id="rooms_discover",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "message": "No areas found with temperature sensors. "
                    "Assign your indoor sensors to areas in Home Assistant, "
                    "then try again."
                },
            )

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
            label = f"{area.area_name} ({', '.join(parts)})"
            area_options.append(
                selector.SelectOptionDict(value=area.area_id, label=label)
            )
            default_selected.append(area.area_id)

        return self.async_show_form(
            step_id="rooms_discover",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "selected_areas",
                        default=default_selected,
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=area_options,
                            multiple=True,
                            translation_key="room_areas",
                        ),
                    ),
                }
            ),
        )
