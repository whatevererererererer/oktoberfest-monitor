from __future__ import annotations

import json
import time
import unittest
from unittest.mock import patch

import httpx

from src.config import ReservierungsmanagerConfig
from src.fetchers.reservierungsmanager import (
    WidgetSchemaError,
    _get_bounded,
    extract_widget_config,
    fetch,
    parse_event_days_payload,
)

TARGET = "2026-09-26"


def ticket(
    ticket_id: str = "8",
    name: str = "Mittags-Wiesn im Zelt",
    days: dict[str, list[list[str]]] | None = None,
) -> dict:
    if days is None:
        days = {"2026-09-26T00:00:00": [["1100", "1400"]]}
    return {
        "ticketTypeId": ticket_id,
        "ticketTypeName": name,
        "ticketMinPerson": "10",
        "ticketMaxPerson": "144",
        "availableDays": [{key: value} for key, value in days.items()],
    }


def widget_html(
    token: str,
    *,
    event_ids: str | None = None,
    dom_id: str = "portal-container",
    include_loader: bool = True,
) -> str:
    event_line = f"eventID: '{event_ids}'," if event_ids else ""
    loader = (
        '<script src="https://widget.reservierungsmanager.de/dist/latest/portal.js">'
        "</script>"
        if include_loader
        else ""
    )
    return f"""
    {loader}
    <div id="{dom_id}"></div>
    <script>window.logbyte.gateway({{
      widget: 'WidgetRequestEvent',
      src: document.getElementById('{dom_id}'),
      font: 'inherit',
      authToken: '{token}', {event_line} view: 'portal',
      theme: 'knoedelei', lang: 'DE'
    }});</script>
    """


class ReservierungsmanagerParserTests(unittest.TestCase):
    def test_target_is_available_with_normalized_midday_shift(self) -> None:
        result = parse_event_days_payload(
            {"error": False, "result": [ticket()]},
            TARGET,
            include_name_regex=r"im\s+Zelt",
            exclude_name_regex=r"Biergarten",
        )
        self.assertEqual(result.status, "available")
        self.assertEqual(result.shifts, ("Mittag (11:00–14:00, Zelt)",))

    def test_combined_shift_name_is_fail_closed(self) -> None:
        result = parse_event_days_payload(
            {
                "error": False,
                "result": [ticket(name="Mittag und Abend im Zelt")],
            },
            TARGET,
            include_name_regex=r"im\s+Zelt",
            exclude_name_regex=r"Biergarten",
        )
        self.assertEqual(result.status, "unknown")

    def test_valid_response_without_target_is_unavailable(self) -> None:
        result = parse_event_days_payload(
            {
                "error": False,
                "result": [
                    ticket(days={"2026-09-25T00:00:00": [["1100", "1400"]]})
                ],
            },
            TARGET,
            include_name_regex=r"im\s+Zelt",
            exclude_name_regex=r"Biergarten",
        )
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.shifts, ())

    def test_biergarten_and_unembedded_events_are_ignored(self) -> None:
        result = parse_event_days_payload(
            {
                "error": False,
                "result": [
                    ticket("8", days={"2026-09-25T00:00:00": [["1100", "1400"]]}),
                    ticket("13", "Mittags-Wiesn im Biergarten"),
                    ticket("999", "Abend-Wiesn im Zelt"),
                ],
            },
            TARGET,
            allowed_event_ids=("8", "13"),
            include_name_regex=r"im\s+Zelt",
            exclude_name_regex=r"Biergarten",
        )
        self.assertEqual(result.status, "unavailable")

    def test_only_explicitly_excluded_or_unembedded_tickets_is_unavailable(self) -> None:
        cases = (
            dict(
                payload={
                    "error": False,
                    "result": [ticket("13", "Mittags-Wiesn im Biergarten")],
                },
                include_name_regex=r"im\s+Zelt",
                exclude_name_regex=r"Biergarten",
            ),
            dict(
                payload={
                    "error": False,
                    "result": [ticket("160", "Gutschein")],
                },
                allowed_event_ids=("149",),
            ),
        )
        for case in cases:
            payload = case.pop("payload")
            with self.subTest(case=case):
                result = parse_event_days_payload(payload, TARGET, **case)
                self.assertEqual(result.status, "unavailable")

    def test_unknown_ticket_name_cannot_hide_target_availability(self) -> None:
        result = parse_event_days_payload(
            {
                "error": False,
                "result": [
                    ticket("8", days={"2026-09-25T00:00:00": [["1100", "1400"]]}),
                    ticket("14", "Nachmittags-Wiesn Festhalle"),
                ],
            },
            TARGET,
            include_name_regex=r"im\s+Zelt",
            exclude_name_regex=r"Biergarten",
        )
        self.assertEqual(result.status, "unknown")

    def test_duplicate_ticket_id_and_invalid_party_range_are_unknown(self) -> None:
        duplicate = {"error": False, "result": [ticket("8"), ticket("8")]}
        invalid_range_ticket = {
            **ticket(),
            "ticketMinPerson": "144",
            "ticketMaxPerson": "10",
        }
        for payload in (
            duplicate,
            {"error": False, "result": [invalid_range_ticket]},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(
                    parse_event_days_payload(payload, TARGET).status, "unknown"
                )

    def test_ticket_name_and_api_time_must_agree(self) -> None:
        for name in (
            "Abend-Wiesn im Zelt",
            "Mittags-Wiesn im Zelt 17:30–23:00",
        ):
            with self.subTest(name=name):
                result = parse_event_days_payload(
                    {"error": False, "result": [ticket(name=name)]},
                    TARGET,
                    include_name_regex=r"im\s+Zelt",
                    exclude_name_regex=r"Biergarten",
                )
                self.assertEqual(result.status, "unknown")

    def test_changed_or_empty_schema_is_unknown(self) -> None:
        cases = [
            {"error": True, "result": [ticket()]},
            {"error": False, "result": []},
            {"error": False, "result": [ticket(days={TARGET + "T00:00:00": []})]},
            {"error": False, "result": [{**ticket(), "ticketMinPerson": 10}]},
            {
                "error": False,
                "result": [
                    ticket(days={"2025-09-26T00:00:00": [["1100", "1400"]]})
                ],
            },
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(
                    parse_event_days_payload(payload, TARGET).status, "unknown"
                )

    def test_target_outside_festival_is_unknown(self) -> None:
        self.assertEqual(
            parse_event_days_payload(
                {"error": False, "result": [ticket()]}, "2027-09-26"
            ).status,
            "unknown",
        )

    def test_widget_config_is_strict_and_never_returns_token_in_diagnostics(self) -> None:
        token = "a" * 32
        config = extract_widget_config(widget_html(token), expected_theme="knoedelei")
        self.assertEqual(config.token, token)
        self.assertEqual(config.event_ids, ())
        with self.assertRaises(WidgetSchemaError):
            extract_widget_config(widget_html(token) * 2, expected_theme="knoedelei")

    def test_widget_parser_rejects_comments_strings_and_dynamic_overrides(self) -> None:
        token = "a" * 32
        valid = widget_html(token)
        inline_start = valid.index("<script>")
        inline_end = valid.index("</script>", inline_start) + len("</script>")
        commented_script = (
            "<script><!-- window.logbyte.gateway({widget:'WidgetRequestEvent',"
            f"authToken:'{token}',theme:'knoedelei',lang:'DE'}});</script>"
        )
        dynamic_script = f"""
        <script>window.logbyte.gateway({{
          payload: "theme: 'knoedelei', eventID: '149'",
          widget: 'WidgetRequestEvent', authToken: '{token}',
          ['theme']: activeTheme, ['eventID']: activeIds, lang: 'DE'
        }});</script>
        """
        commented = valid[:inline_start] + commented_script + valid[inline_end:]
        dynamic = valid[:inline_start] + dynamic_script + valid[inline_end:]
        for html in (commented, dynamic):
            with self.subTest(html=html), self.assertRaises(WidgetSchemaError):
                extract_widget_config(html, expected_theme="knoedelei")

        dynamic_source = widget_html(token).replace(
            "document.getElementById('portal-container')", "getPortalContainer()"
        )
        with self.assertRaises(WidgetSchemaError):
            extract_widget_config(dynamic_source, expected_theme="knoedelei")

    def test_only_active_literal_inline_script_is_evidence(self) -> None:
        token = "a" * 32
        valid = widget_html(token)
        gateway = valid[valid.index("<script>") :]
        cases = (
            valid.replace("<script>", '<script type="application/json">'),
            valid.replace("<script>", '<script src="actual.js">'),
            valid.replace("<script>", "<script nomodule>"),
            valid.replace(gateway, f"<template>{gateway}</template>"),
            valid.replace(
                gateway,
                gateway.replace("<script>", "<script>/").replace(
                    "</script>", "/;</script>"
                ),
            ),
        )
        for html in cases:
            with self.subTest(html=html), self.assertRaises(WidgetSchemaError):
                extract_widget_config(html, expected_theme="knoedelei")

    def test_widget_source_must_resolve_to_exactly_one_live_dom_node(self) -> None:
        token = "a" * 32
        missing = widget_html(token).replace('<div id="portal-container"></div>', "")
        duplicate = widget_html(token).replace(
            '<div id="portal-container"></div>',
            '<div id="portal-container"></div><div id="portal-container"></div>',
        )
        inert = widget_html(token).replace(
            '<div id="portal-container"></div>',
            '<template><div id="portal-container"></div></template>',
        )
        without_target = widget_html(token).replace(
            '<div id="portal-container"></div>', ""
        )
        before_close, close = without_target.rsplit("</script>", 1)
        after_script = (
            before_close
            + "</script>"
            + '<div id="portal-container"></div>'
            + close
        )
        wrong_element = widget_html(token).replace(
            '<div id="portal-container"></div>',
            '<span id="portal-container"></span>',
        )
        for html in (missing, duplicate, inert, after_script, wrong_element):
            with self.subTest(html=html), self.assertRaises(WidgetSchemaError):
                extract_widget_config(html, expected_theme="knoedelei")

    def test_official_loader_must_be_active_unique_and_before_gateway(self) -> None:
        token = "a" * 32
        valid = widget_html(token)
        loader = (
            '<script src="https://widget.reservierungsmanager.de/dist/latest/'
            'portal.js"></script>'
        )
        missing = valid.replace(loader, "")
        after = valid.replace(loader, "").replace("</script>", "</script>" + loader)
        wrong_host = valid.replace(
            "https://widget.reservierungsmanager.de/",
            "https://attacker.example/",
        )
        inert = valid.replace(loader, f"<template>{loader}</template>")
        attributed = valid.replace("<script src=", "<script defer src=")
        duplicate = valid.replace(loader, loader + loader)
        for html in (missing, after, wrong_host, inert, attributed, duplicate):
            with self.subTest(html=html), self.assertRaises(WidgetSchemaError):
                extract_widget_config(html, expected_theme="knoedelei")

    def test_foreign_widget_does_not_make_target_ambiguous(self) -> None:
        target = widget_html("a" * 32)
        foreign = widget_html(
            "b" * 32,
            dom_id="foreign-container",
            include_loader=False,
        ).replace(
            "theme: 'knoedelei'", "theme: 'some-other-tent'"
        )
        config = extract_widget_config(target + foreign, expected_theme="knoedelei")
        self.assertEqual(config.token, "a" * 32)


class ReservierungsmanagerFetchTests(unittest.TestCase):
    def test_config_rejects_unsafe_endpoint_and_invalid_filters(self) -> None:
        base = dict(
            landing_url="https://official.example/reservierung",
            event_days_endpoint=(
                "https://api.reservierungsmanager.de/event-days/get/"
            ),
            expected_theme="knoedelei",
        )
        invalid_overrides = (
            {"landing_url": "http://official.example/reservierung"},
            {
                "event_days_endpoint": (
                    "http://api.reservierungsmanager.de/event-days/get/"
                )
            },
            {"event_days_endpoint": "https://foreign.example/event-days/get/"},
            {"include_name_regex": "[", "exclude_name_regex": "Biergarten"},
            {"include_name_regex": "Zelt"},
        )
        for override in invalid_overrides:
            with self.subTest(override=override), self.assertRaises(ValueError):
                ReservierungsmanagerConfig(**{**base, **override})

    def test_fetch_uses_get_only_and_refreshes_once_after_401(self) -> None:
        tokens = ("a" * 32, "b" * 32)
        calls: list[tuple[str, str | None]] = []
        landing_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal landing_count
            self.assertEqual(request.method, "GET")
            auth = request.headers.get("Authorization")
            calls.append((str(request.url), auth))
            if request.url.host == "official.example":
                token = tokens[landing_count]
                landing_count += 1
                return httpx.Response(200, text=widget_html(token))
            if auth == f"Bearer {tokens[0]}":
                return httpx.Response(401)
            return httpx.Response(
                200,
                json={"error": False, "result": [ticket()]},
            )

        cfg = ReservierungsmanagerConfig(
            landing_url="https://official.example/reservierung",
            event_days_endpoint="https://api.reservierungsmanager.de/event-days/get/",
            expected_theme="knoedelei",
            include_name_regex=r"im\s+Zelt",
            exclude_name_regex=r"Biergarten",
        )
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = fetch(cfg, TARGET, client)
        self.assertEqual(result.status, "available")
        self.assertEqual(len(calls), 4)
        self.assertNotIn(tokens[0], str(result.diagnostic_dict()))
        self.assertNotIn(tokens[1], str(result.diagnostic_dict()))

    def test_landing_redirect_is_never_followed(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(
                302,
                headers={"Location": "http://attacker.example/widget"},
            )

        cfg = ReservierungsmanagerConfig(
            landing_url="https://official.example/reservierung",
            event_days_endpoint=(
                "https://api.reservierungsmanager.de/event-days/get/"
            ),
            expected_theme="knoedelei",
        )
        with httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                fetch(cfg, TARGET, client)
        self.assertEqual(calls, ["https://official.example/reservierung"])

    def test_empty_body_still_checks_wall_clock_deadline(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            time.sleep(0.01)
            return httpx.Response(200, content=b"")

        with httpx.Client(transport=httpx.MockTransport(handler)) as client, patch(
            "src.fetchers.reservierungsmanager._MAX_REQUEST_SECONDS", 0.001
        ):
            with self.assertRaisesRegex(WidgetSchemaError, "deadline exceeded"):
                _get_bounded(
                    client,
                    "https://official.example/",
                    headers={},
                    limit=100,
                    overall_deadline=time.monotonic() + 1,
                )

    def test_event_ids_are_appended_and_filter_extra_api_events(self) -> None:
        token = "a" * 32
        seen_api_url = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_api_url
            if request.url.host == "official.example":
                html = widget_html(token, event_ids="149,151")
                html = html.replace("theme: 'knoedelei'", "theme: 'ammerwiesn'")
                return httpx.Response(200, text=html)
            seen_api_url = str(request.url)
            return httpx.Response(
                200,
                json={
                    "error": False,
                    "result": [
                        ticket("149", "Wiesnreservierung Mittag"),
                        ticket("999", "Wiesnreservierung Abend"),
                    ],
                },
            )

        cfg = ReservierungsmanagerConfig(
            landing_url="https://official.example/reservierung",
            event_days_endpoint=(
                "https://api.reservierungsmanager.de/event-days/get/{event_ids}"
            ),
            expected_theme="ammerwiesn",
        )
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = fetch(cfg, TARGET, client)
        self.assertEqual(result.status, "available")
        self.assertEqual(result.shifts, ("Mittag (11:00–14:00)",))
        self.assertTrue(seen_api_url.endswith("/149,151"), seen_api_url)


if __name__ == "__main__":
    unittest.main()
