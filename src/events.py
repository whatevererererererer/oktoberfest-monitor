from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

from .notification_policy import needs_notification_burst
from .state import OutboxEvent, State, TentDateState, TentState, now_iso

_WS = re.compile(r"\s+")
_TIME = re.compile(r"\b\d{1,2}(?::|\.)\d{2}\s*(?:uhr)?\b.*$", re.IGNORECASE)
_PARENS = re.compile(r"\s*[\(\[].*?[\)\]]\s*")
_NON_WORD = re.compile(r"[^\wäöüß]+", re.IGNORECASE)
_KNOWN_SHIFTS = ("vormittag", "mittag", "nachmittag", "abend", "ganztag")
_SHIFT_EVIDENCE_LOSS_CLASSES = frozenset(
    {
        "available_without_shifts",
        "ambiguous_shift_control",
        "event_days_empty",
        "event_days_no_matching_tickets",
        "reservation_form_no_dates",
        "shift_control_missing",
        "shift_evidence_unavailable",
        "shift_options_empty",
        "shift_update_response_unconfirmed",
        "shift_update_unconfirmed",
        "target_selection_unconfirmed",
        "target_slots_incomplete",
    }
)


def normalize_shift_label(label: str) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFKC", label)).strip()


def canonical_shift_key(label: str) -> str:
    normalized = normalize_shift_label(label).casefold()
    known_matches = [
        known
        for known in _KNOWN_SHIFTS
        if re.search(rf"(?<!\w){re.escape(known)}(?!\w)", normalized)
    ]
    if known_matches:
        # Preserve combined business meanings such as "Mittag / Nachmittag";
        # otherwise a newly added high-attention component could be suppressed.
        return "+".join(known_matches)
    normalized = _PARENS.sub(" ", normalized)
    normalized = _TIME.sub("", normalized)
    normalized = _NON_WORD.sub(" ", normalized)
    return _WS.sub(" ", normalized).strip()


def canonicalize_shifts(labels: Iterable[str]) -> tuple[list[str], list[str]]:
    display: list[str] = []
    keys: list[str] = []
    seen: set[str] = set()
    for raw in labels:
        label = normalize_shift_label(raw)
        key = canonical_shift_key(label)
        if not key or key in seen:
            continue
        seen.add(key)
        display.append(label)
        keys.append(key)
    return display, keys


def _diagnostics_dict(result: Any) -> dict[str, Any]:
    if hasattr(result, "diagnostic_dict"):
        value = result.diagnostic_dict()
        if isinstance(value, dict):
            return dict(value)
    diagnostics = getattr(result, "diagnostics", {})
    if hasattr(diagnostics, "model_dump"):
        return dict(diagnostics.model_dump(mode="json"))
    if isinstance(diagnostics, dict):
        return dict(diagnostics)
    return {"detail": str(diagnostics)}


def _structured_reliable_error(
    result: Any,
    status: str,
    raw_shifts: list[str],
    diagnostics: dict[str, Any],
) -> str | None:
    """Reject impossible success records from structured probe implementations.

    Lightweight legacy/generic result objects intentionally remain supported;
    their adapter owns the evidence contract. A result exposing
    ``diagnostic_dict`` is the structured contract used by ``ProbeResult`` and
    must contain internally consistent Festzelt evidence.
    """
    if not hasattr(result, "diagnostic_dict"):
        return None
    plausible_count = diagnostics.get("plausible_date_option_count")
    explicit_empty_feed = (
        status == "unavailable"
        and diagnostics.get("target_found") is False
        and diagnostics.get("unavailable_confirmed") is True
    )
    common = (
        diagnostics.get("health") == "healthy"
        and diagnostics.get("page_type") == "booking"
        and diagnostics.get("date_control_count") == 1
        and isinstance(plausible_count, int)
        and (plausible_count > 0 or explicit_empty_feed)
        and not diagnostics.get("error_class")
    )
    if not common:
        return f"inconsistent_{status}_diagnostics"
    if status == "available":
        consistent = (
            bool(raw_shifts)
            and diagnostics.get("target_found") is True
            and diagnostics.get("target_enabled") is not False
            and diagnostics.get("shift_control_count") == 1
            and diagnostics.get("shift_control_found") is True
            and diagnostics.get("update_confirmed") is True
            and diagnostics.get("shift_count") == len(raw_shifts)
        )
    else:
        absent_target = diagnostics.get("target_found") is False
        zero_capacity = (
            diagnostics.get("target_found") is True
            and diagnostics.get("target_enabled") is True
            and diagnostics.get("shift_control_count") == 1
            and diagnostics.get("shift_control_found") is True
            and diagnostics.get("update_confirmed") is True
            and diagnostics.get("unavailable_confirmed") is True
        )
        consistent = diagnostics.get("shift_count", 0) == 0 and (
            absent_target or zero_capacity
        )
    return None if consistent else f"inconsistent_{status}_diagnostics"


def _event_id(
    tent_slug: str,
    iso_date: str,
    sequence: int,
    reason: str,
    shift_keys: list[str],
) -> str:
    raw = "|".join(
        [tent_slug, iso_date, str(sequence), reason, ",".join(sorted(shift_keys))]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _enqueue_availability(
    *,
    state: State,
    cfg: Any,
    date_state: TentDateState,
    iso_date: str,
    reason: str,
    shifts: list[str],
    new_shifts: list[str],
    new_shift_keys: list[str],
    timestamp: str,
) -> OutboxEvent:
    date_state.alert_sequence += 1
    event_id = _event_id(
        cfg.slug, iso_date, date_state.alert_sequence, reason, new_shift_keys
    )
    burst = needs_notification_burst(iso_date, new_shifts)
    event = OutboxEvent(
        event_id=event_id,
        tent_slug=cfg.slug,
        tent_name=cfg.name,
        iso_date=iso_date,
        booking_url=cfg.booking_url,
        reason=reason,
        shifts=shifts,
        new_shifts=new_shifts,
        burst=burst,
        created_at=timestamp,
        next_attempt_at=timestamp,
        total_messages=8 if burst else 1,
    )
    state.outbox.setdefault(event_id, event)
    return state.outbox[event_id]


@dataclass(frozen=True)
class AppliedProbe:
    event: OutboxEvent | None
    health: str
    observed_status: str


def apply_probe_result(
    *,
    state: State,
    cfg: Any,
    tent_state: TentState,
    iso_date: str,
    result: Any,
    timestamp: str | None = None,
) -> AppliedProbe:
    """Apply one observation while preserving the last reliable baseline."""
    timestamp = timestamp or now_iso()
    date_state = tent_state.dates.setdefault(iso_date, TentDateState())
    raw_status = str(result.status)
    raw_shifts = list(getattr(result, "shifts", []) or [])
    shifts, shift_keys = canonicalize_shifts(raw_shifts)
    diagnostics = _diagnostics_dict(result)

    # Defensive invariant even if a future fetcher regresses.
    if raw_status == "available" and not shift_keys:
        raw_status = "unknown"
        diagnostics["health"] = "degraded"
        diagnostics["error_class"] = "available_without_shifts"
    elif raw_status in {"available", "unavailable"}:
        consistency_error = _structured_reliable_error(
            result, raw_status, raw_shifts, diagnostics
        )
        if consistency_error:
            raw_status = "unknown"
            diagnostics["health"] = "degraded"
            diagnostics["error_class"] = consistency_error

    date_state.last_check = timestamp
    date_state.observed_status = raw_status  # type: ignore[assignment]
    date_state.diagnostics = diagnostics

    if raw_status == "unknown":
        date_state.health = "degraded"
        date_state.consecutive_degraded += 1
        date_state.consecutive_errors = 0
        error_class = diagnostics.get("error_class")
        if (
            date_state.baseline_verified
            and date_state.status == "available"
            and isinstance(error_class, str)
            and error_class in _SHIFT_EVIDENCE_LOSS_CLASSES
        ):
            date_state.availability_evidence_lost = True
        return AppliedProbe(None, date_state.health, raw_status)

    if raw_status == "error":
        date_state.health = "error"
        date_state.consecutive_errors += 1
        date_state.consecutive_degraded = 0
        return AppliedProbe(None, date_state.health, raw_status)

    if raw_status not in ("available", "unavailable"):
        raise ValueError(f"unsupported probe status: {raw_status}")

    diagnostics["health"] = "healthy"
    diagnostics["error_class"] = None
    date_state.diagnostics = diagnostics
    date_state.health = "healthy"
    date_state.consecutive_degraded = 0
    date_state.consecutive_errors = 0

    previous_status = date_state.status
    baseline_was_verified = date_state.baseline_verified
    evidence_was_lost = date_state.availability_evidence_lost
    previous_shifts, previous_keys = canonicalize_shifts(date_state.shifts)
    if date_state.shift_keys:
        previous_keys = list(date_state.shift_keys)

    event: OutboxEvent | None = None
    if raw_status == "available":
        if not baseline_was_verified or previous_status != "available":
            event = _enqueue_availability(
                state=state,
                cfg=cfg,
                date_state=date_state,
                iso_date=iso_date,
                reason="available",
                shifts=shifts,
                new_shifts=shifts,
                new_shift_keys=shift_keys,
                timestamp=timestamp,
            )
        elif evidence_was_lost:
            event = _enqueue_availability(
                state=state,
                cfg=cfg,
                date_state=date_state,
                iso_date=iso_date,
                reason="availability_reconfirmed",
                shifts=shifts,
                new_shifts=shifts,
                new_shift_keys=shift_keys,
                timestamp=timestamp,
            )
        else:
            previous_key_set = set(previous_keys)
            added_keys = [key for key in shift_keys if key not in previous_key_set]
            if added_keys:
                label_for_key = dict(zip(shift_keys, shifts, strict=True))
                added_labels = [label_for_key[key] for key in added_keys]
                event = _enqueue_availability(
                    state=state,
                    cfg=cfg,
                    date_state=date_state,
                    iso_date=iso_date,
                    reason="shifts_added",
                    shifts=shifts,
                    new_shifts=added_labels,
                    new_shift_keys=added_keys,
                    timestamp=timestamp,
                )

    reliable_shift_keys = shift_keys if raw_status == "available" else []
    reliable_changed = (
        not baseline_was_verified
        or previous_status != raw_status
        or set(previous_keys) != set(reliable_shift_keys)
    )
    date_state.status = raw_status  # type: ignore[assignment]
    date_state.shifts = shifts if raw_status == "available" else []
    date_state.shift_keys = shift_keys if raw_status == "available" else []
    date_state.baseline_verified = True
    date_state.last_reliable_at = timestamp
    date_state.last_reliable_diagnostics = dict(diagnostics)
    date_state.availability_evidence_lost = False
    if reliable_changed:
        date_state.last_change = timestamp
    return AppliedProbe(event, date_state.health, raw_status)


def enqueue_monitor_error(
    *,
    state: State,
    tent_slug: str,
    tent_name: str,
    booking_url: str,
    details: str,
    incident_number: int,
    timestamp: str | None = None,
) -> OutboxEvent:
    timestamp = timestamp or now_iso()
    raw = f"monitor-error|{tent_slug}|{incident_number}"
    event_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    event = OutboxEvent(
        event_id=event_id,
        kind="monitor_error",
        tent_slug=tent_slug,
        tent_name=tent_name,
        booking_url=booking_url,
        reason=details[:1024],
        created_at=timestamp,
        next_attempt_at=timestamp,
        total_messages=1,
    )
    state.outbox.setdefault(event_id, event)
    return state.outbox[event_id]


def prune_delivered(state: State, keep: int = 500) -> None:
    completed = sorted(
        (
            event for event in state.outbox.values()
            if event.status == "delivered" and event.completed_at
        ),
        key=lambda event: event.completed_at or "",
        reverse=True,
    )
    for event in completed[keep:]:
        state.outbox.pop(event.event_id, None)
