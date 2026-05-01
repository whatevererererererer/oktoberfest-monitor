from __future__ import annotations

import os
from datetime import date as date_type, datetime
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import httpx

PUSHOVER_API = "https://api.pushover.net/1/messages.json"

WEEKDAY_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _weekday_short(iso_date: str) -> str:
    return WEEKDAY_DE[date_type.fromisoformat(iso_date).weekday()]


def _de_numeric_date(iso_date: str) -> str:
    return date_type.fromisoformat(iso_date).strftime("%d.%m.%Y")


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
    weekday = _weekday_short(iso_date)
    de_date = _de_numeric_date(iso_date)

    if reason == "shifts_added" and new_shifts:
        # Mark newly added shifts with a leading "+", keep order: new first, then existing
        new_set = set(new_shifts)
        ordered = [f"+{s}" for s in new_shifts] + [s for s in (shifts or []) if s not in new_set]
        shifts_label = ", ".join(ordered) if ordered else "+?"
        title = f"[{shifts_label}] {weekday} {tent_name} {de_date}"
        message = f"Neue Schicht erkannt {when}. Tippen zum Buchen."
    else:
        shifts_label = ", ".join(shifts) if shifts else "?"
        title = f"[{shifts_label}] {weekday} {tent_name} {de_date}"
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
