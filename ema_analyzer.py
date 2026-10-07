"""Analyze platform-rendered EMA curves, never calculate EMA prices or trade."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import cv2
import numpy as np

import config
from candle_tracker import Box, Image, load_image

Relation = Literal["ABOVE", "BELOW", "TOUCHING", "UNKNOWN"]
Slope = Literal["POSITIVE", "NEGATIVE", "FLAT"]
MarketState = Literal["BULLISH", "BEARISH", "CROSSING", "SIDEWAYS", "UNKNOWN"]
Points = list[tuple[int, float]]


@dataclass(frozen=True)
class EMAResult:
    timestamp: float
    fast_period: int = config.EMA_FAST_PERIOD
    slow_period: int = config.EMA_SLOW_PERIOD
    fast_above_slow: bool | None = None
    previous_relation: Relation = "UNKNOWN"
    current_relation: Relation = "UNKNOWN"
    fast_slope: Slope | None = None
    slow_slope: Slope | None = None
    fast_slope_value: float | None = None
    slow_slope_value: float | None = None
    distance: float | None = None
    distance_class: str | None = None
    distance_increasing: bool | None = None
    analysis_x: int | None = None
    crossover: bool = False
    crossover_detected: str | None = None
    crossover_timestamp: float | None = None
    spatial_crossing: bool = False
    market_state: MarketState = "UNKNOWN"
    confidence: float = 0.
    search_stage: str = "FULL"
    reason: str = ""
    fast_points: Points = field(default_factory=list)
    slow_points: Points = field(default_factory=list)
    confidence_factors: dict[str, float] = field(default_factory=dict)


@dataclass
class Curve:
    points: Points
    observed_count: int
    continuity: float
    color_quality: float


class EMAAnalyzer:
    def __init__(self) -> None:
        positive = (
            config.EMA_MIN_POINTS, config.EMA_MIN_SEGMENT_WIDTH,
            config.EMA_MAX_LINE_THICKNESS_PX, config.EMA_MAX_GAP_PX,
            config.EMA_MAX_STEP_SLOPE, config.EMA_SLOPE_LOOKBACK,
            config.EMA_TRACK_RADIUS_PX, config.EMA_RECOVERY_RADIUS_PX,
            config.EMA_RECALIBRATION_FRAMES, config.EMA_MAX_FRAME_GAP_SECONDS,
            config.EMA_CROSS_CONFIRMATION_FRAMES,
        )
        if (
            any(not math.isfinite(v) or v <= 0 for v in positive)
            or config.EMA_SLOPE_LOOKBACK < 3
            or not 0 <= config.EMA_RELATION_MARGIN_PX <= config.EMA_CLOSE_DISTANCE_PX
            < config.EMA_SEPARATED_DISTANCE_PX
            or not 0 <= config.EMA_MIN_CONFIDENCE <= 100
            or config.EMA_SLOPE_FLAT_MARGIN < 0
            or config.EMA_DISTANCE_CHANGE_MARGIN_PX < 0
        ):
            raise ValueError("Configuracion visual EMA invalida.")
        weights = config.EMA_CONFIDENCE_WEIGHTS
        if len(weights) != 5 or any(not math.isfinite(w) or w < 0 for w in weights) or not math.isclose(sum(weights), 1):
            raise ValueError("Los cinco pesos EMA deben sumar 1.")
        for low, high in (
            (config.EMA_FAST_HSV_LOW, config.EMA_FAST_HSV_HIGH),
            (config.EMA_SLOW_HSV_LOW, config.EMA_SLOW_HSV_HIGH),
        ):
            if len(low) != 3 or len(high) != 3 or any(
                not 0 <= a <= b <= limit for a, b, limit in zip(low, high, (179, 255, 255))
            ):
                raise ValueError("Rango HSV EMA invalido.")
        self.reset()

    def reset(self) -> None:
        self.previous: EMAResult | None = None
        self.geometry: tuple[int, ...] | None = None
        self.last_timestamp: float | None = None
        self.misses = 0
        self.stable_relation: Relation = "UNKNOWN"
        self.pending_relation: Relation = "UNKNOWN"
        self.pending_frames = 0
        self.last_crossover: float | None = None
        self.ema_fast_mask: Image | None = None
        self.ema_slow_mask: Image | None = None

    def _extract(self, hsv: Image, low: tuple[int, int, int], high: tuple[int, int, int],
                 previous: Points, radius: int | None) -> tuple[Curve | None, Image]:
        raw = cv2.inRange(hsv, np.array(low, np.uint8), np.array(high, np.uint8))
        mask = raw.copy()
        if radius is not None and previous:
            corridor = np.zeros_like(mask)
            path = np.array([(x, round(y)) for x, y in previous], np.int32)
            cv2.polylines(corridor, [path], False, 255, radius * 2 + 1)
            mask = cv2.bitwise_and(mask, corridor)
        # Closing preserves one-pixel curves; opening would erase them.
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        segments: list[Points] = []
        cleaned = np.zeros_like(mask)
        for label in range(1, count):
            x, y, width, height, area = stats[label]
            if (width < config.EMA_MIN_SEGMENT_WIDTH
                    or height > width * config.EMA_MAX_STEP_SLOPE + config.EMA_MAX_LINE_THICKNESS_PX
                    or area / width > config.EMA_MAX_LINE_THICKNESS_PX):
                continue
            points: Points = []
            for column in range(int(x), int(x + width)):
                rows = np.flatnonzero(labels[y:y+height, column] == label) + y
                if rows.size and rows[-1] - rows[0] < config.EMA_MAX_LINE_THICKNESS_PX:
                    points.append((column, float(np.median(rows))))
            if len(points) >= config.EMA_MIN_SEGMENT_WIDTH:
                segments.append(points)
                cleaned[labels == label] = 255
        if not segments:
            return None, cleaned
        chains: list[Points] = []
        for segment in sorted(segments, key=lambda item: item[0][0]):
            choices = [
                chain for chain in chains
                if 0 < segment[0][0] - chain[-1][0] <= config.EMA_MAX_GAP_PX + 1
                and abs(segment[0][1] - chain[-1][1]) <=
                config.EMA_MAX_STEP_SLOPE * (segment[0][0] - chain[-1][0])
            ]
            if len(choices) == 1:
                choices[0].extend(segment)
            else:
                chains.append(segment.copy())
        chains.sort(key=len, reverse=True)
        points = chains[0]
        # Multiple comparable curves of the same color are not a valid EMA identity.
        if len(chains) > 1 and len(chains[1]) >= max(config.EMA_MIN_POINTS, len(points) * .5):
            return None, cleaned
        if len(points) < config.EMA_MIN_POINTS:
            return None, cleaned
        xs, ys = np.array(points, dtype=float).T
        if np.max(np.abs(np.diff(ys) / np.diff(xs))) > config.EMA_MAX_STEP_SLOPE:
            return None, cleaned
        full_x = np.arange(int(xs[0]), int(xs[-1]) + 1)
        full_y = np.interp(full_x, xs, ys)
        color_quality = float(np.mean([raw[round(y), x] > 0 for x, y in points]))
        return Curve(
            [(int(x), float(y)) for x, y in zip(full_x, full_y)],
            len(points), len(points) / len(full_x), color_quality,
        ), cleaned

    def update(self, image: Image, timestamp: float, roi: Box | None = None) -> EMAResult:
        if (not math.isfinite(timestamp) or
                self.last_timestamp is not None and timestamp <= self.last_timestamp):
            raise ValueError("Las capturas EMA requieren timestamps crecientes.")
        if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
            raise ValueError("EMAAnalyzer requiere imagen BGR uint8.")
        height, width = image.shape[:2]
        box = roi or (0, 0, width, height)
        x1, y1, x2, y2 = box
        if not 0 <= x1 < x2 <= width or not 0 <= y1 < y2 <= height:
            raise ValueError("ROI EMA invalida.")
        geometry = (height, width, *box)
        if self.geometry != geometry or (
            self.last_timestamp is not None
            and timestamp - self.last_timestamp > config.EMA_MAX_FRAME_GAP_SECONDS
        ):
            self.reset()
        self.geometry = geometry
        self.last_timestamp = timestamp
        hsv = cv2.cvtColor(image[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
        prior = self.previous
        fast_prior = [(x-x1, y-y1) for x, y in prior.fast_points] if prior else []
        slow_prior = [(x-x1, y-y1) for x, y in prior.slow_points] if prior else []
        stages = [("FULL", None)] if prior is None else [
            ("TRACKING", config.EMA_TRACK_RADIUS_PX),
            ("RECOVERY", config.EMA_RECOVERY_RADIUS_PX),
        ]
        fast = slow = None
        stage = "FULL"
        for stage, radius in stages:
            fast, self.ema_fast_mask = self._extract(
                hsv, config.EMA_FAST_HSV_LOW, config.EMA_FAST_HSV_HIGH, fast_prior, radius)
            slow, self.ema_slow_mask = self._extract(
                hsv, config.EMA_SLOW_HSV_LOW, config.EMA_SLOW_HSV_HIGH, slow_prior, radius)
            if fast is not None and slow is not None:
                break
        if fast is None or slow is None:
            return self._unknown(timestamp, stage, "EMA ausente, ambigua o trayectoria insuficiente.")
        fast_map, slow_map = dict(fast.points), dict(slow.points)
        if abs(fast.points[-1][0] - slow.points[-1][0]) > config.EMA_MAX_GAP_PX:
            return self._unknown(timestamp, stage, "Extremos EMA no coinciden; posible perdida de la zona reciente.")
        common = sorted(fast_map.keys() & slow_map.keys())
        if len(common) < config.EMA_MIN_POINTS:
            return self._unknown(timestamp, stage, "Las EMA no tienen suficientes X comunes.")
        differences = np.array([slow_map[x] - fast_map[x] for x in common])
        recent_x = np.array(common[-config.EMA_SLOPE_LOOKBACK:], dtype=float)
        fast_y = np.array([fast_map[int(x)] for x in recent_x])
        slow_y = np.array([slow_map[int(x)] for x in recent_x])
        # Negate screen slope: decreasing Y means rising price.
        fast_slope = -float(np.polyfit(recent_x - recent_x[0], fast_y, 1)[0])
        slow_slope = -float(np.polyfit(recent_x - recent_x[0], slow_y, 1)[0])
        signed = float(np.median(differences[-5:]))
        relation: Relation = (
            "ABOVE" if signed > config.EMA_RELATION_MARGIN_PX else
            "BELOW" if signed < -config.EMA_RELATION_MARGIN_PX else "TOUCHING"
        )
        local = differences[-config.EMA_SLOPE_LOOKBACK:]
        consistent = float(np.mean(
            local > config.EMA_RELATION_MARGIN_PX if relation == "ABOVE" else
            local < -config.EMA_RELATION_MARGIN_PX if relation == "BELOW" else
            np.abs(local) <= config.EMA_CLOSE_DISTANCE_PX
        ))
        temporal = .5
        if prior:
            displacements = []
            for points, old_points in ((fast.points, fast_prior), (slow.points, slow_prior)):
                old = dict(old_points)
                changes = [abs(y-old[x]) for x, y in points if x in old]
                displacements.append(float(np.median(changes)) if changes else float("inf"))
            temporal = max(0., 1 - max(displacements) / config.EMA_RECOVERY_RADIUS_PX)
        factors = {
            "color_detection": min(fast.color_quality, slow.color_quality),
            "line_continuity": min(fast.continuity, slow.continuity),
            "point_count": min(1., min(fast.observed_count, slow.observed_count) / (config.EMA_MIN_POINTS * 2)),
            "temporal_consistency": temporal,
            "relationship_consistency": consistent,
        }
        confidence = 100 * sum(w*v for w, v in zip(config.EMA_CONFIDENCE_WEIGHTS, factors.values()))
        if confidence < config.EMA_MIN_CONFIDENCE:
            return self._unknown(timestamp, stage, "Confianza EMA insuficiente.", confidence, factors)
        previous_relation = self.stable_relation
        crossover = None
        if relation in ("ABOVE", "BELOW"):
            if relation != self.pending_relation:
                self.pending_relation, self.pending_frames = relation, 1
            else:
                self.pending_frames += 1
            if self.pending_frames >= config.EMA_CROSS_CONFIRMATION_FRAMES:
                if self.stable_relation not in ("UNKNOWN", relation):
                    crossover = "CALL" if relation == "ABOVE" else "PUT"
                    self.last_crossover = timestamp
                self.stable_relation = relation
        else:
            self.pending_relation, self.pending_frames = "UNKNOWN", 0
        distance = abs(signed)
        distance_class = (
            "CLOSE" if distance <= config.EMA_CLOSE_DISTANCE_PX else
            "SEPARATED" if distance >= config.EMA_SEPARATED_DISTANCE_PX else "NORMAL"
        )
        increasing = None
        if prior is not None and prior.distance is not None:
            increasing = distance - prior.distance > config.EMA_DISTANCE_CHANGE_MARGIN_PX
        fast_direction, slow_direction = self._direction(fast_slope), self._direction(slow_slope)
        spatial_crossing = bool(np.any(differences > config.EMA_RELATION_MARGIN_PX)
                                and np.any(differences < -config.EMA_RELATION_MARGIN_PX))
        contracting = (prior is not None and prior.distance is not None
                       and distance < prior.distance - config.EMA_DISTANCE_CHANGE_MARGIN_PX)
        state: MarketState = "SIDEWAYS"
        if distance_class == "CLOSE" and fast_direction == slow_direction == "FLAT":
            state = "SIDEWAYS"
        elif relation == "TOUCHING" or crossover or (
            self.stable_relation not in ("UNKNOWN", relation)
        ):
            state = "CROSSING"
        elif relation == "ABOVE" and fast_direction == slow_direction == "POSITIVE" and not contracting:
            state = "BULLISH"
        elif relation == "BELOW" and fast_direction == slow_direction == "NEGATIVE" and not contracting:
            state = "BEARISH"
        result = EMAResult(
            timestamp=timestamp, fast_above_slow=relation == "ABOVE" if relation != "TOUCHING" else None,
            previous_relation=previous_relation, current_relation=relation,
            fast_slope=fast_direction, slow_slope=slow_direction,
            fast_slope_value=fast_slope, slow_slope_value=slow_slope,
            distance=distance, distance_class=distance_class, distance_increasing=increasing,
            analysis_x=common[-1] + x1,
            crossover=crossover is not None, crossover_detected=crossover,
            crossover_timestamp=self.last_crossover, spatial_crossing=spatial_crossing,
            market_state=state, confidence=confidence, search_stage=stage,
            fast_points=[(x+x1, y+y1) for x, y in fast.points],
            slow_points=[(x+x1, y+y1) for x, y in slow.points],
            confidence_factors=factors,
        )
        self.previous, self.misses = result, 0
        return result

    @staticmethod
    def _direction(value: float) -> Slope:
        return ("POSITIVE" if value > config.EMA_SLOPE_FLAT_MARGIN else
                "NEGATIVE" if value < -config.EMA_SLOPE_FLAT_MARGIN else "FLAT")

    def _unknown(self, timestamp: float, stage: str, reason: str, confidence: float = 0.,
                 factors: dict[str, float] | None = None) -> EMAResult:
        self.misses += 1
        # Missing evidence breaks crossover persistence; retained curves are search hints only.
        self.stable_relation = self.pending_relation = "UNKNOWN"
        self.pending_frames = 0
        if self.misses >= config.EMA_RECALIBRATION_FRAMES:
            self.previous = None
        return EMAResult(timestamp, confidence=confidence, search_stage=stage, reason=reason,
                         confidence_factors=factors or {})


def report(result: EMAResult) -> str:
    distance = f"{result.distance:.1f}px ({result.distance_class})" if result.distance is not None else "N/A"
    return (
        f"EMA {result.fast_period}: {result.current_relation} EMA {result.slow_period} | "
        f"Fast: {result.fast_slope or 'N/A'} | Slow: {result.slow_slope or 'N/A'}\n"
        f"Distance: {distance} | State: {result.market_state} | Confidence: {result.confidence:.0f}%\n"
        f"Search: {result.search_stage} | X: {result.analysis_x if result.analysis_x is not None else 'N/A'} | "
        f"Cross: {result.crossover_detected or 'NONE'}"
        + (f"\n{result.reason}" if result.reason else "")
    )


def draw_debug(image: Image, result: EMAResult, include_panel: bool = True) -> Image:
    output = image.copy()
    for points, color, period in (
        (result.fast_points, (255, 255, 0), result.fast_period),
        (result.slow_points, (0, 165, 255), result.slow_period),
    ):
        if points:
            path = np.array([(x, round(y)) for x, y in points], np.int32)
            cv2.polylines(output, [path], False, color, 1)
            for x, y in path[::20]:
                cv2.circle(output, (int(x), int(y)), 2, color, 1)
            x, y = path[len(path)//2]
            cv2.putText(output, f"EMA {period}", (int(x), max(14, int(y)-10)),
                        cv2.FONT_HERSHEY_SIMPLEX, .45, color, 1, cv2.LINE_AA)
    if include_panel:
        lines = report(result).splitlines()
        panel = np.zeros((28 * len(lines) + 10, output.shape[1], 3), np.uint8)
        for index, text in enumerate(lines):
            cv2.putText(panel, text, (8, 22 + index*28), cv2.FONT_HERSHEY_SIMPLEX,
                        .42, (240, 240, 240), 1, cv2.LINE_AA)
        output = np.vstack((output, panel))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="EMA visual offline; no operaciones.")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--next-image", type=Path, action="append", default=[])
    parser.add_argument("--frame-interval", type=float, default=.2)
    parser.add_argument("--roi", help="x1,y1,x2,y2 del grafico; default imagen completa")
    parser.add_argument("--output", type=Path, help="PNG debug; JSON al lado")
    args = parser.parse_args()
    try:
        if not math.isfinite(args.frame_interval) or args.frame_interval <= 0:
            raise ValueError("Intervalo de frames debe ser positivo.")
        roi: Box | None = None
        if args.roi:
            values = [int(v) for v in args.roi.split(",")]
            if len(values) != 4:
                raise ValueError("ROI requiere cuatro enteros.")
            roi = (values[0], values[1], values[2], values[3])
        analyzer = EMAAnalyzer()
        results = []
        for index, path in enumerate([args.image, *args.next_image]):
            frame = load_image(path)
            result = analyzer.update(frame, 1. + index*args.frame_interval, roi)
            results.append(asdict(result))
        output = args.output or args.image.with_name(args.image.stem + ".ema.debug.png")
        if output.resolve() in {p.resolve() for p in [args.image, *args.next_image]}:
            raise ValueError("La salida no puede sobrescribir una captura.")
        success, encoded = cv2.imencode(".png", draw_debug(frame, result))
        if not success:
            raise ValueError("No se pudo codificar el debug.")
        output.write_bytes(encoded.tobytes())
        output.with_suffix(".json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(report(result))
        print(f"Debug: {output}")
        return 0 if result.market_state != "UNKNOWN" else 2
    except (OSError, ValueError, cv2.error) as exc:
        print(f"Error EMA: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
