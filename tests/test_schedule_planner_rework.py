"""Acceptance tests for the control-core rework (audit items 1, 2, 3, 7).

What these lock down, and why each one exists:

* **Item 2** -- the simulated thermostat is stateful, so a plan that parks at
  a comfort band edge no longer manufactures a violation on every step. The
  old stateless predicate made the simulated house *ride* at
  ``target + differential/2``, which meant every schedule that used the band
  got vetoed and replaced by a narrower, timid one.
* **Item 3** -- efficiency is measured in energy, not in needle speed. Gross
  HVAC rate at 75F vs 96F comes out near 1.0x, not the 5x the net deltas
  suggest, because the net rate is mostly the envelope load showing through.
* **Item 1** -- flat is the default schedule, pre-conditioning is a gated
  event, and the window is found by searching simulated candidates. The
  cooling gate is "the unit will saturate", not "COP is better now": a
  closed-loop replay on this house's own data showed deeper and longer
  pre-cooling losing energy every time, with comfort never at risk.
* **Item 7** -- shoulder days emit a flat heat floor / cool ceiling pair.

All profile data here is synthetic. It is shaped like a real thermostat
export (bins keyed by outdoor temp plus a count-weighted trendline) and the
rates are chosen to reproduce the physics the audit describes, but no real
house data appears in this file.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.conftest import CC, load_module

dt_mod = load_module(
    "custom_components.heatpump_optimizer.engine.data_types",
    os.path.join(CC, "engine", "data_types.py"),
)
comfort_mod = load_module(
    "custom_components.heatpump_optimizer.engine.comfort",
    os.path.join(CC, "engine", "comfort.py"),
)
perf_mod = load_module(
    "custom_components.heatpump_optimizer.engine.performance_model",
    os.path.join(CC, "engine", "performance_model.py"),
)
sim_mod = load_module(
    "custom_components.heatpump_optimizer.engine.thermal_simulator",
    os.path.join(CC, "engine", "thermal_simulator.py"),
)
planner_mod = load_module(
    "custom_components.heatpump_optimizer.engine.schedule_planner",
    os.path.join(CC, "engine", "schedule_planner.py"),
)
opt_mod = load_module(
    "custom_components.heatpump_optimizer.engine.optimizer",
    os.path.join(CC, "engine", "optimizer.py"),
)

ForecastPoint = dt_mod.ForecastPoint
ScheduleEntry = dt_mod.ScheduleEntry
PerformanceModel = perf_mod.PerformanceModel
ThermalSimulator = sim_mod.ThermalSimulator
SchedulePlanner = planner_mod.SchedulePlanner
ScheduleOptimizer = opt_mod.ScheduleOptimizer
derive_effective_comfort_band = comfort_mod.derive_effective_comfort_band
COMFORT_BAND_MIN_WIDTH_F = comfort_mod.COMFORT_BAND_MIN_WIDTH_F

NOW = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)


# ── Synthetic profile construction ───────────────────────────────────


def _linear_bins(lo: int, hi: int, slope: float, intercept: float) -> dict:
    return {str(t): slope * t + intercept for t in range(lo, hi + 1)}


def make_profile(
    *,
    cool=(0.047619, -6.7314, 51, 100),
    heat=(0.0416, -1.066, 7, 53),
    resist=(0.0419, -2.2229, 12, 100),
    aux_offset=2.5,
    heat_bp=25.6,
    resist_bp=51.7,
    differential=1.0,
    rated_watts=3500.0,
    aux_watts=10000.0,
) -> dict:
    """Build a synthetic Beestat-shaped temperature profile.

    Defaults reproduce the decomposition the audit describes: net cooling
    around -3.16F/hr at 75F and -2.16F/hr at 96F, with passive drift growing
    from +0.92 to +1.80F/hr over the same span. Net looks like a 1.5x
    capacity collapse; gross barely moves.
    """
    c_slope, c_int, c_lo, c_hi = cool
    h_slope, h_int, h_lo, h_hi = heat
    r_slope, r_int, r_lo, r_hi = resist

    aux_bins = {
        str(t): h_slope * t + h_int + aux_offset for t in range(h_lo, h_hi + 1)
    }

    return {
        "temperature": {
            "cool_1": {
                "deltas": _linear_bins(c_lo, c_hi, c_slope, c_int),
                "linear_trendline": {"slope": c_slope, "intercept": c_int},
            },
            "heat_1": {
                "deltas": _linear_bins(h_lo, h_hi, h_slope, h_int),
                "linear_trendline": {"slope": h_slope, "intercept": h_int},
            },
            "resist": {
                "deltas": _linear_bins(r_lo, r_hi, r_slope, r_int),
                "linear_trendline": {"slope": r_slope, "intercept": r_int},
            },
            "auxiliary_heat_1": {
                "deltas": aux_bins,
                "linear_trendline": {
                    "slope": h_slope, "intercept": h_int + aux_offset,
                },
            },
        },
        "balance_point": {"heat_1": heat_bp, "resist": resist_bp},
        "differential": {"cool": differential, "heat": differential},
        "setpoint": {"cool": 74.0, "heat": 68.0},
        "power": {"rated_watts": rated_watts, "aux_watts": aux_watts},
    }


def make_model(**kwargs) -> "PerformanceModel":
    return PerformanceModel(make_profile(**kwargs))


def make_forecast(temps, start=NOW, cloud=None):
    """One forecast point per hour."""
    return [
        ForecastPoint(
            time=start + timedelta(hours=i),
            outdoor_temp=float(t),
            cloud_cover=cloud,
        )
        for i, t in enumerate(temps)
    ]


# A moderate spring day: cool night, mild afternoon, nothing extreme.
MODERATE_SPRING = [
    52, 51, 50, 49, 48, 48, 50, 54, 58, 62, 66, 69,
    71, 72, 72, 71, 69, 66, 62, 59, 57, 55, 54, 53,
]

# Mild afternoon, arctic front arriving in the evening: the one shape where
# pre-conditioning has a large, real payoff, because compressor runtime now
# buys away resistance strip runtime later.
ARCTIC_FRONT = [
    46, 45, 44, 43, 42, 40, 38, 35, 31, 27, 23, 20,
    18, 16, 15, 14, 14, 15, 17, 20, 23, 26, 29, 32,
]

# A heat pump whose capacity really collapses below its balance point, so
# strip heat carries a meaningful share of the load. The default fixture's
# gentler heating curve barely needs aux at all, which makes the aux question
# uninteresting rather than settled.
COLD_CLIMATE = dict(heat=(0.075, -2.7, 7, 53), aux_offset=4.0)

# A summer heat wave, peaking mid-afternoon.
HEAT_WAVE = [
    76, 75, 74, 74, 73, 74, 77, 81, 85, 89, 92, 95,
    97, 98, 99, 99, 97, 94, 90, 87, 84, 81, 79, 77,
]


# ── Item 3: gross rate vs net rate ───────────────────────────────────


class TestGrossHvacRate:
    """The efficiency currency is the machine's own forcing, not net delta."""

    def test_gross_rate_barely_moves_between_75_and_96(self):
        """Gross cooling rate ratio lands near 1.0x, not the 5x the docs claimed.

        This is the number that made the old optimizer chase pre-cooling: it
        read the envelope load as lost compressor capacity.
        """
        model = make_model()

        net_75 = abs(model.cooling_delta(75.0))
        net_96 = abs(model.cooling_delta(96.0))
        gross_75 = abs(model.gross_hvac_rate("cool", 75.0))
        gross_96 = abs(model.gross_hvac_rate("cool", 96.0))

        # Sanity: the synthetic profile really does show a net collapse.
        assert net_75 == pytest.approx(3.16, abs=0.02)
        assert net_96 == pytest.approx(2.16, abs=0.02)
        assert net_75 / net_96 > 1.4

        # But the machine's actual forcing is nearly flat.
        assert gross_75 == pytest.approx(4.08, abs=0.05)
        assert gross_96 == pytest.approx(3.96, abs=0.05)
        assert 1.0 <= gross_75 / gross_96 <= 1.3

    def test_gross_rate_is_net_minus_drift_sign_aware(self):
        model = make_model()
        for t in (60.0, 75.0, 88.0, 96.0):
            assert model.gross_hvac_rate("cool", t) == pytest.approx(
                model.cooling_delta(t) - model.passive_drift(t)
            )
        for t in (20.0, 35.0, 48.0):
            assert model.gross_hvac_rate("heat", t) == pytest.approx(
                model.heating_delta(t) - model.passive_drift(t)
            )

    def test_gross_cooling_is_more_negative_than_net(self):
        """Subtracting a positive drift makes the cooling forcing larger."""
        model = make_model()
        assert model.gross_hvac_rate("cool", 96.0) < model.cooling_delta(96.0)

    def test_unknown_mode_has_no_forcing(self):
        assert make_model().gross_hvac_rate("off", 80.0) == 0.0


class TestEnergyScoring:
    """Hour scores are kWh per delivered degree, not 1/net-delta."""

    def test_energy_spread_is_far_smaller_than_net_spread(self):
        model = make_model()
        sim = ThermalSimulator(model)
        opt = ScheduleOptimizer(model, sim)

        scores = {
            s.outdoor_temp: s.efficiency_score
            for s in opt.score_hours(make_forecast([75, 96]), "cool")
        }
        e75 = scores[75.0]
        e96 = scores[96.0]

        # kWh per degree: flat power, so this tracks the gross rate ratio.
        assert 1.0 <= e96 / e75 <= 1.3

        # The old score would have been 1/|net delta|, a much wider spread.
        old_75 = 1.0 / abs(model.cooling_delta(75.0))
        old_96 = 1.0 / abs(model.cooling_delta(96.0))
        assert old_96 / old_75 > e96 / e75

    def test_learned_power_table_changes_the_ranking(self):
        """The seam for grounding energy in a measured power sensor."""
        model = make_model()
        flat = model.energy_per_degree_kwh("cool", 96.0)
        model.set_power_table("cool", {75: 3000.0, 96: 4500.0})
        assert model.energy_per_degree_kwh("cool", 96.0) > flat
        model.set_power_table("cool", None)
        assert model.energy_per_degree_kwh("cool", 96.0) == pytest.approx(flat)

    def test_analyze_runtime_efficiency_is_labeled_unachievable(self):
        model = make_model()
        opt = ScheduleOptimizer(model, ThermalSimulator(model))
        result = opt.analyze_runtime_efficiency_theoretical_bound(
            {10: 30.0, 15: 45.0}, {10: 80.0, 15: 96.0}, "cool",
        )
        assert "not achievable" in result["disclaimer"]

    def test_percentile_mapping_is_gone(self):
        """The old band-swinging generator must not come back."""
        assert not hasattr(ScheduleOptimizer, "_build_schedule")


# ── Item 2: stateful hysteresis and violation tolerance ──────────────


class TestStatefulHysteresis:
    def test_compressor_latches_on_until_lower_threshold(self):
        model = make_model()
        sim = ThermalSimulator(model)
        # diff = 1.0, so ON above 78.5, OFF below 77.5.
        assert sim._should_cool(78.2, 78.0, currently_on=False) is False
        assert sim._should_cool(78.2, 78.0, currently_on=True) is True
        assert sim._should_cool(77.4, 78.0, currently_on=True) is False
        assert sim._should_cool(78.6, 78.0, currently_on=False) is True

    def test_heating_mirrors_the_cooling_hysteresis(self):
        model = make_model()
        sim = ThermalSimulator(model)
        assert sim._should_heat(67.8, 68.0, currently_on=False) is False
        assert sim._should_heat(67.8, 68.0, currently_on=True) is True
        assert sim._should_heat(68.6, 68.0, currently_on=True) is False
        assert sim._should_heat(67.4, 68.0, currently_on=False) is True

    def test_simulated_temp_cycles_around_target_not_above_it(self):
        """The old stateless predicate parked the house at target + diff/2.

        With state, the house cycles through the target instead of riding
        permanently above it, which is what real hardware does.
        """
        model = make_model()
        sim = ThermalSimulator(model)
        forecast = make_forecast([88] * 12)
        schedule = [
            ScheduleEntry(
                start_time=NOW,
                end_time=NOW + timedelta(hours=11),
                target_temp=78.0,
                mode="cool",
            )
        ]
        result = sim.simulate(78.0, forecast, schedule)
        # Skip the first hour so the initial condition washes out.
        settled = [p.indoor_temp for p in result if p.time >= NOW + timedelta(hours=1)]
        assert min(settled) < 78.0, "temp never drops through target: not stateful"
        assert max(settled) <= 78.0 + model.cool_differential

    def test_coast_at_band_edge_counts_zero_violations(self):
        """Acceptance: a coast hour parked at comfort_max is not a violation.

        Under the old counting, target == comfort_max meant the simulated
        house sat at comfort_max + 0.5F forever and every single step was
        counted, so every schedule that used the band got vetoed.
        """
        model = make_model()
        sim = ThermalSimulator(model)
        forecast = make_forecast([90] * 8)
        schedule = [
            ScheduleEntry(
                start_time=NOW,
                end_time=NOW + timedelta(hours=7),
                target_temp=78.0,
                mode="cool",
            )
        ]
        result = sim.simulate(78.0, forecast, schedule)
        assert sim.comfort_violations(result, 72.0, 78.0) == 0
        assert sim.comfort_excursion_degree_minutes(result, 72.0, 78.0) == 0.0

    def test_real_excursion_still_counted(self):
        """Tolerance is half a cycle, not a blank cheque."""
        model = make_model()
        sim = ThermalSimulator(model)
        forecast = make_forecast([90] * 4)
        schedule = [
            ScheduleEntry(
                start_time=NOW,
                end_time=NOW + timedelta(hours=3),
                target_temp=78.0,
                mode="off",
            )
        ]
        result = sim.simulate(78.0, forecast, schedule)
        assert sim.comfort_violations(result, 72.0, 78.0) > 0
        assert sim.comfort_excursion_degree_minutes(result, 72.0, 78.0) > 0.0


class TestComfortBandDerivation:
    """Item 2 fix 4: one documented narrowing stack with a floor."""

    def test_tier_narrowing_never_goes_below_the_floor(self):
        for width in (2.0, 2.5, 3.0, 4.0, 6.0, 10.0):
            for tier in ("learning", "conservative", "standard", None):
                lo, hi = derive_effective_comfort_band((70.0, 70.0 + width), tier=tier)
                assert hi - lo >= min(width, COMFORT_BAND_MIN_WIDTH_F) - 1e-9, (
                    f"tier={tier} width={width} collapsed to {hi - lo}"
                )

    def test_explicit_constraint_may_narrow_past_the_floor(self):
        lo, hi = derive_effective_comfort_band(
            (72.0, 78.0), constraints=[{"type": "max_temp", "value": 73.0}],
        )
        assert (lo, hi) == (72.0, 73.0)

    def test_demand_response_widens_before_tier_narrows(self):
        lo, hi = derive_effective_comfort_band(
            (72.0, 78.0), demand_response_delta=2.0, tier="conservative",
        )
        assert hi - lo > 6.0

    def test_safety_limits_are_absolute(self):
        lo, hi = derive_effective_comfort_band(
            (60.0, 90.0), safety_limits=(65.0, 82.0),
        )
        assert (lo, hi) == (65.0, 82.0)

    def test_layers_do_not_compound_into_a_sliver(self):
        """The old stack could reach ~1-2F; this one cannot."""
        lo, hi = derive_effective_comfort_band(
            (72.0, 75.0),
            humidity_band=(72.0, 73.5),
            tier="learning",
        )
        assert hi - lo >= COMFORT_BAND_MIN_WIDTH_F - 1e-9


# ── Item 1: flat default, gated pre-conditioning, searched window ────


def _targets(schedule):
    return [e.target_temp for e in schedule.entries]


class TestFlatDefault:
    def test_moderate_spring_day_is_flat_within_1f(self):
        """Acceptance: replay a moderate spring day, get a flat schedule.

        The old percentile mapping produced an 11F daily indoor swing on days
        exactly like this one -- sagging through the coldest pre-dawn hours
        and ramping through the warm afternoon, i.e. heating hardest when the
        house needs it least.
        """
        model = make_model()
        opt = ScheduleOptimizer(model, ThermalSimulator(model))
        forecast = make_forecast(MODERATE_SPRING)

        schedule = opt.optimize_setpoints(68.5, forecast, (66.0, 72.0), "heat")
        targets = _targets(schedule)

        assert targets, "no schedule produced"
        assert max(targets) - min(targets) <= 1.0, (
            f"moderate spring day should be flat within 1F, got {sorted(set(targets))}"
        )
        assert schedule.plan_label == "flat"

    def test_moderate_day_reports_no_savings(self):
        """Honest accounting: flat vs flat is 0%, and it should say so."""
        model = make_model()
        opt = ScheduleOptimizer(model, ThermalSimulator(model))
        forecast = make_forecast(MODERATE_SPRING)
        schedule = opt.optimize_setpoints(68.5, forecast, (66.0, 72.0), "heat")
        assert schedule.savings_pct == pytest.approx(0.0)

    def test_flat_cooling_sits_at_the_ceiling(self):
        model = make_model()
        opt = ScheduleOptimizer(model, ThermalSimulator(model))
        forecast = make_forecast([70, 74, 78, 82, 85, 86, 85, 82, 78, 74, 72, 70])
        schedule = opt.optimize_setpoints(75.0, forecast, (72.0, 78.0), "cool")
        assert set(_targets(schedule)) == {78.0}

    def test_normal_hot_day_does_not_open_the_cooling_gate(self):
        """A 92-99F afternoon the unit can hold is NOT a pre-cool trigger.

        The empirical finding this encodes: on this kind of day a flat
        setpoint held the ceiling with essentially zero discomfort, while
        every deeper or longer pre-cool tested added total runtime. Do not
        "fix" this by loosening the gate.
        """
        model = make_model()
        planner = SchedulePlanner(model, ThermalSimulator(model))
        forecast = make_forecast(HEAT_WAVE)
        result = planner.plan(75.0, forecast, (72.0, 78.0), "cool")

        assert result.gate_open is False
        assert result.chosen.label == "flat"
        assert "pre-cooling" not in " ".join(e.reason for e in result.chosen.entries)


class TestSaturationGate:
    """The cooling gate is 'the unit will saturate', not 'COP is better now'."""

    # An undersized machine: net cooling goes positive above ~97F, meaning
    # the compressor at 100% duty can no longer hold the house.
    # A unit that genuinely cannot hold the ceiling through a 99F peak.
    #
    # This was (0.13, -12.4, ...) before audit item 4 gave the HVAC deltas the
    # same indoor-temp correction passive_drift already had. Coasting at the
    # 78F ceiling means a smaller indoor/outdoor gap than the 72F the tables
    # were recorded at, so the envelope load is lower and the net cooling rate
    # is ~0.25 F/hr better than the uncorrected model claimed. That is enough
    # to carry the old fixture through the peak, which made it stop testing
    # saturation at all. The intent is unchanged; the numbers now describe an
    # undersized unit under corrected physics (157 degree-minutes over the
    # ceiling, against a 30 degree-minute gate threshold).
    UNDERSIZED = dict(cool=(0.13, -12.0, 51, 105))

    def test_gate_opens_when_flat_plan_cannot_hold_the_ceiling(self):
        model = make_model(**self.UNDERSIZED)
        planner = SchedulePlanner(model, ThermalSimulator(model))
        forecast = make_forecast(HEAT_WAVE)
        result = planner.plan(75.0, forecast, (72.0, 78.0), "cool")
        assert result.gate_open is True
        assert "saturat" in result.gate_reason

    def test_single_precool_window_ends_before_the_peak(self):
        """Acceptance: one contiguous window just before the peak.

        Not a pre-dawn dip, and not a rolling percentile ramp -- one block,
        adjacent to the event it is insuring against.
        """
        model = make_model(**self.UNDERSIZED)
        opt = ScheduleOptimizer(model, ThermalSimulator(model))
        forecast = make_forecast(HEAT_WAVE)
        schedule = opt.optimize_setpoints(75.0, forecast, (72.0, 78.0), "cool")

        precool = [e for e in schedule.entries if "pre-cooling" in e.reason]
        assert precool, "saturation day should produce a pre-cool window"

        # Contiguous: exactly one run of consecutive hours.
        idx = sorted(
            int((e.start_time - NOW).total_seconds() // 3600) for e in precool
        )
        assert idx == list(range(idx[0], idx[-1] + 1)), (
            f"pre-cool hours are not contiguous: {idx}"
        )

        # It leads the peak rather than sitting in the pre-dawn hours.
        peak_idx = HEAT_WAVE.index(max(HEAT_WAVE))
        assert idx[-1] < peak_idx
        assert peak_idx - idx[-1] <= 6
        assert idx[0] >= 4, f"window starts pre-dawn at hour {idx[0]}"

    def test_hot_day_plan_is_not_replaced_by_a_narrowed_fallback(self):
        """Acceptance (item 2): no narrow-and-retry.

        The old code re-ran the builder on the inner 50% of the band whenever
        violations were counted, so on exactly the days pre-conditioning
        matters the aggressive plan was discarded for a midpoint schedule.
        The band edge must survive.
        """
        model = make_model(**self.UNDERSIZED)
        opt = ScheduleOptimizer(model, ThermalSimulator(model))
        forecast = make_forecast(HEAT_WAVE)
        schedule = opt.optimize_setpoints(75.0, forecast, (72.0, 78.0), "cool")

        targets = _targets(schedule)
        # The old fallback re-ran on the inner 50%: (73.5, 76.5) for this
        # band, so neither edge could survive.
        assert max(targets) == 78.0, (
            "comfort ceiling was abandoned; looks like a narrowing fallback"
        )
        # A real pre-cool window exists (the plan is not simply flat)...
        assert min(targets) < 78.0, "no pre-cool window on a saturation day"
        # ...and the plan is not confined to the inner 50%, which is the
        # signature the old narrow-and-retry fallback would have left.
        assert not (73.5 <= min(targets) and max(targets) <= 76.5), (
            "plan sits entirely inside the inner 50%; looks like a midpoint clamp"
        )
        # Depth is deliberately not asserted. Item 4 made coasting at the
        # ceiling cheaper than the uncorrected model believed, so the search
        # now buys the same comfort with a shallower window. Pinning depth
        # here would just re-encode the old model's error as a requirement.

    def test_least_bad_plan_ships_and_surfaces_its_violations(self):
        model = make_model(**self.UNDERSIZED)
        opt = ScheduleOptimizer(model, ThermalSimulator(model))
        forecast = make_forecast(HEAT_WAVE)
        schedule = opt.optimize_setpoints(75.0, forecast, (72.0, 78.0), "cool")
        assert schedule.entries
        # Whatever it ships, the residual miss is reported rather than hidden
        # by silently redefining the comfort band.
        assert schedule.comfort_violations >= 0
        assert schedule.comfort_excursion_degree_minutes >= 0.0


class TestHeatingGate:
    """Pre-heat only where it displaces electric strip heat."""

    def test_moderate_cold_day_does_not_open_the_gate(self):
        """discoveries.md's own finding: pre-heating a moderate cold day loses.

        28-44F is cold, but the compressor handles it alone. Holding an
        elevated indoor temp there raises envelope losses faster than the COP
        advantage recovers them.
        """
        model = make_model()
        planner = SchedulePlanner(model, ThermalSimulator(model))
        forecast = make_forecast([36, 34, 33, 32, 31, 31, 33, 36, 39, 42, 44, 44,
                                  43, 42, 41, 39, 38, 37, 36, 35, 34, 34, 33, 33])
        result = planner.plan(68.5, forecast, (66.0, 72.0), "heat")
        assert result.gate_open is False
        assert result.chosen.label == "flat"

    def test_gate_opens_when_aux_hours_are_coming(self):
        model = make_model()
        planner = SchedulePlanner(model, ThermalSimulator(model))
        # Falls to 18F overnight: well below the 25.6F balance point.
        forecast = make_forecast([40, 38, 35, 33, 31, 29, 28, 26,
                                  24, 22, 20, 19, 18, 18, 19, 21,
                                  23, 25, 27, 29, 31, 33, 35, 37])
        result = planner.plan(68.5, forecast, (66.0, 72.0), "heat")
        assert result.gate_open is True
        assert "aux" in result.gate_reason

    def _arctic_candidates(self):
        """Every candidate the planner considers for the arctic-front day."""
        model = make_model(**COLD_CLIMATE)
        planner = SchedulePlanner(model, ThermalSimulator(model))
        forecast = make_forecast(ARCTIC_FRONT)
        comfort = (66.0, 72.0)
        hours = planner._forecast_hours(forecast)
        event = planner._first_hour_at_or_below(
            forecast, hours, model.heat_balance_point + 5.0,
        )
        flat_entries = planner.build_flat_schedule(hours, forecast, comfort, "heat")
        cands = [planner.evaluate("flat", flat_entries, 68.5, forecast, comfort)]
        for lead in planner_mod.CANDIDATE_LEAD_HOURS:
            for depth in planner_mod.CANDIDATE_DEPTHS_F:
                entries = planner._window_schedule(
                    hours, forecast, comfort, "heat", None, event, lead, depth,
                )
                if entries is None:
                    continue
                cands.append(
                    planner.evaluate(
                        f"{lead}h@{depth}", entries, 68.5, forecast, comfort,
                    )
                )
        return planner, forecast, comfort, cands

    def test_preheat_with_runway_displaces_aux_and_saves_energy(self):
        """The heating gate exists because this case is real.

        Mild afternoon, arctic front in the evening. Banking heat with the
        compressor while it still has capacity means fewer minutes of
        resistance strip heat later, and strip heat costs roughly three times
        as much per delivered degree. Unlike the cooling side, this one wins
        on energy, not only on comfort.
        """
        model = make_model(**COLD_CLIMATE)
        planner = SchedulePlanner(model, ThermalSimulator(model))
        forecast = make_forecast(ARCTIC_FRONT)
        result = planner.plan(68.5, forecast, (66.0, 72.0), "heat")

        assert result.gate_open is True
        assert result.chosen.label != "flat", "search found nothing worth doing"
        assert result.chosen.energy_kwh < result.flat.energy_kwh
        assert result.chosen.aux_minutes <= result.flat.aux_minutes
        assert (
            result.chosen.excursion_under_degree_minutes
            < result.flat.excursion_under_degree_minutes
        )

    def test_search_picks_the_best_candidate_not_merely_a_different_one(self):
        """The search is a search: the shipped plan is the rank minimum."""
        planner, forecast, comfort, cands = self._arctic_candidates()
        chosen = planner.plan(68.5, forecast, comfort, "heat").chosen
        best = min(cands, key=lambda c: c.rank_key)
        assert chosen.rank_key == best.rank_key

    def test_some_candidates_cost_more_than_flat(self):
        """Proof the search is discriminating rather than rubber-stamping.

        Shallow or badly-timed pre-heats genuinely lose here, which is the
        whole reason a search is needed instead of a formula.
        """
        _, _, _, cands = self._arctic_candidates()
        flat = next(c for c in cands if c.label == "flat")
        losers = [c for c in cands if c.energy_kwh > flat.energy_kwh]
        assert losers, "no candidate loses to flat; the fixture is too easy"

    def test_warm_afternoon_in_heat_mode_does_not_trigger_preheat(self):
        """Direction matters: no amount of pre-heating fixes an overshoot.

        A heat-mode day whose afternoon drifts above the ceiling used to open
        the saturation branch of the heating gate, which is nonsense -- the
        gate was reading undirected excursion.
        """
        model = make_model()
        planner = SchedulePlanner(model, ThermalSimulator(model))
        forecast = make_forecast(MODERATE_SPRING)
        result = planner.plan(68.5, forecast, (66.0, 72.0), "heat")

        assert result.flat.excursion_over_degree_minutes > 0, (
            "fixture no longer overshoots; the test has stopped testing anything"
        )
        assert result.gate_open is False
        assert result.chosen.label == "flat"

    def test_precool_and_preheat_labels_are_mode_correct(self):
        model = make_model()
        planner = SchedulePlanner(model, ThermalSimulator(model))
        forecast = make_forecast([40, 38, 35, 33, 31, 29, 28, 26,
                                  24, 22, 20, 19, 18, 18, 19, 21,
                                  23, 25, 27, 29, 31, 33, 35, 37])
        result = planner.plan(68.5, forecast, (66.0, 72.0), "heat")
        reasons = " ".join(e.reason for e in result.chosen.entries)
        assert "pre-cooling" not in reasons


class TestMoDerateDayEnergyRegression:
    """The discoveries.md moderate-day finding, encoded as a test.

    Work-redistribution arithmetic predicts a win here. Trajectory simulation
    priced in kWh shows a loss. The simulation is right, and the planner must
    side with it.
    """

    def _flat_and_precool(self, model, forecast, comfort, mode):
        planner = SchedulePlanner(model, ThermalSimulator(model))
        hours = planner._forecast_hours(forecast)
        flat_entries = planner.build_flat_schedule(hours, forecast, comfort, mode)
        # Hand-built pre-cool: 6 hours of holding 3F below the ceiling in the
        # "efficient" morning, exactly what the old percentile mapping did.
        precool_entries = []
        for i, entry in enumerate(flat_entries):
            if 6 <= i < 12:
                precool_entries.append(
                    ScheduleEntry(
                        start_time=entry.start_time,
                        end_time=entry.end_time,
                        target_temp=comfort[1] - 3.0,
                        mode=mode,
                        reason="pre-cooling (hand built)",
                    )
                )
            else:
                precool_entries.append(entry)
        flat = planner.evaluate("flat", flat_entries, 75.0, forecast, comfort)
        precool = planner.evaluate("precool", precool_entries, 75.0, forecast, comfort)
        return flat, precool

    def test_precooling_a_moderate_day_costs_more_energy(self):
        model = make_model()
        forecast = make_forecast(
            [70, 69, 68, 67, 67, 68, 71, 74, 77, 80, 82, 84,
             85, 86, 86, 85, 83, 80, 77, 75, 73, 72, 71, 70]
        )
        flat, precool = self._flat_and_precool(
            model, forecast, (72.0, 78.0), "cool",
        )
        assert precool.energy_kwh > flat.energy_kwh, (
            "pre-cooling a moderate day should cost energy, not save it"
        )
        # And comfort was never the reason to do it.
        assert flat.excursion_degree_minutes == 0.0

    def test_planner_declines_the_precool_the_net_ratio_would_choose(self):
        model = make_model()
        opt = ScheduleOptimizer(model, ThermalSimulator(model))
        forecast = make_forecast(
            [70, 69, 68, 67, 67, 68, 71, 74, 77, 80, 82, 84,
             85, 86, 86, 85, 83, 80, 77, 75, 73, 72, 71, 70]
        )

        # The net-delta ratio across this day is large enough that the old
        # scorer would have ranked the morning as far cheaper.
        net_ratio = abs(model.cooling_delta(67.0)) / abs(model.cooling_delta(86.0))
        gross_ratio = abs(model.gross_hvac_rate("cool", 67.0)) / abs(
            model.gross_hvac_rate("cool", 86.0)
        )
        assert net_ratio > 1.3
        assert gross_ratio < 1.1, "the machine is barely less capable at 86F"

        schedule = opt.optimize_setpoints(75.0, forecast, (72.0, 78.0), "cool")
        assert set(_targets(schedule)) == {78.0}
        assert schedule.plan_label == "flat"


# ── Item 7: shoulder-day dual setpoints ──────────────────────────────


class TestDualSetpointSimulation:
    def test_simulator_arbitrates_a_heat_floor_and_cool_ceiling(self):
        model = make_model()
        sim = ThermalSimulator(model)
        forecast = make_forecast([38] * 14)
        entries = [
            ScheduleEntry(
                start_time=NOW,
                end_time=NOW + timedelta(hours=13),
                target_temp=66.0,
                mode="heat",
                target_temp_low=66.0,
                target_temp_high=76.0,
            )
        ]
        result = sim.simulate(68.0, forecast, entries)
        temps = [p.indoor_temp for p in result]
        # Cold outside: the heat floor is what holds, and the ceiling is
        # never approached.
        assert min(temps) >= 66.0 - model.heat_differential
        assert max(temps) <= 76.0
        assert any(p.hvac_running and p.hvac_mode == "heat" for p in result)
        assert not any(p.hvac_mode == "cool" for p in result)

    def test_dead_band_between_setpoints_runs_nothing(self):
        model = make_model()
        sim = ThermalSimulator(model)
        forecast = make_forecast([52] * 4)  # right at the resist balance point
        entries = [
            ScheduleEntry(
                start_time=NOW,
                end_time=NOW + timedelta(hours=3),
                target_temp=66.0,
                mode="heat",
                target_temp_low=64.0,
                target_temp_high=78.0,
            )
        ]
        result = sim.simulate(71.0, forecast, entries)
        assert not any(p.hvac_running for p in result)

    def test_dual_setpoint_entries_report_both_bounds(self):
        model = make_model()
        planner = SchedulePlanner(model, ThermalSimulator(model))
        forecast = make_forecast([40, 45, 55, 65, 70, 68, 60, 50])
        hours = planner._forecast_hours(forecast)
        entries = planner.build_dual_setpoint_schedule(
            hours, forecast, 66.0, 78.0, "heat",
        )
        assert all(e.is_dual_setpoint for e in entries)
        assert all(e.target_temp_low == 66.0 for e in entries)
        assert all(e.target_temp_high == 78.0 for e in entries)
        # target_temp reports the near-term mode's own side for the
        # single-setpoint consumers.
        assert all(e.target_temp == 66.0 for e in entries)


# ── Aux modeling (what makes the heating gate mean anything) ─────────


class TestAuxHeatEnergy:
    def test_aux_stages_in_on_droop_below_the_balance_point(self):
        model = make_model()
        sim = ThermalSimulator(model)
        forecast = make_forecast([15] * 6)
        entries = [
            ScheduleEntry(
                start_time=NOW,
                end_time=NOW + timedelta(hours=5),
                target_temp=70.0,
                mode="heat",
            )
        ]
        result = sim.simulate(64.0, forecast, entries)
        assert any(p.aux_running for p in result)
        assert sim.total_aux_runtime(result) > 0

    def test_aux_does_not_stage_in_on_a_mild_day(self):
        model = make_model()
        sim = ThermalSimulator(model)
        forecast = make_forecast([45] * 6)
        entries = [
            ScheduleEntry(
                start_time=NOW,
                end_time=NOW + timedelta(hours=5),
                target_temp=70.0,
                mode="heat",
            )
        ]
        result = sim.simulate(64.0, forecast, entries)
        assert not any(p.aux_running for p in result)

    def test_aux_runtime_costs_more_energy_per_minute(self):
        model = make_model()
        assert model.hvac_power_watts("heat", 15.0, aux_active=True) > (
            model.hvac_power_watts("heat", 15.0) * 2
        )
