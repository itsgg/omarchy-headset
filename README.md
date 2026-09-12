# Headset for Omarchy

Control a Bluetooth headset from the [Omarchy](https://omarchy.org/) bar: noise
cancelling, ambient sound, the hardware equaliser, battery, and the behaviour
settings that normally need a phone. It speaks the headset's own protocol over
Bluetooth. No vendor app, no daemon to install, no root, no udev rule.

<img src="docs/panel.png" alt="The headset panel: noise control, equaliser and behaviour settings" width="380">

Built for the **Sony WH-1000XM5**, which is the headset every byte below was read
off. The architecture is not Sony-shaped: a driver says which questions a headset
answers and how to encode a change, and the panel draws whatever the driver
reports. Adding a headset is a module, not a redesign.

## Compatibility

- Omarchy with its Quickshell bar, tested on **4.0.3-1**.
- Python 3 and BlueZ, both already on the system. Standard library only: nothing
  to compile, nothing to pip install, no D-Bus bindings.
- **Sony WH-1000XM5**, verified on firmware 2.5.1. The driver also claims the
  XM4, XM6, WF-1000XM4/XM5, CH-720N, C700N and LinkBuds, which speak the same
  protocol; those are unverified, and anything the headset does not answer for
  simply does not appear in the panel.

## Install

```sh
omarchy plugin add https://github.com/itsgg/omarchy-headset.git --enable
```

That is all. The widget appears in the bar's right section as soon as a headset
it recognises is connected, and disappears when it is not.

## Controls

<img src="docs/bar.png" alt="The bar widget: a headset glyph and the battery level" width="300">

| Where | Action | Result |
| --- | --- | --- |
| Bar | Left click | Open or close the panel |
| Bar | Middle click | Noise cancelling on or off |
| Bar | Right click | Ambient sound on or off |
| Bar | Scroll | Ambient level, while in ambient mode |
| Panel | `a` / `t` | Noise cancelling / ambient |
| Panel | `j` `k` | Move between rows |
| Panel | `h` `l` | Adjust the row, or pick a band on the equaliser |
| Panel | `+` `-` | Change the value, including one equaliser band |
| Panel | `r` / `Esc` | Re-read the headset / close |

Bind the panel to a key:

```
bindd = SUPER, H, Headset, exec, omarchy-shell gg.headset toggle
```

`omarchy-shell gg.headset` also takes `open`, `close`, `toggleNoise`,
`cycleNoise`, `ambient`, `noise <off|ambient|anc>` and `status`.

## What this headset actually honours

An acknowledgement from a Sony headset means the message arrived, not that it was
obeyed, and a session cannot read back its own write for up to a minute. So every
control was established the only honest way: write it, drop the control session,
reconnect, read it again. `tools/verify.py` is that test, and this table is its
output on a WH-1000XM5 on firmware 2.5.1.

| Honoured | Ignored | Never answered |
| --- | --- | --- |
| Noise mode, ambient level, focus on voice | Touch panel | Volume |
| Equaliser preset, five bands, clear bass | | Multipoint |
| Speak-to-chat, its sensitivity and timeout | | Auto power off timer |
| Pause when taken off, voice guidance, DSEE | | Noise cancelling optimiser |

The touch panel is therefore shown read-only rather than as a switch that lies,
and nothing that never answered appears at all. On another model the panel will
differ, because the helper asks the headset rather than a table.

<img src="docs/behaviour.png" alt="Behaviour settings and the headset's own readouts" width="380">

## Settings

`omarchy bar set gg.headset`, or `barWidget.defaults` in `shell.json`:

| Setting | Default | What it does |
| --- | --- | --- |
| `sessionPolicy` | `hold` | A headset allows one control session. `hold` keeps it, so the bar always shows live settings. `on-demand` releases it after the panel closes, so a phone can take it back. |
| `idleSeconds` | `30` | How long `on-demand` waits before releasing. |

With multipoint on and a phone attached, the phone may be holding the session; the
panel says so rather than failing silently. Battery still shows, because that
comes from BlueZ and not from the session.

## From a terminal

```sh
headsetctl devices                  # connected headsets a driver claims
headsetctl status --pretty          # everything it reports, as JSON
headsetctl set noise ambient
headsetctl set ambient_level 6
headsetctl toggle speak_to_chat
headsetctl watch                    # JSON lines, commands on stdin
```

While the bar widget is running it owns the session, so these route through it
rather than fighting it for the headset.

## How it works

```
BarWidget.qml     the bar button and the panel
Model.js          the panel as data: one entry per row, and its labels
components/       one file per row kind, each renderable alone
headsetctl        entry point only
headset/
  framing.py        MDR message framing: escaping, checksum, decode
  sdp.py            which RFCOMM channel carries the control service
  session.py        one session, and the acknowledgement discipline it needs
  device.py         a headset, and what it answered for
  state.py          the rule that keeps the panel honest after a write
  server.py         one owner, many watchers, over a unix socket
  drivers/sony_mdr.py   every opcode, encode and decode
```

Three ideas hold it together:

**BlueZ decides which headset exists.** The widget reads Quickshell's own
Bluetooth service, so connect and disconnect are events rather than a poll, and
the helper is never started for a device that is not there. A control session
answers reads from its own cache long after the headset is switched off; only
BlueZ knows, so BlueZ is asked.

**One process owns the headset.** A bar widget is instantiated once per monitor,
and a headset allows one control session. The first `headsetctl watch` to win a
lock opens the session and serves state over a socket in `$XDG_RUNTIME_DIR`; the
rest follow. The owner outlives the widget that started it.

**A written value is shown, then checked.** Because the device reports the old
value back to the writer, a write is displayed at once and marked; the device's
reading is believed again only once it moves. A write that never lands is
surfaced rather than hidden, since "this control does nothing on your model" is
worth knowing.

See [docs/protocol.md](docs/protocol.md) for the wire format and the captured
bytes behind every decoder.

## Development

```sh
make check       # everything CI runs: tests, manifest, omarchy validate
make test        # Python, and Model.js under node
make lint        # Qt 6 qmllint with Quickshell's import alias
make reload      # install, clear the QML cache, restart the shell
python3 tools/verify.py <address>   # write, reconnect, read back
```

Two things worth knowing before you debug the wrong code, both learned the hard
way here. A plugin installed as a **symlink never hot-reloads**: the shell's file
watcher does not follow one. And Quickshell **caches compiled QML** in
`~/.cache/quickshell/qmlcache`, which a changed file does not always invalidate,
so `make reload` clears it and restarts.

The tests carry the bytes this headset actually sent, so a decoder that drifts
from the hardware fails rather than quietly showing the wrong thing.

## Removing it

```sh
omarchy plugin disable gg.headset   # keep it, take it off the bar
omarchy plugin remove gg.headset
```

It writes nothing outside `$XDG_RUNTIME_DIR/omarchy-headset`, which is tmpfs and
gone at logout, and `~/.cache/omarchy-headset`, which holds one number per
headset: the RFCOMM channel its control service answers on. It never changes your
BlueZ configuration. It does change settings on the headset, but only the ones you
change in the panel, and nothing until you do.

## Credits

Started as a rewrite of
[original-david-knight/omarchy-arctis-headset](https://github.com/original-david-knight/omarchy-arctis-headset),
whose panel shape, JSON-lines helper and general approach this keeps. That plugin
drives a SteelSeries Arctis Nova Pro Omni over USB HID; this one generalises the
idea behind it into a driver seam and implements Bluetooth instead. The USB
transport and its privileged udev installer are not carried over, which is why
nothing here crosses a privilege boundary.

Protocol knowledge, none of it code:

- [Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge)'s Sony
  headphone implementation, for the message framing and the V1/V2 payload tables.
- [mos9527/SonyHeadphonesClient](https://github.com/mos9527/SonyHeadphonesClient)
  and its `libmdr`, for the service UUIDs and device support notes.
- The Omarchy plugins that got here first and wrote down what they learned:
  [agusmoura](https://github.com/agusmoura/omarchy-sony-headphones)'s byte-level
  protocol reference, [delarosa1312](https://github.com/delarosa1312/omarchy-sony-xm5)'s
  catalogue of behavioural traps, [VyomJain6904](https://github.com/VyomJain6904/sony-headphones-linux)'s
  session-policy design, and [gabamnml](https://github.com/gabamnml/omarchy-sony-headphones)'s
  warning never to guess an RFCOMM channel.

MIT © [Ganesh Gunasegaran](https://itsgg.com). An independent community plugin,
not affiliated with Sony.
