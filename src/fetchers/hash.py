from __future__ import annotations

import hashlib
import re

import httpx
from selectolax.parser import HTMLParser

from ..config import HashConfig

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

_WS = re.compile(r"\s+")


def _render(template: str, iso_date: str) -> str:
    return template.replace("{date}", iso_date)


def fetch_hash(cfg: HashConfig, iso_date: str, client: httpx.Client) -> str:
    r = client.get(_render(cfg.url_template, iso_date), headers=DEFAULT_HEADERS)
    r.raise_for_status()
    tree = HTMLParser(r.text)
    if cfg.selector:
        node = tree.css_first(cfg.selector)
        text = node.text(strip=True) if node else ""
    else:
        text = tree.body.text(strip=True) if tree.body else r.text
    normalized = _WS.sub(" ", text).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
