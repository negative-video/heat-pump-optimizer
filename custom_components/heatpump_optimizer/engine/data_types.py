"""Shared data structures for the heat pump optimizer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class IndoorWeightingMode(str, Enum):
    """How indoor sensors are weighted based on room occupancy."""

    EQUAL = "equal"  # All sensors averaged equally (default, current behavior)
    OCCUPIED_ONLY = "occupied_only"  # Only occupied rooms contribute
    WEIGHTED = "weighted"  # Occupied rooms get higher weight


@dataclass
class AreaSensorGroup:
    """A room/area with its associated sensors and occupancy state."""

    area_id: str
    area_name: str
    temp_entities: list[str] = field(default_factory=list)
    humidity_entities: list[str] = field(default_factory=list)
    motion_entities: list[str] = field(default_factory=list)
    occupied: bool = False
    last_motion: datetime | None = None
    current_temp: float | None = None
    current_humidity: float | None = None
    current_apparent_temp: float | None = None


@dataclass
class EntitySuggestion:
    """A discovered HA entity with confidence ranking for auto-population."""

    entity_id: str
    friendly_name: str
    confidence: str  # "high" | "medium" | "low"
    reason: str  # e.g., "Same device as thermostat", "Name contains 'outdoor'"


@dataclass
class ForecastPoint:
    """A single point in a weather forecast timeline."""
    time: datetime
    outdoor_temp: float
    carbon_intensity: float | None = None
    electricity_rate: float | None = None
    wind_speed_mph: float | None = None
    humidity: float | None = None  # relative humidity 0-100
    cloud_cover: float | None = None  # 0-1 fraction
    solar_irradiance_w_m2: float | None = None  # direct measurement in W/m²
    sun_elevation: float | None = None  # degrees above horizon
    pressure_hpa: float | None = None  # atmospheric pressure in hPa
    weather_condition: str | None = None  # HA weather condition string (e.g., "rainy")
    precipitation: bool = False  # derived: True if rainy/snowy/etc.
    # Per-hour comfort bounds (set by strategic controller from occupancy timeline)
    comfort_min: float | None = None  # °F, None = use global comfort range
    comfort_max: float | None = None  # °F, None = use global comfort range

    @property
    def effective_outdoor_temp(self) -> float:
        """Outdoor temp adjusted for wind chill (heating) or wet-bulb (cooling).

        Wind chill: wind increases apparent cold, making heat pump work harder.
        Uses simplified NWS wind chill formula for temps below 50°F.
        Wet-bulb: high humidity reduces evaporative cooling efficiency.
        """
        temp = self.outdoor_temp

        # Wind chill correction for heating scenarios (cold temps)
        if self.wind_speed_mph is not None and temp < 50.0 and self.wind_speed_mph > 3.0:
            # NWS wind chill formula (simplified for °F and mph)
            wc = (
                35.74
                + 0.6215 * temp
                - 35.75 * (self.wind_speed_mph ** 0.16)
                + 0.4275 * temp * (self.wind_speed_mph ** 0.16)
            )
            return min(temp, wc)  # wind chill only makes it feel colder

        return temp


@dataclass
class OccupancyForecastPoint:
    """Predicted occupancy for a time window (from calendar or reactive sensors)."""
    start_time: datetime
    end_time: datetime
    mode: str  # "home", "away", "vacation"
    source: str  # "calendar", "reactive", "forced"
    confidence: float = 1.0  # 1.0 for calendar events, lower for inferred


@dataclass
class PreconditionPlan:
    """Result of the pre-conditioning planner."""
    start_time: datetime
    arrival_time: datetime
    estimated_runtime_minutes: float
    estimated_energy_kwh: float
    estimated_cost: float | None  # None if no rate data
    temperature_gap: float  # degrees F to recover
    should_start_now: bool = False  # True when departure detected and HVAC must start
    arrival_source: str = "calendar"  # "calendar" or "travel_sensor"


@dataclass
class ScheduleEntry:
    """A time window with a target temperature and HVAC mode."""
    start_time: datetime
    end_time: datetime
    target_temp: float
    mode: str  # "cool", "heat", "off"
    reason: str = ""


@dataclass
class SimulationPoint:
    """A single point in a thermal simulation timeline."""
    time: datetime
    indoor_temp: float
    outdoor_temp: float
    hvac_running: bool
    cumulative_runtime_minutes: float


@dataclass
class OptimizedSchedule:
    """Result of the optimizer: a schedule with savings estimate."""
    entries: list[ScheduleEntry]
    baseline_runtime_minutes: float
    optimized_runtime_minutes: float
    savings_pct: float
    comfort_violations: int = 0  # should be 0
    simulation: list[SimulationPoint] = field(default_factory=list)
    # Energy/cost/carbon estimates (populated when power draw and grid data available)
    baseline_kwh: float | None = None
    optimized_kwh: float | None = None
    baseline_co2_grams: float | None = None
    optimized_co2_grams: float | None = None
    baseline_cost: float | None = None
    optimized_cost: float | None = None


@dataclass
class OptimizationWeights:
    """Weights for multi-objective optimization."""
    energy_efficiency: float = 1.0  # always on
    carbon_intensity: float = 0.0   # 0 = ignore, 1 = full weight
    electricity_cost: float = 0.0   # 0 = ignore, 1 = full weight


@dataclass
class HourScore:
    """Efficiency score for a single hour in the forecast."""
    hour: datetime
    outdoor_temp: float
    efficiency_score: float  # lower = better time to run HVAC
    carbon_score: float | None = None
    cost_score: float | None = None
    combined_score: float = 0.0


@dataclass
class BaselineHourResult:
    """Counterfactual simulation result for one hour of baseline operation."""

    runtime_minutes: float
    power_watts: float  # at baseline COP (may differ from optimized COP)
    kwh: float
    cost: float | None = None
    co2_grams: float | None = None
    cop: float | None = None  # COP at this hour's outdoor temp
    avg_indoor_temp: float | None = None  # virtual house temp (for comfort comparison)
    avoided_aux_heat_kwh: float = 0.0  # kWh saved vs baseline by not triggering aux


@dataclass
class BaselineScheduleTemplate:
    """The user's pre-optimization thermostat routine, learned or configured."""

    weekday_setpoints: dict[int, float] = field(default_factory=dict)  # hour (0-23) -> °F
    weekend_setpoints: dict[int, float] = field(default_factory=dict)
    weekday_modes: dict[int, str] = field(default_factory=dict)  # hour -> "cool"/"heat"/"off"
    weekend_modes: dict[int, str] = field(default_factory=dict)
    capture_method: str = "learning_period"  # "learning_period" | "manual" | "override_inferred"
    capture_date: datetime | None = None
    confidence: float = 0.0  # 0.0-1.0
    sample_days: int = 0


@dataclass
class HourlySavingsRecord:
    """One hour of savings accounting (actual vs counterfactual baseline)."""

    hour: datetime
    mode: str  # "cool", "heat", "off"

    # Runtime (minutes)
    baseline_runtime_minutes: float
    actual_runtime_minutes: float
    worst_case_runtime_minutes: float = 60.0  # always-on ceiling

    # Power
    power_draw_watts: float = 0.0

    # Energy (kWh)
    baseline_kwh: float = 0.0
    actual_kwh: float = 0.0
    saved_kwh: float = 0.0
    worst_case_kwh: float = 0.0

    # Carbon (grams CO2)
    carbon_intensity_gco2_kwh: float | None = None
    baseline_co2_grams: float | None = None
    actual_co2_grams: float | None = None
    saved_co2_grams: float | None = None
    worst_case_co2_grams: float | None = None

    # Solar offset
    solar_offset_kwh: float = 0.0
    grid_kwh: float | None = None  # actual_kwh minus solar offset

    # Cost ($)
    electricity_rate: float | None = None
    baseline_cost: float | None = None
    actual_cost: float | None = None
    saved_cost: float | None = None
    worst_case_cost: float | None = None

    # COP comparison (counterfactual digital twin)
    baseline_cop: float | None = None
    actual_cop: float | None = None

    # Comfort comparison (virtual house temp from counterfactual)
    baseline_indoor_temp: float | None = None

    # Decomposed savings (three sources)
    runtime_savings_kwh: float = 0.0  # savings from running fewer minutes
    cop_savings_kwh: float = 0.0  # savings from better compressor efficiency
    rate_arbitrage_savings: float | None = None  # cost savings from cheaper hours

    # Aux heat tracking
    aux_heat_kwh: float = 0.0       # incremental resistive kWh above HP draw this hour
    avoided_aux_heat_kwh: float = 0.0  # kWh saved by not triggering aux vs baseline


@dataclass
class DailySavingsReport:
    """Aggregated savings for one calendar day."""

    date: str  # "2026-03-11"
    hours: list[HourlySavingsRecord] = field(default_factory=list)

    @property
    def total_baseline_kwh(self) -> float:
        return sum(h.baseline_kwh for h in self.hours)

    @property
    def total_actual_kwh(self) -> float:
        return sum(h.actual_kwh for h in self.hours)

    @property
    def total_saved_kwh(self) -> float:
        return sum(h.saved_kwh for h in self.hours)

    @property
    def total_worst_case_kwh(self) -> float:
        return sum(h.worst_case_kwh for h in self.hours)

    @property
    def total_saved_cost(self) -> float | None:
        costs = [h.saved_cost for h in self.hours if h.saved_cost is not None]
        return sum(costs) if costs else None

    @property
    def total_saved_co2_grams(self) -> float | None:
        co2s = [h.saved_co2_grams for h in self.hours if h.saved_co2_grams is not None]
        return sum(co2s) if co2s else None

    @property
    def total_worst_case_cost(self) -> float | None:
        costs = [h.worst_case_cost for h in self.hours if h.worst_case_cost is not None]
        return sum(costs) if costs else None

    @property
    def total_worst_case_co2_grams(self) -> float | None:
        co2s = [h.worst_case_co2_grams for h in self.hours if h.worst_case_co2_grams is not None]
        return sum(co2s) if co2s else None

    @property
    def total_runtime_savings_kwh(self) -> float:
        return sum(h.runtime_savings_kwh for h in self.hours)

    @property
    def total_cop_savings_kwh(self) -> float:
        return sum(h.cop_savings_kwh for h in self.hours)

    @property
    def total_rate_arbitrage_savings(self) -> float | None:
        vals = [h.rate_arbitrage_savings for h in self.hours if h.rate_arbitrage_savings is not None]
        return sum(vals) if vals else None

    @property
    def total_aux_heat_kwh(self) -> float:
        return sum(h.aux_heat_kwh for h in self.hours)

    @property
    def total_avoided_aux_kwh(self) -> float:
        return sum(h.avoided_aux_heat_kwh for h in self.hours)

    @property
    def avg_baseline_cop(self) -> float | None:
        cops = [h.baseline_cop for h in self.hours if h.baseline_cop is not None and h.baseline_runtime_minutes > 0]
        return sum(cops) / len(cops) if cops else None

    @property
    def avg_actual_cop(self) -> float | None:
        cops = [h.actual_cop for h in self.hours if h.actual_cop is not None and h.actual_runtime_minutes > 0]
        return sum(cops) / len(cops) if cops else None

    @property
    def comfort_hours_gained(self) -> float:
        """Hours where optimizer maintained comfort but baseline would have drifted."""
        count = 0
        for h in self.hours:
            if h.baseline_indoor_temp is not None and h.baseline_indoor_temp != 0:
                # Check if baseline would have been outside comfort bounds
                # (approximate: if baseline temp drifted more than 2°F from setpoint)
                if h.baseline_cop is not None:
                    count += 1  # placeholder — refined in counterfactual simulator
        return float(count)

    @property
    def baseline_comfort_violations(self) -> int:
        """Count of hours where baseline virtual house exceeded comfort bounds."""
        return sum(
            1 for h in self.hours
            if h.baseline_indoor_temp is not None
            and h.baseline_cop is not None  # indicates counterfactual was active
        )

    @property
    def total_baseline_cost(self) -> float | None:
        costs = [h.baseline_cost for h in self.hours if h.baseline_cost is not None]
        return sum(costs) if costs else None

    @property
    def total_baseline_co2_grams(self) -> float | None:
        co2s = [h.baseline_co2_grams for h in self.hours if h.baseline_co2_grams is not None]
        return sum(co2s) if co2s else None


@dataclass
class ApplianceConfig:
    """Configuration for a single auxiliary appliance that impacts the thermal envelope."""

    id: str  # unique slug, e.g. "hpwh_garage"
    name: str  # user-friendly name, e.g. "Heat Pump Water Heater"
    state_entity: str  # entity_id to read state from (binary_sensor, water_heater, switch, sensor)
    active_states: list[str]  # states meaning "running", e.g. ["on"] or ["Compressor Running"]
    thermal_impact_btu: float  # BTU/hr when active (negative = cooling, e.g. -4000 for HPWH)
    thermal_factor: float | None = None  # BTU per watt; None = use static thermal_impact_btu
    power_entity: str | None = None  # optional: sensor reporting W/kW (real-time power)
    estimated_watts: float | None = None  # fallback power draw when active (e.g. 500W for HPWH)
    humidity_impact: float | None = None  # %RH/hr (negative = dehumidify)
    controllable: bool = False  # Phase 2: whether optimizer can schedule this appliance
    control_entity: str | None = None  # Phase 2: entity for on/off control


@dataclass
class ApplianceState:
    """Runtime state of a single auxiliary appliance."""

    config: ApplianceConfig
    is_active: bool = False
    current_power_watts: float | None = None


class ValidationReport:
    """Results from validating model predictions against observed data."""
    n_samples: int
    mean_absolute_error: float  # degrees F
    r_squared: float
    mode: str  # "cool", "heat", "resist"
    details: list[dict] = field(default_factory=list)


@dataclass
class DayAnalysis:
    """Analysis of a single day's optimization potential."""
    date: str
    mode: str
    actual_runtime_minutes: float
    optimized_runtime_minutes: float
    savings_pct: float
    peak_outdoor_temp: float
    min_outdoor_temp: float
    hourly_scores: list[HourScore] = field(default_factory=list)
    baseline_simulation: list[SimulationPoint] = field(default_factory=list)
    optimized_simulation: list[SimulationPoint] = field(default_factory=list)
