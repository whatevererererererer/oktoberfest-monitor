from __future__ import annotations

import os
import importlib.util
import sys
import types
import unittest
from unittest.mock import patch

if importlib.util.find_spec("httpx") is None:
    httpx_stub = types.ModuleType("httpx")
    httpx_stub.Client = object
    sys.modules["httpx"] = httpx_stub

from src.notification_policy import needs_notification_burst
from src.notify import alert_available


class NotificationPolicyTests(unittest.TestCase):
    def test_saturday_only_mittag_is_a_normal_alert(self) -> None:
        self.assertFalse(needs_notification_burst("2026-09-26", ["Mittag"]))

    def test_saturday_other_shifts_use_a_burst(self) -> None:
        self.assertTrue(needs_notification_burst("2026-09-26", ["Mittag", "Nachmittag"]))
        self.assertTrue(needs_notification_burst("2026-09-26", ["Abend"]))
        self.assertTrue(needs_notification_burst("2026-09-26", ["Ganztag"]))

    def test_friday_mittag_and_nachmittag_are_normal_alerts(self) -> None:
        self.assertFalse(needs_notification_burst("2026-09-25", ["Mittag"]))
        self.assertFalse(needs_notification_burst("2026-09-25", ["Nachmittag"]))
        self.assertFalse(needs_notification_burst("2026-09-25", ["Mittag", "Nachmittag"]))

    def test_friday_other_shifts_use_a_burst(self) -> None:
        self.assertTrue(needs_notification_burst("2026-09-25", ["Abend"]))
        self.assertTrue(needs_notification_burst("2026-09-25", ["Vormittag"]))

    def test_shift_names_may_include_times(self) -> None:
        self.assertFalse(needs_notification_burst("2026-09-26", ["Mittag (11:00 Uhr)"]))
        self.assertFalse(needs_notification_burst("2026-09-25", ["Nachmittag 15:30 Uhr"]))


class NotificationBurstTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(
            os.environ,
            {"PUSHOVER_TOKEN": "token", "PUSHOVER_USER": "user"},
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()

    @patch("src.notify.time.sleep")
    @patch("src.notify._post")
    def test_burst_sends_two_groups_of_four(self, post, sleep) -> None:
        alert_available(
            tent_name="Testzelt",
            tent_slug="testzelt",
            iso_date="2026-09-26",
            booking_url="https://example.com/book",
            shifts=["Nachmittag"],
            burst=True,
        )

        self.assertEqual(post.call_count, 8)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [5, 5, 5, 30, 5, 5, 5],
        )

    @patch("src.notify.time.sleep")
    @patch("src.notify._post")
    def test_normal_alert_stays_single(self, post, sleep) -> None:
        alert_available(
            tent_name="Testzelt",
            tent_slug="testzelt",
            iso_date="2026-09-26",
            booking_url="https://example.com/book",
            shifts=["Mittag"],
        )

        post.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
