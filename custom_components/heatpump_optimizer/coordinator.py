"""DataUpdateCoordinator for the Heat Pump Optimizer.

Wires together the three control layers and adaptive learning:
  Layer 1 (Strategic): StrategicPlanner — re-optimize every 1-4 hours
  Layer 2 (Tactical): TacticalController — 5-min reality check & corrections
  Layer 3 (Watchdog): WatchdogController — event-driven override detection

Learning:
  ThermalEstimator — Extended Kalman Filter for online building parameter estimation
  ModelTracker — prediction error tracking & correction factors (legacy, still used for reporting)
  SolarAdjuster — cloud cover / solar gain corrections (legacy, absorbed by EKF)
  OverrideTracker — manual override pattern detection
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util
import homeassistant.helpers.issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .coordinator_data_builder import DataBuilderMixin
from .coordinator_helpers import HelpersMixin
from .coordinator_persistence import PersistenceMixin

from .adapters.area_occupancy import AreaOccupancyManager
from .adapters.calendar_occupancy import CalendarOccupancyAdapter
from .adapters.forecast import async_get_forecast, async_get_forecast_multi, enrich_forecast_with_grid_data, populate_sun_elevation
from .adapters.occupancy import OccupancyAdapter, OccupancyMode
from .adapters.profile import ProfileAdapter
from .adapters.appliance_manager import ApplianceManager
from .adapters.sensor_hub import SensorHub
from .adapters.thermostat import ThermostatAdapter
from .const import (
    CONF_BAROMETRIC_PRESSURE_ENTITY,
    CONF_CALENDAR_AWAY_KEYWORDS,
    CONF_CALENDAR_DEFAULT_MODE,
    CONF_CALENDAR_ENTITIES,
    CONF_CALENDAR_ENTITY,
    CONF_CALENDAR_HOME_KEYWORDS,
    CONF_ATTIC_TEMP_ENTITY,
    CONF_AUX_HEAT_OVERRIDE_ENTITY,
    CONF_CARBON_WEIGHT,
    CONF_CO2_ENTITY,
    CONF_COOLING_WATTS,
    CONF_COST_WEIGHT,
    CONF_CRAWLSPACE_TEMP_ENTITY,
    CONF_DUCT_TEMP_ENTITY,
    CONF_AUXILIARY_APPLIANCES,
    CONF_DEPARTURE_PROFILES,
    CONF_DEPARTURE_TRIGGER_WINDOW_MINUTES,
    CONF_DEPARTURE_ZONE,
    CONF_DOOR_WINDOW_ENTITIES,
    CONF_DEPARTURE_ZONES,
    CONF_ELECTRICITY_FLAT_RATE,
    CONF_ELECTRICITY_RATE_ENTITY,
    CONF_GRID_IMPORT_ENTITY,
    CONF_HVAC_POWER_DEFAULT_WATTS,
    CONF_HEATING_WATTS,
    CONF_HVAC_POWER_ENTITY,
    CONF_HOME_SQFT,
    CONF_HVAC_TONNAGE,
    CONF_HVAC_SEER,
    CONF_AUX_HEAT_TYPE,
    CONF_AUX_HEAT_KW,
    BLEND_MODE_NONE,
    BLEND_MODE_MEDIAN,
    BLEND_MODE_OCCUPANCY,
    BLEND_MODE_SCHEDULE,
    CONF_AREA_SENSOR_CONFIG,
    CONF_BLEND_MITIGATION_MODE,
    CONF_BLEND_OUTLIER_THRESHOLD_F,
    CONF_BLEND_SCHEDULE_END,
    CONF_BLEND_SCHEDULE_START,
    CONF_HUMIDITY_SQUELCH_PAIRS,
    CONF_THERMOSTAT_OCCUPANCY_ENTITY,
    CONF_INDOOR_HUMIDITY_ENTITIES,
    CONF_INDOOR_TEMP_ENTITIES,
    CONF_INDOOR_WEIGHTING_MODE,
    CONF_OCCUPIED_WEIGHT_MULTIPLIER,
    CONF_OCCUPANCY_DEBOUNCE_MINUTES,
    CONF_ROOM_OCCUPANCY_DEBOUNCE_MINUTES,
    CONF_OCCUPANCY_ENTITIES,
    CONF_HOME_ZONE_STATES,
    CONF_PROFILE_CONTROL_ENABLED,
    CONF_PROFILE_ENTITY,
    CONF_PROFILE_ENTITY_TYPE,
    CONF_PROFILE_MAP_HOME,
    CONF_PROFILE_MAP_AWAY,
    CONF_PROFILE_MAP_SLEEP,
    CONF_ARRIVAL_SLEEP_CUTOFF,
    CONF_SLEEP_AWAY_OVERRIDE,
    CONF_SLEEP_AWAY_DELAY_MINUTES,
    DEFAULT_PROFILE_CONTROL_ENABLED,
    DEFAULT_PROFILE_ENTITY_TYPE,
    DEFAULT_PROFILE_MAP_HOME,
    DEFAULT_PROFILE_MAP_AWAY,
    DEFAULT_PROFILE_MAP_SLEEP,
    DEFAULT_ARRIVAL_SLEEP_CUTOFF,
    DEFAULT_SLEEP_AWAY_OVERRIDE,
    DEFAULT_SLEEP_AWAY_DELAY_MINUTES,
    EVENT_PROFILE_CHANGED,
    CONF_PRECONDITIONING_BUFFER_MINUTES,
    CONF_TRAVEL_TIME_SENSOR,
    CONF_TRAVEL_TIME_SENSORS,
    CONF_OUTDOOR_HUMIDITY_ENTITIES,
    CONF_OUTDOOR_TEMP_ENTITIES,
    CONF_SOLAR_EXPORT_RATE_ENTITY,
    CONF_SOLAR_IRRADIANCE_ENTITY,
    CONF_SOLAR_PRODUCTION_ENTITY,
    CONF_SUN_ENTITY,
    CONF_UV_INDEX_ENTITY,
    CONF_TOU_SCHEDULE,
    CONF_CALIBRATION_ENABLED,
    CONF_USE_ADAPTIVE_MODEL,
    CONF_WIND_SPEED_ENTITY,
    DEFAULT_CALENDAR_AWAY_KEYWORDS,
    DEFAULT_CALENDAR_DEFAULT_MODE,
    DEFAULT_CALENDAR_HOME_KEYWORDS,
    DEFAULT_DEPARTURE_TRIGGER_WINDOW_MINUTES,
    DEFAULT_BLEND_OUTLIER_THRESHOLD_F,
    DEFAULT_BLEND_SCHEDULE_END,
    DEFAULT_BLEND_SCHEDULE_START,
    DEFAULT_INDOOR_WEIGHTING_MODE,
    DEFAULT_OCCUPANCY_DEBOUNCE_MINUTES,
    DEFAULT_OCCUPIED_WEIGHT_MULTIPLIER,
    DEFAULT_PRECONDITIONING_BUFFER_MINUTES,
    DEFAULT_ROOM_OCCUPANCY_DEBOUNCE_MINUTES,
    DEFAULT_SUN_ENTITY,
    DEFAULT_CARBON_WEIGHT,
    DEFAULT_COST_WEIGHT,
    DEFAULT_DUCT_AUX_RATE_MIN,
    DEFAULT_DUCT_AUX_RATE_THRESHOLD_F,
    DEFAULT_DUCT_AUX_THRESHOLD_F,
    DEFAULT_HVAC_POWER_WATTS,
    DEFAULT_MODEL_CONFIDENCE_THRESHOLD,
    ACTIVATION_TIER_LEARNING,
    ACTIVATION_TIER_CONSERVATIVE,
    ACTIVATION_TIER_STANDARD,
    ACTIVATION_TIER_CONFIDENT,
    CONSERVATIVE_DRIFT_CONFIDENCE,
    CONSERVATIVE_MIN_MODE_OBS,
    AGGRESSIVENESS_AGGRESSIVE,
    AGGRESSIVENESS_CONSERVATIVE,
    DEFAULT_FORECAST_CACHE_MAX_AGE_HOURS,
    DEFAULT_HISTORY_BOOTSTRAP_DAYS,
    DEFAULT_SAFETY_COOL_MAX,
    DEFAULT_SAFETY_HEAT_MIN,
    DEFAULT_STALE_FORECAST_HOURS,
    DEFAULT_THERMOSTAT_TOLERANCE_CYCLES,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    EVENT_ACCURACY_TIER_CHANGED,
    EVENT_BASELINE_COMPLETE,
    EVENT_CALENDAR_OVERRIDE,
    EVENT_CONFIDENCE_REACHED,
    EVENT_DISTURBED,
    EVENT_MODE_CHANGED,
    EVENT_MODEL_ALERT,
    EVENT_OPTIMIZATION_COMPLETE,
    EVENT_OVERRIDE_DETECTED,
    EVENT_PRECONDITIONING_COMPLETE,
    EVENT_PRECONDITIONING_START,
    EVENT_SAFE_MODE_ENTERED,
    PHASE_PRECONDITIONING,
    PHASE_IDLE,
    PHASE_LEARNING,
    PHASE_PAUSED,
    PHASE_SAFE_MODE,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .controllers.strategic import StrategicPlanner
from .controllers.tactical import TacticalController, TacticalState
from .controllers.watchdog import WatchdogController
from .engine.data_types import (
    ForecastPoint,
    OccupancyForecastPoint,
    OptimizationWeights,
    PreconditionPlan,
    ScheduleEntry,
)
from .engine.comfort import calculate_apparent_temperature
from .engine.optimizer import ScheduleOptimizer
from .engine.precondition_planner import PreconditionPlanner
from .engine.performance_model import PerformanceModel
from .engine.thermal_simulator import ThermalSimulator
from .engine.adaptive_performance_model import AdaptivePerformanceModel
from .engine.counterfactual_simulator import CounterfactualSimulator
from .learning.aux_heat_learner import AuxHeatLearner
from .learning.baseline_capture import BaselineCapture
from .learning.model_tracker import ModelTracker
from .learning.override_tracker import OverrideTracker
from .learning.performance_profiler import PerformanceProfiler
from .learning.solar_adjuster import SolarAdjuster
from .learning.coefficient_calibrator import CoefficientCalibrator
from .learning.coefficient_store import CoefficientStore
from .learning.thermal_estimator import ThermalEstimator
from .savings_tracker import SavingsTracker, TIER_LEARNING, TIER_PROJECTED, TIER_ESTIMATED, TIER_SIMULATED, TIER_CALIBRATED

_LOGGER = logging.getLogger(__name__)

# How often to persist learned parameters
LEARNING_PERSIST_INTERVAL_HOURS = 1

# Timeouts for long-running awaits inside the 5-min update cycle.
# Both must stay well under the 5-min interval to avoid overlapping refreshes.
FORECAST_FETCH_TIMEOUT_S = 30.0
OPTIMIZER_TIMEOUT_S = 90.0


def _float_or_none(value: object) -> float | None:
    """Return a float if *value* is a non-zero number, else None."""
    try:
        v = float(value)  # type: ignore[arg-type]
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


class HeatPumpOptimizerCoordinator(
    DataBuilderMixin, HelpersMixin, PersistenceMixin, DataUpdateCoordinator,
):
    """Coordinator that manages the full optimization lifecycle."""

    def __init__(
        self,
        hass: HomeAssistant,
        climate_entity_id: str,
        weather_entity_id: str,
        comfort_cool: tuple[float, float],
        comfort_heat: tuple[float, float],
        *,
        entry: ConfigEntry | None = None,
        weather_entity_ids: list[str] | None = None,
        safety_limits: tuple[float, float] | None = None,
        occupancy_entity_ids: list[str] | None = None,
        options: dict[str, Any] | None = None,
        initialization_mode: str = "learning",
        model_import_data: str | None = None,
        behavior: dict[str, Any] | None = None,
        sleep_config: dict | None = None,
        monitor_only: bool = False,
    ):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES),
        )

        self.entry = entry

        # Configuration
        self._monitor_only = monitor_only
        self.climate_entity_id = climate_entity_id
        self.weather_entity_id = weather_entity_id
        self._weather_entity_ids = weather_entity_ids or [weather_entity_id]
        self.comfort_cool = comfort_cool
        self.comfort_heat = comfort_heat
        self.sleep_config = sleep_config or {}

        # Safety limits (absolute guardrails)
        self.safety_limits = safety_limits or (DEFAULT_SAFETY_HEAT_MIN, DEFAULT_SAFETY_COOL_MAX)

        # Behavior parameters
        behavior = behavior or {}
        self._aggressiveness: str = behavior.get("aggressiveness", "balanced")
        self._override_grace_hours: float = behavior.get("override_grace_hours", 2.0)
        self._reoptimize_interval_hours: int = behavior.get("reoptimize_interval_hours", 4)
        self._max_setpoint_change_per_hour: float = behavior.get("max_setpoint_change_per_hour", 4.0)
        self._away_comfort_delta: float = behavior.get("away_comfort_delta", 4.0)
        self._thermostat_deadband: float = behavior.get("thermostat_deadband", 0.5)
        self._dwell_time_minutes: int = behavior.get("dwell_time_minutes", 15)

        # Savings tracking config (from options flow)
        opts = options or {}
        self._co2_entity_id: str | None = opts.get(CONF_CO2_ENTITY) or None
        self._rate_entity_id: str | None = opts.get(CONF_ELECTRICITY_RATE_ENTITY) or None
        self._flat_rate: float | None = opts.get(CONF_ELECTRICITY_FLAT_RATE) or None
        self._power_entity_id: str | None = opts.get(CONF_HVAC_POWER_ENTITY) or None
        self._tou_schedule: list[dict] | None = opts.get(CONF_TOU_SCHEDULE) or None
        self._carbon_weight: float = opts.get(CONF_CARBON_WEIGHT, DEFAULT_CARBON_WEIGHT)
        self._cost_weight: float = opts.get(CONF_COST_WEIGHT, DEFAULT_COST_WEIGHT)
        self._aux_heat_override_entity_id: str | None = opts.get(CONF_AUX_HEAT_OVERRIDE_ENTITY) or None
        self._duct_temp_entity: str | None = opts.get(CONF_DUCT_TEMP_ENTITY) or None
        self._duct_temp_buffer: list[tuple[float, float]] = []  # (timestamp_s, temp_f)

        # Physical system specs — merged into opts by __init__.py
        # (onboarding data merged first, options flow overrides second)
        self._home_sqft: float | None = _float_or_none(opts.get(CONF_HOME_SQFT))
        self._hvac_tonnage: float | None = _float_or_none(opts.get(CONF_HVAC_TONNAGE))
        self._hvac_seer: float | None = _float_or_none(opts.get(CONF_HVAC_SEER))
        self._aux_heat_type: str = opts.get(CONF_AUX_HEAT_TYPE, "unknown")
        self._aux_heat_kw: float | None = _float_or_none(opts.get(CONF_AUX_HEAT_KW))

        # Derive power_watts from user-provided specs when no explicit override is set.
        # Priority: explicit watts > tonnage+SEER > tonnage-only estimate > hard default.
        if CONF_HVAC_POWER_DEFAULT_WATTS in opts:
            self._power_default_watts: float = float(opts[CONF_HVAC_POWER_DEFAULT_WATTS])
        elif self._hvac_tonnage and self._hvac_seer:
            # Rated watts = rated BTU/hr ÷ SEER  (e.g. 3 ton @ 16 SEER = 2,250 W)
            self._power_default_watts = (self._hvac_tonnage * 12000.0) / self._hvac_seer
            _LOGGER.debug(
                "Derived power_default_watts=%.0f W from tonnage=%.1f + SEER=%.1f",
                self._power_default_watts, self._hvac_tonnage, self._hvac_seer,
            )
        elif self._hvac_tonnage:
            # Estimate: ~850 W/ton ≈ 14 SEER (conservative — slightly overestimates cost)
            self._power_default_watts = self._hvac_tonnage * 850.0
            _LOGGER.debug(
                "Estimated power_default_watts=%.0f W from tonnage=%.1f (no SEER provided)",
                self._power_default_watts, self._hvac_tonnage,
            )
        else:
            self._power_default_watts = float(DEFAULT_HVAC_POWER_WATTS)

        # Thermostat satellite blend mitigation config
        self._blend_mode: str = opts.get(CONF_BLEND_MITIGATION_MODE, BLEND_MODE_NONE)
        self._thermostat_occ_entity: str | None = opts.get(CONF_THERMOSTAT_OCCUPANCY_ENTITY) or None
        self._blend_outlier_threshold_f: float = float(
            opts.get(CONF_BLEND_OUTLIER_THRESHOLD_F, DEFAULT_BLEND_OUTLIER_THRESHOLD_F)
        )
        # Parse schedule start/end as datetime.time objects
        def _parse_time(s: str, default: str) -> dt_time:
            try:
                h, m = (s or default).split(":")
                return dt_time(int(h), int(m))
            except (ValueError, AttributeError):
                h, m = default.split(":")
                return dt_time(int(h), int(m))
        self._blend_schedule_start: dt_time = _parse_time(
            opts.get(CONF_BLEND_SCHEDULE_START, DEFAULT_BLEND_SCHEDULE_START),
            DEFAULT_BLEND_SCHEDULE_START,
        )
        self._blend_schedule_end: dt_time = _parse_time(
            opts.get(CONF_BLEND_SCHEDULE_END, DEFAULT_BLEND_SCHEDULE_END),
            DEFAULT_BLEND_SCHEDULE_END,
        )
        # Runtime blend detection state (updated each cycle)
        self._thermostat_blend_suspected: bool = False
        self._last_indoor_noise_scale: float = 1.0

        # SensorHub — centralized sensor reads with fallback chains
        self.sensor_hub = SensorHub(
            hass,
            outdoor_temp_entities=opts.get(CONF_OUTDOOR_TEMP_ENTITIES) or [],
            outdoor_humidity_entities=opts.get(CONF_OUTDOOR_HUMIDITY_ENTITIES) or [],
            indoor_temp_entities=opts.get(CONF_INDOOR_TEMP_ENTITIES) or [],
            indoor_humidity_entities=opts.get(CONF_INDOOR_HUMIDITY_ENTITIES) or [],
            wind_speed_entity=opts.get(CONF_WIND_SPEED_ENTITY),
            solar_irradiance_entity=opts.get(CONF_SOLAR_IRRADIANCE_ENTITY),
            uv_index_entity=opts.get(CONF_UV_INDEX_ENTITY),
            barometric_pressure_entity=opts.get(CONF_BAROMETRIC_PRESSURE_ENTITY),
            sun_entity=opts.get(CONF_SUN_ENTITY, DEFAULT_SUN_ENTITY),
            solar_production_entity=opts.get(CONF_SOLAR_PRODUCTION_ENTITY),
            grid_import_entity=opts.get(CONF_GRID_IMPORT_ENTITY),
            solar_export_rate_entity=opts.get(CONF_SOLAR_EXPORT_RATE_ENTITY),
            door_window_entities=opts.get(CONF_DOOR_WINDOW_ENTITIES) or [],
            attic_temp_entity=opts.get(CONF_ATTIC_TEMP_ENTITY),
            crawlspace_temp_entity=opts.get(CONF_CRAWLSPACE_TEMP_ENTITY),
            duct_temp_entity=opts.get(CONF_DUCT_TEMP_ENTITY),
            power_entity=self._power_entity_id,
            power_default_watts=self._power_default_watts,
            cooling_watts=opts.get(CONF_COOLING_WATTS),
            heating_watts=opts.get(CONF_HEATING_WATTS),
            aux_heat_kw=self._aux_heat_kw,
            co2_entity=self._co2_entity_id,
            rate_entity=self._rate_entity_id,
            flat_rate=self._flat_rate,
            blend_mode=self._blend_mode,
            blend_outlier_threshold_f=self._blend_outlier_threshold_f,
            humidity_squelch_pairs=self._parse_squelch_pairs(
                opts.get(CONF_HUMIDITY_SQUELCH_PAIRS, "")
            ),
        )

        # Auxiliary appliance manager (optional — HPWH, dryer, etc.)
        self.appliance_manager = ApplianceManager(
            hass, opts.get(CONF_AUXILIARY_APPLIANCES)
        )
        if self.appliance_manager.configured:
            _LOGGER.info(
                "Auxiliary appliances configured: %d appliance(s)",
                len(self.appliance_manager._configs),
            )

        # Room-aware area occupancy manager (optional)
        self.area_manager: AreaOccupancyManager | None = None
        weighting_mode = opts.get(CONF_INDOOR_WEIGHTING_MODE, DEFAULT_INDOOR_WEIGHTING_MODE)
        area_config_json = opts.get(CONF_AREA_SENSOR_CONFIG, "")
        if area_config_json:
            area_config = AreaOccupancyManager.deserialize_area_config(area_config_json)
            self.area_manager = AreaOccupancyManager(
                hass,
                weighting_mode=weighting_mode,
                area_config=area_config,
                debounce_minutes=opts.get(
                    CONF_ROOM_OCCUPANCY_DEBOUNCE_MINUTES,
                    DEFAULT_ROOM_OCCUPANCY_DEBOUNCE_MINUTES,
                ),
                occupied_weight_multiplier=opts.get(
                    CONF_OCCUPIED_WEIGHT_MULTIPLIER,
                    DEFAULT_OCCUPIED_WEIGHT_MULTIPLIER,
                ),
            )
            self.sensor_hub.set_area_manager(self.area_manager)
            _LOGGER.info(
                "Room-aware sensing enabled: mode=%s, %d areas configured",
                weighting_mode,
                len(area_config),
            )

        # Engine initialization — varies by mode
        self._initialization_mode = initialization_mode
        self._use_adaptive = opts.get(CONF_USE_ADAPTIVE_MODEL, True)

        # Read current indoor temp for EKF initialization (may not be available yet)
        _init_state = ThermostatAdapter(hass, climate_entity_id).read_state()
        _init_indoor = (
            _init_state.indoor_temp
            if _init_state.available and _init_state.indoor_temp is not None
            else 72.0
        )

        if initialization_mode == "learning":
            # Cold start: synthetic defaults, EKF learns from scratch.
            # Tonnage and sqft provide better priors when available, reducing
            # the convergence window from weeks to days.
            self.model = PerformanceModel.from_defaults()
            self.estimator = ThermalEstimator.cold_start(
                indoor_temp=_init_indoor,
                tonnage=self._hvac_tonnage,
                sqft=self._home_sqft,
            )
            _LOGGER.info(
                "Initialized in learning mode — model will calibrate over %s "
                "(tonnage=%s, sqft=%s)",
                "2-3 days" if self._hvac_tonnage else "2-3 weeks",
                f"{self._hvac_tonnage:.1f} ton" if self._hvac_tonnage else "unknown",
                f"{self._home_sqft:.0f} ft²" if self._home_sqft else "unknown",
            )
        elif initialization_mode == "import" and model_import_data:
            # Restore from exported model
            try:
                parsed = json.loads(model_import_data) if isinstance(model_import_data, str) else model_import_data
                state_data = parsed.get("state", parsed)
                self.estimator = ThermalEstimator.from_dict(state_data)
                self.model = PerformanceModel.from_estimator(self.estimator)
                _LOGGER.info(
                    "Initialized from imported model (%d observations, confidence=%.0f%%)",
                    self.estimator._n_obs, self.estimator.confidence * 100,
                )
            except (KeyError, ValueError, TypeError) as err:
                _LOGGER.error("Failed to import model (%s) — falling back to defaults", type(err).__name__, exc_info=True)
                self.model = PerformanceModel.from_defaults()
                self.estimator = ThermalEstimator.cold_start(
                    indoor_temp=_init_indoor,
                    tonnage=self._hvac_tonnage,
                    sqft=self._home_sqft,
                )
        else:
            # Unknown mode — fall back to learning
            _LOGGER.warning(
                "Unknown initialization mode '%s' — using learning mode",
                initialization_mode,
            )
            self.model = PerformanceModel.from_defaults()
            self.estimator = ThermalEstimator.cold_start(
                indoor_temp=_init_indoor,
                tonnage=self._hvac_tonnage,
                sqft=self._home_sqft,
            )

        self.simulator = ThermalSimulator(self.model)
        self.optimizer = ScheduleOptimizer(self.model, self.simulator)

        # Adaptive model (Kalman filter)
        self.adaptive_model = AdaptivePerformanceModel(self.estimator)
        # Propagate thermostat differential from model
        self.adaptive_model.cool_differential = self.model.cool_differential
        self.adaptive_model.heat_differential = self.model.heat_differential

        # Performance profiler (empirical HVAC deltas, reaches confidence faster
        # than EKF; primary model for optimization)
        self.profiler = PerformanceProfiler()

        # Coefficient calibrator (slow outer loop for EKF physics coefficients)
        self.coeff_store = CoefficientStore()
        self.coeff_calibrator = CoefficientCalibrator(self.coeff_store)
        self.estimator._coeff_store = self.coeff_store

        # Aux heat activation learner
        self.aux_heat_learner = AuxHeatLearner(
            default_hp_watts=self._power_default_watts
        )
        self._cached_aux_heat_active: bool = False

        # Adapters (HA ↔ engine)
        self.thermostat = ThermostatAdapter(hass, climate_entity_id)
        self.occupancy = OccupancyAdapter(
            hass,
            entity_ids=occupancy_entity_ids,
            home_zone_states=opts.get(CONF_HOME_ZONE_STATES, []),
        )

        # Thermostat profile control (optional — home/away/sleep presets)
        self._profile_adapter: ProfileAdapter | None = None
        if opts.get(CONF_PROFILE_CONTROL_ENABLED, DEFAULT_PROFILE_CONTROL_ENABLED):
            profile_entity = opts.get(CONF_PROFILE_ENTITY)
            profile_type = opts.get(
                CONF_PROFILE_ENTITY_TYPE, DEFAULT_PROFILE_ENTITY_TYPE
            )
            if profile_entity or profile_type == "preset":
                self._profile_adapter = ProfileAdapter(
                    hass,
                    entity_type=profile_type,
                    entity_id=profile_entity,
                    climate_entity_id=climate_entity_id,
                    profile_map={
                        "home": opts.get(CONF_PROFILE_MAP_HOME, DEFAULT_PROFILE_MAP_HOME),
                        "away": opts.get(CONF_PROFILE_MAP_AWAY, DEFAULT_PROFILE_MAP_AWAY),
                        "sleep": opts.get(CONF_PROFILE_MAP_SLEEP, DEFAULT_PROFILE_MAP_SLEEP),
                    },
                )
        self._arrival_sleep_cutoff: str = opts.get(
            CONF_ARRIVAL_SLEEP_CUTOFF, DEFAULT_ARRIVAL_SLEEP_CUTOFF
        )
        self._sleep_away_override: bool = opts.get(
            CONF_SLEEP_AWAY_OVERRIDE, DEFAULT_SLEEP_AWAY_OVERRIDE
        )
        self._sleep_away_delay_minutes: int = opts.get(
            CONF_SLEEP_AWAY_DELAY_MINUTES, DEFAULT_SLEEP_AWAY_DELAY_MINUTES
        )
        self._last_profile_mode: str | None = None
        self._away_delay_pending_since: datetime | None = None

        # Calendar-based occupancy scheduling (optional, multi-calendar)
        calendar_entities = opts.get(CONF_CALENDAR_ENTITIES, [])
        # Migrate singular → plural
        if not calendar_entities:
            singular = opts.get(CONF_CALENDAR_ENTITY)
            if singular:
                calendar_entities = [singular]
        if calendar_entities:
            self.calendar_occupancy: CalendarOccupancyAdapter | None = CalendarOccupancyAdapter(
                hass,
                calendar_entity_ids=calendar_entities,
                home_keywords=opts.get(CONF_CALENDAR_HOME_KEYWORDS, DEFAULT_CALENDAR_HOME_KEYWORDS),
                away_keywords=opts.get(CONF_CALENDAR_AWAY_KEYWORDS, DEFAULT_CALENDAR_AWAY_KEYWORDS),
                default_when_no_event=opts.get(CONF_CALENDAR_DEFAULT_MODE, DEFAULT_CALENDAR_DEFAULT_MODE),
            )
        else:
            self.calendar_occupancy = None

        # Pre-conditioning planner
        self.precondition_planner = PreconditionPlanner(self.model)
        self._precondition_plan: PreconditionPlan | None = None
        self._precondition_buffer_minutes: int = opts.get(
            CONF_PRECONDITIONING_BUFFER_MINUTES, DEFAULT_PRECONDITIONING_BUFFER_MINUTES
        )

        # Departure-aware pre-conditioning — per-person profiles
        self._departure_profiles: list[dict[str, str]] = self._load_departure_profiles(opts)
        self._departure_trigger_window: int = opts.get(
            CONF_DEPARTURE_TRIGGER_WINDOW_MINUTES, DEFAULT_DEPARTURE_TRIGGER_WINDOW_MINUTES
        )
        # Track per-person departure detection state
        self._departure_detected: dict[str, bool] = {}
        self._occupancy_timeline: list[OccupancyForecastPoint] = []

        # Layer 1: Strategic Planner
        # Aggressive mode: halve the reoptimize interval for faster adaptation
        reopt_hours = self._reoptimize_interval_hours
        if self._aggressiveness == AGGRESSIVENESS_AGGRESSIVE:
            reopt_hours = max(1, reopt_hours // 2)

        self.strategic = StrategicPlanner(
            optimizer=self.optimizer,
            resist_balance_point=self.model.resist_balance_point or 50.0,
            sleep_config=self.sleep_config,
            reoptimize_interval_hours=float(reopt_hours),
        )

        # Layer 2: Tactical Controller
        self.tactical = TacticalController()

        # Layer 3: Watchdog
        self.watchdog = WatchdogController()
        self.watchdog.set_callbacks(
            on_override_detected=self._on_override_detected,
            on_override_cleared=self._on_override_cleared,
            on_mode_change=self._on_mode_change,
        )

        # Learning
        self.model_tracker = ModelTracker()
        self.solar_adjuster = SolarAdjuster(
            latitude=self._get_latitude(),
        )
        self.override_tracker = OverrideTracker()

        # Savings tracking
        self.savings_tracker = SavingsTracker()
        self.baseline_capture = BaselineCapture()
        self.counterfactual = CounterfactualSimulator(
            deadband=self._thermostat_deadband
        )

        # Set rated HVAC capacity from user's tonnage config
        if self._hvac_tonnage:
            self.counterfactual.set_rated_capacity(
                q_cool=self._hvac_tonnage * 12000.0,
                q_heat=self._hvac_tonnage * 12000.0 * 1.1,
            )

        # Coordinator state
        self._phase: str = PHASE_IDLE
        self._active: bool = True
        self._paused: bool = False
        self._cached_activation_tier: str | None = None  # Reset each update cycle
        self._last_learning_persist: datetime | None = None
        self._thermostat_was_unavailable: bool = False
        self._thermostat_unavailable_count: int = 0
        # _startup_delay_done removed — boot readiness handled by
        # EVENT_HOMEASSISTANT_STARTED listener in __init__.py
        self._last_good_thermo_state: Any = None
        self._confidence_threshold_reached: bool = False
        self._last_model_alert: bool = False
        self._last_accuracy_tier: str = TIER_LEARNING
        self._baseline_complete_fired: bool = False
        self._last_profiler_status: str = "pending"
        self._history_bootstrap_completed: bool = False
        self._history_bootstrap_result: str | None = None  # "ok", reason, or None
        self._bootstrap_retry_count: int = 0
        self._bootstrap_retry_unsub: Any = None  # cancel callback for scheduled retry
        self._demand_response_active: bool = False
        self._demand_response_delta: float = 0.0
        self._demand_response_end: datetime | None = None
        self._temp_history: list[float] = []  # last 288 readings (24h at 5min)

        # Forecast cache for resilience
        self._last_good_forecast: list[ForecastPoint] | None = None
        self._last_forecast_time: datetime | None = None
        self._last_forecast_source: str | None = None

        # Apparent temperature (humidity-adjusted comfort)
        self._current_indoor_humidity: float | None = None
        self._current_apparent_temp: float | None = None

        # External constraints (from set_constraint service)
        self._active_constraints: list[dict[str, Any]] = []

        # Rate limiter state
        self._last_written_setpoint_time: datetime | None = None
        self._min_dwell_seconds: int = self._dwell_time_minutes * 60

        # Storage
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._unsub_state_listener = None
        self._unsub_occupancy_listener = None

    # ── Setup / Shutdown ────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Initialize: load persisted state, start watchdog, run first optimization."""
        # Restore persisted learning data
        stored = await self._store.async_load()
        if stored:
            self._restore_learning_state(stored)
            # Track whether a previous bootstrap succeeded (persisted flag)
            self._history_bootstrap_completed = stored.get(
                "_history_bootstrap_completed", False
            )
            self._bootstrap_retry_count = stored.get(
                "_bootstrap_retry_count", 0
            )

        # NOTE: History bootstrap moved to async_at_started callback
        # (called from __init__.py) where the recorder is guaranteed ready.

        # Layer 3: Watchdog — listen for thermostat state changes
        self._unsub_state_listener = async_track_state_change_event(
            self.hass,
            self.climate_entity_id,
            self._handle_thermostat_state_change,
        )
        if self.entry is not None:
            self.entry.async_on_unload(self._unsub_state_listener)

        # Profile control: listen for person entity changes for fast updates
        if self._profile_adapter and self.occupancy.entity_ids:
            self._unsub_occupancy_listener = async_track_state_change_event(
                self.hass,
                self.occupancy.entity_ids,
                self._handle_occupancy_state_change,
            )
            if self.entry is not None:
                self.entry.async_on_unload(self._unsub_occupancy_listener)

        # Run initial optimization (non-fatal — weather/thermostat may not be ready yet)
        try:
            await self._run_strategic_optimization()
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Initial optimization failed — will retry on next update cycle",
                exc_info=True,
            )

    async def async_shutdown(self) -> None:
        """Graceful shutdown: safe setpoint, persist state.

        State-change listeners registered with entry.async_on_unload are
        cleaned up by HA automatically; only listeners without an entry
        (bootstrap retry) are unsubbed here.
        """
        if self._bootstrap_retry_unsub is not None:
            self._bootstrap_retry_unsub()
            self._bootstrap_retry_unsub = None

        # Fallback unsub for unit-test instances created without an entry.
        if self.entry is None:
            if self._unsub_state_listener:
                self._unsub_state_listener()
            if self._unsub_occupancy_listener:
                self._unsub_occupancy_listener()

        # Write safe midpoint so thermostat doesn't hold an extreme temp
        thermo_state = self.thermostat.read_state()
        if thermo_state.available and self._baseline_ready_for_control and not self._monitor_only:
            comfort = self._active_comfort_range()
            await self.thermostat.async_write_safe_default(*comfort)

        # Persist everything
        await self._persist_state()

    # ── Main update loop (Layer 2: every 5 minutes) ─────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        """Called every 5 minutes by DataUpdateCoordinator."""
        try:
            return await self._update_cycle()
        except UpdateFailed:
            raise
        except Exception as err:
            _LOGGER.error(
                "Update cycle failed: %s: %s",
                type(err).__name__, err, exc_info=True,
            )
            raise UpdateFailed(f"{type(err).__name__}: {err}") from err

    async def _update_cycle(self) -> dict[str, Any]:
        """Full 5-minute update cycle."""
        self._cached_activation_tier = None  # Invalidate per-cycle cache
        now = datetime.now(timezone.utc)

        # Always fetch weather early — panel needs it even when paused/override
        try:
            new_forecast, forecast_source = await asyncio.wait_for(
                async_get_forecast_multi(self.hass, self._weather_entity_ids),
                timeout=FORECAST_FETCH_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Forecast fetch timed out after %.0fs — using last-good forecast",
                FORECAST_FETCH_TIMEOUT_S,
            )
            new_forecast, forecast_source = None, None
        if new_forecast:
            # Populate sun elevation for all forecast hours using lat/lon
            populate_sun_elevation(
                new_forecast,
                latitude=self._get_latitude(),
                longitude=self.hass.config.longitude or -77.0,
            )
            self._last_good_forecast = new_forecast
            self._last_forecast_time = now
            self._last_forecast_source = forecast_source
            ir.async_delete_issue(self.hass, DOMAIN, "forecast_unavailable")

        # Expire any constraints that have timed out
        self._expire_constraints()

        # Read current thermostat state
        thermo_state = self.thermostat.read_state()
        if not thermo_state.available:
            self._thermostat_unavailable_count += 1
            if self._thermostat_unavailable_count < DEFAULT_THERMOSTAT_TOLERANCE_CYCLES:
                # Brief unavailability — use last known state if available
                if self._last_good_thermo_state is not None:
                    _LOGGER.warning(
                        "Thermostat unavailable (cycle %d/%d) — using last known state",
                        self._thermostat_unavailable_count,
                        DEFAULT_THERMOSTAT_TOLERANCE_CYCLES,
                    )
                    thermo_state = self._last_good_thermo_state
                else:
                    _LOGGER.warning("Thermostat unavailable — no cached state, skipping update")
                    self._thermostat_was_unavailable = True
                    return self._build_data(thermo_state=None)
            else:
                _LOGGER.warning(
                    "Thermostat unavailable for %d cycles — skipping update",
                    self._thermostat_unavailable_count,
                )
                self._thermostat_was_unavailable = True
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    "thermostat_unavailable",
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="thermostat_unavailable",
                    translation_placeholders={
                        "entity_id": self.climate_entity_id,
                        "minutes": str(self._thermostat_unavailable_count * DEFAULT_UPDATE_INTERVAL_MINUTES),
                    },
                )
                return self._build_data(thermo_state=None)
        else:
            # Thermostat is available — reset counter and cache state
            if self._thermostat_unavailable_count > 0:
                _LOGGER.info(
                    "Thermostat recovered after %d unavailable cycles",
                    self._thermostat_unavailable_count,
                )
                ir.async_delete_issue(self.hass, DOMAIN, "thermostat_unavailable")
            self._thermostat_unavailable_count = 0
            self._last_good_thermo_state = thermo_state

        # Thermostat recovered from unavailable — force re-optimization
        if self._thermostat_was_unavailable:
            _LOGGER.info("Thermostat recovered from unavailable — triggering re-optimization")
            self._thermostat_was_unavailable = False
            await self._run_strategic_optimization()

        # Track temperature history for stale sensor detection
        if thermo_state.indoor_temp is not None:
            self._temp_history.append(thermo_state.indoor_temp)
            if len(self._temp_history) > 288:  # 24h at 5min intervals
                self._temp_history = self._temp_history[-288:]

        # Update room-level occupancy and sensor readings (if configured)
        if self.area_manager is not None:
            self.area_manager.update_occupancy()
            self.area_manager.update_readings()

        # Read indoor humidity from SensorHub (weighted → entity average → thermostat)
        indoor_humidity_reading = self.sensor_hub.read_weighted_indoor_humidity(
            thermo_state.humidity
        )
        self._current_indoor_humidity = (
            indoor_humidity_reading.value if indoor_humidity_reading else None
        )

        # Thermostat satellite blend detection (occupancy/schedule modes).
        # For median mode, SensorHub handles detection internally during read_indoor_temp.
        # We run detection before reading so occupancy/schedule modes can exclude
        # the thermostat by passing thermostat_temp=None to SensorHub.
        if self._blend_mode != BLEND_MODE_MEDIAN:
            self._thermostat_blend_suspected = self._detect_thermostat_blend()

        # Compute the effective thermostat temperature for sensor fusion.
        # When blend is suspected (occupancy or schedule modes), pass None so
        # SensorHub falls back to indoor entity averaging only (e.g. EP1).
        effective_thermo_temp = (
            None if (
                self._thermostat_blend_suspected
                and self._blend_mode in (BLEND_MODE_OCCUPANCY, BLEND_MODE_SCHEDULE)
            )
            else thermo_state.indoor_temp
        )

        # Read weighted indoor temp (falls back to thermostat if no area manager)
        indoor_temp_reading = self.sensor_hub.read_weighted_indoor_temp(
            effective_thermo_temp
        )

        # For median mode, update the blend flag after SensorHub has run its filter
        if self._blend_mode == BLEND_MODE_MEDIAN:
            self._thermostat_blend_suspected = self.sensor_hub.thermostat_blend_suspected

        effective_indoor_temp = (
            indoor_temp_reading.value if indoor_temp_reading
            else thermo_state.indoor_temp
        )
        self._effective_indoor_temp = effective_indoor_temp

        # Noise scale for EKF: inflate R_meas only when thermostat is the sole
        # remaining sensor and blend is suspected (occupancy/schedule modes only).
        # In median mode the thermostat is simply excluded from the pool, so the
        # remaining sensor data is clean and no noise inflation is needed.
        thermostat_is_only_source = (
            indoor_temp_reading is not None
            and indoor_temp_reading.sensor_count == 1
            and "thermostat" in (indoor_temp_reading.source or "")
        )
        self._last_indoor_noise_scale = (
            4.0 if (
                self._thermostat_blend_suspected
                and self._blend_mode in (BLEND_MODE_OCCUPANCY, BLEND_MODE_SCHEDULE)
                and thermostat_is_only_source
            )
            else 1.0
        )

        # Compute cross-sensor spread for diagnostics (always, regardless of mode)
        self._cross_sensor_spread_f: float = 0.0
        if thermo_state.indoor_temp is not None and self.sensor_hub._indoor_temp_entities:
            entity_only_reading = self.sensor_hub.read_indoor_temp(thermostat_temp=None)
            if entity_only_reading is not None:
                self._cross_sensor_spread_f = abs(
                    thermo_state.indoor_temp - entity_only_reading.value
                )

        # Calculate apparent temperature (humidity-adjusted feels-like temp)
        if (
            effective_indoor_temp is not None
            and self._current_indoor_humidity is not None
        ):
            self._current_apparent_temp = calculate_apparent_temperature(
                effective_indoor_temp, self._current_indoor_humidity
            )
        else:
            self._current_apparent_temp = None

        # ── Sensor health checks (HA issue reporting) ────────────────

        # Outdoor temp staleness
        outdoor_reading = self.sensor_hub.read_outdoor_temp(
            forecast_snapshot=self.strategic.forecast_snapshot
        )
        if outdoor_reading and outdoor_reading.stale:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "outdoor_temp_stale",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="outdoor_temp_stale",
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, "outdoor_temp_stale")

        # Indoor sensor divergence (>3°F spread across multiple sensors)
        if indoor_temp_reading and indoor_temp_reading.sensor_count > 1:
            if indoor_temp_reading.max_spread > 3.0:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    "indoor_sensors_diverging",
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="indoor_sensors_diverging",
                    translation_placeholders={
                        "spread": f"{indoor_temp_reading.max_spread:.1f}",
                    },
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, "indoor_sensors_diverging")

        # Door/window sensor unavailability
        dw_entities = self.sensor_hub._door_window_entities
        if dw_entities:
            unavailable_dw = [
                eid for eid in dw_entities
                if (s := self.hass.states.get(eid)) is None
                or s.state in ("unknown", "unavailable")
            ]
            if unavailable_dw:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    "door_window_sensor_unavailable",
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="door_window_sensor_unavailable",
                    translation_placeholders={
                        "entities": ", ".join(unavailable_dw),
                    },
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, "door_window_sensor_unavailable")

        # ── Watchdog checks ─────────────────────────────────────────

        # Override detection (skip in monitor mode — we never write setpoints)
        if not self._monitor_only and self.watchdog.is_override_active:
            if self.watchdog.check_grace_period():
                # Grace expired — resume
                await self._run_strategic_optimization()
            else:
                self._phase = PHASE_PAUSED
                return self._build_data(thermo_state)
        elif not self._monitor_only and not self._paused:
            override = self.watchdog.check_override(
                self.thermostat.last_written_setpoint,
                thermo_state.effective_setpoint,
            )
            if override:
                self.override_tracker.record_override(
                    expected_setpoint=self.thermostat.last_written_setpoint or 0,
                    actual_setpoint=thermo_state.effective_setpoint or 0,
                )
                self._phase = PHASE_PAUSED
                return self._build_data(thermo_state)

        # Mode change detection (with hysteresis)
        if thermo_state.hvac_mode:
            self.watchdog.check_mode_change(thermo_state.hvac_mode)

        # ── Paused check ────────────────────────────────────────────

        if self._paused:
            self._phase = PHASE_PAUSED
            return self._build_data(thermo_state)

        # ── Strategic check: need re-optimization? ──────────────────

        if self.strategic.should_reoptimize(
            new_forecast,
            occupancy_timeline=self._occupancy_timeline or None,
        ):
            await self._run_strategic_optimization(forecast=new_forecast)
        elif self._outdoor_sensor_diverges_from_forecast():
            _LOGGER.info("Outdoor sensor diverges from forecast — triggering re-optimization")
            await self._run_strategic_optimization(forecast=new_forecast)

        # ── Forecast staleness ──────────────────────────────────────

        if self._is_forecast_stale():
            _LOGGER.warning("Forecast stale — entering safe mode")
            self._phase = PHASE_SAFE_MODE
            self._active = False
            self._fire_event(EVENT_SAFE_MODE_ENTERED, {
                "reason": "forecast_stale",
            })
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "forecast_unavailable",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="forecast_unavailable",
            )
            if self._baseline_ready_for_control and not self._monitor_only:
                comfort = self._active_comfort_range()
                await self.thermostat.async_write_safe_default(*comfort)
            # Don't return — let EKF, baseline, and savings continue below

        # ── Tactical: reality check & setpoint execution ────────────

        schedule = self.strategic.schedule
        current_entry = self._get_current_entry(now)

        if schedule and self._active and current_entry and thermo_state.indoor_temp is not None and self._baseline_ready_for_control:
            # Run tactical evaluation
            tactical_result = self.tactical.evaluate(
                actual_indoor_temp=thermo_state.indoor_temp,
                schedule=schedule,
                current_entry=current_entry,
                now=now,
                apparent_temp=self._current_apparent_temp,
            )

            # Track tactical correction for diagnostic sensor
            self._last_tactical_correction = tactical_result.setpoint_correction

            # Classify phase
            self._phase = self._classify_phase(current_entry, thermo_state.indoor_temp)

            # Apply tactical correction or write scheduled setpoint
            if tactical_result.state == TacticalState.DISTURBED:
                # Don't write setpoints while disturbed (window open, etc.)
                _LOGGER.debug("Tactical: %s", tactical_result.reason)
                # Fire event only on transition into disturbed (not every cycle)
                if self.tactical._disturbed_since and (now - self.tactical._disturbed_since).total_seconds() < DEFAULT_UPDATE_INTERVAL_MINUTES * 60 + 30:
                    self._fire_event(EVENT_DISTURBED, {
                        "error": round(tactical_result.error, 1),
                        "actual_temp": round(tactical_result.actual_temp, 1),
                        "predicted_temp": round(tactical_result.predicted_temp, 1) if tactical_result.predicted_temp else None,
                    })
            elif tactical_result.should_write_setpoint and tactical_result.corrected_setpoint is not None:
                target = tactical_result.corrected_setpoint
                comfort = self._active_comfort_range()
                current_sp = thermo_state.effective_setpoint
                target = self._apply_rate_limit(target, current_sp)
                if not self._monitor_only and (current_sp is None or abs(current_sp - target) > 0.5):
                    if self._check_dwell_time(now):
                        await self.thermostat.async_set_temperature(target, *comfort)
                        self._last_written_setpoint_time = now
            else:
                # Write scheduled setpoint if different from current
                current_sp = thermo_state.effective_setpoint
                if not self._monitor_only and current_sp is not None:
                    target = self._apply_rate_limit(current_entry.target_temp, current_sp)
                    diff = abs(current_sp - target)
                    if diff > 0.5:
                        if self._check_dwell_time(now):
                            comfort = self._active_comfort_range()
                            await self.thermostat.async_set_temperature(target, *comfort)
                            self._last_written_setpoint_time = now

            # Disturbed-state timeout triggered re-optimization request
            if self.tactical.needs_reoptimization:
                _LOGGER.info("Tactical disturbed timeout — triggering re-optimization")
                self.tactical.clear_reoptimization_flag()
                await self._run_strategic_optimization()

            # Feed model tracker with prediction error data
            if tactical_result.predicted_temp is not None:
                self._feed_model_tracker(thermo_state, tactical_result, current_entry, now)
        else:
            # Show "learning" when the model isn't ready for control yet,
            # rather than "idle" which implies the system could be active.
            if not self._baseline_ready_for_control:
                self._phase = PHASE_LEARNING
            else:
                self._phase = PHASE_IDLE

        # ── Departure detection (Stage 2 pre-conditioning) ───────────

        if self._precondition_plan and thermo_state.indoor_temp is not None:
            await self._check_departure_trigger(
                now, thermo_state.indoor_temp, new_forecast
            )

        # ── Kalman filter update ──────────────────────────────────────

        if thermo_state.indoor_temp is not None and self._use_adaptive:
            self._feed_estimator(thermo_state, now)
            # Fire event when confidence threshold is first reached
            if not self._confidence_threshold_reached and self.estimator.confidence >= DEFAULT_MODEL_CONFIDENCE_THRESHOLD:
                self._confidence_threshold_reached = True
                self._fire_event(EVENT_CONFIDENCE_REACHED, {
                    "confidence": round(self.estimator.confidence * 100, 1),
                    "observations": self.estimator._n_obs,
                })

        # ── Shared sensor reads (used by savings, counterfactual, profiler) ──

        hvac_running = self._is_hvac_running(thermo_state)

        # Aux heat state — computed before power draw so mode-aware estimation
        # can use the correct wattage (resistive strips vs compressor)
        aux_heat_active = self._is_aux_heat_running(thermo_state)
        self._cached_aux_heat_active = aux_heat_active

        hvac_action = getattr(thermo_state, "hvac_action", None) if thermo_state else None
        power_watts = self.sensor_hub.read_power_draw(
            hvac_action=hvac_action,
            aux_heat_active=aux_heat_active,
        )
        co2_intensity = self.sensor_hub.read_co2_intensity()
        elec_rate = self.sensor_hub.read_electricity_rate()
        solar_reading = self.sensor_hub.read_solar_production()
        grid_import_reading = self.sensor_hub.read_grid_import()
        outdoor_reading = self.sensor_hub.read_outdoor_temp(
            self.strategic.forecast_snapshot
        )
        outdoor_temp = outdoor_reading.value if outdoor_reading else None

        # Effective outdoor temp for aux learner (wind-chill-adjusted if available)
        _eff_outdoor = outdoor_temp
        if new_forecast and outdoor_temp is not None:
            try:
                _eff_outdoor = new_forecast[0].effective_outdoor_temp or outdoor_temp
            except (AttributeError, TypeError, IndexError):
                pass

        # Aux heat activation learning and HP baseline watts tracking
        try:
            if outdoor_temp is not None and thermo_state.indoor_temp is not None:
                _setpoint_delta = abs(
                    thermo_state.indoor_temp
                    - (thermo_state.effective_setpoint or thermo_state.indoor_temp)
                )
                _hum_reading = self.sensor_hub.read_outdoor_humidity(
                    forecast_snapshot=self.strategic.forecast_snapshot
                )
                _outdoor_humidity = (_hum_reading.value if _hum_reading else None) or 50.0
                self.aux_heat_learner.record_interval(
                    aux_heat_active=aux_heat_active,
                    outdoor_temp_f=outdoor_temp,
                    effective_outdoor_temp_f=_eff_outdoor,
                    outdoor_humidity=_outdoor_humidity,
                    setpoint_delta_f=_setpoint_delta,
                    dt_minutes=DEFAULT_UPDATE_INTERVAL_MINUTES,
                    hvac_running=hvac_running,
                    hvac_mode=thermo_state.hvac_mode or "off",
                    power_watts=power_watts,
                )
        except (ValueError, TypeError, ZeroDivisionError):
            _LOGGER.warning("Aux heat learner update failed", exc_info=True)

        # Compute resistive BTU for EKF (separates HP from strip contribution).
        # When a power sensor is available, subtract the learned HP baseline from
        # the measured circuit draw.  When no sensor is present but the user has
        # declared their aux heat capacity (aux_heat_kw), use that directly so
        # the EKF receives correct heat input rather than absorbing strip heat
        # into the compressor capacity estimate.
        self._cached_aux_resistive_btu = 0.0
        if aux_heat_active:
            if power_watts is not None:
                hp_watts = self.aux_heat_learner.learned_hp_watts
                if power_watts > hp_watts:
                    self._cached_aux_resistive_btu = (power_watts - hp_watts) * 3.412  # BTU/hr
            elif self._aux_heat_kw is not None and self._aux_heat_type == "electric_strip":
                # No power sensor — use declared strip capacity as a fixed prior
                self._cached_aux_resistive_btu = self._aux_heat_kw * 3412.0  # kW → BTU/hr

        # ── Savings tracking ──────────────────────────────────────────

        try:
            actual_cop = None
            if outdoor_temp is not None and self._use_adaptive:
                mode = self.strategic.mode or "off"
                actual_cop = self.counterfactual._cop_at_outdoor_temp(outdoor_temp, mode)

            self.savings_tracker.record_interval(
                now=now,
                hvac_running=hvac_running,
                interval_minutes=DEFAULT_UPDATE_INTERVAL_MINUTES,
                power_watts=power_watts,
                carbon_intensity=co2_intensity,
                electricity_rate=elec_rate,
                mode=self.strategic.mode or "off",
                solar_production_watts=solar_reading.value if solar_reading else None,
                grid_import_watts=grid_import_reading.value if grid_import_reading else None,
                actual_cop=actual_cop,
                aux_heat_active=aux_heat_active,
                hp_baseline_watts=self.aux_heat_learner.learned_hp_watts,
            )
        except (ValueError, TypeError, ZeroDivisionError, KeyError):
            _LOGGER.warning("Savings tracking failed", exc_info=True)

        # ── Baseline capture (during learning phase) ───────────────

        try:
            effective_sp = thermo_state.effective_setpoint
            if not self.baseline_capture.is_ready and effective_sp is not None:
                self.baseline_capture.record_observation(
                    now=dt_util.as_local(now),
                    setpoint=effective_sp,
                    mode=self.strategic.mode or "off",
                )
                if self.baseline_capture.is_ready and self.baseline_capture.template is None:
                    self.baseline_capture.build_template()
                    _LOGGER.info("Baseline schedule captured after %d days",
                                 self.baseline_capture.sample_days)
        except (ValueError, TypeError, KeyError):
            _LOGGER.warning("Baseline capture failed", exc_info=True)

        # ── Counterfactual simulation step ─────────────────────────
        # The counterfactual models a dual-setpoint (heat_cool) thermostat
        # running at the user's configured comfort midpoints, adjusted by
        # current occupancy.  No 7-day baseline capture needed.

        try:
            if outdoor_temp is not None:
                # Construct baseline setpoints from user's comfort config
                effective_mode = self.occupancy.get_effective_mode(
                    self._occupancy_timeline or None
                )
                adj_cool = OccupancyAdapter.adjust_comfort_for_mode(
                    self.comfort_cool, "cool", effective_mode,
                )
                adj_heat = OccupancyAdapter.adjust_comfort_for_mode(
                    self.comfort_heat, "heat", effective_mode,
                )
                # Baseline setpoints: midpoints of the occupancy-adjusted comfort ranges
                baseline_sp_low = (adj_heat[0] + adj_heat[1]) / 2
                baseline_sp_high = (adj_cool[0] + adj_cool[1]) / 2

                cloud_cover = None
                sun_elevation = None
                if new_forecast:
                    cloud_cover = new_forecast[0].cloud_cover
                    sun_elevation = new_forecast[0].sun_elevation

                # Use profiler model for counterfactual physics
                active_model = self._get_active_model()

                self.counterfactual.step(
                    now=now,
                    outdoor_temp=outdoor_temp,
                    setpoint_low=baseline_sp_low,
                    setpoint_high=baseline_sp_high,
                    model=active_model,
                    dt_minutes=DEFAULT_UPDATE_INTERVAL_MINUTES,
                    cloud_cover=cloud_cover,
                    sun_elevation=sun_elevation,
                    carbon_intensity=co2_intensity,
                    electricity_rate=elec_rate,
                    real_indoor_temp=thermo_state.indoor_temp,
                    aux_threshold_f=(
                        self.aux_heat_learner.threshold_f
                        if self.aux_heat_learner.is_learned else None
                    ),
                )
        except (ValueError, TypeError, ZeroDivisionError, KeyError):
            _LOGGER.warning("Counterfactual simulation failed", exc_info=True)

        # ── Performance profiler feed ─────────────────────────────
        # The profiler handles hvac_mode="off" internally (sets _previous_*
        # for interval tracking but doesn't record a delta). We must call it
        # even when off so that the next non-off observation has valid timing.

        # Read attic temp and sun elevation for solar-aware profiler binning.
        # These are also read later for the EKF; the sensor hub caches readings
        # so the second read is free.
        _profiler_attic = self.sensor_hub.read_attic_temp()
        _profiler_sun_elev = self.sensor_hub.read_sun_elevation()

        try:
            if (
                thermo_state.indoor_temp is not None
                and outdoor_temp is not None
            ):
                c_inv = self.estimator.C_inv
                c_air = 1.0 / c_inv if c_inv > 0 else None
                self._last_profiler_status = self.profiler.record_observation(
                    indoor_temp=thermo_state.indoor_temp,
                    outdoor_temp=outdoor_temp,
                    hvac_action=thermo_state.hvac_action,
                    hvac_mode=thermo_state.hvac_mode,
                    aux_heat_active=self._is_aux_heat_running(thermo_state),
                    solar_irradiance=solar_reading.value if solar_reading else None,
                    now=now,
                    appliance_btu=self.appliance_manager.total_thermal_impact_btu(),
                    c_air=c_air,
                    attic_temp=_profiler_attic.value if _profiler_attic else None,
                    sun_elevation=_profiler_sun_elev,
                )
            else:
                self._last_profiler_status = "skipped_no_temps"
                _LOGGER.warning(
                    "Profiler skipped — missing temps: indoor=%s, outdoor=%s",
                    thermo_state.indoor_temp, outdoor_temp,
                )
        except Exception:  # noqa: BLE001
            self._last_profiler_status = "error"
            _LOGGER.error("Profiler observation failed", exc_info=True)

        # ── Seed EKF from profiler (one-time) ─────────────────────
        try:
            self._try_seed_ekf_from_profiler()
        except (ValueError, TypeError, KeyError, AttributeError):
            _LOGGER.warning("Profiler EKF seeding failed", exc_info=True)

        # ── Update accuracy tier ───────────────────────────────────

        try:
            self._update_accuracy_tier()
        except (ValueError, TypeError, KeyError, AttributeError):
            _LOGGER.warning("Accuracy tier update failed", exc_info=True)

        # ── Model progress check (repair issue) ──────────────────

        try:
            self._check_model_progress()
        except (ValueError, TypeError, KeyError, AttributeError):
            _LOGGER.warning("Model progress check failed", exc_info=True)

        # ── Periodic learning persistence ───────────────────────────

        if self._should_persist_learning(now):
            self.model_tracker.update_corrections()
            try:
                await self._persist_state()
                self._last_learning_persist = now
            except (OSError, ValueError, TypeError):
                _LOGGER.warning("Failed to persist learning state, will retry next cycle", exc_info=True)

        return self._build_data(thermo_state)

    # ── Strategic optimization ──────────────────────────────────────

    async def _safe_reoptimize(self) -> None:
        """Run strategic optimization with error logging for fire-and-forget tasks."""
        try:
            await self._run_strategic_optimization()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.error("Background re-optimization failed", exc_info=True)

    async def _run_strategic_optimization(
        self, forecast: list[ForecastPoint] | None = None,
    ) -> None:
        """Run the strategic planner."""
        _LOGGER.info("Running strategic re-optimization")

        if forecast is None:
            forecast, source = await async_get_forecast_multi(
                self.hass, self._weather_entity_ids
            )
            if forecast:
                now = datetime.now(timezone.utc)
                self._last_good_forecast = forecast
                self._last_forecast_time = now
                self._last_forecast_source = source
        if not forecast:
            # Try cached forecast as last resort
            if self._last_good_forecast and self._last_forecast_time:
                cache_age_hours = (
                    datetime.now(timezone.utc) - self._last_forecast_time
                ).total_seconds() / 3600
                if cache_age_hours < DEFAULT_FORECAST_CACHE_MAX_AGE_HOURS:
                    _LOGGER.warning(
                        "All weather entities failed — using cached forecast (%.1fh old)",
                        cache_age_hours,
                    )
                    forecast = self._last_good_forecast
            if not forecast:
                _LOGGER.warning("No forecast — cannot optimize")
                return

        # Correct current hour with ground-truth sensor data (if available)
        forecast = self.sensor_hub.correct_current_forecast(forecast)

        # Enrich forecast with CO2 and electricity rate data
        await enrich_forecast_with_grid_data(
            self.hass,
            forecast,
            co2_entity_id=self._co2_entity_id,
            rate_entity_id=self._rate_entity_id,
            flat_rate=self._flat_rate,
            tou_schedule=self._tou_schedule,
        )

        thermo_state = self.thermostat.read_state()
        if not thermo_state.available or thermo_state.indoor_temp is None:
            _LOGGER.warning("Thermostat unavailable — cannot optimize")
            return

        # Fetch calendar occupancy timeline (if configured)
        occupancy_timeline: list[OccupancyForecastPoint] | None = None
        if self.calendar_occupancy is not None:
            try:
                occupancy_timeline = (
                    await self.calendar_occupancy.async_get_occupancy_timeline()
                )
                self._occupancy_timeline = occupancy_timeline or []
            except Exception:
                _LOGGER.warning(
                    "Calendar occupancy fetch failed — using reactive only",
                    exc_info=True,
                )

        # Adjust comfort for current occupancy (calendar-aware if available)
        effective_mode = self.occupancy.get_effective_mode(occupancy_timeline)
        comfort_cool = OccupancyAdapter.adjust_comfort_for_mode(
            self.comfort_cool, "cool", effective_mode
        )
        comfort_heat = OccupancyAdapter.adjust_comfort_for_mode(
            self.comfort_heat, "heat", effective_mode
        )

        # Apply sleep comfort bounds when HOME and within sleep window
        if (
            self.sleep_config.get("enabled")
            and effective_mode == OccupancyMode.HOME
            and StrategicPlanner._is_in_sleep_window(
                datetime.now(timezone.utc), self.sleep_config
            )
        ):
            comfort_cool = self.sleep_config.get("comfort_cool", comfort_cool)
            comfort_heat = self.sleep_config.get("comfort_heat", comfort_heat)

        # If reactive overrides calendar (person came home early), fire event
        if (
            occupancy_timeline
            and effective_mode == OccupancyMode.HOME
        ):
            cal_mode = StrategicPlanner._lookup_occupancy_at(
                datetime.now(timezone.utc), occupancy_timeline
            )
            if cal_mode == OccupancyMode.AWAY:
                _LOGGER.info("Reactive occupancy overrides calendar (person came home early)")
                self._fire_event(EVENT_CALENDAR_OVERRIDE, {
                    "reason": "reactive_home_override",
                    "calendar_mode": "away",
                    "effective_mode": "home",
                })

        # Profile control: update thermostat profile on occupancy transitions
        if self._profile_adapter is not None and not self._monitor_only:
            await self._update_thermostat_profile(effective_mode, thermo_state)

        # Apply preemptive override learning adjustments
        now_hour = datetime.now(timezone.utc).hour
        override_adj = self.override_tracker.get_comfort_adjustment(now_hour)
        if override_adj != 0.0:
            _LOGGER.debug(
                "Override learning: adjusting comfort by %+.1f°F at hour %d",
                override_adj, now_hour,
            )
            # Shift both comfort ranges in the override direction
            comfort_cool = (comfort_cool[0] + override_adj, comfort_cool[1] + override_adj)
            comfort_heat = (comfort_heat[0] + override_adj, comfort_heat[1] + override_adj)

        # Apply demand response widening
        if self._demand_response_active:
            delta = self._demand_response_delta
            comfort_cool = (comfort_cool[0] - delta, comfort_cool[1] + delta)
            comfort_heat = (comfort_heat[0] - delta, comfort_heat[1] + delta)

        # Apply aggressiveness: conservative uses inner 60% of band,
        # limiting how far the optimizer can push setpoints from midpoint.
        if self._aggressiveness == AGGRESSIVENESS_CONSERVATIVE:
            for name, rng in [("cool", comfort_cool), ("heat", comfort_heat)]:
                band = rng[1] - rng[0]
                margin = band * 0.2  # shrink 20% each side = inner 60%
                narrowed = (rng[0] + margin, rng[1] - margin)
                if name == "cool":
                    comfort_cool = narrowed
                else:
                    comfort_heat = narrowed

        # Select active model
        active_model = self._get_active_model()

        # Update balance point.  Prefer the profiler's measured value (derived
        # from a trendline fit to real observations) over the adaptive model's
        # EKF formula, which amplifies small T_mass-T_air gaps by R_int_inv/R_inv
        # and regularly produces absurd values (1000 F+) when poorly converged.
        profiler_data = self.profiler.to_profile_data()
        profiler_bp = profiler_data.get("balance_point", {}).get("resist")
        if profiler_bp is not None:
            self.strategic.resist_balance_point = profiler_bp
        elif active_model is self.adaptive_model:
            param_conf = self.estimator.parameter_confidence
            if param_conf.get("internal_coupling", 0) >= 0.1:
                bp = self.adaptive_model.resist_balance_point
                if bp is not None:
                    self.strategic.resist_balance_point = bp

        # Temporarily swap model for optimization if using adaptive model
        original_model = self.optimizer.model
        original_sim_model = self.simulator.model
        if active_model is not self.model:
            self.optimizer.model = active_model
            self.simulator.model = active_model

        # Gather current environmental context for the optimizer
        people_count = self.occupancy.get_people_home_count()

        # Run optimizer in executor (synchronous engine)
        try:
            _aux_threshold = (
                self.aux_heat_learner.threshold_f
                if self.aux_heat_learner.is_learned else None
            )
            _aux_active = getattr(self, "_cached_aux_heat_active", False)
            schedule = await asyncio.wait_for(
                self.hass.async_add_executor_job(
                    self.strategic.optimize,
                    thermo_state.indoor_temp,
                    forecast,
                    comfort_cool,
                    comfort_heat,
                    self._current_indoor_humidity,
                    True,  # humidity_correction
                    occupancy_timeline,
                    people_count,
                    self._current_indoor_humidity,
                    self.appliance_manager.total_thermal_impact_btu(),
                    _aux_threshold,
                    _aux_active,
                ),
                timeout=OPTIMIZER_TIMEOUT_S,
            )
        finally:
            # Restore original model references
            self.optimizer.model = original_model
            self.simulator.model = original_sim_model

        # During learning mode, replace the simulation with a passive-only
        # version so tactical predictions reflect reality (no HVAC control).
        # Also clear the stale runtime metadata to avoid showing contradictory
        # numbers (e.g., 2880 min runtime but hvac:false in the forecast).
        if schedule and self._is_learning_active() and schedule.simulation:
            passive_sim = self.simulator.simulate(
                thermo_state.indoor_temp,
                forecast,
                schedule.entries,
                passive_only=True,
            )
            schedule.simulation = passive_sim
            schedule.baseline_runtime_minutes = 0
            schedule.optimized_runtime_minutes = 0
            schedule.savings_pct = 0
            _LOGGER.debug(
                "Learning mode: replaced schedule simulation with passive-only forecast"
            )

        if schedule:
            self._active = True
            _LOGGER.info(
                "Optimization [%s]: baseline=%.1f min, optimized=%.1f min, savings=%.1f%%",
                self.strategic.mode,
                schedule.baseline_runtime_minutes,
                schedule.optimized_runtime_minutes,
                schedule.savings_pct,
            )
            # Update savings tracker with new baseline ratio — only when
            # the optimizer is actually controlling the thermostat, otherwise
            # the ratio produces phantom savings during baseline capture.
            if self._baseline_ready_for_control:
                self.savings_tracker.set_baseline_ratio(
                    schedule.baseline_runtime_minutes,
                    schedule.optimized_runtime_minutes,
                )
            self._fire_event(EVENT_OPTIMIZATION_COMPLETE, {
                "mode": self.strategic.mode,
                "savings_pct": round(schedule.savings_pct, 1),
                "schedule_entries": len(schedule.entries),
                "baseline_runtime": round(schedule.baseline_runtime_minutes, 1),
                "optimized_runtime": round(schedule.optimized_runtime_minutes, 1),
            })

            # Plan pre-conditioning for next AWAY→HOME transition
            await self._plan_preconditioning(
                forecast, occupancy_timeline, thermo_state.indoor_temp
            )
        else:
            self._active = False
            self._phase = PHASE_IDLE

    # ── Calendar / pre-conditioning helpers ──────────────────────────

    async def _plan_preconditioning(
        self,
        forecast: list[ForecastPoint],
        occupancy_timeline: list[OccupancyForecastPoint] | None,
        indoor_temp: float,
    ) -> None:
        """Plan pre-conditioning for the next AWAY->HOME transition."""
        # Skip pre-conditioning at conservative tier -- model predictions
        # are not reliable enough for cost-optimal timing decisions.
        if self._is_conservative_mode:
            self._precondition_plan = None
            return

        if not occupancy_timeline or not self.calendar_occupancy:
            self._precondition_plan = None
            return

        mode = self.strategic.mode
        if not mode or mode == "off":
            self._precondition_plan = None
            return

        arrival_time = self.calendar_occupancy.get_next_transition(
            occupancy_timeline, "away", "home"
        )
        if arrival_time is None:
            self._precondition_plan = None
            return

        # Determine comfort ranges
        home_comfort = self.comfort_cool if mode == "cool" else self.comfort_heat
        away_comfort = OccupancyAdapter.adjust_comfort_for_mode(
            home_comfort, mode, OccupancyMode.AWAY
        )

        plan = self.precondition_planner.plan(
            arrival_time=arrival_time,
            current_indoor_temp=indoor_temp,
            forecast=forecast,
            mode=mode,
            home_comfort=home_comfort,
            away_comfort=away_comfort,
            power_watts=self._power_default_watts,
            buffer_minutes=self._precondition_buffer_minutes,
        )

        self._precondition_plan = plan
        if plan:
            _LOGGER.info(
                "Pre-conditioning scheduled: start %s for %s arrival (%.0f min, %.1f°F gap)",
                plan.start_time.strftime("%H:%M"),
                plan.arrival_time.strftime("%H:%M"),
                plan.estimated_runtime_minutes,
                plan.temperature_gap,
            )

    @staticmethod
    def _load_departure_profiles(opts: dict) -> list[dict[str, str]]:
        """Load departure profiles from options, with legacy migration."""
        raw = opts.get(CONF_DEPARTURE_PROFILES)
        if raw:
            try:
                profiles = json.loads(raw)
                if isinstance(profiles, list):
                    return profiles
            except (ValueError, TypeError):
                pass

        # Legacy migration: single zone/sensor → one profile for first person
        zone = opts.get(CONF_DEPARTURE_ZONE)
        travel = opts.get(CONF_TRAVEL_TIME_SENSOR)
        if not zone and not travel:
            # Try plural legacy keys
            zones = opts.get(CONF_DEPARTURE_ZONES, [])
            travels = opts.get(CONF_TRAVEL_TIME_SENSORS, [])
            zone = zones[0] if zones else None
            travel = travels[0] if travels else None

        if zone or travel:
            # Can't determine person here (no occupancy entities yet),
            # but store what we have — coordinator will match at runtime
            profile: dict[str, str] = {}
            if zone:
                profile["zone"] = zone
            if travel:
                profile["travel_sensor"] = travel
            return [profile]

        return []

    @staticmethod
    def _parse_squelch_pairs(raw: str) -> list[dict]:
        """Parse humidity squelch pairs from JSON string."""
        if not raw:
            return []
        try:
            pairs = json.loads(raw)
            if isinstance(pairs, list):
                return [
                    p for p in pairs
                    if isinstance(p, dict)
                    and p.get("temp_entity")
                    and p.get("humidity_entity")
                ]
        except (ValueError, TypeError):
            pass
        return []

    async def _check_departure_trigger(
        self,
        now: datetime,
        indoor_temp: float,
        forecast: list[ForecastPoint] | None,
    ) -> None:
        """Stage 2: refine pre-conditioning with per-person zone departure + travel time."""
        plan = self._precondition_plan
        if plan is None or not self._departure_profiles:
            return

        # Check if we're within the departure trigger window
        window_start = plan.arrival_time - timedelta(minutes=self._departure_trigger_window)
        if now < window_start:
            return

        # Check each person's departure profile independently
        for profile in self._departure_profiles:
            person = profile.get("person", "")
            zone = profile.get("zone")
            travel_sensor = profile.get("travel_sensor")

            if not zone or not travel_sensor:
                continue

            # Skip if this person's departure was already detected
            if self._departure_detected.get(person, False):
                continue

            departed = self._has_person_left_zone(person, zone)
            if not departed:
                continue

            self._departure_detected[person] = True
            travel_minutes = self._read_travel_time(travel_sensor)
            if travel_minutes is None:
                continue

            refined_arrival = now + timedelta(minutes=travel_minutes)
            _LOGGER.info(
                "Departure detected for %s — refining arrival to %s (%.0f min travel)",
                person or "unknown",
                refined_arrival.strftime("%H:%M"),
                travel_minutes,
            )

            # Re-plan with the soonest real arrival time
            # Only re-plan if this arrival is sooner than current plan
            if refined_arrival < plan.arrival_time:
                mode = self.strategic.mode
                if mode and mode != "off" and forecast:
                    home_comfort = self.comfort_cool if mode == "cool" else self.comfort_heat
                    away_comfort = OccupancyAdapter.adjust_comfort_for_mode(
                        home_comfort, mode, OccupancyMode.AWAY
                    )
                    new_plan = self.precondition_planner.plan(
                        arrival_time=refined_arrival,
                        current_indoor_temp=indoor_temp,
                        forecast=forecast,
                        mode=mode,
                        home_comfort=home_comfort,
                        away_comfort=away_comfort,
                        power_watts=self._power_default_watts,
                        buffer_minutes=self._precondition_buffer_minutes,
                        arrival_source=f"travel_sensor:{person}",
                    )
                    if new_plan:
                        self._precondition_plan = new_plan
                        plan = new_plan  # update local ref for start check

        # Check if it's time to start pre-conditioning
        if plan.should_start_now or (plan.start_time <= now):
            if self._phase != PHASE_PRECONDITIONING:
                self._phase = PHASE_PRECONDITIONING
                self._fire_event(EVENT_PRECONDITIONING_START, {
                    "arrival_time": plan.arrival_time.isoformat(),
                    "estimated_runtime_minutes": plan.estimated_runtime_minutes,
                    "temperature_gap": plan.temperature_gap,
                    "arrival_source": plan.arrival_source,
                })

    def _has_person_left_zone(self, person_eid: str, zone_eid: str) -> bool:
        """Check if a specific person has left their configured departure zone."""
        if not person_eid:
            # Legacy profile without person — check all person entities
            for eid in self.occupancy.entity_ids:
                if eid.startswith("person."):
                    state = self.hass.states.get(eid)
                    if state is not None and state.state == "not_home":
                        return True
            return False

        state = self.hass.states.get(person_eid)
        if state is None:
            return False

        zone_name = zone_eid.replace("zone.", "")
        # Person was at this zone and has now left (state is "not_home" or another zone)
        return state.state != zone_name

    def _read_travel_time(self, sensor_id: str) -> float | None:
        """Read a single travel time sensor value in minutes."""
        state = self.hass.states.get(sensor_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    @property
    def precondition_plan(self) -> PreconditionPlan | None:
        """Current pre-conditioning plan (for sensor exposure)."""
        return self._precondition_plan

    @property
    def occupancy_timeline(self) -> list[OccupancyForecastPoint]:
        """Current occupancy timeline (for sensor exposure)."""
        return self._occupancy_timeline

    # ── Service call handlers ───────────────────────────────────────

    async def async_reset_model(self) -> None:
        """Reset the learned thermal model and profiler to a fresh state (service call).

        Clears the EKF state, performance profiler, model tracker, solar adjuster,
        baseline capture, and counterfactual simulator. Re-runs history bootstrap
        afterward so the fresh model starts learning immediately.
        """
        try:
            _LOGGER.warning(
                "Resetting learned model — clearing %d EKF observations, "
                "profiler (%.0f%% confidence), and all learning state",
                self.estimator._n_obs,
                self.profiler.confidence() * 100,
            )
        except Exception:
            _LOGGER.warning("Resetting learned model — clearing all learning state")

        # Read current indoor temp for cold start
        try:
            thermo_state = self.thermostat.read_state()
            init_indoor = (
                thermo_state.indoor_temp
                if thermo_state and thermo_state.available and thermo_state.indoor_temp
                else 72.0
            )
        except Exception:
            init_indoor = 72.0

        # Reset static performance model to conservative defaults
        self.model = PerformanceModel.from_defaults()

        # Reset EKF thermal estimator
        self.estimator = ThermalEstimator.cold_start(
            indoor_temp=init_indoor,
            tonnage=self._hvac_tonnage,
            sqft=self._home_sqft,
        )
        self.estimator._coeff_store = self.coeff_store
        self.adaptive_model = AdaptivePerformanceModel(self.estimator)
        try:
            self.adaptive_model.cool_differential = self.model.cool_differential
            self.adaptive_model.heat_differential = self.model.heat_differential
        except AttributeError:
            pass

        # Update downstream model references
        self.simulator.model = self.model
        self.optimizer.model = self.model
        self.precondition_planner = PreconditionPlanner(self.model)
        self.strategic.resist_balance_point = self.model.resist_balance_point or 50.0

        # Reset coefficient calibrator (but keep the learned store)
        self.coeff_calibrator = CoefficientCalibrator(self.coeff_store)

        # Reset profiler, model tracker, solar adjuster
        self.profiler = PerformanceProfiler()
        self.model_tracker = ModelTracker()
        self.solar_adjuster = SolarAdjuster(latitude=self._get_latitude())

        # Reset baseline capture and counterfactual
        self.baseline_capture = BaselineCapture()
        self.counterfactual = CounterfactualSimulator(
            deadband=self._thermostat_deadband
        )
        self._set_counterfactual_capacity()

        # Reset learning flags
        self._confidence_threshold_reached = False
        self._last_model_alert = False
        self._last_accuracy_tier = TIER_LEARNING
        self._baseline_complete_fired = False
        self._last_profiler_status = "pending"

        # Re-run history bootstrap for immediate learning
        self._history_bootstrap_completed = False
        self._history_bootstrap_result = None
        self._bootstrap_retry_count = 0
        if self._bootstrap_retry_unsub is not None:
            self._bootstrap_retry_unsub()
            self._bootstrap_retry_unsub = None

        # Persist the clean state
        await self._persist_state()

        # Kick off bootstrap + reoptimize (best-effort)
        try:
            await self._try_history_bootstrap()
        except Exception:  # noqa: BLE001
            _LOGGER.error("History bootstrap after reset failed", exc_info=True)
        await self.async_request_refresh()

        _LOGGER.info("Model reset complete — learning from scratch with fresh EKF")

    def _set_counterfactual_capacity(self) -> None:
        """Set rated HVAC capacity on the counterfactual from tonnage config."""
        if self._hvac_tonnage:
            self.counterfactual.set_rated_capacity(
                q_cool=self._hvac_tonnage * 12000.0,
                q_heat=self._hvac_tonnage * 12000.0 * 1.1,
            )

    async def async_force_reoptimize(self) -> None:
        """Force immediate re-optimization (service call)."""
        self.strategic.should_reoptimize(force=True)
        await self._run_strategic_optimization()

    def pause(self) -> None:
        """Pause optimization (service call)."""
        if self._monitor_only:
            _LOGGER.warning("Pause ignored — instance is in monitor-only mode")
            return
        self._paused = True
        self._phase = PHASE_PAUSED
        _LOGGER.info("Optimization paused by service call")

    async def async_resume(self) -> None:
        """Resume optimization (service call)."""
        if self._monitor_only:
            _LOGGER.warning("Resume ignored — instance is in monitor-only mode")
            return
        self._paused = False
        self.watchdog.clear_override()
        _LOGGER.info("Optimization resumed by service call")
        await self._run_strategic_optimization()

    def set_occupancy(self, mode: OccupancyMode | None) -> None:
        """Set occupancy mode (service call)."""
        self.occupancy.force_mode(mode)

    # ── Thermostat profile control ──────────────────────────────────

    async def _update_thermostat_profile(
        self,
        effective_mode: OccupancyMode,
        thermo_state: Any,
    ) -> None:
        """Update thermostat comfort profile based on occupancy + time-of-day."""
        desired = self._determine_desired_profile(effective_mode)
        if desired == self._last_profile_mode:
            self._away_delay_pending_since = None
            return

        # HVAC-active delay for transitions to "away"
        if desired == "away" and self._should_delay_away_transition(thermo_state):
            return

        old_profile = self._last_profile_mode
        success = await self._profile_adapter.async_set_profile(desired)  # type: ignore[union-attr]
        if success:
            self._last_profile_mode = desired
            self._away_delay_pending_since = None
            self._fire_event(EVENT_PROFILE_CHANGED, {
                "old_profile": old_profile,
                "new_profile": desired,
                "effective_mode": effective_mode.value,
            })

    def _determine_desired_profile(self, effective_mode: OccupancyMode) -> str:
        """Map occupancy mode + time-of-day to thermostat profile name."""
        if effective_mode in (OccupancyMode.AWAY, OccupancyMode.VACATION):
            return "away"

        # HOME: check sleep window
        if (
            self.sleep_config.get("enabled")
            and StrategicPlanner._is_in_sleep_window(
                datetime.now(timezone.utc), self.sleep_config
            )
        ):
            return "sleep"

        # Arrival cutoff: if transitioning from away and it's late, use sleep
        if self._last_profile_mode == "away":
            try:
                parts = self._arrival_sleep_cutoff.split(":")
                cutoff = dt_time(int(parts[0]), int(parts[1]))
            except (ValueError, IndexError):
                cutoff = dt_time(21, 30)
            if dt_util.now().time() >= cutoff:
                return "sleep"

        return "home"

    def _should_delay_away_transition(self, thermo_state: Any) -> bool:
        """Check if away transition should be delayed due to active HVAC."""
        if not self._sleep_away_override:
            return False
        if self._sleep_away_delay_minutes <= 0:
            return False

        hvac_action = getattr(thermo_state, "hvac_action", None)
        if hvac_action not in ("heating", "cooling"):
            return False

        now = datetime.now(timezone.utc)
        if self._away_delay_pending_since is None:
            self._away_delay_pending_since = now
            _LOGGER.info(
                "Delaying away profile transition -- HVAC is %s "
                "(will wait up to %d min)",
                hvac_action,
                self._sleep_away_delay_minutes,
            )
            return True

        elapsed = (now - self._away_delay_pending_since).total_seconds() / 60.0
        if elapsed < self._sleep_away_delay_minutes:
            return True

        _LOGGER.info(
            "Away transition delay expired (%.1f min) -- proceeding",
            elapsed,
        )
        return False

    async def async_demand_response(self, mode: str, duration_minutes: int) -> None:
        """Activate or deactivate demand response mode.

        When 'reduce' is active, comfort bounds are widened by
        DEFAULT_DEMAND_RESPONSE_DELTA_F to reduce HVAC load.
        Auto-restores after duration_minutes.
        """
        if self._monitor_only:
            _LOGGER.warning("Demand response ignored — instance is in monitor-only mode")
            return
        if mode == "restore":
            self._demand_response_active = False
            self._demand_response_end = None
            _LOGGER.info("Demand response deactivated")
            self._fire_event(f"{DOMAIN}_demand_response", {"mode": "restore"})
            await self._run_strategic_optimization()
            return

        from .const import DEFAULT_DEMAND_RESPONSE_DELTA_F
        self._demand_response_active = True
        self._demand_response_delta = DEFAULT_DEMAND_RESPONSE_DELTA_F
        self._demand_response_end = (
            datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
        )
        _LOGGER.info(
            "Demand response activated: widening comfort by ±%.1f°F for %d min",
            DEFAULT_DEMAND_RESPONSE_DELTA_F, duration_minutes,
        )
        self._fire_event(f"{DOMAIN}_demand_response", {
            "mode": "reduce",
            "duration_minutes": duration_minutes,
            "delta_f": DEFAULT_DEMAND_RESPONSE_DELTA_F,
        })
        await self._run_strategic_optimization()

    async def async_set_constraint(
        self,
        constraint_type: str,
        value: float,
        duration_minutes: int = 60,
        source: str = "unknown",
    ) -> None:
        """Apply a temporary constraint from an external integration.

        Supported types: max_temp, min_temp, max_power, pause_until.
        Auto-expires after duration_minutes.
        """
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=duration_minutes)

        if constraint_type == "pause_until":
            self._paused = True
            self._phase = PHASE_PAUSED
            # Schedule auto-resume
            self._active_constraints.append({
                "type": "pause_until",
                "value": 0,
                "expires": expires,
                "source": source,
            })
            _LOGGER.info(
                "Constraint from %s: paused for %d min",
                source, duration_minutes,
            )
        else:
            self._active_constraints.append({
                "type": constraint_type,
                "value": value,
                "expires": expires,
                "source": source,
            })
            _LOGGER.info(
                "Constraint from %s: %s=%.1f for %d min",
                source, constraint_type, value, duration_minutes,
            )
            # Re-optimize with new constraints in effect
            await self._run_strategic_optimization()

    def _expire_constraints(self) -> None:
        """Remove expired constraints."""
        now = datetime.now(timezone.utc)
        was_paused_by_constraint = any(
            c["type"] == "pause_until" for c in self._active_constraints
        )
        self._active_constraints = [
            c for c in self._active_constraints if c["expires"] > now
        ]
        # Auto-resume if pause constraint expired
        if was_paused_by_constraint and not any(
            c["type"] == "pause_until" for c in self._active_constraints
        ):
            if not self._paused:  # don't override user-initiated pause
                return
            self._paused = False
            _LOGGER.info("Pause constraint expired -- resuming optimization")
            self._schedule_background_task(
                self._safe_reoptimize(), "hpo.reoptimize_pause_expired",
            )

    def export_model(self) -> dict:
        """Export learned Kalman filter parameters in human-readable format."""
        return {
            "confidence": round(self.estimator.confidence * 100, 1),
            "observations": self.estimator._n_obs,
            "parameters": {
                "R_value": self.estimator.R_value,
                "thermal_mass": self.estimator.thermal_mass,
                "cooling_capacity_btu_hr": float(self.estimator.x[6]),
                "heating_capacity_btu_hr": float(self.estimator.x[7]),
                "T_mass": self.estimator.T_mass,
            },
            "state": self.estimator.to_dict(),
        }

    def import_model(self, model_data: dict) -> None:
        """Import Kalman filter state from exported data."""
        state_data = model_data.get("state")
        if not state_data:
            _LOGGER.error("Import failed: no 'state' key in model data")
            return
        try:
            self.estimator = ThermalEstimator.from_dict(state_data)
            self.estimator._coeff_store = self.coeff_store
            self.adaptive_model = AdaptivePerformanceModel(self.estimator)
            self.adaptive_model.cool_differential = self.model.cool_differential
            self.adaptive_model.heat_differential = self.model.heat_differential
            _LOGGER.info(
                "Imported model: %d observations, confidence=%.0f%%",
                self.estimator._n_obs,
                self.estimator.confidence * 100,
            )
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.error("Failed to import model data (%s)", type(err).__name__, exc_info=True)

    # ── Watchdog callbacks ──────────────────────────────────────────

    def _on_override_detected(self, event) -> None:
        _LOGGER.info("Watchdog: override detected, pausing optimization")
        self._phase = PHASE_PAUSED
        self._fire_event(EVENT_OVERRIDE_DETECTED, {
            "expected_setpoint": self.thermostat.last_written_setpoint,
            "actual_setpoint": getattr(event, "actual_setpoint", None),
        })

    def _on_override_cleared(self) -> None:
        _LOGGER.info("Watchdog: override cleared, will re-optimize on next cycle")

    def _on_mode_change(self, new_mode: str) -> None:
        old_mode = self.strategic.mode
        _LOGGER.info("Watchdog: mode changed to %s, triggering re-optimization", new_mode)
        self._fire_event(EVENT_MODE_CHANGED, {
            "old_mode": old_mode,
            "new_mode": new_mode,
        })
        self._schedule_background_task(
            self._safe_reoptimize(), "hpo.reoptimize_mode_change",
        )

    @callback
    def _handle_thermostat_state_change(self, event) -> None:
        """React to thermostat state changes."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None:
            return

        # HVAC mode change (heat->cool, etc.)
        if old_state and old_state.state != new_state.state:
            if self.watchdog.check_mode_change(new_state.state):
                self._schedule_background_task(
                    self._safe_reoptimize(), "hpo.reoptimize_hvac_mode",
                )

    @callback
    def _handle_occupancy_state_change(self, event) -> None:
        """React to person entity state changes for immediate profile updates."""
        if self._profile_adapter and not self._monitor_only:
            self._schedule_background_task(
                self.async_request_refresh(), "hpo.refresh_occupancy",
            )

    def _schedule_background_task(self, coro, name: str) -> None:
        """Schedule a background coroutine tied to the config entry lifecycle.

        When the entry is set (production), uses ConfigEntry.async_create_background_task
        so HA auto-cancels the task on entry unload. Falls back to hass.async_create_task
        when no entry is attached (unit-test construction).
        """
        if self.entry is not None:
            self.entry.async_create_background_task(self.hass, coro, name)
        else:
            self.hass.async_create_task(coro)

    # ── Model tracker feeding ───────────────────────────────────────

    def _feed_model_tracker(self, thermo_state, tactical_result, entry, now) -> None:
        """Feed prediction vs reality data to the model tracker."""
        if thermo_state.indoor_temp is None or tactical_result.predicted_temp is None:
            return

        # Determine what mode the model predicted for this interval
        mode = entry.mode if entry else "resist"

        # Approximate outdoor temp from forecast
        outdoor_temp = None
        snapshot = self.strategic.forecast_snapshot
        if snapshot:
            closest = min(snapshot, key=lambda pt: abs((pt.time - now).total_seconds()))
            if abs((closest.time - now).total_seconds()) < 600:
                outdoor_temp = closest.outdoor_temp

        if outdoor_temp is None:
            return

        # Predicted vs actual delta for this 5-min interval
        # (simplified: using the instantaneous error as a proxy)
        predicted_delta = tactical_result.predicted_temp - (thermo_state.indoor_temp - tactical_result.error)
        actual_delta = tactical_result.error  # This is actual - predicted

        self.model_tracker.record_observation(
            mode=mode,
            outdoor_temp=outdoor_temp,
            predicted_delta=predicted_delta,
            actual_delta=predicted_delta + actual_delta,
            timestamp=now,
        )

    # ── Thermostat blend detection ───────────────────────────────────

    def _detect_thermostat_blend(self) -> bool:
        """Return True when the thermostat is likely blending toward a satellite sensor.

        Three detection strategies based on configured blend_mitigation_mode:

        - occupancy: thermostat area shows no occupancy but people are home elsewhere.
          Requires CONF_THERMOSTAT_OCCUPANCY_ENTITY (e.g. binary_sensor.my_ecobee_occupancy).

        - schedule: current local time is within the configured suppression window
          (e.g. 22:00–08:00). Works for regular-schedule households without occupancy sensors.

        - median: detection is handled by SensorHub during read_indoor_temp(); this method
          just returns the cached SensorHub state.

        - none (default): always returns False (feature disabled).
        """
        if self._blend_mode == BLEND_MODE_OCCUPANCY:
            if not self._thermostat_occ_entity:
                return False
            state = self.hass.states.get(self._thermostat_occ_entity)
            if state is None or state.state in ("unavailable", "unknown"):
                return False
            if state.state == "on":
                return False  # thermostat area is occupied — no blending
            # Thermostat area unoccupied; if people are home elsewhere, blend is active
            return self.occupancy.get_people_home_count() > 0

        if self._blend_mode == BLEND_MODE_SCHEDULE:
            from homeassistant.util import dt as dt_util
            now_time = dt_util.now().time()
            start = self._blend_schedule_start
            end = self._blend_schedule_end
            if start > end:  # overnight window e.g. 22:00–08:00
                return now_time >= start or now_time < end
            return start <= now_time < end

        if self._blend_mode == BLEND_MODE_MEDIAN:
            # SensorHub sets thermostat_blend_suspected during read_indoor_temp()
            return self.sensor_hub.thermostat_blend_suspected

        return False  # BLEND_MODE_NONE

    # ── Kalman filter feeding ────────────────────────────────────────

    def _feed_estimator(self, thermo_state, now: datetime) -> None:
        """Feed the EKF with current sensor readings via SensorHub."""
        if thermo_state.indoor_temp is None:
            return

        # Outdoor temp: standalone sensor → forecast → last known
        snapshot = self.strategic.forecast_snapshot
        outdoor_reading = self.sensor_hub.read_outdoor_temp(snapshot)
        if outdoor_reading is None:
            return
        outdoor_temp = outdoor_reading.value

        # Determine HVAC state
        hvac_running = self._is_hvac_running(thermo_state)
        # Use actual thermostat action when HVAC is running, so the EKF
        # attributes heat flow in the correct direction (positive=heating,
        # negative=cooling).  Fall back to strategic mode for idle periods.
        if hvac_running and thermo_state.hvac_action in ("heating", "cooling"):
            hvac_mode = "heat" if thermo_state.hvac_action == "heating" else "cool"
        else:
            hvac_mode = self.strategic.mode or "off"

        # Cloud cover from forecast (now a proper field on ForecastPoint)
        # Use 7200s (2hr) window — OWM updates hourly; 600s was too aggressive
        # and caused cloud_cover to be None ~87% of the time.
        cloud_cover = None
        if snapshot:
            closest = min(snapshot, key=lambda pt: abs((pt.time - now).total_seconds()))
            if abs((closest.time - now).total_seconds()) < 7200:
                cloud_cover = closest.cloud_cover

        # Sun elevation via SensorHub (configurable entity, default sun.sun)
        sun_elevation = self.sensor_hub.read_sun_elevation()

        # UV index for improved irradiance estimation
        uv_reading = self.sensor_hub.read_uv_index()

        # Solar irradiance: prefer direct sensor, fall back to panel-derived
        irradiance_reading = self.sensor_hub.read_solar_irradiance()
        solar_irradiance_w_m2 = None
        irradiance_from_panels = False
        if irradiance_reading and not irradiance_reading.stale:
            solar_irradiance_w_m2 = irradiance_reading.value
        else:
            # Try to derive from solar panel production + specs
            opts = self.config_entry.options
            panel_area = opts.get("solar_panel_area")
            panel_eff = opts.get("solar_panel_efficiency")
            derived = self.sensor_hub.derive_solar_irradiance_from_panels(
                panel_area, panel_eff,
            )
            if derived is not None:
                solar_irradiance_w_m2 = derived
                irradiance_from_panels = True

        # Wind speed, humidity, and pressure for enhanced COP modeling
        wind_reading = self.sensor_hub.read_wind_speed(snapshot)
        humidity_reading = self.sensor_hub.read_outdoor_humidity(
            thermo_state.humidity, snapshot
        )
        pressure_reading = self.sensor_hub.read_barometric_pressure()

        # Indoor humidity for latent load correction
        indoor_humidity_reading = self.sensor_hub.read_weighted_indoor_humidity(
            thermo_state.humidity
        )

        # Occupancy: people count for internal heat gain scaling
        people_count = self.occupancy.get_people_home_count()

        # Door/window contacts for infiltration modeling
        open_count, _total = self.sensor_hub.read_door_window_open_count()

        # Buffer zone temperatures (attic, crawlspace)
        attic_reading = self.sensor_hub.read_attic_temp()
        crawlspace_reading = self.sensor_hub.read_crawlspace_temp()

        # Precipitation from current forecast point
        is_precipitation = False
        if snapshot:
            closest_pt = min(snapshot, key=lambda pt: abs((pt.time - now).total_seconds()))
            if abs((closest_pt.time - now).total_seconds()) < 7200:
                is_precipitation = closest_pt.precipitation

        # Auxiliary appliance thermal load
        self.appliance_manager.update()
        appliance_btu = self.appliance_manager.total_thermal_impact_btu()

        # Use multi-sensor averaged temp for better EKF convergence
        observed = getattr(self, "_effective_indoor_temp", None) or thermo_state.indoor_temp
        noise_scale = getattr(self, "_last_indoor_noise_scale", 1.0)
        # Flag for estimator to know if irradiance came from panels
        self.estimator._irradiance_from_panels = irradiance_from_panels
        innovation = self.estimator.update(
            observed_temp=observed,
            outdoor_temp=outdoor_temp,
            hvac_mode=hvac_mode,
            hvac_running=hvac_running,
            cloud_cover=cloud_cover,
            sun_elevation=sun_elevation,
            wind_speed_mph=wind_reading.value if wind_reading else None,
            humidity=humidity_reading.value if humidity_reading else None,
            pressure_hpa=pressure_reading.value if pressure_reading else None,
            indoor_humidity=indoor_humidity_reading.value if indoor_humidity_reading else None,
            people_home_count=people_count,
            open_door_window_count=open_count,
            attic_temp=attic_reading.value if attic_reading else None,
            crawlspace_temp=crawlspace_reading.value if crawlspace_reading else None,
            precipitation=is_precipitation,
            appliance_btu=appliance_btu,
            aux_resistive_btu_hr=getattr(self, "_cached_aux_resistive_btu", 0.0),
            measurement_noise_scale=noise_scale,
            uv_index=uv_reading.value if uv_reading and not uv_reading.stale else None,
            solar_irradiance_w_m2=solar_irradiance_w_m2,
        )

        # ── Coefficient calibrator check (daily slow loop) ──────
        if self.coeff_calibrator.should_calibrate(now):
            try:
                cond_inns = self.estimator.get_conditioned_innovations()
                # Dry-run by default; live mode requires explicit config flag
                cal_enabled = self.config_entry.options.get(
                    CONF_CALIBRATION_ENABLED, False
                )
                result = self.coeff_calibrator.calibrate(
                    cond_inns, dry_run=not cal_enabled,
                )
                if not result.get("skipped"):
                    _LOGGER.info(
                        "Coefficient calibration %s (%d samples, residual=%.3f°F)",
                        "applied" if result.get("applied") else "dry-run",
                        result.get("samples_used", 0),
                        result.get("regression_residual", 0),
                    )
            except (ValueError, TypeError, KeyError, ZeroDivisionError):
                _LOGGER.warning(
                    "Coefficient calibration failed", exc_info=True
                )

        if self.estimator._n_obs % 100 == 0:
            _LOGGER.info(
                "Kalman filter: %d observations, confidence=%.0f%%, "
                "R=%.1f, C_mass=%.0f, Q_cool=%.0f, Q_heat=%.0f, solar=%.0f",
                self.estimator._n_obs,
                self.estimator.confidence * 100,
                self.estimator.R_value,
                self.estimator.thermal_mass,
                float(self.estimator.x[6]),
                float(self.estimator.x[7]),
                self.estimator.solar_gain_btu,
            )

    def _get_active_model(self):
        """Get the performance model for optimization.

        Profiler-primary: always prefer the profiler's measured model.
        Only fall back to EKF-adaptive or static defaults when the
        profiler has insufficient data.
        """
        # Profiler: measured performance from direct observation
        profiler_model = self.profiler.to_performance_model()
        if profiler_model is not None:
            return profiler_model

        # Fallback: adaptive model (EKF-derived) if available and not learning
        if self._use_adaptive and self._activation_tier != ACTIVATION_TIER_LEARNING:
            return self.adaptive_model

        # Last resort: conservative static defaults
        return self.model

