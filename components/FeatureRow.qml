import QtQuick

// Turns one spec entry into the control it describes.
//
// The panel does not know what rows exist; it walks what the helper says the
// headset supports and hands each entry here. A new feature is a spec entry in
// Model.js and, at most, one new kind in the switch below.
Item {
  id: root

  property var spec: ({})
  property var status: ({})
  property var reading: ({})
  property var options: []
  property QtObject theme: null
  property QtObject bar: null
  property bool hasCursor: false
  property var host: null

  signal requested(var values)
  signal hovered(bool on)
  signal confirmRequested(string message)

  readonly property string rowId: String(spec.id || "")
  readonly property var control: loader.item

  implicitHeight: loader.item ? loader.item.implicitHeight : 0
  height: implicitHeight

  // Delegated so the panel's keyboard can drive whichever control this became.
  function step(direction) {
    if (loader.item && loader.item.step) loader.item.step(direction)
  }
  function nudge(direction) {
    if (loader.item && loader.item.nudge) loader.item.nudge(direction)
    else if (loader.item && loader.item.step) loader.item.step(direction)
  }
  function activate() {
    if (loader.item && loader.item.activate) loader.item.activate()
    else step(1)
  }

  Component.onCompleted: if (host && host.registerRow) host.registerRow(rowId, root)
  Component.onDestruction: if (host && host.unregisterRow) host.unregisterRow(rowId)

  Loader {
    id: loader
    width: parent.width
    sourceComponent: {
      switch (String(root.spec.kind || "")) {
      case "segmented": return segmented
      case "choice": return choice
      case "slider": return slider
      case "toggle": return toggle
      case "equalizer": return equalizer
      case "action": return action
      default: return readout
      }
    }
  }

  Component {
    id: segmented
    SegmentedRow {
      spec: root.spec
      status: root.status
      options: root.options
      theme: root.theme
      bar: root.bar
      hasCursor: root.hasCursor
      onRequested: function(values) { root.requested(values) }
      onHovered: function(on) { root.hovered(on) }
    }
  }

  Component {
    id: choice
    ChoiceRow {
      spec: root.spec
      status: root.status
      options: root.options
      theme: root.theme
      bar: root.bar
      hasCursor: root.hasCursor
      onRequested: function(values) { root.requested(values) }
      onHovered: function(on) { root.hovered(on) }
    }
  }

  Component {
    id: slider
    SliderRow {
      spec: root.spec
      status: root.status
      theme: root.theme
      bar: root.bar
      hasCursor: root.hasCursor
      onRequested: function(values) { root.requested(values) }
      onHovered: function(on) { root.hovered(on) }
    }
  }

  Component {
    id: toggle
    ToggleRow {
      spec: root.spec
      status: root.status
      theme: root.theme
      bar: root.bar
      hasCursor: root.hasCursor
      onRequested: function(values) { root.requested(values) }
      onHovered: function(on) { root.hovered(on) }
    }
  }

  Component {
    id: equalizer
    EqualizerRow {
      spec: root.spec
      status: root.status
      reading: root.reading
      theme: root.theme
      bar: root.bar
      hasCursor: root.hasCursor
      onRequested: function(values) { root.requested(values) }
      onHovered: function(on) { root.hovered(on) }
    }
  }

  Component {
    id: action
    ActionRow {
      spec: root.spec
      status: root.status
      theme: root.theme
      bar: root.bar
      hasCursor: root.hasCursor
      onRequested: function(values) { root.requested(values) }
      onHovered: function(on) { root.hovered(on) }
      onConfirmRequested: function(message) { root.confirmRequested(message) }
    }
  }

  Component {
    id: readout
    ReadoutRow {
      spec: root.spec
      status: root.status
      theme: root.theme
      bar: root.bar
      hasCursor: root.hasCursor
    }
  }
}
