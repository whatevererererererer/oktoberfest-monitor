from __future__ import annotations

import re

import httpx
from selectolax.parser import HTMLParser

from ..config import HtmlConfig
from ..state import Availability

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

_ERROR_PAGE = re.compile(
    r"cloudflare|captcha|verify you are human|access denied|forbidden|bad gateway|service unavailable",
    re.IGNORECASE,
)


def _render(template: str, iso_date: str) -> str:
    return template.replace("{date}", iso_date)


def fetch(cfg: HtmlConfig, iso_date: str, client: httpx.Client) -> Availability:
    if cfg.available_regex is None and cfg.unavailable_regex is None:
        raise ValueError("html config must define available_regex or unavailable_regex")

    r = client.get(_render(cfg.url_template, iso_date), headers=DEFAULT_HEADERS)
    r.raise_for_status()

    if cfg.match_html:
        haystack = r.text
    else:
        tree = HTMLParser(r.text)
        if cfg.selector:
            node = tree.css_first(cfg.selector)
            if node is None:
                return "unknown"
            haystack = node.text(strip=True)
        else:
            haystack = tree.body.text(strip=True) if tree.body else r.text

    if _ERROR_PAGE.search(haystack):
        raise ValueError("html error/bot page detected")
    available = bool(
        cfg.available_regex
        and re.search(_render(cfg.available_regex, iso_date), haystack, re.IGNORECASE)
    )
    unavailable = bool(
        cfg.unavailable_regex
        and re.search(_render(cfg.unavailable_regex, iso_date), haystack, re.IGNORECASE)
    )
    if available and unavailable:
        raise ValueError("html availability markers are contradictory")
    if available:
        return "available"
    if unavailable:
        return "unavailable"
    return "unknown"
