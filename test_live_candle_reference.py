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
from live_ema_analysis import LiveEMAAnalysis
from monitors import Monitor
from test_bot import arrow_image
from test_candle_detector import synthetic_chart
from ema_analyzer import EMAResult
import config

COLORS = ((48, 164, 108), (229, 72, 77))


def pil_chart():
    return Image.fromarray(cv2.cvtColor(synthetic_chart(), cv2.COLOR_BGR2RGB))


class LiveReferenceTests(unittest.TestCase):
    def ema_chart(self, state, direction=None):
        bgr = synthetic_chart()
        if state != "UNKNOWN":
            fast, slow = [], []
            for x in range(20, 575):
                if state == "BULLISH":
                    fy, sy = 270-.08*x, 280-.04*x
                elif state == "BEARISH":
                    fy, sy = 230+.04*x, 210+.03*x
                else:
                    fy, sy = 250, 247
                fast.append((x, round(fy)))
                slow.append((x, round(sy)))
            import numpy as np
            cv2.polylines(bgr, [np.array(fast, np.int32)], False, (255, 220, 0), 1)
            cv2.polylines(bgr, [np.array(slow, np.int32)], False, (0, 170, 255), 1)
        image = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        if direction:
            image.paste(arrow_image(direction).crop((10, 10, 27, 27)), (342, 190))
        return image

    def replay_ema_frames(self, app, frames):
        with (
            patch("bot.ImageGrab.grab", side_effect=frames),
            patch("bot.list_monitors", return_value=app.monitors),
            patch("bot.ImageTk.PhotoImage"),
            patch("bot.time.time", return_value=1_800_000_002),
            patch("bot.time.monotonic", side_effect=itertools.count(10, .001)),
            patch("bot.write_event") as write,
            patch("bot.messagebox.showerror") as error,
        ):
            for _ in range(5):
                app.tick()
        error.assert_not_called()
        self.assertTrue(app.execution.armed)
        self.assertEqual(app.root.after.call_count, 5)
        return write

    def test_matching_ema_and_arrow_execute_once_with_final_ema_check(self):
        for direction, state in (("CALL", "BULLISH"), ("PUT", "BEARISH")):
            with self.subTest(direction=direction):
                app = self.app()
                blank = self.ema_chart(state)
                arrow = self.ema_chart(state, direction)
                write = self.replay_ema_frames(app, [blank, blank, arrow, arrow, arrow, arrow])
                self.assertEqual(app.ema_result.market_state, state)
                app.execution.submit.assert_called_once_with(direction, 1_800_000_002, 300)
                write.assert_called_once()

    def test_opposite_sideways_unknown_block_without_event_or_disarming(self):
        for direction, state in (
            ("CALL", "BEARISH"), ("PUT", "BULLISH"),
            ("CALL", "SIDEWAYS"), ("PUT", "SIDEWAYS"),
            ("CALL", "UNKNOWN"), ("PUT", "UNKNOWN"),
        ):
            with self.subTest(direction=direction, state=state):
                app = self.app()
                blank, arrow = self.ema_chart(state), self.ema_chart(state, direction)
                write = self.replay_ema_frames(app, [blank, blank, arrow, arrow, arrow])
                app.execution.submit.assert_not_called()
                write.assert_not_called()
                self.assertFalse(app.gate.emitted)
                self.assertEqual(app.gate.consecutive, 0)
                self.assertIn("sin entrada", app.status.set.call_args.args[0].lower())

    def test_final_ema_loss_or_reversal_cancels_without_retry(self):
        for state in ("UNKNOWN", "BEARISH", "SIDEWAYS"):
            with self.subTest(state=state):
                app = self.app()
                blank, arrow = self.ema_chart("BULLISH"), self.ema_chart("BULLISH", "CALL")
                final = self.ema_chart(state, "CALL")
                self.replay_ema_frames(app, [blank, blank, arrow, arrow, arrow, final])
                app.execution.submit.assert_not_called()
                self.assertTrue(app.gate.emitted)
                self.assertIn("Captura final", app.order_status.set.call_args.args[0])
                with (
                    patch("bot.ImageGrab.grab", return_value=arrow),
                    patch("bot.list_monitors", return_value=app.monitors),
                    patch("bot.ImageTk.PhotoImage"),
                    patch("bot.time.time", return_value=1_800_000_003),
                    patch("bot.time.monotonic", side_effect=itertools.count(11, .001)),
                    patch("bot.write_event") as write,
                ):
                    app.tick()
                app.execution.submit.assert_not_called()
                write.assert_not_called()

    def test_filter_exact_confidence_freshness_and_crossing(self):
        app = self.app()
        for direction, expected in (("CALL", "BULLISH"), ("PUT", "BEARISH")):
            for state in ("BULLISH", "BEARISH", "CROSSING", "SIDEWAYS", "UNKNOWN"):
                for confidence in (config.EMA_MIN_CONFIDENCE-.001, config.EMA_MIN_CONFIDENCE):
                    with self.subTest(direction=direction, state=state, confidence=confidence):
                        app.ema_result = EMAResult(10., market_state=state, confidence=confidence)
                        with patch("bot.time.monotonic", return_value=10.1):
                            reason = app.ema_entry_block_reason(direction)
                        self.assertEqual(not reason, state == expected and confidence >= config.EMA_MIN_CONFIDENCE)
        app.ema_result = EMAResult(10., market_state="BULLISH", confidence=99.)
        with patch("bot.time.monotonic", return_value=11.001):
            self.assertIn("desactualizada", app.ema_entry_block_reason("CALL"))
        app.ema_analysis = None
        with patch("bot.time.monotonic", return_value=10.1):
            self.assertIn("no disponible", app.ema_entry_block_reason("CALL"))

    def test_ema_recovery_requires_three_matching_frames_without_false_rearm(self):
        app = self.app()
        blank = self.ema_chart("BULLISH")
        blocked = self.ema_chart("UNKNOWN", "CALL")
        good = self.ema_chart("BULLISH", "CALL")
        with (
            patch("bot.ImageGrab.grab", side_effect=[blank, blank, good, good, blocked, good, good, good, good]),
            patch("bot.list_monitors", return_value=app.monitors),
            patch("bot.ImageTk.PhotoImage"),
            patch("bot.time.time", return_value=1_800_000_002),
            patch("bot.time.monotonic", side_effect=itertools.count(10, .001)),
            patch("bot.write_event") as write,
        ):
            for _ in range(7):
                app.tick()
                app.execution.submit.assert_not_called()
            self.assertEqual(app.gate.consecutive, 2)
            app.tick()
        app.execution.submit.assert_called_once()
        write.assert_called_once()
        # A visible but EMA-blocked arrow is never a valid absence to rearm.
        app = self.app()
        self.replay_ema_frames(app, [blocked]*5)
        self.assertFalse(app.gate.armed)
        self.assertEqual(app.gate.absent, 0)

    def test_matching_ema_does_not_override_pending_or_disarmed_execution(self):
        for pending in (True, False):
            with self.subTest(pending=pending):
                app = self.app()
                if pending:
                    app.ledger.pending.return_value = {"id": "existing"}
                else:
                    app.execution.armed = False
                blank, arrow = self.ema_chart("BULLISH"), self.ema_chart("BULLISH", "CALL")
                with (
                    patch("bot.ImageGrab.grab", side_effect=[blank, blank, arrow, arrow, arrow]),
                    patch("bot.list_monitors", return_value=app.monitors),
                    patch("bot.ImageTk.PhotoImage"),
                    patch("bot.time.time", return_value=1_800_000_002),
                    patch("bot.time.monotonic", side_effect=itertools.count(10, .001)),
                    patch("bot.write_event"),
                ):
                    for _ in range(5):
                        app.tick()
                app.execution.submit.assert_not_called()

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
        app.ema_analysis = LiveEMAAnalysis()
        app.ema_result = None
        app.ema_status = Mock()
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
                    patch("config.EMA_ENTRY_FILTER_ENABLED", False),
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
                    self.assertEqual(app.ema_result.market_state, "UNKNOWN")
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
