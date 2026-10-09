import os
import time
import unittest
from decimal import Decimal
from unittest.mock import Mock

from playwright.sync_api import Error as BrowserError, sync_playwright

from demo_execution import DemoBrowser, Settings, validate_snapshot
from test_demo_execution import snapshot


class SettingsSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.browser = DemoBrowser()
        page = self.browser.page = Mock()
        page.url = "https://iqoption.com/pwa/traderoom"
        page.is_closed.return_value = False
        self.controls = [
            snapshot("$100", "balanceAmount-Wrapper"),
            snapshot("Cantidad ($) 20", "amountSelector"),
            snapshot("Tiempo 21:10", "expirationSelector"),
            snapshot("EUR/USD Binaria", "positions-inspector"),
        ]
        page.locator.return_value.evaluate_all.return_value = self.controls
        self.timeframe = Mock()
        self.timeframe.evaluate_all.return_value = [snapshot("5m")]
        self.up = Mock()
        self.down = Mock()
        self.up.evaluate_all.return_value = [snapshot("Sube")]
        self.down.evaluate_all.return_value = [snapshot("Baja")]
        self.toolbar = Mock()
        self.toolbar.get_by_role.return_value = self.timeframe
        page.get_by_role.side_effect = lambda role, **kwargs: (
            self.toolbar if role == "toolbar"
            else self.up if kwargs["name"] == "Sube" else self.down
        )

    def test_four_batched_reads_replace_individual_dom_queries(self):
        self.assertEqual(
            self.browser.settings(),
            Settings("EUR/USD Binaria", Decimal("20"), "Tiempo 21:10"),
        )
        self.browser.page.locator.return_value.evaluate_all.assert_called_once()
        for control in (self.timeframe, self.up, self.down):
            control.evaluate_all.assert_called_once()
            control.count.assert_not_called()
            control.inner_text.assert_not_called()
            control.is_visible.assert_not_called()
            control.is_enabled.assert_not_called()
        self.browser.page.get_by_test_id.assert_not_called()

    def test_missing_duplicate_or_malformed_data_never_returns_settings(self):
        for name in ("amountSelector", "expirationSelector", "positions-inspector"):
            for duplicate in (False, True):
                with self.subTest(name=name, duplicate=duplicate):
                    controls = [control.copy() for control in self.controls]
                    item = next(control for control in controls if control["test_id"] == name)
                    if duplicate:
                        controls.append(item.copy())
                    else:
                        controls.remove(item)
                    self.browser.page.locator.return_value.evaluate_all.return_value = controls
                    with self.assertRaisesRegex(RuntimeError, "ausente o ambiguo"):
                        self.browser.settings()
        for value in (None, {}, [None], [{}], [snapshot(visible="true")]):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                validate_snapshot(value)

    def test_each_read_is_fresh_and_real_account_is_rejected(self):
        self.browser.settings()
        self.controls[1]["text"] = "Cantidad ($) 30"
        self.assertEqual(self.browser.settings().stake, Decimal("30"))
        self.controls[0]["container_text"] = "Cuenta real $100"
        with self.assertRaisesRegex(RuntimeError, "demo"):
            self.browser.settings()

    def test_invalid_account_currency_and_header_ambiguity_are_rejected(self):
        for headers in (
            [], [snapshot("$100", "balanceAmount-Wrapper", visible=False)],
            [snapshot("$100", "balanceAmount-Wrapper")] * 2,
            [snapshot("EUR100", "balanceAmount-Wrapper")],
            [snapshot("$100", "balanceAmount-Wrapper", container="Cuenta demo Cuenta real")],
        ):
            with self.subTest(headers=headers):
                self.browser.page.locator.return_value.evaluate_all.return_value = (
                    headers + self.controls[1:]
                )
                with self.assertRaises(RuntimeError):
                    self.browser.settings()

    def test_invalid_values_buttons_and_timeframe_are_rejected(self):
        for index, text in (
            (1, "Cantidad ($) 0"), (1, "Cantidad (EUR) 20"),
            (2, "Tiempo desconocido"), (3, "EUR/USD Digital"),
        ):
            with self.subTest(index=index, text=text):
                controls = [control.copy() for control in self.controls]
                controls[index]["text"] = text
                self.browser.page.locator.return_value.evaluate_all.return_value = controls
                with self.assertRaises((ValueError, RuntimeError)):
                    self.browser.settings()
        self.browser.page.locator.return_value.evaluate_all.return_value = self.controls
        for control in (self.timeframe, self.up, self.down):
            original = control.evaluate_all.return_value
            for invalid in ([], original * 2, [snapshot(visible=False)], [snapshot(enabled=False)]):
                if control is self.timeframe and invalid == [snapshot(enabled=False)]:
                    continue
                with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                    control.evaluate_all.return_value = invalid
                    self.browser.settings()
            control.evaluate_all.return_value = original

    def test_wrong_domain_or_closed_browser_never_reads_dom(self):
        for url in ("https://example.com/pwa/traderoom", "http://iqoption.com/pwa/traderoom",
                    "https://iqoption.com/pwa/trades"):
            self.browser.page.url = url
            with self.assertRaises(RuntimeError):
                self.browser.settings()
        self.browser.page.is_closed.return_value = True
        with self.assertRaises(RuntimeError):
            self.browser.settings()
        self.browser.page.locator.assert_not_called()

    def test_failed_read_is_logged_and_never_returns_default_settings(self):
        self.browser.page.locator.return_value.evaluate_all.side_effect = BrowserError("DOM cerrado")
        with self.assertLogs("iq_option_bot", level="INFO") as logs:
            with self.assertRaisesRegex(BrowserError, "DOM cerrado"):
                self.browser.settings()
        self.assertTrue(any("cuenta/datos" in line for line in logs.output))
        self.browser.page.get_by_role.assert_not_called()


HTML = """
<html><body>
<div>Cuenta demo<div><span data-testid="balanceAmount-Wrapper">$100</span></div></div>
<div data-testid="amountSelector">Cantidad ($) 20</div>
<div data-testid="expirationSelector">Tiempo <span data-testid="content">21:10</span></div>
<div data-testid="positions-inspector">EUR/USD Binaria</div>
<div role="toolbar"><button>5m</button></div>
<button aria-label="Sube"><span>Comprar arriba</span></button>
<button aria-label="Baja"><span>Comprar abajo</span></button>
</body></html>
"""


@unittest.skipUnless(os.environ.get("IQ_OPTION_SETTINGS_BROWSER_TESTS") == "1",
                     "Activa la prueba local de Edge con IQ_OPTION_SETTINGS_BROWSER_TESTS=1.")
class LocalBrowserSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = sync_playwright().start()
        try:
            cls.edge = cls.runtime.chromium.launch(channel="msedge", headless=True)
        except BrowserError:
            cls.runtime.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.edge.close()
        cls.runtime.stop()

    def setUp(self):
        self.page = self.edge.new_page()
        self.addCleanup(self.page.close)
        # Intercept every request: this fixture never connects to the platform.
        self.page.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html", body=HTML,
        ))
        self.page.goto("https://iqoption.com/pwa/traderoom")
        self.browser = DemoBrowser()
        self.browser.page = self.page

    def test_local_dom_mean_read_time_under_one_second(self):
        samples = []
        for _ in range(10):
            started = time.perf_counter()
            self.assertEqual(
                self.browser.settings(),
                Settings("EUR/USD Binaria", Decimal("20"), "Tiempo 21:10"),
            )
            samples.append(time.perf_counter() - started)
        average = sum(samples) / len(samples)
        print(f"Lectura local Edge: media={average:.3f}s; maximo={max(samples):.3f}s; n=10")
        self.assertLess(average, 1.)

    def test_dom_visibility_disabled_and_accessible_names_preserve_safety(self):
        self.browser.settings()
        for script in (
            """document.querySelector('[aria-label="Sube"]').disabled = true""",
            """document.querySelector('[aria-label="Baja"]').setAttribute('aria-disabled','true')""",
            """document.querySelector('[aria-label="Sube"]').style.visibility = 'hidden'""",
            """document.querySelector('[role="toolbar"]').innerHTML += '<button>1m</button>'""",
            """document.body.innerHTML += '<button aria-label="Sube">duplicado</button>'""",
            """document.querySelector('[data-testid="balanceAmount-Wrapper"]').parentElement.parentElement.firstChild.textContent = 'Cuenta real'""",
        ):
            with self.subTest(script=script):
                self.page.set_content(HTML)
                self.page.evaluate(script)
                with self.assertRaises(RuntimeError):
                    self.browser.settings()

    def test_hidden_account_duplicate_does_not_override_visible_demo(self):
        self.page.evaluate("""() => {
            const hidden = document.createElement('div');
            hidden.style.display = 'none';
            hidden.innerHTML = '<div>Cuenta real<div><span data-testid="balanceAmount-Wrapper">$100</span></div></div>';
            document.body.append(hidden);
        }""")
        self.browser.settings()
        self.browser.require_demo(self.page)

    def test_final_read_detects_configuration_and_account_changes_before_any_click(self):
        expected = self.browser.settings()
        self.page.get_by_test_id("amountSelector").evaluate(
            "element => element.innerText = 'Cantidad ($) 30'",
        )
        with self.assertRaisesRegex(RuntimeError, "Cambio de activo"):
            self.browser.click_once("CALL", expected)
        self.page.set_content(HTML)
        self.page.get_by_test_id("balanceAmount-Wrapper").evaluate(
            "element => element.parentElement.parentElement.firstChild.textContent = 'Cuenta real'",
        )
        with self.assertRaisesRegex(RuntimeError, "demo"):
            self.browser.click_once("PUT", expected)


if __name__ == "__main__":
    unittest.main()
