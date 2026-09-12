// The panel as data: one entry per row, and the pure functions that label them.
//
// Nothing here knows about any particular headset. The helper reports which
// features the device answered for, and `visibleSections` keeps the rows those
// features support. A headset with no equaliser therefore shows no equaliser
// section, and adding a driver adds rows without touching any QML.
//
// Loaded as a QML JS library, so plain `var` and `function` declarations and no
// exports; the tests evaluate this file in a fresh context.

var BAND_FREQUENCIES = ["400", "1k", "2.5k", "6.3k", "16k"];
var BAND_MIN = -10;
var BAND_MAX = 10;
var AMBIENT_MAX = 20;

var NOISE_OPTIONS = [
  { value: "off", label: "Off", tooltip: "Passive isolation only" },
  { value: "ambient", label: "Ambient", tooltip: "Let the room in" },
  { value: "anc", label: "ANC", tooltip: "Active noise cancelling" }
];

// Presets the protocol defines. Only the ones the device reports are offered.
var PRESET_OPTIONS = [
  { value: "off", label: "Off" },
  { value: "bright", label: "Bright" },
  { value: "excited", label: "Excited" },
  { value: "mellow", label: "Mellow" },
  { value: "relaxed", label: "Relaxed" },
  { value: "vocal", label: "Vocal" },
  { value: "treble", label: "Treble" },
  { value: "bass", label: "Bass" },
  { value: "speech", label: "Speech" },
  { value: "manual", label: "Manual" }
];

var SENSITIVITY_OPTIONS = [
  { value: "auto", label: "Auto" },
  { value: "high", label: "High" },
  { value: "low", label: "Low" }
];

var TIMEOUT_OPTIONS = [
  { value: "short", label: "Short" },
  { value: "standard", label: "Standard" },
  { value: "long", label: "Long" },
  { value: "off", label: "Never" }
];

function speakToChatOn(state) {
  return !!(state && state.speak_to_chat);
}

var SECTIONS = [
  {
    id: "noise",
    title: "NOISE CONTROL",
    rows: [
      { id: "noise", kind: "segmented", label: "Mode", options: NOISE_OPTIONS },
      {
        id: "ambient_level", kind: "slider", label: "Ambient sound",
        minimum: 0, maximum: AMBIENT_MAX, step: 1, ticks: 0,
        hint: "How much of the room around you comes through.",
        unavailableHint: "Only applies in ambient mode. The headset discards it otherwise."
      },
      {
        id: "focus_on_voice", kind: "toggle", label: "Focus on voice",
        description: "Keeps voices and drops the rest of the room",
        unavailableHint: "Only applies in ambient mode"
      }
    ]
  },
  {
    id: "equalizer",
    title: "EQUALISER",
    rows: [
      { id: "eq_preset", kind: "choice", label: "Preset", options: PRESET_OPTIONS },
      {
        id: "eq", kind: "equalizer", label: "Bands", feature: "eq_bands",
        hint: "Five bands and clear bass, in decibels. Moving one selects Manual."
      }
    ]
  },
  {
    id: "behaviour",
    title: "BEHAVIOUR",
    rows: [
      {
        id: "speak_to_chat", kind: "toggle", label: "Speak-to-chat",
        description: "Pauses playback and lets the room in when you talk"
      },
      {
        id: "speak_to_chat_sensitivity", kind: "segmented", label: "Sensitivity",
        options: SENSITIVITY_OPTIONS, showWhen: speakToChatOn
      },
      {
        id: "speak_to_chat_timeout", kind: "segmented", label: "Resumes after",
        options: TIMEOUT_OPTIONS, showWhen: speakToChatOn
      },
      {
        id: "pause_on_removal", kind: "toggle", label: "Pause when taken off",
        description: "Stops playback when the headset leaves your head"
      },
      {
        id: "voice_guidance", kind: "toggle", label: "Voice guidance",
        description: "The spoken announcements from the headset itself"
      },
      {
        id: "dsee", kind: "toggle", label: "DSEE Extreme",
        description: "Sony's upscaling of compressed audio"
      }
    ]
  },
  {
    id: "device",
    title: "HEADSET",
    rows: [
      { id: "touch_sensor", kind: "readout", label: "Touch panel", format: "onOff" },
      { id: "codec", kind: "readout", label: "Codec" },
      { id: "firmware", kind: "readout", label: "Firmware" },
      { id: "power_off", kind: "action", label: "Turn the headset off", confirm: true }
    ]
  }
];

// ------------------------------------------------------------------ row state

// A control's presence comes from the helper's `controls` map and nothing else. A
// readout is not a control, so its presence comes from the record's support. Letting
// a control fall back to `support` made a section survive the loss of every control
// in it, because the two maps can disagree.
function controlInfo(payload, id, kind) {
  var controls = (payload && payload.controls) || {};
  if (kind === "readout") {
    var support = (payload && payload.support) || {};
    var known = controls[id];
    return {
      supported: !!support[id],
      writable: false,
      available: !known || known.available !== false
    };
  }
  return controls[id] || { supported: false, writable: false, available: false };
}

function rowState(row, payload) {
  var id = row.feature || row.id;
  var info = controlInfo(payload, id, row.kind);
  var state = (payload && payload.state) || {};
  var pending = (payload && payload.pending) || [];
  var ignored = (payload && payload.ignored) || [];
  return {
    supported: !!info.supported,
    writable: !!info.writable,
    available: info.available !== false,
    pending: pending.indexOf(id) !== -1,
    ignored: ignored.indexOf(id) !== -1,
    value: state[id]
  };
}

function rowVisible(row, payload) {
  var status = rowState(row, payload);
  if (!status.supported) return false;
  if (row.showWhen && !row.showWhen((payload && payload.state) || {})) return false;
  return true;
}

function visibleSections(payload) {
  var out = [];
  for (var i = 0; i < SECTIONS.length; i++) {
    var section = SECTIONS[i];
    var rows = [];
    for (var j = 0; j < section.rows.length; j++) {
      if (rowVisible(section.rows[j], payload)) rows.push(section.rows[j]);
    }
    if (rows.length > 0) out.push({ id: section.id, title: section.title, rows: rows });
  }
  return out;
}

// Presets the device has actually reported, plus whatever it is set to now, so a
// preset this list does not know about is still shown rather than silently dropped.
function presetOptions(payload) {
  var state = (payload && payload.state) || {};
  var options = PRESET_OPTIONS.slice();
  var current = state.eq_preset;
  if (!current) return options;
  for (var i = 0; i < options.length; i++) {
    if (options[i].value === current) return options;
  }
  options.push({ value: current, label: titleCase(current) });
  return options;
}

// ------------------------------------------------------------------- labelling

function titleCase(value) {
  var text = String(value === undefined || value === null ? "" : value).replace(/_/g, " ");
  return text.length === 0 ? "" : text.charAt(0).toUpperCase() + text.slice(1);
}

function onOff(value) {
  return value ? "On" : "Off";
}

function gainText(db) {
  if (db === undefined || db === null) return "";
  var value = Number(db);
  if (value === 0) return "0";
  return (value > 0 ? "+" : "") + value;
}

function batteryGlyph(level, charging) {
  if (charging) return "󰂄";
  if (level === undefined || level === null || level < 0) return "󰂑";
  if (level >= 90) return "󰁹";
  if (level >= 70) return "󰂁";
  if (level >= 50) return "󰁿";
  if (level >= 30) return "󰁽";
  if (level >= 10) return "󰁻";
  return "󰂃";
}

function batteryText(payload) {
  var state = (payload && payload.state) || {};
  if (state.battery === undefined || state.battery === null) return "";
  return state.battery + "%" + (state.charging ? " charging" : "");
}

function noiseSummary(payload) {
  var state = (payload && payload.state) || {};
  if (state.noise === "anc") return "Noise cancelling";
  if (state.noise === "ambient") {
    var level = state.ambient_level;
    var suffix = (level === undefined || level === null) ? "" : " " + level + "/" + AMBIENT_MAX;
    return "Ambient sound" + suffix;
  }
  if (state.noise === "wind") return "Wind noise reduction";
  if (state.noise === "off") return "Noise control off";
  return "";
}

function deviceName(payload) {
  var device = (payload && payload.device) || {};
  var name = String(device.name || "").trim();
  return name === "" ? "Headset" : name;
}

function heroMeta(payload) {
  if (!payload || !payload.connected) return payload && payload.error ? payload.error : "Not connected";
  var parts = [];
  var battery = batteryText(payload);
  if (battery) parts.push("Battery " + battery);
  var state = payload.state || {};
  if (state.codec) parts.push(state.codec);
  return parts.join("  ·  ");
}

function tooltipText(payload) {
  var lines = [deviceName(payload)];
  var meta = heroMeta(payload);
  if (meta) lines.push(meta);
  var noise = noiseSummary(payload);
  if (noise) lines.push(noise);
  lines.push("Left: panel · Middle: noise on/off · Right: ambient · Scroll: ambient level");
  return lines.join("\n");
}

// What the bar shows beside the glyph: the battery, because it is the number you
// actually want at a glance, and it is available even with no control session.
function barText(payload) {
  var state = (payload && payload.state) || {};
  if (state.battery !== undefined && state.battery !== null) return state.battery + "%";
  var fallback = payload && payload.bluezBattery;
  if (fallback !== undefined && fallback !== null && fallback >= 0) return fallback + "%";
  return "";
}

function accented(payload) {
  var state = (payload && payload.state) || {};
  return state.noise === "anc" || state.noise === "ambient";
}

// ------------------------------------------------------------------- commands

function nextNoiseMode(current, direction) {
  var order = ["off", "ambient", "anc"];
  var at = order.indexOf(String(current));
  if (at === -1) at = 0;
  var next = (at + direction) % order.length;
  if (next < 0) next += order.length;
  return order[next];
}

function clamp(value, low, high) {
  return Math.max(low, Math.min(high, value));
}

function setCommand(values) {
  return JSON.stringify({ set: values });
}

function bandsWith(state, index, value) {
  var bands = (state && state.eq_bands) ? state.eq_bands.slice() : [0, 0, 0, 0, 0];
  bands[index] = clamp(Math.round(value), BAND_MIN, BAND_MAX);
  return bands;
}
