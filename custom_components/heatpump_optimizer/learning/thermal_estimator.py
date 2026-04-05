"""Extended Kalman Filter for online building thermal parameter estimation.

Models the building as a two-node RC thermal circuit (air + thermal mass)
and continuously estimates the physical parameters from thermostat readings:

  C_air · dT_air/dt  = (T_out - T_air)/R + (T_mass - T_air)/R_int + Q_hvac + (1-f_s)*Q_solar + Q_internal
                        + Q_attic + Q_crawlspace
  C_mass · dT_mass/dt = (T_air - T_mass)/R_int + f_s * Q_solar

where f_s = 0.3 (solar mass fraction -- sunlight through windows directly heats thermal mass).

State vector (9 elements):
  [T_air, T_mass, R_inv, R_int_inv, C_inv, C_mass_inv, Q_cool_base, Q_heat_base, solar_gain_btu]

The filter estimates building envelope resistance (R), internal coupling (R_int),
air and mass thermal capacitance (C, C_mass), HVAC capacity at a reference
temperature, and peak solar heat gain — continuously adapting parameters learned
from thermostat observations.

Additional environmental inputs (all optional, gracefully degrade to no-op):
- Occupancy count: scales internal heat gain (Q_internal)
- Door/window open count: applies infiltration penalty, pauses parameter learning
- Indoor humidity: adjusts sensible heat ratio in cooling mode
- Attic temperature: models duct loss and ceiling heat transfer
- Crawlspace temperature: models floor heat transfer
- Precipitation: applies evaporative cooling correction to envelope
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from .coefficient_store import CoefficientStore

_LOGGER = logging.getLogger(__name__)

# State vector indices
IDX_T_AIR = 0
IDX_T_MASS = 1
IDX_R_INV = 2       # 1/R  — envelope conductance (BTU/hr/°F)
IDX_R_INT_INV = 3   # 1/R_int — air↔mass coupling conductance
IDX_C_INV = 4       # 1/C_air — inverse air thermal capacitance
IDX_C_MASS_INV = 5  # 1/C_mass — inverse mass thermal capacitance
IDX_Q_COOL = 6      # Base cooling capacity at T_ref (BTU/hr)
IDX_Q_HEAT = 7      # Base heating capacity at T_ref (BTU/hr)
IDX_SOLAR_GAIN = 8  # Peak solar heat gain at clear-sky noon (BTU/hr)

N_STATES = 9

# Index of first learned parameter (used for learning-pause on open doors/windows)
_IDX_FIRST_PARAM = IDX_R_INV

# Reference temperature for HVAC capacity model
T_REF_F = 75.0

# COP degradation slopes (per °F deviation from T_ref)
# Cooling gets worse as outdoor temp rises above T_ref
ALPHA_COOL = 0.012  # ~1.2% capacity loss per °F above reference
# Heating gets worse as outdoor temp drops below T_ref
ALPHA_HEAT = 0.015  # ~1.5% capacity loss per °F below reference

# Default time step
DT_MINUTES = 5.0
DT_HOURS = DT_MINUTES / 60.0

# Solar gain scaling (BTU/hr per unit of clear_sky * sin(elevation))
DEFAULT_SOLAR_GAIN_BTU = 3000.0  # typical residential solar gain at peak

# Internal heat gain from occupants, appliances, and lighting (BTU/hr).
# Typical occupied home: 2 people (~400 BTU/hr) + appliances/electronics (~800 BTU/hr).
DEFAULT_INTERNAL_GAIN_BTU = 1200.0
# Occupancy-scaled components
_INTERNAL_GAIN_BASE_BTU = 800.0   # appliances/electronics (always present)
_INTERNAL_GAIN_PER_PERSON_BTU = 350.0  # ~350 BTU/hr per occupant

# Attic and crawlspace boundary heat transfer coefficients (BTU/hr/°F)
_K_ATTIC = 50.0       # ceiling conductance (typical insulated attic)
_K_CRAWLSPACE = 25.0  # floor conductance (typically better insulated than attic)
# Duct loss factor: fraction of HVAC capacity lost per °F of (T_attic - T_air)
_DUCT_LOSS_PER_F = 0.003

# Precipitation: evaporative cooling offset applied to outdoor temp (°F)
_PRECIPITATION_OFFSET_F = 3.0

# Wind infiltration coefficient: fractional increase in envelope leakage per mph.
# Typical residential: 2-3% per mph at moderate speeds, diminishing at high speeds.
_WIND_INFILTRATION_COEFF = 0.025

# Cap infiltration multiplier to prevent unrealistic heat loss with many
# open doors/windows. 3+ open contacts stacked linearly would exceed any
# physical air exchange rate; capping at 4× prevents the model from
# attributing all heat loss to infiltration and corrupting R-value learning.
_MAX_INFILTRATION_MULTIPLIER = 4.0

# Stack effect coefficient: buoyancy-driven infiltration from warm air rising
# out through upper-story cracks and drawing cold air in below.
# Per ASHRAE Fundamentals Ch. 26, stack-driven leakage scales with sqrt(ΔT).
# Coefficient of 0.02 per sqrt(°F) yields ~0.11 at 30°F delta, ~0.14 at 50°F —
# a modest contribution that compounds with wind infiltration.
_STACK_EFFECT_COEFF = 0.02

# Thermal-mass observability thresholds.  C_mass_inv is only observable when
# |T_mass − T_air| is in a moderate range: too small means equilibrium (no
# information), too large means the gap is likely a filter artifact (positive
# feedback: high C_mass → slow T_mass → larger gap → more learning → higher
# C_mass).  The bell-shaped gating peaks at _MASS_PEAK_OBS_DELTA and falls
# to zero at both ends.
_MASS_OBS_THRESHOLD = 0.5   # °F — below: unobservable (equilibrium), freeze
_MASS_PEAK_OBS_DELTA = 3.0  # °F — optimal observability, full Kalman gain
_MASS_MAX_OBS_DELTA = 8.0   # °F — above: likely diverged, freeze learning

# Physical constraint: thermal mass in a conditioned residence cannot
# realistically differ from air temperature by more than this.  Even heavy
# masonry with direct solar exposure rarely exceeds 5–8°F delta.  Values
# beyond this indicate filter divergence, not physical reality.
_MAX_MASS_AIR_DELTA_F = 8.0

# Maximum thermal-mass time constant (hours).  τ = C_mass / R_int_inv.
# Residential buildings: 4–48 hrs (light frame to heavy masonry).  Values
# beyond 72 hrs mean the filter is storing energy in a mode too slow to
# ever be validated against observed data.
_MAX_TAU_HOURS = 72.0

# Initial thermal mass time constant for cold start (hours).
# Must be short enough for T_mass to track daily temperature cycles;
# otherwise the mass acts as a phantom heat source/sink that confounds
# R_inv estimation.  24 hr means T_mass substantially tracks the diurnal
# cycle within one day instead of taking 3+ days at the old 72 hr value.
# The filter can learn a longer tau if the building warrants it.
_INITIAL_TAU_HOURS = 24.0

# Maximum fractional change in C_mass_inv per 5-minute EKF cycle.
# Physical thermal mass doesn't change between cycles; large jumps indicate
# the filter is fitting noise.  5 % per step ≈ 60 %/hr — fast enough for
# legitimate convergence, slow enough to prevent runaway.
_C_MASS_MAX_CHANGE_FRAC = 0.05

# Maximum fractional change in Q_cool/Q_heat per cycle when the user
# provided a tonnage rating.  2 % per step ≈ 24 %/hr — still converges
# over days, but prevents the 75 % collapse seen in early learning.
_Q_HVAC_MAX_CHANGE_FRAC = 0.02

# Maximum fractional change in R_inv per 5-minute EKF cycle.
# Envelope thermal resistance is a physical property that doesn't change
# between cycles.  1 % per step ≈ 12 %/hr — fast enough for convergence
# but prevents the R-value crashes (6.06 to 5.63 in 3 hours) seen when
# solar gain transitions or HVAC mode changes confound envelope estimation.
_R_INV_MAX_CHANGE_FRAC = 0.01

# Kalman gain attenuation for R_inv and R_int_inv during active HVAC.
# When HVAC is running, Q_hvac dominates the temperature change, making it
# hard to observe envelope characteristics separately.  Reducing the gain
# prevents HVAC-driven temperature changes from corrupting R estimates.
_R_INV_HVAC_GAIN_FACTOR = 0.2

# Fraction of solar gain that goes directly to thermal mass.
# In real buildings, sunlight through windows heats floors, interior walls,
# and furniture (thermal mass) directly, not just the air.  Without this,
# the EKF attributes evening thermal mass heat release to envelope leakiness,
# causing R-value to crash after every sunny day.
_SOLAR_MASS_FRACTION = 0.3

# Physical bounds for parameter clamping
BOUNDS = {
    IDX_R_INV: (0.01, 1.0),       # R: 1.0 to 100 °F·hr/BTU
    IDX_R_INT_INV: (0.5, 500.0),  # R_int: 0.002 to 2 — allows mass τ from ~20 hr to ~20,000 hr
    IDX_C_INV: (1e-5, 0.01),      # C_air: 100 to 100,000 BTU/°F
    IDX_C_MASS_INV: (3.3e-5, 0.001),  # C_mass: 1,000 to 30,000 BTU/°F
    IDX_Q_COOL: (5000, 80000),    # 5k to 80k BTU/hr
    IDX_Q_HEAT: (5000, 80000),
    IDX_SOLAR_GAIN: (500, 15000), # 500 to 15k BTU/hr peak solar
}


def _expand_matrix(mat: np.ndarray, new_diag_val: float) -> np.ndarray:
    """Expand an NxN matrix to (N+1)x(N+1) by appending a row/column of zeros
    with the given diagonal value. Used for state vector migration."""
    n = mat.shape[0]
    expanded = np.zeros((n + 1, n + 1))
    expanded[:n, :n] = mat
    expanded[n, n] = new_diag_val
    return expanded


@dataclass
class ThermalEstimator:
    """Extended Kalman Filter for building thermal parameter estimation.

    Call update() every 5 minutes with current sensor readings.
    The filter jointly estimates indoor air temperature, hidden thermal
    mass temperature, and 6 building/HVAC parameters.
    """

    # State vector and covariance
    x: np.ndarray = field(default_factory=lambda: np.zeros(N_STATES))
    P: np.ndarray = field(default_factory=lambda: np.eye(N_STATES))

    # Process noise covariance
    Q: np.ndarray = field(default_factory=lambda: np.eye(N_STATES))

    # Measurement noise variance (°F²).
    # Set to 0.5 (std ≈ 0.71°F) to account for thermostat quantization.
    # Most thermostats report integer °F, creating a staircase signal.
    # With R_meas=0.25 the filter over-reacts to ±1°F boundary crossings
    # and over-trusts during flat periods, causing parameter oscillation.
    R_meas: float = 0.5

    # Last observed temperature — used to detect quantization no-change
    _last_observed_temp: float | None = None

    # Last pre-update prediction — stored so predicted_indoor_temp sensor
    # shows what the model PREDICTED before seeing the observation, making
    # it consistent with prediction_error (innovation = observed - predicted).
    _last_predicted_temp: float | None = None

    # Innovation (prediction error) history for accuracy reporting
    _innovations: list[tuple[datetime, float]] = field(default_factory=list)
    _n_obs: int = 0
    _last_update: datetime | None = None
    _initialized: bool = False
    _P_initial: np.ndarray = field(default_factory=lambda: np.eye(N_STATES))

    # High-water mark for confidence — prevents user-facing metric from dropping
    _confidence_hwm: float = 0.0

    # Envelope area (ft²) — scales per-area R_inv to whole-building conductance
    _envelope_area: float = 2000.0

    # Current environmental conditions (set each update, used by _hvac_output)
    _current_wind_speed: float | None = None
    _current_humidity: float | None = None
    _current_pressure: float | None = None
    _current_indoor_humidity: float | None = None
    _current_people_count: int | None = None
    _current_open_doors_windows: int = 0
    _current_attic_temp: float | None = None
    _current_crawlspace_temp: float | None = None
    _current_precipitation: bool = False

    # Previous R_inv value for rate limiting (prevents wild swings)
    _prev_r_inv: float | None = None

    # Rolling R_inv min/max over the last 24h (288 obs at 5-min).
    # When the range exceeds 20%, R_inv is oscillating and the rate
    # limit is tightened further to dampen diurnal aliasing.
    _r_inv_recent: list[float] = field(default_factory=list)

    # Previous solar gain for solar transition gating (not persisted)
    _prev_q_solar: float | None = None

    # Last computed thermal load components (BTU/hr), populated by _predict_state
    _last_thermal_loads: dict = field(default_factory=dict)

    # Conditioned innovations: innovation + full thermal load context for the
    # coefficient calibrator.  72-hour rolling buffer, NOT persisted (rebuilds
    # from live data after restart, same as _innovations).
    _conditioned_innovations: list[dict] = field(default_factory=list)

    # Optional coefficient store — when set, the EKF reads calibrated
    # multipliers instead of raw module-level constants.
    _coeff_store: CoefficientStore | None = None

    # Previous C_mass_inv for per-cycle rate limiting (not persisted)
    _prev_c_mass_inv: float | None = None

    # Tonnage-prior tracking: when the user provides rated tonnage, we
    # rate-limit Q_cool/Q_heat drift so the filter respects the prior
    # during early learning.  _has_tonnage_prior is persisted.
    _has_tonnage_prior: bool = False

    # Whether profiler data has been injected as priors (persisted, one-time)
    _profiler_seeded: bool = False
    _prev_q_cool: float | None = None
    _prev_q_heat: float | None = None

    def __post_init__(self):
        if not self._initialized:
            self._setup_default_noise()

    def _setup_default_noise(self):
        """Configure process noise Q matrix.

        States (T_air, T_mass) get larger noise to absorb model mismatch.
        Parameters get small noise to allow slow drift (adaptation).
        """
        q_diag = np.array([
            0.01,    # T_air — moderate (sensor noise + model error)
            0.01,    # T_mass — moderate (tracks air temp via coupling)
            1e-8,    # R_inv — very slow drift
            1e-8,    # R_int_inv — very slow drift
            1e-10,   # C_inv — extremely slow (thermal mass doesn't change)
            1e-12,   # C_mass_inv — extremely slow
            1.0,     # Q_cool_base — moderate (filter condition, refrigerant)
            1.0,     # Q_heat_base — moderate
            1e-4,    # solar_gain_btu — moderate (changes with foliage/seasons)
        ])
        self.Q = np.diag(q_diag)

    # ── Initialization ──────────────────────────────────────────────

    @classmethod
    def cold_start(
        cls,
        indoor_temp: float = 72.0,
        tonnage: float | None = None,
        sqft: float | None = None,
    ) -> ThermalEstimator:
        """Initialize with conservative defaults.

        When ``tonnage`` is provided the Q_cool/Q_heat priors are set to the
        rated capacity (tons × 12,000 BTU/hr) and the initial covariance is
        narrowed to ±20%, dramatically reducing convergence time from weeks to
        days.  When ``sqft`` is provided the air thermal capacitance prior uses
        a 0.6 BTU/°F/ft² formula.

        Without either, the filter falls back to generic conservative defaults
        suitable for a ~2,000 ft² home, which will still converge within
        ~2 weeks of mixed-weather operation.
        """
        est = cls()

        # ── Capacity priors ──────────────────────────────────────────
        if tonnage is not None:
            q_cool = tonnage * 12000.0        # rated BTU/hr
            q_heat = tonnage * 12000.0 * 1.1  # ~110% of cooling rating at 47°F outdoor
            q_cool_var = (0.10 * q_cool) ** 2  # ±10% SD — trust user-provided tonnage
            q_heat_var = (0.10 * q_heat) ** 2
        else:
            q_cool = 20000.0   # ~1.7 ton generic default
            q_heat = 18000.0
            q_cool_var = 1e8   # very uncertain — let filter converge freely
            q_heat_var = 1e8

        # ── Thermal mass priors ──────────────────────────────────────
        if sqft is not None:
            sqft = max(300.0, min(10000.0, sqft))
            est._envelope_area = sqft
            c_air = 0.6 * sqft           # BTU/°F
            c_inv = 1.0 / c_air
            c_inv_var = (0.30 * c_inv) ** 2  # ±30% SD
        else:
            c_inv = 0.001    # C ≈ 1000 BTU/°F (~2000 ft² default)
            c_inv_var = 1e-4

        est.x = np.array([
            indoor_temp,  # T_air
            indoor_temp,  # T_mass (assume equilibrium at start)
            0.10,         # R_inv → R ≈ 10 °F·hr/BTU (moderate insulation)
            50.0,         # R_int_inv → R_int ≈ 0.02 (mass τ ≈ 72 hr at default C_mass)
            c_inv,        # C_inv
            1.0 / (_INITIAL_TAU_HOURS * 50.0),  # C_mass_inv → τ = 24 hr at R_int_inv=50
            q_cool,       # Q_cool_base
            q_heat,       # Q_heat_base
            DEFAULT_SOLAR_GAIN_BTU,  # solar_gain_btu ≈ 3000 BTU/hr
        ])
        est.P = np.diag([
            0.1,        # T_air — we trust the thermostat
            25.0,       # T_mass — very uncertain (hidden state)
            0.01,       # R_inv — wide range possible
            100.0,      # R_int_inv — wide range to explore coupling strength
            c_inv_var,  # C_inv — tighter if sqft known
            1e-6,       # C_mass_inv
            q_cool_var, # Q_cool_base — tight if tonnage known, open otherwise
            q_heat_var, # Q_heat_base
            1e6,        # solar_gain_btu — uncertain without data
        ])
        est._P_initial = est.P.copy()
        est._prev_r_inv = float(est.x[IDX_R_INV])
        est._prev_c_mass_inv = float(est.x[IDX_C_MASS_INV])
        est._prev_q_cool = float(est.x[IDX_Q_COOL])
        est._prev_q_heat = float(est.x[IDX_Q_HEAT])
        est._initialized = True
        est._setup_default_noise()

        # When tonnage is known, reduce process noise for capacity states
        # so the filter respects the user-provided rating during early learning.
        if tonnage is not None:
            est._has_tonnage_prior = True
            est.Q[IDX_Q_COOL, IDX_Q_COOL] = 0.01
            est.Q[IDX_Q_HEAT, IDX_Q_HEAT] = 0.01

        return est

    # ── EKF Update ──────────────────────────────────────────────────

    def update(
        self,
        observed_temp: float,
        outdoor_temp: float,
        hvac_mode: str,
        hvac_running: bool,
        cloud_cover: float | None = None,
        sun_elevation: float | None = None,
        dt_hours: float = DT_HOURS,
        wind_speed_mph: float | None = None,
        humidity: float | None = None,
        pressure_hpa: float | None = None,
        indoor_humidity: float | None = None,
        people_home_count: int | None = None,
        open_door_window_count: int = 0,
        attic_temp: float | None = None,
        crawlspace_temp: float | None = None,
        precipitation: bool = False,
        appliance_btu: float = 0.0,
        aux_resistive_btu_hr: float = 0.0,
        measurement_noise_scale: float = 1.0,
        uv_index: float | None = None,
        solar_irradiance_w_m2: float | None = None,
    ) -> float:
        """Run one EKF predict-update cycle.

        Args:
            observed_temp: Indoor temperature from thermostat (°F).
            outdoor_temp: Current outdoor temperature (°F).
            hvac_mode: "cool", "heat", or "off"/"resist".
            hvac_running: Whether HVAC compressor is currently active.
            cloud_cover: 0.0 (clear) to 1.0 (overcast), or None.
            sun_elevation: Degrees above horizon, or None.
            dt_hours: Time step in hours (default 5 min).
            wind_speed_mph: Wind speed in mph, or None.
            humidity: Outdoor relative humidity 0-100, or None.
            pressure_hpa: Atmospheric pressure in hPa, or None.
            indoor_humidity: Indoor relative humidity 0-100, or None.
            people_home_count: Number of people currently home, or None.
            open_door_window_count: Number of doors/windows currently open.
            attic_temp: Attic temperature in °F, or None.
            crawlspace_temp: Crawlspace temperature in °F, or None.
            precipitation: Whether it is currently raining/snowing.
            appliance_btu: Net BTU/hr from auxiliary appliances (negative = cooling).
            aux_resistive_btu_hr: Known BTU/hr from aux/emergency resistive heat strip.
                Computed as (total_circuit_watts - hp_baseline_watts) * 3.412 when aux
                is active. Injected as an exogenous thermal load so the EKF correctly
                attributes only the heat pump's contribution to IDX_Q_HEAT.
            measurement_noise_scale: Multiplier for R_meas (default 1.0 = no change).
                Set >1 when the indoor temperature sensor is suspected unreliable (e.g.
                thermostat satellite blending). When >1, parameter-learning rows of the
                Kalman gain are also zeroed to prevent bad measurements from corrupting
                building thermal estimates.
            uv_index: UV index from weather integration (0-15), or None.
            solar_irradiance_w_m2: Direct or panel-derived solar irradiance in W/m², or None.

        Returns:
            Innovation (prediction error before update) in °F.
        """
        # Store environmental conditions for _hvac_output and _predict_state
        self._current_wind_speed = wind_speed_mph
        self._current_humidity = humidity
        self._current_pressure = pressure_hpa
        self._current_indoor_humidity = indoor_humidity
        self._current_people_count = people_home_count
        self._current_open_doors_windows = open_door_window_count
        self._current_attic_temp = attic_temp
        self._current_crawlspace_temp = crawlspace_temp
        self._current_precipitation = precipitation
        self._current_appliance_btu = appliance_btu
        self._current_aux_resistive_btu = aux_resistive_btu_hr
        self._current_uv_index = uv_index
        self._current_solar_irradiance = solar_irradiance_w_m2

        # ── PREDICT ──────────────────────────────────────────────
        x_pred = self._predict_state(
            self.x, outdoor_temp, hvac_mode, hvac_running,
            cloud_cover, sun_elevation, dt_hours,
        )
        F = self._jacobian(
            self.x, outdoor_temp, hvac_mode, hvac_running,
            cloud_cover, sun_elevation, dt_hours,
        )

        # ── Process noise gating ────────────────────────────────
        # Gate HVAC capacity process noise by observability to prevent
        # covariance leakage: unobserved states (e.g. Q_cool during heating
        # season) drift through off-diagonal P coupling, causing systematic
        # parameter drift toward clamp bounds.
        Q_effective = self.Q.copy()
        if not (hvac_running and hvac_mode == "cool"):
            Q_effective[IDX_Q_COOL, IDX_Q_COOL] = 0.0
        if not (hvac_running and hvac_mode == "heat"):
            Q_effective[IDX_Q_HEAT, IDX_Q_HEAT] = 0.0

        # Gate thermal-mass process noise when |T_mass − T_air| is small.
        # The Jacobian entry for C_mass_inv ≈ −R_int_inv·(T_mass−T_air)·dt
        # vanishes near equilibrium, so the observation carries no information
        # about thermal mass and unchecked covariance growth causes runaway.
        mass_air_delta = abs(x_pred[IDX_T_MASS] - x_pred[IDX_T_AIR])
        if mass_air_delta < _MASS_OBS_THRESHOLD:
            Q_effective[IDX_C_MASS_INV, IDX_C_MASS_INV] = 0.0

        P_pred = F @ self.P @ F.T + Q_effective

        # Zero cross-correlations for unobserved HVAC capacity states
        # to prevent covariance leakage from dragging them via Kalman gain.
        if not (hvac_running and hvac_mode == "cool"):
            P_pred[IDX_Q_COOL, :] = 0.0
            P_pred[:, IDX_Q_COOL] = 0.0
            P_pred[IDX_Q_COOL, IDX_Q_COOL] = self.P[IDX_Q_COOL, IDX_Q_COOL]
        if not (hvac_running and hvac_mode == "heat"):
            P_pred[IDX_Q_HEAT, :] = 0.0
            P_pred[:, IDX_Q_HEAT] = 0.0
            P_pred[IDX_Q_HEAT, IDX_Q_HEAT] = self.P[IDX_Q_HEAT, IDX_Q_HEAT]

        # Same pattern for C_mass_inv: zero cross-correlations when
        # thermal-mass observability is poor.
        if mass_air_delta < _MASS_OBS_THRESHOLD:
            P_pred[IDX_C_MASS_INV, :] = 0.0
            P_pred[:, IDX_C_MASS_INV] = 0.0
            P_pred[IDX_C_MASS_INV, IDX_C_MASS_INV] = self.P[
                IDX_C_MASS_INV, IDX_C_MASS_INV
            ]

        # ── UPDATE ───────────────────────────────────────────────
        # Observation model: z = H @ x = T_air
        H = np.zeros((1, N_STATES))
        H[0, IDX_T_AIR] = 1.0

        # Innovation
        z = observed_temp
        z_pred = x_pred[IDX_T_AIR]
        self._last_predicted_temp = float(z_pred)
        innovation = z - z_pred

        # ── Quantization-aware measurement noise ────────────────
        # Most thermostats report integer °F, creating a staircase signal.
        # When the observation hasn't changed, the innovation is artificially
        # small (the sensor just hasn't crossed a boundary yet). Freeze
        # parameter learning during these "no-change" steps to prevent the
        # filter from shrinking P based on false confirmation.
        temp_unchanged = (
            self._last_observed_temp is not None
            and observed_temp == self._last_observed_temp
        )
        self._last_observed_temp = observed_temp

        # Innovation covariance — scale R_meas when sensor trust is reduced
        effective_R = self.R_meas * measurement_noise_scale
        S = H @ P_pred @ H.T + effective_R
        S_scalar = float(S[0, 0])

        # Kalman gain
        K = P_pred @ H.T / S_scalar  # (N,1)

        # Bell-shaped C_mass_inv observability gating.  The Jacobian entry
        # for C_mass_inv scales with |T_mass − T_air|, so the parameter is
        # unobservable near equilibrium (small delta).  But large deltas are
        # equally suspect — they indicate the filter has diverged, creating
        # a positive feedback loop (high C_mass → slow T_mass → larger gap
        # → more learning → higher C_mass).  Peak learning at moderate delta.
        if mass_air_delta < _MASS_OBS_THRESHOLD:
            obs_factor = 0.0
        elif mass_air_delta < _MASS_PEAK_OBS_DELTA:
            obs_factor = (mass_air_delta - _MASS_OBS_THRESHOLD) / (
                _MASS_PEAK_OBS_DELTA - _MASS_OBS_THRESHOLD
            )
        elif mass_air_delta < _MASS_MAX_OBS_DELTA:
            obs_factor = 1.0 - (mass_air_delta - _MASS_PEAK_OBS_DELTA) / (
                _MASS_MAX_OBS_DELTA - _MASS_PEAK_OBS_DELTA
            )
        else:
            obs_factor = 0.0  # diverged — freeze C_mass learning entirely
        K[IDX_C_MASS_INV, :] *= obs_factor

        # R_inv observability gating during active HVAC.  When HVAC is
        # running, Q_hvac dominates the temperature signal, confounding
        # envelope resistance estimation.  Attenuate the Kalman gain for
        # R_inv and R_int_inv to prevent HVAC-driven changes from
        # corrupting insulation estimates.
        if hvac_running:
            K[IDX_R_INV, :] *= _R_INV_HVAC_GAIN_FACTOR
            K[IDX_R_INT_INV, :] *= _R_INV_HVAC_GAIN_FACTOR

        # R_inv gating during solar transitions.  When solar gain is
        # changing rapidly (sunrise/sunset), thermal mass absorbs or
        # releases stored solar energy, creating innovation bias that
        # the EKF would otherwise attribute to envelope leakiness.
        current_q_solar = self._last_thermal_loads.get("q_solar", 0.0)
        solar_change_rate = 0.0  # saved for bias correction gating below
        if self._prev_q_solar is not None:
            solar_change_rate = abs(current_q_solar - self._prev_q_solar)
            if solar_change_rate > 500:  # BTU/hr change per cycle
                solar_gate = max(0.1, 1.0 - solar_change_rate / 2000.0)
                K[IDX_R_INV, :] *= solar_gate
                K[IDX_R_INT_INV, :] *= solar_gate
        self._prev_q_solar = current_q_solar

        # Early learning damping: reduce parameter Kalman gain for the
        # first ~12 hours (144 observations at 5-min intervals).  Wide
        # initial priors cause enormous gains that produce chaotic
        # parameter jumps on minimal data.  Ramps from 50% to 100%.
        if self._n_obs < 144:
            damping = 0.5 + 0.5 * (self._n_obs / 144.0)
            K[_IDX_FIRST_PARAM:, :] *= damping

        # Persistent bias correction: when recent innovations show a
        # consistent directional error (mean > 1.5°F over the last 12
        # steps / 1 hour), boost parameter Kalman gains to accelerate
        # correction.  Without this, the EKF can overpredict or
        # underpredict for 12+ hours without meaningful self-correction.
        #
        # However, exclude R_inv and R_int_inv from the boost when
        # HVAC is running or solar gain is changing rapidly.  In those
        # conditions the bias is likely from Q_hvac or Q_solar model
        # error, not envelope estimation — boosting R_inv would amplify
        # the diurnal R-value oscillation instead of fixing the bias.
        if len(self._innovations) >= 12:
            recent_innovations = [v for _, v in self._innovations[-12:]]
            mean_inn = sum(recent_innovations) / len(recent_innovations)
            if abs(mean_inn) > 1.5:
                # Boost parameter gains by up to 3x (scaled by bias magnitude)
                boost = min(3.0, 1.0 + abs(mean_inn) / 1.5)
                K[_IDX_FIRST_PARAM:, :] *= boost
                # Undo the boost for R_inv/R_int_inv when the bias is
                # likely from HVAC or solar, not envelope properties.
                if hvac_running or solar_change_rate > 500:
                    K[IDX_R_INV, :] /= boost
                    K[IDX_R_INT_INV, :] /= boost

        # Quantization pause: when thermostat reports same integer as last
        # time, freeze parameter learning. The zero innovation carries no
        # real information about building parameters — it just means the
        # sensor hasn't crossed a degree boundary. T_air/T_mass still update.
        if temp_unchanged:
            K[_IDX_FIRST_PARAM:, :] = 0.0

        # Door/window learning pause: freeze parameter rows when doors/windows
        # are open to prevent infiltration from corrupting building estimates.
        # Temperature states (T_air, T_mass) still update normally.
        if open_door_window_count > 0:
            K[_IDX_FIRST_PARAM:, :] = 0.0
            _LOGGER.debug(
                "EKF learning paused: %d door(s)/window(s) open",
                open_door_window_count,
            )

        # Thermostat blend mitigation: when noise scale > 1 the sensor is
        # suspected to be blending toward an occupied satellite sensor.
        # Freeze parameter-learning rows so bad observations can't corrupt R/C.
        # T_air and T_mass state rows still update (they track real temperatures).
        if measurement_noise_scale > 1.0:
            K[_IDX_FIRST_PARAM:, :] = 0.0
            _LOGGER.debug(
                "EKF parameter learning paused: thermostat blend mitigation "
                "(noise_scale=%.1f)",
                measurement_noise_scale,
            )

        # State update
        self.x = x_pred + (K * innovation).flatten()

        # Covariance update (Joseph form for numerical stability)
        I_KH = np.eye(N_STATES) - K @ H
        self.P = I_KH @ P_pred @ I_KH.T + (K * effective_R) @ K.T

        # Clamp parameters to physical bounds
        self._clamp_parameters()

        # Record innovation for accuracy tracking
        now = datetime.now(timezone.utc)
        inn_val = float(innovation)
        self._innovations.append((now, inn_val))
        self._trim_innovations()

        # Record conditioned innovation (full context for coefficient calibrator)
        if self._last_thermal_loads:
            cond = dict(self._last_thermal_loads)
            cond["timestamp"] = now.isoformat()
            cond["innovation"] = inn_val
            cond["hvac_mode"] = hvac_mode
            cond["hvac_running"] = hvac_running
            cond["outdoor_temp"] = outdoor_temp
            cond["precipitation"] = self._current_precipitation
            cond["dt_hours"] = dt_hours
            self._conditioned_innovations.append(cond)
            self._trim_conditioned_innovations()

        self._n_obs += 1
        self._last_update = now

        return inn_val

    def _predict_state(
        self,
        x: np.ndarray,
        outdoor_temp: float,
        hvac_mode: str,
        hvac_running: bool,
        cloud_cover: float | None,
        sun_elevation: float | None,
        dt_hours: float,
    ) -> np.ndarray:
        """Process model: predict next state from current state + inputs."""
        T_air = x[IDX_T_AIR]
        T_mass = x[IDX_T_MASS]
        R_inv = x[IDX_R_INV]
        R_int_inv = x[IDX_R_INT_INV]
        C_inv = x[IDX_C_INV]
        C_mass_inv = x[IDX_C_MASS_INV]
        Q_cool_base = x[IDX_Q_COOL]
        Q_heat_base = x[IDX_Q_HEAT]
        solar_gain_btu = x[IDX_SOLAR_GAIN]

        # ── Resolve calibratable coefficients ──────────────────
        cs = self._coeff_store
        wind_coeff = cs.effective("wind_infiltration", _WIND_INFILTRATION_COEFF) if cs else _WIND_INFILTRATION_COEFF
        stack_coeff = cs.effective("stack_effect", _STACK_EFFECT_COEFF) if cs else _STACK_EFFECT_COEFF
        precip_offset = cs.effective("precipitation_offset", _PRECIPITATION_OFFSET_F) if cs else _PRECIPITATION_OFFSET_F
        int_gain_base = cs.effective("internal_gain_base", _INTERNAL_GAIN_BASE_BTU) if cs else _INTERNAL_GAIN_BASE_BTU
        int_gain_pp = cs.effective("internal_gain_per_person", _INTERNAL_GAIN_PER_PERSON_BTU) if cs else _INTERNAL_GAIN_PER_PERSON_BTU
        cal_k_attic = cs.effective("k_attic", _K_ATTIC) if cs else _K_ATTIC
        cal_k_crawl = cs.effective("k_crawlspace", _K_CRAWLSPACE) if cs else _K_CRAWLSPACE
        solar_mass_frac = cs.effective("solar_mass_fraction", _SOLAR_MASS_FRACTION) if cs else _SOLAR_MASS_FRACTION

        # ── Effective outdoor temp (precipitation correction) ────
        effective_outdoor = outdoor_temp
        if self._current_precipitation:
            effective_outdoor = outdoor_temp - precip_offset

        # ── Envelope heat flow ───────────────────────────────────
        # R_inv is per-area conductance (1/R_wall); multiply by envelope area
        # to get total building conductance (UA value).
        # Infiltration multiplier: open doors/windows and wind increase leakage
        UA = R_inv * self._envelope_area
        wind_infiltration = 0.0
        if self._current_wind_speed is not None and self._current_wind_speed > 0:
            wind_infiltration = wind_coeff * self._current_wind_speed
        # Stack effect: buoyancy-driven infiltration scales with sqrt(|ΔT|)
        stack_effect = stack_coeff * math.sqrt(abs(effective_outdoor - T_air))
        infiltration = min(
            _MAX_INFILTRATION_MULTIPLIER,
            1.0 + 2.0 * self._current_open_doors_windows + wind_infiltration + stack_effect,
        )
        Q_env = UA * infiltration * (effective_outdoor - T_air)

        # Internal coupling
        Q_int = R_int_inv * (T_mass - T_air)

        # ── HVAC output ──────────────────────────────────────────
        Q_hvac = self._hvac_output(hvac_mode, hvac_running, outdoor_temp,
                                    Q_cool_base, Q_heat_base)

        # Attic duct loss: hot/cold attic reduces HVAC effectiveness
        attic_temp = self._current_attic_temp
        if attic_temp is not None and Q_hvac != 0:
            delta = attic_temp - T_air
            if hvac_mode == "cool" and delta > 0:
                Q_hvac *= max(0.5, 1.0 - _DUCT_LOSS_PER_F * delta)
            elif hvac_mode == "heat" and delta < 0:
                Q_hvac *= max(0.5, 1.0 + _DUCT_LOSS_PER_F * delta)

        # ── Solar gain (learned parameter + multi-source irradiance) ──
        irradiance_fraction, irradiance_source = self._estimate_irradiance_fraction(
            cloud_cover, sun_elevation,
        )
        Q_solar_direct = self._solar_gain(irradiance_fraction, sun_elevation, solar_gain_btu)

        # ── Internal heat gain (occupancy-scaled) ────────────────
        people = self._current_people_count
        if people is not None:
            Q_internal = int_gain_base + int_gain_pp * people
        else:
            Q_internal = DEFAULT_INTERNAL_GAIN_BTU

        # ── Boundary zone heat transfer ──────────────────────────
        # Scale conductances by home size (base values assume 2000 ft²)
        area_scale = self._envelope_area / 2000.0
        k_attic = cal_k_attic * area_scale
        k_crawl = cal_k_crawl * area_scale

        Q_boundary = 0.0
        k_boundary = 0.0  # total boundary conductance for exponential integrator
        crawl_temp = self._current_crawlspace_temp
        Q_solar_via_attic = 0.0
        duct_loss_solar_fraction = 0.0
        attic_solar_surplus = 0.0

        if attic_temp is not None:
            total_attic = k_attic * (attic_temp - T_air)

            # Decompose attic heat: solar vs weather
            # When the sun is up and attic is hotter than outdoor air, the
            # surplus (T_attic - T_outdoor) is solar energy absorbed by the
            # roof.  The remainder is weather-driven boundary heat transfer.
            if (sun_elevation is not None and sun_elevation > 0
                    and outdoor_temp is not None):
                attic_solar_surplus = max(0.0, attic_temp - outdoor_temp)
                Q_solar_via_attic = k_attic * attic_solar_surplus
                attic_weather_contribution = total_attic - Q_solar_via_attic
                Q_boundary += attic_weather_contribution
                # Only weather-driven portion acts as temperature-coupled conductance
                weather_fraction = max(0.0, 1.0 - attic_solar_surplus / max(1.0, abs(attic_temp - T_air)))
                k_boundary += k_attic * weather_fraction
                # Track what fraction of duct loss is solar-driven
                if Q_hvac != 0 and abs(attic_temp - T_air) > 0.1:
                    duct_loss_solar_fraction = attic_solar_surplus / max(1.0, abs(attic_temp - T_air))
            else:
                # Sun is down or no outdoor temp: all attic heat → boundary
                Q_boundary += total_attic
                k_boundary += k_attic
        else:
            total_attic = 0.0
            attic_weather_contribution = 0.0

        if crawl_temp is not None:
            Q_boundary += k_crawl * (crawl_temp - T_air)
            k_boundary += k_crawl

        # Total solar = direct (window/wall) + via attic (roof absorption)
        Q_solar = Q_solar_direct + Q_solar_via_attic

        # Split solar between air and mass nodes.  In real buildings,
        # sunlight through windows directly heats floors, walls, and
        # furniture (thermal mass), not just the air.
        Q_solar_to_air = Q_solar * (1.0 - solar_mass_frac)
        Q_solar_to_mass = Q_solar * solar_mass_frac

        # ── Temperature updates (exponential decay integration) ──
        # Unconditionally stable: avoids oscillation at extreme parameter
        # bounds that forward Euler could produce when λ·dt approaches 2.
        #
        # Air node: dT_air/dt = C_inv * [-λ_air * T_air + forcing_air]
        # where λ_air = total conductance away from air node
        # and forcing_air = conductance-weighted source temps + non-temp heat
        lambda_air = max(1e-10, UA * infiltration + R_int_inv + k_boundary)
        alpha = lambda_air * C_inv * dt_hours
        # Forcing: Q that doesn't depend on T_air
        # Q_env(T_air=0) = UA * infiltration * effective_outdoor
        # Q_int(T_air=0) = R_int_inv * T_mass
        # Q_boundary(T_air=0) = k_attic*T_attic + k_crawl*T_crawl (if present)
        # Auxiliary appliance load (e.g., HPWH cooling = negative BTU/hr)
        Q_appliances = getattr(self, "_current_appliance_btu", 0.0)
        # Resistive strip BTU: computed from (circuit_watts - hp_baseline_watts) * 3.412
        # when aux heat is active, so EKF only attributes heat pump output to IDX_Q_HEAT.
        Q_aux_resistive = getattr(self, "_current_aux_resistive_btu", 0.0)

        forcing_air = (
            UA * infiltration * effective_outdoor
            + R_int_inv * T_mass
            + Q_hvac + Q_solar_to_air + Q_internal + Q_appliances + Q_aux_resistive
        )
        # Add boundary zone source terms (conductance × source temp)
        # Only the weather-coupled portion of attic conductance acts as a
        # temperature-dependent source.  The solar portion (Q_solar_via_attic)
        # is already included in Q_solar as a flat heat flow.
        if attic_temp is not None:
            if sun_elevation is not None and sun_elevation > 0 and outdoor_temp is not None:
                weather_frac = max(0.0, 1.0 - attic_solar_surplus / max(1.0, abs(attic_temp - T_air)))
                forcing_air += k_attic * weather_frac * attic_temp
            else:
                forcing_air += k_attic * attic_temp
        if crawl_temp is not None:
            forcing_air += k_crawl * crawl_temp

        if alpha > 1e-8:
            exp_neg_alpha = math.exp(-alpha)
            T_eq_air = forcing_air / lambda_air
            T_air_new = T_air * exp_neg_alpha + T_eq_air * (1.0 - exp_neg_alpha)
        else:
            # Very small alpha: fall back to linear (avoids 0/0)
            T_air_new = T_air + C_inv * (forcing_air - lambda_air * T_air) * dt_hours

        # Mass node: dT_mass/dt = C_mass_inv * [R_int_inv * (T_air - T_mass) + Q_solar_to_mass]
        # Rewritten as: dT_mass/dt = -lambda_mass * T_mass + C_mass_inv * forcing_mass
        # where lambda_mass = R_int_inv * C_mass_inv
        # and forcing_mass = R_int_inv * T_air + Q_solar_to_mass
        lambda_mass = R_int_inv * C_mass_inv
        beta = lambda_mass * dt_hours
        forcing_mass = R_int_inv * T_air + Q_solar_to_mass
        if beta > 1e-8:
            exp_neg_beta = math.exp(-beta)
            T_eq_mass = forcing_mass / max(1e-10, R_int_inv)
            T_mass_new = T_mass * exp_neg_beta + T_eq_mass * (1.0 - exp_neg_beta)
        else:
            T_mass_new = T_mass + C_mass_inv * (R_int_inv * (T_air - T_mass) + Q_solar_to_mass) * dt_hours

        # ── Cache thermal load components for sensor exposure ───────
        attic_contribution = total_attic if attic_temp is not None else 0.0
        crawl_contribution = k_crawl * (crawl_temp - T_air) if crawl_temp is not None else 0.0
        self._last_thermal_loads = {
            # Heat flow components (BTU/hr)
            "q_env": Q_env,
            "q_int": Q_int,
            "q_hvac": Q_hvac,
            "q_solar": Q_solar,  # total: direct + via attic
            "q_solar_direct": Q_solar_direct,
            "q_solar_via_attic": Q_solar_via_attic,
            "q_internal": Q_internal,
            "q_boundary": Q_boundary,  # excludes solar-driven attic heat during daytime
            "q_appliances": Q_appliances,
            "q_aux_resistive": Q_aux_resistive,
            # Irradiance context
            "irradiance_fraction": irradiance_fraction,
            "irradiance_source": irradiance_source,
            "uv_index": getattr(self, "_current_uv_index", None),
            # Context values for sensor attributes
            "infiltration_factor": infiltration,
            "ua_value": UA,
            "effective_outdoor_temp": effective_outdoor,
            "indoor_temp": T_air,
            "people_count": self._current_people_count,
            "wind_speed_mph": self._current_wind_speed,
            "doors_windows_open": self._current_open_doors_windows,
            "cloud_cover": cloud_cover,
            "sun_elevation": sun_elevation,
            "learned_peak_solar_gain": solar_gain_btu,
            "attic_temp": attic_temp,
            "crawlspace_temp": crawl_temp,
            "attic_contribution_btu": attic_contribution,
            "attic_solar_contribution_btu": Q_solar_via_attic,
            "attic_weather_contribution_btu": attic_contribution - Q_solar_via_attic if attic_temp is not None else 0.0,
            "crawlspace_contribution_btu": crawl_contribution,
            "duct_loss_solar_fraction": duct_loss_solar_fraction,
        }

        x_new = x.copy()
        x_new[IDX_T_AIR] = T_air_new
        x_new[IDX_T_MASS] = T_mass_new
        # Parameters don't change in prediction (random walk model)
        return x_new

    def _jacobian(
        self,
        x: np.ndarray,
        outdoor_temp: float,
        hvac_mode: str,
        hvac_running: bool,
        cloud_cover: float | None,
        sun_elevation: float | None,
        dt_hours: float,
    ) -> np.ndarray:
        """Compute the Jacobian F = df/dx analytically."""
        T_air = x[IDX_T_AIR]
        T_mass = x[IDX_T_MASS]
        R_inv = x[IDX_R_INV]
        R_int_inv = x[IDX_R_INT_INV]
        C_inv = x[IDX_C_INV]
        C_mass_inv = x[IDX_C_MASS_INV]
        solar_gain_btu = x[IDX_SOLAR_GAIN]

        # ── Resolve calibratable coefficients (must match _predict_state) ──
        cs = self._coeff_store
        wind_coeff = cs.effective("wind_infiltration", _WIND_INFILTRATION_COEFF) if cs else _WIND_INFILTRATION_COEFF
        stack_coeff = cs.effective("stack_effect", _STACK_EFFECT_COEFF) if cs else _STACK_EFFECT_COEFF
        precip_offset = cs.effective("precipitation_offset", _PRECIPITATION_OFFSET_F) if cs else _PRECIPITATION_OFFSET_F
        int_gain_base = cs.effective("internal_gain_base", _INTERNAL_GAIN_BASE_BTU) if cs else _INTERNAL_GAIN_BASE_BTU
        int_gain_pp = cs.effective("internal_gain_per_person", _INTERNAL_GAIN_PER_PERSON_BTU) if cs else _INTERNAL_GAIN_PER_PERSON_BTU
        cal_k_attic = cs.effective("k_attic", _K_ATTIC) if cs else _K_ATTIC
        cal_k_crawl = cs.effective("k_crawlspace", _K_CRAWLSPACE) if cs else _K_CRAWLSPACE
        alpha_cool = cs.effective("alpha_cool", ALPHA_COOL) if cs else ALPHA_COOL
        alpha_heat = cs.effective("alpha_heat", ALPHA_HEAT) if cs else ALPHA_HEAT
        solar_mass_frac = cs.effective("solar_mass_fraction", _SOLAR_MASS_FRACTION) if cs else _SOLAR_MASS_FRACTION

        # Precipitation correction for effective outdoor temp
        effective_outdoor = outdoor_temp
        if self._current_precipitation:
            effective_outdoor = outdoor_temp - precip_offset

        # Infiltration multiplier (must match _predict_state)
        wind_infiltration = 0.0
        if self._current_wind_speed is not None and self._current_wind_speed > 0:
            wind_infiltration = wind_coeff * self._current_wind_speed
        stack_effect = stack_coeff * math.sqrt(abs(effective_outdoor - T_air))
        infiltration = min(
            _MAX_INFILTRATION_MULTIPLIER,
            1.0 + 2.0 * self._current_open_doors_windows + wind_infiltration + stack_effect,
        )

        # UA = per-area R_inv × envelope area (total building conductance)
        UA = R_inv * self._envelope_area

        # Total heat into air node (for C_inv Jacobian entry)
        Q_env = UA * infiltration * (effective_outdoor - T_air)
        Q_int = R_int_inv * (T_mass - T_air)
        Q_hvac = self._hvac_output(hvac_mode, hvac_running, outdoor_temp,
                                    x[IDX_Q_COOL], x[IDX_Q_HEAT])
        irradiance_fraction, _ = self._estimate_irradiance_fraction(cloud_cover, sun_elevation)
        Q_solar_direct = self._solar_gain(irradiance_fraction, sun_elevation, solar_gain_btu)

        people = self._current_people_count
        if people is not None:
            Q_internal = int_gain_base + int_gain_pp * people
        else:
            Q_internal = DEFAULT_INTERNAL_GAIN_BTU

        # Boundary zone heat transfer (scaled by home size)
        area_scale = self._envelope_area / 2000.0
        k_attic = cal_k_attic * area_scale
        k_crawl = cal_k_crawl * area_scale

        Q_boundary = 0.0
        Q_solar_via_attic = 0.0
        k_boundary = 0.0  # total boundary conductance affecting dT_air/dT_air
        attic_temp = self._current_attic_temp
        if attic_temp is not None:
            total_attic = k_attic * (attic_temp - T_air)
            if sun_elevation is not None and sun_elevation > 0 and outdoor_temp is not None:
                attic_solar_surplus = max(0.0, attic_temp - outdoor_temp)
                Q_solar_via_attic = k_attic * attic_solar_surplus
                Q_boundary += total_attic - Q_solar_via_attic
                weather_fraction = max(0.0, 1.0 - attic_solar_surplus / max(1.0, abs(attic_temp - T_air)))
                k_boundary += k_attic * weather_fraction
            else:
                Q_boundary += total_attic
                k_boundary += k_attic
        crawl_temp = self._current_crawlspace_temp
        if crawl_temp is not None:
            Q_boundary += k_crawl * (crawl_temp - T_air)
            k_boundary += k_crawl

        Q_solar = Q_solar_direct + Q_solar_via_attic

        Q_appliances = getattr(self, "_current_appliance_btu", 0.0)
        Q_aux_resistive = getattr(self, "_current_aux_resistive_btu", 0.0)

        # Solar split: only the air fraction enters the air node forcing
        Q_solar_to_air = Q_solar * (1.0 - solar_mass_frac)
        Q_solar_to_mass = Q_solar * solar_mass_frac
        Q_total_air = Q_env + Q_int + Q_hvac + Q_solar_to_air + Q_internal + Q_boundary + Q_appliances + Q_aux_resistive

        F = np.eye(N_STATES)
        dt = dt_hours

        # ── dT_air_new / d(state) ──────────────────────────────
        F[IDX_T_AIR, IDX_T_AIR] = 1.0 + C_inv * (
            -UA * infiltration - R_int_inv - k_boundary
        ) * dt
        F[IDX_T_AIR, IDX_T_MASS] = C_inv * R_int_inv * dt
        # dT_air/dR_inv: R_inv appears as UA = R_inv * area, so derivative includes area
        F[IDX_T_AIR, IDX_R_INV] = C_inv * self._envelope_area * infiltration * (effective_outdoor - T_air) * dt
        F[IDX_T_AIR, IDX_R_INT_INV] = C_inv * (T_mass - T_air) * dt
        F[IDX_T_AIR, IDX_C_INV] = Q_total_air * dt

        # dT_air / dQ_cool_base and dQ_heat_base
        # Include environmental corrections so Jacobian matches _hvac_output
        if hvac_running and hvac_mode == "cool":
            cop_factor = max(0.1, 1.0 - alpha_cool * (outdoor_temp - T_REF_F))
            # Outdoor humidity correction
            humidity = getattr(self, "_current_humidity", None)
            if humidity is not None and humidity > 50.0:
                cop_factor *= max(0.8, 1.0 - (humidity - 50.0) / 500.0)
            # Indoor humidity SHR correction
            indoor_hum = getattr(self, "_current_indoor_humidity", None)
            if indoor_hum is not None and indoor_hum > 50.0:
                shr = max(0.65, 1.0 - (indoor_hum - 50.0) / 100.0)
                cop_factor *= shr
            # Pressure correction
            pressure = getattr(self, "_current_pressure", None)
            if pressure is not None:
                cop_factor *= (pressure / 1013.25) ** 0.1
            # Duct loss factor
            jac_attic_temp = self._current_attic_temp
            if jac_attic_temp is not None:
                delta = jac_attic_temp - T_air
                if delta > 0:
                    cop_factor *= max(0.5, 1.0 - _DUCT_LOSS_PER_F * delta)
            F[IDX_T_AIR, IDX_Q_COOL] = C_inv * (-cop_factor) * dt
        elif hvac_running and hvac_mode == "heat":
            cop_factor = max(0.1, 1.0 - alpha_heat * (T_REF_F - outdoor_temp))
            # Pressure correction
            pressure = getattr(self, "_current_pressure", None)
            if pressure is not None:
                cop_factor *= (pressure / 1013.25) ** 0.1
            # Duct loss factor
            jac_attic_temp = self._current_attic_temp
            if jac_attic_temp is not None:
                delta = jac_attic_temp - T_air
                if delta < 0:
                    cop_factor *= max(0.5, 1.0 + _DUCT_LOSS_PER_F * delta)
            F[IDX_T_AIR, IDX_Q_HEAT] = C_inv * cop_factor * dt

        # dT_air / d(solar_gain_btu): partial of Q_solar_direct w.r.t. solar_gain_btu
        # Only the air fraction (1 - f_s) enters the air node
        # Q_solar_direct = solar_gain_btu * irradiance_fraction * sin(elevation)
        if sun_elevation is not None and sun_elevation > 0:
            altitude_factor = math.sin(math.radians(max(0, min(90, sun_elevation))))
            dQ_solar_d_param = irradiance_fraction * altitude_factor
            F[IDX_T_AIR, IDX_SOLAR_GAIN] = C_inv * (1.0 - solar_mass_frac) * dQ_solar_d_param * dt

        # ── dT_mass_new / d(state) ─────────────────────────────
        F[IDX_T_MASS, IDX_T_AIR] = C_mass_inv * R_int_inv * dt
        F[IDX_T_MASS, IDX_T_MASS] = 1.0 - C_mass_inv * R_int_inv * dt
        F[IDX_T_MASS, IDX_R_INT_INV] = C_mass_inv * (T_air - T_mass) * dt
        F[IDX_T_MASS, IDX_C_MASS_INV] = (R_int_inv * (T_air - T_mass) + Q_solar_to_mass) * dt

        # dT_mass / d(solar_gain_btu): mass fraction of solar goes directly to mass
        if sun_elevation is not None and sun_elevation > 0:
            F[IDX_T_MASS, IDX_SOLAR_GAIN] = C_mass_inv * solar_mass_frac * dQ_solar_d_param * dt

        # Parameters: F[i,i] = 1.0 (already set by eye)

        return F

    # ── HVAC and Solar Models ───────────────────────────────────────

    def _hvac_output(
        self,
        mode: str,
        running: bool,
        outdoor_temp: float,
        Q_cool_base: float,
        Q_heat_base: float,
    ) -> float:
        """Calculate HVAC heat flow (BTU/hr) with COP degradation.

        Cooling: negative (removes heat), degrades as outdoor temp rises.
        Heating: positive (adds heat), degrades as outdoor temp drops.

        Environmental adjustments (when data available):
        - Wind speed: wind chill reduces effective outdoor temp for heating COP
        - Humidity: high humidity reduces cooling COP
        - Pressure: altitude/weather pressure affects compressor efficiency
        """
        if not running:
            return 0.0

        # Use dry-bulb outdoor temp for COP/capacity calculation.
        # Wind chill (NWS formula) models perceived temp on exposed human skin
        # and should NOT be applied to heat pump condensers — wind actually
        # improves condenser heat exchange via forced convection. Wind effects
        # on the building envelope are handled separately via infiltration.

        if mode == "cool":
            # Capacity decreases as outdoor temp rises above reference
            cs = self._coeff_store
            a_cool = cs.effective("alpha_cool", ALPHA_COOL) if cs else ALPHA_COOL
            raw_factor = 1.0 - a_cool * (outdoor_temp - T_REF_F)
            cop_factor = max(0.1, raw_factor)
            if raw_factor <= 0.1:
                _LOGGER.warning(
                    "COP degradation at floor (0.1) for cooling: outdoor=%.1f°F "
                    "— possible sensor issue or extreme conditions",
                    outdoor_temp,
                )

            # Outdoor humidity correction for cooling: high humidity reduces COP
            humidity = getattr(self, "_current_humidity", None)
            if humidity is not None and humidity > 50.0:
                cop_factor *= max(0.8, 1.0 - (humidity - 50.0) / 500.0)

            # Indoor humidity: high indoor RH means more latent cooling
            # (dehumidification) and less sensible cooling (temperature change).
            # Apply Sensible Heat Ratio (SHR) correction.
            indoor_hum = getattr(self, "_current_indoor_humidity", None)
            if indoor_hum is not None and indoor_hum > 50.0:
                shr = max(0.65, 1.0 - (indoor_hum - 50.0) / 100.0)
                cop_factor *= shr

            # Pressure correction
            pressure = getattr(self, "_current_pressure", None)
            if pressure is not None:
                cop_factor *= (pressure / 1013.25) ** 0.1

            return -Q_cool_base * cop_factor

        if mode == "heat":
            # Capacity decreases as outdoor temp drops below reference
            cs = self._coeff_store
            a_heat = cs.effective("alpha_heat", ALPHA_HEAT) if cs else ALPHA_HEAT
            raw_factor = 1.0 - a_heat * (T_REF_F - outdoor_temp)
            cop_factor = max(0.1, raw_factor)
            if raw_factor <= 0.1:
                _LOGGER.warning(
                    "COP degradation at floor (0.1) for heating: outdoor=%.1f°F "
                    "— possible sensor issue or extreme conditions",
                    outdoor_temp,
                )

            # Pressure correction
            pressure = getattr(self, "_current_pressure", None)
            if pressure is not None:
                cop_factor *= (pressure / 1013.25) ** 0.1

            return Q_heat_base * cop_factor

        return 0.0

    def _estimate_irradiance_fraction(
        self,
        cloud_cover: float | None,
        sun_elevation: float | None,
    ) -> tuple[float, str]:
        """Estimate irradiance as a fraction of clear-sky peak (0.0-1.0).

        Uses a priority hierarchy of available data sources:
        1. Direct solar irradiance sensor (W/m²)
        2. Solar panel-derived irradiance (W/m²)
        3. UV index + cloud cover blend
        4. UV index only
        5. Cloud cover only
        6. Elevation only (no weather data)

        Returns:
            (irradiance_fraction, source_label)
        """
        if sun_elevation is None or sun_elevation <= 0:
            return 0.0, "night"

        sin_elev = math.sin(math.radians(max(0.5, min(90, sun_elevation))))

        # Tier 1: Direct or panel-derived solar irradiance (W/m²)
        irradiance = getattr(self, "_current_solar_irradiance", None)
        if irradiance is not None and irradiance > 0:
            # Clear-sky reference ~1000 W/m² at normal incidence
            frac = min(1.0, irradiance / (1000.0 * sin_elev))
            source = "sensor" if not getattr(self, "_irradiance_from_panels", False) else "panel_derived"
            return frac, source

        # Tier 2/3: UV index (smooth hourly signal, measures actual irradiance)
        uv = getattr(self, "_current_uv_index", None)
        if uv is not None and uv >= 0:
            # UV peaks ~6-8 at clear-sky noon for mid-latitudes; normalize
            # against an elevation-adjusted clear-sky reference.
            uv_clear_sky = 6.0 * sin_elev
            uv_fraction = min(1.0, uv / max(0.1, uv_clear_sky))

            if cloud_cover is not None:
                # Blend: UV is the better irradiance measure (70%), cloud cover
                # captures area coverage (30%)
                cloud_fraction = 1.0 - cloud_cover
                frac = 0.7 * uv_fraction + 0.3 * cloud_fraction
                return max(0.0, min(1.0, frac)), "uv_blend"
            else:
                return max(0.0, min(1.0, uv_fraction)), "uv_only"

        # Tier 4: Cloud cover only (current fallback)
        if cloud_cover is not None:
            return max(0.0, 1.0 - cloud_cover), "cloud"

        # Tier 5: No weather data at all — assume moderate clear-sky
        return 0.6, "elevation"

    @staticmethod
    def _solar_gain(
        irradiance_fraction: float,
        sun_elevation: float | None,
        solar_gain_btu: float = DEFAULT_SOLAR_GAIN_BTU,
    ) -> float:
        """Estimate solar heat gain (BTU/hr) using learned peak solar gain.

        Args:
            irradiance_fraction: 0.0-1.0 from _estimate_irradiance_fraction().
            sun_elevation: Degrees above horizon, or None.
            solar_gain_btu: Learned peak clear-sky-noon BTU/hr.
        """
        if sun_elevation is None or sun_elevation <= 0:
            return 0.0

        altitude_factor = math.sin(math.radians(max(0, min(90, sun_elevation))))

        return solar_gain_btu * irradiance_fraction * altitude_factor

    # ── Parameter Access ────────────────────────────────────────────

    @property
    def T_air(self) -> float:
        return float(self.x[IDX_T_AIR])

    @property
    def T_mass(self) -> float:
        return float(self.x[IDX_T_MASS])

    @property
    def R_inv(self) -> float:
        return float(self.x[IDX_R_INV])

    @property
    def R_int_inv(self) -> float:
        return float(self.x[IDX_R_INT_INV])

    @property
    def C_inv(self) -> float:
        return float(self.x[IDX_C_INV])

    @property
    def C_mass_inv(self) -> float:
        return float(self.x[IDX_C_MASS_INV])

    @property
    def solar_gain_btu(self) -> float:
        """Learned peak solar heat gain (BTU/hr at clear-sky noon)."""
        return float(self.x[IDX_SOLAR_GAIN])

    @property
    def thermal_load_components(self) -> dict[str, float | None]:
        """Last computed thermal load breakdown (BTU/hr) from the EKF predict step."""
        return dict(self._last_thermal_loads)

    @property
    def R_value(self) -> float:
        """Envelope thermal resistance (°F·hr/BTU)."""
        return 1.0 / max(self.R_inv, 1e-6)

    @property
    def envelope_area(self) -> float:
        """Envelope area in ft²."""
        return self._envelope_area

    @property
    def thermal_mass(self) -> float:
        """Mass thermal capacitance (BTU/°F)."""
        return 1.0 / max(self.C_mass_inv, 1e-9)

    def cooling_capacity(self, outdoor_temp: float) -> float:
        """Cooling capacity at given outdoor temp (BTU/hr, positive)."""
        cop_factor = max(0.1, 1.0 - ALPHA_COOL * (outdoor_temp - T_REF_F))
        return float(self.x[IDX_Q_COOL]) * cop_factor

    def heating_capacity(self, outdoor_temp: float) -> float:
        """Heating capacity at given outdoor temp (BTU/hr, positive)."""
        cop_factor = max(0.1, 1.0 - ALPHA_HEAT * (T_REF_F - outdoor_temp))
        return float(self.x[IDX_Q_HEAT]) * cop_factor

    def cop_factor_at_temp(self, outdoor_temp: float, mode: str) -> float:
        """COP degradation factor at a given outdoor temp (0.1 to 1.0+).

        This is the multiplier applied to base capacity. Higher = better efficiency.
        Used by the counterfactual simulator to compare COP between time-shifted
        and baseline operating hours.
        """
        if mode == "cool":
            return max(0.1, 1.0 - ALPHA_COOL * (outdoor_temp - T_REF_F))
        elif mode == "heat":
            return max(0.1, 1.0 - ALPHA_HEAT * (T_REF_F - outdoor_temp))
        return 1.0

    # ── Confidence ──────────────────────────────────────────────────

    @property
    def confidence(self) -> float:
        """Model confidence from 0.0 (no data) to 1.0 (well-converged).

        Uses per-parameter relative variance reduction so that envelope
        parameters (R, C) can drive confidence even when HVAC capacity
        parameters haven't been observed yet.

        Returns the high-water mark of instantaneous confidence so that
        the user-facing metric never decreases. Instantaneous drops occur
        when unobserved parameters accumulate process noise (e.g. Q_cool
        during heating season), but this doesn't reflect actual knowledge loss.
        """
        if self._n_obs < 10:
            return 0.0

        # Per-parameter relative variance reduction (indices 2-8, all learned params)
        current_diag = np.diag(self.P)[2:N_STATES]
        initial_diag = np.diag(self._P_initial)[2:N_STATES]

        # For each parameter: how much has its variance shrunk?
        # ratio=1 means no learning, ratio→0 means well-converged
        ratios = current_diag / np.maximum(initial_diag, 1e-30)

        # Convert to per-parameter confidence and average
        param_confidences = 1.0 - np.minimum(ratios, 1.0)
        instantaneous = float(np.mean(param_confidences))
        instantaneous = max(0.0, min(1.0, instantaneous))

        # Return monotonically non-decreasing high-water mark
        self._confidence_hwm = max(self._confidence_hwm, instantaneous)
        return self._confidence_hwm

    @property
    def parameter_uncertainty(self) -> dict[str, float]:
        """Standard deviation of each parameter estimate."""
        stds = np.sqrt(np.diag(self.P))
        return {
            "R_inv": float(stds[IDX_R_INV]),
            "R_int_inv": float(stds[IDX_R_INT_INV]),
            "C_inv": float(stds[IDX_C_INV]),
            "C_mass_inv": float(stds[IDX_C_MASS_INV]),
            "Q_cool_base": float(stds[IDX_Q_COOL]),
            "Q_heat_base": float(stds[IDX_Q_HEAT]),
            "solar_gain_btu": float(stds[IDX_SOLAR_GAIN]),
        }

    @property
    def parameter_confidence(self) -> dict[str, float]:
        """Per-parameter confidence (0.0-1.0) based on variance reduction."""
        names = [
            "envelope", "internal_coupling", "air_mass",
            "thermal_mass", "cooling_capacity", "heating_capacity",
            "solar_gain",
        ]
        if self._n_obs < 10:
            return {name: 0.0 for name in names}
        current_diag = np.diag(self.P)[2:N_STATES]
        initial_diag = np.diag(self._P_initial)[2:N_STATES]
        ratios = current_diag / np.maximum(initial_diag, 1e-30)
        confs = 1.0 - np.minimum(ratios, 1.0)
        return {name: round(float(c), 3) for name, c in zip(names, confs)}

    def mode_confidence(self, hvac_mode: str) -> float:
        """Confidence for a specific HVAC mode (0.0-1.0).

        Only averages parameters relevant to the given mode, so unobserved
        seasonal parameters (e.g. Q_cool in winter) don't drag the score down.

        Heating uses: R_inv, R_int_inv, C_inv, C_mass_inv, Q_heat
        Cooling uses: R_inv, R_int_inv, C_inv, C_mass_inv, Q_cool, solar_gain
        """
        if self._n_obs < 10:
            return 0.0

        current_diag = np.diag(self.P)[2:N_STATES]
        initial_diag = np.diag(self._P_initial)[2:N_STATES]
        ratios = current_diag / np.maximum(initial_diag, 1e-30)
        all_confs = 1.0 - np.minimum(ratios, 1.0)

        # Indices into the learned-parameter sub-array (offset by 2 from state vector)
        idx_r = IDX_R_INV - 2       # 0
        idx_rint = IDX_R_INT_INV - 2  # 1
        idx_c = IDX_C_INV - 2       # 2
        idx_cm = IDX_C_MASS_INV - 2  # 3
        idx_qcool = IDX_Q_COOL - 2  # 4
        idx_qheat = IDX_Q_HEAT - 2  # 5
        idx_solar = IDX_SOLAR_GAIN - 2  # 6

        # Shared envelope parameters always included
        envelope_indices = [idx_r, idx_rint, idx_c, idx_cm]

        if hvac_mode == "heat":
            relevant = envelope_indices + [idx_qheat]
        elif hvac_mode == "cool":
            relevant = envelope_indices + [idx_qcool, idx_solar]
        else:
            # For idle/auto, use all parameters (same as global confidence)
            relevant = list(range(len(all_confs)))

        selected = [float(all_confs[i]) for i in relevant]
        return max(0.0, min(1.0, sum(selected) / len(selected)))

    @property
    def learning_needs(self) -> list[str]:
        """Human-readable list of what the model still needs to learn."""
        pc = self.parameter_confidence
        needs = []
        if pc["envelope"] < 0.5:
            needs.append("More HVAC-off periods needed to measure insulation")
        if pc["cooling_capacity"] < 0.3 and pc["heating_capacity"] < 0.3:
            needs.append("Waiting for HVAC cycling to measure system capacity")
        elif pc["cooling_capacity"] < 0.3:
            needs.append("No cooling cycles observed yet")
        elif pc["heating_capacity"] < 0.3:
            needs.append("No heating cycles observed yet")
        if pc["solar_gain"] < 0.3:
            needs.append("Needs sunny daytime data for solar gain estimate")
        if pc["thermal_mass"] < 0.3:
            needs.append("More temperature swings needed to measure thermal mass")
        return needs

    @property
    def learning_rate(self) -> str:
        """Qualitative learning rate: 'active', 'slow', or 'paused'.

        Based on how many recent innovations (last hour / 12 observations)
        had magnitude > 0.3°F.  Helps users understand why confidence is
        stagnating during stable temperature periods.
        """
        if self._n_obs < 10:
            return "initializing"
        recent = self._innovations[-12:]
        if not recent:
            return "paused"
        significant = sum(1 for _, v in recent if abs(v) > 0.3)
        if significant >= 6:
            return "active"
        elif significant >= 2:
            return "slow"
        return "paused"

    # ── Accuracy Reporting ──────────────────────────────────────────

    @property
    def mean_absolute_error(self) -> float | None:
        """Rolling MAE from innovations (last 24 hours)."""
        if not self._innovations:
            return None
        return sum(abs(inn) for _, inn in self._innovations) / len(self._innovations)

    @property
    def mean_signed_error(self) -> float | None:
        """Rolling bias from innovations (last 24 hours)."""
        if not self._innovations:
            return None
        return sum(inn for _, inn in self._innovations) / len(self._innovations)

    def get_accuracy_report(self) -> dict:
        """Generate accuracy stats compatible with ModelTracker report format."""
        mae = self.mean_absolute_error
        bias = self.mean_signed_error
        return {
            "cool": {
                "samples": self._n_obs,
                "mae": round(mae, 3) if mae is not None else None,
                "bias": round(bias, 3) if bias is not None else None,
                "correction": 1.0,  # Not applicable — Kalman does continuous correction
                "alert": False,
            },
            "heat": {
                "samples": self._n_obs,
                "mae": round(mae, 3) if mae is not None else None,
                "bias": round(bias, 3) if bias is not None else None,
                "correction": 1.0,
                "alert": False,
            },
            "resist": {
                "samples": self._n_obs,
                "mae": round(mae, 3) if mae is not None else None,
                "bias": round(bias, 3) if bias is not None else None,
                "correction": 1.0,
                "alert": False,
            },
        }

    # ── Helpers ──────────────────────────────────────────────────────

    def _clamp_parameters(self):
        """Enforce physical bounds and rate limits on estimated parameters."""
        # Rate-limit R_inv: cap change to ±frac per cycle.
        # During early learning (first ~2 weeks / 4032 obs), use a tighter
        # limit to prevent the diurnal R_inv oscillations seen when solar
        # gain lag and thermal mass coupling confound envelope estimation.
        # Ramps from 30% to 100% of the base rate limit.
        _EARLY_LEARNING_OBS = 4032  # ~2 weeks at 5-min intervals
        if self._n_obs < _EARLY_LEARNING_OBS:
            progress = self._n_obs / _EARLY_LEARNING_OBS
            frac = _R_INV_MAX_CHANGE_FRAC * (0.3 + 0.7 * progress)
        else:
            frac = _R_INV_MAX_CHANGE_FRAC

        # Diurnal oscillation damping: if R_inv has swung > 20% over the
        # last 24h, tighten the rate limit proportionally.  This catches
        # parameter aliasing where R_inv absorbs solar/HVAC model errors
        # and oscillates with the day/night cycle.
        _R_INV_HISTORY_LEN = 288  # 24h at 5-min intervals
        r_inv_now = float(self.x[IDX_R_INV])
        self._r_inv_recent.append(r_inv_now)
        if len(self._r_inv_recent) > _R_INV_HISTORY_LEN:
            self._r_inv_recent = self._r_inv_recent[-_R_INV_HISTORY_LEN:]
        if len(self._r_inv_recent) >= 36:  # need at least 3 hours of data
            r_min = min(self._r_inv_recent)
            r_max = max(self._r_inv_recent)
            if r_min > 0:
                swing_pct = (r_max - r_min) / r_min
                if swing_pct > 0.20:
                    # Scale down: 20% swing = 1.0x, 50% swing = 0.1x
                    damper = max(0.1, 1.0 - (swing_pct - 0.20) / 0.30)
                    frac *= damper

        if self._prev_r_inv is not None and self._prev_r_inv > 0:
            prev = self._prev_r_inv
            max_delta = frac * prev
            self.x[IDX_R_INV] = np.clip(
                self.x[IDX_R_INV], prev - max_delta, prev + max_delta,
            )
        self._prev_r_inv = float(self.x[IDX_R_INV])

        # Rate-limit C_mass_inv: cap change to ±_C_MASS_MAX_CHANGE_FRAC per cycle.
        if self._prev_c_mass_inv is not None and self._prev_c_mass_inv > 0:
            prev = self._prev_c_mass_inv
            max_delta = _C_MASS_MAX_CHANGE_FRAC * prev
            self.x[IDX_C_MASS_INV] = np.clip(
                self.x[IDX_C_MASS_INV], prev - max_delta, prev + max_delta,
            )
        self._prev_c_mass_inv = float(self.x[IDX_C_MASS_INV])

        # Rate-limit Q_cool/Q_heat when user provided tonnage rating.
        if self._has_tonnage_prior:
            for idx, prev_attr in (
                (IDX_Q_COOL, "_prev_q_cool"),
                (IDX_Q_HEAT, "_prev_q_heat"),
            ):
                prev = getattr(self, prev_attr)
                if prev is not None and prev > 0:
                    max_delta = _Q_HVAC_MAX_CHANGE_FRAC * prev
                    self.x[idx] = np.clip(
                        self.x[idx], prev - max_delta, prev + max_delta,
                    )
        self._prev_q_cool = float(self.x[IDX_Q_COOL])
        self._prev_q_heat = float(self.x[IDX_Q_HEAT])

        # Constrain T_mass to physical proximity of T_air.
        # Residential thermal mass (walls, slab, furniture) equilibrates
        # with air within a few degrees.  Larger gaps indicate filter
        # divergence, not physical reality — breaks the positive feedback
        # loop that drives thermal mass runaway.
        mass_air_delta = self.x[IDX_T_MASS] - self.x[IDX_T_AIR]
        if abs(mass_air_delta) > _MAX_MASS_AIR_DELTA_F:
            self.x[IDX_T_MASS] = (
                self.x[IDX_T_AIR] + np.sign(mass_air_delta) * _MAX_MASS_AIR_DELTA_F
            )

        # Enforce maximum thermal mass time constant.
        # τ = C_mass / R_int_inv = (1/C_mass_inv) / R_int_inv
        # Values beyond _MAX_TAU_HOURS mean the filter is storing energy
        # in a mode too slow to validate against observed data.
        c_mass_inv = max(float(self.x[IDX_C_MASS_INV]), 1e-10)
        r_int_inv = max(float(self.x[IDX_R_INT_INV]), 0.5)
        tau = (1.0 / c_mass_inv) / r_int_inv
        if tau > _MAX_TAU_HOURS:
            max_c_mass = _MAX_TAU_HOURS * r_int_inv
            self.x[IDX_C_MASS_INV] = 1.0 / max_c_mass

        # Standard bounds clamping
        for idx, (lo, hi) in BOUNDS.items():
            self.x[idx] = np.clip(self.x[idx], lo, hi)

    def _trim_innovations(self, max_hours: int = 24):
        """Keep only last 24 hours of innovations."""
        if not self._innovations:
            return
        cutoff = datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=max_hours)
        self._innovations = [(t, v) for t, v in self._innovations if t > cutoff]

    def _trim_conditioned_innovations(self, max_hours: int = 72):
        """Keep only last 72 hours of conditioned innovations."""
        if not self._conditioned_innovations:
            return
        cutoff = (
            datetime.now(timezone.utc)
            - __import__("datetime").timedelta(hours=max_hours)
        ).isoformat()
        self._conditioned_innovations = [
            c for c in self._conditioned_innovations if c.get("timestamp", "") > cutoff
        ]

    def get_conditioned_innovations(self) -> list[dict]:
        """Return a copy of the conditioned innovation buffer (read-only)."""
        return list(self._conditioned_innovations)

    # ── Profiler Prior Injection ──────────────────────────────────

    def inject_profiler_priors(
        self,
        resist_slope: float,
        resist_intercept: float,
        cool_delta_at_ref: float | None,
        heat_delta_at_ref: float | None,
        resist_delta_at_ref: float,
    ) -> bool:
        """One-time injection of profiler-derived priors into EKF parameters.

        Uses the profiler's trendline data to improve EKF parameter estimates
        when the profiler has reached sufficient confidence but the EKF is
        still immature.  Only runs once (sets _profiler_seeded flag).

        Args:
            resist_slope: F/hr per degree outdoor (from resist trendline)
            resist_intercept: F/hr at 0F outdoor (from resist trendline)
            cool_delta_at_ref: Net cooling F/hr at T_ref (75F), or None
            heat_delta_at_ref: Net heating F/hr at T_ref (75F), or None
            resist_delta_at_ref: Passive drift F/hr at T_ref (75F)

        Returns:
            True if priors were injected, False if skipped.
        """
        if self._profiler_seeded:
            return False

        self._profiler_seeded = True
        area = self._envelope_area

        # Extract R_inv * C_inv * area from resist slope
        # slope_resist ~= C_inv * R_inv * area (the net envelope decay rate)
        profiler_lambda = resist_slope
        current_lambda = float(self.x[IDX_R_INV]) * area * float(self.x[IDX_C_INV])

        if abs(profiler_lambda) < 1e-6 or current_lambda < 1e-10:
            _LOGGER.info("Profiler seeding: skipped (insufficient data)")
            return False

        # Soft blend: 70% profiler + 30% current EKF
        BLEND = 0.7

        # Adjust R_inv and C_inv to match profiler's observed decay rate
        ratio = profiler_lambda / current_lambda
        adj = abs(ratio) ** 0.5  # split correction via geometric mean
        new_r_inv = float(self.x[IDX_R_INV]) * (1.0 + BLEND * (adj - 1.0))
        new_c_inv = float(self.x[IDX_C_INV]) * (1.0 + BLEND * (abs(ratio) / adj - 1.0))

        # Clamp to physical bounds
        lo_r, hi_r = BOUNDS[IDX_R_INV]
        lo_c, hi_c = BOUNDS[IDX_C_INV]
        new_r_inv = max(lo_r, min(hi_r, new_r_inv))
        new_c_inv = max(lo_c, min(hi_c, new_c_inv))

        old_r = float(self.x[IDX_R_INV])
        old_c = float(self.x[IDX_C_INV])
        self.x[IDX_R_INV] = new_r_inv
        self.x[IDX_C_INV] = new_c_inv

        # Inject HVAC capacity from active-mode deltas
        if cool_delta_at_ref is not None and new_c_inv > 1e-10:
            hvac_delta = cool_delta_at_ref - resist_delta_at_ref
            profiler_q_cool = abs(hvac_delta) / new_c_inv
            lo_q, hi_q = BOUNDS[IDX_Q_COOL]
            profiler_q_cool = max(lo_q, min(hi_q, profiler_q_cool))
            old_q_cool = float(self.x[IDX_Q_COOL])
            self.x[IDX_Q_COOL] = old_q_cool * (1.0 - BLEND) + profiler_q_cool * BLEND
            _LOGGER.info(
                "Profiler seeding Q_cool: %.0f -> %.0f (profiler=%.0f)",
                old_q_cool, float(self.x[IDX_Q_COOL]), profiler_q_cool,
            )

        if heat_delta_at_ref is not None and new_c_inv > 1e-10:
            hvac_delta = heat_delta_at_ref - resist_delta_at_ref
            profiler_q_heat = abs(hvac_delta) / new_c_inv
            lo_q, hi_q = BOUNDS[IDX_Q_HEAT]
            profiler_q_heat = max(lo_q, min(hi_q, profiler_q_heat))
            old_q_heat = float(self.x[IDX_Q_HEAT])
            self.x[IDX_Q_HEAT] = old_q_heat * (1.0 - BLEND) + profiler_q_heat * BLEND
            _LOGGER.info(
                "Profiler seeding Q_heat: %.0f -> %.0f (profiler=%.0f)",
                old_q_heat, float(self.x[IDX_Q_HEAT]), profiler_q_heat,
            )

        # Shrink covariance for seeded parameters (50% reduction)
        for idx in (IDX_R_INV, IDX_C_INV, IDX_Q_COOL, IDX_Q_HEAT):
            self.P[idx, idx] *= 0.5

        # Update rate-limit tracking to prevent immediate clamp-back
        self._prev_r_inv = float(self.x[IDX_R_INV])
        self._prev_q_cool = float(self.x[IDX_Q_COOL])
        self._prev_q_heat = float(self.x[IDX_Q_HEAT])

        _LOGGER.info(
            "Profiler seeding complete: R_inv %.4f->%.4f, C_inv %.6f->%.6f, "
            "R-value %.1f->%.1f",
            old_r, new_r_inv, old_c, new_c_inv,
            1.0 / old_r if old_r > 0 else 0, 1.0 / new_r_inv if new_r_inv > 0 else 0,
        )
        return True

    # ── Persistence ─────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialize full state for HA storage."""
        return {
            "state": self.x.tolist(),
            "covariance": self.P.tolist(),
            "process_noise": self.Q.tolist(),
            "measurement_noise": self.R_meas,
            "n_observations": self._n_obs,
            "last_update": self._last_update.isoformat() if self._last_update else None,
            "initial_covariance": self._P_initial.tolist(),
            "envelope_area": self._envelope_area,
            "confidence_hwm": self._confidence_hwm,
            "has_tonnage_prior": self._has_tonnage_prior,
            "profiler_seeded": self._profiler_seeded,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ThermalEstimator:
        """Restore from persisted data — full state, no sample loss.

        Handles migration from 8-state (pre-solar-gain) to 9-state format.
        """
        est = cls()
        est.x = np.array(data["state"])
        est.P = np.array(data["covariance"])
        if "process_noise" in data:
            est.Q = np.array(data["process_noise"])
        else:
            est._setup_default_noise()
        if "measurement_noise" in data:
            est.R_meas = data["measurement_noise"]
        est._n_obs = data.get("n_observations", 0)
        if data.get("last_update"):
            try:
                est._last_update = datetime.fromisoformat(data["last_update"])
            except (ValueError, TypeError):
                pass
        if "initial_covariance" in data:
            est._P_initial = np.array(data["initial_covariance"])
        else:
            # Legacy data without initial covariance — use cold-start defaults
            est._P_initial = np.diag([
                0.1, 25.0, 0.01, 0.25, 1e-4, 1e-6, 1e8, 1e8,
            ])

        # Restore envelope area (default 2000 for legacy data)
        est._envelope_area = data.get("envelope_area", 2000.0)
        est._confidence_hwm = data.get("confidence_hwm", 0.0)

        # ── Migration: 8-state → 9-state (add solar_gain_btu) ────
        if len(est.x) == 8:
            _LOGGER.info(
                "Migrating EKF state vector from 8 to 9 elements "
                "(adding learned solar gain parameter)"
            )
            est.x = np.append(est.x, DEFAULT_SOLAR_GAIN_BTU)
            # Expand P by appending a row and column with high initial uncertainty
            est.P = _expand_matrix(est.P, 1e6)
            est._P_initial = _expand_matrix(est._P_initial, 1e6)
            # Expand Q (process noise) — use default noise for solar gain
            est.Q = _expand_matrix(est.Q, 1e-4)

        est._prev_r_inv = float(est.x[IDX_R_INV])
        est._prev_c_mass_inv = float(est.x[IDX_C_MASS_INV])
        est._prev_q_cool = float(est.x[IDX_Q_COOL])
        est._prev_q_heat = float(est.x[IDX_Q_HEAT])
        est._has_tonnage_prior = data.get("has_tonnage_prior", False)
        est._profiler_seeded = data.get("profiler_seeded", False)
        est._initialized = True
        return est
