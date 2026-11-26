"""Validation and unit-conversion helpers for the config and options flow.

Split out of config_flow.py so the schema/step code isn't dominated by
temperature conversion boilerplate. These helpers are stateless; they take
the HA instance (or raw user input) and return values.
"""

from __future__ import annotations

import json
from typing import Any

from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CONF_COMFORT_COOL_MAX,
    CONF_COMFORT_COOL_MIN,
    CONF_COMFORT_HEAT_MAX,
    CONF_COMFORT_HEAT_MIN,
    CONF_MORNING_HOLD_TEMP,
    CONF_MORNING_WARM_OUTDOOR_TEMP,
    CONF_SAFETY_COOL_MAX,
    CONF_SAFETY_HEAT_MIN,
    CONF_SLEEP_COMFORT_COOL_MAX,
    CONF_SLEEP_COMFORT_COOL_MIN,
    CONF_SLEEP_COMFORT_HEAT_MAX,
    CONF_SLEEP_COMFORT_HEAT_MIN,
    DEFAULT_COMFORT_COOL_MAX,
    DEFAULT_COMFORT_COOL_MIN,
    DEFAULT_COMFORT_HEAT_MAX,
    DEFAULT_COMFORT_HEAT_MIN,
    DEFAULT_SAFETY_COOL_MAX,
    DEFAULT_SAFETY_HEAT_MIN,
)


def _is_metric(hass: HomeAssistant) -> bool:
    """Check if the HA instance uses metric (Celsius) temperatures."""
    return hass.config.units.temperature_unit == UnitOfTemperature.CELSIUS


def _f_to_display(temp_f: float, hass: HomeAssistant) -> float:
    """Convert °F to the user's display unit (°F or °C), rounded to int."""
    if _is_metric(hass):
        return round(TemperatureConverter.convert(temp_f, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS))
    return temp_f


def _display_to_f(temp_display: float, hass: HomeAssistant) -> float:
    """Convert from user's display unit back to °F for internal storage."""
    if _is_metric(hass):
        return TemperatureConverter.convert(temp_display, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT)
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
        CONF_COMFORT_COOL_MIN,
        CONF_COMFORT_COOL_MAX,
        CONF_COMFORT_HEAT_MIN,
        CONF_COMFORT_HEAT_MAX,
        CONF_SAFETY_HEAT_MIN,
        CONF_SAFETY_COOL_MAX,
        CONF_SLEEP_COMFORT_COOL_MIN,
        CONF_SLEEP_COMFORT_COOL_MAX,
        CONF_SLEEP_COMFORT_HEAT_MIN,
        CONF_SLEEP_COMFORT_HEAT_MAX,
        CONF_MORNING_HOLD_TEMP,
        CONF_MORNING_WARM_OUTDOOR_TEMP,
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
