from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

if importlib.util.find_spec("httpx") is None:
    httpx_stub = types.ModuleType("httpx")
    httpx_stub.Client = object
    sys.modules["httpx"] = httpx_stub

import httpx

import src.notify as notify
from src.notification_policy import needs_notification_burst
from src.notify import PushoverDeliveryError, build_payload, send_event_part
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

    def test_only_durable_outbox_transport_is_exposed(self) -> None:
        self.assertFalse(hasattr(notify, "alert_available"))
        self.assertFalse(hasattr(notify, "alert_error"))
        self.assertFalse(hasattr(notify, "_post_burst"))

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
        now = datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)
        response = unittest.mock.Mock(
            status_code=429,
            headers=httpx.Headers({"Retry-After": "30"}),
        )
        client = unittest.mock.Mock()
        client.post.return_value = response
        with self.assertRaises(PushoverDeliveryError) as raised:
            send_event_part(self.event, client=client, now=now)
        self.assertEqual(raised.exception.failure_class, "rate_limited")
        self.assertEqual(raised.exception.retry_at, now + timedelta(seconds=30))
        self.assertEqual(raised.exception.retry_after_seconds, 30)

    def test_429_uses_later_quota_reset_and_captures_headers(self) -> None:
        now = datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)
        reset = int((now + timedelta(minutes=3)).timestamp())
        response = unittest.mock.Mock(
            status_code=429,
            headers=httpx.Headers(
                {
                    "Retry-After": "30",
                    "X-Limit-App-Limit": "10000",
                    "X-Limit-App-Remaining": "0",
                    "X-Limit-App-Reset": str(reset),
                }
            ),
        )
        client = unittest.mock.Mock()
        client.post.return_value = response

        with self.assertRaises(PushoverDeliveryError) as raised:
            send_event_part(self.event, client=client, now=now)

        error = raised.exception
        self.assertEqual(error.retry_at, now + timedelta(minutes=3))
        self.assertEqual(
            (error.quota_limit, error.quota_remaining, error.quota_reset),
            (10000, 0, reset),
        )

    def test_429_past_reset_has_a_deterministic_minimum_delay(self) -> None:
        now = datetime(2026, 8, 4, 8, 0, 0, 900000, tzinfo=timezone.utc)
        response = unittest.mock.Mock(
            status_code=429,
            headers=httpx.Headers(
                {
                    "X-Limit-App-Remaining": "0",
                    "X-Limit-App-Reset": str(int(now.timestamp()) - 10),
                }
            ),
        )
        client = unittest.mock.Mock()
        client.post.return_value = response

        with self.assertRaises(PushoverDeliveryError) as raised:
            send_event_part(self.event, client=client, now=now)

        self.assertEqual(raised.exception.retry_at, now + timedelta(seconds=5))

    def test_429_without_usable_headers_uses_bounded_default_delay(self) -> None:
        now = datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)
        response = unittest.mock.Mock(
            status_code=429,
            headers=httpx.Headers({"Retry-After": "not-a-date"}),
        )
        client = unittest.mock.Mock()
        client.post.return_value = response

        with self.assertRaises(PushoverDeliveryError) as raised:
            send_event_part(self.event, client=client, now=now)

        self.assertEqual(raised.exception.retry_at, now + timedelta(seconds=60))

    def test_429_huge_reset_is_ignored_without_overflow(self) -> None:
        now = datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)
        response = unittest.mock.Mock(
            status_code=429,
            headers=httpx.Headers(
                {
                    "X-Limit-App-Remaining": "0",
                    "X-Limit-App-Reset": str(10**100),
                }
            ),
        )
        client = unittest.mock.Mock()
        client.post.return_value = response

        with self.assertRaises(PushoverDeliveryError) as raised:
            send_event_part(self.event, client=client, now=now)

        self.assertEqual(raised.exception.failure_class, "rate_limited")
        self.assertEqual(raised.exception.retry_at, now + timedelta(seconds=60))
        self.assertIsNone(raised.exception.quota_reset)

    def test_429_positive_remaining_does_not_wait_for_quota_reset(self) -> None:
        now = datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)
        reset = int((now + timedelta(hours=1)).timestamp())
        response = unittest.mock.Mock(
            status_code=429,
            headers=httpx.Headers(
                {
                    "Retry-After": "30",
                    "X-Limit-App-Limit": "10000",
                    "X-Limit-App-Remaining": "5",
                    "X-Limit-App-Reset": str(reset),
                }
            ),
        )
        client = unittest.mock.Mock()
        client.post.return_value = response

        with self.assertRaises(PushoverDeliveryError) as raised:
            send_event_part(self.event, client=client, now=now)

        self.assertEqual(raised.exception.retry_at, now + timedelta(seconds=30))
        self.assertEqual(raised.exception.quota_remaining, 5)

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
