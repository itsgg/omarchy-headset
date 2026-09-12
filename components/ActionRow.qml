import QtQuick
import qs.Commons
import qs.Ui

// Something that happens once and cannot be undone from here, so it asks first.
Item {
  id: root

  property var spec: ({})
  property var status: ({})
  property QtObject theme: null
  property QtObject bar: null
  property bool hasCursor: false

  signal requested(var values)
  signal hovered(bool on)
  signal confirmRequested(string message)

  implicitHeight: button.implicitHeight + Style.space(6)
  enabled: !!status.writable

  function step(direction) {}
  function activate() {
    if (!root.enabled) return
    root.confirmRequested(root.spec.label || "")
  }

  Button {
    id: button
    opacity: root.enabled ? 1 : 0.45
    anchors.left: parent.left
    anchors.leftMargin: Style.space(4)
    anchors.verticalCenter: parent.verticalCenter
    text: root.spec.label || ""
    bordered: true
    enabled: root.enabled
    hasCursor: root.hasCursor
    foreground: root.theme ? root.theme.foreground : Color.foreground
    fontFamily: root.theme ? root.theme.fontFamily : Style.font.family
    fontSize: Style.font.bodySmall
    onHovered: function(on) { root.hovered(on) }
    onClicked: root.activate()
  }
}
