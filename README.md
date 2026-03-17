# Heat Pump Optimizer

[![HACS][hacs-badge]][hacs-url]
[![GitHub Release][release-badge]][release-url]
[![License][license-badge]][license-url]

A Home Assistant integration that learns the thermal behavior of your home and uses weather forecasts to schedule your heat pump more efficiently. It shifts *when* your HVAC runs — pre-heating or pre-cooling during favorable conditions and coasting through unfavorable ones — while staying within a comfort range you define.

Works with any thermostat Home Assistant can control (Ecobee, Nest, Z-Wave, generic climate entities).

## Background

A heat pump's efficiency depends on the outdoor temperature. On a mild morning it might deliver 3–4 units of heating or cooling per unit of electricity. By the hottest part of the afternoon, that ratio can drop below 2:1.

Most thermostats don't account for this — they react to the current temperature and run whenever the house drifts outside the setpoint, even during the least efficient hours. This integration looks at the forecast, identifies when the system will run most efficiently, and front-loads work into those hours.

## How It Works

The integration builds a physics-based thermal model of your home (insulation, thermal mass, HVAC capacity) that it learns automatically from thermostat readings via a Kalman filter. Three control layers run on top of it:

1. **Strategic planner** — Re-optimizes setpoint schedule every 1–4 hours based on weather forecasts, electricity rates, and grid carbon intensity.
2. **Tactical controller** — Checks reality against the model every 5 minutes and nudges the setpoint if the house is drifting.
3. **Watchdog** — Detects manual thermostat changes, mode switches, and sensor failures.

## Features

- **Automatic learning** — Extended Kalman Filter estimates building thermal parameters from thermostat data. Or import a [Beestat](https://beestat.io/) profile to start with measured data.
- **Forecast-driven scheduling** — Hourly weather forecasts, with optional electricity rate and carbon intensity awareness.
- **Savings tracking** — A counterfactual simulation of what your thermostat would have done without optimization, decomposed into runtime, COP, rate, and carbon components.
- **Occupancy-aware** — Widens comfort range when away, with calendar integration and pre-conditioning before arrival.
- **Room-aware sensing** — Weights indoor temperature by room occupancy instead of averaging all sensors equally.
- **Auxiliary appliances** — Model thermal impacts of other equipment (heat pump water heaters, dryers, ovens) so the Kalman filter doesn't confuse their effects with building parameter changes.
- **Demand response** — Temporarily widen comfort bounds via service call or automation.
- **Diagnostic sensors** — 45+ entities exposing model state, predictions, savings breakdowns, and confidence levels.

## Getting Started

### Requirements

- Home Assistant 2025.1+
- [HACS](https://hacs.xyz/) installed
- A climate entity that supports `climate.set_temperature`
- A weather entity with hourly forecasts

### Installation

[![Open HACS repository][hacs-install-badge]][hacs-install-url]

Or manually:

1. Open **HACS** → three-dot menu → **Custom repositories**
2. Add `https://github.com/negative-video/heat-pump-optimizer` as an **Integration**
3. Search for **Heat Pump Optimizer** and install
4. Restart Home Assistant
5. Go to **Settings → Devices & Services → Add Integration** → search **Heat Pump Optimizer**

### Initial Setup

The config flow has three steps:

| Step | What you'll configure |
|------|----------------------|
| **Equipment** | Thermostat + one or more weather entities (first is primary, rest are fallbacks) |
| **Thermal Model** | Learn from scratch, import a Beestat profile, or restore a previously exported model |
| **Temperature Boundaries** | Comfort range (where the optimizer works) and safety limits (never exceeded) |

After setup, open **Configure** on the integration card for advanced options: sensors, energy tracking, optimizer tuning, occupancy, calendars, room-aware sensing, and auxiliary appliances.

### What to Expect

| Timeline | What's happening |
|----------|-----------------|
| **Day 1** | Conservative setpoint shifts begin. Model starts collecting observations. |
| **Week 1** | Baseline capture completes (7 day minimum). Model is learning basic thermal characteristics. |
| **Week 2–3** | With a reasonable temperature range, the model starts producing useful estimates. |
| **Month 1–2+** | Confidence grows as it observes different weather patterns. Full calibration depends on temperature variety. |

> **Tip:** Importing a Beestat profile gives the model a head start, but baseline capture still needs 7 days. Restoring a previously exported model is immediate.

> **History bootstrap:** On first setup, the integration loads up to 10 days of thermostat and weather history from Home Assistant's recorder. If sufficient data exists, this can reduce or skip the cold-start learning period.

### Dashboard

A **Heat Pump** tab appears in the sidebar after installation, showing:

- Current phase, setpoint, next action, schedule
- Daily and cumulative savings (energy, cost, CO₂)
- Model learning progress and accuracy tier

No configuration needed — it discovers your optimizer entities automatically. All sensors are also available as standard HA entities for dashboards and automations.

## Configuration

### Initialization Modes

| Mode | Description |
|------|-------------|
| **Learn automatically** | Starts with conservative defaults and learns from thermostat readings (weeks to months depending on weather variety) |
| **Import Beestat profile** | Uses measured temperature deltas from a [Beestat](https://beestat.io/) export; Kalman filter continues refining (faster start, still needs 1–2 weeks) |
| **Restore exported model** | Loads a previously exported model via the `export_model` service (immediate) |

### Optional Sensors

None of these are required. Each one improves accuracy or unlocks additional features:

| Sensor | What it improves |
|--------|-----------------|
| Outdoor temperature | Direct measurement instead of forecast-derived values |
| Outdoor humidity | Wind chill and wet-bulb adjustments for COP modeling |
| Wind speed | Infiltration-adjusted heat loss estimates |
| Solar irradiance | Solar gain modeling |
| Barometric pressure | Atmospheric pressure corrections to COP |
| Indoor temperature (multi) | Room-weighted averages instead of a single thermostat reading |
| Indoor humidity | Humidity-adjusted apparent temperature for comfort |
| HVAC power | Actual power draw for energy accounting (otherwise uses a default wattage) |
| Solar production | Net energy calculations — subtracts self-consumed solar |
| Grid import | Track grid-purchased energy separately |
| CO₂ intensity | Carbon-aware optimization — shift runtime to cleaner grid hours |
| Electricity rate | Cost-aware optimization — shift runtime to cheaper hours |

### Comfort and Safety Ranges

- **Comfort range** (e.g., 70–78°F for cooling) — The optimizer works within this band, pre-cooling toward one end during efficient hours and coasting toward the other. Wider range = more flexibility.
- **Safety limits** (e.g., 50°F min, 85°F max) — Absolute guardrails, never exceeded.

### Auxiliary Appliances

Equipment that impacts indoor temperature — such as a heat pump water heater extracting heat from conditioned air, or a dryer adding heat — can be modeled so the Kalman filter treats their thermal effects as known inputs rather than attributing them to building parameter changes.

Configure appliances in **Configure → Auxiliary Appliances**. Each appliance needs:

- **State entity** — Any HA entity that indicates whether it's running (binary_sensor, switch, water_heater, sensor)
- **Active states** — What state values mean "running" (e.g., `on`, `Compressor Running`)
- **Thermal impact** — BTU/hr when active (negative for cooling, e.g., -4000 for a HPWH; positive for heating, e.g., +3000 for a dryer)
- **Estimated watts** (optional) — Fallback power draw for energy accounting when no real-time power entity is available
- **Power entity** (optional) — A sensor reporting real-time W or kW (e.g., a smart plug)

The profiler automatically skips observations while appliances are active to prevent corrupting its performance bins.

### Time-of-Use Rate Schedule

If your utility charges different rates by time of day:

```yaml
tou_schedule:
  - days: [0, 1, 2, 3, 4]  # Mon–Fri (0=Monday, 6=Sunday)
    start_hour: 16
    end_hour: 21
    rate: 0.35             # $/kWh — peak rate
  - days: [0, 1, 2, 3, 4, 5, 6]
    start_hour: 0
    end_hour: 16
    rate: 0.12             # $/kWh — off-peak
```

Hours use 0–23. First matching period wins. Unmatched hours fall back to `electricity_flat_rate` or the rate entity.

### Calendar and Occupancy

The optimizer can read calendar entities to predict when you're home or away:

- Events matching **home keywords** (e.g., "WFH", "Remote") → home
- Events matching **away keywords** (e.g., "Office", "In-Person") → away
- Comfort range widens when away; pre-conditions before expected return

#### Departure-Aware Pre-conditioning

For more precise timing, add departure profiles in the Schedule options step pairing each person with:

- **Departure zone** — The HA zone to monitor (typically `zone.home`)
- **Travel time sensor** — Commute time in minutes (e.g., Waze or Google Maps)

When configured, if a calendar event shows "Office" at 9:00 AM and the travel sensor reads 25 minutes, the optimizer ensures comfort through ~8:35 AM, then relaxes when you leave.

### Room-Aware Sensing

When multiple indoor temperature sensors are configured with area assignments:

| Mode | Behavior |
|------|----------|
| **Equal** | All sensors averaged equally (default) |
| **Occupied only** | Only rooms with detected motion contribute |
| **Weighted** | Occupied rooms get higher weight (default 3×), unoccupied rooms still contribute |

---

<details>
<summary><strong>Entity Reference</strong></summary>

### Sensors

#### Control

| Entity | Description | Unit |
|--------|-------------|------|
| Current Phase | Optimizer phase (pre-cooling, coasting, maintaining, idle, paused, safe_mode) | — |
| Target Setpoint | Current desired thermostat setpoint | °F |
| Next Action | Human-readable next scheduled action | — |
| Schedule | Schedule entry count (full schedule in attributes) | — |
| Learning Progress | Human-readable learning status | — |

#### Thermal Model

| Entity | Description | Unit |
|--------|-------------|------|
| Model Confidence | Kalman filter confidence level | % |
| Envelope R-Value | Learned envelope thermal resistance | °F·hr/BTU |
| Thermal Mass | Learned building thermal mass | BTU/°F |
| Cooling Capacity | Estimated maximum cooling output | BTU/hr |
| Heating Capacity | Estimated maximum heating output | BTU/hr |
| Thermal Mass Temperature | Hidden thermal mass node temperature | °F |
| Grey-Box Active | Whether the LP-based optimizer is being used | — |

#### Predictions and Diagnostics

| Entity | Description | Unit |
|--------|-------------|------|
| Predicted Temperature | Model's prediction of current indoor temperature | °F |
| Prediction Error | Difference between actual and predicted temperature | °F |
| Model Accuracy | Rolling mean absolute error of predictions | °F |
| Tactical Correction | Real-time setpoint correction from Layer 2 | °F |
| Forecast Deviation | Max divergence between current forecast and last optimization | °F |
| Outdoor Temp Source | Which entity or forecast is providing outdoor temperature | — |
| Indoor Temp Source | Which entity or average is providing indoor temperature | — |
| Net HVAC Power | HVAC power draw after solar offset | W |
| Source Health | Overall sensor health status | — |

#### Auxiliary Appliances

| Entity | Description | Unit |
|--------|-------------|------|
| Appliance Thermal Load | Net thermal impact of all active appliances | BTU/hr |
| Active Appliances | Count and names of currently active appliances | — |

#### Savings (Daily)

| Entity | Description | Unit |
|--------|-------------|------|
| Energy Saved Today | Total energy saved vs baseline | kWh |
| Cost Saved Today | Total cost saved vs baseline | $ |
| CO₂ Avoided Today | Total carbon avoided vs baseline | g |
| Baseline Energy Today | Counterfactual baseline energy usage | kWh |
| Worst Case Energy Today | Theoretical maximum (HVAC always on) | kWh |

#### Savings (Cumulative)

| Entity | Description | Unit |
|--------|-------------|------|
| Energy Saved Cumulative | All-time energy savings | kWh |
| Cost Saved Cumulative | All-time cost savings | $ |
| CO₂ Avoided Cumulative | All-time carbon avoided | kg |

#### Savings Decomposition

| Entity | Description | Unit |
|--------|-------------|------|
| Runtime Savings Today | Energy saved from fewer runtime minutes | kWh |
| COP Savings Today | Energy saved from better compressor efficiency | kWh |
| Rate Savings Today | Cost saved from cheaper electricity hours | $ |
| Carbon Shift Savings Today | CO₂ avoided from cleaner grid hours | g |
| Baseline Avg COP | Average COP of the counterfactual routine | — |
| Optimized Avg COP | Average COP with optimizer | — |
| COP Improvement | Percentage COP improvement over baseline | % |
| Comfort Hours Gained | Hours where optimizer maintained comfort vs baseline violation | hr |
| Baseline Comfort Violations | Hours the baseline would have exceeded comfort bounds | — |
| Baseline Avg Indoor Temp | Average temperature of the virtual house | °F |

#### Savings Confidence

| Entity | Description | Unit |
|--------|-------------|------|
| Baseline Confidence | How well the integration knows your old routine | % |
| Savings Accuracy Tier | Current accuracy tier (learning, estimated, simulated, calibrated) | — |
| Savings Percent | Estimated runtime savings percentage | % |

#### Performance Profiler

| Entity | Description | Unit |
|--------|-------------|------|
| Profiler Confidence | Performance profiler confidence level | % |
| Profiler Active | Whether profiler has replaced default model | — |
| Profiler Observations | Total profiler observations accumulated | — |

#### Comfort and Occupancy

| Entity | Description | Unit |
|--------|-------------|------|
| Apparent Temperature | Humidity-adjusted indoor temperature | °F |
| Occupied Rooms | Count of currently occupied rooms | — |
| Weighted Indoor Temp | Indoor temperature weighted by room occupancy | °F |
| Occupancy Forecast | Next scheduled departure/arrival from calendar | — |
| Pre-conditioning Status | Pre-conditioning plan details and estimates | — |

### Binary Sensors

| Entity | Description |
|--------|-------------|
| Optimizer Active | Whether the optimizer is currently controlling the thermostat |
| Override Detected | Whether a manual thermostat override has been detected |
| Sensor Stale | Whether the thermostat sensor appears stuck (identical readings for 24h+) |
| Aux Heat Active | Whether auxiliary/emergency heat is running |
| Learning Active | Whether the thermal model is still in learning mode |

### Switch

| Entity | Description |
|--------|-------------|
| Optimizer Enabled | Master enable/disable — turning off pauses optimization and stops writing setpoints |

</details>

<details>
<summary><strong>Services</strong></summary>

### `heatpump_optimizer.force_reoptimize`

Re-run the schedule optimizer with the latest forecast.

```yaml
service: heatpump_optimizer.force_reoptimize
```

### `heatpump_optimizer.pause` / `resume`

Pause optimization (thermostat holds its current setpoint) or resume and trigger a re-optimization.

```yaml
service: heatpump_optimizer.pause
```

```yaml
service: heatpump_optimizer.resume
```

### `heatpump_optimizer.set_occupancy`

Override occupancy detection.

```yaml
service: heatpump_optimizer.set_occupancy
data:
  mode: away  # home, away, vacation, or auto
```

### `heatpump_optimizer.demand_response`

Temporarily widen comfort bounds to reduce HVAC load. Auto-restores after the duration.

```yaml
service: heatpump_optimizer.demand_response
data:
  mode: reduce  # reduce or restore
  duration_minutes: 60
```

### `heatpump_optimizer.export_model` / `import_model`

Export the learned thermal model as JSON for backup or transfer, or import a previously exported one.

```yaml
service: heatpump_optimizer.export_model
```

```yaml
service: heatpump_optimizer.import_model
data:
  model_data: { ... }  # JSON from export_model
```

### `heatpump_optimizer.reset_model`

Fully reset the learned thermal model, profiler, and all learning state back to a fresh cold-start. History bootstrap re-runs immediately. Use this after significant changes (e.g., adding appliance corrections, major insulation work) that would make old learned parameters inaccurate.

```yaml
service: heatpump_optimizer.reset_model
```

### `heatpump_optimizer.rebootstrap`

Re-run history bootstrap, replaying up to 10 days of recorder history through the thermal model. Does not clear existing learning — just feeds more data.

```yaml
service: heatpump_optimizer.rebootstrap
```

### `heatpump_optimizer.set_constraint`

Apply a temporary constraint from an external energy manager or automation.

```yaml
service: heatpump_optimizer.set_constraint
data:
  type: max_temp  # max_temp, min_temp, max_power, or pause_until
  value: 76
  duration_minutes: 120
  source: my_automation
```

</details>

<details>
<summary><strong>Events</strong></summary>

The integration fires custom events for use in automations:

| Event | Description |
|-------|-------------|
| `heatpump_optimizer_optimization_complete` | Schedule was updated |
| `heatpump_optimizer_override_detected` | Manual thermostat change detected |
| `heatpump_optimizer_mode_changed` | HVAC mode switched (cool/heat/off) |
| `heatpump_optimizer_model_alert` | Model confidence issue |
| `heatpump_optimizer_safe_mode_entered` | Forecast data stale, using safe defaults |
| `heatpump_optimizer_disturbed` | Large temperature drift detected (window open, etc.) |
| `heatpump_optimizer_confidence_reached` | Kalman filter crossed confidence threshold |
| `heatpump_optimizer_preconditioning_start` | Pre-arrival conditioning started |
| `heatpump_optimizer_preconditioning_complete` | Pre-arrival conditioning finished |
| `heatpump_optimizer_occupancy_forecast_changed` | Calendar occupancy timeline updated |
| `heatpump_optimizer_calendar_override` | Manual override to calendar-based plan |
| `heatpump_optimizer_accuracy_tier_changed` | Savings accuracy tier changed |
| `heatpump_optimizer_baseline_complete` | Baseline capture finished (7-day minimum) |

</details>

<details>
<summary><strong>Automation Examples</strong></summary>

#### Notify when savings accuracy improves

```yaml
automation:
  - alias: "Heat Pump: Accuracy tier upgraded"
    trigger:
      - platform: event
        event_type: heatpump_optimizer_accuracy_tier_changed
    action:
      - service: notify.mobile_app
        data:
          title: "Heat Pump Optimizer"
          message: "Savings accuracy upgraded to {{ trigger.event.data.tier }}"
```

#### Demand response from a grid signal

```yaml
automation:
  - alias: "Heat Pump: Grid demand response"
    trigger:
      - platform: state
        entity_id: binary_sensor.grid_peak_event
        to: "on"
    action:
      - service: heatpump_optimizer.demand_response
        data:
          mode: reduce
          duration_minutes: 120
  - alias: "Heat Pump: Grid demand response restore"
    trigger:
      - platform: state
        entity_id: binary_sensor.grid_peak_event
        to: "off"
    action:
      - service: heatpump_optimizer.demand_response
        data:
          mode: restore
```

#### Weekly model backup

```yaml
automation:
  - alias: "Heat Pump: Weekly model export"
    trigger:
      - platform: time
        at: "03:00:00"
    condition:
      - condition: time
        weekday: [sun]
    action:
      - service: heatpump_optimizer.export_model
```

#### Notify on manual override

```yaml
automation:
  - alias: "Heat Pump: Override detected"
    trigger:
      - platform: event
        event_type: heatpump_optimizer_override_detected
    action:
      - service: notify.mobile_app
        data:
          title: "Thermostat Override"
          message: "Someone changed the thermostat manually. The optimizer will resume in {{ states('sensor.heatpump_optimizer_override_grace_period') }} hours."
```

</details>

<details>
<summary><strong>How Savings Tracking Works</strong></summary>

The integration maintains a counterfactual simulation — a parallel model of what your thermostat would have done without optimization, running against the same actual weather.

Each hour, it compares the optimizer's actual behavior against this baseline:

- **Runtime savings** — Fewer total runtime minutes while maintaining comfort
- **COP savings** — Runtime shifted to outdoor temperatures where the heat pump is more efficient
- **Rate arbitrage** — Runtime shifted to cheaper electricity hours (requires rate sensor or TOU schedule)
- **Carbon shifting** — Runtime shifted to hours when the grid is cleaner (requires CO₂ intensity sensor)

Savings accuracy improves over time:

| Tier | When | Meaning |
|------|------|---------|
| Learning | Days 0–7 | Baseline still being captured; worst-case estimates only |
| Estimated | ~Day 7+ | Baseline captured, model confidence still low |
| Simulated | ~Week 2+ | Counterfactual digital twin is active |
| Calibrated | Weeks–months | Model and baseline both high-confidence |

</details>

<details>
<summary><strong>Architecture</strong></summary>

### Three-Tier Control

1. **Strategic planner (Layer 1)** — Runs every 1–4 hours. Fetches weather forecast, generates optimized setpoint schedule, decides when to re-optimize.

2. **Tactical controller (Layer 2)** — Runs every 5 minutes. Compares predicted indoor temperature against reality. Applies damped corrections when they diverge. Detects "disturbed" states (window open, large party) when error exceeds 2°F.

3. **Watchdog controller (Layer 3)** — Event-driven. Detects manual overrides, mode changes, and thermostat unavailability. Triggers a grace period on override before resuming.

### Thermal Model

Two-node RC thermal circuit:

```
C_air  * dT_air/dt  = (T_out - T_air)/R + (T_mass - T_air)/R_int + Q_hvac + Q_solar + Q_appliances
C_mass * dT_mass/dt = (T_air - T_mass)/R_int
```

Where:
- `R` — Envelope thermal resistance (insulation)
- `R_int` — Air-to-mass coupling (internal surfaces)
- `C_air` — Air thermal capacitance
- `C_mass` — Thermal mass (walls, slab, furniture)
- `Q_hvac` — HVAC output (temperature-dependent COP)
- `Q_solar` — Solar heat gain
- `Q_appliances` — Known thermal loads from auxiliary appliances

An Extended Kalman Filter estimates 9 state parameters online:

```
[T_air, T_mass, 1/R, 1/R_int, 1/C_air, 1/C_mass, Q_cool_base, Q_heat_base, Q_solar_peak]
```

### Schedule Optimization

Two methods:

- **Work-based heuristic** (default) — Scores each hour by HVAC efficiency at the forecasted outdoor temperature. Shifts runtime to efficient hours. Fast, no dependencies.

- **Grey-box LP optimizer** (optional) — Formulates scheduling as a linear program minimizing energy subject to comfort bounds. Propagates parameter uncertainty to tighten margins when the model is less confident. Enable via `use_greybox_model` in Behavior options. Falls back to heuristic if the solver fails.

### Data Flow

```
Climate Entity (thermostat)
    |
ThermostatAdapter (read state, detect overrides, write setpoints)
    |
Coordinator (5-min update cycle)
    |-- SensorHub (weather, occupancy, power, solar)
    |-- ApplianceManager (auxiliary appliance state tracking)
    |-- ThermalEstimator (Kalman filter parameter learning)
    |-- StrategicPlanner --> ScheduleOptimizer or GreyBoxOptimizer
    |-- TacticalController (drift correction)
    |-- WatchdogController (override detection)
    |-- CounterfactualSimulator (digital twin)
    |-- SavingsTracker (energy/cost/CO2 accounting)
    |
Sensor Entities (45+)
```

</details>

<details>
<summary><strong>Troubleshooting</strong></summary>

| Problem | Likely cause | Fix |
|---------|-------------|-----|
| Savings show 0 kWh | Baseline capture hasn't completed (needs 7 days) | Wait for the `baseline_complete` event; check Learning Progress sensor |
| Model confidence stuck at 0% | Not enough weather variety | Needs outdoor temperature swings; importing a Beestat profile helps |
| Setpoint not changing | Optimizer paused or switch off | Check the Optimizer Enabled switch and Current Phase sensor |
| "Safe mode entered" event | Forecast data stale (>6 hours) | Verify weather entity is updating; check Source Health sensor |
| Override detected repeatedly | Manual thermostat adjustments | Increase `override_grace_period_hours` in Behavior options |
| Dashboard not appearing | Panel registration failed | Restart Home Assistant; check logs for frontend errors |
| Sensors show "unknown" | First update hasn't completed | Wait 5 minutes after restart; check integration logs |
| R-value or capacity unstable | Unmodeled thermal load (appliance, window, etc.) | Configure auxiliary appliances; call `reset_model` to retrain cleanly |

</details>

---

<details>
<summary><strong>Math</strong> — Full thermal model, EKF, and optimizer formulation</summary>

## Thermal Model

The building is modeled as a two-node RC (resistance-capacitance) thermal circuit. One node represents the indoor air; the other represents the thermal mass (walls, slab, furniture). The two nodes exchange heat through an internal coupling resistance, and the air node exchanges heat with the outdoor environment through the building envelope.

### Governing equations

```
C_air  · dT_air/dt  = Q_env + Q_coupling + Q_hvac + Q_solar + Q_internal + Q_boundary + Q_appliances
C_mass · dT_mass/dt = R_int_inv · (T_air - T_mass)
```

Where each heat flow term (in BTU/hr) is:

| Term | Expression | Meaning |
|------|-----------|---------|
| Q_env | `UA · φ · (T_out_eff - T_air)` | Envelope heat flow (conduction + infiltration) |
| Q_coupling | `R_int_inv · (T_mass - T_air)` | Internal coupling between air and thermal mass |
| Q_hvac | See [HVAC model](#hvac-capacity-model) | Heating or cooling output |
| Q_solar | `Q_solar_peak · (1 - cloud_cover) · sin(elevation)` | Solar heat gain through windows/surfaces |
| Q_internal | `800 + 350 · n_people` BTU/hr | Occupant and appliance base load |
| Q_boundary | `K_attic · (T_attic - T_air) + K_crawl · (T_crawl - T_air)` | Buffer zone heat transfer |
| Q_appliances | Configured per-appliance BTU/hr | Auxiliary appliance thermal loads |

**Definitions:**

- `UA = R_inv · A_envelope` — whole-building conductance (per-area conductance × envelope area, BTU/hr/°F)
- `φ = 1 + 2·n_open_doors + 0.025·v_wind` — infiltration multiplier (dimensionless)
- `T_out_eff = T_out - 3°F` during precipitation (evaporative cooling correction), otherwise `T_out`
- `R_int_inv` — air-to-mass coupling conductance (BTU/hr/°F)
- `K_attic = 50`, `K_crawl = 25` BTU/hr/°F — boundary zone conductances
- `Q_solar_peak` — learned peak solar gain at clear-sky noon (BTU/hr, estimated by the EKF)

### HVAC capacity model

HVAC output depends on operating mode and degrades with outdoor temperature:

**Cooling** (Q_hvac is negative, removes heat):

```
Q_hvac = -Q_cool_base · η_cool · η_humidity · η_SHR · η_pressure · η_duct
```

**Heating** (Q_hvac is positive, adds heat):

```
Q_hvac = Q_heat_base · η_heat · η_pressure · η_duct
```

Where the correction factors are:

| Factor | Expression | Condition |
|--------|-----------|-----------|
| η_cool | `max(0.1, 1 - 0.012 · (T_out - 75))` | Capacity loss ~1.2%/°F above 75°F |
| η_heat | `max(0.1, 1 - 0.015 · (75 - T_out))` | Capacity loss ~1.5%/°F below 75°F |
| η_humidity | `max(0.8, 1 - (RH_out - 50) / 500)` | Outdoor humidity > 50% reduces cooling COP |
| η_SHR | `max(0.65, 1 - (RH_in - 50) / 100)` | Indoor humidity > 50%: more latent, less sensible cooling |
| η_pressure | `(P / 1013.25)^0.1` | Altitude/weather pressure correction |
| η_duct | `max(0.5, 1 - 0.003 · |T_attic - T_air|)` | Duct loss when attic temp diverges from conditioned air |

All correction factors default to 1.0 when the corresponding sensor data is unavailable.

### Time integration

The continuous equations are integrated using an exponential decay (matrix exponential) method rather than forward Euler. This is unconditionally stable even at extreme parameter values:

**Air node:**

```
λ_air = UA · φ + R_int_inv + K_boundary
α = λ_air · C_inv · Δt

If α > 0:
  T_eq = (sum of all forcing terms) / λ_air
  T_air(t+Δt) = T_air(t) · e^(-α) + T_eq · (1 - e^(-α))
```

**Mass node:**

```
β = R_int_inv · C_mass_inv · Δt
T_mass(t+Δt) = T_mass(t) · e^(-β) + T_air(t) · (1 - e^(-β))
```

This exponential integration avoids the oscillation problems that forward Euler exhibits when `λ·Δt` approaches 2, which can happen with large conductance values or long time steps.

---

## Extended Kalman Filter

The EKF jointly estimates the two temperature states and seven building/HVAC parameters online, updating every 5 minutes.

### State vector (9 elements)

```
x = [T_air, T_mass, R_inv, R_int_inv, C_inv, C_mass_inv, Q_cool_base, Q_heat_base, Q_solar_peak]
```

| Index | Symbol | Unit | Meaning |
|-------|--------|------|---------|
| 0 | T_air | °F | Indoor air temperature |
| 1 | T_mass | °F | Hidden thermal mass temperature |
| 2 | R_inv | BTU/hr/°F/ft² | Envelope conductance (per unit area) |
| 3 | R_int_inv | BTU/hr/°F | Air↔mass coupling conductance |
| 4 | C_inv | 1/(BTU/°F) | Inverse air thermal capacitance |
| 5 | C_mass_inv | 1/(BTU/°F) | Inverse mass thermal capacitance |
| 6 | Q_cool_base | BTU/hr | Base cooling capacity at 75°F reference |
| 7 | Q_heat_base | BTU/hr | Base heating capacity at 75°F reference |
| 8 | Q_solar_peak | BTU/hr | Peak solar gain at clear-sky noon |

### Parameter bounds

Parameters are clamped to physical bounds after each update:

| Parameter | Min | Max | Physical meaning |
|-----------|-----|-----|-----------------|
| R_inv | 0.01 | 2.0 | R-value range: 0.5–100 °F·hr/BTU |
| R_int_inv | 0.5 | 500 | Mass time constant: ~20–20,000 hours |
| C_inv | 1e-5 | 0.01 | Air capacitance: 100–100,000 BTU/°F |
| C_mass_inv | 1e-6 | 0.001 | Mass capacitance: 1,000–1,000,000 BTU/°F |
| Q_cool_base | 5,000 | 80,000 | Cooling capacity in BTU/hr |
| Q_heat_base | 5,000 | 80,000 | Heating capacity in BTU/hr |
| Q_solar_peak | 500 | 15,000 | Peak solar gain in BTU/hr |

### Predict step

The process model `f(x)` applies the thermal equations to predict the next state:

```
x_pred = f(x, inputs)
P_pred = F · P · Fᵀ + Q
```

Where:
- `F` is the Jacobian ∂f/∂x (computed analytically, see below)
- `P` is the state covariance matrix
- `Q` is the process noise covariance (diagonal)

Parameters follow a random walk model — they don't change in the predict step. Only T_air and T_mass evolve according to the thermal equations.

### Jacobian (F matrix)

The Jacobian is computed analytically. Key partial derivatives (using forward Euler notation for clarity; actual integration uses exponential form):

**Air temperature row:**

```
∂T_air'/∂T_air    = 1 - C_inv · (UA·φ + R_int_inv + K_boundary) · Δt
∂T_air'/∂T_mass   = C_inv · R_int_inv · Δt
∂T_air'/∂R_inv    = C_inv · A_envelope · φ · (T_out_eff - T_air) · Δt
∂T_air'/∂R_int_inv = C_inv · (T_mass - T_air) · Δt
∂T_air'/∂C_inv    = Q_total · Δt
∂T_air'/∂Q_cool   = -C_inv · η_total · Δt    (when cooling and running)
∂T_air'/∂Q_heat   = C_inv · η_total · Δt     (when heating and running)
∂T_air'/∂Q_solar  = C_inv · (1-cloud) · sin(elev) · Δt
```

**Mass temperature row:**

```
∂T_mass'/∂T_air      = C_mass_inv · R_int_inv · Δt
∂T_mass'/∂T_mass     = 1 - C_mass_inv · R_int_inv · Δt
∂T_mass'/∂R_int_inv  = C_mass_inv · (T_air - T_mass) · Δt
∂T_mass'/∂C_mass_inv = R_int_inv · (T_air - T_mass) · Δt
```

All other entries in F are 0 (off-diagonal) or 1 (parameter self-transition on the diagonal).

### Update step

The observation model is a direct measurement of T_air from the thermostat:

```
z = T_air_observed
H = [1, 0, 0, 0, 0, 0, 0, 0, 0]
```

Standard EKF update with Joseph form for numerical stability:

```
innovation = z - H · x_pred
S = H · P_pred · Hᵀ + R_meas           (innovation covariance; R_meas = 0.25 °F²)
K = P_pred · Hᵀ · S⁻¹                  (Kalman gain)
x = x_pred + K · innovation
P = (I - K·H) · P_pred · (I - K·H)ᵀ + K · R_meas · Kᵀ
```

### Learning pause

When doors or windows are detected open, the Kalman gain for parameter rows (indices 2–8) is zeroed:

```
K[2:, :] = 0   when open_door_window_count > 0
```

Temperature states (T_air, T_mass) continue to update normally, but the extra infiltration from open doors doesn't corrupt the learned envelope and capacity parameters.

### Process noise (Q matrix)

The diagonal process noise controls how fast each state can drift:

| State | Q diagonal | Rationale |
|-------|-----------|-----------|
| T_air | 0.01 | Moderate — absorbs sensor noise and model mismatch |
| T_mass | 0.01 | Moderate — tracks air temp through coupling |
| R_inv | 1e-8 | Very slow — insulation doesn't change day-to-day |
| R_int_inv | 1e-8 | Very slow — internal coupling is stable |
| C_inv | 1e-10 | Extremely slow — air volume is fixed |
| C_mass_inv | 1e-12 | Extremely slow — thermal mass doesn't change |
| Q_cool_base | 1.0 | Moderate — refrigerant charge, filter condition |
| Q_heat_base | 1.0 | Moderate — same reasoning |
| Q_solar_peak | 1e-4 | Moderate — changes with foliage, seasons |

### Confidence metric

Model confidence (0–100%) is the average variance reduction across all learned parameters:

```
confidence = mean(1 - P_current[i,i] / P_initial[i,i])   for i in [2..8]
```

Where `P_initial` is the covariance at filter initialization. A parameter that hasn't been observed retains its initial variance (confidence contribution = 0). A well-converged parameter has variance near zero (confidence contribution → 1).

### Initialization

**Cold start** (no prior data): Conservative defaults for a ~2,000 ft² home. High initial covariance so the filter converges quickly. Requires 2–3 weeks of mixed weather for full calibration.

**Beestat import**: R and C derived from Beestat's resist (passive drift) trendline slope. `R·C ≈ 1/slope`. HVAC capacity derived from cooling/heating delta measurements. Lower initial covariance (faster convergence, ~3–5 days).

---

## Grey-Box LP Optimizer

The grey-box optimizer uses the EKF's learned parameters to formulate HVAC scheduling as a constrained optimization problem. It finds hourly duty cycles (0–1) that minimize a weighted cost function subject to comfort temperature bounds.

### Linearized thermal model

For the LP, the two-node model is linearized into a scalar recurrence for T_air with T_mass treated as an exogenous trajectory:

```
T_air[t+1] = A[t] · T_air[t] + B[t] · u[t] + d[t]
```

Where:
- `u[t] ∈ [0, 1]` is the HVAC duty cycle at hour t
- `A[t]`, `B[t]`, `d[t]` are time-varying coefficients derived from the EKF parameters and forecast

**Coefficient definitions:**

```
A[t] = 1 - C_inv · (UA + R_int_inv) · Δt
B[t] = C_inv · Q_hvac(T_out[t]) · Δt
d[t] = C_inv · (UA · T_out[t] + R_int_inv · T_mass[t] + Q_solar[t] + Q_internal + Q_appliances) · Δt
```

For cooling, `Q_hvac` is negative so `B[t]` is negative (more duty → lower temperature). For heating, both are positive.

### Thermal mass pre-computation

T_mass changes slowly (C_mass >> C_air), so it's pre-computed as an exogenous trajectory:

1. First pass: simulate T_mass assuming no HVAC (u=0), using the coupled equations
2. Solve the LP using this T_mass trajectory
3. Second pass: re-simulate T_mass using the LP's duty cycles, then re-solve

This two-pass approach corrects for cases where aggressive pre-heating/cooling shifts T_mass enough to matter (e.g., lightweight construction), without requiring a full two-node LP.

### Objective function

The cost of running HVAC at hour t is a weighted combination of three dimensions:

```
cost[t] = w_energy · efficiency[t] + w_carbon · carbon[t] + w_cost · rate[t]
```

Each dimension is min-max normalized to [0, 1]:

- **Efficiency**: inverse of temperature change per hour of runtime. Hours where the heat pump is less effective (high outdoor temp for cooling, low for heating) cost more.

  ```
  efficiency_raw[t] = 1 / (C_inv · |Q_hvac(T_out[t])|)
  ```

- **Carbon**: grid carbon intensity at hour t (gCO₂/kWh), from sensor or forecast
- **Rate**: electricity price at hour t ($/kWh), from sensor, TOU schedule, or forecast

### Uncertainty-aware comfort margins

The EKF covariance matrix propagates forward through the thermal model to compute how much the temperature prediction could drift due to parameter uncertainty:

```
margin[t] = k · σ_T[t]
```

Where:
- `k = 1.5 · (1 - confidence) + 0.2` — scales from 0.2 (confident) to 1.5 (uncertain)
- `σ_T[t]` — predicted temperature standard deviation at hour t, computed by propagating parameter covariance through the linearized model:

```
σ²_T[t+1] = A²[t] · σ²_T[t] + J_R² · σ²_R + J_C² · σ²_C + J_Q² · σ²_Q
```

With sensitivity coefficients:
```
J_R = C_inv · A_envelope · (T_out - T_air) · Δt      (∂T/∂R_inv)
J_C = (UA · (T_out - T_air) + |Q_hvac|) · Δt         (∂T/∂C_inv)
J_Q = C_inv · Δt                                       (∂T/∂Q_hvac)
```

Margins are capped at 3°F to prevent infeasibility. The effective comfort band is:

```
T_eff_min[t] = T_comfort_min + margin[t]
T_eff_max[t] = T_comfort_max - margin[t]
```

This means a model with low confidence optimizes conservatively (narrow effective band, less aggressive setpoint shifts), while a well-calibrated model can use nearly the full comfort range.

### LP solver (greedy thermal-constrained assignment)

Rather than a general-purpose LP solver, the optimizer exploits the chain structure of the thermal dynamics (`T[t+1]` depends only on `T[t]` and `u[t]`):

1. **Passive trajectory**: simulate with u=0 to find where comfort would be violated
2. **Forward pass**: walk forward in time, assigning minimum duty at each hour to keep the trajectory in bounds (binary search for minimum feasible duty)
3. **Greedy fill**: sort all hours by marginal cost (`cost[t] / |B[t]|`), then greedily assign additional duty to cheapest hours first, using binary search for maximum feasible duty at each
4. **Backward trim**: walk from most expensive to cheapest hours, reducing duty to the minimum needed (binary search) — removes over-assignment from the forward pass

Each binary search uses 12 iterations (~0.02% precision on duty cycle). The full solve typically completes in microseconds for a 24-hour horizon.

### Duty-to-setpoint conversion

The LP produces a duty cycle `u[t] ∈ [0, 1]` for each hour. This maps to a thermostat setpoint:

**Cooling:**
```
target[t] = T_comfort_max - u[t] · (T_comfort_max - T_comfort_min)
```

**Heating:**
```
target[t] = T_comfort_min + u[t] · (T_comfort_max - T_comfort_min)
```

High duty → setpoint near the active end of the comfort range (triggering HVAC). Low duty → setpoint near the passive end (coasting).

### Baseline comparison

The counterfactual baseline simulates a conventional thermostat holding a fixed midpoint setpoint with ±0.5°F hysteresis:

```
if mode == "cool" and T > setpoint + 0.5: u_baseline = 1.0
if mode == "cool" and T > setpoint:       u_baseline = (T - setpoint) / 0.5
else:                                      u_baseline = 0.0
```

Savings = baseline runtime − optimized runtime, computed using the same thermal model and weather conditions.

### COP and power estimation

Electrical power draw at each hour:

```
Power_W = (capacity_BTU / COP) · 0.293071

COP_cool = max(1.0, 3.5 · (1 - 0.012 · (T_out - 75)))
COP_heat = max(1.0, 3.0 · (1 - 0.015 · (75 - T_out)))
```

Energy and cost for each hour:
```
kWh[t] = u[t] · Power_W[t] / 1000
cost[t] = kWh[t] · electricity_rate[t]
CO₂[t] = kWh[t] · carbon_intensity[t]
```

</details>

## License

MIT License. See [LICENSE](LICENSE) for details.

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://hacs.xyz/
[release-badge]: https://img.shields.io/github/v/release/negative-video/heat-pump-optimizer
[release-url]: https://github.com/negative-video/heat-pump-optimizer/releases
[license-badge]: https://img.shields.io/github/license/negative-video/heat-pump-optimizer
[license-url]: https://github.com/negative-video/heat-pump-optimizer/blob/main/LICENSE
[hacs-install-badge]: https://my.home-assistant.io/badges/hacs_repository.svg
[hacs-install-url]: https://my.home-assistant.io/redirect/hacs_repository/?owner=negative-video&repository=heat-pump-optimizer&category=integration
