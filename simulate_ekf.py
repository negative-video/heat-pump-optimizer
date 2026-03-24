#!/usr/bin/env python3
"""Simulate EKF thermal model performance against historical CSV data.

Compares three initialization paths:
  A) Beestat profile only (no user onboarding)
  B) cold_start with user-provided tonnage + sqft (onboarding path)
  C) Beestat + tonnage override (both data sources)
"""

import csv
import json
import sys
import math
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import numpy as np

# Import thermal_estimator directly (avoid HA dependency chain)
import importlib.util
spec = importlib.util.spec_from_file_location(
    "thermal_estimator",
    "custom_components/heatpump_optimizer/learning/thermal_estimator.py",
)
_mod = importlib.util.module_from_spec(spec)
sys.modules["thermal_estimator"] = _mod
spec.loader.exec_module(_mod)
ThermalEstimator = _mod.ThermalEstimator
IDX_T_AIR = _mod.IDX_T_AIR
IDX_T_MASS = _mod.IDX_T_MASS
IDX_R_INV = _mod.IDX_R_INV
IDX_R_INT_INV = _mod.IDX_R_INT_INV
IDX_C_INV = _mod.IDX_C_INV
IDX_C_MASS_INV = _mod.IDX_C_MASS_INV
IDX_Q_COOL = _mod.IDX_Q_COOL
IDX_Q_HEAT = _mod.IDX_Q_HEAT
IDX_SOLAR_GAIN = _mod.IDX_SOLAR_GAIN

# ── Load data ──────────────────────────────────────────────────────────
with open("docs/internal/Temperature Profile - 2026-03-06.json") as f:
    profile = json.load(f)

def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

entity_data = defaultdict(list)
with open("history(22).csv") as f:
    for row in csv.DictReader(f):
        try:
            ts = parse_ts(row["last_changed"])
        except:
            continue
        entity_data[row["entity_id"]].append((ts, row))

def numeric_series(eid):
    series = []
    for ts, row in entity_data[eid]:
        val = row.get("state", row.get("current_temperature", ""))
        try:
            series.append((ts, float(val)))
        except:
            continue
    series.sort(key=lambda x: x[0])
    return series

# Climate entity
climate_rows = []
for ts, row in entity_data["climate.my_ecobee"]:
    try:
        temp = float(row["current_temperature"])
    except:
        continue
    climate_rows.append({
        "ts": ts, "temp": temp, "action": row.get("hvac_action", "idle"),
        "target_high": float(row.get("target_temp_high") or 80),
        "target_low": float(row.get("target_temp_low") or 60),
    })
climate_rows.sort(key=lambda x: x["ts"])

# Sensor series
outdoor_temp_series = numeric_series("sensor.openweathermap_stats_temperature")
patio_temp_series = numeric_series("sensor.patio_air_temperature")
sun_elevation_series = numeric_series("sensor.sun_solar_elevation")
cloud_cover_series = numeric_series("sensor.openweathermap_stats_cloud_coverage")
wind_speed_series = numeric_series("sensor.openweathermap_stats_wind_speed")
outdoor_humidity_series = numeric_series("sensor.openweathermap_stats_humidity")
pressure_series = numeric_series("sensor.openweathermap_stats_pressure")
indoor_humidity_series = numeric_series("sensor.my_ecobee_current_humidity")
crawlspace_series = numeric_series("sensor.crawlspace_air_temperature")
attic_temp_series = numeric_series("sensor.third_reality_inc_3rths0224z_temperature")
uv_index_series = numeric_series("sensor.openweathermap_stats_uv_index")

def interp_at(series, target_ts, max_age_minutes=30):
    if not series:
        return None
    lo, hi = 0, len(series) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] < target_ts:
            lo = mid + 1
        else:
            hi = mid
    best = None
    best_age = timedelta(minutes=max_age_minutes)
    for idx in [lo - 1, lo, lo + 1]:
        if 0 <= idx < len(series):
            age = abs(series[idx][0] - target_ts)
            if age < best_age:
                best_age = age
                best = series[idx][1]
    return best

# Resample to 5-min intervals
start_ts = climate_rows[0]["ts"]
end_ts = climate_rows[-1]["ts"]
step = timedelta(minutes=5)

def resample_climate(rows, start, end, step):
    result = []
    current_ts = start
    idx = 0
    last_row = rows[0]
    while current_ts <= end:
        while idx < len(rows) - 1 and rows[idx + 1]["ts"] <= current_ts:
            idx += 1
        if rows[idx]["ts"] <= current_ts:
            last_row = rows[idx]
        result.append({**last_row, "ts": current_ts})
        current_ts += step
    return result

samples = resample_climate(climate_rows, start_ts, end_ts, step)
total_hours = (end_ts - start_ts).total_seconds() / 3600
print(f"Data: {len(samples)} intervals over {total_hours:.1f}h ({total_hours/24:.1f} days)")
print(f"Range: {start_ts.strftime('%m/%d %H:%M')} to {end_ts.strftime('%m/%d %H:%M')}")

# ── Simulation runner ──────────────────────────────────────────────────
def run_sim(est, label, extra_update_kwargs=None):
    print(f"\n{'=' * 70}")
    print(f"  {label}")
    print(f"{'=' * 70}")
    print(f"  Initial: Q_cool={est.x[IDX_Q_COOL]/12000:.2f}ton  Q_heat={est.x[IDX_Q_HEAT]/12000:.2f}ton  "
          f"R={est.R_value:.1f}  C_air={1/est.C_inv:.0f}  C_mass={est.thermal_mass:.0f}  "
          f"area={est.envelope_area:.0f}ft²  tonnage_prior={'YES' if est._has_tonnage_prior else 'no'}")

    innovations = []
    mode_innov = {"heating": [], "cooling": [], "idle": []}
    state_hist = []
    load_hist = []
    counts = {"heating": 0, "cooling": 0, "idle": 0, "skip": 0}

    for i, s in enumerate(samples):
        ts, temp, action = s["ts"], s["temp"], s["action"]

        outdoor = interp_at(outdoor_temp_series, ts, 60)
        if outdoor is None:
            outdoor = interp_at(patio_temp_series, ts, 60)
        if outdoor is None:
            counts["skip"] += 1
            continue

        sun = interp_at(sun_elevation_series, ts, 15)
        cloud_raw = interp_at(cloud_cover_series, ts, 60)
        cloud = cloud_raw / 100.0 if cloud_raw is not None else None
        wind = interp_at(wind_speed_series, ts, 60)
        hum_out = interp_at(outdoor_humidity_series, ts, 60)
        pres = interp_at(pressure_series, ts, 120)
        hum_in = interp_at(indoor_humidity_series, ts, 30)
        crawl = interp_at(crawlspace_series, ts, 60)
        attic = interp_at(attic_temp_series, ts, 30)
        uv = interp_at(uv_index_series, ts, 120)

        running = action in ("heating", "cooling")
        mode = {"heating": "heat", "cooling": "cool"}.get(action, "off")
        key = action if action in ("heating", "cooling") else "idle"
        counts[key] += 1

        update_kw = dict(
            observed_temp=temp, outdoor_temp=outdoor,
            hvac_mode=mode, hvac_running=running,
            cloud_cover=cloud, sun_elevation=sun,
            wind_speed_mph=wind, humidity=hum_out,
            pressure_hpa=pres, indoor_humidity=hum_in,
            crawlspace_temp=crawl,
            attic_temp=attic,
            uv_index=uv,
        )
        if extra_update_kwargs:
            update_kw.update(extra_update_kwargs)
        innov = est.update(**update_kw)
        innovations.append(innov)
        mode_innov[key].append(innov)

        if i % 12 == 0:
            state_hist.append({
                "ts": ts, "R": est.R_value, "C_air": 1/est.C_inv,
                "C_mass": est.thermal_mass, "Q_cool": est.x[IDX_Q_COOL],
                "Q_heat": est.x[IDX_Q_HEAT], "solar": est.solar_gain_btu,
                "conf": est.confidence, "T_out": outdoor,
            })
        if est._last_thermal_loads:
            load_hist.append({"action": action, **{k: v for k, v in est._last_thermal_loads.items() if isinstance(v, (int, float))}})

    arr = np.array(innovations)
    mae = np.mean(np.abs(arr))
    rmse = np.sqrt(np.mean(arr ** 2))
    bias = np.mean(arr)

    print(f"\n  FINAL STATE:")
    print(f"    Q_cool: {est.x[IDX_Q_COOL]:.0f} BTU/hr ({est.x[IDX_Q_COOL]/12000:.2f} tons)  "
          f"Q_heat: {est.x[IDX_Q_HEAT]:.0f} BTU/hr ({est.x[IDX_Q_HEAT]/12000:.2f} tons)")
    print(f"    R_value: {est.R_value:.2f}  C_air: {1/est.C_inv:.0f}  C_mass: {est.thermal_mass:.0f}")
    print(f"    Solar: {est.solar_gain_btu:.0f} BTU/hr  Confidence: {est.confidence:.1%}")

    print(f"\n  ACCURACY (n={len(innovations)}, H:{counts['heating']} C:{counts['cooling']} I:{counts['idle']}):")
    print(f"    MAE={mae:.3f}°F  RMSE={rmse:.3f}°F  Bias={bias:+.3f}°F  Max={np.max(np.abs(arr)):.2f}°F")
    w05 = np.mean(np.abs(arr) <= 0.5) * 100
    w10 = np.mean(np.abs(arr) <= 1.0) * 100
    print(f"    Within ±0.5°F: {w05:.1f}%   Within ±1.0°F: {w10:.1f}%")

    print(f"\n  BY MODE:")
    for m, il in mode_innov.items():
        if il:
            a = np.array(il)
            print(f"    {m:10s}: MAE={np.mean(np.abs(a)):.3f}°F  Bias={np.mean(a):+.3f}°F  n={len(a)}")

    print(f"\n  CONVERGENCE:")
    ns = len(state_hist)
    pts = [0, ns//4, ns//2, 3*ns//4, ns-1]
    print(f"  {'Time':>18s}  {'R':>5s} {'C_air':>6s} {'C_mass':>6s} {'Q_cool':>6s} {'Q_heat':>6s} {'Solar':>5s} {'Conf':>5s} {'T_out':>5s}")
    for qi in pts:
        if qi < ns:
            h = state_hist[qi]
            print(f"  {h['ts'].strftime('%m/%d %H:%M'):>18s}  {h['R']:5.1f} {h['C_air']:6.0f} {h['C_mass']:6.0f} "
                  f"{h['Q_cool']:6.0f} {h['Q_heat']:6.0f} {h['solar']:5.0f} {h['conf']:5.1%} {h['T_out']:5.1f}")

    print(f"\n  THERMAL LOADS (avg BTU/hr):")
    lk = ["q_env", "q_int", "q_hvac", "q_solar", "q_solar_direct", "q_solar_via_attic", "q_internal", "q_boundary"]
    for mf in ["heating", "cooling", "idle"]:
        ml = [l for l in load_hist if l["action"] == mf]
        if ml:
            parts = [f"{k}={sum(l.get(k,0) for l in ml)/len(ml):+.0f}" for k in lk]
            print(f"    {mf:10s}: {', '.join(parts)}")

    # Rolling MAE
    window = 144
    if len(innovations) > window:
        print(f"\n  ROLLING MAE (12hr):")
        for si in range(0, len(innovations) - window + 1, window):
            c = arr[si:si+window]
            t0 = samples[si]["ts"].strftime("%m/%d %H:%M")
            t1 = samples[min(si+window-1, len(samples)-1)]["ts"].strftime("%m/%d %H:%M")
            print(f"    {t0} - {t1}: {np.mean(np.abs(c)):.3f}°F")

    return {"mae": mae, "rmse": rmse, "bias": bias, "conf": est.confidence, "est": est}

# ══════════════════════════════════════════════════════════════════════
# PATH A: Beestat profile only
# ══════════════════════════════════════════════════════════════════════
est_a = ThermalEstimator.from_beestat(profile, indoor_temp=climate_rows[0]["temp"])
ra = run_sim(est_a, "PATH A: Beestat-only (no onboarding)")

# ══════════════════════════════════════════════════════════════════════
# PATH B: cold_start with tonnage + sqft (user onboarding)
# ══════════════════════════════════════════════════════════════════════
est_b = ThermalEstimator.cold_start(indoor_temp=climate_rows[0]["temp"], tonnage=3.5, sqft=2500)
rb = run_sim(est_b, "PATH B: Onboarding (3.5 ton, 2500 ft², 14 SEER)")

# ══════════════════════════════════════════════════════════════════════
# PATH C: Beestat + tonnage override (both data sources)
# ══════════════════════════════════════════════════════════════════════
est_c = ThermalEstimator.from_beestat(profile, indoor_temp=climate_rows[0]["temp"])
tonnage = 3.5
est_c.x[IDX_Q_COOL] = tonnage * 12000.0
est_c.x[IDX_Q_HEAT] = tonnage * 12000.0 * 1.1
est_c.P[IDX_Q_COOL, IDX_Q_COOL] = (0.10 * est_c.x[IDX_Q_COOL]) ** 2
est_c.P[IDX_Q_HEAT, IDX_Q_HEAT] = (0.10 * est_c.x[IDX_Q_HEAT]) ** 2
est_c._has_tonnage_prior = True
est_c.Q[IDX_Q_COOL, IDX_Q_COOL] = 0.01
est_c.Q[IDX_Q_HEAT, IDX_Q_HEAT] = 0.01
est_c._prev_q_cool = float(est_c.x[IDX_Q_COOL])
est_c._prev_q_heat = float(est_c.x[IDX_Q_HEAT])
rc = run_sim(est_c, "PATH C: Beestat + Tonnage (3.5 ton override)")

# ══════════════════════════════════════════════════════════════════════
# PATH D: Beestat + Tonnage + occupancy/appliance loads
#   1200 BTU/hr human load → ~1 person (model: 800 base + 350/person, so
#     with people_home_count=1 → 800+350=1150, close enough to 1200)
#   2500 BTU/hr constant appliance load → appliance_btu=2500
# ══════════════════════════════════════════════════════════════════════
est_d = ThermalEstimator.from_beestat(profile, indoor_temp=climate_rows[0]["temp"])
est_d.x[IDX_Q_COOL] = tonnage * 12000.0
est_d.x[IDX_Q_HEAT] = tonnage * 12000.0 * 1.1
est_d.P[IDX_Q_COOL, IDX_Q_COOL] = (0.10 * est_d.x[IDX_Q_COOL]) ** 2
est_d.P[IDX_Q_HEAT, IDX_Q_HEAT] = (0.10 * est_d.x[IDX_Q_HEAT]) ** 2
est_d._has_tonnage_prior = True
est_d.Q[IDX_Q_COOL, IDX_Q_COOL] = 0.01
est_d.Q[IDX_Q_HEAT, IDX_Q_HEAT] = 0.01
est_d._prev_q_cool = float(est_d.x[IDX_Q_COOL])
est_d._prev_q_heat = float(est_d.x[IDX_Q_HEAT])
rd = run_sim(est_d, "PATH D: Beestat + Tonnage + Occupancy/Appliances (1200+2500 BTU)",
             extra_update_kwargs={"people_home_count": 1, "appliance_btu": 2500.0})

# ══════════════════════════════════════════════════════════════════════
# PATH E: Same as D but now with attic temp + UV index (enhanced solar model)
#   Attic temp and UV index are already passed via run_sim's update_kw.
#   This path is identical to D in parameters — the difference is that
#   the thermal_estimator now uses the improved irradiance hierarchy and
#   attic heat decomposition automatically when attic_temp + uv_index
#   are provided.
# ══════════════════════════════════════════════════════════════════════
# Note: PATH E uses the SAME code as PATH D. The attic_temp and uv_index
# are already being passed in the base update_kw (added above). The
# new irradiance hierarchy in _estimate_irradiance_fraction() and the
# attic heat decomposition in _predict_state() activate automatically.
# So PATH D is actually already using the enhanced model! We just need
# to label it correctly.

# ══════════════════════════════════════════════════════════════════════
# COMPARISON
# ══════════════════════════════════════════════════════════════════════
print(f"\n{'=' * 70}")
print("COMPARISON SUMMARY")
print(f"{'=' * 70}")
print(f"  {'Metric':<22s} {'Beestat':>12s} {'Onboard':>12s} {'Bee+Ton':>12s} {'Bee+Ton+Occ':>12s}")
print(f"  {'─'*22}  {'─'*12}  {'─'*12}  {'─'*12}  {'─'*12}")
results = [ra, rb, rc, rd]
print(f"  {'MAE (°F)':<22s} {ra['mae']:>12.3f} {rb['mae']:>12.3f} {rc['mae']:>12.3f} {rd['mae']:>12.3f}")
print(f"  {'RMSE (°F)':<22s} {ra['rmse']:>12.3f} {rb['rmse']:>12.3f} {rc['rmse']:>12.3f} {rd['rmse']:>12.3f}")
print(f"  {'Bias (°F)':<22s} {ra['bias']:>+12.3f} {rb['bias']:>+12.3f} {rc['bias']:>+12.3f} {rd['bias']:>+12.3f}")
print(f"  {'Confidence':<22s} {ra['conf']:>12.1%} {rb['conf']:>12.1%} {rc['conf']:>12.1%} {rd['conf']:>12.1%}")
for label, idx in [("Q_cool (tons)", IDX_Q_COOL), ("Q_heat (tons)", IDX_Q_HEAT)]:
    vals = [r["est"].x[idx] / 12000 for r in results]
    print(f"  {label:<22s} {vals[0]:>12.2f} {vals[1]:>12.2f} {vals[2]:>12.2f} {vals[3]:>12.2f}")
rvals = [r["est"].R_value for r in results]
print(f"  {'R_value':<22s} {rvals[0]:>12.2f} {rvals[1]:>12.2f} {rvals[2]:>12.2f} {rvals[3]:>12.2f}")
cvals = [r["est"].thermal_mass for r in results]
print(f"  {'C_mass':<22s} {cvals[0]:>12.0f} {cvals[1]:>12.0f} {cvals[2]:>12.0f} {cvals[3]:>12.0f}")
svals = [r["est"].solar_gain_btu for r in results]
print(f"  {'Solar gain':<22s} {svals[0]:>12.0f} {svals[1]:>12.0f} {svals[2]:>12.0f} {svals[3]:>12.0f}")

seer = 14.0
print(f"\n  SEER-derived power: {tonnage * 12000 / seer:.0f} W (3.5 ton @ {seer} SEER)")
print(f"  Actual capacity:   {tonnage * 12000:.0f} BTU/hr = {tonnage} tons")
print(f"  Thermal loads injected in Path D: people=1 (1150 BTU/hr), appliance=2500 BTU/hr")
print(f"  Attic temp + UV index now included in all paths (enhanced solar model)")

# Solar breakdown for PATH D (best path)
est_best = rd["est"]
comps = est_best.thermal_load_components
if comps:
    print(f"\n  SOLAR BREAKDOWN (last update, Path D):")
    print(f"    irradiance_fraction: {comps.get('irradiance_fraction', 'N/A')}")
    print(f"    irradiance_source:   {comps.get('irradiance_source', 'N/A')}")
    print(f"    q_solar_direct:      {comps.get('q_solar_direct', 0):.0f} BTU/hr")
    print(f"    q_solar_via_attic:   {comps.get('q_solar_via_attic', 0):.0f} BTU/hr")
    print(f"    q_solar (total):     {comps.get('q_solar', 0):.0f} BTU/hr")
    print(f"    q_boundary:          {comps.get('q_boundary', 0):.0f} BTU/hr")

print(f"\n{'=' * 70}")
print("DONE")
print(f"{'=' * 70}")
