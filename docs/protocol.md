# Sony MDR over Bluetooth, as read off a WH-1000XM5

Every byte here was captured from a WH-1000XM5 on firmware 2.5.1. Where a claim
comes from somewhere else it says so. The captured replies are the fixtures in
`tests/test_sony_mdr.py`, so a decoder that drifts from the hardware fails.

## Reaching the control channel

The headset advertises a vendor service over SDP whose RFCOMM channel carries the
control protocol:

| | |
| --- | --- |
| Service UUID (v2, XM5 and newer) | `956c7b26-d49a-4ba8-b03f-b17d393cb6e2` |
| Legacy UUID (older models) | `96cc203e-5068-46ad-b32d-e316f5e069ba` |
| Channel on this headset | 9, found by SDP, cached per address |

The UUID is not discoverable: the device advertises several vendor UUIDs and none
of them is the one that works. It has to be known in advance, which is what the
list above is for.

The channel must not be. BlueZ exposes no API for a remote service record and
Arch no longer ships `sdptool`, so `headset/sdp.py` speaks SDP itself over L2CAP
PSM 1: one ServiceSearchAttribute request for the 128-bit UUID, then the RFCOMM
channel lifted out of the ProtocolDescriptorList. Guessing instead is worse than
it looks: a wrong channel refuses only after a slow blocking connect, and a device
that has been walked channel by channel is reported to start refusing the right
one too.

No privilege is involved. An RFCOMM socket to a bonded device, and an L2CAP
socket for the SDP query, are both ordinary user operations.

## Message framing

```
3e <type> <seq> <length:4 big-endian> <payload...> <checksum> 3c
```

- `type` is `01` for an acknowledgement, `0c` and `0e` for the two command
  channels.
- `checksum` is a one-byte sum of everything between the header and itself.
- Every byte in that span that would collide with `3e`, `3c` or `3d` is escaped:
  `3d`, then the original masked with `ef`.

Acknowledgement discipline, which the protocol requires and which a UI will
otherwise break:

- One request may be on the wire at a time. The rest queue.
- The acknowledgement carries the sequence number the **next** command must use.
- A command arriving from the headset must be acknowledged with the complement
  of its own sequence number before it will send another. That acknowledgement
  is not optional: miss one and the headset stops announcing anything at all.
- An acknowledgement belongs to the frame that earned it, which after a
  retransmission is not the request that happens to be outstanding. Counting
  frames sent against acknowledgements received is the only way to tell.
- A reply can arrive before its acknowledgement. The queue has to wait for both,
  or the next request goes out with a sequence number the headset never handed
  back.

A request's reply is its payload type plus one: `66` asks, `67` answers, `68`
writes, `69` announces a change made on the headset itself. The announcement
matters: it is the only reason the panel notices the button on the earcup.

Records are told apart by their payload type **and the sub-byte they echo back**.
Speak-to-chat and pause-on-removal both answer as `f7`; matching on the type alone
routes one record's reply into the other's decoder, which declines it, and the
real feature then looks like one the headset does not have.

## The records this headset answers

Request, and the reply captured from the device:

| Feature | Request | Reply | Decoded |
| --- | --- | --- | --- |
| Init | `0c 00 00` | `01 00 03 00 20 16 00 00` | eight bytes, so protocol v2 |
| Firmware | `0c 04 02` | `05 02 05 32 2e 35 2e 31` | `2.5.1` |
| Battery | `0c 22 00` | `23 00 23 00` | 35%, not charging |
| Codec | `0c 12 02` | `13 02 10` | LDAC |
| Noise control | `0c 66 17` | `67 17 01 00 00 00 14` | off, focus off, ambient 20 |
| Equaliser | `0c 56 00` | `57 00 10 06 09 0a 0f 11 11 13` | preset Bright, clear bass -1, bands 0 5 7 7 9 |
| Speak-to-chat | `0c f6 0c` | `f7 0c 01 01` | off |
| Its sensitivity and timeout | `0c fa 0c` | `fb 0c 00 01` | auto, standard |
| Pause when taken off | `0c f6 01` | `f7 01 00` | on |
| Voice guidance | `0e 46 01` | `47 01 00 01` | on |
| DSEE | `0c e6 02` | `e7 02 00` | off |
| Touch panel | `0c d6 d2` | `d7 d2 00 00` | on; the state is the **last** byte |

Asked and never answered on this firmware: volume (`a6`), multipoint
(`96 06`), service link (`b6`), the noise cancelling optimiser (`86`) and the
auto-power-off timer (`26`). A request that is acknowledged and then met with
silence is how an unsupported feature announces itself, and it is what
`headset/session.py` reports as `silent`.

## Writes

```
Noise control   68 17 01 <on> <ambient> <focus> <level>
Equaliser       58 00 a0 06 <clear bass+10> <five bands+10>
Equaliser preset 58 00 <preset> 00
Speak-to-chat   f8 0c <0 if on else 1> 01
Its config      fc 0c <sensitivity> <timeout>
Pause on removal f8 01 <0 if on else 1>
Voice guidance  0e 48 01 <0 if on else 1>
DSEE            e8 02 <1 if on else 0>
Touch panel     d8 d2 00 <0 if on else 1>   (acknowledged and ignored)
Power off       24 03 01
```

Three rules that are easy to get wrong:

**Polarity is inverted for the shared on/off value.** Zero is on. It does not
apply to noise control's own enable byte, and the charging flag is an enum where
one means charging. Both are confirmed above: speak-to-chat read `01` and was
off, pause-on-removal read `00` and was on, and the battery read `00` while not
charging.

**One message carries several settings.** Mode, ambient level and focus on voice
are one frame, so any write restates all three. Clear bass is band zero of the
equaliser array, in the same message as the bands: sending the bands alone
re-sends a stale clear bass, and it drifts a step every time a band is touched.

**Ambient level and focus on voice are discarded outside ambient mode.** The
headset takes them and does nothing. The panel offers them only in ambient mode.

## An acknowledgement is not agreement

The headset acknowledges writes it intends to discard, and a session cannot read
back its own write: it keeps being told the old value for up to a minute while
the hardware has already changed. A press of the button on the earcup, by
contrast, is announced within a second or two.

So there is only one honest test, and `tools/verify.py` is it: read the value,
write a different one, drop the session, reconnect, read it again, restore.

```
$ tools/verify.py AC:80:0A:44:B3:93
noise                      HONOURED  {'noise': 'off'} -> {'noise': 'anc'}
ambient_level              HONOURED  {'ambient_level': 20} -> {'ambient_level': 8}
focus_on_voice             HONOURED  {'focus_on_voice': False} -> {'focus_on_voice': True}
eq_bands                   HONOURED  [0, 5, 7, 7, 9] -> [2, -3, 4, -1, 6]
eq_clear_bass              HONOURED  {'eq_clear_bass': -1} -> {'eq_clear_bass': 5}
eq_preset                  HONOURED  {'eq_preset': 'manual'} -> {'eq_preset': 'vocal'}
speak_to_chat              HONOURED  {'speak_to_chat': False} -> {'speak_to_chat': True}
speak_to_chat_sensitivity  HONOURED  {'...': 'auto'} -> {'...': 'high'}
speak_to_chat_timeout      HONOURED  {'...': 'standard'} -> {'...': 'long'}
pause_on_removal           HONOURED  {'pause_on_removal': True} -> {'pause_on_removal': False}
voice_guidance             HONOURED  {'voice_guidance': True} -> {'voice_guidance': False}
dsee                       HONOURED  {'dsee': False} -> {'dsee': True}
touch_sensor               IGNORED   {'touch_sensor': True}    {'touch_sensor': True}

restored to the original reading
```

Twelve of thirteen. The touch panel is acknowledged and discarded, so the panel
shows it read-only. Note that the equaliser preset **is** honoured here, which
two other projects report as ignored on the XM5; theirs may be a different
firmware, and this is what 2.5.1 does.

## Sessions and disconnects

One control session exists at a time, across every host the headset is connected
to. With multipoint on and a phone attached, connecting fails with `EBUSY` or a
timeout while audio keeps playing perfectly, and Sony's app does not need to be
open for the phone to be holding it. A failed connect must close its own
half-open socket, or the next attempt fails against the leftovers rather than
against the headset.

Nothing in the protocol says the headset has been switched off: a session keeps
answering from its own cache. Only BlueZ knows, which is why device presence
comes from `Quickshell.Bluetooth` and not from the session.

## Diagnostics

```sh
headsetctl devices
headsetctl status --pretty
headsetctl set noise ambient
python3 tools/verify.py <address> [control ...]
omarchy-shell io.github.itsgg.headset status
journalctl --user -u '*' | grep omarchy-headset
```

# Google Fast Pair Message Stream

The one cross-vendor channel. A WH-1000XM5 and a Nothing Ear (open) both
advertise it, which is the strongest available evidence that it is real rather
than a Google-only path.

| | |
| --- | --- |
| Service UUID | `df21fe2c-2515-4fdb-8886-f12c4d67927c` |
| Channel on a Nothing Ear (open) | 17, found by the same SDP query as everything else |
| Framing | `<group:1> <code:1> <length:2 big-endian> <data>` |
| Acknowledgement | none, no sequence number, no checksum |
| Authentication | none. The HMAC and nonce were removed from the specification in April 2024 |

The device volunteers what it knows as soon as the channel opens and answers
nothing on demand, so support is established by listening rather than by asking.
Captured from a Nothing Ear (open) within a second of connecting:

```
03 01 0003 fc 3a af      device information, model id
03 02 0006 78 72 ...     device information, BLE address
03 03 0003 64 64 7f      device information, battery: left 100%, right 100%, no case reading
```

Battery is one byte per part: bit seven is charging, the low seven bits are a
percentage, `0x7f` means unknown and `0xff` in the case slot means there is no
case. Google's own worked example, `0303000357417f`, decodes to left 87%, right
65%, case unknown, and this implementation reproduces it byte for byte.

## Noise control, group 0x08

| Code | Direction | Meaning |
| --- | --- | --- |
| `0x11` | to the headset | ask for the noise state |
| `0x12` | to the headset | set it |
| `0x13` | from the headset | announce it |

Four bytes: a version, the modes the headset **has**, the modes it will let you
**set**, and the one it is **in**. The second and third bytes are the reason this
is worth implementing generically: the headset describes its own capabilities, so
no table of models is needed to know what to offer.

Google numbers the bits from the most significant, so bit 0 is `0x80`:

```
0x80 transparent   0x40 adaptive   0x20 off   0x10 reserved   0x08 ANC
```

Reading them the other way round is the obvious mistake and would offer modes the
headset does not have.

A Nothing Ear (open) does not answer group `0x08` at all, which is consistent
with it being an open-ear model with no active noise cancellation to control. The
capability probe therefore does not offer a noise row for it, which is the
behaviour wanted: the headset decides, not a table here.

**Reading this is implemented; setting it is not.** The specification's Set
request is a seeker version byte followed by the control data, and it does not
say what the capability bytes should carry on the way in. No headset here has
noise cancelling to try it against: the Nothing Ear has none, and the Sony has a
driver of its own that already does it properly. Writing a frame of that shape
and hoping is how an untested guess ends up on somebody else's hardware. Enabling
it needs one headset with noise cancelling and no vendor driver, plus a run of
`tools/verify.py`.
