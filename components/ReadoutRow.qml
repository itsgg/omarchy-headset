import QtQuick
import qs.Commons

// Something the headset reports and nothing can change from here.
Item {
  id: root

  property var spec: ({})
  property var status: ({})
  // Why this row cannot be changed, or why the last attempt was refused.
  property string reason: ""
  property QtObject theme: null
  property QtObject bar: null
  property bool hasCursor: false

  signal requested(var values)
  signal hovered(bool on)

  function step(direction) {}
  function activate() {}

  implicitHeight: label.implicitHeight + Style.space(4)

  RowLabel {
    id: label
    width: parent.width
    anchors.verticalCenter: parent.verticalCenter
    label: root.spec.label || ""
    value: root.spec.format === "onOff"
      ? (root.status.value ? "On" : "Off")
      : (root.status.value === undefined || root.status.value === null ? "" : String(root.status.value))
    theme: root.theme
  }

  // Only ever a refusal here: there is no standing hint on this kind of row.
  ReasonLine {
    theme: root.theme
    reason: root.reason
  }
}
