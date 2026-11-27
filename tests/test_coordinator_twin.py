"""Tests for the coordinator twin mixin (TWIN_STATS_SPEC section 6; Phase 3)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.heatpump_optimizer.coordinator_twin import TwinMixin, replay_twin_history, twin_location
from custom_components.heatpump_optimizer.dashboard_archive import DashboardArchive
from custom_components.heatpump_optimizer.engine.twin import DayInputs
from custom_components.heatpump_optimizer.learning.twin_ledger import TwinLedger


def _rec(day, replay_heat):
    return {
        "date": day,
        "source": "live",
        "simulated": True,
        "warmup": False,
        "actual": {
            "heat_h": 10.0,
            "cool_h": 0.0,
            "aux_h": 0.0,
            "out": 30.0,
            "in": 66.0,
            "controlled": 0.0,
            "covered": 1440.0,
            "out_frac": 1.0,
            "metered_kwh": None,
            "mae": [12.0, 24],
        },
        "arms": {
            "replay": {"heat_h": replay_heat, "cool_h": 0, "aux_h": 0, "in": 66, "off_h": 0, "end": 66},
            "reference": {"heat_h": replay_heat + 5, "cool_h": 0, "aux_h": 0, "in": 72, "off_h": 0, "end": 72},
            "warmer": {"heat_h": replay_heat + 1, "cool_h": 0, "aux_h": 0, "in": 67, "off_h": 0, "end": 67},
            "plan": {"heat_h": replay_heat - 1, "cool_h": 0, "aux_h": 0, "in": 66, "off_h": 0, "end": 66},
        },
        "aux": {"recovery": 0.0, "cold": 0.0, "other": 0.0},
        "aux_bins": {},
        "watts": {"heat": 3000, "cool": 3000, "aux": 10000, "heat_source": "rated", "cool_source": "rated", "aux_source": "configured"},
        "ref": [72.0, 72.0],
    }


class _Stub(TwinMixin):
    def __init__(self, replay_heat: float):
        self.hass = SimpleNamespace(config=SimpleNamespace(time_zone="UTC", latitude=40.0, longitude=-80.0))
        self.entry = SimpleNamespace(options={})
        self.sensor_hub = MagicMock()
        self.sensor_hub.read_electricity_rate.return_value = 0.2
        self.sensor_hub._heating_watts = 3000.0
        self.sensor_hub._cooling_watts = 3000.0
        self.strategic = SimpleNamespace(forecast_snapshot=[])
        self._aux_heat_kw = 10.0
        self.aux_heat_learner = SimpleNamespace(is_learned=False, threshold_f=None)
        self.optimizer_is_controlling = False
        self.dashboard_archive = DashboardArchive()
        self.twin_ledger = TwinLedger("UTC", 40.0, -80.0)
        d0 = datetime(2026, 1, 1)
        for i in range(30):
            day = (d0 + timedelta(days=i)).date().isoformat()
            self.twin_ledger.records[day] = _rec(day, replay_heat)
        self._twin_summary_cache = None

    def _get_active_model(self):
        return None

    def _get_current_entry(self, now):
        return None


class TestSensorData:
    def test_trusted_grade_publishes(self):
        data = _Stub(10.0)._twin_sensor_data()
        assert data["twin_grade"] == "good"
        assert data["twin_accuracy_pct"] == 0.0
        assert data["twin_reference_kwh"] == pytest.approx(30 * 15.0)
        assert data["twin_effect_kwh"] == pytest.approx(30 * 3.0)
        assert data["twin_effect_arm"] == "plan"

    def test_poor_grade_hides_modeled_figures(self):
        data = _Stub(16.0)._twin_sensor_data()
        assert data["twin_grade"] == "poor"
        assert data["twin_accuracy_pct"] == pytest.approx(60.0)
        assert data["twin_reference_kwh"] is None
        assert data["twin_reference_cost"] is None
        assert data["twin_effect_kwh"] is None

    def test_cached(self):
        stub = _Stub(10.0)
        first = stub._twin_sensor_data()
        stub.twin_ledger.records.clear()
        assert stub._twin_sensor_data() is first

    def test_reference_option(self):
        stub = _Stub(10.0)
        stub.entry.options = {"twin_reference_heat_f": 70, "twin_reference_cool_f": "75"}
        assert stub._twin_reference() == (70.0, 75.0)
        stub.entry.options = {"twin_reference_heat_f": "bad"}
        assert stub._twin_reference() == (72.0, 72.0)


class TestCycleFeed:
    def test_day_close_runs_inline(self):
        stub = _Stub(10.0)
        stub.twin_ledger = TwinLedger("UTC", 40.0, -80.0)
        thermo = SimpleNamespace(
            indoor_temp=66.0, hvac_mode="heat", target_temp=66.0, target_temp_low=None, target_temp_high=None,
            hvac_action="idle",
        )
        start = datetime(2026, 1, 10, 23, 50, tzinfo=UTC)
        for i in range(4):
            stub._record_twin_cycle(start + timedelta(minutes=5 * i), thermo, 30.0, False, None, False, None)
        assert "2026-01-10" in stub.twin_ledger.records
        assert stub.twin_ledger.records["2026-01-10"]["simulated"] is False  # no fit yet


class TestReplayHistory:
    def test_recorder_days_marked(self):
        start = datetime(2026, 1, 10, tzinfo=UTC)
        grid = [start + timedelta(minutes=5 * i) for i in range(2 * 288 + 1)]
        n = len(grid)
        series = {
            "grid": grid,
            "thermostat_indoor": [65.0] * n,
            "outdoor": [30.0] * n,
            "action": ["heating" if i % 4 == 0 else "idle" for i in range(n)],
            "aux": [False] * n,
            "mode": ["heat"] * n,
            "temperature": [66.0] * n,
            "target_low": [None] * n,
            "target_high": [None] * n,
        }
        led = TwinLedger("UTC")
        days = replay_twin_history(led, series)
        assert [d.date for d, _ in days] == ["2026-01-10", "2026-01-11"]
        assert all(d.source == "recorder" for d, _ in days)
        assert days[0][0].heat_min[0] == pytest.approx(15.0)
        assert days[0][1] == 65.0


class TestImportAndMerge:
    def test_import_merges_and_feeds_archive(self, tmp_path):
        stub = _Stub(10.0)
        header = (
            'Timestamp,"Compressor 1","Compressor 2","Compressor Mode","Auxiliary Heat 1","Auxiliary Heat 2",Fan,'
            'Accessory,"Accessory Type","System Mode","Indoor Temperature","Indoor Humidity","Outdoor Temperature",'
            '"Outdoor Humidity",Event,Climate,"Setpoint Cool","Setpoint Heat"'
        )
        lines = [header]
        t = datetime(2025, 1, 5)
        for i in range(288 * 2):
            ts = (t + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f'"{ts}",{300 if i % 3 == 0 else 0},0,heat,0,0,0,0,off,heat,65.0,50,30.0,60,,Home,74,66')
        path = tmp_path / "export.csv"
        path.write_text("\n".join(lines))

        async def _exec(fn, *args):
            return fn(*args)

        stub.hass.async_add_executor_job = _exec
        counts = asyncio.run(stub.async_import_thermostat_history(str(path)))
        assert counts["read"] == 2 and counts["merged"] == 2
        assert counts["archive_days"] == 2
        assert stub.twin_ledger.records["2025-01-05"]["source"] == "import"
        assert "2026-01-01" in stub.twin_ledger.records  # live days kept

    def test_merge_days_skips_existing(self):
        arch = DashboardArchive()
        arch._days = [{"d": "2025-01-05", "out": 1.0, "heat": 0, "cool": 0, "aux": 0, "ctl": 0, "n": 24}]
        day = DayInputs.from_dict({"date": "2025-01-05", "hours": 24, "covered_min": 1440})
        other = DayInputs.from_dict({"date": "2025-01-06", "hours": 24, "covered_min": 1440, "heat_min": [30.0] * 24})
        assert arch.merge_days([day, other]) == 1
        assert arch._days[-1]["heat"] == 12.0
        assert arch._days[0]["out"] == 1.0


def test_twin_location_tolerates_mocks():
    assert twin_location(MagicMock()) == ("UTC", None, None)
    hass = SimpleNamespace(config=SimpleNamespace(time_zone="America/New_York", latitude=40, longitude=-80.5))
    assert twin_location(hass) == ("America/New_York", 40.0, -80.5)
