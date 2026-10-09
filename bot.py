"""Detector visual CALL/PUT con ejecucion demo opcional, sin APIs.

Instalar: pip install -r requirements.txt
Ejecutar: python bot.py
Al iniciar se abre IQ Option en una ventana dedicada de Microsoft Edge.
El inicio de sesion y la seleccion de cuenta demo son manuales.

Calibrar dos flechas historicas como muestras, confirmar sus vistas previas
y seleccionar el area del grafico. Se filtran botones anchos y elementos
aislados buscando una secuencia de al menos tres velas con espaciado regular.
OpenCV exige linea roja de expiracion, vela actual y punto blanco con
coincidencia espacial; admite una vela a la izquierda o cruzando la linea.
El grafico debe estar en tiempo real; no se puede verificar esto por imagen.
Los colores de velas son configurables y distintos de los de las flechas.
Mantener el grafico visible y sin cambiar activo, periodo, zoom o posicion.
Las marcas de tiempo de los intervalos se infieren del reloj del PC:
no se leen las horas de las velas. El CSV registra detecciones, no ganancias.
Las operaciones demo y resultados confirmados se guardan en SQLite local.
El perfil .iq_option_demo_profile guarda la sesion: no compartir ni subir.
"""

from __future__ import annotations

import csv
import math
import logging
from logging.handlers import RotatingFileHandler
import sys
import time
import tkinter as tk
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox
from tkinter import ttk
from typing import Callable, Literal

import cv2
from PIL import Image, ImageChops, ImageDraw, ImageGrab, ImageTk
from playwright.sync_api import Error as BrowserError

from demo_execution import DemoBrowser, DemoExecution, EntryWindowExpired, Ledger
from monitors import (
    Monitor, click_screen, enable_physical_coordinates, list_monitors, place_selector,
)
from live_candle_reference import LiveCandleReference, ReferenceUnavailable
from live_ema_analysis import LiveEMAAnalysis
from ema_analyzer import EMAResult, report as ema_report
import config

Signal = Literal["CALL", "PUT"]


class EMAEntryBlocked(RuntimeError):
    pass


Box = tuple[int, int, int, int]
Point = tuple[int, int]
COLORS: dict[Signal, tuple[int, int, int]] = {
    "CALL": (0, 200, 83),
    "PUT": (255, 23, 68),
}
COLOR_TOLERANCE = 24
MIN_SCORE = 0.70
MIN_ARROW_WIDTH = 17
MIN_ARROW_HEIGHT = 24
MAX_ARROW_FRAGMENT_GAP = 3
CAPTURE_INTERVAL_MS = 100
CSV_FIELDS = (
    "observed_at_utc", "asset_declared", "timeframe_seconds",
    "inferred_interval_start_utc", "signal", "visual_score", "mode",
)
LOGGER = logging.getLogger("iq_option_bot")


def chart_background_click_point(
    image: Image.Image, region: Box,
) -> tuple[int, int] | None:
    """Buscar una zona vacia azul oscura dentro del grafico, lejos de velas/UI."""
    left, top, right, bottom = region
    if image.size != (right - left, bottom - top):
        raise ValueError("La captura no coincide con la zona seleccionada.")
    width, height = image.size
    if width < 40 or height < 40:
        return None
    ideal_x, ideal_y = round(width * .30), round(height * .86)
    candidates: list[tuple[int, int, int]] = []
    for y in range(max(3, round(height * .70)), min(height - 3, round(height * .96)), 5):
        for x in range(max(3, round(width * .12)), min(width - 3, round(width * .88)), 5):
            patch = [
                image.getpixel((x + dx, y + dy))[:3]
                for dy in range(-2, 3) for dx in range(-2, 3)
            ]
            if all(
                red < 48 and green < 54 and blue < 90 and blue - red >= 5
                for red, green, blue in patch
            ):
                variance = sum(max(pixel) - min(pixel) for pixel in patch)
                distance = (x - ideal_x) ** 2 + (y - ideal_y) ** 2
                candidates.append((variance * 1000 + distance, x, y))
    if not candidates:
        return None
    _, x, y = min(candidates)
    return left + x, top + y


def point_over_window(point: Point, window: tk.Tk) -> bool:
    x, y = point
    left, top = window.winfo_rootx(), window.winfo_rooty()
    return left <= x < left + window.winfo_width() and top <= y < top + window.winfo_height()


def configure_diagnostics(path: Path) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path, maxBytes=1_000_000, backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)
    return handler


@dataclass(frozen=True)
class Shape:
    width: int
    height: int
    pixels: frozenset[Point]
    origin: Point = field(default=(0, 0), compare=False)


@dataclass(frozen=True)
class Detection:
    signal: Signal
    score: float


class TrackingUnavailable(RuntimeError):
    """No emitir senales mientras no se pueda identificar la serie de velas."""


def components(
    image: Image.Image, signal: Signal,
    color: tuple[int, int, int] | None = None, tolerance: int = COLOR_TOLERANCE,
    min_pixels: int = 12, min_width: int = 4, min_height: int = 4,
) -> list[Shape]:
    """Separar formas conectadas del color configurado, no velas ni EMAs."""
    rgb = image.convert("RGB")
    target = COLORS[signal] if color is None else color
    mask = Image.new("L", rgb.size, 255)
    for channel, value in zip(rgb.split(), target):
        lookup = [255 if abs(candidate - value) <= tolerance else 0 for candidate in range(256)]
        mask = ImageChops.multiply(mask, channel.point(lookup))
    remaining = {
        (index % rgb.width, index // rgb.width)
        for index, value in enumerate(mask.tobytes()) if value
    }
    shapes: list[Shape] = []
    while remaining:
        seed = remaining.pop()
        connected = {seed}
        pending = [seed]
        while pending:
            x, y = pending.pop()
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                neighbor = (x + dx, y + dy)
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    connected.add(neighbor)
                    pending.append(neighbor)
        if len(connected) < min_pixels:
            continue
        left = min(x for x, _ in connected)
        top = min(y for _, y in connected)
        width = max(x for x, _ in connected) - left + 1
        height = max(y for _, y in connected) - top + 1
        if width >= min_width and height >= min_height:
            shapes.append(Shape(
                width, height,
                frozenset((x - left, y - top) for x, y in connected),
                (left, top),
            ))
    return shapes


def template_from(image: Image.Image, signal: Signal) -> Shape:
    shapes = components(image, signal)
    if not shapes:
        raise ValueError(
            f"No se encontro una flecha {signal} del color esperado. "
            "Selecciona solo la flecha, sin texto, y verifica el indicador."
        )
    shapes.sort(key=lambda shape: len(shape.pixels), reverse=True)
    # Las letras separadas del rotulo pueden entrar en el rectangulo.
    # Solo descartarlas si son mucho menores que la figura principal.
    if len(shapes) > 1 and len(shapes[1].pixels) * 4 >= len(shapes[0].pixels):
        raise ValueError(
            "Hay varias figuras de tamano similar. Selecciona una sola flecha."
        )
    return shapes[0]


def arrow_size_allowed(actual: Shape, expected: Shape) -> bool:
    return (
        MIN_ARROW_WIDTH <= actual.width <= expected.width + 1
        and MIN_ARROW_HEIGHT <= actual.height <= expected.height + 2
    )


def similarity(actual: Shape, expected: Shape) -> float:
    if not arrow_size_allowed(actual, expected):
        return 0.0
    if actual.width < expected.width - 1 or actual.height < expected.height - 2:
        mask = Image.new("L", (expected.width, expected.height))
        for point in expected.pixels:
            mask.putpixel(point, 255)
        mask = mask.resize((actual.width, actual.height), Image.Resampling.NEAREST)
        expected = Shape(
            actual.width, actual.height,
            frozenset(
                (i % mask.width, i // mask.width)
                for i, value in enumerate(mask.tobytes()) if value
            ),
        )
    return max(
        len(shifted & expected.pixels) / len(shifted | expected.pixels)
        for dx in range(-1, 2)
        for dy in range(-2, 3)
        for shifted in (frozenset((x + dx, y + dy) for x, y in actual.pixels),)
    )


def arrow_shapes(image: Image.Image, signal: Signal, template: Shape) -> list[Shape]:
    """Agrupar cortes de hasta tres pixeles sin rellenar la silueta visible."""
    fragments = components(
        image, signal, min_pixels=3, min_width=1, min_height=1,
    )
    shapes = [
        fragment for fragment in fragments
        if len(fragment.pixels) >= 12 and fragment.width >= 4 and fragment.height >= 4
    ]
    candidates = [
        fragment for fragment in fragments
        if fragment.width <= template.width + 1
        and fragment.height <= template.height + 2
        and similarity(fragment, template) < MIN_SCORE
    ]
    owners: dict[Point, int] = {}
    for index, fragment in enumerate(candidates):
        left, top = fragment.origin
        for x, y in fragment.pixels:
            owners[(left + x, top + y)] = index
    neighbors: list[set[int]] = [set() for _ in candidates]
    for (x, y), index in owners.items():
        for distance in range(2, MAX_ARROW_FRAGMENT_GAP + 2):
            for point in ((x + distance, y), (x, y + distance)):
                other = owners.get(point)
                if other is not None and other != index:
                    neighbors[index].add(other)
                    neighbors[other].add(index)
    visited: set[int] = set()
    for index in range(len(candidates)):
        if index in visited:
            continue
        group = {index}
        pending = [index]
        visited.add(index)
        while pending:
            for other in neighbors[pending.pop()]:
                if other not in visited:
                    visited.add(other)
                    group.add(other)
                    pending.append(other)
        if len(group) < 2:
            continue
        pixels = {
            (fragment.origin[0] + x, fragment.origin[1] + y)
            for member in group
            for fragment in (candidates[member],)
            for x, y in fragment.pixels
        }
        left = min(x for x, _ in pixels)
        top = min(y for _, y in pixels)
        width = max(x for x, _ in pixels) - left + 1
        height = max(y for _, y in pixels) - top + 1
        if width > template.width + 1 or height > template.height + 2:
            continue
        shapes.append(Shape(
            width, height,
            frozenset((x - left, y - top) for x, y in pixels),
            (left, top),
        ))
    return shapes


def detect(
    image: Image.Image, templates: dict[Signal, Shape],
    center_range: tuple[float, float] | None = None,
    diagnostics: list[str] | None = None,
) -> Detection | None:
    matches: list[Detection] = []
    for signal, template in templates.items():
        shapes = [
            shape for shape in arrow_shapes(image, signal, template)
            if center_range is None
            or center_range[0] < shape.origin[0] + (shape.width - 1) / 2 < center_range[1]
        ]
        scores = [similarity(shape, template) for shape in shapes]
        accepted = [score for score in scores if score >= MIN_SCORE]
        if diagnostics is not None and shapes and not accepted:
            best = max(shapes, key=lambda shape: (
                similarity(shape, template),
                -abs(shape.width - template.width) - abs(shape.height - template.height),
            ))
            if not arrow_size_allowed(best, template):
                diagnostics.append(
                    f"{signal}: figura {best.width}x{best.height}, muestra "
                    f"{template.width}x{template.height}. Tamano fuera de rango "
                    f"(minimo {MIN_ARROW_WIDTH}x{MIN_ARROW_HEIGHT}); recalibra."
                )
            else:
                diagnostics.append(
                    f"{signal}: coincidencia {similarity(best, template):.0%}; "
                    f"requiere {MIN_SCORE:.0%}. Forma/color no coincide con la muestra "
                    f"(figura {best.width}x{best.height})."
                )
        if len(accepted) > 1:
            raise RuntimeError(
                f"Varias flechas {signal} en la zona: recalibra solo la vela actual."
            )
        if accepted:
            matches.append(Detection(signal, accepted[0]))
    if len(matches) > 1:
        raise RuntimeError("CALL y PUT simultaneos: zona ambigua. Recalibra.")
    return matches[0] if matches else None


def make_preview(
    image: Image.Image, band: tuple[float, float] | None, color: str,
) -> Image.Image:
    preview = image.copy()
    preview.thumbnail((400, 160))
    if band is not None:
        scale = preview.width / image.width
        draw = ImageDraw.Draw(preview)
        for boundary in band:
            x = max(0, min(preview.width - 1, round(boundary * scale)))
            draw.line((x, 0, x, preview.height - 1), fill=color, width=2)
    return preview


def parse_color(value: str) -> tuple[int, int, int]:
    if len(value) != 7 or not value.startswith("#"):
        raise ValueError("Usa colores de velas en formato #RRGGBB.")
    try:
        return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
    except ValueError as exc:
        raise ValueError("Color de vela invalido: usa #RRGGBB.") from exc


def candle_mask(
    image: Image.Image, colors: tuple[tuple[int, int, int], tuple[int, int, int]],
) -> Image.Image:
    rgb = image.convert("RGB")
    channels = rgb.split()
    mask = Image.new("L", rgb.size, 0)
    for color in colors:
        color_mask = Image.new("L", rgb.size, 255)
        for channel, target in zip(channels, color):
            lookup = [255 if abs(value - target) <= 10 else 0 for value in range(256)]
            color_mask = ImageChops.multiply(color_mask, channel.point(lookup))
        mask = ImageChops.lighter(mask, color_mask)
    return mask


def track_candle(
    image: Image.Image, colors: tuple[tuple[int, int, int], tuple[int, int, int]],
) -> tuple[float, float]:
    """Buscar una secuencia regular de velas, no el ultimo objeto de color."""
    return _track_mask(candle_mask(image, colors))


def current_candle_reference(
    image: Image.Image, colors: tuple[tuple[int, int, int], tuple[int, int, int]],
) -> tuple[float, float]:
    dot = white_price_point(image)
    band = _track_mask(candle_mask(image, colors), anchor=dot)
    center = sum(band) / 2
    spacing = band[1] - band[0]
    tolerance = min(3.0, spacing * 0.12)
    if abs(dot - center) > tolerance:
        raise TrackingUnavailable(
            "Esperando: punto blanco no coincide con la ultima vela. Sin entrada."
        )
    return (dot - tolerance, dot + tolerance)


def white_price_point(image: Image.Image) -> float:
    rgb = image.convert("RGB")
    candidates: list[float] = []
    for shape in components(
        rgb, "CALL", color=(255, 255, 255), tolerance=26,
        min_pixels=4, min_width=2, min_height=2,
    ):
        if not (2 <= shape.width <= 12 and 2 <= shape.height <= 12):
            continue
        if max(shape.width, shape.height) > min(shape.width, shape.height) * 1.5:
            continue
        if not 0.45 <= len(shape.pixels) / (shape.width * shape.height) <= 1.0:
            continue
        x = shape.origin[0] + (shape.width - 1) / 2
        y = shape.origin[1] + (shape.height - 1) / 2
        supported = False
        for row in range(max(0, round(y) - 3), min(rgb.height, round(y) + 4)):
            left_support = 0
            right_support = 0
            for distance in range(8, 25):
                left, right = round(x) - distance, round(x) + distance
                if left < 0 or right >= rgb.width or row + 6 >= rgb.height:
                    continue
                a, b = rgb.getpixel((left, row)), rgb.getpixel((right, row))
                below_a = rgb.getpixel((left, row + 6))
                below_b = rgb.getpixel((right, row + 6))
                if max(a) >= 100 and max(abs(a[i] - below_a[i]) for i in range(3)) >= 40:
                    left_support += 1
                if max(b) >= 100 and max(abs(b[i] - below_b[i]) for i in range(3)) >= 40:
                    right_support += 1
            if left_support >= 12 and right_support >= 12:
                supported = True
                break
        if supported:
            candidates.append(x)
    if len(candidates) != 1:
        raise TrackingUnavailable(
            "Esperando: punto blanco de precio ausente o ambiguo. Sin entrada."
        )
    return candidates[0]


def _track_mask(mask: Image.Image, anchor: float | None = None) -> tuple[float, float]:
    columns: list[int] = []
    for x in range(mask.width):
        if mask.crop((x, 0, x + 1, mask.height)).histogram()[255] >= 1:
            columns.append(x)
    groups: list[list[int]] = []
    for x in columns:
        if not groups or x > groups[-1][-1] + 1:
            groups.append([x])
        else:
            groups[-1].append(x)
    sequences: list[list[list[int]]] = []
    for start in range(len(groups) - 2):
        first, second = groups[start:start + 2]
        spacing = (second[0] + second[-1] - first[0] - first[-1]) / 2
        if spacing < 6 or max(len(first), len(second)) >= spacing * 0.8:
            continue
        sequence = [first, second]
        index = start + 2
        while index < len(groups):
            candidate = groups[index]
            expected_center = (sequence[-1][0] + sequence[-1][-1]) / 2 + spacing
            slot_left = expected_center - spacing * 0.45
            slot_right = expected_center + spacing * 0.45
            # Una linea de precio/mecha puede separar el cuerpo en fragmentos.
            # Unirlos solo dentro de la misma posicion de la cuadricula de velas.
            if candidate[0] >= slot_left and candidate[-1] <= slot_right:
                end = index + 1
                while end < len(groups) and groups[end][-1] <= slot_right:
                    end += 1
                candidate = list(range(candidate[0], groups[end - 1][-1] + 1))
                index = end
            else:
                index += 1
            step = (
                candidate[0] + candidate[-1]
                - sequence[-1][0] - sequence[-1][-1]
            ) / 2
            if (
                abs(step - spacing) > max(2, spacing * 0.15)
                or len(candidate) >= spacing * 0.8
            ):
                break
            sequence.append(candidate)
        if len(sequence) >= 3:
            sequences.append(sequence)
    if not sequences:
        raise TrackingUnavailable(
            "Esperando seguimiento: no hay tres velas separadas con espaciado "
            "regular. Verifica colores y que el grafico este visible."
        )
    if anchor is not None:
        sequences = [
            sequence for sequence in sequences
            if abs((sequence[-1][0] + sequence[-1][-1]) / 2 - anchor) <= 3
        ]
        if not sequences:
            raise TrackingUnavailable(
                "Esperando: punto blanco no coincide con una vela de la serie. Sin entrada."
            )
    sequences.sort(key=len, reverse=True)
    if len(sequences) > 1 and len(sequences[0]) == len(sequences[1]):
        raise TrackingUnavailable(
            "Seguimiento ambiguo: varias series de velas. Selecciona un solo grafico."
        )
    sequence = sequences[0]
    latest = sequence[-1]
    if latest[-1] >= mask.width - 1:
        raise TrackingUnavailable("Esperando: ultima vela recortada por el borde.")
    previous = sequence[-2]
    center = (latest[0] + latest[-1]) / 2
    spacing = center - (previous[0] + previous[-1]) / 2
    # No aceptar una serie truncada si hay un objeto estrecho en la posicion
    # esperada para la nueva vela pero su separacion no encaja.
    later = [group for group in groups if group[0] > latest[-1]]
    if later:
        next_group = later[0]
        next_center = (next_group[0] + next_group[-1]) / 2
        if len(next_group) < spacing * 0.8 and next_center - center < spacing * 1.5:
            raise TrackingUnavailable("Esperando: nueva vela aun no identificable.")
    previous_center = (previous[0] + previous[-1]) / 2
    return ((previous_center + center) / 2, min(mask.width, center + spacing / 2))


class SignalGate:
    """Rearmar tras dos ausencias y aceptar una captura; un evento/intervalo."""

    def __init__(self, period: int) -> None:
        if period <= 0:
            raise ValueError("El periodo debe ser positivo.")
        self.period = period
        self.bucket: int | None = None
        self.last_timestamp: float | None = None
        self.absent = 0
        self.armed = False
        self.emitted = False
        self.candidate: Signal | None = None
        self.consecutive = 0

    def observe(self, timestamp: float, detection: Detection | None) -> bool:
        if not math.isfinite(timestamp):
            raise ValueError("Hora del PC invalida.")
        if self.last_timestamp is not None and timestamp < self.last_timestamp:
            raise RuntimeError("El reloj retrocedio. Deten el bot y revisa la hora del PC.")
        self.last_timestamp = timestamp
        bucket = int(timestamp // self.period)
        if bucket != self.bucket:
            self.bucket = bucket
            self.absent = 0
            self.armed = False
            self.emitted = False
            self.candidate = None
            self.consecutive = 0
        if detection is None:
            self.absent += 1
            self.candidate = None
            self.consecutive = 0
            if self.absent >= 2:
                self.armed = True
            return False
        self.absent = 0
        if not self.armed or self.emitted:
            return False
        if detection.signal == self.candidate:
            self.consecutive += 1
        else:
            self.candidate = detection.signal
            self.consecutive = 1
        if self.consecutive >= 1:
            self.emitted = True
            return True
        return False


def utc_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def write_event(
    path: Path, asset: str, period: int, timestamp: float, detection: Detection,
) -> None:
    exists = path.exists() and path.stat().st_size > 0
    if exists:
        with path.open(newline="", encoding="utf-8") as stream:
            if next(csv.reader(stream), None) != list(CSV_FIELDS):
                raise ValueError("El CSV existente tiene otro formato; usa otro archivo.")
    with path.open("a", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        if not exists:
            writer.writerow(CSV_FIELDS)
        writer.writerow((
            utc_text(timestamp), asset, period,
            utc_text(int(timestamp // period) * period),
            detection.signal, f"{detection.score:.3f}", "VISUAL_SIMULATION",
        ))


class Selector:
    """Seleccion manual sobre una captura local que no se guarda en disco."""

    def __init__(
        self, root: tk.Tk, image: Image.Image, title: str,
        callback: Callable[[Box], None],
        monitor: Monitor | None = None,
    ) -> None:
        self.window = tk.Toplevel(root)
        if monitor is None:
            self.window.attributes("-fullscreen", True)
        else:
            self.window.overrideredirect(True)
            self.window.geometry(f"{image.width}x{image.height}")
        self.window.attributes("-topmost", True)
        self.photo = ImageTk.PhotoImage(image)
        self.canvas = tk.Canvas(self.window, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.canvas.create_text(
            20, 20, text=f"{title} | Arrastra un rectangulo. Esc: cancelar",
            anchor="nw", fill="white", font=("Arial", 16),
        )
        self.start: Point | None = None
        self.rectangle: int | None = None
        self.callback = callback
        self.image = image
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        if monitor is not None:
            self.window.update_idletasks()
            try:
                place_selector(self.window.winfo_id(), monitor.bounds)
            except (OSError, RuntimeError):
                self.window.destroy()
                raise
        self.window.focus_force()

    def press(self, event: tk.Event) -> None:
        self.start = (event.x, event.y)
        if self.rectangle is not None:
            self.canvas.delete(self.rectangle)
        self.rectangle = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="white", width=2,
        )

    def drag(self, event: tk.Event) -> None:
        if self.start is not None and self.rectangle is not None:
            self.canvas.coords(self.rectangle, *self.start, event.x, event.y)

    def release(self, event: tk.Event) -> None:
        if self.start is None:
            return
        x, y = self.start
        box = (
            max(0, min(x, event.x)), max(0, min(y, event.y)),
            min(self.image.width, max(x, event.x)),
            min(self.image.height, max(y, event.y)),
        )
        if box[2] - box[0] < 4 or box[3] - box[1] < 4:
            messagebox.showerror(
                "Seleccion demasiado pequena",
                "El rectangulo debe medir al menos 4 pixeles de ancho y alto. "
                "Arrastra de nuevo y suelta el boton del raton para guardar.",
                parent=self.window,
            )
            return
        self.window.destroy()
        self.callback(box)


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.templates: dict[Signal, Shape] = {}
        self.region: Box | None = None
        self.gate: SignalGate | None = None
        self.job: str | None = None
        self.log_path = Path(__file__).with_name("senales_simuladas.csv")
        self.period = tk.StringVar(value="300")
        self.status = tk.StringVar(value="Detenido. Calibra muestras y zona.")
        self.running_asset = ""
        self.running_period = 0
        self.calibration_status = tk.StringVar(value="CALL: pendiente | PUT: pendiente")
        self.region_status = tk.StringVar(value="Zona: pendiente")
        self.green_candle = tk.StringVar(value="#2D9E6B")
        self.red_candle = tk.StringVar(value="#E44A4E")
        self.candle_colors = (parse_color("#2D9E6B"), parse_color("#E44A4E"))
        self.visual_reference: LiveCandleReference | None = None
        self.reference_status = tk.StringVar(value="Referencia: pendiente")
        self.ema_analysis: LiveEMAAnalysis | None = None
        self.ema_result: EMAResult | None = None
        self.ema_status = tk.StringVar(value="EMA: pendiente.")
        self.signal_text = tk.StringVar(value="SIN SENAL ACTUAL")
        self.preview_photo: ImageTk.PhotoImage | None = None
        self.browser = DemoBrowser()
        self.ledger = Ledger(Path(__file__).with_name("operaciones_demo.sqlite3"))
        self.execution = DemoExecution(self.browser, self.ledger)
        self.order_status = tk.StringVar(value="Ejecucion DESARMADA: solo avisos")
        self.summary_text = tk.StringVar(value=self.statistics_summary())
        self.result_job: str | None = None
        self.monitors = list_monitors()
        self.monitor_index = 0
        self.monitor_label = tk.StringVar(value=self.monitors[0].label)
        root.title("IQ Option - Detector y ejecucion SOLO DEMO")
        root.attributes("-topmost", True)
        root.geometry("470x850")
        tk.Button(
            root, text="PARADA DE EMERGENCIA / DESARMAR (Esc)",
            command=self.stop, bg="#FFCCCC",
        ).pack(fill="x", padx=8, pady=5)
        scroll = tk.Canvas(root, highlightthickness=0)
        scrollbar = ttk.Scrollbar(root, orient="vertical", command=scroll.yview)
        scrollbar.pack(side="right", fill="y")
        scroll.pack(side="left", fill="both", expand=True)
        scroll.configure(yscrollcommand=scrollbar.set)
        content = tk.Frame(scroll)
        content_id = scroll.create_window((0, 0), window=content, anchor="nw")
        content.bind(
            "<Configure>",
            lambda _event: scroll.configure(scrollregion=scroll.bbox("all")),
        )
        scroll.bind(
            "<Configure>", lambda event: scroll.itemconfigure(content_id, width=event.width),
        )
        scroll.bind_all(
            "<MouseWheel>", lambda event: scroll.yview_scroll(-int(event.delta / 120), "units"),
        )
        tk.Label(
            content, text="SOLO DEMO: compras desactivadas al iniciar.\n"
            "Elige el monitor del grafico; mueve este panel fuera del grafico.\n"
            "Cambios de zoom/activo/periodo/posicion requieren recalibrar.\n"
            "Las senales de la vela abierta pueden desaparecer.",
            justify="left",
        ).pack(padx=12, pady=10)
        self.monitor_picker = ttk.Combobox(
            content, textvariable=self.monitor_label,
            values=[monitor.label for monitor in self.monitors], state="readonly",
            width=48,
        )
        self.monitor_picker.pack(pady=3)
        self.monitor_picker.bind("<<ComboboxSelected>>", self.change_monitor)
        tk.Button(
            content, text="Actualizar monitores", command=self.refresh_monitors,
        ).pack(pady=3)
        tk.Button(
            content, text="Abrir IQ Option", command=self.open_iq_option,
        ).pack(fill="x", padx=12, pady=3)
        tk.Label(content, text="Temporalidad: 60 = 1 minuto; 300 = 5 minutos").pack()
        ttk.Combobox(
            content, textvariable=self.period, values=("60", "300"),
            state="readonly", width=12,
        ).pack()
        tk.Label(content, text="Colores de velas (no flechas), formato #RRGGBB:").pack()
        tk.Entry(content, textvariable=self.green_candle, width=12).pack()
        tk.Entry(content, textvariable=self.red_candle, width=12).pack()
        for signal in ("CALL", "PUT"):
            tk.Button(
                content, text=f"Seleccionar muestra {signal} (solo flecha)",
                command=lambda selected=signal: self.select_sample(selected),
            ).pack(fill="x", padx=12, pady=3)
        tk.Label(content, textvariable=self.calibration_status).pack(pady=3)
        tk.Button(
            content, text="Seleccionar area de velas del grafico",
            command=self.select_region,
        ).pack(fill="x", padx=12, pady=3)
        tk.Label(content, textvariable=self.region_status, wraplength=430).pack(pady=3)
        tk.Button(content, text="INICIAR detector y operaciones DEMO", command=self.start).pack(pady=5)
        tk.Button(content, text="DETENER (Esc)", command=self.stop).pack(pady=5)
        tk.Label(content, textvariable=self.order_status, wraplength=430).pack(pady=3)
        tk.Label(content, textvariable=self.summary_text, justify="left").pack(pady=3)
        tk.Button(
            content, text="REINICIAR ESTADISTICAS (conservar historial)",
            command=self.reset_statistics,
        ).pack(pady=3)
        self.signal_panel = tk.Label(
            content, textvariable=self.signal_text, font=("Arial", 16, "bold"),
            fg="gray",
        )
        self.signal_panel.pack(pady=5)
        self.preview_panel = tk.Label(content)
        self.preview_panel.pack(pady=3)
        tk.Label(content, textvariable=self.reference_status, wraplength=430, justify="left").pack(pady=3)
        tk.Label(content, textvariable=self.ema_status, wraplength=430, justify="left").pack(pady=3)
        tk.Label(content, textvariable=self.status, wraplength=430).pack(padx=12, pady=8)
        tk.Label(content, text=f"Registro local: {self.log_path}", wraplength=430).pack(pady=5)
        root.bind("<Escape>", lambda _event: self.stop())
        root.protocol("WM_DELETE_WINDOW", self.close)

    def open_iq_option(self) -> None:
        self.stop()
        try:
            self.browser.open(Path(__file__).with_name(".iq_option_demo_profile"))
        except (OSError, BrowserError, RuntimeError) as exc:
            self.status.set("No se pudo abrir IQ Option. Detector detenido.")
            messagebox.showerror(
                "No se pudo abrir IQ Option",
                f"{exc}\nSe requiere Microsoft Edge instalado. "
                "No se habilitaran operaciones en otro navegador.",
            )
            return
        self.status.set(
            "Apertura de IQ Option solicitada en ventana dedicada. Inicia sesion manualmente, "
            "selecciona cuenta demo y calibra el grafico antes de iniciar."
        )

    def statistics_summary(self) -> str:
        return (
            f"{self.ledger.summary()}\n"
            f"Perdidas de seguridad: {self.execution.session_losses}/3"
        )

    def change_monitor(self, _event: tk.Event | None = None) -> None:
        self.stop()
        self.monitor_index = self.monitor_picker.current()
        self.clear_calibration()

    def clear_calibration(self) -> None:
        self.region = None
        self.templates.clear()
        self.calibration_status.set("CALL: pendiente | PUT: pendiente")
        self.region_status.set("Zona: pendiente")
        self.preview_panel.configure(image="")
        self.preview_photo = None
        self.status.set("Monitor cambiado. Mueve el navegador alli y recalibra muestras y zona.")

    def refresh_monitors(self) -> None:
        self.stop()
        try:
            monitors = list_monitors()
        except (OSError, RuntimeError) as exc:
            messagebox.showerror("Monitores no disponibles", str(exc))
            return
        self.monitors = monitors
        self.monitor_index = 0
        self.monitor_picker.configure(values=[monitor.label for monitor in monitors])
        self.monitor_picker.current(0)
        self.clear_calibration()

    def capture_monitor(self) -> tuple[Monitor, Image.Image]:
        monitor = self.monitors[self.monitor_index]
        if monitor not in list_monitors():
            raise RuntimeError("La pantalla cambio. Actualiza monitores y recalibra.")
        return monitor, ImageGrab.grab(bbox=monitor.bounds, all_screens=True)

    def select_sample(self, signal: Signal) -> None:
        self.stop()
        try:
            monitor, image = self.capture_monitor()
        except (OSError, RuntimeError) as exc:
            messagebox.showerror("Captura fallida", str(exc))
            return

        def selected(box: Box) -> None:
            try:
                template = template_from(image.crop(box), signal)
            except ValueError as exc:
                messagebox.showerror(
                    "Muestra invalida",
                    f"{exc}\n\nColores esperados: CALL verde #00C853, PUT rojo "
                    "#FF1744. Selecciona la figura completa y comprueba que "
                    "la ventana del bot no la tapa.",
                )
                return
            if not self.confirm_sample(template, signal):
                self.status.set(f"Nueva muestra {signal} cancelada; no se guardo.")
                return
            self.templates[signal] = template
            self.calibration_status.set(" | ".join(
                f"{name}: {'lista' if name in self.templates else 'pendiente'}"
                for name in ("CALL", "PUT")
            ))
            self.status.set(
                f"Muestra {signal} confirmada ({template.width}x{template.height})."
            )

        try:
            Selector(
                self.root, image, f"Muestra {signal}: solo la flecha, sin texto",
                selected, monitor=monitor,
            )
        except (OSError, RuntimeError) as exc:
            messagebox.showerror("Selector no disponible", str(exc))

    def confirm_sample(self, template: Shape, signal: Signal) -> bool:
        preview = Image.new(
            "RGB", (template.width + 8, template.height + 8), "#101827",
        )
        for x, y in template.pixels:
            preview.putpixel((x + 4, y + 4), COLORS[signal])
        scale = max(1, min(6, 240 // max(preview.size)))
        preview = preview.resize(
            (preview.width * scale, preview.height * scale),
            Image.Resampling.NEAREST,
        )
        window = tk.Toplevel(self.root)
        window.title(f"Confirmar muestra {signal}")
        window.attributes("-topmost", True)
        photo = ImageTk.PhotoImage(preview)
        tk.Label(
            window, text="Esta es la figura que reconocio el detector.\n"
            "Confirma solo si es la flecha completa, no letras ni una vela.",
        ).pack(padx=12, pady=10)
        tk.Label(window, image=photo).pack(padx=12, pady=10)
        accepted = False

        def confirm() -> None:
            nonlocal accepted
            accepted = True
            window.destroy()

        tk.Button(window, text="Es la flecha correcta", command=confirm).pack(pady=5)
        tk.Button(window, text="Cancelar", command=window.destroy).pack(pady=5)
        window.bind("<Escape>", lambda _event: window.destroy())
        window.transient(self.root)
        window.grab_set()
        self.root.wait_window(window)
        return accepted

    def select_region(self) -> None:
        self.stop()
        try:
            monitor, image = self.capture_monitor()
        except (OSError, RuntimeError) as exc:
            messagebox.showerror("Captura fallida", str(exc))
            return

        def selected(box: Box) -> None:
            self.region = monitor.to_screen(box)
            self.region_status.set(
                f"Zona guardada: {box[2] - box[0]}x{box[3] - box[1]} px "
                f"| izquierda={self.region[0]}, arriba={self.region[1]}"
            )
            self.status.set(
                f"Zona {box}. Incluye al menos tres velas, flechas, punto blanco "
                "y linea roja. Excluye botones y paneles."
            )

        try:
            Selector(
                self.root, image,
                "Selecciona solo el grafico con linea roja, velas y punto",
                selected, monitor=monitor,
            )
        except (OSError, RuntimeError) as exc:
            messagebox.showerror("Selector no disponible", str(exc))

    def start(self) -> None:
        self.stop()
        missing = [
            f"muestra {signal}" for signal in ("CALL", "PUT")
            if signal not in self.templates
        ]
        if self.region is None:
            missing.append("area de velas del grafico")
        if missing:
            messagebox.showerror(
                "Falta calibracion",
                "Falta guardar: " + ", ".join(missing) + ".\n"
                "Las muestras necesitan confirmacion; la zona se guarda "
                "al soltar el raton. Revisa los estados debajo de los botones.",
            )
            return
        try:
            period = int(self.period.get())
            if period not in (60, 300):
                raise ValueError("Selecciona 1 minuto (60) o 5 minutos (300).")
            gate = SignalGate(period)
            if config.EMA_ENTRY_FILTER_ENABLED and not config.EMA_ANALYSIS_ENABLED:
                raise ValueError("El filtro de entradas EMA requiere EMA_ANALYSIS_ENABLED = True.")
            ema_analysis = LiveEMAAnalysis() if config.EMA_ANALYSIS_ENABLED else None
            colors = (parse_color(self.green_candle.get()), parse_color(self.red_candle.get()))
            if colors[0] == colors[1]:
                raise ValueError("Las velas alcistas y bajistas necesitan colores distintos.")
        except ValueError as exc:
            messagebox.showerror("Configuracion invalida", str(exc))
            return
        if not messagebox.askokcancel(
            "Iniciar detector y operaciones SOLO DEMO",
            "La zona debe excluir botones e incluir la linea roja, el punto blanco y al menos tres velas visibles "
            "de un solo grafico. El grafico debe estar EN TIEMPO REAL. Se sigue la vela "
            "visible mas a la derecha; no se puede verificar su hora.\n\n"
            + ("Filtro EMA activo: CALL requiere BULLISH; PUT requiere BEARISH. "
               "CROSSING, SIDEWAYS y UNKNOWN bloquean entradas.\n\n"
               if config.EMA_ENTRY_FILTER_ENABLED else "") +
            "INICIAR valida la cuenta demo y habilita operaciones: CALL pulsa Sube "
            f"y PUT pulsa Baja al cierre de la vela de {period // 60} minuto(s) (Colombia). "
            "Usa el importe configurado en la web. Se detiene tras 3 perdidas "
            "totales; solo permite una posicion pendiente. "
            "No cambies de cuenta ni operes manualmente. Esc/DETENER desactiva "
            "las entradas, pero no cancela posiciones enviadas. Confirmas iniciar?",
        ):
            return
        self.running_asset = "NO_IDENTIFICADO"
        self.running_period = period
        self.candle_colors = colors
        try:
            self.execution.arm(period)
        except (BrowserError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            LOGGER.exception("Inicio bloqueado: no se pudo validar ejecucion demo")
            self.execution.armed = False
            self.order_status.set(f"DEMO BLOQUEADA: {exc}")
            self.status.set("No se inicio: fallo la validacion de la cuenta demo.")
            if self.ledger.pending() is not None:
                self.schedule_results()
            messagebox.showerror("No se pudo iniciar demo", str(exc))
            return
        LOGGER.info("DEMO ACTIVADA AL INICIAR; intervalo=%s; perdidas por activacion=0", period)
        self.order_status.set("DEMO ACTIVA: esperando una nueva senal validada")
        self.gate = gate
        self.last_reference_center = None
        self.visual_reference = LiveCandleReference(colors)
        self.ema_analysis = ema_analysis
        self.ema_result = None
        self.ema_status.set("EMA: esperando captura." if ema_analysis else "EMA: desactivado.")
        self.tick()

    def update_ema_analysis(self, image: Image.Image) -> None:
        analysis = getattr(self, "ema_analysis", None)
        self.ema_result = None
        if analysis is None:
            return
        try:
            self.ema_result = analysis.update(image, time.monotonic())
            mode = "Filtro de entradas activo." if config.EMA_ENTRY_FILTER_ENABLED else "Solo informacion."
            self.ema_status.set(ema_report(self.ema_result) + "\n" + mode)
        except (ValueError, cv2.error) as exc:
            self.ema_analysis_failed(analysis, exc)

    def ema_analysis_failed(self, analysis: LiveEMAAnalysis, exc: ValueError | cv2.error) -> None:
        LOGGER.exception("Analisis EMA no disponible; UNKNOWN")
        analysis.reset()
        self.ema_result = None
        self.ema_status.set(f"EMA: UNKNOWN; error: {exc}.")

    def ema_entry_block_reason(self, direction: Signal) -> str:
        if not config.EMA_ENTRY_FILTER_ENABLED:
            return ""
        result = getattr(self, "ema_result", None)
        if not config.EMA_ANALYSIS_ENABLED or getattr(self, "ema_analysis", None) is None or result is None:
            return "EMA no disponible; sin entrada."
        age = time.monotonic() - result.timestamp
        if not math.isfinite(age) or not 0 <= age <= config.EMA_MAX_FRAME_GAP_SECONDS:
            return "EMA desactualizada; sin entrada."
        if not math.isfinite(result.confidence) or result.confidence < config.EMA_MIN_CONFIDENCE:
            return f"Confianza EMA insuficiente ({result.confidence:.0f}%); sin entrada."
        expected = "BULLISH" if direction == "CALL" else "BEARISH"
        if result.market_state != expected:
            return f"{direction} requiere EMA {expected}; estado {result.market_state}. Sin entrada."
        return ""

    def locate_visual_reference(self, image: Image.Image) -> tuple[float, float]:
        if self.gate is None:
            raise RuntimeError("Detector no iniciado.")
        if self.visual_reference is None:
            self.visual_reference = LiveCandleReference(self.candle_colors)
        try:
            return self.visual_reference.locate(
                image, time.monotonic(), int(time.time() // self.gate.period),
            )
        except ReferenceUnavailable as exc:
            raise TrackingUnavailable(str(exc)) from exc
        finally:
            self.reference_status.set(self.visual_reference.report)
            result = self.visual_reference.result
            if result is not None:
                state = (result.spatial_agreement, result.point_state, result.point_search_stage)
                if state != getattr(self, "last_visual_state", None):
                    LOGGER.info("Referencia OpenCV: %s", self.visual_reference.report)
                    self.last_visual_state = state

    def reference_preview(self, image: Image.Image, band: tuple[float, float] | None) -> Image.Image:
        annotated = self.visual_reference.image if self.visual_reference is not None else None
        preview = annotated if annotated is not None else image
        analysis = getattr(self, "ema_analysis", None)
        if analysis is not None:
            try:
                preview = analysis.overlay(preview)
            except (ValueError, cv2.error) as exc:
                self.ema_analysis_failed(analysis, exc)
        return make_preview(preview, band, "white")

    def tick(self) -> None:
        if self.gate is None or self.region is None:
            return
        image: Image.Image | None = None
        try:
            if self.monitors[self.monitor_index] not in list_monitors():
                raise RuntimeError("Monitor desconectado o cambiado. Recalibra antes de reiniciar.")
            image = ImageGrab.grab(bbox=self.region, all_screens=True)
            self.update_ema_analysis(image)
            try:
                candle_range = self.locate_visual_reference(image)
            except TrackingUnavailable:
                preview = self.reference_preview(image, None)
                self.update_preview(preview)
                raise
            # Recortar el trabajo de reconocimiento sin cortar flechas anchas.
            margin = max(template.width for template in self.templates.values())
            left = max(0, int(candle_range[0]) - margin)
            right = min(image.width, math.ceil(candle_range[1]) + margin)
            diagnostics: list[str] = []
            detection = detect(
                image.crop((left, 0, right, image.height)), self.templates,
                (candle_range[0] - left, candle_range[1] - left),
                diagnostics,
            )
            preview = self.reference_preview(image, candle_range)
            self.update_preview(preview)
            timestamp = time.time()
            reference_center = sum(candle_range) / 2
            previous_center = getattr(self, "last_reference_center", None)
            if previous_center is not None and abs(reference_center - previous_center) > 3:
                self.gate.absent = 0
                self.gate.armed = False
                self.gate.candidate = None
                self.gate.consecutive = 0
            self.last_reference_center = reference_center
            if detection is None and diagnostics:
                self.gate.absent = 0
                self.gate.candidate = None
                self.gate.consecutive = 0
                registered = False
            else:
                registered = self.gate.observe(timestamp, detection)
            ema_block = self.ema_entry_block_reason(detection.signal) if detection is not None else ""
            if ema_block:
                if registered:
                    self.gate.emitted = False
                registered = False
                self.gate.candidate = None
                self.gate.consecutive = 0
            self.show_signal(
                detection,
                detection is not None
                and detection.signal == self.gate.candidate
                and self.gate.consecutive >= 1,
            )
            if detection is None and diagnostics:
                self.signal_text.set("FIGURA DETECTADA / SENAL NO VALIDADA")
                self.signal_panel.configure(fg="#B36B00")
            if registered and detection is not None:
                pending = self.ledger.pending()
                LOGGER.info(
                    "Senal %s validada score=%.3f demo_armada=%s pendiente=%s vela_actual_x=%.1f franja=%s",
                    detection.signal, detection.score, self.execution.armed,
                    pending["id"] if pending is not None else None,
                    reference_center, candle_range,
                )
                write_event(
                    self.log_path, self.running_asset, self.running_period,
                    timestamp, detection,
                )
                if self.execution.armed and pending is None:
                    fresh = ImageGrab.grab(bbox=self.region, all_screens=True)
                    image = fresh
                    self.update_ema_analysis(fresh)
                    fresh_range = self.locate_visual_reference(fresh)
                    self.update_preview(self.reference_preview(fresh, fresh_range))
                    fresh_detection = detect(fresh, self.templates, fresh_range)
                    if (
                        abs(sum(fresh_range) / 2 - reference_center) > 2
                        or fresh_detection is None
                        or fresh_detection.signal != detection.signal
                    ):
                        raise TrackingUnavailable(
                            "Entrada cancelada: punto blanco o flecha cambio antes del clic."
                        )
                    final_ema_block = self.ema_entry_block_reason(detection.signal)
                    if final_ema_block:
                        raise EMAEntryBlocked("Captura final: " + final_ema_block)
                    LOGGER.info("Intentando entrada %s al cierre; periodo=%s", detection.signal, self.running_period)
                    identity = self.execution.submit(
                        detection.signal, timestamp, self.running_period,
                    )
                    LOGGER.info("Solicitud %s enviada; falta confirmar posicion", identity)
                    self.order_status.set(
                        "Clic enviado; esperando confirmar posicion. No se repetira."
                    )
                    self.summary_text.set(self.statistics_summary())
                    self.schedule_results()
                else:
                    reason = (
                        "hay una solicitud pendiente" if pending is not None
                        else "la ejecucion demo esta desarmada"
                    )
                    LOGGER.warning("Senal %s sin entrada: %s", detection.signal, reason)
                    self.order_status.set(f"Senal {detection.signal} sin entrada: {reason}.")
                self.status.set(
                    f"{detection.signal} visible registrada ({utc_text(timestamp)}). "
                    "Revisa el panel de ejecucion demo."
                )
            elif detection is None and diagnostics:
                self.status.set("Sin senal validada: " + " | ".join(diagnostics))
            elif not self.gate.armed:
                self.status.set(
                    "Esperando dos capturas sin flechas para rearmar. "
                    "Las flechas presentes al iniciar no se registran."
                )
            elif not self.gate.emitted:
                if detection is not None:
                    self.status.set(
                        f"{detection.signal} reconocida; validando "
                        f"{self.gate.consecutive}/1 captura valida."
                    )
                else:
                    self.status.set("Linea roja, vela y punto confirmados. Sin compras.")
            if detection is not None:
                reason = (
                    "ya registrada en este intervalo" if self.gate.emitted else
                    "espera ausencia previa; no habilitada como senal nueva"
                    if not self.gate.armed else
                    f"validando {self.gate.consecutive}/1 captura"
                )
                report = f"{detection.signal} reconocida: {reason}"
            else:
                report = " | ".join(diagnostics) if diagnostics else "Sin figuras del color esperado en vela actual"
            if ema_block:
                report = ema_block
                self.status.set(ema_block)
                self.order_status.set(ema_block)
                self.signal_text.set(f"{detection.signal} / BLOQUEADA POR EMA")
                self.signal_panel.configure(fg="#B36B00")
            if report != getattr(self, "last_detection_report", None):
                LOGGER.info("Reconocimiento: %s", report)
                self.last_detection_report = report
        except EMAEntryBlocked as exc:
            LOGGER.warning("Entrada bloqueada por EMA: %s", exc)
            self.status.set(str(exc))
            self.order_status.set(str(exc))
            self.signal_text.set("SIN ENTRADA / EMA CAMBIO")
            self.signal_panel.configure(fg="#B36B00")
        except TrackingUnavailable as exc:
            report = f"Entrada bloqueada por referencia: {exc}"
            if report != getattr(self, "last_detection_report", None):
                LOGGER.warning("%s", report)
                self.last_detection_report = report
            self.last_reference_center = None
            self.gate.armed = False
            self.gate.absent = 0
            self.gate.candidate = None
            self.gate.consecutive = 0
            self.signal_text.set("ESPERANDO VELA / SIN AVISO")
            self.signal_panel.configure(fg="gray")
            self.status.set(str(exc))
        except EntryWindowExpired as exc:
            LOGGER.warning("Entrada omitida por tiempo; detector sigue activo: %s", exc)
            self.summary_text.set(self.statistics_summary())
            self.order_status.set(f"Sin entrada por tiempo vencido: {exc}")
            self.status.set("Tiempo de entrada vencido. Esperando una nueva senal.")
        except (OSError, ValueError, RuntimeError, BrowserError, sqlite3.Error, cv2.error) as exc:
            LOGGER.exception("Detector detenido por error; sin reintentar ordenes")
            self.stop()
            self.order_status.set(f"DEMO DETENIDA: {exc}")
            self.status.set(f"Detector detenido por error: {exc}")
            messagebox.showerror("Detector detenido", str(exc))
            return
        self.job = self.root.after(CAPTURE_INTERVAL_MS, self.tick)

    def update_preview(self, preview: Image.Image) -> None:
        self.preview_photo = ImageTk.PhotoImage(preview)
        self.preview_panel.configure(image=self.preview_photo)

    def show_signal(self, detection: Detection | None, confirmed: bool) -> None:
        if detection is None:
            self.signal_text.set("SIN SENAL ACTUAL")
            self.signal_panel.configure(fg="gray")
            return
        action = "COMPRA / CALL" if detection.signal == "CALL" else "VENTA / PUT"
        qualifier = "visible" if confirmed else "provisional"
        self.signal_text.set(f"{action} ({qualifier})\nEjecucion segun modo demo")
        self.signal_panel.configure(
            fg="#00C853" if detection.signal == "CALL" else "#FF1744",
        )

    def schedule_results(self) -> None:
        pending = self.ledger.pending()
        if self.result_job is None and pending is not None:
            delay = 5000
            if pending["deadline"] is not None:
                delay = max(delay, math.ceil((pending["deadline"] - time.time()) * 1000))
            self.result_job = self.root.after(delay, self.refresh_results)

    def refresh_results(self) -> None:
        if self.result_job is not None:
            self.root.after_cancel(self.result_job)
            self.result_job = None
        try:
            result_recorded = self.execution.refresh()
            overlay_status = ""
            if result_recorded:
                try:
                    if self.region is None:
                        raise RuntimeError("No hay un area de grafico seleccionada.")
                    screenshot = ImageGrab.grab(bbox=self.region, all_screens=True)
                    point = chart_background_click_point(screenshot, self.region)
                    if point is None:
                        raise RuntimeError(
                            "No se encontro un punto de fondo seguro dentro del area del grafico."
                        )
                    if point_over_window(point, self.root):
                        raise RuntimeError(
                            "El punto seguro coincide con la ventana superior del bot; "
                            "mueve el panel fuera del grafico."
                        )
                    click_screen(point)
                    overlay_status = " Velo del grafico limpiado."
                    LOGGER.info("Velo del grafico limpiado tras registrar el resultado.")
                except (OSError, RuntimeError, ValueError) as exc:
                    overlay_status = f" No se limpio el velo: {exc}"
                    LOGGER.warning("Resultado registrado; limpieza del velo omitida: %s", exc)
            LOGGER.info(
                "Resultados consultados: demo_armada=%s perdidas=%s",
                self.execution.armed, self.execution.session_losses,
            )
            self.summary_text.set(self.statistics_summary())
            if self.ledger.pending() is not None:
                self.order_status.set("Esperando vencimiento/resultado demo. Sin nuevas entradas.")
                self.schedule_results()
            else:
                self.order_status.set(
                    "DEMO DESARMADA: 3 perdidas totales en esta activacion."
                    if self.execution.session_losses >= 3 else
                    f"Resultados conciliados. Perdidas: {self.execution.session_losses}/3. "
                    + ("DEMO ARMADA." if self.execution.armed else "DEMO DESARMADA.")
                    + overlay_status
                )
        except (BrowserError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            LOGGER.exception("Lectura de resultados fallida; demo desarmada")
            self.execution.armed = False
            self.order_status.set(f"DEMO BLOQUEADA: {exc}")
            self.summary_text.set(self.statistics_summary())
            # Consultar de nuevo es seguro: nunca vuelve a pulsar una orden.
            self.schedule_results()

    def reset_statistics(self) -> None:
        if not messagebox.askyesno(
            "Reiniciar estadisticas",
            "Poner a cero las estadisticas desde ahora? El historial y las "
            "operaciones pendientes o inciertas se conservaran. No se "
            "reiniciara el limite de seguridad de perdidas ni se armara el bot.",
        ):
            return
        try:
            self.ledger.reset_statistics()
            self.summary_text.set(self.statistics_summary())
            LOGGER.info(
                "Estadisticas reiniciadas; historial conservado; demo_armada=%s perdidas_activacion=%s",
                self.execution.armed, self.execution.session_losses,
            )
        except (OSError, RuntimeError, sqlite3.Error, ValueError):
            LOGGER.exception("No se pudieron reiniciar las estadisticas")
            messagebox.showerror(
                "No se pudieron reiniciar las estadisticas",
                "El registro no cambio. Revisa el diagnostico local.",
            )

    def stop(self) -> None:
        LOGGER.info("DETENER: detector detenido y ejecucion desarmada")
        if self.job is not None:
            self.root.after_cancel(self.job)
            self.job = None
        self.gate = None
        self.visual_reference = None
        self.ema_analysis = None
        self.ema_result = None
        if hasattr(self, "ema_status"):
            self.ema_status.set("EMA: detenido.")
        self.last_visual_state = None
        self.last_detection_report = None
        self.execution.armed = False
        self.order_status.set("Ejecucion DESARMADA. No cancela posiciones enviadas.")
        self.signal_text.set("DETECTOR DETENIDO")
        self.signal_panel.configure(fg="gray")
        self.status.set("Detector detenido; ejecucion demo desarmada.")

    def close(self) -> None:
        self.stop()
        if self.result_job is not None:
            self.root.after_cancel(self.result_job)
        try:
            self.browser.close()
        finally:
            self.ledger.close()
            self.root.destroy()


def main() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Esta version de captura y calibracion requiere Windows.")
    # Unificar coordenadas de captura y Tk en pantallas con escalado DPI.
    enable_physical_coordinates()
    handler = configure_diagnostics(Path(__file__).resolve().parent / "diagnostico_bot.log")
    try:
        LOGGER.info("Inicio del bot")
        root = tk.Tk()
        app = App(root)
        root.after_idle(app.open_iq_option)
        root.mainloop()
    finally:
        LOGGER.info("Fin del bot")
        LOGGER.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    main()