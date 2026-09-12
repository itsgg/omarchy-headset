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

  implicitHeight: control.implicitHeight
  enabled: !!status.writable && status.available !== false
  opacity: enabled ? 1 : 0.45

  function activate() {
    if (!root.enabled) return
    var values = {}
    values[root.spec.feature || root.spec.id] = !root.status.value
    root.requested(values)
  }

  function step(direction) {
    root.activate()
  }

  Toggle {
    id: control
    width: parent.width
    label: root.spec.label || ""
    description: root.status.ignored
      ? "Acknowledged and ignored by this headset"
      : (root.status.available === false && root.spec.unavailableHint)
        ? root.spec.unavailableHint
        : (root.spec.description || "")
    checked: !!root.status.value
    hasCursor: root.hasCursor
    foreground: root.theme ? root.theme.foreground : Color.foreground
    fontFamily: root.theme ? root.theme.fontFamily : Style.font.family
    titleSize: Style.font.body
    onHovered: function(on) { root.hovered(on) }
    onClicked: root.activate()
  }
}
