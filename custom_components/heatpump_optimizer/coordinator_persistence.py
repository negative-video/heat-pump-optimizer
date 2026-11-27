"""Persistence mixin for the Heat Pump Optimizer coordinator.

Split out of coordinator.py to keep the main class focused on orchestration.
Provides save / restore / history-bootstrap behaviour via a mixin that relies
on the concrete coordinator owning the expected subsystem attributes
(profiler, savings_tracker, etc.). Any AttributeError raised from
this mixin indicates a wiring bug in the parent class, not in the mixin
itself.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from .adapters.occupancy import OccupancyMode
from .const import DEFAULT_HISTORY_BOOTSTRAP_DAYS
from .coordinator_twin import twin_location
from .dashboard_archive import DashboardArchive
from .engine.counterfactual_simulator import CounterfactualSimulator
from .learning.ab_trial import ABTrial
from .learning.aux_heat_learner import AuxHeatLearner
from .learning.baseline_capture import BaselineCapture
from .learning.coefficient_calibrator import CoefficientCalibrator
from .learning.coefficient_store import CoefficientStore
from .learning.energy_baseline import EnergyBaseline
from .learning.model_tracker import ModelTracker
from .learning.override_tracker import OverrideTracker
from .learning.performance_profiler import PerformanceProfiler
from .learning.solar_adjuster import SolarAdjuster
from .learning.twin_ledger import TwinLedger
from .savings_tracker import SavingsTracker

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)


class PersistenceMixin:
    """Coordinator methods for Store save / restore and history bootstrap.

    Concrete class is expected to provide: hass, _store, strategic, occupancy,
    model_tracker, solar_adjuster, override_tracker, savings_tracker,
    baseline_capture, counterfactual, profiler, aux_heat_learner, coeff_store,
    coeff_calibrator, sensor_hub, climate_entity_id,
    _weather_entity_ids, _power_default_watts, _phase, _active,
    _history_bootstrap_completed, _bootstrap_retry_count,
    _bootstrap_retry_unsub, _history_bootstrap_result.
    """

    # Internal schema version for the persisted state dict.
    # Bump this when the state format changes and add migration logic
    # in _restore_learning_state(). Separate from HA's STORAGE_VERSION
    # which controls the on-disk container format.
    _STATE_SCHEMA_VERSION = 1

    async def _persist_state(self) -> None:
        """Persist learning data and coordinator state to HA storage."""
        data = {
            "_schema_version": self._STATE_SCHEMA_VERSION,
            "last_optimization_time": (
                self.strategic.last_optimization_time.isoformat() if self.strategic.last_optimization_time else None
            ),
            "phase": self._phase,
            "active": self._active,
            # A user pause is a standing instruction; it has to outlive a
            # restart. It was saved under neither name and so never did.
            "paused": bool(
                self._paused
                and not any(c.get("type") == "pause_until" for c in getattr(self, "_active_constraints", []))
            ),
            "morning_recovery_starts": self.strategic.recovery_state(),
            # Rolling diagnostics that used to restart from empty on every
            # reload: the stale-sensor window and the forecast-error history
            # behind the accuracy sensors.
            "temp_history": list(getattr(self, "_temp_history", []))[-288:],
            "tactical_error_history": [
                [t.isoformat(), e] for t, e in getattr(self.tactical, "error_history", [])[-288:]
            ]
            if getattr(self, "tactical", None) is not None
            else [],
            "forced_occupancy_mode": (
                self.occupancy._forced_mode.value if self.occupancy._forced_mode is not None else None
            ),
            "model_tracker": self.model_tracker.to_dict(),
            "solar_adjuster": self.solar_adjuster.to_dict(),
            "override_tracker": self.override_tracker.to_dict(),
            "savings_tracker": self.savings_tracker.to_dict(),
            "energy_baseline": self.energy_baseline.to_dict(),
            "dashboard_archive": self.dashboard_archive.to_dict(),
            "twin_ledger": self.twin_ledger.to_dict(),
            "ab_trial": self.ab_trial.to_dict(),
            "ab_trial_paused": self._ab_trial_paused,
            "baseline_capture": self.baseline_capture.to_dict(),
            "counterfactual": self.counterfactual.to_dict(),
            "performance_profiler": self.profiler.to_dict(),
            "aux_heat_learner": self.aux_heat_learner.to_dict(),
            "coefficient_store": self.coeff_store.to_dict(),
            "coefficient_calibrator": self.coeff_calibrator.to_dict(),
            "_history_bootstrap_completed": self._history_bootstrap_completed,
            "_bootstrap_retry_count": self._bootstrap_retry_count,
        }
        await self._store.async_save(data)

    def _restore_learning_state(self, stored: dict) -> None:
        """Restore learning data from persisted storage.

        Each component is restored independently with error handling so that
        a corrupt or incompatible sub-dict doesn't prevent the rest from loading.
        On failure, the component stays at its default (fresh) state and a
        warning is logged instead of crashing async_setup().
        """
        schema = stored.get("_schema_version", 0)
        if schema > self._STATE_SCHEMA_VERSION:
            _LOGGER.warning(
                "Stored state has schema version %d but we only support up to %d "
                "— ignoring persisted state to avoid corruption. "
                "The model will re-learn from scratch.",
                schema,
                self._STATE_SCHEMA_VERSION,
            )
            return
        if schema < self._STATE_SCHEMA_VERSION:
            _LOGGER.info(
                "Migrating stored state from schema version %d to %d",
                schema,
                self._STATE_SCHEMA_VERSION,
            )
            # Add migration logic here when _STATE_SCHEMA_VERSION is bumped
        if stored.get("forced_occupancy_mode") is not None:
            try:
                self.occupancy.force_mode(OccupancyMode(stored["forced_occupancy_mode"]))
                _LOGGER.debug("Restored forced occupancy mode: %s", stored["forced_occupancy_mode"])
            except (ValueError, KeyError, TypeError):
                _LOGGER.warning("Invalid stored occupancy mode: %s", stored["forced_occupancy_mode"])

        if "model_tracker" in stored:
            try:
                self.model_tracker = ModelTracker.from_dict(stored["model_tracker"])
                _LOGGER.debug("Restored model tracker state")
            except (KeyError, ValueError, TypeError, AttributeError):
                _LOGGER.warning("Failed to restore model tracker — using fresh state", exc_info=True)
        if "solar_adjuster" in stored:
            try:
                self.solar_adjuster = SolarAdjuster.from_dict(stored["solar_adjuster"])
                _LOGGER.debug("Restored solar adjuster (coefficient=%.3f)", self.solar_adjuster.solar_coefficient)
            except (KeyError, ValueError, TypeError, AttributeError):
                _LOGGER.warning("Failed to restore solar adjuster — using fresh state", exc_info=True)
        if "override_tracker" in stored:
            try:
                self.override_tracker = OverrideTracker.from_dict(stored["override_tracker"])
                _LOGGER.debug("Restored %d override records", self.override_tracker.record_count)
            except (KeyError, ValueError, TypeError, AttributeError):
                _LOGGER.warning("Failed to restore override tracker — using fresh state", exc_info=True)
        if "savings_tracker" in stored:
            try:
                self.savings_tracker = SavingsTracker.from_dict(stored["savings_tracker"])
                totals = self.savings_tracker.cumulative_totals()
                _LOGGER.debug("Restored savings tracker (%.1f kWh saved cumulative)", totals["kwh_saved"])
            except (KeyError, ValueError, TypeError, AttributeError):
                _LOGGER.warning("Failed to restore savings tracker — using fresh state", exc_info=True)
        if "energy_baseline" in stored:
            try:
                self.energy_baseline = EnergyBaseline.from_dict(stored["energy_baseline"])
                _LOGGER.debug(
                    "Restored energy baseline (%d days, source=%s)",
                    len(self.energy_baseline.days),
                    self.energy_baseline.fit().source,
                )
            except (KeyError, ValueError, TypeError, AttributeError):
                _LOGGER.warning("Failed to restore energy baseline — using fresh state", exc_info=True)
        if "dashboard_archive" in stored:
            try:
                self.dashboard_archive = DashboardArchive.from_dict(stored["dashboard_archive"])
            except (KeyError, ValueError, TypeError, AttributeError):
                _LOGGER.warning("Failed to restore dashboard archive, starting empty", exc_info=True)
        if "twin_ledger" in stored:
            try:
                tz, lat, lon = twin_location(self.hass)
                self.twin_ledger = TwinLedger.from_dict(stored["twin_ledger"], tz, lat, lon)
            except (KeyError, ValueError, TypeError, AttributeError):
                _LOGGER.warning("Failed to restore twin ledger, starting empty", exc_info=True)
        if "ab_trial" in stored:
            try:
                self.ab_trial = ABTrial.from_dict(stored["ab_trial"])
                _LOGGER.debug("Restored A/B trial (active=%s)", self.ab_trial.is_active)
            except (KeyError, ValueError, TypeError, AttributeError):
                _LOGGER.warning("Failed to restore A/B trial — using fresh state", exc_info=True)
        self._ab_trial_paused = bool(stored.get("ab_trial_paused", False))
        self._paused = bool(stored.get("paused", False)) or self._ab_trial_paused
        try:
            self.strategic.restore_recovery_state(stored.get("morning_recovery_starts"))
        except (TypeError, ValueError, AttributeError):
            _LOGGER.warning("Failed to restore morning recovery state", exc_info=True)
        try:
            history = [float(t) for t in stored.get("temp_history") or []]
            if history:
                self._temp_history = history[-288:]
        except (TypeError, ValueError):
            _LOGGER.warning("Failed to restore temperature history", exc_info=True)
        try:
            self._restore_tactical_history(stored.get("tactical_error_history"))
        except (TypeError, ValueError, AttributeError):
            _LOGGER.warning("Failed to restore forecast-error history", exc_info=True)
        # Re-link after any of the three objects above may have been replaced
        self._link_energy_baseline()
        if "coefficient_store" in stored:
            try:
                self.coeff_store = CoefficientStore.from_dict(stored["coefficient_store"])
                _LOGGER.debug(
                    "Restored coefficient store (%d calibrations)",
                    self.coeff_store.calibration_count,
                )
            except (KeyError, ValueError, TypeError, AttributeError):
                _LOGGER.warning("Failed to restore coefficient store — using defaults", exc_info=True)
        if "coefficient_calibrator" in stored:
            try:
                self.coeff_calibrator = CoefficientCalibrator.from_dict(
                    stored["coefficient_calibrator"],
                    self.coeff_store,
                )
                _LOGGER.debug("Restored coefficient calibrator")
            except (KeyError, ValueError, TypeError, AttributeError):
                _LOGGER.warning("Failed to restore coefficient calibrator — using fresh state", exc_info=True)
        if "baseline_capture" in stored:
            try:
                self.baseline_capture = BaselineCapture.from_dict(stored["baseline_capture"])
                _LOGGER.debug(
                    "Restored baseline capture (%d days, confidence=%.0f%%)",
                    self.baseline_capture.sample_days,
                    self.baseline_capture.confidence * 100,
                )
            except (KeyError, ValueError, TypeError, AttributeError):
                _LOGGER.warning("Failed to restore baseline capture — using fresh state", exc_info=True)
        if "counterfactual" in stored:
            try:
                self.counterfactual = CounterfactualSimulator.from_dict(stored["counterfactual"])
                # The restored object carries whatever capacity and deadband
                # were stored, which for older state is the 30,000 BTU/hr
                # default. Configuration is the authority on both.
                self._reapply_counterfactual_config()
                _LOGGER.debug(
                    "Restored counterfactual simulator (virtual_temp=%.1f°F)",
                    self.counterfactual.virtual_indoor_temp,
                )
            except (KeyError, ValueError, TypeError, AttributeError):
                _LOGGER.warning("Failed to restore counterfactual simulator — using fresh state", exc_info=True)
        if "performance_profiler" in stored:
            try:
                self.profiler = PerformanceProfiler.from_dict(stored["performance_profiler"])
                _LOGGER.debug(
                    "Restored performance profiler (%d observations, confidence=%.0f%%)",
                    self.profiler.total_observations,
                    self.profiler.confidence() * 100,
                )
            except (KeyError, ValueError, TypeError, AttributeError):
                _LOGGER.warning("Failed to restore performance profiler — using fresh state", exc_info=True)
        if "aux_heat_learner" in stored:
            try:
                self.aux_heat_learner = AuxHeatLearner.from_dict(
                    stored["aux_heat_learner"],
                    default_hp_watts=self._power_default_watts,
                )
                _LOGGER.debug(
                    "Restored aux heat learner (%d events, threshold=%.1f°F, hp_watts=%.0fW)",
                    self.aux_heat_learner.event_count,
                    self.aux_heat_learner.threshold_f,
                    self.aux_heat_learner.learned_hp_watts,
                )
            except (KeyError, ValueError, TypeError, AttributeError):
                _LOGGER.warning("Failed to restore aux heat learner — using fresh state", exc_info=True)

    def _reapply_counterfactual_config(self) -> None:
        """Push configured capacity and deadband onto the digital twin."""
        tonnage = getattr(self, "_hvac_tonnage", None)
        if tonnage:
            self.counterfactual.set_rated_capacity(
                q_cool=tonnage * 12000.0,
                q_heat=tonnage * 12000.0 * 1.1,
            )
        deadband = getattr(self, "_thermostat_deadband", None)
        if deadband is not None and hasattr(self.counterfactual, "_deadband"):
            self.counterfactual._deadband = deadband

    def _restore_tactical_history(self, raw: list | None) -> None:
        """Reload recent forecast errors, dropping anything older than a day."""
        tactical = getattr(self, "tactical", None)
        if tactical is None or not raw:
            return
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        restored = []
        for item in raw:
            try:
                when = datetime.fromisoformat(item[0])
                if when > cutoff:
                    restored.append((when, float(item[1])))
            except (TypeError, ValueError, IndexError):
                continue
        if restored:
            tactical._error_history = restored

    async def async_try_history_bootstrap_if_needed(self) -> None:
        """Public entry point: attempt history bootstrap if not already done.

        Called from the async_at_started callback in __init__.py where the
        recorder is guaranteed to be ready.
        """
        if self._history_bootstrap_completed:
            _LOGGER.debug("History bootstrap already completed — skipping")
            return
        if self._bootstrap_retry_unsub is not None:
            _LOGGER.debug("History bootstrap retry already scheduled — skipping")
            return
        await self._try_history_bootstrap()

    def _schedule_bootstrap_retry(self) -> None:
        """Schedule a delayed bootstrap retry if retries remain."""
        from .const import BOOTSTRAP_RETRY_DELAY_SECONDS, MAX_BOOTSTRAP_RETRIES

        if self._bootstrap_retry_count >= MAX_BOOTSTRAP_RETRIES:
            _LOGGER.warning(
                "History bootstrap: exhausted %d retries, giving up. Baseline will complete via live observation.",
                MAX_BOOTSTRAP_RETRIES,
            )
            self._history_bootstrap_completed = True
            return

        async def _retry_callback(_now) -> None:
            self._bootstrap_retry_unsub = None
            await self._try_history_bootstrap()

        from homeassistant.helpers.event import async_call_later

        self._bootstrap_retry_unsub = async_call_later(self.hass, BOOTSTRAP_RETRY_DELAY_SECONDS, _retry_callback)
        _LOGGER.info(
            "History bootstrap: baseline not ready, retry %d/%d scheduled in 24h",
            self._bootstrap_retry_count + 1,
            MAX_BOOTSTRAP_RETRIES,
        )

    async def _try_history_bootstrap(self) -> None:
        """Attempt to bootstrap learning subsystems from recorder history.

        Batch-replays up to 10 days of historical thermostat/sensor data
        through the EKF to achieve meaningful model convergence immediately.
        """
        from .learning.history_bootstrap import async_bootstrap_from_history

        result = await async_bootstrap_from_history(
            hass=self.hass,
            climate_entity_id=self.climate_entity_id,
            outdoor_temp_entities=self.sensor_hub._outdoor_temp_entities,
            weather_entity_ids=self._weather_entity_ids,
            wind_speed_entity=getattr(self.sensor_hub, "_wind_speed_entity", None),
            humidity_entities=self.sensor_hub._outdoor_humidity_entities,
            baseline_capture=self.baseline_capture,
            profiler=self.profiler,
            max_days=DEFAULT_HISTORY_BOOTSTRAP_DAYS,
            indoor_temp_entities=self.sensor_hub._indoor_temp_entities,
            attic_temp_entity=getattr(self.sensor_hub, "_attic_temp_entity", None),
        )

        self._history_bootstrap_result = result.reason if not result.success else "ok"

        if result.success:
            # Reset profiler tracking so the first live cycle starts clean
            # (avoids perpetual skipped_interval from stale bootstrap timestamp)
            self.profiler._previous_indoor_temp = None
            self.profiler._previous_timestamp = None

            _LOGGER.info(
                "History bootstrap complete: profiler confidence %.0f%%, baseline %s (%d obs), profiler %d obs",
                result.final_confidence * 100,
                "ready" if self.baseline_capture.is_ready else "not ready",
                result.baseline_observations,
                result.profiler_observations,
            )

            if self.baseline_capture.is_ready:
                self._history_bootstrap_completed = True
            else:
                # Profiler benefited but baseline needs more days — schedule retry
                self._bootstrap_retry_count += 1
                self._schedule_bootstrap_retry()

            # Persist immediately (includes bootstrap flag + retry count)
            await self._persist_state()
        else:
            _LOGGER.warning("History bootstrap failed: %s", result.reason)
            self._bootstrap_retry_count += 1
            self._schedule_bootstrap_retry()
            await self._persist_state()
