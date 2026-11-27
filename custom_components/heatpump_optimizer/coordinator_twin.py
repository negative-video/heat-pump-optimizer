"""Coordinator mixin for the twin ledger (TWIN_STATS_SPEC section 6).

The ledger and the twin engine are plain Python. This mixin is the Home
Assistant side: one feed per cycle, the day close, a one-time recorder
backfill, the history import service, the sensor summary and the panel
payload. Observation only: nothing here writes a setpoint or touches the
lifetime savings ledger.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from homeassistant.util import dt as dt_util

from .const import CONF_TWIN_REFERENCE_COOL_F, CONF_TWIN_REFERENCE_HEAT_F, DEFAULT_TWIN_REFERENCE_F
from .engine.twin import parse_beestat_csv, plan_row
from .learning.twin_ledger import TRUSTED_GRADES, TwinLedger, replay_twin_history

_LOGGER = logging.getLogger(__name__)

TWIN_BACKFILL_DAYS = 10
TWIN_BACKFILL_IF_FEWER_DAYS = 2
TWIN_SUMMARY_TTL_S = 900


def twin_location(hass: Any) -> tuple[str, float | None, float | None]:
    """``(time zone, latitude, longitude)`` from HA config, tolerant of mocks."""
    cfg = getattr(hass, "config", None)
    tz = getattr(cfg, "time_zone", None)
    lat = getattr(cfg, "latitude", None)
    lon = getattr(cfg, "longitude", None)
    return (
        tz if isinstance(tz, str) and tz else "UTC",
        float(lat) if isinstance(lat, (int, float)) else None,
        float(lon) if isinstance(lon, (int, float)) else None,
    )


class TwinMixin:
    """Twin ledger plumbing for the coordinator."""

    twin_ledger: TwinLedger

    # -- settings ----------------------------------------------------------

    def _twin_reference(self) -> tuple[float, float]:
        opts = self.entry.options if getattr(self, "entry", None) is not None else {}
        try:
            heat = float(opts.get(CONF_TWIN_REFERENCE_HEAT_F, DEFAULT_TWIN_REFERENCE_F))
            cool = float(opts.get(CONF_TWIN_REFERENCE_COOL_F, DEFAULT_TWIN_REFERENCE_F))
        except (TypeError, ValueError):
            heat = cool = DEFAULT_TWIN_REFERENCE_F
        return heat, cool

    def _twin_close_kwargs(self) -> dict[str, Any]:
        hub = self.sensor_hub
        ref_heat, ref_cool = self._twin_reference()
        learner = getattr(self, "aux_heat_learner", None)
        return {
            "rated_heat": float(getattr(hub, "_heating_watts", 3500.0) or 3500.0),
            "rated_cool": float(getattr(hub, "_cooling_watts", 3500.0) or 3500.0),
            "aux_kw": getattr(self, "_aux_heat_kw", None),
            "ref_heat": ref_heat,
            "ref_cool": ref_cool,
            "aux_threshold": learner.threshold_f if learner is not None and learner.is_learned else None,
        }

    def _twin_set_location(self) -> None:
        _, lat, lon = twin_location(self.hass)
        if lat is not None and lon is not None:
            self.twin_ledger.set_location(lat, lon)

    # -- per cycle ---------------------------------------------------------

    def _record_twin_cycle(
        self,
        now: datetime,
        thermo_state: Any,
        outdoor_temp: float | None,
        aux_heat_active: bool,
        power_watts: float | None,
        power_measured: bool,
        forecast: list | None,
    ) -> None:
        cloud = sun = None
        if forecast:
            cloud = getattr(forecast[0], "cloud_cover", None)
            sun = getattr(forecast[0], "sun_elevation", None)
        mode, heat_sp, cool_sp = plan_row(self._get_current_entry(now))
        res = self.twin_ledger.record_cycle(
            now,
            indoor_temp=getattr(thermo_state, "indoor_temp", None),
            outdoor_temp=outdoor_temp,
            hvac_mode=getattr(thermo_state, "hvac_mode", None),
            target_temp=getattr(thermo_state, "target_temp", None),
            target_low=getattr(thermo_state, "target_temp_low", None),
            target_high=getattr(thermo_state, "target_temp_high", None),
            hvac_action=getattr(thermo_state, "hvac_action", None),
            aux_active=bool(aux_heat_active),
            power_watts=power_watts,
            power_measured=bool(power_measured),
            controlled=bool(self.optimizer_is_controlling),
            cloud=cloud,
            sun_elevation=sun,
            plan=(mode, heat_sp, cool_sp),
        )
        if res is None:
            return
        day, next_top = res
        self._twin_set_location()
        self.twin_ledger.close_day(
            day, base_model=self._get_active_model(), next_top=next_top, **self._twin_close_kwargs()
        )
        self._twin_summary_cache = None

    # -- sensors -----------------------------------------------------------

    def _twin_payload_now(self) -> dict[str, Any]:
        rate = None
        try:
            rate = self.sensor_hub.read_electricity_rate()
        except (AttributeError, TypeError, ValueError):
            rate = None
        ref_heat, ref_cool = self._twin_reference()
        return self.twin_ledger.payload(
            datetime.now(UTC),
            rate=rate,
            today_outdoor=self._twin_today_outdoor(),
            base_model=self._get_active_model(),
            ref_heat=ref_heat,
            ref_cool=ref_cool,
        )

    def _twin_today_outdoor(self) -> float | None:
        """Mean forecast outdoor temperature for the rest of today, local."""
        snapshot = getattr(getattr(self, "strategic", None), "forecast_snapshot", None) or []
        today = dt_util.as_local(datetime.now(UTC)).date()
        temps = []
        for point in snapshot:
            when = getattr(point, "time", None)
            temp = getattr(point, "outdoor_temp", None)
            if when is None or temp is None:
                continue
            if dt_util.as_local(when).date() == today:
                temps.append(temp)
        return sum(temps) / len(temps) if temps else None

    def _twin_sensor_data(self) -> dict[str, Any]:
        """Twin figures for the four sensors, cached for a quarter hour."""
        cache = getattr(self, "_twin_summary_cache", None)
        now = datetime.now(UTC)
        if cache is not None and (now - cache[0]).total_seconds() < TWIN_SUMMARY_TTL_S:
            return cache[1]
        try:
            p = self._twin_payload_now()
        except (ValueError, TypeError, AttributeError, KeyError, ZeroDivisionError):
            _LOGGER.debug("Twin summary failed", exc_info=True)
            return {}
        trusted = p["grade"] in TRUSTED_GRADES
        ref = p["reference"]
        effect = p.get("effect") or {}
        deg = p["degree"]
        data = {
            "twin_grade": p["grade"],
            "twin_accuracy_pct": None if p["gain"] is None else round((p["gain"] - 1) * 100, 1),
            "twin_daily_error_pct": None if p["daily_error"] is None else round(p["daily_error"] * 100, 1),
            "twin_indoor_mae": p["indoor_mae"],
            "twin_days": p["days"],
            "twin_compressor_gain": p["comp_gain"],
            "twin_aux_gain": p["aux_gain"],
            "twin_reference_kwh": ref["saved_kwh"] if trusted else None,
            "twin_reference_cost": ref["saved_cost"] if trusted else None,
            "twin_reference_pct": None if ref["pct"] is None or not trusted else round(ref["pct"] * 100, 1),
            "twin_reference_band_pct": round(ref["band"] * 100, 1),
            "twin_reference_heat_f": ref["heat_f"],
            "twin_reference_cool_f": ref["cool_f"],
            "twin_reference_days": ref["days"],
            "twin_effect_kwh": effect.get("kwh") if trusted else None,
            "twin_effect_cost": effect.get("cost") if trusted else None,
            "twin_effect_band_pct": None if not effect else round(effect["band"] * 100, 1),
            "twin_effect_days": effect.get("days", 0),
            "twin_effect_arm": effect.get("key"),
            "twin_degree_kwh": deg["today_kwh"],
            "twin_degree_cost": deg["today_cost"],
            "twin_degree_source": deg["source"],
            "twin_degree_season": deg["season"],
        }
        self._twin_summary_cache = (now, data)
        return data

    def twin_payload(self) -> dict[str, Any]:
        """The panel's ``twin`` section."""
        return self._twin_payload_now()

    # -- recorder backfill -------------------------------------------------

    async def async_backfill_twin(self) -> None:
        """Replay recorder history into the ledger when it has under 2 days."""
        if len(self.twin_ledger.records) >= TWIN_BACKFILL_IF_FEWER_DAYS:
            return
        series = await self._async_recorder_series(days=TWIN_BACKFILL_DAYS)
        if series is None:
            return
        self._twin_set_location()
        snapshot = self.twin_ledger.to_dict()
        tz = self.twin_ledger.tz
        base_model = self._get_active_model()
        kwargs = self._twin_close_kwargs()

        def _run() -> TwinLedger:
            tmp = TwinLedger.from_dict({"fit": snapshot.get("fit")}, tz)
            finished = replay_twin_history(tmp, series)
            tmp.fit.add_days([d for d, _ in finished])
            for day, next_top in finished:
                tmp.close_day(day, base_model=base_model, next_top=next_top, **kwargs)
            return tmp

        tmp = await self.hass.async_add_executor_job(_run)
        before = len(self.twin_ledger.records)
        self._merge_twin_ledger(tmp, take_open=True)
        _LOGGER.info("Twin ledger backfilled with %d recorder days", len(self.twin_ledger.records) - before)

    def _merge_twin_ledger(self, tmp: TwinLedger, take_open: bool = False) -> None:
        """Adopt a ledger built in an executor job, keeping what the live one
        gained meanwhile (a day closed while the job ran)."""
        live = self.twin_ledger
        for d, rec in live.records.items():
            tmp.records.setdefault(d, rec)
        for d, inp in live.inputs.items():
            tmp.inputs.setdefault(d, inp)
        for d in sorted(live.fit.dates):
            if d not in tmp.fit.dates and d in live.inputs:
                tmp.fit.add_day(live.inputs[d])
        if not take_open or tmp._open is None:
            tmp._open = live._open
            tmp._last_sample = live._last_sample
            tmp._heat_run_min = live._heat_run_min
        tmp.prune()
        self.twin_ledger = tmp
        self._twin_summary_cache = None

    # -- history import (section 7) ----------------------------------------

    async def async_import_thermostat_history(self, path: str) -> dict[str, int]:
        """Parse a Beestat export and merge it. Returns counts."""
        self._twin_set_location()
        tz = self.twin_ledger.tz
        base_model = self._get_active_model()
        kwargs = self._twin_close_kwargs()
        kwargs.pop("ref_heat", None)
        kwargs.pop("ref_cool", None)
        ref_heat, ref_cool = self._twin_reference()
        snapshot = self.twin_ledger.to_dict()

        def _run() -> tuple[dict[str, int], list, TwinLedger]:
            with open(path, newline="", encoding="utf-8") as fh:
                days = parse_beestat_csv(fh, tz)
            tmp = TwinLedger.from_dict(snapshot, tz)
            counts = tmp.add_imported(days, base_model=base_model, ref_heat=ref_heat, ref_cool=ref_cool, **kwargs)
            return counts, days, tmp

        counts, days, tmp = await self.hass.async_add_executor_job(_run)
        self._merge_twin_ledger(tmp)
        try:
            counts["archive_days"] = self.dashboard_archive.merge_days(days)
        except (AttributeError, TypeError, ValueError):
            _LOGGER.debug("Dashboard archive merge failed", exc_info=True)
            counts["archive_days"] = 0
        self._twin_summary_cache = None
        return counts
