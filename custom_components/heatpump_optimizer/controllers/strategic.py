"""Layer 1: Strategic Planner — periodic schedule optimization.

Manages the optimization lifecycle: when to re-optimize, mode detection,
shoulder day handling, and comfort range adjustments.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time

from ..adapters.occupancy import (
    AWAY_COMFORT_DELTA,
    OccupancyAdapter,
    OccupancyMode,
)
from ..engine.comfort import (
    COMFORT_BAND_MIN_WIDTH_F,
    calculate_apparent_temperature,
)
from ..engine.data_types import (
    ForecastPoint,
    OccupancyForecastPoint,
    OptimizationWeights,
    OptimizedSchedule,
)
from ..engine.morning_recovery import (
    RECOVERY_AT_SLEEP_END,
    RECOVERY_TOLERANCE_F,
    RECOVERY_WAIT_FOR_WARMTH,
    HourFacts,
    MorningRecoveryConfig,
    MorningWindow,
    find_morning_windows,
    hold_floor,
    parse_time,
    recovery_floors,
    wait_for_warmth_start,
    warm_by_candidates,
)
from ..engine.optimizer import ScheduleOptimizer
from ..engine.schedule_planner import SATURATION_EXCURSION_DEGREE_MINUTES

_LOGGER = logging.getLogger(__name__)

# Shoulder day detection
SHOULDER_MARGIN_F = 2.0
MODE_SWITCH_HYSTERESIS_F = 5.0

# When both a warm peak and a cold trough sit inside the look-ahead, the next
# few hours decide which side the house needs now.
MODE_TIEBREAK_HOURS = 3

# Lead used for a warm-by recovery whose deadline lies beyond the simulated
# horizon, where there is nothing to size it against yet.
DEFAULT_RECOVERY_LEAD_HOURS = 2


def _hour_key(time: datetime) -> datetime:
    return time.replace(minute=0, second=0, microsecond=0)


@dataclass
class StrategicPlanner:
    """Decides when and how to re-optimize the HVAC schedule."""

    optimizer: ScheduleOptimizer
    resist_balance_point: float  # From performance model (~50.2°F)
    reoptimize_interval_hours: float = 4.0

    sleep_config: dict | None = None
    # Absolute (heat floor, cool ceiling). No band this planner derives, for
    # any occupancy state, may reach past them.
    safety_limits: tuple[float, float] | None = None
    away_comfort_delta: float = AWAY_COMFORT_DELTA
    morning_recovery: MorningRecoveryConfig | None = None

    _last_optimization_time: datetime | None = None
    _last_forecast_snapshot: list[ForecastPoint] = field(default_factory=list)
    _current_schedule: OptimizedSchedule | None = None
    _current_mode: str | None = None
    _last_occupancy_hash: str = ""
    # Per-hour heat and cool bands from the last optimization. The single
    # source of truth for "what band applies at this hour", shared by the
    # planner and by the coordinator's setpoint clamp.
    _hour_bands: dict[datetime, dict] = field(default_factory=dict)
    # Recovery start already decided for a morning, keyed by its deadline.
    # Once a recovery has begun it is not un-begun by a later re-plan.
    _recovery_starts: dict[str, datetime] = field(default_factory=dict)
    _recovery_plan: list[dict] = field(default_factory=list)

    def should_reoptimize(
        self,
        new_forecast: list[ForecastPoint] | None = None,
        forecast_threshold_f: float = 5.0,
        force: bool = False,
        occupancy_timeline: list[OccupancyForecastPoint] | None = None,
    ) -> bool:
        """Determine whether a strategic re-optimization is needed."""
        if force:
            return True

        now = datetime.now(UTC)

        # Never optimized
        if self._last_optimization_time is None:
            return True

        # Time-based
        elapsed_hrs = (now - self._last_optimization_time).total_seconds() / 3600
        if elapsed_hrs >= self.reoptimize_interval_hours:
            _LOGGER.debug("Re-optimize: %.1f hrs since last run", elapsed_hrs)
            return True

        # Forecast deviation
        if new_forecast and self._last_forecast_snapshot:
            if self._forecast_deviated(new_forecast, forecast_threshold_f):
                _LOGGER.debug("Re-optimize: forecast deviated beyond threshold")
                return True

        # Dynamic shoulder day / mode conflict detection
        if new_forecast and self._current_mode:
            new_mode = self.detect_mode(new_forecast)
            if new_mode != self._current_mode and new_mode != "off":
                _LOGGER.debug(
                    "Re-optimize: mode conflict detected (current=%s, forecast suggests=%s)",
                    self._current_mode,
                    new_mode,
                )
                return True

        # Occupancy timeline changed
        if occupancy_timeline is not None:
            new_hash = self._hash_occupancy_timeline(occupancy_timeline)
            if new_hash != self._last_occupancy_hash:
                _LOGGER.debug("Re-optimize: occupancy timeline changed")
                return True

        return False

    def optimize(
        self,
        indoor_temp: float,
        forecast: list[ForecastPoint],
        comfort_cool: tuple[float, float],
        comfort_heat: tuple[float, float],
        humidity: float | None = None,
        humidity_correction: bool = False,
        occupancy_timeline: list[OccupancyForecastPoint] | None = None,
        people_home_count: int | None = None,
        indoor_humidity: float | None = None,
        appliance_btu: float = 0.0,
        aux_threshold_f: float | None = None,
        aux_heat_active: bool = False,
        weights: OptimizationWeights | None = None,
        current_occupancy: OccupancyMode | None = None,
    ) -> OptimizedSchedule | None:
        """Run the optimizer and update internal state.

        Handles mode detection, shoulder days, humidity correction, the
        per-hour comfort bands (occupancy, sleep window, morning recovery,
        safety limits) and stamping them onto the forecast.

        ``comfort_cool`` and ``comfort_heat`` are the HOME bands. Occupancy
        is applied here, once, per hour: from ``occupancy_timeline`` for
        future hours and from ``current_occupancy`` for the hour in progress
        (and for the whole horizon when there is no timeline). Callers used
        to pre-adjust the bands for the current occupancy as well, which
        applied the away delta twice and pushed plans below the safety floor.

        Returns the new schedule, or None if optimization is not needed/possible.
        """
        if not forecast:
            _LOGGER.warning("No forecast data — cannot optimize")
            return None

        now = datetime.now(UTC)

        # Determine mode
        mode = self.detect_mode(forecast)
        mild = mode == "off"
        if mild:
            # Near the balance point there is nothing to optimize, but there
            # is still a plan: hold the heat floor and the cool ceiling, both
            # of which move through the day (sleep window, away block,
            # morning recovery). Returning no schedule here left the
            # thermostat at whatever was last written (the night floor all
            # day, say) and re-planned on every five-minute cycle because
            # "never optimized" stayed true. The side below only labels the
            # plan; the pair is what gets written.
            heat_gap = indoor_temp - comfort_heat[0]
            cool_gap = comfort_cool[1] - indoor_temp
            mode = "heat" if heat_gap <= cool_gap else "cool"
            _LOGGER.debug("Near balance point: flat floor/ceiling plan (%s side)", mode)

        # Apply humidity correction to the band of the mode in play
        cool_base, heat_base = comfort_cool, comfort_heat
        if humidity is not None and humidity_correction:
            if mode == "cool":
                cool_base = self._apply_humidity_correction(comfort_cool, humidity, mode)
            else:
                heat_base = self._apply_humidity_correction(comfort_heat, humidity, mode)

        bands = self._build_hour_bands(
            forecast,
            cool_base,
            heat_base,
            occupancy_timeline,
            current_occupancy,
            now,
        )

        # Check for shoulder day (needs both heat and cool). A mild day is
        # planned the same way: a floor and a ceiling, nothing else.
        is_shoulder = mild or self._is_shoulder_day(forecast, comfort_cool, comfort_heat)

        # Morning recovery moves the heat floor, so it has to be settled
        # before either planning path builds entries from the bands.
        self._recovery_plan = []
        if mode == "heat" or is_shoulder:
            try:
                self._plan_morning_recovery(
                    bands,
                    forecast,
                    indoor_temp,
                    heat_base,
                    mode,
                    is_shoulder,
                    now,
                )
            except Exception:  # noqa: BLE001 - never let this block a plan
                _LOGGER.warning("Morning recovery planning failed", exc_info=True)

        self._hour_bands = bands
        current_band = self._band_for(now, bands)
        comfort = current_band[mode] if current_band is not None else (cool_base if mode == "cool" else heat_base)

        if is_shoulder:
            shoulder = self._build_shoulder_schedule(
                indoor_temp,
                forecast,
                cool_base,
                heat_base,
                mode,
                bands=bands,
            )
            if shoulder is not None:
                for pt in forecast:
                    band = bands.get(_hour_key(pt.time))
                    if band is not None:
                        pt.comfort_min = band["heat"][0]
                        pt.comfort_max = band["cool"][1]
                        pt.heat_target = band.get("heat_target")
                self._current_schedule = shoulder
                self._current_mode = mode
                self._last_forecast_snapshot = list(forecast)
                self._last_optimization_time = datetime.now(UTC)
                if occupancy_timeline is not None:
                    self._last_occupancy_hash = self._hash_occupancy_timeline(occupancy_timeline)
                return shoulder
            _LOGGER.info(
                "Shoulder day, but the dual-setpoint plan leaves the comfort "
                "band -- falling through to gated single-mode planning",
            )

        # Stamp each hour's band for the mode onto the forecast
        for pt in forecast:
            band = bands.get(_hour_key(pt.time))
            if band is not None:
                pt.comfort_min, pt.comfort_max = band[mode]
                pt.heat_target = band.get("heat_target") if mode == "heat" else None

        # Run optimizer
        try:
            schedule = self.optimizer.optimize_setpoints(indoor_temp, forecast, comfort, mode, weights=weights)
        except Exception:
            _LOGGER.error("Optimization failed", exc_info=True)
            return None

        # Store state
        self._current_schedule = schedule
        self._current_mode = mode
        self._last_forecast_snapshot = list(forecast)
        self._last_optimization_time = datetime.now(UTC)
        if occupancy_timeline is not None:
            self._last_occupancy_hash = self._hash_occupancy_timeline(occupancy_timeline)

        _LOGGER.info(
            "Optimization complete [%s]: baseline=%.1f min, optimized=%.1f min, savings=%.1f%%, violations=%d",
            mode,
            schedule.baseline_runtime_minutes,
            schedule.optimized_runtime_minutes,
            schedule.savings_pct,
            schedule.comfort_violations,
        )

        return schedule

    def _build_shoulder_schedule(
        self,
        indoor_temp: float,
        forecast: list[ForecastPoint],
        comfort_cool: tuple[float, float],
        comfort_heat: tuple[float, float],
        mode: str,
        bands: dict[datetime, dict] | None = None,
    ) -> OptimizedSchedule | None:
        """Dual-setpoint schedule for a shoulder day.

        In central Virginia a shoulder day is most of March-April and most of
        October-November, so whatever happens here happens for months at a
        time. The old handling clamped comfort to midpoint +/- 1.5F and then
        ran a single detected mode, re-detected every cycle. Mode flips forced
        re-optimization, and heat targets and cool targets ended up
        interleaved within one day across a 15F span. Stacked on the other
        narrowing layers it left roughly a 3F slot to plan in.

        The natural representation for a heat_cool thermostat is a pair:
        a heat floor it must not fall below and a cool ceiling it must not
        rise above, both flat. The thermostat arbitrates between them at
        hardware speed with no help from us, which is strictly better than
        re-deciding a single mode every cycle from an 8-point forecast.

        Returns None when the dual-setpoint plan cannot hold the band, in
        which case the caller falls back to gated single-mode planning.
        """
        heat_floor = comfort_heat[0]
        cool_ceiling = comfort_cool[1]

        if cool_ceiling - heat_floor < COMFORT_BAND_MIN_WIDTH_F:
            midpoint = (heat_floor + cool_ceiling) / 2
            heat_floor = midpoint - COMFORT_BAND_MIN_WIDTH_F / 2
            cool_ceiling = midpoint + COMFORT_BAND_MIN_WIDTH_F / 2

        planner = self.optimizer.planner
        hour_keys = sorted({pt.time.replace(minute=0, second=0, microsecond=0) for pt in forecast})
        if not hour_keys:
            return None

        # Each hour carries its own pair when the bands were derived (sleep
        # window, away block, morning recovery); the flat pair is the fallback.
        setpoints = self._dual_bounds(bands, setpoints=True) if bands else None
        promised = self._dual_bounds(bands) if bands else None
        if promised:
            first = promised.get(hour_keys[0], promised.get(min(promised)))
            if first is not None:
                heat_floor, cool_ceiling = first

        entries = planner.build_dual_setpoint_schedule(
            hour_keys,
            forecast,
            heat_floor,
            cool_ceiling,
            mode,
            per_hour_bounds=setpoints,
        )
        candidate = planner.evaluate(
            "shoulder dual setpoint",
            entries,
            indoor_temp,
            forecast,
            (heat_floor, cool_ceiling),
            promised,
        )

        if candidate.excursion_degree_minutes > SATURATION_EXCURSION_DEGREE_MINUTES:
            return None

        _LOGGER.info(
            "Shoulder day: dual setpoint heat %.1fF / cool %.1fF, flat (%.1f min runtime, %.2f kWh)",
            heat_floor,
            cool_ceiling,
            candidate.runtime_minutes,
            candidate.energy_kwh,
        )

        return OptimizedSchedule(
            entries=entries,
            baseline_runtime_minutes=candidate.runtime_minutes,
            optimized_runtime_minutes=candidate.runtime_minutes,
            savings_pct=0.0,
            comfort_violations=candidate.violations,
            simulation=candidate.simulation,
            comfort_excursion_degree_minutes=candidate.excursion_degree_minutes,
            plan_label="shoulder dual setpoint",
            dual_setpoint=True,
            baseline_kwh=candidate.energy_kwh,
            optimized_kwh=candidate.energy_kwh,
        )

    def detect_mode(self, forecast: list[ForecastPoint]) -> str:
        """Determine HVAC mode from near-term forecast temperatures.

        Returns "cool", "heat", or "off" (near balance point).

        Uses only the first 8 hours of the forecast for mode determination.
        The optimizer re-runs every 1-4 hours, so it only needs to pick the
        right mode for the immediate future.  Using the full 24-hour peak
        caused "cool" selection on shoulder mornings that need heat because
        a warm afternoon peak exceeded the balance point threshold.
        """
        if not forecast:
            return "off"

        # Near-term focus: commit to a mode for the next few hours only.
        near_term = forecast[:8] if len(forecast) > 8 else forecast
        temps = [pt.outdoor_temp for pt in near_term]
        avg = sum(temps) / len(temps)
        max_temp = max(temps)
        min_temp = min(temps)

        # Sanity-check balance point: if it drifted outside a reasonable
        # residential range (20-90°F), fall back to a safe default.
        bp = self.resist_balance_point
        if bp < 20 or bp > 90:
            _LOGGER.warning(
                "Balance point %.1f°F out of range, using default 50°F",
                bp,
            )
            bp = 50.0

        # Well above balance point -> cooling
        if avg > bp + MODE_SWITCH_HYSTERESIS_F:
            return "cool"

        # Well below balance point -> heating
        if avg < bp - MODE_SWITCH_HYSTERESIS_F:
            return "heat"

        # Near balance point -- use peak/trough to decide
        warm_peak = max_temp > bp + 10
        cold_trough = min_temp < bp - 10
        if warm_peak and cold_trough:
            # Both sides are in the look-ahead. Testing the warm peak first
            # returned "cool" at 31 F on a frosty morning that happened to
            # have a mild afternoon behind it. The next few hours decide what
            # the house needs now; the other side gets its turn when it is
            # the near term, and a heat_cool thermostat holds both anyway.
            soon = temps[:MODE_TIEBREAK_HOURS]
            return "cool" if sum(soon) / len(soon) > bp else "heat"
        if warm_peak:
            return "cool"
        if cold_trough:
            return "heat"

        # Truly mild -- HVAC probably not needed
        return "off"

    def _is_shoulder_day(
        self,
        forecast: list[ForecastPoint],
        comfort_cool: tuple[float, float],
        comfort_heat: tuple[float, float],
    ) -> bool:
        """Detect a shoulder day that might need both heating and cooling.

        Also triggers on days where outdoor temps cross the balance point
        significantly (cold morning + warm afternoon), even if the
        afternoon isn't hot enough to need active cooling.  These days
        need conservative scheduling to avoid pre-heating during warm hours.
        """
        temps = [pt.outdoor_temp for pt in forecast]
        if not temps:
            return False

        max_temp = max(temps)
        min_temp = min(temps)

        needs_cool = max_temp > comfort_cool[1] + SHOULDER_MARGIN_F
        needs_heat = min_temp < comfort_heat[0] - SHOULDER_MARGIN_F

        if needs_cool and needs_heat:
            return True

        # Balance-point crossing: cold morning + warm afternoon where
        # passive drift reverses direction during the day.
        bp = self.resist_balance_point
        crosses_balance = min_temp < bp - 5 and max_temp > bp + 5
        return crosses_balance

    @staticmethod
    def _apply_humidity_correction(
        comfort: tuple[float, float],
        humidity: float,
        mode: str = "cool",
    ) -> tuple[float, float]:
        """Adjust comfort range based on humidity's effect on perceived temperature.

        Cooling: high humidity makes it feel warmer, so lower the comfort max
        by the heat index overshoot at the comfort ceiling.
        Heating: low humidity makes it feel cooler, so raise the comfort min
        slightly to compensate for dry air.
        """
        if mode == "cool":
            apparent_at_max = calculate_apparent_temperature(comfort[1], humidity)
            delta = apparent_at_max - comfort[1]
            if delta <= 0:
                return comfort
            new_max = comfort[1] - delta
            # Don't let max go below min + 2°F
            new_max = max(new_max, comfort[0] + 2.0)
            return (comfort[0], new_max)

        if mode == "heat" and humidity < 30:
            # Dry air makes it feel cooler — lift comfort range slightly
            lift = (30.0 - humidity) * 0.05  # max ~1.5°F at 0% RH
            return (comfort[0] + lift, comfort[1] + lift)

        return comfort

    def _forecast_deviated(
        self,
        new_forecast: list[ForecastPoint],
        threshold_f: float,
    ) -> bool:
        """Check if new forecast deviates significantly from the snapshot."""
        now = datetime.now(UTC)
        lookahead = now + timedelta(hours=6)

        old_by_hour: dict[int, float] = {}
        for pt in self._last_forecast_snapshot:
            if now <= pt.time <= lookahead:
                key = int(pt.time.timestamp()) // 3600
                old_by_hour[key] = pt.outdoor_temp

        for pt in new_forecast:
            if pt.time > lookahead:
                continue
            key = int(pt.time.timestamp()) // 3600
            old_temp = old_by_hour.get(key)
            if old_temp is not None and abs(pt.outdoor_temp - old_temp) > threshold_f:
                return True

        return False

    def _stamp_per_hour_comfort(
        self,
        forecast: list[ForecastPoint],
        base_comfort: tuple[float, float],
        mode: str,
        timeline: list[OccupancyForecastPoint],
    ) -> None:
        """Set comfort_min/comfort_max on each ForecastPoint from the timeline."""
        for pt in forecast:
            occ_mode = self._lookup_occupancy_at(pt.time, timeline)
            adjusted = OccupancyAdapter.adjust_comfort_for_mode(
                base_comfort,
                mode,
                occ_mode,
                away_delta=self.away_comfort_delta,
                safety_limits=self.safety_limits,
            )
            # Apply sleep bounds when HOME and within sleep window
            if (
                self.sleep_config
                and self.sleep_config.get("enabled")
                and occ_mode == OccupancyMode.HOME
                and self._is_in_sleep_window(pt.time, self.sleep_config)
            ):
                sleep_comfort = self.sleep_config.get("comfort_cool" if mode == "cool" else "comfort_heat")
                if sleep_comfort:
                    adjusted = sleep_comfort
            pt.comfort_min = adjusted[0]
            pt.comfort_max = adjusted[1]

    # ── Per-hour comfort bands ─────────────────────────────────────

    def _clamp_to_safety(self, band: tuple[float, float]) -> tuple[float, float]:
        if self.safety_limits is None:
            return (float(band[0]), float(band[1]))
        lo = max(float(band[0]), self.safety_limits[0])
        hi = min(float(band[1]), self.safety_limits[1])
        return (lo, max(lo, hi))

    def _build_hour_bands(
        self,
        forecast: list[ForecastPoint],
        comfort_cool: tuple[float, float],
        comfort_heat: tuple[float, float],
        timeline: list[OccupancyForecastPoint] | None,
        current_occupancy: OccupancyMode | None,
        now: datetime,
    ) -> dict[datetime, dict]:
        """Heat and cool band for every forecast hour.

        Occupancy first (away/vacation widen, never past the safety limits),
        then the sleep window for occupied hours. The morning recovery pass
        runs afterwards and only ever moves the heat floor.
        """
        sleep = self.sleep_config or {}
        sleep_on = bool(sleep.get("enabled"))
        now_hour = _hour_key(now)
        bands: dict[datetime, dict] = {}

        # Where the timeline disagrees with reality about *now* (the usual
        # case being a calendar whose default is "home" while the house is
        # empty), it is not trusted about the following hours either, until
        # it next changes its mind. Otherwise each new hour starts on the
        # timeline's band and flips to the real one at the next re-plan:
        # two setpoint writes an hour for as long as nobody is home.
        distrusted: OccupancyMode | None = None
        if current_occupancy is not None and timeline:
            claimed_now = self._lookup_occupancy_at(now, timeline)
            if claimed_now != current_occupancy:
                distrusted = claimed_now

        for hour in sorted({_hour_key(pt.time) for pt in forecast}):
            if current_occupancy is not None and (hour <= now_hour or not timeline):
                occ = current_occupancy
            elif timeline:
                occ = self._lookup_occupancy_at(hour, timeline)
                if distrusted is not None:
                    if occ == distrusted:
                        occ = current_occupancy
                    else:
                        distrusted = None  # the timeline moved on; trust it again
            else:
                occ = OccupancyMode.HOME

            heat = OccupancyAdapter.adjust_comfort_for_mode(
                comfort_heat,
                "heat",
                occ,
                away_delta=self.away_comfort_delta,
                safety_limits=self.safety_limits,
            )
            cool = OccupancyAdapter.adjust_comfort_for_mode(
                comfort_cool,
                "cool",
                occ,
                away_delta=self.away_comfort_delta,
                safety_limits=self.safety_limits,
            )

            home = occ == OccupancyMode.HOME
            in_sleep = sleep_on and self._is_in_sleep_window(hour, sleep)
            if home and in_sleep:
                if sleep.get("comfort_heat"):
                    heat = self._clamp_to_safety(sleep["comfort_heat"])
                if sleep.get("comfort_cool"):
                    cool = self._clamp_to_safety(sleep["comfort_cool"])

            bands[hour] = {
                "heat": (float(heat[0]), float(heat[1])),
                "cool": (float(cool[0]), float(cool[1])),
                "home": home,
                "sleep": in_sleep,
            }
        return bands

    @staticmethod
    def _band_for(time: datetime, bands: dict[datetime, dict]) -> dict | None:
        """The band entry in force at ``time`` (nearest earlier hour)."""
        if not bands:
            return None
        hour = _hour_key(time)
        if hour in bands:
            return bands[hour]
        earlier = [h for h in bands if h <= hour]
        return bands[max(earlier)] if earlier else bands[min(bands)]

    def heat_target_at(self, time: datetime) -> float | None:
        """The planned heat setpoint at ``time``: the recovery target if a
        recovery is under way, otherwise the heat floor. None if unplanned."""
        band = self._band_for(time, self._hour_bands)
        if band is None:
            return None
        target = band.get("heat_target")
        return target if target is not None else band["heat"][0]

    def band_at(self, time: datetime, mode: str) -> tuple[float, float] | None:
        """The planned ``(min, max)`` for ``mode`` at ``time``, if planned."""
        band = self._band_for(time, self._hour_bands)
        if band is None or mode not in ("heat", "cool"):
            return None
        return band[mode]

    @staticmethod
    def _dual_bounds(
        bands: dict[datetime, dict],
        setpoints: bool = False,
    ) -> dict[datetime, tuple[float, float]]:
        """Per-hour (heat floor, cool ceiling), kept at least plannable apart.

        By default the promised bounds, which plans are scored against. With
        ``setpoints`` the heat side is the setpoint instead, which is higher
        than the promise while a morning recovery is under way.
        """
        out: dict[datetime, tuple[float, float]] = {}
        for hour, band in bands.items():
            floor, ceiling = band["heat"][0], band["cool"][1]
            if setpoints and band.get("heat_target") is not None:
                floor = max(floor, band["heat_target"])
            if ceiling - floor < COMFORT_BAND_MIN_WIDTH_F:
                mid = (floor + ceiling) / 2
                floor = mid - COMFORT_BAND_MIN_WIDTH_F / 2
                ceiling = mid + COMFORT_BAND_MIN_WIDTH_F / 2
            out[hour] = (floor, ceiling)
        return out

    @staticmethod
    def _to_local(time: datetime) -> datetime:
        try:
            from homeassistant.util import dt as dt_util

            return dt_util.as_local(time)
        except ImportError:
            return time.astimezone()

    # ── Morning recovery ───────────────────────────────────────────

    def _plan_morning_recovery(
        self,
        bands: dict[datetime, dict],
        forecast: list[ForecastPoint],
        indoor_temp: float,
        heat_base: tuple[float, float],
        mode: str,
        dual: bool,
        now: datetime,
    ) -> None:
        """Set the heat floor between the end of sleep and the morning target.

        See engine/morning_recovery.py for the two occupant preferences. This
        method owns the parts that need planner state: remembering a recovery
        that has already begun, and sizing a warm-by start by simulation.
        """
        config = self.morning_recovery
        sleep = self.sleep_config or {}
        if (
            config is None
            or config.mode == RECOVERY_AT_SLEEP_END
            or not sleep.get("enabled")
            or not sleep.get("comfort_heat")
            or not bands
        ):
            return

        night_floor = self._clamp_to_safety(sleep["comfort_heat"])[0]
        day_floor = self._clamp_to_safety(heat_base)[0]
        sleep_end = parse_time(sleep.get("end"), dt_time(7, 0))
        planner = self.optimizer.planner

        facts = [
            HourFacts(
                hour=hour,
                local=self._to_local(hour),
                in_sleep=band["sleep"],
                home=band["home"],
                day_floor=day_floor,
                outdoor_temp=planner._hour_outdoor_temp(hour, forecast),
            )
            for hour, band in sorted(bands.items())
        ]
        windows = find_morning_windows(facts, night_floor, sleep_end, config)
        now_hour = _hour_key(now)

        for window in windows:
            if window.deadline <= now:
                continue
            remembered = self._recovery_starts.get(window.key)
            if remembered is not None and remembered <= now_hour:
                # Already under way. A later, more optimistic simulation does
                # not get to drop the setpoint back and start over.
                start = remembered
            elif config.mode == RECOVERY_WAIT_FOR_WARMTH:
                start = wait_for_warmth_start(window, config)
            else:
                start = self._search_warm_by_start(
                    window,
                    config,
                    bands,
                    forecast,
                    indoor_temp,
                    mode,
                    dual,
                    now_hour,
                )
            self._recovery_starts[window.key] = start
            self._apply_recovery(bands, window, config, start)
            self._recovery_plan.append(
                {
                    "mode": config.mode,
                    "target_time": window.deadline.isoformat(),
                    "recovery_start": start.isoformat(),
                    "night_floor": window.night_floor,
                    "day_floor": window.day_floor,
                }
            )
            _LOGGER.debug(
                "Morning recovery [%s]: target %s, start %s (%.1f -> %.1f F)",
                config.mode,
                window.deadline.isoformat(),
                start.isoformat(),
                window.night_floor,
                window.day_floor,
            )

        # Forget mornings that are over
        cutoff = (now - timedelta(hours=12)).isoformat()
        self._recovery_starts = {key: value for key, value in self._recovery_starts.items() if key >= cutoff}

    @staticmethod
    def _apply_recovery(
        bands: dict[datetime, dict],
        window: MorningWindow,
        config: MorningRecoveryConfig,
        start: datetime,
    ) -> None:
        """Write one morning's recovery into the hour bands.

        Two different things change. The *promise* (``band["heat"]``, which
        plans are scored against and writes are clamped within) drops to the
        held floor for the hours between the end of sleep and the target
        time: the occupant has said they do not need the daytime floor until
        then. The *setpoint* (``band["heat_target"]``) is the daytime floor
        from the recovery start, so the thermostat is already on its way.
        """
        held = hold_floor(window, config)
        setpoints = recovery_floors(window, config, start)
        for hour in window.hold_hours:
            band = bands.get(hour)
            if band is None:
                continue
            target = setpoints.get(hour, held)
            band["heat"] = (held, max(band["heat"][1], target))
            band["heat_target"] = target if target > held + 1e-6 else None
        for hour in window.early_hours:
            band = bands.get(hour)
            if band is None or hour not in setpoints:
                continue
            target = setpoints[hour]
            band["heat"] = (band["heat"][0], max(band["heat"][1], target))
            band["heat_target"] = target if target > band["heat"][0] + 1e-6 else None

    def _recovery_entries(
        self,
        bands: dict[datetime, dict],
        forecast: list[ForecastPoint],
        mode: str,
        dual: bool,
    ):
        planner = self.optimizer.planner
        hour_keys = sorted(bands)
        if dual:
            per_hour = self._dual_bounds(bands, setpoints=True)
            floor, ceiling = per_hour[hour_keys[0]]
            return planner.build_dual_setpoint_schedule(
                hour_keys,
                forecast,
                floor,
                ceiling,
                mode,
                per_hour_bounds=per_hour,
            )
        per_hour = {hour: band["heat"] for hour, band in bands.items()}
        targets = {hour: band["heat_target"] for hour, band in bands.items() if band.get("heat_target") is not None}
        return planner.build_flat_schedule(
            hour_keys,
            forecast,
            per_hour[hour_keys[0]],
            "heat",
            per_hour,
            targets,
        )

    def _search_warm_by_start(
        self,
        window: MorningWindow,
        config: MorningRecoveryConfig,
        bands: dict[datetime, dict],
        forecast: list[ForecastPoint],
        indoor_temp: float,
        mode: str,
        dual: bool,
        now_hour: datetime,
    ) -> datetime:
        """Latest recovery start that is simulated to be warm by the target.

        Just in time, because every earlier hour of recovery is an hour spent
        holding a warmer house through the coldest part of the day.
        """
        candidates = warm_by_candidates(window, config, now_hour)
        if not candidates:
            return now_hour

        simulator = self.optimizer.simulator
        earliest = candidates[-1]

        for start in candidates:  # latest first
            trial = {hour: dict(band) for hour, band in bands.items()}
            self._apply_recovery(trial, window, config, start)
            entries = self._recovery_entries(trial, forecast, mode, dual)
            simulation = simulator.simulate(indoor_temp, forecast, entries)
            if self._simulated_temp_at(simulation, window.deadline) is None:
                # Target beyond the simulated horizon: nothing to size
                # against yet, so use a plain lead and refine on a later run.
                fallback = window.deadline - timedelta(hours=DEFAULT_RECOVERY_LEAD_HOURS)
                return min(candidates, key=lambda h: abs((h - fallback).total_seconds()))
            # Arrival is the warmest the house gets between the start and the
            # target. Reading the temperature at the target instant instead
            # would need the thermostat differential as slack (a warm house
            # sags that far between cycles), and that slack let a recovery
            # that was still a degree short count as on time.
            arrived = self._simulated_peak_between(simulation, start, window.deadline)
            if arrived is not None and arrived >= window.day_floor - RECOVERY_TOLERANCE_F:
                return start

        # Nothing arrives on time: start as early as allowed and say so.
        _LOGGER.info(
            "Morning recovery cannot reach %.1fF by %s; starting at %s",
            window.day_floor,
            window.deadline.isoformat(),
            earliest.isoformat(),
        )
        return earliest

    @staticmethod
    def _simulated_temp_at(simulation, when: datetime) -> float | None:
        """Simulated indoor temperature at ``when``, or None if not covered."""
        if not simulation or when > simulation[-1].time + timedelta(minutes=10):
            return None
        best = None
        for point in simulation:
            if point.time <= when:
                best = point
            else:
                break
        return best.indoor_temp if best is not None else simulation[0].indoor_temp

    @staticmethod
    def _simulated_peak_between(simulation, start: datetime, end: datetime) -> float | None:
        """Warmest simulated indoor temperature in ``[start, end]``."""
        temps = [point.indoor_temp for point in simulation if start <= point.time <= end]
        return max(temps) if temps else None

    def recovery_state(self) -> dict[str, str]:
        """Recoveries already decided, for persistence across restarts."""
        return {key: value.isoformat() for key, value in self._recovery_starts.items()}

    def restore_recovery_state(self, data: dict | None) -> None:
        restored: dict[str, datetime] = {}
        for key, value in (data or {}).items():
            try:
                restored[str(key)] = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                continue
        self._recovery_starts = restored

    @property
    def recovery_plan(self) -> list[dict]:
        """What the last optimization decided about upcoming mornings."""
        return list(self._recovery_plan)

    @staticmethod
    def _is_in_sleep_window(time: datetime, sleep_config: dict) -> bool:
        """Check if a time falls within the sleep window (handles overnight)."""
        start_str = sleep_config.get("start", "22:00")
        end_str = sleep_config.get("end", "07:00")
        try:
            start_parts = start_str.split(":")
            end_parts = end_str.split(":")
            start_h, start_m = int(start_parts[0]), int(start_parts[1])
            end_h, end_m = int(end_parts[0]), int(end_parts[1])
        except (ValueError, IndexError):
            _LOGGER.warning("Invalid sleep window format: %s-%s", start_str, end_str)
            return False

        try:
            from homeassistant.util import dt as dt_util

            local_time = dt_util.as_local(time).time()
        except ImportError:
            local_time = time.astimezone().time()

        start = dt_time(start_h, start_m)
        end = dt_time(end_h, end_m)

        if start <= end:  # same-day window (e.g., 01:00-06:00)
            return start <= local_time < end
        else:  # overnight window (e.g., 22:00-07:00)
            return local_time >= start or local_time < end

    @staticmethod
    def _lookup_occupancy_at(
        time: datetime,
        timeline: list[OccupancyForecastPoint],
    ) -> OccupancyMode:
        """Find the occupancy mode at a given time from the timeline."""
        for seg in timeline:
            if seg.start_time <= time < seg.end_time:
                mode_str = seg.mode.lower()
                if mode_str == "away":
                    return OccupancyMode.AWAY
                if mode_str == "vacation":
                    return OccupancyMode.VACATION
                return OccupancyMode.HOME
        return OccupancyMode.HOME  # default when time not covered

    @staticmethod
    def _hash_occupancy_timeline(
        timeline: list[OccupancyForecastPoint],
    ) -> str:
        """Produce a simple hash to detect timeline changes."""
        parts = []
        for seg in timeline:
            parts.append(f"{seg.start_time.isoformat()}:{seg.end_time.isoformat()}:{seg.mode}")
        return "|".join(parts)

    @property
    def schedule(self) -> OptimizedSchedule | None:
        return self._current_schedule

    @property
    def mode(self) -> str | None:
        """Active plan mode, or None when there is no conditioned plan."""
        return None if self._current_mode == "off" else self._current_mode

    @property
    def last_optimization_time(self) -> datetime | None:
        return self._last_optimization_time

    @property
    def forecast_snapshot(self) -> list[ForecastPoint]:
        return list(self._last_forecast_snapshot)
