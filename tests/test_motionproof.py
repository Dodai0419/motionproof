import contextlib, io, json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import motionproof as mp

EASE = mp.EASINGS["cubic-bezier(0.33,1,0.68,1) easeOutCubic"]


def solid(r, g, b, w=8, h=8):
    return bytes((r, g, b)) * (w * h)


class PixelRules(unittest.TestCase):
    """The pixelmatch 8 rules that reanimate relies on."""

    def test_identical_frames_have_no_mismatch(self):
        self.assertEqual(mp.mismatch(solid(10, 20, 30), solid(10, 20, 30), 8, 8), 0.0)

    def test_black_vs_white_is_all_different(self):
        self.assertEqual(mp.mismatch(solid(0, 0, 0), solid(255, 255, 255), 8, 8), 1.0)

    def test_small_colour_shift_stays_under_threshold(self):
        self.assertEqual(mp.mismatch(solid(100, 100, 100), solid(103, 103, 103), 8, 8), 0.0)

    def test_threshold_zero_sees_the_small_shift(self):
        self.assertEqual(mp.mismatch(solid(100, 100, 100), solid(103, 103, 103), 8, 8, limit=0.0), 1.0)

    def test_oklab_white_has_lightness_one(self):
        self.assertAlmostEqual(mp._oklab(255, 255, 255)[3], 1.0, places=4)

    def test_antialiased_edge_pixel_is_not_counted(self):
        # A hard vertical edge against the same edge with a grey ramp pixel column: the ramp is anti-aliasing.
        w, h = 12, 12
        row_a = bytes((0, 0, 0)) * 6 + bytes((255, 255, 255)) * 6
        row_b = bytes((0, 0, 0)) * 5 + bytes((128, 128, 128)) + bytes((255, 255, 255)) * 6
        self.assertEqual(mp.mismatch(row_a * h, row_b * h, w, h), 0.0)

    def test_memo_gives_the_same_answer_as_a_fresh_count(self):
        a = mp.scene(300, EASE)
        b = mp.scene(380, EASE)
        first = mp.mismatch(a, b, 480, 200)
        mp._pairs.clear(); mp._rows.clear()
        self.assertEqual(mp.mismatch(a, b, 480, 200), first)
        self.assertGreater(first, 0)


class Timing(unittest.TestCase):
    def test_motion_window(self):
        win = mp.motion_window([0, 0, 0.01, 0.05, 0.02, 0, 0], 25)
        self.assertEqual((win["first"], win["last"], win["peak"]), (2, 4, 3))
        self.assertAlmostEqual(win["durationS"], 0.12)

    def test_still_clip_has_no_window(self):
        self.assertEqual(mp.motion_window([0, 0, 0], 25)["first"], -1)


class Easing(unittest.TestCase):
    def fit(self, fn, dur=600, n=40):
        return mp.fit_easing([[i * 25, 100 * fn(min(1, i * 25 / dur))] for i in range(n)])

    def test_names_each_common_curve(self):
        for name in ("linear", "ease-in-out", "cubic-bezier(0.33,1,0.68,1) easeOutCubic", "cubic-bezier(0.16,1,0.3,1) easeOutExpo"):
            self.assertEqual(self.fit(mp.EASINGS[name])["bestFits"][0]["name"], name)

    def test_fitted_duration_is_close(self):
        self.assertAlmostEqual(self.fit(mp.EASINGS["linear"])["bestFits"][0]["durationMs"], 600, delta=30)

    def test_overshoot_reads_as_spring(self):
        r = self.fit(mp.EASINGS["cubic-bezier(0.34,1.56,0.64,1) easeOutBack"])
        self.assertIn("spring", r["verdict"])
        self.assertGreater(r["overshootPct"], 2)

    def test_delay_is_measured(self):
        series = [[i * 25, 0 if i * 25 < 200 else 100 * min(1, (i * 25 - 200) / 400)] for i in range(40)]
        self.assertAlmostEqual(mp.fit_easing(series)["delayMs"], 200, delta=25)

    def test_too_few_samples(self):
        self.assertEqual(mp.fit_easing([[0, 0], [1, 1]])["bestFits"], [])

    def test_family_of_each_kind(self):
        for name, family in (("linear", "linear"), ("ease-in", "in"), ("cubic-bezier(0.32,0,0.67,0) easeInCubic", "in"), ("ease-out", "out"), ("ease", "out"),
                             ("cubic-bezier(0.19,1,0.22,1) 'expo' (common in landing pages)", "out"), ("ease-in-out", "in-out"),
                             ("cubic-bezier(0.4,0,0.2,1) material standard", "in-out"), ("cubic-bezier(0.77,0,0.175,1) 'power3.inOut' (gsap)", "in-out"),
                             ("cubic-bezier(0.34,1.56,0.64,1) easeOutBack", "spring")):
            self.assertEqual(mp.easing_family(name), family, name)
        self.assertEqual(self.fit(mp.EASINGS["cubic-bezier(0.34,1.56,0.64,1) easeOutBack"])["family"], "spring")


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        mp.make_clip(cls.tmp / "ref", EASE, n=30, delay=200, dur=400)
        mp.make_clip(cls.tmp / "same", EASE, n=30, delay=200, dur=400)
        mp.make_clip(cls.tmp / "slow", mp.EASINGS["linear"], n=30, delay=200, dur=800)
        mp.make_clip(cls.tmp / "shifted", EASE, n=30, delay=320, dur=400)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def run_compare(self, clone):
        with contextlib.redirect_stdout(io.StringIO()):
            return mp.compare(self.tmp / "ref", self.tmp / clone, self.tmp / f"out-{clone}")

    def test_identical_copy_passes_with_zero_error(self):
        r = self.run_compare("same")
        self.assertEqual(r["meanMismatch"], 0)
        self.assertEqual(r["offset"], 0)
        self.assertTrue(r["pass"]["pixels"] and r["pass"]["timing"] and r["pass"]["motion"])
        self.assertEqual(r["motion"]["timingError"]["durationS"], 0)

    def test_slow_linear_copy_fails_timing_and_gets_a_different_easing(self):
        r = self.run_compare("slow")
        self.assertFalse(r["pass"]["timing"])
        self.assertGreater(r["motion"]["timingError"]["durationS"], 0.2)
        self.assertEqual(r["easing"]["clone"]["bestFits"][0]["name"], "linear")
        self.assertIn("easeOutCubic", r["easing"]["reference"]["verdict"])

    def test_late_copy_is_aligned_by_the_offset(self):
        r = self.run_compare("shifted")
        self.assertEqual(r["offset"], 3)  # 120 ms at 25 fps
        self.assertEqual(r["meanMismatch"], 0)
        self.assertAlmostEqual(r["motion"]["startDelayS"], 0.12)
        self.assertTrue(r["pass"]["timing"])  # after alignment, as in reanimate
        self.assertFalse(r["pass"]["start"])
        self.assertFalse(r["pass"]["motion"])

    def test_wrong_easing_over_the_same_window_fails_motion(self):
        mp.make_clip(self.tmp / "inout", mp.EASINGS["cubic-bezier(0.65,0,0.35,1) easeInOutCubic"], delay=200, dur=600)
        mp.make_clip(self.tmp / "even", mp.EASINGS["linear"], delay=200, dur=600)
        with contextlib.redirect_stdout(io.StringIO()):
            r = mp.compare(self.tmp / "inout", self.tmp / "even", self.tmp / "out-even")
        self.assertEqual((r["easing"]["reference"]["family"], r["easing"]["clone"]["family"]), ("in-out", "linear"))
        self.assertFalse(r["pass"]["easing"])
        self.assertFalse(r["pass"]["motion"])

    def test_own_colour_copy_passes_motion_and_fails_pixels(self):
        mp.make_clip(self.tmp / "orange", EASE, n=30, delay=200, dur=400, color=(232, 89, 12))
        r = self.run_compare("orange")
        self.assertTrue(r["pass"]["motion"])
        self.assertTrue(r["pass"]["easing"])

    def test_lossy_copy_stays_in_the_same_family(self):
        video = self.tmp / "lossy.mp4"
        subprocess.run([mp.FFMPEG, "-v", "error", "-y", "-framerate", "25", "-i", str(self.tmp / "ref" / "%04d.png"),
                        "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p", str(video)], check=True)
        with contextlib.redirect_stdout(io.StringIO()):
            r = mp.compare(self.tmp / "ref", video, self.tmp / "out-lossy")
        self.assertTrue(r["pass"]["easing"], r["easing"])

    def test_png_folder_with_quote_and_non_ascii_in_its_path(self):
        folder = self.tmp / "it's dir é"
        shutil.copytree(self.tmp / "ref", folder)
        frames, w, h = mp.load_frames(folder, 25)
        self.assertEqual((len(frames), w, h), (30, 480, 200))

    def test_report_files_exist_and_html_is_self_contained(self):
        self.run_compare("same")
        out = self.tmp / "out-same"
        data = json.loads((out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(data["frames"], 30)
        page = (out / "report.html").read_text(encoding="utf-8")
        self.assertIn("data:image/png;base64,", page)
        self.assertNotIn("http", page)
        self.assertTrue(list((out / "diff").glob("*.png")))

    def test_video_input_matches_png_input_timing(self):
        video = self.tmp / "ref.mp4"
        subprocess.run([mp.FFMPEG, "-v", "error", "-y", "-framerate", "25", "-i", str(self.tmp / "ref" / "%04d.png"),
                        "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv444p", str(video)], check=True)
        frames, w, h = mp.load_frames(video, 25)
        self.assertEqual((len(frames), w, h), (30, 480, 200))
        png_frames, _, _ = mp.load_frames(self.tmp / "ref", 25)
        act = lambda fs: [0.0] + [mp.mismatch(fs[i - 1], fs[i], w, h) for i in range(1, len(fs))]
        v, p = mp.motion_window(act(frames), 25), mp.motion_window(act(png_frames), 25)
        self.assertEqual((v["first"], v["last"]), (p["first"], p["last"]))

    def test_region_and_width_change_the_frame_size(self):
        self.assertEqual(mp.load_frames(self.tmp / "ref", 25, region=(0, 0, 100, 50))[1:], (100, 50))
        self.assertEqual(mp.load_frames(self.tmp / "ref", 25, width=240)[1:], (240, 100))

    def test_cli_reports_a_missing_input(self):
        p = subprocess.run([sys.executable, str(ROOT / "motionproof.py"), "compare", "nope.mp4", "nope2.mp4", "--out", str(self.tmp / "x")],
                           capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no such file", p.stderr)


if __name__ == "__main__":
    unittest.main()
