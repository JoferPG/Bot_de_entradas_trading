import unittest
import os
from pathlib import Path

import numpy as np

from candle_tracker import CandleTracker, chart_roi, draw_debug, load_image, main
from test_candle_detector import synthetic_chart


class TrackerTests(unittest.TestCase):
    def test_point_right_of_crossing_line_is_still_spatially_valid(self):
        import cv2
        image = synthetic_chart()
        cv2.line(image, (390, 40), (390, 268), (26, 20, 12), 5)
        cv2.line(image, (335, 40), (335, 268), (65, 65, 210), 2)
        result = CandleTracker().update(image, 1., (5, 40, 575, 270))
        self.assertTrue(result.spatial_agreement)
        self.assertGreater(result.point.x, result.expiration.x)
        self.assertLess(result.candle.body[0], result.expiration.x)
        self.assertGreater(result.candle.body[2], result.expiration.x)

    def test_three_objects_agree_and_debug_does_not_mutate_source(self):
        image = synthetic_chart()
        original = image.copy()
        result = CandleTracker().update(image, 1., (5, 40, 575, 270))
        self.assertTrue(result.spatial_agreement)
        self.assertGreater(result.global_confidence, .85)
        self.assertEqual(result.reasons, ())
        output = draw_debug(image, result)
        self.assertGreater(output.shape[0], image.shape[0])
        np.testing.assert_array_equal(image, original)

    def test_blank_input_explicitly_low_confidence(self):
        image = np.zeros((300, 600, 3), dtype=np.uint8)
        result = CandleTracker().update(image, 1.)
        self.assertEqual(result.point_state, "LOW_CONFIDENCE")
        self.assertFalse(result.spatial_agreement)
        self.assertEqual(result.global_confidence, 0.)
        self.assertEqual(len(result.reasons), 3)

    def test_roi_rejects_invalid_area(self):
        for roi in ((0, 0, 0, 20), (-1, 0, 20, 20), (0, 0, 601, 300)):
            with self.assertRaises(ValueError):
                chart_roi(synthetic_chart(), roi)


@unittest.skipUnless(os.environ.get("IQ_OPTION_REFERENCE_IMAGE"), "Captura real no especificada.")
class RealCaptureTests(unittest.TestCase):
    def test_shared_reference_geometry_at_three_scales(self):
        image = load_image(Path(os.environ["IQ_OPTION_REFERENCE_IMAGE"]))
        import cv2
        for scale in (.75, 1., 1.5):
            with self.subTest(scale=scale):
                frame = cv2.resize(image, None, fx=scale, fy=scale)
                result = CandleTracker().update(frame, 1.)
                self.assertTrue(result.spatial_agreement)
                self.assertGreater(result.global_confidence, .85)
                self.assertAlmostEqual(result.expiration.x / scale, 1024.5, delta=2)
                self.assertAlmostEqual(result.point.x / scale, 984.5, delta=2)
                self.assertAlmostEqual(result.point.y / scale, 298.5, delta=2)
                self.assertEqual(result.candle.color, "GREEN")
                for actual, expected in zip(result.candle.bounding_box, (956, 277, 1014, 355)):
                    self.assertAlmostEqual(actual / scale, expected, delta=2)
                for actual, expected in zip(result.candle.body, (956, 300, 1014, 352)):
                    self.assertAlmostEqual(actual / scale, expected, delta=2)
                self.assertIsNotNone(result.candle.upper_wick)
                self.assertIsNotNone(result.candle.lower_wick)
                self.assertIsNone(result.motion)


@unittest.skipUnless(os.environ.get("IQ_OPTION_OPENING_IMAGE"), "Captura de apertura no especificada.")
class OpeningCaptureTests(unittest.TestCase):
    def test_red_opening_body_crosses_line_at_three_scales(self):
        import cv2
        image = load_image(Path(os.environ["IQ_OPTION_OPENING_IMAGE"]))
        for scale in (.75, 1., 1.5):
            with self.subTest(scale=scale):
                frame = cv2.resize(image, None, fx=scale, fy=scale)
                result = CandleTracker().update(frame, 1.)
                self.assertTrue(result.spatial_agreement)
                self.assertGreater(result.global_confidence, .85)
                self.assertAlmostEqual(result.expiration.x / scale, 639.5, delta=2)
                self.assertAlmostEqual(result.point.x / scale, 672., delta=2)
                self.assertAlmostEqual(result.point.y / scale, 446.5, delta=2)
                self.assertEqual(result.candle.color, "RED")
                self.assertLess(result.candle.body[0], result.expiration.x)
                self.assertGreater(result.candle.body[2], result.expiration.x)
                for actual, expected in zip(result.candle.bounding_box, (633, 422, 713, 453)):
                    self.assertAlmostEqual(actual / scale, expected, delta=2)
                for actual, expected in zip(result.candle.body, (633, 423, 713, 446)):
                    self.assertAlmostEqual(actual / scale, expected, delta=2)
                self.assertIsNotNone(result.candle.lower_wick)
                self.assertIsNone(result.motion)


if __name__ == "__main__":
    raise SystemExit(main())
