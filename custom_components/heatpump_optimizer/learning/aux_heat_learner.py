"""Aux/emergency heat activation pattern learner.

Tracks:
  - When aux heat activates (outdoor conditions at each event)
  - A learned effective-outdoor-temp threshold below which aux is likely
  - The heat pump's baseline power draw during normal (non-aux) heating,
    so the resistive strip's incremental BTU can be separated from the
    total power reading for the EKF and savings accounting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)

# EMA decay for threshold learning — adapts over ~10 events
_EMA_ALPHA = 0.2
# EMA decay for heat pump baseline watts — slow, adapts over ~20 samples
_EMA_HP_ALPHA = 0.05
# Default threshold before any learning (conservative — well below freezing)
_DEFAULT_THRESHOLD_F = 25.0
# Minimum events before threshold is considered "learned"
_MIN_EVENTS = 3
# Minimum non-aux heating samples before hp_watts is considered "learned"
_MIN_HP_SAMPLES = 12
# Maximum stored events
_MAX_EVENTS = 50


@dataclass
class AuxHeatEvent:
    """One recorded aux heat activation event."""

    timestamp: str               # ISO-8601 UTC
    outdoor_temp_f: float        # raw outdoor temp at activation
    outdoor_humidity: float      # outdoor RH at activation (ice buildup factor)
    effective_outdoor_temp_f: float  # wind-chill-adjusted temp (better proxy for condenser stress)
    setpoint_delta_f: float      # how far indoor was from setpoint when aux kicked in
    hp_runtime_before_min: float  # how long heat pump ran alone before aux activated


class AuxHeatLearner:
    """Learns aux heat activation conditions and heat pump baseline power.

    Uses exponential moving averages to track two quantities:
    1. ``threshold_f``: effective outdoor temperature below which aux heat is
       likely to activate (driven by observed activation events).
    2. ``learned_hp_watts``: heat pump power draw during normal (non-aux)
       heating, used to separate resistive strip BTU from total circuit power.
    """

    def __init__(self, default_hp_watts: float = 3500.0) -> None:
        self._default_hp_watts = default_hp_watts
        self._events: list[AuxHeatEvent] = []
        self._t_aux_threshold: float = _DEFAULT_THRESHOLD_F
        self._learned_hp_watts: float = default_hp_watts
        self._hp_sample_count: int = 0
        self._is_aux_running: bool = False
        self._hp_runtime_since_start: float = 0.0  # minutes of heat pump before aux

    # ── Public API ────────────────────────────────────────────────────────────

    def record_interval(
        self,
        aux_heat_active: bool,
        outdoor_temp_f: float,
        effective_outdoor_temp_f: float,
        outdoor_humidity: float,
        setpoint_delta_f: float,
        dt_minutes: float,
        hvac_running: bool,
        hvac_mode: str,
        power_watts: float | None,
    ) -> None:
        """Called every coordinator update (~5 min). Updates both learned values.

        Args:
            aux_heat_active: Whether aux/emergency heat is currently running.
            outdoor_temp_f: Raw outdoor temperature in °F.
            effective_outdoor_temp_f: Wind-chill-adjusted outdoor temp in °F.
            outdoor_humidity: Outdoor relative humidity 0-100.
            setpoint_delta_f: |indoor_temp - setpoint| at this moment.
            dt_minutes: Length of this interval in minutes.
            hvac_running: Whether the HVAC system is active at all.
            hvac_mode: "heat", "cool", or "off".
            power_watts: Current HVAC circuit power draw in watts, or None.
        """
        # ── Learn heat pump baseline watts (non-aux heating only) ─────────
        if hvac_running and not aux_heat_active and hvac_mode == "heat" and power_watts:
            if self._hp_sample_count == 0:
                # Cold-start: seed EMA with first observation
                self._learned_hp_watts = power_watts
            else:
                self._learned_hp_watts = (
                    _EMA_HP_ALPHA * power_watts
                    + (1.0 - _EMA_HP_ALPHA) * self._learned_hp_watts
                )
            self._hp_sample_count += 1

        # ── Accumulate heat pump runtime before aux kicks in ──────────────
        if hvac_running and not aux_heat_active:
            self._hp_runtime_since_start += dt_minutes

        if not hvac_running:
            # HVAC cycle ended — reset runtime counter
            self._hp_runtime_since_start = 0.0

        # ── Detect aux heat rising edge ───────────────────────────────────
        was_running = self._is_aux_running
        self._is_aux_running = aux_heat_active

        if aux_heat_active and not was_running:
            # Aux just activated — record event
            event = AuxHeatEvent(
                timestamp=datetime.now(timezone.utc).isoformat(),
                outdoor_temp_f=outdoor_temp_f,
                outdoor_humidity=outdoor_humidity,
                effective_outdoor_temp_f=effective_outdoor_temp_f,
                setpoint_delta_f=setpoint_delta_f,
                hp_runtime_before_min=self._hp_runtime_since_start,
            )
            self._events.append(event)
            if len(self._events) > _MAX_EVENTS:
                self._events.pop(0)

            self._update_threshold(effective_outdoor_temp_f)
            self._hp_runtime_since_start = 0.0

            _LOGGER.info(
                "Aux heat activated: outdoor=%.1f°F (eff=%.1f°F), RH=%.0f%%, "
                "hp_runtime=%.0f min, learned threshold=%.1f°F",
                outdoor_temp_f,
                effective_outdoor_temp_f,
                outdoor_humidity,
                event.hp_runtime_before_min,
                self._t_aux_threshold,
            )

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def threshold_f(self) -> float:
        """Learned effective outdoor temp threshold for aux activation (°F)."""
        return self._t_aux_threshold

    @property
    def learned_hp_watts(self) -> float:
        """Heat pump baseline watts (non-aux heating). Falls back to default before learned."""
        return self._learned_hp_watts

    @property
    def hp_watts_learned(self) -> bool:
        """True once we have enough samples to trust learned_hp_watts."""
        return self._hp_sample_count >= _MIN_HP_SAMPLES

    @property
    def is_learned(self) -> bool:
        """True once enough aux events have been recorded to trust threshold_f."""
        return len(self._events) >= _MIN_EVENTS

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def last_event(self) -> AuxHeatEvent | None:
        return self._events[-1] if self._events else None

    # ── Persistence ───────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "events": [asdict(e) for e in self._events],
            "t_aux_threshold": self._t_aux_threshold,
            "learned_hp_watts": self._learned_hp_watts,
            "hp_sample_count": self._hp_sample_count,
        }

    @classmethod
    def from_dict(cls, data: dict, default_hp_watts: float = 3500.0) -> AuxHeatLearner:
        learner = cls(default_hp_watts=default_hp_watts)
        learner._t_aux_threshold = data.get("t_aux_threshold", _DEFAULT_THRESHOLD_F)
        learner._learned_hp_watts = data.get("learned_hp_watts", default_hp_watts)
        learner._hp_sample_count = data.get("hp_sample_count", 0)
        for ev in data.get("events", []):
            try:
                learner._events.append(AuxHeatEvent(**ev))
            except TypeError:
                pass  # skip malformed entries from schema changes
        return learner

    # ── Private ───────────────────────────────────────────────────────────────

    def _update_threshold(self, effective_outdoor_temp: float) -> None:
        """EMA update of activation threshold using effective outdoor temp."""
        if len(self._events) == 1:
            # First event: seed threshold with this observation
            self._t_aux_threshold = effective_outdoor_temp
        else:
            self._t_aux_threshold = (
                _EMA_ALPHA * effective_outdoor_temp
                + (1.0 - _EMA_ALPHA) * self._t_aux_threshold
            )
