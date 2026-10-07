import unittest

import cv2
import numpy as np

from candle_tracker import CandleDetector, ExpirationLine, main


def synthetic_chart(point_y=120, point_x=350):
    image = np.full((300, 600, 3), (26, 20, 12), dtype=np.uint8)
    for x in (110, 190, 270, 350):
        cv2.rectangle(image, (x-20, 110), (x+20, 155), (108, 164, 48), -1)
        cv2.line(image, (x, 95), (x, 170), (108, 164, 48), 2)
    cv2.line(image, (390, 40), (390, 268), (65, 65, 210), 2)
    cv2.line(image, (10, point_y), (565, point_y), (210, 210, 210), 1)
    cv2.circle(image, (point_x, point_y), 3, (255, 245, 240), -1)
    return image


class CandleTests(unittest.TestCase):
    def test_red_current_body_crosses_expiration_without_line_becoming_wick(self):
        image = synthetic_chart()
        cv2.rectangle(image, (328, 90), (373, 175), (26, 20, 12), -1)
        cv2.rectangle(image, (330, 110), (370, 130), (75, 72, 229), -1)
        cv2.line(image, (350, 106), (350, 136), (75, 72, 229), 2)
        cv2.line(image, (335, 40), (335, 268), (65, 65, 210), 2)
        result = CandleDetector().detect(image, (5, 40, 575, 270), ExpirationLine(335, 40, 270, .95))
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.center_x, 350, delta=1)
        self.assertEqual(result.color, "RED")
        self.assertEqual(result.body, (330, 110, 371, 131))
        self.assertGreater(result.bounding_box[1], 100)
        self.assertLess(result.bounding_box[3], 145)

    def test_green_current_body_can_cross_red_expiration(self):
        image = synthetic_chart()
        cv2.line(image, (335, 40), (335, 268), (65, 65, 210), 2)
        result = CandleDetector().detect(image, (5, 40, 575, 270), ExpirationLine(335, 40, 270, .95))
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.center_x, 350, delta=1)
        self.assertEqual(result.color, "GREEN")
        self.assertEqual(result.body, (330, 110, 371, 156))

    def test_body_wicks_color_and_current_position(self):
        image = synthetic_chart()
        result = CandleDetector().detect(image, (5, 40, 575, 270), ExpirationLine(390, 40, 270, .95))
        self.assertIsNotNone(result)
        self.assertEqual(result.color, "GREEN")
        self.assertAlmostEqual(result.center_x, 350, delta=1)
        self.assertEqual(result.body, (330, 110, 371, 156))
        self.assertIsNotNone(result.upper_wick)
        self.assertIsNotNone(result.lower_wick)
        self.assertLess(result.upper_wick[1], result.body[1])
        self.assertGreater(result.lower_wick[3], result.body[3])
        self.assertGreater(result.confidence, .85)

    def test_missing_expiration_reduces_confidence(self):
        result = CandleDetector().detect(synthetic_chart(), (5, 40, 575, 270), None)
        self.assertIsNotNone(result)
        self.assertLess(result.confidence, .7)

    def test_empty_image_does_not_fabricate_candle(self):
        self.assertIsNone(CandleDetector().detect(
            np.zeros((300, 600, 3), dtype=np.uint8), (5, 40, 575, 270), None,
        ))


if __name__ == "__main__":
    raise SystemExit(main("candle"))
