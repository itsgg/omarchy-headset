import QtQuick
import qs.Commons
import qs.Ui

// A number with a range: the ambient sound level.
Column {
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

  spacing: Style.space(4)
  enabled: !!status.writable && status.available !== false

  // Dim the control, never the words. WCAG 1.4.3 withdraws the contrast floor
  // from an inactive component, so a blanket opacity lands hardest on the one
  // piece of text the user most needs to read: the reason it is inactive.

  readonly property int minimum: root.spec.minimum === undefined ? 0 : root.spec.minimum
  readonly property int maximum: root.spec.maximum === undefined ? 10 : root.spec.maximum
  // Zero is a real level, so the reading is checked for null rather than for
  // being falsy. `value || minimum` would show an empty room as full.
  readonly property int current: status.value === undefined || status.value === null
    ? root.minimum : Number(status.value)

  function step(direction) {
    if (!root.enabled) return
    var next = Math.max(root.minimum, Math.min(root.maximum, root.current + direction))
    if (next === root.current) return
    var values = {}
    values[root.spec.feature || root.spec.id] = next
    root.requested(values)
  }

  RowLabel {
    width: parent.width
    label: root.spec.label || ""
    value: (slider.dragging ? Math.round(slider.liveValue) : root.current) + " / " + root.maximum
    pending: !!root.status.pending
    ignored: !!root.status.ignored
    theme: root.theme
  }

  CursorSurface {
    width: parent.width
    height: slider.implicitHeight + Style.spacing.controlGap
    opacity: root.enabled ? 1 : 0.45
    hasCursor: root.hasCursor
    outline: true
    foreground: root.theme ? root.theme.foreground : Color.foreground
    HoverHandler { onHoveredChanged: root.hovered(hovered) }

    PanelSlider {
      id: slider
      bar: root.bar
      anchors.fill: parent
      anchors.leftMargin: Style.space(6)
      anchors.rightMargin: Style.space(6)
      minimum: root.minimum
      maximum: root.maximum
      step: root.spec.step === undefined ? 1 : root.spec.step
      integer: true
      tickCount: root.spec.ticks === undefined ? 0 : root.spec.ticks
      enabled: root.enabled
      value: root.current
      // On release, not on every pixel. A drag that writes continuously fills the
      // device's queue with values that are already wrong.
      onReleased: function(value) {
        var values = {}
        values[root.spec.feature || root.spec.id] = Math.round(value)
        root.requested(values)
      }
    }
  }

  ReasonLine {
    theme: root.theme
    reason: root.reason
    hint: root.spec.hint || ""}
}
