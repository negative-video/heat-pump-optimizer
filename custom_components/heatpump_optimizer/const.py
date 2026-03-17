"""Constants for the Heat Pump Optimizer integration."""

DOMAIN = "heatpump_optimizer"
VERSION = "0.1.8"
PLATFORMS = ["sensor", "binary_sensor", "switch"]

# Config keys
CONF_CLIMATE_ENTITY = "climate_entity"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_PROFILE_PATH = "profile_path"
CONF_INITIALIZATION_MODE = "initialization_mode"
CONF_MODEL_IMPORT_DATA = "model_import_data"
CONF_COMFORT_COOL_MIN = "comfort_cool_min"
CONF_COMFORT_COOL_MAX = "comfort_cool_max"
CONF_COMFORT_HEAT_MIN = "comfort_heat_min"
CONF_COMFORT_HEAT_MAX = "comfort_heat_max"
CONF_SAFETY_COOL_MAX = "safety_cool_max"
CONF_SAFETY_HEAT_MIN = "safety_heat_min"
CONF_OCCUPANCY_ENTITY = "occupancy_entity"

# Initialization modes
INIT_MODE_LEARNING = "learning"
INIT_MODE_BEESTAT = "beestat"
INIT_MODE_IMPORT = "import"

# Savings tracking config keys (optional — configured via options flow)
CONF_CO2_ENTITY = "co2_entity"
CONF_ELECTRICITY_RATE_ENTITY = "electricity_rate_entity"
CONF_ELECTRICITY_FLAT_RATE = "electricity_flat_rate"
CONF_HVAC_POWER_ENTITY = "hvac_power_entity"
CONF_HVAC_POWER_DEFAULT_WATTS = "hvac_power_default_watts"
CONF_CARBON_WEIGHT = "carbon_weight"
CONF_COST_WEIGHT = "cost_weight"

# Defaults — comfort optimization range (where the optimizer works when home)
DEFAULT_COMFORT_COOL_MIN = 72.0
DEFAULT_COMFORT_COOL_MAX = 78.0  # wider band = more savings opportunity
DEFAULT_COMFORT_HEAT_MIN = 62.0
DEFAULT_COMFORT_HEAT_MAX = 70.0

# Defaults — safety limits (absolute guardrails, never exceeded)
DEFAULT_SAFETY_COOL_MAX = 90.0
DEFAULT_SAFETY_HEAT_MIN = 45.0

DEFAULT_REOPTIMIZE_INTERVAL_HOURS = 4
DEFAULT_FORECAST_DEVIATION_THRESHOLD = 5.0  # °F
DEFAULT_OVERRIDE_GRACE_PERIOD_HOURS = 2
DEFAULT_STALE_FORECAST_HOURS = 6
DEFAULT_UPDATE_INTERVAL_MINUTES = 5
DEFAULT_HVAC_POWER_WATTS = 3500  # typical heat pump cooling draw
DEFAULT_CARBON_WEIGHT = 0.0
DEFAULT_COST_WEIGHT = 0.0

# Configurable behavior parameters
CONF_OVERRIDE_GRACE_PERIOD_HOURS = "override_grace_period_hours"
CONF_OPTIMIZATION_AGGRESSIVENESS = "optimization_aggressiveness"
CONF_REOPTIMIZE_INTERVAL_HOURS = "reoptimize_interval_hours"
CONF_AWAY_COMFORT_DELTA = "away_comfort_delta"
CONF_MAX_SETPOINT_CHANGE_PER_HOUR = "max_setpoint_change_per_hour"
CONF_THERMOSTAT_DEADBAND = "thermostat_deadband"
CONF_DWELL_TIME_MINUTES = "dwell_time_minutes"

DEFAULT_AWAY_COMFORT_DELTA = 4.0
DEFAULT_MAX_SETPOINT_CHANGE_PER_HOUR = 4.0
DEFAULT_THERMOSTAT_DEADBAND = 0.5  # °F
DEFAULT_DWELL_TIME_MINUTES = 15  # minutes between setpoint writes

# Aggressiveness presets
AGGRESSIVENESS_CONSERVATIVE = "conservative"
AGGRESSIVENESS_BALANCED = "balanced"
AGGRESSIVENESS_AGGRESSIVE = "aggressive"
DEFAULT_AGGRESSIVENESS = AGGRESSIVENESS_BALANCED

# Optimizer phases
PHASE_PRE_COOLING = "pre-cooling"
PHASE_PRE_HEATING = "pre-heating"
PHASE_COASTING = "coasting"
PHASE_MAINTAINING = "maintaining"
PHASE_PAUSED = "paused"
PHASE_SAFE_MODE = "safe_mode"
PHASE_IDLE = "idle"

# Storage keys
STORAGE_KEY = f"{DOMAIN}_data"
STORAGE_VERSION = 1

# Kalman filter / adaptive model
CONF_USE_ADAPTIVE_MODEL = "use_adaptive_model"
DEFAULT_MODEL_CONFIDENCE_THRESHOLD = 0.5  # fall back to Beestat below this

# Grey-box model (LP optimizer + Kalman filter)
CONF_USE_GREYBOX_MODEL = "use_greybox_model"

# HA event types
EVENT_OPTIMIZATION_COMPLETE = f"{DOMAIN}_optimization_complete"
EVENT_OVERRIDE_DETECTED = f"{DOMAIN}_override_detected"
EVENT_MODE_CHANGED = f"{DOMAIN}_mode_changed"
EVENT_MODEL_ALERT = f"{DOMAIN}_model_alert"
EVENT_SAFE_MODE_ENTERED = f"{DOMAIN}_safe_mode_entered"
EVENT_DISTURBED = f"{DOMAIN}_disturbed"
EVENT_CONFIDENCE_REACHED = f"{DOMAIN}_confidence_reached"
EVENT_ACCURACY_TIER_CHANGED = f"{DOMAIN}_accuracy_tier_changed"
EVENT_BASELINE_COMPLETE = f"{DOMAIN}_baseline_complete"

# Demand response
CONF_DEMAND_RESPONSE_ENTITY = "demand_response_entity"
DEFAULT_DEMAND_RESPONSE_DELTA_F = 3.0

# Time-of-use rate schedule
# Format: list of {"days": [0-6], "start_hour": int, "end_hour": int, "rate": float}
CONF_TOU_SCHEDULE = "tou_schedule"

# Environmental sensors (all optional — sensor slots)
CONF_OUTDOOR_TEMP_ENTITIES = "outdoor_temp_entities"
CONF_OUTDOOR_HUMIDITY_ENTITIES = "outdoor_humidity_entities"
CONF_WIND_SPEED_ENTITY = "wind_speed_entity"
CONF_SOLAR_IRRADIANCE_ENTITY = "solar_irradiance_entity"
CONF_BAROMETRIC_PRESSURE_ENTITY = "barometric_pressure_entity"
CONF_SUN_ENTITY = "sun_entity"

# Indoor sensors (all optional)
CONF_INDOOR_TEMP_ENTITIES = "indoor_temp_entities"
CONF_INDOOR_HUMIDITY_ENTITIES = "indoor_humidity_entities"

# Door/window contact sensors (optional — for infiltration modeling)
CONF_DOOR_WINDOW_ENTITIES = "door_window_entities"

# Buffer zone temperature sensors (optional — for boundary heat transfer)
CONF_ATTIC_TEMP_ENTITY = "attic_temp_entity"
CONF_CRAWLSPACE_TEMP_ENTITY = "crawlspace_temp_entity"

# Aux heat override (optional — for thermostats that don't report aux heat)
CONF_AUX_HEAT_OVERRIDE_ENTITY = "aux_heat_override_entity"

# Occupancy (multi-entity, replaces singular CONF_OCCUPANCY_ENTITY)
CONF_OCCUPANCY_ENTITIES = "occupancy_entities"
CONF_OCCUPANCY_DEBOUNCE_MINUTES = "occupancy_debounce_minutes"

# Energy/solar (all optional)
CONF_SOLAR_PRODUCTION_ENTITY = "solar_production_entity"
CONF_GRID_IMPORT_ENTITY = "grid_import_entity"
CONF_SOLAR_EXPORT_RATE_ENTITY = "solar_export_rate_entity"

# Sensor slot defaults
DEFAULT_SUN_ENTITY = "sun.sun"
DEFAULT_OCCUPANCY_DEBOUNCE_MINUTES = 5
DEFAULT_SENSOR_STALE_MINUTES = 15

# Calendar-based occupancy scheduling
CONF_CALENDAR_ENTITY = "calendar_entity"  # singular (backward compat)
CONF_CALENDAR_ENTITIES = "calendar_entities"  # plural (preferred)
CONF_CALENDAR_HOME_KEYWORDS = "calendar_home_keywords"
CONF_CALENDAR_AWAY_KEYWORDS = "calendar_away_keywords"
CONF_CALENDAR_DEFAULT_MODE = "calendar_default_mode"
CONF_PRECONDITIONING_BUFFER_MINUTES = "preconditioning_buffer_minutes"
DEFAULT_CALENDAR_HOME_KEYWORDS = ["WFH", "Work from Home", "Home", "Remote"]
DEFAULT_CALENDAR_AWAY_KEYWORDS = ["Office", "In-Person", "On-Site", "Work"]
DEFAULT_CALENDAR_DEFAULT_MODE = "home"
DEFAULT_PRECONDITIONING_BUFFER_MINUTES = 15

# Departure-aware pre-conditioning (optional, refines calendar-based plan)
CONF_DEPARTURE_ZONE = "departure_zone"  # singular (backward compat)
CONF_DEPARTURE_ZONES = "departure_zones"  # deprecated plural (replaced by profiles)
CONF_TRAVEL_TIME_SENSOR = "travel_time_sensor"  # singular (backward compat)
CONF_TRAVEL_TIME_SENSORS = "travel_time_sensors"  # deprecated plural (replaced by profiles)
CONF_DEPARTURE_TRIGGER_WINDOW_MINUTES = "departure_trigger_window_minutes"
DEFAULT_DEPARTURE_TRIGGER_WINDOW_MINUTES = 60

# Departure profiles — pairs each person with their departure zone + travel sensor
# Stored as JSON string: [{"person": "person.x", "zone": "zone.y", "travel_sensor": "sensor.z"}, ...]
CONF_DEPARTURE_PROFILES = "departure_profiles"

# Auxiliary appliances — appliances that impact the thermal envelope (e.g., HPWH, dryer)
# Stored as JSON string: [{"id": "hpwh", "name": "...", "state_entity": "...", ...}, ...]
CONF_AUXILIARY_APPLIANCES = "auxiliary_appliances"

# Calendar/pre-conditioning events
EVENT_PRECONDITIONING_START = f"{DOMAIN}_preconditioning_start"
EVENT_PRECONDITIONING_COMPLETE = f"{DOMAIN}_preconditioning_complete"
EVENT_OCCUPANCY_FORECAST_CHANGED = f"{DOMAIN}_occupancy_forecast_changed"
EVENT_CALENDAR_OVERRIDE = f"{DOMAIN}_calendar_override"

# Pre-conditioning phase
PHASE_PRECONDITIONING = "pre-conditioning"

# Room-aware occupancy-weighted indoor sensing (all optional)
CONF_INDOOR_WEIGHTING_MODE = "indoor_weighting_mode"
CONF_AREA_SENSOR_CONFIG = "area_sensor_config"
CONF_ROOM_OCCUPANCY_DEBOUNCE_MINUTES = "room_occupancy_debounce_minutes"
CONF_OCCUPIED_WEIGHT_MULTIPLIER = "occupied_weight_multiplier"

WEIGHTING_MODE_EQUAL = "equal"
WEIGHTING_MODE_OCCUPIED_ONLY = "occupied_only"
WEIGHTING_MODE_WEIGHTED = "weighted"

DEFAULT_INDOOR_WEIGHTING_MODE = WEIGHTING_MODE_EQUAL
DEFAULT_ROOM_OCCUPANCY_DEBOUNCE_MINUTES = 10
DEFAULT_OCCUPIED_WEIGHT_MULTIPLIER = 3.0
DEFAULT_SPIKE_HUMIDITY_THRESHOLD = 15.0  # % RH jump in 10 min
DEFAULT_SPIKE_TEMP_THRESHOLD = 5.0  # °F jump in 10 min
DEFAULT_SPIKE_WINDOW_MINUTES = 10
DEFAULT_SPIKE_HISTORY_MINUTES = 30

# History bootstrap
CONF_HISTORY_BOOTSTRAP_DAYS = "history_bootstrap_days"
DEFAULT_HISTORY_BOOTSTRAP_DAYS = 10

# EMA temperature smoothing
DEFAULT_EMA_ALPHA = 0.2  # weight for new reading (lower = more smoothing)

# Door/window debounce
DEFAULT_DOOR_WINDOW_DEBOUNCE_SECONDS = 120  # 2 minutes

# Setpoint switching penalty
DEFAULT_SWITCHING_PENALTY_WEIGHT = 0.3  # soft penalty for consecutive setpoint changes

# Resilience constants
CONF_WEATHER_ENTITIES = "weather_entities"
DEFAULT_FORECAST_CACHE_MAX_AGE_HOURS = 3
DEFAULT_THERMOSTAT_TOLERANCE_CYCLES = 3  # 15 min at 5-min intervals
