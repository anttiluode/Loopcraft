"""GUI regression tests (Windows Tk desktop, or Linux with xvfb-run).

    py -m unittest discover -s tests -v
    xvfb-run -a python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tkinter as tk
from loopcraft import LoopCraftApp


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.app = LoopCraftApp(self.root)
        self.root.update()
        self.app._schedule_still = lambda _sec: None
        self.app._schedule_detail_thumbnails = lambda: None
        self.app.source = Path(__file__)
        self.app.duration = 1436.9
        self.app.in_point = 0.0
        self.app.out_point = 8.0
        self.app.head = 0.0
        self.app._set_view(0.0, 24.0)
        self.root.update()

    def tearDown(self):
        self.app._close()

    def test_default_short_clip_is_draggable_when_zoomed(self):
        self.assertGreater(self.app._time_to_x(self.app.out_point) -
                           self.app._time_to_x(self.app.in_point), 120)
        x = self.app._time_to_x(self.app.out_point)
        self.app._on_timeline_press(SimpleNamespace(x=x))
        self.assertEqual(self.app.dragging, "out")
        self.app._on_timeline_drag(SimpleNamespace(x=x + 20))
        self.app._on_timeline_release(None)
        self.assertGreater(self.app.out_point, 8.0)

    def test_overview_moves_selection_to_middle_of_long_video(self):
        self.app._on_overview_press(SimpleNamespace(x=self.app.overview.winfo_width() / 2))
        self.assertTrue(690 < self.app.in_point < 730)
        self.assertAlmostEqual(self.app.out_point - self.app.in_point, 8.0)
        self.assertTrue(self.app.view_start <= self.app.in_point < self.app.out_point <= self.app.view_end)

    def test_mark_in_and_out_use_playhead(self):
        self.app.head = 300.0
        self.app.mark_in()
        self.assertAlmostEqual(self.app.in_point, 300)
        self.assertAlmostEqual(self.app.out_point, 308)
        self.app.head = 302.0
        self.app.mark_out()
        self.assertAlmostEqual(self.app.out_point, 302)

    def test_typed_times_can_move_both_endpoints(self):
        self.app.in_var.set("05:05")
        self.app.out_var.set("05:08")
        self.assertTrue(self.app.apply_times())
        self.assertAlmostEqual(self.app.in_point, 305)
        self.assertAlmostEqual(self.app.out_point, 308)

    def test_full_and_selection_zoom(self):
        self.app.zoom_full()
        self.assertAlmostEqual(self.app.view_end, self.app.duration)
        self.app.zoom_to_selection()
        self.assertLess(self.app.view_end - self.app.view_start, 30.0)

    def test_export_is_visible_on_smaller_window(self):
        self.root.geometry("1100x730")
        self.root.update()
        self.assertTrue(self.app.export_button.winfo_ismapped())
        self.assertGreater(self.app.export_button.winfo_height(), 30)
        self.assertLess(self.app.export_button.winfo_rooty() +
                        self.app.export_button.winfo_height(),
                        self.root.winfo_rooty() + self.root.winfo_height())
        self.assertTrue(self.app.overview.winfo_ismapped())


if __name__ == "__main__":
    unittest.main()
