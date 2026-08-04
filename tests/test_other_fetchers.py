from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.config import ApiConfig, HashConfig, HeadlessConfig, HtmlConfig
from src.fetchers import api, hash as hash_fetcher, headless, html


class Response:
    def __init__(self, *, text: str = "", data=None) -> None:
        self.text = text
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._data


class LegacyFetcherSafetyTests(unittest.TestCase):
    def test_api_missing_predicate_path_is_unknown_not_opposite(self) -> None:
        client = Mock()
        client.get.return_value = Response(data={"status": "new-schema"})
        cfg = ApiConfig(endpoint="https://example.com/api", unavailable_when="$.sold_out")
        self.assertEqual(api.fetch(cfg, "2026-09-25", client), "unknown")

    def test_api_contradictory_predicates_are_rejected(self) -> None:
        client = Mock()
        client.get.return_value = Response(data={"available": True, "sold_out": True})
        cfg = ApiConfig(
            endpoint="https://example.com/api",
            available_when="$.available",
            unavailable_when="$.sold_out",
        )
        with self.assertRaises(ValueError):
            api.fetch(cfg, "2026-09-25", client)

    def test_html_no_marker_is_unknown_and_bot_page_is_error(self) -> None:
        client = Mock()
        cfg = HtmlConfig(
            url_template="https://example.com",
            available_regex="verfügbar",
            unavailable_regex="ausverkauft",
        )
        client.get.return_value = Response(text="<html><body>Willkommen</body></html>")
        self.assertEqual(html.fetch(cfg, "2026-09-25", client), "unknown")
        client.get.return_value = Response(text="<html><body>Cloudflare CAPTCHA</body></html>")
        with self.assertRaises(ValueError):
            html.fetch(cfg, "2026-09-25", client)

    def test_hash_missing_or_empty_region_is_error(self) -> None:
        client = Mock()
        client.get.return_value = Response(text="<html><body><div id='x'></div></body></html>")
        with self.assertRaises(ValueError):
            hash_fetcher.fetch_hash(
                HashConfig(url_template="https://example.com", selector="#missing"),
                "2026-09-25",
                client,
            )
        with self.assertRaises(ValueError):
            hash_fetcher.fetch_hash(
                HashConfig(url_template="https://example.com", selector="#x"),
                "2026-09-25",
                client,
            )

    def test_headless_no_marker_is_unknown(self) -> None:
        page = Mock()
        page.locator.return_value.inner_text.return_value = "Willkommen"
        context = Mock()
        context.new_page.return_value = page
        browser = Mock()
        browser.new_context.return_value = context
        cfg = HeadlessConfig(
            url_template="https://example.com",
            wait_extra_ms=0,
            available_regex="verfügbar",
            unavailable_regex="ausverkauft",
        )
        self.assertEqual(headless.fetch(cfg, "2026-09-25", browser), "unknown")
        self.assertEqual(
            browser.new_context.call_args.kwargs,
            {
                "user_agent": headless.SAFARI_MACOS_USER_AGENT,
                "locale": "de-DE",
            },
        )
        context.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
