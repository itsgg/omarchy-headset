import QtQuick
import qs.Commons

// A row's name on the left and its current value on the right, which is the
// shape every row in an Omarchy panel uses above its control.
Item {
  id: root

  property string label: ""
  property string value: ""
  property QtObject theme: null
  property bool pending: false
  property bool ignored: false

  implicitHeight: Math.max(name.implicitHeight, reading.implicitHeight)

  Text {
    id: name
    anchors.left: parent.left
    anchors.leftMargin: Style.space(4)
    anchors.verticalCenter: parent.verticalCenter
    text: root.label
    textFormat: Text.PlainText
    color: root.theme ? root.theme.dim : "#888"
    font.family: root.theme ? root.theme.fontFamily : Style.font.family
    font.pixelSize: Style.font.bodySmall
  }

  Text {
    id: reading
    anchors.right: parent.right
    anchors.rightMargin: Style.space(6)
    anchors.verticalCenter: parent.verticalCenter
    // An asterisk marks a value this session asked for and the headset has not
    // confirmed. It is not decoration: the device reports the old value for a
    // while after a write, and pretending otherwise is how a panel lies.
    text: root.value + (root.ignored ? "  not applied" : root.pending ? " *" : "")
    textFormat: Text.PlainText
    color: root.ignored
      ? (root.theme ? root.theme.urgent : "#c33")
      : (root.theme ? root.theme.dim : "#888")
    font.family: root.theme ? root.theme.fontFamily : Style.font.family
    font.pixelSize: Style.font.caption
    font.bold: true
  }
}
