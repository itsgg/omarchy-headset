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

// The two connection modes every Bluetooth headset has. A2DP carries the good
// codec and no microphone; the headset profile carries a microphone and a much
// worse codec. Bluetooth offers no third option, and no headset escapes it.
var MODE_OPTIONS = [
  { value: "a2dp", label: "Music", tooltip: "The best codec the headset offers, and no microphone" },
  { value: "headset", label: "Calls", tooltip: "Turns the microphone on. Audio drops to call quality." }
];

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
        options: SENSITIVITY_OPTIONS,
        unavailableHint: "Switch speak-to-chat on to change this"
      },
      {
        id: "speak_to_chat_timeout", kind: "segmented", label: "Resumes after",
        options: TIMEOUT_OPTIONS,
        unavailableHint: "Switch speak-to-chat on to change this"
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
    id: "audio",
    title: "AUDIO",
    rows: [
      {
        id: "codec", kind: "choice", label: "Codec", optionsFrom: "codecs",
        hint: "What is carrying the audio now. Changing it reconnects the headset."
      },
      {
        id: "audio_mode", kind: "segmented", label: "Mode", options: MODE_OPTIONS,
        hint: "Bluetooth cannot do both at once: a microphone costs the codec."
      },
      {
        id: "microphone", kind: "toggle", label: "Microphone",
        description: "Muted for every application on this machine",
        unavailableHint: "Switch to Calls to use the microphone"
      }
    ]
  },
  {
    id: "device",
    title: "HEADSET",
    rows: [
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

// A row whose choices come from the device rather than from this file: the codecs
// a headset offers are its own, and no list here could know them.
function optionsFor(row, payload) {
  if (row.id === "eq_preset") return presetOptions(payload);
  if (!row.optionsFrom) return row.options || [];
  var source = (payload && payload.audio) || {};
  var list = source[row.optionsFrom];
  return Array.isArray(list) ? list : [];
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

// Earbuds discharge at different rates, so one number for two of them is a
// number that is wrong about at least one. Where the headset reports each part
// separately, say each part.
function batteryText(payload) {
  var state = (payload && payload.state) || {};
  var parts = state.battery_parts;
  if (parts) {
    var pieces = [];
    if (parts.left) pieces.push("L " + parts.left.level + "%");
    if (parts.right) pieces.push("R " + parts.right.level + "%");
    // The case charges too, and saying so is the difference between "put them
    // away" and "the case is flat as well".
    if (parts["case"]) {
      pieces.push("case " + parts["case"].level + "%"
        + (parts["case"].charging ? " charging" : ""));
    }
    if (pieces.length > 0) {
      var worn = (parts.left && parts.left.charging) || (parts.right && parts.right.charging);
      return pieces.join("  ") + (worn ? " charging" : "");
    }
  }
  var level = state.battery;
  if (level === undefined || level === null) {
    var fallback = payload && payload.bluezBattery;
    if (fallback === undefined || fallback === null || fallback < 0) return "";
    level = fallback;
  }
  return level + "%" + (state.charging ? " charging" : "");
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
  var parts = [];
  var battery = batteryText(payload);
  if (battery) parts.push(((payload && payload.state && payload.state.battery_parts)
    ? "" : "Battery ") + battery);
  var sound = (payload && payload.audio) || {};
  if (sound.active_codec) parts.push(sound.active_codec);
  if (parts.length > 0) return parts.join("  ·  ");
  // Nothing to say yet. Only then is the reason worth the line.
  if (payload && payload.error) return payload.error;
  return payload && payload.present ? "Connected" : "Not connected";
}

// While a driver is opening its session there is nothing in `controls` yet, and
// every vendor row is hidden for exactly the same reason an unsupported headset
// hides them. Three words are enough to tell the two apart.
function probeNotice(payload) {
  if (!payload || !payload.present) return "";
  if (payload.connected || payload.unsupported) return "";
  return "Asking the headset\u2026";
}

// The keys this particular panel has. Advertising "a noise" to a headset with no
// noise control is a promise the panel cannot keep.
function keyboardHint(payload) {
  var controls = (payload && payload.controls) || {};
  var parts = [];
  if (controls.noise && controls.noise.writable) parts.push("a noise");
  if (controls.noise && controls.noise.writable) parts.push("t ambient");
  parts.push("j k rows");
  parts.push("h l adjust");
  parts.push("+ − value");
  parts.push("Esc close");
  return parts.join(" · ");
}

// What the panel says when no driver claims this headset: not an error, just the
// boundary of what Bluetooth standardises.
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
