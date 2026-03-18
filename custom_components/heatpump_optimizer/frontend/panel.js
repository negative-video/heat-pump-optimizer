/**
 * Heat Pump Optimizer — Sidebar Dashboard Panel
 *
 * A custom panel web component for Home Assistant that displays
 * optimizer status, savings, forecast, and learning progress.
 *
 * Receives `hass`, `narrow`, and `panel` properties from HA automatically.
 */

const ENTITY_PREFIX = "heat_pump_optimizer";

// ── Helpers ──────────────────────────────────────────────────────────

/** Find the optimizer entity whose ID contains the prefix and ends with `_<suffix>`. */
function findEntity(states, suffix) {
  for (const id of Object.keys(states)) {
    if (id.includes(ENTITY_PREFIX) && id.endsWith(`_${suffix}`)) return states[id];
  }
  return null;
}

/** Find a binary_sensor entity by suffix. */
function findBinary(states, suffix) {
  for (const id of Object.keys(states)) {
    if (id.startsWith("binary_sensor.") && id.endsWith(`_${suffix}`))
      return states[id];
  }
  return null;
}

/** Format a numeric state with fallback. */
function fmt(entity, decimals = 1, fallback = "\u2014") {
  if (!entity || entity.state === "unknown" || entity.state === "unavailable")
    return fallback;
  const n = Number(entity.state);
  if (isNaN(n)) return entity.state;
  return n.toFixed(decimals);
}

/** Get temperature unit symbol from hass config. */
function tempUnit(hass) {
  try {
    const u = hass.config.unit_system.temperature;
    return u || "\u00b0F";
  } catch {
    return "\u00b0F";
  }
}

/** Check if an entity exists and has a valid numeric value. */
function hasValue(entity) {
  if (!entity) return false;
  if (entity.state === "unknown" || entity.state === "unavailable") return false;
  return !isNaN(Number(entity.state));
}

/** Check if entity exists and its state is not unknown/unavailable. */
function isAvailable(entity) {
  return entity && entity.state !== "unknown" && entity.state !== "unavailable";
}

/** Phase → friendly label + color class */
const PHASE_MAP = {
  "pre-cooling": { label: "Pre-Cooling", cls: "phase-active" },
  "pre-heating": { label: "Pre-Heating", cls: "phase-active" },
  coasting: { label: "Coasting", cls: "phase-coast" },
  maintaining: { label: "Maintaining", cls: "phase-maintain" },
  idle: { label: "Idle", cls: "phase-idle" },
  paused: { label: "Paused", cls: "phase-paused" },
  safe_mode: { label: "Safe Mode", cls: "phase-warn" },
  preconditioning: { label: "Pre-conditioning", cls: "phase-active" },
};

/** Accuracy tier → dot count + label */
const TIER_MAP = {
  learning: { dots: 0, label: "Learning" },
  estimated: { dots: 1, label: "Estimated" },
  simulated: { dots: 3, label: "Simulated" },
  calibrated: { dots: 4, label: "Calibrated" },
};

/** Reason string → PHASE_MAP key (best effort). */
function reasonToPhaseKey(reason) {
  if (!reason) return "idle";
  const r = reason.toLowerCase().replace(/[_\s]+/g, "-");
  if (r.includes("pre-cool")) return "pre-cooling";
  if (r.includes("pre-heat")) return "pre-heating";
  if (r.includes("precondition")) return "preconditioning";
  if (r.includes("coast")) return "coasting";
  if (r.includes("maintain")) return "maintaining";
  return "idle";
}

// ── Section Renderers ────────────────────────────────────────────────

/** [A] Alert Banner — conditional warnings. */
function renderAlerts(states) {
  const alerts = [];
  const phase = findEntity(states, "current_phase");
  const override = findBinary(states, "override_detected");
  const stale = findBinary(states, "sensor_stale");
  const auxHeat = findBinary(states, "aux_heat_active");

  if (phase?.state === "safe_mode")
    alerts.push({ cls: "alert-error", msg: "Safe mode \u2014 using conservative defaults" });
  if (auxHeat?.state === "on") {
    const auxThresh = findEntity(states, "aux_heat_threshold");
    const threshMsg = hasValue(auxThresh) ? ` (threshold: ${fmt(auxThresh, 0)}° eff)` : "";
    alerts.push({ cls: "alert-warning", msg: `Auxiliary/emergency heat is running${threshMsg}` });
  }
  if (stale?.state === "on")
    alerts.push({ cls: "alert-warning", msg: "Temperature sensor may be stale" });
  if (override?.state === "on")
    alerts.push({ cls: "alert-info", msg: "Manual override detected \u2014 optimizer paused" });

  if (alerts.length === 0) return "";
  return alerts
    .map((a) => `<div class="alert ${a.cls}">${a.msg}</div>`)
    .join("");
}

/** [B] Hero Status Strip — phase, temps, setpoint, toggle. */
function renderHeroStrip(states, hass) {
  const phase = findEntity(states, "current_phase");
  const apparent = findEntity(states, "apparent_temperature");
  const setpoint = findEntity(states, "target_setpoint");
  const tactical = findEntity(states, "tactical_correction");
  const outdoor = findEntity(states, "outdoor_temp_source");
  const enabled = findEntity(states, "enabled");

  const occupancy = findEntity(states, "occupancy_forecast");
  const power = findEntity(states, "net_hvac_power");
  const applianceLoad = findEntity(states, "appliance_thermal_load");
  const activeAppliances = findEntity(states, "active_appliances");

  const phaseVal = phase?.state || "unknown";
  const phaseInfo = PHASE_MAP[phaseVal] || { label: phaseVal, cls: "phase-idle" };
  const unit = tempUnit(hass);
  const isEnabled = enabled?.state === "on";

  // Occupancy chip
  let occupancyChip = "";
  if (isAvailable(occupancy)) {
    const mode = occupancy.state.toLowerCase();
    const OCCUPANCY_LABELS = { home: "Home", away: "Away", vacation: "Vacation" };
    const OCCUPANCY_CLS = { home: "occ-home", away: "occ-away", vacation: "occ-away" };
    const label = OCCUPANCY_LABELS[mode] || occupancy.state;
    const cls = OCCUPANCY_CLS[mode] || "occ-home";
    occupancyChip = `<span class="occupancy-chip ${cls}">${label}</span>`;
  }

  // Power chip
  let powerChip = "";
  if (hasValue(power) && Number(power.state) > 0) {
    powerChip = `<span class="power-chip">${(Number(power.state) / 1000).toFixed(1)} kW</span>`;
  }

  // Appliance chip
  let applianceChip = "";
  if (hasValue(applianceLoad) && Number(applianceLoad.state) !== 0) {
    const btu = Number(applianceLoad.state);
    const names = activeAppliances?.state || "";
    const label = names && names !== "None" ? names : `${Math.abs(btu).toLocaleString()} BTU/hr`;
    const cls = btu < 0 ? "appliance-cooling" : "appliance-heating";
    applianceChip = `<span class="appliance-chip ${cls}" title="${Math.abs(btu).toLocaleString()} BTU/hr">${label}</span>`;
  }

  // Tactical correction annotation
  let tacticalNote = "";
  if (hasValue(tactical) && Math.abs(Number(tactical.state)) >= 0.1) {
    const v = Number(tactical.state);
    tacticalNote = `<span class="tactical-delta">${v > 0 ? "+" : ""}${v.toFixed(1)}\u00b0</span>`;
  }

  // Indoor subtitle: raw temp + humidity from apparent_temperature attributes
  let indoorSub = "";
  if (apparent?.attributes) {
    const parts = [];
    if (apparent.attributes.raw_temp != null)
      parts.push(`${Number(apparent.attributes.raw_temp).toFixed(1)}${unit} actual`);
    if (apparent.attributes.indoor_humidity != null)
      parts.push(`${Number(apparent.attributes.indoor_humidity).toFixed(0)}% humidity`);
    if (parts.length) indoorSub = `<span class="hero-sub">${parts.join(" \u00b7 ")}</span>`;
  }

  return `
    <div class="card hero-card">
      <div class="hero-row">
        <div class="hero-badges">
          <span class="phase-badge ${phaseInfo.cls}">${phaseInfo.label}</span>
          ${occupancyChip}
          ${powerChip}
          ${applianceChip}
        </div>
        <div class="hero-temps">
          <div class="hero-temp-item">
            <span class="hero-label">Indoor</span>
            <span class="hero-value">${fmt(apparent)}${unit}</span>
            ${indoorSub}
          </div>
          <div class="hero-temp-item">
            <span class="hero-label">Setpoint</span>
            <span class="hero-value">${fmt(setpoint)}${unit}${tacticalNote}</span>
          </div>
          ${isAvailable(outdoor) ? `
          <div class="hero-temp-item">
            <span class="hero-label">Outdoor</span>
            <span class="hero-value">${fmt(outdoor)}${unit}</span>
          </div>` : ""}
        </div>
        <button class="toggle-btn ${isEnabled ? "on" : "off"}" id="toggle-optimizer">
          ${isEnabled ? "Enabled" : "Disabled"}
        </button>
      </div>
    </div>`;
}

/** [C] Forecast Chart — 24h predicted indoor/outdoor temps + HVAC. */
function renderForecastChart(states, hass) {
  const schedule = findEntity(states, "schedule");
  const tier = findEntity(states, "savings_accuracy_tier");
  const apparent = findEntity(states, "apparent_temperature");
  const unit = tempUnit(hass);

  // During learning, show weather-only outdoor temp chart
  const tierVal = tier?.state || "learning";
  if (tierVal === "learning") {
    const weather = schedule?.attributes?.weather_forecast;
    if (!weather || !Array.isArray(weather) || weather.length < 2) {
      return `
        <div class="card forecast-card">
          <h2>Weather Outlook</h2>
          <div class="forecast-placeholder">Loading weather data\u2026</div>
        </div>`;
    }

    // Outdoor-only temperature curve
    const temps = weather.map(pt => pt.outdoor);
    const minT = Math.floor(Math.min(...temps) - 2);
    const maxT = Math.ceil(Math.max(...temps) + 2);
    const range = maxT - minT || 1;
    const step = range / 4;
    const gridlines = [];
    for (let i = 0; i <= 4; i++) {
      const temp = minT + step * i;
      gridlines.push({ temp: Math.round(temp), pct: ((temp - minT) / range) * 100 });
    }
    const nowHour = new Date().getHours();
    const cols = weather.map((pt, i) => {
      const time = new Date(pt.time);
      const hour = time.getHours();
      const pct = ((pt.outdoor - minT) / range) * 100;
      const isNow = hour === nowHour && i === weather.findIndex(p => new Date(p.time).getHours() === nowHour);
      const timeLabel = hour % 3 === 0
        ? `<span class="chart-time">${hour === 0 ? "12a" : hour === 12 ? "12p" : hour > 12 ? (hour - 12) + "p" : hour + "a"}</span>`
        : "";
      return `
        <div class="chart-col${isNow ? " chart-col-now" : ""}">
          <div class="chart-area">
            <div class="chart-dot chart-dot-outdoor chart-dot-weather" style="bottom:${pct}%"></div>
          </div>
          ${timeLabel}
        </div>`;
    });

    return `
      <div class="card forecast-card">
        <h2>Weather Outlook</h2>
        <div class="chart-legend">
          <span class="legend-item"><span class="legend-dot legend-outdoor"></span>Outdoor temp</span>
        </div>
        <div class="chart-container">
          <div class="chart-yaxis">
            ${gridlines.map(g => `<span class="chart-ylabel" style="bottom:${g.pct}%">${g.temp}\u00b0</span>`).join("")}
          </div>
          <div class="chart-grid">
            ${gridlines.map(g => `<div class="chart-gridline" style="bottom:${g.pct}%"></div>`).join("")}
            ${cols.join("")}
          </div>
        </div>
        <div class="forecast-note">Indoor prediction available after calibration</div>
      </div>`;
  }

  const forecast = schedule?.attributes?.forecast;
  if (!forecast || !Array.isArray(forecast) || forecast.length < 2) {
    return `
      <div class="card forecast-card">
        <h2>Forecast</h2>
        <div class="forecast-placeholder">No forecast data available</div>
      </div>`;
  }

  // Actual indoor temp for "now" marker
  const actualIndoor = hasValue(apparent) ? Number(apparent.state) : null;

  // Compute temperature range
  let allTemps = [];
  for (const pt of forecast) {
    allTemps.push(pt.indoor, pt.outdoor);
    if (pt.comfort_min != null) allTemps.push(pt.comfort_min);
    if (pt.comfort_max != null) allTemps.push(pt.comfort_max);
  }
  if (actualIndoor != null) allTemps.push(actualIndoor);
  const minT = Math.floor(Math.min(...allTemps) - 1);
  const maxT = Math.ceil(Math.max(...allTemps) + 1);
  const range = maxT - minT || 1;

  // Generate gridlines (4 evenly spaced)
  const step = range / 4;
  const gridlines = [];
  for (let i = 0; i <= 4; i++) {
    const temp = minT + step * i;
    gridlines.push({ temp: Math.round(temp), pct: ((temp - minT) / range) * 100 });
  }

  // Current hour for "now" marker
  const nowHour = new Date().getHours();

  // Build columns
  const cols = forecast.map((pt, i) => {
    const time = new Date(pt.time);
    const hour = time.getHours();
    const indoorPct = ((pt.indoor - minT) / range) * 100;
    const outdoorPct = ((pt.outdoor - minT) / range) * 100;
    const isNow = hour === nowHour && i === forecast.findIndex(p => new Date(p.time).getHours() === nowHour);

    // Comfort band
    let comfortBand = "";
    if (pt.comfort_min != null && pt.comfort_max != null) {
      const minPct = ((pt.comfort_min - minT) / range) * 100;
      const maxPct = ((pt.comfort_max - minT) / range) * 100;
      comfortBand = `<div class="chart-comfort" style="bottom:${minPct}%;height:${maxPct - minPct}%"></div>`;
    }

    // HVAC strip
    const hvacStrip = pt.hvac
      ? `<div class="chart-hvac ${pt.outdoor > pt.indoor ? "chart-hvac-cool" : "chart-hvac-heat"}"></div>`
      : "";

    // Time label (every 3 hours)
    const timeLabel = hour % 3 === 0
      ? `<span class="chart-time">${hour === 0 ? "12a" : hour === 12 ? "12p" : hour > 12 ? (hour - 12) + "p" : hour + "a"}</span>`
      : "";

    // Actual indoor temp ring on "now" column
    let actualDot = "";
    if (isNow && actualIndoor != null) {
      const actualPct = ((actualIndoor - minT) / range) * 100;
      actualDot = `<div class="chart-dot chart-dot-actual" style="bottom:${actualPct}%" title="Actual ${actualIndoor.toFixed(1)}${unit}"></div>`;
    }

    return `
      <div class="chart-col${isNow ? " chart-col-now" : ""}">
        <div class="chart-area">
          ${comfortBand}
          <div class="chart-dot chart-dot-outdoor" style="bottom:${outdoorPct}%"></div>
          <div class="chart-dot chart-dot-indoor" style="bottom:${indoorPct}%"></div>
          ${actualDot}
          ${hvacStrip}
        </div>
        ${timeLabel}
      </div>`;
  });

  return `
    <div class="card forecast-card">
      <h2>Forecast</h2>
      <div class="chart-legend">
        <span class="legend-item"><span class="legend-dot legend-indoor"></span>Predicted</span>
        ${actualIndoor != null ? `<span class="legend-item"><span class="legend-dot legend-actual"></span>Actual</span>` : ""}
        <span class="legend-item"><span class="legend-dot legend-outdoor"></span>Outdoor</span>
        <span class="legend-item"><span class="legend-band"></span>Comfort</span>
        <span class="legend-item"><span class="legend-hvac"></span>HVAC</span>
      </div>
      <div class="chart-container">
        <div class="chart-yaxis">
          ${gridlines.map(g => `<span class="chart-ylabel" style="bottom:${g.pct}%">${g.temp}\u00b0</span>`).join("")}
        </div>
        <div class="chart-grid">
          ${gridlines.map(g => `<div class="chart-gridline" style="bottom:${g.pct}%"></div>`).join("")}
          ${cols.join("")}
        </div>
      </div>
    </div>`;
}

/** [D] Schedule Timeline — 24h horizontal bar with phase colors. */
function renderTimeline(states, hass) {
  const schedule = findEntity(states, "schedule");
  const nextAction = findEntity(states, "next_action");
  const precond = findEntity(states, "preconditioning_status");
  const unit = tempUnit(hass);

  const entries = schedule?.attributes?.entries;
  if (!entries || !Array.isArray(entries) || entries.length === 0) {
    // During learning, show multi-bar calibration progress
    const tier = findEntity(states, "savings_accuracy_tier");
    const tierVal = tier?.state || "learning";
    if (tierVal === "learning") {
      const baselineConf = findEntity(states, "baseline_confidence");
      const modelConf = findEntity(states, "model_confidence");
      const profilerConf = findEntity(states, "profiler_confidence");
      const profilerObs = findEntity(states, "profiler_observations");
      const progress = findEntity(states, "learning_progress");

      const bars = [
        { label: "Baseline", pct: hasValue(baselineConf) ? Number(baselineConf.state) : 0, cls: "learn-bar-baseline" },
        { label: "Thermal Model", pct: hasValue(modelConf) ? Number(modelConf.state) : 0, cls: "learn-bar-model" },
        { label: "Profiler", pct: hasValue(profilerConf) ? Number(profilerConf.state) : 0, cls: "learn-bar-profiler" },
      ];

      const barsHtml = bars.map(b => `
        <div class="learn-bar-row">
          <span class="learn-bar-label">${b.label}</span>
          <div class="learn-bar-track">
            <div class="learn-bar-fill ${b.cls}" style="width:${Math.min(100, b.pct)}%"></div>
          </div>
          <span class="learn-bar-pct">${b.pct.toFixed(0)}%</span>
        </div>`).join("");

      const statusText = isAvailable(progress) ? progress.state : "Capturing your home\u2019s thermal signature\u2026";
      const obsText = hasValue(profilerObs) ? `${fmt(profilerObs, 0)} observations collected` : "";

      return `
        <div class="card timeline-card">
          <h2>Calibration Progress</h2>
          <div class="learn-bars">${barsHtml}</div>
          <div class="learn-status">${statusText}</div>
          ${obsText ? `<div class="learn-obs">${obsText}</div>` : ""}
        </div>`;
    }

    return `
      <div class="card timeline-card">
        <h2>Schedule</h2>
        <div class="timeline-empty">No active schedule</div>
        ${isAvailable(nextAction) ? `<div class="timeline-next">${nextAction.state}</div>` : ""}
      </div>`;
  }

  // Parse entries and compute hour positions relative to start of day
  const now = new Date();
  const dayStart = new Date(now);
  dayStart.setHours(0, 0, 0, 0);
  const dayMs = 24 * 60 * 60 * 1000;

  // Build segments with labels
  const usedPhases = new Set();
  const segments = entries.map((e) => {
    const start = new Date(e.start);
    const end = new Date(e.end);
    const leftPct = Math.max(0, ((start - dayStart) / dayMs) * 100);
    const widthPct = Math.min(100 - leftPct, ((end - start) / dayMs) * 100);
    const phaseKey = reasonToPhaseKey(e.reason);
    const phaseInfo = PHASE_MAP[phaseKey] || PHASE_MAP["idle"];
    usedPhases.add(phaseKey);
    // Format time range for tooltip
    const fmtTime = (d) => { const h = d.getHours(); const m = d.getMinutes(); return `${h === 0 ? 12 : h > 12 ? h - 12 : h}:${String(m).padStart(2, "0")}${h >= 12 ? "p" : "a"}`; };
    const tooltip = `${phaseInfo.label}: ${fmtTime(start)}\u2013${fmtTime(end)} \u00b7 ${e.target_temp}${unit}`;
    // Show inline label if segment is wide enough (>8% of day = ~2hrs)
    const inlineLabel = widthPct > 8 ? `<span class="tl-seg-label">${phaseInfo.label}</span>` : "";
    return `<div class="tl-segment ${phaseInfo.cls}" style="left:${leftPct}%;width:${Math.max(widthPct, 0.5)}%" title="${tooltip}">${inlineLabel}</div>`;
  });

  const nowPct = ((now - dayStart) / dayMs) * 100;

  // Hour ticks
  const ticks = [0, 6, 12, 18].map(
    (h) => `<span class="tl-tick" style="left:${(h / 24) * 100}%">${h === 0 ? "12a" : h === 12 ? "12p" : h > 12 ? h - 12 + "p" : h + "a"}</span>`
  );

  // Legend showing only phases that appear in today's schedule
  const PHASE_LEGEND = {
    "pre-cooling": { label: "Pre-Cooling", cls: "phase-active" },
    "pre-heating": { label: "Pre-Heating", cls: "phase-active" },
    coasting: { label: "Coasting", cls: "phase-coast" },
    maintaining: { label: "Maintaining", cls: "phase-maintain" },
    idle: { label: "Idle", cls: "phase-idle" },
    preconditioning: { label: "Pre-conditioning", cls: "phase-active" },
  };
  const legendItems = [...usedPhases]
    .filter(k => PHASE_LEGEND[k])
    .map(k => {
      const p = PHASE_LEGEND[k];
      return `<span class="tl-legend-item"><span class="tl-legend-swatch ${p.cls}"></span>${p.label}</span>`;
    }).join("");

  // Occupancy underlay — thin bar showing home/away from calendar
  const occupancy = findEntity(states, "occupancy_forecast");
  let occBar = "";
  if (occupancy?.attributes?.source === "calendar" && Array.isArray(occupancy.attributes.timeline)) {
    const occSegs = occupancy.attributes.timeline.map(seg => {
      const start = new Date(seg.start);
      const end = new Date(seg.end);
      const leftPct = Math.max(0, ((start - dayStart) / dayMs) * 100);
      const widthPct = Math.min(100 - leftPct, ((end - start) / dayMs) * 100);
      const cls = seg.mode === "home" ? "tl-occ-home" : "tl-occ-away";
      const label = seg.mode === "home" ? "Home" : "Away";
      return `<div class="tl-occ-seg ${cls}" style="left:${leftPct}%;width:${Math.max(widthPct, 0.5)}%" title="${label}"></div>`;
    }).join("");
    occBar = `<div class="tl-occ-bar">${occSegs}</div>`;
  }

  // Preconditioning info
  let precondInfo = "";
  if (isAvailable(precond) && precond.attributes) {
    const a = precond.attributes;
    if (a.arrival_time) {
      const parts = [`Arrival: ${a.arrival_time}`];
      if (a.energy_estimate != null) parts.push(`${Number(a.energy_estimate).toFixed(1)} kWh`);
      if (a.cost_estimate != null) parts.push(`$${Number(a.cost_estimate).toFixed(2)}`);
      precondInfo = `<div class="timeline-precond">${parts.join(" \u00b7 ")}</div>`;
    }
  }

  return `
    <div class="card timeline-card">
      <h2>Schedule</h2>
      ${legendItems ? `<div class="tl-legend">${legendItems}</div>` : ""}
      <div class="tl-bar">
        ${segments.join("")}
        <div class="tl-now" style="left:${nowPct}%"></div>
      </div>
      ${occBar}
      <div class="tl-ticks">${ticks.join("")}</div>
      ${isAvailable(nextAction) ? `<div class="timeline-next">${nextAction.state}</div>` : ""}
      ${precondInfo}
    </div>`;
}

/** [D2] Decision Engine — explains why the optimizer is doing what it's doing. */
function renderDecisionCard(states, hass) {
  const phase = findEntity(states, "current_phase");
  const tier = findEntity(states, "savings_accuracy_tier");
  const tacticalCorr = findEntity(states, "tactical_correction");
  const predicted = findEntity(states, "predicted_indoor_temp");
  const apparent = findEntity(states, "apparent_temperature");
  const predError = findEntity(states, "prediction_error");
  const accuracy = findEntity(states, "model_accuracy");
  const occupancy = findEntity(states, "occupancy_forecast");
  const precond = findEntity(states, "preconditioning_status");
  const unit = tempUnit(hass);

  // Hidden during learning — no schedule means no tactical state
  if ((tier?.state || "learning") === "learning") return "";

  const tacticalState = phase?.attributes?.tactical_state || "nominal";

  // Tactical state badge
  const TACTICAL_MAP = {
    nominal: { label: "On Track", cls: "tac-nominal" },
    correcting: { label: "Correcting", cls: "tac-correcting" },
    disturbed: { label: "Disturbed", cls: "tac-disturbed" },
  };
  const tacInfo = TACTICAL_MAP[tacticalState] || TACTICAL_MAP["nominal"];

  // Build narrative sentence
  let narrative = "";
  if (tacticalState === "disturbed") {
    const err = hasValue(predError) ? `${Number(predError.state) > 0 ? "+" : ""}${fmt(predError)}` : "";
    narrative = `Large divergence from model${err ? ` (${err}\u00b0 drift)` : ""} \u2014 possible window open or appliance`;
  } else if (hasValue(predicted) && hasValue(apparent)) {
    const pred = Number(predicted.state);
    const act = Number(apparent.state);
    const drift = act - pred;
    const driftStr = `${drift >= 0 ? "+" : ""}${drift.toFixed(1)}\u00b0`;
    narrative = `Predicted ${pred.toFixed(1)}${unit}, actual ${act.toFixed(1)}${unit} (${driftStr} drift)`;
  }

  // Correction stat
  let correctionText = "\u2014";
  if (hasValue(tacticalCorr)) {
    const c = Number(tacticalCorr.state);
    if (Math.abs(c) < 0.05) correctionText = "0.0\u00b0 (none needed)";
    else correctionText = `${c > 0 ? "+" : ""}${c.toFixed(1)}\u00b0 applied`;
  }

  // Model fit stat
  let fitText = "";
  if (hasValue(accuracy)) {
    fitText = `\u00b1${fmt(accuracy)}${unit} MAE`;
    const bias = accuracy?.attributes?.model_bias;
    if (bias != null) {
      const b = Number(bias);
      const bLabel = Math.abs(b) < 0.15 ? "no bias" : b > 0 ? "warm bias" : "cool bias";
      fitText += ` \u00b7 ${b >= 0 ? "+" : ""}${b.toFixed(1)}\u00b0 ${bLabel}`;
    }
  }

  // What's Next — occupancy transition + preconditioning
  let nextSection = "";
  const nextTime = occupancy?.attributes?.next_transition;
  const nextType = occupancy?.attributes?.next_transition_type;
  if (nextTime) {
    const fmtTime = new Date(nextTime).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const typeLabel = (nextType || "").replace(/_/g, " \u2192 ").replace(/\b\w/g, c => c.toUpperCase());
    let nextLine = `<div class="decision-next-line">${typeLabel} at ${fmtTime}</div>`;

    // Preconditioning plan details
    if (precond && (precond.state === "scheduled" || precond.state === "active")) {
      const a = precond.attributes || {};
      const parts = [];
      if (a.temperature_gap != null) parts.push(`${Number(a.temperature_gap).toFixed(1)}\u00b0 gap`);
      if (a.estimated_energy_kwh != null) parts.push(`~${Number(a.estimated_energy_kwh).toFixed(1)} kWh`);
      if (a.estimated_cost != null) parts.push(`~$${Number(a.estimated_cost).toFixed(2)}`);
      if (parts.length) {
        const startTime = a.scheduled_start
          ? new Date(a.scheduled_start).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
          : null;
        nextLine += `<div class="decision-precond">${precond.state === "active" ? "Pre-conditioning now" : `Pre-condition starts ${startTime || "soon"}`} (${parts.join(", ")})</div>`;
      }
    }
    nextSection = `<div class="decision-next">${nextLine}</div>`;
  }

  return `
    <div class="card decision-card">
      <h2>Decisions</h2>
      <div class="decision-top">
        <span class="tactical-badge ${tacInfo.cls}">${tacInfo.label}</span>
        ${narrative ? `<span class="decision-narrative">${narrative}</span>` : ""}
      </div>
      <div class="decision-stats">
        <div class="decision-stat">
          <span class="decision-stat-label">Correction</span>
          <span class="decision-stat-value">${correctionText}</span>
        </div>
        ${fitText ? `
        <div class="decision-stat">
          <span class="decision-stat-label">Model Fit</span>
          <span class="decision-stat-value">${fitText}</span>
        </div>` : ""}
      </div>
      ${nextSection}
    </div>`;
}

/** [E] Savings Card — today + decomposition + all-time. */
function renderSavingsCard(states, hass) {
  // Hide during learning — zeros are misleading
  const tier = findEntity(states, "savings_accuracy_tier");
  if ((tier?.state || "learning") === "learning") return "";

  const savingsKwh = findEntity(states, "savings_kwh_today");
  const savingsCost = findEntity(states, "savings_cost_today");
  const savingsCo2 = findEntity(states, "savings_co2_today");
  const comfortHours = findEntity(states, "comfort_hours_gained");
  const baselineKwh = findEntity(states, "baseline_kwh_today");
  const savingsPct = findEntity(states, "estimated_savings");
  const runtimeSavings = findEntity(states, "runtime_savings_today");
  const copSavings = findEntity(states, "cop_savings_today");
  const rateSavings = findEntity(states, "rate_savings_today");
  const carbonSavings = findEntity(states, "carbon_shift_savings_today");
  const baselineCop = findEntity(states, "baseline_avg_cop");
  const optimizedCop = findEntity(states, "optimized_avg_cop");
  const copImprovement = findEntity(states, "cop_improvement_pct");
  const cumulKwh = findEntity(states, "savings_kwh_cumulative");
  const cumulCost = findEntity(states, "savings_cost_cumulative");
  const cumulCo2 = findEntity(states, "savings_co2_cumulative");
  const auxHeatKwh = findEntity(states, "aux_heat_kwh_today");
  const avoidedAuxKwh = findEntity(states, "avoided_aux_heat_kwh_today");

  const tierVal = tier?.state || "learning";
  const tierInfo = TIER_MAP[tierVal] || TIER_MAP["learning"];

  // Tier dots
  const dots = Array.from({ length: 4 }, (_, i) =>
    `<span class="tier-dot${i < tierInfo.dots ? " filled" : ""}"></span>`
  ).join("");

  // Tier accuracy note for sub-calibrated tiers
  const tierNote = tierVal !== "calibrated"
    ? `<div class="tier-note">Savings are ${tierInfo.label.toLowerCase()}-grade — accuracy improves as the model learns your home.</div>`
    : "";

  // Context bar: savings as % of baseline
  let contextBar = "";
  if (hasValue(baselineKwh) && hasValue(savingsKwh)) {
    const baseline = Number(baselineKwh.state);
    const saved = Number(savingsKwh.state);
    if (baseline > 0) {
      const pct = Math.round((saved / baseline) * 100);
      const usedPct = Math.max(0, Math.min(100, 100 - pct));
      contextBar = `
        <div class="context-bar-wrap">
          <div class="context-bar">
            <div class="context-bar-used" style="width:${usedPct}%"></div>
          </div>
          <span class="context-bar-label">${pct}% less than baseline</span>
        </div>`;
    }
  } else if (hasValue(savingsPct)) {
    contextBar = `
      <div class="context-bar-wrap">
        <div class="context-bar">
          <div class="context-bar-used" style="width:${100 - Number(savingsPct.state)}%"></div>
        </div>
        <span class="context-bar-label">${fmt(savingsPct, 0)}% runtime savings</span>
      </div>`;
  }

  // Decomposition chips
  const chips = [];
  if (hasValue(runtimeSavings) && Number(runtimeSavings.state) > 0)
    chips.push(`<span class="chip">Runtime ${fmt(runtimeSavings, 2)} kWh</span>`);
  if (hasValue(copSavings) && Number(copSavings.state) > 0)
    chips.push(`<span class="chip">COP ${fmt(copSavings, 2)} kWh</span>`);
  if (hasValue(rateSavings) && Number(rateSavings.state) > 0)
    chips.push(`<span class="chip">Rate $${fmt(rateSavings, 2)}</span>`);
  if (hasValue(carbonSavings) && Number(carbonSavings.state) > 0)
    chips.push(`<span class="chip">Carbon ${fmt(carbonSavings, 0)}g</span>`);
  if (hasValue(auxHeatKwh) && Number(auxHeatKwh.state) > 0)
    chips.push(`<span class="chip chip-warn">Resistive +${fmt(auxHeatKwh, 2)} kWh</span>`);
  if (hasValue(avoidedAuxKwh) && Number(avoidedAuxKwh.state) > 0)
    chips.push(`<span class="chip">Avoided aux ${fmt(avoidedAuxKwh, 2)} kWh</span>`);

  // COP comparison
  let copLine = "";
  if (hasValue(baselineCop) && hasValue(optimizedCop) && hasValue(copImprovement)) {
    copLine = `<div class="cop-compare">COP: ${fmt(optimizedCop)} vs ${fmt(baselineCop)} baseline <span class="cop-gain">(+${fmt(copImprovement, 0)}%)</span></div>`;
  }

  return `
    <div class="card savings-card">
      <div class="savings-header">
        <h2>Savings Today</h2>
        <span class="tier-indicator">${dots} <span class="tier-label-text">${tierInfo.label}</span></span>
      </div>
      ${tierNote}
      <div class="savings-grid">
        ${hasValue(savingsCost) ? `<div class="savings-item"><span class="savings-value">$${fmt(savingsCost, 2)}</span><span class="savings-unit">saved</span></div>` : ""}
        ${hasValue(savingsKwh) ? `<div class="savings-item"><span class="savings-value">${fmt(savingsKwh, 2)}</span><span class="savings-unit">kWh</span></div>` : ""}
        ${hasValue(savingsCo2) ? `<div class="savings-item"><span class="savings-value">${fmt(savingsCo2, 0)}</span><span class="savings-unit">g CO\u2082</span></div>` : ""}
        ${hasValue(comfortHours) ? `<div class="savings-item"><span class="savings-value">+${fmt(comfortHours)}</span><span class="savings-unit">comfort hrs</span></div>` : ""}
      </div>
      ${contextBar}
      ${chips.length > 0 ? `<div class="chips-row">${chips.join("")}</div>` : ""}
      ${copLine}
      <div class="alltime-row">
        All time:
        ${hasValue(cumulCost) ? ` $${fmt(cumulCost, 2)}` : ""}
        ${hasValue(cumulKwh) ? ` \u00b7 ${fmt(cumulKwh, 1)} kWh` : ""}
        ${hasValue(cumulCo2) ? ` \u00b7 ${fmt(cumulCo2, 1)} kg CO\u2082` : ""}
      </div>
    </div>`;
}

/** [F] System Health Card — learning, confidence, diagnostics. */
function renderHealthCard(states, hass) {
  const confidence = findEntity(states, "model_confidence");
  const progress = findEntity(states, "learning_progress");
  const baselineConf = findEntity(states, "baseline_confidence");
  const sourceHealth = findEntity(states, "source_health");
  const accuracy = findEntity(states, "model_accuracy");
  const learningActive = findBinary(states, "learning_active");
  const unit = tempUnit(hass);

  const confPct = hasValue(confidence) ? fmt(confidence, 0) : "0";

  // Day count from baseline_confidence attributes
  let dayInfo = "";
  if (baselineConf?.attributes) {
    const a = baselineConf.attributes;
    if (a.sample_days != null) {
      dayInfo = `Day ${a.sample_days}`;
      if (a.days_remaining != null && a.days_remaining > 0)
        dayInfo += ` (${a.days_remaining} remaining)`;
    }
  }

  // Source health coloring
  let healthCls = "health-ok";
  if (isAvailable(sourceHealth)) {
    const m = sourceHealth.state.match(/(\d+)\/(\d+)/);
    if (m && Number(m[1]) < Number(m[2])) healthCls = "health-warn";
    if (m && Number(m[1]) === 0) healthCls = "health-error";
  }

  // Remaining diagnostics (baseline/comfort — thermal params moved to Building card)
  const baselineTemp = findEntity(states, "baseline_avg_indoor_temp");
  const baselineViols = findEntity(states, "baseline_comfort_violations");
  const profiler = findEntity(states, "profiler_status");

  let diagnosticsSection = "";
  const diagRows = [];

  // Profiler status (useful both during and after learning)
  if (isAvailable(profiler)) {
    const profConf = profiler.attributes?.confidence;
    const profObs = profiler.attributes?.observations;
    let profLine = profiler.state;
    if (profConf != null) profLine += ` \u00b7 ${Number(profConf).toFixed(0)}% confident`;
    if (profObs != null) profLine += ` \u00b7 ${Number(profObs).toLocaleString()} obs`;
    diagRows.push(`<div class="diag-row"><span>Profiler</span><span>${profLine}</span></div>`);
  }

  // Override intelligence
  if (profiler?.attributes?.override_count_30d > 0) {
    const count = profiler.attributes.override_count_30d;
    const pattern = profiler.attributes.override_pattern;
    let overrideLine = `${count} override${count !== 1 ? "s" : ""} in 30 days`;
    diagRows.push(`<div class="diag-row"><span>Overrides</span><span>${overrideLine}</span></div>`);
    if (pattern) {
      diagRows.push(`<div class="diag-row diag-row-sub"><span>Pattern</span><span>${pattern}</span></div>`);
    }
  }

  if (hasValue(baselineTemp)) diagRows.push(`<div class="diag-row"><span>Baseline avg temp</span><span>${fmt(baselineTemp)}${unit}</span></div>`);
  if (hasValue(baselineViols)) diagRows.push(`<div class="diag-row"><span>Baseline comfort violations</span><span>${fmt(baselineViols, 0)}</span></div>`);

  // Aux heat learner diagnostics
  const auxThreshSensor = findEntity(states, "aux_heat_threshold");
  if (hasValue(auxThreshSensor)) {
    const evCount = auxThreshSensor.attributes?.event_count || 0;
    diagRows.push(`<div class="diag-row"><span>Aux threshold</span><span>${fmt(auxThreshSensor, 0)}° eff (${evCount} events)</span></div>`);
  }
  const auxHpWatts = auxThreshSensor?.attributes?.learned_hp_watts;
  if (auxHpWatts != null) {
    diagRows.push(`<div class="diag-row diag-row-sub"><span>HP baseline</span><span>${(auxHpWatts / 1000).toFixed(1)} kW</span></div>`);
  }
  if (diagRows.length > 0) {
    diagnosticsSection = `
      <details class="diag-details">
        <summary class="diag-summary">Diagnostics</summary>
        <div class="diag-grid">${diagRows.join("")}</div>
      </details>`;
  }

  return `
    <div class="card health-card">
      <h2>System Health</h2>
      <div class="progress-label">
        <span>${isAvailable(progress) ? progress.state : "Initializing..."}</span>
        <span>${confPct}% confidence</span>
      </div>
      <div class="progress-bar">
        <div class="progress-fill" style="width:${confPct}%"></div>
      </div>
      ${dayInfo ? `<div class="day-info">${dayInfo}</div>` : ""}
      ${isAvailable(baselineConf) && learningActive?.state === "on" ? `
      <div class="stat-row">
        <span class="label">Baseline captured</span>
        <span class="value">${fmt(baselineConf, 0)}%</span>
      </div>` : ""}
      <div class="health-row">
        ${isAvailable(sourceHealth) ? `
        <div class="health-item ${healthCls}">
          <span class="health-label">Sources</span>
          <span class="health-value">${sourceHealth.state}</span>
        </div>` : ""}
        ${hasValue(accuracy) ? `
        <div class="health-item health-ok">
          <span class="health-label">Prediction</span>
          <span class="health-value">\u00b1${fmt(accuracy)}${unit}</span>
        </div>` : ""}
      </div>
      ${diagnosticsSection}
    </div>`;
}

/** [C] Environment Context — what the optimizer currently "sees". */
function renderEnvironmentCard(states, hass) {
  const outdoor = findEntity(states, "outdoor_temp_source");
  const apparent = findEntity(states, "apparent_temperature");
  const power = findEntity(states, "net_hvac_power");
  const sourceHealth = findEntity(states, "source_health");
  const occupiedRooms = findEntity(states, "occupied_rooms");
  const weightedTemp = findEntity(states, "weighted_indoor_temp");
  const unit = tempUnit(hass);

  const items = [];

  // Outdoor temp source provenance
  if (isAvailable(outdoor)) {
    const count = outdoor.attributes?.entity_count || 0;
    const sub = count > 1 ? `<span class="env-source">${count} sensors avg</span>` : "";
    items.push(`<div class="env-item"><span class="env-label">Outdoor</span><span class="env-value">${fmt(outdoor)}${unit}</span>${sub}</div>`);
  }

  // Indoor humidity
  if (apparent?.attributes?.indoor_humidity != null) {
    items.push(`<div class="env-item"><span class="env-label">Humidity</span><span class="env-value">${Number(apparent.attributes.indoor_humidity).toFixed(0)}%</span></div>`);
  }

  // HVAC power draw
  if (hasValue(power) && Number(power.state) > 0) {
    const kw = (Number(power.state) / 1000).toFixed(1);
    let powerSub = "";
    if (power.attributes) {
      if (power.attributes.solar_offset_w != null && Number(power.attributes.solar_offset_w) > 0) {
        powerSub = `<span class="env-source">${(Number(power.attributes.gross_w || power.state) / 1000).toFixed(1)} kW gross</span>`;
      }
    }
    items.push(`<div class="env-item"><span class="env-label">HVAC Power</span><span class="env-value">${kw} kW</span>${powerSub}</div>`);
  }

  // Source health
  if (isAvailable(sourceHealth)) {
    let healthCls = "env-health-ok";
    const m = sourceHealth.state.match(/(\d+)\/(\d+)/);
    if (m && Number(m[1]) < Number(m[2])) healthCls = "env-health-warn";
    if (m && Number(m[1]) === 0) healthCls = "env-health-error";
    items.push(`<div class="env-item"><span class="env-label">Sources</span><span class="env-value ${healthCls}">${sourceHealth.state}</span></div>`);
  }

  // Electricity rate (from outdoor_temp_source attributes)
  if (outdoor?.attributes?.electricity_rate != null) {
    const rate = Number(outdoor.attributes.electricity_rate);
    items.push(`<div class="env-item"><span class="env-label">Elec Rate</span><span class="env-value">$${rate.toFixed(rate < 0.1 ? 4 : 3)}/kWh</span></div>`);
  }

  // CO2 intensity
  if (outdoor?.attributes?.co2_intensity != null) {
    items.push(`<div class="env-item"><span class="env-label">Grid CO\u2082</span><span class="env-value">${Number(outdoor.attributes.co2_intensity).toFixed(0)} g/kWh</span></div>`);
  }

  // Wind speed
  if (outdoor?.attributes?.wind_speed_mph != null) {
    items.push(`<div class="env-item"><span class="env-label">Wind</span><span class="env-value">${Number(outdoor.attributes.wind_speed_mph).toFixed(0)} mph</span></div>`);
  }

  // Solar irradiance
  if (outdoor?.attributes?.solar_irradiance != null && Number(outdoor.attributes.solar_irradiance) > 0) {
    items.push(`<div class="env-item"><span class="env-label">Solar</span><span class="env-value">${Number(outdoor.attributes.solar_irradiance).toFixed(0)} W/m\u00b2</span></div>`);
  }

  if (items.length === 0) return "";

  // Room-aware occupancy pills
  let roomPills = "";
  if (isAvailable(occupiedRooms) && occupiedRooms.attributes?.areas) {
    const areas = occupiedRooms.attributes.areas;
    const pills = areas.map(a => {
      const occupied = a.occupied ? "room-pill-occupied" : "room-pill-empty";
      const tempStr = a.temperature != null ? ` ${Number(a.temperature).toFixed(0)}${unit}` : "";
      return `<span class="room-pill ${occupied}">${a.name}${tempStr}</span>`;
    }).join("");
    roomPills = `<div class="room-pills">${pills}</div>`;
  }

  return `
    <div class="card env-card">
      <h2>Environment</h2>
      <div class="env-grid">${items.join("")}</div>
      ${roomPills}
    </div>`;
}

/** [F] Building Profile — thermal model parameters. */
function renderBuildingCard(states, hass) {
  const rValue = findEntity(states, "envelope_r_value");
  const thermalMass = findEntity(states, "thermal_mass");
  const coolCap = findEntity(states, "cooling_capacity");
  const heatCap = findEntity(states, "heating_capacity");
  const confidence = findEntity(states, "model_confidence");
  const greybox = findEntity(states, "greybox_active");

  const hasData = hasValue(rValue) || hasValue(thermalMass) || hasValue(coolCap) || hasValue(heatCap);
  if (!hasData) return "";

  const confPct = hasValue(confidence) ? Number(confidence.state) : 0;
  const converging = confPct < 50;
  const convergingTag = converging
    ? `<span class="converging-tag">Converging\u2026</span>`
    : "";

  const params = [];

  if (hasValue(rValue)) {
    const rv = Number(rValue.state);
    let quality = "Moderate", qCls = "quality-medium";
    if (rv >= 15) { quality = "Well insulated"; qCls = "quality-good"; }
    else if (rv < 8) { quality = "Leaky"; qCls = "quality-poor"; }
    params.push(`<div class="model-param"><span class="param-label">R-Value</span><span class="param-value">${rv.toFixed(1)}</span><span class="param-quality ${qCls}">${quality}</span></div>`);
  }

  if (hasValue(thermalMass)) {
    const tm = Number(thermalMass.state);
    let quality = "Medium", qCls = "quality-medium";
    if (tm >= 5000) { quality = "Heavy \u2014 masonry"; qCls = "quality-good"; }
    else if (tm < 2000) { quality = "Light \u2014 wood frame"; qCls = "quality-neutral"; }
    params.push(`<div class="model-param"><span class="param-label">Thermal Mass</span><span class="param-value">${tm.toFixed(0)} BTU/\u00b0F</span><span class="param-quality ${qCls}">${quality}</span></div>`);
  }

  if (hasValue(coolCap)) {
    params.push(`<div class="model-param"><span class="param-label">Cooling</span><span class="param-value">${(Number(coolCap.state) / 1000).toFixed(1)}k BTU/hr</span></div>`);
  }

  if (hasValue(heatCap)) {
    params.push(`<div class="model-param"><span class="param-label">Heating</span><span class="param-value">${(Number(heatCap.state) / 1000).toFixed(1)}k BTU/hr</span></div>`);
  }

  // Model type
  let modelType = "";
  if (isAvailable(greybox)) {
    const gba = greybox.attributes || {};
    if (greybox.state === "on" || greybox.state === "true") modelType = "Grey-Box LP";
    else if (gba.using_adaptive === true || gba.using_adaptive === "true") modelType = "Kalman Filter";
    else modelType = "Heuristic";
  }

  return `
    <div class="card building-card">
      <div class="building-header">
        <h2>Your Home</h2>
        ${convergingTag}
      </div>
      <div class="model-params">${params.join("")}</div>
      ${modelType ? `<div class="model-type">${modelType}${confPct > 0 ? ` \u00b7 ${confPct.toFixed(0)}% confidence` : ""}</div>` : ""}
    </div>`;
}

// ── Main Component ───────────────────────────────────────────────────

class HeatPumpOptimizerPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  set narrow(val) {
    this._narrow = val;
  }

  set panel(val) {
    this._panel = val;
  }

  _render() {
    if (!this._hass) return;
    const s = this._hass.states;

    // Preserve <details> open state across re-renders
    const openDetails = new Set();
    this.shadowRoot.querySelectorAll("details[open]").forEach((el) => {
      const key = el.className || el.querySelector("summary")?.textContent;
      if (key) openDetails.add(key);
    });

    this.shadowRoot.innerHTML = `
      <style>${PANEL_CSS}</style>
      <div class="panel">
        <header class="header">
          <h1>Heat Pump Optimizer</h1>
        </header>
        ${renderAlerts(s)}
        ${renderHeroStrip(s, this._hass)}
        ${renderEnvironmentCard(s, this._hass)}
        ${renderForecastChart(s, this._hass)}
        ${renderTimeline(s, this._hass)}
        ${renderDecisionCard(s, this._hass)}
        ${renderBuildingCard(s, this._hass)}
        ${renderSavingsCard(s, this._hass)}
        ${renderHealthCard(s, this._hass)}
      </div>
    `;

    // Restore <details> open state
    openDetails.forEach((key) => {
      this.shadowRoot.querySelectorAll("details").forEach((el) => {
        const elKey = el.className || el.querySelector("summary")?.textContent;
        if (elKey === key) el.setAttribute("open", "");
      });
    });

    this._bindEvents();
  }

  _bindEvents() {
    const btn = this.shadowRoot.getElementById("toggle-optimizer");
    if (btn) {
      const enabled = findEntity(this._hass.states, "enabled");
      if (enabled) {
        const isOn = enabled.state === "on";
        btn.addEventListener("click", () => {
          this._hass.callService("switch", isOn ? "turn_off" : "turn_on", {
            entity_id: enabled.entity_id,
          });
        });
      }
    }
  }
}

// ── Styles ───────────────────────────────────────────────────────────

const PANEL_CSS = `
  :host {
    display: block;
    height: 100%;
    --card-bg: var(--ha-card-background, var(--card-background-color, #fff));
    --text-primary: var(--primary-text-color, #212121);
    --text-secondary: var(--secondary-text-color, #727272);
    --accent: var(--primary-color, #03a9f4);
    --accent-light: color-mix(in srgb, var(--accent) 15%, transparent);
    --green: #4caf50;
    --green-light: color-mix(in srgb, var(--green) 15%, transparent);
    --orange: #ff9800;
    --red: #f44336;
    --blue: #2196f3;
    --border: var(--divider-color, #e0e0e0);
    --radius: 12px;
  }

  .panel {
    max-width: 720px;
    margin: 0 auto;
    padding: 16px;
    font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
    color: var(--text-primary);
  }

  .header {
    margin-bottom: 16px;
  }
  h1 {
    margin: 0;
    font-size: 22px;
    font-weight: 500;
  }
  h2 {
    margin: 0 0 12px 0;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--text-secondary);
  }

  .card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px;
    margin-bottom: 12px;
  }

  /* ── Alerts ── */
  .alert {
    padding: 10px 16px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 500;
    margin-bottom: 12px;
  }
  .alert-error { background: color-mix(in srgb, var(--red) 12%, transparent); color: var(--red); }
  .alert-warning { background: color-mix(in srgb, var(--orange) 12%, transparent); color: var(--orange); }
  .alert-info { background: color-mix(in srgb, var(--orange) 10%, transparent); color: var(--orange); }

  /* ── Hero Strip ── */
  .hero-card {
    background: color-mix(in srgb, var(--accent) 3%, var(--card-bg));
  }
  .hero-row {
    display: flex;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
  }
  .hero-temps {
    display: flex;
    gap: 20px;
    flex: 1;
    flex-wrap: wrap;
  }
  .hero-temp-item {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  .hero-label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-secondary);
    margin-bottom: 2px;
  }
  .hero-value {
    font-size: 20px;
    font-weight: 600;
  }
  .hero-sub {
    font-size: 11px;
    color: var(--text-secondary);
    margin-top: 1px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 100%;
  }
  .tactical-delta {
    font-size: 12px;
    color: var(--accent);
    margin-left: 4px;
    font-weight: 500;
  }

  .phase-badge {
    padding: 6px 14px;
    border-radius: 16px;
    font-size: 13px;
    font-weight: 600;
    white-space: nowrap;
  }
  .phase-active { background: var(--accent-light); color: var(--accent); }
  .phase-coast { background: var(--green-light); color: var(--green); }
  .phase-maintain { background: color-mix(in srgb, var(--text-primary) 10%, transparent); color: var(--text-primary); }
  .phase-idle { background: color-mix(in srgb, var(--text-secondary) 15%, transparent); color: var(--text-secondary); }
  .phase-paused { background: color-mix(in srgb, var(--orange) 15%, transparent); color: var(--orange); }
  .phase-warn { background: color-mix(in srgb, var(--red) 15%, transparent); color: var(--red); }

  .toggle-btn {
    border: none;
    padding: 8px 20px;
    border-radius: 20px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    margin-left: auto;
  }
  .toggle-btn.on { background: var(--green-light); color: var(--green); }
  .toggle-btn.off { background: color-mix(in srgb, var(--red) 15%, transparent); color: var(--red); }
  .toggle-btn:hover { filter: brightness(0.95); }

  /* ── Forecast Chart ── */
  .forecast-card { }
  .forecast-placeholder {
    color: var(--text-secondary);
    font-size: 14px;
    text-align: center;
    padding: 24px 0;
    font-style: italic;
  }
  .chart-legend {
    display: flex;
    gap: 12px;
    margin-bottom: 8px;
    font-size: 11px;
    color: var(--text-secondary);
  }
  .legend-item { display: flex; align-items: center; gap: 4px; }
  .legend-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    display: inline-block;
  }
  .legend-indoor { background: var(--accent); }
  .legend-actual {
    background: transparent;
    border: 2px solid var(--green);
    width: 8px; height: 8px;
    box-sizing: border-box;
  }
  .legend-outdoor { background: var(--text-secondary); }
  .legend-band {
    width: 12px; height: 8px;
    background: color-mix(in srgb, var(--green) 20%, transparent);
    border-radius: 2px;
    display: inline-block;
  }
  .legend-hvac {
    width: 12px; height: 4px;
    background: var(--blue);
    border-radius: 1px;
    display: inline-block;
  }

  .chart-container {
    display: flex;
    height: 140px;
  }
  .chart-yaxis {
    width: 32px;
    position: relative;
    flex-shrink: 0;
  }
  .chart-ylabel {
    position: absolute;
    right: 4px;
    transform: translateY(50%);
    font-size: 10px;
    color: var(--text-secondary);
  }
  .chart-grid {
    flex: 1;
    position: relative;
    display: flex;
    border-left: 1px solid var(--border);
    border-bottom: 1px solid var(--border);
  }
  .chart-gridline {
    position: absolute;
    left: 0;
    right: 0;
    height: 0;
    border-top: 1px dashed color-mix(in srgb, var(--border) 50%, transparent);
  }
  .chart-col {
    flex: 1;
    position: relative;
  }
  .chart-col-now {
    border-left: 2px solid var(--accent);
  }
  .chart-area {
    position: absolute;
    inset: 0;
  }
  .chart-comfort {
    position: absolute;
    left: 0;
    right: 0;
    background: color-mix(in srgb, var(--green) 12%, transparent);
  }
  .chart-dot {
    position: absolute;
    left: 50%;
    transform: translate(-50%, 50%);
    border-radius: 50%;
    z-index: 1;
  }
  .chart-dot-indoor {
    width: 6px; height: 6px;
    background: var(--accent);
  }
  .chart-dot-outdoor {
    width: 4px; height: 4px;
    background: var(--text-secondary);
  }
  .chart-dot-actual {
    width: 10px; height: 10px;
    background: transparent;
    border: 2px solid var(--green);
    z-index: 2;
  }
  .chart-hvac {
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    height: 4px;
  }
  .chart-hvac-cool { background: var(--blue); }
  .chart-hvac-heat { background: var(--orange); }
  .chart-time {
    position: absolute;
    bottom: -16px;
    left: 0;
    font-size: 9px;
    color: var(--text-secondary);
    transform: translateX(-50%);
  }

  /* ── Schedule Timeline ── */
  .timeline-card { }
  .timeline-empty, .timeline-next {
    font-size: 13px;
    color: var(--text-secondary);
  }
  .timeline-next { margin-top: 8px; }
  .timeline-precond {
    font-size: 12px;
    color: var(--text-secondary);
    margin-top: 4px;
  }
  .tl-bar {
    position: relative;
    height: 28px;
    background: color-mix(in srgb, var(--border) 40%, transparent);
    border-radius: 6px;
    overflow: hidden;
  }
  .tl-segment {
    position: absolute;
    top: 0;
    height: 100%;
    opacity: 0.85;
  }
  .tl-segment.phase-active, .tl-legend-swatch.phase-active { background: var(--accent); }
  .tl-segment.phase-coast, .tl-legend-swatch.phase-coast { background: var(--green); }
  .tl-segment.phase-maintain, .tl-legend-swatch.phase-maintain { background: color-mix(in srgb, var(--text-primary) 30%, transparent); }
  .tl-segment.phase-idle, .tl-legend-swatch.phase-idle { background: color-mix(in srgb, var(--text-secondary) 20%, transparent); }
  .tl-segment.phase-paused, .tl-legend-swatch.phase-paused { background: var(--orange); }
  .tl-segment.phase-warn, .tl-legend-swatch.phase-warn { background: var(--red); }
  .tl-now {
    position: absolute;
    top: 0;
    width: 2px;
    height: 100%;
    background: var(--red);
    z-index: 2;
  }
  .tl-occ-bar {
    position: relative;
    height: 10px;
    background: color-mix(in srgb, var(--border) 25%, transparent);
    border-radius: 4px;
    overflow: hidden;
    margin-top: 3px;
  }
  .tl-occ-seg {
    position: absolute;
    top: 0;
    height: 100%;
  }
  .tl-occ-home {
    background: color-mix(in srgb, var(--green) 25%, transparent);
  }
  .tl-occ-away {
    background: color-mix(in srgb, var(--text-secondary) 12%, transparent);
  }
  .tl-ticks {
    position: relative;
    height: 14px;
    margin-top: 2px;
  }
  .tl-tick {
    position: absolute;
    font-size: 9px;
    color: var(--text-secondary);
    transform: translateX(-50%);
  }
  .tl-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 8px;
    font-size: 11px;
    color: var(--text-secondary);
  }
  .tl-legend-item {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .tl-legend-swatch {
    width: 10px;
    height: 10px;
    border-radius: 3px;
    display: inline-block;
    opacity: 0.85;
  }
  .tl-seg-label {
    font-size: 9px;
    color: #fff;
    text-shadow: 0 1px 2px rgba(0,0,0,0.3);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    padding: 0 4px;
    line-height: 28px;
    display: block;
  }

  /* ── Savings Card ── */
  .savings-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .savings-header h2 { margin-bottom: 0; }
  .tier-indicator {
    display: flex;
    align-items: center;
    gap: 3px;
    font-size: 11px;
    color: var(--text-secondary);
  }
  .tier-dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: color-mix(in srgb, var(--text-secondary) 25%, transparent);
    display: inline-block;
  }
  .tier-dot.filled { background: var(--green); }
  .tier-label-text { margin-left: 4px; }
  .tier-note {
    font-size: 11px;
    color: var(--text-secondary);
    margin-top: 4px;
    font-style: italic;
  }

  .savings-grid {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    margin: 12px 0;
  }
  .savings-item {
    flex: 1;
    min-width: 80px;
    padding: 8px 12px;
    background: color-mix(in srgb, var(--accent) 5%, transparent);
    border-radius: 8px;
    text-align: center;
  }
  .savings-value {
    display: block;
    font-size: 20px;
    font-weight: 700;
    color: var(--accent);
  }
  .savings-unit {
    display: block;
    font-size: 11px;
    color: var(--text-secondary);
    margin-top: 2px;
  }

  .context-bar-wrap {
    margin: 8px 0;
  }
  .context-bar {
    height: 6px;
    background: color-mix(in srgb, var(--green) 20%, transparent);
    border-radius: 3px;
    overflow: hidden;
  }
  .context-bar-used {
    height: 100%;
    background: var(--accent);
    border-radius: 3px;
    transition: width 0.6s ease;
  }
  .context-bar-label {
    font-size: 12px;
    color: var(--green);
    font-weight: 500;
    margin-top: 4px;
    display: block;
  }

  .chips-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin: 8px 0;
  }
  .chip {
    padding: 3px 10px;
    border-radius: 12px;
    font-size: 11px;
    background: color-mix(in srgb, var(--accent) 8%, transparent);
    color: var(--text-secondary);
  }
  .chip-warn {
    background: color-mix(in srgb, var(--orange, #f59e0b) 12%, transparent);
    color: color-mix(in srgb, var(--orange, #f59e0b) 80%, var(--text-secondary));
  }

  .cop-compare {
    font-size: 13px;
    color: var(--text-secondary);
    margin: 6px 0;
  }
  .cop-gain {
    color: var(--green);
    font-weight: 500;
  }

  .alltime-row {
    font-size: 12px;
    color: var(--text-secondary);
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px solid color-mix(in srgb, var(--border) 50%, transparent);
  }

  /* ── Health Card ── */
  .progress-label {
    display: flex;
    justify-content: space-between;
    font-size: 13px;
    margin-bottom: 6px;
  }
  .progress-bar {
    height: 6px;
    background: color-mix(in srgb, var(--border) 60%, transparent);
    border-radius: 3px;
    overflow: hidden;
  }
  .progress-fill {
    height: 100%;
    background: var(--accent);
    border-radius: 3px;
    transition: width 0.6s ease;
  }

  .day-info {
    font-size: 12px;
    color: var(--text-secondary);
    margin-top: 6px;
  }

  .stat-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 0;
  }
  .label {
    color: var(--text-secondary);
    font-size: 13px;
  }
  .value {
    font-size: 14px;
    font-weight: 500;
  }

  .health-row {
    display: flex;
    gap: 16px;
    margin-top: 10px;
  }
  .health-item {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .health-label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-secondary);
  }
  .health-value {
    font-size: 14px;
    font-weight: 500;
  }
  .health-ok .health-value { color: var(--green); }
  .health-warn .health-value { color: var(--orange); }
  .health-error .health-value { color: var(--red); }

  /* ── Diagnostics ── */
  .diag-details {
    margin-top: 12px;
    border-top: 1px solid color-mix(in srgb, var(--border) 50%, transparent);
    padding-top: 8px;
  }
  .diag-summary {
    font-size: 12px;
    color: var(--text-secondary);
    cursor: pointer;
    user-select: none;
    padding: 4px 0;
  }
  .diag-summary:hover { color: var(--text-primary); }
  .diag-grid {
    margin-top: 8px;
  }
  .diag-row {
    display: flex;
    justify-content: space-between;
    padding: 3px 0;
    font-size: 12px;
    color: var(--text-secondary);
  }
  .diag-row + .diag-row {
    border-top: 1px solid color-mix(in srgb, var(--border) 30%, transparent);
  }
  .diag-row-sub {
    font-style: italic;
    padding-left: 12px;
  }

  /* ── Hero badges row ── */
  .hero-badges {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }
  .occupancy-chip, .power-chip, .appliance-chip {
    padding: 4px 10px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 500;
  }
  .occ-home { background: var(--green-light); color: var(--green); }
  .occ-away { background: color-mix(in srgb, var(--text-secondary) 12%, transparent); color: var(--text-secondary); }
  .power-chip { background: color-mix(in srgb, var(--orange) 12%, transparent); color: var(--orange); }
  .appliance-cooling { background: color-mix(in srgb, var(--blue) 12%, transparent); color: var(--blue); }
  .appliance-heating { background: color-mix(in srgb, var(--red) 12%, transparent); color: var(--red); }

  /* ── Environment Card ── */
  .env-card { }
  .env-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
    gap: 8px;
  }
  .env-item {
    display: flex;
    flex-direction: column;
    padding: 8px 10px;
    background: color-mix(in srgb, var(--accent) 4%, transparent);
    border-radius: 8px;
  }
  .env-label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-secondary);
    margin-bottom: 2px;
  }
  .env-value {
    font-size: 14px;
    font-weight: 600;
  }
  .env-source {
    font-size: 10px;
    color: var(--text-secondary);
    margin-top: 1px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 100%;
  }
  .env-health-ok { color: var(--green); }
  .env-health-warn { color: var(--orange); }
  .env-health-error { color: var(--red); }

  .room-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 10px;
  }
  .room-pill {
    padding: 3px 10px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 500;
  }
  .room-pill-occupied { background: var(--green-light); color: var(--green); }
  .room-pill-empty { background: color-mix(in srgb, var(--text-secondary) 10%, transparent); color: var(--text-secondary); }

  /* ── Decision Engine Card ── */
  .decision-card { }
  .decision-top {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 12px;
  }
  .tactical-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
    flex-shrink: 0;
  }
  .tac-nominal { background: var(--green-light); color: var(--green); }
  .tac-correcting { background: var(--accent-light); color: var(--accent); }
  .tac-disturbed { background: color-mix(in srgb, var(--orange) 15%, transparent); color: var(--orange); }
  .decision-narrative {
    font-size: 13px;
    color: var(--text-secondary);
    line-height: 1.4;
    padding-top: 2px;
  }
  .decision-stats {
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 8px;
  }
  .decision-stat {
    display: flex;
    flex-direction: column;
    background: color-mix(in srgb, var(--text-secondary) 6%, transparent);
    border-radius: 8px;
    padding: 8px 14px;
    min-width: 120px;
  }
  .decision-stat-label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-secondary);
    margin-bottom: 2px;
  }
  .decision-stat-value {
    font-size: 13px;
    font-weight: 500;
  }
  .decision-next {
    border-top: 1px solid var(--border);
    padding-top: 10px;
    margin-top: 4px;
  }
  .decision-next-line {
    font-size: 13px;
    font-weight: 500;
  }
  .decision-precond {
    font-size: 12px;
    color: var(--text-secondary);
    margin-top: 2px;
  }

  /* ── Building Profile Card ── */
  .building-card { }
  .building-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .building-header h2 { margin-bottom: 0; }
  .converging-tag {
    font-size: 11px;
    color: var(--orange);
    font-weight: 500;
    animation: pulse-fade 2s ease-in-out infinite;
  }
  @keyframes pulse-fade {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
  .model-params {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 8px;
    margin-top: 12px;
  }
  .model-param {
    display: flex;
    flex-direction: column;
    padding: 8px 10px;
    background: color-mix(in srgb, var(--accent) 4%, transparent);
    border-radius: 8px;
  }
  .param-label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-secondary);
    margin-bottom: 2px;
  }
  .param-value {
    font-size: 15px;
    font-weight: 600;
  }
  .param-quality {
    font-size: 11px;
    margin-top: 2px;
  }
  .quality-good { color: var(--green); }
  .quality-medium { color: var(--orange); }
  .quality-neutral { color: var(--text-secondary); }
  .quality-poor { color: var(--red); }
  .model-type {
    font-size: 12px;
    color: var(--text-secondary);
    margin-top: 10px;
  }

  /* ── Forecast weather-only additions ── */
  .chart-dot-weather {
    width: 5px;
    height: 5px;
  }
  .forecast-note {
    font-size: 11px;
    color: var(--text-secondary);
    text-align: center;
    margin-top: 24px;
    font-style: italic;
  }

  /* ── Calibration Progress (in timeline card) ── */
  .learn-bars {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .learn-bar-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .learn-bar-label {
    font-size: 12px;
    color: var(--text-secondary);
    width: 90px;
    flex-shrink: 0;
  }
  .learn-bar-track {
    flex: 1;
    height: 6px;
    background: color-mix(in srgb, var(--border) 60%, transparent);
    border-radius: 3px;
    overflow: hidden;
  }
  .learn-bar-fill {
    height: 100%;
    border-radius: 3px;
    transition: width 0.6s ease;
  }
  .learn-bar-baseline { background: var(--green); }
  .learn-bar-model { background: var(--accent); }
  .learn-bar-profiler { background: var(--orange); }
  .learn-bar-pct {
    font-size: 12px;
    color: var(--text-secondary);
    width: 35px;
    text-align: right;
    flex-shrink: 0;
  }
  .learn-status {
    font-size: 13px;
    color: var(--text-secondary);
    margin-top: 8px;
  }
  .learn-obs {
    font-size: 12px;
    color: var(--text-secondary);
    margin-top: 4px;
  }
`;

customElements.define("heatpump-optimizer-panel", HeatPumpOptimizerPanel);
