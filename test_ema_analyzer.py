import os
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

from candle_tracker import load_image
from ema_analyzer import EMAAnalyzer, draw_debug, main
from live_ema_analysis import LiveEMAAnalysis
from PIL import Image


def chart(relation="ABOVE", slope=.15, gap=False, offset=None):
    image = np.full((260, 500, 3), (26, 20, 12), np.uint8)
    offset = offset if offset is not None else -24 if relation == "ABOVE" else 24
    for x in range(25, 475):
        y = round(150 - slope*(x-25))
        if not gap or x % 100 not in range(40, 46):
            cv2.circle(image, (x, y), 1, (0, 170, 255), -1)
            cv2.circle(image, (x, y+offset), 1, (255, 220, 0), -1)
    cv2.rectangle(image, (70, 20), (100, 70), (80, 158, 45), -1)
    cv2.line(image, (490, 0), (490, 259), (65, 65, 210), 2)
    cv2.putText(image, "CALL 123", (10, 240), cv2.FONT_HERSHEY_SIMPLEX, .5, (255, 220, 0), 1)
    return image


class EMATests(unittest.TestCase):
    def test_screen_y_and_slope_signs_distance_debug_and_no_mutation(self):
        for relation, slope, state, direction in (
            ("ABOVE", .15, "BULLISH", "POSITIVE"),
            ("BELOW", -.15, "BEARISH", "NEGATIVE"),
        ):
            with self.subTest(state=state):
                frame = chart(relation, slope)
                original = frame.copy()
                result = EMAAnalyzer().update(frame, 1.)
                self.assertEqual(result.market_state, state)
                self.assertEqual(result.current_relation, relation)
                self.assertEqual(result.fast_slope, direction)
                self.assertAlmostEqual(result.fast_slope_value, slope, delta=.01)
                self.assertAlmostEqual(result.distance, 24., delta=1)
                self.assertGreaterEqual(result.confidence, 75)
                self.assertFalse(result.crossover)
                self.assertGreater(draw_debug(frame, result).shape[0], frame.shape[0])
                np.testing.assert_array_equal(frame, original)

    def test_gaps_noise_and_translation(self):
        frame = chart(gap=True)
        result = EMAAnalyzer().update(frame, 1.)
        self.assertEqual(result.market_state, "BULLISH")
        shifted = cv2.warpAffine(frame, np.float32([[1, 0, 8], [0, 1, 15]]), (500, 260))
        translated = EMAAnalyzer().update(shifted, 1.)
        self.assertEqual(translated.market_state, result.market_state)
        self.assertAlmostEqual(translated.distance, result.distance, delta=1)

    def test_tracking_recovery_loss_and_recalibration(self):
        analyzer = EMAAnalyzer()
        frame = chart()
        analyzer.update(frame, 1.)
        self.assertEqual(analyzer.update(frame, 1.2).search_stage, "TRACKING")
        moved = cv2.warpAffine(frame, np.float32([[1, 0, 0], [0, 1, 20]]), (500, 260))
        result = analyzer.update(moved, 1.4)
        self.assertEqual(result.search_stage, "RECOVERY")
        for timestamp in (1.6, 1.8, 2.):
            lost = analyzer.update(np.zeros_like(frame), timestamp)
            self.assertEqual(lost.market_state, "UNKNOWN")
            self.assertEqual(lost.fast_points, [])
            self.assertIsNone(lost.fast_above_slow)
        self.assertEqual(analyzer.update(frame, 2.2).search_stage, "FULL")

    def test_small_xy_shift_keeps_tracking_and_does_not_invent_cross(self):
        analyzer = EMAAnalyzer()
        frame = chart()
        analyzer.update(frame, 1.)
        moved = cv2.warpAffine(frame, np.float32([[1, 0, 5], [0, 1, 3]]), (500, 260))
        result = analyzer.update(moved, 1.2)
        self.assertEqual(result.search_stage, "TRACKING")
        self.assertEqual(result.market_state, "BULLISH")
        self.assertFalse(result.crossover)

    def test_cli_writes_debug_json_without_mutating_capture(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "chart.png"
            output = Path(directory) / "debug.png"
            cv2.imencode(".png", chart())[1].tofile(source)
            original = source.read_bytes()
            with patch("sys.argv", ["test_ema_analyzer.py", "--image", str(source),
                                   "--next-image", str(source), "--output", str(output)]):
                self.assertEqual(main(), 0)
            results = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(len(results), 2)
            self.assertEqual(results[1]["search_stage"], "TRACKING")
            self.assertEqual(results[1]["fast_period"], 9)
            self.assertFalse(results[1]["crossover"])
            self.assertIsInstance(results[1]["confidence"], float)
            self.assertTrue(output.is_file())
            self.assertEqual(source.read_bytes(), original)

    def test_cross_requires_frames_and_does_not_repeat(self):
        for initial, final, crossover in (("BELOW", "ABOVE", "CALL"), ("ABOVE", "BELOW", "PUT")):
            with self.subTest(crossover=crossover):
                analyzer = EMAAnalyzer()
                for index in range(3):
                    self.assertFalse(analyzer.update(chart(initial), 1+index*.2).crossover)
                sign = 1 if initial == "BELOW" else -1
                analyzer.update(chart(offset=sign*8), 1.6)
                for index in range(3):
                    result = analyzer.update(chart(offset=-sign*8), 1.8+index*.2)
                    self.assertEqual(result.crossover, index == 2)
                self.assertEqual(result.crossover_detected, crossover)
                self.assertEqual(result.previous_relation, initial)
                self.assertIsNotNone(result.crossover_timestamp)
                self.assertFalse(analyzer.update(chart(offset=-sign*8), 2.4).crossover)

    def test_loss_and_time_gap_never_create_cross(self):
        analyzer = EMAAnalyzer()
        for index in range(3):
            analyzer.update(chart("BELOW"), 1+index*.2)
        analyzer.update(np.zeros((260, 500, 3), np.uint8), 1.6)
        for index in range(3):
            self.assertFalse(analyzer.update(chart(), 1.8+index*.2).crossover)
        self.assertEqual(analyzer.update(chart(), 4.).search_stage, "FULL")

    def test_blank_candles_text_and_ambiguous_lines_are_unknown(self):
        blank = np.zeros((260, 500, 3), np.uint8)
        cv2.rectangle(blank, (50, 20), (100, 100), (255, 220, 0), -1)
        cv2.putText(blank, "EMA 21", (120, 80), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 170, 255), 2)
        self.assertEqual(EMAAnalyzer().update(blank, 1.).market_state, "UNKNOWN")
        frame = chart()
        cv2.line(frame, (25, 20), (475, 20), (255, 220, 0), 1)
        self.assertEqual(EMAAnalyzer().update(frame, 1.).market_state, "UNKNOWN")

    def test_invalid_clock_roi_and_image(self):
        analyzer = EMAAnalyzer()
        frame = chart()
        analyzer.update(frame, 1.)
        for timestamp in (1., .9, float("nan")):
            with self.assertRaises(ValueError):
                analyzer.update(frame, timestamp)
        with self.assertRaises(ValueError):
            EMAAnalyzer().update(frame, 1., (-1, 0, 20, 20))

    def test_sideways_distance_boundaries_and_increasing_distance(self):
        for distance, category in ((4, "CLOSE"), (8, "NORMAL"), (12, "SEPARATED")):
            with self.subTest(distance=distance):
                result = EMAAnalyzer().update(chart(slope=0., offset=-distance), 1.)
                self.assertEqual(result.distance_class, category)
                self.assertEqual(result.fast_slope, "FLAT")
                self.assertEqual(result.market_state, "SIDEWAYS")
        analyzer = EMAAnalyzer()
        analyzer.update(chart(offset=-16), 1.)
        result = analyzer.update(chart(offset=-24), 1.2)
        self.assertTrue(result.distance_increasing)
        self.assertEqual(result.market_state, "BULLISH")

    def test_low_confidence_exact_threshold_and_short_reversal(self):
        frame = chart()
        confidence = EMAAnalyzer().update(frame, 1.).confidence
        with patch("config.EMA_MIN_CONFIDENCE", confidence):
            self.assertNotEqual(EMAAnalyzer().update(frame, 1.).market_state, "UNKNOWN")
        with patch("config.EMA_MIN_CONFIDENCE", confidence+.01):
            result = EMAAnalyzer().update(frame, 1.)
            self.assertEqual(result.market_state, "UNKNOWN")
            self.assertFalse(result.crossover)
        analyzer = EMAAnalyzer()
        for index in range(3):
            analyzer.update(chart(offset=8), 1+index*.2)
        self.assertFalse(analyzer.update(chart(offset=-8), 1.6).crossover)
        self.assertFalse(analyzer.update(chart(offset=8), 1.8).crossover)

    def test_roi_coordinates_reset_and_truncated_recent_curve(self):
        frame = chart()
        padded = cv2.copyMakeBorder(frame, 20, 10, 30, 10, cv2.BORDER_CONSTANT)
        analyzer = EMAAnalyzer()
        result = analyzer.update(padded, 1., (30, 20, 530, 280))
        self.assertAlmostEqual(result.distance, 24, delta=1)
        self.assertEqual(result.analysis_x, 505)
        self.assertEqual(analyzer.update(frame, 1.2).search_stage, "FULL")
        mask = cv2.inRange(frame, np.array((200, 180, 0)), np.array((255, 255, 10)))
        mask[:, :350] = 0
        frame[mask > 0] = 0
        result = EMAAnalyzer().update(frame, 1.)
        self.assertEqual(result.market_state, "UNKNOWN")
        self.assertIn("Extremos", result.reason)


class LiveEMAIntegrationTests(unittest.TestCase):
    def test_pil_adapter_and_debug_disabled_preserve_source(self):
        adapter = LiveEMAAnalysis()
        frame = Image.fromarray(cv2.cvtColor(chart(), cv2.COLOR_BGR2RGB))
        source = np.array(frame).copy()
        result = adapter.update(frame, 1.)
        self.assertEqual(result.market_state, "BULLISH")
        adapter.overlay(frame)
        np.testing.assert_array_equal(np.array(frame), source)
        with patch("config.DEBUG_MODE", False):
            self.assertIs(adapter.overlay(frame), frame)
        adapter.reset()
        self.assertIsNone(adapter.result)

    def test_ema_error_is_reported_without_disarming_execution(self):
        from bot import App
        app = App.__new__(App)
        app.ema_analysis = Mock()
        app.ema_analysis.update.side_effect = ValueError("Frame invalido")
        app.ema_status = Mock()
        app.execution = Mock(armed=True)
        with self.assertLogs("iq_option_bot", level="ERROR"):
            app.update_ema_analysis(Image.new("RGB", (10, 10)))
        app.ema_analysis.reset.assert_called_once()
        self.assertIsNone(app.ema_result)
        self.assertTrue(app.execution.armed)
        self.assertIn("Frame invalido", app.ema_status.set.call_args.args[0])

    def test_overlay_error_preserves_original_preview_and_execution(self):
        from bot import App
        app = App.__new__(App)
        app.ema_analysis = Mock()
        app.ema_analysis.overlay.side_effect = cv2.error("Overlay invalido")
        app.ema_status = Mock()
        app.visual_reference = None
        app.execution = Mock(armed=True)
        image = Image.new("RGB", (50, 50))
        with self.assertLogs("iq_option_bot", level="ERROR"), patch("bot.make_preview") as preview:
            app.reference_preview(image, None)
        preview.assert_called_once_with(image, None, "white")
        self.assertTrue(app.execution.armed)
        self.assertIsNone(app.ema_result)


@unittest.skipUnless(os.environ.get("IQ_OPTION_EMA_IMAGE"), "Captura EMA no especificada.")
class RealEMATests(unittest.TestCase):
    def test_shared_cyan_and_orange_curves(self):
        frame = load_image(Path(os.environ["IQ_OPTION_EMA_IMAGE"]))
        for scale in (.75, 1., 1.5):
            with self.subTest(scale=scale):
                image = cv2.resize(frame, None, fx=scale, fy=scale)
                result = EMAAnalyzer().update(image, 1.)
                self.assertEqual(result.current_relation, "ABOVE")
                self.assertEqual(result.fast_slope, "POSITIVE")
                self.assertEqual(result.slow_slope, "POSITIVE")
                self.assertEqual(result.market_state, "BULLISH")
                self.assertTrue(result.spatial_crossing)
                self.assertFalse(result.crossover)
                self.assertAlmostEqual(result.distance / scale, 69., delta=4)
                self.assertGreater(result.confidence, 85)
                analyzer = EMAAnalyzer()
                for index in range(4):
                    self.assertFalse(analyzer.update(image, 1+index*.2).crossover)


if __name__ == "__main__":
    if "--image" in sys.argv:
        raise SystemExit(main())
    unittest.main()
