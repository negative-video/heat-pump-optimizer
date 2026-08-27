"""Tests for the confidence high-water mark (audit item 11).

The displayed overall confidence stays smooth, but it may never lead the raw
measurement by more than CONFIDENCE_HWM_MAX_LEAD, and nothing behavioural is
allowed to read the smoothed number.
"""

import importlib.util
import os
import sys
import types

# ── Module loading (same pattern as tests/test_performance_profiler.py) ──

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, PROJECT_ROOT)

CC = os.path.join(PROJECT_ROOT, "custom_components", "heatpump_optimizer")

pkg = types.ModuleType("custom_components")
pkg.__path__ = [os.path.join(PROJECT_ROOT, "custom_components")]
sys.modules.setdefault("custom_components", pkg)

ho = types.ModuleType("custom_components.heatpump_optimizer")
ho.__path__ = [CC]
sys.modules.setdefault("custom_components.heatpump_optimizer", ho)

engine = types.ModuleType("custom_components.heatpump_optimizer.engine")
engine.__path__ = [os.path.join(CC, "engine")]
sys.modules.setdefault("custom_components.heatpump_optimizer.engine", engine)

learning = types.ModuleType("custom_components.heatpump_optimizer.learning")
learning.__path__ = [os.path.join(CC, "learning")]
sys.modules.setdefault("custom_components.heatpump_optimizer.learning", learning)


def _load(full_name: str, path: str):
    spec = importlib.util.spec_from_file_location(full_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


_load(
    "custom_components.heatpump_optimizer.engine.data_types",
    os.path.join(CC, "engine", "data_types.py"),
)
_load(
    "custom_components.heatpump_optimizer.engine.performance_model",
    os.path.join(CC, "engine", "performance_model.py"),
)
profiler_mod = _load(
    "custom_components.heatpump_optimizer.learning.performance_profiler",
    os.path.join(CC, "learning", "performance_profiler.py"),
)

PerformanceProfiler = profiler_mod.PerformanceProfiler
BinAccumulator = profiler_mod.BinAccumulator
MAX_LEAD = profiler_mod.CONFIDENCE_HWM_MAX_LEAD


# ── Helpers ──────────────────────────────────────────────────────────

def _fill(profiler, mode: str, lo: int, hi: int, per_bin: int, delta: float):
    """Populate bins directly so confidence factors are deterministic."""
    for temp in range(lo, hi + 1):
        profiler._bins[mode][temp] = BinAccumulator(
            sum_delta=delta * per_bin,
            sum_sq_delta=delta * delta * per_bin,
            count=per_bin,
        )


def _well_covered(profiler):
    """Give every mode broad, deep, high-quality coverage."""
    _fill(profiler, "cool_1", 65, 100, 40, -3.0)
    _fill(profiler, "heat_1", 0, 55, 40, 1.0)
    _fill(profiler, "auxiliary_heat_1", 0, 35, 40, 2.0)
    _fill(profiler, "resist", 20, 90, 40, -0.5)


# ── Raw vs displayed ─────────────────────────────────────────────────

class TestRawVsDisplayed:
    def test_empty_profiler_is_zero_both_ways(self):
        p = PerformanceProfiler()
        assert p.confidence() == 0.0
        assert p.confidence(raw=True) == 0.0

    def test_raw_matches_displayed_while_data_only_grows(self):
        p = PerformanceProfiler()
        _well_covered(p)
        assert p.confidence(raw=True) > 0.7
        assert p.confidence() == p.confidence(raw=True)

    def test_raw_reports_the_drop_immediately(self):
        """Data loss (repair, partial reset, failed restore) shows up at once."""
        p = PerformanceProfiler()
        _well_covered(p)
        high = p.confidence()  # arms the high-water mark
        assert high > 0.7

        # Simulate the model losing its basis: bins gone.
        p._bins = {m: {} for m in profiler_mod.MODES}

        assert p.confidence(raw=True) == 0.0

    def test_displayed_value_may_not_lead_raw_forever(self):
        p = PerformanceProfiler()
        _well_covered(p)
        assert p.confidence() > 0.7

        p._bins = {m: {} for m in profiler_mod.MODES}

        # Displayed number decays to the bounded lead, not the stale mark.
        assert p.confidence() <= MAX_LEAD + 1e-9

    def test_displayed_value_absorbs_small_jitter(self):
        """A new sparse bin dents quality; the display should not flinch."""
        p = PerformanceProfiler()
        _well_covered(p)
        smooth_before = p.confidence()

        # One brand-new cooling bin with a single sample drags quality down.
        p._bins["cool_1"][101] = BinAccumulator(
            sum_delta=-1.0, sum_sq_delta=1.0, count=1
        )

        raw_after = p.confidence(raw=True)
        assert raw_after < smooth_before  # the measurement did regress
        assert smooth_before - raw_after < MAX_LEAD  # but only slightly
        assert p.confidence() == smooth_before  # display is unchanged

    def test_per_mode_confidence_is_never_smoothed(self):
        p = PerformanceProfiler()
        _well_covered(p)
        p.confidence()  # arm the mark

        before = p.confidence("cool_1")
        p._bins["cool_1"] = {}
        assert before > 0.0
        assert p.confidence("cool_1") == 0.0

    def test_raw_read_does_not_arm_the_mark(self):
        p = PerformanceProfiler()
        _well_covered(p)
        assert p.confidence(raw=True) > 0.7
        assert p._confidence_hwm == 0.0

    def test_restore_with_stale_mark_and_no_bins_does_not_lie(self):
        """from_dict() carries confidence_hwm; empty bins must still read low."""
        p = PerformanceProfiler()
        _well_covered(p)
        p.confidence()
        data = p.to_dict()
        data["bins"] = {}
        data["solar_bins"] = {}

        restored = PerformanceProfiler.from_dict(data)
        assert restored._confidence_hwm > 0.7  # the stale mark survived
        assert restored.confidence(raw=True) == 0.0
        assert restored.confidence() <= MAX_LEAD + 1e-9


# ── Behaviour gating ─────────────────────────────────────────────────

class TestBehaviourUsesRaw:
    def test_activation_tier_reads_raw_confidence(self):
        """The tier logic in coordinator.py must not inherit the smoothing."""
        src_path = os.path.join(CC, "coordinator.py")
        with open(src_path, encoding="utf-8") as handle:
            src = handle.read()

        start = src.index("def _compute_activation_tier")
        end = src.index("def _baseline_ready_for_control", start)
        body = src[start:end]

        assert "self.profiler.confidence(raw=True)" in body
        assert "self.profiler.confidence()" not in body
