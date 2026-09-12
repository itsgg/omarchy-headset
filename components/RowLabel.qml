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
    // Words, not glyphs. An asterisk already means "required field" everywhere
    // else in an interface, and a glyph alone carries nothing to a screen reader
    // (WCAG 1.4.1). "sending" says what is happening; "not applied" says what
    // did not, beside the value the headset is actually using.
    text: root.value + (root.ignored ? "  not applied" : root.pending ? "  sending" : "")
    textFormat: Text.PlainText
    color: root.ignored
      ? (root.theme ? root.theme.urgent : "#c33")
      : (root.theme ? root.theme.dim : "#888")
    font.family: root.theme ? root.theme.fontFamily : Style.font.family
    font.pixelSize: Style.font.caption
    font.bold: true
  }
}
