import QtQuick
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// Five bands and clear bass, in decibels.
//
// Clear bass travels to the headset in the same message as the bands, so every
// edit restates all six values. Sending the bands alone re-sends whatever clear
// bass was last known, and it drifts a step every time a band is touched.
Column {
  id: root

  property var spec: ({})
  property var status: ({})
  // Why this row cannot be changed, or why the last attempt was refused.
  property string reason: ""
  property var reading: ({})
  property QtObject theme: null
  property QtObject bar: null
  property bool hasCursor: false
  property int bandIndex: 0

  signal requested(var values)
  signal hovered(bool on)

  spacing: Style.space(6)
  enabled: !!status.writable && status.available !== false

  // Dim the control, never the words. WCAG 1.4.3 withdraws the contrast floor
  // from an inactive component, so a blanket opacity lands hardest on the one
  // piece of text the user most needs to read: the reason it is inactive.

  // The bands come from the row, because there are two equalisers here with
  // different band counts, and this component should not have to know which one
  // it is drawing. One carries a clear bass alongside its bands; the other does
  // not, and says so by leaving it out.
  readonly property var bandSpec: root.spec.bands || ({})
  readonly property var bandLabels: bandSpec.labels || []
  readonly property real bandStep: bandSpec.step || 1

  readonly property var columns: {
    var out = []
    if (bandSpec.extra) {
      out.push({ label: bandSpec.extra.label, feature: bandSpec.extra.feature, band: -1 })
    }
    for (var i = 0; i < root.bandLabels.length; i++) {
      out.push({ label: root.bandLabels[i], feature: bandSpec.feature, band: i })
    }
    return out
  }

  function gainAt(index) {
    var column = root.columns[index]
    if (column.band < 0) {
      var single = root.reading[column.feature]
      return single === undefined || single === null ? 0 : Number(single)
    }
    var bands = root.reading[column.feature]
    if (!bands || bands.length <= column.band) return 0
    var value = bands[column.band]
    return value === undefined || value === null ? 0 : Number(value)
  }

  function write(index, gain) {
    if (!root.enabled) return
    var column = root.columns[index]
    var quantum = root.bandStep
    var clamped = Math.max(Model.BAND_MIN,
      Math.min(Model.BAND_MAX, Math.round(gain / quantum) * quantum))
    var values = {}
    if (column.band < 0) {
      values[column.feature] = clamped
    } else {
      values[column.feature] = Model.bandsWith(root.reading, column.feature, column.band,
                                               root.bandLabels.length, clamped, quantum)
    }
    root.requested(values)
  }

  // Left and right walk the bands. Changing a gain is plus and minus, so an arrow
  // key can never alter the sound by accident while moving through the panel.
  function step(direction) {
    var next = root.bandIndex + direction
    root.bandIndex = Math.max(0, Math.min(root.columns.length - 1, next))
  }

  function nudge(direction) {
    root.write(root.bandIndex, root.gainAt(root.bandIndex) + direction * root.bandStep)
  }

  function activate() {
    if (!root.enabled) return
    var flat = []
    for (var i = 0; i < root.bandLabels.length; i++) flat.push(0)
    var values = {}
    values[bandSpec.feature] = flat
    if (bandSpec.extra) values[bandSpec.extra.feature] = 0
    root.requested(values)
  }

  Item {
    width: parent.width
    implicitHeight: Math.max(header.implicitHeight, flat.implicitHeight)

    Text {
      id: header
      text: "Bands"
      textFormat: Text.PlainText
      color: root.theme ? root.theme.dim : "#888"
      font.family: root.theme ? root.theme.fontFamily : Style.font.family
      font.pixelSize: Style.font.bodySmall
      anchors.left: parent.left
      anchors.leftMargin: Style.space(4)
      anchors.verticalCenter: parent.verticalCenter
    }

    Button {
      id: flat
      text: "Flat"
      tooltipText: "Every band and clear bass back to 0 dB"
      bordered: true
      enabled: root.enabled
      foreground: root.theme ? root.theme.foreground : Color.foreground
      fontFamily: root.theme ? root.theme.fontFamily : Style.font.family
      fontSize: Style.font.bodySmall
      verticalPadding: Style.space(3)
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      onClicked: root.activate()
    }
  }

  CursorSurface {
    width: parent.width
    height: Style.space(148)
    opacity: root.enabled ? 1 : 0.45
    hasCursor: root.hasCursor
    foreground: root.theme ? root.theme.foreground : Color.foreground
    HoverHandler { onHoveredChanged: root.hovered(hovered) }

    Row {
      anchors.fill: parent
      anchors.margins: Style.space(6)
      spacing: Style.space(4)

      Repeater {
        model: root.columns.length

        Item {
          id: band
          required property int index
          readonly property real gain: root.gainAt(band.index)
          readonly property bool selected: root.hasCursor && root.bandIndex === band.index
          width: (parent.width - parent.spacing * (root.columns.length - 1)) / root.columns.length
          height: parent.height

          Text {
            id: gainLabel
            anchors.top: parent.top
            anchors.horizontalCenter: parent.horizontalCenter
            text: Model.gainText(band.gain)
            textFormat: Text.PlainText
            color: band.gain === 0
              ? (root.theme ? root.theme.dim : "#888")
              : (root.theme ? root.theme.foreground : Color.foreground)
            font.family: root.theme ? root.theme.fontFamily : Style.font.family
            font.pixelSize: Style.font.caption
            font.bold: band.gain !== 0
          }

          Item {
            id: rail
            anchors.top: gainLabel.bottom
            anchors.bottom: freqLabel.top
            anchors.topMargin: Style.space(4)
            anchors.bottomMargin: Style.space(4)
            width: parent.width
            readonly property real knobSize: Math.max(10, Math.round(Style.spacing.controlHeight * 0.34))
            readonly property real travel: Math.max(1, height - knobSize)
            readonly property real span: Model.BAND_MAX - Model.BAND_MIN

            Rectangle {
              anchors.horizontalCenter: parent.horizontalCenter
              width: Math.max(3, Style.space(3))
              height: parent.height
              radius: width / 2
              color: root.bar ? Style.selectedFillFor(root.bar.foreground, Color.accent) : "#333"
            }
            Rectangle {
              anchors.horizontalCenter: parent.horizontalCenter
              width: parent.width * 0.7
              height: 1
              y: parent.height / 2
              color: root.theme ? Qt.rgba(root.theme.foreground.r, root.theme.foreground.g,
                                          root.theme.foreground.b, 0.25) : "#444"
            }
            Rectangle {
              anchors.horizontalCenter: parent.horizontalCenter
              width: Math.max(3, Style.space(3))
              radius: width / 2
              height: Math.abs(band.gain) / rail.span * parent.height
              y: band.gain >= 0 ? parent.height / 2 - height : parent.height / 2
              color: root.theme ? root.theme.accent : Color.accent
            }
            BorderSurface {
              anchors.horizontalCenter: parent.horizontalCenter
              width: rail.knobSize
              height: rail.knobSize
              radius: rail.knobSize / 2
              color: root.theme ? root.theme.foreground : Color.foreground
              borderSpec: Border.flat(root.theme ? root.theme.background : "#101315",
                                      Math.max(1, Style.space(2)))
              y: (Model.BAND_MAX - band.gain) / rail.span * rail.travel
              scale: band.selected || bandMouse.containsMouse || bandMouse.pressed ? 1.18 : 1
              Behavior on y { enabled: !bandMouse.pressed; NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
              Behavior on scale { NumberAnimation { duration: 110; easing.type: Easing.OutCubic } }
            }
            MouseArea {
              id: bandMouse
              anchors.fill: parent
              anchors.leftMargin: -Style.space(2)
              anchors.rightMargin: -Style.space(2)
              hoverEnabled: true
              enabled: root.enabled
              cursorShape: Qt.PointingHandCursor
              property real preview: 0
              property bool previewing: false
              function gainAt(y) {
                var fraction = (y - rail.knobSize / 2) / rail.travel
                return Math.max(Model.BAND_MIN, Math.min(Model.BAND_MAX,
                  Math.round(Model.BAND_MAX - fraction * rail.span)))
              }
              onEntered: root.hovered(true)
              onPressed: function(mouse) { root.bandIndex = band.index }
              // Written on release: a drag across the rail would otherwise send one
              // full six-value curve per pixel.
              onReleased: function(mouse) { root.write(band.index, gainAt(mouse.y)) }
              onWheel: function(wheel) {
                root.bandIndex = band.index
                root.write(band.index,
                  band.gain + (wheel.angleDelta.y > 0 ? root.bandStep : -root.bandStep))
              }
            }
          }

          Text {
            id: freqLabel
            anchors.bottom: parent.bottom
            anchors.horizontalCenter: parent.horizontalCenter
            text: root.columns[band.index].label
            textFormat: Text.PlainText
            color: band.selected
              ? (root.theme ? root.theme.accent : Color.accent)
              : (root.theme ? root.theme.dim : "#888")
            font.family: root.theme ? root.theme.fontFamily : Style.font.family
            font.pixelSize: Style.font.caption
            font.bold: band.selected
          }
        }
      }
    }
  }

  ReasonLine {
    theme: root.theme
    reason: root.reason
  }
}
