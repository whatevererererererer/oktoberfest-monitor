from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.config import KaeferConfig
from src.fetchers.kaefer import fetch
from src.fetchers.kaefer import parse_slot_payload

TARGET = "2026-09-26"


def row(
    slot_id: int,
    *,
    inside: int = 0,
    outside: int = 0,
    day_count: int = 0,
    inside_sizes=None,
    outside_sizes=None,
) -> dict:
    times = {
        0: ("11:30:00", "15:00:00"),
        1: ("15:30:00", "19:00:00"),
    }
    start, end = times[slot_id]
    return {
        "slot_id": 1049 + slot_id,
        "zeit_ID": slot_id,
        "anz": inside,
        "anzBereich": outside,
        "anzDat": day_count,
        "bereich": "Haus innen",
        "tische": inside_sizes,
        "bereich1": "Überdachter Freisitz",
        "tische1": outside_sizes,
        "rDatum": TARGET + "T00:00:00",
        "res_ab": start,
        "res_bis": end,
    }


class KaeferParserTests(unittest.TestCase):
    def test_complete_zero_capacity_target_is_unavailable(self) -> None:
        result = parse_slot_payload(
            [row(0, inside_sizes="6,8"), row(1, outside_sizes="8,10")],
            TARGET,
        )
        self.assertEqual(result.status, "unavailable")
        self.assertTrue(result.diagnostics.unavailable_confirmed)

    def test_positive_capacity_with_sizes_is_available(self) -> None:
        result = parse_slot_payload(
            [
                row(0, inside=1, day_count=1, inside_sizes="8,10"),
                row(1, day_count=1),
            ],
            TARGET,
        )
        self.assertEqual(result.status, "available")
        self.assertEqual(result.shifts, ("Mittag (11:30–15:00, Haus innen)",))

    def test_positive_outside_capacity_is_labeled(self) -> None:
        result = parse_slot_payload(
            [
                row(0, day_count=2),
                row(1, outside=2, day_count=2, outside_sizes="10,12"),
            ],
            TARGET,
        )
        self.assertEqual(result.status, "available")
        self.assertEqual(
            result.shifts,
            ("Nachmittag (15:30–19:00, Überdachter Freisitz)",),
        )

    def test_incomplete_or_contradictory_target_is_unknown(self) -> None:
        cases = [
            [row(0)],
            [row(0, inside=1, day_count=1), row(1, day_count=1)],
            [row(0, inside=1, day_count=0, inside_sizes="8"), row(1)],
            [row(0, day_count=1), row(1, day_count=0)],
            [row(0), row(0), row(1)],
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(parse_slot_payload(payload, TARGET).status, "unknown")

    def test_target_slot_identity_requires_strict_stable_integers(self) -> None:
        cases = []

        missing_slot_id = [row(0), row(1)]
        missing_slot_id[0].pop("slot_id")
        cases.append(missing_slot_id)

        wrong_slot_id = [row(0), row(1)]
        wrong_slot_id[1]["slot_id"] = 9999
        cases.append(wrong_slot_id)

        float_slot_id = [row(0), row(1)]
        float_slot_id[0]["slot_id"] = 1049.0
        cases.append(float_slot_id)

        boolean_time_ids = [row(0), row(1)]
        boolean_time_ids[0]["zeit_ID"] = False
        boolean_time_ids[1]["zeit_ID"] = True
        cases.append(boolean_time_ids)

        float_time_id = [row(0), row(1)]
        float_time_id[1]["zeit_ID"] = 1.0
        cases.append(float_time_id)

        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(parse_slot_payload(payload, TARGET).status, "unknown")


class KaeferFetchTests(unittest.TestCase):
    def test_fetch_captures_only_the_configured_get_and_closes_context(self) -> None:
        payload = [row(0), row(1)]
        page = MagicMock()

        def completed_body() -> bytes:
            finished_context.__exit__.assert_called_once()
            return json.dumps(payload).encode("utf-8")

        response = SimpleNamespace(
            request=SimpleNamespace(
                method="GET", url="https://api.example//api/slot"
            ),
            url="https://api.example//api/slot",
            status=200,
            headers={
                "content-type": "application/json; charset=utf-8",
                "content-length": "1234",
            },
            body=completed_body,
        )
        response_info = SimpleNamespace(value=response)
        response_context = MagicMock()
        response_context.__enter__.return_value = response_info
        response_context.__exit__.return_value = False
        finished_context = MagicMock()
        finished_context.__enter__.return_value = SimpleNamespace(
            value=response.request
        )
        finished_context.__exit__.return_value = False
        page.expect_response.return_value = response_context
        page.expect_request_finished.return_value = finished_context
        context = MagicMock()
        context.new_page.return_value = page
        browser = MagicMock()
        browser.new_context.return_value = context
        cfg = KaeferConfig(
            url_template="https://official.example/",
            slot_endpoint="https://api.example/api/slot",
        )

        result = fetch(cfg, TARGET, browser)

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(
            browser.new_context.call_args.kwargs["service_workers"], "block"
        )
        context.route.assert_called_once()
        predicate = page.expect_response.call_args.args[0]
        self.assertTrue(predicate(response))
        self.assertFalse(
            predicate(
                SimpleNamespace(
                    request=SimpleNamespace(method="POST"),
                    url=response.url,
                )
            )
        )
        finished_predicate = page.expect_request_finished.call_args.args[0]
        self.assertTrue(finished_predicate(response.request))
        page.goto.assert_called_once_with(
            cfg.url_template,
            wait_until=cfg.wait_until,
            timeout=cfg.navigation_timeout_ms,
        )
        page.wait_for_load_state.assert_not_called()
        context.close.assert_called_once_with()

    def test_fetch_rejects_oversized_content_length_before_reading_body(self) -> None:
        response = SimpleNamespace(
            request=SimpleNamespace(
                method="GET", url="https://api.example/api/slot"
            ),
            url="https://api.example/api/slot",
            status=200,
            headers={
                "content-type": "application/json",
                "content-length": "1000001",
            },
            body=MagicMock(),
        )
        response_context = MagicMock()
        response_context.__enter__.return_value = SimpleNamespace(value=response)
        response_context.__exit__.return_value = False
        finished_context = MagicMock()
        finished_context.__enter__.return_value = SimpleNamespace(
            value=response.request
        )
        finished_context.__exit__.return_value = False
        page = MagicMock()
        page.expect_response.return_value = response_context
        page.expect_request_finished.return_value = finished_context
        context = MagicMock()
        context.new_page.return_value = page
        browser = MagicMock()
        browser.new_context.return_value = context
        cfg = KaeferConfig(
            url_template="https://official.example/",
            slot_endpoint="https://api.example/api/slot",
        )

        with self.assertRaisesRegex(ValueError, "too large"):
            fetch(cfg, TARGET, browser)

        response.body.assert_not_called()
        page.wait_for_load_state.assert_not_called()
        context.close.assert_called_once_with()

    def test_fetch_ignores_unrelated_get_longpoll_after_slot_finishes(self) -> None:
        payload = [row(0), row(1)]
        response = SimpleNamespace(
            request=SimpleNamespace(
                method="GET", url="https://api.example/api/slot"
            ),
            url="https://api.example/api/slot",
            status=200,
            headers={"content-type": "application/json"},
            body=MagicMock(return_value=json.dumps(payload).encode("utf-8")),
        )
        response_context = MagicMock()
        response_context.__enter__.return_value = SimpleNamespace(value=response)
        response_context.__exit__.return_value = False
        finished_context = MagicMock()
        finished_context.__enter__.return_value = SimpleNamespace(
            value=response.request
        )
        finished_context.__exit__.return_value = False
        page = MagicMock()
        page.expect_response.return_value = response_context
        page.expect_request_finished.return_value = finished_context
        context = MagicMock()
        context.new_page.return_value = page
        longpoll_route = MagicMock()

        def emit_unrelated_get(*_args, **_kwargs) -> None:
            route_handler = context.route.call_args.args[1]
            route_handler(
                longpoll_route,
                SimpleNamespace(
                    method="GET", url="https://telemetry.example/longpoll"
                ),
            )

        page.goto.side_effect = emit_unrelated_get
        browser = MagicMock()
        browser.new_context.return_value = context
        cfg = KaeferConfig(
            url_template="https://official.example/",
            slot_endpoint="https://api.example/api/slot",
            slot_timeout_ms=3210,
        )

        result = fetch(cfg, TARGET, browser)

        self.assertEqual(result.status, "unavailable")
        page.expect_request_finished.assert_called_once()
        self.assertEqual(
            page.expect_request_finished.call_args.kwargs["timeout"], 3210
        )
        longpoll_route.continue_.assert_called_once_with()
        longpoll_route.abort.assert_not_called()
        page.wait_for_load_state.assert_not_called()
        response.body.assert_called_once_with()
        context.close.assert_called_once_with()

    def test_fetch_blocks_post_during_navigation_and_fails_closed(self) -> None:
        response = SimpleNamespace(
            request=SimpleNamespace(
                method="GET", url="https://api.example/api/slot"
            ),
            url="https://api.example/api/slot",
            status=200,
            headers={"content-type": "application/json"},
            body=MagicMock(),
        )
        response_context = MagicMock()
        response_context.__enter__.return_value = SimpleNamespace(value=response)
        response_context.__exit__.return_value = False
        finished_context = MagicMock()
        finished_context.__enter__.return_value = SimpleNamespace(
            value=response.request
        )
        finished_context.__exit__.return_value = False
        page = MagicMock()
        page.expect_response.return_value = response_context
        page.expect_request_finished.return_value = finished_context
        context = MagicMock()
        context.new_page.return_value = page
        blocked_route = MagicMock()

        def emit_post(*_args, **_kwargs) -> None:
            route_handler = context.route.call_args.args[1]
            route_handler(blocked_route, SimpleNamespace(method="POST"))

        page.goto.side_effect = emit_post
        browser = MagicMock()
        browser.new_context.return_value = context
        cfg = KaeferConfig(
            url_template="https://official.example/",
            slot_endpoint="https://api.example/api/slot",
        )

        with self.assertRaisesRegex(ValueError, "non-GET request blocked"):
            fetch(cfg, TARGET, browser)

        self.assertEqual(
            browser.new_context.call_args.kwargs["service_workers"], "block"
        )
        blocked_route.abort.assert_called_once_with()
        blocked_route.continue_.assert_not_called()
        response.body.assert_not_called()
        context.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
