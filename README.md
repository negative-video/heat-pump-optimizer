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
- **Aux/emergency heat awareness** — Automatically learns your heat pump's baseline power draw and derives the resistive strip's BTU contribution so the EKF treats it as a known input rather than a surprise heating event.
- **Demand response** — Temporarily widen comfort bounds via service call or automation.
- **Tier-aware dashboard** — Custom sidebar panel adapts its layout to your current learning stage: live observation cards and a retrospective chart during learning mode; savings, forecast, and thermal profile views once calibrated.
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

| Timeline | What's happening | Dashboard shows |
|----------|-----------------|-----------------|
| **Day 1** | Conservative setpoint shifts begin. Model starts collecting observations. | Learning mode layout: retrospective 48h chart, three learning-progress cards (thermal model, baseline schedule, profiler), milestone checklist, today's snapshot |
| **Week 1** | Baseline capture completes (7 day minimum). Model is learning basic thermal characteristics. | Same as Day 1 until baseline completes; milestone "Baseline schedule" marks as done |
| **Week 2–3** | With a reasonable temperature range, the model starts producing useful estimates. | Estimated-tier layout: conservative savings with uncertainty band, rough indoor forecast (dashed), baseline schedule grid |
| **Month 1–2+** | Confidence grows as it observes different weather patterns. Full calibration depends on temperature variety. | Calibrated-tier layout: full savings panel, 24h forecast chart, thermal profile card with plain-English narrative and per-parameter position bars |

> **Tip:** Importing a Beestat profile gives the model a head start, but baseline capture still needs 7 days. Restoring a previously exported model is immediate.

> **History bootstrap:** On first setup, the integration loads up to 10 days of thermostat and weather history from Home Assistant's recorder. If sufficient data exists, this can reduce or skip the cold-start learning period.

### Dashboard

A **Heat Pump** tab appears in the sidebar after installation. Its layout adapts to your current learning stage automatically. No configuration needed — it discovers your optimizer entities from Home Assistant.

#### Learning mode (Days 0–7+)

While the system is building its baseline, the panel shifts focus from forecasting to observing:

- **Retrospective chart** — 48h of actual indoor and outdoor temperatures with HVAC on/off shading and a dashed overlay of the model's predicted temperature ("what we think happened"). Useful for spotting drafty conditions and verifying the model is tracking reality.
- **Three progress cards** — Thermal Model (confidence %, R-value with climate-relative quality label, thermal mass, capacity), Baseline Schedule (day-of-week dot grid showing which days have been captured), and Performance Profiler (observation count and confidence).
- **Milestone checklist** — Step-by-step progress from "sensors connected" through "full optimization enabled." Shows which step is currently in progress.
- **Today's Snapshot** — Objective energy facts for the day (estimated kWh, current draw, outdoor temp range, aux heat if any). No savings comparisons until the baseline completes.

#### Post-baseline (calibrated)

Once the model is calibrated the panel shows the full suite:

- Current phase, setpoint, next action, schedule
- Daily and cumulative savings (energy, cost, CO₂) with decomposition (runtime, COP, rate, carbon)
- 24h forecast chart with indoor temperature prediction
- **Thermal Profile card** — Your home's characteristics translated into plain English. Four position bars (Insulation, Thermal Mass, HVAC Capacity, Solar Gain) show where your home sits relative to climate-typical ranges. A 1–2 sentence narrative summarizes the combination (e.g., "Well-insulated with high thermal mass — your home stays comfortable for hours after the HVAC shuts off."). Expandable "Raw values" section for power users.
- Diagnostics panel with model accuracy, bias direction, correction factor, and per-sensor health table

All sensors are also available as standard HA entities for dashboards and automations.

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
| Attic temperature | Boundary heat transfer through ceiling; duct efficiency correction (see below) |
| Crawlspace temperature | Boundary heat transfer through floor; improves winter heat loss modeling |
| Door/window contact sensors | Infiltration scaling — EKF pauses parameter updates while open to avoid corrupting envelope estimates |

### Comfort and Safety Ranges

- **Comfort range** (e.g., 70–78°F for cooling) — The optimizer works within this band, pre-cooling toward one end during efficient hours and coasting toward the other. Wider range = more flexibility.
- **Safety limits** (e.g., 50°F min, 85°F max) — Absolute guardrails, never exceeded.

### HVAC System Specifications

The integration asks for a few optional system specs during initial setup. Every field can be left blank — the model will still converge, just more slowly. Providing them tightens the EKF's initial covariance and seeds the energy accounting with accurate values from day one, rather than waiting for the learning pipeline to figure them out over weeks.

| Field | Where to find it | Effect |
|---|---|---|
| **Home conditioned area (sq ft)** | Listing, property tax record, or floor plan | Seeds air thermal capacitance (`C_air`); a 1,000 ft² condo and a 4,000 ft² house have 4× different thermal mass |
| **System tonnage** | Outdoor unit nameplate, HVAC permit, or installer paperwork | Seeds `Q_cool` / `Q_heat` priors in the EKF at ±20% rather than the ±316% blind default; reduces capacity convergence from weeks to days |
| **Aux / emergency heat type** | Thermostat wiring label (`W2`/`E`), HVAC documentation, or air handler label | `electric_strip` enables BTU injection into the EKF when no power sensor is present (see below); `gas`/`oil` flags heat as non-electric for cost modeling |
| **Aux heat capacity (kW)** | Air handler nameplate (e.g., "10 kW" strip kit) | Provides an accurate BTU estimate for each aux heating interval when no circuit power meter is configured |

**Advanced — available in the Energy options step after initial setup:**

| Field | Where to find it | Effect |
|---|---|---|
| **SEER / SEER2 rating** | Unit nameplate, EnergyGuide label, or manufacturer spec sheet | Combined with tonnage, derives rated power draw: `W_rated = (tons × 12,000) / SEER`. Overrides the 3,500 W flat default for all cost and savings calculations |
| **Rated watts (override)** | Clamp meter reading, or nameplate if labeled | Direct override of the derived estimate; highest priority in the power chain |

**How power draw is resolved** (in priority order):
1. Explicit rated watts set in the Energy options step
2. Derived from tonnage + SEER: `(tons × 12,000) / SEER`
3. Estimated from tonnage alone: `tons × 850 W` (≈ 14 SEER, conservative)
4. Flat 3,500 W default

A live HVAC power sensor (clamp meter or smart plug, configured in the Energy step) always supersedes all of the above for actual runtime accounting.

### Auxiliary Appliances

Equipment that impacts indoor temperature — such as a heat pump water heater extracting heat from conditioned air, or a dryer adding heat — can be modeled so the Kalman filter treats their thermal effects as known inputs rather than attributing them to building parameter changes.

> **Aux/emergency heat (resistive strips)** is handled separately and requires no manual configuration. The integration automatically learns your heat pump's baseline power draw from observed non-aux heating intervals, then uses the difference between measured circuit watts and that learned baseline to compute how much heat the resistive strips are actually producing each interval. That derived BTU value (`Q_aux_resistive`) is injected into the EKF as an exogenous load. See [Aux Heat Learner](#aux-heat-learner) in the Math section for the full derivation.

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

#### Aux/Emergency Heat

| Entity | Description | Unit |
|--------|-------------|------|
| Aux Heat Learned HP Watts | EMA-learned heat pump baseline power draw during non-aux heating intervals | W |
| Aux Heat Resistive BTU | Estimated resistive strip output (derived from circuit watts − learned HP watts) | BTU/hr |

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

| Tier | When | Panel layout | Meaning |
|------|------|-------------|---------|
| **Learning** | Days 0–7 | Retrospective chart, progress cards, milestone checklist, today's snapshot (no savings) | Baseline still being captured; savings suppressed |
| **Estimated** | ~Day 7+ | Savings panel with ±uncertainty label in amber, rough indoor forecast | Baseline captured; model confidence still building |
| **Simulated** | ~Week 2+ | Full savings panel + forecast, counterfactual digital twin active | Thermal model producing useful estimates |
| **Calibrated** | Weeks–months | Full panel including thermal profile card with position bars and narrative | Model and baseline both high-confidence |

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
C_air  * dT_air/dt  = (T_out - T_air)/R + (T_mass - T_air)/R_int + Q_hvac + Q_solar + Q_appliances + Q_aux_resistive
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
- `Q_aux_resistive` — Resistive strip heat output derived from circuit power minus learned HP baseline (injected as exogenous load when aux/emergency heat is active)

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
    |-- AuxHeatLearner (learns HP baseline watts; derives Q_aux_resistive from circuit power)
    |-- ThermalEstimator (Kalman filter parameter learning; receives Q_aux_resistive as exogenous input)
    |-- StrategicPlanner --> ScheduleOptimizer or GreyBoxOptimizer
    |-- TacticalController (drift correction)
    |-- WatchdogController (override detection)
    |-- CounterfactualSimulator (digital twin)
    |-- SavingsTracker (energy/cost/CO2 accounting; uses learned HP baseline for aux kWh)
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

<details>
<summary><strong>FAQ</strong></summary>

### Do I actually need all the optional sensors, occupancy tracking, and appliance configuration?

No — the integration works with just a thermostat and a weather entity. That's a fully functional setup. But each additional data source removes a source of ambiguity that the model would otherwise have to guess at, and the impact varies considerably by category.

**What works fine without extra sensors:**
The EKF can learn envelope R-value, thermal mass, and HVAC capacity from thermostat readings alone, given enough time. Savings tracking uses a default 3,500W power estimate multiplied by HVAC runtime. The optimizer runs correctly on weather-only forecasts. Most homes reach meaningful savings within a few weeks on the bare minimum configuration — the model just takes longer to converge and carries wider uncertainty bands throughout.

**What each category actually buys you:**

| Category | Value if missing | Value when added |
|----------|-----------------|-----------------|
| **Outdoor temp sensor** | Uses weather forecast (~1°F typical error, sometimes 3–5°F on calm nights) | Direct measurement eliminates the forecast bias during critical morning pre-heat/cool windows when forecast error is largest |
| **Outdoor humidity + wind speed** | Wind chill and infiltration corrections default to zero | Improves COP estimation during cold snaps (wind drives more infiltration) and enables wet-bulb corrections for cooling efficiency |
| **HVAC power sensor** | Energy estimates use 3,500W default × runtime | Actual energy accounting within ~5%; enables aux heat BTU learning; catches COP degradation (dirty filter, low refrigerant) as learned capacity drops |
| **Solar irradiance** | Grey-box LP uses forecast cloud fraction only for solar load weighting | Provides real-time measured sky condition to the LP planner for better scheduling on variable-cloud days; modestly improves solar_gain_btu convergence by tightening scheduling |
| **Room temp sensors + occupancy** | Single thermostat reading, uniform comfort band | Prevents over-conditioning unoccupied zones; room weighting means the optimizer maintains comfort where people actually are, not at the thermostat location |
| **Calendar / presence** | Comfort band stays constant | Enables pre-conditioning before arrival (you come home to comfort, not the start of a cycle) and relaxed setpoints during long away periods |
| **Auxiliary appliances** | HPWH, dryer, oven loads appear as unexplained temperature anomalies | EKF doesn't misattribute a dryer cycle as building parameter drift; profiler bins stay clean; prevents false tactical corrections |
| **Electricity rate / TOU schedule** | Optimizer minimizes runtime only; ignores price variation | Shifts runtime to off-peak hours; rate arbitrage savings can exceed COP-shift savings on aggressive TOU plans |
| **CO₂ intensity sensor** | Carbon accounting uses a flat average | Shifts runtime to genuinely cleaner grid hours; meaningful for solar households with time-varying grid carbon |

**The honest answer on bare-minimum operation:**

A thermostat + weather entity gives you 70–80% of the value at 0% of the sensor complexity. The optimizer will shift runtime to efficient outdoor temperatures, pre-condition the house during mild mornings, and coast through peak hours — all without any extra setup. The model will converge in roughly 3–4 weeks instead of 1–2, and energy estimates will carry ±20–30% uncertainty instead of ±5–10%.

The configuration that consistently moves the needle the most is an **HVAC circuit power sensor** (a clamp meter or smart breaker on the air handler circuit). It unlocks accurate energy accounting, aux heat BTU learning, and a direct signal for detecting equipment degradation. If you're only going to add one thing, that's it.

Occupancy and calendar integration matter most if your household has a consistent and irregular schedule — work-from-home some days, away others. For a home that's always occupied or always on a fixed schedule, a fixed comfort band works nearly as well.

Auxiliary appliances (especially a HPWH) are worth configuring if the appliance runs frequently and has a large thermal impact relative to your home's size. A 4,000 BTU/hr heat extraction from a HPWH in a small, well-insulated house will noticeably confuse the EKF during summer. In a large, leaky house it may be below the noise floor.

---

### What do attic and crawlspace temperature sensors actually do? Are they worth adding?

These are among the most impactful optional sensors for homes with unconditioned attics or crawlspaces, but they're easy to overlook because they don't appear in "smart home sensor" lists.

**What they do without sensors:**

Without configured sensors, the model omits the boundary heat transfer term entirely — it treats your ceiling and floor as perfect insulators. The EKF still learns *something*, but it compensates by absorbing the unmodeled attic and crawlspace loads into whatever parameters it can adjust: it may slightly underestimate your envelope R-value (attributing ceiling conduction to wall/window loss) or overestimate solar gain (attributing summer attic heat push to solar). The model remains functional; it just carries a systematic bias for homes where attic/floor loads are significant.

**What they do with sensors — two separate effects:**

*1. Boundary heat transfer (both sensors)*

The model applies a fixed conductance for each zone:
- Attic → ceiling: **50 BTU/hr/°F** above or below indoor air temperature
- Crawlspace → floor: **25 BTU/hr/°F**

These constants represent typical insulated assemblies. With a temperature sensor, the actual load becomes:

```
Q_attic   = 50 · (T_attic − T_air)
Q_crawl   = 25 · (T_crawl − T_air)
```

Without a sensor, both terms are zero. With a sensor, the EKF sees an accurate accounting of this load at every 5-minute interval and can separate it from envelope conductance and solar gain in the state vector.

The difference is largest in summer, when an unventilated attic can reach 130–150°F. On a 95°F day with an indoor temp of 75°F, the unaccounted ceiling load from a 140°F attic is `50 × (140 − 75) = 3,250 BTU/hr` — roughly equivalent to a 1-ton ghost load that the model would otherwise try to explain with other parameters.

*2. Duct efficiency correction (attic sensor only)*

If your supply ducts run through the attic (common in forced-air systems), duct conduction loss scales with how far the attic deviates from indoor air temperature:

```
η_duct = max(0.5, 1 − 0.003 · |T_attic − T_air|)
```

At a 140°F attic with 75°F indoor air: `η_duct = max(0.5, 1 − 0.003 × 65) = 0.805` — the model treats the HVAC system as delivering only 80.5% of its rated capacity due to duct conduction losses. Without the attic sensor, `η_duct` defaults to 1.0 (no correction), so cooling runs longer than the model expects and the EKF may under-learn `Q_cool_base` to compensate.

**Is it worth adding?**

For homes with a **vented attic and forced-air ducts in that attic**: absolutely yes. The summer duct correction alone can shift `Q_cool_base` estimates by 10–20%. The attic sensor is the single highest-impact sensor for homes built before 1990 with ductwork in the attic (which is the majority of forced-air homes in the US).

For homes with **conditioned attic space, spray-foam roof deck, or radiant/mini-split systems with no attic ducts**: the boundary term still applies, but the duct correction doesn't matter and the load is smaller. Still useful, but lower priority.

For homes with a **vented crawlspace**: the crawlspace sensor matters most in winter when the crawlspace can drop well below outdoor air temperature on calm, clear nights. An uninsulated floor over a 10°F crawlspace in a 70°F house represents `25 × (70 − 10) = 1,500 BTU/hr` of floor loss the model won't see without the sensor.

A cheap zigbee temperature sensor (under $15) placed in the attic and/or crawlspace is likely one of the highest-return-per-dollar hardware additions for this integration.

---

### How does solar gain work, and what do the attic and irradiance sensors actually change about it?

Solar gain is handled by two completely separate mechanisms in the model that are easy to conflate. Understanding the distinction is important because the two optional sensors — attic temperature and solar irradiance — affect different parts of the calculation.

**The solar gain formula (Q_solar):**

```
Q_solar = solar_gain_btu × (1 − cloud_cover) × sin(sun_elevation)
```

- `solar_gain_btu` is a **learned EKF parameter** — state index 8, initialized at 3,000 BTU/hr, bounded 500–15,000 BTU/hr. The filter estimates it from thermostat data over time.
- `cloud_cover` comes from the **weather forecast** (0 = clear, 1 = overcast). This is always available as long as you have a weather entity. There is no sensor that replaces it.
- `sun_elevation` comes from HA's built-in `sun.sun` entity at runtime, and is pre-calculated from your lat/lon for future forecast hours.

If either `cloud_cover` or `sun_elevation` is unavailable, `Q_solar` returns 0.0 for that interval.

**What the attic sensor does (and does not do) for solar gain:**

The attic sensor feeds an entirely different term — `Q_boundary = 50 × (T_attic − T_air)` — not `Q_solar`. These are separate lines in the governing equation. However, they *interact through the EKF's learned parameter* in a subtle way:

On a hot sunny day, your attic might be 140°F. Without an attic sensor, the model sees two unexplained heat sources at once: actual solar gain through windows AND unaccounted ceiling conduction from a hot attic. Both appear to the EKF as "temperature rose faster than my current parameters predict." Since it can't separate them, it nudges `solar_gain_btu` upward to reduce the residual — but this inflated value is actually compensating for the missing ceiling load. The result is an overestimated `solar_gain_btu` that's entangled with your attic's thermal behavior.

**With** the attic sensor, the ceiling conduction is fully accounted for as a known exogenous input. The EKF's residual on sunny days is now genuinely attributable to window/surface gain, so `solar_gain_btu` converges to a more physically accurate value (typically 2–8 weeks faster, and less likely to swing seasonally as attic temperatures change).

**What the solar irradiance sensor (W/m²) does:**

Counterintuitively, the irradiance sensor does **not** directly improve how `Q_solar` is computed at each 5-minute EKF update. The formula still uses `cloud_cover × sin(elevation)` — the irradiance reading doesn't substitute for that.

What it does instead: the irradiance value is attached to the current `ForecastPoint` and aggregated into the grey-box LP optimizer's hourly planning data. This gives the optimizer a measured sky condition for the current hour rather than relying solely on forecast cloud fraction. On partly-cloudy days where the forecast says "50% cloud cover" but actual irradiance is spiking between 800 and 100 W/m² as clouds pass, the LP's cost weighting for that hour becomes more accurate — which slightly improves when it chooses to pre-cool.

The irradiance sensor also helps the `solar_gain_btu` parameter converge faster during EKF learning because the grey-box optimizer's tighter scheduling means HVAC runs at more predictable times, giving the filter cleaner training intervals (less confounding from unexpected HVAC cycles).

**The four scenarios summarized:**

| Configuration | Q_solar accuracy | Solar gain parameter convergence | Planning accuracy on cloudy/variable days |
|---------------|-----------------|----------------------------------|------------------------------------------|
| Neither sensor | Uses forecast cloud × sin(elev); `solar_gain_btu` entangled with unmodeled ceiling load | Slowest — inflated by attic heat in summer; needs seasonal correction | Relies on forecast cloud fraction only |
| Attic sensor only | Same formula, but ceiling load is separated out | Faster — `solar_gain_btu` gets clean signal from window/surface gain alone | Still relies on forecast cloud fraction |
| Irradiance sensor only | Same formula | Moderate improvement — tighter scheduling means cleaner learning intervals | Real-time sky condition for LP planning |
| Both sensors | Same formula (cloud cover still from forecast) | Fastest convergence, least seasonal bias | Best: ceiling load separated + real-time sky for planner |

**The key takeaway:** No sensor combination changes the *formula* for `Q_solar` — it will always be `solar_gain_btu × clear_sky_fraction × sin(elevation)`. What changes is how accurately `solar_gain_btu` gets learned (attic sensor removes a major confounding source) and how precisely the LP optimizer weighs solar-load hours when scheduling (irradiance sensor provides ground-truth sky condition). The forecast cloud cover is the only input that can't be improved by a sensor — it's always forecast-derived.

---

### Why does the optimizer only update every 5 minutes? My thermostat changes state faster than that.

Two separate mechanisms handle fast vs. slow events.

**Fast (instant):** The watchdog listens to `state_changed` events from the thermostat directly. HVAC mode changes (heat→cool), manual setpoint overrides, and thermostat unavailability are all caught the moment they happen and trigger an immediate re-optimization if needed.

**Slow (5-minute poll):** The Kalman filter, tactical controller, savings accounting, and profiler all run on the 5-minute tick. This is intentional — building thermal dynamics operate on hourly time constants, so indoor air temperature typically moves only 0.1–0.3°F per 5-minute interval. Sampling faster would give you multiple nearly-identical readings per real observation, which adds noise to the filter without adding information. The 5-minute `Δt` is also baked into the EKF's process noise matrix (how much each parameter is allowed to drift per step) and the exponential integration formula. Running at 1 minute would require re-tuning those values and would likely make the filter less stable, not more responsive.

In practice, the weather forecast (hourly), electricity rates (hourly), and most outdoor sensors (1–5 minute reporting intervals) don't change faster than the coordinator polls anyway — so there's no faster signal to capture for the learning and control path.

---

### Why does calibration take weeks? Other "smart" thermostats learn in a few days.

Most smart thermostats learn a *schedule* — when you typically adjust the temperature, and what setpoints you prefer. That can be inferred from a few days of usage.

This integration learns *physics* — the actual thermal resistance of your building envelope, the thermal mass of your walls and slab, the real capacity of your heat pump at different outdoor temperatures, and how much solar gain your windows produce. These values can only be estimated when the house is actually responding to a range of conditions: different outdoor temperatures, sunny vs. cloudy days, heating vs. cooling cycles.

A single week of mild spring weather won't excite the parameter space enough to distinguish good insulation from high thermal mass — both cause the house to hold temperature well. The EKF needs to see the house *losing* temperature (cold nights with HVAC off) and *gaining* it (sunny afternoons) to separate envelope loss from solar gain. Full calibration of all 7 parameters typically requires 1–2 months of seasonal variation, though meaningful optimization starts much earlier.

---

### I entered my system specs (tonnage, square footage, aux heat). How much does that actually help?

The short answer: it can shrink the cold-start learning window from **2–3 weeks down to 2–3 days** for the parameters that matter most.

Here's why. The EKF maintains a covariance matrix alongside the state vector — essentially a confidence interval around each learned parameter. When the filter has no prior information, it starts those intervals very wide so it doesn't get locked into a wrong value early on. The tradeoff is that with wide priors, many observations are needed before the estimate settles down. For HVAC capacity (Q_cool / Q_heat), the default uncertainty is so large that a 1.5-ton window unit and a 5-ton whole-home system are both plausible starting points — the filter has to work through a 16× range before it homes in.

When you provide your system specs, those starting intervals are narrowed to physically reasonable values:

| What you provide | Parameter seeded | Starting uncertainty | Without it |
|---|---|---|---|
| Tonnage | Q_cool, Q_heat | ±20% of rated capacity | ±316% (essentially unconstrained) |
| Home sq ft | C_air (thermal mass) | ±30% | Wide open |

The EKF update equations themselves don't change — every interval still refines every parameter based on observed temperature response. But starting closer to the truth means **fewer intervals needed to reach a useful estimate**, which directly translates to:

- More accurate pre-conditioning start times in the first week
- Savings estimates that reflect reality rather than a wild guess about your system size
- The `Savings Accuracy Tier` sensor advancing from `learning` to `estimated` sooner

**The EKF will still correct wrong values** — if you misremember your tonnage by half a ton, the filter will drift to the right answer within a few days of actual operation. The specs are priors, not hard constraints. Providing them just gives the filter a head start rather than making it start from scratch.

One field that matters even after the model converges: **aux heat type and capacity**. Without a circuit power sensor, the EKF has no way to know how much heat the resistive strip is adding versus the compressor. If you declare `electric_strip` and enter the strip's kW rating, that heat is modeled as a known input — preventing the filter from incorrectly attributing it to building conductance or inflating the compressor capacity estimate during cold-weather aux events.

---

### Can the optimizer push my home's temperature outside my comfort range?

No. The comfort range and safety limits you configure are hard constraints, not targets. The optimizer works *within* the comfort band — shifting *when* it runs and how far toward one end of the band it pre-conditions — but it will never set a temperature outside that range. Safety limits (e.g., 50°F min, 85°F max) are enforced as absolute guardrails that take priority over the schedule under all conditions, including safe mode.

If the model is uncertain, the LP optimizer actually *narrows* its effective working range (adds a margin proportional to parameter uncertainty) so it errs toward running the HVAC rather than drifting outside comfort bounds.

---

### Why do the savings numbers change over time, or sometimes go down?

Savings are measured against a *counterfactual baseline* — what your old thermostat routine would have done that same day. Both the baseline and the thermal model are still being refined during the first few weeks.

Three things cause reported savings to change:

1. **Baseline capture** — The 7-day baseline records how your thermostat normally behaves (setpoints, runtime patterns). Early in that window, the baseline is extrapolated from fewer days. Once all 7 days are captured, it becomes more representative.
2. **Model confidence** — Savings estimates are uncertainty-weighted. When model confidence is low, the optimizer is conservative (it doesn't pre-condition as aggressively), so actual savings are smaller. As confidence grows, the optimizer takes better advantage of efficient hours and real savings improve.
3. **Seasonal variation** — A cold March looks nothing like a mild November to the counterfactual. The optimizer's advantage is largest when there's a meaningful spread between efficient and inefficient hours (big daily temperature swings, variable grid prices). Mild, stable weather naturally produces smaller savings.

The `Savings Accuracy Tier` sensor tells you how much to trust the current numbers. During `learning` and `estimated` tiers, treat the figures as directional, not precise.

---

### What happens when weather data goes stale or Home Assistant loses internet?

The integration enters **safe mode** when the weather forecast is more than 6 hours old. In safe mode:

- The strategic optimizer stops making setpoint changes
- The thermostat holds its last setpoint
- A `heatpump_optimizer_safe_mode_entered` event fires for your automations
- The `Source Health` sensor shows degraded status

When fresh weather data returns, safe mode exits automatically and a re-optimization runs. The tactical controller and Kalman filter continue operating normally during safe mode since they only need the thermostat and temperature sensors, not the forecast.

---

### My utility bill shows more HVAC energy than the integration reports. Why?

A few possible reasons:

- **No power sensor configured** — Without an HVAC circuit power sensor, the integration estimates energy from a default wattage (3,500W) multiplied by runtime. If your actual heat pump draws more (common with larger equipment or electric resistance backup), the estimate will be low. Adding a clamp meter or smart breaker sensor fixes this.
- **Aux/emergency heat** — Before the `AuxHeatLearner` has enough samples (12+ non-aux heating intervals), it uses the same 3,500W default for the baseline. If your system runs aux heat frequently, the first week or two of energy estimates will undercount. Once learned, strip wattage is derived from the actual circuit power delta.
- **Other HVAC loads** — The integration only tracks your primary heat pump circuit. Electric air handlers, supplemental duct heaters, or separate zone controllers on different circuits won't be counted.
- **Standby draw** — Some power sensors report standby draw from the air handler even when the compressor is off. The integration uses the thermostat's `hvac_action` to gate power accounting, so standby may not be included in its figures but would still appear on your utility bill.

---

### Why does model confidence sometimes drop after being high?

The EKF's confidence metric measures how much each parameter's uncertainty has *shrunk* from its initial value. It can decrease when:

- **Seasonal transitions** — Moving from heating to cooling season (or vice versa) means the system starts observing conditions outside the range it was calibrated on. The filter widens uncertainty on capacity parameters until it re-confirms them in the new mode.
- **Equipment changes** — A new air filter, refrigerant recharge, or thermostat replacement changes the actual system behavior. The EKF detects the mismatch and increases uncertainty to allow re-learning.
- **Process noise** — `Q_cool_base` and `Q_heat_base` have relatively high process noise (1.0) because compressor capacity drifts with refrigerant charge, filter condition, and coil fouling. The filter intentionally keeps some uncertainty on these parameters so it continues tracking gradual changes.

A temporary drop is normal and expected. The filter will re-converge as it accumulates observations in the new conditions.

---

### I imported a Beestat profile — why do I still need to wait 7 days?

Beestat gives the thermal model a head start on *building physics* (R-value, thermal mass, capacity). That's what the Kalman filter estimates, and importing a Beestat profile reduces the learning period from weeks to roughly 3–5 days.

Baseline capture is a separate process that records *your routine* — what setpoints you normally run, what times of day the HVAC runs, how your schedule varies by day of week. There's no equivalent shortcut for that. The 7-day minimum ensures the baseline covers a full week (including weekend patterns) before savings comparisons begin. On day 6 of baseline capture, the integration genuinely doesn't know what you normally do on Saturdays yet.

---

### Does it work with multi-zone systems or mini-splits?

One integration instance manages one climate entity. For multi-zone systems, the supported approaches are:

- **Single main zone** — Configure only the primary zone's climate entity; configure the other zones as auxiliary appliances if they affect the conditioned space's temperature.
- **Multiple instances** — Install separate integration instances for each zone (each with its own config entry). They operate independently.
- **Mini-splits** — Work well if the unit is exposed as a standard `climate` entity in Home Assistant. Units that only expose fan/swing controls without temperature feedback won't produce useful EKF learning.

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
| Q_aux_resistive | `(W_circuit - W_hp_learned) · 3.412` when aux active and W_circuit > W_hp_learned; else 0 | Resistive strip heat output, derived from circuit power minus learned HP baseline |

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

**Cold start** (no prior data): Defaults for a ~2,000 ft² home with a ~1.7-ton system. Initial covariance is deliberately high so the filter can converge to truth without fighting a strong prior.

When system specs are provided during setup, the cold-start priors are tightened:

| User provides | Effect on state vector | Covariance improvement |
|---|---|---|
| Tonnage | `Q_cool = tons × 12,000 BTU/hr`; `Q_heat = tons × 13,200 BTU/hr` | `P[Q_cool] = (0.20 × Q_cool)²` — ±20% SD vs. a near-infinite default |
| Home sq ft | `C_inv = 1 / (0.6 × sqft)` — same formula as Beestat path | `P[C_inv] = (0.30 × C_inv)²` — ±30% SD vs. wide open default |

With both provided, capacity and thermal mass converge in days rather than weeks. Without either, the filter still converges but the first-week predictions and savings estimates will be imprecise.

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

---

## Aux Heat Learner

When a heat pump switches to auxiliary or emergency heat, the circuit draws 2–3× more power as resistive heating strips engage alongside (or instead of) the compressor. If this extra heat is not accounted for, the EKF sees a large unexplained temperature rise and incorrectly inflates `Q_heat_base` — poisoning the learned heating capacity.

The `AuxHeatLearner` solves this by learning two quantities adaptively:

### 1. Heat pump baseline watts

During every 5-minute coordinator interval where the HVAC is running in heating mode and aux heat is **not** active, the measured circuit power is used to update an Exponential Moving Average:

```
On first non-aux heating sample:
    W_hp = W_circuit          (cold-start seed)

On subsequent samples:
    W_hp = α_hp · W_circuit + (1 − α_hp) · W_hp
```

Where:
- `W_hp` — learned heat pump baseline power draw (watts)
- `W_circuit` — measured HVAC circuit power at this interval (watts)
- `α_hp = 0.05` — slow decay (adapts over ~20 samples; 1 − (1 − 0.05)^20 ≈ 64% weight on recent 20 observations)
- Default before learning: `W_hp = 3,500 W` (flat default) — or `tons × 850 W` / `(tons × 12,000) / SEER` when system specs are provided
- Considered "learned" once ≥ 12 non-aux heating samples have been observed

The slow EMA is intentional. A fast EMA would let a single cold-snap reading corrupt the estimate; a slow one smooths out compressor surge at startup and partial-capacity operation.

### 2. Resistive strip BTU output

When aux heat is active and circuit power is available:

```
Q_aux_resistive = (W_circuit − W_hp) · 3.412    [BTU/hr]
                  (clamped to ≥ 0)
```

The `3.412` factor converts watts to BTU/hr (1 W = 3.412 BTU/hr). This works because resistive heating is 100% efficient — every watt above the heat pump's baseline goes directly to heat output.

When **no power sensor is configured** but the user has declared `aux_heat_type = electric_strip` and provided `aux_heat_kw`, that capacity is used directly as a fixed prior:

```
Q_aux_resistive = aux_heat_kw × 3,412    [BTU/hr]
```

This prevents the EKF from misattributing strip heat output to building conductance or compressor capacity during aux intervals — an important correction for installs without a clamp meter.

This `Q_aux_resistive` value is injected into `ThermalEstimator` each interval as an exogenous forcing input, treated identically to `Q_appliances` in the governing equations. The EKF therefore sees: "the temperature rose this interval because of a known resistive load of X BTU/hr" — and does not misattribute it to building conductance or HVAC capacity.

### 3. Aux activation threshold learning

Separately from the BTU accounting, the learner also tracks **when** aux heat activates to predict future occurrences. On each aux heat rising edge, it records an `AuxHeatEvent` with the effective outdoor temperature (wind-chill adjusted), outdoor humidity, setpoint delta, and how long the heat pump ran alone before aux kicked in.

The activation threshold is updated as an EMA over recorded effective outdoor temperatures:

```
On first event:
    T_threshold = T_eff_outdoor       (seed)

On subsequent events:
    T_threshold = α_aux · T_eff_outdoor + (1 − α_aux) · T_threshold
```

Where:
- `T_threshold` — learned effective outdoor temp below which aux heat is likely (°F)
- `α_aux = 0.2` — faster decay (adapts over ~10 events)
- Default before learning: `T_threshold = 25°F` (conservative; below freezing)
- Considered "learned" once ≥ 3 events have been observed

### 4. Savings accounting and the counterfactual gap

The savings tracker uses `W_hp` (learned baseline) to compute actual aux kWh:

```
W_resistive  = W_circuit − W_hp
kWh_aux_actual = W_resistive · (interval_minutes / 60) / 1000
```

The **counterfactual simulator** (baseline model) uses a rough proxy for the baseline's aux consumption:

```
W_resistive_proxy ≈ Q_heat_base · 0.293071      [watts]
```

(`0.293071` = 1 BTU/hr / 3.412 W·hr/BTU.) This proxy assumes the baseline would have run the same capacity in full-resistive mode, which is a known approximation. Actual "avoided aux kWh" in the savings decomposition is therefore an estimate, while the EKF accounting (what the house actually experienced) is accurate.

---

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
