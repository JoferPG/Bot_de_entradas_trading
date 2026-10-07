import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw, ImageFont
from monitors import Monitor
from demo_execution import EntryWindowExpired

from bot import (
    App, COLORS, CSV_FIELDS, IQ_OPTION_URL, Detection, Selector, SignalGate,
    TrackingUnavailable,
    LOGGER, configure_diagnostics, current_candle_reference, detect, main, make_preview, parse_color,
    reference_loss_is_transient, template_from, track_candle,
    white_price_point, write_event,
)


def arrow_image(signal="CALL", position=(10, 10)):
    image = Image.new("RGB", (90, 80), "#101827")
    x, y = position
    ImageDraw.Draw(image).polygon(
        [(x + 8, y), (x, y + 16), (x + 16, y + 16)], fill=COLORS[signal],
    )
    return image


class RegionTests(unittest.TestCase):
    def setUp(self):
        self.app = App.__new__(App)
        self.app.root = Mock()
        self.app.stop = Mock()
        self.app.status = Mock()
        self.app.region_status = Mock()
        self.app.signal_text = Mock()
        self.app.signal_panel = Mock()
        self.app.execution = Mock()
        self.app.order_status = Mock()
        self.app.ema_status = Mock()
        self.app.region = None
        self.app.templates = {"CALL": Mock(), "PUT": Mock()}
        self.monitor = Monitor((0, 0, 400, 300), True)
        self.app.capture_monitor = Mock(return_value=(
            self.monitor, Image.new("RGB", (400, 300)),
        ))

    def test_secondary_monitor_negative_coordinates_are_saved(self):
        self.app.capture_monitor.return_value = (
            Monitor((-1920, -200, 0, 880), False),
            Image.new("RGB", (1920, 1080)),
        )
        with patch("bot.Selector") as selector:
            self.app.select_region()
            selector.call_args.args[3]((100, 20, 140, 280))
        self.assertEqual(self.app.region, (-1820, -180, -1780, 80))

    def test_monitor_change_disarms_and_clears_calibration(self):
        self.app.monitor_picker = Mock()
        self.app.monitor_picker.current.return_value = 1
        self.app.calibration_status = Mock()
        self.app.preview_panel = Mock()
        self.app.region = (1, 2, 3, 4)
        self.app.change_monitor()
        self.app.stop.assert_called_once()
        self.assertEqual(self.app.monitor_index, 1)
        self.assertIsNone(self.app.region)
        self.assertFalse(self.app.templates)

    def test_local_box_must_fit_monitor(self):
        with self.assertRaises(ValueError):
            self.monitor.to_screen((0, 0, 401, 100))

    def test_reset_statistics_keeps_execution_safety_state(self):
        self.app.ledger = Mock()
        self.app.ledger.summary.return_value = "zero"
        self.app.summary_text = Mock()
        self.app.execution.armed = True
        self.app.execution.session_losses = 2
        with patch("bot.messagebox.askyesno", return_value=True):
            self.app.reset_statistics()
        self.app.ledger.reset_statistics.assert_called_once_with()
        self.app.summary_text.set.assert_called_once_with(
            "zero\nPerdidas de seguridad: 2/3",
        )
        self.assertTrue(self.app.execution.armed)
        self.assertEqual(self.app.execution.session_losses, 2)

    def test_statistics_summary_displays_security_loss_counter(self):
        self.app.ledger = Mock()
        self.app.ledger.summary.return_value = "Ganadas: 0"
        self.app.execution.session_losses = 1
        self.assertEqual(
            self.app.statistics_summary(),
            "Ganadas: 0\nPerdidas de seguridad: 1/3",
        )

    @patch("bot.list_monitors")
    @patch("bot.ImageGrab.grab")
    def test_monitor_capture_uses_virtual_desktop(self, grab, monitors):
        self.app.monitors = [Monitor((-400, 0, 0, 300), False)]
        self.app.monitor_index = 0
        monitors.return_value = self.app.monitors
        App.capture_monitor(self.app)
        grab.assert_called_once_with(bbox=(-400, 0, 0, 300), all_screens=True)

    @patch("bot.Selector")
    @patch("bot.ImageGrab.grab", return_value=Image.new("RGB", (400, 300)))
    def test_region_callback_saves_box_and_survives_stop(self, grab, selector):
        self.app.select_region()
        callback = selector.call_args.args[3]
        callback((100, 20, 140, 280))
        self.assertEqual(self.app.region, (100, 20, 140, 280))
        self.assertIn("40x260", self.app.region_status.set.call_args.args[0])
        self.app.job = None
        App.stop(self.app)
        self.assertEqual(self.app.region, (100, 20, 140, 280))

    @patch("bot.messagebox.showerror")
    def test_missing_region_message_is_specific(self, error):
        self.app.start()
        self.assertIn("Falta guardar: area de velas del grafico.", error.call_args.args[1])

    @patch("bot.messagebox.showerror")
    def test_saved_region_is_not_reported_missing_when_sample_missing(self, error):
        self.app.region = (10, 10, 50, 200)
        del self.app.templates["PUT"]
        self.app.start()
        self.assertIn("Falta guardar: muestra PUT.", error.call_args.args[1])

    def make_selector(self):
        selector = Selector.__new__(Selector)
        selector.start = (80, 90)
        selector.image = Image.new("RGB", (100, 100))
        selector.window = Mock()
        selector.callback = Mock()
        return selector

    def configured_start_app(self):
        self.app.region = (0, 0, 250, 200)
        self.app.period = Mock()
        self.app.period.get.return_value = "300"
        self.app.green_candle = Mock()
        self.app.green_candle.get.return_value = "#2D9E6B"
        self.app.red_candle = Mock()
        self.app.red_candle.get.return_value = "#E44A4E"
        self.app.tick = Mock()
        self.app.gate = None
        self.app.ledger = Mock()
        self.app.ledger.pending.return_value = None
        return self.app

    @patch("bot.messagebox.askokcancel", return_value=True)
    def test_start_validates_demo_before_detection(self, confirm):
        app = self.configured_start_app()
        def activate(period):
            self.assertEqual(period, 300)
            self.assertIsNone(app.gate)
            app.execution.armed = True
        app.execution.arm.side_effect = activate
        app.start()
        app.execution.arm.assert_called_once_with(300)
        app.tick.assert_called_once()
        self.assertIsInstance(app.gate, SignalGate)
        self.assertTrue(app.execution.armed)
        self.assertIn("DEMO ACTIVA", app.order_status.set.call_args.args[0])

    @patch("config.EMA_ANALYSIS_ENABLED", False)
    @patch("config.EMA_ENTRY_FILTER_ENABLED", True)
    @patch("bot.messagebox.showerror")
    def test_ema_filter_without_analysis_cannot_arm_or_start(self, error):
        app = self.configured_start_app()
        app.start()
        app.execution.arm.assert_not_called()
        app.tick.assert_not_called()
        app.stop.assert_called_once()
        self.assertIn("EMA_ANALYSIS_ENABLED", error.call_args.args[1])

    @patch("bot.messagebox.showerror")
    @patch("bot.messagebox.askokcancel", return_value=True)
    def test_failed_demo_validation_does_not_start_detector(self, confirm, error):
        app = self.configured_start_app()
        app.execution.arm.side_effect = RuntimeError("Cuenta real")
        with self.assertLogs(LOGGER, level="ERROR"):
            app.start()
        self.assertFalse(app.execution.armed)
        self.assertIsNone(app.gate)
        app.tick.assert_not_called()
        self.assertIn("Cuenta real", app.order_status.set.call_args.args[0])

    @patch("bot.messagebox.askokcancel", return_value=False)
    def test_cancelled_start_does_not_activate_demo(self, confirm):
        app = self.configured_start_app()
        app.start()
        app.execution.arm.assert_not_called()
        app.tick.assert_not_called()

    @patch("bot.messagebox.showerror")
    def test_start_rejects_non_five_minute_period(self, error):
        app = self.configured_start_app()
        app.period.get.return_value = "120"
        app.start()
        app.execution.arm.assert_not_called()
        app.tick.assert_not_called()
        self.assertIn("300", error.call_args.args[1])

    def test_release_saves_reverse_drag(self):
        selector = self.make_selector()
        selector.release(Mock(x=20, y=10))
        selector.callback.assert_called_once_with((20, 10, 80, 90))
        selector.window.destroy.assert_called_once()

    @patch("bot.messagebox.showerror")
    def test_narrow_region_has_explicit_error_and_can_retry(self, error):
        selector = self.make_selector()
        selector.release(Mock(x=82, y=20))
        error.assert_called_once()
        selector.callback.assert_not_called()
        selector.window.destroy.assert_not_called()
        selector.release(Mock(x=40, y=20))
        selector.callback.assert_called_once_with((40, 20, 80, 90))


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.colors = (parse_color("#2D9E6B"), parse_color("#E44A4E"))
        self.templates = {
            signal: template_from(arrow_image(signal), signal)
            for signal in ("CALL", "PUT")
        }
        # Aislar las regresiones antiguas de flechas/rearme del nuevo extractor visual.
        reference_patch = patch.object(
            App, "locate_visual_reference",
            lambda app, image: current_candle_reference(image, app.candle_colors),
        )
        preview_patch = patch.object(App, "reference_preview", lambda app, image, band: make_preview(image, band, "white"))
        for patcher in (reference_patch, preview_patch):
            patcher.start()
            self.addCleanup(patcher.stop)
        ema_patch = patch("config.EMA_ENTRY_FILTER_ENABLED", False)
        ema_patch.start()
        self.addCleanup(ema_patch.stop)

    def chart(self, centers, current_signal=None):
        image = Image.new("RGB", (250, 200), "#101827")
        draw = ImageDraw.Draw(image)
        for center in centers:
            draw.rectangle((center - 6, 70, center + 6, 110), fill=self.colors[0])
        historical = arrow_image("PUT").crop((10, 10, 27, 27))
        image.paste(historical, (centers[0] - 8, 25))
        if current_signal:
            arrow = arrow_image(current_signal).crop((10, 10, 27, 27))
            image.paste(arrow, (centers[-1] - 8, 135))
        draw.line((0, 90, 249, 90), fill="#F0B414")
        draw.ellipse(
            (centers[-1] - 3, 87, centers[-1] + 3, 93), fill="white",
        )
        return image

    def test_white_point_aligns_only_current_arrow(self):
        for direction in ("CALL", "PUT"):
            image = self.chart([30, 70, 110], direction)
            band = current_candle_reference(image, self.colors)
            self.assertEqual(band, (107, 113))
            self.assertEqual(detect(image, self.templates, band).signal, direction)
            historical = self.chart([30, 70, 110])
            historical.paste(arrow_image(direction).crop((10, 10, 27, 27)), (62, 135))
            self.assertIsNone(detect(
                historical, self.templates, current_candle_reference(historical, self.colors),
            ))

    def test_reference_blocks_missing_ambiguous_or_old_point(self):
        for scenario in ("missing", "ambiguous", "old", "no_line"):
            with self.subTest(scenario=scenario):
                image = self.chart([30, 70, 110], "PUT")
                draw = ImageDraw.Draw(image)
                if scenario in ("missing", "old", "no_line"):
                    draw.rectangle((107, 87, 113, 93), fill=self.colors[0])
                if scenario in ("old", "ambiguous"):
                    draw.ellipse((67, 87, 73, 93), fill="white")
                if scenario == "no_line":
                    draw.ellipse((107, 47, 113, 53), fill="white")
                with self.assertRaises(TrackingUnavailable):
                    current_candle_reference(image, self.colors)

    def test_white_text_or_flat_background_is_not_price_point(self):
        image = Image.new("RGB", (250, 200), "#999999")
        draw = ImageDraw.Draw(image)
        draw.ellipse((107, 87, 113, 93), fill="white")
        with self.assertRaises(TrackingUnavailable):
            white_price_point(image)
        image = Image.new("RGB", (250, 200), "#101827")
        ImageDraw.Draw(image).rectangle((107, 87, 113, 93), fill="white")
        with self.assertRaises(TrackingUnavailable):
            white_price_point(image)

    def test_bright_core_with_glow_and_two_color_dashed_price_line(self):
        image = self.chart([30, 70, 110], "PUT")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 87, 249, 93), fill="#101827")
        for left in range(0, 108, 9):
            draw.line((left, 90, left + 6, 90), fill="#EEF7FF")
        draw.line((114, 90, 249, 90), fill="#617299")
        draw.line((102, 90, 114, 90), fill=(215, 224, 243))
        draw.rectangle((108, 88, 111, 91), fill=(229, 238, 255))
        draw.point((108, 88), fill=(215, 224, 243))
        draw.point((111, 91), fill=(215, 224, 243))
        self.assertEqual(white_price_point(image), 109.5)
        band = current_candle_reference(image, self.colors)
        self.assertEqual(detect(image, self.templates, band).signal, "PUT")

    def test_small_two_by_two_price_point_is_detected(self):
        image = self.chart([30, 70, 110])
        draw = ImageDraw.Draw(image)
        draw.rectangle((106, 86, 114, 94), fill="#101827")
        draw.line((0, 90, 105, 90), fill="#EEF7FF")
        draw.line((114, 90, 249, 90), fill="#617299")
        draw.rectangle((109, 89, 110, 90), fill=(240, 248, 255))
        self.assertEqual(white_price_point(image), 109.5)
        self.assertEqual(
            current_candle_reference(image, self.colors),
            (106.5, 112.5),
        )

    def test_single_bright_pixel_or_thin_line_is_not_a_price_point(self):
        image = self.chart([30, 70, 110])
        draw = ImageDraw.Draw(image)
        draw.rectangle((106, 86, 114, 94), fill="#101827")
        draw.point((109, 90), fill="white")
        with self.assertRaises(TrackingUnavailable):
            white_price_point(image)
        draw.rectangle((109, 89, 110, 89), fill="white")
        with self.assertRaises(TrackingUnavailable):
            white_price_point(image)

    def test_point_resolves_grid_ambiguity_without_accepting_history(self):
        image = Image.new("RGB", (906, 496), "#101827")
        draw = ImageDraw.Draw(image)
        for left, right in (
            (0, 9), (24, 55), (69, 100), (115, 146), (161, 192),
            (206, 237), (252, 283), (297, 329), (343, 374),
            (389, 420), (434, 466), (480, 515),
            (810, 815), (818, 823), (826, 831),
        ):
            draw.rectangle((left, 120, right, 160), fill=self.colors[1])
        draw.line((0, 160, 905, 160), fill="#F0B414")
        draw.ellipse((493, 157, 499, 163), fill="white")
        band = current_candle_reference(image, self.colors)
        self.assertEqual(band, (493, 499))
        image.paste(arrow_image("PUT").crop((10, 10, 27, 27)), (442, 65))
        self.assertIsNone(detect(image, self.templates, band))

    def test_gray_current_candle_does_not_select_previous_put(self):
        image = self.chart([30, 70, 110, 150])
        draw = ImageDraw.Draw(image)
        draw.rectangle((144, 70, 156, 110), fill="#777777")
        draw.line((0, 90, 249, 90), fill="#F0B414")
        draw.ellipse((147, 87, 153, 93), fill="white")
        image.paste(arrow_image("PUT").crop((10, 10, 27, 27)), (102, 135))
        with self.assertRaises(TrackingUnavailable):
            current_candle_reference(image, self.colors)

    def test_point_shift_disarms_before_old_candidate_can_submit(self):
        app = self.ready_app()
        app.last_reference_center = 70
        self.run_signal_tick(app)
        app.execution.submit.assert_not_called()
        self.assertFalse(app.gate.armed)
        self.assertEqual(app.gate.consecutive, 0)

    def test_missing_point_never_submits_even_when_gate_ready(self):
        app = self.ready_app()
        image = self.chart([30, 70, 110], "CALL")
        ImageDraw.Draw(image).rectangle((107, 87, 113, 93), fill=self.colors[0])
        with (
            patch("bot.ImageGrab.grab", return_value=image),
            patch("bot.list_monitors", return_value=app.monitors),
            patch("bot.ImageTk.PhotoImage"),
            patch("bot.write_event") as write,
        ):
            app.tick()
        app.execution.submit.assert_not_called()
        write.assert_not_called()
        self.assertFalse(app.gate.armed)

    def test_reference_rechecked_before_submission(self):
        app = self.ready_app()
        first = self.chart([30, 70, 110], "CALL")
        second = self.chart([30, 70, 110], "CALL")
        ImageDraw.Draw(second).rectangle((107, 87, 113, 93), fill=self.colors[0])
        with (
            patch("bot.ImageGrab.grab", side_effect=(first, second)),
            patch("bot.list_monitors", return_value=app.monitors),
            patch("bot.ImageTk.PhotoImage"),
            patch("bot.time.time", return_value=1800000002),
            patch("bot.write_event"),
        ):
            app.tick()
        app.execution.submit.assert_not_called()
        self.assertFalse(app.gate.armed)

    def test_follows_moving_candle_and_excludes_history(self):
        for centers in ([30, 70, 110], [30, 70, 110, 150], [20, 60, 100, 140]):
            for signal in (None, "CALL", "PUT"):
                with self.subTest(centers=centers, signal=signal):
                    image = self.chart(centers, signal)
                    band = track_candle(image, self.colors)
                    self.assertLess(band[0], centers[-1])
                    self.assertGreater(band[1], centers[-1])
                    detection = detect(image, self.templates, band)
                    self.assertEqual(detection.signal if detection else None, signal)

    def test_missing_candles_and_wrong_colors_stop(self):
        with self.assertRaises(RuntimeError):
            track_candle(Image.new("RGB", (200, 200)), self.colors)
        with self.assertRaises(RuntimeError):
            track_candle(self.chart([30, 70]), ((1, 2, 3), (4, 5, 6)))

    def test_static_candle_is_tracked_without_motion(self):
        image = self.chart([30, 70, 110])
        for _ in range(5):
            self.assertEqual(track_candle(image, self.colors), (90, 130))

    def test_thin_latest_does_not_fall_back_to_history(self):
        image = self.chart([30, 70, 110])
        ImageDraw.Draw(image).line((150, 70, 150, 110), fill=self.colors[0])
        band = track_candle(image, self.colors)
        self.assertLess(band[0], 150)
        self.assertGreater(band[1], 150)

    def test_one_pixel_high_current_body_is_not_previous_candle(self):
        image = self.chart([30, 70, 110])
        ImageDraw.Draw(image).line((144, 80, 156, 80), fill=self.colors[0])
        band = track_candle(image, self.colors)
        self.assertLess(band[0], 150)
        self.assertGreater(band[1], 150)
        self.assertGreater(band[0], 110)

    def test_fragmented_current_body_and_wick_share_one_candle(self):
        image = self.chart([30, 70, 110])
        draw = ImageDraw.Draw(image)
        draw.rectangle((144, 80, 149, 86), fill=self.colors[0])
        draw.rectangle((151, 80, 156, 86), fill=self.colors[0])
        draw.line((150, 70, 150, 79), fill=self.colors[0])
        band = track_candle(image, self.colors)
        self.assertLess(band[0], 150)
        self.assertGreater(band[1], 150)
        self.assertGreater(band[0], 110)

    def test_fragmented_small_body_without_colored_center_is_tracked(self):
        image = self.chart([30, 70, 110], "PUT")
        draw = ImageDraw.Draw(image)
        draw.line((144, 80, 148, 80), fill=self.colors[0])
        draw.line((152, 80, 156, 80), fill=self.colors[0])
        band = track_candle(image, self.colors)
        self.assertGreater(band[0], 110)
        self.assertEqual(detect(image, self.templates, band), None)

    def test_separated_outer_wick_does_not_select_previous_body(self):
        image = Image.new("RGB", (322, 161), "#101827")
        draw = ImageDraw.Draw(image)
        for left, right in (
            (25, 34), (43, 52), (60, 71), (78, 90), (97, 108),
            (115, 126), (134, 143), (151, 162), (169, 180), (187, 199),
            (206, 215), (218, 219),
        ):
            draw.rectangle((left, 70, right, 76), fill=self.colors[0])
        band = track_candle(image, self.colors)
        self.assertGreater(band[0], 199)
        self.assertLess(band[0], 211)
        self.assertGreater(band[1], 219)

    def test_wide_buttons_do_not_become_latest_candle(self):
        image = self.chart([30, 70, 110], "CALL")
        draw = ImageDraw.Draw(image)
        draw.rectangle((170, 20, 240, 60), fill=self.colors[0])
        draw.rectangle((170, 130, 240, 170), fill=self.colors[1])
        band = track_candle(image, self.colors)
        self.assertEqual(detect(image, self.templates, band).signal, "CALL")
        self.assertLess(band[1], 170)

    def test_isolated_small_button_is_not_latest_candle(self):
        image = self.chart([30, 70, 110])
        ImageDraw.Draw(image).rectangle((220, 10, 230, 20), fill=self.colors[0])
        band = track_candle(image, self.colors)
        self.assertLess(band[1], 170)

    def test_irregular_new_candle_does_not_fall_back_to_history(self):
        image = self.chart([30, 70, 110])
        ImageDraw.Draw(image).line((165, 70, 165, 110), fill=self.colors[0])
        with self.assertRaises(TrackingUnavailable):
            track_candle(image, self.colors)

    def test_two_candles_are_insufficient(self):
        image = self.chart([30, 70])
        ImageDraw.Draw(image).line((110, 70, 110, 110), fill=self.colors[0])
        # Una mecha constituye la tercera vela si respeta el espaciado.
        self.assertGreater(track_candle(image, self.colors)[1], 110)
        with self.assertRaises(TrackingUnavailable):
            track_candle(self.chart([30, 70]), self.colors)

    def test_clipped_candle_stops(self):
        with self.assertRaises(RuntimeError):
            track_candle(self.chart([133, 190, 247]), self.colors)

    def test_tracking_loss_retries_without_event_or_error_dialog(self):
        app = App.__new__(App)
        app.gate = SignalGate(300)
        app.gate.armed = True
        app.gate.consecutive = 2
        app.region = (0, 0, 250, 200)
        app.candle_colors = self.colors
        app.signal_text = Mock()
        app.signal_panel = Mock()
        app.preview_panel = Mock()
        app.status = Mock()
        app.root = Mock()
        app.execution = Mock()
        app.order_status = Mock()
        app.monitors = [Monitor((0, 0, 250, 200), True)]
        app.monitor_index = 0
        captured = self.chart([30, 70, 110])
        ImageDraw.Draw(captured).line((165, 70, 165, 110), fill=self.colors[0])
        with (
            patch("bot.ImageGrab.grab", return_value=captured),
            patch("bot.ImageTk.PhotoImage") as photo,
            patch("bot.messagebox.showerror") as error,
            patch("bot.write_event") as write,
            patch("bot.list_monitors", return_value=app.monitors),
        ):
            app.tick()
        error.assert_not_called()
        write.assert_not_called()
        app.root.after.assert_called_once_with(100, app.tick)
        self.assertFalse(app.gate.armed)
        self.assertEqual(app.gate.consecutive, 0)
        self.assertIn("Esperando", app.status.set.call_args.args[0])
        displayed = photo.call_args.args[0]
        expected = captured.copy()
        expected.thumbnail((400, 160))
        self.assertEqual(displayed.size, expected.size)
        self.assertEqual(displayed.tobytes(), expected.tobytes())
        app.preview_panel.configure.assert_called_once_with(image=photo.return_value)
        app.execution.submit.assert_not_called()

    def test_brief_tracking_loss_disarms_and_restarts_three_frame_count(self):
        app = App.__new__(App)
        app.gate = SignalGate(300)
        timestamp = 1800000002.0
        app.gate.bucket = int(timestamp // app.gate.period)
        app.gate.armed = True
        app.gate.candidate = "PUT"
        app.gate.consecutive = 2
        app.region = (0, 0, 250, 200)
        app.candle_colors = self.colors
        app.signal_text = Mock()
        app.signal_panel = Mock()
        app.preview_panel = Mock()
        app.status = Mock()
        app.root = Mock()
        app.execution = Mock()
        app.order_status = Mock()
        app.monitors = [Monitor((0, 0, 250, 200), True)]
        app.monitor_index = 0
        app.last_reference_center = 110
        app.last_reference_at = 99.5
        captured = self.chart([30, 70, 110])
        ImageDraw.Draw(captured).line((165, 70, 165, 110), fill=self.colors[0])
        with (
            patch("bot.ImageGrab.grab", return_value=captured),
            patch("bot.ImageTk.PhotoImage"),
            patch("bot.time.time", return_value=timestamp),
            patch("bot.time.monotonic", return_value=100.0),
            patch("bot.list_monitors", return_value=app.monitors),
        ):
            app.tick()
        self.assertFalse(app.gate.armed)
        self.assertIsNone(app.gate.candidate)
        self.assertEqual(app.gate.consecutive, 0)
        self.assertIn("Esperando", app.status.set.call_args.args[0])
        app.execution.submit.assert_not_called()

    def test_transient_tracking_grace_rejects_expired_or_moved_reference(self):
        gate = SignalGate(300)
        timestamp = 1800000002.0
        gate.bucket = int(timestamp // gate.period)
        image = self.chart([30, 70, 110])
        self.assertTrue(
            reference_loss_is_transient(image, gate, 110, 99.5, timestamp, 100.0),
        )
        self.assertFalse(
            reference_loss_is_transient(image, gate, 110, 98.9, timestamp, 100.0),
        )
        self.assertFalse(
            reference_loss_is_transient(image, gate, 110, 99.5, timestamp + 300, 100.0),
        )
        moved = self.chart([30, 70, 150])
        self.assertFalse(
            reference_loss_is_transient(moved, gate, 110, 99.5, timestamp, 100.0),
        )

    def test_invalid_color(self):
        for color in ("green", "#GG0000", "#123"):
            with self.assertRaises(ValueError):
                parse_color(color)

    def test_panel_reports_direction_and_clears_disappeared_signal(self):
        app = App.__new__(App)
        app.signal_text = Mock()
        app.signal_panel = Mock()
        app.show_signal(Detection("CALL", 1), False)
        self.assertIn("COMPRA / CALL (provisional)", app.signal_text.set.call_args.args[0])
        app.show_signal(Detection("PUT", 1), True)
        self.assertIn("VENTA / PUT (visible)", app.signal_text.set.call_args.args[0])
        app.show_signal(None, False)
        app.signal_text.set.assert_called_with("SIN SENAL ACTUAL")

    @patch("bot.ImageTk.PhotoImage")
    def test_preview_displays_each_new_capture_without_blank_frames(self, photo):
        app = App.__new__(App)
        app.preview_panel = Mock()
        preview = Image.new("RGB", (200, 100), "#ffffff")
        app.update_preview(preview)
        self.assertIs(photo.call_args.args[0], preview)
        next_preview = Image.new("RGB", (200, 100), "#00c853")
        app.update_preview(next_preview)
        self.assertIs(photo.call_args.args[0], next_preview)
        self.assertEqual(photo.call_count, 2)
        self.assertEqual(app.preview_panel.configure.call_count, 2)

    def ready_app(self):
        app = App.__new__(App)
        app.gate = SignalGate(300)
        for timestamp, detection in (
            (1800000000, None), (1800000000.5, None),
            (1800000001, Detection("CALL", 1)),
            (1800000001.5, Detection("CALL", 1)),
        ):
            app.gate.observe(timestamp, detection)
        app.region = (0, 0, 250, 200)
        app.candle_colors = self.colors
        app.templates = self.templates
        app.monitors = [Monitor(app.region, True)]
        app.monitor_index = 0
        app.running_asset = "NO_IDENTIFICADO"
        app.running_period = 300
        app.log_path = Path("unused.csv")
        app.job = None
        for name in (
            "root", "execution", "ledger", "status", "order_status",
            "signal_text", "signal_panel", "preview_panel",
        ):
            setattr(app, name, Mock())
        app.ledger.pending.return_value = None
        return app

    def run_signal_tick(self, app):
        with (
            patch("bot.ImageGrab.grab", return_value=self.chart([30, 70, 110], "CALL")),
            patch("bot.list_monitors", return_value=app.monitors),
            patch("bot.ImageTk.PhotoImage"),
            patch("bot.time.time", return_value=1800000002),
            patch("bot.write_event"),
        ):
            app.tick()

    def test_new_arrow_on_static_current_candle_submits_once(self):
        for period in (60, 300):
            for direction in ("CALL", "PUT"):
                with self.subTest(period=period, direction=direction):
                    app = self.ready_app()
                    app.gate = SignalGate(period)
                    app.running_period = period
                    app.execution.armed = True
                    app.schedule_results = Mock()
                    app.summary_text = Mock()
                    blank = self.chart([30, 70, 110])
                    signal = self.chart([30, 70, 110], direction)
                    with (
                        patch("bot.ImageGrab.grab") as grab,
                        patch("bot.list_monitors", return_value=app.monitors),
                        patch("bot.ImageTk.PhotoImage"),
                        patch("bot.time.time") as clock,
                        patch("bot.write_event") as write,
                    ):
                        for i, image in enumerate((blank, blank, signal, signal, signal, signal)):
                            grab.return_value = image
                            clock.return_value = 1800000001 + i * 0.2
                            app.tick()
                    app.execution.submit.assert_called_once()
                    self.assertEqual(app.execution.submit.call_args.args[0], direction)
                    write.assert_called_once()
                    app.schedule_results.assert_called_once()

    def test_arrow_visible_at_start_on_static_candle_does_not_submit(self):
        app = self.ready_app()
        app.gate = SignalGate(300)
        app.execution.armed = True
        for _ in range(5):
            self.run_signal_tick(app)
        app.execution.submit.assert_not_called()

    def test_valid_signal_reports_unarmed_reason_without_click(self):
        app = self.ready_app()
        app.execution.armed = False
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.run_signal_tick(app)
        app.execution.submit.assert_not_called()
        self.assertIn("desarmada", app.order_status.set.call_args.args[0])
        self.assertTrue(any("Senal CALL sin entrada" in entry for entry in logs.output))

    def test_pre_submit_failure_preserves_reason_after_stop(self):
        app = self.ready_app()
        app.execution.armed = True
        app.execution.submit.side_effect = RuntimeError("Vencimiento no disponible")
        with self.assertLogs(LOGGER, level="INFO") as logs, patch("bot.messagebox.showerror"):
            self.run_signal_tick(app)
        self.assertFalse(app.execution.armed)
        self.assertIsNone(app.gate)
        self.assertIn("Vencimiento no disponible", app.order_status.set.call_args.args[0])
        self.assertTrue(any("Vencimiento no disponible" in entry for entry in logs.output))

    def test_expired_entry_updates_attempts_without_stopping_or_retrying_same_signal(self):
        app = self.ready_app()
        app.execution.armed = True
        app.execution.session_losses = 1
        app.execution.submit.side_effect = EntryWindowExpired("Senal demasiado antigua")
        app.ledger.summary.return_value = "Intentos: 1"
        app.summary_text = Mock()
        gate = app.gate
        with self.assertLogs(LOGGER, level="WARNING"), patch("bot.messagebox.showerror") as error:
            self.run_signal_tick(app)
            self.run_signal_tick(app)
        self.assertIs(app.gate, gate)
        self.assertTrue(app.execution.armed)
        self.assertTrue(gate.emitted)
        app.execution.submit.assert_called_once()
        app.summary_text.set.assert_called_once_with("Intentos: 1\nPerdidas de seguridad: 1/3")
        self.assertIn("tiempo vencido", app.order_status.set.call_args.args[0])
        self.assertEqual(app.root.after.call_count, 2)
        error.assert_not_called()
        app.execution.submit.side_effect = None
        app.schedule_results = Mock()
        with (
            patch("bot.ImageGrab.grab") as grab,
            patch("bot.list_monitors", return_value=app.monitors),
            patch("bot.ImageTk.PhotoImage"),
            patch("bot.time.time") as clock,
            patch("bot.write_event"),
        ):
            for index, direction in enumerate((None, None, "PUT", "PUT", "PUT")):
                grab.return_value = self.chart([30, 70, 110], direction)
                clock.return_value = 1_800_000_301 + index * 0.2
                app.tick()
        self.assertEqual(app.execution.submit.call_count, 2)
        self.assertEqual(app.execution.submit.call_args.args[0], "PUT")
        app.schedule_results.assert_called_once()
        self.assertTrue(app.execution.armed)

    def test_rejected_colored_shape_does_not_rearm_as_absence(self):
        app = self.ready_app()
        app.gate.armed = False
        app.gate.absent = 1
        def rejected(image, templates, band, diagnostics):
            diagnostics.append("PUT: forma no coincide")
            return None
        with patch("bot.detect", side_effect=rejected):
            self.run_signal_tick(app)
        self.assertEqual(app.gate.absent, 0)
        self.assertFalse(app.gate.armed)
        self.assertEqual(
            app.signal_text.set.call_args.args[0],
            "FIGURA DETECTADA / SENAL NO VALIDADA",
        )
        app.execution.submit.assert_not_called()


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.app = App.__new__(App)
        self.app.stop = Mock()
        self.app.status = Mock()
        self.app.browser = Mock()

    def test_opens_traderoom_and_stops_detector(self):
        self.app.open_iq_option()
        self.app.browser.open.assert_called_once()
        self.app.stop.assert_called_once_with()
        self.assertIn("solicitada", self.app.status.set.call_args.args[0])

    @patch("bot.messagebox.showerror")
    def test_rejected_open_shows_error(self, error):
        self.app.browser.open.side_effect = RuntimeError("Browser unavailable")
        self.app.open_iq_option()
        error.assert_called_once()
        self.assertIn("No se pudo", self.app.status.set.call_args.args[0])

    @patch("bot.messagebox.showerror")
    def test_browser_failure_shows_error(self, error):
        self.app.browser.open.side_effect = OSError("Browser unavailable")
        self.app.open_iq_option()
        error.assert_called_once()
        self.assertIn("Browser unavailable", error.call_args.args[1])

    @patch("bot.sys.platform", "win32")
    @patch("bot.configure_diagnostics")
    @patch("bot.enable_physical_coordinates")
    @patch("bot.App")
    @patch("bot.tk.Tk")
    def test_startup_schedules_browser_after_window_creation(self, root_factory, app_factory, dpi, diagnostics):
        main()
        root_factory.return_value.after_idle.assert_called_once_with(
            app_factory.return_value.open_iq_option,
        )
        root_factory.return_value.mainloop.assert_called_once_with()
        diagnostics.return_value.close.assert_called_once()


class DiagnosticTests(unittest.TestCase):
    def test_tracking_lines_remain_visible_after_preview_downscaling(self):
        image = Image.new("RGB", (1600, 640), "#101827")
        for color, expected in (("white", (255, 255, 255)), ("yellow", (255, 255, 0))):
            preview = make_preview(image, (800, 880), color)
            self.assertEqual(preview.size, (400, 160))
            self.assertEqual(preview.getpixel((200, 80)), expected)
            self.assertEqual(preview.getpixel((201, 80)), expected)
            self.assertEqual(preview.getpixel((220, 80)), expected)

    def test_result_validation_is_scheduled_at_expiry_only_once(self):
        app = App.__new__(App)
        app.result_job = None
        app.root = Mock()
        app.ledger = Mock()
        app.ledger.pending.return_value = {"deadline": 1800000300}
        with patch("bot.time.time", return_value=1800000100):
            app.schedule_results()
            app.schedule_results()
        app.root.after.assert_called_once_with(200000, app.refresh_results)

    def test_expired_pending_result_is_rechecked_after_five_seconds(self):
        app = App.__new__(App)
        app.result_job = None
        app.root = Mock()
        app.ledger = Mock()
        app.ledger.pending.return_value = {"deadline": 1800000300}
        with patch("bot.time.time", return_value=1800000301):
            app.schedule_results()
        app.root.after.assert_called_once_with(5000, app.refresh_results)

    def test_no_result_query_is_scheduled_without_operation(self):
        app = App.__new__(App)
        app.result_job = None
        app.root = Mock()
        app.ledger = Mock()
        app.ledger.pending.return_value = None
        app.schedule_results()
        app.root.after.assert_not_called()

    def test_diagnostics_persist_after_handler_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "diagnostico.log"
            handler = configure_diagnostics(path)
            try:
                LOGGER.info("Senal CALL sin entrada: ejecucion desarmada")
            finally:
                LOGGER.removeHandler(handler)
                handler.close()
            self.assertIn("Senal CALL sin entrada", path.read_text(encoding="utf-8"))


class VisualTests(unittest.TestCase):
    def setUp(self):
        self.templates = {
            signal: template_from(arrow_image(signal), signal)
            for signal in ("CALL", "PUT")
        }

    def test_diagnostic_explains_size_mismatch_without_accepting(self):
        image = Image.new("RGB", (90, 80), "#101827")
        ImageDraw.Draw(image).polygon([(20, 10), (10, 30), (30, 30)], fill=COLORS["CALL"])
        diagnostics = []
        self.assertIsNone(detect(image, self.templates, diagnostics=diagnostics))
        self.assertIn("21x21", diagnostics[0])
        self.assertIn("17x17", diagnostics[0])
        self.assertIn("Tamano distinto", diagnostics[0])

    def test_minor_raster_edge_loss_remains_same_arrow(self):
        image = arrow_image()
        for width_loss, height_loss in ((1, 0), (0, 1), (1, 2)):
            actual = image.copy()
            draw = ImageDraw.Draw(actual)
            if width_loss:
                draw.line((26, 10, 26, 26), fill="#101827")
            if height_loss:
                draw.rectangle((10, 10, 26, 9+height_loss), fill="#101827")
            result = detect(actual, self.templates)
            self.assertIsNotNone(result)
            self.assertEqual(result.signal, "CALL")

    def test_narrow_10_and_16_by_27_arrows_keep_shape_validation(self):
        for signal in ("CALL", "PUT"):
            sample = Image.new("RGB", (60, 60), "#101827")
            ImageDraw.Draw(sample).polygon(
                [(21, 5), (10, 31), (32, 31)], fill=COLORS[signal],
            )
            template = template_from(sample, signal)
            for width in (10, 16, 22, 23):
                with self.subTest(signal=signal, width=width):
                    image = Image.new("RGB", (60, 60), "#101827")
                    image.paste(
                        sample.crop((10, 5, 33, 32)).resize(
                            (width, 27), Image.Resampling.NEAREST,
                        ), (10, 5),
                    )
                    result = detect(image, {signal: template})
                    self.assertIsNotNone(result)
                    self.assertEqual(result.signal, signal)
                    self.assertGreaterEqual(result.score, 0.88)
            for width, height in ((9, 27), (10, 24), (25, 27)):
                image = Image.new("RGB", (60, 60), "#101827")
                image.paste(
                    sample.crop((10, 5, 33, 32)).resize((width, height)), (10, 5),
                )
                self.assertIsNone(detect(image, {signal: template}))
            rectangle = Image.new("RGB", (60, 60), "#101827")
            ImageDraw.Draw(rectangle).rectangle((10, 5, 19, 31), fill=COLORS[signal])
            self.assertIsNone(detect(rectangle, {signal: template}))
            clipped = Image.new("RGB", (60, 60), "#101827")
            clipped.paste(sample.crop((17, 5, 33, 32)), (10, 5))
            result = detect(clipped, {signal: template})
            self.assertIsNotNone(result)
            self.assertEqual(result.signal, signal)
            self.assertGreaterEqual(result.score, 0.70)
            self.assertLess(result.score, 0.88)

    def test_visible_arrow_cannot_revalidate_at_next_candle(self):
        gate = SignalGate(60)
        gate.observe(1800000000, None)
        gate.observe(1800000000.5, None)
        for timestamp in (1800000057, 1800000058, 1800000059):
            gate.observe(timestamp, detect(arrow_image(), self.templates))
        self.assertTrue(gate.emitted)
        for timestamp in (1800000060, 1800000061, 1800000062):
            image = arrow_image()
            ImageDraw.Draw(image).line((26, 10, 26, 26), fill="#101827")
            self.assertFalse(gate.observe(timestamp, detect(image, self.templates)))
        self.assertFalse(gate.armed)

    def test_diagnostic_explains_shape_mismatch_without_accepting(self):
        image = Image.new("RGB", (90, 80), "#101827")
        ImageDraw.Draw(image).rectangle((10, 10, 26, 26), fill=COLORS["CALL"])
        diagnostics = []
        self.assertIsNone(detect(image, self.templates, diagnostics=diagnostics))
        self.assertIn("requiere 70%", diagnostics[0])
        self.assertIn("Forma/color", diagnostics[0])

    def test_shape_threshold_accepts_70_percent_but_not_below(self):
        for signal in ("CALL", "PUT"):
            for score in (0.6999, 0.70, 0.79):
                with self.subTest(signal=signal, score=score):
                    with patch("bot.similarity", return_value=score):
                        result = detect(arrow_image(signal), {signal: self.templates[signal]})
                    if score < 0.70:
                        self.assertIsNone(result)
                    else:
                        self.assertIsNotNone(result)
                        self.assertEqual(result.signal, signal)
                        self.assertEqual(result.score, score)

    def test_matching_arrow_has_no_rejection_diagnostic(self):
        diagnostics = []
        self.assertEqual(detect(arrow_image(), self.templates, diagnostics=diagnostics).signal, "CALL")
        self.assertEqual(diagnostics, [])

    def test_both_signals_and_translation(self):
        for signal in ("CALL", "PUT"):
            with self.subTest(signal=signal):
                result = detect(arrow_image(signal, (40, 35)), self.templates)
                self.assertIsNotNone(result)
                self.assertEqual(result.signal, signal)
                self.assertEqual(result.score, 1.0)

    def test_empty_and_candle_colors(self):
        image = Image.new("RGB", (90, 80), "#101827")
        draw = ImageDraw.Draw(image)
        draw.rectangle((10, 10, 30, 60), fill=(45, 158, 107))
        draw.rectangle((40, 10, 60, 60), fill=(228, 74, 78))
        self.assertIsNone(detect(image, self.templates))

    def test_same_color_rectangle_is_not_arrow(self):
        image = Image.new("RGB", (90, 80), "#101827")
        ImageDraw.Draw(image).rectangle((10, 10, 26, 26), fill=COLORS["CALL"])
        self.assertIsNone(detect(image, self.templates))

    def test_ambiguous_signals_stop(self):
        image = arrow_image("CALL")
        image.paste(arrow_image("PUT").crop((10, 10, 27, 27)), (50, 40))
        with self.assertRaisesRegex(RuntimeError, "simultaneos"):
            detect(image, self.templates)

    def test_multiple_same_direction_stop(self):
        image = arrow_image()
        image.paste(arrow_image().crop((10, 10, 27, 27)), (50, 40))
        with self.assertRaisesRegex(RuntimeError, "Varias"):
            detect(image, self.templates)

    def test_empty_sample_rejected(self):
        with self.assertRaises(ValueError):
            template_from(Image.new("RGB", (30, 30)), "CALL")

    def test_sample_with_label_extracts_arrow(self):
        for signal in ("CALL", "PUT"):
            image = Image.new("RGB", (100, 90), "#101827")
            draw = ImageDraw.Draw(image)
            draw.polygon([(30, 35), (18, 60), (42, 60)], fill=COLORS[signal])
            expected = template_from(image, signal)
            draw.text(
                (10, 5), signal, font=ImageFont.load_default(size=16),
                fill=COLORS[signal],
            )
            self.assertEqual(template_from(image, signal), expected)

    def test_sample_with_two_arrows_rejected(self):
        image = arrow_image()
        image.paste(arrow_image().crop((10, 10, 27, 27)), (50, 40))
        with self.assertRaisesRegex(ValueError, "varias figuras"):
            template_from(image, "CALL")

    def test_zoom_change_rejected(self):
        image = arrow_image().resize((180, 160))
        self.assertIsNone(detect(image, self.templates))


class GateTests(unittest.TestCase):
    def test_existing_signal_never_logged_on_start(self):
        gate = SignalGate(300)
        for timestamp in range(10, 20):
            self.assertFalse(gate.observe(timestamp, Detection("CALL", 1.0)))

    def test_debounce_and_one_event_per_interval(self):
        gate = SignalGate(300)
        self.assertFalse(gate.observe(1, None))
        self.assertFalse(gate.observe(2, None))
        self.assertFalse(gate.observe(3, Detection("CALL", 1)))
        self.assertFalse(gate.observe(4, Detection("CALL", 1)))
        self.assertTrue(gate.observe(5, Detection("CALL", 1)))
        for timestamp in range(6, 10):
            self.assertFalse(gate.observe(timestamp, Detection("PUT", 1)))

    def test_next_interval_requires_absence(self):
        gate = SignalGate(300)
        gate.observe(1, None)
        gate.observe(2, None)
        for timestamp in (3, 4, 5):
            gate.observe(timestamp, Detection("CALL", 1))
        self.assertFalse(gate.observe(300, Detection("CALL", 1)))
        gate.observe(301, None)
        gate.observe(302, None)
        self.assertFalse(gate.observe(303, Detection("PUT", 1)))
        self.assertFalse(gate.observe(304, Detection("PUT", 1)))
        self.assertTrue(gate.observe(305, Detection("PUT", 1)))

    def test_transient_or_changed_signal_does_not_pass(self):
        gate = SignalGate(300)
        gate.observe(1, None)
        gate.observe(2, None)
        self.assertFalse(gate.observe(3, Detection("CALL", 1)))
        self.assertFalse(gate.observe(4, Detection("PUT", 1)))
        self.assertFalse(gate.observe(5, None))

    def test_clock_and_period_errors(self):
        with self.assertRaises(ValueError):
            SignalGate(0)
        gate = SignalGate(300)
        gate.observe(10, None)
        with self.assertRaises(RuntimeError):
            gate.observe(9, None)
        with self.assertRaises(ValueError):
            gate.observe(float("nan"), None)


class LogTests(unittest.TestCase):
    def test_persistent_csv_and_no_duplicate_header(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.csv"
            for timestamp in (10, 310):
                write_event(path, "EUR/USD", 300, timestamp, Detection("CALL", 1))
            with path.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(rows[0], list(CSV_FIELDS))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[1][-1], "VISUAL_SIMULATION")
            self.assertEqual(rows[1][4], "CALL")

    def test_incompatible_csv_is_not_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.csv"
            path.write_text("another,format\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                write_event(path, "EUR/USD", 300, 10, Detection("CALL", 1))
            self.assertEqual(path.read_text(encoding="utf-8"), "another,format\n")


if __name__ == "__main__":
    unittest.main()
