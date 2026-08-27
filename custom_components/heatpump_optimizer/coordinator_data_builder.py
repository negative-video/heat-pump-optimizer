"""Data-builder mixin for the Heat Pump Optimizer coordinator.

Assembles the dict that DataUpdateCoordinator exposes to the sensor, binary
sensor, and panel platforms. Split out of coordinator.py to keep the main
class focused on orchestration.

The mixin relies on the concrete coordinator providing: hass, strategic,
estimator, tactical, model_tracker, override_tracker, savings_tracker,
profiler, baseline_capture, counterfactual, aux_heat_learner, watchdog,
calendar_occupancy, occupancy, appliance_manager, sensor_hub, thermostat,
_profile_adapter, _precondition_plan, _phase, _active, _monitor_only,
_departure_detected, _occupancy_timeline, _last_good_forecast,
_last_forecast_source, _last_forecast_time, _last_profile_mode, _paused,
_active_constraints, _cached_aux_heat_active, _last_profiler_status,
and the classification/helper methods _get_current_entry, _get_next_entry,
_classify_phase, _compute_forecast_deviation, _build_profiler_chart_data,
_describe_next_action, _check_model_alert, _get_calibration_status,
_compute_net_passive_load, _is_aux_heat_running, _is_hvac_running,
_is_learning_active, _is_forecast_stale, _is_sensor_stale,
_activation_tier, _active_comfort_range, _get_last_tactical_correction,
_baseline_ready_for_control, _is_conservative_mode, export_model, etc.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.util import dt as dt_util

from .const import ACTIVATION_TIER_LEARNING, BLEND_MODE_NONE, PHASE_PRECONDITIONING
from .savings_tracker import TIER_LEARNING, TIER_PROJECTED


def _thermal_keys(profile) -> dict[str, Any]:
    """Flatten the thermal profile into the sensor payload.

    Named explicitly rather than prefixed programmatically so the key names
    stay greppable from the sensor classes that read them.
    """
    d = profile.as_dict()
    return {
        "thermal_drift_slope": d["drift_slope_per_hour"],
        "thermal_time_constant_hours": d["time_constant_hours"],
        "thermal_mass_btu_per_f": d["thermal_mass_btu_per_f"],
        "thermal_envelope_loss": d["envelope_loss_btu_per_hour_f"],
        "thermal_observations": d["observations"],
        "thermal_source": d["source"],
    }


class DataBuilderMixin:
    """Methods that serialize coordinator state into the sensor payload."""

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

        # Subsample to one point per hour (optimizer uses 5-min
        # steps producing ~288 points).
        result = []
        seen_hours: set[int] = set()
        for pt in schedule.simulation:
            hour_key = int(pt.time.timestamp()) // 3600
            if hour_key in seen_hours:
                continue
            seen_hours.add(hour_key)
            bounds = comfort_bounds.get(hour_key)
            entry: dict = {
                "time": pt.time.isoformat(),
                "indoor": float(round(pt.indoor_temp, 1)),
                "outdoor": float(round(pt.outdoor_temp, 1)),
                "hvac": bool(pt.hvac_running),
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

        # Tactical stats (None until the tactical layer has run)
        tactical_mae = self.tactical.mean_absolute_error
        tactical_bias = self.tactical.mean_signed_error

        # Model tracker report
        accuracy_report = self.model_tracker.get_accuracy_report()

        # Override stats
        override_stats = self.override_tracker.get_stats()

        # Savings
        today_savings = self.savings_tracker.today_report(tz=dt_util.DEFAULT_TIME_ZONE)
        cumulative = self.savings_tracker.cumulative_totals()

        # Effective HVAC capacity at current outdoor temp
        _outdoor_info = self.sensor_hub.get_outdoor_temp_info(
            self.strategic.forecast_snapshot
        )
        _outdoor_val = _outdoor_info.get("value") if _outdoor_info else None
        # Capacity is the equipment's own forcing, envelope load removed --
        # measured, rather than an EKF state that absorbed residual error.
        _active_model = self._get_active_model()
        _eff_cool = (
            _active_model.gross_hvac_rate("cool", _outdoor_val)
            if _outdoor_val is not None else None
        )
        _eff_heat = (
            _active_model.gross_hvac_rate("heat", _outdoor_val)
            if _outdoor_val is not None else None
        )

        return {
            # Core state
            "phase": self._phase,
            "active": self._active and not self._paused and not self.watchdog.is_override_active,
            "monitor_only": self._monitor_only,
            "override_detected": self.watchdog.is_override_active,
            "baseline_only_mode": not self._baseline_ready_for_control,
            "activation_tier": self._activation_tier,
            "baseline_ready": self.baseline_capture.is_ready,
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
            # Which candidate the planner shipped ("flat" on a normal day) and
            # whether it handed the thermostat a heat floor / cool ceiling pair.
            "plan_label": schedule.plan_label if schedule else None,
            "dual_setpoint": schedule.dual_setpoint if schedule else False,
            "last_optimization": (
                self.strategic.last_optimization_time.isoformat()
                if self.strategic.last_optimization_time else None
            ),
            "mode": self.strategic.mode,

            # Thermostat
            "current_indoor_temp": thermo_state.indoor_temp if thermo_state else None,
            "weighted_indoor_temp": getattr(self, "_effective_indoor_temp", None),
            "humidity": thermo_state.humidity if thermo_state else None,

            # Thermostat blend mitigation diagnostics
            "thermostat_blend_suspected": getattr(self, "_thermostat_blend_suspected", False),
            "thermostat_blend_mode": self._blend_mode,
            "cross_sensor_spread_f": getattr(self, "_cross_sensor_spread_f", 0.0),
            "indoor_thermo_excluded": (
                getattr(self, "_thermostat_blend_suspected", False)
                and self._blend_mode != BLEND_MODE_NONE
            ),

            # Humidity squelch diagnostics
            "humidity_squelch_active": bool(self.sensor_hub.squelched_entities),
            "squelched_entities": self.sensor_hub.squelched_entities,

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
                if self.tactical.error_history
                else None
            ),
            "predicted_indoor_temp": self.tactical.predicted_indoor_temp,
            "model_accuracy_mae": tactical_mae,
            "model_bias": tactical_bias,

            # Learning
            "model_corrections": {
                mode: accuracy_report[mode]["correction"]
                for mode in ("cool", "heat", "resist")
            },
            "model_alert": self._check_model_alert(accuracy_report),
            "solar_coefficient": self.solar_adjuster.solar_coefficient,

            # Measured thermal characterisation. Replaces the nine EKF states,
            # which were not separately identifiable from one observed
            # temperature and so reported precision they never had.
            **_thermal_keys(self._thermal_profile()),
            "effective_cooling_capacity": _eff_cool,
            "effective_heating_capacity": _eff_heat,
            "learning_active": self._is_learning_active(),
            "initialization_mode": self._initialization_mode,
            "history_bootstrap_completed": self._history_bootstrap_completed,
            "history_bootstrap_result": self._history_bootstrap_result,
            "bootstrap_retry_count": self._bootstrap_retry_count,

            "house_thermal_load_btu": self._compute_net_passive_load(),

            # Coefficient calibration (slow outer loop)
            "coefficient_calibration_status": self._get_calibration_status(),
            "coefficient_multipliers": self.coeff_store.multipliers,
            "coefficient_confidence": self.coeff_store.all_confidence,
            "coefficient_proposed": self.coeff_calibrator.proposed_multipliers,
            "coefficient_calibration_count": self.coeff_store.calibration_count,
            "coefficient_last_calibration": (
                self.coeff_store.last_calibration.isoformat()
                if self.coeff_store.last_calibration else None
            ),
            "coefficient_experiments_detected": len(self.coeff_calibrator.last_experiments),

            # Overrides
            "override_count_30d": override_stats.get("total_overrides_30d", 0),
            "override_pattern": override_stats.get("top_pattern"),

            # Occupancy
            "occupancy_mode": self.occupancy.get_effective_mode(
                self._occupancy_timeline or None
            ).value,

            # Diagnostics
            "forecast_age_minutes": (
                round((now - self._last_forecast_time).total_seconds() / 60.0, 1)
                if self._last_forecast_time else None
            ),
            "forecast_deviation": self._compute_forecast_deviation(),
            "schedule_detail": self._build_schedule_detail(schedule),
            "forecast_detail": self._build_forecast_detail(schedule),

            # SensorHub diagnostics
            "outdoor_temp_info": _outdoor_info,
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
            # ── Headline: measured vs weather-normalized baseline ──
            # Metered kWh against a degree-day regression of the user's own
            # optimizer-off days. No thermal model on either side.
            **self._measured_savings_data(),

            "baseline_kwh_today": today_savings.total_baseline_kwh,
            "actual_kwh_today": today_savings.total_actual_kwh,
            "actual_kwh_cumulative": cumulative["kwh_actual"],
            "worst_case_kwh_today": today_savings.total_worst_case_kwh,
            "savings_kwh_cumulative": (
                cumulative["kwh_saved"]
                if self.savings_tracker.accuracy_tier not in (TIER_LEARNING, TIER_PROJECTED)
                else 0.0 if self.savings_tracker.accuracy_tier == TIER_PROJECTED
                else None
            ),
            "savings_cost_cumulative": (
                cumulative["cost_saved"]
                if self.savings_tracker.accuracy_tier not in (TIER_LEARNING, TIER_PROJECTED)
                else 0.0 if self.savings_tracker.accuracy_tier == TIER_PROJECTED
                else None
            ),
            "savings_co2_cumulative_grams": (
                cumulative["co2_saved_grams"]
                if self.savings_tracker.accuracy_tier not in (TIER_LEARNING, TIER_PROJECTED)
                else 0.0 if self.savings_tracker.accuracy_tier == TIER_PROJECTED
                else None
            ),

            # Counterfactual digital twin — modeled attribution only.
            # These explain where the optimizer thinks a difference came from.
            # They are NOT evidence that it happened: the shadow house runs on
            # the same thermal model as the optimizer being graded.
            "modeled_attribution": self.savings_tracker.modeled_attribution(
                today_savings
            ),
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
            "savings_is_projected": self.savings_tracker.accuracy_tier == TIER_PROJECTED,

            # Performance profiler
            "profiler_confidence": round(self.profiler.confidence() * 100, 0),
            # 0-1 form for entities that display a percentage themselves.
            "profiler_confidence_fraction": self.profiler.confidence(),
            "profiler_active": self.profiler.confidence() >= 0.7,
            "profiler_observations": self.profiler.total_observations,
            "profiler_status": self._last_profiler_status,
            "profiler_mode_detail": {
                mode: {
                    "confidence": round(
                        self.profiler.confidence(mode) * 100, 0
                    ),
                    "observations": sum(
                        acc.count
                        for acc in self.profiler._bins[mode].values()
                    ),
                }
                for mode in ("cool_1", "heat_1", "auxiliary_heat_1", "resist")
                if self.profiler._bins[mode]
            },

            # Temperature profile chart data (per-mode bin coverage + trendlines)
            "profiler_chart_data": self._build_profiler_chart_data(),

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
            "grid_import_watts": (
                r.value if (r := self.sensor_hub.read_grid_import())
                else None
            ),

            # Weather forecast for panel (enables chart during learning)
            "weather_forecast": self._build_weather_forecast(),

            # Auxiliary appliances
            "appliance_thermal_load_btu": self.appliance_manager.total_thermal_impact_btu(),
            "appliance_diagnostics": self.appliance_manager.get_diagnostics(),

            # Aux heat learner
            "aux_heat_threshold_f": (
                self.aux_heat_learner.threshold_f
                if self.aux_heat_learner.is_learned else None
            ),
            "aux_heat_threshold_learned": self.aux_heat_learner.is_learned,
            "aux_heat_event_count": self.aux_heat_learner.event_count,
            "aux_heat_learned_hp_watts": (
                round(self.aux_heat_learner.learned_hp_watts)
                if self.aux_heat_learner.hp_watts_learned else None
            ),
            "aux_heat_kwh_today": today_savings.total_aux_heat_kwh,
            "avoided_aux_heat_kwh_today": today_savings.total_avoided_aux_kwh,

            # Profile control
            "profile_control": {
                "enabled": self._profile_adapter is not None,
                "current_profile": (
                    self._profile_adapter.current_profile
                    if self._profile_adapter else None
                ),
                "last_set_profile": self._last_profile_mode,
                "entity": (
                    self._profile_adapter.target_entity_id
                    if self._profile_adapter else None
                ),
            },
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

        # Power provenance (diagnostic snapshot — no mode context needed)
        total_count += 1
        power = self.sensor_hub.read_power_draw()  # legacy fallback OK for diagnostics
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
