"""Sony MDR, protocol v2: the WH-1000XM5 and its relatives.

Every opcode and every layout below was read off a WH-1000XM5 on firmware 2.5.1
rather than copied from a table. The captured replies are the test fixtures in
`tests/test_sony_mdr.py`, so a decode that drifts from the hardware fails.

Two conventions to hold on to, because getting either backwards is the usual bug:

    Polarity   most on/off settings send and report zero for ON. Noise control's
               own enable byte and the charging flag do not: there, one is on.
    Records    a reply's payload type is the request's plus one, so 0x66 asks and
               0x67 answers. A write is the request plus two: 0x68.
"""
from __future__ import annotations

import struct

from .spec import COMMAND_1, COMMAND_2, Control, Driver, Record

SERVICE_UUID = "956c7b26-d49a-4ba8-b03f-b17d393cb6e2"
INIT = bytes((0x00, 0x00))

# Model names this driver claims. Matching on a substring rather than an exact name
# is deliberate: bluez reports "WH-1000XM5" but an alias may carry anything around it.
MODELS = ("WH-1000XM5", "WH-1000XM6", "WF-1000XM5", "WH-1000XM4", "WF-1000XM4",
          "WH-CH720N", "WF-C700N", "LinkBuds")

NOISE_OFF, NOISE_ANC, NOISE_AMBIENT = "off", "anc", "ambient"
AMBIENT_MIN, AMBIENT_MAX = 0, 20
BAND_MIN, BAND_MAX = -10, 10
BAND_COUNT = 5
EQ_MANUAL_PRESET = 0xA0

CODECS = {0x00: "unknown", 0x01: "SBC", 0x02: "AAC", 0x10: "LDAC", 0x20: "aptX", 0x21: "aptX HD"}

EQ_PRESETS = {
    0x00: "off", 0x10: "bright", 0x11: "excited", 0x12: "mellow", 0x13: "relaxed",
    0x14: "vocal", 0x15: "treble", 0x16: "bass", 0x17: "speech",
    0xA0: "manual", 0xA1: "custom 1", 0xA2: "custom 2",
}
EQ_PRESET_CODES = {name: code for code, name in EQ_PRESETS.items()}

SENSITIVITIES = {0x00: "auto", 0x01: "high", 0x02: "low"}
SENSITIVITY_CODES = {name: code for code, name in SENSITIVITIES.items()}
TIMEOUTS = {0x00: "short", 0x01: "standard", 0x02: "long", 0x03: "off"}
TIMEOUT_CODES = {name: code for code, name in TIMEOUTS.items()}

# The noise record's second byte. 0x15 is the older layout; this device answers only
# 0x17 and ignores 0x15 entirely, which is how the right one was established.
NOISE_VARIANT = 0x17


def _switch_on(byte: int) -> bool:
    """Zero means on for every setting that uses the shared on/off value."""
    return byte == 0x00


def _switch_byte(enabled: bool) -> int:
    return 0x00 if enabled else 0x01


def _clamp(value, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _pick(value, feature: str, state: dict, default=None):
    """The value for one feature, whether it arrived alone or in a group.

    Writes are grouped by the message they share, so an encoder is handed a dict
    even when its message carries a single feature. Reading that dict as the value
    is how a switch ends up sending "on" for every write, because a non-empty dict
    is truthy. Every scalar encoder goes through here.
    """
    if isinstance(value, dict):
        if feature in value:
            return value[feature]
        return state.get(feature, default)
    return value


# --------------------------------------------------------------------- decoders


def decode_firmware(payload: bytes) -> dict | None:
    # 05 02 <length> <ascii>. The declared length has to be there: a frame cut
    # short otherwise reports a firmware of "2" and looks like a successful read.
    if len(payload) < 4 or payload[1] != 0x02:
        return None
    length = payload[2]
    if length == 0 or len(payload) < 3 + length:
        return None
    text = payload[3:3 + length].decode("ascii", "replace").strip("\x00")
    return {"firmware": text} if text else None


def decode_battery(payload: bytes) -> dict | None:
    # 23 00 <level> <charging>; charging is an enum where 1 means charging.
    if len(payload) < 4 or payload[1] != 0x00:
        return None
    level = payload[2]
    if level > 100:
        return None
    return {"battery": level, "charging": payload[3] == 0x01}


def decode_codec(payload: bytes) -> dict | None:
    if len(payload) < 3 or payload[1] != 0x02:
        return None
    return {"codec": CODECS.get(payload[2], f"0x{payload[2]:02x}")}


def decode_noise(payload: bytes) -> dict | None:
    """67 17 01 <enabled> <ambient> [wind] <focus> <level>."""
    if payload[1:2] and payload[1] not in (0x15, 0x17, 0x22):
        return None
    # The 0x17 layout is at least seven bytes. Accepting six read the level from
    # the wrong offset and turned a truncated frame into a plausible lie: ambient
    # sound at zero when the headset had said twenty.
    minimum = 7 if payload[1:2] == b"\x17" else 6
    if len(payload) < minimum or len(payload) > 8:
        return None
    has_wind = payload[1] == 0x17 and len(payload) > 7
    if payload[3] == 0x00:
        mode = NOISE_OFF
    elif payload[3] == 0x01:
        if has_wind and payload[5] in (0x03, 0x05):
            mode = "wind"
        else:
            mode = NOISE_AMBIENT if payload[4] == 0x01 else NOISE_ANC
    else:
        return None
    level = payload[-1]
    if level > AMBIENT_MAX:
        return None
    return {
        "noise": mode,
        "focus_on_voice": payload[-2] == 0x01,
        "ambient_level": level,
        "noise_variant": payload[1],
    }


def decode_equalizer(payload: bytes) -> dict | None:
    """57 00 <preset> <count> <values>. With six values the first is clear bass."""
    if len(payload) < 4 or payload[1] != 0x00:
        return None
    count = payload[3]
    values = payload[4:4 + count]
    if len(values) != count or count not in (6, 10):
        return None
    if count == 6:
        clear_bass, bands = values[0] - 10, [value - 10 for value in values[1:]]
    else:
        clear_bass, bands = None, [value - 6 for value in values]
    # A corrupt frame decoded to gains of hundreds of decibels, because this was
    # the one decoder that trusted its arithmetic instead of checking the range.
    if clear_bass is not None and not (BAND_MIN <= clear_bass <= BAND_MAX):
        return None
    if any(not (BAND_MIN <= band <= BAND_MAX) for band in bands):
        return None
    return {
        "eq_preset": EQ_PRESETS.get(payload[2], f"0x{payload[2]:02x}"),
        "eq_clear_bass": clear_bass,
        "eq_bands": bands,
    }


def decode_speak_to_chat(payload: bytes) -> dict | None:
    if len(payload) < 4 or payload[1] != 0x0C:
        return None
    return {"speak_to_chat": _switch_on(payload[2])}


def decode_speak_to_chat_config(payload: bytes) -> dict | None:
    if len(payload) < 4 or payload[1] != 0x0C:
        return None
    sensitivity = SENSITIVITIES.get(payload[2])
    timeout = TIMEOUTS.get(payload[3])
    if sensitivity is None or timeout is None:
        return None
    return {"speak_to_chat_sensitivity": sensitivity, "speak_to_chat_timeout": timeout}


def decode_pause_on_removal(payload: bytes) -> dict | None:
    if len(payload) < 3 or payload[1] != 0x01:
        return None
    return {"pause_on_removal": _switch_on(payload[2])}


def decode_voice_guidance(payload: bytes) -> dict | None:
    if len(payload) < 4 or payload[1] != 0x01:
        return None
    return {"voice_guidance": _switch_on(payload[2])}


def decode_dsee(payload: bytes) -> dict | None:
    if len(payload) < 3 or payload[1] != 0x02:
        return None
    return {"dsee": payload[2] == 0x01}


def decode_touch_sensor(payload: bytes) -> dict | None:
    # d7 d2 00 <state>. The state is the last byte; index 2 is a constant, and
    # reading it made this always report the touch panel as on.
    if len(payload) < 4 or payload[1] != 0xD2:
        return None
    return {"touch_sensor": _switch_on(payload[3])}


def identify(payload: bytes) -> dict:
    """The init reply says which protocol generation this is by its length."""
    if len(payload) == 8:
        return {"protocol": "v2"}
    if len(payload) == 4:
        return {"protocol": "v1"}
    return {"protocol": "unknown"}


# ---------------------------------------------------------------------- encoders


def encode_noise(value, state: dict) -> tuple[int, bytes]:
    """One message carries the mode, focus-on-voice and the ambient level.

    The caller passes a dict of the parts it wants changed; the rest come from the
    last reading, because a partial write would zero whatever it omitted.
    """
    wanted = dict(value) if isinstance(value, dict) else {"noise": value}
    mode = wanted.get("noise", state.get("noise", NOISE_OFF))
    if mode not in (NOISE_OFF, NOISE_ANC, NOISE_AMBIENT):
        raise ValueError("noise must be off, anc or ambient")
    focus = bool(wanted.get("focus_on_voice", state.get("focus_on_voice", False)))
    level = _clamp(wanted.get("ambient_level", state.get("ambient_level", AMBIENT_MAX)),
                   AMBIENT_MIN, AMBIENT_MAX)
    variant = state.get("noise_variant") or NOISE_VARIANT
    return COMMAND_1, bytes((
        0x68,
        variant,
        0x01,
        0x00 if mode == NOISE_OFF else 0x01,
        0x01 if mode == NOISE_AMBIENT else 0x00,
        0x01 if focus else 0x00,
        level,
    ))


def encode_equalizer(value, state: dict) -> tuple[int, bytes]:
    """58 00 a0 06 <clear bass> <five bands>, every value offset by ten.

    Clear bass travels in the same message as the bands and must be restated even
    when only a band changed, or it drifts a step every time a band is touched.
    """
    wanted = dict(value) if isinstance(value, dict) else {"eq_bands": value}
    bands = list(wanted.get("eq_bands", state.get("eq_bands") or [0] * BAND_COUNT))
    if len(bands) != BAND_COUNT:
        raise ValueError(f"the equaliser needs exactly {BAND_COUNT} bands")
    clear_bass = wanted.get("eq_clear_bass", state.get("eq_clear_bass"))
    if clear_bass is None:
        clear_bass = 0
    values = [_clamp(clear_bass, BAND_MIN, BAND_MAX)] + [_clamp(band, BAND_MIN, BAND_MAX) for band in bands]
    return COMMAND_1, bytes((0x58, 0x00, EQ_MANUAL_PRESET, len(values))) + bytes(v + 10 for v in values)


def encode_eq_preset(value, state: dict) -> tuple[int, bytes]:
    code = EQ_PRESET_CODES.get(str(_pick(value, "eq_preset", state)))
    if code is None:
        raise ValueError(f"unknown equaliser preset {value!r}")
    return COMMAND_1, bytes((0x58, 0x00, code, 0x00))


def encode_speak_to_chat(value, state: dict) -> tuple[int, bytes]:
    enabled = bool(_pick(value, "speak_to_chat", state, False))
    return COMMAND_1, bytes((0xF8, 0x0C, _switch_byte(enabled), 0x01))


def encode_speak_to_chat_config(value, state: dict) -> tuple[int, bytes]:
    wanted = dict(value) if isinstance(value, dict) else {}
    sensitivity = SENSITIVITY_CODES.get(
        str(wanted.get("speak_to_chat_sensitivity", state.get("speak_to_chat_sensitivity", "auto"))))
    timeout = TIMEOUT_CODES.get(
        str(wanted.get("speak_to_chat_timeout", state.get("speak_to_chat_timeout", "standard"))))
    if sensitivity is None or timeout is None:
        raise ValueError("unknown speak-to-chat sensitivity or timeout")
    return COMMAND_1, bytes((0xFC, 0x0C, sensitivity, timeout))


def encode_pause_on_removal(value, state: dict) -> tuple[int, bytes]:
    enabled = bool(_pick(value, "pause_on_removal", state, False))
    return COMMAND_1, bytes((0xF8, 0x01, _switch_byte(enabled)))


def encode_voice_guidance(value, state: dict) -> tuple[int, bytes]:
    enabled = bool(_pick(value, "voice_guidance", state, False))
    return COMMAND_2, bytes((0x48, 0x01, _switch_byte(enabled)))


def encode_dsee(value, state: dict) -> tuple[int, bytes]:
    enabled = bool(_pick(value, "dsee", state, False))
    return COMMAND_1, bytes((0xE8, 0x02, 0x01 if enabled else 0x00))


def encode_touch_sensor(value, state: dict) -> tuple[int, bytes]:
    # d8 d2 00 <off?1:0>. The two trailing bytes are the other way round on the
    # older protocol, which is a good way to write a frame the device ignores.
    enabled = bool(_pick(value, "touch_sensor", state, False))
    return COMMAND_1, bytes((0xD8, 0xD2, 0x00, _switch_byte(enabled)))


def encode_power_off(value, state: dict) -> tuple[int, bytes]:
    return COMMAND_1, bytes((0x24, 0x03, 0x01))


# ----------------------------------------------------------------------- driver

RECORDS = (
    Record("firmware", bytes((0x04, 0x02)), decode_firmware, ("firmware",)),
    Record("battery", bytes((0x22, 0x00)), decode_battery, ("battery", "charging"), poll_seconds=120.0),
    Record("codec", bytes((0x12, 0x02)), decode_codec, ("codec",)),
    Record("noise", bytes((0x66, NOISE_VARIANT)), decode_noise,
           ("noise", "focus_on_voice", "ambient_level")),
    Record("equalizer", bytes((0x56, 0x00)), decode_equalizer,
           ("eq_preset", "eq_bands", "eq_clear_bass")),
    Record("speak_to_chat", bytes((0xF6, 0x0C)), decode_speak_to_chat, ("speak_to_chat",)),
    Record("speak_to_chat_config", bytes((0xFA, 0x0C)), decode_speak_to_chat_config,
           ("speak_to_chat_sensitivity", "speak_to_chat_timeout")),
    Record("pause_on_removal", bytes((0xF6, 0x01)), decode_pause_on_removal, ("pause_on_removal",)),
    Record("voice_guidance", bytes((0x46, 0x01)), decode_voice_guidance, ("voice_guidance",),
           message_type=COMMAND_2),
    Record("dsee", bytes((0xE6, 0x02)), decode_dsee, ("dsee",)),
    Record("touch_sensor", bytes((0xD6, 0xD2)), decode_touch_sensor, ("touch_sensor",)),
)


def _ambient_mode(state: dict) -> bool:
    return state.get("noise") == NOISE_AMBIENT


CONTROLS = (
    Control("noise", encode_noise, record="noise", key="noise"),
    Control("ambient_level", encode_noise, record="noise", key="noise", available=_ambient_mode),
    Control("focus_on_voice", encode_noise, record="noise", key="noise", available=_ambient_mode),
    Control("eq_bands", encode_equalizer, record="equalizer", key="equalizer"),
    Control("eq_clear_bass", encode_equalizer, record="equalizer", key="equalizer"),
    # Its own key: a preset and a band curve are two different messages, and
    # grouping them together let one encoder swallow the other's values.
    Control("eq_preset", encode_eq_preset, record="equalizer", key="eq_preset"),
    Control("speak_to_chat", encode_speak_to_chat, record="speak_to_chat", key="speak_to_chat"),
    Control("speak_to_chat_sensitivity", encode_speak_to_chat_config,
            record="speak_to_chat_config", key="speak_to_chat_config"),
    Control("speak_to_chat_timeout", encode_speak_to_chat_config,
            record="speak_to_chat_config", key="speak_to_chat_config"),
    Control("pause_on_removal", encode_pause_on_removal, record="pause_on_removal", key="pause_on_removal"),
    Control("voice_guidance", encode_voice_guidance, record="voice_guidance", key="voice_guidance"),
    Control("dsee", encode_dsee, record="dsee", key="dsee"),
    # Verified on a WH-1000XM5 on firmware 2.5.1: the touch panel setting is
    # acknowledged, survives nothing, and reads back unchanged from a fresh session.
    # Offered read-only rather than as a switch that lies.
    Control("touch_sensor", encode_touch_sensor, record="touch_sensor",
            key="touch_sensor", honoured=False),
    Control("power_off", encode_power_off, record="battery", key="power_off"),
)


def claims(name: str) -> bool:
    text = str(name or "").upper().replace("_", "-")
    return any(model.upper() in text for model in MODELS)


DRIVER = Driver(
    id="sony-mdr",
    name="Sony",
    service_uuid=SERVICE_UUID,
    init=INIT,
    records=RECORDS,
    controls=CONTROLS,
    claims=claims,
    identify=identify,
)
