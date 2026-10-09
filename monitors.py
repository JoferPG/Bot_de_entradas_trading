"""Coordenadas fisicas de monitores Windows, incluidos origenes negativos."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

Box = tuple[int, int, int, int]


@dataclass(frozen=True)
class Monitor:
    bounds: Box
    primary: bool

    @property
    def label(self) -> str:
        left, top, right, bottom = self.bounds
        return (
            f"{'Principal' if self.primary else 'Secundario'}: "
            f"{right - left}x{bottom - top} ({left}, {top})"
        )

    def to_screen(self, box: Box) -> Box:
        left, top, right, bottom = box
        x, y, end_x, end_y = self.bounds
        if not (0 <= left < right <= end_x - x and 0 <= top < bottom <= end_y - y):
            raise ValueError("Seleccion fuera del monitor.")
        return (left + x, top + y, right + x, bottom + y)


def enable_physical_coordinates() -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    function = user32.SetProcessDpiAwarenessContext
    function.argtypes = [wintypes.HANDLE]
    function.restype = wintypes.BOOL
    if not function(ctypes.c_void_p(-4)):
        raise ctypes.WinError(ctypes.get_last_error())


def click_screen(point: tuple[int, int]) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    set_cursor = user32.SetCursorPos
    set_cursor.argtypes = [ctypes.c_int, ctypes.c_int]
    set_cursor.restype = wintypes.BOOL
    x, y = point
    if not set_cursor(x, y):
        raise ctypes.WinError(ctypes.get_last_error())
    mouse_event = user32.mouse_event
    mouse_event.argtypes = [
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_size_t,
    ]
    mouse_event.restype = None
    mouse_event(0x0002, 0, 0, 0, 0)
    mouse_event(0x0004, 0, 0, 0, 0)


def list_monitors() -> list[Monitor]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class Info(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD), ("monitor", wintypes.RECT),
            ("work", wintypes.RECT), ("flags", wintypes.DWORD),
        ]

    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
    )
    info_function = user32.GetMonitorInfoW
    info_function.argtypes = [wintypes.HANDLE, ctypes.POINTER(Info)]
    info_function.restype = wintypes.BOOL
    result: list[Monitor] = []
    errors: list[int] = []

    def visit(handle: int, _dc: int, _rect: object, _data: int) -> bool:
        info = Info()
        info.size = ctypes.sizeof(Info)
        if not info_function(handle, ctypes.byref(info)):
            errors.append(ctypes.get_last_error())
            return False
        rect = info.monitor
        result.append(Monitor(
            (rect.left, rect.top, rect.right, rect.bottom), bool(info.flags & 1),
        ))
        return True

    callback = callback_type(visit)
    enum = user32.EnumDisplayMonitors
    enum.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), callback_type, wintypes.LPARAM]
    enum.restype = wintypes.BOOL
    if not enum(None, None, callback, 0) or errors:
        raise ctypes.WinError(errors[0] if errors else ctypes.get_last_error())
    if not result:
        raise RuntimeError("Windows no informa monitores disponibles.")
    return sorted(result, key=lambda monitor: (not monitor.primary, monitor.bounds))


def place_selector(window_id: int, bounds: Box) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    ancestor = user32.GetAncestor
    ancestor.argtypes = [wintypes.HWND, wintypes.UINT]
    ancestor.restype = wintypes.HWND
    # Tk devuelve el HWND cliente; mover la ventana contenedora, no el hijo.
    handle = ancestor(window_id, 2)
    if not handle:
        raise RuntimeError("No se encontro la ventana de seleccion.")
    function = user32.SetWindowPos
    function.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    function.restype = wintypes.BOOL
    left, top, right, bottom = bounds
    if not function(handle, ctypes.c_void_p(-1), left, top, right - left, bottom - top, 0x0040):
        raise ctypes.WinError(ctypes.get_last_error())
