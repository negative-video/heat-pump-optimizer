"""Twin house model: rates fitted from the home's own history, with delayed sun.

Pure Python: no Home Assistant imports and no numpy. Design of record:
``docs/internal/TWIN_STATS_SPEC.md`` sections 3.5 and 3.6.

Why this exists: replayed over a year, the Beestat-profile ``PerformanceModel``
missed real runtime by a quarter to a third (Gate A, heating and cooling both
``poor``). Its tables bake average drift into each outdoor bin at whatever
indoor temperature the house happened to hold, and carry no sun. The same
simple physics fitted from the house's own hourly data, with sun that is
stored and released hours later, tracks it.

Model, all rates in F/hr per 5-minute step::

    drift = k * (T_out - T_in) + c + s . sun(t)
    heat  = a_h + b_h * T_out + drift          compressor heating (net)
    cool  = a_c + b_c * T_out + drift          cooling (net)
    aux   = heat + a_x                         strips on top of the compressor

``sun(t)`` is five features: clear-sky sun on the roof and on one wall
(``wall_az``, picked from the data), the same two through a 2 h lag, and their
sum through an 8 h lag. The lags are how heat soaked into the attic and the
walls in the afternoon keeps arriving in the evening and overnight.

The fit is kept as running sums (sufficient statistics), split into two folds
by day parity and decayed with a one-year half-life, so winter coefficients
survive a summer and every day can be simulated by a model that never saw it.

Known limits, stated rather than fixed: one thermal node (stored heat appears
only through the sun lags), sun is clear-sky scaled by cloud cover, and the
fit is linear in outdoor temperature.
"""

from __future__ import annotations

import copy
import math
import statistics
from collections.abc import Iterable
from datetime import date, datetime, timedelta

from .twin import STEP_MINUTES, STEPS_PER_HOUR, DayInputs

# ── Tunables ──────────────────────────────────────────────────────────

SUN_LAG_FAST_H = 2.0
SUN_LAG_SLOW_H = 8.0
# Hours of weather run through the lag filters before local midnight. Three
# slow time constants, so the state at midnight no longer depends on where
# the filter started.
SUN_LEAD_IN_H = 72

WALL_AZIMUTHS = (0, 45, 90, 135, 180, 225, 270, 315)

TWIN_FIT_HALF_LIFE_DAYS = 365.0
TWIN_FIT_MIN_DAYS = 14
TWIN_FIT_MIN_MODE_H = 20.0
TWIN_FIT_MAX_DATES = 800

TWIN_AUX_MIN_STARTS = 10
TWIN_AUX_MAX_STARTS = 500
TWIN_AUX_LOCKOUT_PCT = 0.99

# A day enters the fit with the section 5 valid-day thresholds.
FIT_MIN_COVERED_MIN = 1200
FIT_MIN_OUTDOOR_FRAC = 0.8

FEATURES = (
    "heat",
    "heat_x_tout",
    "cool",
    "cool_x_tout",
    "aux",
    "tout_minus_tin",
    "const",
    "roof",
    "wall",
    "roof_lag2",
    "wall_lag2",
    "all_lag8",
)
N_COEF = len(FEATURES)
N_SUN = 5
_I_HEAT, _I_COOL, _I_CONST = 0, 2, 6

_STEP = timedelta(minutes=STEP_MINUTES)
_ZERO_SUN = (0.0,) * N_SUN


# ── Sun geometry ──────────────────────────────────────────────────────


def sun_position(t: datetime, lat: float, lon: float) -> tuple[float, float]:
    """``(elevation, azimuth)`` in degrees at aware instant ``t``.

    NOAA's low-precision formulas: about 0.5 degree, plenty for a heat-gain
    feature. Azimuth is clockwise from north.
    """
    utc = t.utctimetuple()  # a naive t is taken as UTC
    hour = utc.tm_hour + utc.tm_min / 60.0 + utc.tm_sec / 3600.0
    g = 2 * math.pi / 365 * (utc.tm_yday - 1 + (hour - 12) / 24)
    eqt = 229.18 * (
        0.000075
        + 0.001868 * math.cos(g)
        - 0.032077 * math.sin(g)
        - 0.014615 * math.cos(2 * g)
        - 0.040849 * math.sin(2 * g)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(g)
        + 0.070257 * math.sin(g)
        - 0.006758 * math.cos(2 * g)
        + 0.000907 * math.sin(2 * g)
        - 0.002697 * math.cos(3 * g)
        + 0.00148 * math.sin(3 * g)
    )
    ha = math.radians((hour * 60 + eqt + 4 * lon) / 4 - 180)
    la = math.radians(lat)
    cz = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    cz = max(-1.0, min(1.0, cz))
    zen = math.acos(cz)
    elev = 90 - math.degrees(zen)
    sz = math.sin(zen)
    if sz < 1e-6:
        return elev, 180.0
    ca = (math.sin(decl) - math.sin(la) * cz) / (math.cos(la) * sz)
    az = math.degrees(math.acos(max(-1.0, min(1.0, ca))))
    if ha > 0:
        az = 360 - az
    return elev, az


def clear_sky(elev: float) -> float:
    """Relative clear-sky beam irradiance, about 1.0 with the sun overhead."""
    if elev <= 0:
        return 0.0
    air_mass = 1 / max(math.sin(math.radians(elev)), 0.05)
    return 0.7 ** (air_mass**0.678) / 0.7


def cloud_factor(cloud: float | None) -> float:
    """Kasten-Czeplak attenuation; 1.0 when cloud cover is unknown."""
    if cloud is None:
        return 1.0
    c = max(0.0, min(1.0, float(cloud)))
    return 1 - 0.75 * c**3.4


def _hour_value(day: DayInputs | None, t: datetime, attr: str):
    if day is None:
        return None
    idx = int((t - day.start).total_seconds() // 3600)
    values = getattr(day, attr)
    if 0 <= idx < len(values):
        return values[idx]
    return None


def sun_series_all(
    inputs: DayInputs,
    prev_inputs: DayInputs | None,
    lat: float,
    lon: float,
    azimuths: Iterable[int] = WALL_AZIMUTHS,
    cache: dict | None = None,
) -> dict[int, list[tuple]]:
    """Sun features for every step of the day, for each wall azimuth.

    ``hours * 12 + 1`` tuples of ``(roof, wall, roof_lag2, wall_lag2,
    all_lag8)``, entry ``i`` at ``start + 5 * i`` minutes. The lag filters
    start ``SUN_LEAD_IN_H`` before local midnight, using the previous day's
    cloud cover for its hours when that day is given and clear sky before
    that. The result depends on time and weather only, so every arm of a day
    gets the same series. ``cache`` (any dict) memoizes sun positions across
    calls.
    """
    azs = tuple(azimuths)
    start = inputs.start
    n_out = inputs.hours * STEPS_PER_HOUR + 1
    lead = SUN_LEAD_IN_H * STEPS_PER_HOUR
    a_fast = 1 - math.exp(-STEP_MINUTES / (SUN_LAG_FAST_H * 60))
    a_slow = 1 - math.exp(-STEP_MINUTES / (SUN_LAG_SLOW_H * 60))
    prev_ok = (
        prev_inputs is not None and (date.fromisoformat(inputs.date) - date.fromisoformat(prev_inputs.date)).days == 1
    )
    lag_r = 0.0
    lag_w = dict.fromkeys(azs, 0.0)
    lag_all = dict.fromkeys(azs, 0.0)
    out: dict[int, list[tuple]] = {az: [] for az in azs}
    for i in range(-lead, n_out):
        t = start + _STEP * i
        key = int(t.timestamp()) // 60
        pos = cache.get(key) if cache is not None else None
        if pos is None:
            pos = sun_position(t, lat, lon)
            if cache is not None:
                cache[key] = pos
        elev, az_sun = pos
        if i >= 0:
            cloud = _hour_value(inputs, t, "cloud")
        else:
            cloud = _hour_value(prev_inputs, t, "cloud") if prev_ok else None
        beam = clear_sky(elev) * cloud_factor(cloud)
        if elev > 0:
            roof = beam * math.sin(math.radians(elev)) + 0.1 * beam
            cos_e = math.cos(math.radians(elev))
        else:
            roof = 0.0
            cos_e = 0.0
        lag_r += a_fast * (roof - lag_r)
        for az in azs:
            wall = beam * max(0.0, cos_e * math.cos(math.radians(az_sun - az))) if elev > 0 else 0.0
            lag_w[az] += a_fast * (wall - lag_w[az])
            lag_all[az] += a_slow * (roof + wall - lag_all[az])
            if i >= 0:
                out[az].append((roof, wall, lag_r, lag_w[az], lag_all[az]))
    return out


def sun_series(
    inputs: DayInputs,
    prev_inputs: DayInputs | None,
    lat: float,
    lon: float,
    wall_az: int,
    cache: dict | None = None,
) -> list[tuple]:
    """``sun_series_all`` for one wall azimuth."""
    return sun_series_all(inputs, prev_inputs, lat, lon, (wall_az,), cache)[wall_az]


# ── Fit rows ──────────────────────────────────────────────────────────


def fit_valid(inputs: DayInputs) -> bool:
    """Section 5 valid-day thresholds, applied before a day enters the fit."""
    if inputs.covered_min < FIT_MIN_COVERED_MIN:
        return False
    have = sum(1 for v in inputs.outdoor if v is not None)
    return have >= FIT_MIN_OUTDOOR_FRAC * inputs.hours


def fit_rows(inputs: DayInputs, sun: list[tuple], next_top: float | None = None) -> list[tuple[list[float], float]]:
    """``(x, y)`` per usable hour: ``y`` is the change in indoor temperature
    from the top of the hour to the top of the next one (F/hr)."""
    tops = list(inputs.indoor_top) if inputs.indoor_top else [None] * inputs.hours
    tops = tops[: inputs.hours] + [None] * max(0, inputs.hours - len(tops))
    tops.append(next_top)
    rows = []
    for h in range(inputs.hours):
        t0, t1 = tops[h], tops[h + 1]
        tout = inputs.outdoor[h] if h < len(inputs.outdoor) else None
        tin = inputs.indoor[h] if h < len(inputs.indoor) else None
        if t0 is None or t1 is None or tout is None or tin is None:
            continue
        seg = sun[h * STEPS_PER_HOUR : (h + 1) * STEPS_PER_HOUR]
        if len(seg) < STEPS_PER_HOUR:
            continue
        sun_mean = [sum(s[j] for s in seg) / len(seg) for j in range(N_SUN)]
        fh = min(inputs.heat_min[h], 60.0) / 60.0
        fc = min(inputs.cool_min[h], 60.0) / 60.0
        fa = min(inputs.aux_min[h], 60.0) / 60.0
        x = [fh, fh * tout, fc, fc * tout, fa, tout - tin, 1.0, *sun_mean]
        rows.append((x, t1 - t0))
    return rows


# ── Linear algebra (12 x 12, pure Python) ─────────────────────────────


def solve(xtx: list[list[float]], xty: list[float], ridge: float = 1e-6) -> list[float] | None:
    """``(XtX + Lambda)^-1 Xty`` by Gaussian elimination with partial pivoting.

    ``Lambda`` is diagonal: ``ridge`` times each column's own ``XtX`` diagonal
    (scale-free), or times the mean diagonal for a column that is all zero
    (cooling in a winter-only fit), whose coefficient then solves to zero.
    Returns None when there is nothing to solve.
    """
    n = len(xty)
    diag = sum(xtx[i][i] for i in range(n)) / n if n else 0.0
    if diag <= 0:
        return None
    lam = [ridge * (xtx[i][i] if xtx[i][i] > 0 else diag) for i in range(n)]
    a = [[xtx[i][j] + (lam[i] if i == j else 0.0) for j in range(n)] + [xty[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-15:
            return None
        a[col], a[piv] = a[piv], a[col]
        p = a[col][col]
        for r in range(col + 1, n):
            f = a[r][col] / p
            if f:
                row_r, row_c = a[r], a[col]
                for c in range(col, n + 1):
                    row_r[c] -= f * row_c[c]
    beta = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = a[r][n] - sum(a[r][c] * beta[c] for c in range(r + 1, n))
        beta[r] = s / a[r][r]
    return beta


class Sums:
    """Weighted least-squares sufficient statistics."""

    __slots__ = ("n", "xtx", "xty", "yty")

    def __init__(self) -> None:
        self.xtx = [[0.0] * N_COEF for _ in range(N_COEF)]
        self.xty = [0.0] * N_COEF
        self.yty = 0.0
        self.n = 0.0

    def add(self, x: list[float], y: float, w: float = 1.0) -> None:
        for i in range(N_COEF):
            wxi = w * x[i]
            if wxi == 0.0:
                continue
            row = self.xtx[i]
            for j in range(i, N_COEF):
                row[j] += wxi * x[j]
            self.xty[i] += wxi * y
        self.yty += w * y * y
        self.n += w

    def scale(self, f: float) -> None:
        for row in self.xtx:
            for j in range(N_COEF):
                row[j] *= f
        self.xty = [v * f for v in self.xty]
        self.yty *= f
        self.n *= f

    def full(self) -> list[list[float]]:
        """Symmetric matrix from the stored upper triangle."""
        m = [row[:] for row in self.xtx]
        for i in range(N_COEF):
            for j in range(i):
                m[i][j] = m[j][i]
        return m

    def plus(self, other: Sums) -> Sums:
        out = Sums()
        for i in range(N_COEF):
            for j in range(i, N_COEF):
                out.xtx[i][j] = self.xtx[i][j] + other.xtx[i][j]
        out.xty = [a + b for a, b in zip(self.xty, other.xty, strict=True)]
        out.yty = self.yty + other.yty
        out.n = self.n + other.n
        return out

    def solve(self) -> list[float] | None:
        if self.n <= N_COEF:
            return None
        return solve(self.full(), self.xty)

    def rss(self, beta: list[float]) -> float:
        m = self.full()
        quad = sum(beta[i] * m[i][j] * beta[j] for i in range(N_COEF) for j in range(N_COEF))
        return self.yty - 2 * sum(b * v for b, v in zip(beta, self.xty, strict=True)) + quad

    def mode_hours(self, index: int) -> float:
        """Runtime hours of a mode: its fraction column times the intercept."""
        return self.xtx[index][_I_CONST]

    def to_dict(self) -> dict:
        return {
            "xtx": [self.xtx[i][j] for i in range(N_COEF) for j in range(i, N_COEF)],
            "xty": list(self.xty),
            "yty": self.yty,
            "n": self.n,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Sums:
        out = cls()
        flat = list(data.get("xtx", []))
        if len(flat) == N_COEF * (N_COEF + 1) // 2:
            k = 0
            for i in range(N_COEF):
                for j in range(i, N_COEF):
                    out.xtx[i][j] = float(flat[k])
                    k += 1
        xty = list(data.get("xty", []))
        if len(xty) == N_COEF:
            out.xty = [float(v) for v in xty]
        out.yty = float(data.get("yty", 0.0))
        out.n = float(data.get("n", 0.0))
        return out


# ── The model the simulator runs ──────────────────────────────────────


class TwinHouseModel:
    """Fitted rates in the shape ``ThermalSimulator`` reads from a model.

    Provides ``passive_drift``, ``heating_delta``, ``cooling_delta``,
    ``aux_heating_delta``, the differentials, ``heat_balance_point``,
    ``hvac_power_watts``, and the two hooks of TWIN_STATS_SPEC 3.6:
    ``begin_step`` and ``aux_rule``. Not a ``PerformanceModel`` subclass.
    """

    def __init__(
        self,
        beta: list[float],
        wall_az: int,
        *,
        heat_differential: float = 1.0,
        cool_differential: float = 1.0,
        heat_balance_point: float | None = 25.0,
        aux_rule: dict | None = None,
        rated_power_watts: float = 3500.0,
        aux_power_watts: float = 10000.0,
    ) -> None:
        self.beta = list(beta)
        self.wall_az = wall_az
        self.heat_differential = heat_differential
        self.cool_differential = cool_differential
        self.heat_balance_point = heat_balance_point
        self.aux_rule = aux_rule
        self.rated_power_watts = rated_power_watts
        self.aux_power_watts = aux_power_watts
        self._series: list[tuple] | None = None
        self._series_start: datetime | None = None
        self._sun: tuple = _ZERO_SUN

    def with_sun(self, series: list[tuple] | None, start: datetime) -> TwinHouseModel:
        """A copy that reads its sun features from ``series`` (one scenario)."""
        twin = copy.copy(self)
        twin._series = series
        twin._series_start = start
        twin._sun = series[0] if series else _ZERO_SUN
        return twin

    def begin_step(self, time: datetime) -> None:
        if not self._series or self._series_start is None:
            self._sun = _ZERO_SUN
            return
        idx = round((time - self._series_start).total_seconds() / (STEP_MINUTES * 60))
        idx = max(0, min(len(self._series) - 1, idx))
        self._sun = self._series[idx]

    def passive_drift(self, outdoor_temp: float, indoor_temp: float | None = None, solar_condition=None) -> float:
        b = self.beta
        tin = outdoor_temp if indoor_temp is None else indoor_temp
        sun = self._sun
        return (
            b[5] * (outdoor_temp - tin)
            + b[6]
            + b[7] * sun[0]
            + b[8] * sun[1]
            + b[9] * sun[2]
            + b[10] * sun[3]
            + b[11] * sun[4]
        )

    def heating_delta(self, outdoor_temp: float, indoor_temp: float | None = None) -> float:
        return self.beta[0] + self.beta[1] * outdoor_temp + self.passive_drift(outdoor_temp, indoor_temp)

    def cooling_delta(self, outdoor_temp: float, indoor_temp: float | None = None) -> float:
        return self.beta[2] + self.beta[3] * outdoor_temp + self.passive_drift(outdoor_temp, indoor_temp)

    def aux_heating_delta(self, outdoor_temp: float, indoor_temp: float | None = None) -> float:
        return self.heating_delta(outdoor_temp, indoor_temp) + self.beta[4]

    def hvac_power_watts(self, mode: str, outdoor_temp: float, aux_active: bool = False) -> float:
        return self.rated_power_watts + (self.aux_power_watts if aux_active else 0.0)

    def coefficients(self) -> dict[str, float]:
        return {name: round(v, 5) for name, v in zip(FEATURES, self.beta, strict=True)}


# ── Running fit ───────────────────────────────────────────────────────


def fold_of(day: str) -> int:
    return date.fromisoformat(day).toordinal() % 2


def learn_aux_rule(starts: list[list]) -> dict | None:
    """``{"runtime_min": R, "lockout_f": L}`` from real aux starts.

    ``starts`` rows are ``[comp_before_min, mean_outdoor]``. ``R`` is the
    median compressor runtime before aux came on, ``L`` the 99th percentile
    outdoor temperature at aux start.
    """
    runs = [float(s[0]) for s in starts if s[0] is not None]
    outs = sorted(float(s[1]) for s in starts if s[1] is not None)
    if len(runs) < TWIN_AUX_MIN_STARTS or len(outs) < TWIN_AUX_MIN_STARTS:
        return None
    lockout = outs[int(TWIN_AUX_LOCKOUT_PCT * (len(outs) - 1))]
    return {"runtime_min": float(statistics.median(runs)), "lockout_f": round(lockout, 2)}


class TwinFit:
    """Running two-fold fit of the twin house model, plus aux starts."""

    def __init__(self, lat: float, lon: float) -> None:
        self.lat = float(lat)
        self.lon = float(lon)
        self.sums: list[dict[int, Sums]] = [{az: Sums() for az in WALL_AZIMUTHS} for _ in range(2)]
        self.days = [0.0, 0.0]  # decayed count of valid days per fold
        self.dates: list[str] = []
        self.last_date: str | None = None
        self.aux_starts: list[list] = []
        self._cache: dict = {}
        self._wall_az: int | None = None

    # -- accumulation ------------------------------------------------------

    def _decay(self) -> float:
        return 0.5 ** (1.0 / TWIN_FIT_HALF_LIFE_DAYS)

    def add_day(
        self,
        inputs: DayInputs,
        prev_inputs: DayInputs | None = None,
        next_top: float | None = None,
    ) -> bool:
        """Fold one finished day into the statistics. False if skipped.

        Days may arrive in any order: a day newer than any seen decays the
        stored sums by its age difference; an older one is added at its own
        decayed weight. A date already added is ignored.
        """
        if inputs.date in self.dates:
            return False
        self._note_date(inputs.date)
        for run in inputs.aux_runs:
            if len(run) >= 4 and run[3] is not None:
                self.aux_starts.append([float(run[3]), run[2]])
        self.aux_starts = self.aux_starts[-TWIN_AUX_MAX_STARTS:]
        if not fit_valid(inputs):
            return False

        d = date.fromisoformat(inputs.date)
        weight = 1.0
        if self.last_date is None or d > date.fromisoformat(self.last_date):
            if self.last_date is not None:
                f = self._decay() ** (d - date.fromisoformat(self.last_date)).days
                for fold in self.sums:
                    for s in fold.values():
                        s.scale(f)
                self.days = [n * f for n in self.days]
            self.last_date = inputs.date
        else:
            weight = self._decay() ** (date.fromisoformat(self.last_date) - d).days

        series = sun_series_all(inputs, prev_inputs, self.lat, self.lon, WALL_AZIMUTHS, self._cache)
        fold = fold_of(inputs.date)
        added = False
        for az, sun in series.items():
            rows = fit_rows(inputs, sun, next_top)
            for x, y in rows:
                self.sums[fold][az].add(x, y, weight)
            added = added or bool(rows)
        if added:
            self.days[fold] += weight
        self._wall_az = None
        if len(self._cache) > 50000:
            self._cache.clear()
        return added

    def _note_date(self, day: str) -> None:
        self.dates.append(day)
        if len(self.dates) > TWIN_FIT_MAX_DATES:
            self.dates = sorted(self.dates)[-TWIN_FIT_MAX_DATES:]

    def add_days(self, days: Iterable[DayInputs]) -> int:
        """Add days in date order, linking each to its neighbours."""
        ordered = sorted(days, key=lambda x: x.date)
        n = 0
        for i, d in enumerate(ordered):
            prev = ordered[i - 1] if i > 0 else None
            nxt = ordered[i + 1] if i + 1 < len(ordered) else None
            next_top = None
            if nxt is not None and (date.fromisoformat(nxt.date) - date.fromisoformat(d.date)).days == 1:
                next_top = nxt.indoor_top[0] if nxt.indoor_top else None
            n += self.add_day(d, prev, next_top)
        return n

    # -- reading -----------------------------------------------------------

    @property
    def wall_az(self) -> int:
        """Azimuth with the lowest residual on both folds combined."""
        if self._wall_az is None:
            best, best_rss = WALL_AZIMUTHS[0], math.inf
            for az in WALL_AZIMUTHS:
                both = self.sums[0][az].plus(self.sums[1][az])
                beta = both.solve()
                if beta is None:
                    continue
                rss = both.rss(beta)
                if rss < best_rss:
                    best, best_rss = az, rss
            self._wall_az = best
        return self._wall_az

    def mode_hours(self, fold: int, mode: str) -> float:
        s = self.sums[fold][WALL_AZIMUTHS[0]]
        return s.mode_hours(_I_HEAT if mode == "heat" else _I_COOL)

    def fold_ready(self, fold: int) -> bool:
        return self.days[fold] >= TWIN_FIT_MIN_DAYS - 1e-6

    def coefficients(self, fold: int | None = None) -> list[float] | None:
        """Fitted coefficients for one fold, or both combined (``None``)."""
        az = self.wall_az
        s = self.sums[0][az].plus(self.sums[1][az]) if fold is None else self.sums[fold][az]
        return s.solve()

    def aux_rule(self) -> dict | None:
        return learn_aux_rule(self.aux_starts)

    def model_for(self, day: DayInputs, base=None) -> TwinHouseModel | None:
        """The model that simulates every arm of ``day``: the other fold's fit.

        None when that fold has fewer than ``TWIN_FIT_MIN_DAYS`` valid days,
        or has seen under ``TWIN_FIT_MIN_MODE_H`` of runtime in the mode that
        carries most of this day's runtime. ``base`` (the active
        ``PerformanceModel``, optional) supplies differentials, balance point
        and watts.
        """
        other = 1 - fold_of(day.date)
        if not self.fold_ready(other):
            return None
        if day.heat_h > 0 or day.cool_h > 0:
            mode = "heat" if day.heat_h >= day.cool_h else "cool"
            if self.mode_hours(other, mode) < TWIN_FIT_MIN_MODE_H:
                return None
        beta = self.coefficients(other)
        if beta is None:
            return None
        return TwinHouseModel(
            beta,
            self.wall_az,
            heat_differential=getattr(base, "heat_differential", 1.0) if base is not None else 1.0,
            cool_differential=getattr(base, "cool_differential", 1.0) if base is not None else 1.0,
            heat_balance_point=getattr(base, "heat_balance_point", 25.0) if base is not None else 25.0,
            aux_rule=self.aux_rule(),
            rated_power_watts=getattr(base, "rated_power_watts", 3500.0) if base is not None else 3500.0,
            aux_power_watts=getattr(base, "aux_power_watts", 10000.0) if base is not None else 10000.0,
        )

    def sun_for(self, day: DayInputs, prev: DayInputs | None) -> list[tuple]:
        """The day's sun series at the current wall azimuth."""
        return sun_series(day, prev, self.lat, self.lon, self.wall_az, self._cache)

    def summary(self) -> dict:
        beta = self.coefficients()
        return {
            "wall_az": self.wall_az,
            "days": [round(n, 1) for n in self.days],
            "days_needed": int(math.ceil(sum(max(0.0, TWIN_FIT_MIN_DAYS - n) for n in self.days))),
            "heat_h": [round(self.mode_hours(f, "heat"), 1) for f in (0, 1)],
            "cool_h": [round(self.mode_hours(f, "cool"), 1) for f in (0, 1)],
            "aux_rule": self.aux_rule(),
            "coefficients": None if beta is None else dict(zip(FEATURES, (round(b, 4) for b in beta), strict=True)),
        }

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "sums": [{str(az): s.to_dict() for az, s in fold.items()} for fold in self.sums],
            "days": list(self.days),
            "dates": list(self.dates),
            "last_date": self.last_date,
            "aux_starts": [list(s) for s in self.aux_starts],
        }

    @classmethod
    def from_dict(cls, data: dict, lat: float | None = None, lon: float | None = None) -> TwinFit:
        out = cls(
            lat if lat is not None else float(data.get("lat", 0.0)),
            lon if lon is not None else float(data.get("lon", 0.0)),
        )
        sums = data.get("sums") or []
        for f, fold in enumerate(sums[:2]):
            for az in WALL_AZIMUTHS:
                if str(az) in fold:
                    out.sums[f][az] = Sums.from_dict(fold[str(az)])
        days = list(data.get("days", [0.0, 0.0]))
        out.days = [float(v) for v in (days + [0.0, 0.0])[:2]]
        out.dates = [str(d) for d in data.get("dates", [])]
        out.last_date = data.get("last_date")
        out.aux_starts = [list(s) for s in data.get("aux_starts", [])][-TWIN_AUX_MAX_STARTS:]
        return out
