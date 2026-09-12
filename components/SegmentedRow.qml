import QtQuick
import qs.Commons
import qs.Ui

// Pick one of a few: noise mode, equaliser preset, speak-to-chat sensitivity.
Column {
  id: root

  property var spec: ({})
  property var status: ({})
  property var options: []
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

  function labelFor(value) {
    for (var i = 0; i < root.options.length; i++) {
      if (root.options[i].value === value) return root.options[i].label
    }
    return value === undefined || value === null ? "" : String(value)
  }

  function step(direction) {
    if (!root.enabled || root.options.length === 0) return
    var at = -1
    for (var i = 0; i < root.options.length; i++) {
      if (root.options[i].value === root.status.value) { at = i; break }
    }
    if (at === -1) at = 0
    var next = (at + direction) % root.options.length
    if (next < 0) next += root.options.length
    var values = {}
    values[root.spec.feature || root.spec.id] = root.options[next].value
    root.requested(values)
  }

  RowLabel {
    width: parent.width
    label: root.spec.label || ""
    // The selected chip already says which one it is. The text on the right is
    // for the cases the chip cannot show: a write not yet confirmed, or refused.
    value: root.status.pending || root.status.ignored ? root.labelFor(root.status.value) : ""
    pending: !!root.status.pending
    ignored: !!root.status.ignored
    theme: root.theme
  }

  CursorSurface {
    width: parent.width
    height: group.implicitHeight + Style.space(8)
    opacity: root.enabled ? 1 : 0.45
    hasCursor: root.hasCursor
    foreground: root.theme ? root.theme.foreground : Color.foreground
    HoverHandler { onHoveredChanged: root.hovered(hovered) }

    ButtonGroup {
      id: group
      anchors.left: parent.left
      anchors.leftMargin: Style.space(4)
      anchors.right: parent.right
      anchors.rightMargin: Style.space(4)
      anchors.verticalCenter: parent.verticalCenter
      enabled: root.enabled
      focusable: false
      foreground: root.theme ? root.theme.foreground : Color.foreground
      fontFamily: root.theme ? root.theme.fontFamily : Style.font.family
      fontSize: Style.font.bodySmall
      options: root.options
      value: root.status.value === undefined || root.status.value === null
        ? "" : String(root.status.value)
      onChanged: function(value) {
        var values = {}
        values[root.spec.feature || root.spec.id] = value
        root.requested(values)
      }
    }
  }
}
