#!/usr/bin/env python3
"""Take the README's screenshots off the running shell.

The panel is drawn inside a full-screen layer surface, so Hyprland has no
geometry to give for it. Comparing a capture before and after opening it does
not work either: opening it dims the whole screen, its drop shadow changes
pixels far outside it, and a video playing behind it changes more than the panel
does.

So it is found in a single capture, by the one thing that is true of a panel and
of nothing else on screen: two tall columns of one unbroken colour, several
hundred pixels apart, which are its own inside edges. A photograph does not hold
one colour for five hundred rows, and a window that did would have to be exactly
panel-width to be mistaken for one.

Verified against three captures with a terminal, a bare wallpaper and a playing
video behind the panel, which give the same box relative to it in each.

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
# The border sits just outside the inside edges this finds.
MARGIN = 3
# A column of one colour this tall is a panel edge, not a photograph.
MIN_RUN = 500
# How far apart the two edges may be and still be one panel.
MIN_WIDTH, MAX_WIDTH = 300, 800
# Columns this close together are one edge of one panel.
CLUSTER_GAP = 40
# And an edge is a band of columns, never a single one: a lone column of the
# same colour elsewhere on screen is a coincidence, and one of them dragged the
# crop out to the width of a browser page whose background happened to match.
MIN_BAND = 3


def shell(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout


def runs_by_colour(image: Image.Image) -> dict:
    """Every column that holds one colour for MIN_RUN rows, keyed by colour.

    Done by comparing the image with itself shifted down a row, so "the same as
    the pixel above" is one image operation rather than two million Python ones,
    and then carrying a running height per column. Per pixel in Python this took
    five seconds an image, which made the tests slower than everything else in
    the repository put together.
    """
    width, height = image.size
    if height <= MIN_RUN:
        return {}
    shifted = Image.new(image.mode, image.size)
    shifted.paste(image.crop((0, 0, width, height - 1)), (0, 1))
    same = ImageChops.difference(image, shifted).convert("L").point(lambda v: 0 if v else 1)
    # The first row has nothing above it, so it never continues a run.
    same.paste(Image.new("L", (width, 1), 0), (0, 0))
    flat = same.tobytes()

    found: dict = {}
    pixels = image.load()
    tall = [1] * width
    for y in range(height):
        row = flat[y * width:(y + 1) * width]
        for x in range(width):
            if row[x]:
                tall[x] += 1
                continue
            if tall[x] >= MIN_RUN:
                found.setdefault(pixels[x, y - 1], []).append((x, y - tall[x], y - 1))
            tall[x] = 1
    for x in range(width):
        if tall[x] >= MIN_RUN:
            found.setdefault(pixels[x, height - 1], []).append((x, height - tall[x], height - 1))
    return found


def clusters(xs: list) -> list:
    """Columns grouped into the bands they form, nearest first."""
    out: list = []
    for x in sorted(xs):
        if out and x - out[-1][-1] <= CLUSTER_GAP:
            out[-1].append(x)
        else:
            out.append([x])
    return out


def panel_box(image: Image.Image) -> tuple | None:
    """The panel's bounding box in the image, or None if it is not open.

    A panel shows up as two bands of columns, its left and right edges, where
    nothing is drawn over its background from top to bottom. The middle is
    broken up by the rows themselves, so the bands are what there is to find.

    A single stray column of the same colour elsewhere is not a band, which is
    what this filters: a browser page behind the panel was its exact colour, and
    one such column 270 pixels away stretched the crop half the screen wide.

    Verified against four captures with a terminal, a bare wallpaper, a playing
    video and a dark page of the panel's own colour behind it. It would still be
    fooled by a wide block of the panel's exact colour at a plausible distance,
    which is a coincidence not yet seen; matching those bands by the height they
    start and end at was tried for it and did worse on the real captures, so
    this stays simple and the limitation is written down instead.
    """
    for _, runs in sorted(runs_by_colour(image).items(), key=lambda kv: -len(kv[1])):
        bands = [band for band in clusters([run[0] for run in runs])
                 if len(band) >= MIN_BAND]
        if not bands:
            continue
        left, right = bands[0][0], bands[-1][-1]
        if not MIN_WIDTH < right - left < MAX_WIDTH:
            continue
        inside = {x for band in bands for x in band}
        edges = [run for run in runs if run[0] in inside]
        top = min(run[1] for run in edges)
        bottom = max(run[2] for run in edges)
        return (max(0, left - MARGIN), max(0, top - MARGIN),
                min(image.width, right + MARGIN + 1),
                min(image.height, bottom + MARGIN + 1))
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    name = sys.argv[1]
    with tempfile.TemporaryDirectory() as scratch:
        shot = Path(scratch) / "screen.png"
        shell("omarchy-shell", PLUGIN, "open")
        time.sleep(3)
        subprocess.run(["grim", str(shot)], check=True)
        shell("omarchy-shell", PLUGIN, "close")
        image = Image.open(shot).convert("RGB")
        box = panel_box(image)
        if box is None:
            print("no panel on screen: is a headset connected?")
            return 1
        out = ROOT / "docs" / f"{name}.png"
        image.crop(box).save(out)
    print(f"{out.relative_to(ROOT)}  {box[2] - box[0]}x{box[3] - box[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
