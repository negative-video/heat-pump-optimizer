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

/** Escape HTML so entity-provided strings never execute as markup. */
function esc(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Entity IDs come from the entity's display name, not from the internal key
// the cards were written against. These lookups never matched anything, so
// the savings tier always read as "learning" and the savings, accuracy and
// pre-conditioning figures never appeared.
const ENTITY_ALIASES = {
  savings_accuracy_tier: "savings_accuracy",
  model_accuracy: "model_accuracy_mae",
  savings_kwh_today: "energy_saved_today",
  savings_kwh_cumulative: "energy_saved_total",
  savings_cost_today: "cost_saved_today",
  savings_cost_cumulative: "cost_saved_total",
  savings_co2_today: "co2_avoided_today",
  savings_co2_cumulative: "co2_avoided_total",
  baseline_kwh_today: "baseline_energy_today",
  baseline_avg_cop: "baseline_average_cop",
  optimized_avg_cop: "optimized_average_cop",
  cop_improvement_pct: "cop_improvement",
  rate_savings_today: "rate_arbitrage_savings_today",
  preconditioning_status: "pre_conditioning_status",
  profiler_status: "profiler",
  weighted_indoor_temp: "weighted_indoor_temperature",
  measured_savings_30d: "measured_savings_30_days",
  twin_reference_savings_30d: "twin_savings_vs_reference_30_days",
  twin_plan_effect_30d: "twin_optimizer_effect_30_days",
  cost_of_a_degree: "cost_of_a_degree_today",
  ab_trial: "a_b_validation_trial",
};

// Entity IDs keyed by the integration's own entity key, from the frontend's
// entity registry. This is exact: it does not depend on what an entity is
// called, in which language, or whether the user renamed it. The suffix
// match below remains for entities without a key (binary sensors, the
// switch) and for frontends that do not expose the registry.
let ENTITY_IDS_BY_KEY = {};
function indexEntities(hass) {
  const out = {};
  const registry = hass && hass.entities;
  if (registry) {
    for (const [id, entry] of Object.entries(registry)) {
      if (entry && entry.platform === "heatpump_optimizer" && entry.translation_key) {
        out[entry.translation_key] = id;
      }
    }
  }
  ENTITY_IDS_BY_KEY = out;
}

/** Find an optimizer entity by its key, falling back to an ID suffix match. */
function findEntity(states, suffix) {
  const byKey = ENTITY_IDS_BY_KEY[suffix];
  if (byKey && states[byKey]) return states[byKey];
  const wanted = [suffix];
  if (ENTITY_ALIASES[suffix]) wanted.push(ENTITY_ALIASES[suffix]);
  for (const name of wanted) {
    for (const id of Object.keys(states)) {
      if (id.includes(ENTITY_PREFIX) && id.endsWith(`_${name}`)) return states[id];
    }
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
  learning: { label: "Learning", cls: "phase-idle" },
  paused: { label: "Paused", cls: "phase-paused" },
  safe_mode: { label: "Safe Mode", cls: "phase-warn" },
  preconditioning: { label: "Pre-conditioning", cls: "phase-active" },
};

/** Accuracy tier → dot count + label */
const TIER_MAP = {
  learning: { dots: 0, label: "Learning" },
  projected: { dots: 1, label: "Projected" },
  estimated: { dots: 2, label: "Estimated" },
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
    alerts.push({ cls: "alert-error", msg: "Safe mode: using conservative defaults" });
  if (auxHeat?.state === "on") {
    const auxThresh = findEntity(states, "aux_heat_threshold");
    const threshMsg = hasValue(auxThresh) ? ` (threshold: ${fmt(auxThresh, 0)}° eff)` : "";
    alerts.push({ cls: "alert-warning", msg: `Auxiliary/emergency heat is running${threshMsg}` });
  }
  if (stale?.state === "on")
    alerts.push({ cls: "alert-warning", msg: "Temperature sensor may be stale" });
  if (override?.state === "on")
    alerts.push({ cls: "alert-info", msg: "Manual override detected: optimizer paused" });

  const learning = findEntity(states, "learning_progress");
  if (learning?.state?.startsWith("Day ") && learning.state.includes("Observing")) {
    alerts.push({ cls: "alert-info", msg: `${learning.state}: thermostat unchanged` });
  } else if (learning?.state?.startsWith("Baseline captured")) {
    alerts.push({ cls: "alert-info", msg: `${learning.state}` });
  } else if (learning?.state?.startsWith("Optimizer active") && learning.state.includes("conservative")) {
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
    applianceChip = `<span class="appliance-chip ${cls}" title="${Math.abs(btu).toLocaleString()} BTU/hr">${esc(label)}</span>`;
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
    narrative = `Large divergence from model${err ? ` (${err}\u00b0 drift)` : ""}: possible window open or appliance`;
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
function renderSavingsCard(states, hass, isProjected = false) {
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

  // Projected disclaimer banner
  const projectedBanner = isProjected
    ? `<div class="projected-banner">Projected &mdash; based on early model estimates. Accuracy improves as baseline observation completes.</div>`
    : "";

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
      ${projectedBanner}
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

  // Activation tier badge
  const activationTier = progress?.attributes?.activation_tier || "learning";
  const ATIER = {
    learning:     { label: "Learning",      cls: "atier-learning",      dots: 0, desc: "Observing your home" },
    conservative: { label: "Conservative",  cls: "atier-conservative",  dots: 1, desc: "Active with wider comfort bands" },
    standard:     { label: "Active",        cls: "atier-standard",      dots: 2, desc: "Full optimization" },
    confident:    { label: "Confident",     cls: "atier-confident",     dots: 3, desc: "High-accuracy optimization" },
  };
  const at = ATIER[activationTier] || ATIER.learning;
  const atDots = Array.from({ length: 3 }, (_, i) =>
    `<span class="atier-dot${i < at.dots ? " filled" : ""}"></span>`
  ).join("");

  // Learning needs
  const learningNeeds = confidence?.attributes?.learning_needs || [];
  const needsHtml = learningNeeds.length
    ? `<div class="health-needs">${learningNeeds.map(n => `<div class="health-need-item">${n}</div>`).join("")}</div>`
    : "";

  return `
    <div class="card health-card">
      <h2>System Health</h2>
      <div class="atier-header">
        <span class="atier-badge ${at.cls}"><span class="atier-dots">${atDots}</span> ${at.label}</span>
        <span class="atier-desc">${at.desc}</span>
      </div>
      ${dayInfo ? `<div class="day-info">${dayInfo}</div>` : ""}
      ${needsHtml}
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

/** [F] Building Profile — what has been measured about the house and equipment. */
function renderBuildingCard(states, hass) {
  // All of these are measured from the home's own temperature history. The
  // card used to grade an "R-value", a thermal mass, an HVAC tonnage and a
  // solar gain; those were states of a thermal estimator that has been
  // removed, and the entities behind them no longer exist.
  const timeConstant = findEntity(states, "thermal_time_constant");
  const thermalMass = findEntity(states, "thermal_mass");
  const coolRate = findEntity(states, "effective_cooling_rate");
  const heatRate = findEntity(states, "effective_heating_rate");
  const confidence = findEntity(states, "model_confidence");

  const hasData = hasValue(timeConstant) || hasValue(coolRate) || hasValue(heatRate);
  if (!hasData) return "";

  const confPct = hasValue(confidence) ? Number(confidence.state) : 0;
  const converging = confPct < 50;
  const unit = tempUnit(hass);

  // ── Position bar: track with a dot marker ──
  function posBar(pct, colorVar) {
    const clamped = Math.max(3, Math.min(97, pct));
    return `<div class="profile-bar-track"><div class="profile-bar-marker" style="left:${clamped}%;background:${colorVar}"></div></div>`;
  }

  const profileRows = [];
  let narrative = "";

  if (hasValue(timeConstant)) {
    const tau = Number(timeConstant.state);
    let label, cls, color;
    if (tau >= 24) { label = "Holds temperature well"; cls = "quality-good"; color = "var(--green)"; }
    else if (tau < 10) { label = "Drifts quickly"; cls = "quality-poor"; color = "var(--red)"; }
    else { label = "Typical"; cls = "quality-medium"; color = "var(--orange)"; }
    profileRows.push(`
      <div class="profile-row">
        <div class="profile-row-header">
          <span class="profile-label">Heat retention</span>
          <span class="profile-qual ${cls}">${label} (${tau.toFixed(0)} h)</span>
        </div>
        ${posBar(Math.min(100, (tau / 48) * 100), color)}
        <div class="profile-scale"><span>Drifts quickly</span><span>Holds temperature</span></div>
      </div>`);
    narrative = `With the system off, the gap between indoor and outdoor temperature closes with a time constant of about ${tau.toFixed(0)} hours, averaged over the conditions seen so far.`;
  }

  // Equipment rates at the current outdoor temperature, envelope load removed
  const rateRows = [];
  if (hasValue(heatRate)) rateRows.push(["Heating now", Math.abs(Number(heatRate.state))]);
  if (hasValue(coolRate)) rateRows.push(["Cooling now", Math.abs(Number(coolRate.state))]);
  for (const [label, rate] of rateRows) {
    profileRows.push(`
      <div class="profile-row">
        <div class="profile-row-header">
          <span class="profile-label">${label}</span>
          <span class="profile-qual">${rate.toFixed(1)} ${unit}/h</span>
        </div>
        ${posBar(Math.min(100, (rate / 5) * 100), "var(--blue)")}
        <div class="profile-scale"><span>Slower</span><span>Faster</span></div>
      </div>`);
  }

  // ── Raw values (collapsible, power users) ──
  const rawRows = [];
  if (hasValue(timeConstant)) rawRows.push(`<div class="diag-row"><span>Time constant</span><span>${Number(timeConstant.state).toFixed(1)} h</span></div>`);
  if (hasValue(heatRate)) rawRows.push(`<div class="diag-row"><span>Heating rate</span><span>${Number(heatRate.state).toFixed(2)} ${unit}/h</span></div>`);
  if (hasValue(coolRate)) rawRows.push(`<div class="diag-row"><span>Cooling rate</span><span>${Number(coolRate.state).toFixed(2)} ${unit}/h</span></div>`);
  if (hasValue(thermalMass)) rawRows.push(`<div class="diag-row"><span>Thermal mass (from floor area)</span><span>${Number(thermalMass.state).toFixed(0)} BTU/\u00b0F</span></div>`);

  const modelTypeLine = isAvailable(confidence)
    ? `<div class="model-type">${confPct > 0 ? "Measured profile" : "Default profile"}</div>`
    : "";

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

  // Component data from the primary sensor's attributes (environmental + HVAC)
  const envComponents = [
    { key: "weather", label: "Weather", value: attrs.weather_heat_transfer ?? 0, color: "var(--orange)" },
    { key: "solar", label: "Solar", value: attrs.solar_heat_gain ?? 0, color: "#fdd835" },
    { key: "people", label: "People", value: attrs.occupancy_heat_gain ?? 0, color: "var(--accent)" },
    { key: "boundary", label: "Boundary", value: attrs.boundary_zone_heat_transfer ?? 0, color: "#8d6e63" },
    { key: "appliances", label: "Appliances", value: attrs.appliance_load ?? 0, color: "var(--blue)" },
  ];
  const hvacComponent = { key: "hvac", label: "HVAC Output", value: attrs.hvac_output ?? 0, color: "#4caf50" };
  const components = [...envComponents, ...(Math.abs(hvacComponent.value) > 0.5 ? [hvacComponent] : [])];

  // Filter out zero-value components and split by direction
  const active = components.filter(c => Math.abs(c.value) > 0.5);
  const gains = active.filter(c => c.value > 0);
  const removals = active.filter(c => c.value < 0);

  // Shared scale: both bars normalize against the same max so relative width is truthful
  const gainTotal = gains.reduce((s, c) => s + Math.abs(c.value), 0);
  const removalTotal = removals.reduce((s, c) => s + Math.abs(c.value), 0);
  const sharedMax = Math.max(gainTotal, removalTotal, 1);

  // Build a stacked bar from a set of components, scaled relative to sharedMax
  function stackedBar(items, cssExtra) {
    const total = items.reduce((s, c) => s + Math.abs(c.value), 0);
    if (total === 0) return "";
    const barWidthPct = (total / sharedMax) * 100;
    let left = 0;
    const segs = items.map(c => {
      const pct = Math.max(2, (Math.abs(c.value) / total) * 100);
      const seg = `<div class="tl-bar-seg" data-label="${c.label}" data-value="${fmtBtu(c.value)} BTU/hr" style="left:${left}%;width:${pct}%;background:${c.color}"></div>`;
      left += pct;
      return seg;
    }).join("");
    return `<div class="tl-stacked-bar${cssExtra || ""}" style="width:${barWidthPct}%">${segs}</div>`;
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
      <div class="tl-bar-track">${stackedBar(gains, converging ? " tl-converging" : "")}</div>`;
  }

  // Removal bar
  let removalSection = "";
  if (removals.length > 0) {
    removalSection = `
      <div class="tl-bar-label">Removing heat</div>
      <div class="tl-bar-track">${stackedBar(removals, converging ? " tl-converging" : "")}</div>`;
  }

  // Legend — only active components
  const legendItems = active.map(c =>
    `<span class="tl-load-legend-item"><span class="tl-load-legend-dot" style="background:${c.color}"></span>${c.label}</span>`
  ).join("");

  // Collapsible details — all components including HVAC and stored heat
  const allDetailComponents = [
    ...envComponents,
    hvacComponent,
  ];
  // Only components the sensor actually reports. The itemised breakdown came
  // from the removed thermal estimator; listing each as "+0 BTU/hr" read as
  // a measurement of nothing.
  const detailRows = [
    ...allDetailComponents.filter(c => Math.abs(c.value) > 0.5).map(c =>
      `<div class="diag-row"><span>${c.label}</span><span>${fmtBtu(c.value)} BTU/hr</span></div>`
    ),
    attrs.stored_heat_exchange != null
      ? `<div class="diag-row"><span>Stored Heat</span><span>${fmtBtu(attrs.stored_heat_exchange)} BTU/hr</span></div>` : "",
  ].filter(Boolean).join("");

  return `
    <div class="card thermal-load-card">
      <div class="building-header">
        <h2>House Thermal Load</h2>
        ${converging ? `<span class="converging-tag">Estimates</span>` : ""}
      </div>
      <div class="tl-headline">
        <span class="tl-headline-value">${fmtBtu(netLoad)} BTU/hr</span>
        <span class="tl-headline-dir">${direction}</span>
      </div>
      ${gainSection}
      ${removalSection}
      <div class="tl-seg-tooltip" style="display:none"></div>
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
  const timeConstant = findEntity(states, "thermal_time_constant");
  const coolRate = findEntity(states, "effective_cooling_rate");
  const heatRate = findEntity(states, "effective_heating_rate");
  const baselineConf = findEntity(states, "baseline_confidence");
  const profilerStatus = findEntity(states, "profiler_status") || findEntity(states, "profiler");

  // ── Thermal Model card ──
  const confPct = hasValue(modelConf) ? Math.min(100, Number(modelConf.state)) : 0;
  let rLine = "", massLine = "", capLine = "";
  if (hasValue(timeConstant)) {
    const tau = Number(timeConstant.state);
    let lbl, cls;
    if (tau >= 24) { lbl = "Holds temperature well"; cls = "quality-good"; }
    else if (tau < 10) { lbl = "Drifts quickly"; cls = "quality-poor"; }
    else { lbl = "Typical"; cls = "quality-medium"; }
    rLine = `<div class="lp-param"><span class="lp-param-label">Heat retention</span><span class="lp-param-val">${tau.toFixed(0)} h</span><span class="lp-param-qual ${cls}">${lbl}</span></div>`;
  }
  if (hasValue(heatRate)) {
    massLine = `<div class="lp-param"><span class="lp-param-label">Heating rate</span><span class="lp-param-val">${Math.abs(Number(heatRate.state)).toFixed(2)} \u00b0F/h</span></div>`;
  }
  if (hasValue(coolRate)) {
    capLine = `<div class="lp-param"><span class="lp-param-label">Cooling rate</span><span class="lp-param-val">${Math.abs(Number(coolRate.state)).toFixed(2)} \u00b0F/h</span></div>`;
  }
  // Per-mode profiler confidence. This previously read the EKF's
  // per-parameter confidence, whose keys no longer exist, so every row
  // rendered empty.
  const paramAttrs = modelConf?.attributes || {};
  const PARAM_CONF = [
    ["cool_confidence", "Cooling"],
    ["heat_confidence", "Heating"],
    ["resist_confidence", "Passive Drift"],
    ["aux_heat_confidence", "Aux Heat"],
  ];
  const paramConfRows = PARAM_CONF.map(([key, label]) => {
    const val = paramAttrs[key];
    if (val == null) return "";
    const cls = val >= 70 ? "quality-good" : val >= 30 ? "quality-medium" : "quality-poor";
    return `<div class="lp-param"><span class="lp-param-label">${label}</span><span class="lp-param-qual ${cls}">${val}%</span></div>`;
  }).filter(Boolean).join("");

  // Learning needs
  const modelNeeds = paramAttrs.learning_needs || [];
  const needsRows = modelNeeds.length
    ? `<div class="lp-needs">${modelNeeds.map(n => `<div class="lp-need-item">${n}</div>`).join("")}</div>`
    : "";

  const modelCard = `
    <div class="card lp-card">
      <div class="lp-card-title">Thermal Model</div>
      <div class="lp-bar-track"><div class="lp-bar-fill lp-bar-model" style="width:${confPct}%"></div></div>
      <div class="lp-conf-pct">${confPct.toFixed(0)}% confident</div>
      <div class="lp-params">${rLine}${massLine}${capLine}</div>
      ${paramConfRows ? `<div class="lp-params" style="margin-top:6px;border-top:1px solid #333;padding-top:6px">${paramConfRows}</div>` : ""}
      ${needsRows}
      ${confPct < 20 && !needsRows ? `<div class="lp-ready-note">Refining estimates\u2026</div>` : ""}
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
      <div class="lp-params">${modeRows}</div>
      <div class="lp-obs-note">+1 observation every 5 min</div>
    </div>`;

  return `<div class="lp-grid">${modelCard}${baselineCard}${profilerCard}</div>`;
}

/** [G2] Temperature Profile Chart — observation coverage by outdoor temp. */
function renderTemperatureProfile(states) {
  const profiler = findEntity(states, "profiler_status") || findEntity(states, "profiler");
  const chartData = profiler?.attributes?.chart_data;
  if (!chartData) return "";

  const MODE_COLORS = {
    heat_1: "#e8912d",
    cool_1: "#4fa8d6",
    auxiliary_heat_1: "#e05555",
    resist: "#888888",
  };
  const MODE_LABELS = {
    heat_1: "Heating",
    cool_1: "Cooling",
    auxiliary_heat_1: "Aux Heat",
    resist: "Passive Drift",
  };
  const DRAW_ORDER = ["resist", "heat_1", "cool_1", "auxiliary_heat_1"];

  // Collect all observed temps across modes to set axis bounds
  let allTemps = [];
  let allDeltas = [];
  for (const mode of DRAW_ORDER) {
    const md = chartData[mode];
    if (!md || !md.bins) continue;
    for (const [t, info] of Object.entries(md.bins)) {
      allTemps.push(Number(t));
      allDeltas.push(info.mean_delta);
    }
  }
  if (allTemps.length < 3) return "";

  // Axis bounds — scale to observed range with padding
  const tMin = Math.min(...allTemps) - 3;
  const tMax = Math.max(...allTemps) + 3;
  const dMin = Math.min(...allDeltas, -1);
  const dMax = Math.max(...allDeltas, 1);
  const dPad = (dMax - dMin) * 0.15;
  const yMin = dMin - dPad;
  const yMax = dMax + dPad;

  // SVG dimensions
  const W = 540, H = 220, pad = { l: 52, r: 16, t: 12, b: 32 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;

  const xScale = (t) => pad.l + (t - tMin) / (tMax - tMin) * plotW;
  const yScale = (d) => pad.t + (1 - (d - yMin) / (yMax - yMin)) * plotH;

  // Grid lines + axis labels
  let gridSvg = "";
  // Y-axis: zero line + a few ticks
  const yTicks = [];
  const yStep = Math.max(0.5, Math.round((yMax - yMin) / 4 * 2) / 2);
  for (let v = Math.ceil(yMin / yStep) * yStep; v <= yMax; v += yStep) {
    yTicks.push(v);
  }
  for (const v of yTicks) {
    const y = yScale(v);
    const isZero = Math.abs(v) < 0.01;
    gridSvg += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y}" y2="${y}" stroke="${isZero ? '#666' : '#333'}" stroke-width="${isZero ? 1.5 : 0.5}" ${isZero ? '' : 'stroke-dasharray="4,3"'}/>`;
    gridSvg += `<text x="${pad.l - 6}" y="${y + 4}" fill="#aaa" font-size="10" text-anchor="end">${v.toFixed(1)}</text>`;
  }
  // X-axis ticks
  const xStep = Math.max(5, Math.round((tMax - tMin) / 6 / 5) * 5);
  for (let t = Math.ceil(tMin / xStep) * xStep; t <= tMax; t += xStep) {
    const x = xScale(t);
    gridSvg += `<line x1="${x}" x2="${x}" y1="${pad.t}" y2="${H - pad.b}" stroke="#333" stroke-width="0.5" stroke-dasharray="4,3"/>`;
    gridSvg += `<text x="${x}" y="${H - pad.b + 14}" fill="#aaa" font-size="10" text-anchor="middle">${t}\u00b0</text>`;
  }
  // Axis labels
  gridSvg += `<text x="${W / 2}" y="${H - 2}" fill="#777" font-size="10" text-anchor="middle">Outdoor Temperature (\u00b0F)</text>`;
  gridSvg += `<text x="12" y="${H / 2}" fill="#777" font-size="10" text-anchor="middle" transform="rotate(-90,12,${H / 2})">\u0394T/hr (\u00b0F/hr)</text>`;

  // Plot data points + trendlines per mode
  let dotsSvg = "";
  let trendSvg = "";
  let legendItems = [];

  for (const mode of DRAW_ORDER) {
    const md = chartData[mode];
    if (!md || !md.bins) continue;
    const color = MODE_COLORS[mode];
    const bins = md.bins;
    const entries = Object.entries(bins).map(([t, info]) => [Number(t), info]);
    if (entries.length === 0) continue;

    // Dots — size scaled by observation count
    for (const [t, info] of entries) {
      const x = xScale(t);
      const y = yScale(info.mean_delta);
      const r = Math.min(5, Math.max(2, Math.sqrt(info.count / 6)));
      const opacity = info.count >= 6 ? 0.85 : 0.35;
      dotsSvg += `<circle cx="${x}" cy="${y}" r="${r}" fill="${color}" opacity="${opacity}"/>`;
    }

    // Trendline — only within observed range
    if (md.trendline && md.trendline.slope !== 0 && md.temp_range) {
      const [rMin, rMax] = md.temp_range;
      const x1 = xScale(rMin);
      const x2 = xScale(rMax);
      const y1 = yScale(md.trendline.slope * rMin + md.trendline.intercept);
      const y2 = yScale(md.trendline.slope * rMax + md.trendline.intercept);
      trendSvg += `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${color}" stroke-width="2" stroke-dasharray="6,3" opacity="0.8"/>`;
    }

    legendItems.push(`<span style="display:inline-flex;align-items:center;gap:4px;margin-right:12px"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color}"></span>${MODE_LABELS[mode]} (${md.total_observations})</span>`);
  }

  // Balance point markers
  let bpSvg = "";
  const bps = chartData.balance_points || {};
  for (const [mode, bp] of Object.entries(bps)) {
    if (bp >= tMin && bp <= tMax) {
      const x = xScale(bp);
      bpSvg += `<line x1="${x}" x2="${x}" y1="${pad.t}" y2="${H - pad.b}" stroke="${MODE_COLORS[mode] || '#666'}" stroke-width="1" stroke-dasharray="2,4" opacity="0.5"/>`;
      bpSvg += `<text x="${x}" y="${pad.t - 2}" fill="${MODE_COLORS[mode] || '#666'}" font-size="9" text-anchor="middle">${bp}\u00b0</text>`;
    }
  }

  const svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">
    ${gridSvg}${bpSvg}${dotsSvg}${trendSvg}
  </svg>`;

  const legend = `<div style="display:flex;flex-wrap:wrap;gap:2px 0;font-size:11px;color:#aaa;padding:4px 0">${legendItems.join("")}</div>`;

  // Gap annotation
  let gapNote = "";
  for (const mode of ["heat_1", "cool_1"]) {
    const md = chartData[mode];
    if (md && md.total_observations > 0 && md.total_observations < 30) {
      gapNote = `<div style="font-size:11px;color:#888;padding:2px 0">More ${MODE_LABELS[mode].toLowerCase()} data at varied outdoor temps will improve accuracy</div>`;
      break;
    }
  }

  return `
    <div class="card">
      <div class="section-title">Temperature Profile</div>
      <div style="width:100%">${svg}</div>
      ${legend}${gapNote}
    </div>`;
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
    { done: tierVal !== "learning" && tierVal !== "projected", label: "Savings comparison & optimization active", sub: tierVal === "projected" ? "projected (accumulating baseline)" : (tierVal !== "learning" ? tierVal : null), current: tierVal === "projected" },
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

// ── Dashboard tabs: Now / Model / Energy ─────────────────────────────
//
// Charts are drawn as SVG strings at the panel's measured pixel width, so
// marks and text keep their real size at any width. Series colours are CSS
// variables (see PANEL_CSS) and follow the entity, not its position.

const DV_HOUR = 3600e3;
const DV_DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const DV_MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DV_TABS = [
  ["overview", "Overview"],
  ["now", "Now"],
  ["model", "Model"],
  ["energy", "Energy"],
  ["twin", "Twin"],
];

function dvNum(v) {
  if (v == null || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
function dvFmt(v, digits = 1) {
  const n = dvNum(v);
  return n == null ? "–" : n.toFixed(digits);
}
function dvSigned(v, digits = 2) {
  const n = dvNum(v);
  if (n == null) return "–";
  return (n > 0 ? "+" : n < 0 ? "−" : "") + Math.abs(n).toFixed(digits);
}
function dvPad(n) {
  return String(n).padStart(2, "0");
}
function dvHM(ms) {
  const d = new Date(ms);
  return dvPad(d.getHours()) + ":" + dvPad(d.getMinutes());
}
function dvScale(d0, d1, r0, r1) {
  const k = (r1 - r0) / (d1 - d0 || 1);
  return (v) => r0 + (v - d0) * k;
}
function dvPath(pts) {
  return pts.length ? "M" + pts.map((p) => p[0].toFixed(1) + " " + p[1].toFixed(1)).join("L") : "";
}
/** Step path: hold each value until the next point's x. */
function dvStepPath(pts) {
  if (!pts.length) return "";
  let d = "M" + pts[0][0].toFixed(1) + " " + pts[0][1].toFixed(1);
  for (let i = 1; i < pts.length; i++) d += "H" + pts[i][0].toFixed(1) + "V" + pts[i][1].toFixed(1);
  return d;
}
function dvTicks(lo, hi, target = 5) {
  const span = hi - lo;
  if (!(span > 0)) return [lo];
  const raw = span / target;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map((m) => m * pow).find((m) => m >= raw) || raw;
  const out = [];
  for (let v = Math.ceil(lo / step - 1e-9) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}
function dvSeries(arr) {
  const out = [];
  for (const p of arr || []) {
    const v = Number(p.state);
    const t = Date.parse(p.last_changed || p.last_updated || "");
    if (Number.isFinite(v) && Number.isFinite(t)) out.push([t, v]);
  }
  return out;
}
/** Last value at or before t in a time-sorted [[t, v]] series. */
function dvAt(series, t) {
  let lo = 0;
  let hi = series.length - 1;
  let ans = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (series[mid][0] <= t) {
      ans = series[mid][1];
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return ans;
}
function dvEmpty(text) {
  return `<div class="dv-empty">${esc(text)}</div>`;
}
function dvLegend(items) {
  return `<div class="dv-legend">${items
    .map(([cls, label]) => `<span><i class="dv-key ${cls}"></i>${esc(label)}</span>`)
    .join("")}</div>`;
}
function dvCard(title, meta, body, cls = "") {
  return `<section class="card dv-card ${cls}"><div class="dv-head"><h2>${esc(title)}</h2>${
    meta ? `<span class="dv-meta">${meta}</span>` : ""
  }</div>${body}</section>`;
}

// ── Now ──────────────────────────────────────────────────────────────

function dvStatStrip(states, hass) {
  const unit = tempUnit(hass);
  const sched = findEntity(states, "schedule");
  const a = (sched && sched.attributes) || {};
  const fc = Array.isArray(a.forecast) ? a.forecast : [];
  const entries = Array.isArray(a.entries) ? a.entries : [];
  const now = Date.now();

  const indoor = findEntity(states, "weighted_indoor_temp") || findEntity(states, "apparent_temperature");
  const outdoor = findEntity(states, "outdoor_temp_source");
  const phase = findEntity(states, "current_phase");
  const mae = findEntity(states, "model_accuracy");
  const profiler = findEntity(states, "profiler_status");

  const live = fc.find((p) => Date.parse(p.time) + DV_HOUR > now) || fc[0];
  const floor = live ? dvNum(a.mode === "cool" ? live.comfort_max : live.comfort_min) : null;
  const floorText = floor == null ? "" : `${a.mode === "cool" ? "ceiling" : "floor"} ${dvFmt(floor, 0)}°`;

  let low = null;
  for (const p of fc) {
    const t = Date.parse(p.time);
    const v = dvNum(p.outdoor);
    if (v == null || t < now || t > now + 24 * DV_HOUR) continue;
    if (!low || v < low.v) low = { v, t };
  }

  const current = entries.find((e) => Date.parse(e.start) <= now && Date.parse(e.end) > now) || entries[0];
  const next = current
    ? entries.find((e) => Date.parse(e.start) > now && dvNum(e.target_temp) !== dvNum(current.target_temp))
    : null;
  const recovery = (Array.isArray(a.morning_recovery) ? a.morning_recovery : []).find(
    (r) => next && Math.abs(Date.parse(r.recovery_start) - Date.parse(next.start)) < 60e3
  );

  const phaseInfo = PHASE_MAP[(phase && phase.state) || ""] || { label: (phase && phase.state) || "–" };
  const tactical = phase && phase.attributes ? phase.attributes : {};
  const detail = (profiler && profiler.attributes && profiler.attributes.mode_detail) || {};
  const bars = [
    ["drift", "resist"],
    ["cool", "cool_1"],
    ["heat", "heat_1"],
    ["aux", "auxiliary_heat_1"],
  ]
    .map(([label, key]) => {
      const c = dvNum(detail[key] && detail[key].confidence) || 0;
      return `<span>${label}</span><span class="dv-bar"><i style="width:${Math.max(0, Math.min(100, c))}%"></i></span><span class="dv-n">${c.toFixed(0)}</span>`;
    })
    .join("");

  const cell = (label, value, sub) =>
    `<div class="dv-stat"><div class="dv-stat-label">${label}</div><div class="dv-stat-value">${value}</div><div class="dv-stat-sub">${sub || "&nbsp;"}</div></div>`;

  return `<section class="card dv-stats">
    ${cell("Indoor", `${fmt(indoor)}<small>${esc(unit)}</small>`, floorText)}
    ${cell("Outdoor", `${fmt(outdoor)}<small>${esc(unit)}</small>`, low ? `low ${dvFmt(low.v, 0)}° at ${dvHM(low.t)}` : "")}
    ${cell("Phase", esc(phaseInfo.label), tactical.tactical_state ? `${esc(tactical.tactical_state)}, ${dvSigned(tactical.tactical_correction, 1)}°` : "")}
    ${cell(
      "Next change",
      next ? `${dvHM(Date.parse(next.start))}<small> → ${dvFmt(next.target_temp, 0)}°</small>` : "–",
      recovery ? `warm by ${dvHM(Date.parse(recovery.target_time))}` : ""
    )}
    ${cell(
      "Forecast error",
      `${fmt(mae, 2)}<small>° MAE</small>`,
      mae && mae.attributes && mae.attributes.model_bias != null ? `bias ${dvSigned(mae.attributes.model_bias)}°` : ""
    )}
    <div class="dv-stat"><div class="dv-stat-label">Coverage %</div><div class="dv-cov">${bars}</div></div>
  </section>`;
}

/** Interpolated 90th-percentile forecast error at a lead time, or null. */
function dvP90(dash) {
  const leads = ((dash && dash.verification && dash.verification.leads) || []).filter((l) => l.n >= 12);
  if (leads.length < 2) return null;
  const pts = [[0, 0]].concat(leads.map((l) => [l.lead, l.p90]));
  return (h) => {
    for (let i = 1; i < pts.length; i++) {
      if (h <= pts[i][0]) {
        const f = (h - pts[i - 1][0]) / (pts[i][0] - pts[i - 1][0]);
        return pts[i - 1][1] + f * (pts[i][1] - pts[i - 1][1]);
      }
    }
    return pts[pts.length - 1][1];
  };
}

function dvTimeline(states, retro, dash, W) {
  const sched = findEntity(states, "schedule");
  const a = (sched && sched.attributes) || {};
  const cooling = a.mode === "cool";
  const now = Date.now();
  const t0 = now - 24 * DV_HOUR;
  const fc = (Array.isArray(a.forecast) ? a.forecast : [])
    .map((p) => ({
      t: Date.parse(p.time),
      in: dvNum(p.indoor),
      out: dvNum(p.outdoor),
      hvac: !!p.hvac,
      lo: dvNum(p.comfort_min),
      hi: dvNum(p.comfort_max),
    }))
    .filter((p) => Number.isFinite(p.t) && p.t > now - DV_HOUR && p.t <= now + 24 * DV_HOUR);
  const t1 = fc.length ? now + 24 * DV_HOUR : now + DV_HOUR;

  const r = retro || {};
  const indoor = dvSeries(r.indoorW && r.indoorW.length ? r.indoorW : r.indoor).filter((p) => p[0] >= t0);
  const outdoor = dvSeries(r.outdoor).filter((p) => p[0] >= t0);
  const spRaw = (r.setpoints || []).map((p) => [p.t, cooling ? p.high : p.low]).filter((p) => p[1] != null);
  const plan = (Array.isArray(a.entries) ? a.entries : [])
    .map((e) => ({ s: Date.parse(e.start), e: Date.parse(e.end), v: dvNum(e.target_temp) }))
    .filter((e) => e.v != null && e.e > now && e.s < t1);

  if (indoor.length < 2 && !fc.length) return { html: dvEmpty("Waiting for history and a plan."), model: null };

  // Geometry
  const ML = 38;
  const MR = 10;
  const x = dvScale(t0, t1, ML, W - MR);
  const P1 = { top: 32, h: 224 };
  const P2 = { top: 268, h: 90 };
  const P3 = { top: 370, h: 44 };
  const H = 434;

  // Indoor domain
  const vals = [];
  indoor.forEach((p) => vals.push(p[1]));
  fc.forEach((p) => {
    if (p.in != null) vals.push(p.in);
    if (p.lo != null) vals.push(p.lo);
    if (p.hi != null) vals.push(p.hi);
  });
  spRaw.forEach((p) => p[0] >= t0 - 12 * DV_HOUR && vals.push(p[1]));
  plan.forEach((e) => vals.push(e.v));
  const yLo = Math.floor(Math.min(...vals) - 1);
  const yHi = Math.ceil(Math.max(...vals) + 1);
  const y1 = dvScale(yLo, yHi, P1.top + P1.h, P1.top);
  const clampY = (v) => Math.max(P1.top, Math.min(P1.top + P1.h, y1(v)));

  // Outdoor domain
  const outs = outdoor.map((p) => p[1]).concat(fc.filter((p) => p.out != null).map((p) => p.out));
  const oLo = outs.length ? Math.floor((Math.min(...outs) - 2) / 5) * 5 : 0;
  const oHi = outs.length ? Math.ceil((Math.max(...outs) + 2) / 5) * 5 : 10;
  const y2 = dvScale(oLo, oHi, P2.top + P2.h, P2.top);

  const parts = [];
  const xNow = x(now);
  const right = W - MR;

  // Future shading, grids, ticks
  for (const P of [P1, P2, P3]) {
    parts.push(`<rect x="${xNow.toFixed(1)}" y="${P.top}" width="${Math.max(0, right - xNow).toFixed(1)}" height="${P.h}" class="dv-future"/>`);
  }
  const first6 = new Date(t0);
  first6.setMinutes(0, 0, 0);
  first6.setHours(Math.ceil((first6.getHours() + 0.01) / 6) * 6);
  for (let t = first6.getTime(); t <= t1; t += 6 * DV_HOUR) {
    const px = x(t);
    if (px < ML || px > right) continue;
    parts.push(`<line x1="${px.toFixed(1)}" x2="${px.toFixed(1)}" y1="${P1.top}" y2="${P3.top + P3.h}" class="dv-grid"/>`);
    const d = new Date(t);
    const label = d.getHours() === 0 ? `${DV_DOW[d.getDay()]} 00:00` : dvHM(t);
    parts.push(`<text x="${px.toFixed(1)}" y="${H - 6}" text-anchor="middle"${d.getHours() === 0 ? ' class="dv-strong"' : ""}>${label}</text>`);
  }
  for (const v of dvTicks(yLo, yHi, 5)) {
    parts.push(`<line x1="${ML}" x2="${right}" y1="${y1(v).toFixed(1)}" y2="${y1(v).toFixed(1)}" class="dv-grid"/>`);
    parts.push(`<text x="${ML - 6}" y="${(y1(v) + 4).toFixed(1)}" text-anchor="end">${v}</text>`);
  }
  for (const v of dvTicks(oLo, oHi, 3)) {
    parts.push(`<line x1="${ML}" x2="${right}" y1="${y2(v).toFixed(1)}" y2="${y2(v).toFixed(1)}" class="dv-grid"/>`);
    parts.push(`<text x="${ML - 6}" y="${(y2(v) + 4).toFixed(1)}" text-anchor="end">${v}</text>`);
  }
  parts.push(`<text x="0" y="${P1.top + 10}">in</text><text x="0" y="${P2.top + 10}">out</text><text x="0" y="${P3.top + 10}">duty</text>`);
  parts.push(`<line x1="${ML}" x2="${right}" y1="${P3.top + P3.h}" y2="${P3.top + P3.h}" class="dv-axis"/>`);

  // Promised band and error fan (future)
  if (fc.length) {
    const band = fc.filter((p) => p.lo != null && p.hi != null && p.t + DV_HOUR > now);
    if (band.length) {
      const edge = (p) => Math.max(xNow, x(p.t));
      const last = Math.min(right, x(band[band.length - 1].t + DV_HOUR));
      let d = `M${edge(band[0]).toFixed(1)} ${clampY(band[0].hi).toFixed(1)}`;
      for (let i = 1; i < band.length; i++) d += `H${edge(band[i]).toFixed(1)}V${clampY(band[i].hi).toFixed(1)}`;
      d += `H${last.toFixed(1)}V${clampY(band[band.length - 1].lo).toFixed(1)}`;
      for (let i = band.length - 1; i >= 1; i--) d += `H${edge(band[i]).toFixed(1)}V${clampY(band[i - 1].lo).toFixed(1)}`;
      d += `H${edge(band[0]).toFixed(1)}Z`;
      parts.push(`<path d="${d}" class="dv-band"/>`);
    }
    const p90 = dvP90(dash);
    const pts = fc.filter((p) => p.in != null && p.t >= now - DV_HOUR);
    if (p90 && pts.length > 1) {
      const up = pts.map((p) => [x(p.t), clampY(p.in + p90(Math.max(0, (p.t - now) / DV_HOUR)))]);
      const dn = pts.map((p) => [x(p.t), clampY(p.in - p90(Math.max(0, (p.t - now) / DV_HOUR)))]).reverse();
      parts.push(`<path d="${dvPath(up.concat(dn))}Z" class="dv-fan"/>`);
    }
  }

  // Markers: recovery start and deadline, setpoint changes
  const marks = [{ t: now, label: `now ${dvHM(now)}`, strong: true }];
  for (const rec of Array.isArray(a.morning_recovery) ? a.morning_recovery : []) {
    const s = Date.parse(rec.recovery_start);
    const e = Date.parse(rec.target_time);
    if (s > now && s < t1) marks.push({ t: s, label: `${dvHM(s)} recovery`, strong: true });
    if (e > now && e < t1) marks.push({ t: e, label: `${dvHM(e)} deadline`, strong: true });
  }
  for (let i = 1; i < spRaw.length; i++) {
    const [t, v] = spRaw[i];
    if (t > t0 && t < now && Math.abs(v - spRaw[i - 1][1]) >= 1) marks.push({ t, label: `${dvHM(t)} → ${dvFmt(v, 0)}°` });
  }
  for (let i = 1; i < plan.length; i++) {
    if (plan[i].v !== plan[i - 1].v && plan[i].s > now && !marks.some((m) => Math.abs(m.t - plan[i].s) < 60e3)) {
      marks.push({ t: plan[i].s, label: `${dvHM(plan[i].s)} → ${dvFmt(plan[i].v, 0)}°` });
    }
  }
  const lanes = [[], []];
  marks
    .sort((p, q) => (q.strong ? 1 : 0) - (p.strong ? 1 : 0) || p.t - q.t)
    .forEach((m) => {
      const px = x(m.t);
      const w = m.label.length * 6.1 + 8;
      const flip = px + w > right;
      const lo = flip ? px - w : px;
      const hi = flip ? px : px + w;
      if (m.t !== now) {
        parts.push(`<line x1="${px.toFixed(1)}" x2="${px.toFixed(1)}" y1="${P1.top}" y2="${P1.top + P1.h}" class="dv-mark"/>`);
      }
      const lane = lanes.findIndex((row) => !row.some(([p, q]) => lo < q && hi > p));
      if (lane < 0) return;
      lanes[lane].push([lo, hi]);
      parts.push(
        `<text x="${(flip ? px - 4 : px + 4).toFixed(1)}" y="${lane === 0 ? 11 : 24}" text-anchor="${flip ? "end" : "start"}"${m.strong ? ' class="dv-strong"' : ""}>${esc(m.label)}</text>`
      );
    });

  // Setpoints
  if (spRaw.length) {
    const pts = [];
    for (const [t, v] of spRaw) {
      if (t > now) break;
      if (t <= t0) pts[0] = [x(t0), clampY(v)];
      else pts.push([x(t), clampY(v)]);
    }
    if (pts.length) {
      pts.push([xNow, pts[pts.length - 1][1]]);
      parts.push(`<path d="${dvStepPath(pts)}" class="dv-line dv-sp"/>`);
    }
  }
  if (plan.length) {
    const pts = plan.map((e) => [Math.max(xNow, x(e.s)), clampY(e.v)]);
    pts.push([Math.min(right, x(plan[plan.length - 1].e)), pts[pts.length - 1][1]]);
    parts.push(`<path d="${dvStepPath(pts)}" class="dv-line dv-sp dv-dash"/>`);
  }

  // Indoor
  const fcIn = fc.filter((p) => p.in != null).map((p) => [x(p.t), y1(p.in)]);
  if (fcIn.length > 1) parts.push(`<path d="${dvPath(fcIn)}" class="dv-line dv-indoor dv-dash"/>`);
  if (indoor.length > 1) {
    parts.push(`<path d="${dvPath(indoor.map((p) => [x(p[0]), y1(p[1])]))}" class="dv-line dv-indoor"/>`);
    const end = indoor[indoor.length - 1];
    parts.push(`<circle cx="${x(end[0]).toFixed(1)}" cy="${y1(end[1]).toFixed(1)}" r="4" class="dv-dot dv-indoor-fill"/>`);
  }

  // Outdoor
  const aux = findEntity(states, "aux_heat_threshold");
  const auxF = aux && aux.attributes && aux.attributes.is_learned ? dvNum(aux.state) : null;
  if (!cooling && auxF != null && auxF > oLo && auxF < oHi) {
    parts.push(`<line x1="${ML}" x2="${right}" y1="${y2(auxF).toFixed(1)}" y2="${y2(auxF).toFixed(1)}" class="dv-mark"/>`);
    parts.push(`<text x="${right - 4}" y="${(y2(auxF) - 4).toFixed(1)}" text-anchor="end">aux ${auxF.toFixed(0)}°</text>`);
  }
  const fcOut = fc.filter((p) => p.out != null).map((p) => [x(p.t), y2(p.out)]);
  if (fcOut.length > 1) parts.push(`<path d="${dvPath(fcOut)}" class="dv-line dv-out dv-dash"/>`);
  if (outdoor.length > 1) parts.push(`<path d="${dvPath(outdoor.map((p) => [x(p[0]), y2(p[1])]))}" class="dv-line dv-out"/>`);

  // Duty: half-hour buckets from hvac_action, then planned hours
  const actions = (r.hvacAction || [])
    .map((p) => [Date.parse(p.last_changed), p.state === "heating" || p.state === "cooling" ? 1 : 0])
    .filter((p) => Number.isFinite(p[0]));
  const duty = [];
  if (actions.length) {
    const B = DV_HOUR / 2;
    for (let b = Math.floor(t0 / B) * B; b < now; b += B) {
      const s = Math.max(b, t0);
      const e = Math.min(b + B, now);
      let on = 0;
      let cur = dvAt(actions, s) || 0;
      let at = s;
      for (const [t, v] of actions) {
        if (t <= s) continue;
        if (t >= e) break;
        if (cur) on += t - at;
        at = t;
        cur = v;
      }
      if (cur) on += e - at;
      const f = e > s ? on / (e - s) : 0;
      duty.push([b, f]);
      if (f > 0.01) {
        const h = f * P3.h;
        parts.push(
          `<rect x="${(x(s) + 0.5).toFixed(1)}" y="${(P3.top + P3.h - h).toFixed(1)}" width="${Math.max(1, x(e) - x(s) - 1).toFixed(1)}" height="${h.toFixed(1)}" class="dv-run"><title>${dvHM(b)}: ${(f * 100).toFixed(0)}%</title></rect>`
        );
      }
    }
  }
  for (const p of fc) {
    if (!p.hvac || p.t + DV_HOUR <= now) continue;
    const s = Math.max(xNow, x(p.t));
    const e = Math.min(right, x(p.t + DV_HOUR));
    if (e - s > 1) parts.push(`<rect x="${(s + 0.5).toFixed(1)}" y="${P3.top}" width="${(e - s - 1).toFixed(1)}" height="${P3.h}" class="dv-run dv-planned"/>`);
  }

  // Now line and crosshair
  parts.push(`<line x1="${xNow.toFixed(1)}" x2="${xNow.toFixed(1)}" y1="${P1.top}" y2="${P3.top + P3.h}" class="dv-now"/>`);
  parts.push(`<line x1="0" x2="0" y1="${P1.top}" y2="${P3.top + P3.h}" class="dv-cross" style="display:none"/>`);

  const html = `<svg class="dv-svg dv-tl" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Indoor and outdoor temperature, setpoints and compressor duty, last 24 hours and plan">${parts.join("")}</svg>`;
  const planSp = plan.map((e) => [e.s, e.v]);
  return {
    html,
    model: {
      left: ML, right, t0, t1, now,
      indoor, outdoor, duty,
      fcIn: fc.filter((p) => p.in != null).map((p) => [p.t, p.in]),
      fcOut: fc.filter((p) => p.out != null).map((p) => [p.t, p.out]),
      sp: spRaw, planSp,
    },
  };
}

/** One-line readout for a time on the timeline (used by the crosshair). */
function dvReadout(m, t) {
  const future = t > m.now;
  const d = new Date(t);
  const bits = [`${DV_DOW[d.getDay()]} ${dvHM(t)}`];
  const inV = future ? dvAt(m.fcIn, t) : dvAt(m.indoor, t);
  const outV = future ? dvAt(m.fcOut, t) : dvAt(m.outdoor, t);
  const spV = future ? dvAt(m.planSp, t) : dvAt(m.sp, t);
  if (inV != null) bits.push(`in ${inV.toFixed(1)}°${future ? " fcst" : ""}`);
  if (outV != null) bits.push(`out ${outV.toFixed(1)}°`);
  if (spV != null) bits.push(`set ${spV.toFixed(0)}°`);
  if (!future) {
    const f = dvAt(m.duty, t);
    if (f != null) bits.push(`duty ${(f * 100).toFixed(0)}%`);
  }
  return bits.join("  ·  ");
}

function dvPlanTable(states) {
  const sched = findEntity(states, "schedule");
  const a = (sched && sched.attributes) || {};
  const entries = Array.isArray(a.entries) ? a.entries : [];
  const fc = Array.isArray(a.forecast) ? a.forecast : [];
  const now = Date.now();
  const segs = [];
  for (const e of entries) {
    const s = Date.parse(e.start);
    const end = Date.parse(e.end);
    if (!(end > now) || s > now + 24 * DV_HOUR) continue;
    const m = /:\s*([a-z-]+)/i.exec(e.reason || "");
    const action = m ? m[1] : e.mode || "";
    const v = dvNum(e.target_temp);
    const last = segs[segs.length - 1];
    if (last && last.v === v && last.action === action) last.e = end;
    else segs.push({ s: Math.max(s, now), e: end, v, action });
  }
  if (!segs.length) return dvEmpty("No plan yet.");
  const range = (vals, digits = 0) => {
    const xs = vals.filter((v) => v != null);
    if (!xs.length) return "–";
    const lo = Math.min(...xs);
    const hi = Math.max(...xs);
    return lo.toFixed(digits) === hi.toFixed(digits) ? `${lo.toFixed(digits)}°` : `${lo.toFixed(digits)} to ${hi.toFixed(digits)}°`;
  };
  const rows = segs
    .map((g) => {
      const pts = fc.filter((p) => {
        const t = Date.parse(p.time);
        return t + DV_HOUR > g.s && t < g.e;
      });
      const lo = pts.map((p) => dvNum(p.comfort_min)).filter((v) => v != null);
      const hi = pts.map((p) => dvNum(p.comfort_max)).filter((v) => v != null);
      const promised = lo.length && hi.length ? `${Math.min(...lo).toFixed(0)} to ${Math.max(...hi).toFixed(0)}°` : "–";
      return `<tr><td class="dv-mono">${dvHM(g.s)} to ${dvHM(g.e)}</td><td>${esc(g.action)}</td><td class="dv-num">${dvFmt(g.v, 0)}°</td><td class="dv-num">${promised}</td><td class="dv-num">${range(pts.map((p) => dvNum(p.outdoor)))}</td></tr>`;
    })
    .join("");
  return `<div class="dv-scroll"><table class="dv-table"><thead><tr><th>Window</th><th>Action</th><th class="dv-num">Setpoint</th><th class="dv-num">Promised</th><th class="dv-num">Outdoor</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function dvInputs(states) {
  const src = findEntity(states, "outdoor_temp_source");
  const a = (src && src.attributes) || {};
  const sources = a.source_health_sources || {};
  const rows = Object.entries(sources).map(([name, s]) => {
    const what = (s && (s.entity_id || s.source)) || "";
    return `<tr><td>${esc(name.replace(/_/g, " "))}</td><td class="dv-mono">${esc(String(what).replace(/^entity:/, ""))}</td><td>${esc((s && s.status) || "")}</td></tr>`;
  });
  const occ = findEntity(states, "occupancy_forecast");
  if (occ) rows.push(`<tr><td>occupancy</td><td class="dv-mono">${esc((occ.attributes && occ.attributes.source) || "")}</td><td>${esc(occ.state)}</td></tr>`);
  if (a.electricity_rate != null) rows.push(`<tr><td>rate</td><td class="dv-mono">$${dvFmt(a.electricity_rate, 4)} / kWh</td><td></td></tr>`);
  if (!rows.length) return dvEmpty("No source data.");
  return `<div class="dv-scroll"><table class="dv-table"><tbody>${rows.join("")}</tbody></table></div>`;
}

function renderNowTab(states, hass, retro, dash, L) {
  const tl = dvTimeline(states, retro, dash, L.full);
  const src = findEntity(states, "outdoor_temp_source");
  const health = src && src.attributes && src.attributes.source_health_total ? `${src.attributes.source_health_healthy} of ${src.attributes.source_health_total} ok` : "";
  const legend = dvLegend([
    ["k-indoor", "Indoor"],
    ["k-indoor k-dash", "Indoor forecast"],
    ...(dvP90(dash) ? [["k-fan", "90% of past errors"]] : []),
    ["k-sp", "Setpoint"],
    ["k-sp k-dash", "Planned setpoint"],
    ["k-band", "Promised band"],
    ["k-out", "Outdoor"],
    ["k-run", "Ran"],
    ["k-run k-planned", "Planned"],
  ]);
  return {
    html: `
      ${dvStatStrip(states, hass)}
      ${dvCard("Last 24 h and next 24 h", `<span class="dv-readout" data-default=""></span>`, legend + tl.html, "dv-tl-card")}
      <div class="dv-row dv-row-64">
        ${dvCard("Plan", "", dvPlanTable(states))}
        ${dvCard("Inputs", esc(health), dvInputs(states))}
      </div>`,
    model: tl.model,
  };
}

// ── Model ────────────────────────────────────────────────────────────

const DV_MODES = [
  ["resist", "Drift", "drift"],
  ["heat_1", "Heating", "heat"],
  ["cool_1", "Cooling", "cool"],
];

function dvBins(mode) {
  const bins = (mode && mode.bins) || {};
  return Object.keys(bins)
    .map((k) => ({ t: Number(k), d: dvNum(bins[k].mean_delta), n: dvNum(bins[k].count) || 0 }))
    .filter((b) => Number.isFinite(b.t) && b.d != null)
    .sort((p, q) => p.t - q.t);
}

function dvCurves(chart, detail, outdoorNow, W) {
  const series = DV_MODES.map(([key, label, cls]) => ({ key, label, cls, bins: dvBins(chart[key]), trend: chart[key] && chart[key].trendline }));
  const shown = series.flatMap((s) => s.bins.filter((b) => b.n >= 5));
  if (shown.length < 3) return dvEmpty("Not enough observations yet.");

  const ML = 64;
  const MR = 44;
  const tLo = Math.floor((Math.min(...shown.map((b) => b.t)) - 3) / 5) * 5;
  const tHi = Math.ceil((Math.max(...shown.map((b) => b.t)) + 1) / 5) * 5;
  const dLo = Math.floor(Math.min(...shown.map((b) => b.d)) - 0.5);
  const dHi = Math.ceil(Math.max(...shown.map((b) => b.d)) + 0.5);
  const top = 8;
  const h = 300;
  const x = dvScale(tLo, tHi, ML, W - MR);
  const y = dvScale(dLo, dHi, top + h, top);
  const parts = [];

  for (const v of dvTicks(dLo, dHi, 8)) {
    parts.push(`<line x1="${ML}" x2="${W - MR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${v === 0 ? "dv-axis" : "dv-grid"}"/>`);
    parts.push(`<text x="${ML - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${v > 0 ? "+" : ""}${v}</text>`);
  }
  parts.push(`<text x="0" y="${top + 8}">°/h</text>`);
  if (outdoorNow != null && outdoorNow > tLo && outdoorNow < tHi) {
    parts.push(`<line x1="${x(outdoorNow).toFixed(1)}" x2="${x(outdoorNow).toFixed(1)}" y1="${top}" y2="${top + h}" class="dv-now"/>`);
    parts.push(`<text x="${(x(outdoorNow) + 4).toFixed(1)}" y="${top + h - 4}" class="dv-strong">now ${outdoorNow.toFixed(0)}°</text>`);
  }
  for (const s of series) {
    const vis = s.bins.filter((b) => b.n >= 5);
    if (!vis.length) continue;
    if (s.trend && dvNum(s.trend.slope) != null) {
      const a = vis[0].t;
      const b = vis[vis.length - 1].t;
      const f = (t) => s.trend.slope * t + s.trend.intercept;
      parts.push(`<line x1="${x(a).toFixed(1)}" x2="${x(b).toFixed(1)}" y1="${y(f(a)).toFixed(1)}" y2="${y(f(b)).toFixed(1)}" class="dv-line dv-thin dv-${s.cls}"/>`);
    }
    for (const b of vis) {
      parts.push(
        `<circle cx="${x(b.t).toFixed(1)}" cy="${y(b.d).toFixed(1)}" r="3.2" class="dv-dot dv-${s.cls}-fill${b.n < 20 ? " dv-faint" : ""}"><title>${s.label} at ${b.t}°: ${dvSigned(b.d)}°/h, n=${b.n}</title></circle>`
      );
    }
  }

  // Coverage strip on the same x axis
  const rows = DV_MODES.concat([["auxiliary_heat_1", "Aux", "aux"]]);
  const cTop = top + h + 26;
  const rowH = 14;
  const cellW = (x(tLo + 1) - x(tLo)) * 0.86;
  rows.forEach(([key, label], r) => {
    const yy = cTop + r * (rowH + 4);
    parts.push(`<rect x="${ML}" y="${yy}" width="${W - MR - ML}" height="${rowH}" class="dv-q0"/>`);
    parts.push(`<text x="0" y="${yy + 11}">${label}</text>`);
    const conf = detail[key] ? dvNum(detail[key].confidence) : null;
    parts.push(`<text x="${W}" y="${yy + 11}" text-anchor="end" class="dv-strong">${conf == null ? "" : conf.toFixed(0) + "%"}</text>`);
    for (const b of dvBins(chart[key])) {
      if (b.n < 1 || b.t < tLo || b.t > tHi) continue;
      const q = b.n >= 200 ? 4 : b.n >= 50 ? 3 : b.n >= 10 ? 2 : 1;
      parts.push(`<rect x="${(x(b.t) - cellW / 2).toFixed(1)}" y="${yy}" width="${cellW.toFixed(1)}" height="${rowH}" class="dv-q${q}"><title>${label} at ${b.t}°: n=${b.n}</title></rect>`);
    }
  });
  const axisY = cTop + rows.length * (rowH + 4) + 12;
  for (const v of dvTicks(tLo, tHi, 8)) {
    parts.push(`<line x1="${x(v).toFixed(1)}" x2="${x(v).toFixed(1)}" y1="${top}" y2="${top + h}" class="dv-grid"/>`);
    parts.push(`<text x="${x(v).toFixed(1)}" y="${top + h + 14}" text-anchor="middle">${v}°</text>`);
  }
  const H = axisY;
  return (
    dvLegend([
      ["k-drift k-dot", "Drift, compressor off"],
      ["k-heat k-dot", "Heating"],
      ["k-cool k-dot", "Cooling"],
      ["k-faint k-dot", "5 to 19 obs"],
    ]) +
    `<svg class="dv-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Indoor change per hour by outdoor temperature, with observation counts per bin">${parts.join("")}</svg>` +
    `<div class="dv-legend dv-legend-q"><span>obs per 1° bin</span><span><i class="dv-key dv-q1"></i>1 to 9</span><span><i class="dv-key dv-q2"></i>10 to 49</span><span><i class="dv-key dv-q3"></i>50 to 199</span><span><i class="dv-key dv-q4"></i>200+</span></div>`
  );
}

function dvParams(states, chart, detail) {
  const tc = findEntity(states, "thermal_time_constant");
  const heat = findEntity(states, "effective_heating_rate");
  const cool = findEntity(states, "effective_cooling_rate");
  const aux = findEntity(states, "aux_heat_threshold");
  const tca = (tc && tc.attributes) || {};
  const rows = [];
  if (hasValue(tc)) rows.push(["Time constant", `${fmt(tc, 1)} h`]);
  if (tca.envelope_loss_btu_per_hour_f != null) rows.push(["Envelope loss", `${dvFmt(tca.envelope_loss_btu_per_hour_f, 1)} BTU/h·°`]);
  if (hasValue(heat)) rows.push(["Heating rate now", `${dvSigned(heat.state)} °/h`]);
  if (hasValue(cool)) rows.push(["Cooling rate now", `${dvSigned(cool.state)} °/h`]);
  if (hasValue(aux)) rows.push(["Aux threshold", `${fmt(aux, 1)}°${aux.attributes && aux.attributes.event_count != null ? `, ${aux.attributes.event_count} events` : ""}`]);
  for (const [key, label] of DV_MODES) {
    const t = chart[key] && chart[key].trendline;
    if (t && dvNum(t.slope) != null) rows.push([`${label} fit`, `${Number(t.slope).toFixed(4)}·T ${t.intercept < 0 ? "−" : "+"} ${Math.abs(t.intercept).toFixed(2)}`]);
  }
  for (const [key, label] of DV_MODES.concat([["auxiliary_heat_1", "Aux"]])) {
    if (detail[key]) rows.push([`${label} obs`, Number(detail[key].observations || 0).toLocaleString()]);
  }
  if (!rows.length) return dvEmpty("No parameters yet.");
  return `<table class="dv-table dv-kv"><tbody>${rows.map(([k, v]) => `<tr><td>${esc(k)}</td><td class="dv-mono dv-num">${esc(v)}</td></tr>`).join("")}</tbody></table>`;
}

function dvLeadChart(ver, W) {
  const leads = (ver && ver.leads) || [];
  if (!leads.length) return dvEmpty(`Scoring starts with the next plan. ${(ver && ver.samples) || 0} samples so far.`);
  const ML = 30;
  const MR = 118;
  const top = 8;
  const h = 170;
  const xMax = Math.max(...leads.map((l) => l.lead));
  const yMax = Math.max(0.5, ...leads.map((l) => Math.max(l.mae, l.no_change_mae))) * 1.15;
  const x = dvScale(0, xMax, ML, W - MR);
  const y = dvScale(0, yMax, top + h, top);
  const parts = [];
  for (const v of dvTicks(0, yMax, 4)) {
    parts.push(`<line x1="${ML}" x2="${W - MR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${v === 0 ? "dv-axis" : "dv-grid"}"/>`);
    parts.push(`<text x="${ML - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${v}</text>`);
  }
  const line = (key, cls, fill) => {
    parts.push(`<path d="${dvPath(leads.map((l) => [x(l.lead), y(l[key])]))}" class="dv-line ${cls}"/>`);
    for (const l of leads) {
      parts.push(`<circle cx="${x(l.lead).toFixed(1)}" cy="${y(l[key]).toFixed(1)}" r="4" class="dv-dot ${fill}"><title>${l.lead} h: ${l[key].toFixed(2)}°, n=${l.n}</title></circle>`);
    }
  };
  line("no_change_mae", "dv-drift dv-dash", "dv-drift-fill");
  line("mae", "dv-indoor", "dv-indoor-fill");
  const last = leads[leads.length - 1];
  let ya = y(last.mae);
  let yb = y(last.no_change_mae);
  if (Math.abs(ya - yb) < 14) {
    const mid = (ya + yb) / 2;
    const below = ya >= yb;
    ya = mid + (below ? 7 : -7);
    yb = mid + (below ? -7 : 7);
  }
  parts.push(`<text x="${W - MR + 10}" y="${(ya + 4).toFixed(1)}" class="dv-strong">Forecast ${last.mae.toFixed(2)}</text>`);
  parts.push(`<text x="${W - MR + 10}" y="${(yb + 4).toFixed(1)}">No change ${last.no_change_mae.toFixed(2)}</text>`);
  for (const l of leads) parts.push(`<text x="${x(l.lead).toFixed(1)}" y="${top + h + 14}" text-anchor="middle">${l.lead} h</text>`);
  const rows = leads
    .map((l) => {
      const skill = l.no_change_mae > 0 ? Math.round((1 - l.mae / l.no_change_mae) * 100) : null;
      return `<tr><td>${l.lead} h</td><td>${l.n}</td><td>${l.mae.toFixed(2)}</td><td>${dvSigned(l.bias)}</td><td>${l.p90.toFixed(2)}</td><td>${l.no_change_mae.toFixed(2)}</td><td>${skill == null ? "–" : skill + "%"}</td></tr>`;
    })
    .join("");
  return (
    `<svg class="dv-svg" width="${W}" height="${top + h + 22}" viewBox="0 0 ${W} ${top + h + 22}" role="img" aria-label="Forecast error by lead time against assuming no change">${parts.join("")}</svg>` +
    `<div class="dv-scroll"><table class="dv-table dv-mono dv-right"><thead><tr><th>Lead</th><th>n</th><th>MAE</th><th>Bias</th><th>P90</th><th>No change</th><th>Skill</th></tr></thead><tbody>${rows}</tbody></table></div>`
  );
}

/** Diverging bars: measured minus predicted, per category. */
function dvBiasBars(rows, labelOf, W, tickEvery) {
  if (!rows.length) return dvEmpty("No scored forecasts yet.");
  const ML = 30;
  const MR = 6;
  const top = 8;
  const h = 150;
  const span = Math.max(0.5, ...rows.map((r) => Math.abs(r.bias))) * 1.15;
  const x = dvScale(0, rows.length, ML, W - MR);
  const y = dvScale(-span, span, top + h, top);
  const parts = [];
  for (const v of dvTicks(-span, span, 4)) {
    parts.push(`<line x1="${ML}" x2="${W - MR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${v === 0 ? "dv-axis" : "dv-grid"}"/>`);
    parts.push(`<text x="${ML - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${v > 0 ? "+" : ""}${v}</text>`);
  }
  const bw = Math.max(2, (x(1) - x(0)) * 0.72);
  rows.forEach((r, i) => {
    const cx = x(i + 0.5);
    const y0 = y(0);
    const yv = y(r.bias);
    parts.push(
      `<rect x="${(cx - bw / 2).toFixed(1)}" y="${Math.min(y0, yv).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1, Math.abs(yv - y0)).toFixed(1)}" rx="2" class="${r.bias < 0 ? "dv-indoor-fill" : "dv-aux-fill"}"><title>${labelOf(r)}: bias ${dvSigned(r.bias)}°, MAE ${r.mae.toFixed(2)}°, n=${r.n}</title></rect>`
    );
    if (i % tickEvery === 0) parts.push(`<text x="${cx.toFixed(1)}" y="${top + h + 14}" text-anchor="middle">${labelOf(r)}</text>`);
  });
  return `<svg class="dv-svg" width="${W}" height="${top + h + 22}" viewBox="0 0 ${W} ${top + h + 22}" role="img" aria-label="Forecast bias by category">${parts.join("")}</svg>`;
}

function renderModelTab(states, dash, L) {
  const profiler = findEntity(states, "profiler_status");
  const pa = (profiler && profiler.attributes) || {};
  const chart = pa.chart_data || {};
  const detail = pa.mode_detail || {};
  const outdoor = findEntity(states, "outdoor_temp_source");
  const ver = (dash && dash.verification) || null;
  const meta = ver && ver.samples ? `${ver.samples} samples, ${ver.span_days} d` : "";
  const biasLegend = dvLegend([
    ["k-indoor k-box", "Colder than forecast"],
    ["k-aux k-box", "Warmer"],
  ]);
  const byHour = (ver && ver.by_hour) || [];
  const byOut = (ver && ver.by_outdoor) || [];
  return `
    <div class="dv-row dv-row-73">
      ${dvCard("Response by outdoor temperature", pa.observations != null ? `${Number(pa.observations).toLocaleString()} obs` : "", dvCurves(chart, detail, hasValue(outdoor) ? Number(outdoor.state) : null, L.wide73))}
      ${dvCard("Parameters", "", dvParams(states, chart, detail))}
    </div>
    <div class="dv-row dv-row-3">
      ${dvCard("Forecast error by lead", esc(meta), dvLeadChart(ver, L.third))}
      ${dvCard("1 h bias by hour of day", "", byHour.length ? biasLegend + dvBiasBars(byHour, (r) => dvPad(r.hour), L.third, 3) : dvEmpty("No scored forecasts yet."))}
      ${dvCard("1 h bias by outdoor temperature", "", byOut.length ? biasLegend + dvBiasBars(byOut, (r) => `${r.bin}°`, L.third, 1) : dvEmpty("No scored forecasts yet."))}
    </div>`;
}

// ── Energy ───────────────────────────────────────────────────────────

function dvDayLabel(iso) {
  const d = new Date(iso + "T12:00:00");
  return `${DV_DOW[d.getDay()]} ${d.getDate()}`;
}

function dvScatter(days, fit, auxHours, cooling, W) {
  const pts = days
    .filter((d) => d.out != null)
    .map((d) => ({ d: d.d, x: d.out, y: cooling ? d.cool : d.heat + d.aux, aux: !cooling && d.aux >= auxHours, heat: d.heat, auxH: d.aux }));
  if (pts.length < 3) return dvEmpty(`${pts.length} full days logged. Needs 3.`);
  const ML = 34;
  const MR = 10;
  const top = 20;
  const h = 270;
  const xLo = Math.floor((Math.min(...pts.map((p) => p.x)) - 2) / 10) * 10;
  const xHi = Math.ceil((Math.max(...pts.map((p) => p.x)) + 2) / 10) * 10;
  const yHi = Math.max(4, Math.ceil(Math.max(...pts.map((p) => p.y)) * 1.1));
  const x = dvScale(xLo, xHi, ML, W - MR);
  const y = dvScale(0, yHi, top + h, top);
  const parts = [];
  for (const v of dvTicks(0, yHi, 5)) {
    parts.push(`<line x1="${ML}" x2="${W - MR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${v === 0 ? "dv-axis" : "dv-grid"}"/>`);
    parts.push(`<text x="${ML - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${v}</text>`);
  }
  for (const v of dvTicks(xLo, xHi, 6)) parts.push(`<text x="${x(v).toFixed(1)}" y="${top + h + 14}" text-anchor="middle">${v}°</text>`);
  parts.push(`<text x="0" y="10">h/day</text>`);
  if (fit && !cooling) {
    const f = (t) => Math.max(0, fit.intercept + fit.slope * Math.max(0, fit.balance - t));
    const ts = [];
    for (let t = xLo; t <= xHi; t += 0.5) if (f(t) <= yHi) ts.push(t);
    const cy = (v) => Math.max(top, Math.min(top + h, y(v)));
    const up = ts.map((t) => [x(t), cy(f(t) + fit.sd)]);
    const dn = ts.map((t) => [x(t), cy(Math.max(0, f(t) - fit.sd))]).reverse();
    parts.push(`<path d="${dvPath(up.concat(dn))}Z" class="dv-band"/>`);
    parts.push(`<path d="${dvPath(ts.map((t) => [x(t), cy(f(t))]))}" class="dv-line dv-sp dv-thin"/>`);
  }
  for (const p of pts) {
    const title = `${dvDayLabel(p.d)} ${p.d.slice(0, 7)}: ${p.x.toFixed(0)}°, ${cooling ? p.y.toFixed(1) + " h" : p.heat.toFixed(1) + " h" + (p.auxH > 0.01 ? " + " + p.auxH.toFixed(1) + " h aux" : "")}`;
    parts.push(`<circle cx="${x(p.x).toFixed(1)}" cy="${y(p.y).toFixed(1)}" r="${p.aux ? 4.5 : 4}" class="dv-dot ${cooling ? "dv-cool-fill" : p.aux ? "dv-aux-fill dv-ring" : "dv-heat-fill"}"><title>${title}</title></circle>`);
  }
  const stats = fit && !cooling
    ? `<div class="dv-statline"><span>h = ${fit.slope.toFixed(2)} × (${fit.balance.toFixed(0)} − T) ${fit.intercept < 0 ? "−" : "+"} ${Math.abs(fit.intercept).toFixed(1)}</span><span>R² ${fit.r2.toFixed(2)}</span><span>SD ${fit.sd.toFixed(1)} h</span><span>n ${fit.n}</span></div>`
    : `<div class="dv-statline"><span>n ${pts.length}</span>${cooling ? "" : "<span>fit needs 10 days</span>"}</div>`;
  const legend = cooling
    ? dvLegend([["k-cool k-dot", "Cooling day"]])
    : dvLegend([
        ["k-heat k-dot", "Compressor only"],
        ["k-aux k-dot k-ring", "Aux ran"],
        ["k-sp", "Fit"],
        ["k-band", "±1 SD"],
      ]);
  return legend + `<svg class="dv-svg" width="${W}" height="${top + h + 22}" viewBox="0 0 ${W} ${top + h + 22}" role="img" aria-label="Daily runtime hours against mean outdoor temperature">${parts.join("")}</svg>` + stats;
}

function dvEnergyScatter(energy, W) {
  const pts = ((energy && energy.days) || []).filter((d) => d.out != null && d.kwh != null);
  if (pts.length < 3) return dvEmpty(`${pts.length} metered days. Baseline ${(energy && energy.baseline_days) || 0}, needs ${((energy && energy.baseline_days) || 0) + ((energy && energy.days_until_fit) || 0)}.`);
  const ML = 34;
  const MR = 10;
  const top = 20;
  const h = 210;
  const xLo = Math.floor((Math.min(...pts.map((p) => p.out)) - 2) / 10) * 10;
  const xHi = Math.ceil((Math.max(...pts.map((p) => p.out)) + 2) / 10) * 10;
  const yHi = Math.max(4, Math.ceil(Math.max(...pts.map((p) => p.kwh)) * 1.1));
  const x = dvScale(xLo, xHi, ML, W - MR);
  const y = dvScale(0, yHi, top + h, top);
  const parts = [];
  for (const v of dvTicks(0, yHi, 4)) {
    parts.push(`<line x1="${ML}" x2="${W - MR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${v === 0 ? "dv-axis" : "dv-grid"}"/>`);
    parts.push(`<text x="${ML - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${v}</text>`);
  }
  for (const v of dvTicks(xLo, xHi, 6)) parts.push(`<text x="${x(v).toFixed(1)}" y="${top + h + 14}" text-anchor="middle">${v}°</text>`);
  parts.push(`<text x="0" y="10">kWh</text>`);
  for (const p of pts) {
    const cls = p.label === "optimized" ? "dv-indoor-fill" : p.label === "baseline" ? "dv-drift-fill" : "dv-drift-fill dv-faint";
    parts.push(`<circle cx="${x(p.out).toFixed(1)}" cy="${y(p.kwh).toFixed(1)}" r="4" class="dv-dot ${cls}"><title>${p.d}: ${p.out.toFixed(0)}°, ${p.kwh.toFixed(1)} kWh, ${esc(p.label)}</title></circle>`);
  }
  return (
    dvLegend([
      ["k-drift k-dot", "Baseline day"],
      ["k-indoor k-dot", "Optimized day"],
      ["k-faint k-dot", "Mixed"],
    ]) +
    `<svg class="dv-svg" width="${W}" height="${top + h + 22}" viewBox="0 0 ${W} ${top + h + 22}" role="img" aria-label="Daily metered HVAC energy against mean outdoor temperature">${parts.join("")}</svg>` +
    `<div class="dv-statline"><span>${dvFmt(energy.kwh_per_hdd, 2)} kWh/HDD</span><span>${dvFmt(energy.kwh_per_cdd, 2)} kWh/CDD</span><span>${esc(energy.source)}</span></div>`
  );
}

function dvMonths(days, W) {
  const by = {};
  for (const d of days) {
    const k = d.d.slice(0, 7);
    by[k] = by[k] || { heat: 0, aux: 0, cool: 0, n: 0 };
    by[k].heat += d.heat;
    by[k].aux += d.aux;
    by[k].cool += d.cool;
    by[k].n += 1;
  }
  const keys = Object.keys(by).sort().slice(-13);
  if (!keys.length) return dvEmpty("No full days logged yet.");
  const ML = 34;
  const MR = 6;
  const top = 18;
  const h = 220;
  const yHi = Math.max(10, ...keys.map((k) => by[k].heat + by[k].aux + by[k].cool)) * 1.08;
  const x = dvScale(0, Math.max(keys.length, 6), ML, W - MR);
  const y = dvScale(0, yHi, top + h, top);
  const parts = [];
  for (const v of dvTicks(0, yHi, 4)) {
    parts.push(`<line x1="${ML}" x2="${W - MR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${v === 0 ? "dv-axis" : "dv-grid"}"/>`);
    parts.push(`<text x="${ML - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${v}</text>`);
  }
  const bw = Math.min(40, (x(1) - x(0)) * 0.62);
  keys.forEach((k, i) => {
    const m = by[k];
    const cx = x(i + 0.5);
    let base = 0;
    for (const [key, cls, label] of [["heat", "dv-heat-fill", "heating"], ["aux", "dv-aux-fill", "aux"], ["cool", "dv-cool-fill", "cooling"]]) {
      const v = m[key];
      if (v < 0.5) continue;
      const y0 = y(base) - (base > 0 ? 2 : 0);
      const y1 = y(base + v);
      parts.push(`<rect x="${(cx - bw / 2).toFixed(1)}" y="${y1.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1, y0 - y1).toFixed(1)}" class="${cls}"><title>${k}: ${v.toFixed(0)} h ${label}, ${m.n} days</title></rect>`);
      base += v;
    }
    parts.push(`<text x="${cx.toFixed(1)}" y="${(y(base) - 5).toFixed(1)}" text-anchor="middle">${base.toFixed(0)}</text>`);
    const mo = Number(k.slice(5, 7)) - 1;
    parts.push(`<text x="${cx.toFixed(1)}" y="${top + h + 14}" text-anchor="middle"${mo === 0 ? ' class="dv-strong"' : ""}>${DV_MONTH[mo]}${mo === 0 || i === 0 ? " " + k.slice(2, 4) : ""}</text>`);
  });
  return (
    dvLegend([
      ["k-heat k-box", "Heating"],
      ["k-aux k-box", "Aux"],
      ["k-cool k-box", "Cooling"],
    ]) + `<svg class="dv-svg" width="${W}" height="${top + h + 22}" viewBox="0 0 ${W} ${top + h + 22}" role="img" aria-label="Runtime hours by month">${parts.join("")}</svg>`
  );
}

function dvHeatmap(hours, cooling, W) {
  const idx = cooling ? 4 : 3;
  const by = {};
  for (const hr of hours) {
    const day = hr[0].slice(0, 10);
    (by[day] = by[day] || {})[Number(hr[0].slice(11, 13))] = hr;
  }
  const dates = Object.keys(by).sort().slice(-21);
  if (!dates.length) return dvEmpty("No hours logged yet.");
  const ML = 52;
  const MR = 86;
  const top = 4;
  const rowH = 18;
  const x = dvScale(0, 24, ML, W - MR);
  const cw = x(1) - x(0);
  const parts = [];
  dates.forEach((date, r) => {
    const yy = top + r * rowH;
    parts.push(`<text x="0" y="${yy + 12}">${dvDayLabel(date)}</text>`);
    let total = 0;
    const outs = [];
    for (let hcol = 0; hcol < 24; hcol++) {
      const rec = by[date][hcol];
      if (!rec) continue;
      const v = rec[idx] || 0;
      total += v;
      if (rec[2] != null) outs.push(rec[2]);
      const q = v >= 46 ? 4 : v >= 31 ? 3 : v >= 16 ? 2 : v >= 1 ? 1 : 0;
      parts.push(`<rect x="${(x(hcol) + 1).toFixed(1)}" y="${yy + 1}" width="${Math.max(1, cw - 2).toFixed(1)}" height="${rowH - 2}" class="dv-q${q}"><title>${dvDayLabel(date)} ${dvPad(hcol)}:00: ${v.toFixed(0)} min${rec[2] != null ? ", " + rec[2].toFixed(0) + "° out" : ""}</title></rect>`);
    }
    const mean = outs.length ? outs.reduce((p, q) => p + q, 0) / outs.length : null;
    parts.push(`<text x="${W}" y="${yy + 12}" text-anchor="end">${(total / 60).toFixed(1)} h${mean == null ? "" : " · " + mean.toFixed(0) + "°"}</text>`);
  });
  const axisY = top + dates.length * rowH + 14;
  for (const hcol of [0, 6, 12, 18, 23]) parts.push(`<text x="${x(hcol + 0.5).toFixed(1)}" y="${axisY}" text-anchor="middle">${dvPad(hcol)}</text>`);
  return (
    `<div class="dv-legend dv-legend-q"><span>min per hour</span><span><i class="dv-key dv-q0"></i>0</span><span><i class="dv-key dv-q1"></i>1 to 15</span><span><i class="dv-key dv-q2"></i>16 to 30</span><span><i class="dv-key dv-q3"></i>31 to 45</span><span><i class="dv-key dv-q4"></i>46 to 60</span></div>` +
    `<svg class="dv-svg" width="${W}" height="${axisY + 6}" viewBox="0 0 ${W} ${axisY + 6}" role="img" aria-label="${cooling ? "Cooling" : "Heating"} minutes per hour by day">${parts.join("")}</svg>`
  );
}

function dvRecoveries(dash) {
  const done = ((dash && dash.recoveries) || []).slice(-14);
  const planned = ((dash && dash.plan && dash.plan.recovery) || []).filter((p) => p.start && p.deadline && p.start > (dash.generated || ""));
  if (!done.length && !planned.length) return dvEmpty("No morning recoveries logged yet.");
  const longest = Math.max(60, ...done.map((r) => r.minutes || 0));
  const hm = (s) => (s ? s.slice(11, 16) : "–");
  const mins = (a, b) => {
    const [ah, am] = a.slice(11, 16).split(":").map(Number);
    const [bh, bm] = b.slice(11, 16).split(":").map(Number);
    return ah * 60 + am - (bh * 60 + bm);
  };
  const rows = done.map((r) => {
    let late = "";
    if (r.deadline && r.arrived) {
      const m = mins(r.arrived, r.deadline);
      late = m > 0 ? `${m} late` : m < 0 ? `${-m} early` : "on time";
    }
    const bar = r.minutes == null ? "not reached" : r.minutes === 0 ? "already warm" : `<span class="dv-inbar"><i style="width:${Math.min(100, (r.minutes / longest) * 100).toFixed(1)}%"></i></span>${r.minutes}`;
    return `<tr><td>${dvDayLabel(r.start.slice(0, 10))}</td><td>${hm(r.start)}</td><td>${dvFmt(r.out, 0)}°</td><td>${dvFmt(r.in, 1)}°</td><td>${hm(r.arrived)}</td><td class="dv-barcell">${bar}</td><td>${r.aux > 0.5 ? r.aux.toFixed(0) : ""}</td><td>${late}</td></tr>`;
  });
  for (const p of planned) {
    rows.push(`<tr class="dv-planrow"><td>${dvDayLabel(p.start.slice(0, 10))}</td><td>${hm(p.start)}</td><td></td><td></td><td>${hm(p.deadline)} due</td><td>planned</td><td></td><td></td></tr>`);
  }
  return `<div class="dv-scroll"><table class="dv-table dv-mono"><thead><tr><th>Day</th><th>Step</th><th>Out</th><th>In</th><th>Warm</th><th>Minutes</th><th>Aux</th><th>Deadline</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}

function dvSavingsStrip(states, dash) {
  const measured = findEntity(states, "measured_savings_30d");
  const trial = findEntity(states, "ab_trial");
  const total = findEntity(states, "savings_kwh_cumulative");
  const src = findEntity(states, "outdoor_temp_source");
  const e = (dash && dash.energy) || {};
  const ma = (measured && measured.attributes) || {};
  const cell = (label, value, sub) =>
    `<div class="dv-stat"><div class="dv-stat-label">${label}</div><div class="dv-stat-value dv-small">${value}</div><div class="dv-stat-sub">${sub || "&nbsp;"}</div></div>`;
  const reset = total && total.attributes && total.attributes.last_reset ? new Date(total.attributes.last_reset) : null;
  const need = (e.baseline_days || 0) + (e.days_until_fit || 0);
  return `<section class="card dv-stats">
    ${cell("Measured, 30 d", hasValue(measured) ? `${fmt(measured, 1)}<small> kWh</small>` : "–", ma.saved_pct != null ? `${dvFmt(ma.saved_pct, 1)}%, ${ma.days_compared} days` : `baseline ${e.baseline_days || 0} of ${need} days`)}
    ${cell("A/B trial", trial ? esc(trial.state) : "–", trial && trial.attributes && trial.attributes.difference_pct != null ? `${dvSigned(trial.attributes.difference_pct, 1)}%` : "")}
    ${cell("Ledger", hasValue(total) ? `${fmt(total, 1)}<small> kWh</small>` : "–", reset ? `since ${DV_MONTH[reset.getMonth()]} ${reset.getDate()}` : "")}
    ${cell("Rate", src && src.attributes && src.attributes.electricity_rate != null ? `$${dvFmt(src.attributes.electricity_rate, 4)}<small> / kWh</small>` : "–", "")}
  </section>`;
}

function renderEnergyTab(states, dash, L) {
  if (!dash) return dvEmpty("Loading archive…");
  const days = dash.days || [];
  const recent = days.slice(-30);
  const cooling = recent.reduce((s, d) => s + d.cool, 0) > recent.reduce((s, d) => s + d.heat + d.aux, 0);
  const auxDays = days.filter((d) => d.aux >= (dash.aux_day_hours || 0.25)).length;
  return `
    <div class="dv-row dv-row-64">
      ${dvCard(`${cooling ? "Cooling" : "Heating"} hours vs outdoor`, `${days.length} days${cooling ? "" : `, ${auxDays} with aux`}`, dvScatter(days, dash.fit, dash.aux_day_hours || 0.25, cooling, L.wide64))}
      ${dvCard("Runtime by month", "hours", dvMonths(days, L.narrow64))}
    </div>
    <div class="dv-row dv-row-2">
      ${dvCard(`${cooling ? "Cooling" : "Heating"} minutes by hour`, "", dvHeatmap(dash.hours || [], cooling, L.half))}
      ${dvCard("Morning recoveries", "", dvRecoveries(dash))}
    </div>
    <div class="dv-row dv-row-2">
      ${dvCard("Metered kWh vs outdoor", "", dvEnergyScatter(dash.energy, L.half))}
      ${dvSavingsStrip(states, dash)}
    </div>`;
}

// ── Twin ─────────────────────────────────────────────────────────────
// Twin-against-twin figures (docs/internal/TWIN_STATS_SPEC.md section 8).
// Modeled figures say "modeled" once per card, in the meta slot.

const TWIN_TRUSTED = ["good", "fair"];

function dvTwinReason(tw) {
  if (!tw) return "No twin data yet.";
  const fitNeed = tw.model && tw.model.days_needed;
  if (fitNeed > 0 && (tw.days || 0) === 0) return `Twin learning this house, about ${fitNeed} days to go`;
  if (tw.grade === "warming_up") return `${tw.days_needed || 7} days needed, ${tw.days || 0} so far`;
  if (tw.grade === "idle") return "Too little runtime to judge";
  if (tw.grade === "poor") {
    const g = dvNum(tw.gain);
    const c = dvNum(tw.comp_gain);
    const a = dvNum(tw.aux_gain);
    const off = (v) => (v == null ? 0 : Math.abs(v - 1));
    if (off(c) > off(g) && off(c) >= off(a)) return `Twin compressor runs ${Math.round(off(c) * 100)}% ${c > 1 ? "long" : "short"}`;
    if (off(a) > off(g)) return `Twin aux runs ${Math.round(off(a) * 100)}% ${a > 1 ? "long" : "short"}`;
    if (g != null) return `Twin runs ${Math.round(off(g) * 100)}% ${g > 1 ? "long" : "short"}`;
    return "Twin does not track this house yet";
  }
  return "";
}

function dvMoney(v) {
  const n = dvNum(v);
  return n == null ? "–" : (n < 0 ? "−$" : "$") + Math.abs(n).toFixed(2);
}

function dvTwinStrip(tw) {
  const trusted = TWIN_TRUSTED.includes(tw.grade);
  const reason = dvTwinReason(tw);
  const cell = (label, value, sub) =>
    `<div class="dv-stat"><div class="dv-stat-label">${label}</div><div class="dv-stat-value dv-small">${value}</div><div class="dv-stat-sub">${sub || "&nbsp;"}</div></div>`;
  const ref = tw.reference || {};
  const band = (b) => (b == null ? "" : `about ±${Math.round(b * 100)}%`);
  const refCell = trusted && ref.saved_kwh != null
    ? cell(`vs holding ${dvFmt(ref.heat_f, 0)}, 30 d`, `${dvFmt(ref.saved_kwh, 0)}<small> kWh</small>`, [dvMoney(ref.saved_cost), band(ref.band), "modeled"].filter((x) => x && x !== "–").join(", "))
    : cell(`vs holding ${dvFmt(ref.heat_f ?? 72, 0)}, 30 d`, "–", esc(reason));
  const eff = tw.effect;
  const effLabel = eff && eff.key === "schedule" ? "vs your schedule, 30 d" : "Optimizer plan, 30 d";
  const effCell = !eff
    ? cell(effLabel, "–", "No plan days yet")
    : trusted && eff.kwh != null
      ? cell(effLabel, `${dvSigned(eff.kwh, 0)}<small> kWh</small>`, [dvMoney(eff.cost), band(eff.band)].filter((x) => x && x !== "–").join(", "))
      : cell(effLabel, "–", esc(reason));
  const deg = tw.degree || {};
  const warmer = deg.season === "cool" ? "1 degree cooler today" : "1 degree warmer today";
  const degCell = deg.today_kwh != null
    ? cell(warmer, deg.today_cost != null ? `${dvMoney(deg.today_cost)}<small> / day</small>` : `${dvFmt(deg.today_kwh, 1)}<small> kWh</small>`, `${dvFmt(deg.today_kwh, 1)} kWh, ${esc(deg.source || "")}`)
    : cell(warmer, "–", "Needs 10 days in season");
  const g = dvNum(tw.gain);
  const accCell = cell("Twin accuracy", g == null ? "–" : `${dvSigned((g - 1) * 100, 0)}<small>%</small>`, g == null ? esc(reason) : `${esc(tw.grade.replace("_", " "))}, ${tw.days} days`);
  return `<section class="card dv-stats">${refCell}${effCell}${degCell}${accCell}</section>`;
}

function dvTwinDaily(daily, W) {
  const rows = (daily || []).slice(-60);
  if (rows.length < 2) return dvEmpty(`${rows.length} days logged. Needs 2.`);
  const ML = 34;
  const MR = 6;
  const top = 18;
  const h = 200;
  const yHi = Math.max(5, ...rows.map((r) => Math.max(r.actual || 0, r.replay || 0))) * 1.1;
  const x = dvScale(0, rows.length, ML, W - MR);
  const y = dvScale(0, yHi, top + h, top);
  const parts = [];
  for (const v of dvTicks(0, yHi, 4)) {
    parts.push(`<line x1="${ML}" x2="${W - MR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${v === 0 ? "dv-axis" : "dv-grid"}"/>`);
    parts.push(`<text x="${ML - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${v}</text>`);
  }
  parts.push(`<text x="0" y="10">kWh</text>`);
  const bw = Math.max(2, (x(1) - x(0)) * 0.7);
  const line = [];
  rows.forEach((r, i) => {
    const cx = x(i + 0.5);
    const yv = y(r.actual || 0);
    const cls = r.warmup || !r.valid ? "dv-drift-fill dv-faint" : "dv-drift-fill";
    parts.push(`<rect x="${(cx - bw / 2).toFixed(1)}" y="${yv.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1, y(0) - yv).toFixed(1)}" class="${cls}"><title>${r.d}: actual ${dvFmt(r.actual, 1)} kWh, twin ${dvFmt(r.replay, 1)} kWh, ${dvFmt(r.out, 0)}° out${r.warmup ? ", warm-up" : ""}</title></rect>`);
    if (r.replay != null) line.push([cx, y(r.replay)]);
    else if (line.length) {
      parts.push(`<path d="${dvPath(line)}" class="dv-line dv-indoor"/>`);
      line.length = 0;
    }
    const d = new Date(r.d + "T12:00:00");
    if (d.getDate() === 1 || i === 0) parts.push(`<text x="${cx.toFixed(1)}" y="${top + h + 14}" text-anchor="middle">${DV_MONTH[d.getMonth()]} ${d.getDate()}</text>`);
  });
  if (line.length) parts.push(`<path d="${dvPath(line)}" class="dv-line dv-indoor"/>`);
  return (
    dvLegend([
      ["k-drift k-box", "Actual"],
      ["k-indoor", "Twin, your setpoints"],
      ["k-faint k-box", "Warm-up or partial day"],
    ]) + `<svg class="dv-svg" width="${W}" height="${top + h + 22}" viewBox="0 0 ${W} ${top + h + 22}" role="img" aria-label="Daily energy, actual against twin">${parts.join("")}</svg>`
  );
}

function dvTwinTable(tw) {
  const arms = tw.arms || [];
  if (!arms.length) return dvEmpty(dvTwinReason(tw) || "No twin days yet.");
  const actual = arms.find((a) => a.key === "actual");
  const base = actual ? dvNum(actual.kwh_day) : null;
  const rows = arms.map((a) => {
    const k = dvNum(a.kwh_day);
    const vs = a.key === "actual" || k == null || base == null ? "" : dvSigned(k - base, 1);
    return `<tr${a.key === "actual" ? ' class="dv-strong"' : ""}><td title="${esc(a.label)}">${esc(a.short || a.label)}</td><td>${k == null ? "–" : k.toFixed(1)}</td><td>${vs}</td><td>${dvFmt(a.aux_h, 1)}</td><td>${dvFmt(a.indoor_mean, 1)}°</td><td>${a.off_setpoint_h == null ? "" : dvFmt(a.off_setpoint_h, 1)}</td></tr>`;
  });
  return `<div class="dv-scroll"><table class="dv-table dv-mono"><thead><tr><th></th><th>kWh/d</th><th>vs act</th><th>Aux h</th><th>In</th><th>Off h</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}

function dvPctBars(rows, W) {
  if (!rows.length) return dvEmpty("No graded days yet.");
  const ML = 36;
  const MR = 6;
  const top = 8;
  const h = 150;
  const span = Math.max(10, ...rows.map((r) => Math.abs(r.pct))) * 1.15;
  const x = dvScale(0, rows.length, ML, W - MR);
  const y = dvScale(-span, span, top + h, top);
  const parts = [];
  for (const v of dvTicks(-span, span, 4)) {
    parts.push(`<line x1="${ML}" x2="${W - MR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${v === 0 ? "dv-axis" : "dv-grid"}"/>`);
    parts.push(`<text x="${ML - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${v > 0 ? "+" : ""}${v}%</text>`);
  }
  const bw = Math.max(2, Math.min(48, (x(1) - x(0)) * 0.6));
  rows.forEach((r, i) => {
    const cx = x(i + 0.5);
    const y0 = y(0);
    const yv = y(r.pct);
    parts.push(`<rect x="${(cx - bw / 2).toFixed(1)}" y="${Math.min(y0, yv).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1, Math.abs(yv - y0)).toFixed(1)}" rx="2" class="${r.pct < 0 ? "dv-indoor-fill" : "dv-aux-fill"}"><title>${esc(r.label)}: ${dvSigned(r.pct, 0)}%, ${r.n} days</title></rect>`);
    parts.push(`<text x="${cx.toFixed(1)}" y="${top + h + 14}" text-anchor="middle">${esc(r.label)}</text>`);
  });
  return `<svg class="dv-svg" width="${W}" height="${top + h + 22}" viewBox="0 0 ${W} ${top + h + 22}" role="img" aria-label="Twin error by outdoor temperature">${parts.join("")}</svg>`;
}

function dvDegreeChart(deg, W) {
  const bins = (deg && deg.bins) || [];
  const fit = deg && deg.fit;
  if (!bins.length && !fit) return dvEmpty("Needs 10 days in season.");
  const cooling = deg.season === "cool";
  const xs = bins.map((b) => b.bin + 2.5);
  const xLo = Math.floor((Math.min(...xs, cooling ? 65 : 20) - 5) / 10) * 10;
  const xHi = Math.ceil((Math.max(...xs, cooling ? 90 : 55) + 5) / 10) * 10;
  const reg = (t) => {
    if (!fit) return null;
    const b = fit.balance;
    return cooling
      ? fit.slope * (Math.max(0, t - (b - 1)) - Math.max(0, t - b))
      : fit.slope * (Math.max(0, b + 1 - t) - Math.max(0, b - t));
  };
  const ts = [];
  for (let t = xLo; t <= xHi; t += 0.5) ts.push(t);
  const yHi = Math.max(1, ...bins.map((b) => b.kwh), ...ts.map((t) => reg(t) || 0)) * 1.15;
  const ML = 34;
  const MR = 8;
  const top = 18;
  const h = 150;
  const x = dvScale(xLo, xHi, ML, W - MR);
  const y = dvScale(0, yHi, top + h, top);
  const parts = [];
  for (const v of dvTicks(0, yHi, 4)) {
    parts.push(`<line x1="${ML}" x2="${W - MR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${v === 0 ? "dv-axis" : "dv-grid"}"/>`);
    parts.push(`<text x="${ML - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${v}</text>`);
  }
  for (const v of dvTicks(xLo, xHi, 6)) parts.push(`<text x="${x(v).toFixed(1)}" y="${top + h + 14}" text-anchor="middle">${v}°</text>`);
  parts.push(`<text x="0" y="10">kWh/day</text>`);
  if (fit) parts.push(`<path d="${dvPath(ts.map((t) => [x(t), y(reg(t))]))}" class="dv-line dv-sp dv-thin"/>`);
  for (const b of bins) {
    parts.push(`<circle cx="${x(b.bin + 2.5).toFixed(1)}" cy="${y(b.kwh).toFixed(1)}" r="4.5" class="dv-dot ${cooling ? "dv-cool-fill" : "dv-heat-fill"}"><title>${b.bin} to ${b.bin + 5}°: ${b.kwh.toFixed(2)} kWh per degree, ${b.n} days</title></circle>`);
  }
  const k = dvNum(deg.k);
  return (
    dvLegend([
      [cooling ? "k-cool k-dot" : "k-heat k-dot", "Twin, by outdoor bin"],
      ["k-sp", "Regression"],
    ]) +
    `<svg class="dv-svg" width="${W}" height="${top + h + 22}" viewBox="0 0 ${W} ${top + h + 22}" role="img" aria-label="Cost of one degree by outdoor temperature">${parts.join("")}</svg>` +
    `<div class="dv-statline"><span>twin / regression ${k == null ? "–" : k.toFixed(2)}</span>${fit ? `<span>balance ${fit.balance.toFixed(0)}°</span><span>R² ${fit.r2.toFixed(2)}</span>` : ""}</div>`
  );
}

function dvAuxDays(aux, W) {
  const days = ((aux && aux.days) || []).slice(-30);
  const win = (aux && aux.window) || {};
  if (!days.length) return dvEmpty("No graded days yet.");
  const ML = 30;
  const MR = 6;
  const top = 14;
  const h = 130;
  const yHi = Math.max(1, ...days.map((d) => d.recovery + d.cold + d.other)) * 1.1;
  const x = dvScale(0, Math.max(days.length, 10), ML, W - MR);
  const y = dvScale(0, yHi, top + h, top);
  const parts = [];
  for (const v of dvTicks(0, yHi, 3)) {
    parts.push(`<line x1="${ML}" x2="${W - MR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${v === 0 ? "dv-axis" : "dv-grid"}"/>`);
    parts.push(`<text x="${ML - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${v}</text>`);
  }
  parts.push(`<text x="0" y="10">h</text>`);
  const bw = Math.max(2, (x(1) - x(0)) * 0.7);
  days.forEach((d, i) => {
    const cx = x(i + 0.5);
    let base = 0;
    for (const [key, cls] of [["recovery", "dv-aux-fill"], ["cold", "dv-heat-fill"], ["other", "dv-drift-fill"]]) {
      const v = d[key] || 0;
      if (v <= 0) continue;
      const y0 = y(base);
      const y1 = y(base + v);
      parts.push(`<rect x="${(cx - bw / 2).toFixed(1)}" y="${y1.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1, y0 - y1).toFixed(1)}" class="${cls}"><title>${d.d}: ${v.toFixed(1)} h ${key}</title></rect>`);
      base += v;
    }
    const dt = new Date(d.d + "T12:00:00");
    if (i === 0 || dt.getDate() === 1) parts.push(`<text x="${cx.toFixed(1)}" y="${top + h + 14}" text-anchor="middle">${DV_MONTH[dt.getMonth()]} ${dt.getDate()}</text>`);
  });
  const c = win.classes || {};
  const kwh = win.kwh || {};
  const label = aux.kwh_label === "assumed" ? " (assumed watts)" : "";
  const share = dvNum(win.share);
  return (
    dvLegend([
      ["k-aux k-box", "Recovery"],
      ["k-heat k-box", "Cold"],
      ["k-drift k-box", "Other"],
    ]) +
    `<svg class="dv-svg" width="${W}" height="${top + h + 22}" viewBox="0 0 ${W} ${top + h + 22}" role="img" aria-label="Aux heat hours by day and cause">${parts.join("")}</svg>` +
    `<div class="dv-statline"><span>recovery ${dvFmt(c.recovery, 1)} h, ${dvFmt(kwh.recovery, 0)} kWh</span><span>cold ${dvFmt(c.cold, 1)} h, ${dvFmt(kwh.cold, 0)} kWh</span><span>other ${dvFmt(c.other, 1)} h</span><span>${share == null ? "" : Math.round(share * 100) + "% of heating energy" + label}</span></div>`
  );
}

function dvYoy(rows) {
  if (!rows || !rows.length) return dvEmpty("Needs a month with last year on record.");
  const pct = (v) => (v == null ? "–" : `${dvSigned(v * 100, 0)}%`);
  const body = rows
    .slice()
    .reverse()
    .map((r) => {
      const m = DV_MONTH[Number(r.month.slice(5, 7)) - 1] + " " + r.month.slice(2, 4);
      return r.within_noise
        ? `<tr><td>${m}</td><td colspan="3">within noise (±${Math.round(r.band * 100)}%)</td><td>${r.n}</td></tr>`
        : `<tr><td>${m}</td><td>${pct(r.total)}</td><td>${pct(r.house)}</td><td>${pct(r.setpoints)}</td><td>${r.n}</td></tr>`;
    });
  return `<div class="dv-scroll"><table class="dv-table dv-mono"><thead><tr><th>Month</th><th>Total</th><th>House</th><th>Setpoints</th><th>Days</th></tr></thead><tbody>${body.join("")}</tbody></table></div>`;
}

function renderTwinTab(states, dash, L) {
  if (!dash) return dvEmpty("Loading archive…");
  const tw = dash.twin;
  if (!tw) return dvEmpty("No twin data yet.");
  const trusted = TWIN_TRUSTED.includes(tw.grade);
  const err = (tw.by_outdoor || []).filter((r) => r.error != null).map((r) => ({ label: `${r.bin}°`, pct: r.error * 100, n: r.n }));
  const daysMeta = `${(tw.daily || []).length} days`;
  const accMeta = tw.gain == null ? "" : `${dvSigned((tw.gain - 1) * 100, 0)}%, daily ±${Math.round((tw.daily_error || 0) * 100)}%, ${esc(tw.grade.replace("_", " "))}`;
  return `
    ${dvTwinStrip(tw)}
    <div class="dv-row dv-row-64">
      ${dvCard("Actual vs twin, daily", daysMeta, dvTwinDaily(tw.daily, L.wide64))}
      ${dvCard("Last 30 days", trusted ? "modeled" : esc(dvTwinReason(tw)), dvTwinTable(tw))}
    </div>
    <div class="dv-row dv-row-2">
      ${dvCard("Twin error by outdoor temperature", accMeta, dvPctBars(err, L.half))}
      ${dvCard("Cost of a degree", "modeled", dvDegreeChart(tw.degree, L.half))}
    </div>
    <div class="dv-row dv-row-2">
      ${dvCard("Aux heat", "30 d", dvAuxDays(tw.aux, L.half))}
      ${dvCard("Same weather, last year", "runtime", dvYoy(tw.yoy))}
    </div>`;
}

class HeatPumpOptimizerPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = PANEL_CSS;
    this.shadowRoot.appendChild(style);
    this._container = document.createElement("div");
    this.shadowRoot.appendChild(this._container);
    this._hass = null;
    this._retro = null;          // cached 48h history for retrospective chart
    this._retroFetchedAt = 0;    // timestamp of last successful fetch
    this._retroFetching = false;
    this._renderScheduled = false;
    this._rafId = null;
    this._lastFingerprint = "";
    this._dash = null;           // dashboard archive payload (websocket)
    this._dashFetchedAt = 0;
    this._dashFetching = false;
    this._tlModel = null;        // timeline scales for the crosshair
    this._width = 0;
    this._resizeObserver = null;
    this._tab = null;
    try {
      this._tab = window.localStorage.getItem("hpo.tab");
    } catch (_e) {
      this._tab = null;
    }
  }

  connectedCallback() {
    // Charts are drawn at the measured width, so a resize means a redraw.
    if (typeof ResizeObserver === "undefined" || this._resizeObserver) return;
    this._resizeObserver = new ResizeObserver(() => {
      const w = this._container.clientWidth || 0;
      if (Math.abs(w - this._width) > 8) this._scheduleRender();
    });
    this._resizeObserver.observe(this._container);
  }

  disconnectedCallback() {
    if (this._rafId) {
      cancelAnimationFrame(this._rafId);
      this._rafId = null;
      this._renderScheduled = false;
    }
    if (this._resizeObserver) {
      this._resizeObserver.disconnect();
      this._resizeObserver = null;
    }
  }

  _scheduleRender() {
    if (this._renderScheduled) return;
    this._renderScheduled = true;
    this._rafId = requestAnimationFrame(() => {
      this._renderScheduled = false;
      this._rafId = null;
      this._render();
    });
  }

  async _maybeRefreshDash() {
    if (this._dashFetching || !this._hass || typeof this._hass.callWS !== "function") return;
    const now = Date.now();
    if (now - this._dashFetchedAt < 5 * 60 * 1000) return;
    this._dashFetching = true;
    this._dashFetchedAt = now;
    try {
      this._dash = await this._hass.callWS({ type: "heatpump_optimizer/dashboard" });
      this._scheduleRender();
    } catch (e) {
      // Older backend, or the entry is still loading: the tabs say so.
      console.warn("HPO: dashboard fetch failed", e);
    } finally {
      this._dashFetching = false;
    }
  }

  set hass(hass) {
    this._hass = hass;
    indexEntities(hass);
    this._maybeRefreshHistory();
    this._maybeRefreshDash();

    // Only re-render when optimizer-relevant entity states actually change.
    // HA pushes hass updates for ALL entities (dozens/sec), so rendering on
    // every update causes DOM thrashing (blinking, lost selections, unclickable
    // elements).  We fingerprint only the states we care about and skip the
    // render if nothing changed.
    const fp = this._buildFingerprint(hass.states);
    if (fp === this._lastFingerprint) return;
    this._lastFingerprint = fp;

    this._scheduleRender();
  }

  /** Build a lightweight fingerprint of optimizer + climate entity states. */
  _buildFingerprint(states) {
    let fp = "";
    for (const id of Object.keys(states)) {
      if (id.includes(ENTITY_PREFIX) || id.startsWith("climate.")) {
        const s = states[id];
        fp += id + ":" + s.state + ":" + (s.last_changed || "") + ";";
      }
    }
    return fp;
  }

  async _maybeRefreshHistory() {
    if (this._retroFetching) return;
    const now = Date.now();
    if (now - this._retroFetchedAt < 5 * 60 * 1000) return; // throttle: once per 5 min
    this._retroFetching = true;
    try {
      const s = this._hass.states;
      const indoorE = findEntity(s, "apparent_temperature");
      const indoorWE = findEntity(s, "weighted_indoor_temp");
      const outdoorE = findEntity(s, "outdoor_temp_source");
      const predictedE = findEntity(s, "predicted_indoor_temp");
      const powerE = findEntity(s, "net_hvac_power");
      const auxE = findBinary(s, "aux_heat_active");
      // Find the climate entity for hvac_action history
      const climateE = Object.values(s).find(e => e.entity_id.startsWith("climate.") && e.attributes?.hvac_action != null);
      const ids = [indoorE, indoorWE, outdoorE, predictedE, powerE, auxE].filter(Boolean).map(e => e.entity_id).join(",");
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
      // A thermostat in heat_cool never changes *state*; its action and
      // setpoints change as attributes, and only last_updated moves then.
      let hvacActions = [];
      let setpoints = [];
      if (Array.isArray(climateData)) {
        for (const series of climateData) {
          if (series.length > 0 && series[0].entity_id === climateE?.entity_id) {
            hvacActions = series.map(pt => ({
              last_changed: pt.last_updated || pt.last_changed,
              state: pt.attributes?.hvac_action || "idle",
            }));
            setpoints = series.map(pt => {
              const a = pt.attributes || {};
              const single = dvNum(a.temperature);
              const low = dvNum(a.target_temp_low);
              const high = dvNum(a.target_temp_high);
              return {
                t: Date.parse(pt.last_updated || pt.last_changed),
                low: low != null ? low : (pt.state === "heat" ? single : null),
                high: high != null ? high : (pt.state === "cool" ? single : null),
              };
            }).filter(pt => Number.isFinite(pt.t));
          }
        }
      }
      this._retro = {
        indoor:    byId[indoorE?.entity_id]    || [],
        indoorW:   byId[indoorWE?.entity_id]   || [],
        setpoints,
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
    this._container.querySelectorAll("details[open]").forEach((el) => {
      const key = el.className || el.querySelector("summary")?.textContent;
      if (key) openDetails.add(key);
    });

    const tierVal = (findEntity(s, "savings_accuracy_tier")?.state || "learning");
    const isLearning = tierVal === "learning";
    const isProjected = tierVal === "projected";
    const showSavings = !isLearning;  // projected and above show savings

    // Tabs: the original cards live under Overview; the rest are wide.
    const tab = DV_TABS.some(([k]) => k === this._tab) ? this._tab : (isLearning ? "overview" : "now");
    const width = this._container.clientWidth || 1200;
    this._width = width;
    const inner = Math.min(width, tab === "overview" ? 720 : 1500) - 32;
    const two = inner >= 900;
    const three = inner >= 1100;
    const gap = 12;
    const pad = 34; // card padding and border
    const fit = (w) => Math.max(240, Math.floor(w - pad));
    const L = {
      full: fit(inner),
      wide64: fit(two ? (inner - gap) * 0.64 : inner),
      narrow64: fit(two ? (inner - gap) * 0.36 : inner),
      wide73: fit(two ? (inner - gap) * 0.72 : inner),
      half: fit(two ? (inner - gap) / 2 : inner),
      third: fit(three ? (inner - 2 * gap) / 3 : inner),
    };
    this._tlModel = null;
    let dashBody = "";
    if (tab === "now") {
      const built = renderNowTab(s, this._hass, this._retro, this._dash, L);
      dashBody = built.html;
      this._tlModel = built.model;
    } else if (tab === "model") {
      dashBody = renderModelTab(s, this._dash, L);
    } else if (tab === "energy") {
      dashBody = renderEnergyTab(s, this._dash, L);
    } else if (tab === "twin") {
      dashBody = renderTwinTab(s, this._dash, L);
    }
    const planAt = Date.parse(findEntity(s, "schedule")?.attributes?.last_optimization || "");
    const chips = [
      this._dash?.plan?.monitor_only ? `<span class="dv-chip">Monitor only</span>` : "",
      Number.isFinite(planAt) ? `<span>plan ${dvHM(planAt)}</span>` : "",
    ].join("");
    const dark = !!this._hass.themes?.darkMode;
    const panelClass = ["panel", tab !== "overview" ? "wide" : "", dark ? "dv-dark" : "", two ? "" : "dv-narrow", three ? "" : "dv-mid"]
      .filter(Boolean).join(" ");

    this._container.innerHTML = `
      <div class="${panelClass}">
        <header class="header">
          <div class="header-row">
            <h1>Heat Pump Optimizer</h1>
            <div class="dv-chips">${chips}</div>
          </div>
          <div class="tabs" role="tablist">
            ${DV_TABS.map(([k, label]) => `<button class="tab" role="tab" data-tab="${k}" aria-selected="${k === tab}">${label}</button>`).join("")}
          </div>
        </header>
        ${renderAlerts(s)}
        ${tab !== "overview" ? dashBody : `
        ${isLearning ? renderWelcomeCard(s) : ""}
        ${renderHeroStrip(s, this._hass)}
        ${renderEnvironmentCard(s, this._hass)}
        ${renderThermalLoadCard(s, this._hass)}
        ${(() => {
          if (showSavings && !isProjected) return renderUnifiedTimeline(s, this._hass, this._retro);
          const fc = findEntity(s, "schedule")?.attributes?.forecast;
          const hasRetro = this._retro && (this._retro.indoor.length > 0 || this._retro.outdoor.length > 0);
          return (fc && Array.isArray(fc) && fc.length >= 2 && hasRetro)
            ? renderUnifiedTimeline(s, this._hass, this._retro)
            : renderRetrospectiveChart(s, this._hass, this._retro);
        })()}
        ${(isLearning || isProjected) ? renderLearningProgressCards(s) : ""}
        ${renderTemperatureProfile(s)}
        ${isLearning ? renderLearningMilestones(s) : renderDecisionCard(s, this._hass)}
        ${renderBuildingCard(s, this._hass)}
        ${showSavings ? renderSavingsCard(s, this._hass, isProjected) : renderSnapshotCard(s, this._hass)}
        ${renderHealthCard(s, this._hass)}
        `}
      </div>
    `;

    // Restore <details> open state
    openDetails.forEach((key) => {
      this._container.querySelectorAll("details").forEach((el) => {
        const elKey = el.className || el.querySelector("summary")?.textContent;
        if (elKey === key) el.setAttribute("open", "");
      });
    });

    this._bindEvents();
  }

  _bindEvents() {
    this._container.querySelectorAll(".tab").forEach((el) => {
      el.addEventListener("click", () => {
        this._tab = el.dataset.tab;
        try {
          window.localStorage.setItem("hpo.tab", this._tab);
        } catch (_e) {
          // private mode: the choice just does not persist
        }
        this._render();
      });
    });

    const tl = this._container.querySelector(".dv-tl");
    const readout = this._container.querySelector(".dv-readout");
    const model = this._tlModel;
    if (tl && readout && model) {
      const cross = tl.querySelector(".dv-cross");
      const resting = dvReadout(model, model.now);
      readout.textContent = resting;
      tl.addEventListener("mousemove", (ev) => {
        const box = tl.getBoundingClientRect();
        const px = (ev.clientX - box.left) * (tl.viewBox.baseVal.width / box.width);
        if (px < model.left || px > model.right) return;
        const t = model.t0 + ((px - model.left) / (model.right - model.left)) * (model.t1 - model.t0);
        cross.setAttribute("x1", px.toFixed(1));
        cross.setAttribute("x2", px.toFixed(1));
        cross.style.display = "";
        readout.textContent = dvReadout(model, t);
      });
      tl.addEventListener("mouseleave", () => {
        cross.style.display = "none";
        readout.textContent = resting;
      });
    }

    const btn = this._container.querySelector("#toggle-optimizer");
    if (btn) {
      const enabled = findEntity(this._hass.states, "enabled");
      if (enabled) {
        const isOn = enabled.state === "on";
        const newBtn = btn.cloneNode(true);
        btn.parentNode.replaceChild(newBtn, btn);
        newBtn.addEventListener("click", () => {
          this._hass.callService("switch", isOn ? "turn_off" : "turn_on", {
            entity_id: enabled.entity_id,
          });
        });
      }
    }

    // Tap-to-reveal tooltip on thermal load bar segments
    const tooltip = this._container.querySelector(".tl-seg-tooltip");
    const segs = this._container.querySelectorAll(".tl-bar-seg");
    if (tooltip && segs.length) {
      let activeEl = null;
      segs.forEach(seg => {
        seg.addEventListener("click", (e) => {
          e.stopPropagation();
          if (activeEl === seg) {
            tooltip.style.display = "none";
            activeEl = null;
            return;
          }
          activeEl = seg;
          const label = seg.dataset.label;
          const value = seg.dataset.value;
          tooltip.textContent = `${label}: ${value}`;
          tooltip.style.background = seg.style.background;
          tooltip.style.display = "block";
        });
      });
      // Dismiss tooltip when tapping elsewhere on the card
      const card = this._container.querySelector(".thermal-load-card");
      if (card) {
        card.addEventListener("click", () => {
          tooltip.style.display = "none";
          activeEl = null;
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
  .projected-banner {
    background: var(--warning-color, #ff9800);
    color: var(--text-primary-color, #fff);
    padding: 8px 12px;
    border-radius: 8px;
    margin-bottom: 8px;
    font-size: 0.85em;
    text-align: center;
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
  .tl-bar-track {
    position: relative;
    height: 20px;
    background: color-mix(in srgb, var(--border) 30%, transparent);
    border-radius: 6px;
    overflow: hidden;
  }
  .tl-stacked-bar {
    position: relative;
    height: 100%;
    border-radius: 6px;
    overflow: hidden;
    transition: width 0.4s ease;
  }
  .tl-stacked-bar.tl-converging {
    background: repeating-linear-gradient(
      -45deg,
      transparent,
      transparent 4px,
      rgba(255,255,255,0.08) 4px,
      rgba(255,255,255,0.08) 8px
    );
    animation: tl-shimmer 2s linear infinite;
  }
  @keyframes tl-shimmer {
    0% { background-position: 0 0; }
    100% { background-position: 20px 0; }
  }
  .tl-bar-seg {
    position: absolute;
    top: 0;
    height: 100%;
    opacity: 0.85;
    cursor: pointer;
    transition: opacity 0.15s;
  }
  .tl-bar-seg:hover, .tl-bar-seg:active {
    opacity: 1;
  }
  .tl-seg-tooltip {
    margin-top: 6px;
    padding: 4px 10px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 500;
    color: #fff;
    text-shadow: 0 1px 2px rgba(0,0,0,0.4);
    width: fit-content;
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

  /* ── Activation Tier Badge (System Health card) ── */
  .atier-header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
    flex-wrap: wrap;
  }
  .atier-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    font-weight: 600;
    padding: 4px 10px;
    border-radius: 6px;
  }
  .atier-learning { background: color-mix(in srgb, var(--orange) 12%, transparent); color: var(--orange); }
  .atier-conservative { background: color-mix(in srgb, var(--accent) 12%, transparent); color: var(--accent); }
  .atier-standard { background: color-mix(in srgb, var(--green) 12%, transparent); color: var(--green); }
  .atier-confident { background: color-mix(in srgb, var(--green) 18%, transparent); color: var(--green); }
  .atier-dots { display: inline-flex; gap: 3px; }
  .atier-dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: color-mix(in srgb, currentColor 25%, transparent);
  }
  .atier-dot.filled { background: currentColor; }
  .atier-desc {
    font-size: 12px;
    color: var(--text-secondary);
  }

  /* ── Learning Needs ── */
  .health-needs {
    margin-top: 8px;
    font-size: 12px;
    color: var(--text-secondary);
  }
  .health-need-item {
    padding: 2px 0;
  }
  .health-need-item::before {
    content: "\\2192  ";
    color: var(--orange);
  }
  .lp-needs {
    margin-top: 6px;
    font-size: 11px;
    color: var(--text-secondary);
  }
  .lp-need-item {
    padding: 2px 0;
  }
  .lp-need-item::before {
    content: "\\2192  ";
    color: var(--orange);
  }

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

  /* ── Dashboard tabs (Now / Model / Energy) ── */
  :host {
    --s-indoor: #2a78d6;
    --s-heat: #eb6834;
    --s-out: #1baf7a;
    --s-aux: #e34948;
    --s-cool: #6250d6;
    --q1: #cde2fb;
    --q2: #86b6ef;
    --q3: #3987e5;
    --q4: #184f95;
  }
  .panel.dv-dark {
    --s-indoor: #3987e5;
    --s-heat: #d95926;
    --s-out: #199e70;
    --s-aux: #e66767;
    --s-cool: #9085e9;
    --q1: #184f95;
    --q2: #256abf;
    --q3: #3987e5;
    --q4: #9ec5f4;
  }
  .panel.wide { max-width: 1500px; }
  .header-row { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  .dv-chips { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; font-size: 12px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
  .dv-chip { border: 1px solid var(--text-primary); color: var(--text-primary); border-radius: 999px; padding: 1px 9px; font-weight: 500; }
  .tabs { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 10px; border-bottom: 1px solid var(--border); }
  .tab {
    appearance: none; background: none; border: 0; border-bottom: 2px solid transparent;
    margin-bottom: -1px; padding: 8px 14px; min-height: 40px;
    font: inherit; font-size: 14px; font-weight: 500; color: var(--text-secondary); cursor: pointer;
  }
  .tab:hover { color: var(--text-primary); }
  .tab[aria-selected="true"] { color: var(--text-primary); border-bottom-color: var(--text-primary); }
  .tab:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

  .dv-row { display: grid; gap: 12px; margin-bottom: 12px; align-items: stretch; }
  .dv-row > .card { margin-bottom: 0; min-width: 0; }
  .dv-row-64 { grid-template-columns: minmax(0, 64fr) minmax(0, 36fr); }
  .dv-row-73 { grid-template-columns: minmax(0, 72fr) minmax(0, 28fr); }
  .dv-row-2 { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .dv-row-3 { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1fr); }
  .dv-narrow .dv-row, .dv-mid .dv-row-3 { grid-template-columns: minmax(0, 1fr); }

  .dv-head { display: flex; align-items: baseline; justify-content: space-between; gap: 6px 12px; flex-wrap: wrap; margin-bottom: 8px; }
  .dv-head h2 { margin: 0; }
  .dv-meta { font-size: 12px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
  .dv-readout { color: var(--text-primary); white-space: nowrap; }
  .dv-empty { font-size: 13px; color: var(--text-secondary); padding: 18px 0; }
  .dv-statline { display: flex; flex-wrap: wrap; gap: 4px 18px; margin-top: 6px; font-size: 12px; font-variant-numeric: tabular-nums; }

  .dv-svg { display: block; max-width: 100%; height: auto; overflow: visible; }
  .dv-svg text { font-size: 11px; fill: var(--text-secondary); font-variant-numeric: tabular-nums; }
  .dv-svg text.dv-strong { fill: var(--text-primary); font-weight: 600; }
  .dv-grid { stroke: var(--border); stroke-width: 1; opacity: 0.7; }
  .dv-axis { stroke: var(--text-secondary); stroke-width: 1; opacity: 0.6; }
  .dv-mark { stroke: var(--text-secondary); stroke-width: 1; stroke-dasharray: 2 3; }
  .dv-now, .dv-cross { stroke: var(--text-primary); stroke-width: 1; }
  .dv-cross { opacity: 0.5; pointer-events: none; }
  .dv-future { fill: color-mix(in srgb, var(--text-primary) 3%, transparent); }
  .dv-band { fill: color-mix(in srgb, var(--text-primary) 9%, transparent); }
  .dv-fan { fill: color-mix(in srgb, var(--s-indoor) 18%, transparent); }
  .dv-line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
  .dv-thin { stroke-width: 1.5; }
  .dv-dash { stroke-dasharray: 6 4; }
  .dv-indoor { stroke: var(--s-indoor); }
  .dv-out { stroke: var(--s-out); }
  .dv-heat { stroke: var(--s-heat); }
  .dv-cool { stroke: var(--s-cool); }
  .dv-drift { stroke: var(--text-secondary); }
  .dv-sp { stroke: var(--text-primary); stroke-width: 1.5; }
  .dv-indoor-fill { fill: var(--s-indoor); }
  .dv-heat-fill { fill: var(--s-heat); }
  .dv-cool-fill { fill: var(--s-cool); }
  .dv-aux-fill { fill: var(--s-aux); }
  .dv-drift-fill { fill: var(--text-secondary); }
  .dv-dot { stroke: var(--card-bg); stroke-width: 1.5; }
  .dv-ring { stroke: var(--text-primary); stroke-width: 1.5; }
  .dv-faint { opacity: 0.35; }
  .dv-hollow { fill: none; stroke: var(--text-secondary); stroke-width: 1; }
  .dv-run { fill: var(--s-heat); }
  .dv-planned { opacity: 0.3; }
  rect.dv-q0 { fill: color-mix(in srgb, var(--text-primary) 6%, transparent); }
  rect.dv-q1 { fill: var(--q1); }
  rect.dv-q2 { fill: var(--q2); }
  rect.dv-q3 { fill: var(--q3); }
  rect.dv-q4 { fill: var(--q4); }

  .dv-legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin-bottom: 6px; font-size: 12px; }
  .dv-legend span { display: inline-flex; align-items: center; gap: 6px; }
  .dv-legend-q { color: var(--text-secondary); margin-top: 6px; margin-bottom: 0; }
  .dv-key { display: inline-block; width: 16px; height: 0; border-top: 2px solid currentColor; color: var(--text-secondary); }
  .dv-key.k-dash { border-top-style: dashed; }
  .dv-key.k-indoor { color: var(--s-indoor); }
  .dv-key.k-out { color: var(--s-out); }
  .dv-key.k-heat { color: var(--s-heat); }
  .dv-key.k-cool { color: var(--s-cool); }
  .dv-key.k-aux { color: var(--s-aux); }
  .dv-key.k-drift { color: var(--text-secondary); }
  .dv-key.k-sp { color: var(--text-primary); border-top-width: 1.5px; }
  .dv-key.k-dot { width: 9px; height: 9px; border: 0; border-radius: 50%; background: currentColor; }
  .dv-key.k-box, .dv-key.k-run { width: 10px; height: 10px; border: 0; border-radius: 2px; background: currentColor; }
  .dv-key.k-run { color: var(--s-heat); }
  .dv-key.k-planned, .dv-key.k-faint { opacity: 0.35; }
  .dv-key.k-ring { box-shadow: 0 0 0 1.5px var(--text-primary); }
  .dv-key.k-band, .dv-key.k-fan { width: 14px; height: 10px; border: 0; border-radius: 2px; }
  .dv-key.k-band { background: color-mix(in srgb, var(--text-primary) 12%, transparent); }
  .dv-key.k-fan { background: color-mix(in srgb, var(--s-indoor) 22%, transparent); }
  .dv-legend-q .dv-key { width: 12px; height: 10px; border: 0; }
  .dv-key.dv-q0 { background: color-mix(in srgb, var(--text-primary) 6%, transparent); }
  .dv-key.dv-q1 { background: var(--q1); }
  .dv-key.dv-q2 { background: var(--q2); }
  .dv-key.dv-q3 { background: var(--q3); }
  .dv-key.dv-q4 { background: var(--q4); }

  .dv-stats { display: flex; flex-wrap: wrap; padding: 0; overflow: hidden; }
  .dv-stat { flex: 1 1 150px; min-width: 0; padding: 12px 16px; box-shadow: -1px 0 0 var(--border); }
  .dv-stat-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.8px; color: var(--text-secondary); }
  .dv-stat-value { font-size: 24px; font-weight: 500; line-height: 1.25; font-variant-numeric: tabular-nums; }
  .dv-stat-value.dv-small { font-size: 19px; }
  .dv-stat-value small { font-size: 13px; font-weight: 400; color: var(--text-secondary); }
  .dv-stat-sub { font-size: 12px; color: var(--text-secondary); }
  .dv-cov { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 3px 8px; align-items: center; margin-top: 4px; font-size: 11.5px; font-variant-numeric: tabular-nums; }
  .dv-bar { height: 6px; border-radius: 3px; background: color-mix(in srgb, var(--text-primary) 10%, transparent); }
  .dv-bar i { display: block; height: 6px; border-radius: 3px; background: var(--text-primary); }
  .dv-n { text-align: right; }

  .dv-scroll { overflow-x: auto; }
  .dv-table { width: 100%; border-collapse: collapse; font-size: 13px; font-variant-numeric: tabular-nums; }
  .dv-table th {
    text-align: left; padding: 5px 10px 5px 0; white-space: nowrap;
    font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.6px;
    color: var(--text-secondary); border-bottom: 1px solid var(--border);
  }
  .dv-table td { padding: 6px 10px 6px 0; white-space: nowrap; border-bottom: 1px solid color-mix(in srgb, var(--border) 60%, transparent); }
  .dv-table tr:last-child td { border-bottom: 0; }
  .dv-table .dv-num { text-align: right; }
  .dv-right th, .dv-right td { text-align: right; }
  .dv-right th:first-child, .dv-right td:first-child { text-align: left; }
  .dv-mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
  .dv-kv td:last-child { padding-right: 0; }
  .dv-planrow td { color: var(--text-secondary); }
  .dv-inbar { display: inline-block; width: 90px; height: 8px; margin-right: 8px; vertical-align: middle; border-radius: 0 4px 4px 0; background: color-mix(in srgb, var(--text-primary) 8%, transparent); }
  .dv-inbar i { display: block; height: 8px; border-radius: 0 4px 4px 0; background: var(--s-heat); }
`;

customElements.define("heatpump-optimizer-panel", HeatPumpOptimizerPanel);
