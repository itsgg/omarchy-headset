import QtQuick
import qs.Commons

// The words under a row: why it cannot be changed, why the last attempt was
// refused, or what it does.
//
// Outside the dimmed control on purpose. WCAG 1.4.3 withdraws the contrast floor
// from an inactive component, so dimming the whole row lands hardest on the one
// piece of text the user most needs to read: the reason it is inactive.
//
// One component rather than a copy in each row, because three rows worked this
// out separately and two of them left a refusal with nowhere to appear.
Text {
  id: root

  property QtObject theme: null
  // Why the row is inactive or was refused. Takes precedence over the hint.
  property string reason: ""
  // What the row does, when there is nothing to explain.
  property string hint: ""

  visible: text !== ""
  width: parent ? parent.width : 0
  text: root.reason !== "" ? root.reason : root.hint
  textFormat: Text.PlainText
  color: root.theme
    ? (root.reason !== "" ? root.theme.dim : root.theme.faint)
    : (root.reason !== "" ? "#888" : "#666")
  font.family: root.theme ? root.theme.fontFamily : Style.font.family
  font.pixelSize: Style.font.caption
  wrapMode: Text.WordWrap
  leftPadding: Style.space(6)
}
