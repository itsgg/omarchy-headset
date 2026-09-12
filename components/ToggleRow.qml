import QtQuick
import qs.Commons
import qs.Ui

// On or off, with a line saying what it does.
Item {
  id: root

  property var spec: ({})
  property var status: ({})
  property QtObject theme: null
  property QtObject bar: null
  property bool hasCursor: false

  signal requested(var values)
  signal hovered(bool on)

  implicitHeight: column.implicitHeight
  enabled: !!status.writable && status.available !== false

  // Dim the control, never the words. WCAG 1.4.3 withdraws the contrast floor
  // from an inactive component, so a blanket opacity lands hardest on the one
  // piece of text the user most needs to read: the reason it is inactive.

  function activate() {
    if (!root.enabled) return
    var values = {}
    values[root.spec.feature || root.spec.id] = !root.status.value
    root.requested(values)
  }

  function step(direction) {
    root.activate()
  }

  readonly property string reason: root.status.ignored
    ? "This headset accepts this and does not act on it"
    : (root.status.supported && !root.status.writable)
      ? "This headset reports this and does not accept changes"
      : (root.status.available === false && root.spec.unavailableHint)
        ? root.spec.unavailableHint
        : ""

  Column {
    id: column
    width: parent.width
    spacing: Style.space(2)

    Toggle {
      id: control
      width: parent.width
      opacity: root.enabled ? 1 : 0.45
      label: root.spec.label || ""
      description: root.reason === "" ? (root.spec.description || "") : ""
      checked: !!root.status.value
      hasCursor: root.hasCursor
      foreground: root.theme ? root.theme.foreground : Color.foreground
      fontFamily: root.theme ? root.theme.fontFamily : Style.font.family
      titleSize: Style.font.body
      onHovered: function(on) { root.hovered(on) }
      onClicked: root.activate()
    }

    // Outside the dimmed control on purpose: this is the sentence that explains
    // the dimming, and it is the last text that should be hard to read.
    Text {
      visible: root.reason !== ""
      width: parent.width
      text: root.reason
      textFormat: Text.PlainText
      color: root.theme ? root.theme.dim : "#888"
      font.family: root.theme ? root.theme.fontFamily : Style.font.family
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
      leftPadding: Style.space(6)
    }
  }
}
