"""Finding the panel in a screenshot, without asking it where it is.

The panel is drawn inside a full-screen layer surface, so Hyprland has no
geometry to give for it. Four approaches were tried and thrown away before this
one, and each of them is a test here, because each looked right until it met a
real screen:

- its border colour, which found the focused terminal's border instead, since
  they are the same accent;
- the difference between a capture before and after opening it, which took in
  the whole screen because opening it dims everything;
- the largest solid rectangle of that difference, which stopped at the first
  hole where a dark panel sat over a dark terminal;
- the outer bounds of the difference, which the panel's own drop shadow
  stretched hundreds of pixels to the left.

What is left is the one thing true of a panel and of nothing else on a screen:
two tall columns of a single unbroken colour, several hundred pixels apart,
which are its own inside edges.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    Image = None


def load():
    spec = importlib.util.spec_from_file_location("shots", ROOT / "tools" / "shots.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipIf(Image is None, "Pillow is only needed to take screenshots")
class PanelBoxTests(unittest.TestCase):
    PANEL = (990, 40, 1490, 860)

    def setUp(self):
        self.shots = load()

    def desktop(self, kind="plain"):
        image = Image.new("RGB", (1920, 1200), (18, 20, 24))
        draw = ImageDraw.Draw(image)
        if kind == "terminal":
            # Text: short horizontal runs, no long vertical ones.
            for y in range(40, 1160, 16):
                for x in range(40, 900, 11):
                    draw.rectangle([x, y, x + 6, y + 8], fill=(120 + (x % 7) * 5, 130, 140))
        if kind == "photo":
            for y in range(0, 1200, 3):
                for x in range(0, 1920, 3):
                    draw.rectangle([x, y, x + 2, y + 2],
                                   fill=((x * 7 + y) % 256, (y * 5) % 256, (x + y * 3) % 256))
        if kind == "window":
            # A window with a flat interior, but nothing like a panel's width.
            draw.rectangle([100, 100, 240, 1100], fill=(30, 34, 40))
        return image

    def with_panel(self, image, box=None, shadow=False):
        out = image.copy()
        draw = ImageDraw.Draw(out)
        box = list(box or self.PANEL)
        if shadow:
            # A gradient reaching well outside the panel, which is what pulled
            # the crop hundreds of pixels left when bounds were taken instead.
            for step in range(60, 0, -1):
                shade = int(18 + step * 0.4)
                draw.rectangle([box[0] - step, box[1] - step, box[2] + step, box[3] + step],
                               outline=(shade, shade, shade))
        draw.rectangle(box, fill=(10, 16, 20), outline=(70, 150, 210), width=2)
        return out

    def assertFound(self, image, box=None):
        want = list(box or self.PANEL)
        found = self.shots.panel_box(image)
        self.assertIsNotNone(found, "the panel was not found")
        for got, expected in zip(found, want):
            self.assertLessEqual(abs(got - expected), self.shots.MARGIN + 2,
                                 f"{found} is not {tuple(want)}")
        return found

    def test_the_panel_is_found_over_a_terminal(self):
        self.assertFound(self.with_panel(self.desktop("terminal")))

    def test_the_panel_is_found_over_a_photograph(self):
        # A photograph does not hold one colour for five hundred rows.
        self.assertFound(self.with_panel(self.desktop("photo")))

    def test_a_drop_shadow_does_not_widen_the_crop(self):
        found = self.assertFound(self.with_panel(self.desktop("terminal"), shadow=True))
        self.assertGreater(found[0], 900, "the shadow pulled the crop left")

    def test_the_panel_is_found_wherever_along_the_bar_the_widget_sits(self):
        for left in (300, 700, 1380):
            box = (left, 40, left + 500, 860)
            with self.subTest(left=left):
                self.assertFound(self.with_panel(self.desktop("terminal"), box), box)

    def test_no_panel_on_screen_is_reported_rather_than_cropping_something_else(self):
        # Which is what happens with no headset connected: the widget is hidden
        # and there is nothing to open.
        for kind in ("plain", "terminal", "photo", "window"):
            with self.subTest(kind=kind):
                self.assertIsNone(self.shots.panel_box(self.desktop(kind)))

    def test_a_tall_narrow_window_is_not_mistaken_for_a_panel(self):
        self.assertIsNone(self.shots.panel_box(self.desktop("window")))

    def test_a_stray_column_of_the_panels_own_colour_does_not_widen_it(self):
        # A browser page behind the panel was its exact colour, and one column
        # of it 270 pixels away stretched the crop to 771 wide. An edge is a
        # band of columns; a lone column is a coincidence.
        image = self.desktop("terminal")
        ImageDraw.Draw(image).rectangle([700, 0, 700, 1199], fill=(10, 16, 20))
        found = self.assertFound(self.with_panel(image))
        self.assertGreater(found[0], 900, f"the stray column was included: {found}")

    def test_a_panel_and_a_window_together_still_finds_the_panel(self):
        self.assertFound(self.with_panel(self.desktop("window")))


@unittest.skipIf(Image is None, "Pillow is only needed to take screenshots")
class RunTests(unittest.TestCase):
    def setUp(self):
        self.shots = load()

    def test_a_column_of_one_colour_is_only_an_edge_when_it_is_tall(self):
        image = Image.new("RGB", (400, 800), (18, 20, 24))
        draw = ImageDraw.Draw(image)
        draw.rectangle([100, 10, 102, 10 + self.shots.MIN_RUN - 20], fill=(200, 0, 0))
        runs = self.shots.runs_by_colour(image)
        self.assertNotIn((200, 0, 0), runs)
        draw.rectangle([100, 10, 102, 10 + self.shots.MIN_RUN + 20], fill=(200, 0, 0))
        self.assertIn((200, 0, 0), self.shots.runs_by_colour(image))

    def test_a_run_reaching_the_bottom_edge_still_counts(self):
        image = Image.new("RGB", (400, 800), (18, 20, 24))
        ImageDraw.Draw(image).rectangle([100, 100, 102, 799], fill=(200, 0, 0))
        self.assertIn((200, 0, 0), self.shots.runs_by_colour(image))


if __name__ == "__main__":
    unittest.main()
