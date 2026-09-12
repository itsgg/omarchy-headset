import QtQuick
import qs.Commons

// Why the last attempt on this row was refused, and nothing else.
//
// Omarchy's panels put a short live status in this position and leave it empty
// the rest of the time. Nothing here explains what a row does or why it is
// dimmed: the control says the first and the dimming says the second.
//
// Outside the dimmed control on purpose. WCAG 1.4.3 withdraws the contrast floor
// from an inactive component, so dimming the whole row would land hardest on the
// one piece of text the user most needs to read.
//
// One component rather than a copy in each row, because three rows worked this
// out separately and two of them left a refusal with nowhere to appear.
Text {
  id: root

  property QtObject theme: null
  // Why the last attempt was refused. Empty the rest of the time, which is
  // nearly always.
  property string reason: ""

  visible: text !== ""
  width: parent ? parent.width : 0
  text: root.reason
  textFormat: Text.PlainText
  color: root.theme ? root.theme.dim : "#888"
  font.family: root.theme ? root.theme.fontFamily : Style.font.family
  font.pixelSize: Style.font.caption
  wrapMode: Text.WordWrap
  leftPadding: Style.space(6)
}
