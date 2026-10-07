"""Detectores visuales experimentales. No importan ni ejecutan el bot."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from numpy.typing import NDArray

Image = NDArray[np.uint8]
Box = tuple[int, int, int, int]
State = Literal["TRACKING", "LOW_CONFIDENCE"]


@dataclass(frozen=True)
class ExpirationLine:
    x: float
    y_top: int
    y_bottom: int
    confidence: float


@dataclass(frozen=True)
class Candle:
    bounding_box: Box
    body: Box
    upper_wick: Box | None
    lower_wick: Box | None
    color: Literal["GREEN", "RED"]
    color_rgb: tuple[int, int, int]
    confidence: float
    center_x: float
    spacing: float


@dataclass(frozen=True)
class WhitePoint:
    x: float
    y: float
    radius: float
    confidence: float
    timestamp: float


@dataclass(frozen=True)
class PointMotion:
    delta_x: float
    delta_y: float
    velocity_y: float
    direction: Literal["UP", "DOWN", "STABLE"]


@dataclass(frozen=True)
class TrackingResult:
    roi: Box
    expiration: ExpirationLine | None
    candle: Candle | None
    point: WhitePoint | None
    motion: PointMotion | None
    point_state: State
    point_search_stage: str
    recalibration_required: bool
    global_confidence: float
    spatial_agreement: bool
    reasons: tuple[str, ...]


def load_image(path: Path) -> Image:
    image = cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"No se pudo decodificar la imagen: {path}")
    return image


def chart_roi(image: Image, roi: Box | None = None) -> Box:
    height, width = image.shape[:2]
    # Perfil de captura del grafico: excluir encabezado, escala lateral y pie.
    chosen = roi or (round(width * .01), round(height * .14),
                     round(width * .97), round(height * .92))
    x1, y1, x2, y2 = chosen
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise ValueError("ROI invalida: debe ser x1,y1,x2,y2 dentro de la imagen.")
    return chosen


def intersect(a: Box, b: Box) -> Box | None:
    box = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return box if box[0] < box[2] and box[1] < box[3] else None


def color_masks(image: Image) -> tuple[Image, Image]:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (40, 100, 65), (95, 255, 255))
    red = cv2.bitwise_or(
        cv2.inRange(hsv, (0, 100, 100), (12, 255, 255)),
        cv2.inRange(hsv, (170, 100, 100), (179, 255, 255)),
    )
    return green, red


class ExpirationLineDetector:
    def detect(self, image: Image, roi: Box) -> ExpirationLine | None:
        x1, y1, x2, y2 = roi
        _, red = color_masks(image[y1:y2, x1:x2])
        height, width = red.shape
        gap = max(3, round(height / 599 * 7)) | 1
        red = cv2.morphologyEx(red, cv2.MORPH_CLOSE, np.ones((gap, 1), np.uint8))
        vertical = cv2.morphologyEx(
            red, cv2.MORPH_OPEN, np.ones((max(20, height // 4), 1), np.uint8),
        )
        count, _, stats, _ = cv2.connectedComponentsWithStats(vertical)
        candidates: list[ExpirationLine] = []
        for index in range(1, count):
            x, y, w, h, area = (int(v) for v in stats[index])
            if not (w <= max(5, round(width * .004)) and h >= height * .65):
                continue
            if x < width * .05 or x + w > width * .97:
                continue
            coverage = area / (w * h)
            confidence = min(.99, .65 * h / height + .35 * coverage)
            candidates.append(ExpirationLine(x1 + x + (w - 1) / 2, y1 + y, y1 + y + h, confidence))
        # No escoger arbitrariamente entre dos lineas verticales similares.
        if len(candidates) != 1:
            return None
        return candidates[0]


class CandleDetector:
    def __init__(
        self, colors: tuple[tuple[int, int, int], tuple[int, int, int]] | None = None,
    ) -> None:
        self.colors = colors

    def detect(
        self, image: Image, roi: Box, expiration: ExpirationLine | None,
    ) -> Candle | None:
        x1, y1, x2, y2 = roi
        crop = image[y1:y2, x1:x2]
        masks = color_masks(crop)
        if self.colors is not None:
            masks = tuple(
                cv2.inRange(
                    crop,
                    np.array([max(0, channel-24) for channel in rgb[::-1]], dtype=np.uint8),
                    np.array([min(255, channel+24) for channel in rgb[::-1]], dtype=np.uint8),
                )
                for rgb in self.colors
            )
        candidates: list[Candle] = []
        for color, mask in zip(("GREEN", "RED"), masks):
            gap = max(3, round(image.shape[0] / 768 * 9)) | 1
            structural_mask = mask.copy()
            if expiration is not None:
                line_x = round(expiration.x) - x1
                half_width = max(2, round(image.shape[1] / 1526 * 3))
                structural_mask[:, max(0, line_x-half_width):min(mask.shape[1], line_x+half_width+1)] = 0
                # Separar la linea roja del cuerpo rojo y reconstruir solo el hueco horizontal.
                structural_mask = cv2.morphologyEx(
                    structural_mask, cv2.MORPH_CLOSE,
                    np.ones((1, half_width * 2 + 3), np.uint8),
                )
            joined = cv2.morphologyEx(structural_mask, cv2.MORPH_CLOSE, np.ones((gap, 1), np.uint8))
            count, labels, stats, _ = cv2.connectedComponentsWithStats(joined)
            for index in range(1, count):
                x, y, w, h, _ = (int(v) for v in stats[index])
                if w < 6 or w > (x2 - x1) * .09 or h < 4 or x == 0 or x + w >= crop.shape[1]:
                    continue
                component = labels[y:y+h, x:x+w] == index
                body_rows = np.flatnonzero(component.sum(axis=1) >= w * .65)
                if len(body_rows) < 3:
                    continue
                top, bottom = int(body_rows[0]), int(body_rows[-1]) + 1
                if len(body_rows) / (bottom - top) < .85:
                    continue
                bx = np.flatnonzero(component[top:bottom].sum(axis=0) >= (bottom - top) * .6)
                if len(bx) < 6:
                    continue
                left, right = int(bx[0]), int(bx[-1]) + 1
                center = x1 + x + (left + right - 1) / 2
                if expiration is not None and x1 + x + left > expiration.x + 2:
                    continue
                body = (x1+x+left, y1+y+top, x1+x+right, y1+y+bottom)
                axis = (left + right - 1) / 2
                wicks: list[Box | None] = []
                for a, b in ((0, top), (bottom, h)):
                    wy, wx = np.where(component[a:b])
                    near = np.abs(wx - axis) <= max(2, (right-left) * .08)
                    wy, wx = wy[near], wx[near]
                    wicks.append(
                        (x1+x+int(wx.min()), y1+y+a+int(wy.min()),
                         x1+x+int(wx.max())+1, y1+y+a+int(wy.max())+1)
                        if len(wx) else None
                    )
                raw_body = mask[y+top:y+bottom, x+left:x+right] > 0
                pixels = crop[y+top:y+bottom, x+left:x+right][raw_body]
                if len(pixels) == 0:
                    continue
                rgb = tuple(int(v) for v in np.median(pixels, axis=0)[::-1])
                bounds = (body[0], min(body[1], wicks[0][1] if wicks[0] else body[1]),
                          body[2], max(body[3], wicks[1][3] if wicks[1] else body[3]))
                fill = float(raw_body.mean())
                candidates.append(Candle(
                    bounds, body, wicks[0], wicks[1],
                    "GREEN" if color == "GREEN" else "RED",
                    (rgb[0], rgb[1], rgb[2]), min(.99, .7 + .25 * fill), center, 0,
                ))
        candidates.sort(key=lambda candle: candle.center_x)
        if len(candidates) < 3:
            return None
        recent = candidates[-4:]
        gaps = np.diff([c.center_x for c in recent])
        spacing = float(np.median(gaps))
        if spacing <= 0 or np.max(np.abs(gaps - spacing)) > spacing * .25:
            return None
        latest = recent[-1]
        if latest.body[2] - latest.body[0] >= spacing * .85:
            return None
        if expiration is not None:
            crosses_line = latest.body[0] - 2 <= expiration.x <= latest.body[2]
            left_of_line = latest.body[2] <= expiration.x and 0 < expiration.x - latest.center_x <= spacing * 1.25
            if not (crosses_line or left_of_line):
                return None
        confidence = latest.confidence * (1 if expiration is not None else .65)
        return Candle(
            latest.bounding_box, latest.body, latest.upper_wick, latest.lower_wick,
            latest.color, latest.color_rgb, confidence, latest.center_x, spacing,
        )


class WhitePointTracker:
    def __init__(
        self, search_radius_x: int = 24, search_radius_y: int = 40,
        max_gap_seconds: float = 1.0,
    ) -> None:
        if (
            search_radius_x <= 0 or search_radius_y <= 0
            or not math.isfinite(max_gap_seconds) or max_gap_seconds <= 0
        ):
            raise ValueError("Radios de busqueda y tiempo de tracking deben ser positivos.")
        self.search_radius_x = search_radius_x
        self.search_radius_y = search_radius_y
        self.max_gap_seconds = max_gap_seconds
        self.point: WhitePoint | None = None
        self.motion: PointMotion | None = None
        self.state: State = "LOW_CONFIDENCE"
        self.recalibration_required = True
        self.search_stage = "INITIAL"
        self._frame_size: tuple[int, int] | None = None
        self._roi: Box | None = None
        self._last_timestamp: float | None = None
        self._lost = False

    @staticmethod
    def _candidates(image: Image, area: Box, candle: Candle) -> list[tuple[float, float, float, float]]:
        x1, y1, x2, y2 = area
        crop = image[y1:y2, x1:x2]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        white = cv2.inRange(hsv, (0, 0, 220), (179, 55, 255))
        opened = cv2.morphologyEx(
            white, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        )
        results: list[tuple[float, float, float, float]] = []
        for mask in (white, opened):
            count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
            for index in range(1, count):
                x, y, w, h, size = (int(v) for v in stats[index])
                if not (2 <= w <= 16 and 2 <= h <= 16 and size >= 4 and max(w, h) <= min(w, h) * 1.7):
                    continue
                fill = size / (w * h)
                if fill < .45:
                    continue
                component = np.uint8(labels[y:y+h, x:x+w] == index) * 255
                contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                contour = max(contours, key=cv2.contourArea)
                perimeter = cv2.arcLength(contour, True)
                circularity = 4 * math.pi * cv2.contourArea(contour) / perimeter**2 if perimeter else 0
                if circularity < .45:
                    continue
                (cx, cy), radius = cv2.minEnclosingCircle(contour)
                px, py = x1+x+float(cx), y1+y+float(cy)
                if not 1 <= radius <= 8:
                    continue
                if abs(px - candle.center_x) > max(6, (candle.body[2]-candle.body[0]) * .6):
                    continue
                if not candle.bounding_box[1] - 10 <= py <= candle.bounding_box[3] + 10:
                    continue
                # La linea puede terminar en el punto o quedar oculta por el cuerpo.
                row = round(py)
                line_support = []
                for a, b in ((round(px)-32, round(px)-9), (round(px)+9, round(px)+32)):
                    strip_image = image[max(0, row-2):row+3, max(0, a):min(image.shape[1], b)]
                    if strip_image.size == 0:
                        line_support.append(0.)
                        continue
                    strip = cv2.cvtColor(strip_image, cv2.COLOR_BGR2HSV)
                    bright = (strip[:, :, 1] <= 90) & (strip[:, :, 2] >= 160)
                    line_support.append(float(bright.any(axis=0).mean()) if bright.size else 0)
                if max(line_support) < .45:
                    continue
                spatial = max(0., 1 - abs(px-candle.center_x) / max(6, candle.spacing * .35))
                confidence = min(.99, .3 * min(1., circularity) + .2 * fill
                                 + .25 * spatial + .25 * max(line_support))
                if confidence < .65:
                    continue
                if not any(math.hypot(px-a, py-b) < 4 for a, b, _, _ in results):
                    results.append((px, py, float(radius), confidence))
        return results

    def update(
        self, image: Image, roi: Box, candle: Candle | None, timestamp: float,
    ) -> WhitePoint | None:
        if not math.isfinite(timestamp) or (
            self._last_timestamp is not None and timestamp <= self._last_timestamp
        ):
            raise ValueError("Los timestamps deben ser finitos y estrictamente crecientes.")
        self._last_timestamp = timestamp
        frame_size = (image.shape[1], image.shape[0])
        previous = self.point
        if (
            frame_size != self._frame_size or roi != self._roi
            or previous is not None and timestamp - previous.timestamp > self.max_gap_seconds
        ):
            previous = None
        self._frame_size, self._roi = frame_size, roi
        self.motion = None
        if candle is None:
            self.state = "LOW_CONFIDENCE"
            self.recalibration_required = True
            self._lost = True
            return None
        margin = max(12, round(candle.spacing * .25))
        candle_area = intersect(roi, (
            candle.body[0]-margin, candle.bounding_box[1]-12,
            candle.body[2]+margin, candle.bounding_box[3]+12,
        ))
        if candle_area is None:
            self.state = "LOW_CONFIDENCE"
            self.recalibration_required = True
            self._lost = True
            return None
        stages: list[tuple[str, Box]] = []
        if previous is not None and not self._lost:
            for multiplier, name in ((1, "LOCAL"), (3, "EXPANDED")):
                area = intersect(candle_area, (
                    math.floor(previous.x-self.search_radius_x*multiplier),
                    math.floor(previous.y-self.search_radius_y*multiplier),
                    math.ceil(previous.x+self.search_radius_x*multiplier),
                    math.ceil(previous.y+self.search_radius_y*multiplier),
                ))
                if area is not None:
                    stages.append((name, area))
        else:
            stages.append(("RECALIBRATION" if self._lost else "INITIAL", candle_area))
        for name, area in stages:
            self.search_stage = name
            candidates = self._candidates(image, area, candle)
            if len(candidates) > 1:
                break
            if len(candidates) == 1:
                x, y, radius, confidence = candidates[0]
                if previous is not None and not self._lost:
                    dx, dy = x-previous.x, y-previous.y
                    self.motion = PointMotion(
                        dx, dy, dy/(timestamp-previous.timestamp),
                        "UP" if dy < -1 else "DOWN" if dy > 1 else "STABLE",
                    )
                    confidence = min(.99, confidence + .03)
                self.point = WhitePoint(x, y, radius, confidence, timestamp)
                self.state = "TRACKING"
                self.recalibration_required = False
                self._lost = False
                return self.point
        # Guardar el ultimo objeto solo como historial; nunca devolverlo como deteccion actual.
        self.state = "LOW_CONFIDENCE"
        self.recalibration_required = True
        self._lost = True
        return None


class CandleTracker:
    def __init__(
        self, colors: tuple[tuple[int, int, int], tuple[int, int, int]] | None = None,
    ) -> None:
        self.expiration_detector = ExpirationLineDetector()
        self.candle_detector = CandleDetector(colors)
        self.white_point_tracker = WhitePointTracker()

    def update(
        self, image: Image, timestamp: float, roi: Box | None = None,
    ) -> TrackingResult:
        area = chart_roi(image, roi)
        expiration = self.expiration_detector.detect(image, area)
        candle = self.candle_detector.detect(image, area, expiration)
        point = self.white_point_tracker.update(image, area, candle, timestamp)
        reasons = []
        if expiration is None:
            reasons.append("Linea de expiracion ausente o ambigua.")
        if candle is None:
            reasons.append("Vela actual no confirmada por forma, serie y posicion.")
        if point is None:
            reasons.append("Punto blanco ausente o ambiguo; LOW_CONFIDENCE, recalibracion requerida.")
        agreement = (
            candle is not None and point is not None and expiration is not None
            and candle.body[0] - 2 <= expiration.x <= candle.center_x + candle.spacing * 1.25
            and abs(point.x - candle.center_x) <= candle.spacing * .35
            and candle.bounding_box[1]-10 <= point.y <= candle.bounding_box[3]+10
        )
        score = sum((.4 * candle.confidence if candle else 0.,
                     .35 * point.confidence if point else 0.,
                     .25 * expiration.confidence if expiration else 0.))
        if agreement:
            score = min(.99, score + .03)
        else:
            score = min(.69, score)
            if not reasons:
                reasons.append("Los tres objetos no coinciden espacialmente.")
        return TrackingResult(
            area, expiration, candle, point, self.white_point_tracker.motion,
            self.white_point_tracker.state, self.white_point_tracker.search_stage,
            self.white_point_tracker.recalibration_required, score, agreement, tuple(reasons),
        )


def draw_debug(image: Image, result: TrackingResult, include_panel: bool = True) -> Image:
    output = image.copy()
    cv2.rectangle(output, result.roi[:2], result.roi[2:], (110, 110, 110), 1)
    if result.expiration:
        line = result.expiration
        cv2.line(output, (round(line.x), line.y_top), (round(line.x), line.y_bottom), (255, 0, 255), 2)
    if result.candle:
        candle = result.candle
        for box, color in ((candle.bounding_box, (255, 255, 0)), (candle.body, (0, 255, 255)),
                           (candle.upper_wick, (255, 100, 0)), (candle.lower_wick, (0, 140, 255))):
            if box:
                cv2.rectangle(output, box[:2], (box[2]-1, box[3]-1), color, 2)
    if result.point:
        point = result.point
        center = (round(point.x), round(point.y))
        cv2.circle(output, center, math.ceil(point.radius)+4, (255, 255, 255), 1)
        cv2.drawMarker(output, center, (255, 0, 255), cv2.MARKER_CROSS, 8, 1)
    if not include_panel:
        return output
    lines = [
        f"GLOBAL {result.global_confidence:.1%} | spatial agreement: {result.spatial_agreement}",
        f"POINT STATE {result.point_state} search={result.point_search_stage} recalibrate={result.recalibration_required}",
    ]
    if result.expiration:
        lines.append(f"EXPIRATION X={result.expiration.x:.1f} confidence={result.expiration.confidence:.1%} [magenta]")
    if result.candle:
        c = result.candle
        lines += [
            f"CANDLE {c.color} RGB={c.color_rgb} confidence={c.confidence:.1%}",
            f"BBOX {c.bounding_box} [cyan] BODY {c.body} [yellow]",
            f"UPPER {c.upper_wick} [blue] LOWER {c.lower_wick} [orange]",
        ]
        if result.expiration:
            scenario = (
                "CROSSES RED LINE" if c.body[0]-2 <= result.expiration.x <= c.body[2]
                else "LEFT OF RED LINE"
            )
            lines.append(f"SCENARIO: {scenario} (visual placement, not a time measurement)")
    if result.point:
        p = result.point
        lines.append(f"WHITE POINT X={p.x:.1f} Y={p.y:.1f} radius={p.radius:.1f} confidence={p.confidence:.1%}")
    if result.motion:
        m = result.motion
        lines.append(f"DX={m.delta_x:.1f} DY={m.delta_y:.1f} VY={m.velocity_y:.1f}px/s {m.direction}")
    else:
        lines.append("MOTION: not measured (first frame, lost track or recalibration)")
    lines.extend(result.reasons)
    lines.append("Heuristic scores, not probabilities. Offline diagnostic only.")
    panel_height = 26 * len(lines) + 14
    output = cv2.copyMakeBorder(output, 0, panel_height, 0, 0, cv2.BORDER_CONSTANT, value=(12, 15, 24))
    for index, line in enumerate(lines):
        cv2.putText(output, line, (14, image.shape[0]+25+index*26),
                    cv2.FONT_HERSHEY_SIMPLEX, .52, (235, 235, 235), 1, cv2.LINE_AA)
    return output


def main(component: str = "tracker") -> int:
    parser = argparse.ArgumentParser(description="Prueba visual offline; no envia operaciones.")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="PNG de debug; tambien genera JSON.")
    parser.add_argument("--roi", help="Area valida del grafico: x1,y1,x2,y2 (coordenadas de la imagen).")
    parser.add_argument("--next-image", type=Path, action="append", default=[])
    parser.add_argument("--frame-interval", type=float, default=.2, help="Segundos reales entre capturas.")
    args = parser.parse_args()
    try:
        if not math.isfinite(args.frame_interval) or args.frame_interval <= 0:
            raise ValueError("El intervalo de frames debe ser positivo y finito.")
        roi = None
        if args.roi:
            values = tuple(int(value) for value in args.roi.split(","))
            if len(values) != 4:
                raise ValueError("ROI requiere exactamente cuatro enteros.")
            roi = (values[0], values[1], values[2], values[3])
        output = args.output or args.image.with_name(f"{args.image.stem}.{component}.debug.png")
        if output.suffix.lower() != ".png":
            raise ValueError("La salida de debug debe tener extension .png.")
        json_path = output.with_suffix(".json")
        sources = [args.image, *args.next_image]
        if output.resolve() in [p.resolve() for p in sources] or json_path.resolve() in [p.resolve() for p in sources]:
            raise ValueError("La salida no puede sobrescribir una captura original.")
        tracker = CandleTracker()
        started = time.time()
        records = []
        final_image: Image | None = None
        final_result: TrackingResult | None = None
        for index, source in enumerate(sources):
            final_image = load_image(source)
            final_result = tracker.update(final_image, started + index * args.frame_interval, roi)
            records.append({"image": str(source), **asdict(final_result)})
        if final_image is None or final_result is None:
            raise RuntimeError("No hay capturas para procesar.")
        encoded, buffer = cv2.imencode(".png", draw_debug(final_image, final_result))
        if not encoded:
            raise RuntimeError("No se pudo codificar la imagen de debug.")
        output.write_bytes(buffer.tobytes())
        json_path.write_text(json.dumps(records, indent=2), encoding="utf-8")
        print(json.dumps(records, indent=2))
        print(f"Debug: {output}\nJSON: {json_path}")
        present = {
            "tracker": final_result.spatial_agreement,
            "expiration": final_result.expiration is not None,
            "candle": final_result.candle is not None,
            "point": final_result.point is not None,
        }
        return 0 if present[component] else 2
    except (OSError, ValueError, RuntimeError, cv2.error) as exc:
        print(f"Error de prueba visual: {exc}", file=sys.stderr)
        return 1
