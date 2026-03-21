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

  const learning = findEntity(states, "learning_progress");
  if (learning?.state?.startsWith("Day ") && learning.state.includes("Observing")) {
    alerts.push({ cls: "alert-info", msg: `${learning.state} \u2014 thermostat unchanged` });
  } else if (learning?.state?.startsWith("Baseline captured")) {
    alerts.push({ cls: "alert-info", msg: `${learning.state}` });
  }

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
      <div class="hero-status-row">
        <div class="hero-badges">
          <span class="phase-badge ${phaseInfo.cls}">${phaseInfo.label}</span>
          ${occupancyChip}
          ${powerChip}
        </div>
        <button class="toggle-btn ${isEnabled ? "on" : "off"}" id="toggle-optimizer">
          ${isEnabled ? "Enabled" : "Disabled"}
        </button>
      </div>
      ${applianceChip ? `<div class="hero-appliance-row">${applianceChip}</div>` : ""}
      <div class="hero-temps">
        <div class="hero-temp-item">
          <span class="hero-label">Indoor</span>
          <span class="hero-value">${fmt(apparent)}${unit}</span>
          ${indoorSub}
        </div>
        <div class="hero-temp-item hero-temp-mid">
          <span class="hero-label">Setpoint</span>
          <span class="hero-value">${fmt(setpoint)}${unit}${tacticalNote}</span>
        </div>
        ${isAvailable(outdoor) ? `
        <div class="hero-temp-item hero-temp-end">
          <span class="hero-label">Outdoor</span>
          <span class="hero-value">${fmt(outdoor)}${unit}</span>
        </div>` : ""}
      </div>
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
    ? `<div class="tier-note">These numbers will get more accurate as the model learns your patterns.</div>`
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
  const profiler = findEntity(states, "profiler_status") || findEntity(states, "profiler");

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
    diagRows.push(`<div class="diag-row"><span>Aux threshold</span><span>${fmt(auxThreshSensor, 0)}\u00b0 eff (${evCount} events)</span></div>`);
  }
  const auxHpWatts = auxThreshSensor?.attributes?.learned_hp_watts;
  if (auxHpWatts != null) {
    diagRows.push(`<div class="diag-row diag-row-sub"><span>HP baseline</span><span>${(auxHpWatts / 1000).toFixed(1)} kW</span></div>`);
  }

  // ── Power user: Model accuracy detail ──
  if (hasValue(accuracy) && accuracy.attributes) {
    const a = accuracy.attributes;
    if (a.model_bias != null) {
      const b = Number(a.model_bias);
      const bLabel = Math.abs(b) < 0.15 ? "no bias" : b > 0 ? "running warm" : "running cool";
      diagRows.push(`<div class="diag-row"><span>Model bias</span><span>${b >= 0 ? "+" : ""}${b.toFixed(2)}${unit} \u00b7 ${bLabel}</span></div>`);
    }
    if (a.correction != null && Math.abs(Number(a.correction) - 1.0) > 0.05) {
      const c = Number(a.correction);
      const cCls = Math.abs(c - 1.0) > 0.25 ? "diag-warn" : "";
      diagRows.push(`<div class="diag-row ${cCls}"><span>Model correction</span><span>${c.toFixed(2)}\u00d7 ${Math.abs(c - 1.0) > 0.25 ? "\u26a0 large drift" : ""}</span></div>`);
    }
    if (a.sample_count != null) {
      diagRows.push(`<div class="diag-row diag-row-sub"><span>Accuracy samples</span><span>${Number(a.sample_count).toLocaleString()}</span></div>`);
    }
  }

  // ── Power user: Per-source sensor health table ──
  let sourceDetailSection = "";
  if (sourceHealth?.attributes?.sources && Array.isArray(sourceHealth.attributes.sources)) {
    const srcRows = sourceHealth.attributes.sources.map(src => {
      const ok = src.ok !== false;
      const cls = ok ? "src-ok" : "src-fail";
      const lastSeen = src.last_updated
        ? (() => {
            const ageMs = Date.now() - new Date(src.last_updated).getTime();
            const ageMin = Math.floor(ageMs / 60000);
            return ageMin < 2 ? "now" : ageMin < 60 ? `${ageMin}m ago` : `${Math.floor(ageMin / 60)}h ago`;
          })()
        : "\u2014";
      const fallback = src.is_fallback ? ` <span class="src-fallback">(fallback)</span>` : "";
      return `<div class="src-row">
        <span class="src-dot ${cls}"></span>
        <span class="src-name">${src.name || src.entity_id || "?"}</span>
        <span class="src-age">${lastSeen}${fallback}</span>
        ${src.value != null ? `<span class="src-val">${Number(src.value).toFixed(1)}</span>` : ""}
      </div>`;
    }).join("");
    sourceDetailSection = `
      <details class="diag-details">
        <summary class="diag-summary">Sensor Sources</summary>
        <div class="src-table">${srcRows}</div>
      </details>`;
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
      ${sourceDetailSection}
    </div>`;
}

/** [C] Environment Context — what the optimizer currently "sees". */
function renderEnvironmentCard(states, hass) {
  const outdoor = findEntity(states, "outdoor_temp_source");
  const power = findEntity(states, "net_hvac_power");
  const sourceHealth = findEntity(states, "source_health");
  const occupiedRooms = findEntity(states, "occupied_rooms");
  const unit = tempUnit(hass);

  const items = [];

  // Outdoor temp source provenance
  if (isAvailable(outdoor)) {
    const count = outdoor.attributes?.entity_count || 0;
    const sub = count > 1 ? `<span class="env-source">${count} sensors avg</span>` : "";
    items.push(`<div class="env-item"><span class="env-label">Outdoor</span><span class="env-value">${fmt(outdoor)}${unit}</span>${sub}</div>`);
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

/** Infer rough climate zone from today's weather forecast minimum outdoor temp. */
function getClimateZone(states) {
  const weather = findEntity(states, "schedule")?.attributes?.weather_forecast;
  if (weather && Array.isArray(weather)) {
    const temps = weather.map(p => p.outdoor).filter(v => v != null);
    if (temps.length) {
      const min = Math.min(...temps);
      return min < 20 ? "cold" : min < 40 ? "mixed" : "mild";
    }
  }
  return "mixed";
}

/** [F] Building Profile — thermal model with plain-English narrative + visual scales. */
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
  const zone = getClimateZone(states);

  // ── Climate-relative thresholds ──
  const R_GOOD = zone === "cold" ? 15 : zone === "mixed" ? 12 : 9;
  const R_POOR = zone === "cold" ? 7  : zone === "mixed" ? 5  : 4;
  const R_MAX  = zone === "cold" ? 25 : zone === "mixed" ? 20 : 15;

  // ── Classify parameters ──
  let rBand = "medium", rLabel = "Typical for your region";
  if (hasValue(rValue)) {
    const rv = Number(rValue.state);
    if (rv >= R_GOOD) { rBand = "good"; rLabel = "Well insulated"; }
    else if (rv < R_POOR) { rBand = "poor"; rLabel = "Leaky envelope"; }
  }

  let massBand = "medium", massLabel = "Mixed construction";
  if (hasValue(thermalMass)) {
    const tm = Number(thermalMass.state);
    if (tm >= 5000) { massBand = "high"; massLabel = "Masonry / concrete"; }
    else if (tm < 1500) { massBand = "low"; massLabel = "Wood frame"; }
  }

  let coolSizeLabel = "";
  if (hasValue(coolCap)) {
    const qk = Number(coolCap.state) / 1000;
    coolSizeLabel = qk < 12 ? "< 1 ton" : `${(qk / 12).toFixed(1)} ton`;
  }

  // ── Plain-English narrative ──
  let narrative = "";
  if (hasValue(rValue) && hasValue(thermalMass)) {
    if (rBand === "good" && massBand === "high")
      narrative = "Tight envelope and heavy thermal mass \u2014 holds temperature well, ideal for pre-conditioning ahead of rate changes.";
    else if (rBand === "good" && massBand === "low")
      narrative = "Well insulated but lightweight \u2014 heats and cools fast, best served by just-in-time pre-conditioning.";
    else if (rBand === "good")
      narrative = "Low heat loss lets the optimizer focus on shifting runtime to cheaper, cleaner hours.";
    else if (massBand === "high")
      narrative = "Heavy construction absorbs temperature swings, but the envelope leaks more than average. Rate-timed cycles save the most.";
    else if (rBand === "poor" && massBand === "low")
      narrative = "Responds quickly to outdoor conditions \u2014 the optimizer stays ahead of swings rather than coasting.";
    else if (rBand === "poor")
      narrative = "Higher-than-average heat loss. The optimizer prioritizes running during the cheapest, most efficient windows.";
    else
      narrative = "Typical thermal characteristics \u2014 the optimizer balances pre-conditioning, rate windows, and occupancy.";
  }

  // ── Position bar: track with a dot marker ──
  function posBar(pct, colorVar) {
    const clamped = Math.max(3, Math.min(97, pct));
    return `<div class="profile-bar-track"><div class="profile-bar-marker" style="left:${clamped}%;background:${colorVar}"></div></div>`;
  }

  // ── Profile rows ──
  const profileRows = [];

  if (hasValue(rValue)) {
    const pct = Math.min(100, (Number(rValue.state) / R_MAX) * 100);
    const color = rBand === "good" ? "var(--green)" : rBand === "poor" ? "var(--red)" : "var(--orange)";
    const qCls = rBand === "good" ? "quality-good" : rBand === "poor" ? "quality-poor" : "quality-medium";
    profileRows.push(`
      <div class="profile-row">
        <div class="profile-row-header">
          <span class="profile-label">Insulation</span>
          <span class="profile-qual ${qCls}">${rLabel}</span>
        </div>
        ${posBar(pct, color)}
        <div class="profile-scale"><span>Leaky</span><span>Well insulated</span></div>
      </div>`);
  }

  if (hasValue(thermalMass)) {
    const tm = Number(thermalMass.state);
    const pct = Math.min(100, (tm / 10000) * 100);
    const color = massBand === "high" ? "var(--green)" : massBand === "low" ? "var(--text-secondary)" : "var(--accent)";
    profileRows.push(`
      <div class="profile-row">
        <div class="profile-row-header">
          <span class="profile-label">Thermal Mass</span>
          <span class="profile-qual">${massLabel}</span>
        </div>
        ${posBar(pct, color)}
        <div class="profile-scale"><span>Light frame</span><span>Heavy masonry</span></div>
      </div>`);
  }

  if (hasValue(coolCap)) {
    const pct = Math.min(100, (Number(coolCap.state) / 60000) * 100);
    profileRows.push(`
      <div class="profile-row">
        <div class="profile-row-header">
          <span class="profile-label">HVAC Capacity</span>
          <span class="profile-qual">${coolSizeLabel}</span>
        </div>
        ${posBar(pct, "var(--blue)")}
        <div class="profile-scale"><span>Smaller</span><span>Larger</span></div>
      </div>`);
  }

  // Solar gain — from model_confidence attributes or show estimating state
  const solarGain = confidence?.attributes?.solar_gain_btu;
  if (solarGain != null) {
    const sg = Number(solarGain);
    let solarDesc;
    if (sg < 1000) solarDesc = "Minimal exposure";
    else if (sg < 3000) solarDesc = "Partial shade";
    else if (sg < 6000) solarDesc = "Typical exposure";
    else solarDesc = "High exposure";
    const pct = Math.min(100, (sg / 10000) * 100);
    profileRows.push(`
      <div class="profile-row">
        <div class="profile-row-header">
          <span class="profile-label">Solar Gain</span>
          <span class="profile-qual">${solarDesc}</span>
        </div>
        ${posBar(pct, "var(--orange)")}
        <div class="profile-scale"><span>Minimal</span><span>High</span></div>
      </div>`);
  } else {
    profileRows.push(`
      <div class="profile-row">
        <div class="profile-row-header">
          <span class="profile-label">Solar Gain</span>
          <span class="profile-qual quality-medium">Measuring\u2026</span>
        </div>
      </div>`);
  }

  // ── Raw values (collapsible, power users) ──
  const rawRows = [];
  if (hasValue(rValue)) rawRows.push(`<div class="diag-row"><span>R-Value</span><span>${Number(rValue.state).toFixed(1)} hr\u00b7\u00b0F/BTU</span></div>`);
  if (hasValue(thermalMass)) rawRows.push(`<div class="diag-row"><span>Thermal Mass</span><span>${Number(thermalMass.state).toFixed(0)} BTU/\u00b0F</span></div>`);
  if (hasValue(coolCap)) rawRows.push(`<div class="diag-row"><span>Cooling</span><span>${(Number(coolCap.state) / 1000).toFixed(1)}k BTU/hr</span></div>`);
  if (hasValue(heatCap)) rawRows.push(`<div class="diag-row"><span>Heating</span><span>${(Number(heatCap.state) / 1000).toFixed(1)}k BTU/hr</span></div>`);
  if (solarGain != null) rawRows.push(`<div class="diag-row"><span>Solar Gain</span><span>${Number(solarGain).toFixed(0)} BTU/hr peak</span></div>`);

  // ── Model type line ──
  let modelTypeLine = "";
  if (isAvailable(greybox)) {
    const gba = greybox.attributes || {};
    let mt;
    if (greybox.state === "on" || greybox.state === "true") mt = "Grey-Box LP";
    else if (gba.using_adaptive === true || gba.using_adaptive === "true") mt = "Kalman Filter";
    else mt = "Heuristic";
    modelTypeLine = `<div class="model-type">${mt}${confPct > 0 ? ` \u00b7 ${confPct.toFixed(0)}% confidence` : ""}</div>`;
  }

  // ── Parameter uncertainty (power users) ──
  let uncertaintySection = "";
  if (confidence?.attributes?.parameter_uncertainty && !converging) {
    const pu = confidence.attributes.parameter_uncertainty;
    const puRows = Object.entries(pu).map(([k, v]) => {
      const label = { r_inv: "R (conductance)", c_inv: "C (air mass)", q_cool: "Cooling cap", q_heat: "Heating cap", solar: "Solar gain" }[k] || k;
      return `<div class="diag-row"><span>${label}</span><span>\u00b1${Number(v).toExponential(1)}</span></div>`;
    }).join("");
    if (puRows) {
      uncertaintySection = `
        <details class="diag-details">
          <summary class="diag-summary">Parameter Uncertainty</summary>
          <div class="diag-grid">${puRows}</div>
        </details>`;
    }
  }

  return `
    <div class="card building-card">
      <div class="building-header">
        <h2>Building Profile</h2>
        ${converging ? `<span class="converging-tag">Learning</span>` : ""}
      </div>
      ${narrative ? `<p class="profile-narrative">${narrative}</p>` : ""}
      <div class="profile-rows">${profileRows.join("")}</div>
      ${rawRows.length ? `
        <details class="diag-details">
          <summary class="diag-summary">Raw values</summary>
          <div class="diag-grid">${rawRows.join("")}</div>
        </details>` : ""}
      ${modelTypeLine}
      ${uncertaintySection}
    </div>`;
}

/** [F2] House Thermal Load — stacked bar breakdown of passive heat flows. */
function renderThermalLoadCard(states, _hass) {
  const loadEntity = findEntity(states, "house_thermal_load");
  if (!loadEntity || !hasValue(loadEntity)) return "";

  const attrs = loadEntity.attributes || {};
  const netLoad = Number(loadEntity.state);
  const confidence = attrs.model_confidence ?? 0;
  const converging = confidence < 50;

  // Format BTU/hr with comma separators and sign
  function fmtBtu(n) {
    const abs = Math.abs(Math.round(n));
    const str = abs.toLocaleString("en-US");
    return n >= 0 ? `+${str}` : `\u2212${str}`;
  }

  // Component data from the primary sensor's attributes
  const components = [
    { key: "weather", label: "Weather", value: attrs.weather_heat_transfer ?? 0, color: "var(--orange)" },
    { key: "solar", label: "Solar", value: attrs.solar_heat_gain ?? 0, color: "#fdd835" },
    { key: "people", label: "People", value: attrs.occupancy_heat_gain ?? 0, color: "var(--accent)" },
    { key: "boundary", label: "Boundary", value: attrs.boundary_zone_heat_transfer ?? 0, color: "#8d6e63" },
    { key: "appliances", label: "Appliances", value: attrs.appliance_load ?? 0, color: "var(--blue)" },
  ];

  // Filter out zero-value components and split by direction
  const active = components.filter(c => Math.abs(c.value) > 0.5);
  const gains = active.filter(c => c.value > 0);
  const removals = active.filter(c => c.value < 0);

  // Build a stacked bar from a set of components
  function stackedBar(items, cssExtra) {
    const total = items.reduce((s, c) => s + Math.abs(c.value), 0);
    if (total === 0) return "";
    let left = 0;
    const segs = items.map(c => {
      const pct = Math.max(2, (Math.abs(c.value) / total) * 100);
      const seg = `<div class="tl-bar-seg" style="left:${left}%;width:${pct}%;background:${c.color}"></div>`;
      left += pct;
      return seg;
    }).join("");
    return `<div class="tl-stacked-bar${cssExtra || ""}">${segs}</div>`;
  }

  // Direction label
  let direction = "balanced";
  if (netLoad > 50) direction = "gaining heat";
  else if (netLoad < -50) direction = "losing heat";

  // Gain bar
  let gainSection = "";
  if (gains.length > 0) {
    gainSection = `
      <div class="tl-bar-label">Adding heat</div>
      ${stackedBar(gains, converging ? " tl-converging" : "")}`;
  }

  // Removal bar
  let removalSection = "";
  if (removals.length > 0) {
    removalSection = `
      <div class="tl-bar-label">Removing heat</div>
      ${stackedBar(removals, converging ? " tl-converging" : "")}`;
  }

  // Legend — only active components
  const legendItems = active.map(c =>
    `<span class="tl-load-legend-item"><span class="tl-load-legend-dot" style="background:${c.color}"></span>${c.label}</span>`
  ).join("");

  // Collapsible details — all components including HVAC and stored heat
  const detailRows = [
    ...components.map(c =>
      `<div class="diag-row"><span>${c.label}</span><span>${fmtBtu(c.value)} BTU/hr</span></div>`
    ),
    attrs.hvac_output != null
      ? `<div class="diag-row"><span>HVAC Output</span><span>${fmtBtu(attrs.hvac_output)} BTU/hr</span></div>` : "",
    attrs.stored_heat_exchange != null
      ? `<div class="diag-row"><span>Stored Heat</span><span>${fmtBtu(attrs.stored_heat_exchange)} BTU/hr</span></div>` : "",
  ].filter(Boolean).join("");

  return `
    <div class="card thermal-load-card">
      <div class="building-header">
        <h2>House Thermal Load</h2>
        ${converging ? `<span class="converging-tag">Learning</span>` : ""}
      </div>
      <div class="tl-headline">
        <span class="tl-headline-value">${fmtBtu(netLoad)} BTU/hr</span>
        <span class="tl-headline-dir">${direction}</span>
      </div>
      ${gainSection}
      ${removalSection}
      <div class="tl-load-legend">${legendItems}</div>
      ${detailRows ? `
        <details class="diag-details thermal-load-details">
          <summary class="diag-summary">Component Details</summary>
          <div class="diag-grid">${detailRows}</div>
        </details>` : ""}
    </div>`;
}

// ── Learning Mode Renderers ──────────────────────────────────────────

/** Helper: bucket a history series into N equal-time slots and average values. */
function bucketHistory(series, startMs, numBuckets, bucketMs) {
  const buckets = Array.from({ length: numBuckets }, () => []);
  for (const pt of series) {
    const raw = pt.last_changed;
    const ts = typeof raw === "number" ? raw * 1000 : new Date(raw).getTime();
    const v = Number(pt.state);
    if (ts < startMs || isNaN(v)) continue;
    const idx = Math.min(numBuckets - 1, Math.floor((ts - startMs) / bucketMs));
    buckets[idx].push(v);
  }
  return buckets.map(vals => vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null);
}

/** Compute HVAC state per bucket from retro history: 'aux', 'heat', 'cool', or null. */
function computeHvacBuckets(retro, startMs, numBuckets, bucketMs) {
  const ptTs = pt => { const r = pt.last_changed; return typeof r === "number" ? r * 1000 : new Date(r).getTime(); };

  // Build hvac_action lookup: find the most recent action at any timestamp
  const actionTimeline = (retro.hvacAction || []).map(pt => ({ ts: ptTs(pt), action: pt.state })).sort((a, b) => a.ts - b.ts);
  const getAction = (ts) => {
    let action = "idle";
    for (const entry of actionTimeline) {
      if (entry.ts > ts) break;
      action = entry.action;
    }
    return action;
  };

  return Array.from({ length: numBuckets }, (_, i) => {
    const bStart = startMs + i * bucketMs;
    const bEnd = bStart + bucketMs;

    // Check if HVAC was on (avg power > 50W)
    const powerReadings = retro.power.filter(pt => {
      const ts = ptTs(pt);
      return ts >= bStart && ts < bEnd && !isNaN(Number(pt.state));
    });
    if (!powerReadings.length) return null;
    const avgPower = powerReadings.reduce((a, b) => a + Number(b.state), 0) / powerReadings.length;
    if (avgPower <= 50) return null;

    // Check if aux/emergency heat was active (>30% of readings = on)
    if (retro.aux && retro.aux.length) {
      const auxReadings = retro.aux.filter(pt => {
        const ts = ptTs(pt);
        return ts >= bStart && ts < bEnd;
      });
      if (auxReadings.length) {
        const onCount = auxReadings.filter(pt => pt.state === "on").length;
        if (onCount / auxReadings.length > 0.3) return "aux";
      }
    }

    // Use actual hvac_action from climate entity if available
    // Check all action transitions within the bucket for any active period
    if (actionTimeline.length) {
      let hadCooling = false, hadHeating = false;
      // Check the action at bucket start (carried from before)
      const startAction = getAction(bStart);
      if (startAction === "cooling") hadCooling = true;
      if (startAction === "heating") hadHeating = true;
      // Check any transitions within the bucket
      for (const entry of actionTimeline) {
        if (entry.ts >= bEnd) break;
        if (entry.ts >= bStart) {
          if (entry.action === "cooling") hadCooling = true;
          if (entry.action === "heating") hadHeating = true;
        }
      }
      if (hadCooling) return "cool";
      if (hadHeating) return "heat";
      return null;
    }

    // Fallback: compare avg indoor to avg outdoor (legacy heuristic)
    const inReadings = retro.indoor.filter(pt => {
      const ts = ptTs(pt);
      return ts >= bStart && ts < bEnd && !isNaN(Number(pt.state));
    });
    const outReadings = retro.outdoor.filter(pt => {
      const ts = ptTs(pt);
      return ts >= bStart && ts < bEnd && !isNaN(Number(pt.state));
    });
    if (inReadings.length && outReadings.length) {
      const avgIn = inReadings.reduce((a, b) => a + Number(b.state), 0) / inReadings.length;
      const avgOut = outReadings.reduce((a, b) => a + Number(b.state), 0) / outReadings.length;
      return avgOut > avgIn ? "cool" : "heat";
    }
    return "heat"; // fallback
  });
}

/** [C-learning] 48h retrospective chart — actual temps + HVAC overlay + model prediction. */
function renderRetrospectiveChart(states, hass, retro) {
  const unit = tempUnit(hass);
  const indoor = findEntity(states, "apparent_temperature");
  const outdoor = findEntity(states, "outdoor_temp_source");
  const NUM_BUCKETS = 48;
  const BUCKET_MS = 1 * 60 * 60 * 1000; // 1-hour buckets
  const startMs = Date.now() - NUM_BUCKETS * BUCKET_MS;

  if (!retro) {
    const currentIndoor = hasValue(indoor) ? Number(indoor.state) : null;
    const currentOutdoor = hasValue(outdoor) ? Number(outdoor.state) : null;
    const loadMsg = (currentIndoor || currentOutdoor)
      ? `Currently ${currentIndoor != null ? currentIndoor.toFixed(1) + unit + " indoor" : ""} ${currentOutdoor != null ? "/ " + currentOutdoor.toFixed(1) + unit + " outdoor" : ""} \u2014 loading history\u2026`
      : "Loading observation history\u2026";
    return `
      <div class="card forecast-card">
        <h2>Recent Activity</h2>
        <div class="forecast-placeholder">${loadMsg}</div>
      </div>`;
  }

  const indoorVals = bucketHistory(retro.indoor, startMs, NUM_BUCKETS, BUCKET_MS);
  const outdoorVals = bucketHistory(retro.outdoor, startMs, NUM_BUCKETS, BUCKET_MS);
  const predictedVals = bucketHistory(retro.predicted, startMs, NUM_BUCKETS, BUCKET_MS);
  const hvacStates = retro.power?.length ? computeHvacBuckets(retro, startMs, NUM_BUCKETS, BUCKET_MS) : indoorVals.map(() => null);

  const allTemps = [...indoorVals, ...outdoorVals, ...predictedVals].filter(v => v != null);
  if (hasValue(indoor)) allTemps.push(Number(indoor.state));
  if (hasValue(outdoor)) allTemps.push(Number(outdoor.state));

  if (allTemps.length < 2) {
    return `
      <div class="card forecast-card">
        <h2>Recent Activity</h2>
        <div class="forecast-placeholder">Still collecting data\u2026 check back in a few minutes.</div>
      </div>`;
  }

  const minT = Math.floor(Math.min(...allTemps) - 1);
  const maxT = Math.ceil(Math.max(...allTemps) + 1);
  const range = maxT - minT || 1;
  const step = range / 4;
  const gridlines = [];
  for (let i = 0; i <= 4; i++) {
    const temp = minT + step * i;
    gridlines.push({ temp: Math.round(temp), pct: ((temp - minT) / range) * 100 });
  }

  const hasPredicted = predictedVals.some(v => v != null);
  const hasHeat = hvacStates.some(v => v === "heat");
  const hasCool = hvacStates.some(v => v === "cool");
  const hasAux = hvacStates.some(v => v === "aux");

  const fmtHour = h => h === 0 ? "12a" : h === 12 ? "12p" : h > 12 ? (h - 12) + "p" : h + "a";

  const cols = indoorVals.map((indoorVal, i) => {
    const outdoorVal = outdoorVals[i];
    const predictedVal = predictedVals[i];
    const hvacState = hvacStates[i];
    const bucketTs = startMs + i * BUCKET_MS;
    const hour = new Date(bucketTs).getHours();
    const timeLabel = (hour % 6 === 0)
      ? `<span class="chart-time">${fmtHour(hour)}</span>` : "";
    const indoorDot = indoorVal != null
      ? `<div class="chart-dot chart-dot-indoor" style="bottom:${((indoorVal - minT) / range) * 100}%"></div>` : "";
    const outdoorDot = outdoorVal != null
      ? `<div class="chart-dot chart-dot-outdoor" style="bottom:${((outdoorVal - minT) / range) * 100}%"></div>` : "";
    const predictedDot = predictedVal != null
      ? `<div class="chart-dot chart-dot-predicted" style="bottom:${((predictedVal - minT) / range) * 100}%"></div>` : "";
    let hvacStrip = "";
    if (hvacState === "aux") hvacStrip = `<div class="chart-hvac chart-hvac-aux"></div>`;
    else if (hvacState === "heat") hvacStrip = `<div class="chart-hvac chart-hvac-heat"></div>`;
    else if (hvacState === "cool") hvacStrip = `<div class="chart-hvac chart-hvac-cool"></div>`;
    return `<div class="chart-col"><div class="chart-area">${indoorDot}${outdoorDot}${predictedDot}${hvacStrip}</div>${timeLabel}</div>`;
  });

  return `
    <div class="card forecast-card">
      <h2>Recent Activity</h2>
      <div class="chart-legend">
        <span class="legend-item"><span class="legend-dot legend-indoor"></span>Indoor</span>
        <span class="legend-item"><span class="legend-dot legend-outdoor"></span>Outdoor</span>
        ${hasPredicted ? `<span class="legend-item"><span class="legend-dot legend-predicted"></span>Model est.</span>` : ""}
        ${hasHeat ? `<span class="legend-item"><span class="legend-hvac legend-hvac-heat"></span>Heating</span>` : ""}
        ${hasCool ? `<span class="legend-item"><span class="legend-hvac legend-hvac-cool"></span>Cooling</span>` : ""}
        ${hasAux ? `<span class="legend-item"><span class="legend-hvac legend-hvac-aux"></span>Aux heat</span>` : ""}
      </div>
      <div class="chart-container">
        <div class="chart-yaxis">
          ${gridlines.map(g => `<span class="chart-ylabel" style="bottom:${g.pct}%">${g.temp}\u00b0</span>`).join("")}
        </div>
        <div class="chart-grid">
          ${gridlines.map(g => `<div class="chart-gridline" style="bottom:${g.pct}%"></div>`).join("")}
          ${cols.join("")}
          <div class="chart-now-bar"><span class="chart-now-label">Now</span></div>
        </div>
      </div>
      <div class="forecast-note">Forecast &amp; schedule will appear once learning completes</div>
    </div>`;
}

/** [C+D] Unified Timeline — 24h history left of Now, 24h forecast/schedule right of Now. */
function renderUnifiedTimeline(states, _hass, retro) {
  const schedule = findEntity(states, "schedule");
  const apparent = findEntity(states, "apparent_temperature");
  const nextAction = findEntity(states, "next_action");
  const precond = findEntity(states, "preconditioning_status");

  // History: 24 buckets × 1 hour each, clock-aligned, ending at current hour
  const HIST_BUCKETS = 24;
  const nowMs = Date.now();
  const BUCKET_MS = 60 * 60 * 1000; // 1 hour per bucket
  const currentHourMs = nowMs - (nowMs % BUCKET_MS); // floor to hour boundary
  const histStartMs = currentHourMs - (HIST_BUCKETS - 1) * BUCKET_MS;

  let indoorVals = Array(HIST_BUCKETS).fill(null);
  let outdoorVals = Array(HIST_BUCKETS).fill(null);
  let predictedVals = Array(HIST_BUCKETS).fill(null);
  let hvacStates = Array(HIST_BUCKETS).fill(null);

  if (retro) {
    indoorVals = bucketHistory(retro.indoor, histStartMs, HIST_BUCKETS, BUCKET_MS);
    outdoorVals = bucketHistory(retro.outdoor, histStartMs, HIST_BUCKETS, BUCKET_MS);
    predictedVals = bucketHistory(retro.predicted, histStartMs, HIST_BUCKETS, BUCKET_MS);
    if (retro.power?.length) hvacStates = computeHvacBuckets(retro, histStartMs, HIST_BUCKETS, BUCKET_MS);
  }

  // 24h forecast: start at the next clock hour (history covers current hour)
  const allForecast = schedule?.attributes?.forecast;
  const nextHourMs = currentHourMs + BUCKET_MS;
  const forecastCols = (Array.isArray(allForecast) ? allForecast : [])
    .filter(pt => new Date(pt.time).getTime() >= nextHourMs)
    .slice(0, 24);

  // Schedule entries for phase strip coloring on forecast side
  const entries = schedule?.attributes?.entries || [];

  // Compute unified temperature range (exclude comfort band — it's decorative, not data)
  const allTemps = [
    ...indoorVals, ...outdoorVals, ...predictedVals,
    ...forecastCols.map(p => p.indoor),
    ...forecastCols.map(p => p.outdoor),
  ].filter(v => v != null);
  if (hasValue(apparent)) allTemps.push(Number(apparent.state));
  const outdoorE = findEntity(states, "outdoor_temp_source");
  if (hasValue(outdoorE)) allTemps.push(Number(outdoorE.state));

  if (allTemps.length < 2) {
    const loadMsg = retro ? "Insufficient data\u2026" : "Loading history\u2026";
    return `
      <div class="card unified-card">
        <h2>Activity &amp; Schedule</h2>
        <div class="forecast-placeholder">${loadMsg}</div>
      </div>`;
  }

  const minT = Math.floor(Math.min(...allTemps) - 2);
  const maxT = Math.ceil(Math.max(...allTemps) + 2);
  const range = maxT - minT || 1;
  const step = range / 4;
  const gridlines = [];
  for (let i = 0; i <= 4; i++) {
    const temp = minT + step * i;
    gridlines.push({ temp: Math.round(temp), pct: ((temp - minT) / range) * 100 });
  }

  const fmtHour = h => h === 0 ? "12a" : h === 12 ? "12p" : h > 12 ? (h - 12) + "p" : h + "a";

  // ── History columns (left of Now) ──
  const histColsHtml = indoorVals.map((indoorVal, i) => {
    const outdoorVal = outdoorVals[i];
    const predictedVal = predictedVals[i];
    const hvacState = hvacStates[i];
    const bucketTs = histStartMs + i * BUCKET_MS;
    const hour = new Date(bucketTs).getHours();
    const isLast = i === HIST_BUCKETS - 1;
    const timeLabel = isLast
      ? `<span class="chart-time chart-time-now">Now</span>`
      : (hour % 6 === 0 ? `<span class="chart-time">${fmtHour(hour)}</span>` : "");
    const dots = [
      indoorVal != null ? `<div class="chart-dot chart-dot-indoor" style="bottom:${((indoorVal - minT) / range) * 100}%"></div>` : "",
      outdoorVal != null ? `<div class="chart-dot chart-dot-outdoor" style="bottom:${((outdoorVal - minT) / range) * 100}%"></div>` : "",
      predictedVal != null ? `<div class="chart-dot chart-dot-predicted" style="bottom:${((predictedVal - minT) / range) * 100}%"></div>` : "",
    ].join("");
    let hvacStrip = "";
    if (hvacState === "aux") hvacStrip = `<div class="chart-hvac chart-hvac-aux"></div>`;
    else if (hvacState === "heat") hvacStrip = `<div class="chart-hvac chart-hvac-heat"></div>`;
    else if (hvacState === "cool") hvacStrip = `<div class="chart-hvac chart-hvac-cool"></div>`;
    return `<div class="chart-col${isLast ? " chart-col-now" : ""}"><div class="chart-area">${dots}${hvacStrip}</div>${timeLabel}</div>`;
  }).join("");

  // ── Forecast columns (right of Now) ──
  const fcColsHtml = forecastCols.map((pt) => {
    const time = new Date(pt.time);
    const hour = time.getHours();
    const timeLabel = (hour % 6 === 0)
      ? `<span class="chart-time">${fmtHour(hour)}</span>` : "";

    let comfortBand = "";
    if (pt.comfort_min != null && pt.comfort_max != null) {
      const minPct = Math.max(0, ((pt.comfort_min - minT) / range) * 100);
      const maxPct = Math.min(100, ((pt.comfort_max - minT) / range) * 100);
      comfortBand = `<div class="chart-comfort" style="bottom:${minPct}%;height:${maxPct - minPct}%"></div>`;
    }

    const dots = [
      `<div class="chart-dot chart-dot-fc-indoor" style="bottom:${((pt.indoor - minT) / range) * 100}%"></div>`,
      `<div class="chart-dot chart-dot-outdoor" style="bottom:${((pt.outdoor - minT) / range) * 100}%"></div>`,
    ].join("");

    // Phase strip: match column to schedule entry for phase color
    const ts = time.getTime();
    const matchEntry = entries.find(e => new Date(e.start).getTime() <= ts && ts < new Date(e.end).getTime());
    const phaseKey = reasonToPhaseKey(matchEntry?.reason || "");
    const phaseInfo = PHASE_MAP[phaseKey] || PHASE_MAP["idle"];
    const phaseStrip = phaseKey !== "idle"
      ? `<div class="chart-phase ${phaseInfo.cls}"></div>` : "";

    return `<div class="chart-col"><div class="chart-area">${comfortBand}${dots}${phaseStrip}</div>${timeLabel}</div>`;
  }).join("");

  // ── Pad forecast side to 24 columns so "Now" stays centered at 50% ──
  const FC_SLOTS = HIST_BUCKETS;
  const fcPadCount = Math.max(0, FC_SLOTS - forecastCols.length);
  // Padding columns need time labels so the forecast axis isn't blank
  const lastFcTime = forecastCols.length
    ? new Date(forecastCols[forecastCols.length - 1].time).getTime()
    : nowMs;
  const fcPadHtml = Array.from({ length: fcPadCount }, (_, i) => {
    const padTs = lastFcTime + (i + 1) * BUCKET_MS;
    const hour = new Date(padTs).getHours();
    const timeLabel = (hour % 6 === 0) ? `<span class="chart-time">${fmtHour(hour)}</span>` : "";
    return `<div class="chart-col"><div class="chart-area"></div>${timeLabel}</div>`;
  }).join("");

  // If no forecast columns at all, insert a dedicated "Now" marker column
  const nowMarker = forecastCols.length === 0
    ? `<div class="chart-col chart-col-now"><div class="chart-area"></div><span class="chart-time chart-time-now">Now</span></div>`
    : "";

  // ── Legend ──
  const hasPredicted = predictedVals.some(v => v != null);
  const hasHeat = hvacStates.some(v => v === "heat");
  const hasCool = hvacStates.some(v => v === "cool");
  const hasAux = hvacStates.some(v => v === "aux");
  const hasComfort = forecastCols.some(p => p.comfort_min != null);
  const usedPhaseKeys = [...new Set(forecastCols.map(pt => {
    const ts = new Date(pt.time).getTime();
    const e = entries.find(e => new Date(e.start).getTime() <= ts && ts < new Date(e.end).getTime());
    return reasonToPhaseKey(e?.reason || "");
  }).filter(k => k !== "idle"))];

  const legendParts = [
    `<span class="legend-item"><span class="legend-dot legend-indoor"></span>Actual</span>`,
    hasPredicted ? `<span class="legend-item"><span class="legend-dot legend-predicted"></span>Model</span>` : "",
    forecastCols.length ? `<span class="legend-item"><span class="legend-dot legend-fc-indoor"></span>Forecast</span>` : "",
    `<span class="legend-item"><span class="legend-dot legend-outdoor"></span>Outdoor</span>`,
    hasComfort ? `<span class="legend-item"><span class="legend-band"></span>Comfort</span>` : "",
    hasHeat ? `<span class="legend-item"><span class="legend-hvac legend-hvac-heat"></span>Heating</span>` : "",
    hasCool ? `<span class="legend-item"><span class="legend-hvac legend-hvac-cool"></span>Cooling</span>` : "",
    hasAux ? `<span class="legend-item"><span class="legend-hvac legend-hvac-aux"></span>Aux heat</span>` : "",
    ...usedPhaseKeys.filter(k => PHASE_MAP[k]).map(k => {
      const p = PHASE_MAP[k];
      return `<span class="legend-item"><span class="legend-phase ${p.cls}"></span>${p.label}</span>`;
    }),
  ].filter(Boolean).join("");

  // ── Occupancy underlay — thin bar showing home/away from calendar ──
  const occupancy = findEntity(states, "occupancy_forecast");
  let occBar = "";
  if (occupancy?.attributes?.source === "calendar" && Array.isArray(occupancy.attributes.timeline)) {
    const totalMs = (HIST_BUCKETS + FC_SLOTS) * BUCKET_MS;
    const timelineStart = histStartMs;
    const occSegs = occupancy.attributes.timeline.map(seg => {
      const start = Math.max(new Date(seg.start).getTime(), timelineStart);
      const end = Math.min(new Date(seg.end).getTime(), timelineStart + totalMs);
      if (end <= start) return "";
      const leftPct = ((start - timelineStart) / totalMs) * 100;
      const widthPct = ((end - start) / totalMs) * 100;
      const cls = seg.mode === "home" ? "tl-occ-home" : "tl-occ-away";
      return `<div class="tl-occ-seg ${cls}" style="left:${leftPct}%;width:${Math.max(widthPct, 0.5)}%"></div>`;
    }).join("");
    occBar = `<div class="tl-occ-bar">${occSegs}</div>`;
  }

  // ── Forecast footer notes ──
  let footerNote = "";
  if (isAvailable(nextAction)) {
    footerNote = `<div class="timeline-next">${nextAction.state}</div>`;
  }
  if (isAvailable(precond) && precond.attributes?.arrival_time) {
    const a = precond.attributes;
    const parts = [`Arrival: ${a.arrival_time}`];
    if (a.energy_estimate != null) parts.push(`${Number(a.energy_estimate).toFixed(1)} kWh`);
    if (a.cost_estimate != null) parts.push(`$${Number(a.cost_estimate).toFixed(2)}`);
    footerNote += `<div class="timeline-precond">${parts.join(" \u00b7 ")}</div>`;
  }

  return `
    <div class="card unified-card">
      <h2>Activity &amp; Schedule</h2>
      <div class="chart-legend">${legendParts}</div>
      <div class="chart-container">
        <div class="chart-yaxis">
          ${gridlines.map(g => `<span class="chart-ylabel" style="bottom:${g.pct}%">${g.temp}\u00b0</span>`).join("")}
        </div>
        <div class="chart-grid">
          ${gridlines.map(g => `<div class="chart-gridline" style="bottom:${g.pct}%"></div>`).join("")}
          ${histColsHtml}
          ${nowMarker}${fcColsHtml}${fcPadHtml}
        </div>
      </div>
      ${occBar}
      ${footerNote}
      <div style="font-size:10px;color:var(--text-secondary);margin-top:4px;font-family:monospace">
        in=${retro?.indoor?.length??0} last=${retro?.indoor?.length ? retro.indoor[retro.indoor.length-1].last_changed : "?"} bucketed=${indoorVals.filter(v=>v!=null).length}/24
      </div>
    </div>`;
}

/** [G] Learning Progress Cards — three-card grid during learning mode. */
function renderLearningProgressCards(states) {
  const modelConf = findEntity(states, "model_confidence");
  const rValue = findEntity(states, "envelope_r_value");
  const thermalMass = findEntity(states, "thermal_mass");
  const coolCap = findEntity(states, "cooling_capacity");
  const baselineConf = findEntity(states, "baseline_confidence");
  const profilerStatus = findEntity(states, "profiler_status") || findEntity(states, "profiler");

  const zone = getClimateZone(states);
  const R_GOOD = zone === "cold" ? 15 : zone === "mixed" ? 12 : 9;
  const R_POOR = zone === "cold" ? 7  : zone === "mixed" ? 5  : 4;

  // ── Thermal Model card ──
  const confPct = hasValue(modelConf) ? Math.min(100, Number(modelConf.state)) : 0;
  let rLine = "", massLine = "", capLine = "";
  if (hasValue(rValue)) {
    const rv = Number(rValue.state);
    let lbl, cls;
    if (rv >= R_GOOD) { lbl = "Well insulated"; cls = "quality-good"; }
    else if (rv < R_POOR) { lbl = "Needs attention"; cls = "quality-poor"; }
    else { lbl = "Typical for region"; cls = "quality-medium"; }
    rLine = `<div class="lp-param"><span class="lp-param-label">Insulation</span><span class="lp-param-val">${rv.toFixed(1)}</span><span class="lp-param-qual ${cls}">${lbl}</span></div>`;
  }
  if (hasValue(thermalMass)) {
    const tm = Number(thermalMass.state);
    let lbl, cls;
    if (tm >= 4000) { lbl = "High — holds temp well"; cls = "quality-good"; }
    else if (tm < 1500) { lbl = "Light — responds fast"; cls = "quality-neutral"; }
    else { lbl = "Moderate"; cls = "quality-medium"; }
    massLine = `<div class="lp-param"><span class="lp-param-label">Thermal Mass</span><span class="lp-param-val">${(tm / 1000).toFixed(1)}k BTU/\u00b0F</span><span class="lp-param-qual ${cls}">${lbl}</span></div>`;
  }
  if (hasValue(coolCap)) {
    capLine = `<div class="lp-param"><span class="lp-param-label">Capacity</span><span class="lp-param-val">${(Number(coolCap.state) / 1000).toFixed(1)}k BTU/hr</span></div>`;
  }
  const modelCard = `
    <div class="card lp-card">
      <div class="lp-card-title">Thermal Model</div>
      <div class="lp-bar-track"><div class="lp-bar-fill lp-bar-model" style="width:${confPct}%"></div></div>
      <div class="lp-conf-pct">${confPct.toFixed(0)}% confident</div>
      <div class="lp-params">${rLine}${massLine}${capLine}</div>
      ${confPct < 20 ? `<div class="lp-ready-note">Refining estimates\u2026</div>` : ""}
    </div>`;

  // ── Baseline Schedule card ──
  const baselinePct = hasValue(baselineConf) ? Math.min(100, Number(baselineConf.state)) : 0;
  const sampleDays = baselineConf?.attributes?.sample_days ?? 0;
  const daysRemaining = baselineConf?.attributes?.days_remaining;
  // Day-of-week dots: approximate filled days backwards from today
  const today = new Date().getDay();
  const DAY_ABBR = ["S", "M", "T", "W", "T", "F", "S"];
  const dayDots = DAY_ABBR.map((lbl, i) => {
    const daysAgo = ((today - i) % 7 + 7) % 7;
    const filled = daysAgo < sampleDays;
    return `<span class="day-dot${filled ? " day-dot-filled" : ""}">${lbl}</span>`;
  }).join("");
  const baselineCard = `
    <div class="card lp-card">
      <div class="lp-card-title">Baseline Schedule</div>
      <div class="lp-bar-track"><div class="lp-bar-fill lp-bar-baseline" style="width:${baselinePct}%"></div></div>
      <div class="lp-conf-pct">${sampleDays} of 7 days captured</div>
      <div class="day-dots">${dayDots}</div>
      ${daysRemaining != null && daysRemaining > 0
        ? `<div class="lp-ready-note">~${daysRemaining} day${daysRemaining !== 1 ? "s" : ""} remaining</div>`
        : baselinePct >= 100 ? `<div class="lp-ready-note lp-ready-ok">\u2713 Baseline ready</div>` : ""}
    </div>`;

  // ── Performance Profiler card ──
  const modeDetail = profilerStatus?.attributes?.mode_detail || {};
  const MODE_LABELS = {"resist": "Passive Drift", "heat_1": "Heating", "cool_1": "Cooling", "auxiliary_heat_1": "Aux Heat"};
  const ALL_MODES = ["resist", "heat_1", "cool_1", "auxiliary_heat_1"];
  const MIN_OBS_DISPLAY = 30;
  const significantModes = ALL_MODES.filter(m => modeDetail[m] && modeDetail[m].observations >= MIN_OBS_DISPLAY);
  const significantConfs = significantModes.map(m => modeDetail[m].confidence || 0);
  const bestPct = significantConfs.length ? Math.max(...significantConfs) : 0;
  const modeRows = ALL_MODES.map(mode => {
    const d = modeDetail[mode];
    const label = MODE_LABELS[mode];
    if (!d || d.observations < MIN_OBS_DISPLAY) return "";
    const pct = d.confidence || 0;
    const obs = d.observations || 0;
    return `<div class="lp-param"><span class="lp-param-label">${label}</span><span class="lp-param-val">${pct}% \u00b7 ${obs} obs</span></div>`;
  }).join("");
  const profilerCard = `
    <div class="card lp-card">
      <div class="lp-card-title">Performance Data</div>
      <div class="lp-bar-track"><div class="lp-bar-fill lp-bar-profiler" style="width:${bestPct}%"></div></div>
      <div class="lp-conf-pct">${bestPct.toFixed(0)}% confident</div>
      <div class="lp-params">${modeRows}</div>
      <div class="lp-obs-note">+1 observation every 5 min</div>
    </div>`;

  return `<div class="lp-grid">${modelCard}${baselineCard}${profilerCard}</div>`;
}

/** [H] Learning Milestones — onboarding progress checklist. */
function renderLearningMilestones(states) {
  const baselineConf = findEntity(states, "baseline_confidence");
  const modelConf = findEntity(states, "model_confidence");
  const tier = findEntity(states, "savings_accuracy_tier");

  const sampleDays = baselineConf?.attributes?.sample_days ?? 0;
  const confPct = hasValue(modelConf) ? Number(modelConf.state) : 0;
  const tierVal = tier?.state || "learning";

  const milestones = [
    { done: true, label: "Sensors verified & connected", sub: null },
    { done: confPct > 0, label: "First observation cycle complete", sub: null },
    {
      done: sampleDays >= 7 || (hasValue(baselineConf) && Number(baselineConf.state) >= 100),
      label: "Baseline schedule captured",
      sub: sampleDays > 0 ? `Day ${sampleDays} of 7` : null,
      current: sampleDays > 0 && sampleDays < 7,
    },
    {
      done: confPct >= 50,
      label: "Thermal model confidence reached",
      sub: confPct > 0 ? `${confPct.toFixed(0)}% \u2192 50% needed` : null,
      current: confPct > 0 && confPct < 50,
    },
    { done: tierVal !== "learning", label: "Savings comparison & optimization active", sub: tierVal !== "learning" ? tierVal : null },
  ];

  const items = milestones.map(m => {
    const icon = m.done ? "\u2713" : (m.current ? "\u25cf" : "\u25cb");
    const cls = m.done ? "ms-done" : (m.current ? "ms-current" : "ms-pending");
    return `
      <div class="ms-item ${cls}">
        <span class="ms-icon">${icon}</span>
        <div class="ms-text">
          <span class="ms-label">${m.label}</span>
          ${m.sub ? `<span class="ms-sub">${m.sub}</span>` : ""}
        </div>
      </div>`;
  }).join("");

  // Contextual tips based on current configuration
  const tips = [];
  const indoorSource = findEntity(states, "indoor_temp_source");
  const occupancy = findEntity(states, "occupancy_forecast");
  const schedule = findEntity(states, "schedule");

  const indoorCount = indoorSource?.attributes?.entity_count || 0;
  if (indoorCount === 0) {
    tips.push("Adding indoor temperature sensors improves accuracy. Configure them in Settings.");
  }
  if (!isAvailable(occupancy) || occupancy.state === "unknown") {
    tips.push("Adding presence detection enables away-mode savings when nobody is home.");
  }

  const comfortMin = schedule?.attributes?.comfort_min;
  const comfortMax = schedule?.attributes?.comfort_max;
  if (comfortMin != null && comfortMax != null && (comfortMax - comfortMin) < 3) {
    tips.push("A wider comfort range gives the optimizer more room to save energy.");
  }

  if (tips.length === 0) {
    tips.push("Everything looks good. The optimizer is learning in the background.");
  }

  const tipHtml = tips.map(t => `<div class="tip-item">${t}</div>`).join("");

  return `
    <div class="card milestone-card">
      <h2>Getting Started</h2>
      <div class="ms-list">${items}</div>
      <div class="tips-section">${tipHtml}</div>
    </div>`;
}

/** [I] Today's Snapshot — objective facts during learning (replaces savings card). */
function renderSnapshotCard(states, hass) {
  const unit = tempUnit(hass);
  const baselineKwh = findEntity(states, "baseline_kwh_today");
  const power = findEntity(states, "net_hvac_power");
  const auxKwh = findEntity(states, "aux_heat_kwh_today");
  const schedule = findEntity(states, "schedule");

  // Outdoor temp range from today's weather forecast
  let outdoorRange = "";
  const weather = schedule?.attributes?.weather_forecast;
  if (weather && Array.isArray(weather)) {
    const temps = weather.map(p => p.outdoor).filter(v => v != null);
    if (temps.length) {
      const lo = Math.min(...temps), hi = Math.max(...temps);
      outdoorRange = `${Math.round(lo)}\u2013${Math.round(hi)}${unit}`;
    }
  }

  const items = [];
  if (hasValue(baselineKwh) && Number(baselineKwh.state) > 0)
    items.push({ label: "Est. energy today", value: `${fmt(baselineKwh, 1)} kWh`, sub: "based on your usage pattern" });
  if (hasValue(power) && Number(power.state) > 0)
    items.push({ label: "Current draw", value: `${(Number(power.state) / 1000).toFixed(1)} kW`, sub: "HVAC power" });
  if (outdoorRange)
    items.push({ label: "Outdoor today", value: outdoorRange, sub: "from forecast" });
  if (hasValue(auxKwh) && Number(auxKwh.state) > 0)
    items.push({ label: "Aux heat used", value: `${fmt(auxKwh, 2)} kWh`, sub: "resistive element" });

  if (items.length === 0) return "";

  const grid = items.map(item => `
    <div class="snap-item">
      <span class="snap-label">${item.label}</span>
      <span class="snap-value">${item.value}</span>
      ${item.sub ? `<span class="snap-sub">${item.sub}</span>` : ""}
    </div>`).join("");

  return `
    <div class="card snapshot-card">
      <div class="snapshot-header">
        <h2>Today\u2019s Snapshot</h2>
        <span class="snapshot-note">Savings comparison unlocks after baseline</span>
      </div>
      <div class="snap-grid">${grid}</div>
    </div>`;
}

/** [W] Welcome Card -- shown only on fresh installations before any data arrives. */
function renderWelcomeCard(states) {
  const modelConf = findEntity(states, "model_confidence");
  const baselineConf = findEntity(states, "baseline_confidence");
  const sampleDays = baselineConf?.attributes?.sample_days ?? 0;
  const confPct = hasValue(modelConf) ? Number(modelConf.state) : 0;

  // Only show when truly fresh: no observations yet
  if (confPct > 0 || sampleDays > 0) return "";

  return `
    <div class="card welcome-card">
      <h2>Welcome</h2>
      <p class="welcome-text">
        The optimizer is observing your thermostat and weather data to learn how
        your home behaves. It will not change your thermostat settings during this
        initial observation period.
      </p>
      <p class="welcome-text">
        After about a week of baseline data, it will begin shifting HVAC runtime
        to save energy while keeping you comfortable. No action needed.
      </p>
    </div>`;
}

// ── Main Component ───────────────────────────────────────────────────

class HeatPumpOptimizerPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._retro = null;          // cached 48h history for retrospective chart
    this._retroFetchedAt = 0;    // timestamp of last successful fetch
    this._retroFetching = false;
  }

  set hass(hass) {
    this._hass = hass;
    this._maybeRefreshHistory();
    this._render();
  }

  async _maybeRefreshHistory() {
    if (this._retroFetching) return;
    const now = Date.now();
    if (now - this._retroFetchedAt < 5 * 60 * 1000) return; // throttle: once per 5 min
    this._retroFetching = true;
    try {
      const s = this._hass.states;
      const indoorE = findEntity(s, "apparent_temperature");
      const outdoorE = findEntity(s, "outdoor_temp_source");
      const predictedE = findEntity(s, "predicted_indoor_temp");
      const powerE = findEntity(s, "net_hvac_power");
      const auxE = findBinary(s, "aux_heat_active");
      // Find the climate entity for hvac_action history
      const climateE = Object.values(s).find(e => e.entity_id.startsWith("climate.") && e.attributes?.hvac_action != null);
      const ids = [indoorE, outdoorE, predictedE, powerE, auxE].filter(Boolean).map(e => e.entity_id).join(",");
      if (!ids) return;
      const start = new Date(now - 24 * 60 * 60 * 1000).toISOString();
      const fetches = [
        this._hass.callApi("GET", `history/period/${start}?filter_entity_id=${ids}&minimal_response&no_attributes`),
      ];
      // Fetch climate entity separately (needs attributes for hvac_action)
      if (climateE) {
        fetches.push(this._hass.callApi("GET", `history/period/${start}?filter_entity_id=${climateE.entity_id}&significant_changes_only`));
      }
      const [data, climateData] = await Promise.all(fetches);
      if (!Array.isArray(data)) return;
      const byId = {};
      for (const series of data) {
        if (series.length > 0) byId[series[0].entity_id] = series;
      }
      // Extract hvac_action timeline from climate entity history
      let hvacActions = [];
      if (Array.isArray(climateData)) {
        for (const series of climateData) {
          if (series.length > 0 && series[0].entity_id === climateE?.entity_id) {
            hvacActions = series.map(pt => ({
              last_changed: pt.last_changed,
              state: pt.attributes?.hvac_action || "idle",
            }));
          }
        }
      }
      this._retro = {
        indoor:    byId[indoorE?.entity_id]    || [],
        outdoor:   byId[outdoorE?.entity_id]   || [],
        predicted: byId[predictedE?.entity_id] || [],
        power:     byId[powerE?.entity_id]     || [],
        aux:       byId[auxE?.entity_id]       || [],
        hvacAction: hvacActions,
      };
      this._retroFetchedAt = now;
      this._render();
    } catch (e) {
      console.warn("HPO: history fetch failed", e);
    } finally {
      this._retroFetching = false;
    }
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

    const tierVal = (findEntity(s, "savings_accuracy_tier")?.state || "learning");
    const isLearning = tierVal === "learning";

    this.shadowRoot.innerHTML = `
      <style>${PANEL_CSS}</style>
      <div class="panel">
        <header class="header">
          <h1>Heat Pump Optimizer</h1>
        </header>
        ${renderAlerts(s)}
        ${isLearning ? renderWelcomeCard(s) : ""}
        ${renderHeroStrip(s, this._hass)}
        ${renderEnvironmentCard(s, this._hass)}
        ${renderThermalLoadCard(s, this._hass)}
        ${(() => {
          if (!isLearning) return renderUnifiedTimeline(s, this._hass, this._retro);
          const fc = findEntity(s, "schedule")?.attributes?.forecast;
          const hasRetro = this._retro && (this._retro.indoor.length > 0 || this._retro.outdoor.length > 0);
          return (fc && Array.isArray(fc) && fc.length >= 2 && hasRetro)
            ? renderUnifiedTimeline(s, this._hass, this._retro)
            : renderRetrospectiveChart(s, this._hass, this._retro);
        })()}
        ${isLearning ? renderLearningProgressCards(s) : ""}
        ${isLearning ? renderLearningMilestones(s) : renderDecisionCard(s, this._hass)}
        ${renderBuildingCard(s, this._hass)}
        ${isLearning ? renderSnapshotCard(s, this._hass) : renderSavingsCard(s, this._hass)}
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
  .hero-status-row {
    display: flex;
    align-items: center;
  }
  .hero-appliance-row {
    margin: 6px 0 0;
  }
  .hero-temps {
    display: flex;
    margin-top: 14px;
    padding-top: 14px;
    border-top: 1px solid color-mix(in srgb, var(--border) 60%, transparent);
  }
  .hero-temp-item {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
  }
  .hero-temp-mid { text-align: center; align-items: center; }
  .hero-temp-end { text-align: right; align-items: flex-end; }
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
  .forecast-card { overflow: hidden; }
  .forecast-placeholder {
    color: var(--text-secondary);
    font-size: 14px;
    text-align: center;
    padding: 24px 0;
    font-style: italic;
  }
  .chart-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 12px;
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
    border-radius: 1px;
    display: inline-block;
  }
  .legend-hvac-heat { background: var(--orange); }
  .legend-hvac-cool { background: var(--blue); }
  .legend-hvac-aux { background: var(--red); }
  .legend-fc-indoor {
    background: transparent;
    border: 1.5px solid var(--accent);
    width: 8px; height: 8px;
    box-sizing: border-box;
    border-radius: 50%;
    display: inline-block;
    opacity: 0.85;
  }
  .legend-phase {
    width: 12px; height: 4px;
    border-radius: 1px;
    display: inline-block;
    opacity: 0.75;
  }
  .legend-phase.phase-active { background: var(--accent); }
  .legend-phase.phase-coast { background: var(--green); }
  .legend-phase.phase-maintain { background: color-mix(in srgb, var(--text-secondary) 40%, transparent); }

  .chart-container {
    display: flex;
    height: 160px;
    margin-bottom: 18px;
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
    border-right: 2px solid var(--red);
  }
  .chart-area {
    position: absolute;
    inset: 0;
    overflow: hidden;
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
    width: 5px; height: 5px;
    background: var(--accent);
  }
  .chart-dot-outdoor {
    width: 3px; height: 3px;
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
  .chart-hvac-aux { background: var(--red); }
  .chart-phase {
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    height: 4px;
    opacity: 0.75;
  }
  .chart-phase.phase-active { background: var(--accent); }
  .chart-phase.phase-coast { background: var(--green); }
  .chart-phase.phase-maintain { background: color-mix(in srgb, var(--text-secondary) 40%, transparent); }
  .chart-dot-fc-indoor {
    width: 6px; height: 6px;
    background: transparent;
    border: 1.5px solid var(--accent);
    border-radius: 50%;
    opacity: 0.85;
  }
  .chart-time {
    position: absolute;
    bottom: -16px;
    left: 0;
    font-size: 9px;
    color: var(--text-secondary);
    transform: translateX(-50%);
  }
  .chart-time-now {
    color: var(--red);
    font-weight: 600;
  }
  /* Right-edge "Now" overlay — used in the retro chart (learning mode) */
  .chart-now-bar {
    position: absolute;
    right: 0;
    top: 0;
    bottom: 0;
    width: 2px;
    background: var(--red);
    z-index: 3;
    pointer-events: none;
  }
  .chart-now-label {
    position: absolute;
    bottom: -16px;
    right: 0;
    font-size: 9px;
    font-weight: 600;
    color: var(--red);
    transform: translateX(50%);
    white-space: nowrap;
  }

  /* ── Unified Timeline Card ── */
  .unified-card { overflow: hidden; }

  /* ── Schedule Timeline (legacy, kept for compatibility) ── */
  .timeline-card { }
  .timeline-empty, .timeline-next {
    font-size: 13px;
    color: var(--text-secondary);
  }
  .timeline-next { margin-top: 20px; }
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
    flex: 1;
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
  .profile-narrative {
    font-size: 13px;
    color: var(--text-secondary);
    line-height: 1.5;
    margin: 10px 0 14px;
    padding: 10px 12px;
    background: color-mix(in srgb, var(--accent) 4%, transparent);
    border-radius: 8px;
    border-left: 3px solid var(--accent-light);
  }
  .profile-rows {
    display: flex;
    flex-direction: column;
    gap: 20px;
  }
  .profile-row { }
  .profile-row-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 8px;
    gap: 8px;
  }
  .profile-label {
    font-size: 13px;
    font-weight: 600;
    flex-shrink: 0;
  }
  .profile-qual {
    font-size: 12px;
    color: var(--text-secondary);
    text-align: right;
  }
  .profile-bar-track {
    height: 4px;
    background: color-mix(in srgb, var(--border) 40%, transparent);
    border-radius: 2px;
    position: relative;
    margin: 0 6px;
  }
  .profile-bar-marker {
    position: absolute;
    top: 50%;
    transform: translate(-50%, -50%);
    width: 14px;
    height: 14px;
    border-radius: 50%;
    border: 2.5px solid var(--card-bg);
    box-shadow: 0 1px 4px rgba(0,0,0,0.3);
  }
  .profile-scale {
    display: flex;
    justify-content: space-between;
    margin-top: 6px;
    padding: 0 2px;
    font-size: 10px;
    color: color-mix(in srgb, var(--text-secondary) 70%, transparent);
    letter-spacing: 0.3px;
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

  /* ── Thermal Load Card ── */
  .thermal-load-card { overflow: hidden; }
  .tl-headline {
    margin-bottom: 12px;
  }
  .tl-headline-value {
    font-size: 20px;
    font-weight: 600;
    display: block;
  }
  .tl-headline-dir {
    font-size: 12px;
    color: var(--text-secondary);
  }
  .tl-bar-label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-secondary);
    margin-bottom: 4px;
    margin-top: 8px;
  }
  .tl-stacked-bar {
    position: relative;
    height: 20px;
    background: color-mix(in srgb, var(--border) 30%, transparent);
    border-radius: 6px;
    overflow: hidden;
  }
  .tl-stacked-bar.tl-converging {
    opacity: 0.5;
  }
  .tl-bar-seg {
    position: absolute;
    top: 0;
    height: 100%;
    opacity: 0.85;
  }
  .tl-load-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 10px;
    margin-top: 12px;
    font-size: 11px;
    color: var(--text-secondary);
  }
  .tl-load-legend-item {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .tl-load-legend-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
    flex-shrink: 0;
  }

  /* ── Forecast additions ── */
  .chart-dot-weather {
    width: 5px;
    height: 5px;
  }
  .chart-dot-predicted {
    width: 5px; height: 5px;
    background: transparent;
    border: 1.5px dashed var(--accent);
    border-radius: 50%;
  }
  .legend-predicted {
    background: transparent;
    border: 1.5px dashed var(--accent);
    width: 8px; height: 8px;
    box-sizing: border-box;
    border-radius: 50%;
  }
  .forecast-note {
    font-size: 11px;
    color: var(--text-secondary);
    text-align: center;
    margin-top: 24px;
    font-style: italic;
  }

  /* ── Learning Progress Cards grid ── */
  .lp-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 10px;
    margin-bottom: 12px;
  }
  .lp-card {
    margin-bottom: 0;
  }
  .lp-card-title {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--text-secondary);
    margin-bottom: 8px;
  }
  .lp-bar-track {
    height: 5px;
    background: color-mix(in srgb, var(--border) 60%, transparent);
    border-radius: 3px;
    overflow: hidden;
    margin-bottom: 4px;
  }
  .lp-bar-fill {
    height: 100%;
    border-radius: 3px;
    transition: width 0.6s ease;
  }
  .lp-bar-model { background: var(--accent); }
  .lp-bar-baseline { background: var(--green); }
  .lp-bar-profiler { background: var(--orange); }
  .lp-conf-pct {
    font-size: 11px;
    color: var(--text-secondary);
    margin-bottom: 8px;
  }
  .lp-params {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .lp-param {
    display: flex;
    flex-direction: column;
  }
  .lp-param-label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    color: var(--text-secondary);
  }
  .lp-param-val {
    font-size: 13px;
    font-weight: 600;
  }
  .lp-param-qual {
    font-size: 10px;
    margin-bottom: 2px;
  }
  .lp-ready-note {
    font-size: 11px;
    color: var(--text-secondary);
    margin-top: 6px;
    font-style: italic;
  }
  .lp-ready-ok { color: var(--green); font-style: normal; font-weight: 500; }
  .lp-obs-note {
    font-size: 10px;
    color: var(--text-secondary);
    margin-top: 4px;
    font-style: italic;
  }
  .lp-dim { opacity: 0.45; font-style: italic; }
  .day-dots {
    display: flex;
    gap: 4px;
    margin: 6px 0;
  }
  .day-dot {
    width: 20px; height: 20px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 9px;
    font-weight: 600;
    background: color-mix(in srgb, var(--border) 40%, transparent);
    color: var(--text-secondary);
  }
  .day-dot-filled {
    background: var(--green-light);
    color: var(--green);
  }

  /* ── Milestone Checklist ── */
  .milestone-card { }
  .ms-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .ms-item {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 6px 0;
    border-bottom: 1px solid color-mix(in srgb, var(--border) 30%, transparent);
  }
  .ms-item:last-child { border-bottom: none; }
  .ms-icon {
    width: 18px;
    text-align: center;
    font-size: 13px;
    flex-shrink: 0;
    margin-top: 1px;
  }
  .ms-done .ms-icon { color: var(--green); }
  .ms-current .ms-icon { color: var(--accent); }
  .ms-pending .ms-icon { color: var(--text-secondary); }
  .ms-text {
    display: flex;
    flex-direction: column;
    gap: 1px;
  }
  .ms-label {
    font-size: 13px;
  }
  .ms-done .ms-label { color: var(--text-secondary); }
  .ms-current .ms-label { font-weight: 500; }
  .ms-pending .ms-label { color: var(--text-secondary); }
  .ms-sub {
    font-size: 11px;
    color: var(--accent);
  }
  .ms-done .ms-sub { color: var(--green); }

  /* ── Tips Section (inside milestone card) ── */
  .tips-section {
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px solid color-mix(in srgb, var(--border) 30%, transparent);
  }
  .tip-item {
    font-size: 12px;
    color: var(--text-secondary);
    padding: 3px 0;
  }
  .tip-item::before {
    content: "Tip: ";
    font-weight: 500;
    color: var(--accent);
  }

  /* ── Welcome Card ── */
  .welcome-card {
    border-left: 3px solid var(--accent, #4CAF50);
  }
  .welcome-text {
    margin: 0.5em 0;
    line-height: 1.5;
    color: var(--text-secondary);
    font-size: 13px;
  }
  .welcome-text:last-child { margin-bottom: 0; }

  /* ── Today's Snapshot Card ── */
  .snapshot-card { }
  .snapshot-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
  }
  .snapshot-header h2 { margin-bottom: 0; }
  .snapshot-note {
    font-size: 10px;
    color: var(--text-secondary);
    font-style: italic;
    text-align: right;
    max-width: 150px;
    line-height: 1.3;
  }
  .snap-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
    gap: 8px;
  }
  .snap-item {
    display: flex;
    flex-direction: column;
    padding: 8px 10px;
    background: color-mix(in srgb, var(--accent) 5%, transparent);
    border-radius: 8px;
  }
  .snap-label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    color: var(--text-secondary);
    margin-bottom: 2px;
  }
  .snap-value {
    font-size: 18px;
    font-weight: 700;
  }
  .snap-sub {
    font-size: 10px;
    color: var(--text-secondary);
    margin-top: 1px;
  }

  /* ── Power user diagnostics additions ── */
  .diag-warn span { color: var(--orange); }
  .src-table {
    margin-top: 8px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .src-row {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--text-secondary);
    padding: 2px 0;
  }
  .src-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .src-ok { background: var(--green); }
  .src-fail { background: var(--red); }
  .src-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .src-age { flex-shrink: 0; color: var(--text-secondary); font-size: 11px; }
  .src-val { flex-shrink: 0; font-weight: 500; font-size: 12px; margin-left: 4px; }
  .src-fallback { color: var(--orange); font-size: 10px; }

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
