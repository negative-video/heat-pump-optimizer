"""Helpers mixin for the Heat Pump Optimizer coordinator.

Houses pure-logic helpers: activation-tier classification, phase
classification, comfort-range selection, rate limiting, dwell-time
bookkeeping, HVAC state inspection, and diagnostic predicates. All methods
operate entirely off ``self`` and the coordinator's subsystem attributes;
they do not own state of their own.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import homeassistant.helpers.issue_registry as ir

from .const import (
    ACTIVATION_TIER_CONFIDENT,
    ACTIVATION_TIER_CONSERVATIVE,
    ACTIVATION_TIER_LEARNING,
    ACTIVATION_TIER_STANDARD,
    CONSERVATIVE_DRIFT_CONFIDENCE,
    CONSERVATIVE_MIN_MODE_OBS,
    DEFAULT_DUCT_AUX_RATE_MIN,
    DEFAULT_DUCT_AUX_RATE_THRESHOLD_F,
    DEFAULT_DUCT_AUX_THRESHOLD_F,
    DEFAULT_FORECAST_DEVIATION_THRESHOLD,
    DEFAULT_MODEL_CONFIDENCE_THRESHOLD,
    DEFAULT_STALE_FORECAST_HOURS,
    DOMAIN,
    EVENT_ACCURACY_TIER_CHANGED,
    EVENT_BASELINE_COMPLETE,
    EVENT_MODEL_ALERT,
    PHASE_COASTING,
    PHASE_MAINTAINING,
    PHASE_PRE_COOLING,
    PHASE_PRE_HEATING,
)
from .engine.comfort import derive_effective_comfort_band
from .engine.data_types import ScheduleEntry
from .savings_tracker import (
    TIER_CALIBRATED,
    TIER_ESTIMATED,
    TIER_LEARNING,
    TIER_PROJECTED,
    TIER_SIMULATED,
)

LEARNING_PERSIST_INTERVAL_HOURS = 1

_LOGGER = logging.getLogger(__name__)


class HelpersMixin:
    """Pure helpers previously inlined in HeatPumpOptimizerCoordinator."""

    # ── Helpers ─────────────────────────────────────────────────────

    def _active_comfort_range(self) -> tuple[float, float]:
        """Get the current comfort range: occupancy, DR, constraints, safety, tier.

        All narrowing goes through ``derive_effective_comfort_band`` so the
        layers cannot compound invisibly and the result has a documented
        minimum width. See engine/comfort.py for the ordering.
        """
        mode = self.thermostat.get_active_mode()
        base = self.comfort_heat if mode == "heat" else self.comfort_cool
        comfort = self.occupancy.adjust_comfort_range(base, mode)

        # Demand response expiry has a side effect, so it is resolved here
        # rather than inside the pure band derivation.
        dr_delta = 0.0
        if self._demand_response_active:
            now = datetime.now(timezone.utc)
            if self._demand_response_end and now >= self._demand_response_end:
                # Auto-restore
                self._demand_response_active = False
                self._demand_response_end = None
                _LOGGER.info("Demand response auto-expired — restoring normal comfort")
                self._fire_event(f"{DOMAIN}_demand_response", {"mode": "auto_restored"})
            else:
                dr_delta = self._demand_response_delta

        if self._is_conservative_mode:
            tier = ACTIVATION_TIER_CONSERVATIVE
        elif self._is_learning_active():
            tier = ACTIVATION_TIER_LEARNING
        else:
            tier = None

        effective = derive_effective_comfort_band(
            comfort,
            demand_response_delta=dr_delta,
            constraints=self._active_constraints,
            safety_limits=self.safety_limits,
            tier=tier,
        )

        if effective != comfort:
            _LOGGER.debug(
                "Effective comfort band %.1f-%.1fF (from %.1f-%.1f; "
                "dr=%.1f, tier=%s, constraints=%d)",
                effective[0], effective[1], comfort[0], comfort[1],
                dr_delta, tier or "none", len(self._active_constraints),
            )

        return effective

    @property
    def learning_active(self) -> bool:
        """Whether the model is still in learning mode."""
        return self._is_learning_active()

    def _is_learning_active(self) -> bool:
        """Check if the model is still in learning mode (not yet controlling).

        Respects the graduated activation tier: if the optimizer has
        activated (conservative/standard/confident), it is no longer
        in learning mode even if global EKF confidence is below 50%.
        """
        return self._activation_tier == ACTIVATION_TIER_LEARNING

    @property
    def _activation_tier(self) -> str:
        """Determine the optimizer's current activation tier (cached per cycle).

        Graduated activation allows the optimizer to provide value sooner
        by operating conservatively when it has partial data, rather than
        waiting for full confidence on all parameters.

        Tiers:
        - learning: baseline not captured, or insufficient data
        - conservative: baseline captured + good passive drift data + some HVAC obs
        - standard: global EKF confidence >= 50% or mode-specific readiness
        - confident: global EKF >= 70% or profiler per-mode >= 70%
        """
        if self._cached_activation_tier is not None:
            return self._cached_activation_tier

        tier = self._compute_activation_tier()
        self._cached_activation_tier = tier
        return tier

    def _compute_activation_tier(self) -> str:
        """Compute the activation tier (called once per cycle, cached by property).

        Profiler-primary: the profiler is the main confidence signal.
        The EKF confidence is checked as a secondary signal.
        Baseline capture is no longer required -- the profiler bootstraps
        from recorder history and can produce a model within minutes.

        All profiler confidence reads here are raw (unsmoothed).  The
        displayed overall confidence is smoothed with a bounded high-water
        mark for a calm UI; behavior must never inherit that smoothing, or
        stale confidence would keep the optimizer aggressive after a season
        change, an HVAC repair, insulation work, or a partial model reset.
        """
        profiler_conf = self.profiler.confidence(raw=True)

        # Confident: profiler has broad coverage or strong EKF
        if profiler_conf >= 0.7 or self.estimator.confidence >= 0.7:
            return ACTIVATION_TIER_CONFIDENT

        # Standard: profiler has moderate coverage or mode-specific readiness
        current_mode = self.thermostat.get_active_mode()
        profiler_mode = "heat_1" if current_mode == "heat" else "cool_1"
        profiler_mode_conf = self.profiler.confidence(profiler_mode)
        if profiler_conf >= 0.5 or profiler_mode_conf >= 0.5:
            return ACTIVATION_TIER_STANDARD

        # Conservative: good passive drift data + some active mode observations
        drift_conf = self.profiler.confidence("resist")
        mode_obs = self.profiler.mode_observation_count(profiler_mode)
        if drift_conf >= CONSERVATIVE_DRIFT_CONFIDENCE and mode_obs >= CONSERVATIVE_MIN_MODE_OBS:
            return ACTIVATION_TIER_CONSERVATIVE

        # Fallback: check if baseline is captured (legacy path)
        if self.baseline_capture.is_ready and self.estimator.confidence >= DEFAULT_MODEL_CONFIDENCE_THRESHOLD:
            return ACTIVATION_TIER_STANDARD

        return ACTIVATION_TIER_LEARNING

    @property
    def _baseline_ready_for_control(self) -> bool:
        """Optimizer may write setpoints when activation tier is above learning."""
        return self._activation_tier != ACTIVATION_TIER_LEARNING

    @property
    def _is_conservative_mode(self) -> bool:
        """True when the model has limited data and optimization is restrained.

        Conservative activation *narrows* the band the optimizer may work in
        to the inner 80% of the configured comfort range.  A wider band lets
        the optimizer push setpoints further from the midpoint, so narrowing
        is what makes it less aggressive.  All narrowing layers and their
        ordering live in engine/comfort.py.
        """
        return self._activation_tier == ACTIVATION_TIER_CONSERVATIVE

    def _build_profiler_chart_data(self) -> dict:
        """Build temperature profile chart data for the frontend.

        Returns per-mode bin data (outdoor temp -> mean delta, count) and
        trendlines for rendering a BeeStat-style observation coverage chart.
        """
        profile = self.profiler.to_profile_data()
        chart = {}
        for mode in ("cool_1", "heat_1", "auxiliary_heat_1", "resist"):
            coverage = self.profiler.bin_coverage(mode)
            temp_range = self.profiler.observed_temp_range(mode)
            trendline = None
            if profile["temperature"].get(mode) is not None:
                trendline = profile["temperature"][mode].get("linear_trendline")
            chart[mode] = {
                "bins": coverage["bins"],
                "total_observations": coverage["total_observations"],
                "temp_range": list(temp_range) if temp_range else None,
                "trendline": trendline,
            }
        chart["balance_points"] = profile.get("balance_point", {})
        return chart

    def _get_calibration_status(self) -> str:
        """Human-readable coefficient calibration status."""
        cal = self.coeff_calibrator
        n_cond = len(self.estimator._conditioned_innovations)
        if cal._first_innovation_time is None:
            return "waiting_for_data"
        hours_elapsed = (
            datetime.now(timezone.utc) - cal._first_innovation_time
        ).total_seconds() / 3600
        if hours_elapsed < 72:
            hours_left = round(72 - hours_elapsed)
            return f"collecting_data ({hours_left}h until first calibration)"
        if n_cond < 200:
            return f"collecting_samples ({n_cond}/200 innovations)"
        return "active"

    def _update_accuracy_tier(self) -> None:
        """Update the savings accuracy tier based on baseline and model confidence."""
        baseline_conf = self.baseline_capture.confidence
        model_conf = self.estimator.confidence
        profiler_conf = self.profiler.confidence()

        if baseline_conf >= 0.7 and model_conf >= 0.5:
            tier = TIER_CALIBRATED
        elif baseline_conf >= 0.3 and model_conf >= 0.3:
            tier = TIER_SIMULATED
        elif self.baseline_capture.is_ready:
            tier = TIER_ESTIMATED
        elif profiler_conf >= 0.5 or model_conf > 0:
            tier = TIER_PROJECTED
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

    def _try_seed_ekf_from_profiler(self) -> None:
        """Seed EKF parameters from profiler data when profiler is confident but EKF is not."""
        if self.estimator._profiler_seeded:
            return
        if self.estimator.confidence >= 0.3:
            return  # EKF already has reasonable confidence
        if self.profiler.confidence("resist") < 0.7:
            return  # profiler not ready

        profile = self.profiler.to_profile_data()
        resist_data = profile["temperature"].get("resist")
        if resist_data is None or resist_data.get("linear_trendline") is None:
            return

        resist_trend = resist_data["linear_trendline"]
        t_ref = 75.0
        resist_delta_ref = resist_trend["slope"] * t_ref + resist_trend["intercept"]

        cool_delta_ref = None
        cool_data = profile["temperature"].get("cool_1")
        if cool_data and cool_data.get("linear_trendline"):
            ct = cool_data["linear_trendline"]
            cool_delta_ref = ct["slope"] * t_ref + ct["intercept"]

        heat_delta_ref = None
        heat_data = profile["temperature"].get("heat_1")
        if heat_data and heat_data.get("linear_trendline"):
            ht = heat_data["linear_trendline"]
            heat_delta_ref = ht["slope"] * t_ref + ht["intercept"]

        self.estimator.inject_profiler_priors(
            resist_slope=resist_trend["slope"],
            resist_intercept=resist_trend["intercept"],
            cool_delta_at_ref=cool_delta_ref,
            heat_delta_at_ref=heat_delta_ref,
            resist_delta_at_ref=resist_delta_ref,
        )

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

    def _classify_phase(
        self, entry: ScheduleEntry, indoor_temp: float | None = None,
    ) -> str:
        """Determine phase from schedule entry reason text and current conditions."""
        from .const import PHASE_COASTING, PHASE_MAINTAINING, PHASE_PRE_COOLING, PHASE_PRE_HEATING
        reason = entry.reason.lower()

        # Override: if indoor temp already at or beyond the target, the HVAC
        # would not actually run -- label as maintaining, not pre-heating/cooling.
        if indoor_temp is not None:
            if "pre-heating" in reason and indoor_temp >= entry.target_temp - 0.5:
                return PHASE_MAINTAINING
            if "pre-cooling" in reason and indoor_temp <= entry.target_temp + 0.5:
                return PHASE_MAINTAINING

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

    def _compute_net_passive_load(self) -> float | None:
        """Net passive thermal load on the house (BTU/hr).

        Sum of all non-HVAC, non-coupling heat flows: envelope, solar,
        internal gains, boundary zones, appliances, and aux resistive heat.
        Positive = house gaining heat, negative = losing heat.
        """
        comps = self.estimator.thermal_load_components
        if not comps:
            return None
        return (
            comps.get("q_env", 0.0)
            + comps.get("q_solar", 0.0)
            + comps.get("q_internal", 0.0)
            + comps.get("q_boundary", 0.0)
            + comps.get("q_appliances", 0.0)
            + comps.get("q_aux_resistive", 0.0)
        )

    # ── Event firing ──────────────────────────────────────────────

    @callback
    def _fire_event(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Fire an HA event for automation triggers."""
        self.hass.bus.async_fire(event_type, data or {})


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
        # Priority 1: User-provided override entity
        if self._aux_heat_override_entity_id:
            state = self.hass.states.get(self._aux_heat_override_entity_id)
            if state and state.state not in ("unavailable", "unknown"):
                return state.state == "on"
            # Override unavailable — fall through

        # Priority 2: Native duct temperature detection
        if self._duct_temp_entity:
            is_heating = (
                thermo_state is not None
                and thermo_state.available
                and getattr(thermo_state, "hvac_action", None) == "heating"
            )
            if is_heating:
                duct_result = self._detect_aux_heat_from_duct()
                if duct_result is not None:
                    return duct_result

        # Priority 3: Thermostat hvac_action
        if thermo_state is None or not thermo_state.available:
            return False
        action = getattr(thermo_state, "hvac_action", None)
        if action:
            return action in ("aux_heating", "emergency_heating")
        # Priority 4: Thermostat aux_heat attribute
        attrs = getattr(thermo_state, "attributes", {})
        return bool(attrs.get("aux_heat", False))

    def _detect_aux_heat_from_duct(self) -> bool | None:
        """Detect aux heat from supply duct temperature and rate of change.

        Returns True if aux heat is likely running, False if not,
        None if insufficient data or sensor unavailable.
        """
        reading = self.sensor_hub.read_duct_temp()
        if reading is None or reading.stale:
            return None

        now_ts = reading.timestamp.timestamp()
        duct_temp = reading.value

        # Append to ring buffer, trim to last 5 readings
        self._duct_temp_buffer.append((now_ts, duct_temp))
        if len(self._duct_temp_buffer) > 5:
            self._duct_temp_buffer = self._duct_temp_buffer[-5:]

        # High absolute temperature = almost certainly resistive heat
        if duct_temp >= DEFAULT_DUCT_AUX_THRESHOLD_F:
            return True

        # Moderate temperature + rapid rise = aux heat starting up
        if duct_temp >= DEFAULT_DUCT_AUX_RATE_THRESHOLD_F and len(self._duct_temp_buffer) >= 2:
            oldest_ts, oldest_temp = self._duct_temp_buffer[0]
            elapsed_min = (now_ts - oldest_ts) / 60.0
            if elapsed_min > 0:
                rate = (duct_temp - oldest_temp) / elapsed_min
                if rate >= DEFAULT_DUCT_AUX_RATE_MIN:
                    return True

        return False

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
        except Exception:  # noqa: BLE001
            return None
