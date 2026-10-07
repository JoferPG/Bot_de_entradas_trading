"""Referencia visual estricta para el bot; nunca devuelve objetos retenidos."""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

from candle_tracker import CandleTracker, TrackingResult, draw_debug


class ReferenceUnavailable(RuntimeError):
    pass


class LiveCandleReference:
    def __init__(self, colors: tuple[tuple[int, int, int], tuple[int, int, int]]) -> None:
        self.colors = colors
        self.tracker = CandleTracker(colors)
        self.bucket: int | None = None
        self.result: TrackingResult | None = None
        self.image: Image.Image | None = None
        self.report = "Referencia pendiente."

    def locate(self, image: Image.Image, timestamp: float, bucket: int) -> tuple[float, float]:
        if self.bucket != bucket:
            self.tracker = CandleTracker(self.colors)
            self.bucket = bucket
        bgr = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)
        self.result = self.tracker.update(bgr, timestamp, (0, 0, image.width, image.height))
        result = self.result
        self.image = Image.fromarray(cv2.cvtColor(draw_debug(bgr, result, include_panel=False), cv2.COLOR_BGR2RGB))
        confidence = f"Confianza visual: {result.global_confidence:.0%}"
        if (
            result.candle is None or result.point is None or result.expiration is None
            or not result.spatial_agreement or result.global_confidence < .80
            or result.candle.confidence < .70 or result.point.confidence < .65
            or result.expiration.confidence < .70
        ):
            reason = " ".join(result.reasons) or "Confianza visual insuficiente."
            self.report = f"{confidence}. {reason}"
            raise ReferenceUnavailable(f"Esperando: {reason} Sin entrada.")
        motion = result.motion
        movement = (
            f"DY={motion.delta_y:+.1f}, {motion.direction}"
            if motion is not None else "movimiento no medido"
        )
        self.report = (
            f"{confidence}. Vela {result.candle.color}; "
            f"punto ({result.point.x:.1f}, {result.point.y:.1f}); {movement}. "
            f"Busqueda: {result.point_search_stage}."
        )
        # Las flechas se alinean con la vela, no con un punto de precio descentrado.
        center = result.candle.center_x
        tolerance = min(3., result.candle.spacing * .12)
        return center-tolerance, center+tolerance
