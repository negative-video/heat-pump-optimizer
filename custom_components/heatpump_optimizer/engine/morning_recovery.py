"""Morning recovery: when and how the heat floor climbs back after the night.

A heat pump's output falls with outdoor temperature, and the coldest hour of
the day is the hour a fixed thermostat schedule asks it to lift the house from
the sleep floor to the daytime floor. On this project's reference house the
compressor nets only about +0.5 F/hr at 30-35 F outdoor, so a 4 F recovery
that begins at dawn runs for hours and still arrives late.

A clock time for "wake" is the wrong thing to ask the occupant for. What they
actually have is a preference about the morning, and there are two honest
versions of it:

``warm_by``
    "Be at the daytime temperature by this time." The planner holds the night
    floor as long as it can and starts recovery just in time, sized by
    simulating the candidates. This is the comfort-first choice and it costs
    energy on cold mornings, because the work has to happen in the cold.

``wait_for_warmth``
    "Take the chill off when I get up, and finish once it is warmer outside."
    The planner holds a modest interim floor from the end of the sleep window
    and raises it to the daytime floor when the forecast outdoor temperature
    reaches a threshold, or at a latest time, whichever comes first. This is
    the energy-first choice.

``sleep_end``
    The floor steps up when the sleep window ends, as a plain schedule would.

Everything here is pure: it takes hour-level facts and returns hour-level
floors. Choosing the just-in-time start needs a simulator, so that search is
driven by the caller through ``warm_by_candidates``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta

RECOVERY_WARM_BY = "warm_by"
RECOVERY_WAIT_FOR_WARMTH = "wait_for_warmth"
RECOVERY_AT_SLEEP_END = "sleep_end"
RECOVERY_MODES = (RECOVERY_WARM_BY, RECOVERY_WAIT_FOR_WARMTH, RECOVERY_AT_SLEEP_END)

# How close to the daytime floor counts as "warm" at the deadline. A real
# thermostat cycles about half a degree below its setpoint, so demanding the
# exact floor would start every recovery an hour early for nothing.
RECOVERY_TOLERANCE_F = 0.5


@dataclass(frozen=True)
class MorningRecoveryConfig:
    """Occupant preference for how the house warms up in the morning."""

    mode: str = RECOVERY_WARM_BY
    weekday_time: dt_time = dt_time(9, 0)
    weekend_time: dt_time = dt_time(12, 0)
    # Interim heat floor for ``wait_for_warmth``. None means halfway between
    # the night floor and the daytime floor.
    hold_temp: float | None = None
    # Outdoor temperature at which ``wait_for_warmth`` finishes the recovery.
    warm_outdoor_temp: float = 35.0
    # Earliest a ``warm_by`` recovery may start, in hours before the deadline.
    max_lead_hours: int = 6

    def target_time(self, local_day: datetime) -> dt_time:
        """The configured time for the given local day (weekend aware)."""
        return self.weekend_time if local_day.weekday() >= 5 else self.weekday_time


@dataclass
class HourFacts:
    """What the recovery planner needs to know about one schedule hour."""

    hour: datetime          # schedule hour key (timezone-aware)
    local: datetime         # the same instant in local time
    in_sleep: bool          # inside the configured sleep window
    home: bool              # occupied, so comfort preferences apply
    day_floor: float        # daytime (non-sleep) heat floor for this hour
    outdoor_temp: float     # forecast outdoor temperature


@dataclass
class MorningWindow:
    """One morning's recovery problem."""

    deadline: datetime                     # the configured time, as an instant
    night_floor: float
    day_floor: float
    # Hours after the sleep window ends and before the deadline, in order.
    hold_hours: list[datetime] = field(default_factory=list)
    # Sleep-window hours a ``warm_by`` recovery is allowed to start in.
    early_hours: list[datetime] = field(default_factory=list)
    outdoor_by_hour: dict[datetime, float] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable identifier for remembering a started recovery."""
        return self.deadline.isoformat()

    @property
    def all_hours(self) -> list[datetime]:
        return sorted(self.early_hours + self.hold_hours)


def parse_time(value, default: dt_time) -> dt_time:
    """Parse ``HH:MM`` or ``HH:MM:SS`` (the HA time selector emits both)."""
    if isinstance(value, dt_time):
        return value
    try:
        parts = str(value).split(":")
        return dt_time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError, TypeError):
        return default


def find_morning_windows(
    hours: list[HourFacts],
    night_floor: float,
    sleep_end: dt_time,
    config: MorningRecoveryConfig,
) -> list[MorningWindow]:
    """Split the planning horizon into one recovery window per local morning.

    A morning has a window only when there is something to recover: the
    daytime floor is above the night floor, the configured time is later than
    the end of the sleep window, and somebody is home.
    """
    if config.mode == RECOVERY_AT_SLEEP_END or not hours:
        return []

    by_day: dict[tuple[int, int, int], list[HourFacts]] = {}
    for fact in hours:
        key = (fact.local.year, fact.local.month, fact.local.day)
        by_day.setdefault(key, []).append(fact)

    windows: list[MorningWindow] = []
    for day_hours in by_day.values():
        day_hours.sort(key=lambda f: f.hour)
        first = day_hours[0]
        target = config.target_time(first.local)
        if target <= sleep_end:
            continue  # nothing between the end of sleep and the target

        deadline_local = first.local.replace(
            hour=target.hour, minute=target.minute, second=0, microsecond=0
        )
        deadline = first.hour + (deadline_local - first.local)
        earliest = deadline - timedelta(hours=max(1, config.max_lead_hours))

        hold: list[datetime] = []
        early: list[datetime] = []
        day_floor = None
        outdoor: dict[datetime, float] = {}
        for fact in day_hours:
            if fact.hour >= deadline or not fact.home:
                continue
            local_t = fact.local.time()
            if not fact.in_sleep and local_t >= sleep_end:
                hold.append(fact.hour)
                day_floor = fact.day_floor if day_floor is None else day_floor
                outdoor[fact.hour] = fact.outdoor_temp
            elif fact.in_sleep and local_t < sleep_end and fact.hour >= earliest:
                early.append(fact.hour)
                outdoor[fact.hour] = fact.outdoor_temp

        if not hold and not early:
            continue
        if day_floor is None:
            day_floor = next(
                (f.day_floor for f in day_hours if f.home), night_floor
            )
        if day_floor <= night_floor:
            continue

        windows.append(
            MorningWindow(
                deadline=deadline,
                night_floor=night_floor,
                day_floor=day_floor,
                hold_hours=hold,
                early_hours=early,
                outdoor_by_hour=outdoor,
            )
        )

    windows.sort(key=lambda w: w.deadline)
    return windows


def hold_floor(window: MorningWindow, config: MorningRecoveryConfig) -> float:
    """The floor held between the end of sleep and the start of recovery."""
    if config.mode == RECOVERY_WAIT_FOR_WARMTH:
        interim = (
            config.hold_temp
            if config.hold_temp is not None
            else (window.night_floor + window.day_floor) / 2.0
        )
        return max(window.night_floor, min(window.day_floor, interim))
    return window.night_floor


def wait_for_warmth_start(
    window: MorningWindow, config: MorningRecoveryConfig
) -> datetime:
    """First post-sleep hour warm enough outside, else the latest time."""
    for hour in window.hold_hours:
        if window.outdoor_by_hour.get(hour, float("-inf")) >= config.warm_outdoor_temp:
            return hour
    return window.deadline


def warm_by_candidates(
    window: MorningWindow, config: MorningRecoveryConfig, now_hour: datetime
) -> list[datetime]:
    """Recovery start hours to try, latest first.

    The caller simulates each and keeps the first (latest) one that is warm
    by the deadline. Starts in the past are not offered.
    """
    hours = [h for h in window.all_hours if h >= now_hour]
    return sorted(hours, reverse=True)


def recovery_floors(
    window: MorningWindow,
    config: MorningRecoveryConfig,
    start: datetime,
) -> dict[datetime, float]:
    """Heat floor for every hour of the window, given the recovery start."""
    held = hold_floor(window, config)
    floors: dict[datetime, float] = {}
    for hour in window.hold_hours:
        floors[hour] = window.day_floor if hour >= start else held
    for hour in window.early_hours:
        # Still inside the sleep window: keep the night floor until recovery
        # begins, then lift it early so the house arrives on time.
        if hour >= start:
            floors[hour] = window.day_floor
    return floors
