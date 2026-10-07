import unittest

import cv2

from candle_tracker import CandleDetector, ExpirationLine, WhitePointTracker, main
from test_candle_detector import synthetic_chart


class WhitePointTests(unittest.TestCase):
    def setUp(self):
        self.roi = (5, 40, 575, 270)
        self.candle = CandleDetector().detect(
            synthetic_chart(), self.roi, ExpirationLine(390, 40, 270, .95),
        )
        self.tracker = WhitePointTracker(search_radius_y=5)

    def update(self, y, timestamp, x=350):
        return self.tracker.update(synthetic_chart(y, x), self.roi, self.candle, timestamp)

    def test_acquisition_local_motion_and_expanded_search(self):
        point = self.update(120, 1.)
        self.assertIsNotNone(point)
        self.assertAlmostEqual(point.x, 350, delta=1)
        self.assertEqual(self.tracker.search_stage, "INITIAL")
        self.assertIsNotNone(self.update(117, 1.2))
        self.assertEqual(self.tracker.search_stage, "LOCAL")
        self.assertAlmostEqual(self.tracker.motion.delta_y, -3, delta=.5)
        self.assertAlmostEqual(self.tracker.motion.velocity_y, -15, delta=2)
        self.assertEqual(self.tracker.motion.direction, "UP")
        self.assertIsNotNone(self.update(128, 1.4))
        self.assertEqual(self.tracker.search_stage, "EXPANDED")
        self.assertEqual(self.tracker.motion.direction, "DOWN")

    def test_off_center_point_still_near_candle(self):
        self.assertIsNotNone(self.update(120, 1., x=360))

    def test_stable_point_and_resized_frame_have_no_false_motion(self):
        self.assertIsNotNone(self.update(120, 1.))
        self.assertIsNotNone(self.update(120, 1.2))
        self.assertEqual(self.tracker.motion.direction, "STABLE")
        self.assertEqual(self.tracker.motion.velocity_y, 0.)
        image = cv2.resize(synthetic_chart(), (900, 450))
        roi = (8, 60, 862, 405)
        candle = CandleDetector().detect(image, roi, ExpirationLine(585, 60, 405, .95))
        self.assertIsNotNone(self.tracker.update(image, roi, candle, 1.4))
        self.assertIsNone(self.tracker.motion)

    def test_without_candle_or_outside_roi_no_point_is_returned(self):
        image = synthetic_chart()
        self.assertIsNone(self.tracker.update(image, self.roi, None, 1.))
        self.assertIsNone(self.tracker.update(image, (5, 40, 300, 270), self.candle, 1.2))
        self.assertTrue(self.tracker.recalibration_required)

    def test_missing_point_has_no_current_detection_then_recalibrates(self):
        self.assertIsNotNone(self.update(120, 1.))
        image = synthetic_chart()
        cv2.rectangle(image, (345, 115), (355, 125), (108, 164, 48), -1)
        self.assertIsNone(self.tracker.update(image, self.roi, self.candle, 1.2))
        self.assertEqual(self.tracker.state, "LOW_CONFIDENCE")
        self.assertTrue(self.tracker.recalibration_required)
        self.assertIsNone(self.tracker.motion)
        self.assertIsNotNone(self.update(125, 1.4))
        self.assertEqual(self.tracker.search_stage, "RECALIBRATION")
        self.assertIsNone(self.tracker.motion)

    def test_line_text_and_ambiguous_points_do_not_pass(self):
        for scenario in ("line", "text", "ambiguous"):
            tracker = WhitePointTracker()
            image = synthetic_chart()
            if scenario != "ambiguous":
                cv2.rectangle(image, (345, 115), (355, 125), (108, 164, 48), -1)
                cv2.line(image, (10, 120), (565, 120), (210, 210, 210), 1)
            if scenario == "text":
                cv2.putText(image, "123", (330, 122), cv2.FONT_HERSHEY_SIMPLEX, .4, (255, 255, 255), 1)
            elif scenario == "ambiguous":
                cv2.circle(image, (364, 120), 3, (255, 255, 255), -1)
            with self.subTest(scenario=scenario):
                self.assertIsNone(tracker.update(image, self.roi, self.candle, 1.))

    def test_timestamp_gap_resets_motion_and_invalid_time_is_explicit(self):
        self.assertIsNotNone(self.update(120, 1.))
        self.assertIsNotNone(self.update(124, 3.))
        self.assertIsNone(self.tracker.motion)
        for timestamp in (3., 2., float("nan")):
            with self.assertRaises(ValueError):
                self.update(124, timestamp)


if __name__ == "__main__":
    raise SystemExit(main("point"))
