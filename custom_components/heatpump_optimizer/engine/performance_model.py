"""Performance model using temperature profile data.

Models HVAC performance using measured temperature deltas — the indoor temperature
change (°F) per hour of HVAC runtime at a given outdoor temperature.

Two different rates live in here and must not be confused:

* **Net delta** (``cooling_delta`` / ``heating_delta``) is what the thermostat
  observes: HVAC output *minus* the envelope load arriving at the same moment.
  It is the right rate for simulating indoor temperature.
* **Gross HVAC rate** (``gross_hvac_rate``) is the machine's own forcing, with
  the concurrent passive drift subtracted back out. It is the right rate for
  judging how hard the equipment is working, and therefore for any efficiency
  comparison.

The distinction is not academic. A net cooling delta collapses from about
-3.2°F/hr to -2.2°F/hr between 75°F and 96°F outdoor, which looks like the
compressor losing most of its capacity. Subtract the drift that grew from
+0.9°F/hr to +1.8°F/hr over the same span and the gross rate barely moves
(~4.1 vs ~4.0°F/hr). Ranking hours by 1/net-delta therefore over-rewards cool
hours by roughly the ratio of the net spread to the gross spread, and that
inflation is what made the old optimizer chase pre-conditioning that costs
energy.
"""

from __future__ import annotations

from ..const import DEFAULT_HVAC_POWER_WATTS

# Electric resistance strip heat draw when staged in. Sized for a typical
# residential 10 kW backup element; overridden per install where known.
DEFAULT_AUX_POWER_WATTS = 10000.0

# The empirical delta tables are anchored at the indoor temperature the
# thermostat usually holds. Both the HVAC tables and the drift table share
# this anchor, which is what lets gross = net - drift come out clean.
NOMINAL_INDOOR_F = 72.0


class PerformanceModel:
    """Models HVAC performance using temperature profile data."""

    def __init__(self, profile_data: dict):
        self._raw = profile_data

        # Cooling deltas: outdoor_temp -> °F/hr (negative = cooling)
        cool_raw = profile_data["temperature"]["cool_1"]["deltas"]
        self._cool_deltas = {int(k): v for k, v in cool_raw.items()}
        self._cool_trendline = profile_data["temperature"]["cool_1"]["linear_trendline"]

        # Heating deltas: outdoor_temp -> °F/hr (positive = warming)
        heat_raw = profile_data["temperature"]["heat_1"]["deltas"]
        self._heat_deltas = {int(k): v for k, v in heat_raw.items()}
        self._heat_trendline = profile_data["temperature"]["heat_1"]["linear_trendline"]

        # Passive drift (resist): outdoor_temp -> °F/hr
        # Negative below ~50°F (house cools), positive above (house warms)
        resist_raw = profile_data["temperature"]["resist"]["deltas"]
        self._resist_deltas = {int(k): v for k, v in resist_raw.items()}
        self._resist_trendline = profile_data["temperature"]["resist"]["linear_trendline"]

        # Aux heat deltas
        if profile_data["temperature"]["auxiliary_heat_1"]:
            aux_raw = profile_data["temperature"]["auxiliary_heat_1"]["deltas"]
            self._aux_heat_deltas = {int(k): v for k, v in aux_raw.items()}
            self._aux_heat_trendline = profile_data["temperature"]["auxiliary_heat_1"]["linear_trendline"]
        else:
            self._aux_heat_deltas = {}
            self._aux_heat_trendline = None

        # Balance points
        self.heat_balance_point = profile_data["balance_point"].get("heat_1")  # 24.9°F
        self.resist_balance_point = profile_data["balance_point"].get("resist")  # 50.2°F

        # System info
        self.cool_differential = profile_data.get("differential", {}).get("cool", 1.0)
        self.heat_differential = profile_data.get("differential", {}).get("heat", 1.0)
        self.cool_setpoint = profile_data.get("setpoint", {}).get("cool", 72.6)
        self.heat_setpoint = profile_data.get("setpoint", {}).get("heat", 60.9)

        # Solar-condition-specific resist trendlines (optional, from profiler)
        # Keys: "sunny", "cloudy", "night" -> {"slope": float, "intercept": float}
        self._solar_resist_trendlines: dict[str, dict[str, float]] = {}
        self._solar_resist_balance_points: dict[str, float] = {}

        # Electrical draw model. Flat rated watts by default; a learned
        # outdoor-temp-binned table can be installed via set_power_table()
        # once a trusted power sensor has been binned. With a flat table the
        # energy ranking degrades gracefully to gross-rate ranking, which is
        # still far better than net-rate ranking.
        power = profile_data.get("power", {}) or {}
        self.rated_power_watts: float = float(
            power.get("rated_watts", DEFAULT_HVAC_POWER_WATTS)
        )
        self.aux_power_watts: float = float(
            power.get("aux_watts", DEFAULT_AUX_POWER_WATTS)
        )
        self._power_by_temp: dict[str, dict[int, float]] = {}

    @classmethod
    def from_defaults(cls) -> PerformanceModel:
        """Create a conservative default model for cold-start learning mode.

        Based on a ~2000 sqft moderately-insulated home with a typical 2-3 ton
        heat pump. Deltas are intentionally conservative (less effective) so the
        optimizer doesn't over-promise during the learning period.
        """
        # Synthesize realistic delta curves
        cool_deltas = {}
        heat_deltas = {}
        resist_deltas = {}

        # Cooling: effective at low outdoor temps, struggles at high
        # Conservative: ~2.5°F/hr at 70°F, ~0.5°F/hr at 100°F
        for t in range(65, 106):
            cool_deltas[t] = -(2.5 - 0.057 * (t - 70))
            cool_deltas[t] = max(cool_deltas[t], -4.0)  # cap best
            cool_deltas[t] = min(cool_deltas[t], -0.3)  # floor worst

        # Heating: effective at mild temps, struggles in extreme cold
        # Conservative: ~1.5°F/hr at 50°F, ~0.3°F/hr at 0°F
        for t in range(-5, 56):
            heat_deltas[t] = 0.3 + 0.024 * (t - 0)
            heat_deltas[t] = max(heat_deltas[t], 0.2)
            heat_deltas[t] = min(heat_deltas[t], 2.0)

        # Passive drift: negative below ~50°F, positive above
        for t in range(-5, 106):
            resist_deltas[t] = 0.03 * (t - 50)

        data = {
            "temperature": {
                "cool_1": {
                    "deltas": {str(k): v for k, v in cool_deltas.items()},
                    "linear_trendline": {"slope": 0.057, "intercept": -6.5},
                },
                "heat_1": {
                    "deltas": {str(k): v for k, v in heat_deltas.items()},
                    "linear_trendline": {"slope": 0.024, "intercept": 0.3},
                },
                "resist": {
                    "deltas": {str(k): v for k, v in resist_deltas.items()},
                    "linear_trendline": {"slope": 0.03, "intercept": -1.5},
                },
                "auxiliary_heat_1": None,
            },
            "balance_point": {
                "heat_1": 25.0,
                "resist": 50.0,
            },
            "differential": {
                "cool": 1.0,
                "heat": 1.0,
            },
            "setpoint": {
                "cool": 74.0,
                "heat": 68.0,
            },
        }
        return cls(data)

    @classmethod
    def from_estimator(cls, estimator) -> PerformanceModel:
        """Build a PerformanceModel from a ThermalEstimator's learned parameters.

        Derives synthetic delta curves from the estimator's R, C, and Q values.
        """
        state = estimator.state_dict()
        r_inv = state.get("R_inv", 0.005)
        q_cool = state.get("Q_cool_base", 24000)
        q_heat = state.get("Q_heat_base", 20000)

        # Derive deltas from physics: dT/dt = (Q_hvac + (T_out - T_in)/R) / C
        # We approximate by assuming T_in ≈ 72°F for cooling, 68°F for heating
        # and C_air from estimator (or reasonable default)
        c_inv = state.get("C_inv", 1.0 / 2000.0)
        c_air = 1.0 / c_inv if c_inv > 0 else 2000.0

        cool_deltas = {}
        heat_deltas = {}
        resist_deltas = {}

        for t in range(65, 106):
            # Cooling delta: net indoor change per hour
            drift = r_inv * (t - 72.0)
            cool_rate = (-q_cool + drift) / c_air
            cool_deltas[t] = max(min(cool_rate, -0.2), -10.0)

            # Passive drift
            resist_deltas[t] = (r_inv * (t - 72.0)) / c_air

        for t in range(-5, 56):
            drift = r_inv * (t - 68.0)
            heat_rate = (q_heat + drift) / c_air
            heat_deltas[t] = max(min(heat_rate, 5.0), 0.1)

            if t not in resist_deltas:
                resist_deltas[t] = (r_inv * (t - 70.0)) / c_air

        data = {
            "temperature": {
                "cool_1": {
                    "deltas": {str(k): v for k, v in cool_deltas.items()},
                    "linear_trendline": {"slope": r_inv / c_air, "intercept": (-q_cool - r_inv * 72) / c_air},
                },
                "heat_1": {
                    "deltas": {str(k): v for k, v in heat_deltas.items()},
                    "linear_trendline": {"slope": r_inv / c_air, "intercept": (q_heat - r_inv * 68) / c_air},
                },
                "resist": {
                    "deltas": {str(k): v for k, v in resist_deltas.items()},
                    "linear_trendline": {"slope": r_inv / c_air, "intercept": -(r_inv * 70) / c_air},
                },
                "auxiliary_heat_1": None,
            },
            "balance_point": {
                "heat_1": 25.0,
                "resist": 50.0,
            },
            "differential": {
                "cool": 1.0,
                "heat": 1.0,
            },
            "setpoint": {
                "cool": 74.0,
                "heat": 68.0,
            },
        }
        return cls(data)

    # ── Delta lookups ──────────────────────────────────────────────

    def _lookup_delta(
        self,
        deltas: dict[int, float],
        trendline: dict[str, float],
        outdoor_temp: float,
    ) -> float:
        """Look up a delta value with interpolation, clamped to observed data.

        Strategy:
        1. If outdoor_temp matches a measured point exactly, use it.
        2. If between two measured points, linearly interpolate.
        3. If outside the measured range, return the nearest edge bin's value.

        Step 3 used to extrapolate along the count-weighted trendline, with no
        bound. That is how a cooling fit with slope 0.0631 and intercept -8.06
        produced -9.6 F/hr at 48 F outdoor: a rate the equipment cannot
        physically achieve, invented from a line fitted 20 degrees away. The
        optimizer then ranked those phantom-efficient hours as the best in the
        day. Clamping keeps lookups inside the range the house has actually
        been observed in, which is what the documentation always claimed the
        model did.

        Callers that need to know whether a lookup was clamped should ask
        ``is_extrapolating()`` and treat the result as low confidence.
        """
        temp_int = int(round(outdoor_temp))

        # Exact match
        if temp_int in deltas:
            return deltas[temp_int]

        # Find surrounding measured points for interpolation
        measured_temps = sorted(deltas.keys())
        if not measured_temps:
            return trendline["slope"] * outdoor_temp + trendline["intercept"]

        # Outside measured range -> clamp to the nearest observed bin
        if outdoor_temp < measured_temps[0]:
            return deltas[measured_temps[0]]
        if outdoor_temp > measured_temps[-1]:
            return deltas[measured_temps[-1]]

        # Between measured points -> linear interpolation
        lower = None
        upper = None
        for t in measured_temps:
            if t <= outdoor_temp:
                lower = t
            if t >= outdoor_temp and upper is None:
                upper = t

        if lower is None or upper is None or lower == upper:
            return trendline["slope"] * outdoor_temp + trendline["intercept"]

        # Interpolate between lower and upper
        frac = (outdoor_temp - lower) / (upper - lower)
        return deltas[lower] + frac * (deltas[upper] - deltas[lower])

    def _observed_range(self, deltas: dict[int, float]) -> tuple[float, float] | None:
        """Outdoor-temp range the given mode has actually been observed in."""
        if not deltas:
            return None
        temps = sorted(deltas.keys())
        return (float(temps[0]), float(temps[-1]))

    def is_extrapolating(self, mode: str, outdoor_temp: float) -> bool:
        """Whether a lookup for this mode/temp falls outside observed data.

        Lookups are clamped rather than extrapolated, so the returned rate is
        still physically plausible. It is simply not evidence. Callers making
        aggressive decisions (pre-conditioning gates, window search) should
        treat a True here as a reason to stay conservative.
        """
        deltas = {
            "cool_1": self._cool_deltas,
            "heat_1": self._heat_deltas,
            "auxiliary_heat_1": self._aux_heat_deltas,
            "resist": self._resist_deltas,
        }.get(mode)
        observed = self._observed_range(deltas or {})
        if observed is None:
            return True
        return not (observed[0] <= outdoor_temp <= observed[1])

    def _indoor_correction(self, indoor_temp: float | None) -> float:
        """Drift-slope correction for running away from the nominal indoor temp.

        The empirical delta tables were recorded at whatever indoor temp the
        thermostat happened to hold, which is ~72 F. The optimizer's entire
        purpose is to visit temperatures that are not that: pre-cool to 70,
        coast to 78. At 70 F indoor the envelope gain is larger, so net
        cooling is genuinely slower than the table says.

        Decomposing net = gross + drift, the gross HVAC forcing is
        indoor-independent to first order; only the drift term moves with
        indoor temp. Since ``passive_drift`` already applies
        ``base - slope * (T_in - 72)``, the same term carries through to the
        net rate unchanged:

            net(T_in) = [net_table - drift_base] + [drift_base - k(T_in - 72)]
                      = net_table - k(T_in - 72)

        Note this is the *same* sign for heating and cooling, not mirrored:
        a warmer house both cools faster and heats slower for the same reason.
        Applying it here keeps the simulator's pre-cool cost and coast payback
        mutually consistent, which they were not while drift was corrected and
        the HVAC rates were not.
        """
        if indoor_temp is None:
            return 0.0
        slope = (self._resist_trendline or {}).get("slope", 0.03)
        return slope * (indoor_temp - 72.0)

    def cooling_delta(self, outdoor_temp: float, indoor_temp: float | None = None) -> float:
        """Indoor °F change per hour of cooling runtime at given outdoor temp.

        Returns negative values (cooling lowers indoor temp). This is the
        NET rate: it already includes the envelope gain arriving during the
        same hour, so it is the right rate for simulating indoor temperature
        and the WRONG rate for judging equipment efficiency. Use
        ``gross_hvac_rate`` for the latter.

        Representative measured values: about -3.2°F/hr at 75°F outdoor and
        -2.2°F/hr at 96°F. Most of that apparent 1.5x collapse is the growing
        envelope load, not lost compressor capacity.

        When indoor_temp is given, the rate is corrected for running away from
        the ~72 F the tables were recorded at. See ``_indoor_correction``.
        """
        return self._lookup_delta(
            self._cool_deltas, self._cool_trendline, outdoor_temp
        ) - self._indoor_correction(indoor_temp)

    def heating_delta(self, outdoor_temp: float, indoor_temp: float | None = None) -> float:
        """Indoor °F change per hour of heating runtime at given outdoor temp.

        Returns positive values (heating raises indoor temp) while the
        compressor keeps up, and can go negative below the balance point,
        where the house loses ground and strip heat has to stage in.

        Like cooling_delta this is a NET rate; see gross_hvac_rate.

        When indoor_temp is given, the rate is corrected for running away from
        the ~72 F the tables were recorded at. See ``_indoor_correction``.
        """
        return self._lookup_delta(
            self._heat_deltas, self._heat_trendline, outdoor_temp
        ) - self._indoor_correction(indoor_temp)

    def aux_heating_delta(self, outdoor_temp: float, indoor_temp: float | None = None) -> float:
        """Indoor °F change per hour of auxiliary (electric resistance) heat runtime.

        Indoor-corrected like the other HVAC rates; see ``_indoor_correction``.
        """
        if not self._aux_heat_deltas or not self._aux_heat_trendline:
            return 0.0
        return self._lookup_delta(
            self._aux_heat_deltas, self._aux_heat_trendline, outdoor_temp
        ) - self._indoor_correction(indoor_temp)

    def passive_drift(
        self,
        outdoor_temp: float,
        indoor_temp: float | None = None,
        solar_condition: str | None = None,
    ) -> float:
        """Indoor F change per hour with HVAC off (passive thermal drift).

        This IS the building's thermal model, measured directly.
        Negative below ~50F (house loses heat), positive above (house gains heat).

        When solar_condition is provided ("sunny", "cloudy", or "night") and
        solar-specific trendlines are available, uses the condition-specific
        trendline for a more accurate prediction.  Falls back to the aggregate
        trendline if the condition has insufficient data.

        When indoor_temp is provided, the drift is adjusted for the difference
        between the actual indoor temp and the nominal ~72F assumed by the
        empirical lookup tables.
        """
        # Select trendline: solar-specific if available, else aggregate
        trendline = self._resist_trendline
        if solar_condition and solar_condition in self._solar_resist_trendlines:
            trendline = self._solar_resist_trendlines[solar_condition]

        # Use trendline directly (not bin lookup) for solar-specific predictions,
        # since solar bins may not have the same bin coverage as the aggregate.
        if solar_condition and solar_condition in self._solar_resist_trendlines:
            base = trendline["slope"] * outdoor_temp + trendline["intercept"]
        else:
            base = self._lookup_delta(
                self._resist_deltas, self._resist_trendline, outdoor_temp
            )

        if indoor_temp is None:
            return base
        slope = trendline["slope"] if trendline else 0.03
        return base - slope * (indoor_temp - 72.0)

    # ── Gross HVAC forcing and energy ──────────────────────────────

    def gross_hvac_rate(
        self,
        mode: str,
        outdoor_temp: float,
        solar_condition: str | None = None,
    ) -> float:
        """The machine's own °F/hr of forcing, envelope load removed.

        ``net_delta(T_out) - passive_drift(T_out, solar)``, sign-aware:
        negative for cooling, positive for heating. Both terms are evaluated
        at the tables' shared nominal indoor anchor, because the gross rate is
        indoor-independent to first order -- only the drift component shifts
        with indoor temperature, and it has been subtracted out.

        This is the correct input to any efficiency comparison. The net delta
        is not: at a hot outdoor temperature most of the machine's output is
        merely cancelling a simultaneous envelope gain, so the net rate reads
        as capacity loss that the equipment did not actually suffer.
        """
        drift = self.passive_drift(outdoor_temp, solar_condition=solar_condition)

        if mode == "cool":
            return self.cooling_delta(outdoor_temp) - drift
        if mode == "heat":
            return self.heating_delta(outdoor_temp) - drift
        if mode == "aux_heat":
            return self.aux_heating_delta(outdoor_temp) - drift
        return 0.0

    def set_power_table(
        self,
        mode: str,
        table: dict[int, float] | None,
    ) -> None:
        """Install a learned outdoor-temp-binned power draw table (watts).

        Extension seam for grounding the energy currency in a measured power
        sensor. Passing None clears the table and reverts that mode to flat
        rated watts.
        """
        if table:
            self._power_by_temp[mode] = {int(k): float(v) for k, v in table.items()}
        else:
            self._power_by_temp.pop(mode, None)

    def hvac_power_watts(
        self,
        mode: str,
        outdoor_temp: float,
        aux_active: bool = False,
    ) -> float:
        """Electrical draw while running at this outdoor temp, in watts.

        Uses a learned per-mode table when one has been installed, otherwise
        flat rated watts. Aux strip heat is charged at its own resistive draw
        in addition to the compressor, since both run together on droop.
        """
        base = self.rated_power_watts
        table = self._power_by_temp.get(mode)
        if table:
            temps = sorted(table.keys())
            t = int(round(outdoor_temp))
            if t in table:
                base = table[t]
            elif t <= temps[0]:
                base = table[temps[0]]
            elif t >= temps[-1]:
                base = table[temps[-1]]
            else:
                lower = max(x for x in temps if x <= t)
                upper = min(x for x in temps if x >= t)
                frac = (t - lower) / (upper - lower) if upper != lower else 0.0
                base = table[lower] + frac * (table[upper] - table[lower])

        if aux_active:
            return base + self.aux_power_watts
        return base

    def energy_per_degree_kwh(
        self,
        mode: str,
        outdoor_temp: float,
        solar_condition: str | None = None,
    ) -> float:
        """kWh spent per °F of indoor forcing delivered. Lower is better.

        ``power_kW / |gross_hvac_rate|``. This is the honest hour-efficiency
        currency: it answers "what does a degree of work cost right now?"
        rather than "how fast does the thermostat needle move right now?",
        which is what the old 1/net-delta score measured.

        Returns ``float('inf')`` when the machine has no usable forcing.
        """
        gross = abs(self.gross_hvac_rate(mode, outdoor_temp, solar_condition))
        if gross < 1e-6:
            return float("inf")
        watts = self.hvac_power_watts(mode, outdoor_temp)
        return (watts / 1000.0) / gross

    # ── Derived metrics ────────────────────────────────────────────

    def relative_efficiency(self, outdoor_temp: float, mode: str) -> float:
        """Relative efficiency vs. best measured conditions (0-1 scale).

        For cooling: 1.0 at the coolest measured outdoor temp, lower at hotter temps.
        For heating: 1.0 at the warmest measured outdoor temp, lower at colder temps.
        """
        if mode == "cool":
            if not self._cool_deltas:
                return 0.0
            delta = self.cooling_delta(outdoor_temp)
            # Best (most negative) cooling delta in measured data
            best = min(self._cool_deltas.values())  # e.g., -9.6
            if best == 0:
                return 0.0
            return abs(delta) / abs(best)
        elif mode == "heat":
            if not self._heat_deltas:
                return 0.0
            delta = self.heating_delta(outdoor_temp)
            best = max(self._heat_deltas.values())  # e.g., 2.7
            if best == 0:
                return 0.0
            return delta / best
        return 0.0

    def runtime_needed(self, outdoor_temp: float, mode: str, degrees: float) -> float:
        """Minutes of runtime to change indoor temp by `degrees` °F.

        Args:
            outdoor_temp: Current outdoor temperature (°F)
            mode: "cool" or "heat"
            degrees: Desired temperature change (positive value).
                     For cooling, this is how many degrees to lower.
                     For heating, this is how many degrees to raise.

        Returns:
            Minutes of compressor runtime needed. Returns float('inf') if
            the system cannot achieve this change at the given outdoor temp.
        """
        if mode == "cool":
            delta = self.cooling_delta(outdoor_temp)  # negative °F/hr (net, includes drift)
            if delta >= 0:
                return float("inf")  # system can't cool at this temp
            hours = degrees / abs(delta)
        elif mode == "heat":
            delta = self.heating_delta(outdoor_temp)  # positive °F/hr (net, includes drift)
            if delta <= 0:
                return float("inf")
            hours = degrees / delta
        else:
            return float("inf")

        return hours * 60.0

    def coast_duration(self, outdoor_temp: float, mode: str, degrees: float) -> float:
        """Minutes the house can coast (HVAC off) before drifting by `degrees` °F.

        Args:
            outdoor_temp: Outdoor temperature during coast
            mode: "cool" (house will warm) or "heat" (house will cool)
            degrees: How many degrees of drift to allow

        Returns:
            Minutes of coast time. Returns float('inf') if drift goes the
            opposite direction (e.g., house cools when we expected warming).
        """
        drift = self.passive_drift(outdoor_temp)

        if mode == "cool":
            # In cool mode, we're coasting upward (house warming)
            if drift <= 0:
                return float("inf")  # house isn't warming, coast forever
            hours = degrees / drift
        elif mode == "heat":
            # In heat mode, we're coasting downward (house cooling)
            if drift >= 0:
                return float("inf")  # house isn't cooling, coast forever
            hours = degrees / abs(drift)
        else:
            return float("inf")

        return hours * 60.0

    def net_cooling_rate(self, outdoor_temp: float) -> float:
        """Net indoor temp change per hour when cooling is running.

        The cooling delta represents the observed net rate during cooling
        operation (includes passive drift effects).
        More negative = cooling wins more decisively.
        """
        return self.cooling_delta(outdoor_temp)

    def net_heating_rate(self, outdoor_temp: float) -> float:
        """Net indoor temp change per hour when heating is running.

        The heating delta represents the observed net rate during heating
        operation (includes passive drift effects).
        More positive = heating wins more decisively.
        """
        return self.heating_delta(outdoor_temp)

    # ── Summary ────────────────────────────────────────────────────

    def summary(self) -> str:
        """Human-readable summary of the performance model."""
        cool_temps = sorted(self._cool_deltas.keys())
        heat_temps = sorted(self._heat_deltas.keys())

        lines = [
            "=== Performance Model Summary ===",
            "",
            f"Cooling range: {cool_temps[0]}°F to {cool_temps[-1]}°F outdoor",
            f"  Best:  {cool_temps[0]}°F -> {self._cool_deltas[cool_temps[0]]:+.2f}°F/hr",
            f"  Worst: {cool_temps[-1]}°F -> {self._cool_deltas[cool_temps[-1]]:+.2f}°F/hr",
            f"  Efficiency ratio: {abs(self._cool_deltas[cool_temps[0]]) / abs(self._cool_deltas[cool_temps[-1]]):.1f}x",
            "",
            f"Heating range: {heat_temps[0]}°F to {heat_temps[-1]}°F outdoor",
            f"  Best:  {heat_temps[-1]}°F -> {self._heat_deltas[heat_temps[-1]]:+.2f}°F/hr",
            f"  Worst: {heat_temps[0]}°F -> {self._heat_deltas[heat_temps[0]]:+.2f}°F/hr",
            "",
            f"Balance points: heat={self.heat_balance_point}°F, resist={self.resist_balance_point}°F",
            f"Differentials: cool={self.cool_differential}°F, heat={self.heat_differential}°F",
            f"Avg setpoints: cool={self.cool_setpoint}°F, heat={self.heat_setpoint}°F",
        ]
        return "\n".join(lines)
