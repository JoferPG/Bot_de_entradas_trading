import itertools
import os
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
from PIL import Image

from bot import App, SignalGate, template_from
from candle_tracker import load_image
from live_candle_reference import LiveCandleReference, ReferenceUnavailable
from monitors import Monitor
from test_bot import arrow_image
from test_candle_detector import synthetic_chart

COLORS = ((48, 164, 108), (229, 72, 77))


def pil_chart():
    return Image.fromarray(cv2.cvtColor(synthetic_chart(), cv2.COLOR_BGR2RGB))


class LiveReferenceTests(unittest.TestCase):
    def test_global_score_threshold_is_enforced_at_exact_boundary(self):
        reference = LiveCandleReference(COLORS)
        reference.locate(pil_chart(), 1., 100)
        good = reference.result
        for score in (.7999, .80):
            with self.subTest(score=score), patch.object(
                reference.tracker, "update", return_value=replace(good, global_confidence=score),
            ):
                if score < .80:
                    with self.assertRaisesRegex(ReferenceUnavailable, "insuficiente"):
                        reference.locate(pil_chart(), 1.2, 100)
                else:
                    self.assertEqual(reference.locate(pil_chart(), 1.3, 100), (347., 353.))

    def test_valid_three_objects_use_candle_center_not_off_center_point(self):
        image = Image.fromarray(cv2.cvtColor(synthetic_chart(120, 360), cv2.COLOR_BGR2RGB))
        reference = LiveCandleReference(COLORS)
        self.assertEqual(reference.locate(image, 1., 100), (347., 353.))
        self.assertGreater(reference.result.point.x, 350)
        self.assertIn("Confianza visual", reference.report)

    def test_missing_objects_never_return_retained_coordinate(self):
        for scenario in ("line", "point", "candle"):
            with self.subTest(scenario=scenario):
                reference = LiveCandleReference(COLORS)
                self.assertEqual(reference.locate(pil_chart(), 1., 100), (347., 353.))
                bgr = synthetic_chart()
                if scenario == "line":
                    cv2.rectangle(bgr, (387, 35), (393, 275), (26, 20, 12), -1)
                elif scenario == "point":
                    cv2.rectangle(bgr, (345, 115), (355, 125), (108, 164, 48), -1)
                else:
                    cv2.rectangle(bgr, (328, 90), (373, 175), (26, 20, 12), -1)
                image = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
                with self.assertRaises(ReferenceUnavailable):
                    reference.locate(image, 1.2, 100)

    def test_bucket_change_reacquires_and_does_not_measure_cross_candle_motion(self):
        reference = LiveCandleReference(COLORS)
        reference.locate(pil_chart(), 1., 100)
        old = reference.tracker
        reference.locate(pil_chart(), 1.2, 101)
        self.assertIsNot(reference.tracker, old)
        self.assertEqual(reference.result.point_search_stage, "INITIAL")
        self.assertIsNone(reference.result.motion)

    def app(self):
        app = App.__new__(App)
        app.gate = SignalGate(300)
        app.region = (0, 0, 600, 300)
        app.monitors = [Monitor(app.region, True)]
        app.monitor_index = 0
        app.candle_colors = COLORS
        app.visual_reference = LiveCandleReference(COLORS)
        app.templates = {direction: template_from(arrow_image(direction), direction) for direction in ("CALL", "PUT")}
        app.running_asset = "NO_IDENTIFICADO"
        app.running_period = 300
        app.log_path = Path("unused.csv")
        app.job = None
        for name in (
            "root", "execution", "ledger", "status", "order_status", "reference_status",
            "signal_text", "signal_panel", "preview_panel", "summary_text",
        ):
            setattr(app, name, Mock())
        app.ledger.pending.return_value = None
        app.execution.armed = True
        app.execution.session_losses = 0
        app.schedule_results = Mock()
        return app

    def test_full_tick_requires_three_arrow_frames_and_fresh_three_objects(self):
        for final_missing in (False, True):
            with self.subTest(final_missing=final_missing):
                app = self.app()
                blank = pil_chart()
                signal = blank.copy()
                arrow = arrow_image().crop((10, 10, 27, 27))
                signal.paste(arrow, (342, 190))
                final = signal.copy()
                if final_missing:
                    bgr = synthetic_chart()
                    cv2.rectangle(bgr, (387, 35), (393, 275), (26, 20, 12), -1)
                    final = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
                    final.paste(arrow, (342, 190))
                with (
                    patch("bot.ImageGrab.grab", side_effect=[blank, blank, signal, signal, signal, final]),
                    patch("bot.list_monitors", return_value=app.monitors),
                    patch("bot.ImageTk.PhotoImage"),
                    patch("bot.time.time", return_value=1_800_000_002),
                    patch("bot.time.monotonic", side_effect=itertools.count(10, .02)),
                    patch("bot.write_event") as write,
                    patch("bot.messagebox.showerror") as error,
                ):
                    for _ in range(4):
                        app.tick()
                        app.execution.submit.assert_not_called()
                    app.tick()
                if final_missing:
                    app.execution.submit.assert_not_called()
                    self.assertFalse(app.gate.armed)
                else:
                    app.execution.submit.assert_called_once()
                    app.schedule_results.assert_called_once()
                write.assert_called_once()
                error.assert_not_called()
                self.assertEqual(app.root.after.call_count, 5)

    def test_missing_reference_continues_scanning_without_signal_or_click(self):
        app = self.app()
        image = Image.new("RGB", (600, 300), "#101827")
        with (
            patch("bot.ImageGrab.grab", return_value=image),
            patch("bot.list_monitors", return_value=app.monitors),
            patch("bot.ImageTk.PhotoImage"),
            patch("bot.time.time", return_value=1_800_000_002),
            patch("bot.time.monotonic", return_value=10.),
            patch("bot.detect") as detect,
            patch("bot.write_event") as write,
        ):
            app.tick()
        detect.assert_not_called()
        write.assert_not_called()
        app.execution.submit.assert_not_called()
        self.assertTrue(app.execution.armed)
        self.assertFalse(app.gate.armed)
        app.root.after.assert_called_once_with(100, app.tick)
        self.assertIn("Linea de expiracion", app.status.set.call_args.args[0])


class RealLiveTests(unittest.TestCase):
    def test_both_shared_captures_with_calibrated_colors_and_full_crop_roi(self):
        paths = [os.environ.get("IQ_OPTION_REFERENCE_IMAGE"), os.environ.get("IQ_OPTION_OPENING_IMAGE")]
        if not all(paths):
            self.skipTest("Especifica las dos capturas reales.")
        for path, center in zip(paths, (984.5, 672.5)):
            with self.subTest(path=path):
                bgr = load_image(Path(path))
                image = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
                reference = LiveCandleReference(((45, 158, 107), (228, 74, 78)))
                band = reference.locate(image, 1., 100)
                self.assertAlmostEqual(sum(band)/2, center, delta=2)
                self.assertTrue(reference.result.spatial_agreement)
                self.assertGreater(reference.result.global_confidence, .80)


if __name__ == "__main__":
    unittest.main()
