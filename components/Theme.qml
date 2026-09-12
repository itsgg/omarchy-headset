import QtQuick
import qs.Commons

// One palette for every row, so a component takes `theme` rather than six
// look-alike colour properties. Named `theme` and not `palette` because Qt gives
// every Item a `palette` property already, and a clash stays silently null.
QtObject {
  id: theme

  property QtObject bar: null

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color barForeground: bar ? bar.barForeground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.45)
  readonly property color faint: Qt.darker(foreground, 1.9)
  readonly property color accent: Color.accent
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color background: bar ? bar.background : Color.background
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
}
