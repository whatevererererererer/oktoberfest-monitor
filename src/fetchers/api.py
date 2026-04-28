from __future__ import annotations

import json

import httpx
from jsonpath_ng.ext import parse as jsonpath_parse

from ..config import ApiConfig
from ..state import Availability

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


def _render(template: str, iso_date: str) -> str:
    return template.replace("{date}", iso_date)


def _eval_predicate(predicate: str, data) -> bool:
    """A predicate is a JSONPath expression. Match = truthy result."""
    expr = jsonpath_parse(predicate)
    matches = [m.value for m in expr.find(data)]
    return any(bool(v) for v in matches)


def fetch(cfg: ApiConfig, iso_date: str, client: httpx.Client) -> Availability:
    headers = {**DEFAULT_HEADERS, **cfg.headers}

    if cfg.method == "GET":
        params = (
            {k: _render(v, iso_date) for k, v in cfg.query_template.items()}
            if cfg.query_template
            else None
        )
        r = client.get(_render(cfg.endpoint, iso_date), headers=headers, params=params)
    else:
        body = _render(cfg.payload_template or "{}", iso_date)
        r = client.post(
            _render(cfg.endpoint, iso_date),
            headers={**headers, "Content-Type": "application/json"},
            content=body,
        )
    r.raise_for_status()
    data = r.json()

    if cfg.unavailable_when and _eval_predicate(cfg.unavailable_when, data):
        return "unavailable"
    if cfg.available_when and _eval_predicate(cfg.available_when, data):
        return "available"
    if cfg.unavailable_when:
        return "available"
    if cfg.available_when:
        return "unavailable"
    raise ValueError("api config must define unavailable_when or available_when")
