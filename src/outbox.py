from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .events import prune_delivered
from .notify import (
    BURST_DELAYS,
    MAX_ATTEMPTS_PER_PART,
    DeliveryResult,
    PushoverDeliveryError,
    retry_delay_seconds,
    send_event_part,
)
from .state import OutboxEvent, load, now_iso, parse_iso, save


@dataclass(frozen=True)
class DeliveryOutcome:
    status: str
    event_id: str | None = None
    part_index: int | None = None
    wait_seconds: float = 0.0
    fatal: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _next_event(events: list[OutboxEvent]) -> OutboxEvent | None:
    pending = [event for event in events if event.status == "pending"]
    if not pending:
        return None
    return min(
        pending,
        key=lambda event: (
            event.next_attempt_at or event.created_at,
            event.created_at,
            event.event_id,
        ),
    )


def _set_next_attempt(event: OutboxEvent, when: datetime) -> None:
    event.next_attempt_at = when.astimezone(timezone.utc).isoformat(timespec="seconds")


def deliver_next(
    state_path: Path,
    *,
    max_wait_seconds: float = 35,
    sender: Callable[..., DeliveryResult] = send_event_part,
    now_fn: Callable[[], datetime] = _utc_now,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> DeliveryOutcome:
    """Deliver at most one message part and atomically checkpoint local progress."""
    state = load(state_path)
    event = _next_event(list(state.outbox.values()))
    if event is None:
        return DeliveryOutcome("idle")

    now = now_fn().astimezone(timezone.utc)
    due = parse_iso(event.next_attempt_at or event.created_at)
    delay = max(0.0, (due - now).total_seconds())
    if delay > max_wait_seconds:
        return DeliveryOutcome("deferred", event.event_id, event.next_index, delay)
    if delay:
        sleep_fn(delay)
        now = now_fn().astimezone(timezone.utc)

    part_index = event.next_index
    key = str(part_index)
    attempt = event.attempts_by_index.get(key, 0) + 1
    event.attempts_by_index[key] = attempt
    try:
        result = sender(event, now=now)
    except PushoverDeliveryError as exc:
        event.last_error = str(exc)[:300]
        event.last_error_class = exc.failure_class
        event.last_request_id = exc.request_id
        if exc.failure_class == "terminal" or attempt >= MAX_ATTEMPTS_PER_PART:
            event.status = "dead_letter"
            event.completed_at = now_iso()
            save(state_path, state)
            return DeliveryOutcome(
                "dead_letter", event.event_id, part_index, delay, fatal=True
            )
        if exc.failure_class == "rate_limited":
            retry_at = exc.retry_at or (now + timedelta(seconds=60))
            _set_next_attempt(event, retry_at)
            save(state_path, state)
            return DeliveryOutcome("rate_limited", event.event_id, part_index, delay)
        _set_next_attempt(event, now + timedelta(seconds=retry_delay_seconds(attempt)))
        save(state_path, state)
        return DeliveryOutcome("retry_scheduled", event.event_id, part_index, delay)

    event.last_error = None
    event.last_error_class = None
    event.last_request_id = result.request_id
    event.quota_limit = result.quota_limit
    event.quota_remaining = result.quota_remaining
    event.quota_reset = result.quota_reset
    event.next_index += 1
    if event.next_index >= event.total_messages:
        event.status = "delivered"
        event.completed_at = now_iso()
        event.next_attempt_at = None
        status = "delivered"
    else:
        gap = BURST_DELAYS[event.next_index - 1]
        _set_next_attempt(event, now + timedelta(seconds=gap))
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
    event.status = "pending"
    event.completed_at = None
    event.next_attempt_at = timestamp or now_iso()
    event.requeue_count += 1
    # Preserve earlier attempt counters for audit, but give the unresolved part
    # a fresh bounded retry budget after explicit operator intervention.
    event.attempts_by_index.pop(str(event.next_index), None)
    save(state_path, state)
    return event
