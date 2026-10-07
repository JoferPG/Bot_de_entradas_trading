import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from demo_execution import (
    PORTFOLIO_TIMEOUT_MS, DemoBrowser, DemoExecution, EntryWindowExpired, Ledger, Position, Settings,
    candle_close, money,
)
from playwright.sync_api import TimeoutError as BrowserTimeoutError


class DemoTests(unittest.TestCase):
    @patch("demo_execution.sync_playwright")
    def test_opens_dedicated_edge_without_firefox(self, factory):
        runtime = factory.return_value.start.return_value
        context = runtime.chromium.launch_persistent_context.return_value
        page = Mock()
        page.is_closed.return_value = False
        context.pages = [page]
        browser = DemoBrowser()
        profile = Path("edge-test-profile")
        browser.open(profile)
        runtime.chromium.launch_persistent_context.assert_called_once_with(
            str(profile), channel="msedge", headless=False, no_viewport=True,
        )
        runtime.firefox.launch_persistent_context.assert_not_called()
        page.goto.assert_called_once()
        browser.open(profile)
        self.assertEqual(runtime.chromium.launch_persistent_context.call_count, 1)
        browser.close()
        context.close.assert_called_once()
        runtime.stop.assert_called_once()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "demo.sqlite3"
        self.ledger = Ledger(self.path)
        self.browser = Mock(spec=DemoBrowser)
        self.settings = Settings("EUR/USD (OTC) Binaria", Decimal("20"), "Tiempo 23:05")
        self.browser.settings.return_value = self.settings
        self.browser.prepare_expiry.return_value = self.settings
        self.browser.positions.return_value = []
        self.execution = DemoExecution(self.browser, self.ledger)

    def tearDown(self):
        self.ledger.close()
        self.directory.cleanup()

    def position(self, identity="123", closed=False, returned=None, direction="CALL"):
        pending = self.ledger.pending()
        opened = datetime.fromtimestamp(
            pending["created"] if pending is not None else time.time(),
            timezone(timedelta(hours=-5)),
        ).strftime("%d.%m.%Y, %H:%M:%S")
        return Position(
            identity, direction, self.settings.asset, self.settings.stake,
            opened, closed, returned,
            "https://iqoption.com/pwa/trades/test",
        )

    def refresh_after_expiry(self):
        pending = self.ledger.pending()
        now = pending["deadline"] + 1 if pending is not None and pending["deadline"] else time.time()
        with patch("demo_execution.time.time", return_value=now):
            self.execution.refresh()

    def test_no_click_when_unarmed(self):
        with self.assertRaises(RuntimeError):
            self.execution.submit("CALL", time.time(), 300)
        self.browser.click_once.assert_not_called()

    def test_configuration_change_reports_field_without_reserving_or_clicking(self):
        for field, value, label in (
            ("asset", "GBP/USD Binaria", "activo:"),
            ("stake", Decimal("10"), "importe:"),
            ("period", 60, "temporalidad:"),
        ):
            with self.subTest(field=field):
                self.browser.settings.return_value = self.settings
                self.execution.arm()
                self.browser.settings.return_value = replace(self.settings, **{field: value})
                with self.assertRaisesRegex(RuntimeError, label):
                    self.execution.submit("PUT", time.time(), 300)
                self.assertFalse(self.execution.armed)
                self.assertIsNone(self.ledger.pending())
                self.browser.click_once.assert_not_called()

    def test_one_minute_entry_deadline_and_result_wait(self):
        settings = replace(self.settings, period=60)
        self.browser.settings.return_value = settings
        self.browser.prepare_expiry.return_value = settings
        self.execution.arm(60)
        now = datetime(2026, 10, 4, 21, 6, 20,
                       tzinfo=timezone(timedelta(hours=-5))).timestamp()
        with patch("demo_execution.time.time", return_value=now):
            self.execution.submit("PUT", now, 60)
            self.execution.refresh()
        self.browser.positions.assert_not_called()
        self.browser.click_once.assert_called_once_with("PUT", settings, observed_at=now)
        self.assertEqual(self.ledger.pending()["deadline"], now + 40)
        self.browser.positions.return_value = [self.position("minute", True, Decimal(0), "PUT")]
        self.refresh_after_expiry()
        self.assertEqual(self.execution.session_losses, 1)
        self.assertIsNone(self.ledger.pending())

    def test_timeframe_mismatch_blocks_activation(self):
        with self.assertRaisesRegex(RuntimeError, "temporalidad"):
            self.execution.arm(60)
        self.assertFalse(self.execution.armed)
        self.browser.click_once.assert_not_called()

    def test_reads_supported_timeframe_from_chart_toolbar(self):
        browser = DemoBrowser()
        browser.page = Mock()
        browser.page.url = "https://iqoption.com/pwa/traderoom"
        browser.page.is_closed.return_value = False
        texts = {
            "amountSelector": "Cantidad ($) 20",
            "expirationSelector": "Tiempo 21:10",
            "positions-inspector": self.settings.asset,
        }
        browser.page.get_by_test_id.side_effect = lambda name: Mock(
            inner_text=Mock(return_value=texts[name]),
        )
        buttons = browser.page.get_by_role.return_value
        buttons.count.return_value = 1
        buttons.is_visible.return_value = True
        buttons.is_enabled.return_value = True
        timeframe = buttons.get_by_role.return_value
        timeframe.count.return_value = 1
        timeframe.is_visible.return_value = True
        with patch.object(browser, "require_demo"):
            for label, period in (("1m", 60), ("5m", 300)):
                timeframe.inner_text.return_value = label
                self.assertEqual(browser.settings().period, period)
            timeframe.count.return_value = 0
            with self.assertRaisesRegex(RuntimeError, "temporalidad"):
                browser.settings()

    def test_changed_chart_timeframe_blocks_entry(self):
        self.execution.arm()
        self.browser.settings.return_value = replace(self.settings, period=60)
        with self.assertRaisesRegex(RuntimeError, "Configuracion modificada"):
            self.execution.submit("CALL", time.time(), 300)
        self.assertFalse(self.execution.armed)
        self.browser.prepare_expiry.assert_not_called()
        self.browser.click_once.assert_not_called()

    def test_one_minute_colombia_boundaries(self):
        for hour, minute, second, label in (
            (21, 6, 20, "21:07"), (21, 7, 0, "21:08"), (23, 59, 59, "00:00"),
        ):
            observed = datetime(2026, 10, 4, hour, minute, second,
                                tzinfo=timezone(timedelta(hours=-5))).timestamp()
            deadline, target = candle_close(observed, 60)
            self.assertEqual(target, label)
            self.assertTrue(0 < deadline - observed <= 60)
        with self.assertRaises(ValueError):
            candle_close(observed, 120)

    def test_recreates_closed_portfolio_tab_without_sending_orders(self):
        browser = DemoBrowser()
        browser.page = Mock()
        browser.reader = Mock()
        browser.reader.is_closed.return_value = True
        browser.context = Mock()
        replacement = browser.context.new_page.return_value
        replacement.is_closed.return_value = False
        replacement.get_by_role.return_value.filter.return_value.count.return_value = 0
        with patch.object(browser, "require_demo"), patch.object(browser, "load_portfolio"):
            self.assertEqual(browser.positions(), [])
        self.assertIs(browser.reader, replacement)
        browser.context.new_page.assert_called_once()
        replacement.set_default_timeout.assert_called_once_with(1500)
        browser.page.bring_to_front.assert_called_once()

    def test_tab_closed_during_validation_has_specific_error(self):
        browser = DemoBrowser()
        browser.page = Mock()
        browser.reader = Mock()
        browser.reader.is_closed.side_effect = [False, True]
        with patch.object(browser, "require_demo"), patch.object(
            browser, "load_portfolio", side_effect=BrowserTimeoutError("Target closed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Se cerro la pestana de cartera"):
                browser.positions()
        browser.page.bring_to_front.assert_called_once()

    def test_colombia_candle_boundaries_including_midnight(self):
        colombia = timezone(timedelta(hours=-5))
        for hour, minute, second, target in (
            (0, 16, 30, "00:20"), (0, 20, 0, "00:25"),
            (23, 59, 59, "00:00"), (0, 0, 0, "00:05"),
        ):
            observed = datetime(2026, 10, 4, hour, minute, second, tzinfo=colombia).timestamp()
            deadline, label = candle_close(observed)
            self.assertEqual(label, target)
            self.assertGreater(deadline, observed)
            self.assertLessEqual(deadline - observed, 300)

    def test_prepares_expiry_before_reserving_and_uses_selected_expiry(self):
        self.execution.arm()
        prepared = Settings(self.settings.asset, self.settings.stake, "Tiempo 00:20")
        self.browser.settings.return_value = Settings(
            self.settings.asset, self.settings.stake, "Tiempo 00:17",
        )
        def prepare(expected, observed, current=None):
            self.assertIsNone(self.ledger.pending())
            self.assertEqual(current, self.browser.settings.return_value)
            return prepared
        self.browser.prepare_expiry.side_effect = prepare
        self.execution.submit("CALL", time.time(), 300)
        self.browser.click_once.assert_called_once_with("CALL", prepared, observed_at=ANY)
        self.assertEqual(self.ledger.pending()["expiry"], prepared.expiry)

    def test_unavailable_expiry_blocks_without_reserving_or_clicking(self):
        self.execution.arm()
        self.browser.prepare_expiry.side_effect = RuntimeError("Vencimiento no disponible")
        with self.assertRaisesRegex(RuntimeError, "no disponible"):
            self.execution.submit("PUT", time.time(), 300)
        self.browser.click_once.assert_not_called()
        self.assertIsNone(self.ledger.pending())
        self.assertFalse(self.execution.armed)

    def test_rejects_other_periods_and_future_signals(self):
        self.execution.arm()
        with self.assertRaises(ValueError):
            self.execution.submit("CALL", time.time(), 60)
        with self.assertRaises(RuntimeError):
            self.execution.submit("CALL", time.time() + 10, 300)
        self.browser.prepare_expiry.assert_not_called()
        self.browser.click_once.assert_not_called()

    def test_selects_exact_expiry_and_verifies_it(self):
        browser = DemoBrowser()
        browser.page = Mock()
        selector = browser.page.get_by_test_id.return_value
        content = selector.get_by_test_id.return_value
        content.inner_text.side_effect = ["00:17", "00:20"]
        dialog = browser.page.get_by_role.return_value.filter.return_value
        option = dialog.get_by_text.return_value
        option.count.return_value = 1
        option.is_visible.return_value = True
        dialog.count.return_value = 0
        observed = datetime(2026, 10, 4, 0, 16, 30,
                            tzinfo=timezone(timedelta(hours=-5))).timestamp()
        prepared = Settings(self.settings.asset, self.settings.stake, "Tiempo 00:20")
        with patch.object(browser, "settings", side_effect=[self.settings, prepared]), patch(
            "demo_execution.time.time", return_value=observed + 1,
        ):
            self.assertEqual(browser.prepare_expiry(self.settings, observed), prepared)
        dialog.get_by_text.assert_called_once_with("00:20", exact=True)
        option.click.assert_called_once_with(timeout=1500)
        browser.page.get_by_role.assert_called_once_with("dialog")

    def test_missing_expiry_closes_picker_and_does_not_select_another(self):
        browser = DemoBrowser()
        browser.page = Mock()
        browser.page.get_by_test_id.return_value.get_by_test_id.return_value.inner_text.return_value = "00:17"
        dialog = browser.page.get_by_role.return_value.filter.return_value
        dialog.get_by_text.return_value.count.return_value = 0
        dialog.count.return_value = 1
        dialog.is_visible.return_value = True
        with patch.object(browser, "settings", return_value=self.settings):
            with self.assertRaisesRegex(RuntimeError, "no disponible"):
                browser.prepare_expiry(self.settings, time.time())
        dialog.get_by_text.return_value.click.assert_not_called()
        dialog.get_by_role.assert_called_once_with("button")
        dialog.get_by_role.return_value.click.assert_called_once_with(timeout=1500)

    def test_matching_expiry_avoids_redundant_settings_read_and_picker(self):
        browser = DemoBrowser()
        browser.page = Mock()
        observed = datetime(2026, 10, 4, 0, 16, 30,
                            tzinfo=timezone(timedelta(hours=-5))).timestamp()
        content = browser.page.get_by_test_id.return_value.get_by_test_id.return_value
        content.inner_text.return_value = "00:20"
        with patch.object(browser, "settings", return_value=self.settings) as settings, patch(
            "demo_execution.time.time", return_value=observed + 0.5,
        ):
            self.assertEqual(browser.prepare_expiry(self.settings, observed), self.settings)
        settings.assert_called_once()
        content.click.assert_not_called()
        browser.page.get_by_role.assert_not_called()

    def test_matching_expiry_reuses_configuration_from_submit(self):
        browser = DemoBrowser()
        browser.page = Mock()
        observed = datetime(2026, 10, 4, 0, 16, 30,
                            tzinfo=timezone(timedelta(hours=-5))).timestamp()
        content = browser.page.get_by_test_id.return_value.get_by_test_id.return_value
        content.inner_text.return_value = "00:20"
        with patch.object(browser, "settings") as read, patch(
            "demo_execution.time.time", return_value=observed + 0.5,
        ):
            self.assertEqual(
                browser.prepare_expiry(self.settings, observed, current=self.settings),
                self.settings,
            )
        read.assert_not_called()
        content.click.assert_not_called()

    def test_adjusted_expiry_rereads_configuration_even_when_supplied(self):
        browser = DemoBrowser()
        browser.page = Mock()
        observed = datetime(2026, 10, 4, 0, 16, 30,
                            tzinfo=timezone(timedelta(hours=-5))).timestamp()
        content = browser.page.get_by_test_id.return_value.get_by_test_id.return_value
        content.inner_text.side_effect = ["00:17", "00:20"]
        dialog = browser.page.get_by_role.return_value.filter.return_value
        dialog.get_by_text.return_value.count.return_value = 1
        dialog.get_by_text.return_value.is_visible.return_value = True
        dialog.count.return_value = 0
        changed = Settings(self.settings.asset, self.settings.stake + 1, "Tiempo 00:20")
        with patch.object(browser, "settings", return_value=changed) as read, patch(
            "demo_execution.time.time", return_value=observed + 0.5,
        ):
            with self.assertRaisesRegex(RuntimeError, "Cambio de activo o importe"):
                browser.prepare_expiry(self.settings, observed, current=self.settings)
        read.assert_called_once()

    def test_reused_configuration_still_rejects_delay_or_closed_candle(self):
        browser = DemoBrowser()
        browser.page = Mock()
        observed = datetime(2026, 10, 4, 0, 16, 30,
                            tzinfo=timezone(timedelta(hours=-5))).timestamp()
        browser.page.get_by_test_id.return_value.get_by_test_id.return_value.inner_text.return_value = "00:20"
        for delay, message in ((6.01, "Senal antigua"), (211, "vela cerro")):
            with self.subTest(delay=delay), patch(
                "demo_execution.time.time", return_value=observed + delay,
            ):
                with self.assertRaisesRegex(RuntimeError, message):
                    browser.prepare_expiry(self.settings, observed, current=self.settings)

    def test_signal_age_six_seconds_boundary_in_submit_and_preparation(self):
        observed = datetime(2026, 10, 4, 0, 16, 30,
                            tzinfo=timezone(timedelta(hours=-5))).timestamp()
        browser = DemoBrowser()
        browser.page = Mock()
        browser.page.get_by_test_id.return_value.get_by_test_id.return_value.inner_text.return_value = "00:20"
        for delay in (3.65, 6.0, 6.01):
            with self.subTest(delay=delay), patch(
                "demo_execution.time.time", return_value=observed + delay,
            ), patch.object(self.ledger, "reserve", return_value=1):
                self.execution.arm()
                self.browser.click_once.reset_mock()
                if delay <= 6:
                    self.assertEqual(self.execution.submit("PUT", observed, 300), 1)
                    self.browser.click_once.assert_called_once()
                    self.assertEqual(
                        browser.prepare_expiry(self.settings, observed, current=self.settings),
                        self.settings,
                    )
                else:
                    with self.assertRaisesRegex(RuntimeError, "antigua"):
                        self.execution.submit("PUT", observed, 300)
                    self.browser.click_once.assert_not_called()
                    with self.assertRaisesRegex(RuntimeError, "limite 6s"):
                        browser.prepare_expiry(self.settings, observed, current=self.settings)

    def test_blocks_click_at_candle_boundary(self):
        browser = DemoBrowser()
        browser.page = Mock()
        observed = datetime(2026, 10, 4, 0, 19, 59,
                            tzinfo=timezone(timedelta(hours=-5))).timestamp()
        with patch.object(browser, "settings", return_value=self.settings), patch.object(
            browser, "require_demo",
        ), patch("demo_execution.time.time", return_value=observed):
            with self.assertRaisesRegex(RuntimeError, "2 segundos"):
                browser.click_once("CALL", self.settings)
        browser.page.get_by_role.assert_not_called()

    def test_portfolio_waits_longer_and_restores_chart_after_reading(self):
        browser = DemoBrowser()
        browser.page = Mock()
        browser.reader = Mock()
        browser.reader.is_closed.return_value = False
        cards = browser.reader.get_by_role.return_value.filter.return_value
        cards.count.return_value = 2
        cards.nth.return_value.get_by_test_id.return_value.inner_text.return_value = self.settings.asset
        with patch.object(browser, "require_demo"), patch.object(
            browser, "read_position", return_value=self.position("old", True, Decimal(0)),
        ):
            self.assertEqual(len(browser.positions()), 2)
        browser.reader.bring_to_front.assert_called_once()
        browser.page.bring_to_front.assert_called_once()
        self.assertEqual(browser.reader.goto.call_count, 2)
        heading = browser.reader.get_by_role.return_value
        self.assertEqual(heading.wait_for.call_count, 4)
        heading.wait_for.assert_called_with(state="visible", timeout=PORTFOLIO_TIMEOUT_MS)
        cards.first.wait_for.assert_called_once_with(
            state="visible", timeout=PORTFOLIO_TIMEOUT_MS,
        )
        browser.reader.get_by_test_id.return_value.wait_for.assert_called_with(
            state="visible", timeout=PORTFOLIO_TIMEOUT_MS,
        )
        heading.click.assert_not_called()

    def test_portfolio_timeout_after_expiry_keeps_request_pending(self):
        browser = DemoBrowser()
        browser.page = Mock()
        browser.reader = Mock()
        browser.reader.is_closed.return_value = False
        browser.reader.get_by_role.return_value.wait_for.side_effect = BrowserTimeoutError(
            'waiting for get_by_role("heading", name="Activas", exact=True)',
        )
        execution = DemoExecution(browser, self.ledger)
        self.ledger.reserve("CALL", self.settings, set(), "timeout")
        with patch.object(browser, "require_demo"), patch.object(
            browser, "settings", return_value=self.settings,
        ), patch.object(browser, "click_once") as click:
            with self.assertRaisesRegex(RuntimeError, "No se pudo leer la cartera"):
                execution.refresh()
            click.assert_not_called()
        self.assertFalse(execution.armed)
        self.assertIsNotNone(self.ledger.pending())
        browser.page.bring_to_front.assert_called_once()

    def test_no_portfolio_before_expiry_even_after_restart(self):
        self.execution.arm()
        now = time.time()
        self.execution.submit("CALL", now, 300)
        deadline = self.ledger.pending()["deadline"]
        restarted = DemoExecution(self.browser, self.ledger)
        with patch("demo_execution.time.time", return_value=deadline - 1):
            restarted.refresh()
        self.browser.positions.assert_not_called()
        self.assertEqual(self.ledger.pending()["status"], "REQUESTED")

    def test_old_matching_trade_is_not_associated_without_baseline(self):
        self.execution.arm()
        self.execution.submit("CALL", time.time(), 300)
        old = Position("old", "CALL", self.settings.asset, self.settings.stake,
                       "03.10.2026, 23:04:00", True, Decimal(0), "test")
        self.browser.positions.return_value = [old]
        with self.assertRaisesRegex(RuntimeError, "unica"):
            self.refresh_after_expiry()
        self.assertEqual(self.ledger.pending()["status"], "REQUESTED")

    def test_one_click_then_blocked_until_confirmed_closure(self):
        self.execution.arm()
        self.execution.submit("CALL", time.time(), 300)
        self.browser.click_once.assert_called_once_with("CALL", self.settings, observed_at=ANY)
        with self.assertRaises(RuntimeError):
            self.execution.submit("PUT", time.time(), 300)
        self.browser.positions.return_value = [self.position()]
        self.refresh_after_expiry()
        self.assertEqual(self.ledger.pending()["status"], "OPEN")
        self.browser.positions.return_value = [self.position(closed=True, returned=Decimal("37.40"))]
        self.refresh_after_expiry()
        self.assertIsNone(self.ledger.pending())
        self.assertIn("Ganadas: 1", self.ledger.summary())
        self.assertIn("Perdidas: 0", self.ledger.summary())
        self.assertIn("Neto invertido: $20.00", self.ledger.summary())
        self.assertIn("Ingresos: $37.40", self.ledger.summary())
        self.assertIn("Perdidas: $0.00", self.ledger.summary())

    def test_click_failure_is_persistent_unknown_and_never_retried(self):
        self.execution.arm()
        self.browser.click_once.side_effect = RuntimeError("Timeout")
        with self.assertRaisesRegex(RuntimeError, "incierto"):
            self.execution.submit("PUT", time.time(), 300)
        self.assertFalse(self.execution.armed)
        self.assertEqual(self.ledger.pending()["status"], "UNKNOWN")
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.execution = DemoExecution(self.browser, self.ledger)
        with self.assertRaises(RuntimeError):
            self.execution.arm()
        self.browser.click_once.assert_called_once()

    def test_demo_verification_failure_prevents_click(self):
        self.browser.settings.side_effect = RuntimeError("Cuenta real")
        with self.assertRaises(RuntimeError):
            self.execution.arm()
        self.browser.click_once.assert_not_called()

    def test_changed_settings_and_stale_signal_prevent_click(self):
        self.execution.arm()
        with self.assertRaises(RuntimeError):
            self.execution.submit("CALL", time.time() - 7, 300)
        self.browser.settings.return_value = Settings("different", Decimal("1"), "23:00")
        with self.assertRaises(RuntimeError):
            self.execution.submit("CALL", time.time(), 300)
        self.browser.click_once.assert_not_called()

    def test_expired_signal_counts_once_and_allows_a_later_entry(self):
        observed = 1_800_000_002.0
        self.execution.arm()
        with patch("demo_execution.time.time", return_value=observed + 6.01):
            with self.assertRaises(EntryWindowExpired):
                self.execution.submit("CALL", observed, 300)
        self.assertTrue(self.execution.armed)
        self.assertIsNone(self.ledger.pending())
        self.browser.click_once.assert_not_called()
        self.assertIn("Intentos: 1", self.ledger.summary())
        self.assertEqual(self.execution.session_losses, 0)
        with patch("demo_execution.time.time", return_value=observed + 300):
            self.execution.submit("PUT", observed + 300, 300)
        self.browser.click_once.assert_called_once()
        self.assertIn("Intentos: 1", self.ledger.summary())

    def test_expiry_during_preparation_or_before_click_keeps_execution_armed(self):
        observed = 1_800_000_002.0
        self.execution.arm()
        for stage in ("prepare_expiry", "click_once"):
            with self.subTest(stage=stage):
                self.browser.prepare_expiry.side_effect = None
                self.browser.click_once.side_effect = None
                getattr(self.browser, stage).side_effect = EntryWindowExpired("Tiempo vencido")
                with patch("demo_execution.time.time", return_value=observed):
                    with self.assertRaises(EntryWindowExpired):
                        self.execution.submit("CALL", observed, 300)
                self.assertTrue(self.execution.armed)
                self.assertIsNone(self.ledger.pending())
        self.assertIn("Intentos: 2", self.ledger.summary())
        row = self.ledger.db.execute("SELECT status FROM demo_orders").fetchone()
        self.assertEqual(row["status"], "CANCELLED")
        self.assertEqual(self.execution.session_losses, 0)
        self.browser.click_once.side_effect = None
        with patch("demo_execution.time.time", return_value=observed + 300):
            self.execution.submit("PUT", observed + 300, 300)
        self.assertEqual(self.ledger.pending()["status"], "REQUESTED")

    def test_final_time_checks_never_press_order_button(self):
        browser = DemoBrowser()
        browser.page = Mock()
        observed = 1_800_000_002.0
        for now, source_time in (
            (observed + 6.01, observed),
            (1_800_000_300.0, 1_800_000_299.0),
            (1_800_000_298.0, 1_800_000_297.0),
        ):
            with self.subTest(now=now), patch.object(
                browser, "settings", return_value=self.settings,
            ), patch.object(browser, "require_demo"), patch(
                "demo_execution.time.time", return_value=now,
            ):
                with self.assertRaises(EntryWindowExpired):
                    browser.click_once("CALL", self.settings, observed_at=source_time)
        browser.page.get_by_role.assert_not_called()

    def test_attempt_count_persists_and_reset_preserves_attempt_history(self):
        with patch("demo_execution.time.time", return_value=1_800_000_000):
            self.ledger.record_expired_attempt("CALL", 1_799_999_993, "Senal antigua")
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.assertIn("Intentos: 1", self.ledger.summary())
        self.ledger.reset_statistics(1_800_000_010)
        self.assertIn("Intentos: 0", self.ledger.summary())
        self.assertEqual(
            self.ledger.db.execute("SELECT COUNT(*) FROM expired_entry_attempts").fetchone()[0], 1,
        )
        with patch("demo_execution.time.time", return_value=1_800_000_020):
            self.ledger.record_expired_attempt("PUT", 1_800_000_013, "Senal antigua")
        self.assertIn("Intentos: 1", self.ledger.summary())

    def test_invalid_signal_time_is_not_a_recoverable_expiry(self):
        self.execution.arm()
        for observed in (float("nan"), float("inf"), float("-inf"), time.time() + 100):
            with self.subTest(observed=observed):
                with self.assertRaises(RuntimeError) as error:
                    self.execution.submit("CALL", observed, 300)
                self.assertNotIsInstance(error.exception, EntryWindowExpired)
        self.assertIn("Intentos: 0", self.ledger.summary())
        self.browser.click_once.assert_not_called()

    def test_history_is_not_counted_and_ambiguous_new_positions_block(self):
        self.browser.positions.return_value = [
            replace(self.position("old", True, Decimal(0)), opened="03.10.2026, 23:04:00"),
        ]
        self.execution.arm()
        self.execution.submit("CALL", time.time(), 300)
        with self.assertRaises(RuntimeError):
            self.refresh_after_expiry()
        self.browser.positions.return_value = [self.position("a"), self.position("b")]
        with self.assertRaises(RuntimeError):
            self.refresh_after_expiry()
        self.assertEqual(self.ledger.pending()["status"], "REQUESTED")
        self.assertIn("Ganadas: 0", self.ledger.summary())
        self.assertNotIn("CALL:", self.ledger.summary())
        self.assertNotIn("Pendientes", self.ledger.summary())

    def test_losses_pending_and_direction_totals(self):
        first = self.ledger.reserve("CALL", self.settings, set(), "a")
        self.ledger.reconcile([self.position("call", True, Decimal(0))])
        self.ledger.reserve("PUT", self.settings, {"call"}, "b")
        self.ledger.reconcile([self.position("put", True, Decimal("37.40"), "PUT")])
        summary = self.ledger.summary()
        self.assertEqual(summary.count("Ganadas:"), 1)
        self.assertEqual(summary.count("Perdidas:"), 2)
        self.assertIn("Ganadas: 1", summary)
        self.assertIn("Perdidas: 1", summary)
        self.assertIn("Neto invertido: $40.00", summary)
        self.assertIn("Ingresos: $37.40", summary)
        self.assertIn("Perdidas: $20.00", summary)
        self.assertNotIn("CALL:", summary)
        self.assertNotIn("PUT:", summary)
        self.assertNotIn("TOTAL:", summary)
        self.assertIsInstance(first, int)

    def test_missing_return_and_duplicate_signal_do_not_change_totals(self):
        self.ledger.reserve("CALL", self.settings, set(), "unique")
        with self.assertRaises(ValueError):
            self.ledger.reconcile([self.position(closed=True)])
        self.ledger.reconcile([self.position(closed=True, returned=Decimal(0))])
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.reserve("CALL", self.settings, set(), "unique")
        self.assertIn("Ganadas: 0", self.ledger.summary())
        self.assertIn("Perdidas: 1", self.ledger.summary())

    def test_start_does_not_navigate_to_history(self):
        self.browser.positions.return_value = [self.position()]
        self.execution.arm()
        self.assertTrue(self.execution.armed)
        self.browser.positions.assert_not_called()

    def test_more_than_ten_requests_without_rearming(self):
        from unittest.mock import patch
        self.execution.arm()
        for index in range(25):
            now = 1800000000 + index * 300
            with patch("demo_execution.time.time", return_value=now):
                self.execution.submit("CALL", now, 300)
            self.browser.positions.return_value = [
                self.position(str(index), closed=True, returned=Decimal("37.40")),
            ]
            self.refresh_after_expiry()
        self.assertEqual(self.browser.click_once.call_count, 25)
        self.assertTrue(self.execution.armed)
        self.assertIsNone(self.ledger.pending())
        self.assertIn("Ganadas: 25", self.ledger.summary())
        self.assertIn("Perdidas: 0", self.ledger.summary())

    def test_reset_statistics_keeps_history_and_pending_safety_record(self):
        with patch("demo_execution.time.time", return_value=1_800_000_000):
            self.ledger.reserve("CALL", self.settings, set(), "closed-before-reset")
        self.ledger.reconcile([self.position("closed", True, Decimal(0))])
        with patch("demo_execution.time.time", return_value=1_800_000_010):
            pending_id = self.ledger.reserve("PUT", self.settings, set(), "pending-before-reset")
        self.ledger.reset_statistics(1_800_000_020)
        summary = self.ledger.summary()
        self.assertIn("Ganadas: 0", summary)
        self.assertIn("Perdidas: 0", summary)
        self.assertIn("Neto invertido: $0.00", summary)
        self.assertIn("Ingresos: $0.00", summary)
        self.assertIn("Perdidas: $0.00", summary)
        self.assertNotIn("Pendientes", summary)
        self.assertEqual(self.ledger.pending()["id"], pending_id)
        self.assertEqual(
            self.ledger.db.execute("SELECT COUNT(*) FROM demo_orders").fetchone()[0], 2,
        )
        self.ledger.reconcile([
            self.position("pending-closed", True, Decimal(0), "PUT"),
        ])
        with patch("demo_execution.time.time", return_value=1_800_000_030):
            self.ledger.reserve("CALL", self.settings, set(), "closed-after-reset")
        self.ledger.reconcile([self.position("after-reset", True, Decimal("37.40"))])
        updated = self.ledger.summary()
        self.assertIn("Ganadas: 1", updated)
        self.assertIn("Neto invertido: $20.00", updated)
        self.assertIn("Ingresos: $37.40", updated)

    def test_three_total_losses_stop_even_with_wins_between(self):
        self.execution.arm()
        returns = ("0", "37.40", "10", "20", "0")
        for index, returned in enumerate(returns):
            now = 1800000000 + index * 300
            direction = "PUT" if index % 2 else "CALL"
            with patch("demo_execution.time.time", return_value=now):
                self.execution.submit(direction, now, 300)
            self.browser.positions.return_value = [
                self.position(str(index), closed=True,
                              returned=Decimal(returned), direction=direction),
            ]
            self.refresh_after_expiry()
            self.assertEqual(self.execution.armed, index < 4)
            self.refresh_after_expiry()
        self.assertEqual(self.execution.session_losses, 3)
        with self.assertRaisesRegex(RuntimeError, "no armada"):
            self.execution.submit("CALL", time.time(), 300)
        self.assertEqual(self.browser.click_once.call_count, 5)
        self.execution.arm()
        self.assertEqual(self.execution.session_losses, 0)
        self.assertTrue(self.execution.armed)

    def test_pending_uncertain_and_historical_losses_do_not_count(self):
        self.browser.positions.return_value = [
            replace(self.position("old", True, Decimal(0)), opened="03.10.2026, 23:04:00"),
        ]
        self.execution.arm()
        self.execution.submit("CALL", time.time(), 300)
        with self.assertRaises(RuntimeError):
            self.refresh_after_expiry()
        self.assertEqual(self.execution.session_losses, 0)
        self.browser.positions.return_value = [self.position("new")]
        self.refresh_after_expiry()
        self.refresh_after_expiry()
        self.assertEqual(self.execution.session_losses, 0)
        self.browser.positions.return_value = [self.position("new", True, Decimal(0))]
        self.refresh_after_expiry()
        self.refresh_after_expiry()
        self.assertEqual(self.execution.session_losses, 1)
        self.assertTrue(self.execution.armed)

    def test_money_formats_and_rejects_other_currencies(self):
        self.assertEqual(money("$1,000\n.00"), Decimal("1000"))
        self.assertEqual(money("$0\n.00"), Decimal(0))
        for invalid in ("COP20", "$1.000,00", "-$20", "unknown", "$NaN"):
            with self.assertRaises(ValueError):
                money(invalid)

    def test_real_account_or_menu_ambiguity_rejected(self):
        page = Mock()
        page.url = "https://iqoption.com/pwa/traderoom"
        header = page.get_by_test_id.return_value.filter.return_value
        header.count.return_value = 1
        header.inner_text.return_value = "$100"
        header.locator.return_value.inner_text.return_value = "Cuenta real $100"
        with self.assertRaisesRegex(RuntimeError, "demo"):
            DemoBrowser.require_demo(page)
        header.locator.return_value.inner_text.return_value = "Cuenta demo $100"
        DemoBrowser.require_demo(page)
        header.count.return_value = 2
        with self.assertRaises(RuntimeError):
            DemoBrowser.require_demo(page)


if __name__ == "__main__":
    unittest.main()
