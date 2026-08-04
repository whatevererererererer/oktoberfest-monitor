from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date as date_type, datetime, timezone
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import httpx

from .state import OutboxEvent

PUSHOVER_API = "https://api.pushover.net/1/messages.json"
BERLIN = ZoneInfo("Europe/Berlin")

BURST_COUNT = 4
BURST_INTERVAL_SECONDS = 5
BURST_REPEAT_DELAY_SECONDS = 30
BURST_DELAYS = (5, 5, 5, 30, 5, 5, 5)
MAX_ATTEMPTS_PER_PART = 5

WEEKDAY_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

FailureClass = Literal["retryable", "rate_limited", "terminal"]


@dataclass(frozen=True)
class DeliveryResult:
    request_id: str | None
    quota_limit: int | None
    quota_remaining: int | None
    quota_reset: int | None


class PushoverDeliveryError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        failure_class: FailureClass,
        retry_at: datetime | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.retry_at = retry_at
        self.status_code = status_code
        self.request_id = request_id


def _weekday_short(iso_date: str) -> str:
    return WEEKDAY_DE[date_type.fromisoformat(iso_date).weekday()]


def _de_numeric_date(iso_date: str) -> str:
    return date_type.fromisoformat(iso_date).strftime("%d.%m.%Y")


def _booking_url_with_date(booking_url: str, iso_date: str) -> str:
    parts = urlparse(booking_url)
    query = dict(parse_qsl(parts.query))
    query.setdefault("date", iso_date)
    return urlunparse(parts._replace(query=urlencode(query)))


def _header_int(headers: httpx.Headers, name: str) -> int | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def build_payload(event: OutboxEvent, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    when = now.astimezone(BERLIN).strftime("%H:%M")

    if event.kind == "monitor_error":
        return {
            "title": "Wiesn-Monitor: Fehler",
            "message": event.reason[:1024],
            "priority": 0,
        }

    if not event.iso_date or not event.booking_url:
        raise ValueError(f"availability event {event.event_id} lacks date/url")
    weekday = _weekday_short(event.iso_date)
    de_date = _de_numeric_date(event.iso_date)
    if event.reason == "shifts_added" and event.new_shifts:
        new_keys = {value.casefold() for value in event.new_shifts}
        ordered = [f"+{value}" for value in event.new_shifts]
        ordered.extend(
            value for value in event.shifts if value.casefold() not in new_keys
        )
        shifts_label = ", ".join(ordered) if ordered else "+?"
        title = f"[{shifts_label}] {weekday} {event.tent_name} {de_date}"
        message = f"Neue Schicht erkannt {when}. Tippen zum Buchen."
    else:
        shifts_label = ", ".join(event.shifts)
        if not shifts_label:
            raise ValueError("availability notifications require at least one shift")
        title = f"[{shifts_label}] {weekday} {event.tent_name} {de_date}"
        message = f"Verfügbarkeit erkannt {when}. Tippen zum Buchen."
    return {
        "title": title,
        "message": message,
        "url": _booking_url_with_date(event.booking_url, event.iso_date),
        "url_title": "Jetzt reservieren",
        "priority": 1,
        "sound": "persistent",
    }


def _credentials(event: OutboxEvent) -> tuple[str, str]:
    token_name = "PUSHOVER_TOKEN_ERROR" if event.kind == "monitor_error" else "PUSHOVER_TOKEN"
    token = os.environ.get(token_name)
    user = os.environ.get("PUSHOVER_USER")
    if not token or not user:
        raise PushoverDeliveryError(
            f"missing {token_name} or PUSHOVER_USER",
            failure_class="terminal",
        )
    return token, user


def send_event_part(
    event: OutboxEvent,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> DeliveryResult:
    """Send exactly one outbox part. Scheduling/checkpointing lives elsewhere."""
    token, user = _credentials(event)
    payload = build_payload(event, now=now)
    body = {"token": token, "user": user, **payload}
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=10, limits=httpx.Limits(max_connections=2))
    try:
        try:
            response = client.post(PUSHOVER_API, data=body)
        except httpx.TransportError as exc:
            raise PushoverDeliveryError(
                type(exc).__name__, failure_class="retryable"
            ) from exc

        request_id = response.headers.get("X-Pushover-Request")
        status = response.status_code
        if status == 429:
            retry_at: datetime | None = None
            retry_after = _header_int(response.headers, "Retry-After")
            reset = _header_int(response.headers, "X-Limit-App-Reset")
            if retry_after is not None:
                retry_at = datetime.now(timezone.utc).replace(microsecond=0)
                retry_at = datetime.fromtimestamp(
                    retry_at.timestamp() + max(5, retry_after), timezone.utc
                )
            elif reset is not None:
                retry_at = datetime.fromtimestamp(reset, timezone.utc)
            raise PushoverDeliveryError(
                "http_429",
                failure_class="rate_limited",
                retry_at=retry_at,
                status_code=status,
                request_id=request_id,
            )
        if 400 <= status < 500:
            raise PushoverDeliveryError(
                f"http_{status}",
                failure_class="terminal",
                status_code=status,
                request_id=request_id,
            )
        if status >= 500:
            raise PushoverDeliveryError(
                f"http_{status}",
                failure_class="retryable",
                status_code=status,
                request_id=request_id,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise PushoverDeliveryError(
                "invalid_json", failure_class="retryable", status_code=status
            ) from exc
        if status != 200 or data.get("status") != 1:
            raise PushoverDeliveryError(
                "invalid_success_response",
                failure_class="terminal",
                status_code=status,
                request_id=str(data.get("request") or request_id or "") or None,
            )
        return DeliveryResult(
            request_id=str(data.get("request") or request_id or "") or None,
            quota_limit=_header_int(response.headers, "X-Limit-App-Limit"),
            quota_remaining=_header_int(response.headers, "X-Limit-App-Remaining"),
            quota_reset=_header_int(response.headers, "X-Limit-App-Reset"),
        )
    finally:
        if owns_client:
            client.close()


def retry_delay_seconds(attempt: int) -> int:
    return min(60, 5 * (2 ** max(0, attempt - 1)))


# Backward-compatible direct helpers. Production uses the durable outbox path.
def _post(token: str, user: str, payload: dict) -> None:
    with httpx.Client(timeout=10) as client:
        response = client.post(PUSHOVER_API, data={"token": token, "user": user, **payload})
        response.raise_for_status()
        data = response.json()
        if data.get("status") != 1:
            raise RuntimeError("Pushover did not confirm the request")


def _post_burst(token: str, user: str, payload: dict) -> None:
    _post(token, user, payload)
    for delay in BURST_DELAYS:
        time.sleep(delay)
        _post(token, user, payload)


def alert_available(
    *,
    tent_name: str,
    tent_slug: str,
    iso_date: str,
    booking_url: str,
    shifts: list[str] | None = None,
    new_shifts: list[str] | None = None,
    reason: str = "available",
    burst: bool = False,
) -> None:
    event = OutboxEvent(
        event_id=f"legacy-{tent_slug}-{iso_date}",
        tent_slug=tent_slug,
        tent_name=tent_name,
        iso_date=iso_date,
        booking_url=booking_url,
        shifts=shifts or [],
        new_shifts=new_shifts or [],
        reason=reason,
        burst=burst,
        total_messages=8 if burst else 1,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    token, user = _credentials(event)
    payload = build_payload(event)
    if burst:
        _post_burst(token, user, payload)
    else:
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
