#!/usr/bin/env bash
# Qt 6's qmllint over the plugin, with Quickshell's `qs` import alias in place.
#
# /usr/bin/qmllint may belong to Qt 5 on some systems, so the Qt 6 one is named
# explicitly. Warnings about dynamic bar and theme properties are expected: the
# host injects them at load time and the linter cannot see that far.
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
linter=/usr/lib/qt6/bin/qmllint
[[ -x $linter ]] || linter=$(command -v qmllint)

imports=$(mktemp -d)
trap 'rm -rf -- "$imports"' EXIT
ln -s /usr/share/omarchy/shell "$imports/qs"

cd -- "$root"
output=$("$linter" -I "$imports" BarWidget.qml components/*.qml 2>&1 || true)

# These are the kinds worth failing on. Unqualified access and missing-property
# come from properties the shell injects, and drowning the real ones is worse
# than not running the linter at all.
structural=$(grep -E '\[(property-override|index|unused-imports|duplicate|deprecated|incompatible-type)\]' <<<"$output" || true)
if [[ -n $structural ]]; then
  echo "$structural"
  exit 1
fi
echo "qmllint: no structural findings"
