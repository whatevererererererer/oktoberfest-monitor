from __future__ import annotations

import os
from datetime import date as date_type, datetime
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import httpx

PUSHOVER_API = "https://api.pushover.net/1/messages.json"

WEEKDAY_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _format_german_date(iso_date: str) -> str:
    d = date_type.fromisoformat(iso_date)
    return f"{WEEKDAY_DE[d.weekday()]} {d.strftime('%d.%m.%Y')}"


def _booking_url_with_date(booking_url: str, iso_date: str) -> str:
    parts = urlparse(booking_url)
    query = dict(parse_qsl(parts.query))
    query.setdefault("date", iso_date)
    return urlunparse(parts._replace(query=urlencode(query)))


def _post(token: str, user: str, payload: dict) -> None:
    body = {"token": token, "user": user, **payload}
    with httpx.Client(timeout=10) as client:
        r = client.post(PUSHOVER_API, data=body)
        r.raise_for_status()


def alert_available(
    *,
    tent_name: str,
    tent_slug: str,
    iso_date: str,
    booking_url: str,
    shifts: list[str] | None = None,
    new_shifts: list[str] | None = None,
    reason: str = "available",
) -> None:
    token = os.environ["PUSHOVER_TOKEN"]
    user = os.environ["PUSHOVER_USER"]
    when = datetime.now().strftime("%H:%M")
    shifts_str = f" [{', '.join(shifts)}]" if shifts else ""
    if reason == "shifts_added" and new_shifts:
        title = f"Wiesn NEU: {tent_name} — {_format_german_date(iso_date)} +{', '.join(new_shifts)}"
        message = f"Neue Schicht erkannt {when}: {', '.join(new_shifts)}. Bestand: {', '.join(shifts or [])}. Tippen zum Buchen."
    else:
        title = f"Wiesn FREI: {tent_name} — {_format_german_date(iso_date)}{shifts_str}"
        message = f"Verfügbarkeit erkannt {when}. Tippen zum Buchen."
    payload = {
        "title": title,
        "message": message,
        "url": _booking_url_with_date(booking_url, iso_date),
        "url_title": "Jetzt reservieren",
        "priority": 1,
        "sound": "persistent",
    }
    _post(token, user, payload)


def alert_error(*, summary: str, details: str = "") -> None:
    token = os.environ.get("PUSHOVER_TOKEN_ERROR")
    user = os.environ.get("PUSHOVER_USER")
    if not token or not user:
        return
    payload = {
        "title": "Wiesn-Monitor: Fehler",
        "message": (summary + ("\n\n" + details if details else ""))[:1024],
        "priority": 0,
    }
    _post(token, user, payload)
