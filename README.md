# Headset for Omarchy

Control a Bluetooth headset from the [Omarchy](https://omarchy.org/) bar. Every
headset gets battery, the codec carrying the audio, and the music-or-calls
trade-off that silently halves your audio quality when you join a meeting. A
headset with a driver gets its noise cancelling, equaliser and behaviour settings
as well, spoken in that manufacturer's own protocol.

No vendor app, no daemon to install, no root, no udev rule.

<img src="docs/panel.png" alt="The panel on a WH-1000XM5: noise control, the headset's own equaliser and its behaviour settings" width="380">

## Three tiers, and why

Bluetooth standardises the audio, not the headset. There is no vendor-neutral way
to reach the noise cancelling or the equaliser inside a pair of headphones, which
I checked rather than assumed: nothing in PipeWire's Bluetooth plugin relates to
either, and the single "Equalizer" string in `bluetoothd` is an AVRCP player
setting pointing the other way, at your music player, on or off, no bands. An
equaliser can still be run on this end of the link, and is.

So the panel is built in three tiers, and takes the best one the headset offers.

| | What you get | Where it comes from |
| --- | --- | --- |
| **Every headset** | Battery, the codec in use and the choice of codec, music-or-calls mode, microphone, a ten-band equaliser | BlueZ and PipeWire, no vendor protocol |
| **Fast Pair** | Battery per earbud and per case, and noise control where the headset has it | Google's Message Stream, which unrelated manufacturers implement |
| **Vendor driver** | Ambient sound, equaliser, speak-to-chat, pause on removal, voice guidance, DSEE, power off | The manufacturer's own protocol |

The middle tier is the interesting one. Google's Fast Pair Message Stream is a
plain RFCOMM service on a fixed UUID, and a Sony WH-1000XM5 and a Nothing Ear
(open) both advertise it, which is two unrelated manufacturers speaking one
protocol. It carries per-earbud battery, which no standard Bluetooth path can
give, and a noise-control extension whose first bytes are the headset telling
you which modes it has and which you may set. As far as I can establish this is
the first open implementation of that extension on Linux.

One vendor driver ships, Sony MDR over RFCOMM, and every byte of it was read off
a WH-1000XM5. A headset with no driver still gets a useful panel rather than
nothing, which is the point of the split.

## The equaliser

Bluetooth standardises no equaliser, so a headset without a vendor protocol has
none this machine can reach. It can have one anyway. PipeWire will run a filter
chain, and the audio can be sent through it on the way out, so the processing
happens here rather than in the earcups. That is the only difference that
matters, and it works on anything.

Ten bands an octave apart, 32 Hz to 16 kHz, plus or minus 10 dB in half-decibel
steps, and five named curves to start from: Flat, Bass, Vocal, Treble, Podcast.
The name is read back off the bands rather than remembered beside them, so
moving one band says Custom and a preset can never claim a sound the headset is
not making.

Boosting bands and passing the result on unchanged is how a filter chain clips,
so a trim at the end takes the boost back and the loudest thing through the
chain is no louder than it went in. The trim is measured rather than
guessed: neighbouring peaking filters overlap, and two bands raised 10 dB each
sum to 13 dB between them, so taking back the tallest band alone would still
clip. It is measured at both 44.1 and 48 kHz and the louder wins, because
PipeWire recomputes the filters at whatever the graph negotiated.

It is offered only where the headset has no equaliser of its own. A Sony has one
in the earcups, and two equalisers in one panel is a question nobody should have
to answer.

The curve is remembered per headset and comes back when the headset does, which
matters because a reconnect is the normal case rather than the exception. The
chain runs as its own short-lived PipeWire client from a private directory under
`$XDG_RUNTIME_DIR`. Nothing of your PipeWire configuration is read, written or
replaced, and everything it creates is gone at logout however it shut down.

## Compatibility

- Omarchy with its Quickshell bar, tested on **4.0.3-1**.
- Python 3 and BlueZ, both already on the system. Standard library only: nothing
  to compile, no packages to add, no D-Bus bindings.
- PipeWire, for the codec and profile rows and for the equaliser this machine
  runs. The equaliser also needs PipeWire's filter-chain module, which is part
  of the stock `pipewire` package on Arch and is at
  `/usr/share/pipewire/filter-chain.conf`. Where it is missing the equaliser is
  not offered, and nothing else changes.
- **Any** Bluetooth headset, for the first tier. Battery needs the headset to
  report it over the hands-free profile, which most do and the cheapest do not.
- Sony headsets speaking the **v2** MDR protocol, for the driver tier. Every
  record is written for v2, so this list is what the evidence supports and no
  more:

| Model | Basis |
| --- | --- |
| **WH-1000XM5** | Verified on hardware, firmware 2.5.1. Every table in this README came off it. |
| WF-1000XM4, WF-1000XM5, LinkBuds, LinkBuds S | v2 by their handshake replies in Gadgetbridge's captured list |
| WH-1000XM6, WH-CH720N | v2 per `gabamnml/omarchy-sony-headphones`, which refuses them for that reason |

The **WH-1000XM4, XM3 and XM2 are deliberately not claimed**: they speak the
older v1 protocol, which this driver does not implement. The letter matters,
because the WF-1000XM4 is v2 and the WH-1000XM4 is not. A headset that answers
the handshake as v1 is told so plainly rather than being given a panel that
stays empty for no stated reason.

Everything but the XM5 is unverified by me. The panel is built from what the
headset answers rather than from a table, so an unverified model shows the rows
it actually supports and nothing it does not. Reports welcome either way.

A headset with no driver is not a headset with an empty panel. This is a Nothing
Ear (open), which has no vendor driver here at all. The per-earbud battery comes
from Fast Pair and the rest from PipeWire:

<img src="docs/no-driver.png" alt="A headset with no driver: battery, codec, mode and microphone" width="340">

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
| Panel | Scroll | Change a band, on either equaliser |
| Panel | `r` / `Esc` | Re-read the headset / close |

Bind the panel to a key:

```
bindd = SUPER, H, Headset, exec, omarchy-shell io.github.itsgg.headset toggle
```

`omarchy-shell io.github.itsgg.headset` also takes `open`, `close`, `toggleNoise`,
`cycleNoise`, `ambient`, `noise <off|ambient|anc>` and `status`, which answers
with one line the way Omarchy's own plugins do. For the whole state as JSON,
use `headsetctl status --pretty`.

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

`omarchy bar set io.github.itsgg.headset`, or `barWidget.defaults` in `shell.json`:

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
make check       # everything CI runs: tests, lint, manifest, omarchy validate
make test        # Python, the same suite with nothing installed, and Model.js
make lint        # Qt 6 qmllint with Quickshell's import alias
make reload      # install, clear the QML cache, restart the shell
python3 tools/verify.py <address>   # write, reconnect, read back
python3 tools/shots.py <name>       # docs/<name>.png, cropped to the panel
```

Two things worth knowing before you debug the wrong code, both learned the hard
way here. A plugin installed as a **symlink never hot-reloads**: the shell's file
watcher does not follow one. And Quickshell **caches compiled QML** in
`~/.cache/quickshell/qmlcache`, which a changed file does not always invalidate,
so `make reload` clears it and restarts.

The tests carry the bytes this headset actually sent, so a decoder that drifts
from the hardware fails rather than quietly showing the wrong thing.

`make test` runs the Python suite twice, the second time with Bluetooth,
PipeWire, `pactl` and every subprocess taken away. CI has broken twice on a test
that quietly needed the machine it was written on, once on `socket.AF_BLUETOOTH`
and once on PipeWire's filter-chain config, and both times it passed here and
failed there.

`make lint` is in `make check` because it was not, and a widget that could not
load at all once passed a green check: every Python and JavaScript test passes
on QML the shell then refuses. The linter fails on an assignment to a property
that does not exist on one of these components, which is that failure, while
still ignoring members of the objects the shell injects at load time, which it
cannot see into.

## Removing it

```sh
omarchy plugin disable io.github.itsgg.headset   # keep it, take it off the bar
omarchy plugin remove io.github.itsgg.headset
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
