"""Finding the panel in a screenshot, without asking it where it is.

The panel is drawn inside a full-screen layer surface, so there is no window
geometry to ask Hyprland for, and looking for its border finds the focused
terminal's border just as readily: they are the same accent colour. So the
screen is captured with the panel closed and again with it open, and the panel
is whatever changed.
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
    def setUp(self):
        self.shots = load()
        self.desktop = Image.new("RGB", (1920, 1200), (20, 22, 26))
        draw = ImageDraw.Draw(self.desktop)
        for x in range(0, 1920, 7):
            draw.line([(x, 0), (x, 1200)], fill=(28, 30, 34))

    def with_panel(self, box=(1250, 40, 1700, 900), clock=True):
        after = self.desktop.copy()
        draw = ImageDraw.Draw(after)
        draw.rectangle(list(box), fill=(16, 18, 22), outline=(80, 160, 220), width=2)
        if clock:
            # Something small that changed for its own reasons.
            draw.rectangle([900, 8, 960, 24], fill=(200, 200, 200))
        return after

    def test_the_panel_is_found_wherever_on_the_bar_the_widget_sits(self):
        for left in (700, 1250, 1400):
            with self.subTest(left=left):
                box = (left, 40, left + 440, 900)
                found = self.shots.changed_box(self.desktop, self.with_panel(box))
                self.assertIsNotNone(found)
                for got, want in zip(found, box):
                    self.assertLessEqual(abs(got - want), 4)

    def test_a_ticking_clock_does_not_widen_the_crop(self):
        found = self.shots.changed_box(self.desktop, self.with_panel())
        self.assertGreater(found[0], 1000, "the clock at x=900 was included")

    def test_a_panel_that_never_opened_is_reported_rather_than_cropped(self):
        # Which is what happens with no headset connected: the widget is hidden
        # and there is nothing to open.
        self.assertIsNone(self.shots.changed_box(self.desktop, self.desktop.copy()))

    def test_a_smear_of_noise_is_not_mistaken_for_a_panel(self):
        after = self.desktop.copy()
        draw = ImageDraw.Draw(after)
        draw.rectangle([10, 10, 80, 60], fill=(255, 255, 255))
        self.assertIsNone(self.shots.changed_box(self.desktop, after))


if __name__ == "__main__":
    unittest.main()
