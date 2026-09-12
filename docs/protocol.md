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
omarchy-shell gg.headset status
journalctl --user -u '*' | grep omarchy-headset
```
