from __future__ import annotations

import os
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

if importlib.util.find_spec("httpx") is None:
    httpx_stub = types.ModuleType("httpx")
    httpx_stub.Client = object
    sys.modules["httpx"] = httpx_stub

from src.notification_policy import needs_notification_burst
import httpx

from src.notify import PushoverDeliveryError, alert_available, build_payload, send_event_part
from src.state import OutboxEvent


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

    def test_combined_shift_label_is_not_mistaken_for_saturday_mittag_only(self) -> None:
        self.assertTrue(
            needs_notification_burst("2026-09-26", ["Mittag / Nachmittag"])
        )
        self.assertFalse(
            needs_notification_burst("2026-09-25", ["Mittag / Nachmittag"])
        )


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


class NotificationTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(
            os.environ,
            {"PUSHOVER_TOKEN": "token", "PUSHOVER_USER": "user"},
        )
        self.env.start()
        self.event = OutboxEvent(
            event_id="event",
            tent_slug="test",
            tent_name="Testzelt",
            iso_date="2026-09-26",
            booking_url="https://example.com/book",
            shifts=["Mittag"],
            new_shifts=["Mittag"],
            created_at="2026-08-04T00:00:00+00:00",
        )

    def tearDown(self) -> None:
        self.env.stop()

    def test_message_time_uses_europe_berlin(self) -> None:
        payload = build_payload(
            self.event,
            now=datetime(2026, 8, 4, 12, 34, tzinfo=timezone.utc),
        )
        self.assertIn("14:34", payload["message"])

    def test_success_requires_json_status_and_captures_metadata(self) -> None:
        response = unittest.mock.Mock()
        response.status_code = 200
        response.headers = httpx.Headers(
            {
                "X-Pushover-Request": "header-request",
                "X-Limit-App-Limit": "10000",
                "X-Limit-App-Remaining": "9999",
                "X-Limit-App-Reset": "1800000000",
            }
        )
        response.json.return_value = {"status": 1, "request": "body-request"}
        client = unittest.mock.Mock()
        client.post.return_value = response
        result = send_event_part(self.event, client=client)
        self.assertEqual(result.request_id, "body-request")
        self.assertEqual((result.quota_limit, result.quota_remaining), (10000, 9999))

    def test_4xx_is_terminal(self) -> None:
        response = unittest.mock.Mock(status_code=403, headers=httpx.Headers())
        client = unittest.mock.Mock()
        client.post.return_value = response
        with self.assertRaises(PushoverDeliveryError) as raised:
            send_event_part(self.event, client=client)
        self.assertEqual(raised.exception.failure_class, "terminal")

    def test_429_is_rate_limited(self) -> None:
        response = unittest.mock.Mock(
            status_code=429,
            headers=httpx.Headers({"Retry-After": "30"}),
        )
        client = unittest.mock.Mock()
        client.post.return_value = response
        with self.assertRaises(PushoverDeliveryError) as raised:
            send_event_part(self.event, client=client)
        self.assertEqual(raised.exception.failure_class, "rate_limited")
        self.assertIsNotNone(raised.exception.retry_at)

    def test_5xx_and_timeout_are_retryable(self) -> None:
        response = unittest.mock.Mock(status_code=503, headers=httpx.Headers())
        client = unittest.mock.Mock()
        client.post.return_value = response
        with self.assertRaises(PushoverDeliveryError) as server_error:
            send_event_part(self.event, client=client)
        self.assertEqual(server_error.exception.failure_class, "retryable")

        client.post.side_effect = httpx.ReadTimeout("timeout")
        with self.assertRaises(PushoverDeliveryError) as timeout_error:
            send_event_part(self.event, client=client)
        self.assertEqual(timeout_error.exception.failure_class, "retryable")

        client.post.side_effect = httpx.RemoteProtocolError("connection closed")
        with self.assertRaises(PushoverDeliveryError) as protocol_error:
            send_event_part(self.event, client=client)
        self.assertEqual(protocol_error.exception.failure_class, "retryable")


if __name__ == "__main__":
    unittest.main()
