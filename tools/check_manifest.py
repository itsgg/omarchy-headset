#!/usr/bin/python3 -I
"""Check manifest.json against what Omarchy's loader and the marketplace need.

Mirrors omarchy-plugin-validate so CI catches a broken manifest without a running
Omarchy, and adds the marketplace's own required-field list.
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
REQUIRED = ["schemaVersion", "id", "name", "version", "author", "description",
            "kinds", "entryPoints"]

# Omarchy's own table, from omarchy-plugin-validate: a kebab-case kind loads from
# a camelCase entry point, and a kind outside this table is left alone.
KIND_ENTRY_POINTS = {
    "bar": "bar",
    "bar-widget": "barWidget",
    "menu": "menu",
    "overlay": "overlay",
    "panel": "panel",
    "service": "service",
}


def main() -> int:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    problems = []

    for key in REQUIRED:
        if key not in manifest:
            problems.append(f"missing required field: {key}")

    if manifest.get("schemaVersion") != 1:
        problems.append("schemaVersion must be the JSON number 1")

    plugin_id = str(manifest.get("id", ""))
    if plugin_id.startswith("omarchy."):
        problems.append("id must not use the reserved omarchy.* namespace")
    if "." not in plugin_id:
        problems.append("id should be namespaced, e.g. author.plugin")

    for kind, entry in (manifest.get("entryPoints") or {}).items():
        path = ROOT / entry
        if ".." in entry or entry.startswith("/"):
            problems.append(f"entry point for {kind} must be a relative path inside the plugin")
        elif not path.is_file():
            problems.append(f"entry point for {kind} does not exist: {entry}")
        elif path.is_symlink():
            problems.append(f"entry point for {kind} is a symlink: {entry}")

    for kind in manifest.get("kinds") or []:
        entry_key = KIND_ENTRY_POINTS.get(kind)
        if entry_key and entry_key not in (manifest.get("entryPoints") or {}):
            problems.append(f"kind {kind!r} requires entryPoints.{entry_key} to load")

    section = manifest.get("barWidget") or {}
    if section.get("defaultSection") not in (None, "left", "center", "right"):
        problems.append("barWidget.defaultSection must be left, center or right")

    # Every setting the panel reads has to have a default, or a fresh install
    # gets whatever the QML fallback happens to be rather than what is documented.
    defaults = section.get("defaults") or {}
    for entry in section.get("schema") or []:
        if entry.get("key") not in defaults:
            problems.append(f"setting {entry.get('key')!r} has no entry in barWidget.defaults")

    # The shell refuses a plugin folder containing a symlink anywhere.
    for path in ROOT.rglob("*"):
        if ".git" in path.parts:
            continue
        if path.is_symlink():
            problems.append(f"symlinks are not allowed inside a plugin folder: {path.relative_to(ROOT)}")

    for required_file in ("README.md", "LICENSE"):
        if not (ROOT / required_file).is_file():
            problems.append(f"the marketplace requires a root {required_file}")

    if problems:
        for problem in problems:
            print(f"manifest: {problem}", file=sys.stderr)
        return 1

    print(f"manifest ok: {plugin_id} {manifest['version']} {manifest['kinds']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
