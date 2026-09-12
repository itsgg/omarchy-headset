#!/usr/bin/env python3
"""Take the README's screenshots off the running shell.

The panel is drawn inside a full-screen layer surface, so there is no window
geometry to ask Hyprland for, and looking for its border finds the focused
terminal's border just as readily: they are the same accent colour.

So the screen is captured twice, once with the panel closed and once with it
open, and the panel is whatever changed. Nothing is assumed about the theme, the
panel's size, or where on the bar the widget happens to sit. A ticking clock and
a scrolling terminal also change, so a row or column counts only when enough of
its pixels differ to be a panel edge rather than a digit.

Usage: tools/shots.py <name>   ->   docs/<name>.png
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = "io.github.itsgg.headset"
MARGIN = 2
# A panel edge changes most of a line. A clock changes a few dozen pixels.
MIN_RUN = 120
MIN_SIZE = 200


def shell(*args: str) -> None:
    subprocess.run(args, capture_output=True, text=True)


def grab(path: Path) -> Image.Image:
    subprocess.run(["grim", str(path)], check=True)
    return Image.open(path).convert("RGB")


def changed_box(before: Image.Image, after: Image.Image) -> tuple | None:
    if before.size != after.size:
        return None
    mask = ImageChops.difference(before, after).convert("L").point(lambda v: 255 if v > 24 else 0)
    width, height = mask.size
    # Counted a line at a time in C. Per-pixel in Python took thirteen seconds
    # for a screen, which is longer than the whole test suite.
    flat = mask.tobytes()
    rows = [flat[y * width:(y + 1) * width].count(255) for y in range(height)]
    turned = mask.transpose(Image.ROTATE_90).tobytes()
    columns = [turned[i * height:(i + 1) * height].count(255) for i in range(width)]
    # ROTATE_90 puts the leftmost column last.
    columns.reverse()
    xs = [x for x, n in enumerate(columns) if n > MIN_RUN]
    ys = [y for y, n in enumerate(rows) if n > MIN_RUN]
    if not xs or not ys:
        return None
    box = (max(0, xs[0] - MARGIN), max(0, ys[0] - MARGIN),
           min(width, xs[-1] + MARGIN + 1), min(height, ys[-1] + MARGIN + 1))
    if box[2] - box[0] < MIN_SIZE or box[3] - box[1] < MIN_SIZE:
        return None
    return box


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    name = sys.argv[1]
    with tempfile.TemporaryDirectory() as scratch:
        closed_at, open_at = Path(scratch) / "closed.png", Path(scratch) / "open.png"
        shell("omarchy-shell", PLUGIN, "close")
        time.sleep(1.5)
        closed = grab(closed_at)
        shell("omarchy-shell", PLUGIN, "open")
        time.sleep(3)
        opened = grab(open_at)
        shell("omarchy-shell", PLUGIN, "close")

        box = changed_box(closed, opened)
        if box is None:
            print("nothing changed when the panel opened: is a headset connected?")
            return 1
        out = ROOT / "docs" / f"{name}.png"
        opened.crop(box).save(out)
    print(f"{out.relative_to(ROOT)}  {box[2] - box[0]}x{box[3] - box[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
