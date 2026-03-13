"""Extended Kalman Filter for online building thermal parameter estimation.

Models the building as a two-node RC thermal circuit (air + thermal mass)
and continuously estimates the physical parameters from thermostat readings:

  C_air · dT_air/dt  = (T_out - T_air)/R + (T_mass - T_air)/R_int + Q_hvac + Q_solar
  C_mass · dT_mass/dt = (T_air - T_mass)/R_int

State vector (8 elements):
  [T_air, T_mass, R_inv, R_int_inv, C_inv, C_mass_inv, Q_cool_base, Q_heat_base]

The filter estimates building envelope resistance (R), internal coupling (R_int),
air and mass thermal capacitance (C, C_mass), and HVAC capacity at a reference
temperature. These replace the static Beestat lookup tables with continuously
adapting parameters.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

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

N_STATES = 8

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

# Physical bounds for parameter clamping
BOUNDS = {
    IDX_R_INV: (0.01, 2.0),       # R: 0.5 to 100 °F·hr/BTU
    IDX_R_INT_INV: (0.05, 5.0),   # R_int: 0.2 to 20
    IDX_C_INV: (1e-5, 0.01),      # C_air: 100 to 100,000 BTU/°F
    IDX_C_MASS_INV: (1e-6, 0.001),  # C_mass: 1,000 to 1,000,000
    IDX_Q_COOL: (5000, 80000),    # 5k to 80k BTU/hr
    IDX_Q_HEAT: (5000, 80000),
}


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

    # Measurement noise variance (°F²)
    R_meas: float = 0.25  # ±0.5°F thermostat accuracy

    # Innovation (prediction error) history for accuracy reporting
    _innovations: list[tuple[datetime, float]] = field(default_factory=list)
    _n_obs: int = 0
    _last_update: datetime | None = None
    _initialized: bool = False
    _P_initial: np.ndarray = field(default_factory=lambda: np.eye(N_STATES))

    # Current environmental conditions (set each update, used by _hvac_output)
    _current_wind_speed: float | None = None
    _current_humidity: float | None = None
    _current_pressure: float | None = None

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
            0.005,   # T_mass — less noisy (thermal mass is stable)
            1e-8,    # R_inv — very slow drift
            1e-8,    # R_int_inv — very slow drift
            1e-10,   # C_inv — extremely slow (thermal mass doesn't change)
            1e-12,   # C_mass_inv — extremely slow
            1.0,     # Q_cool_base — moderate (filter condition, refrigerant)
            1.0,     # Q_heat_base — moderate
        ])
        self.Q = np.diag(q_diag)

    # ── Initialization ──────────────────────────────────────────────

    @classmethod
    def cold_start(cls, indoor_temp: float = 72.0) -> ThermalEstimator:
        """Initialize with conservative defaults (no Beestat data).

        Suitable for a ~2000 sq ft home. The filter will converge to
        the true values within ~2 weeks of mixed weather.
        """
        est = cls()
        est.x = np.array([
            indoor_temp,  # T_air
            indoor_temp,  # T_mass (assume equilibrium at start)
            0.10,         # R_inv → R ≈ 10 °F·hr/BTU (moderate insulation)
            0.50,         # R_int_inv → R_int ≈ 2 (moderate coupling)
            0.001,        # C_inv → C ≈ 1000 BTU/°F
            0.0001,       # C_mass_inv → C_mass ≈ 10,000 BTU/°F
            20000.0,      # Q_cool_base ≈ 20k BTU/hr (~1.7 ton)
            18000.0,      # Q_heat_base ≈ 18k BTU/hr
        ])
        # High initial uncertainty — let the filter find the truth
        est.P = np.diag([
            0.1,       # T_air — we trust the thermostat
            25.0,      # T_mass — very uncertain (hidden state)
            0.01,      # R_inv — wide range possible
            0.25,      # R_int_inv
            1e-4,      # C_inv
            1e-6,      # C_mass_inv
            1e8,       # Q_cool_base — very uncertain without data
            1e8,       # Q_heat_base
        ])
        est._P_initial = est.P.copy()
        est._initialized = True
        est._setup_default_noise()
        return est

    @classmethod
    def from_beestat(
        cls,
        profile_data: dict,
        indoor_temp: float = 72.0,
    ) -> ThermalEstimator:
        """Initialize from Beestat temperature profile (better priors).

        Extracts approximate R, C, Q from the measured deltas.
        The filter will converge faster (~3-5 days) with these priors.
        """
        est = cls()

        # Extract resist (passive drift) trendline to estimate R and C
        resist = profile_data["temperature"]["resist"]
        resist_slope = resist["linear_trendline"]["slope"]  # °F/hr per °F outdoor
        # slope ≈ 1/(R*C), so R*C ≈ 1/slope
        rc_product = 1.0 / max(abs(resist_slope), 0.001)

        # Use balance point to sanity-check
        resist_balance = profile_data["balance_point"].get("resist", 50.0)

        # Estimate C from typical residential range, derive R
        c_air = 1200.0  # BTU/°F — reasonable for 2000 sq ft
        r_envelope = rc_product / c_air
        r_envelope = max(2.0, min(20.0, r_envelope))

        # Estimate cooling capacity from deltas
        cool_deltas = profile_data["temperature"]["cool_1"]["deltas"]
        if cool_deltas:
            # At T_ref (75°F), the net cooling rate includes drift
            # cooling_delta ≈ -(Q_cool/C) + drift
            # Q_cool ≈ C * |cooling_delta - drift|
            ref_temps = [int(t) for t in cool_deltas.keys() if 70 <= int(t) <= 80]
            if ref_temps:
                avg_cool_delta = sum(float(cool_deltas[str(t)]) for t in ref_temps) / len(ref_temps)
            else:
                avg_cool_delta = -2.0  # default
            q_cool = c_air * abs(avg_cool_delta)
            q_cool = max(10000, min(60000, q_cool))
        else:
            q_cool = 20000.0

        # Estimate heating capacity similarly
        heat_deltas = profile_data["temperature"]["heat_1"]["deltas"]
        if heat_deltas:
            ref_temps = [int(t) for t in heat_deltas.keys() if 30 <= int(t) <= 50]
            if ref_temps:
                avg_heat_delta = sum(float(heat_deltas[str(t)]) for t in ref_temps) / len(ref_temps)
            else:
                avg_heat_delta = 1.0
            q_heat = c_air * abs(avg_heat_delta)
            q_heat = max(10000, min(60000, q_heat))
        else:
            q_heat = 18000.0

        est.x = np.array([
            indoor_temp,
            indoor_temp,
            1.0 / r_envelope,    # R_inv
            0.5,                  # R_int_inv (default — hard to extract from Beestat)
            1.0 / c_air,         # C_inv
            1.0 / 10000.0,       # C_mass_inv (default)
            q_cool,
            q_heat,
        ])

        # Lower uncertainty since we have informed priors
        est.P = np.diag([
            0.1,       # T_air
            10.0,      # T_mass — still uncertain
            0.002,     # R_inv — moderate confidence
            0.1,       # R_int_inv — low confidence (not in Beestat)
            1e-5,      # C_inv — moderate confidence
            1e-7,      # C_mass_inv — low confidence
            q_cool * 0.3 * q_cool * 0.3,  # Q_cool — ±30% uncertainty
            q_heat * 0.3 * q_heat * 0.3,  # Q_heat — ±30% uncertainty
        ])
        est._P_initial = est.P.copy()
        est._initialized = True
        est._setup_default_noise()
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
            humidity: Relative humidity 0-100, or None.
            pressure_hpa: Atmospheric pressure in hPa, or None.

        Returns:
            Innovation (prediction error before update) in °F.
        """
        # Store environmental conditions for _hvac_output to access
        self._current_wind_speed = wind_speed_mph
        self._current_humidity = humidity
        self._current_pressure = pressure_hpa

        # ── PREDICT ──────────────────────────────────────────────
        x_pred = self._predict_state(
            self.x, outdoor_temp, hvac_mode, hvac_running,
            cloud_cover, sun_elevation, dt_hours,
        )
        F = self._jacobian(
            self.x, outdoor_temp, hvac_mode, hvac_running,
            cloud_cover, sun_elevation, dt_hours,
        )
        P_pred = F @ self.P @ F.T + self.Q

        # ── UPDATE ───────────────────────────────────────────────
        # Observation model: z = H @ x = T_air
        H = np.zeros((1, N_STATES))
        H[0, IDX_T_AIR] = 1.0

        # Innovation
        z = observed_temp
        z_pred = x_pred[IDX_T_AIR]
        innovation = z - z_pred

        # Innovation covariance
        S = H @ P_pred @ H.T + self.R_meas
        S_scalar = float(S[0, 0])

        # Kalman gain
        K = P_pred @ H.T / S_scalar  # (N,1)

        # State update
        self.x = x_pred + (K * innovation).flatten()

        # Covariance update (Joseph form for numerical stability)
        I_KH = np.eye(N_STATES) - K @ H
        self.P = I_KH @ P_pred @ I_KH.T + (K * self.R_meas) @ K.T

        # Clamp parameters to physical bounds
        self._clamp_parameters()

        # Record innovation for accuracy tracking
        now = datetime.now(timezone.utc)
        self._innovations.append((now, float(innovation)))
        self._trim_innovations()
        self._n_obs += 1
        self._last_update = now

        return float(innovation)

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

        # Heat flows (BTU/hr)
        Q_env = R_inv * (outdoor_temp - T_air)       # envelope
        Q_int = R_int_inv * (T_mass - T_air)          # internal coupling
        Q_hvac = self._hvac_output(hvac_mode, hvac_running, outdoor_temp,
                                    Q_cool_base, Q_heat_base)
        Q_solar = self._solar_gain(cloud_cover, sun_elevation)

        # Temperature updates
        dT_air = C_inv * (Q_env + Q_int + Q_hvac + Q_solar) * dt_hours
        dT_mass = C_mass_inv * (-Q_int) * dt_hours

        x_new = x.copy()
        x_new[IDX_T_AIR] = T_air + dT_air
        x_new[IDX_T_MASS] = T_mass + dT_mass
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
        Q_cool_base = x[IDX_Q_COOL]
        Q_heat_base = x[IDX_Q_HEAT]

        # Total heat into air node
        Q_env = R_inv * (outdoor_temp - T_air)
        Q_int = R_int_inv * (T_mass - T_air)
        Q_hvac = self._hvac_output(hvac_mode, hvac_running, outdoor_temp,
                                    Q_cool_base, Q_heat_base)
        Q_solar = self._solar_gain(cloud_cover, sun_elevation)
        Q_total = Q_env + Q_int + Q_hvac + Q_solar

        F = np.eye(N_STATES)
        dt = dt_hours

        # ── dT_air_new / d(state) ──────────────────────────────
        # T_air_new = T_air + C_inv * (Q_env + Q_int + Q_hvac + Q_solar) * dt
        F[IDX_T_AIR, IDX_T_AIR] = 1.0 + C_inv * (-R_inv - R_int_inv) * dt
        F[IDX_T_AIR, IDX_T_MASS] = C_inv * R_int_inv * dt
        F[IDX_T_AIR, IDX_R_INV] = C_inv * (outdoor_temp - T_air) * dt
        F[IDX_T_AIR, IDX_R_INT_INV] = C_inv * (T_mass - T_air) * dt
        F[IDX_T_AIR, IDX_C_INV] = Q_total * dt

        # dT_air / dQ_cool_base and dQ_heat_base
        if hvac_running and hvac_mode == "cool":
            cop_factor = max(0.1, 1.0 - ALPHA_COOL * (outdoor_temp - T_REF_F))
            F[IDX_T_AIR, IDX_Q_COOL] = C_inv * (-cop_factor) * dt
        elif hvac_running and hvac_mode == "heat":
            cop_factor = max(0.1, 1.0 - ALPHA_HEAT * (T_REF_F - outdoor_temp))
            F[IDX_T_AIR, IDX_Q_HEAT] = C_inv * cop_factor * dt

        # No dependency on C_mass_inv for T_air equation

        # ── dT_mass_new / d(state) ─────────────────────────────
        # T_mass_new = T_mass + C_mass_inv * (-(R_int_inv * (T_mass - T_air))) * dt
        F[IDX_T_MASS, IDX_T_AIR] = C_mass_inv * R_int_inv * dt
        F[IDX_T_MASS, IDX_T_MASS] = 1.0 - C_mass_inv * R_int_inv * dt
        F[IDX_T_MASS, IDX_R_INT_INV] = C_mass_inv * (-(T_mass - T_air)) * dt
        F[IDX_T_MASS, IDX_C_MASS_INV] = (-R_int_inv * (T_mass - T_air)) * dt

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

        # Effective outdoor temp for COP calculation (not for envelope Q_env)
        cop_outdoor_temp = outdoor_temp

        # Wind chill correction for heating mode
        wind_speed = getattr(self, "_current_wind_speed", None)
        if (
            mode == "heat"
            and wind_speed is not None
            and outdoor_temp < 50.0
            and wind_speed > 3.0
        ):
            effective_temp = (
                35.74
                + 0.6215 * outdoor_temp
                - 35.75 * (wind_speed ** 0.16)
                + 0.4275 * outdoor_temp * (wind_speed ** 0.16)
            )
            cop_outdoor_temp = min(outdoor_temp, effective_temp)

        if mode == "cool":
            # Capacity decreases as outdoor temp rises above reference
            raw_factor = 1.0 - ALPHA_COOL * (cop_outdoor_temp - T_REF_F)
            cop_factor = max(0.1, raw_factor)
            if raw_factor <= 0.1:
                _LOGGER.warning(
                    "COP degradation at floor (0.1) for cooling: outdoor=%.1f°F "
                    "— possible sensor issue or extreme conditions",
                    outdoor_temp,
                )

            # Humidity correction for cooling: high humidity reduces COP
            humidity = getattr(self, "_current_humidity", None)
            if humidity is not None and humidity > 50.0:
                cop_factor *= max(0.8, 1.0 - (humidity - 50.0) / 500.0)

            # Pressure correction
            pressure = getattr(self, "_current_pressure", None)
            if pressure is not None:
                cop_factor *= (pressure / 1013.25) ** 0.3

            return -Q_cool_base * cop_factor

        if mode == "heat":
            # Capacity decreases as outdoor temp drops below reference
            raw_factor = 1.0 - ALPHA_HEAT * (T_REF_F - cop_outdoor_temp)
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
                cop_factor *= (pressure / 1013.25) ** 0.3

            return Q_heat_base * cop_factor

        return 0.0

    @staticmethod
    def _solar_gain(
        cloud_cover: float | None,
        sun_elevation: float | None,
    ) -> float:
        """Estimate solar heat gain (BTU/hr)."""
        if cloud_cover is None or sun_elevation is None or sun_elevation <= 0:
            return 0.0

        clear_sky = 1.0 - cloud_cover
        altitude_factor = math.sin(math.radians(max(0, min(90, sun_elevation))))

        return DEFAULT_SOLAR_GAIN_BTU * clear_sky * altitude_factor

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
    def R_value(self) -> float:
        """Envelope thermal resistance (°F·hr/BTU)."""
        return 1.0 / max(self.R_inv, 1e-6)

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
        """
        if self._n_obs < 10:
            return 0.0

        # Per-parameter relative variance reduction (indices 2-7)
        current_diag = np.diag(self.P)[2:8]
        initial_diag = np.diag(self._P_initial)[2:8]

        # For each parameter: how much has its variance shrunk?
        # ratio=1 means no learning, ratio→0 means well-converged
        ratios = current_diag / np.maximum(initial_diag, 1e-30)

        # Convert to per-parameter confidence and average
        param_confidences = 1.0 - np.minimum(ratios, 1.0)
        confidence = float(np.mean(param_confidences))
        return max(0.0, min(1.0, confidence))

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
        }

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
        """Enforce physical bounds on estimated parameters."""
        for idx, (lo, hi) in BOUNDS.items():
            self.x[idx] = np.clip(self.x[idx], lo, hi)

    def _trim_innovations(self, max_hours: int = 24):
        """Keep only last 24 hours of innovations."""
        if not self._innovations:
            return
        cutoff = datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=max_hours)
        self._innovations = [(t, v) for t, v in self._innovations if t > cutoff]

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
        }

    @classmethod
    def from_dict(cls, data: dict) -> ThermalEstimator:
        """Restore from persisted data — full state, no sample loss."""
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
        est._initialized = True
        return est
