// Tests for Model.js, the pure half of the panel.
//
// Model.js is a QML JS library rather than a module, so it is evaluated in a fresh
// context here. That keeps the file free of test-only exports and still lets these
// run in CI, where Qt is not installed.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const M = createContext({});
runInContext(readFileSync(join(root, "Model.js"), "utf8"), M);

// A payload shaped exactly like the helper's, taken from a real WH-1000XM5.
const xm5 = {
  connected: true,
  write_ready: true,
  device: { address: "AC:80:0A:44:B3:93", name: "WH-1000XM5", driver: "sony-mdr", channel: 9 },
  support: {
    battery: true, charging: true, codec: true, firmware: true, noise: true,
    ambient_level: true, focus_on_voice: true, eq_bands: true, eq_clear_bass: true,
    eq_preset: true, speak_to_chat: true, speak_to_chat_sensitivity: true,
    speak_to_chat_timeout: true, pause_on_removal: true, voice_guidance: true,
    dsee: true, touch_sensor: true
  },
  controls: {
    noise: { supported: true, writable: true, available: true },
    ambient_level: { supported: true, writable: true, available: false },
    focus_on_voice: { supported: true, writable: true, available: false },
    eq_bands: { supported: true, writable: true, available: true },
    eq_clear_bass: { supported: true, writable: true, available: true },
    eq_preset: { supported: true, writable: true, available: true },
    speak_to_chat: { supported: true, writable: true, available: true },
    speak_to_chat_sensitivity: { supported: true, writable: true, available: true },
    speak_to_chat_timeout: { supported: true, writable: true, available: true },
    pause_on_removal: { supported: true, writable: true, available: true },
    voice_guidance: { supported: true, writable: true, available: true },
    dsee: { supported: true, writable: true, available: true },
    touch_sensor: { supported: true, writable: false, available: true },
    power_off: { supported: true, writable: true, available: true }
  },
  state: {
    battery: 34, charging: false, codec: "LDAC", firmware: "2.5.1",
    noise: "off", ambient_level: 20, focus_on_voice: false,
    eq_preset: "bright", eq_bands: [0, 5, 7, 7, 9], eq_clear_bass: -1,
    speak_to_chat: false, speak_to_chat_sensitivity: "auto",
    speak_to_chat_timeout: "standard", pause_on_removal: true,
    voice_guidance: true, dsee: false, touch_sensor: true
  },
  pending: [],
  ignored: [],
  error: ""
};

// Arrays crossing back from the VM context carry that realm's prototypes, so
// deepEqual against a host literal fails on identity alone. Compare shape.
const plain = (value) => JSON.parse(JSON.stringify(value));
const sectionIds = (payload) => plain(M.visibleSections(payload)).map((s) => s.id);
const rowIds = (payload, id) =>
  (plain(M.visibleSections(payload)).find((s) => s.id === id) || { rows: [] }).rows.map((r) => r.id);

test("a fully capable headset shows every section", () => {
  assert.deepEqual(sectionIds(xm5), ["noise", "equalizer", "behaviour", "device"]);
});

test("a headset that answered for nothing shows no sections at all", () => {
  assert.deepEqual(sectionIds({ connected: true, controls: {}, support: {}, state: {} }), []);
});

test("a section disappears when its features do, rather than rendering empty", () => {
  const noEq = structuredClone(xm5);
  delete noEq.controls.eq_bands;
  delete noEq.controls.eq_preset;
  noEq.support.eq_bands = false;
  assert.ok(!sectionIds(noEq).includes("equalizer"));
  assert.ok(sectionIds(noEq).includes("noise"));
});

test("the rows stay put whether speak-to-chat is on or off", () => {
  // They used to appear when it was switched on, moving every row below them.
  // A settings list that rearranges itself is measurably slower to use.
  const rows = ["speak_to_chat", "speak_to_chat_sensitivity", "speak_to_chat_timeout",
                "pause_on_removal", "voice_guidance", "dsee"];
  assert.deepEqual(rowIds(xm5, "behaviour"), rows);
  const talking = structuredClone(xm5);
  talking.state.speak_to_chat = true;
  assert.deepEqual(rowIds(talking, "behaviour"), rows);
});

test("a dependent row says why it cannot be changed yet", () => {
  const off = structuredClone(xm5);
  off.controls.speak_to_chat_sensitivity.available = false;
  const row = M.SECTIONS.find((s) => s.id === "behaviour")
    .rows.find((r) => r.id === "speak_to_chat_sensitivity");
  assert.equal(M.rowState(row, off).available, false);
  assert.ok(row.unavailableHint.length > 0);
});

test("a headset still being asked is not mistaken for one with no features", () => {
  // With no session there is nothing in controls, so every vendor row hides for
  // exactly the same reason an unsupported headset hides them.
  assert.ok(M.probeNotice({ present: true, connected: false, unsupported: false }).length > 0);
  assert.equal(M.probeNotice({ present: true, connected: true }), "");
  assert.equal(M.probeNotice({ present: true, unsupported: true }), "");
  assert.equal(M.probeNotice({ present: false }), "");
});

test("a control that can be neither changed nor acted on gets no row", () => {
  // The touch panel is acknowledged and ignored by the headset, so a row for it
  // showed a value nobody could change and nobody would do anything about.
  const rows = M.SECTIONS.flatMap((s) => s.rows.map((r) => r.id));
  assert.ok(!rows.includes("touch_sensor"));
});

test("ambient level is present but unavailable until ambient mode is chosen", () => {
  assert.equal(M.rowState({ id: "ambient_level" }, xm5).available, false);
  const ambient = structuredClone(xm5);
  ambient.controls.ambient_level.available = true;
  assert.equal(M.rowState({ id: "ambient_level" }, ambient).available, true);
});

test("a pending write is flagged so the panel can say it is not confirmed yet", () => {
  const pending = structuredClone(xm5);
  pending.pending = ["noise"];
  pending.ignored = ["dsee"];
  assert.equal(M.rowState({ id: "noise" }, pending).pending, true);
  assert.equal(M.rowState({ id: "dsee" }, pending).ignored, true);
  assert.equal(M.rowState({ id: "noise" }, xm5).pending, false);
});

test("battery glyphs step down and charging wins over level", () => {
  assert.equal(M.batteryGlyph(100, false), M.batteryGlyph(95, false));
  assert.notEqual(M.batteryGlyph(95, false), M.batteryGlyph(5, false));
  assert.equal(M.batteryGlyph(5, true), M.batteryGlyph(95, true));
  assert.ok(M.batteryGlyph(null, false).length > 0);
});

test("gain text keeps a sign and does not dress up zero", () => {
  assert.equal(M.gainText(0), "0");
  assert.equal(M.gainText(6), "+6");
  assert.equal(M.gainText(-3), "-3");
  assert.equal(M.gainText(null), "");
});

test("a zero value is shown, not mistaken for absent", () => {
  const quiet = structuredClone(xm5);
  quiet.state.ambient_level = 0;
  quiet.state.eq_bands = [0, 0, 0, 0, 0];
  quiet.state.noise = "ambient";
  assert.equal(M.rowState({ id: "ambient_level" }, quiet).value, 0);
  assert.equal(M.noiseSummary(quiet), "Ambient sound 0/20");
  assert.equal(M.gainText(quiet.state.eq_bands[0]), "0");
});

test("noise summary names each mode", () => {
  const at = (noise, level) => M.noiseSummary({ state: { noise, ambient_level: level } });
  assert.equal(at("anc"), "Noise cancelling");
  assert.equal(at("ambient", 6), "Ambient sound 6/20");
  assert.equal(at("off"), "Noise control off");
  assert.equal(M.noiseSummary({ state: {} }), "");
});

test("the mode cycle wraps both ways", () => {
  assert.equal(M.nextNoiseMode("off", 1), "ambient");
  assert.equal(M.nextNoiseMode("anc", 1), "off");
  assert.equal(M.nextNoiseMode("off", -1), "anc");
  assert.equal(M.nextNoiseMode("nonsense", 1), "ambient");
});

test("earbuds report each bud rather than one number for both", () => {
  // One number for two buds that discharge differently is wrong about at least
  // one of them, and it is the lower one that decides when the music stops.
  const buds = {
    state: {
      battery: 88,
      battery_parts: { left: { level: 95, charging: false }, right: { level: 88, charging: false } },
    },
  };
  assert.equal(M.batteryText(buds), "L 95%  R 88%");
  assert.equal(M.barText(buds), "88%");
  const withCase = structuredClone(buds);
  withCase.state.battery_parts.case = { level: 40, charging: true };
  assert.ok(M.batteryText(withCase).includes("case 40%"));
  // A headset that reports one battery keeps saying so.
  assert.equal(M.batteryText({ state: { battery: 34 } }), "34%");
});

test("the bar falls back to bluez's battery when there is no session", () => {
  assert.equal(M.barText(xm5), "34%");
  assert.equal(M.barText({ state: {}, bluezBattery: 40 }), "40%");
  assert.equal(M.barText({ state: {} }), "");
});

test("hero meta explains a missing session instead of going blank", () => {
  assert.equal(M.heroMeta({ connected: false, error: "the control channel is busy" }),
    "the control channel is busy");
  assert.equal(M.heroMeta({ connected: false }), "Not connected");
  assert.ok(M.heroMeta(xm5).includes("34%"));
});

test("a band edit restates every band, so none can drift", () => {
  const bands = plain(M.bandsWith(xm5.state, "eq_bands", 2, 5, 4, 1));
  assert.equal(bands.length, 5);
  assert.deepEqual(bands, [0, 5, 4, 7, 9]);
  assert.deepEqual(plain(M.bandsWith({}, "eq_bands", 0, 5, 99, 1)), [10, 0, 0, 0, 0]);
  assert.deepEqual(plain(M.bandsWith({}, "eq_bands", 0, 5, -99, 1)), [-10, 0, 0, 0, 0]);
});

test("the ten-band equaliser gets ten bands and half-decibel steps", () => {
  // Two equalisers with different band counts, and the panel should not have to
  // know which one it is drawing.
  const host = { eq_gains: [6, 4, 0, 0, -2, 0, 2, 3, 4, 2] };
  const out = plain(M.bandsWith(host, "eq_gains", 3, 10, 1.4, 0.5));
  assert.equal(out.length, 10);
  assert.equal(out[3], 1.5);
  assert.deepEqual(plain(M.bandsWith({}, "eq_gains", 0, 10, 0, 0.5)).length, 10);
});

test("both equalisers carry their own bands in the spec", () => {
  const rowIn = (section, id) =>
    M.SECTIONS.find((s) => s.id === section).rows.find((r) => r.id === id);
  const sony = plain(rowIn("equalizer", "eq").bands);
  assert.equal(sony.labels.length, 5);
  assert.equal(sony.extra.feature, "eq_clear_bass");
  const host = plain(rowIn("host_equaliser", "eq_gains").bands);
  assert.equal(host.labels.length, 10);
  assert.equal(host.extra, undefined);
});

test("an unknown preset reported by a device is still offered", () => {
  const odd = structuredClone(xm5);
  odd.state.eq_preset = "gaming";
  const values = plain(M.presetOptions(odd)).map((o) => o.value);
  assert.ok(values.includes("gaming"));
  assert.equal(M.presetOptions(xm5).length, M.PRESET_OPTIONS.length);
});

test("the device keeps its name even when the helper has none", () => {
  assert.equal(M.deviceName(xm5), "WH-1000XM5");
  assert.equal(M.deviceName({ device: { name: "  " } }), "Headset");
  assert.equal(M.deviceName({}), "Headset");
});

// A headset with no driver: everything BlueZ and PipeWire know, and nothing else.
const nothing = {
  connected: false,
  present: true,
  unsupported: true,
  device: { address: "3C:B0:ED:50:BC:9C", name: "Nothing Ear (open)", driver: "", channel: 0 },
  support: {},
  controls: {
    codec: { supported: true, writable: true, available: true },
    audio_mode: { supported: true, writable: true, available: true },
    microphone: { supported: true, writable: false, available: false },
  },
  state: { codec: "a2dp-sink", audio_mode: "a2dp", microphone: false },
  audio: {
    card: "bluez_card.3C_B0_ED_50_BC_9C",
    active_codec: "AAC",
    active_profile: "a2dp-sink",
    mode: "a2dp",
    best_listening: "a2dp-sink",
    headset_profile: "headset-head-unit",
    has_microphone: true,
    codecs: [
      { value: "a2dp-sink-sbc", label: "SBC" },
      { value: "a2dp-sink-sbc_xq", label: "SBC-XQ" },
      { value: "a2dp-sink", label: "AAC" },
    ],
  },
  bluezBattery: 100,
  pending: [],
  ignored: [],
  error: "",
};

test("a headset with no driver still gets a panel", () => {
  // It used to hide entirely, throwing away a battery, a codec and a microphone
  // that BlueZ and PipeWire knew about the whole time.
  assert.deepEqual(sectionIds(nothing), ["audio"]);
  assert.deepEqual(rowIds(nothing, "audio"), ["codec", "audio_mode", "microphone"]);
});

test("a headset with a driver gets both tiers", () => {
  const both = structuredClone(xm5);
  both.audio = structuredClone(nothing.audio);
  both.controls.codec = { supported: true, writable: true, available: true };
  both.controls.audio_mode = { supported: true, writable: true, available: true };
  both.controls.microphone = { supported: true, writable: true, available: false };
  assert.deepEqual(sectionIds(both), ["noise", "equalizer", "behaviour", "audio", "device"]);
});

test("the codec choices come from the headset, not from this file", () => {
  const options = plain(M.optionsFor({ id: "codec", optionsFrom: "codecs" }, nothing));
  assert.deepEqual(options.map((o) => o.label), ["SBC", "SBC-XQ", "AAC"]);
  assert.deepEqual(plain(M.optionsFor({ id: "codec", optionsFrom: "codecs" }, {})), []);
});

test("a row with fixed choices keeps them", () => {
  const mode = M.SECTIONS.find((s) => s.id === "audio").rows.find((r) => r.id === "audio_mode");
  assert.deepEqual(plain(M.optionsFor(mode, nothing)).map((o) => o.value), ["a2dp", "headset"]);
});

test("the hero reads battery from bluez and the codec from pipewire", () => {
  // Neither needs a control session, which is the point.
  const meta = M.heroMeta(nothing);
  assert.ok(meta.includes("100%"), meta);
  assert.ok(meta.includes("AAC"), meta);
  assert.equal(M.deviceName(nothing), "Nothing Ear (open)");
});

test("a headset with no driver is not lectured at", () => {
  // The panel shows the rows it has. A paragraph about which layer of Bluetooth
  // standardises what belongs in the README, not in a bar panel.
  assert.equal(typeof M.vendorNotice, "undefined");
});

test("the keyboard hint promises only keys this panel has", () => {
  assert.ok(!M.keyboardHint(nothing).includes("noise"));
  assert.ok(M.keyboardHint(nothing).includes("j k rows"));
  assert.ok(M.keyboardHint(xm5).includes("a noise"));
});

test("set commands are the wire format the helper parses", () => {
  assert.equal(M.setCommand({ noise: "anc" }), '{"set":{"noise":"anc"}}');
});

test("panel copy fits the width it is given, so no line wraps awkwardly", () => {
  // The panel is about 380 logical pixels wide. A description sits beside its
  // control and has roughly 40 characters before it wraps and shoves the switch
  // around; a reason or hint has the full width and roughly 54. These are the
  // budget, checked here because every previous fix for this was one string at a
  // time and the next person adding a row would not know the limit existed.
  const DESCRIPTION = 40;
  const FULL_WIDTH = 54;
  const over = [];
  for (const section of M.SECTIONS) {
    for (const row of section.rows) {
      if (row.description && row.description.length > DESCRIPTION) {
        over.push(`${row.id} description ${row.description.length} > ${DESCRIPTION}`);
      }
      for (const key of ["hint", "unavailableHint"]) {
        if (row[key] && row[key].length > FULL_WIDTH) {
          over.push(`${row.id} ${key} ${row[key].length} > ${FULL_WIDTH}`);
        }
      }
    }
  }
  assert.deepEqual(over, []);
});

test("a reason for one row is shown on that row and beats the standing hint", () => {
  // A single line at the foot of the panel cannot say which of nine settings it
  // is about, and it used to stay there through everything the user did next.
  const row = { id: "noise", unavailableHint: "Only applies in ambient mode" };
  const payload = {
    controls: { noise: { supported: true, writable: true, available: false } },
    refused: { noise: "This headset does not have this setting" }
  };
  const status = plain(M.rowState(row, payload));
  assert.equal(status.refused, "This headset does not have this setting");
  assert.equal(M.rowReason(row, status), "This headset does not have this setting");
});

test("with nothing refused the row falls back to why it is inactive", () => {
  const row = { id: "ambient_level", unavailableHint: "Only applies in ambient mode" };
  const payload = { controls: { ambient_level: { supported: true, writable: true, available: false } } };
  const status = plain(M.rowState(row, payload));
  assert.equal(status.refused, "");
  assert.equal(M.rowReason(row, status), "Only applies in ambient mode");
});

test("a row the headset reports but will not change says so", () => {
  const row = { id: "touch_sensor" };
  const payload = { controls: { touch_sensor: { supported: true, writable: false, available: true } } };
  const reason = M.rowReason(row, plain(M.rowState(row, payload)));
  assert.match(reason, /does not accept changes/);
});

test("a row with nothing wrong says nothing", () => {
  const row = { id: "noise", unavailableHint: "Only applies in ambient mode" };
  const payload = { controls: { noise: { supported: true, writable: true, available: true } } };
  assert.equal(M.rowReason(row, plain(M.rowState(row, payload))), "");
});

test("every reason the panel can show also fits the width", () => {
  const wide = { id: "x", unavailableHint: "" };
  const cases = [
    { controls: { x: { supported: true, writable: true, available: true } }, refused: {} },
    { controls: { x: { supported: true, writable: false, available: true } } },
    { controls: { x: { supported: true, writable: true, available: true } }, ignored: ["x"] }
  ];
  for (const payload of cases) {
    const reason = M.rowReason(wide, plain(M.rowState(wide, payload)));
    assert.ok(reason.length <= 54, `${reason.length}: ${reason}`);
  }
});

test("a readout is not told off for refusing changes it never accepted", () => {
  // Every readout is unwritable by definition, so the generic line would sit
  // under the firmware version of every headset.
  const row = { id: "firmware", kind: "readout" };
  const payload = { support: { firmware: true }, controls: {} };
  assert.equal(M.rowReason(row, plain(M.rowState(row, payload))), "");
});

test("what the panel shows starts with a capital, wherever it came from", () => {
  // The helper's strings double as journal lines and start lowercase; the
  // panel's own start capitalised. The case is settled where they are shown.
  assert.equal(M.sentence("the headset is not connected"), "The headset is not connected");
  assert.equal(M.sentence("This headset does not have this setting"),
               "This headset does not have this setting");
  assert.equal(M.sentence(""), "");
  assert.equal(M.sentence(null), "");
  const row = { id: "noise" };
  const payload = { controls: { noise: { supported: true, writable: true, available: true } },
                    refused: { noise: "the headset is not connected" } };
  assert.equal(M.rowReason(row, plain(M.rowState(row, payload))),
               "The headset is not connected");
});

test("the equaliser offers presets, from the list the helper reports", () => {
  // Ten bands is more knobs than anyone turns before hearing anything.
  const row = M.SECTIONS.find((s) => s.id === "host_equaliser")
    .rows.find((r) => r.id === "eq_preset_host");
  assert.ok(row);
  const payload = { equaliser: { eq_presets: ["Flat", "Bass", "Vocal"] }, audio: { codecs: ["AAC"] } };
  assert.deepEqual(plain(M.optionsFor(row, payload)), ["Flat", "Bass", "Vocal"]);
  // And a row that reads the audio instead is not disturbed by that.
  const codec = M.SECTIONS.find((s) => s.id === "audio").rows.find((r) => r.id === "codec");
  assert.deepEqual(plain(M.optionsFor(codec, payload)), ["AAC"]);
});

test("a standing hint is a warning, never a description of the row above it", () => {
  // Every row carrying a caption made the panel read as documentation. A hint
  // earns its place only by saying something the control cannot show: what
  // happens as a consequence. What a row is comes from its label.
  const hints = [];
  for (const section of M.SECTIONS) {
    for (const row of section.rows) if (row.hint) hints.push(`${row.id}: ${row.hint}`);
  }
  assert.equal(hints.length, 2, hints.join(" | "));
  for (const hint of hints) assert.match(hint, /selects Manual|reconnects/);
});
