"""Playwright-based fetcher for JS-rendered tent reservation portals.

Lazy-imports playwright; only required when at least one tent uses mode=headless.
A single browser instance is shared across all tent fetches in a run.
"""
from __future__ import annotations

import re
from datetime import date as date_type

from ..config import HeadlessConfig
from ..state import Availability

GERMAN_MONTHS = {
    1: "Januar", 2: "Februar", 3: "März", 4: "April",
    5: "Mai", 6: "Juni", 7: "Juli", 8: "August",
    9: "September", 10: "Oktober", 11: "November", 12: "Dezember",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"
)


def _german_date(iso_date: str) -> str:
    d = date_type.fromisoformat(iso_date)
    return f"{d.day}. {GERMAN_MONTHS[d.month]} {d.year}"


def _render(template: str, iso_date: str) -> str:
    return template.replace("{date}", iso_date).replace("{de_date}", _german_date(iso_date))


def fetch(cfg: HeadlessConfig, iso_date: str, browser) -> Availability:
    """Render the page in `browser` (Playwright Browser instance), regex-match."""
    if cfg.available_regex is None and cfg.unavailable_regex is None:
        raise ValueError("headless config must define available_regex or unavailable_regex")

    ctx = browser.new_context(user_agent=USER_AGENT, locale="de-DE")
    try:
        page = ctx.new_page()
        page.goto(_render(cfg.url_template, iso_date), wait_until=cfg.wait_until, timeout=45000)
        if cfg.wait_extra_ms:
            page.wait_for_timeout(cfg.wait_extra_ms)
        if cfg.selector:
            try:
                haystack = page.locator(cfg.selector).inner_text(timeout=5000)
            except Exception:
                return "unknown"
        else:
            haystack = page.locator("body").inner_text()
    finally:
        ctx.close()

    if cfg.available_regex:
        pattern = _render(cfg.available_regex, iso_date)
        return "available" if re.search(pattern, haystack, re.IGNORECASE) else "unavailable"

    pattern = _render(cfg.unavailable_regex, iso_date)
    return "unavailable" if re.search(pattern, haystack, re.IGNORECASE) else "available"


def launch_browser():
    """Lazy-import playwright and launch a Chromium browser. Caller must close."""
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    return pw, browser
