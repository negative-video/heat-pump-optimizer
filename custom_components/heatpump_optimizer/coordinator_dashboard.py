"""Coordinator mixin that feeds and serves the dashboard archive.

The archive itself (``dashboard_archive.py``) is plain Python. This mixin is
the Home Assistant side: it hands the archive one reading per cycle, replays
recorder history into it once so the panel is not empty on day one, and
assembles the payload the panel requests over the websocket.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .dashboard_archive import DashboardArchive

_LOGGER = logging.getLogger(__name__)

BACKFILL_DAYS = 10
BACKFILL_STEP_MINUTES = 5
# Below this many logged hours the archive is treated as new and backfilled.
BACKFILL_IF_FEWER_HOURS = 24


def _heat_setpoint(thermo_state: Any) -> float | None:
    """The heating setpoint in force, whichever mode the thermostat is in."""
    low = getattr(thermo_state, "target_temp_low", None)
    if low is not None:
        return low
    if getattr(thermo_state, "hvac_mode", None) == "heat":
        return getattr(thermo_state, "target_temp", None)
    return None


def _local_stamp(iso_utc: str | None) -> str | None:
    if not iso_utc:
        return None
    try:
        return dt_util.as_local(datetime.fromisoformat(iso_utc)).strftime("%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return None


def _fill_forward(series: list[tuple[datetime, Any]], grid: list[datetime]) -> list[Any]:
    """Last value at or before each grid instant (None before the first)."""
    out: list[Any] = []
    index = 0
    last: Any = None
    for instant in grid:
        while index < len(series) and series[index][0] <= instant:
            if series[index][1] is not None:
                last = series[index][1]
            index += 1
        out.append(last)
    return out


def replay_history(
    archive: DashboardArchive,
    grid: list[datetime],
    *,
    indoor: list[float | None],
    outdoor: list[float | None],
    action: list[str | None],
    aux: list[bool | None],
    heat_setpoint: list[float | None],
) -> int:
    """Feed aligned historical series through the archive. Returns steps fed."""
    fed = 0
    for i, instant in enumerate(grid):
        if action[i] is None and indoor[i] is None:
            continue
        archive.record_cycle(
            instant,
            dt_util.as_local(instant),
            indoor=indoor[i],
            outdoor=outdoor[i],
            hvac_action=action[i],
            aux_active=bool(aux[i]),
            heat_setpoint=heat_setpoint[i],
            interval_minutes=BACKFILL_STEP_MINUTES,
            controlled=False,
        )
        fed += 1
    return fed


class DashboardMixin:
    """Dashboard archive plumbing for the coordinator."""

    dashboard_archive: DashboardArchive

    def _planned_recovery_today(self, local_now: datetime) -> tuple[str | None, str | None]:
        """(planned start, deadline) for this morning, as local minute stamps."""
        today = local_now.date()
        for plan in self.strategic.recovery_plan:
            deadline = _local_stamp(plan.get("target_time"))
            if deadline and deadline[:10] == today.isoformat():
                return _local_stamp(plan.get("recovery_start")), deadline
        return None, None

    def _record_dashboard_cycle(
        self,
        now: datetime,
        thermo_state: Any,
        outdoor_temp: float | None,
        aux_heat_active: bool,
    ) -> None:
        local_now = dt_util.as_local(now)
        planned_start, deadline = self._planned_recovery_today(local_now)
        thermostat_indoor = getattr(thermo_state, "indoor_temp", None)
        shown_indoor = getattr(self, "_effective_indoor_temp", None)
        action = getattr(thermo_state, "hvac_action", None)
        if aux_heat_active and action not in ("heating", "cooling"):
            action = "heating"  # strips alone still heat the house
        self.dashboard_archive.record_cycle(
            now,
            local_now,
            indoor=shown_indoor if shown_indoor is not None else thermostat_indoor,
            forecast_indoor=thermostat_indoor,
            outdoor=outdoor_temp,
            hvac_action=action,
            aux_active=bool(aux_heat_active),
            heat_setpoint=_heat_setpoint(thermo_state),
            interval_minutes=self.update_interval.total_seconds() / 60.0 if self.update_interval else 5.0,
            controlled=self.optimizer_is_controlling,
            planned_start=planned_start,
            deadline=deadline,
        )

    def dashboard_payload(self) -> dict[str, Any]:
        """Everything the panel cannot read from entity states or the recorder."""
        now = datetime.now(UTC)
        local_now = dt_util.as_local(now)
        payload = self.dashboard_archive.payload(now, local_now)

        fit = self.energy_baseline.fit()
        diagnostics = self.energy_baseline.diagnostics()
        payload["energy"] = {
            "days": [
                {
                    "d": day.date,
                    "kwh": round(day.kwh, 2),
                    "out": round(day.mean_outdoor_f, 1),
                    "label": day.label,
                }
                for day in self.energy_baseline.days
                if day.usable
            ][-400:],
            "source": fit.source,
            "kwh_per_hdd": round(fit.kwh_per_hdd, 4),
            "kwh_per_cdd": round(fit.kwh_per_cdd, 4),
            "baseline_days": diagnostics["baseline_days"],
            "optimized_days": diagnostics["optimized_days"],
            "days_until_fit": diagnostics["days_until_measured_fit"],
        }
        payload["plan"] = {
            "recovery": [
                {
                    "mode": plan.get("mode"),
                    "start": _local_stamp(plan.get("recovery_start")),
                    "deadline": _local_stamp(plan.get("target_time")),
                    "night_floor": plan.get("night_floor"),
                    "day_floor": plan.get("day_floor"),
                }
                for plan in self.strategic.recovery_plan
            ],
            "controlling": self.optimizer_is_controlling,
            "monitor_only": bool(self._monitor_only),
        }
        try:
            payload["twin"] = self.twin_payload()
        except (ValueError, TypeError, AttributeError, KeyError, ZeroDivisionError):
            _LOGGER.warning("Twin payload failed", exc_info=True)
            payload["twin"] = None
        payload["generated"] = local_now.strftime("%Y-%m-%dT%H:%M")
        return payload

    async def _async_recorder_series(
        self, days: int = BACKFILL_DAYS, step_minutes: int = BACKFILL_STEP_MINUTES
    ) -> dict[str, Any] | None:
        """Recorder history on a regular grid, shared by the archive and twin backfills.

        Returns ``{"grid": [...], <series>: [...]}`` with every series filled
        forward onto the grid: ``indoor`` (the panel's weighted temperature
        when the recorder kept enough of it, else the thermostat's),
        ``thermostat_indoor``, ``outdoor``, ``action``, ``aux``,
        ``heat_setpoint``, ``mode``, ``temperature``, ``target_low`` and
        ``target_high``. None when the recorder is unavailable or has no
        thermostat history.
        """
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import get_significant_states
        except ImportError:
            return None

        outdoor_ids = list(getattr(self.sensor_hub, "_outdoor_temp_entities", []) or [])[:1]
        aux_id = getattr(self, "_aux_heat_override_entity_id", None)
        entity_ids = [self.climate_entity_id, *outdoor_ids]
        if aux_id:
            entity_ids.append(aux_id)
        # The panel's indoor temperature is this integration's own weighted
        # sensor, which the recorder has kept; the thermostat's reading is
        # the fallback (an Ecobee reports it in whole degrees).
        weighted_id = None
        try:
            from homeassistant.helpers import entity_registry as er

            from .const import DOMAIN

            if self.entry is not None:
                weighted_id = er.async_get(self.hass).async_get_entity_id(
                    "sensor", DOMAIN, f"{self.entry.entry_id}_weighted_indoor_temp"
                )
        except (ImportError, AttributeError):
            weighted_id = None
        if weighted_id:
            entity_ids.append(weighted_id)

        end = datetime.now(UTC).replace(second=0, microsecond=0)
        start = end - timedelta(days=days)
        try:
            states = await get_instance(self.hass).async_add_executor_job(
                get_significant_states,
                self.hass,
                start,
                end,
                entity_ids,
                None,  # filters
                True,  # include_start_time_state
                False,  # significant_changes_only
                False,  # minimal_response
                False,  # no_attributes
            )
        except Exception:  # noqa: BLE001 - recorder API differs across versions
            _LOGGER.debug("Recorder history query failed", exc_info=True)
            return None

        def _stamp(state: Any) -> datetime | None:
            when = getattr(state, "last_updated", None) or getattr(state, "last_changed", None)
            if when is None:
                return None
            return when if when.tzinfo else when.replace(tzinfo=UTC)

        celsius = str(getattr(self.hass.config.units, "temperature_unit", "")).endswith("C")

        def _number(value: Any, convert: bool = False) -> float | None:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            if number != number:  # NaN
                return None
            return number * 9.0 / 5.0 + 32.0 if convert else number

        climate: list[tuple[datetime, Any]] = []
        for state in states.get(self.climate_entity_id, []):
            when = _stamp(state)
            if when is None or getattr(state, "state", None) in (None, "unavailable", "unknown"):
                continue
            climate.append((when, state))
        if not climate:
            return None

        def _attr(name: str) -> list[tuple[datetime, Any]]:
            return [(when, (state.attributes or {}).get(name)) for when, state in climate]

        def _temps(name: str) -> list[tuple[datetime, float | None]]:
            return [(when, _number(value, celsius)) for when, value in _attr(name)]

        low = _attr("target_temp_low")
        single = [
            (when, (state.attributes or {}).get("temperature") if state.state == "heat" else None)
            for when, state in climate
        ]
        setpoints = [
            (when, _number(value if value is not None else single[i][1], celsius))
            for i, (when, value) in enumerate(low)
        ]
        thermostat_indoor = _temps("current_temperature")
        indoor = thermostat_indoor
        if weighted_id:
            weighted = [
                (when, _number(state.state, celsius))
                for state in states.get(weighted_id, [])
                if (when := _stamp(state)) is not None
            ]
            if sum(1 for _, value in weighted if value is not None) >= 12:
                indoor = weighted
        action = [(when, value) for when, value in _attr("hvac_action")]

        if aux_id:
            aux = [
                (when, state.state == "on")
                for state in states.get(aux_id, [])
                if (when := _stamp(state)) is not None and state.state in ("on", "off")
            ]
        else:
            aux = [(when, "aux" in str(value or "").lower()) for when, value in _attr("equipment_running")]

        outdoor: list[tuple[datetime, float | None]] = []
        if outdoor_ids:
            outdoor = [
                (
                    when,
                    _number(
                        state.state,
                        str((state.attributes or {}).get("unit_of_measurement", "")).endswith("C"),
                    ),
                )
                for state in states.get(outdoor_ids[0], [])
                if (when := _stamp(state)) is not None
            ]

        steps = int((end - start).total_seconds() // (step_minutes * 60))
        grid = [start + timedelta(minutes=step_minutes * i) for i in range(steps)]
        return {
            "grid": grid,
            "indoor": _fill_forward(indoor, grid),
            "thermostat_indoor": _fill_forward(thermostat_indoor, grid),
            "outdoor": _fill_forward(outdoor, grid),
            "action": _fill_forward(action, grid),
            "aux": _fill_forward(aux, grid),
            "heat_setpoint": _fill_forward(setpoints, grid),
            "mode": _fill_forward([(when, state.state) for when, state in climate], grid),
            "temperature": _fill_forward(_temps("temperature"), grid),
            "target_low": _fill_forward(_temps("target_temp_low"), grid),
            "target_high": _fill_forward(_temps("target_temp_high"), grid),
        }

    async def async_backfill_dashboard_archive(self) -> None:
        """Replay recorder history into a new archive so the panel has a past.

        Runs once, when fewer than a day of hours have been logged. It fills
        the hourly log and the morning recoveries; forecast scoring cannot be
        backfilled because past plans were never stored.
        """
        if self.dashboard_archive.hour_count >= BACKFILL_IF_FEWER_HOURS:
            return
        series = await self._async_recorder_series()
        if series is None:
            return
        archive = DashboardArchive()
        fed = replay_history(
            archive,
            series["grid"],
            indoor=series["indoor"],
            outdoor=series["outdoor"],
            action=series["action"],
            aux=series["aux"],
            heat_setpoint=series["heat_setpoint"],
        )
        if archive.hour_count <= self.dashboard_archive.hour_count:
            return
        # Keep anything the live archive has already scored; take the history.
        live = self.dashboard_archive.to_dict()
        merged = archive.to_dict()
        merged["pending"] = live["pending"]
        merged["samples"] = live["samples"]
        merged["last_plan_hour"] = live["last_plan_hour"]
        self.dashboard_archive = DashboardArchive.from_dict(merged)
        _LOGGER.info("Dashboard archive backfilled from %d recorder steps (%d hours)", fed, archive.hour_count)
