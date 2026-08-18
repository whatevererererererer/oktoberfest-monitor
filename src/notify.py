from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date as date_type, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import httpx

from .state import OutboxEvent

PUSHOVER_API = "https://api.pushover.net/1/messages.json"
BERLIN = ZoneInfo("Europe/Berlin")

BURST_DELAYS = (5, 5, 5, 30, 5, 5, 5)
MAX_ATTEMPTS_PER_PART = 5
MAX_RATE_LIMIT_DEFERRALS_PER_PART = 12
MIN_RATE_LIMIT_DELAY_SECONDS = 5
DEFAULT_RATE_LIMIT_DELAY_SECONDS = 60
MAX_RATE_LIMIT_DELAY_SECONDS = 35 * 24 * 60 * 60

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
        quota_limit: int | None = None,
        quota_remaining: int | None = None,
        quota_reset: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.retry_at = retry_at
        self.status_code = status_code
        self.request_id = request_id
        self.quota_limit = quota_limit
        self.quota_remaining = quota_remaining
        self.quota_reset = quota_reset
        self.retry_after_seconds = retry_after_seconds


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


def _quota_header_int(headers: httpx.Headers, name: str) -> int | None:
    value = _header_int(headers, name)
    return value if value is not None and value >= 0 else None


def _quota_metadata(
    headers: httpx.Headers,
    *,
    now: datetime,
) -> tuple[int | None, int | None, int | None]:
    limit = _quota_header_int(headers, "X-Limit-App-Limit")
    remaining = _quota_header_int(headers, "X-Limit-App-Remaining")
    reset = _quota_header_int(headers, "X-Limit-App-Reset")
    if reset is not None:
        try:
            reset_at = datetime.fromtimestamp(reset, timezone.utc)
        except (OverflowError, OSError, ValueError):
            reset = None
        else:
            if reset_at > now + timedelta(seconds=MAX_RATE_LIMIT_DELAY_SECONDS):
                reset = None
    if limit is not None and remaining is not None and remaining > limit:
        limit = None
        remaining = None
    return limit, remaining, reset


def _as_utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _retry_after_seconds(headers: httpx.Headers) -> int | None:
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return min(MAX_RATE_LIMIT_DELAY_SECONDS, max(0, int(raw)))
    except ValueError:
        return None


def _retry_at(
    headers: httpx.Headers,
    *,
    now: datetime,
    quota_remaining: int | None,
    quota_reset: int | None,
) -> datetime:
    """Return a deterministic, non-past 429 retry time.

    Standard ``Retry-After`` values (seconds or HTTP date) are honoured. The
    Pushover quota-reset epoch participates only when the provider explicitly
    reports an exhausted quota.
    """
    minimum = now.timestamp() + MIN_RATE_LIMIT_DELAY_SECONDS
    candidates = [minimum]
    retry_after = headers.get("Retry-After")
    relative_seconds = _retry_after_seconds(headers)
    if relative_seconds is not None:
        candidates.append(now.timestamp() + relative_seconds)
    elif retry_after:
        try:
            parsed = parsedate_to_datetime(retry_after)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed_timestamp = parsed.timestamp()
            upper_bound = now.timestamp() + MAX_RATE_LIMIT_DELAY_SECONDS
            candidates.append(min(upper_bound, parsed_timestamp))
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    if quota_remaining == 0 and quota_reset is not None:
        candidates.append(float(quota_reset))
    if len(candidates) == 1:
        candidates.append(now.timestamp() + DEFAULT_RATE_LIMIT_DELAY_SECONDS)
    return datetime.fromtimestamp(max(candidates), timezone.utc)


def build_payload(event: OutboxEvent, *, now: datetime | None = None) -> dict:
    now = _as_utc(now)
    when = now.astimezone(BERLIN).strftime("%H:%M")

    if event.kind == "monitor_error":
        payload = {
            "title": "Wiesn-Monitor: Fehler",
            "message": event.reason[:1024],
            "priority": 0,
        }
        if event.booking_url:
            payload.update(
                {
                    "url": event.booking_url,
                    "url_title": "Seite manuell prüfen",
                }
            )
        return payload

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
        message = f"Neue Reservierungszeit erkannt {when}. Tippen zum Anfragen."
    else:
        shifts_label = ", ".join(event.shifts)
        if not shifts_label:
            raise ValueError("availability notifications require at least one shift")
        title = f"[{shifts_label}] {weekday} {event.tent_name} {de_date}"
        message = f"Reservierungsanfrage möglich {when}. Tippen zum Öffnen."
    return {
        "title": title,
        "message": message,
        "url": _booking_url_with_date(event.booking_url, event.iso_date),
        "url_title": "Reservierung öffnen",
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
    now = _as_utc(now)
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
            quota_limit, quota_remaining, quota_reset = _quota_metadata(
                response.headers, now=now
            )
            raise PushoverDeliveryError(
                "http_429",
                failure_class="rate_limited",
                retry_at=_retry_at(
                    response.headers,
                    now=now,
                    quota_remaining=quota_remaining,
                    quota_reset=quota_reset,
                ),
                status_code=status,
                request_id=request_id,
                quota_limit=quota_limit,
                quota_remaining=quota_remaining,
                quota_reset=quota_reset,
                retry_after_seconds=_retry_after_seconds(response.headers),
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
        quota_limit, quota_remaining, quota_reset = _quota_metadata(
            response.headers, now=now
        )
        return DeliveryResult(
            request_id=str(data.get("request") or request_id or "") or None,
            quota_limit=quota_limit,
            quota_remaining=quota_remaining,
            quota_reset=quota_reset,
        )
    finally:
        if owns_client:
            client.close()


def retry_delay_seconds(attempt: int) -> int:
    return min(60, 5 * (2 ** max(0, attempt - 1)))
