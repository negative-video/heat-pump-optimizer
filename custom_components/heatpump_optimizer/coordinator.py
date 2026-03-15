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

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
import homeassistant.helpers.issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .adapters.area_occupancy import AreaOccupancyManager
from .adapters.calendar_occupancy import CalendarOccupancyAdapter
from .adapters.forecast import async_get_forecast, async_get_forecast_multi, enrich_forecast_with_grid_data, populate_sun_elevation
from .adapters.occupancy import OccupancyAdapter, OccupancyMode
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
    CONF_CARBON_WEIGHT,
    CONF_CO2_ENTITY,
    CONF_COST_WEIGHT,
    CONF_CRAWLSPACE_TEMP_ENTITY,
    CONF_DEPARTURE_PROFILES,
    CONF_DEPARTURE_TRIGGER_WINDOW_MINUTES,
    CONF_DEPARTURE_ZONE,
    CONF_DOOR_WINDOW_ENTITIES,
    CONF_DEPARTURE_ZONES,
    CONF_ELECTRICITY_FLAT_RATE,
    CONF_ELECTRICITY_RATE_ENTITY,
    CONF_GRID_IMPORT_ENTITY,
    CONF_HVAC_POWER_DEFAULT_WATTS,
    CONF_HVAC_POWER_ENTITY,
    CONF_AREA_SENSOR_CONFIG,
    CONF_INDOOR_HUMIDITY_ENTITIES,
    CONF_INDOOR_TEMP_ENTITIES,
    CONF_INDOOR_WEIGHTING_MODE,
    CONF_OCCUPIED_WEIGHT_MULTIPLIER,
    CONF_OCCUPANCY_DEBOUNCE_MINUTES,
    CONF_ROOM_OCCUPANCY_DEBOUNCE_MINUTES,
    CONF_OCCUPANCY_ENTITIES,
    CONF_PRECONDITIONING_BUFFER_MINUTES,
    CONF_TRAVEL_TIME_SENSOR,
    CONF_TRAVEL_TIME_SENSORS,
    CONF_OUTDOOR_HUMIDITY_ENTITIES,
    CONF_OUTDOOR_TEMP_ENTITIES,
    CONF_SOLAR_EXPORT_RATE_ENTITY,
    CONF_SOLAR_IRRADIANCE_ENTITY,
    CONF_SOLAR_PRODUCTION_ENTITY,
    CONF_SUN_ENTITY,
    CONF_TOU_SCHEDULE,
    CONF_USE_ADAPTIVE_MODEL,
    CONF_USE_GREYBOX_MODEL,
    CONF_WIND_SPEED_ENTITY,
    DEFAULT_CALENDAR_AWAY_KEYWORDS,
    DEFAULT_CALENDAR_DEFAULT_MODE,
    DEFAULT_CALENDAR_HOME_KEYWORDS,
    DEFAULT_DEPARTURE_TRIGGER_WINDOW_MINUTES,
    DEFAULT_INDOOR_WEIGHTING_MODE,
    DEFAULT_OCCUPANCY_DEBOUNCE_MINUTES,
    DEFAULT_OCCUPIED_WEIGHT_MULTIPLIER,
    DEFAULT_PRECONDITIONING_BUFFER_MINUTES,
    DEFAULT_ROOM_OCCUPANCY_DEBOUNCE_MINUTES,
    DEFAULT_SUN_ENTITY,
    DEFAULT_CARBON_WEIGHT,
    DEFAULT_COST_WEIGHT,
    DEFAULT_HVAC_POWER_WATTS,
    DEFAULT_MODEL_CONFIDENCE_THRESHOLD,
    DEFAULT_FORECAST_CACHE_MAX_AGE_HOURS,
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
    PHASE_PAUSED,
    PHASE_SAFE_MODE,
    INIT_MODE_BEESTAT,
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
from .engine.greybox_optimizer import GreyBoxOptimizer
from .learning.baseline_capture import BaselineCapture
from .learning.model_tracker import ModelTracker
from .learning.override_tracker import OverrideTracker
from .learning.performance_profiler import PerformanceProfiler
from .learning.solar_adjuster import SolarAdjuster
from .learning.thermal_estimator import ThermalEstimator
from .savings_tracker import SavingsTracker, TIER_LEARNING, TIER_ESTIMATED, TIER_SIMULATED, TIER_CALIBRATED

_LOGGER = logging.getLogger(__name__)

# How often to persist learned parameters
LEARNING_PERSIST_INTERVAL_HOURS = 6


class HeatPumpOptimizerCoordinator(DataUpdateCoordinator):
    """Coordinator that manages the full optimization lifecycle."""

    def __init__(
        self,
        hass: HomeAssistant,
        profile_path: str | None,
        climate_entity_id: str,
        weather_entity_id: str,
        comfort_cool: tuple[float, float],
        comfort_heat: tuple[float, float],
        *,
        weather_entity_ids: list[str] | None = None,
        safety_limits: tuple[float, float] | None = None,
        occupancy_entity_id: str | None = None,
        options: dict[str, Any] | None = None,
        initialization_mode: str = "beestat",
        model_import_data: str | None = None,
        behavior: dict[str, Any] | None = None,
        profile_json: str | None = None,
    ):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES),
        )

        # Configuration
        self.climate_entity_id = climate_entity_id
        self.weather_entity_id = weather_entity_id
        self._weather_entity_ids = weather_entity_ids or [weather_entity_id]
        self.comfort_cool = comfort_cool
        self.comfort_heat = comfort_heat

        # Safety limits (absolute guardrails)
        from .const import DEFAULT_SAFETY_HEAT_MIN, DEFAULT_SAFETY_COOL_MAX
        self.safety_limits = safety_limits or (DEFAULT_SAFETY_HEAT_MIN, DEFAULT_SAFETY_COOL_MAX)

        # Behavior parameters
        behavior = behavior or {}
        self._aggressiveness: str = behavior.get("aggressiveness", "balanced")
        self._override_grace_hours: float = behavior.get("override_grace_hours", 2.0)
        self._reoptimize_interval_hours: int = behavior.get("reoptimize_interval_hours", 4)
        self._max_setpoint_change_per_hour: float = behavior.get("max_setpoint_change_per_hour", 4.0)
        self._away_comfort_delta: float = behavior.get("away_comfort_delta", 4.0)

        # Savings tracking config (from options flow)
        opts = options or {}
        self._co2_entity_id: str | None = opts.get(CONF_CO2_ENTITY) or None
        self._rate_entity_id: str | None = opts.get(CONF_ELECTRICITY_RATE_ENTITY) or None
        self._flat_rate: float | None = opts.get(CONF_ELECTRICITY_FLAT_RATE) or None
        self._power_entity_id: str | None = opts.get(CONF_HVAC_POWER_ENTITY) or None
        self._power_default_watts: float = opts.get(
            CONF_HVAC_POWER_DEFAULT_WATTS, DEFAULT_HVAC_POWER_WATTS
        )
        self._tou_schedule: list[dict] | None = opts.get(CONF_TOU_SCHEDULE) or None
        self._carbon_weight: float = opts.get(CONF_CARBON_WEIGHT, DEFAULT_CARBON_WEIGHT)
        self._cost_weight: float = opts.get(CONF_COST_WEIGHT, DEFAULT_COST_WEIGHT)

        # SensorHub — centralized sensor reads with fallback chains
        self.sensor_hub = SensorHub(
            hass,
            outdoor_temp_entities=opts.get(CONF_OUTDOOR_TEMP_ENTITIES) or [],
            outdoor_humidity_entities=opts.get(CONF_OUTDOOR_HUMIDITY_ENTITIES) or [],
            indoor_temp_entities=opts.get(CONF_INDOOR_TEMP_ENTITIES) or [],
            indoor_humidity_entities=opts.get(CONF_INDOOR_HUMIDITY_ENTITIES) or [],
            wind_speed_entity=opts.get(CONF_WIND_SPEED_ENTITY),
            solar_irradiance_entity=opts.get(CONF_SOLAR_IRRADIANCE_ENTITY),
            barometric_pressure_entity=opts.get(CONF_BAROMETRIC_PRESSURE_ENTITY),
            sun_entity=opts.get(CONF_SUN_ENTITY, DEFAULT_SUN_ENTITY),
            solar_production_entity=opts.get(CONF_SOLAR_PRODUCTION_ENTITY),
            grid_import_entity=opts.get(CONF_GRID_IMPORT_ENTITY),
            solar_export_rate_entity=opts.get(CONF_SOLAR_EXPORT_RATE_ENTITY),
            door_window_entities=opts.get(CONF_DOOR_WINDOW_ENTITIES) or [],
            attic_temp_entity=opts.get(CONF_ATTIC_TEMP_ENTITY),
            crawlspace_temp_entity=opts.get(CONF_CRAWLSPACE_TEMP_ENTITY),
            power_entity=self._power_entity_id,
            power_default_watts=self._power_default_watts,
            co2_entity=self._co2_entity_id,
            rate_entity=self._rate_entity_id,
            flat_rate=self._flat_rate,
        )

        # Room-aware area occupancy manager (optional)
        self.area_manager: AreaOccupancyManager | None = None
        weighting_mode = opts.get(CONF_INDOOR_WEIGHTING_MODE, DEFAULT_INDOOR_WEIGHTING_MODE)
        area_config_json = opts.get(CONF_AREA_SENSOR_CONFIG, "")
        if weighting_mode != DEFAULT_INDOOR_WEIGHTING_MODE and area_config_json:
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
        self._use_greybox = opts.get(CONF_USE_GREYBOX_MODEL, False)

        if initialization_mode == "learning":
            # Cold start: synthetic defaults, EKF learns from scratch
            self.model = PerformanceModel.from_defaults()
            self.estimator = ThermalEstimator.cold_start()
            _LOGGER.info("Initialized in learning mode — model will calibrate over 2-3 weeks")
        elif initialization_mode == "import" and model_import_data:
            # Restore from exported model
            import json as _json
            try:
                parsed = _json.loads(model_import_data) if isinstance(model_import_data, str) else model_import_data
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
                self.estimator = ThermalEstimator.cold_start()
        elif profile_path:
            # Beestat mode (default, backward compatible)
            try:
                if profile_json:
                    self.model = PerformanceModel.from_file_data(profile_json)
                else:
                    self.model = PerformanceModel.from_file(profile_path)
                self.estimator = ThermalEstimator.from_beestat(self.model._raw)
            except (FileNotFoundError, OSError, KeyError, ValueError) as err:
                _LOGGER.error(
                    "Failed to load Beestat profile '%s' (%s) — falling back to learning mode",
                    profile_path, err,
                )
                self.model = PerformanceModel.from_defaults()
                self.estimator = ThermalEstimator.cold_start()
        else:
            # Beestat mode selected but no profile path — fall back to learning
            _LOGGER.warning(
                "Beestat mode selected but no profile path configured — using learning mode"
            )
            self.model = PerformanceModel.from_defaults()
            self.estimator = ThermalEstimator.cold_start()

        self.simulator = ThermalSimulator(self.model)
        self.optimizer = ScheduleOptimizer(self.model, self.simulator)

        # Adaptive model (Kalman filter)
        self.adaptive_model = AdaptivePerformanceModel(self.estimator)
        # Propagate thermostat differential from model
        self.adaptive_model.cool_differential = self.model.cool_differential
        self.adaptive_model.heat_differential = self.model.heat_differential

        # Grey-box optimizer (LP + Kalman)
        self.greybox_optimizer = GreyBoxOptimizer(self.estimator)

        # Adapters (HA ↔ engine)
        self.thermostat = ThermostatAdapter(hass, climate_entity_id)
        self.occupancy = OccupancyAdapter(hass, occupancy_entity_id)

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
        self.strategic = StrategicPlanner(
            optimizer=self.optimizer,
            resist_balance_point=self.model.resist_balance_point or 50.0,
            greybox_optimizer=self.greybox_optimizer,
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
        self.counterfactual = CounterfactualSimulator()
        self.profiler = PerformanceProfiler()

        # Coordinator state
        self._phase: str = PHASE_IDLE
        self._active: bool = True
        self._paused: bool = False
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
        self._min_dwell_seconds: int = 900  # 15 minutes

        # Storage
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._unsub_state_listener = None

    # ── Setup / Shutdown ────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Initialize: load persisted state, start watchdog, run first optimization."""
        # Restore persisted learning data
        stored = await self._store.async_load()
        if stored:
            self._restore_learning_state(stored)

        # Bootstrap from recorder history if no persisted learning state
        if not stored or "thermal_estimator" not in stored:
            await self._try_history_bootstrap()

        # Layer 3: Watchdog — listen for thermostat state changes
        self._unsub_state_listener = async_track_state_change_event(
            self.hass,
            self.climate_entity_id,
            self._handle_thermostat_state_change,
        )

        # Run initial optimization (non-fatal — weather/thermostat may not be ready yet)
        try:
            await self._run_strategic_optimization()
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Initial optimization failed — will retry on next update cycle",
                exc_info=True,
            )

    async def async_shutdown(self) -> None:
        """Graceful shutdown: safe setpoint, persist state, remove listeners."""
        if self._unsub_state_listener:
            self._unsub_state_listener()

        # Write safe midpoint so thermostat doesn't hold an extreme temp
        thermo_state = self.thermostat.read_state()
        if thermo_state.available:
            comfort = self._active_comfort_range()
            await self.thermostat.async_write_safe_default(*comfort)

        # Persist everything
        await self._persist_state()

    # ── Main update loop (Layer 2: every 5 minutes) ─────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        """Called every 5 minutes by DataUpdateCoordinator."""
        try:
            return await self._update_cycle()
        except Exception as err:
            raise UpdateFailed(f"Update failed: {err}") from err

    async def _update_cycle(self) -> dict[str, Any]:
        """Full 5-minute update cycle."""
        now = datetime.now(timezone.utc)

        # Always fetch weather early — panel needs it even when paused/override
        new_forecast, forecast_source = await async_get_forecast_multi(
            self.hass, self._weather_entity_ids
        )
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

        # Read weighted indoor temp (falls back to thermostat if no area manager)
        indoor_temp_reading = self.sensor_hub.read_weighted_indoor_temp(
            thermo_state.indoor_temp
        )
        effective_indoor_temp = (
            indoor_temp_reading.value if indoor_temp_reading
            else thermo_state.indoor_temp
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

        # ── Watchdog checks ─────────────────────────────────────────

        # Override detection
        if self.watchdog.is_override_active:
            if self.watchdog.check_grace_period():
                # Grace expired — resume
                await self._run_strategic_optimization()
            else:
                self._phase = PHASE_PAUSED
                return self._build_data(thermo_state)
        elif not self._paused:
            override = self.watchdog.check_override(
                self.thermostat.last_written_setpoint,
                thermo_state.target_temp,
            )
            if override:
                self.override_tracker.record_override(
                    expected_setpoint=self.thermostat.last_written_setpoint or 0,
                    actual_setpoint=thermo_state.target_temp or 0,
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
            comfort = self._active_comfort_range()
            await self.thermostat.async_write_safe_default(*comfort)
            # Don't return — let EKF, baseline, and savings continue below

        # ── Tactical: reality check & setpoint execution ────────────

        schedule = self.strategic.schedule
        current_entry = self._get_current_entry(now)

        if schedule and self._active and current_entry and thermo_state.indoor_temp is not None:
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
            self._phase = self._classify_phase(current_entry)

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
                target = self._apply_rate_limit(target, thermo_state.target_temp)
                if thermo_state.target_temp is None or abs(thermo_state.target_temp - target) > 0.5:
                    if self._check_dwell_time(now):
                        await self.thermostat.async_set_temperature(target, *comfort)
                        self._last_written_setpoint_time = now
            else:
                # Write scheduled setpoint if different from current
                if thermo_state.target_temp is not None:
                    target = self._apply_rate_limit(current_entry.target_temp, thermo_state.target_temp)
                    diff = abs(thermo_state.target_temp - target)
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
            self._phase = PHASE_IDLE

        # ── Departure detection (Stage 2 pre-conditioning) ───────────

        if self._precondition_plan and thermo_state.indoor_temp is not None:
            await self._check_departure_trigger(
                now, thermo_state.indoor_temp, new_forecast
            )

        # ── Kalman filter update ──────────────────────────────────────

        if thermo_state.indoor_temp is not None and (self._use_adaptive or self._use_greybox):
            self._feed_estimator(thermo_state, now)
            # Fire event when confidence threshold is first reached
            if not self._confidence_threshold_reached and self.estimator.confidence >= DEFAULT_MODEL_CONFIDENCE_THRESHOLD:
                self._confidence_threshold_reached = True
                self._fire_event(EVENT_CONFIDENCE_REACHED, {
                    "confidence": round(self.estimator.confidence * 100, 1),
                    "observations": self.estimator._n_obs,
                })

        # ── Savings tracking ──────────────────────────────────────────

        hvac_running = self._is_hvac_running(thermo_state)
        power_watts = self.sensor_hub.read_power_draw()
        co2_intensity = self.sensor_hub.read_co2_intensity()
        elec_rate = self.sensor_hub.read_electricity_rate()
        solar_reading = self.sensor_hub.read_solar_production()

        # Compute actual COP for this interval (for savings decomposition)
        actual_cop = None
        outdoor_reading = self.sensor_hub.read_outdoor_temp()
        outdoor_temp = outdoor_reading.value if outdoor_reading else None
        if outdoor_temp is not None and (self._use_adaptive or self._use_greybox):
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
            actual_cop=actual_cop,
        )

        # ── Baseline capture (during learning phase) ───────────────
        if self._is_learning_active() and thermo_state.target_temp is not None:
            self.baseline_capture.record_observation(
                now=now,
                setpoint=thermo_state.target_temp,
                mode=self.strategic.mode or "off",
            )
            # Build template when ready
            if self.baseline_capture.is_ready and self.baseline_capture.template is None:
                self.baseline_capture.build_template()
                _LOGGER.info("Baseline schedule captured after %d days",
                             self.baseline_capture.sample_days)

        # ── Counterfactual simulation step ─────────────────────────
        if self.baseline_capture.template is not None and outdoor_temp is not None:
            baseline_setpoint = self.baseline_capture.get_baseline_setpoint(now)
            baseline_mode = self.baseline_capture.get_baseline_mode(now)
            if baseline_setpoint is not None and baseline_mode is not None:
                cloud_cover = None
                sun_elevation = None
                if new_forecast:
                    cloud_cover = new_forecast[0].cloud_cover
                    sun_elevation = new_forecast[0].sun_elevation

                # Precipitation flag from current forecast
                is_precip = new_forecast[0].precipitation if new_forecast else False

                self.counterfactual.step(
                    now=now,
                    outdoor_temp=outdoor_temp,
                    baseline_setpoint=baseline_setpoint,
                    baseline_mode=baseline_mode,
                    estimator=self.estimator,
                    dt_minutes=DEFAULT_UPDATE_INTERVAL_MINUTES,
                    cloud_cover=cloud_cover,
                    sun_elevation=sun_elevation,
                    carbon_intensity=co2_intensity,
                    electricity_rate=elec_rate,
                    real_indoor_temp=thermo_state.indoor_temp,
                    people_home_count=self.occupancy.get_people_home_count(),
                    precipitation=is_precip,
                    indoor_humidity=self._current_indoor_humidity,
                )

        # ── Performance profiler feed ─────────────────────────────
        if (
            thermo_state.indoor_temp is not None
            and outdoor_temp is not None
            and thermo_state.hvac_mode != "off"
        ):
            solar_reading = self.sensor_hub.read_solar_production()
            self.profiler.record_observation(
                indoor_temp=thermo_state.indoor_temp,
                outdoor_temp=outdoor_temp,
                hvac_action=thermo_state.hvac_action,
                hvac_mode=thermo_state.hvac_mode,
                aux_heat_active=self._is_aux_heat_running(thermo_state),
                solar_irradiance=solar_reading.value if solar_reading else None,
                now=now,
            )

        # ── Update accuracy tier ───────────────────────────────────
        self._update_accuracy_tier()

        # ── Model progress check (repair issue) ──────────────────
        self._check_model_progress()

        # ── Periodic learning persistence ───────────────────────────

        if self._should_persist_learning(now):
            self.model_tracker.update_corrections()
            try:
                await self._persist_state()
                self._last_learning_persist = now
            except Exception:
                _LOGGER.warning("Failed to persist learning state, will retry next cycle", exc_info=True)

        return self._build_data(thermo_state)

    # ── Strategic optimization ──────────────────────────────────────

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

        # If reactive overrides calendar (person came home early), fire event
        if (
            occupancy_timeline
            and effective_mode == OccupancyMode.HOME
        ):
            from .controllers.strategic import StrategicPlanner
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

        # Select active model and optimizer path
        active_model = self._get_active_model()
        use_greybox_now = self._should_use_greybox()

        # Update balance point if using adaptive or grey-box model
        if active_model is self.adaptive_model or use_greybox_now:
            bp = self.adaptive_model.resist_balance_point
            if bp is not None:
                self.strategic.resist_balance_point = bp

        # Configure strategic planner for grey-box or heuristic path
        self.strategic._use_greybox = use_greybox_now

        # Temporarily swap model for optimization if using adaptive model
        original_model = self.optimizer.model
        original_sim_model = self.simulator.model
        if active_model is not self.model and not use_greybox_now:
            self.optimizer.model = active_model
            self.simulator.model = active_model

        # Gather current environmental context for the optimizer
        people_count = self.occupancy.get_people_home_count()

        # Run optimizer in executor (synchronous engine)
        try:
            schedule = await self.hass.async_add_executor_job(
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
            )
        finally:
            # Restore original model references
            self.optimizer.model = original_model
            self.simulator.model = original_sim_model

        if schedule:
            self._active = True
            _LOGGER.info(
                "Optimization [%s]: baseline=%.1f min, optimized=%.1f min, savings=%.1f%%",
                self.strategic.mode,
                schedule.baseline_runtime_minutes,
                schedule.optimized_runtime_minutes,
                schedule.savings_pct,
            )
            # Update savings tracker with new baseline ratio
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
        """Plan pre-conditioning for the next AWAY→HOME transition."""
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
        import json as _json

        raw = opts.get(CONF_DEPARTURE_PROFILES)
        if raw:
            try:
                profiles = _json.loads(raw)
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

    async def async_force_reoptimize(self) -> None:
        """Force immediate re-optimization (service call)."""
        self.strategic.should_reoptimize(force=True)
        await self._run_strategic_optimization()

    def pause(self) -> None:
        """Pause optimization (service call)."""
        self._paused = True
        self._phase = PHASE_PAUSED
        _LOGGER.info("Optimization paused by service call")

    async def async_resume(self) -> None:
        """Resume optimization (service call)."""
        self._paused = False
        self.watchdog.clear_override()
        _LOGGER.info("Optimization resumed by service call")
        await self._run_strategic_optimization()

    def set_occupancy(self, mode: OccupancyMode | None) -> None:
        """Set occupancy mode (service call)."""
        self.occupancy.force_mode(mode)

    async def async_demand_response(self, mode: str, duration_minutes: int) -> None:
        """Activate or deactivate demand response mode.

        When 'reduce' is active, comfort bounds are widened by
        DEFAULT_DEMAND_RESPONSE_DELTA_F to reduce HVAC load.
        Auto-restores after duration_minutes.
        """
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
            _LOGGER.info("Pause constraint expired — resuming optimization")
            self.hass.async_create_task(self._run_strategic_optimization())

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
            self.adaptive_model = AdaptivePerformanceModel(self.estimator)
            self.adaptive_model.cool_differential = self.model.cool_differential
            self.adaptive_model.heat_differential = self.model.heat_differential
            self.greybox_optimizer = GreyBoxOptimizer(self.estimator)
            self.strategic.greybox_optimizer = self.greybox_optimizer
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
        self.hass.async_create_task(self._run_strategic_optimization())

    @callback
    def _handle_thermostat_state_change(self, event) -> None:
        """React to thermostat state changes."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None:
            return

        # HVAC mode change (heat→cool, etc.)
        if old_state and old_state.state != new_state.state:
            if self.watchdog.check_mode_change(new_state.state):
                self.hass.async_create_task(self._run_strategic_optimization())

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
        hvac_mode = self.strategic.mode or "off"

        # Cloud cover from forecast (now a proper field on ForecastPoint)
        cloud_cover = None
        if snapshot:
            closest = min(snapshot, key=lambda pt: abs((pt.time - now).total_seconds()))
            if abs((closest.time - now).total_seconds()) < 600:
                cloud_cover = closest.cloud_cover

        # Sun elevation via SensorHub (configurable entity, default sun.sun)
        sun_elevation = self.sensor_hub.read_sun_elevation()

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

        innovation = self.estimator.update(
            observed_temp=thermo_state.indoor_temp,
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
        """Get the performance model for heuristic optimization.

        Priority:
        1. PerformanceProfiler (measured reality) — if confidence >= 0.7
        2. Adaptive model (EKF-derived) — if confidence above threshold
        3. Static Beestat/default model — fallback
        (Not used when grey-box LP optimizer is active.)
        """
        # Profiler: measured performance trumps EKF-derived estimates
        if self.profiler.confidence() >= 0.7:
            profiler_model = self.profiler.to_performance_model()
            if profiler_model is not None:
                return profiler_model

        if not self._use_adaptive and not self._use_greybox:
            return self.model

        confidence = self.adaptive_model.confidence
        if confidence >= DEFAULT_MODEL_CONFIDENCE_THRESHOLD:
            return self.adaptive_model
        else:
            _LOGGER.debug(
                "Adaptive model confidence %.0f%% below threshold — using Beestat fallback",
                confidence * 100,
            )
            return self.model

    def _should_use_greybox(self) -> bool:
        """Determine whether to use the grey-box LP optimizer.

        Grey-box requires the Kalman filter to have reached minimum
        confidence. Falls back to the heuristic optimizer otherwise.
        """
        if not self._use_greybox:
            return False

        confidence = self.estimator.confidence
        if confidence >= DEFAULT_MODEL_CONFIDENCE_THRESHOLD:
            return True
        else:
            _LOGGER.debug(
                "Grey-box model confidence %.0f%% below threshold — "
                "using heuristic optimizer",
                confidence * 100,
            )
            return False

    # ── Helpers ─────────────────────────────────────────────────────

    def _active_comfort_range(self) -> tuple[float, float]:
        """Get the current comfort range accounting for mode, occupancy, demand response, and safety limits."""
        mode = self.thermostat.get_active_mode()
        base = self.comfort_heat if mode == "heat" else self.comfort_cool
        comfort = self.occupancy.adjust_comfort_range(base, mode)

        # Widen comfort bounds during demand response
        if self._demand_response_active:
            now = datetime.now(timezone.utc)
            if self._demand_response_end and now >= self._demand_response_end:
                # Auto-restore
                self._demand_response_active = False
                self._demand_response_end = None
                _LOGGER.info("Demand response auto-expired — restoring normal comfort")
                self._fire_event(f"{DOMAIN}_demand_response", {"mode": "auto_restored"})
            else:
                delta = self._demand_response_delta
                comfort = (comfort[0] - delta, comfort[1] + delta)

        # Apply active constraints from external integrations
        for constraint in self._active_constraints:
            if constraint["type"] == "min_temp":
                comfort = (max(comfort[0], constraint["value"]), comfort[1])
            elif constraint["type"] == "max_temp":
                comfort = (comfort[0], min(comfort[1], constraint["value"]))

        # Enforce safety limits (absolute guardrails — never exceeded)
        safety_min, safety_max = self.safety_limits
        comfort = (max(comfort[0], safety_min), min(comfort[1], safety_max))

        # Learning mode conservatism: use inner 60% of band
        if self._is_learning_active():
            band = comfort[1] - comfort[0]
            margin = band * 0.2  # shrink by 20% from each side
            comfort = (comfort[0] + margin, comfort[1] - margin)

        return comfort

    @property
    def learning_active(self) -> bool:
        """Whether the model is still in learning mode."""
        return self._is_learning_active()

    def _is_learning_active(self) -> bool:
        """Check if the model is still calibrating (below confidence threshold)."""
        return self.estimator.confidence < DEFAULT_MODEL_CONFIDENCE_THRESHOLD

    def _update_accuracy_tier(self) -> None:
        """Update the savings accuracy tier based on baseline and model confidence."""
        baseline_conf = self.baseline_capture.confidence
        model_conf = self.estimator.confidence
        is_beestat = self._initialization_mode == INIT_MODE_BEESTAT

        if baseline_conf >= 0.7 and model_conf >= 0.5:
            tier = TIER_CALIBRATED
        elif is_beestat and self.baseline_capture.is_ready:
            # Beestat fast-track: skip ESTIMATED, go straight to SIMULATED
            tier = TIER_SIMULATED if baseline_conf >= 0.3 else TIER_ESTIMATED
        elif baseline_conf >= 0.3 and model_conf >= 0.3:
            tier = TIER_SIMULATED
        elif self.baseline_capture.is_ready:
            tier = TIER_ESTIMATED
        else:
            tier = TIER_LEARNING

        # Fire events on tier changes and milestones
        if tier != self._last_accuracy_tier:
            self._fire_event(EVENT_ACCURACY_TIER_CHANGED, {
                "previous_tier": self._last_accuracy_tier,
                "new_tier": tier,
                "model_confidence": round(model_conf * 100, 1),
                "baseline_confidence": round(baseline_conf * 100, 1),
            })
            self._last_accuracy_tier = tier

        if (
            not self._baseline_complete_fired
            and self.baseline_capture.is_ready
        ):
            self._baseline_complete_fired = True
            self._fire_event(EVENT_BASELINE_COMPLETE, {
                "sample_days": self.baseline_capture.sample_days,
            })

        self.savings_tracker.set_accuracy_tier(tier)

        # Wire counterfactual simulator into savings tracker when ready
        if tier in (TIER_SIMULATED, TIER_CALIBRATED):
            self.savings_tracker.set_counterfactual(self.counterfactual)

    def _check_model_progress(self) -> None:
        """Create or clear a repair issue if the model seems stuck."""
        days = self.baseline_capture.sample_days
        confidence = self.estimator.confidence
        if days >= 7 and confidence < 0.15:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "model_not_progressing",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="model_not_progressing",
                translation_placeholders={
                    "days": str(days),
                    "confidence": f"{confidence * 100:.0f}%",
                },
            )
        elif confidence >= 0.15:
            ir.async_delete_issue(self.hass, DOMAIN, "model_not_progressing")

    def _apply_rate_limit(self, target: float, current: float | None) -> float:
        """Limit setpoint change to max_setpoint_change_per_hour per write cycle.

        Prevents jarring temperature swings by ramping the setpoint gradually.
        """
        if current is None:
            return target
        max_change = self._max_setpoint_change_per_hour
        diff = target - current
        if abs(diff) <= max_change:
            return target
        # Clamp to max change in the desired direction
        return current + max_change if diff > 0 else current - max_change

    def _check_dwell_time(self, now: datetime) -> bool:
        """Enforce minimum dwell time between setpoint writes (15 min)."""
        if self._last_written_setpoint_time is None:
            return True
        elapsed = (now - self._last_written_setpoint_time).total_seconds()
        return elapsed >= self._min_dwell_seconds

    def _get_current_entry(self, now: datetime) -> ScheduleEntry | None:
        schedule = self.strategic.schedule
        if not schedule:
            return None
        for entry in schedule.entries:
            if entry.start_time <= now < entry.end_time:
                return entry
        return None

    def _get_next_entry(self, now: datetime) -> ScheduleEntry | None:
        schedule = self.strategic.schedule
        if not schedule:
            return None
        for entry in schedule.entries:
            if entry.start_time > now:
                return entry
        return None

    def _classify_phase(self, entry: ScheduleEntry) -> str:
        """Determine phase from schedule entry reason text."""
        from .const import PHASE_COASTING, PHASE_MAINTAINING, PHASE_PRE_COOLING, PHASE_PRE_HEATING
        reason = entry.reason.lower()
        if "pre-cooling" in reason:
            return PHASE_PRE_COOLING
        if "pre-heating" in reason:
            return PHASE_PRE_HEATING
        if "coasting" in reason:
            return PHASE_COASTING
        return PHASE_MAINTAINING

    def _is_forecast_stale(self) -> bool:
        last = self._last_forecast_time
        if not last:
            # No forecast has ever been fetched — not stale, just needs first fetch.
            # Returning True here would immediately enter safe_mode on startup.
            return False
        elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        return elapsed >= DEFAULT_STALE_FORECAST_HOURS

    def _should_persist_learning(self, now: datetime) -> bool:
        if self._last_learning_persist is None:
            return True
        elapsed = (now - self._last_learning_persist).total_seconds() / 3600
        return elapsed >= LEARNING_PERSIST_INTERVAL_HOURS

    def _get_latitude(self) -> float:
        """Get latitude from HA config or default."""
        return self.hass.config.latitude or 37.9

    def _describe_next_action(self, entry: ScheduleEntry | None) -> str:
        if entry is None:
            return "No upcoming actions"
        time_str = entry.start_time.strftime("%-I:%M %p")
        return f"Target {entry.target_temp:.0f}°F at {time_str}"

    def _check_model_alert(self, accuracy_report: dict) -> bool:
        """Check for model alert and fire event on transition to alert state."""
        alert = any(
            accuracy_report[m].get("alert", False) for m in ("cool", "heat", "resist")
        )
        if alert and not self._last_model_alert:
            alerting_modes = [
                m for m in ("cool", "heat", "resist")
                if accuracy_report[m].get("alert", False)
            ]
            self._fire_event(EVENT_MODEL_ALERT, {
                "modes": alerting_modes,
                "corrections": {
                    m: accuracy_report[m]["correction"] for m in alerting_modes
                },
            })
        self._last_model_alert = alert
        return alert

    # ── Event firing ──────────────────────────────────────────────

    @callback
    def _fire_event(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Fire an HA event for automation triggers."""
        self.hass.bus.async_fire(event_type, data or {})

    # ── Persistence ─────────────────────────────────────────────────

    async def _persist_state(self) -> None:
        """Persist learning data and coordinator state to HA storage."""
        data = {
            "last_optimization_time": (
                self.strategic.last_optimization_time.isoformat()
                if self.strategic.last_optimization_time
                else None
            ),
            "phase": self._phase,
            "active": self._active,
            "forced_occupancy_mode": (
                self.occupancy._forced_mode.value
                if self.occupancy._forced_mode is not None
                else None
            ),
            "model_tracker": self.model_tracker.to_dict(),
            "solar_adjuster": self.solar_adjuster.to_dict(),
            "override_tracker": self.override_tracker.to_dict(),
            "savings_tracker": self.savings_tracker.to_dict(),
            "thermal_estimator": self.estimator.to_dict(),
            "baseline_capture": self.baseline_capture.to_dict(),
            "counterfactual": self.counterfactual.to_dict(),
            "performance_profiler": self.profiler.to_dict(),
        }
        await self._store.async_save(data)

    def _restore_learning_state(self, stored: dict) -> None:
        """Restore learning data from persisted storage."""
        if stored.get("forced_occupancy_mode") is not None:
            try:
                self.occupancy.force_mode(
                    OccupancyMode(stored["forced_occupancy_mode"])
                )
                _LOGGER.debug("Restored forced occupancy mode: %s",
                              stored["forced_occupancy_mode"])
            except ValueError:
                _LOGGER.warning("Invalid stored occupancy mode: %s",
                                stored["forced_occupancy_mode"])

        if "model_tracker" in stored:
            self.model_tracker = ModelTracker.from_dict(stored["model_tracker"])
            _LOGGER.debug("Restored model tracker state")
        if "solar_adjuster" in stored:
            self.solar_adjuster = SolarAdjuster.from_dict(stored["solar_adjuster"])
            _LOGGER.debug("Restored solar adjuster (coefficient=%.3f)",
                          self.solar_adjuster.solar_coefficient)
        if "override_tracker" in stored:
            self.override_tracker = OverrideTracker.from_dict(stored["override_tracker"])
            _LOGGER.debug("Restored %d override records",
                          self.override_tracker.record_count)
        if "savings_tracker" in stored:
            self.savings_tracker = SavingsTracker.from_dict(stored["savings_tracker"])
            totals = self.savings_tracker.cumulative_totals()
            _LOGGER.debug("Restored savings tracker (%.1f kWh saved cumulative)",
                          totals["kwh_saved"])
        if "thermal_estimator" in stored:
            self.estimator = ThermalEstimator.from_dict(stored["thermal_estimator"])
            self.adaptive_model = AdaptivePerformanceModel(self.estimator)
            self.greybox_optimizer = GreyBoxOptimizer(self.estimator)
            self.strategic.greybox_optimizer = self.greybox_optimizer
            _LOGGER.info(
                "Restored Kalman filter (%d observations, confidence=%.0f%%)",
                self.estimator._n_obs,
                self.estimator.confidence * 100,
            )
        if "baseline_capture" in stored:
            self.baseline_capture = BaselineCapture.from_dict(stored["baseline_capture"])
            _LOGGER.debug(
                "Restored baseline capture (%d days, confidence=%.0f%%)",
                self.baseline_capture.sample_days,
                self.baseline_capture.confidence * 100,
            )
        if "counterfactual" in stored:
            self.counterfactual = CounterfactualSimulator.from_dict(stored["counterfactual"])
            _LOGGER.debug(
                "Restored counterfactual simulator (virtual_temp=%.1f°F)",
                self.counterfactual.virtual_indoor_temp,
            )
        if "performance_profiler" in stored:
            self.profiler = PerformanceProfiler.from_dict(stored["performance_profiler"])
            _LOGGER.debug(
                "Restored performance profiler (%d observations, confidence=%.0f%%)",
                self.profiler.total_observations,
                self.profiler.confidence() * 100,
            )

    async def _try_history_bootstrap(self) -> None:
        """Attempt to bootstrap learning subsystems from recorder history.

        Runs at first startup when no persisted state exists. Batch-replays
        up to 10 days of historical thermostat/sensor data through the EKF
        to achieve meaningful model convergence immediately.
        """
        from .learning.history_bootstrap import async_bootstrap_from_history
        from .const import DEFAULT_HISTORY_BOOTSTRAP_DAYS

        result = await async_bootstrap_from_history(
            hass=self.hass,
            climate_entity_id=self.climate_entity_id,
            outdoor_temp_entities=self.sensor_hub._outdoor_temp_entities,
            weather_entity_ids=self._weather_entity_ids,
            wind_speed_entity=getattr(self.sensor_hub, "_wind_speed_entity", None),
            humidity_entities=self.sensor_hub._outdoor_humidity_entities,
            estimator=self.estimator,
            baseline_capture=self.baseline_capture,
            profiler=self.profiler,
            max_days=DEFAULT_HISTORY_BOOTSTRAP_DAYS,
        )

        if result.success:
            # Rebuild dependent objects with updated estimator state
            self.adaptive_model = AdaptivePerformanceModel(self.estimator)
            self.greybox_optimizer = GreyBoxOptimizer(self.estimator)
            self.strategic.greybox_optimizer = self.greybox_optimizer

            _LOGGER.info(
                "History bootstrap complete: %d EKF observations "
                "(confidence=%.0f%%), baseline %s (%d obs), profiler %d obs",
                result.ekf_observations,
                result.final_confidence * 100,
                "ready" if self.baseline_capture.is_ready else "not ready",
                result.baseline_observations,
                result.profiler_observations,
            )

            # Persist immediately so we don't re-bootstrap on next restart
            await self._persist_state()
        else:
            _LOGGER.info("History bootstrap skipped: %s", result.reason)

    # ── HVAC state helpers ──────────────────────────────────────────

    def _is_hvac_running(self, thermo_state) -> bool:
        """Check if the HVAC is currently running (actively heating/cooling)."""
        if thermo_state is None or not thermo_state.available:
            return False
        action = getattr(thermo_state, "hvac_action", None)
        if action:
            return action in ("heating", "cooling")
        # Fallback: if no hvac_action, check if mode is not idle/off
        mode = getattr(thermo_state, "hvac_mode", None)
        return mode is not None and mode not in ("off", "fan_only")

    def _is_aux_heat_running(self, thermo_state) -> bool:
        """Check if auxiliary/emergency heat is currently running."""
        if thermo_state is None or not thermo_state.available:
            return False
        action = getattr(thermo_state, "hvac_action", None)
        if action:
            return action in ("aux_heating", "emergency_heating")
        # Some thermostats expose aux heat as an attribute
        attrs = getattr(thermo_state, "attributes", {})
        return bool(attrs.get("aux_heat", False))

    # ── Diagnostic helpers ─────────────────────────────────────────

    def _outdoor_sensor_diverges_from_forecast(self) -> bool:
        """Check if standalone outdoor sensor diverges from forecast snapshot.

        Only triggers when outdoor temp entities are configured and the reading
        differs from the forecast by more than the deviation threshold.
        """
        if not self.sensor_hub._outdoor_temp_entities:
            return False
        snapshot = self.strategic.forecast_snapshot
        if not snapshot:
            return False

        # Read from standalone sensor only (not forecast fallback)
        reading = self.sensor_hub._read_multi_temp(
            self.sensor_hub._outdoor_temp_entities, "Outdoor temp"
        )
        if reading is None or reading.stale:
            return False

        now = self.sensor_hub._now()
        closest = min(snapshot, key=lambda pt: abs((pt.time - now).total_seconds()))
        if abs((closest.time - now).total_seconds()) > 3600:
            return False

        from .const import DEFAULT_FORECAST_DEVIATION_THRESHOLD
        deviation = abs(reading.value - closest.outdoor_temp)
        if deviation > DEFAULT_FORECAST_DEVIATION_THRESHOLD:
            _LOGGER.debug(
                "Outdoor sensor (%.1f°F) diverges from forecast (%.1f°F) by %.1f°F",
                reading.value, closest.outdoor_temp, deviation,
            )
            return True
        return False

    def _is_sensor_stale(self) -> bool:
        """Check if the thermostat has reported identical temps for 24+ hours."""
        if len(self._temp_history) < 288:  # need at least 24h of data
            return False
        # Check if all readings within ±0.1°F of first reading
        ref = self._temp_history[0]
        return all(abs(t - ref) <= 0.1 for t in self._temp_history)

    def _get_last_tactical_correction(self) -> float | None:
        """Get the last tactical correction applied (°F)."""
        if not hasattr(self, "_last_tactical_correction"):
            return None
        return self._last_tactical_correction

    def _compute_forecast_deviation(self) -> float | None:
        """Max deviation between current forecast snapshot and optimization snapshot."""
        if not self.strategic._last_forecast_snapshot:
            return None
        try:
            current = self.strategic._last_forecast_snapshot
            # If we have both snapshots, compare them
            if not hasattr(self.strategic, "_optimization_forecast_snapshot"):
                return None
            opt_snapshot = self.strategic._optimization_forecast_snapshot
            if not opt_snapshot:
                return None
            deviations = []
            for i in range(min(len(current), len(opt_snapshot), 6)):
                deviations.append(abs(current[i].outdoor_temp - opt_snapshot[i].outdoor_temp))
            return max(deviations) if deviations else None
        except Exception:
            return None

    def _build_schedule_detail(self, schedule) -> list[dict] | None:
        """Build a list of schedule entries for the schedule sensor's attributes."""
        if not schedule or not schedule.entries:
            return None
        return [
            {
                "start": entry.start_time.isoformat(),
                "end": entry.end_time.isoformat(),
                "target_temp": entry.target_temp,
                "mode": entry.mode,
                "reason": entry.reason,
            }
            for entry in schedule.entries
        ]

    def _build_forecast_detail(self, schedule) -> list[dict] | None:
        """Build forecast simulation points for the schedule sensor's attributes."""
        if not schedule or not schedule.simulation:
            return None

        # Build comfort bounds lookup from enriched forecast points
        comfort_bounds: dict[int, tuple[float, float]] = {}
        snapshot = self.strategic.forecast_snapshot
        if snapshot:
            for pt in snapshot:
                if pt.comfort_min is not None and pt.comfort_max is not None:
                    comfort_bounds[int(pt.time.timestamp()) // 3600] = (
                        round(pt.comfort_min, 1),
                        round(pt.comfort_max, 1),
                    )

        result = []
        for pt in schedule.simulation:
            hour_key = int(pt.time.timestamp()) // 3600
            bounds = comfort_bounds.get(hour_key)
            entry: dict = {
                "time": pt.time.isoformat(),
                "indoor": round(pt.indoor_temp, 1),
                "outdoor": round(pt.outdoor_temp, 1),
                "hvac": pt.hvac_running,
            }
            if bounds:
                entry["comfort_min"] = bounds[0]
                entry["comfort_max"] = bounds[1]
            result.append(entry)
        return result

    def _build_weather_forecast(self) -> list[dict] | None:
        """Build simplified weather forecast for the panel (outdoor temps only)."""
        forecast = self._last_good_forecast
        if not forecast:
            return None
        result = []
        for pt in forecast[:24]:
            result.append({
                "time": pt.time.isoformat(),
                "outdoor": round(pt.outdoor_temp, 1),
                "humidity": round(pt.humidity, 0) if pt.humidity else None,
                "cloud_cover": round(pt.cloud_cover, 2) if pt.cloud_cover else None,
            })
        return result or None

    # ── Data for sensor entities ────────────────────────────────────

    def _build_data(self, thermo_state=None) -> dict[str, Any]:
        """Build coordinator data dict for sensor entities."""
        now = datetime.now(timezone.utc)
        schedule = self.strategic.schedule
        current_entry = self._get_current_entry(now)
        next_entry = self._get_next_entry(now)

        # Tactical stats
        tactical_mae = self.tactical.mean_absolute_error
        tactical_bias = self.tactical.mean_signed_error

        # Model tracker report
        accuracy_report = self.model_tracker.get_accuracy_report()

        # Override stats
        override_stats = self.override_tracker.get_stats()

        # Savings
        today_savings = self.savings_tracker.today_report()
        cumulative = self.savings_tracker.cumulative_totals()

        return {
            # Core state
            "phase": self._phase,
            "active": self._active and not self._paused,
            "override_detected": self.watchdog.is_override_active,
            "sensor_stale": self._is_sensor_stale(),
            "aux_heat_active": self._is_aux_heat_running(thermo_state),

            # Schedule
            "target_setpoint": current_entry.target_temp if current_entry else None,
            "next_action": self._describe_next_action(next_entry),
            "schedule_entries": len(schedule.entries) if schedule else 0,
            "savings_pct": (
                schedule.savings_pct if schedule
                and self.savings_tracker.accuracy_tier != TIER_LEARNING
                else None
            ),
            "baseline_runtime": schedule.baseline_runtime_minutes if schedule else None,
            "optimized_runtime": schedule.optimized_runtime_minutes if schedule else None,
            "last_optimization": (
                self.strategic.last_optimization_time.isoformat()
                if self.strategic.last_optimization_time else None
            ),
            "mode": self.strategic.mode,

            # Thermostat
            "current_indoor_temp": thermo_state.indoor_temp if thermo_state else None,
            "humidity": thermo_state.humidity if thermo_state else None,

            # Apparent temperature (humidity-adjusted)
            "apparent_temperature": self._current_apparent_temp,
            "indoor_humidity": self._current_indoor_humidity,

            # Room-aware sensing (None if not configured)
            "area_occupancy": (
                self.area_manager.get_diagnostics()
                if self.area_manager is not None else None
            ),

            # Tactical
            "tactical_correction": self._get_last_tactical_correction(),
            "tactical_state": self.tactical.state.value,
            "prediction_error": (
                self.tactical.error_history[-1][1]
                if self.tactical.error_history else None
            ),
            "predicted_indoor_temp": (
                self.tactical._find_predicted_temp(schedule, now) if schedule else None
            ),
            "model_accuracy_mae": tactical_mae,
            "model_bias": tactical_bias,

            # Learning
            "model_corrections": {
                mode: accuracy_report[mode]["correction"]
                for mode in ("cool", "heat", "resist")
            },
            "model_alert": self._check_model_alert(accuracy_report),
            "solar_coefficient": self.solar_adjuster.solar_coefficient,

            # Kalman filter / adaptive model
            "kalman_confidence": self.estimator.confidence,
            "kalman_r_value": self.estimator.R_value,
            "kalman_thermal_mass": self.estimator.thermal_mass,
            "kalman_cooling_capacity": float(self.estimator.x[6]),
            "kalman_heating_capacity": float(self.estimator.x[7]),
            "kalman_mass_temp": self.estimator.T_mass,
            "kalman_observations": self.estimator._n_obs,
            "using_adaptive_model": (
                self._use_adaptive
                and not self._use_greybox
                and self.estimator.confidence >= DEFAULT_MODEL_CONFIDENCE_THRESHOLD
            ),
            "using_greybox_model": (
                self._use_greybox
                and self.estimator.confidence >= DEFAULT_MODEL_CONFIDENCE_THRESHOLD
            ),
            "learning_active": self._is_learning_active(),
            "initialization_mode": self._initialization_mode,

            # Overrides
            "override_count_30d": override_stats.get("total_overrides_30d", 0),
            "override_pattern": override_stats.get("top_pattern"),

            # Occupancy
            "occupancy_mode": self.occupancy.get_effective_mode(
                self._occupancy_timeline or None
            ).value,

            # Diagnostics
            "forecast_deviation": self._compute_forecast_deviation(),
            "schedule_detail": self._build_schedule_detail(schedule),
            "forecast_detail": self._build_forecast_detail(schedule),

            # SensorHub diagnostics
            "outdoor_temp_info": self.sensor_hub.get_outdoor_temp_info(
                self.strategic.forecast_snapshot
            ),
            "indoor_temp_info": self.sensor_hub.get_indoor_temp_info(
                thermo_state.indoor_temp if thermo_state else None
            ),

            # Savings tracking — suppress unreliable values during learning
            # (tracker still records internally so data is ready when tier upgrades)
            "savings_kwh_today": (
                today_savings.total_saved_kwh
                if self.savings_tracker.accuracy_tier != TIER_LEARNING else None
            ),
            "savings_cost_today": (
                today_savings.total_saved_cost
                if self.savings_tracker.accuracy_tier != TIER_LEARNING else None
            ),
            "savings_co2_today_grams": (
                today_savings.total_saved_co2_grams
                if self.savings_tracker.accuracy_tier != TIER_LEARNING else None
            ),
            "baseline_kwh_today": today_savings.total_baseline_kwh,
            "actual_kwh_today": today_savings.total_actual_kwh,
            "worst_case_kwh_today": today_savings.total_worst_case_kwh,
            "savings_kwh_cumulative": (
                cumulative["kwh_saved"]
                if self.savings_tracker.accuracy_tier != TIER_LEARNING else None
            ),
            "savings_cost_cumulative": (
                cumulative["cost_saved"]
                if self.savings_tracker.accuracy_tier != TIER_LEARNING else None
            ),
            "savings_co2_cumulative_grams": (
                cumulative["co2_saved_grams"]
                if self.savings_tracker.accuracy_tier != TIER_LEARNING else None
            ),

            # Counterfactual digital twin — decomposed savings
            "runtime_savings_kwh_today": (
                today_savings.total_runtime_savings_kwh
                if self.savings_tracker.accuracy_tier != TIER_LEARNING else None
            ),
            "cop_savings_kwh_today": (
                today_savings.total_cop_savings_kwh
                if self.savings_tracker.accuracy_tier != TIER_LEARNING else None
            ),
            "rate_arbitrage_savings_today": (
                today_savings.total_rate_arbitrage_savings
                if self.savings_tracker.accuracy_tier != TIER_LEARNING else None
            ),
            "carbon_shift_savings_today": (
                today_savings.total_saved_co2_grams - sum(
                    h.saved_co2_grams or 0 for h in today_savings.hours
                    if h.runtime_savings_kwh > 0
                ) if today_savings.total_saved_co2_grams
                and self.savings_tracker.accuracy_tier != TIER_LEARNING
                else None
            ),

            # COP comparison
            "baseline_avg_cop": (
                today_savings.avg_baseline_cop
                if self.savings_tracker.accuracy_tier != TIER_LEARNING else None
            ),
            "optimized_avg_cop": today_savings.avg_actual_cop,
            "cop_improvement_pct": (
                round(
                    (today_savings.avg_actual_cop - today_savings.avg_baseline_cop)
                    / today_savings.avg_baseline_cop * 100,
                    1,
                )
                if today_savings.avg_actual_cop and today_savings.avg_baseline_cop
                and today_savings.avg_baseline_cop > 0
                and self.savings_tracker.accuracy_tier != TIER_LEARNING
                else None
            ),

            # Comfort comparison
            "comfort_hours_gained": (
                today_savings.comfort_hours_gained
                if self.savings_tracker.accuracy_tier != TIER_LEARNING else None
            ),
            "baseline_comfort_violations": (
                today_savings.baseline_comfort_violations
                if self.savings_tracker.accuracy_tier != TIER_LEARNING else None
            ),
            "baseline_avg_indoor_temp": (
                round(
                    sum(
                        h.baseline_indoor_temp for h in today_savings.hours
                        if h.baseline_indoor_temp is not None
                    ) / max(1, sum(
                        1 for h in today_savings.hours
                        if h.baseline_indoor_temp is not None
                    )),
                    1,
                )
                if any(h.baseline_indoor_temp is not None for h in today_savings.hours)
                and self.savings_tracker.accuracy_tier != TIER_LEARNING
                else None
            ),

            # Baseline confidence and accuracy tier
            "baseline_confidence": round(self.baseline_capture.confidence * 100, 0),
            "baseline_sample_days": self.baseline_capture.sample_days,
            "baseline_capture_method": (
                self.baseline_capture.template.capture_method
                if self.baseline_capture.template else None
            ),
            "baseline_days_remaining": self.baseline_capture.days_remaining,
            "savings_accuracy_tier": self.savings_tracker.accuracy_tier,

            # Performance profiler
            "profiler_confidence": round(self.profiler.confidence() * 100, 0),
            "profiler_active": self.profiler.confidence() >= 0.7,
            "profiler_observations": self.profiler.total_observations,

            # Calendar occupancy / pre-conditioning
            "occupancy_forecast_source": (
                "calendar" if self.calendar_occupancy and self._occupancy_timeline
                else "reactive"
            ),
            "occupancy_timeline_segments": len(self._occupancy_timeline),
            "next_occupancy_transition": self._next_transition_info(),
            "precondition_status": self._precondition_status(),
            "precondition_plan": self._precondition_plan_info(),

            # Source health diagnostics
            "source_health": self._build_source_health(thermo_state),

            # Environment context for panel
            "electricity_rate": self.sensor_hub.read_electricity_rate(),
            "co2_intensity": self.sensor_hub.read_co2_intensity(),
            "wind_speed_mph": (
                r.value if (r := self.sensor_hub.read_wind_speed(
                    self.strategic.forecast_snapshot
                )) else None
            ),
            "solar_irradiance": (
                r.value if (r := self.sensor_hub.read_solar_irradiance())
                else None
            ),

            # Weather forecast for panel (enables chart during learning)
            "weather_forecast": self._build_weather_forecast(),
        }

    def _next_transition_info(self) -> dict[str, str] | None:
        """Get info about the next occupancy transition from the timeline."""
        if not self._occupancy_timeline or not self.calendar_occupancy:
            return None
        for direction in [("away", "home"), ("home", "away")]:
            t = self.calendar_occupancy.get_next_transition(
                self._occupancy_timeline, direction[0], direction[1]
            )
            if t is not None:
                return {
                    "time": t.isoformat(),
                    "type": f"{direction[0]}_to_{direction[1]}",
                }
        return None

    def _precondition_status(self) -> str:
        """Return pre-conditioning status string."""
        if self._precondition_plan is None:
            return "idle" if self.calendar_occupancy else "not_configured"
        if self._phase == PHASE_PRECONDITIONING:
            return "active"
        if self._departure_detected:
            return "departure_detected"
        return "scheduled"

    def _precondition_plan_info(self) -> dict[str, Any] | None:
        """Serialize the current pre-conditioning plan for sensors."""
        plan = self._precondition_plan
        if plan is None:
            return None
        return {
            "scheduled_start": plan.start_time.isoformat(),
            "arrival_time": plan.arrival_time.isoformat(),
            "arrival_source": plan.arrival_source,
            "estimated_runtime_minutes": plan.estimated_runtime_minutes,
            "estimated_energy_kwh": plan.estimated_energy_kwh,
            "estimated_cost": plan.estimated_cost,
            "temperature_gap": plan.temperature_gap,
            "should_start_now": plan.should_start_now,
        }

    def _build_source_health(self, thermo_state=None) -> dict[str, Any]:
        """Build source health diagnostics for the SourceHealthSensor."""
        sources: dict[str, dict[str, Any]] = {}
        healthy_count = 0
        total_count = 0

        # Weather sources
        for i, entity_id in enumerate(self._weather_entity_ids):
            key = "weather_primary" if i == 0 else f"weather_fallback_{i}"
            total_count += 1
            state = self.hass.states.get(entity_id)
            if state and state.state not in ("unknown", "unavailable"):
                status = "ok"
                if self._last_forecast_source == entity_id:
                    status = "active"
                healthy_count += 1
            else:
                status = "unavailable"
            sources[key] = {"entity_id": entity_id, "status": status}

        # Thermostat
        total_count += 1
        if thermo_state and thermo_state.available:
            sources["thermostat"] = {
                "entity_id": self.climate_entity_id,
                "status": "ok",
                "unavailable_count": self._thermostat_unavailable_count,
            }
            healthy_count += 1
        else:
            sources["thermostat"] = {
                "entity_id": self.climate_entity_id,
                "status": "unavailable",
                "unavailable_count": self._thermostat_unavailable_count,
            }

        # Outdoor temp provenance
        total_count += 1
        outdoor_temp = self.sensor_hub.read_outdoor_temp(
            forecast_snapshot=self.strategic.forecast_snapshot
        )
        if outdoor_temp:
            sources["outdoor_temp"] = {
                "source": outdoor_temp.source,
                "stale": outdoor_temp.stale,
                "status": "stale" if outdoor_temp.stale else "ok",
            }
            if not outdoor_temp.stale:
                healthy_count += 1
        else:
            sources["outdoor_temp"] = {"source": "none", "stale": True, "status": "unavailable"}

        # Outdoor humidity provenance
        total_count += 1
        outdoor_humidity = self.sensor_hub.read_outdoor_humidity(
            forecast_snapshot=self.strategic.forecast_snapshot
        )
        if outdoor_humidity:
            sources["outdoor_humidity"] = {
                "source": outdoor_humidity.source,
                "stale": outdoor_humidity.stale,
                "status": "stale" if outdoor_humidity.stale else "ok",
            }
            if not outdoor_humidity.stale:
                healthy_count += 1
        else:
            sources["outdoor_humidity"] = {"source": "none", "stale": True, "status": "unavailable"}

        # Power provenance
        total_count += 1
        power = self.sensor_hub.read_power_draw()
        if power is not None:
            sources["power"] = {"status": "ok", "value": power}
            healthy_count += 1
        else:
            sources["power"] = {"status": "unavailable", "value": None}

        return {
            "healthy": healthy_count,
            "total": total_count,
            "status": "healthy" if healthy_count == total_count else "degraded",
            "sources": sources,
        }
