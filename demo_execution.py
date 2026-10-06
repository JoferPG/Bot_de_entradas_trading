"""Ejecucion SOLO demo mediante interfaz web, sin API.

No llamar submit() en pruebas contra una cuenta. Probar con adaptadores mock.
El perfil del navegador guarda sesion local: no compartirlo ni subirlo a Git.
Un clic incierto bloquea nuevas ordenes, incluso despues de reiniciar.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from playwright.sync_api import (
    BrowserContext, Error as BrowserError, Page, Playwright, sync_playwright,
)

LOGGER = logging.getLogger("iq_option_bot")
MAX_SIGNAL_AGE_SECONDS = 6

Direction = Literal["CALL", "PUT"]
URL = "https://iqoption.com/pwa/traderoom"
PORTFOLIO_TIMEOUT_MS = 15000
COLOMBIA = timezone(timedelta(hours=-5))


def candle_close(observed_at: float, period: int = 300) -> tuple[float, str]:
    if period not in (60, 300):
        raise ValueError("Solo se permiten velas de 1 o 5 minutos.")
    if not math.isfinite(observed_at):
        raise ValueError("Hora de senal invalida.")
    deadline = (math.floor(observed_at / period) + 1) * period
    return deadline, datetime.fromtimestamp(deadline, COLOMBIA).strftime("%H:%M")


def money(text: str) -> Decimal:
    """Solo moneda dolar y formato ingles observado; rechazar ambiguedades."""
    compact = re.sub(r"\s+", "", text)
    if not re.fullmatch(r"\+?\$?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?", compact):
        raise ValueError(f"Importe no interpretable: {text!r}")
    return Decimal(compact.replace("+", "").replace("$", "").replace(",", ""))


@dataclass(frozen=True)
class Settings:
    asset: str
    stake: Decimal
    expiry: str
    period: int = 300

    def same_parameters(self, other: Settings) -> bool:
        return (self.asset == other.asset and self.stake == other.stake
                and self.period == other.period)


@dataclass(frozen=True)
class Position:
    identity: str
    direction: Direction
    asset: str
    stake: Decimal
    opened: str
    closed: bool
    returned: Decimal | None
    detail_url: str


class Ledger:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS demo_orders (
                id INTEGER PRIMARY KEY, created REAL NOT NULL,
                direction TEXT NOT NULL, asset TEXT NOT NULL,
                stake TEXT NOT NULL, expiry TEXT NOT NULL,
                status TEXT NOT NULL, baseline TEXT NOT NULL,
                position_id TEXT UNIQUE, detail_url TEXT,
                returned TEXT, error TEXT, signal_key TEXT UNIQUE
            )
        """)
        self.db.commit()
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(demo_orders)")}
        if "deadline" not in columns:
            self.db.execute("ALTER TABLE demo_orders ADD COLUMN deadline REAL")
            self.db.commit()
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS statistics_state "
            "(id INTEGER PRIMARY KEY CHECK(id=1), reset_at REAL NOT NULL)"
        )
        self.db.execute(
            "INSERT OR IGNORE INTO statistics_state(id, reset_at) VALUES(1, 0)"
        )
        self.db.commit()

    def pending(self) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM demo_orders WHERE status IN ('REQUESTED','UNKNOWN','OPEN') "
            "ORDER BY id LIMIT 1"
        ).fetchone()

    def reserve(
        self, direction: Direction, settings: Settings, baseline: set[str],
        signal_key: str,
        deadline: float | None = None,
    ) -> int:
        if self.pending() is not None:
            raise RuntimeError("Existe una solicitud pendiente o incierta. No se enviara otra.")
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO demo_orders(created,direction,asset,stake,expiry,status,baseline,signal_key,deadline) "
                "VALUES(?,?,?,?,?,'REQUESTED',?,?,?)",
                (time.time(), direction, settings.asset, str(settings.stake),
                 settings.expiry, json.dumps(sorted(baseline)), signal_key, deadline),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("No se pudo registrar la solicitud antes del clic.")
        return cursor.lastrowid

    def mark(self, identity: int, status: str, error: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE demo_orders SET status=?,error=? WHERE id=?",
                (status, error, identity),
            )

    def reconcile(self, positions: list[Position]) -> bool:
        row = self.pending()
        if row is None:
            return False
        if row["position_id"]:
            matches = [p for p in positions if p.identity == row["position_id"]]
        else:
            baseline = set(json.loads(row["baseline"]))
            matches = [
                p for p in positions
                if p.identity not in baseline and p.direction == row["direction"]
                and p.asset == row["asset"] and p.stake == Decimal(row["stake"])
            ]
            if row["deadline"] is not None:
                recent: list[Position] = []
                for position in matches:
                    match = re.search(r"\d{2}\.\d{2}\.\d{4},\s*\d{2}:\d{2}:\d{2}", position.opened)
                    if match is None:
                        raise ValueError("Hora de apertura ilegible: no se asociara una operacion antigua.")
                    opened = datetime.strptime(match.group(), "%d.%m.%Y, %H:%M:%S").replace(
                        tzinfo=COLOMBIA,
                    ).timestamp()
                    if row["created"] - 2 <= opened <= min(row["deadline"], row["created"] + 15):
                        recent.append(position)
                matches = recent
        if len(matches) != 1:
            raise RuntimeError(
                "No hay una posicion nueva unica que confirme la solicitud. "
                "Bloqueada: revisa el historial; no se reintentara el clic."
            )
        position = matches[0]
        if position.closed and position.returned is None:
            raise ValueError("Posicion cerrada sin devolucion legible.")
        with self.db:
            self.db.execute(
                "UPDATE demo_orders SET position_id=?,detail_url=?,status=?,returned=?,"
                "error=NULL WHERE id=?",
                (position.identity, position.detail_url,
                 "CLOSED" if position.closed else "OPEN",
                 str(position.returned) if position.returned is not None else None, row["id"]),
            )
        return True

    def is_closed_loss(self, identity: int) -> bool:
        row = self.db.execute(
            "SELECT status,stake,returned FROM demo_orders WHERE id=?", (identity,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Solicitud no encontrada en el registro.")
        if row["status"] != "CLOSED":
            return False
        if row["returned"] is None:
            raise ValueError("Posicion cerrada sin devolucion legible.")
        return Decimal(row["returned"]) < Decimal(row["stake"])

    def reset_statistics(self, reset_at: float | None = None) -> None:
        timestamp = time.time() if reset_at is None else reset_at
        if not math.isfinite(timestamp):
            raise ValueError("Fecha invalida para reiniciar estadisticas.")
        with self.db:
            self.db.execute(
                "UPDATE statistics_state SET reset_at=? WHERE id=1",
                (timestamp,),
            )

    def summary(self) -> str:
        reset_at = self.db.execute(
            "SELECT reset_at FROM statistics_state WHERE id=1",
        ).fetchone()["reset_at"]
        rows = self.db.execute(
            "SELECT * FROM demo_orders WHERE created >= ? AND status='CLOSED' ORDER BY id",
            (reset_at,),
        ).fetchall()
        wins = [
            row for row in rows
            if row["returned"] is not None and Decimal(row["returned"]) > Decimal(row["stake"])
        ]
        losses = [
            row for row in rows
            if row["returned"] is not None and Decimal(row["returned"]) < Decimal(row["stake"])
        ]
        invested = sum((Decimal(row["stake"]) for row in rows), Decimal(0))
        income = sum(
            (Decimal(row["returned"]) for row in rows if row["returned"] is not None),
            Decimal(0),
        )
        losses_amount = sum(
            (
                Decimal(row["stake"]) - Decimal(row["returned"])
                for row in losses if row["returned"] is not None
            ),
            Decimal(0),
        )
        return "\n".join((
            f"Ganadas: {len(wins)}",
            f"Perdidas: {len(losses)}",
            f"Neto invertido: ${invested:.2f}",
            f"Ingresos: ${income:.2f}",
            f"Perdidas: ${losses_amount:.2f}",
        ))

    def close(self) -> None:
        self.db.close()


class DemoBrowser:
    def __init__(self) -> None:
        self.runtime: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.reader: Page | None = None

    def open(self, profile: Path) -> None:
        if self.page is not None and not self.page.is_closed():
            self.page.bring_to_front()
            return
        self.runtime = sync_playwright().start()
        try:
            self.context = self.runtime.chromium.launch_persistent_context(
                str(profile), channel="msedge", headless=False, no_viewport=True,
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(1500)
            self.reader = self.context.new_page()
            self.reader.set_default_timeout(1500)
            self.page.goto(URL, wait_until="domcontentloaded", timeout=30000)
            self.page.bring_to_front()
        except (BrowserError, OSError, RuntimeError):
            # Liberar solo los recursos creados y propagar el error al panel.
            self.close()
            raise

    @staticmethod
    def require_demo(page: Page) -> None:
        parsed = urlparse(page.url)
        if parsed.scheme != "https" or parsed.hostname != "iqoption.com":
            raise RuntimeError("Dominio de IQ Option no verificado.")
        # Cabecera visible, no el texto demo dentro del menu de cuentas.
        header = page.get_by_test_id("balanceAmount-Wrapper").filter(visible=True)
        if header.count() != 1:
            raise RuntimeError("Cierra el menu de cuentas: cabecera demo ambigua.")
        container = header.locator("xpath=../..")
        text = container.inner_text()
        if "Cuenta demo" not in text or "Cuenta real" in text:
            raise RuntimeError("No se confirma Cuenta demo en la cabecera. Orden bloqueada.")
        if "$" not in header.inner_text():
            raise RuntimeError("Esta version de contabilidad solo admite la demo en dolares ($).")

    def settings(self) -> Settings:
        page = self.page
        if page is None or page.is_closed():
            raise RuntimeError("Abre el navegador dedicado y selecciona cuenta demo.")
        self.require_demo(page)
        if urlparse(page.url).path != "/pwa/traderoom":
            raise RuntimeError("Cierra la cartera y vuelve al grafico antes de armar.")
        amount = page.get_by_test_id("amountSelector").inner_text()
        match = re.fullmatch(r"Cantidad\s*\(\$\)\s*([\d,.]+)", amount.strip())
        if match is None:
            raise ValueError("No se pudo leer Cantidad ($); no se usara un importe por defecto.")
        stake = money(match.group(1))
        if stake <= 0:
            raise ValueError("El importe configurado debe ser positivo.")
        expiry = page.get_by_test_id("expirationSelector").inner_text().strip()
        if not re.search(r"\b\d{1,2}:\d{2}\b", expiry):
            raise ValueError("No se pudo leer el vencimiento seleccionado.")
        inspector = page.get_by_test_id("positions-inspector")
        asset = " ".join(inspector.inner_text().split())
        if "Binaria" not in asset:
            raise RuntimeError("Solo se permite el instrumento Binaria observado.")
        timeframe = page.get_by_role("toolbar").get_by_role(
            "button", name=re.compile(r"^(1m|5m)$"),
        )
        if timeframe.count() != 1 or not timeframe.is_visible():
            raise RuntimeError("Selecciona un solo grafico con temporalidad 1m o 5m.")
        label = timeframe.inner_text().strip()
        if label not in ("1m", "5m"):
            raise RuntimeError("Temporalidad del grafico no interpretable.")
        period = 60 if label == "1m" else 300
        for name in ("Sube", "Baja"):
            button = page.get_by_role("button", name=name, exact=True)
            if button.count() != 1 or not button.is_visible() or not button.is_enabled():
                raise RuntimeError(f"Boton {name} no disponible o ambiguo.")
        return Settings(asset, stake, expiry, period)

    def prepare_expiry(
        self, expected: Settings, observed_at: float, current: Settings | None = None,
    ) -> Settings:
        deadline, target = candle_close(observed_at, expected.period)
        page = self.page
        if page is None:
            raise RuntimeError("Navegador cerrado.")
        if current is None:
            current = self.settings()
        if not current.same_parameters(expected):
            raise RuntimeError("Cambio de activo o importe: desarma y revisa.")
        selector = page.get_by_test_id("expirationSelector")
        selected = selector.get_by_test_id("content").inner_text().strip()
        adjusted = selected != target
        if adjusted:
            selector.get_by_test_id("content").click(timeout=1500)
            dialog = page.get_by_role("dialog").filter(visible=True)
            try:
                dialog.wait_for(state="visible", timeout=1500)
                option = dialog.get_by_text(target, exact=True)
                if option.count() != 1 or not option.is_visible():
                    raise RuntimeError(
                        f"Vencimiento {target} Colombia no disponible. "
                        "No se elegira otra vela ni se enviara una orden."
                    )
                option.click(timeout=1500)
                dialog.wait_for(state="hidden", timeout=1500)
            finally:
                if dialog.count() == 1 and dialog.is_visible():
                    dialog.get_by_role("button").click(timeout=1500)
        if adjusted:
            current = self.settings()
            if not current.same_parameters(expected):
                raise RuntimeError("Cambio de activo o importe durante el ajuste de tiempo.")
        if selector.get_by_test_id("content").inner_text().strip() != target:
            raise RuntimeError("La web no confirma el vencimiento solicitado.")
        now = time.time()
        if now >= deadline:
            raise RuntimeError("La vela cerro durante la preparacion: no se enviara la orden.")
        if not 0 <= now - observed_at <= MAX_SIGNAL_AGE_SECONDS:
            raise RuntimeError(
                f"Senal antigua tras preparar vencimiento ({now - observed_at:.2f}s; "
                f"limite {MAX_SIGNAL_AGE_SECONDS}s): no se enviara la orden."
            )
        return current

    @staticmethod
    def read_position(page: Page, asset: str) -> Position:
        identity = page.get_by_test_id("positionId-value").inner_text().strip()
        if not identity.isdigit():
            raise ValueError("ID de posicion no interpretable.")
        direction_text = page.get_by_test_id("direction-value").inner_text().strip()
        if direction_text not in ("Sube", "Baja"):
            raise ValueError("Direccion de posicion desconocida.")
        stake = money(page.get_by_test_id("investment-amount").inner_text())
        detail = page.locator('[data-testid^="position-"][data-testid$="-details"]')
        if detail.count() != 1:
            raise RuntimeError("Detalle de posicion ambiguo.")
        closed = detail.get_by_text("Método cerrado", exact=True).count() == 1
        returned = money(page.get_by_test_id("MoneyAmount-profit-amount").inner_text()) if closed else None
        opened = detail.get_by_text("Hora de apertura", exact=True).locator("xpath=..").inner_text()
        return Position(
            identity, "CALL" if direction_text == "Sube" else "PUT",
            asset,
            stake, opened, closed, returned, page.url,
        )

    def load_portfolio(self, reader: Page) -> None:
        reader.goto("https://iqoption.com/pwa/trades", wait_until="domcontentloaded",
                    timeout=PORTFOLIO_TIMEOUT_MS)
        for name in ("Activas", "Cerradas"):
            reader.get_by_role("heading", name=name, exact=True).wait_for(
                state="visible", timeout=PORTFOLIO_TIMEOUT_MS,
            )
        self.require_demo(reader)

    def positions(self) -> list[Position]:
        reader, page = self.reader, self.page
        if reader is None or page is None:
            raise RuntimeError("Navegador dedicado no iniciado.")
        self.require_demo(page)
        if reader.is_closed():
            if self.context is None:
                raise RuntimeError("Navegador dedicado no disponible para abrir la cartera.")
            reader = self.context.new_page()
            reader.set_default_timeout(1500)
            self.reader = reader
        positions: list[Position] = []
        try:
            reader.bring_to_front()
            self.load_portfolio(reader)
            cards = reader.get_by_role("button").filter(
                has=reader.get_by_test_id("investment-amount"),
            )
            # No interpretar una cartera todavia cargando como historial vacio.
            cards.first.wait_for(state="visible", timeout=PORTFOLIO_TIMEOUT_MS)
            count = cards.count()
            for index in range(count):
                if index:
                    self.load_portfolio(reader)
                cards = reader.get_by_role("button").filter(
                    has=reader.get_by_test_id("investment-amount"),
                )
                cards.nth(index).wait_for(state="visible", timeout=PORTFOLIO_TIMEOUT_MS)
                card = cards.nth(index)
                asset = " ".join(card.get_by_test_id("assetInfo").inner_text().split())
                card.click()
                reader.get_by_test_id("positionId-value").wait_for(
                    state="visible", timeout=PORTFOLIO_TIMEOUT_MS,
                )
                self.require_demo(reader)
                positions.append(self.read_position(reader, asset))
        except BrowserError as exc:
            if reader.is_closed():
                raise RuntimeError(
                    "Se cerro la pestana de cartera durante la validacion. "
                    "No se habilitaron nuevas ordenes. No cierres esa pestana: "
                    "el bot vuelve al grafico al terminar. Pulsa INICIAR de nuevo "
                    "para crearla y repetir la comprobacion."
                ) from exc
            raise RuntimeError(
                "No se pudo leer la cartera demo. Se esperan hasta 15 segundos "
                "por elemento. Verifica la sesion, las secciones Activas/Cerradas "
                "y el historial visible en la pestana de cartera. "
                f"No se habilitaran nuevas ordenes. Detalle: {exc}"
            ) from exc
        finally:
            page.bring_to_front()
        return positions

    def click_once(self, direction: Direction, settings: Settings) -> None:
        if direction not in ("CALL", "PUT"):
            raise ValueError("Direccion no permitida.")
        if self.settings() != settings:
            raise RuntimeError("Cambio de activo, importe o vencimiento: desarma y revisa.")
        page = self.page
        if page is None:
            raise RuntimeError("Navegador cerrado.")
        self.require_demo(page)
        selected = page.get_by_test_id("expirationSelector").get_by_test_id(
            "content",
        ).inner_text().strip()
        now = time.time()
        deadline, target = candle_close(now, settings.period)
        if deadline - now <= 2:
            raise RuntimeError("Quedan menos de 2 segundos para el cierre: entrada bloqueada.")
        if selected != target:
            raise RuntimeError("La vela cambio o el vencimiento no coincide con su cierre.")
        page.get_by_role(
            "button", name="Sube" if direction == "CALL" else "Baja", exact=True,
        ).click(timeout=1500)

    def close(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            if self.runtime is not None:
                self.runtime.stop()
            self.context = None
            self.runtime = None
            self.page = None
            self.reader = None


class DemoExecution:
    def __init__(self, browser: DemoBrowser, ledger: Ledger) -> None:
        self.browser = browser
        self.ledger = ledger
        self.armed = False
        self.settings: Settings | None = None
        self.baseline: set[str] = set()
        self.session_losses = 0

    def arm(self, period: int = 300) -> Settings:
        self.armed = False
        if self.ledger.pending() is not None:
            raise RuntimeError("Resuelve primero la solicitud pendiente del registro.")
        settings = self.browser.settings()
        if period not in (60, 300) or settings.period != period:
            raise RuntimeError("La temporalidad del grafico no coincide con la seleccionada en el bot.")
        if not self.browser.settings().same_parameters(settings):
            raise RuntimeError("La configuracion cambio durante la comprobacion.")
        self.settings = settings
        self.baseline = set()
        self.session_losses = 0
        self.armed = True
        return settings

    def submit(self, direction: Direction, observed_at: float, period: int) -> int:
        if not self.armed or self.settings is None:
            raise RuntimeError("Ejecucion demo no armada.")
        if not math.isfinite(observed_at) or not 0 <= time.time() - observed_at <= MAX_SIGNAL_AGE_SECONDS:
            raise RuntimeError("Senal demasiado antigua: no se enviara la orden.")
        started = time.perf_counter()
        try:
            current = self.browser.settings()
        finally:
            LOGGER.info("Latencia lectura configuracion: %.3fs", time.perf_counter() - started)
        if not current.same_parameters(self.settings):
            self.armed = False
            changes = []
            if current.asset != self.settings.asset:
                changes.append(f"activo: {self.settings.asset!r} -> {current.asset!r}")
            if current.stake != self.settings.stake:
                changes.append(f"importe: {self.settings.stake} -> {current.stake}")
            if current.period != self.settings.period:
                changes.append(f"temporalidad: {self.settings.period}s -> {current.period}s")
            raise RuntimeError(
                "Configuracion modificada: ejecucion desarmada. " + "; ".join(changes)
            )
        if direction not in ("CALL", "PUT") or period not in (60, 300) or period != self.settings.period:
            raise ValueError("Senal o periodo invalido.")
        try:
            started = time.perf_counter()
            try:
                prepared = self.browser.prepare_expiry(self.settings, observed_at, current=current)
            finally:
                LOGGER.info("Latencia preparar vencimiento: %.3fs", time.perf_counter() - started)
        except (BrowserError, OSError, RuntimeError, ValueError):
            self.armed = False
            raise
        key = f"{self.settings.asset}|{period}|{int(observed_at // period)}"
        deadline, _ = candle_close(observed_at, period)
        identity = self.ledger.reserve(direction, prepared, self.baseline, key, deadline)
        try:
            started = time.perf_counter()
            try:
                self.browser.click_once(direction, prepared)
            finally:
                LOGGER.info("Latencia validacion final y clic: %.3fs", time.perf_counter() - started)
        except (BrowserError, OSError, RuntimeError, ValueError) as exc:
            self.armed = False
            self.ledger.mark(identity, "UNKNOWN", str(exc))
            raise RuntimeError(
                "Clic incierto: no se reintentara. Comprueba la cartera."
            ) from exc
        return identity

    def refresh(self) -> None:
        pending = self.ledger.pending()
        if pending is None:
            return
        if pending["deadline"] is not None and time.time() < pending["deadline"]:
            return
        positions = self.browser.positions()
        self.ledger.reconcile(positions)
        self.baseline.update(p.identity for p in positions)
        if self.ledger.is_closed_loss(pending["id"]):
            self.session_losses += 1
            if self.session_losses >= 3:
                self.armed = False
