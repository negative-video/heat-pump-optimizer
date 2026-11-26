"""Regression tests for coordinator-level persistence wiring.

These exist because of a real near-miss: the measured-savings work (audit
item 10) was written against the pre-refactor coordinator.py, and when it was
merged onto the refactored tree its `_persist_state` / `_restore_learning_state`
hunks landed on regions that had moved into coordinator_persistence.py. Git
dropped them silently, and the whole suite still passed, because nothing
asserted that the coordinator actually persists its subsystems.

The failure that would have shipped is invisible until a restart: the energy
baseline accumulates for days, then vanishes, and the measured savings sensors
quietly go unavailable again.

So these tests assert the contract at the mixin level rather than testing any
one subsystem: every subsystem the coordinator owns must survive a
save/restore round trip.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from custom_components.heatpump_optimizer.coordinator_persistence import (
    PersistenceMixin,
)
from custom_components.heatpump_optimizer.learning.ab_trial import ABTrial
from custom_components.heatpump_optimizer.dashboard_archive import DashboardArchive
from custom_components.heatpump_optimizer.learning.energy_baseline import (
    EnergyBaseline,
)


# Subsystems whose state must appear in the persisted dict. Adding a new
# persistent subsystem to the coordinator means adding it here too.
REQUIRED_STATE_KEYS = [
    "dashboard_archive",
    "model_tracker",
    "solar_adjuster",
    "override_tracker",
    "savings_tracker",
    "energy_baseline",
    "ab_trial",
    "ab_trial_paused",
    # A user pause and an in-progress morning recovery are standing state:
    # losing either on restart resumes control the user had stopped, or
    # drops a setpoint that had already been raised.
    "paused",
    "morning_recovery_starts",
    "temp_history",
    "tactical_error_history",
    # No "thermal_estimator": audit item 8 removed the EKF. Its state was
    # nine parameters identified from one observation, so persisting it
    # carried a random walk across restarts rather than knowledge.
    "baseline_capture",
    "counterfactual",
    "performance_profiler",
    "aux_heat_learner",
    "coefficient_store",
    "coefficient_calibrator",
]


class _StubCoordinator(PersistenceMixin):
    """Minimal object exposing what PersistenceMixin expects."""

    def __init__(self):
        self._store = MagicMock()
        self.saved = {}

        async def _save(data):
            self.saved = data

        self._store.async_save = _save

        self.strategic = MagicMock()
        self.strategic.last_optimization_time = None
        self.strategic.recovery_state.return_value = {}
        self.tactical = MagicMock()
        self.tactical.error_history = []
        self._paused = False
        self._active_constraints = []
        self._temp_history = []
        self.occupancy = MagicMock()
        self.occupancy._forced_mode = None

        self._phase = "idle"
        self._active = True
        self._history_bootstrap_completed = False
        self._bootstrap_retry_count = 0

        # Real objects for the two subsystems under test; mocks for the rest.
        self.energy_baseline = EnergyBaseline()
        self.ab_trial = ABTrial()
        self._ab_trial_paused = False
        self.dashboard_archive = DashboardArchive()

        for name in (
            "model_tracker", "solar_adjuster", "override_tracker",
            "savings_tracker", "estimator", "baseline_capture",
            "counterfactual", "profiler", "aux_heat_learner",
            "coeff_store", "coeff_calibrator",
        ):
            setattr(self, name, MagicMock())
            getattr(self, name).to_dict.return_value = {}

        self.linked = 0

    def _link_energy_baseline(self) -> None:
        self.linked += 1


def _persist(stub):
    asyncio.run(stub._persist_state())
    return stub.saved


class TestPersistedStateCompleteness:
    def test_all_subsystems_are_persisted(self):
        state = _persist(_StubCoordinator())
        missing = [k for k in REQUIRED_STATE_KEYS if k not in state]
        assert not missing, f"subsystems missing from persisted state: {missing}"

    def test_energy_baseline_and_ab_trial_are_serializable(self):
        state = _persist(_StubCoordinator())
        # These must be real dicts, not mocks — a mock here means the
        # coordinator lost its reference to the real object.
        assert isinstance(state["energy_baseline"], dict)
        assert isinstance(state["ab_trial"], dict)
        assert isinstance(state["ab_trial_paused"], bool)


class TestRestoreRoundTrip:
    def test_energy_baseline_survives_round_trip(self):
        stub = _StubCoordinator()
        saved = _persist(stub)

        fresh = _StubCoordinator()
        fresh._restore_learning_state(saved)

        assert isinstance(fresh.energy_baseline, EnergyBaseline)
        assert isinstance(fresh.ab_trial, ABTrial)

    def test_ab_trial_pause_flag_survives_round_trip(self):
        stub = _StubCoordinator()
        stub._ab_trial_paused = True
        saved = _persist(stub)

        fresh = _StubCoordinator()
        assert fresh._ab_trial_paused is False
        fresh._restore_learning_state(saved)
        assert fresh._ab_trial_paused is True, (
            "a restart mid-trial must not silently resume the optimizer"
        )

    def test_restore_relinks_baseline_to_dependents(self):
        """from_dict replaces the objects, so the wiring must be rebuilt."""
        stub = _StubCoordinator()
        saved = _persist(stub)

        fresh = _StubCoordinator()
        before = fresh.linked
        fresh._restore_learning_state(saved)
        assert fresh.linked > before, (
            "restore replaced energy_baseline/ab_trial without re-linking them"
        )

    def test_restore_tolerates_absent_keys(self):
        """Upgrading from a version without these keys must not crash."""
        fresh = _StubCoordinator()
        fresh._restore_learning_state({"_schema_version": 1})
        assert isinstance(fresh.energy_baseline, EnergyBaseline)
        assert fresh._ab_trial_paused is False

    def test_restore_tolerates_corrupt_subsystem(self):
        """A corrupt sub-dict must not prevent the rest from loading."""
        fresh = _StubCoordinator()
        fresh._restore_learning_state(
            {"_schema_version": 1, "energy_baseline": {"days": "not-a-list"}}
        )
        assert isinstance(fresh.energy_baseline, EnergyBaseline)


class TestDataBuilderExposesMeasuredSavings:
    """The sensors read these keys; something must populate them."""

    def test_measured_savings_reaches_the_data_dict(self):
        import inspect
        from custom_components.heatpump_optimizer import coordinator_data_builder

        src = inspect.getsource(coordinator_data_builder)
        assert "_measured_savings_data()" in src, (
            "coordinator data dict no longer spreads measured savings; the "
            "Measured Savings sensors will read as unavailable"
        )
        assert "modeled_attribution" in src, (
            "modeled attribution missing from the data dict"
        )
