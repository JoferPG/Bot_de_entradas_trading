"""PIL adapter exposing current EMA evidence to the bot and its preview."""

import cv2
import numpy as np
from PIL import Image

import config
from ema_analyzer import EMAAnalyzer, EMAResult, draw_debug


class LiveEMAAnalysis:
    def __init__(self) -> None:
        self.analyzer = EMAAnalyzer()
        self.result: EMAResult | None = None

    def update(self, image: Image.Image, timestamp: float) -> EMAResult:
        bgr = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)
        self.result = self.analyzer.update(bgr, timestamp)
        return self.result

    def overlay(self, image: Image.Image) -> Image.Image:
        if not config.DEBUG_MODE or self.result is None:
            return image
        bgr = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)
        return Image.fromarray(cv2.cvtColor(
            draw_debug(bgr, self.result, include_panel=False), cv2.COLOR_BGR2RGB))

    def reset(self) -> None:
        self.analyzer.reset()
        self.result = None
