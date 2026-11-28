# Changelog

## 0.5.3

### Fixed

- **HVAC Energy Today missed evening hours.** Hours were grouped by UTC date, so in US Eastern time everything after 7 pm counted toward the next day. The day now follows your local time zone.
- **Restarts lost energy.** Home Assistant does not unload integrations when it stops, so the last hour or so of energy, twin data and forecast scores was dropped on every restart. State is now saved when Home Assistant stops.
- **Twin used rated watts instead of your power sensor.** The recorder backfill never read the HVAC power sensor, so the twin converted runtime with rated watts (one figure for heat and cool). Backfilled days now carry metered watts per mode. Days already stored are filled once from the recorder. The Twin tab showed a fixed 3500/3500 W before any day was valid; it now shows the watts actually in use.
- **Warm-night forecasts could run away.** A drift line fitted on a few days of one weather condition (for example a week of 42-52°F nights) was extrapolated far outside that range: with the heat off and 60°F outside, the forecast reached 74°F overnight. Condition-specific drift lines are now used only near the outdoor temperatures they were fitted on, and the year-round line covers the rest.

## 0.5.2

### Fixed

- **Runaway indoor forecasts.** The plan's forecast could climb without limit (95°F with the heat set to 63 and 45°F outside), or fall without limit in hot weather. The profiler had fitted a separate drift line for cloudy hours that sloped the wrong way, and the drift's indoor-temperature correction reused that slope. That turned "a warmer house cools faster" into "a warmer house warms faster". A drift fit that does not rise with outdoor temperature is now rejected, and the indoor correction always uses the main drift line's slope, kept above zero. Present since solar-aware drift was added; it showed once the cloudy fit had enough data.

## 0.5.1

### Fixed

- Home Assistant logged a warning for the three twin kWh sensors: the energy device class is only allowed with a total, and these are trailing-window measurements. They keep the kWh unit without the device class.

## 0.5.0

### Added

- **Twin.** A simulated copy of the house replays every day from your real setpoints, then reruns the same day holding a reference temperature (72 by default), 1 F warmer, the optimizer's plan and your usual schedule. Every comparison is twin against twin on the same weather, so model error cancels instead of showing up as savings.
  - The twin's rates are fitted from the house's own history: heat loss to outside, heating and cooling strength by outdoor temperature, strip heat, and sun on the roof and one wall, including heat stored and released hours later. The wall direction is picked from the data.
  - Strip heat follows how the thermostat actually stages it: after a learned stretch of compressor runtime (30 minutes on an Ecobee), below a learned outdoor temperature.
  - Each day is simulated by a fit that never saw it, and the twin is graded against real runtime. A poor grade hides every modeled figure.
  - New Twin tab: accuracy, savings against the reference, optimizer effect, cost of one degree, aux heat by cause (recovery, cold, other), and same weather last year.
  - New sensors: Twin Accuracy, Twin Savings vs Reference 30 Days, Twin Optimizer Effect 30 Days, Cost of a Degree Today.
  - New options for the reference heat and cool setpoints.
  - New service `import_thermostat_history` reads a Beestat 5-minute export so the twin and the Energy tab start with last season.
  - The twin backfills from recorder history once on first start.

### Fixed

- The runtime-to-weather fit compared each candidate against a rounded copy of the best one, so near-ties could pick the wrong balance temperature.

## 0.4.0

### Added

- **Dashboard tabs.** The sidebar panel gains Now, Model and Energy tabs beside the existing cards (now under Overview).
  - Now: the last 24 hours and the next 24 on one time axis, with indoor, setpoints, the promised band, outdoor and compressor duty; the plan as a table; input health.
  - Model: rate against outdoor temperature for drift, heating and cooling, with observation counts per bin; forecast error by lead time against assuming no change; bias by hour and by outdoor temperature.
  - Energy: daily runtime against outdoor temperature with a degree-day fit; runtime by month; minutes by hour of day; a log of morning recoveries.
- A dashboard archive behind those tabs: each plan's forecast is scored at 1, 3, 6, 12 and 24 hours; an hourly log is kept for two months and a daily roll-up for two years; each morning recovery is timed. The hourly log and recoveries are backfilled once from recorder history. Served to the panel over a websocket command.

### Fixed

- "Warm by" recoveries were sized a full degree short. Arrival was checked against the thermostat differential (1°F by default) instead of the half degree intended, so a plan that would still be at 62°F at the target time counted as on time. Arrival is now the warmest simulated temperature between the start and the target, within 0.5°F of the daytime minimum.
- The panel's compressor activity came out wrong on a heat/cool thermostat: action changes are attribute changes there, and the history was read by the wrong timestamp.

## 0.3.1

### Fixed

- The sidebar panel was a blank page. `panel.js` had a syntax error (a dangling `else` left by the estimator removal), so the browser refused to load it.
- The panel looked entities up by internal key, while entity IDs come from display names. Seventeen lookups never matched: the panel was permanently in its "learning" layout, and the savings, model-accuracy and pre-conditioning figures never appeared. Entities are now resolved through the frontend entity registry, with the ID suffix as a fallback.
- The Building Profile and learning cards graded an R-value, thermal mass, HVAC tonnage and solar gain from sensors that no longer exist. They now show what is measured: the thermal time constant and the current heating and cooling rates.
- The House Thermal Load card no longer lists removed components as "+0 BTU/hr".

### Added

- `tools/validate.py` and the test suite parse the panel's JavaScript, so a syntax error fails validation instead of reaching a browser.

## 0.3.0

Found by replaying the real setup path and five-minute cycle against ten days of recorder data from a live house, plus a winter simulation of schedule alternatives.

### Added

- **Morning warm-up.** How the heat setpoint returns from the sleep minimum to the daytime minimum is now an occupant preference instead of a clock time. "Warm by a set time" (default: 9:00 on weekdays, 12:00 on weekends) holds the sleep minimum and starts just in time, sized by simulation. "Hold a modest temperature, finish when it warms up outside" holds an interim temperature and finishes once the forecast reaches a threshold. The previous step-at-sleep-end behavior remains available. Configure under Sleep Schedule.
- `tools/validate.py` and the test suite now fail on a name that is used but never defined in a module.

### Fixed

- The integration could not be imported: `coordinator_helpers.py` used `@callback` without importing it.
- Opening Configure → Equipment raised an error: the step used a variable it never defined.
- On a heat/cool (auto) thermostat the heat setpoint could be written to 70-76°F. The side to write was guessed from the indoor temperature against the wrong comfort band. Both setpoints are now always written explicitly, each clamped to its own band.
- Away and vacation targets could fall below the safety floor: the dual-setpoint write had no clamp and the away widening was applied twice.
- The "safe default" written on every Home Assistant stop or reload, and repeatedly during a forecast outage, was a comfort-band midpoint (heat 70°F in winter). The optimizer now restores the flat comfort edges, once, and only if it has something of its own to undo.
- A user pause was lost on restart, and unloading while paused wrote a setpoint.
- Pausing or a manual override stopped the profiler, the aux-heat learner and energy metering, not just control.
- Passive-drift and HVAC-rate lookups assumed every table was recorded at 72°F indoors. Each bin now carries the indoor temperature it was recorded at. On a house that winters at 62°F, six-hour coast forecasts improved from 2.4°F to 1.0°F mean error.
- Thinly observed bins were used verbatim, producing a heating table that read +0.12°F/hr at 46-47°F and +2.18 at 48°F. Exported tables are now shrunk toward the trendline and smoothed across neighbouring bins.
- Mode detection chose "cool" on a 31°F morning when the afternoon was mild.
- Mild days produced no plan at all, left the thermostat at its last setpoint and re-planned every five minutes.
- Tactical corrections could raise the heat setpoint above the comfort floor (or lower the cool setpoint below the ceiling) because the house was colder (or warmer) than the model predicted. Corrections may now only relax a pre-conditioning target toward the flat edge.
- Override detection compared the heat setpoint it wrote with the cool setpoint the thermostat reported, and treated its own writes as manual overrides.
- Savings accrued whether or not the optimizer was controlling anything. Hours in monitor-only mode, paused or overridden now claim nothing, and the lifetime totals are banked from measured days instead of the digital twin. Totals from earlier versions are retired on first load.
- The digital twin was consulted before it had closed the hour, so its result was never used.
- After an upgrade the counterfactual ran at a default 30,000 BTU/hr instead of the configured tonnage.
- A calendar's default "home" marked an empty house as occupied; a calendar "away" was ignored when no presence entities were configured.
- Auxiliary appliance states were never refreshed, so their sensors stayed unknown.
- `away_comfort_delta`, `override_grace_period_hours`, `occupancy_debounce_minutes` and `cost_weight` were read from options and then ignored.
- A configured outdoor sensor that had gone stale was silently replaced by the forecast without raising the repair issue.
- "Calibrated" savings accuracy was reported regardless of how wrong recent temperature forecasts had been.
- Restarting cleared today's energy, the forecast-error history and the stale-sensor window.

### Removed

- Weather Heat Transfer, Solar Heat Gain, Occupancy Heat Gain and Boundary Zone Heat Transfer sensors. Nothing had produced their values since the thermal estimator was removed. Their registry entries, and those of four older estimator sensors, are cleaned up on upgrade.
- The "Enable coefficient calibration" option, which no longer did anything.

### Changed

- Effective Cooling/Heating Rate have new entity IDs (`..._effective_cooling_rate`, `..._effective_heating_rate`). Their unit had changed from BTU/hr to °F/h under the old IDs, which the recorder reports as a unit conflict.
- Auxiliary appliance heat is no longer subtracted from profiler observations. It stays inside the measured drift, because nothing added it back at prediction time.

## 0.1.0

Initial release.

- Adaptive thermal model via Extended Kalman Filter (two-node RC circuit, 8-state estimation)
- Beestat temperature profile import for fast initialization
- Three-tier hierarchical control (strategic scheduling, tactical drift correction, watchdog override detection)
- Forecast-based setpoint optimization (work-based heuristic and optional LP solver)
- Counterfactual digital twin for savings tracking with decomposition (runtime, COP, rate, carbon)
- Occupancy-aware scheduling with calendar integration and pre-conditioning
- Room-aware indoor temperature weighting by occupancy
- Demand response support with temporary constraint system
- Multi-entity sensor fallback chains for resilience
- 45+ sensor entities for diagnostics, savings tracking, and model transparency
- Model export/import for backup and transfer
