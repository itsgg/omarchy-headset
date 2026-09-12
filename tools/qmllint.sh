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

# These are the kinds worth failing on. Unqualified access comes from the objects
# the shell injects at load time, and drowning the real findings is worse than
# not running the linter at all.
#
# `missing-property` needs splitting rather than suppressing, because it covers
# two different things. "Member X not found on type QObject" is the host's
# injected `bar` and `theme`, which the linter cannot see into and which are
# fine. "Could not find property X" is an assignment to a property that does not
# exist on one of our own components, which is a hard runtime failure: the whole
# widget refuses to load with "Cannot assign to non-existent property". That one
# shipped once, past a green `make check`, and is why the linter now runs in it.
structural=$(grep -E '\[(property-override|index|unused-imports|duplicate|deprecated|incompatible-type)\]' <<<"$output" || true)
missing=$(grep 'Could not find property' <<<"$output" || true)
structural=$(printf '%s\n%s' "$structural" "$missing" | grep -v '^$' || true)
if [[ -n $structural ]]; then
  echo "$structural"
  exit 1
fi
echo "qmllint: no structural findings"
