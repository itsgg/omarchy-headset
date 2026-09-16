import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import Quickshell.Bluetooth
import Quickshell.Services.Pipewire
import qs.Commons
import qs.Ui
import "Model.js" as Model
import "components"

// A connected Bluetooth headset in the bar.
//
// Three parts, each with one job. bluez decides which headset exists and whether
// it is connected, through Quickshell's own Bluetooth service. The `headsetctl`
// helper owns the single control session the headset allows and streams its state
// as JSON lines. This file draws whatever that state says the headset supports.
//
// Nothing here knows a Sony opcode. Adding a headset is a driver in the helper.
Panel {
  id: root
  moduleName: "io.github.itsgg.headset"
  ipcTarget: ""
  manageIpc: false

  // -------------------------------------------------------------- which headset
  //
  // bluez is already tracking every paired device, so asking it beats polling for
  // a device that may not be there. It also knows the battery level over the
  // standard profile, which is why the bar can show a percentage with no control
  // session at all.
  readonly property var bluetoothDevices: Bluetooth.devices ? Bluetooth.devices.values : []
  readonly property var headset: {
    var best = null
    for (var i = 0; i < bluetoothDevices.length; i++) {
      var device = bluetoothDevices[i]
      if (!device || !device.connected) continue
      var icon = String(device.icon || "")
      // Device class, not a name match: every headset and pair of headphones
      // reports one of these, and nothing else does.
      if (icon !== "audio-headset" && icon !== "audio-headphones") continue
      if (best === null) best = device
    }
    return best
  }
  readonly property string headsetAddress: headset ? String(headset.address || "") : ""
  readonly property string headsetName: headset ? String(headset.deviceName || headset.name || "") : ""
  readonly property int bluezBattery: headset && headset.batteryAvailable
    ? Math.round(headset.battery * 100) : -1

  // ------------------------------------------------------- the tier every headset has
  //
  // None of this is vendor protocol. BlueZ knows the battery, PipeWire knows the
  // codec and holds the microphone, and both know them for a headset no driver
  // has ever heard of. A vendor driver adds noise control and an equaliser on
  // top; it is the extra, not the price of entry.
  readonly property var pipewireNodes: Pipewire.nodes ? Pipewire.nodes.values : []

  function nodeForAddress(wantSink) {
    for (var i = 0; i < pipewireNodes.length; i++) {
      var node = pipewireNodes[i]
      if (!node || node.isStream || node.isSink !== wantSink) continue
      var props = node.properties || ({})
      if (String(props["api.bluez5.address"] || "") === root.headsetAddress) return node
    }
    return null
  }

  readonly property var sinkNode: root.headsetAddress === "" ? null : nodeForAddress(true)
  readonly property var sourceNode: root.headsetAddress === "" ? null : nodeForAddress(false)

  // Switching a profile tears these down and builds new ones, whoever did the
  // switching. Watching them is how the panel notices a call taking the headset
  // to call quality without anybody touching this widget.
  readonly property string audioShape:
    (root.sinkNode ? String(root.sinkNode.name || "") : "-") + "|"
    + (root.sourceNode ? String(root.sourceNode.name || "") : "-")
  onAudioShapeChanged: if (root.headsetAddress !== "") audioSettle.restart()
  readonly property bool microphoneMuted: sourceNode && sourceNode.audio ? sourceNode.audio.muted : false

  // The card, its profiles and which codec each one carries. PipeWire's QML API
  // exposes nodes but not cards, and profiles live on the card, so this one
  // reading comes from the helper.
  property var audioCard: ({ codecs: [], profiles: [], mode: "", active_codec: "",
                             active_profile: "", headset_profile: "", best_listening: "",
                             has_microphone: false, card: "" })
  property bool audioSwitching: false
  // The headset the running read or switch was started for, and a profile asked
  // for while it was busy. Without the first, a result from the headset you just
  // unplugged lands in the panel of the one you plugged in; without the second, a
  // choice made mid-read is silently dropped, because assigning `running = true`
  // to a process that is already running does nothing at all.
  property string audioFor: ""
  property string audioQueued: ""

  PwObjectTracker {
    objects: {
      var out = []
      if (root.sinkNode) out.push(root.sinkNode)
      if (root.sourceNode) out.push(root.sourceNode)
      return out
    }
  }

  // ------------------------------------------------------------------- helper IO
  //
  // resolvedUrl percent-encodes, so a plugin directory containing a space would
  // otherwise yield a path that cannot be executed. Resolving from this file also
  // means the widget survives being installed under a different id or cloned.
  readonly property string helperPath:
    decodeURIComponent(Qt.resolvedUrl("headsetctl").toString().replace(/^file:\/\//, ""))

  // What the helper is started with, rather than a copy of whatever this shell
  // was started with. The helper is launched by the bar, so a PATH or an
  // LD_PRELOAD that reached the shell would otherwise reach the interpreter that
  // runs the helper and everything the helper runs after it, and nobody would be
  // watching when it did. The helper pins its own interpreter in its `#!` line
  // and finds pactl, pipewire and bluetoothctl under /usr/bin; this is the other
  // half of that, and the two Processes below are the only things it applies to.
  //
  // Named one at a time, because a list of what to leave behind is only ever a
  // list of what somebody thought of. XDG_RUNTIME_DIR is where the control
  // socket and the equaliser live, XDG_CACHE_HOME and XDG_STATE_HOME are where
  // the helper remembers a headset between sessions, HOME is the fallback for
  // both, and the locale decides how text the helper prints is encoded. The
  // PULSE_ and PIPEWIRE_ pair are how a machine points its audio clients at a
  // server that is not the local one; they reach pactl and the filter chain
  // through the helper, which narrows this list again for each of them.
  //
  // What is not here is as deliberate: nothing that decides what a program
  // loads rather than what it talks to. PULSE_CLIENTCONFIG belongs to that
  // second kind despite its name, and headset/audio.py says why.
  readonly property var helperEnvironment: {
    var carried = ["XDG_RUNTIME_DIR", "XDG_CACHE_HOME", "XDG_STATE_HOME", "HOME",
                   "LANG", "LC_ALL", "LC_CTYPE",
                   "PULSE_SERVER", "PULSE_COOKIE", "PULSE_RUNTIME_PATH",
                   "PIPEWIRE_REMOTE", "PIPEWIRE_RUNTIME_DIR"]
    var out = { "PATH": "/usr/bin:/bin" }
    for (var i = 0; i < carried.length; i++) {
      var value = Quickshell.env(carried[i])
      if (value) out[carried[i]] = String(value)
    }
    return out
  }

  property var payload: ({ connected: false, state: ({}), controls: ({}), support: ({}),
                           pending: [], ignored: [], error: "" })
  property bool unsupported: false
  property string helperError: ""
  // The helper's last journal line, kept for the journal's sake and shown only
  // before the helper has ever published a state of its own.
  property string helperLog: ""
  // Whether any state has arrived from this helper. Until it has, its stderr is
  // the only evidence there is.
  property bool everPublished: false
  property int restartDelay: 1000

  // Named `reading`, not `state`: every Item already has a `state` property,
  // and shadowing it is a clash that resolves to the wrong thing in silence.
  readonly property var reading: payload.state || ({})
  readonly property var sections: Model.visibleSections(root.viewPayload)
  // One object for Model.js to read, so the bluez battery fallback is visible to
  // the same functions that format the session's own reading.
  // One object for the panel to read: the helper's view, plus everything BlueZ
  // and PipeWire know, normalised into the same `state` and `controls` shape so
  // a row component cannot tell the two tiers apart.
  readonly property var viewPayload: {
    var merged = {}
    for (var key in payload) merged[key] = payload[key]
    merged.bluezBattery = root.bluezBattery
    merged.present = root.headset !== null
    merged.unsupported = root.unsupported
    merged.audio = root.audioCard

    // The helper's own view of the device is gone while it is restarting, and
    // bluez still knows the name, so the panel keeps saying whose headset it is
    // instead of falling back to the word "Headset".
    if (!merged.device || !merged.device.name) {
      merged.device = { name: root.headsetName, address: root.headsetAddress }
    }

    var state = {}
    for (var k in (merged.state || {})) state[k] = merged.state[k]
    var controls = {}
    for (var c in (merged.controls || {})) controls[c] = merged.controls[c]

    var sound = root.audioCard
    var codecs = sound.codecs || []
    var talking = sound.mode === "headset"
    if (sound.card !== undefined && sound.card !== "") {
      state.codec = sound.active_profile
      // Only the two real modes. Reporting anything else, "off" included, as
      // Music selects a chip for a state the headset is not in.
      if (sound.mode === "headset" || sound.mode === "a2dp") {
        state.audio_mode = sound.mode
      }
      state.microphone = root.sourceNode !== null && !root.microphoneMuted
      controls.codec = { supported: codecs.length > 0, writable: codecs.length > 1, available: true }
      controls.audio_mode = {
        supported: codecs.length > 0 && sound.headset_profile !== "",
        writable: codecs.length > 0 && sound.headset_profile !== "",
        available: true
      }
      // Writable in principle whenever the headset has a microphone at all;
      // available only once the mode that carries it is selected. Conflating the
      // two told the user the headset refuses changes, when it simply is not in
      // the mode that has a microphone.
      controls.microphone = {
        supported: sound.headset_profile !== "",
        writable: sound.headset_profile !== "",
        available: root.sourceNode !== null
      }
    }
    merged.state = state
    merged.controls = controls
    return merged
  }

  readonly property bool live: !!payload.connected
  readonly property bool writable: !!payload.write_ready

  // The one line the panel has to say about itself. Two separate Texts each
  // claiming the same string printed a dropped link twice, one above the other.
  readonly property string notice: {
    var reported = Model.sentence(String(payload.error || "") || root.helperError)
    if (root.unsupported) return reported && reported.indexOf("no driver") === -1 ? reported : ""
    if (!root.live) return reported || "Waiting for the headset's control channel."
    return reported
  }

  readonly property string settingSessionPolicy: setting("sessionPolicy", "hold")
  readonly property int settingIdleSeconds: setting("idleSeconds", 30)

  // ------------------------------------------------------------------- appearance
  // Distinct id: a row component owns a `theme` property, so `theme: theme` in a
  // delegate binds the property to itself and QML reports a loop.
  Theme { id: appTheme; bar: root.bar }

  // Shown for any connected headset. Hiding when no driver claimed it threw away
  // the battery, the codec and the microphone, all of which are known regardless.
  visible: root.headset !== null
  implicitWidth: visible ? button.implicitWidth : 0
  implicitHeight: visible ? button.implicitHeight : 0

  readonly property bool compactBar: !!(bar && bar.vertical)

  // --------------------------------------------------------------------- commands

  function send(line) {
    // Silent when there is no vendor helper: a headset with no driver is the
    // normal case, not a fault, and saying so on every panel open was noise.
    if (!helper.running || root.unsupported) return
    helper.write(line + "\n")
  }

  // Features of the universal tier. They are written to PipeWire, not to the
  // headset, and they work whether or not a driver has ever claimed this model.
  readonly property var universalFeatures: ["codec", "audio_mode", "microphone"]

  function applyUniversal(feature, value) {
    if (feature === "microphone") {
      if (!root.sourceNode || !root.sourceNode.audio) {
        root.helperError = "This headset has no microphone in its current mode"
        return
      }
      root.sourceNode.audio.muted = !value
      return
    }
    var profile = ""
    if (feature === "codec") profile = String(value)
    else if (value === "headset") profile = root.audioCard.headset_profile
    else profile = root.audioCard.best_listening
    if (profile === "") {
      root.helperError = "This headset does not offer that mode"
      return
    }
    root.switchProfile(profile)
  }

  function apply(values) {
    root.helperError = ""
    var forHelper = {}
    var any = false
    for (var feature in values) {
      if (root.universalFeatures.indexOf(feature) !== -1) {
        root.applyUniversal(feature, values[feature])
      } else {
        forHelper[feature] = values[feature]
        any = true
      }
    }
    if (!any) return
    if (!root.live) {
      root.helperError = "The headset is not connected"
      return
    }
    root.send(Model.setCommand(forHelper))
  }

  function cycleNoise(direction) {
    if (!root.live) return
    root.apply({ noise: Model.nextNoiseMode(root.reading.noise, direction) })
  }

  function toggleNoise() {
    if (!root.live) return
    root.apply({ noise: root.reading.noise === "off" ? "anc" : "off" })
  }

  function toAmbient() {
    if (!root.live) return
    root.apply({ noise: root.reading.noise === "ambient" ? "off" : "ambient" })
  }

  function nudgeAmbient(steps) {
    if (!root.live || root.reading.noise !== "ambient") return
    var level = root.reading.ambient_level
    if (level === undefined || level === null) level = 0
    root.apply({ ambient_level: Math.max(0, Math.min(Model.AMBIENT_MAX, level + steps)) })
  }

  function consume(line) {
    var text = String(line || "").trim()
    if (text.length === 0 || text.charAt(0) !== "{") return
    try {
      var next = JSON.parse(text)
    } catch (error) {
      root.helperError = "The headset helper sent something unreadable"
      return
    }
    if (next.unsupported) {
      // No driver claims this device. Stop rather than reopening a session
      // against a headset nobody can talk to.
      root.unsupported = true
      return
    }
    root.payload = next
    // From here on the payload is the only thing the panel says. Its `error` and
    // its per-setting reasons are retracted by the next action; a journal line
    // is not, which is what made one linger through everything done after it.
    root.everPublished = true
    root.helperError = ""
    root.restartDelay = 1000
    if (next.connected) root.helperError = ""
  }

  // ------------------------------------------------------------------------ rows

  property var rowItems: ({})
  property string cursorRow: ""
  property bool cursorActive: false

  function registerRow(id, item) {
    var next = root.rowItems
    next[id] = item
    root.rowItems = next
  }

  function unregisterRow(id) {
    var next = root.rowItems
    delete next[id]
    root.rowItems = next
  }

  readonly property var flatRows: {
    var out = []
    for (var s = 0; s < root.sections.length; s++) {
      var rows = root.sections[s].rows
      for (var r = 0; r < rows.length; r++) out.push(rows[r].id)
    }
    return out
  }

  function currentRowItem() {
    return root.rowItems[root.cursorRow] || null
  }

  // Wrapping, like every other Omarchy panel. A list you can only walk off the
  // end of is worse than one that comes back round.
  function moveCursor(delta) {
    var rows = root.flatRows
    if (rows.length === 0) return
    var at = rows.indexOf(root.cursorRow)
    if (at === -1) {
      root.cursorRow = rows[delta > 0 ? 0 : rows.length - 1]
      return
    }
    var next = (at + delta) % rows.length
    if (next < 0) next += rows.length
    root.cursorRow = rows[next]
  }

  function adjustCursor(direction) {
    var item = root.currentRowItem()
    if (item) item.step(direction)
  }

  function nudgeCursor(direction) {
    var item = root.currentRowItem()
    if (item) item.nudge(direction)
  }

  function activateCursor() {
    var item = root.currentRowItem()
    if (item) item.activate()
  }

  // Keep the cursor on screen. Without this, walking down the panel selects rows
  // below the fold and every key then acts on something the user cannot see.
  function ensureRowVisible(id) {
    var item = root.rowItems[id]
    if (!item || !scroll.visible || scroll.height <= 0) return
    var top = item.mapToItem(column, 0, 0).y
    var bottom = top + item.height
    var margin = Style.space(12)
    if (top - margin < scroll.contentY) {
      scroll.contentY = Math.max(0, top - margin)
    } else if (bottom + margin > scroll.contentY + scroll.height) {
      var limit = Math.max(0, scroll.contentHeight - scroll.height)
      scroll.contentY = Math.min(limit, bottom + margin - scroll.height)
    }
  }

  onCursorRowChanged: if (root.cursorActive) Qt.callLater(function() { root.ensureRowVisible(root.cursorRow) })

  function hoverRow(id, on) {
    if (!on) return
    root.cursorActive = true
    root.cursorRow = id
  }

  onOpenedChanged: if (opened) {
    root.helperError = ""
    root.cursorActive = false
    if (root.flatRows.length > 0) root.cursorRow = root.flatRows[0]
    // Reopening a panel halfway down the last thing you read is disorienting.
    scroll.contentY = 0
    root.refreshAudio()
    root.send("refresh")
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  // ----------------------------------------------------------------- the process

  Process {
    id: helper
    command: [root.helperPath, "watch", "--address", root.headsetAddress, "--name", root.headsetName]
    clearEnvironment: true
    environment: root.helperEnvironment
    // Only while bluez says there is a headset to talk to. Spawning a helper for a
    // device that is not there is what turns one absent headset into a respawn loop.
    running: root.headsetAddress !== "" && !root.unsupported
    stdinEnabled: true
    stdout: SplitParser { onRead: function(line) { root.consume(line) } }
    // The helper's stderr is its journal, not a channel to the panel. It carries
    // progress as well as faults ("equaliser running as ..."), and mirroring it
    // into the notice put a line of log in front of the user on every successful
    // action, which flashed and then cleared. What the user must see travels in
    // the payload, where it belongs to a setting and is retracted by the next
    // action. The journal is kept for the one case the payload cannot cover: a
    // helper that failed before it ever published anything.
    stderr: SplitParser {
      onRead: function(line) {
        var text = String(line || "").trim()
        if (text.length === 0) return
        root.helperLog = text.replace(/^omarchy-headset: /, "")
        if (!root.everPublished) root.helperError = root.helperLog
      }
    }
    onExited: function(code) {
      root.payload = Object.assign({}, root.payload, { connected: false, write_ready: false })
      // The last journal line is not shown here. A helper killed outright writes
      // nothing on its way out, so the line still standing is whatever it last
      // did successfully, and presenting that as the reason it died says the
      // opposite of what happened. The payload above already says the headset is
      // not connected, which is the true and useful part; the journal has the
      // rest. Before the first publish there is no payload, and only then is the
      // journal shown, which the stderr handler does.
      root.everPublished = false
      if (!root.unsupported && root.headsetAddress !== "") restartTimer.restart()
    }
  }

  // The card reading. Short-lived and on demand: profiles change only when
  // something switches them, and the codec in use comes from PipeWire live.
  Process {
    id: audioProcess
    clearEnvironment: true
    environment: root.helperEnvironment
    stdout: StdioCollector {
      onStreamFinished: {
        var text = String(this.text || "").trim()
        if (text.charAt(0) !== "{") return
        // A reading belongs to the headset it was asked about. Swapping headsets
        // mid-read otherwise shows one device's codecs under the other's name.
        if (root.audioFor !== root.headsetAddress) return
        try {
          root.audioCard = JSON.parse(text)
        } catch (error) {
          root.helperError = "Could not read the audio profile"
        }
      }
    }
    stderr: SplitParser {
      onRead: function(line) {
        var text = String(line || "").trim()
        if (text.length > 0) root.helperError = text.replace(/^headsetctl: /, "")
      }
    }
    // A profile switch tears the link down and brings it back, so the reading
    // that follows has to wait for PipeWire rather than race it. Only after a
    // switch: re-reading after every read is a loop that never stops.
    onExited: function(code) {
      var wasSwitching = root.audioSwitching
      root.audioSwitching = false
      var queued = root.audioQueued
      root.audioQueued = ""
      if (queued !== "") {
        root.switchProfile(queued)
        return
      }
      if (wasSwitching) audioSettle.restart()
    }
  }

  Timer {
    id: audioSettle
    interval: 900
    onTriggered: root.refreshAudio()
  }

  function refreshAudio() {
    if (root.headsetAddress === "") return
    if (audioProcess.running) return
    root.audioFor = root.headsetAddress
    root.audioSwitching = false
    audioProcess.command = [root.helperPath, "audio", "--address", root.headsetAddress]
    audioProcess.running = true
  }

  function switchProfile(profile) {
    if (root.headsetAddress === "" || profile === "") return
    if (audioProcess.running) {
      root.audioQueued = profile
      return
    }
    root.audioFor = root.headsetAddress
    root.audioSwitching = true
    audioProcess.command = [root.helperPath, "audio", "--address", root.headsetAddress, profile]
    audioProcess.running = true
  }

  // Backoff, because a headset that will never answer should not cost a process
  // every two seconds for the rest of the session.
  Timer {
    id: restartTimer
    interval: root.restartDelay
    onTriggered: {
      root.restartDelay = Math.min(60000, root.restartDelay * 2)
      if (root.headsetAddress !== "" && !root.unsupported) helper.running = true
    }
  }

  // A different headset connected: start again rather than talking to the old one.
  onHeadsetAddressChanged: {
    root.unsupported = false
    root.restartDelay = 1000
    root.audioCard = ({ codecs: [], profiles: [], mode: "", active_codec: "",
                        active_profile: "", headset_profile: "", best_listening: "",
                        has_microphone: false, card: "" })
    root.refreshAudio()
    root.payload = { connected: false, state: ({}), controls: ({}), support: ({}),
                     pending: [], ignored: [], error: "" }
  }

  IpcHandler {
    target: "io.github.itsgg.headset"
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function noise(mode: string): void { root.apply({ noise: mode }) }
    function cycleNoise(): void { root.cycleNoise(1) }
    function toggleNoise(): void { root.toggleNoise() }
    function ambient(): void { root.toAmbient() }
    // A line, not a payload. Omarchy's own plugins answer `status` with the
    // short string they would show, and `headsetctl status --pretty` is already
    // the machine-readable one for anything that wants the whole state.
    function status(): string { return Model.statusLine(root.viewPayload) }
  }

  // ------------------------------------------------------------------ bar button

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰋎"
    labelVisible: false
    dimmed: root.headset === null
    fixedWidth: root.compactBar || Model.barText(root.viewPayload) === ""
      ? Style.space(34) : Style.space(64)
    tooltipText: Model.tooltipText(root.viewPayload)
    onPressed: function(code) {
      if (code === Qt.MiddleButton) root.toggleNoise()
      else if (code === Qt.RightButton) root.toAmbient()
      else root.toggle()
    }
    onWheelMoved: function(delta) {
      var wheel = Util.wheelSteps(root.wheelAccumulator, delta)
      root.wheelAccumulator = wheel.remainder
      if (wheel.steps !== 0) root.nudgeAmbient(wheel.steps)
    }

    Row {
      anchors.centerIn: parent
      spacing: Style.space(5)

      Text {
        anchors.verticalCenter: parent.verticalCenter
        text: "󰋎"
        textFormat: Text.PlainText
        color: Model.accented(root.viewPayload) ? appTheme.accent : appTheme.barForeground
        font.family: appTheme.fontFamily
        font.pixelSize: Style.bar.iconFont
        renderType: Text.NativeRendering
      }
      Text {
        visible: !root.compactBar && text !== ""
        anchors.verticalCenter: parent.verticalCenter
        text: Model.barText(root.viewPayload)
        textFormat: Text.PlainText
        color: appTheme.barForeground
        font.family: appTheme.fontFamily
        font.pixelSize: Style.font.bodySmall
      }
    }
  }

  property real wheelAccumulator: 0

  // ----------------------------------------------------------------------- panel

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(900))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: powerConfirm.opened
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        if (dy !== 0) root.moveCursor(dy)
        else if (dx !== 0) root.adjustCursor(dx)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onTextKey: function(text) {
        if (text === "a" || text === "A") root.toggleNoise()
        else if (text === "t" || text === "T") root.toAmbient()
        else if (text === "r" || text === "R") root.send("refresh")
        else if (text === "+" || text === "=") root.nudgeCursor(1)
        else if (text === "-" || text === "_") root.nudgeCursor(-1)
      }

      Flickable {
        id: scroll
        anchors.fill: parent
        readonly property int gutter: Style.space(10)
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        // The default style fades its handle out whenever nothing is interacting
        // with it, so AlwaysOn alone left an empty gutter and a panel that looked
        // as though it simply ended. This one is always drawn and themed.
        ScrollBar.vertical: ScrollBar {
          id: verticalBar
          policy: scroll.contentHeight > scroll.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
          active: true
          width: Style.space(8)
          background: Item {}
          contentItem: Rectangle {
            implicitWidth: Style.space(4)
            radius: width / 2
            color: Qt.rgba(appTheme.foreground.r, appTheme.foreground.g, appTheme.foreground.b,
                           verticalBar.pressed ? 0.5 : 0.25)
            Behavior on color { ColorAnimation { duration: 120 } }
          }
        }

        Column {
          id: column
          width: scroll.width - scroll.gutter
          spacing: Style.space(12)

          PanelHero {
            width: parent.width
            title: Model.deviceName(root.viewPayload)
            meta: Model.heroMeta(root.viewPayload)
            foreground: appTheme.foreground
            fontFamily: appTheme.fontFamily
            iconOpacity: root.headset !== null ? 1 : 0.45
            iconComponent: Component {
              Text {
                text: "󰋎"
                textFormat: Text.PlainText
                color: Model.accented(root.viewPayload) ? appTheme.accent : appTheme.foreground
                font.family: appTheme.fontFamily
                font.pixelSize: Style.font.display
              }
            }
            trailingControl: root.viewPayload.controls
              && root.viewPayload.controls.noise
              && root.viewPayload.controls.noise.writable ? noiseSwitch : null
          }

          Component {
            id: noiseSwitch
            Item {
              implicitWidth: heroSwitch.implicitWidth
              implicitHeight: heroSwitch.implicitHeight
              ToggleSwitch {
                id: heroSwitch
                checked: root.reading.noise === "anc"
                enabled: root.writable
                opacity: root.writable ? 1 : 0.4
                foreground: appTheme.foreground
                onToggled: root.toggleNoise()
                PanelToolTip {
                  visible: parent.containsMouse
                  text: root.reading.noise === "anc"
                    ? "Switch noise cancelling off" : "Switch noise cancelling on"
                  fontFamily: appTheme.fontFamily
                }
              }
            }
          }

          Text {
            visible: Model.probeNotice(root.viewPayload) !== ""
            width: parent.width
            text: Model.probeNotice(root.viewPayload)
            textFormat: Text.PlainText
            color: appTheme.dim
            font.family: appTheme.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Text {
            visible: root.notice !== ""
            width: parent.width
            text: root.notice
            textFormat: Text.PlainText
            // Not connected is an explanation; a problem while connected is a
            // problem, and they should not look the same.
            color: root.live ? appTheme.urgent : appTheme.dim
            font.family: appTheme.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          // Every section the headset actually reported features for.
          Repeater {
            model: root.sections

            Column {
              id: sectionColumn
              required property var modelData
              width: column.width
              spacing: Style.space(8)

              PanelSeparator { foreground: appTheme.foreground }

              PanelSectionHeader {
                text: sectionColumn.modelData.title
                foreground: appTheme.foreground
                fontFamily: appTheme.fontFamily
              }

              Repeater {
                model: sectionColumn.modelData.rows

                FeatureRow {
                  required property var modelData
                  width: sectionColumn.width
                  spec: modelData
                  status: Model.rowState(modelData, root.viewPayload)
                  reading: root.reading
                  options: Model.optionsFor(modelData, root.viewPayload)
                  theme: appTheme
                  bar: root.bar
                  host: root
                  hasCursor: root.cursorActive && root.cursorRow === String(modelData.id)
                  onRequested: function(values) { root.apply(values) }
                  onHovered: function(on) { root.hoverRow(String(modelData.id), on) }
                  onConfirmRequested: function(message) { powerConfirm.opened = true }
                }
              }
            }
          }

          Text {
            width: parent.width
            text: Model.keyboardHint(root.viewPayload)
            textFormat: Text.PlainText
            color: appTheme.faint
            font.family: appTheme.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
            horizontalAlignment: Text.AlignHCenter
          }
        }
      }
    }

    // The key catcher stops listening while this is up, so the dialog has to
    // take the keyboard itself. Without that, a confirmation reached with the
    // keyboard could only be answered with the mouse.
    ConfirmDialog {
      id: powerConfirm
      anchors.fill: parent
      focus: powerConfirm.opened
      Keys.onPressed: function(event) { event.accepted = powerConfirm.handleKey(event) }
      onOpenedChanged: {
        if (powerConfirm.opened) powerConfirm.forceActiveFocus()
        else Qt.callLater(function() { keyCatcher.forceActiveFocus() })
      }
      message: "Turn the headset off?"
      confirmText: "Turn off"
      cancelText: "Keep on"
      foreground: appTheme.foreground
      fontFamily: appTheme.fontFamily
      onConfirmed: {
        powerConfirm.opened = false
        root.apply({ power_off: true })
        root.close()
      }
      onCanceled: powerConfirm.opened = false
    }
  }
}
