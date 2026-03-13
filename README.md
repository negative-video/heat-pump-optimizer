# Heat Pump Optimizer

[![HACS][hacs-badge]][hacs-url]
[![GitHub Release][release-badge]][release-url]
[![License][license-badge]][license-url]

**Automatically save energy by shifting when your heat pump runs — not whether it runs.**

Heat Pump Optimizer learns the thermal characteristics of your home and uses weather forecasts to build an optimized HVAC schedule. It pre-heats or pre-cools during efficient hours and coasts through expensive ones, all while keeping you comfortable. A built-in digital twin tracks exactly how much energy, money, and carbon you're saving compared to your old routine.

Works with any thermostat Home Assistant can control — Ecobee, Nest, Z-Wave, or generic climate entities.

## Why Optimize a Heat Pump?

Unlike a furnace that burns fuel at roughly the same efficiency regardless of conditions, a heat pump *moves* heat — and how hard it has to work depends on the temperature outside. On a mild morning, your heat pump might deliver 3–4 units of heating or cooling for every unit of electricity it consumes. By the hottest part of the afternoon, that ratio can drop below 2:1, meaning the same comfort costs nearly twice the energy.

Most thermostats don't account for this. They react to the current temperature and run whenever the house drifts outside your setpoint — even if that means doing most of the work during the least efficient hours of the day.

Heat Pump Optimizer flips that approach. It looks at the forecast, identifies when your system will run most efficiently, and front-loads work into those hours. Pre-cool your house in the cool morning air, then coast through the expensive afternoon. Pre-heat before dawn while rates are low, then ride the thermal mass into the day. Your home stays just as comfortable — the optimizer simply chooses *smarter hours* to do the work, saving energy, money, and carbon in the process.

## How It Works

The optimizer runs a physics-based thermal model of your home (think: insulation quality, thermal mass, HVAC capacity) that it learns automatically from your thermostat readings using a Kalman filter. Three control layers work together:

1. **Strategic planner** re-optimizes your setpoint schedule every 1–4 hours based on weather forecasts, electricity rates, and grid carbon intensity.
2. **Tactical controller** checks reality against the model every 5 minutes and nudges the setpoint if your house is drifting off-plan.
3. **Watchdog** detects manual thermostat changes, mode switches, and sensor failures — and responds gracefully.

The result: your HVAC runs the same total amount (or less), but at better times.

## Features

- **Learns your home automatically** — An Extended Kalman Filter estimates your building's thermal envelope, mass, and HVAC capacity from thermostat data alone. Or import a [Beestat](https://beestat.io/) profile to skip the learning period.
- **Forecast-driven scheduling** — Builds an optimized setpoint schedule using hourly weather forecasts, with optional electricity rate and carbon intensity awareness.
- **Counterfactual savings tracking** — A digital twin simulates what your thermostat *would* have done without optimization, then decomposes savings into runtime reduction, COP improvement, rate arbitrage, and carbon shifting.
- **Occupancy-aware** — Widens your comfort range when you're away, with calendar integration and pre-conditioning before you arrive home.
- **Room-aware sensing** — Weights indoor temperature by room occupancy instead of averaging all sensors equally.
- **Demand response ready** — Temporarily widen comfort bounds in response to grid signals, energy managers, or your own automations.
- **45+ diagnostic sensors** — Full transparency into model state, predictions, savings breakdowns, and confidence levels.

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

The config flow walks you through three steps:

| Step | What you'll configure |
|------|----------------------|
| **Equipment** | Your thermostat + one or more weather entities (first is primary, rest are fallbacks) |
| **Thermal Model** | How to initialize — learn from scratch, import a Beestat profile, or restore an exported model |
| **Temperature Boundaries** | Comfort range (where the optimizer works) and safety limits (never exceeded) |

After setup, open **Configure** on the integration card for advanced options: sensors, energy tracking, optimizer tuning, occupancy, calendars, and room-aware sensing.

### What to Expect

| Timeline | What's happening |
|----------|-----------------|
| **Day 1** | Optimization begins with conservative setpoint shifts. The model starts collecting observations but has no confidence yet. |
| **Week 1** | Baseline capture completes (requires 7 days minimum). Model is learning basic thermal characteristics but needs more weather variety. |
| **Week 2–3** | If you've seen a reasonable temperature range, the model starts producing useful estimates. Savings tracking moves beyond rough approximations. |
| **Month 1–2+** | Model confidence grows as it observes different weather patterns. Full calibration depends on seeing enough temperature variety — mid-season with stable weather takes longer than a shoulder season with swings. |

> **Tip:** Importing a Beestat profile gives the model a head start with measured thermal data, but the baseline still needs 7 days to capture your routine. Restoring a previously exported model is immediate.

> **History bootstrap:** On first setup, the integration loads up to 10 days of thermostat and weather history from Home Assistant's recorder (configurable via `history_bootstrap_days`). If sufficient data exists, this can significantly reduce or skip the cold-start learning period.

### Dashboard

After installation, a **Heat Pump** tab appears in the sidebar (left menu). It shows:

- **Status** — current phase, setpoint, next action, schedule
- **Savings Today** — energy, cost, CO₂, and COP improvement
- **All Time** — cumulative savings since installation
- **Model Learning** — progress bar, confidence %, and accuracy tier

The dashboard updates in real-time. No configuration needed — it discovers your optimizer entities automatically.

> All sensors are also available as standard HA entities for use in your own dashboards and automations.

## Configuration

### Initialization Modes

| Mode | Description | Time to calibrate |
|------|-------------|-------------------|
| **Learn automatically** | Starts with conservative defaults and learns from thermostat readings | Weeks to months, depending on weather variety |
| **Import Beestat profile** | Uses measured temperature deltas from a [Beestat](https://beestat.io/) export; Kalman filter continues refining | Faster start, but still needs 1–2 weeks + weather variety |
| **Restore exported model** | Loads a previously exported model via the `export_model` service | Immediate |

### Optional Sensors

These aren't required, but each one improves accuracy or unlocks additional features:

| Sensor | What it improves |
|--------|-----------------|
| Outdoor temperature | Direct measurement instead of forecast-derived values |
| Outdoor humidity | Wind chill and wet-bulb adjustments for COP modeling |
| Wind speed | Infiltration-adjusted heat loss estimates |
| Solar irradiance | Solar gain modeling for better predictions |
| Barometric pressure | Atmospheric pressure corrections to COP |
| Indoor temperature (multi) | Room-weighted averages instead of a single thermostat reading |
| Indoor humidity | Humidity-adjusted apparent temperature for comfort |
| HVAC power | Actual power draw for energy accounting (otherwise uses default wattage) |
| Solar production | Net energy calculations — subtract self-consumed solar |
| Grid import | Track grid-purchased energy separately |
| CO2 intensity | Carbon-aware optimization — shift runtime to cleaner grid hours |
| Electricity rate | Cost-aware optimization — shift runtime to cheaper hours |

### Comfort and Safety Ranges

The optimizer saves energy by shifting *when* your HVAC runs within a comfort range you define:

- **Comfort range** (e.g., 70–78°F for cooling) — The optimizer pre-cools toward one end during efficient hours and lets the house coast toward the other end during inefficient hours. A wider range = more savings.
- **Safety limits** (e.g., 50°F min, 85°F max) — Absolute guardrails that are never exceeded, even when away or during demand response.

### Time-of-Use Rate Schedule

If your utility charges different rates at different times, configure a TOU schedule in the Energy options step:

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

Hours use 0–23. The first matching period wins. Unmatched hours fall back to `electricity_flat_rate` or the rate entity.

### Calendar and Occupancy

The optimizer can use a calendar entity to predict when you're home or away:

- Events matching **home keywords** (e.g., "WFH", "Remote") → you're home
- Events matching **away keywords** (e.g., "Office", "In-Person") → you're away
- The optimizer widens the comfort range when away and pre-conditions before your expected return

#### Departure-Aware Pre-conditioning

For more precise timing, add these optional entities in the Schedule options step:

- **`departure_zone`** — The HA zone to monitor (typically `zone.home`)
- **`travel_time_sensor`** — A sensor providing commute time in minutes (e.g., from Waze or Google Maps)
- **`departure_trigger_window_minutes`** — How far before a calendar "away" event to start monitoring departure (default 60 min)

When configured, if a calendar event shows "Office" at 9:00 AM and the travel time sensor reads 25 minutes, the optimizer pre-conditions so the house is comfortable through ~8:35 AM, then widens comfort bounds when you leave the zone.

### Room-Aware Sensing

When multiple indoor temperature sensors are configured with area assignments:

| Mode | Behavior |
|------|----------|
| **Equal** | All sensors averaged equally (default) |
| **Occupied only** | Only rooms with detected motion contribute |
| **Weighted** | Occupied rooms get higher weight (default 3×), unoccupied rooms still contribute |

---

<details>
<summary><strong>Entity Reference</strong> — 45+ sensors, binary sensors, and switches</summary>

### Sensors

#### Control

| Entity | Description | Unit |
|--------|-------------|------|
| Current Phase | Optimizer phase (pre-cooling, coasting, maintaining, idle, paused, safe_mode) | — |
| Target Setpoint | Current desired thermostat setpoint | °F |
| Next Action | Human-readable next scheduled action | — |
| Schedule | Schedule entry count (full schedule in attributes) | — |
| Learning Progress | Human-readable learning status (e.g. "Day 5 of ~14: Capturing baseline") | — |

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

#### Savings (Daily)

| Entity | Description | Unit |
|--------|-------------|------|
| Energy Saved Today | Total energy saved vs baseline | kWh |
| Cost Saved Today | Total cost saved vs baseline | $ |
| CO2 Avoided Today | Total carbon avoided vs baseline | g |
| Baseline Energy Today | Counterfactual baseline energy usage | kWh |
| Worst Case Energy Today | Theoretical maximum (HVAC always on) | kWh |

#### Savings (Cumulative)

| Entity | Description | Unit |
|--------|-------------|------|
| Energy Saved Cumulative | All-time energy savings | kWh |
| Cost Saved Cumulative | All-time cost savings | $ |
| CO2 Avoided Cumulative | All-time carbon avoided | kg |

#### Savings Decomposition

| Entity | Description | Unit |
|--------|-------------|------|
| Runtime Savings Today | Energy saved from fewer runtime minutes | kWh |
| COP Savings Today | Energy saved from better compressor efficiency | kWh |
| Rate Savings Today | Cost saved from cheaper electricity hours | $ |
| Carbon Shift Savings Today | CO2 avoided from cleaner grid hours | g |
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
| Optimizer Enabled | Master enable/disable switch — turning off pauses optimization and stops writing setpoints |

</details>

<details>
<summary><strong>Services</strong></summary>

### `heatpump_optimizer.force_reoptimize`

Immediately re-run the schedule optimizer with the latest forecast.

```yaml
service: heatpump_optimizer.force_reoptimize
```

### `heatpump_optimizer.pause`

Pause optimization and stop writing setpoints. The thermostat holds its current setpoint.

```yaml
service: heatpump_optimizer.pause
```

### `heatpump_optimizer.resume`

Resume optimization after a pause. Triggers an immediate re-optimization.

```yaml
service: heatpump_optimizer.resume
```

### `heatpump_optimizer.set_occupancy`

Override occupancy detection with a specific mode.

```yaml
service: heatpump_optimizer.set_occupancy
data:
  mode: away  # home, away, vacation, or auto
```

### `heatpump_optimizer.demand_response`

Activate demand response mode to temporarily reduce HVAC load.

```yaml
service: heatpump_optimizer.demand_response
data:
  mode: reduce  # reduce or restore
  duration_minutes: 60
```

### `heatpump_optimizer.export_model`

Export the learned thermal model as JSON for backup or transfer.

```yaml
service: heatpump_optimizer.export_model
```

### `heatpump_optimizer.import_model`

Import a previously exported model.

```yaml
service: heatpump_optimizer.import_model
data:
  model_data: { ... }  # JSON from export_model
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
| `heatpump_optimizer_accuracy_tier_changed` | Savings accuracy tier upgraded (learning → estimated → simulated → calibrated) |
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

The integration maintains a **counterfactual digital twin** — a parallel simulation of what your thermostat would have done without optimization, running against the same actual weather conditions.

Each hour, it compares the optimizer's actual HVAC behavior against this baseline:

- **Runtime savings** — Energy saved from running fewer total minutes while maintaining comfort
- **COP savings** — Energy saved because the optimizer shifted runtime to outdoor temperatures where the heat pump is more efficient (better coefficient of performance)
- **Rate arbitrage** — Cost saved by running during cheaper electricity hours (requires rate sensor or TOU schedule)
- **Carbon shifting** — CO2 avoided by running during hours when the grid is cleaner (requires CO2 intensity sensor)

Savings accuracy improves as the integration learns:

| Tier | When it triggers | What it means |
|------|-----------------|---------------|
| Learning | Days 0–7 | Baseline still being captured — savings shown are worst-case estimates only |
| Estimated | ~Day 7+ | Baseline captured, but model confidence is still low — rough ratio-based estimates |
| Simulated | ~Week 2+ | Both baseline and model have some confidence — counterfactual digital twin is active |
| Calibrated | Weeks to months | Model and baseline both high-confidence — requires observing a range of weather conditions |

</details>

<details>
<summary><strong>Architecture</strong></summary>

### Three-Tier Control

1. **Strategic planner (Layer 1)** — Runs every 1–4 hours. Fetches the weather forecast, generates an optimized setpoint schedule, and decides when to re-optimize (forecast changed significantly, occupancy changed, mode switch needed).

2. **Tactical controller (Layer 2)** — Runs every 5 minutes. Compares the thermal model's predicted indoor temperature against reality. If they diverge by more than 1°F, applies a damped correction to the scheduled setpoint. Detects "disturbed" states (window open, large party) when error exceeds 2°F.

3. **Watchdog controller (Layer 3)** — Event-driven. Listens for thermostat state changes to detect manual overrides, mode changes, and thermostat unavailability. Triggers a grace period on override detection before resuming optimization.

### Thermal Model

Two-node RC thermal circuit:

```
C_air  * dT_air/dt  = (T_out - T_air)/R + (T_mass - T_air)/R_int + Q_hvac + Q_solar
C_mass * dT_mass/dt = (T_air - T_mass)/R_int
```

Where:
- `R` — Envelope thermal resistance (insulation quality)
- `R_int` — Air-to-mass coupling (internal surfaces)
- `C_air` — Air thermal capacitance
- `C_mass` — Thermal mass (walls, slab, furniture)
- `Q_hvac` — HVAC heat output (temperature-dependent, models COP degradation)
- `Q_solar` — Solar heat gain

An Extended Kalman Filter estimates 8 state parameters online:

```
[T_air, T_mass, 1/R, 1/R_int, 1/C_air, 1/C_mass, Q_cool_base, Q_heat_base]
```

### Schedule Optimization

Two methods are available:

- **Work-based heuristic** (default) — Scores each hour by HVAC efficiency at the forecasted outdoor temperature. Shifts runtime from inefficient hours to efficient hours. Fast, no external dependencies.

- **Grey-box LP optimizer** (optional) — Formulates HVAC scheduling as a linear program: minimize energy consumption subject to hourly comfort bounds. Propagates parameter uncertainty to tighten comfort margins when the model is less confident. Enable via `use_greybox_model` in the Behavior options step. Generally produces more aggressive (higher-savings) schedules once the thermal model is confident, but automatically falls back to the heuristic if the LP solver fails.

### Data Flow

```
Climate Entity (thermostat)
    |
ThermostatAdapter (read state, detect overrides, write setpoints)
    |
Coordinator (5-min update cycle)
    |-- SensorHub (weather, occupancy, power, solar)
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
| Savings show 0 kWh | Baseline capture hasn't completed (needs 7 days) | Wait for the `baseline_complete` event; check the Learning Progress sensor |
| Model confidence stuck at 0% | Not enough weather variety observed | Needs outdoor temperature swings; importing a Beestat profile helps bootstrap |
| Thermostat setpoint not changing | Optimizer is paused or switch is off | Check the Optimizer Enabled switch and Current Phase sensor |
| "Safe mode entered" event | Forecast data is stale (>6 hours old) | Verify weather entity is updating; check Source Health sensor |
| Override detected repeatedly | Someone is adjusting the thermostat manually | Increase `override_grace_period_hours` in Behavior options |
| Dashboard not appearing | Panel registration failed on startup | Restart Home Assistant; check logs for frontend registration errors |
| Sensors show "unknown" | Coordinator hasn't completed its first update | Wait 5 minutes after restart; check integration logs |

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
