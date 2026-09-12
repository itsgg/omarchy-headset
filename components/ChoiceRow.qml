import QtQuick
import qs.Commons
import qs.Ui

// Pick one of many. A segmented row is a Row and does not wrap, so ten equaliser
// presets simply ran off the right edge of the panel; anything past a handful of
// options belongs in a dropdown.
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
  opacity: enabled ? 1 : 0.45

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
    root.choose(root.options[next].value)
  }

  function choose(value) {
    var values = {}
    values[root.spec.feature || root.spec.id] = value
    root.requested(values)
  }

  RowLabel {
    width: parent.width
    label: root.spec.label || ""
    value: root.status.pending || root.status.ignored ? root.labelFor(root.status.value) : ""
    pending: !!root.status.pending
    ignored: !!root.status.ignored
    theme: root.theme
  }

  Dropdown {
    id: dropdown
    width: parent.width - Style.space(8)
    x: Style.space(4)
    showLabel: false
    enabled: root.enabled
    hasCursor: root.hasCursor
    fontFamily: root.theme ? root.theme.fontFamily : Style.font.family
    options: root.options
    value: root.status.value === undefined || root.status.value === null
      ? "" : String(root.status.value)
    onChanged: function(value) { root.choose(value) }
    onHovered: function(on) { root.hovered(on) }
  }
}
