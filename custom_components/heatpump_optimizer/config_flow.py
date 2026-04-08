"""Config flow for Heat Pump Optimizer."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.util.unit_conversion import TemperatureConverter

from .adapters.area_occupancy import AreaOccupancyManager
from .adapters.entity_discovery import EntityDiscovery
from .const import (
    AGGRESSIVENESS_AGGRESSIVE,
    AGGRESSIVENESS_BALANCED,
    AGGRESSIVENESS_CONSERVATIVE,
    BLEND_MODE_NONE,
    BLEND_MODE_OCCUPANCY,
    BLEND_MODE_SCHEDULE,
    BLEND_MODE_MEDIAN,
    CONF_AREA_SENSOR_CONFIG,
    CONF_BLEND_MITIGATION_MODE,
    CONF_BLEND_OUTLIER_THRESHOLD_F,
    CONF_BLEND_SCHEDULE_END,
    CONF_BLEND_SCHEDULE_START,
    CONF_HUMIDITY_SQUELCH_PAIRS,
    CONF_THERMOSTAT_OCCUPANCY_ENTITY,
    DEFAULT_BLEND_OUTLIER_THRESHOLD_F,
    DEFAULT_BLEND_SCHEDULE_END,
    DEFAULT_BLEND_SCHEDULE_START,
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
    CONF_AUX_HEAT_OVERRIDE_ENTITY,
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
    CONF_HOME_SQFT,
    CONF_HVAC_TONNAGE,
    CONF_HVAC_SEER,
    CONF_AUX_HEAT_TYPE,
    CONF_AUX_HEAT_KW,
    DEFAULT_AUX_HEAT_TYPE,
    AUX_HEAT_TYPES,
    CONF_INDOOR_HUMIDITY_ENTITIES,
    CONF_INDOOR_TEMP_ENTITIES,
    CONF_INDOOR_WEIGHTING_MODE,
    CONF_INITIALIZATION_MODE,
    CONF_MAX_SETPOINT_CHANGE_PER_HOUR,
    CONF_MODEL_IMPORT_DATA,
    CONF_MONITOR_ONLY,
    CONF_OCCUPANCY_DEBOUNCE_MINUTES,
    CONF_OCCUPANCY_ENTITIES,
    CONF_OCCUPIED_WEIGHT_MULTIPLIER,
    CONF_OPTIMIZATION_AGGRESSIVENESS,
    CONF_PRECONDITIONING_BUFFER_MINUTES,
    CONF_OUTDOOR_HUMIDITY_ENTITIES,
    CONF_OUTDOOR_TEMP_ENTITIES,
    CONF_OVERRIDE_GRACE_PERIOD_HOURS,
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
    CONF_SOLAR_EXPORT_RATE_ENTITY,
    CONF_SOLAR_IRRADIANCE_ENTITY,
    CONF_SOLAR_PANEL_AREA,
    CONF_SOLAR_PANEL_AREA_EACH,
    CONF_SOLAR_PANEL_COUNT,
    CONF_SOLAR_PANEL_EFFICIENCY,
    CONF_SOLAR_PRODUCTION_ENTITY,
    CONF_SUN_ENTITY,
    CONF_UV_INDEX_ENTITY,
    CONF_TOU_SCHEDULE,
    CONF_ROOM_OCCUPANCY_DEBOUNCE_MINUTES,
    CONF_TRAVEL_TIME_SENSOR,
    CONF_TRAVEL_TIME_SENSORS,
    CONF_CALIBRATION_ENABLED,
    CONF_USE_ADAPTIVE_MODEL,
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
    DEFAULT_SLEEP_COMFORT_COOL_MAX,
    DEFAULT_SLEEP_COMFORT_COOL_MIN,
    DEFAULT_SLEEP_COMFORT_HEAT_MAX,
    DEFAULT_SLEEP_COMFORT_HEAT_MIN,
    DEFAULT_SLEEP_SCHEDULE_ENABLED,
    DEFAULT_SLEEP_SCHEDULE_END,
    DEFAULT_SLEEP_SCHEDULE_START,
    DEFAULT_SOLAR_PANEL_EFFICIENCY,
    DEFAULT_SUN_ENTITY,
    DOMAIN,
    WEIGHTING_MODE_EQUAL,
    WEIGHTING_MODE_OCCUPIED_ONLY,
    WEIGHTING_MODE_WEIGHTED,
    INIT_MODE_IMPORT,
    INIT_MODE_LEARNING,
)

_LOGGER = logging.getLogger(__name__)


def _is_metric(hass: HomeAssistant) -> bool:
    """Check if the HA instance uses metric (Celsius) temperatures."""
    return hass.config.units.temperature_unit == UnitOfTemperature.CELSIUS


def _f_to_display(temp_f: float, hass: HomeAssistant) -> float:
    """Convert °F to the user's display unit (°F or °C), rounded to int."""
    if _is_metric(hass):
        return round(TemperatureConverter.convert(
            temp_f, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
        ))
    return temp_f


def _display_to_f(temp_display: float, hass: HomeAssistant) -> float:
    """Convert from user's display unit back to °F for internal storage."""
    if _is_metric(hass):
        return TemperatureConverter.convert(
            temp_display, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
        )
    return temp_display


def _temp_unit(hass: HomeAssistant) -> str:
    """Return the display temperature unit string."""
    return "°C" if _is_metric(hass) else "°F"


def _comfort_selector(hass: HomeAssistant, min_f: float, max_f: float) -> selector.NumberSelector:
    """Create a temperature NumberSelector in the user's unit system."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=_f_to_display(min_f, hass),
            max=_f_to_display(max_f, hass),
            step=1,
            unit_of_measurement=_temp_unit(hass),
            mode=selector.NumberSelectorMode.SLIDER,
        ),
    )


def _convert_comfort_input_to_f(user_input: dict[str, Any], hass: HomeAssistant) -> dict[str, Any]:
    """Convert temperature fields in user_input from display units to °F."""
    if not _is_metric(hass):
        return user_input
    temp_keys = {
        CONF_COMFORT_COOL_MIN, CONF_COMFORT_COOL_MAX,
        CONF_COMFORT_HEAT_MIN, CONF_COMFORT_HEAT_MAX,
        CONF_SAFETY_HEAT_MIN, CONF_SAFETY_COOL_MAX,
        CONF_SLEEP_COMFORT_COOL_MIN, CONF_SLEEP_COMFORT_COOL_MAX,
        CONF_SLEEP_COMFORT_HEAT_MIN, CONF_SLEEP_COMFORT_HEAT_MAX,
    }
    converted = dict(user_input)
    for key in temp_keys:
        if key in converted:
            converted[key] = _display_to_f(converted[key], hass)
    return converted


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

    VERSION = 2

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
            return await self.async_step_hvac_specs()

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

        schema[vol.Optional(CONF_MONITOR_ONLY, default=False)] = selector.BooleanSelector()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema),
        )

    # ── Step 2: HVAC System Specs ────────────────────────────────────

    async def async_step_hvac_specs(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Optional system specs — improves cold-start accuracy.

        All fields are optional.  Skipping this step leaves the thermal model
        at generic defaults; filling it in narrows the EKF priors and produces
        accurate energy estimates from day one.
        """
        if user_input is not None:
            # Convert tonnage string → float before storing
            if CONF_HVAC_TONNAGE in user_input:
                try:
                    user_input[CONF_HVAC_TONNAGE] = float(user_input[CONF_HVAC_TONNAGE])
                except (ValueError, TypeError):
                    user_input.pop(CONF_HVAC_TONNAGE, None)
            # Strip empty / zero / placeholder values so they don't shadow defaults
            cleaned = {
                k: v for k, v in user_input.items()
                if v is not None and v != "" and v != 0 and v != "unknown"
            }
            self._config_data.update(cleaned)
            return await self.async_step_air_sensors()

        existing = self._config_data
        tonnage_options = ["1.5", "2.0", "2.5", "3.0", "3.5", "4.0", "5.0", "unknown"]

        schema: dict[Any, Any] = {
            vol.Optional(
                CONF_HOME_SQFT,
                description={"suggested_value": existing.get(CONF_HOME_SQFT)},
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=300, max=10000, step=100,
                    unit_of_measurement="sq ft",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_HVAC_TONNAGE,
                description={"suggested_value": str(existing.get(CONF_HVAC_TONNAGE, "unknown"))},
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=tonnage_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_AUX_HEAT_TYPE,
                description={"suggested_value": existing.get(CONF_AUX_HEAT_TYPE, DEFAULT_AUX_HEAT_TYPE)},
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=AUX_HEAT_TYPES,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_AUX_HEAT_KW,
                description={"suggested_value": existing.get(CONF_AUX_HEAT_KW)},
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=30, step=0.5,
                    unit_of_measurement="kW",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }

        return self.async_show_form(
            step_id="hvac_specs",
            data_schema=vol.Schema(schema),
        )

    # ── Step 3: Air Sensors ─────────────────────────────────────────

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
            # Convert from display units (°C or °F) to internal °F
            user_input = _convert_comfort_input_to_f(user_input, self.hass)
            errors = _validate_comfort_ranges(user_input)
            if not errors:
                self._config_data.update(user_input)
                title = "Heat Pump Optimizer (Monitor)" if self._config_data.get(CONF_MONITOR_ONLY) else "Heat Pump Optimizer"
                return self.async_create_entry(
                    title=title,
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
                        default=_f_to_display(DEFAULT_SAFETY_HEAT_MIN, self.hass),
                    ): _comfort_selector(self.hass, 35, 65),
                    vol.Optional(
                        CONF_SAFETY_COOL_MAX,
                        default=_f_to_display(DEFAULT_SAFETY_COOL_MAX, self.hass),
                    ): _comfort_selector(self.hass, 75, 100),
                    # Optimization range (where the optimizer works when home)
                    vol.Optional(
                        CONF_COMFORT_COOL_MIN,
                        default=_f_to_display(DEFAULT_COMFORT_COOL_MIN, self.hass),
                    ): _comfort_selector(self.hass, 58, 80),
                    vol.Optional(
                        CONF_COMFORT_COOL_MAX,
                        default=_f_to_display(DEFAULT_COMFORT_COOL_MAX, self.hass),
                    ): _comfort_selector(self.hass, 70, 88),
                    vol.Optional(
                        CONF_COMFORT_HEAT_MIN,
                        default=_f_to_display(DEFAULT_COMFORT_HEAT_MIN, self.hass),
                    ): _comfort_selector(self.hass, 45, 72),
                    vol.Optional(
                        CONF_COMFORT_HEAT_MAX,
                        default=_f_to_display(DEFAULT_COMFORT_HEAT_MAX, self.hass),
                    ): _comfort_selector(self.hass, 55, 78),
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
            # Merge .data (from initial setup) with .options (from previous
            # option edits) so that onboarding values carry over.  Options
            # take precedence when the same key exists in both.
            self._options = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_menu(
            step_id="init",
            menu_options=["mode", "comfort", "sleep_schedule", "presence", "energy", "solar_panels", "equipment", "advanced"],
        )

    async def async_step_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Toggle monitor-only mode."""
        if user_input is not None:
            self._options[CONF_MONITOR_ONLY] = user_input.get(CONF_MONITOR_ONLY, False)
            return self.async_create_entry(data=self._options)

        current = self._options.get(
            CONF_MONITOR_ONLY,
            self.config_entry.data.get(CONF_MONITOR_ONLY, False),
        )
        return self.async_show_form(
            step_id="mode",
            data_schema=vol.Schema({
                vol.Optional(CONF_MONITOR_ONLY, default=current): selector.BooleanSelector(),
            }),
        )

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the advanced options menu."""
        return self.async_show_menu(
            step_id="advanced",
            menu_options=["behavior", "schedule", "indoor_sensing", "outdoor_sensors", "blend_mitigation", "humidity_squelch", "appliances"],
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
        """Change thermostat, weather source, or system specs without losing learned data."""
        if user_input is not None:
            user_input = self._strip_empty_strings(user_input)
            # Convert tonnage string back to float (SelectSelector returns str)
            if CONF_HVAC_TONNAGE in user_input:
                try:
                    user_input[CONF_HVAC_TONNAGE] = float(user_input[CONF_HVAC_TONNAGE])
                except (ValueError, TypeError):
                    user_input.pop(CONF_HVAC_TONNAGE, None)
            self._options.update(user_input)
            return self.async_create_entry(title="", data=self._options)

        # Read current values — options take precedence over initial setup data
        data = self.config_entry.data

        current_climate = self._options.get(
            CONF_CLIMATE_ENTITY, data.get(CONF_CLIMATE_ENTITY)
        )
        current_weather = self._options.get(
            CONF_WEATHER_ENTITIES,
            data.get(CONF_WEATHER_ENTITIES, [data.get(CONF_WEATHER_ENTITY, "")]),
        )
        current_sqft = self._options.get(CONF_HOME_SQFT, data.get(CONF_HOME_SQFT))
        current_tonnage = self._options.get(CONF_HVAC_TONNAGE, data.get(CONF_HVAC_TONNAGE))
        current_aux_type = self._options.get(CONF_AUX_HEAT_TYPE, data.get(CONF_AUX_HEAT_TYPE, DEFAULT_AUX_HEAT_TYPE))
        current_aux_kw = self._options.get(CONF_AUX_HEAT_KW, data.get(CONF_AUX_HEAT_KW))

        tonnage_options = ["1.5", "2.0", "2.5", "3.0", "3.5", "4.0", "5.0", "unknown"]

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
                    vol.Optional(
                        CONF_AUX_HEAT_OVERRIDE_ENTITY,
                        description={"suggested_value": self._options.get(CONF_AUX_HEAT_OVERRIDE_ENTITY)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["binary_sensor", "input_boolean", "switch"],
                        ),
                    ),
                    vol.Optional(
                        CONF_HOME_SQFT,
                        description={"suggested_value": current_sqft},
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=300, max=10000, step=100,
                            unit_of_measurement="sq ft",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_HVAC_TONNAGE,
                        description={"suggested_value": str(current_tonnage) if current_tonnage else "unknown"},
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=tonnage_options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(
                        CONF_AUX_HEAT_TYPE,
                        description={"suggested_value": current_aux_type},
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=AUX_HEAT_TYPES,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(
                        CONF_AUX_HEAT_KW,
                        description={"suggested_value": current_aux_kw},
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=30, step=0.5,
                            unit_of_measurement="kW",
                            mode=selector.NumberSelectorMode.BOX,
                        )
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
                        CONF_UV_INDEX_ENTITY,
                        description={"suggested_value": self._options.get(CONF_UV_INDEX_ENTITY)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor",
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
                            device_class=["door", "window", "opening", "garage_door"],
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
                        CONF_HVAC_SEER,
                        description={"suggested_value": self._options.get(CONF_HVAC_SEER)},
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=8, max=30, step=0.5,
                            unit_of_measurement="SEER1",
                            mode=selector.NumberSelectorMode.BOX,
                        ),
                    ),
                    vol.Optional(
                        CONF_HVAC_POWER_DEFAULT_WATTS,
                        description={"suggested_value": self._options.get(CONF_HVAC_POWER_DEFAULT_WATTS)},
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

    # ── Solar Panels ────────────────────────────────────────────────

    async def async_step_solar_panels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Solar panel configuration: entities, panel specs, efficiency."""
        if user_input is not None:
            user_input = self._strip_empty_strings(user_input)
            # If per-panel area and count are both provided, compute total area
            area_each = user_input.get(CONF_SOLAR_PANEL_AREA_EACH)
            count = user_input.get(CONF_SOLAR_PANEL_COUNT)
            if area_each and count and count > 0:
                # Per-panel × count takes precedence over total area
                computed_total = area_each * count
                # Convert from display units to m² if needed
                if not _is_metric(self.hass):
                    computed_total = computed_total / 10.764
                user_input[CONF_SOLAR_PANEL_AREA] = round(computed_total, 2)
            elif user_input.get(CONF_SOLAR_PANEL_AREA):
                # Convert total area from display units to m²
                if not _is_metric(self.hass):
                    user_input[CONF_SOLAR_PANEL_AREA] = round(
                        user_input[CONF_SOLAR_PANEL_AREA] / 10.764, 2
                    )
            self._options.update(user_input)
            return self.async_create_entry(title="", data=self._options)

        discovery = EntityDiscovery(self.hass)
        exclude = self._get_own_entity_ids()
        solar_default = None
        suggestions = discovery.discover_solar_sensors()
        existing = self._options.get(CONF_SOLAR_PRODUCTION_ENTITY)
        if existing:
            solar_default = existing
        elif suggestions:
            solar_default = suggestions[0].entity_id

        # Display area in user's unit system
        is_metric = _is_metric(self.hass)
        area_unit = "m²" if is_metric else "ft²"
        area_conv = 1.0 if is_metric else 10.764  # m² → ft²
        stored_area = self._options.get(CONF_SOLAR_PANEL_AREA)
        display_area = round(stored_area * area_conv, 1) if stored_area else None
        stored_each = self._options.get(CONF_SOLAR_PANEL_AREA_EACH)
        display_each = round(stored_each * area_conv, 2) if stored_each else None

        return self.async_show_form(
            step_id="solar_panels",
            data_schema=vol.Schema(
                {
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
                        CONF_SOLAR_PANEL_AREA,
                        description={"suggested_value": display_area},
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=10000 if not is_metric else 1000,
                            step=0.1,
                            unit_of_measurement=area_unit,
                            mode=selector.NumberSelectorMode.BOX,
                        ),
                    ),
                    vol.Optional(
                        CONF_SOLAR_PANEL_COUNT,
                        description={"suggested_value": self._options.get(CONF_SOLAR_PANEL_COUNT)},
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=200, step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        ),
                    ),
                    vol.Optional(
                        CONF_SOLAR_PANEL_AREA_EACH,
                        description={"suggested_value": display_each},
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=100 if not is_metric else 10,
                            step=0.01,
                            unit_of_measurement=area_unit,
                            mode=selector.NumberSelectorMode.BOX,
                        ),
                    ),
                    vol.Optional(
                        CONF_SOLAR_PANEL_EFFICIENCY,
                        default=self._options.get(
                            CONF_SOLAR_PANEL_EFFICIENCY, DEFAULT_SOLAR_PANEL_EFFICIENCY
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.05, max=0.35, step=0.01,
                            mode=selector.NumberSelectorMode.BOX,
                        ),
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
        errors: dict[str, str] = {}

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
                        CONF_CALIBRATION_ENABLED,
                        default=self._options.get(CONF_CALIBRATION_ENABLED, False),
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
            # Convert from display units (°C or °F) to internal °F
            user_input = _convert_comfort_input_to_f(user_input, self.hass)
            errors = _validate_comfort_ranges(user_input)
            if not errors:
                # Comfort settings go into config entry data, not options.
                # Use options as a transport mechanism; __init__.py merges them.
                self._options.update(user_input)
                return self.async_create_entry(title="", data=self._options)

        # Read current values from entry data (set during initial setup)
        # and convert stored °F to display units for the form defaults.
        data = self.config_entry.data

        def _get_default(key: str, fallback: float) -> float:
            val_f = self._options.get(key, data.get(key, fallback))
            return _f_to_display(val_f, self.hass)

        return self.async_show_form(
            step_id="comfort",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SAFETY_HEAT_MIN,
                        default=_get_default(CONF_SAFETY_HEAT_MIN, DEFAULT_SAFETY_HEAT_MIN),
                    ): _comfort_selector(self.hass, 35, 65),
                    vol.Optional(
                        CONF_SAFETY_COOL_MAX,
                        default=_get_default(CONF_SAFETY_COOL_MAX, DEFAULT_SAFETY_COOL_MAX),
                    ): _comfort_selector(self.hass, 75, 100),
                    vol.Optional(
                        CONF_COMFORT_COOL_MIN,
                        default=_get_default(CONF_COMFORT_COOL_MIN, DEFAULT_COMFORT_COOL_MIN),
                    ): _comfort_selector(self.hass, 58, 80),
                    vol.Optional(
                        CONF_COMFORT_COOL_MAX,
                        default=_get_default(CONF_COMFORT_COOL_MAX, DEFAULT_COMFORT_COOL_MAX),
                    ): _comfort_selector(self.hass, 70, 88),
                    vol.Optional(
                        CONF_COMFORT_HEAT_MIN,
                        default=_get_default(CONF_COMFORT_HEAT_MIN, DEFAULT_COMFORT_HEAT_MIN),
                    ): _comfort_selector(self.hass, 45, 72),
                    vol.Optional(
                        CONF_COMFORT_HEAT_MAX,
                        default=_get_default(CONF_COMFORT_HEAT_MAX, DEFAULT_COMFORT_HEAT_MAX),
                    ): _comfort_selector(self.hass, 55, 78),
                }
            ),
        )

    # ── Sleep Schedule ────────────────────────────────────────────────

    async def async_step_sleep_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure optional sleep schedule with tighter comfort bounds."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = _convert_comfort_input_to_f(user_input, self.hass)
            # Validate sleep comfort ranges
            s_cool_min = user_input.get(CONF_SLEEP_COMFORT_COOL_MIN, DEFAULT_SLEEP_COMFORT_COOL_MIN)
            s_cool_max = user_input.get(CONF_SLEEP_COMFORT_COOL_MAX, DEFAULT_SLEEP_COMFORT_COOL_MAX)
            s_heat_min = user_input.get(CONF_SLEEP_COMFORT_HEAT_MIN, DEFAULT_SLEEP_COMFORT_HEAT_MIN)
            s_heat_max = user_input.get(CONF_SLEEP_COMFORT_HEAT_MAX, DEFAULT_SLEEP_COMFORT_HEAT_MAX)
            if s_cool_min >= s_cool_max:
                errors[CONF_SLEEP_COMFORT_COOL_MIN] = "cool_range_inverted"
            if s_heat_min >= s_heat_max:
                errors[CONF_SLEEP_COMFORT_HEAT_MIN] = "heat_range_inverted"
            if not errors:
                self._options.update(user_input)
                return self.async_create_entry(title="", data=self._options)

        data = self.config_entry.data

        def _get(key: str, fallback: float) -> float:
            return _f_to_display(self._options.get(key, data.get(key, fallback)), self.hass)

        return self.async_show_form(
            step_id="sleep_schedule",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SLEEP_SCHEDULE_ENABLED,
                        default=self._options.get(CONF_SLEEP_SCHEDULE_ENABLED, DEFAULT_SLEEP_SCHEDULE_ENABLED),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_SLEEP_SCHEDULE_START,
                        default=self._options.get(CONF_SLEEP_SCHEDULE_START, DEFAULT_SLEEP_SCHEDULE_START),
                    ): selector.TimeSelector(),
                    vol.Optional(
                        CONF_SLEEP_SCHEDULE_END,
                        default=self._options.get(CONF_SLEEP_SCHEDULE_END, DEFAULT_SLEEP_SCHEDULE_END),
                    ): selector.TimeSelector(),
                    vol.Optional(
                        CONF_SLEEP_COMFORT_COOL_MIN,
                        default=_get(CONF_SLEEP_COMFORT_COOL_MIN, DEFAULT_SLEEP_COMFORT_COOL_MIN),
                    ): _comfort_selector(self.hass, 50, 80),
                    vol.Optional(
                        CONF_SLEEP_COMFORT_COOL_MAX,
                        default=_get(CONF_SLEEP_COMFORT_COOL_MAX, DEFAULT_SLEEP_COMFORT_COOL_MAX),
                    ): _comfort_selector(self.hass, 55, 88),
                    vol.Optional(
                        CONF_SLEEP_COMFORT_HEAT_MIN,
                        default=_get(CONF_SLEEP_COMFORT_HEAT_MIN, DEFAULT_SLEEP_COMFORT_HEAT_MIN),
                    ): _comfort_selector(self.hass, 45, 72),
                    vol.Optional(
                        CONF_SLEEP_COMFORT_HEAT_MAX,
                        default=_get(CONF_SLEEP_COMFORT_HEAT_MAX, DEFAULT_SLEEP_COMFORT_HEAT_MAX),
                    ): _comfort_selector(self.hass, 50, 78),
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

    APPLIANCE_PRESETS = {
        "hpwh": {
            "name": "Heat Pump Water Heater",
            "estimated_watts": 350,
            "thermal_factor": -9.14,
            "active_states": ["on", "Compressor Running"],
        },
        "electric_dryer": {
            "name": "Electric Dryer (Vented)",
            "estimated_watts": 5000,
            "thermal_factor": 0.6,
            "active_states": ["on"],
        },
        "space_heater": {
            "name": "Space Heater",
            "estimated_watts": 1500,
            "thermal_factor": 3.412,
            "active_states": ["on"],
        },
        "media_center": {
            "name": "Media Center / Gaming PC",
            "estimated_watts": 300,
            "thermal_factor": 3.412,
            "active_states": ["on"],
        },
        "custom": {
            "name": "",
            "estimated_watts": 0,
            "thermal_factor": 3.412,
            "active_states": ["on"],
        },
    }

    def _load_appliances(self) -> list[dict[str, Any]]:
        """Load existing appliances from options."""
        raw = self._options.get(CONF_AUXILIARY_APPLIANCES)
        if raw:
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
        return []

    def _save_appliances(self, appliances: list[dict[str, Any]]) -> None:
        """Save appliances list to options."""
        self._options[CONF_AUXILIARY_APPLIANCES] = json.dumps(appliances)

    async def async_step_appliances(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Appliance hub — summary and actions."""
        self._appliance_preset = None  # clear stale preset state
        appliances = self._load_appliances()

        if user_input is not None:
            action = user_input.get("appliance_action", "done")
            if action == "add":
                self._editing_appliance_index = None
                return await self.async_step_appliance_preset()
            if action == "done":
                return self.async_create_entry(title="", data=self._options)
            if action.startswith("edit_"):
                idx = int(action.split("_", 1)[1])
                if 0 <= idx < len(appliances):
                    self._editing_appliance_index = idx
                    return await self.async_step_appliance_edit()
            if action.startswith("remove_"):
                idx = int(action.split("_", 1)[1])
                if 0 <= idx < len(appliances):
                    removed = appliances.pop(idx)
                    self._save_appliances(appliances)
                    _LOGGER.info("Removed appliance: %s", removed.get("name"))
                return await self.async_step_appliances()

        # Build rich description
        if appliances:
            names = [app.get("name", "Unnamed") for app in appliances]
            summary = f"**Configured appliances:** {', '.join(names)}"
        else:
            summary = (
                "No appliances configured yet. Appliances like water heaters, "
                "dryers, and space heaters affect your home's temperature — "
                "adding them helps the optimizer predict thermal loads more accurately."
            )

        # Build action list: Add first, then edit/remove per appliance, Done last
        options = [
            selector.SelectOptionDict(value="add", label="Add new appliance"),
        ]
        for i, app in enumerate(appliances):
            name = app.get("name", f"Appliance {i+1}")
            options.append(
                selector.SelectOptionDict(value=f"edit_{i}", label=f"Edit: {name}")
            )
            options.append(
                selector.SelectOptionDict(value=f"remove_{i}", label=f"Remove: {name}")
            )
        options.append(
            selector.SelectOptionDict(value="done", label="Done")
        )

        default = "add" if not appliances else "done"

        return self.async_show_form(
            step_id="appliances",
            data_schema=vol.Schema({
                vol.Required("appliance_action", default=default): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.LIST,
                    ),
                ),
            }),
            description_placeholders={"appliance_summary": summary},
        )

    async def async_step_appliance_preset(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick an appliance type preset to pre-fill the edit form."""
        if user_input is not None:
            preset_key = user_input.get("appliance_preset", "custom")
            self._appliance_preset = dict(
                self.APPLIANCE_PRESETS.get(preset_key, self.APPLIANCE_PRESETS["custom"])
            )
            self._appliance_device_discovery = None
            return await self.async_step_appliance_device()

        preset_options = [
            selector.SelectOptionDict(
                value="hpwh",
                label="Heat pump water heater — 350W, cools your home (-9.14 BTU/W)",
            ),
            selector.SelectOptionDict(
                value="electric_dryer",
                label="Electric dryer (vented) — 5,000W, partial heating",
            ),
            selector.SelectOptionDict(
                value="space_heater",
                label="Space heater — 1,500W, heats your home",
            ),
            selector.SelectOptionDict(
                value="media_center",
                label="Media center / gaming PC — 300W, heats your home",
            ),
            selector.SelectOptionDict(
                value="custom",
                label="Custom — enter your own values",
            ),
        ]

        return self.async_show_form(
            step_id="appliance_preset",
            data_schema=vol.Schema({
                vol.Required("appliance_preset", default="hpwh"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=preset_options,
                        mode=selector.SelectSelectorMode.LIST,
                    ),
                ),
            }),
        )

    def _discover_appliance_entities(self, device_id: str) -> dict[str, Any]:
        """Auto-discover state and power entities for a device."""
        from homeassistant.helpers import device_registry, entity_registry

        entity_reg = entity_registry.async_get(self.hass)
        device_reg = device_registry.async_get(self.hass)
        device = device_reg.async_get(device_id)

        result: dict[str, Any] = {}
        if device and device.name:
            result["name"] = device.name

        entities = [
            e for e in entity_reg.entities.values()
            if e.device_id == device_id and not e.disabled
        ]

        # State entity: priority order
        state_entity = None
        for domain in ("water_heater",):
            if not state_entity:
                for e in entities:
                    if e.domain == domain:
                        state_entity = e.entity_id
                        break
        if not state_entity:
            for e in entities:
                if e.domain == "binary_sensor" and e.original_device_class == "running":
                    state_entity = e.entity_id
                    break
        for domain in ("switch", "binary_sensor", "fan"):
            if not state_entity:
                for e in entities:
                    if e.domain == domain:
                        state_entity = e.entity_id
                        break
        if state_entity:
            result["state_entity"] = state_entity

        # Power entity: sensor with device_class power
        for e in entities:
            if e.domain == "sensor" and e.original_device_class == "power":
                result["power_entity"] = e.entity_id
                break

        return result

    async def async_step_appliance_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a device to auto-discover its entities, or configure manually."""
        if user_input is not None:
            skip = user_input.get("skip_device", False)
            device_id = user_input.get("appliance_device")

            if not skip and device_id:
                discovered = self._discover_appliance_entities(device_id)
                if discovered:
                    self._appliance_device_discovery = discovered
            return await self.async_step_appliance_edit()

        return self.async_show_form(
            step_id="appliance_device",
            data_schema=vol.Schema({
                vol.Optional("appliance_device"): selector.DeviceSelector(
                    selector.DeviceSelectorConfig(),
                ),
                vol.Optional("skip_device", default=False): selector.BooleanSelector(),
            }),
        )

    async def async_step_appliance_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add or edit a single appliance."""
        appliances = self._load_appliances()
        editing_idx = getattr(self, "_editing_appliance_index", None)

        # Defaults: from existing appliance (edit), or preset + device discovery (add)
        if editing_idx is not None and editing_idx < len(appliances):
            existing = appliances[editing_idx]
        else:
            preset = getattr(self, "_appliance_preset", None) or {}
            discovered = getattr(self, "_appliance_device_discovery", None) or {}
            existing = {
                "name": discovered.get("name") or preset.get("name", ""),
                "thermal_factor": preset.get("thermal_factor", 3.412),
                "estimated_watts": preset.get("estimated_watts", 0),
                "active_states": preset.get("active_states", ["on"]),
                "state_entity": discovered.get("state_entity", ""),
                "power_entity": discovered.get("power_entity"),
            }

        if user_input is not None:
            name = user_input.get("name", "").strip()
            state_entity = user_input.get("state_entity", "")
            if not name or not state_entity:
                return await self.async_step_appliances()

            active_states_raw = user_input.get("active_states", "on")
            active_states = [s.strip().strip("\"'") for s in active_states_raw.split(",") if s.strip()]
            if not active_states:
                active_states = ["on"]

            slug = name.lower().replace(" ", "_").replace("-", "_")[:32]

            estimated_watts = user_input.get("estimated_watts", 0)
            thermal_factor = user_input.get("thermal_factor") or existing.get("thermal_factor", 3.412)
            watts = float(estimated_watts) if estimated_watts else 0

            appliance: dict[str, Any] = {
                "id": existing.get("id", slug),
                "name": name,
                "state_entity": state_entity,
                "active_states": active_states,
                "thermal_factor": float(thermal_factor),
                "thermal_impact_btu": watts * float(thermal_factor),
            }

            if estimated_watts is not None and estimated_watts > 0:
                appliance["estimated_watts"] = float(estimated_watts)
            power_entity = user_input.get("power_entity")
            if power_entity:
                appliance["power_entity"] = power_entity

            if editing_idx is not None and editing_idx < len(appliances):
                appliances[editing_idx] = appliance
            else:
                appliances.append(appliance)

            self._save_appliances(appliances)
            self._appliance_preset = None
            return await self.async_step_appliances()

        own_eids = self._get_own_entity_ids()

        return self.async_show_form(
            step_id="appliance_edit",
            data_schema=vol.Schema({
                vol.Required(
                    "name",
                    description={"suggested_value": existing.get("name", "")},
                ): selector.TextSelector(),
                vol.Required(
                    "state_entity",
                    description={"suggested_value": existing.get("state_entity", "")},
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain=["switch", "binary_sensor", "sensor", "input_boolean", "water_heater", "fan", "climate"],
                        exclude_entities=own_eids,
                    ),
                ),
                vol.Required(
                    "active_states",
                    default=", ".join(existing.get("active_states", ["on"])),
                ): selector.TextSelector(),
                vol.Optional(
                    "power_entity",
                    description={"suggested_value": existing.get("power_entity")},
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class="power",
                        exclude_entities=own_eids,
                    ),
                ),
                vol.Required(
                    "estimated_watts",
                    default=existing.get("estimated_watts", 0),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=15000, step=50,
                        unit_of_measurement="W",
                        mode=selector.NumberSelectorMode.BOX,
                    ),
                ),
                vol.Required(
                    "thermal_factor",
                    default=existing.get("thermal_factor", 3.412),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=-15, max=5, step=0.01,
                        unit_of_measurement="BTU/W",
                        mode=selector.NumberSelectorMode.BOX,
                    ),
                ),
            }),
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

    async def async_step_blend_mitigation(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure thermostat satellite sensor blend mitigation.

        Some thermostats (Ecobee, Nest) blend their reported temperature toward
        an occupied satellite/room sensor. This causes the EKF thermal model to
        receive artificially warm temperatures at night, corrupting building
        thermal estimates. Three mitigation strategies are available.
        """
        if user_input is not None:
            user_input = self._strip_empty_strings(user_input)
            self._options.update(user_input)
            return self.async_create_entry(title="", data=self._options)

        current_mode = self._options.get(CONF_BLEND_MITIGATION_MODE, BLEND_MODE_NONE)

        schema: dict = {
            vol.Optional(
                CONF_BLEND_MITIGATION_MODE,
                default=current_mode,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=BLEND_MODE_NONE,
                            label="Disabled",
                        ),
                        selector.SelectOptionDict(
                            value=BLEND_MODE_OCCUPANCY,
                            label="Occupancy-Based (requires thermostat occupancy sensor + ≥1 room sensor)",
                        ),
                        selector.SelectOptionDict(
                            value=BLEND_MODE_SCHEDULE,
                            label="Time Schedule (suppress thermostat during configured hours)",
                        ),
                        selector.SelectOptionDict(
                            value=BLEND_MODE_MEDIAN,
                            label="Multi-Sensor Median (requires ≥3 indoor sensors)",
                        ),
                    ],
                ),
            ),
            # Occupancy mode — thermostat built-in occupancy sensor
            vol.Optional(
                CONF_THERMOSTAT_OCCUPANCY_ENTITY,
                default=self._options.get(CONF_THERMOSTAT_OCCUPANCY_ENTITY, ""),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="binary_sensor",
                ),
            ),
            # Schedule mode — suppression window start/end
            vol.Optional(
                CONF_BLEND_SCHEDULE_START,
                default=self._options.get(CONF_BLEND_SCHEDULE_START, DEFAULT_BLEND_SCHEDULE_START),
            ): selector.TimeSelector(),
            vol.Optional(
                CONF_BLEND_SCHEDULE_END,
                default=self._options.get(CONF_BLEND_SCHEDULE_END, DEFAULT_BLEND_SCHEDULE_END),
            ): selector.TimeSelector(),
            # Median mode — outlier threshold
            vol.Optional(
                CONF_BLEND_OUTLIER_THRESHOLD_F,
                default=self._options.get(CONF_BLEND_OUTLIER_THRESHOLD_F, DEFAULT_BLEND_OUTLIER_THRESHOLD_F),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1.5,
                    max=6.0,
                    step=0.5,
                    unit_of_measurement="°F",
                ),
            ),
        }

        return self.async_show_form(
            step_id="blend_mitigation",
            data_schema=vol.Schema(schema),
        )

    async def async_step_humidity_squelch(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure humidity-gated sensor squelching for wet rooms.

        Pairs a temperature sensor with a co-located humidity sensor so the
        temperature sensor is excluded from indoor averages when humidity spikes
        (e.g., during showers), preventing false readings from corrupting the
        thermal model.
        """
        if user_input is not None:
            user_input = self._strip_empty_strings(user_input)
            temp_entity = user_input.get("squelch_temp_entity", "")
            humidity_entity = user_input.get("squelch_humidity_entity", "")
            if temp_entity and humidity_entity:
                pairs = [{"temp_entity": temp_entity, "humidity_entity": humidity_entity}]
            else:
                pairs = []
            self._options[CONF_HUMIDITY_SQUELCH_PAIRS] = json.dumps(pairs)
            return self.async_create_entry(title="", data=self._options)

        # Load existing pair (if any)
        existing_pairs = []
        raw = self._options.get(CONF_HUMIDITY_SQUELCH_PAIRS, "")
        if raw:
            try:
                existing_pairs = json.loads(raw)
            except (ValueError, TypeError):
                pass

        current_temp = existing_pairs[0]["temp_entity"] if existing_pairs else ""
        current_hum = existing_pairs[0]["humidity_entity"] if existing_pairs else ""

        schema: dict = {
            vol.Optional(
                "squelch_temp_entity",
                default=current_temp,
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    device_class="temperature",
                ),
            ),
            vol.Optional(
                "squelch_humidity_entity",
                default=current_hum,
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    device_class="humidity",
                ),
            ),
        }

        return self.async_show_form(
            step_id="humidity_squelch",
            data_schema=vol.Schema(schema),
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
