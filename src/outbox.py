from __future__ import annotations

import hashlib
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .events import canonicalize_shifts, prune_delivered
from .notification_policy import needs_notification_burst
from .notify import (
    BURST_DELAYS,
    DEFAULT_RATE_LIMIT_DELAY_SECONDS,
    MAX_ATTEMPTS_PER_PART,
    MAX_RATE_LIMIT_DELAY_SECONDS,
    MAX_RATE_LIMIT_DEFERRALS_PER_PART,
    MIN_RATE_LIMIT_DELAY_SECONDS,
    DeliveryResult,
    PushoverDeliveryError,
    build_payload,
    retry_delay_seconds,
    send_event_part,
)
from .state import (
    OutboxEvent,
    PushoverQuotaState,
    State,
    load,
    now_iso,
    parse_iso,
    save,
)
from .targets import TARGET_DATE_SET


@dataclass(frozen=True)
class DeliveryOutcome:
    status: str
    event_id: str | None = None
    part_index: int | None = None
    wait_seconds: float = 0.0
    fatal: bool = False


class OutboxValidationError(ValueError):
    """An event cannot safely be handed to an external sender."""


_AVAILABILITY_REASONS = {
    "available",
    "availability_reconfirmed",
    "shifts_added",
}
_SAFE_QUARANTINE_REASON = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    # Preserve sub-second precision: truncating an acknowledgement time could
    # make a nominal five-second gap several hundred milliseconds too short.
    return _as_utc(value).isoformat()


def _strict_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise OutboxValidationError(f"{field}_missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OutboxValidationError(f"{field}_invalid") from exc
    if parsed.tzinfo is None:
        raise OutboxValidationError(f"{field}_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _validate_attempts(
    values: object,
    *,
    field: str,
    current_index: int,
    total_messages: int,
    maximum: int,
) -> None:
    if not isinstance(values, dict):
        raise OutboxValidationError(f"{field}_invalid")
    for raw_index, count in values.items():
        if not isinstance(raw_index, str) or not raw_index.isdigit():
            raise OutboxValidationError(f"{field}_index_invalid")
        index = int(raw_index)
        if str(index) != raw_index or index < 0 or index >= total_messages:
            raise OutboxValidationError(f"{field}_index_invalid")
        if index > current_index:
            raise OutboxValidationError(f"{field}_future_index")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise OutboxValidationError(f"{field}_count_invalid")
        if count > maximum or (index == current_index and count >= maximum):
            raise OutboxValidationError(f"{field}_count_exceeded")


def _validate_event(
    map_key: str,
    event: OutboxEvent,
    *,
    now: datetime,
    enabled_tent_slugs: frozenset[str] | None = None,
) -> None:
    if not map_key or not event.event_id or map_key != event.event_id:
        raise OutboxValidationError("event_id_key_mismatch")
    if not event.tent_slug.strip() or not event.tent_name.strip():
        raise OutboxValidationError("tent_identity_missing")
    if event.status != "pending":
        raise OutboxValidationError("event_not_pending")
    if isinstance(event.burst, bool) is False:
        raise OutboxValidationError("burst_flag_invalid")
    expected_total = len(BURST_DELAYS) + 1 if event.burst else 1
    if isinstance(event.total_messages, bool) or event.total_messages != expected_total:
        raise OutboxValidationError("burst_total_mismatch")
    if (
        isinstance(event.next_index, bool)
        or not isinstance(event.next_index, int)
        or not 0 <= event.next_index < event.total_messages
    ):
        raise OutboxValidationError("cursor_out_of_range")

    created_at = _strict_timestamp(event.created_at, "created_at")
    if event.next_attempt_at is not None:
        next_attempt_at = _strict_timestamp(event.next_attempt_at, "next_attempt_at")
        if next_attempt_at < created_at:
            raise OutboxValidationError("next_attempt_before_creation")
    if event.completed_at is not None:
        raise OutboxValidationError("pending_event_has_completion")

    _validate_attempts(
        event.attempts_by_index,
        field="attempts",
        current_index=event.next_index,
        total_messages=event.total_messages,
        maximum=MAX_ATTEMPTS_PER_PART,
    )
    _validate_attempts(
        event.rate_limit_deferrals_by_index,
        field="rate_limit_deferrals",
        current_index=event.next_index,
        total_messages=event.total_messages,
        maximum=MAX_RATE_LIMIT_DEFERRALS_PER_PART,
    )

    if event.kind == "availability":
        if (
            enabled_tent_slugs is not None
            and event.tent_slug not in enabled_tent_slugs
        ):
            raise OutboxValidationError("tent_disabled")
        if not event.iso_date or not event.booking_url or not event.shifts:
            raise OutboxValidationError("availability_payload_incomplete")
        if event.iso_date not in TARGET_DATE_SET:
            raise OutboxValidationError("target_date_disabled")
        if event.reason not in _AVAILABILITY_REASONS:
            raise OutboxValidationError("availability_reason_unknown")
        parsed_url = urlparse(event.booking_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise OutboxValidationError("booking_url_invalid")
        if not all(isinstance(label, str) and label.strip() for label in event.shifts):
            raise OutboxValidationError("shift_label_invalid")
        if not event.new_shifts or not all(
            isinstance(label, str) and label.strip() for label in event.new_shifts
        ):
            raise OutboxValidationError("new_shift_label_invalid")
        _, shift_keys = canonicalize_shifts(event.shifts)
        _, new_shift_keys = canonicalize_shifts(event.new_shifts)
        if not new_shift_keys or not set(new_shift_keys).issubset(shift_keys):
            raise OutboxValidationError("new_shifts_not_in_shifts")
        expected_burst = needs_notification_burst(event.iso_date, event.new_shifts)
        if event.burst != expected_burst:
            raise OutboxValidationError("notification_policy_mismatch")
    else:
        if event.burst or event.total_messages != 1:
            raise OutboxValidationError("monitor_error_must_be_single")
        if event.booking_url:
            parsed_url = urlparse(event.booking_url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise OutboxValidationError("booking_url_invalid")

    # Exercise all formatting/date validation before credentials or HTTP are touched.
    try:
        build_payload(event, now=now)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OutboxValidationError("payload_invalid") from exc


def _quota_reset_at(value: object, *, reference: datetime) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    try:
        reset_at = datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if reset_at > reference + timedelta(seconds=MAX_RATE_LIMIT_DELAY_SECONDS):
        return None
    return reset_at


def _event_due(state: State, event: OutboxEvent, now: datetime) -> datetime:
    due = parse_iso(event.next_attempt_at or event.created_at)
    quota = state.pushover_quota.get(event.kind)
    if quota and quota.remaining == 0 and quota.reset is not None:
        reset_at = _quota_reset_at(quota.reset, reference=now)
        if reset_at is not None and reset_at > due:
            due = reset_at
    return due


def _next_event(
    state: State,
    *,
    now: datetime,
) -> tuple[str, OutboxEvent, datetime] | None:
    pending = [
        (map_key, event, _event_due(state, event, now))
        for map_key, event in state.outbox.items()
        if event.status == "pending"
    ]
    if not pending:
        return None
    due_continuations = [
        item for item in pending if item[1].next_index > 0 and item[2] <= now
    ]
    # Once a burst continuation is due, honour its provider-acknowledgement
    # based cadence before starting further events. Initial messages can still
    # use otherwise idle gaps, so probing and unrelated alerts are not blocked.
    candidates = due_continuations or pending
    return min(
        candidates,
        key=lambda item: (
            item[2],
            parse_iso(item[1].created_at),
            item[1].event_id,
        ),
    )


def _set_next_attempt(event: OutboxEvent, when: datetime) -> None:
    event.next_attempt_at = _iso(when)


def _record_quota(
    state: State,
    event: OutboxEvent,
    *,
    limit: int | None,
    remaining: int | None,
    reset: int | None,
    observed_at: datetime,
) -> None:
    if limit is None and remaining is None and reset is None:
        return
    previous = state.pushover_quota.get(event.kind)
    resolved_reset = (
        reset if reset is not None else (previous.reset if previous else None)
    )
    resolved_reset_at = _quota_reset_at(resolved_reset, reference=observed_at)
    if resolved_reset_at is None or resolved_reset_at <= observed_at:
        resolved_reset = None
    if remaining == 0 and resolved_reset is None:
        resolved_reset = math.ceil(
            observed_at.timestamp() + DEFAULT_RATE_LIMIT_DELAY_SECONDS
        )
    state.pushover_quota[event.kind] = PushoverQuotaState(
        limit=limit if limit is not None else (previous.limit if previous else None),
        remaining=(
            remaining
            if remaining is not None
            else (previous.remaining if previous else None)
        ),
        reset=resolved_reset,
        observed_at=_iso(observed_at),
    )


def _mark_dead_letter(
    event: OutboxEvent,
    *,
    completed_at: datetime,
    error: str,
    error_class: str,
) -> None:
    event.status = "dead_letter"
    event.completed_at = _iso(completed_at)
    event.next_attempt_at = None
    event.last_error = error[:300]
    event.last_error_class = error_class


def _sanitize_quarantined_event(
    event: OutboxEvent,
    *,
    completed_at: datetime,
) -> None:
    """Replace a runtime-corrupt model with a data-minimised safe skeleton."""
    event.kind = "monitor_error"
    event.tent_slug = "quarantined"
    event.tent_name = "Quarantined outbox event"
    event.iso_date = None
    event.booking_url = None
    event.reason = "Outbox event quarantined before delivery."
    event.shifts = []
    event.new_shifts = []
    event.burst = False
    event.total_messages = 1
    event.next_index = 0
    event.attempts_by_index = {}
    event.rate_limit_deferrals_by_index = {}
    event.created_at = _iso(completed_at)
    event.next_attempt_at = None
    event.completed_at = _iso(completed_at)
    event.last_request_id = None
    event.quota_limit = None
    event.quota_remaining = None
    event.quota_reset = None
    event.requeue_count = 0
    if event.__pydantic_extra__ is not None:
        event.__pydantic_extra__.clear()


def _quarantine(
    state: State,
    map_key: str,
    event: OutboxEvent,
    *,
    completed_at: datetime,
    reason: str,
    stage: str,
) -> str:
    """Checkpoint a poison event without persisting its raw payload."""
    original_key_matched = map_key == event.event_id
    digest = hashlib.sha256(
        f"runtime-quarantine|{stage}|{map_key!s}|{event.event_id!s}".encode("utf-8")
    ).hexdigest()[:32]
    safe_id = f"quarantine-{digest}"
    collision = 0
    while safe_id in state.outbox and state.outbox[safe_id] is not event:
        collision += 1
        collision_digest = hashlib.sha256(
            f"{digest}|{collision}".encode("utf-8")
        ).hexdigest()[:32]
        safe_id = f"quarantine-{collision_digest}"
    for existing_key, existing_event in list(state.outbox.items()):
        if existing_event is event:
            state.outbox.pop(existing_key, None)
    state.outbox[safe_id] = event
    event.event_id = safe_id
    _sanitize_quarantined_event(event, completed_at=completed_at)
    safe_stage = stage if stage in {"validation", "sender"} else "runtime"
    safe_reason = (
        reason if _SAFE_QUARANTINE_REASON.fullmatch(reason) else "runtime_failure"
    )
    _mark_dead_letter(
        event,
        completed_at=completed_at,
        error=f"quarantined {safe_stage}: {safe_reason}",
        error_class="poison_event",
    )
    event.quarantine_reason = safe_reason
    event.quarantined_payload = {
        "stage": safe_stage,
        "event_id_key_matched": original_key_matched,
    }
    return safe_id


def _validate_sender_result(
    result: object,
    *,
    completed_at: datetime,
) -> DeliveryResult:
    if not isinstance(result, DeliveryResult):
        raise OutboxValidationError("sender_result_invalid")
    if result.request_id is not None and not isinstance(result.request_id, str):
        raise OutboxValidationError("sender_request_id_invalid")
    for name in ("quota_limit", "quota_remaining", "quota_reset"):
        value = getattr(result, name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise OutboxValidationError(f"sender_{name}_invalid")
    if (
        result.quota_limit is not None
        and result.quota_remaining is not None
        and result.quota_remaining > result.quota_limit
    ):
        raise OutboxValidationError("sender_quota_inconsistent")
    reset = result.quota_reset
    if reset is not None and _quota_reset_at(reset, reference=completed_at) is None:
        reset = None
    if reset == result.quota_reset:
        return result
    return DeliveryResult(
        request_id=result.request_id,
        quota_limit=result.quota_limit,
        quota_remaining=result.quota_remaining,
        quota_reset=reset,
    )


def deliver_next(
    state_path: Path,
    *,
    max_wait_seconds: float = 35,
    sender: Callable[..., DeliveryResult] = send_event_part,
    now_fn: Callable[[], datetime] = _utc_now,
    sleep_fn: Callable[[float], None] = time.sleep,
    enabled_tent_slugs: frozenset[str] | None = None,
) -> DeliveryOutcome:
    """Deliver at most one message part and atomically checkpoint local progress."""
    state = load(state_path)
    now = _as_utc(now_fn())

    # Validate every pending object before any external side effect. A poison
    # event is quarantined one at a time so the next invocation can continue.
    for map_key, candidate in sorted(state.outbox.items()):
        if candidate.status != "pending":
            continue
        try:
            _validate_event(
                map_key,
                candidate,
                now=now,
                enabled_tent_slugs=enabled_tent_slugs,
            )
        except Exception as exc:
            reason = (
                str(exc)
                if isinstance(exc, OutboxValidationError)
                else f"{type(exc).__name__}"
            )
            safe_id = _quarantine(
                state,
                map_key,
                candidate,
                completed_at=now,
                reason=reason,
                stage="validation",
            )
            save(state_path, state)
            return DeliveryOutcome("quarantined", safe_id, fatal=False)

    selected = _next_event(state, now=now)
    if selected is None:
        return DeliveryOutcome("idle")
    map_key, event, due = selected

    delay = max(0.0, (due - now).total_seconds())
    if delay > max(0.0, max_wait_seconds):
        return DeliveryOutcome("deferred", event.event_id, event.next_index, delay)
    if delay:
        sleep_fn(delay)
        now = _as_utc(now_fn())
        remaining_delay = max(0.0, (due - now).total_seconds())
        if remaining_delay > 0.001:
            return DeliveryOutcome(
                "deferred", event.event_id, event.next_index, remaining_delay
            )

    part_index = event.next_index
    key = str(part_index)
    try:
        raw_result = sender(event, now=now)
        completed_at = _as_utc(now_fn())
        result = _validate_sender_result(raw_result, completed_at=completed_at)
    except PushoverDeliveryError as exc:
        failure_at = _as_utc(now_fn())
        event.last_error = str(exc)[:300]
        event.last_error_class = exc.failure_class
        event.last_request_id = exc.request_id

        if exc.failure_class == "rate_limited":
            deferrals = event.rate_limit_deferrals_by_index.get(key, 0) + 1
            event.rate_limit_deferrals_by_index[key] = deferrals
            relative_retry = exc.retry_after_seconds
            if (
                isinstance(relative_retry, bool)
                or not isinstance(relative_retry, int)
                or relative_retry < 0
            ):
                relative_retry = None
            if relative_retry is not None:
                retry_at = failure_at + timedelta(
                    seconds=min(relative_retry, MAX_RATE_LIMIT_DELAY_SECONDS)
                )
            else:
                retry_at = exc.retry_at or (
                    failure_at + timedelta(seconds=DEFAULT_RATE_LIMIT_DELAY_SECONDS)
                )
            retry_at = max(
                _as_utc(retry_at),
                failure_at + timedelta(seconds=MIN_RATE_LIMIT_DELAY_SECONDS),
            )
            retry_at = min(
                retry_at,
                failure_at + timedelta(seconds=MAX_RATE_LIMIT_DELAY_SECONDS),
            )
            reset_at = _quota_reset_at(exc.quota_reset, reference=failure_at)
            if exc.quota_remaining == 0 and reset_at is not None:
                retry_at = max(retry_at, reset_at)
            safe_quota_reset = (
                int(reset_at.timestamp()) if reset_at is not None else None
            )
            event.quota_limit = exc.quota_limit
            event.quota_remaining = exc.quota_remaining
            event.quota_reset = safe_quota_reset
            if exc.quota_remaining is not None:
                global_reset = safe_quota_reset
                if exc.quota_remaining == 0 and (
                    reset_at is None or reset_at <= failure_at
                ):
                    global_reset = math.ceil(retry_at.timestamp())
                _record_quota(
                    state,
                    event,
                    limit=exc.quota_limit,
                    remaining=exc.quota_remaining,
                    reset=global_reset,
                    observed_at=failure_at,
                )
            if deferrals >= MAX_RATE_LIMIT_DEFERRALS_PER_PART:
                _mark_dead_letter(
                    event,
                    completed_at=failure_at,
                    error="rate_limit_deferrals_exhausted",
                    error_class="rate_limited",
                )
                save(state_path, state)
                return DeliveryOutcome(
                    "dead_letter", event.event_id, part_index, delay, fatal=False
                )
            _set_next_attempt(event, retry_at)
            save(state_path, state)
            return DeliveryOutcome("rate_limited", event.event_id, part_index, delay)

        attempt = event.attempts_by_index.get(key, 0) + 1
        event.attempts_by_index[key] = attempt
        if exc.failure_class == "terminal" or attempt >= MAX_ATTEMPTS_PER_PART:
            _mark_dead_letter(
                event,
                completed_at=failure_at,
                error=str(exc),
                error_class=exc.failure_class,
            )
            save(state_path, state)
            return DeliveryOutcome(
                "dead_letter", event.event_id, part_index, delay, fatal=True
            )
        _set_next_attempt(
            event,
            failure_at + timedelta(seconds=retry_delay_seconds(attempt)),
        )
        save(state_path, state)
        return DeliveryOutcome("retry_scheduled", event.event_id, part_index, delay)
    except Exception as exc:
        failure_at = _as_utc(now_fn())
        safe_id = _quarantine(
            state,
            map_key,
            event,
            completed_at=failure_at,
            reason=f"{type(exc).__name__}",
            stage="sender",
        )
        save(state_path, state)
        return DeliveryOutcome("quarantined", safe_id, part_index, delay, fatal=False)

    # The provider accepted this exact part. Count it and schedule the next part
    # relative to sender return, never relative to the request start.
    event.attempts_by_index[key] = event.attempts_by_index.get(key, 0) + 1
    event.last_error = None
    event.last_error_class = None
    event.last_request_id = result.request_id
    event.quota_limit = result.quota_limit
    event.quota_remaining = result.quota_remaining
    event.quota_reset = result.quota_reset
    _record_quota(
        state,
        event,
        limit=result.quota_limit,
        remaining=result.quota_remaining,
        reset=result.quota_reset,
        observed_at=completed_at,
    )
    event.next_index += 1
    if event.next_index >= event.total_messages:
        event.status = "delivered"
        event.completed_at = _iso(completed_at)
        event.next_attempt_at = None
        status = "delivered"
    else:
        gap = BURST_DELAYS[event.next_index - 1]
        _set_next_attempt(event, completed_at + timedelta(seconds=gap))
        status = "part_delivered"
    prune_delivered(state)
    save(state_path, state)
    return DeliveryOutcome(status, event.event_id, part_index, delay)


def requeue_dead_letter(
    state_path: Path, event_id: str, *, timestamp: str | None = None
) -> OutboxEvent:
    """Explicitly resume a quarantined event after its cause was corrected."""
    state = load(state_path)
    event = state.outbox.get(event_id)
    if event is None:
        raise ValueError(f"unknown outbox event: {event_id}")
    if event.status != "dead_letter":
        raise ValueError(f"outbox event is not dead_letter: {event_id}")
    retry_timestamp = timestamp or now_iso()
    _strict_timestamp(retry_timestamp, "requeue_timestamp")
    if not 0 <= event.next_index < event.total_messages:
        raise ValueError(f"outbox event cursor cannot be resumed: {event_id}")
    event.status = "pending"
    event.completed_at = None
    event.next_attempt_at = retry_timestamp
    event.requeue_count += 1
    event.last_error = None
    event.last_error_class = None
    event.quarantine_reason = None
    event.quarantined_payload = None
    # Preserve earlier counters for audit, but give the unresolved part a fresh
    # bounded retry budget after explicit operator intervention.
    event.attempts_by_index.pop(str(event.next_index), None)
    event.rate_limit_deferrals_by_index.pop(str(event.next_index), None)
    save(state_path, state)
    return event
