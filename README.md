# Heat Pump Optimizer

[![HACS][hacs-badge]][hacs-url]
[![GitHub Release][release-badge]][release-url]
[![License][license-badge]][license-url]

A Home Assistant integration that learns the thermal behavior of your home and uses weather forecasts to schedule your heat pump more efficiently. It shifts *when* your HVAC runs — pre-heating or pre-cooling during favorable conditions and coasting through unfavorable ones — while staying within a comfort range you define.

Works with any thermostat Home Assistant can control (Ecobee, Nest, Z-Wave, generic climate entities), including dual-setpoint thermostats that accept separate heat and cool targets.

## Background

A heat pump's efficiency depends on the outdoor temperature. In mild conditions it may deliver 3 to 4 units of heating or cooling per unit of electricity; at a summer peak or on a cold winter morning that ratio falls, and below the heating balance point resistance strip heat takes over at roughly 1:1.

Most thermostats don't account for this. They react to the current temperature and run whenever the house drifts outside the setpoint, regardless of what the next twelve hours look like. This integration reads the forecast, works out which hours are cheapest to run in, and prefers those hours when scheduling, always inside the comfort range you define.

**How large is the effect, honestly?** Smaller than it first looks, and worth stating plainly rather than discovering later. The only thing a thermostat can observe is the *net* indoor temperature change while the HVAC runs, and that number mixes the machine's output with the envelope load pushing back at the same moment. In one measured 365-day profile, cooling moved the house about 3.2°F/hr at 75°F outdoor and about 2.2°F/hr at 96°F, which reads as a 1.5x efficiency spread. But passive drift at those same outdoor temperatures was +0.9°F/hr and +1.8°F/hr, so the compressor's own contribution was roughly 4.1°F/hr and 4.0°F/hr: nearly flat. Most of the apparent spread is the building, not the heat pump.

Pre-conditioning is also not free. Holding the house further from outdoor conditions raises the loss rate for as long as you hold it, and stored coolth or heat bleeds away on the order of the building's thermal time constant, which is a handful of hours for a typical home rather than a full day. Shifting runtime pays clearly when it displaces something genuinely expensive (resistance strip heat below the balance point, or an expensive time-of-use window) or when it buys comfort on a day the system would otherwise saturate. On a flat electricity rate, expect single-digit percent energy savings, with most of the value coming from not wasting energy on pointless setpoint swings.

## How It Works

The integration learns how your HVAC system actually performs by recording observed temperature change rates across outdoor temperatures and solar conditions. This empirical performance profiler -- the primary thermal model -- builds lookup tables from your thermostat data, binned by outdoor temperature and solar condition (sunny, cloudy, night). On first setup, a history bootstrap replays up to 10 days of recorder data through the profiler, so it can reach initial confidence within hours rather than weeks.

From the profiler's measured passive-drift trendline the integration also derives a coarse thermal characterisation of the house: a drift slope, the time constant that follows from it, and an envelope conductance. These are single-node approximations and are labelled as such. Earlier versions ran a 9-state Extended Kalman Filter alongside the profiler; it was removed because nine parameters cannot be identified from one observed temperature, so its estimates random-walked instead of converging.

Three control layers run on top:

1. **Strategic planner** -- Re-optimizes the setpoint schedule every 1-4 hours from the weather forecast. Electricity rate and grid carbon intensity enter the hour ranking when you have configured those sources; on a flat tariff they change nothing, by construction.
2. **Tactical controller** -- Checks reality against the model every 5 minutes and nudges the setpoint if the house is drifting.
3. **Watchdog** -- Detects manual thermostat changes, mode switches, and sensor failures.

### Performance Profiler

The profiler accumulates observed HVAC performance into solar-aware 2D lookup tables -- keyed by `(outdoor_temp, solar_condition)` -- across four modes: compressor cooling, compressor heating, auxiliary heat, and passive drift (no HVAC). Solar condition is classified from the attic temperature sensor: sunny when the attic exceeds outdoor by more than 10F, cloudy at 7-10F above outdoor, and night otherwise.

The rates it records are **net**: they include whatever the envelope was doing during the same interval. Subtracting the passive drift rate at the same outdoor temperature and solar condition recovers the machine's own contribution, which is far flatter across the outdoor range than the net rates imply. See [Background](#background) for a worked example. Treat a large net spread as mostly a statement about the building.

**Confidence** is the geometric mean of three factors per mode: coverage (fraction of the expected outdoor temp range with data), depth (total observation hours vs a 48-hour minimum), and quality (fraction of temperature bins with 6+ samples). Overall confidence is the minimum across modes with at least 30 observations. The profiler rejects outliers (temperature changes greater than 2F per 5-minute interval or rates exceeding 15F/hr) and skips observations while auxiliary appliances are active.

The number shown on the dashboard is lightly smoothed by a high-water mark, so it does not twitch every time a brand-new outdoor temperature bin opens with a single sample. That smoothing is bounded: the displayed value can never sit more than 10 percentage points above the raw measurement, so it follows real drops down. Activation tiers and every other behavioral decision read the raw value directly, never the smoothed one.


## Features

- **Automatic learning** -- Performance profiler builds empirical lookup tables of your HVAC's actual temperature change rates, binned by outdoor temperature and solar condition. History bootstrap replays recorder data on first startup for immediate progress. Lookups are clamped to the range the house has actually been observed in, so the model never extrapolates a rate it has no evidence for.
- **Graduated activation** -- Starts optimizing conservatively once the profiler has enough data for current conditions, rather than waiting for full model convergence. Four tiers: learning, conservative, standard, confident.
- **Forecast-driven scheduling** -- Hourly weather forecasts, with optional electricity rate and carbon intensity awareness.
- **Savings tracking** -- A counterfactual digital twin simulates what your thermostat would have done without optimization, using the profiler's measured performance data. Decomposed into runtime, COP, rate, and carbon components.
- **Occupancy-aware** -- Widens comfort range when away, with calendar integration, pre-conditioning before arrival, and optional thermostat profile control (home/away/sleep).
- **Room-aware sensing** -- Weights indoor temperature by room occupancy instead of averaging all sensors equally. Includes blend mitigation (detects and compensates for Ecobee/Nest satellite sensor blending) and humidity squelch (suppresses cooling setpoint changes when high humidity would trigger unnecessary dehumidification).
- **House thermal load breakdown** -- Diagnostic sensors decompose the total passive thermal load on your house into individual components: weather heat transfer, solar heat gain, occupancy heat, and boundary zone (attic/crawlspace) effects. See what's actually driving your home's temperature, not just what the HVAC is doing about it.
- **Temperature profile chart** -- Observation coverage visualization showing where the profiler has data and where gaps remain, with per-mode trendlines and observation density by outdoor temperature.
- **Per-mode confidence** -- Profiler confidence broken down by HVAC mode (cooling, heating, aux heat, passive drift) with coverage, depth, and quality metrics.
- **Auxiliary appliances** -- Model thermal impacts of other equipment (heat pump water heaters, dryers, ovens) so the profiler skips contaminated observations rather than recording them as building behaviour.
- **Aux/emergency heat awareness** -- Automatically learns your heat pump's baseline power draw and derives the resistive strip's BTU contribution, so strip runtime is attributed to the strip rather than to the compressor.
- **Sleep schedule** -- Configure tighter nighttime comfort bounds with separate heating and cooling targets, active during a customizable sleep window.
- **Demand response** -- Temporarily widen comfort bounds via service call or automation.
- **Tier-aware dashboard** -- Custom sidebar panel adapts its layout to your current learning stage: live observation cards and a retrospective chart during learning mode; savings, forecast, and thermal profile views once calibrated.
- **Diagnostic sensors** -- 50+ entities exposing model state, thermal load breakdown, predictions, savings breakdowns, and confidence levels.

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

The config flow walks through five screens:

| Step | What you'll configure |
|------|----------------------|
| **Equipment** | Thermostat, weather entities, HVAC specs (tonnage, square footage, SEER) |
| **Temperature Sensors** | Indoor and outdoor temperature/humidity sensor entities |
| **Presence** | Occupancy entities for away detection (optional, can skip) |
| **Thermal Model** | Learn automatically or restore a previously exported model |
| **Temperature Boundaries** | Comfort range (where the optimizer works) and safety limits (never exceeded) |

After setup, open **Configure** on the integration card for advanced options: energy tracking, optimizer tuning, calendars, sleep schedule, blend mitigation, room-aware sensing, and auxiliary appliances.

### What to Expect

| Timeline | What's happening | Dashboard shows |
|----------|-----------------|-----------------|
| **Hours 0-6** | History bootstrap replays up to 10 days of recorder data through the profiler. If sufficient history exists, the profiler can reach initial confidence within hours. | Learning mode layout: retrospective 48h chart, three learning-progress cards (thermal model, baseline schedule, profiler), milestone checklist, today's snapshot |
| **Days 1-3** | Profiler accumulates live observations across current weather conditions. Conservative optimization may begin as soon as the profiler has enough coverage for the active HVAC mode. Baseline capture runs in the background. | Conservative activation: narrowed working band, savings tracking begins |
| **Days 4-14** | Profiler coverage grows as it observes more outdoor temperatures and solar conditions. Activation tier upgrades from conservative to standard. | Standard optimization: full comfort band, full savings panel |
| **Week 2+** | Full confidence as the profiler covers the current season's conditions. Tier upgrades to confident. | Full optimization: aggressive pre-conditioning, rate arbitrage, thermal profile views |

> **Tip:** Providing HVAC tonnage and home square footage during setup improves energy accounting accuracy from day one. Restoring a previously exported model is immediate.

> **History bootstrap:** On first setup, the integration loads up to 10 days of thermostat and weather history from Home Assistant's recorder. This is the primary mechanism for fast cold-start -- the profiler can reach initial confidence from historical data alone, often within hours of setup.

### Graduated Activation

Rather than waiting for a single global confidence score to reach a fixed threshold, the optimizer uses **graduated activation** to provide value sooner while being honest about what it knows:

| Tier | Requirements | Behavior |
|------|-------------|----------|
| **Learning** | Profiler accumulating initial observations. History bootstrap may complete this within hours. | Passive observation only. No thermostat changes. |
| **Conservative** | Profiler passive drift confidence >= 70% + at least 30 HVAC observations in active mode | Optimization active, but restrained: the optimizer works inside the inner 80% of your configured comfort range instead of the whole band. No aggressive pre-conditioning. Shorter lookahead window. |
| **Standard** | Profiler per-mode confidence >= 50% for the current HVAC mode | Full optimization with normal comfort bands. |
| **Confident** | Profiler overall confidence >= 70% | Aggressive pre-conditioning, rate arbitrage, full forecast lookahead. |

**Why does the conservative tier use a *narrower* band?** A wide comfort range is exactly what gives the optimizer room to pre-condition: the further a setpoint may sit from the middle of your range, the more aggressively it can push the house around. Restricting the optimizer to the inner 80% of your range is therefore the direct lever on how much it is allowed to do while the model is still thin. Your configured comfort range is never exceeded in any tier; the conservative tier simply declines to use all of it.

**Why graduated activation?** The profiler builds confidence by accumulating observations across outdoor temperature bins and solar conditions. It reaches useful confidence for current conditions quickly -- often within hours via history bootstrap -- because it only needs to cover the temperature ranges you're actually experiencing. Modes for conditions the home hasn't seen (e.g., cooling capacity in spring, aux heat in summer) are excluded from the confidence calculation until they accumulate enough data.

**Condition matching:** The optimizer only operates within temperature ranges it has observed. If outdoor conditions fall outside the profiler's data range for the current HVAC mode, the system automatically falls back to conservative behavior. The temperature profile chart in the dashboard shows exactly which conditions the model covers.

**Per-mode confidence:** The profiler tracks four modes independently: cooling, heating, auxiliary heat, and passive drift. Confidence per mode is the geometric mean of coverage (fraction of expected outdoor temp range with data), depth (total hours vs a 48-hour minimum), and quality (bins with 6+ samples). This means the profiler can be confident about cooling performance without needing any heating data, and vice versa.

### Dashboard

A **Heat Pump** tab appears in the sidebar after installation. Its layout adapts to your current learning stage automatically. No configuration needed — it discovers your optimizer entities from Home Assistant.

#### Learning mode (Days 0–7+)

While the system is building its baseline, the panel shifts focus from forecasting to observing:

- **Retrospective chart** -- 48h of actual indoor and outdoor temperatures with HVAC on/off shading. The "Model" trace shows where the active plan expected indoor temperature to be (diagnostic). The separate "Forecast" trace (hollow circles, right of the "Now" line) shows forward-looking simulation using the profiler's measured performance data.
- **Three progress cards** -- Performance Profiler (per-mode confidence with coverage, depth, and quality breakdown), Thermal Model (time constant, drift slope, and seeded thermal mass), and Baseline Schedule (day-of-week dot grid showing which days have been captured).
- **Temperature profile chart** -- Scatter/density chart showing observed indoor temperature change rate vs outdoor temperature, colored by HVAC mode (heating, cooling, passive drift). Trendlines drawn only within the observed temperature range. Shows at a glance where the profiler has data and where gaps remain.
- **Milestone checklist** — Step-by-step progress from "sensors connected" through "full optimization enabled." Shows which step is currently in progress.
- **Today's Snapshot** — Objective energy facts for the day (estimated kWh, current draw, outdoor temp range, aux heat if any). No savings comparisons until the baseline completes.

The optimizer phase badge shows **Learning** during this period (not "Idle") to indicate the system is actively collecting data. When learning stalls due to stable temperatures, the learning progress sensor explains: "No insights gained at stable temps. Progress will resume with fresh, unobserved conditions."

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
| **Learn automatically** | Starts with conservative defaults and learns from thermostat readings. Providing tonnage and sqft reduces convergence from weeks to days. History bootstrap replays recorder data on first startup for immediate progress. |
| **Restore exported model** | Loads a previously exported model via the `export_model` service (immediate) |

### Optional Sensors

None of these are required. Each one improves accuracy or unlocks additional features:

| Sensor | What it improves |
|--------|-----------------|
| Outdoor temperature | Direct measurement instead of forecast-derived values |
| Outdoor humidity | Wind chill and wet-bulb adjustments for COP modeling |
| Wind speed | Infiltration-adjusted heat loss estimates |
| Solar irradiance | Direct irradiance measurement for solar gain (highest priority in irradiance hierarchy) |
| UV index | Smooth, hourly irradiance proxy — blended with cloud cover for more accurate solar modeling than cloud cover alone |
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
| Supply duct temperature | Native aux heat detection for thermostats that don't report it (e.g., Ecobee via HomeKit). See below |
| Door/window contact sensors | Infiltration scaling -- the profiler skips observations while open, to avoid recording an open window as building behaviour |

### Comfort and Safety Ranges

- **Comfort range** (e.g., 70–78°F for cooling) — The optimizer works within this band, pre-cooling toward one end during efficient hours and coasting toward the other. Wider range = more flexibility.
- **Safety limits** (e.g., 50°F min, 85°F max) — Absolute guardrails, never exceeded.

### HVAC System Specifications

The integration asks for a few optional system specs during initial setup. Every field can be left blank -- the profiler converges quickly regardless. Providing them seeds the energy accounting with accurate values from day one, and floor area seeds the thermal-mass figure used to convert appliance heat into degrees per hour.

| Field | Where to find it | Effect |
|---|---|---|
| **Home conditioned area (sq ft)** | Listing, property tax record, or floor plan | Seeds air thermal capacitance (`C_air`); a 1,000 ft² condo and a 4,000 ft² house have 4× different thermal mass |
| **System tonnage** | Outdoor unit nameplate, HVAC permit, or installer paperwork | Derives rated power draw for energy accounting |
| **Aux / emergency heat type** | Thermostat wiring label (`W2`/`E`), HVAC documentation, or air handler label | `electric_strip` enables strip-heat BTU accounting when no power sensor is present (see below); `gas`/`oil` flags heat as non-electric for cost modeling |
| **Aux heat capacity (kW)** | Air handler nameplate (e.g., "10 kW" strip kit) | Provides an accurate BTU estimate for each aux heating interval when no circuit power meter is configured |

**Advanced -- available in the Energy options step after initial setup:**

| Field | Where to find it | Effect |
|---|---|---|
| **SEER / SEER2 rating** | Unit nameplate, EnergyGuide label, or manufacturer spec sheet | Combined with tonnage, derives rated power draw: `W_rated = (tons × 12,000) / SEER`. Overrides the 3,500 W flat default for all cost and savings calculations |
| **Rated watts (override)** | Clamp meter reading, or nameplate if labeled | Direct override of the derived estimate; highest priority in the power chain |
| **Cooling power draw** | Clamp meter reading during cooling | Measured power during cooling mode. Overrides the spec-derived default. Useful if you've measured actual draw and want to account for resistive losses or duct leakage |
| **Heating power draw** | Clamp meter reading during heating | Measured power during heat pump heating mode. If not set, defaults to 115% of cooling watts |

**Mode-aware power estimation** -- When no live HVAC power sensor is configured, the integration estimates power draw based on the current HVAC mode:

| HVAC Mode | Power Source |
|---|---|
| Cooling | Cooling watts (configured, or derived from tonnage/SEER) |
| Heating (heat pump) | Heating watts (configured, or cooling watts x 1.15) |
| Aux/emergency heat | Aux heat capacity (kW) from system specs |
| Fan only | 300 W (blower fan only) |
| Idle/off | 0 W |

This replaces the need for a manual template sensor that maps `hvac_action` to wattage. If you have a clamp meter or smart plug providing a live power sensor, it always supersedes all estimates regardless of mode.

### Solar Panels

The **Solar Panels** configuration tab (Options → Solar Panels) lets you configure solar production entities and, optionally, panel specifications for deriving solar irradiance from production data.

| Field | Description |
|---|---|
| **Solar production sensor** | Entity reporting current panel output in watts |
| **Grid import sensor** | Entity reporting current grid import in watts (for net energy tracking) |
| **Solar export rate** | Entity reporting feed-in tariff rate in $/kWh |
| **Total panel area** | Combined surface area of all panels (auto-calculated if per-panel specs are provided). Displayed in your HA unit system (ft² or m²) |
| **Number of panels** | Count of installed panels |
| **Area per panel** | Surface area of a single panel |
| **Efficiency coefficient** | Panel conversion efficiency (0.05–0.35). Modern residential panels are typically 0.18–0.22 |

When both a production sensor and panel specs (area + efficiency) are configured, the optimizer derives solar irradiance: `W/m² = production / (area × efficiency)`. This derived irradiance is used in the [irradiance hierarchy](#irradiance-hierarchy) as priority 2 — better than UV/cloud estimates but superseded by a dedicated irradiance sensor if you have one.

If per-panel area and count are both provided, they take precedence over the total area field (total is auto-calculated on save).

### Auxiliary Appliances

Equipment that impacts indoor temperature -- such as a heat pump water heater extracting heat from conditioned air, or a dryer adding heat -- can be modeled so the profiler skips contaminated observations rather than recording an appliance cycle as building behaviour.

> **Aux/emergency heat (resistive strips)** is handled separately and requires no manual configuration. The integration automatically learns your heat pump's baseline power draw from observed non-aux heating intervals, then uses the difference between measured circuit watts and that learned baseline to compute how much heat the resistive strips are actually producing each interval. That derived BTU value (`Q_aux_resistive`) is accounted as a known exogenous load. See [Aux Heat Learner](#aux-heat-learner) in the Math section for the full derivation.

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

#### Zone-Aware Presence Detection

By default, the integration treats person entities as "home" only when they're in the HA `home` zone. If you have additional zones configured (e.g., a neighborhood zone for faster GPS-based detection), you can add their names in **Configure → Presence & Away → Additional home zone names**.

For example, adding `Lake Monticello` means `person.gerald` will be treated as home when they're in either the `home` zone or the `Lake Monticello` zone. This gives you faster, more reliable presence detection -- GPS updates can detect you entering a larger neighborhood zone before narrowing to the home zone.

Multiple zone names can be comma-separated (e.g., `Lake Monticello, Neighborhood`). Matching is case-insensitive.

#### Thermostat Profile Control

Many smart thermostats (Ecobee, Nest, Honeywell) have built-in comfort profiles -- home, away, sleep -- that apply their own setpoints and behaviors. Without this feature, you'd need a separate automation to switch profiles based on presence, which can conflict with the optimizer's setpoint control.

Enable **Thermostat Profile Control** in the options menu to let the integration manage your thermostat's comfort profile directly. The integration sets the profile based on occupancy state and time of day, then optimizes setpoints within that profile's context.

**How it works:**

| Condition | Profile set |
|-----------|-------------|
| Everyone leaves | Away |
| Someone arrives (before cutoff time) | Home |
| Someone arrives (after cutoff time) | Sleep |
| Sleep schedule active + someone home | Sleep |
| Sleep schedule active + nobody home | Away |

**Configuration** (in **Configure → Thermostat Profile Control**):

| Setting | Default | Description |
|---------|---------|-------------|
| **Profile control method** | Select entity | Controls profiles via a `select.*` entity (Ecobee) or via `climate.set_preset_mode` (other thermostats) |
| **Profile select entity** | -- | The entity that controls your thermostat's mode (e.g., `select.my_ecobee_current_mode`) |
| **Home/Away/Sleep profile names** | home/away/sleep | Map to your thermostat's actual profile names |
| **Arrival sleep cutoff** | 9:30 PM | Arrivals after this time get the sleep profile instead of home |
| **Override sleep to away** | Yes | When nobody is home during the sleep window, use away instead of sleep |
| **Away transition delay** | 10 min | If the HVAC is actively running when transitioning to away, wait this long to let the cycle complete |

Profile updates happen immediately when a person entity changes state (not just on the 5-minute update cycle), so transitions are responsive to GPS and zone events.

This replaces standalone automations that control your thermostat's comfort mode based on presence. The integration coordinates profile selection with its own setpoint optimization, eliminating conflicts between the two.

#### Departure-Aware Pre-conditioning

For more precise timing, add departure profiles in the Schedule options step pairing each person with:

- **Departure zone** -- The HA zone to monitor (typically `zone.home`)
- **Travel time sensor** -- Commute time in minutes (e.g., Waze or Google Maps)

When configured, if a calendar event shows "Office" at 9:00 AM and the travel sensor reads 25 minutes, the optimizer ensures comfort through ~8:35 AM, then relaxes when you leave.

### Room-Aware Sensing

When multiple indoor temperature sensors are configured with area assignments:

| Mode | Behavior |
|------|----------|
| **Equal** | All sensors averaged equally (default) |
| **Occupied only** | Only rooms with detected motion contribute |
| **Weighted** | Occupied rooms get higher weight (default 3×), unoccupied rooms still contribute |

### Sleep Schedule

If you prefer different temperatures at night, enable the sleep schedule in **Configure → Sleep Schedule**. During the configured sleep window, the optimizer uses tighter comfort bounds instead of the standard daytime range.

| Setting | Default |
|---------|---------|
| **Sleep start** | 10:00 PM |
| **Sleep end** | 7:00 AM |
| **Cooling range** | 70–76°F |
| **Heating range** | 60–66°F |

Sleep bounds only apply when someone is home. If the house is unoccupied during sleeping hours, the normal away-mode comfort range is used instead. When thermostat profile control is enabled, the integration also sets the thermostat's profile to "away" (not "sleep") when nobody is home during the sleep window.

### Wet Room Squelch

Bathrooms and laundry rooms cause large temperature spikes during showers or dryer use (typically +5–6°F lasting 3–4 hours). These spikes corrupt the thermal model if included in the indoor average.

**Wet Room Squelch** pairs a temperature sensor with a co-located humidity sensor. When humidity exceeds the activation threshold, the temperature and humidity sensors are excluded from indoor averages until humidity recovers.

| Parameter | Value |
|-----------|-------|
| **Activation threshold** | 65% RH |
| **Deactivation threshold** | 55% RH (hysteresis) |

Configure in **Configure → Advanced → Wet Room Squelch**. Select the temperature sensor to squelch and the humidity sensor that triggers squelching. When squelched, other indoor sensors and the thermostat continue contributing normally. If the squelched sensor is the only indoor sensor, the integration falls back to the thermostat reading.

---

<details>
<summary><strong>Entity Reference</strong></summary>

### Sensors

#### Control

| Entity | Description | Unit |
|--------|-------------|------|
| Current Phase | Optimizer phase (learning, pre-cooling, pre-heating, coasting, maintaining, idle, paused, safe_mode, preconditioning) | — |
| Target Setpoint | Current desired thermostat setpoint | °F |
| Next Action | Human-readable next scheduled action | — |
| Schedule | Schedule entry count (full schedule in attributes) | — |
| Learning Progress | Human-readable learning status | — |

#### Thermal Model

| Entity | Description | Unit |
|--------|-------------|------|
| Model Confidence | Profiler confidence level. Lightly smoothed for display; control reads the raw value | % |
| Envelope R-Value | Learned envelope thermal resistance | °F·hr/BTU |
| Thermal Mass | Learned building thermal mass | BTU/°F |
| Cooling Capacity | Learned base cooling output at 75°F reference | BTU/hr |
| Heating Capacity | Learned base heating output at 75°F reference | BTU/hr |
| Effective Cooling Rate | Equipment's own cooling forcing at the current outdoor temperature, envelope load removed | °F/h |
| Effective Heating Rate | Equipment's own heating forcing at the current outdoor temperature, envelope load removed | °F/h |
| Thermal Mass Temperature | Hidden thermal mass node temperature | °F |

#### Predictions and Diagnostics

| Entity | Description | Unit |
|--------|-------------|------|
| Predicted Temperature | Where the active plan expects indoor temperature to be now (diagnostic) | °F |
| Thermal Time Constant | How long the house holds a temperature offset, from the measured drift slope. Reads `unmeasured` until there is enough passive data | h |
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

#### House Thermal Load

| Entity | Description | Unit |
|--------|-------------|------|
| House Thermal Load | Net passive thermal load on the house (positive = gaining heat, negative = losing heat) | BTU/hr |
| Weather Heat Transfer | Heat flowing through walls, windows, and doors due to outdoor conditions and infiltration | BTU/hr |
| Solar Heat Gain | Heat entering from sunlight, scaled by cloud cover and sun elevation | BTU/hr |
| Occupancy Heat Gain | Heat from people, electronics, and lighting inside the house | BTU/hr |
| Boundary Zone Heat Transfer | Heat from attic and crawlspace into the living space | BTU/hr |

Each sensor includes a `model_confidence` attribute and a `reliable` flag (false when the thermal model is still learning). The House Thermal Load sensor includes a full component breakdown and a `direction` attribute ("gaining heat", "losing heat", or "balanced").

#### Aux/Emergency Heat

| Entity | Description | Unit |
|--------|-------------|------|
| Aux Heat Learned HP Watts | EMA-learned heat pump baseline power draw during non-aux heating intervals | W |
| Aux Heat Resistive BTU | Estimated resistive strip output (derived from circuit watts − learned HP watts) | BTU/hr |

#### Energy Consumption

| Entity | Description | Unit |
|--------|-------------|------|
| HVAC Energy Today | Actual HVAC energy consumed today (resets at midnight). Compatible with HA Energy Dashboard | kWh |
| HVAC Energy Total | All-time cumulative HVAC energy consumed. Monotonically increasing after learning completes | kWh |

These sensors are HA Energy Dashboard compatible (`device_class: energy`, proper `state_class`). To track weekly, monthly, or yearly consumption, wrap either sensor with HA's built-in [utility_meter](https://www.home-assistant.io/integrations/utility_meter/) integration -- no manual template sensors needed.

#### Savings (Measured)

These are the headline numbers, and the only ones with a measurement on both sides: metered HVAC energy against a degree-day regression fitted to your own optimizer-off days. No thermal model is involved in either term, so the arithmetic is checkable by hand from the `Energy Baseline Status` attributes. They stay unavailable until the regression has enough baseline days rather than reporting a modeled guess.

| Entity | Description | Unit |
|--------|-------------|------|
| Measured Savings Today | Metered energy today vs the weather-normalized baseline. Partial-day figure | kWh |
| Measured Savings 30 Days | Same comparison over recent whole days. This is the number to trust | kWh |
| Energy Baseline Status | Fitted kWh per cooling and heating degree-day, baseline day count, and days remaining until the fit is usable | — |

#### Savings (Modeled)

Everything below is a *modeled attribution*: it explains where the optimizer believes a difference came from, using a shadow-house simulation. It is not evidence that the difference happened, because the shadow house runs on the same thermal model as the optimizer being graded. Treat it as commentary on the measured figures above.

| Entity | Description | Unit |
|--------|-------------|------|
| Modeled Savings Today | Modeled energy saved vs the digital twin baseline | kWh |
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
| Modeled COP Effect | Modeled energy attributed to better compressor efficiency. No COP has ever been measured on this system | kWh |
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

The profiler builds lookup tables of observed HVAC performance (°F/hr change vs outdoor temperature) across four modes: cooling, heating, auxiliary heat, and passive drift (no HVAC). It needs observations spread across each mode's expected outdoor temperature range to become confident:

| Mode | Expected outdoor range | What it measures |
|------|----------------------|------------------|
| Cooling | 65–100°F | How fast the system cools at each outdoor temp |
| Heating | 0–55°F | How fast the system heats at each outdoor temp |
| Aux Heat | 0–35°F | Auxiliary/emergency heat performance |
| Passive Drift | 20–90°F | Natural temperature decay rate without HVAC |

Confidence per mode is the geometric mean of three factors: **coverage** (fraction of the expected temp range with sufficient data), **depth** (total observation hours vs a 48-hour minimum), and **quality** (fraction of temperature bins with 6+ samples each). Overall confidence is the minimum across modes with at least 30 observations — modes with fewer observations are excluded from the calculation and hidden from the card until they accumulate enough data. This prevents seasonal modes (e.g., aux heat in spring) from permanently dragging confidence to 0%.

The `Profiler Confidence` sensor reports the lightly smoothed value (see [Performance Profiler](#performance-profiler) for how the smoothing is bounded); per-mode confidences in its attributes are always raw.

The profiler builds coverage from live observations across all HVAC modes, outdoor temperature bins, and solar conditions (sunny, cloudy, night). As more varied weather is experienced, the profiler's confidence grows. The profiler is the only model driving schedule optimization.

| Entity | Description | Unit |
|--------|-------------|------|
| Profiler Confidence | Performance profiler confidence level (min across modes) | % |
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

### `heatpump_optimizer.start_ab_trial` / `stop_ab_trial` / `ab_trial_result`

Runs a controlled experiment: the optimizer is held on for a block of days, then off for a block, alternating, while metered energy per degree-day accumulates separately for each arm. `ab_trial_result` reports the standing.

This is the only measurement that can actually settle whether the integration helps in your house. It is deliberately capable of returning a loss or "no measurable difference": a verdict requires at least four days in each arm and a t-statistic of 2 or better. Pausing the optimizer yourself always takes precedence over a running trial.

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
| `heatpump_optimizer_confidence_reached` | Profiler crossed its confidence threshold |
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

The integration maintains a counterfactual digital twin -- a parallel simulation of what your thermostat would have done without optimization, using the profiler's measured performance data against the same actual weather.

The digital twin uses a dual-setpoint thermostat model (separate heat and cool setpoints, matching how real thermostats operate) with baseline setpoints derived from your comfort configuration and occupancy mode. Solar-condition-aware passive drift calculations ensure the counterfactual accurately captures sunny vs cloudy day differences.

Each hour, it compares the optimizer's actual behavior against this baseline:

- **Runtime savings** -- Fewer total runtime minutes while maintaining comfort
- **COP savings** -- Runtime shifted to outdoor temperatures where the heat pump is more efficient
- **Rate arbitrage** -- Runtime shifted to cheaper electricity hours (requires rate sensor or TOU schedule)
- **Carbon shifting** -- Runtime shifted to hours when the grid is cleaner (requires CO2 intensity sensor)

Savings accuracy improves over time:

| Tier | When | Panel layout | Meaning |
|------|------|-------------|---------|
| **Learning** | First hours-days | Retrospective chart, progress cards, milestone checklist, today's snapshot (no savings) | Profiler accumulating initial data; savings suppressed |
| **Estimated** | ~Day 1-3+ | Savings panel with +/-uncertainty label in amber, rough indoor forecast | Profiler reaching confidence; counterfactual active |
| **Simulated** | ~Days 3-7+ | Full savings panel + forecast, counterfactual digital twin fully operational | Profiler confident, accurate counterfactual |
| **Calibrated** | Week 2+ | Full panel including thermal profile card with position bars and narrative | Profiler high-confidence with a measured baseline |

</details>

<details>
<summary><strong>Architecture</strong></summary>

### Three-Tier Control

1. **Strategic planner (Layer 1)** — Runs every 1–4 hours. Fetches weather forecast, generates optimized setpoint schedule, decides when to re-optimize.

2. **Tactical controller (Layer 2)** — Runs every 5 minutes. Compares predicted indoor temperature against reality. Applies damped corrections when they diverge. Detects "disturbed" states (window open, large party) when error exceeds 2°F.

3. **Watchdog controller (Layer 3)** — Event-driven. Detects manual overrides, mode changes, and thermostat unavailability. Triggers a grace period on override before resuming.

### Thermal Model

**Primary: Performance Profiler** -- Empirical lookup tables of observed temperature change rates, binned by `(outdoor_temp, solar_condition)` across four HVAC modes. The profiler drives all scheduling decisions. See [Performance Profiler](#performance-profiler) in the Math section for details.

**Diagnostic: coarse thermal characterisation** -- A drift slope `k` read straight off the profiler's passive-drift trendline, the time constant `tau = 1/k` that follows from it, and an envelope conductance `UA = C * k` where `C` is seeded from floor area. Single-node approximations, reported as such. See [Thermal Characterisation](#thermal-characterisation) in the Math section.

### Schedule Optimization

**Flat by default.** The schedule sits at the comfort-band edge appropriate to the mode, which is what a plain thermostat does and is the right answer on most days. Pre-conditioning is a gated event, not a continuous mapping from an efficiency ranking onto the comfort band.

The gates are deliberately narrow:

- **Cooling** opens only when the unit is predicted to *saturate* -- when the flat plan's simulated coast leaves the comfort ceiling with the compressor already at 100%. It does not open because the compressor is more efficient earlier. On this house, closed-loop replay against ten days of real data showed deeper and longer pre-cooling losing energy every time, with comfort never at risk: stored coolth leaks away before the afternoon peak.
- **Heating** opens when hours below the heat balance point are coming, where compressor pre-heat displaces resistance strip runtime. This is where pre-conditioning has a real payoff.

When a gate opens, the window is chosen by simulating a small candidate set (start 2-6 hours before the event, depth 1-3°F into the band) and picking the best simulated outcome by comfort excursion first and energy second. The flat plan is always in the candidate set, so it wins whenever pre-conditioning would not pay.

Savings are quoted against the flat plan rather than a midpoint setpoint, so a normal day honestly reports 0.0%, and a saturation day where the planner spends energy to protect comfort can report slightly negative. Electricity rate and grid carbon enter the hour ranking when configured; on a flat tariff they change nothing.

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
    |-- PerformanceProfiler (primary model: empirical HVAC performance bins)
    |-- StrategicPlanner --> ScheduleOptimizer (profiler-driven heuristic)
    |-- TacticalController (drift correction)
    |-- WatchdogController (override detection)
    |-- CounterfactualSimulator (profiler-based digital twin)
    |-- SavingsTracker (energy/cost/CO2 accounting; uses learned HP baseline for aux kWh)
    |
Sensor Entities (50+)
```

</details>

<details>
<summary><strong>Troubleshooting</strong></summary>

| Problem | Likely cause | Fix |
|---------|-------------|-----|
| Savings show 0 kWh | Savings tier still in learning/projected phase | Wait for profiler confidence to build; check Learning Progress sensor |
| Profiler confidence not advancing | Not enough weather variety or HVAC cycling | Needs varied outdoor temperatures and HVAC runtime; history bootstrap accelerates this |
| Model confidence stuck at 0% | Profiler has not yet seen enough varied weather | Check that recorder history is available and that the outdoor temperature sensor is reporting; confidence grows fastest across a range of outdoor temperatures |
| Setpoint not changing | Optimizer paused or switch off | Check the Optimizer Enabled switch and Current Phase sensor |
| "Safe mode entered" event | Forecast data stale (>6 hours) | Verify weather entity is updating; check Source Health sensor |
| Override detected repeatedly | Manual thermostat adjustments | Increase `override_grace_period_hours` in Behavior options |
| Dashboard not appearing | Panel registration failed | Restart Home Assistant; check logs for frontend errors |
| Sensors show "unknown" | First update hasn't completed | Wait 5 minutes after restart; check integration logs |
| Thermal time constant reads `unmeasured` | Too few passive-drift observations, or the fitted slope is outside a credible range | Normal early on. Persistent cases usually mean the HVAC rarely turns off, or an open window is being recorded as drift; configure door/window sensors |

</details>

<details>
<summary><strong>FAQ</strong></summary>

### Do I actually need all the optional sensors, occupancy tracking, and appliance configuration?

No — the integration works with just a thermostat and a weather entity. That's a fully functional setup. But each additional data source removes a source of ambiguity that the model would otherwise have to guess at, and the impact varies considerably by category.

**What works fine without extra sensors:**
The performance profiler learns HVAC efficiency from thermostat readings alone and can reach initial confidence within hours via history bootstrap. Savings tracking uses a default 3,500W power estimate multiplied by HVAC runtime. The optimizer runs correctly on weather-only forecasts. Most homes reach meaningful optimization within days on the bare minimum configuration.

**What each category actually buys you:**

| Category | Value if missing | Value when added |
|----------|-----------------|-----------------|
| **Outdoor temp sensor** | Uses weather forecast (~1°F typical error, sometimes 3–5°F on calm nights) | Direct measurement eliminates the forecast bias during critical morning pre-heat/cool windows when forecast error is largest |
| **Outdoor humidity + wind speed** | Wind chill and infiltration corrections default to zero | Improves COP estimation during cold snaps (wind drives more infiltration) and enables wet-bulb corrections for cooling efficiency |
| **HVAC power sensor** | Energy estimates use 3,500W default × runtime | Actual energy accounting within ~5%; enables aux heat BTU learning; catches COP degradation (dirty filter, low refrigerant) as learned capacity drops |
| **Solar irradiance** | Optimizer uses forecast cloud fraction for solar load weighting | Provides real-time measured sky condition for better scheduling on variable-cloud days; improves profiler's solar-condition classification accuracy |
| **Room temp sensors + occupancy** | Single thermostat reading, uniform comfort band | Prevents over-conditioning unoccupied zones; room weighting means the optimizer maintains comfort where people actually are, not at the thermostat location |
| **Calendar / presence** | Comfort band stays constant | Enables pre-conditioning before arrival (you come home to comfort, not the start of a cycle) and relaxed setpoints during long away periods |
| **Auxiliary appliances** | HPWH, dryer, oven loads appear as unexplained temperature anomalies | Profiler bins stay clean; prevents false tactical corrections |
| **Electricity rate / TOU schedule** | Optimizer minimizes runtime only; ignores price variation | Shifts runtime to off-peak hours; rate arbitrage savings can exceed COP-shift savings on aggressive TOU plans |
| **CO₂ intensity sensor** | Carbon accounting uses a flat average | Shifts runtime to genuinely cleaner grid hours; meaningful for solar households with time-varying grid carbon |

**The honest answer on bare-minimum operation:**

A thermostat + weather entity gives you 70-80% of the value at 0% of the sensor complexity. The optimizer will shift runtime to efficient outdoor temperatures, pre-condition the house during mild mornings, and coast through peak hours -- all without any extra setup. The profiler converges within hours to days via history bootstrap, and energy estimates will carry +/-20-30% uncertainty instead of +/-5-10%.

The configuration that consistently moves the needle the most is an **HVAC circuit power sensor** (a clamp meter or smart breaker on the air handler circuit). It unlocks accurate energy accounting, aux heat BTU learning, and a direct signal for detecting equipment degradation. If you're only going to add one thing, that's it.

Occupancy and calendar integration matter most if your household has a consistent and irregular schedule — work-from-home some days, away others. For a home that's always occupied or always on a fixed schedule, a fixed comfort band works nearly as well.

Auxiliary appliances (especially a HPWH) are worth configuring if the appliance runs frequently and has a large thermal impact relative to your home's size. A 4,000 BTU/hr heat extraction from a HPWH in a small, well-insulated house will noticeably contaminate summer observations. In a large, leaky house it may be below the noise floor.

---

### What do attic and crawlspace temperature sensors actually do? Are they worth adding?

These are among the most impactful optional sensors for homes with unconditioned attics or crawlspaces, but they're easy to overlook because they don't appear in "smart home sensor" lists.

**What they do without sensors:**

Without configured sensors, the model omits the boundary heat transfer term entirely — it treats your ceiling and floor as perfect insulators. The derived envelope conductance then absorbs those loads: it reads as a leakier house than you have, because ceiling and floor conduction is being attributed to the walls. The profiler is unaffected -- it measures net rates whatever their cause -- so this is a diagnostic bias rather than a scheduling one, but it matters for homes where attic and floor loads are significant.

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

Without a sensor, both terms are zero. With a sensor, this load is accounted separately at every 5-minute interval instead of being folded into the envelope figure.

The difference is largest in summer, when an unventilated attic can reach 130–150°F. On a 95°F day with an indoor temp of 75°F, the unaccounted ceiling load from a 140°F attic is `50 × (140 − 75) = 3,250 BTU/hr` — roughly equivalent to a 1-ton ghost load that the model would otherwise try to explain with other parameters.

*2. Duct efficiency correction (attic sensor only)*

If your supply ducts run through the attic (common in forced-air systems), duct conduction loss scales with how far the attic deviates from indoor air temperature:

```
η_duct = max(0.5, 1 − 0.003 · |T_attic − T_air|)
```

At a 140°F attic with 75°F indoor air: `η_duct = max(0.5, 1 − 0.003 × 65) = 0.805` — the model treats the HVAC system as delivering only 80.5% of its rated capacity due to duct conduction losses. Without the attic sensor, `η_duct` defaults to 1.0 (no correction), so cooling runs longer than the model expects.

**Is it worth adding?**

For homes with a **vented attic and forced-air ducts in that attic**: absolutely yes. The summer duct correction alone can shift `Q_cool_base` estimates by 10–20%. The attic sensor is the single highest-impact sensor for homes built before 1990 with ductwork in the attic (which is the majority of forced-air homes in the US).

For homes with **conditioned attic space, spray-foam roof deck, or radiant/mini-split systems with no attic ducts**: the boundary term still applies, but the duct correction doesn't matter and the load is smaller. Still useful, but lower priority.

For homes with a **vented crawlspace**: the crawlspace sensor matters most in winter when the crawlspace can drop well below outdoor air temperature on calm, clear nights. An uninsulated floor over a 10°F crawlspace in a 70°F house represents `25 × (70 − 10) = 1,500 BTU/hr` of floor loss the model won't see without the sensor.

A cheap zigbee temperature sensor (under $15) placed in the attic and/or crawlspace is likely one of the highest-return-per-dollar hardware additions for this integration.

---

### How does solar gain work, and what do the attic, UV index, and irradiance sensors actually change about it?

Solar gain is now modeled as two components that sum together:

1. **Direct solar gain** (through windows and walls): `Q_solar_peak × f_irradiance × sin(elevation)`
2. **Solar via attic** (roof absorption conducted indoors): `K_attic × max(0, T_attic − T_outdoor)`

#### The irradiance fraction hierarchy {#irradiance-hierarchy}

The `f_irradiance` term (0.0–1.0) is computed from the best available data source, checked in this order:

| Priority | Source | Formula | When used |
|----------|--------|---------|-----------|
| 1 | Solar irradiance sensor (W/m²) | `min(1, irradiance / (1000 × sin(elev)))` | Direct pyranometer configured |
| 2 | Solar panel production | Same formula, using `production / (area × efficiency)` as irradiance | Panel specs configured in Solar Panels tab |
| 3 | UV index + cloud cover blend | `0.7 × UV_fraction + 0.3 × (1 − cloud_cover)` | UV index + weather entity available |
| 4 | UV index only | `UV_fraction` alone | UV index available, cloud cover unavailable |
| 5 | Cloud cover only | `1 − cloud_cover` | Weather entity only |
| 6 | Elevation only | `0.6` (reasonable average) | No weather data |

**Why UV index matters:** Cloud cover percentage measures *area coverage*, not *optical depth*. Thin high cirrus at 90% coverage barely attenuates solar radiation — UV index 4.58 has been observed at 90% cloud cover, nearly matching clear-sky levels. UV index directly measures the irradiance reaching the surface, making it a much better proxy.

#### Attic heat decomposition: solar vs weather

When an attic temperature sensor is configured and the sun is above the horizon, the optimizer decomposes attic heat transfer into two components:

- **Solar surplus**: `max(0, T_attic − T_outdoor)` — heat from the sun absorbed by the roof, conducted into the living space. Attributed to Q_solar.
- **Weather boundary**: the remainder — heat exchange driven by the outdoor-indoor temperature differential. Attributed to Q_boundary.

This decomposition means a sunny 85°F day with a 108°F attic is modeled differently from an overcast 85°F day with an 87°F attic. The first has significant solar-driven attic heating (23°F surplus); the second has almost none.

At night (sun below horizon), all attic heat transfer is attributed to Q_boundary as before.

**HVAC duct loss:** For systems with air handlers in unconditioned attics, duct losses caused by solar-heated attic air are tracked separately via the `duct_loss_solar_fraction` attribute. This is diagnostic — it does not change duct loss calculations, but shows what percentage of duct losses are solar-driven.

#### Solar panel-derived irradiance

If you have solar panels but no dedicated irradiance sensor, the optimizer can derive irradiance from your panel production data. In the **Solar Panels** configuration tab, enter:

- Total panel surface area (or per-panel area × number of panels)
- Panel efficiency coefficient (typically 0.18–0.22 for modern residential panels)

The optimizer computes: `irradiance_W_m² = production_watts / (panel_area_m² × efficiency)`

This derived irradiance feeds into the same hierarchy as a direct sensor reading (priority 2).

#### Sensor impact summary

| Configuration | Q_solar accuracy | Solar parameter convergence | Sensor attributes exposed |
|---|---|---|---|
| No optional sensors | Cloud cover only (f_irradiance = 1 − cloud%) | Slowest — `solar_gain_btu` entangled with attic heat | `irradiance_source: cloud` |
| UV index only | UV+cloud blend (much smoother) | Moderate | `irradiance_source: uv_blend` |
| Attic sensor only | Cloud cover, but attic solar properly separated | Faster — clean window/wall signal | `solar_via_attic_btu`, `attic_solar_portion_btu` |
| UV + attic | Best blend + proper decomposition | Fastest convergence | Full solar breakdown |
| Irradiance/panels + attic | Direct measurement + decomposition | Best possible | `irradiance_source: sensor` or `panel_derived` |

---

### The model uses estimates for solar gain, wind infiltration, occupancy heat, and boundary zone conductance. How accurate are these for my specific house?

The thermal model relies on about a dozen physics coefficients — wind infiltration per mph, attic ceiling conductance, COP degradation slope, heat per occupant, etc. — that are initialized to textbook averages from ASHRAE and building science literature. These are reasonable starting points, but every home is different.

**Why the defaults vary per home:**

Your home's wind exposure depends on orientation, tree coverage, and air sealing quality. A well-sealed new-build might see 10× less infiltration per mph than a drafty 1960s ranch. Attic conductance depends on your actual insulation R-value and attic ventilation — a spray-foamed attic at R-49 conducts far less heat than a blown fiberglass attic at R-13. COP degradation slopes are equipment-specific: a variable-speed heat pump degrades more gently than a fixed-speed unit. Even "350 BTU/hr per occupant" is an average — it varies with activity level and body mass.

**What happens without adaptation:**

The profiler sidesteps this problem entirely -- it measures what happens empirically without trying to model the underlying physics. The derived envelope conductance, by contrast, absorbs whatever these coefficients get wrong, which makes it an *effective* value rather than a physical one -- fine as a coarse diagnostic, not a number to quote.

**How the coefficient calibrator fixes this:**

A daily calibration pass analyzes the past 24–72 hours of prediction errors (innovations), tagged with environmental context — wind speed, attic temperature, occupancy count, HVAC mode, outdoor temperature. Using sensitivity-weighted ridge regression, it identifies which coefficients are biased for your home and computes bounded correction multipliers.

If the model consistently over-predicts heat loss on windy days, the wind infiltration coefficient is adjusted downward. If nighttime prediction error correlates with attic temperature, the attic conductance is adjusted. If the HVAC capacity prediction is systematically wrong at extreme outdoor temperatures, the COP degradation slopes are tuned.

The calibrator also detects "natural experiments" — periods where confounders are minimized and a single effect dominates. A calm, clear night with nobody home and HVAC off isolates the pure building envelope. A windy night compared to that baseline isolates wind infiltration. These high-confidence windows provide supplementary calibration when they occur.

**Safeguards:**

- Corrections are rate-limited to 20% per day per coefficient and smoothed with an exponential moving average
- All multipliers are bounded to 20%–500% of the textbook default (no physically nonsensical values)
- Ridge regularization biases the regression toward "no change" when data is ambiguous
- The calibrator ships in observe-only mode by default — it logs what it *would* adjust to diagnostic sensors, but doesn't apply changes until you explicitly enable it

---

### Why does the optimizer only update every 5 minutes? My thermostat changes state faster than that.

Two separate mechanisms handle fast vs. slow events.

**Fast (instant):** The watchdog listens to `state_changed` events from the thermostat directly. HVAC mode changes (heat→cool), manual setpoint overrides, and thermostat unavailability are all caught the moment they happen and trigger an immediate re-optimization if needed.

**Slow (5-minute poll):** The tactical controller, savings accounting, and profiler all run on the 5-minute tick. This is intentional — building thermal dynamics operate on hourly time constants, so indoor air temperature typically moves only 0.1–0.3°F per 5-minute interval. Sampling faster would give you multiple nearly-identical readings per real observation, which adds noise without adding information. Ecobee reports at 5-minute resolution in any case, and at that granularity the per-step temperature deltas are close to quantization noise; the profiler window-averages for exactly this reason.

In practice, the weather forecast (hourly), electricity rates (hourly), and most outdoor sensors (1–5 minute reporting intervals) don't change faster than the coordinator polls anyway — so there's no faster signal to capture for the learning and control path.

---

### How fast does the optimizer learn? Other "smart" thermostats learn in a few days.

Most smart thermostats learn a *schedule* -- when you typically adjust the temperature, and what setpoints you prefer. That can be inferred from a few days of usage.

This integration takes a different approach: the **performance profiler** learns your HVAC system's actual efficiency at different outdoor temperatures by recording observed temperature change rates. It doesn't need to model the physics of why -- it just measures what happens. This is much faster: history bootstrap can replay up to 10 days of recorder data through the profiler on first setup, often reaching initial confidence within hours.

The coarse thermal characterisation (time constant, envelope conductance) sharpens over a longer window, since it needs passive-drift observations across a range of outdoor temperatures. It is a diagnostic; scheduling does not wait for it.

---

### I entered my system specs (tonnage, square footage, aux heat). How much does that actually help?

With the profiler-primary architecture, system specs matter less for initial optimization timeline -- the profiler reaches confidence from observed thermostat data and history bootstrap, regardless of whether you provide specs. But specs still help in two areas:

**Energy accounting from day one:** Tonnage combined with SEER derives rated power draw (`W_rated = tons x 12,000 / SEER`), which gives accurate energy and cost estimates immediately rather than relying on the 3,500W flat default. This is the most visible benefit.

**What the specs are used for:**

| What you provide | Parameter seeded | Starting uncertainty | Without it |
|---|---|---|---|
| Tonnage | Q_cool, Q_heat | +/-10% of rated capacity | +/-316% (essentially unconstrained) |
| Home sq ft | C_air (thermal mass) | +/-30% | Wide open |

The profiler does not need any of this -- it measures performance directly. The specs improve energy accounting and seed the thermal-mass figure.

**Aux heat type and capacity** remains the most impactful spec. Without a circuit power sensor there is no way to separate strip heat from compressor heat, so a cold snap that ran the strips reads as a heat pump that suddenly got stronger. Declaring `electric_strip` with the strip's kW rating keeps that runtime attributed correctly.

---

### Can the optimizer push my home's temperature outside my comfort range?

No. The comfort range and safety limits you configure are hard constraints, not targets. The optimizer works *within* the comfort band -- shifting *when* it runs and how far toward one end of the band it pre-conditions -- but it will never set a temperature outside that range. Safety limits (e.g., 50F min, 85F max) are enforced as absolute guardrails that take priority over the schedule under all conditions, including safe mode.

---

### Why do the savings numbers change over time, or sometimes go down?

Savings are measured against a *counterfactual baseline* -- what your old thermostat routine would have done that same day, simulated using the profiler's measured performance data. The baseline and profiler are still being refined during the first week or two.

Three things cause reported savings to change:

1. **Profiler coverage** -- As the profiler accumulates observations across more outdoor temperatures and solar conditions, its performance predictions become more accurate, which improves counterfactual accuracy.
2. **Optimizer confidence** -- When profiler confidence is low, the optimizer is conservative (it doesn't pre-condition as aggressively), so actual savings are smaller. As confidence grows, the optimizer takes better advantage of efficient hours and real savings improve.
3. **Seasonal variation** -- A cold March looks nothing like a mild November to the counterfactual. The optimizer's advantage is largest when there's a meaningful spread between efficient and inefficient hours (big daily temperature swings, variable grid prices). Mild, stable weather naturally produces smaller savings.

The `Savings Accuracy Tier` sensor tells you how much to trust the current numbers. During `learning` and `estimated` tiers, treat the figures as directional, not precise.

---

### Why do daily savings show small values while cumulative totals stay at zero?

During the early learning period, you may notice `Energy Saved Today`, `Cost Saved Today`, and `CO2 Avoided Today` reporting small, noisy values each day -- but `Energy Saved Cumulative`, `Cost Saved Cumulative`, and `CO2 Avoided Cumulative` remain stuck at zero. This is intentional.

**Daily sensors** record hourly savings regardless of the current accuracy tier. Before the baseline template is built, they use a ratio-based fallback (estimated baseline-to-actual multiplier) that produces rough numbers. These are diagnostic — they show the savings tracker is running, but the values aren't reliable because the optimizer isn't actually controlling the thermostat yet. The daily totals reset at local midnight each day, so nothing accumulates.

**Cumulative sensors** are gated by the accuracy tier. While the tier is `learning` or `projected`, cumulative totals are intentionally blocked from accumulating. This prevents phantom savings (from the noisy ratio fallback) from inflating your all-time numbers before the optimizer has earned them. When the tier eventually upgrades to `estimated` (after baseline capture completes on Day 7+), any previously recorded phantom data is explicitly reset to zero — and from that point forward, savings are accumulated using the counterfactual simulator rather than the ratio fallback.

**In short:** the daily sensors are "best effort" diagnostics during learning. The cumulative sensors wait until the integration can actually measure savings it caused. If you see daily numbers but zero cumulative totals, the system is working correctly — it's just being honest about what it knows.

---

### What happens when weather data goes stale or Home Assistant loses internet?

The integration enters **safe mode** when the weather forecast is more than 6 hours old. In safe mode:

- The strategic optimizer stops making setpoint changes
- The thermostat holds its last setpoint
- A `heatpump_optimizer_safe_mode_entered` event fires for your automations
- The `Source Health` sensor shows degraded status

When fresh weather data returns, safe mode exits automatically and a re-optimization runs. The tactical controller and the profiler continue operating normally during safe mode since they only need the thermostat and temperature sensors, not the forecast.

---

### My utility bill shows more HVAC energy than the integration reports. Why?

A few possible reasons:

- **No power sensor configured** — Without an HVAC circuit power sensor, the integration estimates energy from a default wattage (3,500W) multiplied by runtime. If your actual heat pump draws more (common with larger equipment or electric resistance backup), the estimate will be low. Adding a clamp meter or smart breaker sensor fixes this.
- **Aux/emergency heat** — Before the `AuxHeatLearner` has enough samples (12+ non-aux heating intervals), it uses the same 3,500W default for the baseline. If your system runs aux heat frequently, the first week or two of energy estimates will undercount. Once learned, strip wattage is derived from the actual circuit power delta.
- **Other HVAC loads** — The integration only tracks your primary heat pump circuit. Electric air handlers, supplemental duct heaters, or separate zone controllers on different circuits won't be counted.
- **Standby draw** — Some power sensors report standby draw from the air handler even when the compressor is off. The integration uses the thermostat's `hvac_action` to gate power accounting, so standby may not be included in its figures but would still appear on your utility bill.

---

### Why does model confidence sometimes drop after being high?

**Profiler confidence** can decrease when outdoor conditions shift to temperature ranges the profiler hasn't observed yet. For example, the first warm week of spring may push outdoor temps above the profiler's cooling data range and confidence drops until observations fill in the new bins. This is expected and resolves as the profiler accumulates data in the new conditions.

The displayed number is smoothed: it rises with the measurement immediately, but a dip has to exceed about 10 points before the display follows it down. The optimizer always reacts to the raw value, so it can pull back to conservative behavior before the dashboard number visibly changes.

Confidence can also fall after seasonal transitions or equipment changes (new filter, refrigerant recharge), because the profiler's coverage of the conditions it is now operating in is genuinely thinner than it was. That is the behaviour you want: the displayed value is smoothed, but control reads the raw number and becomes more conservative.

---

### Does the optimizer start working right away?

Yes. The profiler can reach initial confidence within hours via history bootstrap, and conservative optimization can begin as soon as the profiler has enough coverage for the current HVAC mode. There is no mandatory waiting period before the optimizer starts controlling setpoints.

**Baseline capture** still runs in the background to build a picture of your normal thermostat routine for savings comparisons. This takes 7 days to cover a full week (including weekend patterns). During that time, the optimizer is already active -- you just won't see accurate savings numbers until the baseline completes and the counterfactual simulator has a reference to compare against.

---

### I have a Beestat temperature profile. Why can't I import it to speed things up?

Earlier versions of this integration supported importing a [Beestat](https://beestat.io/) temperature profile to seed the thermal model. This was removed because Beestat profiles actually *hurt* model accuracy.

A Beestat profile measures one thing: how fast your indoor temperature changes at each outdoor temperature, broken out by HVAC mode. But those measurements **lump together every heat source and loss mechanism into a single number**. When your house cools at 0.5°F/hr with the HVAC off on a 40°F day, that rate reflects:

- Heat loss through the building envelope (walls, windows, attic)
- Solar radiation warming the house through south-facing windows
- Body heat from occupants
- Appliance waste heat (cooking, dryers, electronics)
- Wind-driven infiltration varying hour to hour
- Thermal mass buffering temperature swings

Beestat's figures already have solar gain baked into them: an R-value that includes it, a heating capacity inflated by solar assistance, a cooling capacity deflated by solar opposition. The profiler avoids the problem by binning observations by solar condition (sunny, cloudy, night) and keeping them separate, rather than fitting one number that averages across all three.

The current approach -- the performance profiler with solar-aware 2D binning -- avoids this problem entirely by measuring performance under specific conditions (outdoor temp + solar condition) rather than trying to decompose the underlying physics. Combined with history bootstrap (which replays your Home Assistant recorder data through the profiler on first startup), the integration typically reaches meaningful confidence within hours to days without any external profile.

---

### Does it work with multi-zone systems or mini-splits?

One integration instance manages one climate entity. For multi-zone systems, the supported approaches are:

- **Single main zone** -- Configure only the primary zone's climate entity; configure the other zones as auxiliary appliances if they affect the conditioned space's temperature.
- **Multiple instances** -- Install separate integration instances for each zone (each with its own config entry). They operate independently but are unaware of each other -- in overlapping spaces, one instance's pre-conditioning will affect the other's model learning.
- **Mini-splits** -- Work well if the unit is exposed as a standard `climate` entity in Home Assistant. Mini-split zones with separate outdoor units are the best-supported multizone configuration since each zone's efficiency is independent. Units that only expose fan/swing controls without temperature feedback won't produce useful learning data.

---

### My thermostat is an Ecobee or Nest with satellite sensors. Is learning corrupted by temperature blending?

Smart thermostats like Ecobee and Nest sell satellite sensors partly on the promise of comfort where you actually are, not just where the thermostat is. When you're in bed at night and the bedroom sensor is warmer than the living room thermostat, the thermostat's firmware quietly blends its reported temperature upward — toward the occupied room. This is a deliberate comfort feature, and it works well for that purpose: the thermostat calls for heat sooner because it "feels" colder than the main sensor actually reads.

The problem is that this blending is invisible to any software reading the thermostat. From this integration's perspective, the thermostat simply reports a temperature that's 2–5°F warmer than the actual air near it — slowly, over several hours, with no flag or warning. That corrupts the thermal model: if the thermostat says it's warmer overnight than it actually is, the model infers that your home holds heat unusually well, and adjusts its learned building parameters accordingly. The effect compounds over days.

**If you suspect this is happening**, the `Cross-Sensor Temp Spread` diagnostic sensor is the best first check. Add one or more additional room temperature sensors, then watch the spread between those and your thermostat overnight. A sustained divergence of 2–5°F during sleeping hours that collapses in the morning is the signature pattern.

**To address it**, go to **Configure → Indoor Temp Blending** and choose a mitigation mode:

- **Occupancy-Based** — Uses the thermostat's own occupancy sensor. When the thermostat's area is unoccupied but people are home, the thermostat reading is excluded and the integration relies on your other room sensors. This is the most precise option for Ecobee/Nest users because it directly detects the condition that triggers blending.

- **Time Schedule** — Excludes the thermostat reading during a configured window (e.g., 10pm–8am). No extra hardware required. Works well for households with a regular sleep schedule; less useful if your schedule varies.

- **Multi-Sensor Median** — Requires at least 3 indoor sensors. All sensors (including the thermostat) are pooled; any that deviate too far from the median are excluded. This handles thermostat blending, sensors cooled by nearby appliances, and kitchen sensors spiking during cooking — all by the same mechanism. The most hardware-intensive option but the most automatic.

> **Note for gas/propane stove users:** A kitchen sensor near the stove will read high during cooking and get excluded in median mode — just like a blending thermostat would. This is intentional. The model won't see the local spike as "the house is warm," and the stove's heat will propagate to other sensors naturally over time.

</details>

---

<details>
<summary><strong>Math</strong> -- Performance profiler, thermal characterisation, and optimizer formulation</summary>

## Performance Profiler {#performance-profiler}

The profiler is the primary thermal model. It accumulates observed indoor temperature change rates (F/hr) into solar-aware 2D lookup tables, keyed by `(outdoor_temp_bin, solar_condition)`, across four HVAC modes.

### Observation recording

Each 5-minute coordinator cycle, the profiler records an observation if the interval is clean (no appliances active, no outliers):

```
delta_F_per_hr = (T_indoor_now - T_indoor_prev) / (interval_minutes / 60)
outdoor_bin = round(T_outdoor)
```

Outlier rejection: observations are discarded if `|T_indoor_now - T_indoor_prev| > 2.0 F` (per 5-min interval) or `|delta_F_per_hr| > 15.0 F/hr`.

### Solar condition classification

When an attic temperature sensor is available, the solar condition is classified from the attic-outdoor temperature delta:

| Condition | Criterion |
|-----------|-----------|
| **Sunny** | `T_attic - T_outdoor > 10.0 F` |
| **Cloudy** | `7.0 F < T_attic - T_outdoor <= 10.0 F` |
| **Night** | `T_attic - T_outdoor <= 7.0 F` |

Without an attic sensor, observations are stored in aggregate bins (no solar differentiation).

### Trendline generation

For each `(mode, solar_condition)` combination with enough data, a linear trendline is computed via least-squares regression across outdoor temperature bins:

```
delta_F_per_hr = slope * T_outdoor + intercept
```

Trendlines require at least 5 distinct outdoor temperature bins each containing 6+ samples.

### Confidence metric

Confidence per mode is the geometric mean of three factors:

```
coverage = (bins_with_data / expected_range_bins)
depth    = min(1.0, total_hours / 48.0)
quality  = (bins_with_6plus_samples / bins_with_data)

mode_confidence = (coverage * depth * quality) ^ (1/3)
```

Expected outdoor ranges per mode: cooling 65-100F, heating 0-55F, aux heat 0-35F, passive drift 20-90F.

Overall confidence is the minimum across modes with at least 30 observations. Modes with fewer observations are excluded -- this prevents seasonal modes (e.g., aux heat in spring) from dragging confidence to 0%.

Two values are derived from that minimum:

| Value | Used for | Behavior |
|-------|----------|----------|
| **Raw** | Activation tier and every other control decision | Follows the measurement exactly, up and down |
| **Displayed** | `Profiler Confidence` sensor, dashboard, log lines | High-water mark, clamped so it can never exceed raw by more than 10 percentage points |

The split exists so the displayed number stays calm while the optimizer stays honest. A high-water mark alone would let a stale score keep the optimizer aggressive after a season change, an HVAC repair, insulation work, or a partial model reset; clamping the mark to a bounded lead means the display tracks real regressions instead of ratcheting forever.

### History bootstrap

On first startup, the profiler replays up to 10 days of thermostat and weather history from Home Assistant's recorder. Each historical interval is processed identically to live data, including solar condition classification (computed from historical attic temperature or sun elevation). This allows the profiler to reach initial confidence within hours rather than weeks.

---

## Thermal Characterisation

> **Note:** This is a coarse diagnostic derived from measured data, not a model that drives scheduling. The performance profiler (above) does that.

Three numbers describe the building well enough for the planner's purposes, and each one is separately observable from data the profiler already collects.

Treating the house as a single node, passive drift with the HVAC off is

```
dT_in/dt = k * (T_out - T_in)
```

so the slope of the measured passive-drift trendline against outdoor temperature *is* `k`, and the time constant is its reciprocal:

```
tau = 1 / k
UA  = C * k
```

`C` (thermal mass) is seeded from conditioned floor area at 0.6 BTU/°F per square foot rather than estimated. A live estimate was never identifiable, and the only thing it feeds -- converting appliance BTU into degrees per hour -- is insensitive to the error a seed carries.

The house is not really one node; the maintainer's own data fits a two-node model (indoor coupled to both outdoor and attic) noticeably better. The derived figures are labelled as approximations and the `Thermal Time Constant` sensor reports `unmeasured` rather than a number when the profiler has too little passive data, or when the fitted slope falls outside a credible range.

Earlier versions ran a 9-state Extended Kalman Filter here, estimating envelope resistance, two capacitances, an internal coupling, two HVAC capacities, a solar gain and two temperatures. Nine parameters cannot be identified from a single observed temperature: whichever parameter absorbed residual error changed with conditions, so the estimates random-walked rather than converged (R-value went 12.7 to 36.2 to 5.2 across three days on the maintainer's install). It was removed in favour of the above.

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
| Q_solar | `Q_solar_peak · f_irradiance · sin(elevation) + K_attic · max(0, T_attic - T_out)` | Solar heat gain: direct (windows/walls) + via attic (roof absorption). See [irradiance hierarchy](#irradiance-hierarchy) |
| Q_internal | `800 + 350 · n_people` BTU/hr | Occupant and appliance base load |
| Q_boundary | `K_attic · min(T_attic - T_out, 0) · ... + K_crawl · (T_crawl - T_air)` | Buffer zone heat transfer (excludes solar-driven attic heat during daytime) |
| Q_appliances | Configured per-appliance BTU/hr | Auxiliary appliance thermal loads |
| Q_aux_resistive | `(W_circuit - W_hp_learned) · 3.412` when aux active and W_circuit > W_hp_learned; else 0 | Resistive strip heat output, derived from circuit power minus learned HP baseline |

**Definitions:**

- `UA = R_inv · A_envelope` — whole-building conductance (per-area conductance × envelope area, BTU/hr/°F)
- `φ = min(4, 1 + 2·n_open_doors + 0.025·v_wind + 0.02·√|T_out_eff - T_air|)` — infiltration multiplier including wind, open doors, and buoyancy-driven stack effect (dimensionless, capped at 4×)
- `T_out_eff = T_out - 3°F` during precipitation (evaporative cooling correction), otherwise `T_out`
- `R_int_inv` — air-to-mass coupling conductance (BTU/hr/°F)
- `K_attic = 50 · (A/2000)`, `K_crawl = 25 · (A/2000)` BTU/hr/°F — boundary zone conductances, scaled by envelope area relative to 2000 ft² baseline
- `Q_solar_peak` — peak solar gain at clear-sky noon (BTU/hr), from the profiler's solar-condition bins

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

### Thermodynamic validity

Every physical model component has been reviewed against building science standards and literature. The table below summarizes the assessment:

| Component | Verdict | Reference / Notes |
|-----------|---------|-------------------|
| Two-node RC model | **Correct** | ISO 13786 validated; 2R2C is the standard approach for residential thermal simulation |
| COP degradation (η_cool, η_heat) | **Approximately correct** | Slopes match AHRI 210/240 test data for single-speed units; variable-capacity heat pumps degrade more gently (~0.008-0.012/°F vs the modeled 0.015) |
| Solar gain geometry | **Correct** | `sin(elevation)` is the geometrically correct factor for direct-beam irradiance on a horizontal surface; diffuse radiation (~10-20% of total) is omitted but absorbed into the learned `Q_solar_peak` parameter |
| Infiltration model | **Correct** | Wind coefficient (2.5%/mph) is within ASHRAE Fundamentals Ch. 26 range for residential; stack effect (buoyancy-driven leakage ∝ √ΔT) is included per ASHRAE guidance; infiltration capped at 4× to prevent parameter corruption |
| Internal heat gain | **Correct** | 350 BTU/hr per occupant matches ASHRAE 90.1; 800 BTU/hr appliance base load is within the residential range documented in IECC |
| Boundary zone conductance | **Approximately correct** | K_attic=50 BTU/hr/°F corresponds to ~2000 ft² ceiling at R-30 insulation; scales with configured home area to generalize across home sizes |
| Exponential integration | **Correct** | Analytical solution to the first-order linear ODE; unconditionally stable for any time step and parameter values |
| Linearized LP optimizer | **Deprecated** | Superseded by profiler-driven heuristic optimizer in v0.2.0 |
| Duty cycle model | **Correct for energy** | Time-integral of heat input (∫Q·dt) is identical whether delivered as pulsed on/off or continuous partial capacity over 1-hour bins |
| Counterfactual simulator | **Excellent** | Uses profiler's measured performance data with dual-setpoint thermostat model -- essential for honest savings attribution |

**Known limitations:**

- **Single-zone assumption** — the model treats the home as one air volume. Multi-zone or multi-story homes with significant temperature stratification are not fully captured.
- **No explicit latent heat** — humidity effects on cooling capacity are modeled via sensible heat ratio (SHR) correction, but the model does not track absolute humidity or enthalpy.
- **Linear COP degradation** — real heat pump performance curves are slightly concave, especially near defrost thresholds (30-40°F). The profiler measures net rates per outdoor-temperature bin and so sidesteps this for scheduling; it still affects the modeled energy attribution.
- **No auto mode-switching** -- the optimizer assumes a single HVAC mode (heat or cool) per optimization horizon. Per-hour balance point suppression prevents unnecessary heating when the house is warming passively (and vice versa), but shoulder season days requiring both modes within 24 hours may still need manual intervention.

---

## Coefficient Calibrator

> **Inactive as of the EKF removal.** The calibrator was built to tune coefficients that the Extended Kalman Filter consumed. With the filter gone, nothing feeds it innovations and nothing reads its multipliers. The code and its persisted state are retained because the same machinery could be repointed at profiler residuals, which is a genuinely useful thing to do, but it is not currently doing anything. This section describes the original design.

~10 hardcoded physics coefficients vary significantly between homes. Without adaptation, whichever learned parameter is most flexible absorbs all coefficient errors, producing systematic prediction bias under novel conditions.

The coefficient calibrator was a slow outer loop running on a different timescale from the fast filter. The filter ran every 5 minutes and never knew the calibrator existed; the calibrator ran daily, analyzed conditioned innovations, and adjusted the coefficients the filter consumed — architecturally identical to how weather data flows in.

### Two-timescale architecture

```
Fast EKF (5-min) → conditioned innovations → Slow Calibrator (daily)
                                                   ↓
                                           CoefficientStore multipliers
                                                   ↓
                                           Fast EKF reads calibrated values
```

The calibrator never touched the filter's internal state vector, covariance, or process noise. It adjusted the *environment* the filter operated in. The timescale separation (daily vs. 5-minute) kept the two layers from fighting each other.

### Sensitivity-weighted ridge regression

Each conditioned innovation record includes the full thermal load context: wind speed, attic temperature, occupancy count, HVAC mode, outdoor temperature, etc. The coefficient sensitivity `s_k(t)` = ∂T_air_pred/∂θ_k is computed analytically:

| Coefficient | Sensitivity |
|---|---|
| Wind infiltration | `s = UA · v_wind · (T_out_eff − T_air) · Δt` |
| Attic conductance | `s = A_scale · (T_attic − T_air) · Δt` |
| COP cooling | `s = −Q_cool · (T_out − T_ref) / (1 − α·ΔT) · Δt` |
| COP heating | `s = Q_heat · (T_ref − T_out) / (1 − α·ΔT) · Δt` |
| Internal base gain | `s = Δt` |
| Per-person gain | `s = n_people · Δt` |

The daily calibrator builds the sensitivity matrix J (N × K) and solves:

```
δ = (JᵀJ + λI)⁻¹ Jᵀe
```

where `e` is the innovation vector and `λ` is a regularization parameter (default 1.0) that biases toward no change when data is ambiguous. This is standard ridge regression, solvable in microseconds.

Corrections are applied as bounded multiplicative updates with EMA smoothing:

```
multiplier_new = β · multiplier_old + (1 − β) · (multiplier_old + α · clamp(δ_k, ±0.20))
```

### Calibratable coefficients

| Tier | Coefficient | Default | Observable when |
|---|---|---|---|
| 1 | Wind infiltration | 0.025/mph | Windy vs. calm periods |
| 1 | Attic conductance | 50 BTU/hr/°F | Attic sensor present |
| 1 | Crawlspace conductance | 25 BTU/hr/°F | Crawlspace sensor present |
| 1 | Internal gain (base) | 800 BTU/hr | Empty house, HVAC off |
| 1 | COP cooling slope | 0.012/°F | Cooling at various outdoor temps |
| 1 | COP heating slope | 0.015/°F | Heating at various outdoor temps |
| 2 | Stack effect | 0.02/√°F | Calm cold nights |
| 2 | Per-person heat gain | 350 BTU/hr | Occupancy transitions |
| 2 | Precipitation offset | 3.0°F | Rainy periods |

Tier 1 coefficients are calibrated via the general regression. Tier 2 coefficients are calibrated opportunistically when data permits.

### Natural experiment detection

The calibrator scans for windows where confounders are minimized:

| Experiment | Conditions | Isolates |
|---|---|---|
| Pure envelope | Night, HVAC off, empty house, calm, dry | Envelope baseline |
| Wind isolation | Same as above but wind > 8 mph | Wind infiltration |
| Occupancy isolation | Night, HVAC off, 0 vs. 2+ people | Internal heat gain |
| COP curve | Active HVAC at varied outdoor temps | COP degradation slopes |

### Safeguards

- **Rate limiting**: Max 20% change per day per coefficient
- **Hard bounds**: Multipliers clamped to [0.2, 5.0] (20%–500% of default)
- **Regularization**: Ridge λ=1.0 biases toward no change
- **EMA smoothing**: β=0.8 dampens oscillation
- **Cold-start holdoff**: No calibration until 72 hours of data; natural experiments require 7 days
- **Dry-run by default**: Ships in observe-only mode — logs proposed adjustments to diagnostic sensors without applying them

---

## Grey-Box LP Optimizer (deprecated)

> **Deprecated in v0.2.0.** The grey-box LP optimizer is no longer active -- the profiler-driven heuristic optimizer produces better results with measured performance data. This section is retained for historical reference.

The grey-box optimizer used the former EKF's learned parameters to formulate HVAC scheduling as a constrained optimization problem. It found hourly duty cycles (0-1) that minimized a weighted cost function subject to comfort temperature bounds.

### Linearized thermal model

For the LP, the two-node model is linearized into a scalar recurrence for T_air with T_mass treated as an exogenous trajectory:

```
T_air[t+1] = A[t] · T_air[t] + B[t] · u[t] + d[t]
```

Where:
- `u[t] ∈ [0, 1]` is the HVAC duty cycle at hour t
- `A[t]`, `B[t]`, `d[t]` are time-varying coefficients derived from those parameters and the forecast

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

> **Historical.** This described propagating the EKF covariance forward and was removed with the filter. Plan uncertainty is now expressed through the activation tier, which narrows the band the optimizer may work in when profiler confidence is low, and through `is_extrapolating`, which flags any lookup outside observed data as low confidence.

The covariance matrix propagated forward through the thermal model to compute how much the temperature prediction could drift due to parameter uncertainty:

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

When a heat pump switches to auxiliary or emergency heat, the circuit draws 2–3× more power as resistive heating strips engage alongside (or instead of) the compressor. If this extra heat is not accounted for, a cold snap that ran the strips reads as a heat pump that suddenly got stronger, poisoning the learned heating capacity.

**Detection methods** -- The integration detects aux heat using the first available source (in priority order):

1. **Override entity** -- A binary_sensor or input_boolean you create (Options > Equipment > Aux heat override)
2. **Supply duct temperature** -- A temperature sensor in your ductwork (Options > Equipment > Supply duct temperature sensor). The integration computes the derivative internally and detects aux heat when: supply air exceeds 110F, or exceeds 100F with a rise rate above 3F/min. Heat pump supply air is typically 90-105F; resistive strips produce 120-140F. This replaces the need for manual HA derivative + template binary_sensor setup
3. **Thermostat hvac_action** -- `aux_heating` or `emergency_heating` reported by the climate entity
4. **Thermostat attribute** -- Some thermostats expose an `aux_heat` attribute

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

This keeps strip heat output from being attributed to the compressor during aux intervals — an important correction for installs without a clamp meter.

This `Q_aux_resistive` value is accounted each interval as a known exogenous load, treated identically to `Q_appliances`: the temperature rose this interval because of a known resistive load of X BTU/hr, so the interval is attributed to the strips rather than to the compressor or the envelope.

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

(`0.293071` = 1 BTU/hr / 3.412 W·hr/BTU.) This proxy assumes the baseline would have run the same capacity in full-resistive mode, which is a known approximation. Actual "avoided aux kWh" in the savings decomposition is therefore an estimate. Metered energy (what the house actually drew) is measured, and the headline savings figure uses that rather than this decomposition.

---

### Counterfactual digital twin

The counterfactual simulator runs a parallel "shadow house" using the profiler's measured performance data. It uses a dual-setpoint thermostat model matching how real thermostats operate:

```
if T_virtual > setpoint_high + deadband: mode = cooling, apply profiler cool delta
if T_virtual < setpoint_low  - deadband: mode = heating, apply profiler heat delta
else:                                    mode = idle, apply profiler passive drift
```

Baseline setpoints are derived from the user's comfort configuration and current occupancy mode (home/away/sleep). Passive drift uses the profiler's solar-condition-aware `resist` trendlines, so the counterfactual correctly distinguishes sunny vs cloudy days.

Savings = baseline runtime - optimized runtime, computed using the same profiler model and actual weather conditions.

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
