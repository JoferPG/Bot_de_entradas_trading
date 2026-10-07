import unittest

import cv2
import numpy as np

from candle_tracker import ExpirationLineDetector, main


class ExpirationTests(unittest.TestCase):
    def test_vertical_line_not_candle_grid_or_side_bar(self):
        image = np.zeros((300, 600, 3), dtype=np.uint8)
        cv2.line(image, (380, 35), (380, 270), (65, 65, 210), 2)
        cv2.line(image, (580, 35), (580, 270), (65, 65, 210), 5)
        cv2.line(image, (250, 35), (250, 270), (75, 65, 45), 1)
        result = ExpirationLineDetector().detect(image, (5, 30, 590, 275))
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.x, 380, delta=1)
        self.assertGreater(result.confidence, .85)

    def test_missing_and_ambiguous_lines_return_none(self):
        image = np.zeros((300, 600, 3), dtype=np.uint8)
        detector = ExpirationLineDetector()
        self.assertIsNone(detector.detect(image, (5, 30, 590, 275)))
        for x in (300, 400):
            cv2.line(image, (x, 35), (x, 270), (65, 65, 210), 2)
        self.assertIsNone(detector.detect(image, (5, 30, 590, 275)))


if __name__ == "__main__":
    raise SystemExit(main("expiration"))
