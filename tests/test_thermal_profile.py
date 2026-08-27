"""Tests for the profiler-derived thermal characterisation (audit item 8).

This replaced a 9-state EKF. The bar it has to clear is not "estimates a lot
of parameters" but "every number it reports is separately observable from
measured data, and it says nothing when it has not measured anything".
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.conftest import CC, load_module

tp_mod = load_module(
    "custom_components.heatpump_optimizer.learning.thermal_profile",
    os.path.join(CC, "learning", "thermal_profile.py"),
)

derive_thermal_profile = tp_mod.derive_thermal_profile
thermal_mass_for = tp_mod.thermal_mass_for
THERMAL_MASS_BTU_PER_SQFT = tp_mod.THERMAL_MASS_BTU_PER_SQFT
DEFAULT_SQFT = tp_mod.DEFAULT_SQFT


class FakeProfiler:
    """Minimal stand-in exposing the two calls the derivation makes."""

    def __init__(self, slope, observations=400):
        self._slope = slope
        self._observations = observations

    def mode_observation_count(self, mode):
        return self._observations

    def to_profile_data(self):
        if self._slope is None:
            return {"temperature": {"resist": None}}
        return {
            "temperature": {
                "resist": {"linear_trendline": {"slope": self._slope, "intercept": -2.2}}
            }
        }


class TestThermalMassSeed:
    def test_scales_with_floor_area(self):
        assert thermal_mass_for(2500) == pytest.approx(2500 * THERMAL_MASS_BTU_PER_SQFT)

    @pytest.mark.parametrize("bad", [None, 0, -100])
    def test_falls_back_when_area_unknown(self, bad):
        assert thermal_mass_for(bad) == pytest.approx(
            DEFAULT_SQFT * THERMAL_MASS_BTU_PER_SQFT
        )

    def test_is_a_seed_not_an_estimate(self):
        """Two profilers with different data must give the same mass.

        Thermal mass is deliberately not learned. The EKF's live estimate fed
        the profiler's appliance-BTU conversion, so the training data moved
        whenever the filter wandered.
        """
        a = derive_thermal_profile(FakeProfiler(0.05), sqft=2500)
        b = derive_thermal_profile(FakeProfiler(0.30), sqft=2500)
        assert a.thermal_mass_btu_per_f == b.thermal_mass_btu_per_f


class TestTimeConstant:
    def test_reproduces_the_measured_house(self):
        """k = 0.19/hr was fitted to 10 days of real 5-minute history.

        The audit's standalone two-node fit put that house's time constant at
        ~5.2 h. The single-node derivation has to land on the same number, or
        the diagnostic is not describing the same building.
        """
        profile = derive_thermal_profile(FakeProfiler(0.19), sqft=2500)
        assert profile.time_constant_hours == pytest.approx(5.26, abs=0.05)

    def test_time_constant_is_the_reciprocal_of_drift_slope(self):
        profile = derive_thermal_profile(FakeProfiler(0.10), sqft=2000)
        assert profile.drift_slope_per_hour == pytest.approx(0.10)
        assert profile.time_constant_hours == pytest.approx(10.0)

    def test_sign_of_the_slope_does_not_matter(self):
        """Trendline sign depends on fit convention; the rate does not."""
        pos = derive_thermal_profile(FakeProfiler(0.08), sqft=2000)
        neg = derive_thermal_profile(FakeProfiler(-0.08), sqft=2000)
        assert pos.time_constant_hours == pytest.approx(neg.time_constant_hours)

    def test_envelope_loss_follows_from_mass_and_slope(self):
        profile = derive_thermal_profile(FakeProfiler(0.19), sqft=2500)
        assert profile.envelope_loss_btu_per_hour_f == pytest.approx(1500 * 0.19)


class TestRefusesToGuess:
    """None means not measured. It must never mean zero."""

    def test_no_trendline_reports_unmeasured(self):
        profile = derive_thermal_profile(FakeProfiler(None), sqft=2500)
        assert profile.source == "unmeasured"
        assert profile.is_measured is False
        assert profile.drift_slope_per_hour is None
        assert profile.time_constant_hours is None
        assert profile.envelope_loss_btu_per_hour_f is None

    def test_thermal_mass_survives_having_no_drift_data(self):
        """The seed does not depend on observations, so it is always available."""
        profile = derive_thermal_profile(FakeProfiler(None), sqft=2500)
        assert profile.thermal_mass_btu_per_f == pytest.approx(1500)

    @pytest.mark.parametrize("implausible", [0.0, 0.001, 0.9, 5.0])
    def test_rejects_implausible_slopes(self, implausible):
        """Too small means too few observations; too large means a window
        left open or HVAC data misclassified as passive drift."""
        profile = derive_thermal_profile(FakeProfiler(implausible), sqft=2500)
        assert profile.source == "unmeasured"

    def test_survives_a_malformed_profiler(self):
        class Broken:
            def mode_observation_count(self, mode):
                raise RuntimeError("boom")

            def to_profile_data(self):
                return {"nonsense": True}

        profile = derive_thermal_profile(Broken(), sqft=2500)
        assert profile.source == "unmeasured"
        assert profile.observations == 0


class TestDiagnosticPayload:
    def test_keys_are_stable_across_measured_and_unmeasured(self):
        measured = derive_thermal_profile(FakeProfiler(0.19), 2500).as_dict()
        unmeasured = derive_thermal_profile(FakeProfiler(None), 2500).as_dict()
        assert set(measured) == set(unmeasured)

    def test_labels_itself_as_an_approximation(self):
        """The house is not one node and the payload has to say so."""
        assert "single-node" in derive_thermal_profile(
            FakeProfiler(0.19), 2500
        ).as_dict()["model"]
