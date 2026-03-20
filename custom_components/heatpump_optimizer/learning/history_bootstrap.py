"""History bootstrap — seed learning subsystems from HA recorder history.

Home Assistant's recorder keeps ~10 days of state history by default. Rather
than cold-starting the EKF with synthetic defaults (2-3 week convergence) or
requiring an external Beestat profile, this module batch-replays historical
thermostat and sensor data through the EKF to achieve meaningful model
convergence immediately at first startup.

It also populates the BaselineCapture (setpoint schedule) and
PerformanceProfiler (measured deltas by condition) from the same history,
enabling counterfactual savings tracking and measured-reality model fallback
from day one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import TemperatureConverter

from .baseline_capture import BaselineCapture
from .performance_profiler import PerformanceProfiler
from .thermal_estimator import ThermalEstimator

_LOGGER = logging.getLogger(__name__)

# Grid resolution — matches the coordinator's 5-minute update cycle
GRID_INTERVAL_MINUTES = 5
DT_HOURS = GRID_INTERVAL_MINUTES / 60.0

# Maximum dt before we treat it as a gap (avoid Jacobian instability)
MAX_DT_HOURS = 1.0

# Minimum data thresholds
MIN_VALID_POINTS = 12  # ~1 hour of data
MAX_TEMP_GAP_MINUTES = 15  # indoor temp interpolation limit
MAX_OUTDOOR_GAP_MINUTES = 30  # outdoor temp interpolation limit


@dataclass
class HistoryDataPoint:
    """One aligned 5-minute observation on the resampled grid."""

    timestamp: datetime
    indoor_temp: float | None  # °F
    outdoor_temp: float | None  # °F
    hvac_mode: str  # "cool", "heat", "off"
    hvac_running: bool
    hvac_action: str | None  # "cooling", "heating", "idle"
    setpoint: float | None  # °F
    humidity: float | None
    wind_speed_mph: float | None

    @property
    def valid(self) -> bool:
        """Whether this point has enough data for an EKF update."""
        return self.indoor_temp is not None and self.outdoor_temp is not None


@dataclass
class BootstrapResult:
    """Outcome of the history bootstrap attempt."""

    success: bool
    reason: str = ""
    ekf_observations: int = 0
    skipped_observations: int = 0
    baseline_observations: int = 0
    profiler_observations: int = 0
    final_confidence: float = 0.0


async def async_bootstrap_from_history(
    hass: HomeAssistant,
    climate_entity_id: str,
    outdoor_temp_entities: list[str],
    weather_entity_ids: list[str],
    wind_speed_entity: str | None,
    humidity_entities: list[str],
    estimator: ThermalEstimator,
    baseline_capture: BaselineCapture,
    profiler: PerformanceProfiler,
    max_days: int = 10,
) -> BootstrapResult:
    """Bootstrap learning subsystems from Home Assistant recorder history.

    This is the main async entry point called from the coordinator at startup
    when no persisted learning state exists.
    """
    # 1. Wait for recorder
    try:
        from homeassistant.components.recorder import get_instance

        recorder = get_instance(hass)
        await asyncio.wait_for(
            recorder.async_db_ready.wait()
            if hasattr(recorder.async_db_ready, "wait")
            else recorder.async_db_ready,
            timeout=30.0,
        )
    except ImportError:
        return BootstrapResult(success=False, reason="recorder_not_available")
    except asyncio.TimeoutError:
        return BootstrapResult(success=False, reason="recorder_not_ready")
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Recorder check failed: %s", err)
        return BootstrapResult(success=False, reason=f"recorder_error: {err}")

    # 2. Fetch historical states
    entity_ids = [climate_entity_id]
    entity_ids.extend(outdoor_temp_entities)
    entity_ids.extend(weather_entity_ids)
    entity_ids.extend(humidity_entities)
    if wind_speed_entity:
        entity_ids.append(wind_speed_entity)

    # De-duplicate
    entity_ids = list(dict.fromkeys(entity_ids))

    start_time = datetime.now(timezone.utc) - timedelta(days=max_days)
    end_time = datetime.now(timezone.utc)

    try:
        from homeassistant.components.recorder.history import get_significant_states

        states = await get_instance(hass).async_add_executor_job(
            get_significant_states,
            hass,
            start_time,
            end_time,
            entity_ids,
            None,  # filters
            True,  # include_start_time_state
            False,  # significant_changes_only
            True,  # minimal_response — we'll access attributes directly
            False,  # no_attributes — must be False to get current_temperature etc.
        )
    except TypeError:
        # Fallback for older HA versions with different signature
        try:
            states = await get_instance(hass).async_add_executor_job(
                get_significant_states,
                hass,
                start_time,
                end_time,
                entity_ids,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("History fetch failed: %s", err)
            return BootstrapResult(success=False, reason=f"history_fetch_error: {err}")
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("History fetch failed: %s", err)
        return BootstrapResult(success=False, reason=f"history_fetch_error: {err}")

    if not states or climate_entity_id not in states:
        return BootstrapResult(success=False, reason="no_climate_history")

    climate_states = states.get(climate_entity_id, [])
    if len(climate_states) < MIN_VALID_POINTS:
        return BootstrapResult(success=False, reason="insufficient_history")

    # 3. Build timelines and align to grid
    data_points = _build_aligned_timeline(
        climate_states=climate_states,
        outdoor_states=_collect_outdoor_states(
            states, outdoor_temp_entities, weather_entity_ids
        ),
        humidity_states=_collect_sensor_states(states, humidity_entities),
        wind_states=_collect_sensor_states(
            states, [wind_speed_entity] if wind_speed_entity else []
        ),
        start_time=start_time,
        end_time=end_time,
    )

    valid_points = [p for p in data_points if p.valid]
    if len(valid_points) < MIN_VALID_POINTS:
        return BootstrapResult(
            success=False,
            reason=f"insufficient_valid_points ({len(valid_points)})",
        )

    # 4. Batch-feed EKF
    ekf_result = _batch_feed_estimator(estimator, data_points)

    # 5. Bootstrap baseline capture
    baseline_count = _bootstrap_baseline(baseline_capture, data_points)

    # 6. Bootstrap performance profiler
    profiler_count = _bootstrap_profiler(profiler, data_points)

    # 7. Clear meaningless innovations (they have wall-clock timestamps)
    estimator._innovations.clear()

    return BootstrapResult(
        success=True,
        reason="ok",
        ekf_observations=ekf_result[0],
        skipped_observations=ekf_result[1],
        baseline_observations=baseline_count,
        profiler_observations=profiler_count,
        final_confidence=estimator.confidence,
    )


# ── Timeline building ───────────────────────────────────────────────────


def _build_aligned_timeline(
    climate_states: list,
    outdoor_states: list[tuple[datetime, float]],
    humidity_states: list[tuple[datetime, float]],
    wind_states: list[tuple[datetime, float]],
    start_time: datetime,
    end_time: datetime,
) -> list[HistoryDataPoint]:
    """Build a 5-minute aligned timeline from raw recorder states."""

    # Parse climate entity into per-attribute timelines
    indoor_timeline: list[tuple[datetime, float]] = []
    mode_timeline: list[tuple[datetime, str]] = []
    action_timeline: list[tuple[datetime, str]] = []
    setpoint_timeline: list[tuple[datetime, float]] = []

    for state in climate_states:
        ts = _state_timestamp(state)
        if ts is None:
            continue

        attrs = _state_attributes(state)
        state_value = _state_value(state)

        # Indoor temperature
        current_temp = attrs.get("current_temperature")
        if current_temp is not None:
            try:
                temp_f = _to_fahrenheit(float(current_temp), attrs)
                indoor_timeline.append((ts, temp_f))
            except (ValueError, TypeError):
                pass

        # HVAC mode from entity state
        if state_value in ("cool", "heat", "heat_cool", "off", "auto", "dry", "fan_only"):
            mode = state_value
            if mode in ("heat_cool", "auto"):
                # Map dual-mode to the active action if available
                action = attrs.get("hvac_action", "idle")
                if action == "cooling":
                    mode = "cool"
                elif action == "heating":
                    mode = "heat"
                else:
                    mode = "off"
            elif mode in ("dry", "fan_only"):
                mode = "off"
            mode_timeline.append((ts, mode))

        # HVAC action
        hvac_action = attrs.get("hvac_action")
        if hvac_action:
            action_timeline.append((ts, str(hvac_action)))

        # Setpoint (single or dual-setpoint mode)
        setpoint = attrs.get("temperature")
        if setpoint is None or setpoint == "":
            # Dual-setpoint: derive from high/low based on hvac_action
            high = attrs.get("target_temp_high")
            low = attrs.get("target_temp_low")
            if hvac_action == "heating" and low is not None:
                setpoint = low
            elif hvac_action == "cooling" and high is not None:
                setpoint = high
            elif low is not None:
                setpoint = low  # default to heating bound when idle
        if setpoint is not None and setpoint != "":
            try:
                sp_f = _to_fahrenheit(float(setpoint), attrs)
                setpoint_timeline.append((ts, sp_f))
            except (ValueError, TypeError):
                pass

    # Generate 5-minute grid
    grid_times = []
    t = start_time
    while t <= end_time:
        grid_times.append(t)
        t += timedelta(minutes=GRID_INTERVAL_MINUTES)

    # Build aligned data points
    data_points: list[HistoryDataPoint] = []

    for grid_time in grid_times:
        indoor_temp = _interpolate_numeric(
            indoor_timeline, grid_time, MAX_TEMP_GAP_MINUTES
        )
        outdoor_temp = _interpolate_numeric(
            outdoor_states, grid_time, MAX_OUTDOOR_GAP_MINUTES
        )
        hvac_mode = _forward_fill_str(mode_timeline, grid_time) or "off"
        hvac_action = _forward_fill_str(action_timeline, grid_time)
        setpoint = _forward_fill_numeric(setpoint_timeline, grid_time)
        humidity = _forward_fill_numeric(humidity_states, grid_time)
        wind = _forward_fill_numeric(wind_states, grid_time)

        # Derive hvac_running from action
        hvac_running = hvac_action in ("cooling", "heating")

        data_points.append(
            HistoryDataPoint(
                timestamp=grid_time,
                indoor_temp=indoor_temp,
                outdoor_temp=outdoor_temp,
                hvac_mode=hvac_mode,
                hvac_running=hvac_running,
                hvac_action=hvac_action,
                setpoint=setpoint,
                humidity=humidity,
                wind_speed_mph=wind,
            )
        )

    return data_points


def _collect_outdoor_states(
    states: dict[str, list],
    outdoor_temp_entities: list[str],
    weather_entity_ids: list[str],
) -> list[tuple[datetime, float]]:
    """Collect outdoor temperature readings from sensor and weather entities."""
    timeline: list[tuple[datetime, float]] = []

    # Prefer dedicated outdoor temp sensors
    for entity_id in outdoor_temp_entities:
        for state in states.get(entity_id, []):
            ts = _state_timestamp(state)
            val = _state_value(state)
            if ts is not None and val is not None:
                try:
                    attrs = _state_attributes(state)
                    temp_f = _to_fahrenheit(float(val), attrs)
                    timeline.append((ts, temp_f))
                except (ValueError, TypeError):
                    pass

    # Fall back to weather entity temperature attribute
    if not timeline:
        for entity_id in weather_entity_ids:
            for state in states.get(entity_id, []):
                ts = _state_timestamp(state)
                attrs = _state_attributes(state)
                temp = attrs.get("temperature")
                if ts is not None and temp is not None:
                    try:
                        temp_f = _to_fahrenheit(float(temp), attrs)
                        timeline.append((ts, temp_f))
                    except (ValueError, TypeError):
                        pass

    timeline.sort(key=lambda x: x[0])
    return timeline


def _collect_sensor_states(
    states: dict[str, list],
    entity_ids: list[str],
) -> list[tuple[datetime, float]]:
    """Collect numeric sensor readings from one or more entities."""
    timeline: list[tuple[datetime, float]] = []

    for entity_id in entity_ids:
        for state in states.get(entity_id, []):
            ts = _state_timestamp(state)
            val = _state_value(state)
            if ts is not None and val is not None:
                try:
                    timeline.append((ts, float(val)))
                except (ValueError, TypeError):
                    pass

    timeline.sort(key=lambda x: x[0])
    return timeline


# ── EKF batch feeding ───────────────────────────────────────────────────


def _batch_feed_estimator(
    estimator: ThermalEstimator,
    data_points: list[HistoryDataPoint],
) -> tuple[int, int]:
    """Feed aligned data points through the EKF sequentially.

    Returns (valid_count, skipped_count).
    """
    valid_count = 0
    skipped_count = 0
    last_valid_time: datetime | None = None

    for point in data_points:
        if not point.valid:
            skipped_count += 1
            continue

        # Compute dt from actual time gap
        if last_valid_time is not None:
            dt_seconds = (point.timestamp - last_valid_time).total_seconds()
            dt_hours = dt_seconds / 3600.0
            # Skip data points with large gaps (e.g. HA restart, DB maintenance).
            # Previously this reset dt to DT_HOURS (5 min), which compressed
            # multi-hour gaps into a single step — the EKF would see an
            # impossibly fast temperature change and learn wrong parameters.
            if dt_hours > MAX_DT_HOURS:
                skipped_count += 1
                last_valid_time = point.timestamp
                continue
        else:
            dt_hours = DT_HOURS

        estimator.update(
            observed_temp=point.indoor_temp,
            outdoor_temp=point.outdoor_temp,
            hvac_mode=point.hvac_mode,
            hvac_running=point.hvac_running,
            cloud_cover=None,  # not available in recorder history
            sun_elevation=None,
            dt_hours=dt_hours,
            wind_speed_mph=point.wind_speed_mph,
            humidity=point.humidity,
        )

        last_valid_time = point.timestamp
        valid_count += 1

    return valid_count, skipped_count


# ── Baseline bootstrap ──────────────────────────────────────────────────


def _bootstrap_baseline(
    baseline: BaselineCapture,
    data_points: list[HistoryDataPoint],
) -> int:
    """Feed historical setpoint/mode data into BaselineCapture.

    Returns number of observations recorded.
    """
    count = 0
    for point in data_points:
        if point.setpoint is not None and point.valid:
            baseline.record_observation(
                now=point.timestamp,
                setpoint=point.setpoint,
                mode=point.hvac_mode,
            )
            count += 1

    # Auto-build template if enough data
    if baseline.is_ready and baseline.template is None:
        baseline.build_template()

    return count


# ── Profiler bootstrap ──────────────────────────────────────────────────


def _bootstrap_profiler(
    profiler: PerformanceProfiler,
    data_points: list[HistoryDataPoint],
) -> int:
    """Feed historical data into PerformanceProfiler.

    Returns number of observations recorded.
    """
    count = 0
    for point in data_points:
        if not point.valid:
            # Reset profiler's internal tracking on gaps
            profiler._previous_indoor_temp = None
            profiler._previous_timestamp = None
            continue

        profiler.record_observation(
            indoor_temp=point.indoor_temp,
            outdoor_temp=point.outdoor_temp,
            hvac_action=point.hvac_action,
            hvac_mode=point.hvac_mode,
            aux_heat_active=False,
            solar_irradiance=None,
            now=point.timestamp,
        )
        count += 1

    return count


# ── Interpolation helpers ───────────────────────────────────────────────


def _interpolate_numeric(
    timeline: list[tuple[datetime, float]],
    target: datetime,
    max_gap_minutes: float,
) -> float | None:
    """Linear interpolation between bracketing readings.

    Returns None if no readings exist within max_gap_minutes of the target.
    """
    if not timeline:
        return None

    max_gap = timedelta(minutes=max_gap_minutes)

    # Binary search for insertion point
    lo, hi = 0, len(timeline) - 1

    # Quick bounds check
    if target <= timeline[0][0]:
        if (timeline[0][0] - target) <= max_gap:
            return timeline[0][1]
        return None
    if target >= timeline[-1][0]:
        if (target - timeline[-1][0]) <= max_gap:
            return timeline[-1][1]
        return None

    # Find bracketing pair
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if timeline[mid][0] <= target:
            lo = mid
        else:
            hi = mid

    t_before, v_before = timeline[lo]
    t_after, v_after = timeline[hi]

    # Check gap limits
    if (target - t_before) > max_gap or (t_after - target) > max_gap:
        # Use nearest if within gap
        gap_before = abs((target - t_before).total_seconds())
        gap_after = abs((t_after - target).total_seconds())
        if gap_before <= max_gap.total_seconds():
            return v_before
        if gap_after <= max_gap.total_seconds():
            return v_after
        return None

    # Linear interpolation
    total_seconds = (t_after - t_before).total_seconds()
    if total_seconds == 0:
        return v_before

    frac = (target - t_before).total_seconds() / total_seconds
    return v_before + frac * (v_after - v_before)


def _forward_fill_str(
    timeline: list[tuple[datetime, str]],
    target: datetime,
) -> str | None:
    """Forward-fill: return most recent value at or before target."""
    if not timeline:
        return None

    result = None
    for ts, val in timeline:
        if ts > target:
            break
        result = val
    return result


def _forward_fill_numeric(
    timeline: list[tuple[datetime, float]],
    target: datetime,
) -> float | None:
    """Forward-fill: return most recent numeric value at or before target."""
    if not timeline:
        return None

    result = None
    for ts, val in timeline:
        if ts > target:
            break
        result = val
    return result


# ── State parsing helpers ───────────────────────────────────────────────


def _state_timestamp(state: Any) -> datetime | None:
    """Extract timestamp from a recorder state object."""
    try:
        ts = state.last_changed
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts
    except AttributeError:
        pass
    return None


def _state_value(state: Any) -> str | None:
    """Extract the main state value, filtering unavailable/unknown."""
    try:
        val = state.state
        if val in (None, "unavailable", "unknown", ""):
            return None
        return str(val)
    except AttributeError:
        return None


def _state_attributes(state: Any) -> dict[str, Any]:
    """Extract attributes dict from a state object."""
    try:
        return state.attributes or {}
    except AttributeError:
        return {}


def _to_fahrenheit(value: float, attrs: dict[str, Any]) -> float:
    """Convert a temperature value to Fahrenheit based on unit_of_measurement."""
    unit = attrs.get("unit_of_measurement", "")
    if unit in (UnitOfTemperature.CELSIUS, "°C"):
        return TemperatureConverter.convert(
            value, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
        )
    # Assume Fahrenheit if no unit or already °F
    return value
