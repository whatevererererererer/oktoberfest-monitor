from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SCHEMA_VERSION = 3

Availability = Literal["available", "unavailable", "unknown", "error"]
Health = Literal["healthy", "degraded", "error", "unknown"]
OutboxStatus = Literal["pending", "delivered", "dead_letter"]
OutboxKind = Literal["availability", "monitor_error"]


class _StateModel(BaseModel):
    # State snapshots are long-lived operational history. Unknown fields from a
    # newer minor producer must survive a load/save round-trip instead of being
    # silently discarded by Pydantic's default ``extra="ignore"`` behaviour.
    model_config = ConfigDict(extra="allow")


class TentDateState(_StateModel):
    # Last reliable business state. Unknown/error observations never overwrite it.
    status: Availability = "unknown"
    observed_status: Availability = "unknown"
    health: Health = "unknown"
    last_check: str | None = None
    last_change: str | None = None
    shifts: list[str] = Field(default_factory=list)
    shift_keys: list[str] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    consecutive_degraded: int = 0
    consecutive_errors: int = 0
    alert_sequence: int = 0
    # Legacy snapshots can contain plausible-looking values without proof that
    # the date and shifts were correlated. They remain visible as history but
    # are never treated as a verified notification baseline.
    baseline_verified: bool = False
    last_reliable_at: str | None = None
    last_reliable_diagnostics: dict[str, Any] = Field(default_factory=dict)
    # Set only when a verified available baseline temporarily loses *shift*
    # evidence. A later verified observation must then be reported once even if
    # it contains the same labels as the retained baseline.
    availability_evidence_lost: bool = False

    @model_validator(mode="after")
    def _business_and_provenance_invariants(self) -> "TentDateState":
        if self.status == "available":
            if not self.shifts or not self.shift_keys:
                raise ValueError("available baseline requires shifts and shift_keys")
            if len(self.shifts) != len(self.shift_keys):
                raise ValueError("available baseline shift labels/keys must align")
            if any(not value.strip() for value in self.shifts + self.shift_keys):
                raise ValueError("available baseline shift labels/keys must be non-empty")
            if len(set(self.shift_keys)) != len(self.shift_keys):
                raise ValueError("available baseline shift_keys must be unique")
        elif self.shifts or self.shift_keys:
            raise ValueError("only an available baseline may retain shifts")

        if self.baseline_verified:
            if self.status not in {"available", "unavailable"}:
                raise ValueError("verified baseline requires a reliable status")
            if not self.last_reliable_at or not self.last_reliable_diagnostics:
                raise ValueError("verified baseline requires reliable provenance")
        if self.availability_evidence_lost and not (
            self.baseline_verified and self.status == "available"
        ):
            raise ValueError(
                "shift evidence loss requires a verified available baseline"
            )
        return self


class TentState(_StateModel):
    dates: dict[str, TentDateState] = Field(default_factory=dict)
    consecutive_failures: int = 0
    consecutive_degraded: int = 0
    consecutive_unhealthy: int = 0
    last_success_at: str | None = None
    last_error: str | None = None
    failure_incident_open: bool = False
    failure_incident_sequence: int = 0
    failure_incident_kind: Literal["degraded", "error"] | None = None


class OutboxEvent(_StateModel):
    event_id: str
    kind: OutboxKind = "availability"
    tent_slug: str
    tent_name: str
    iso_date: str | None = None
    booking_url: str | None = None
    reason: str = "available"
    shifts: list[str] = Field(default_factory=list)
    new_shifts: list[str] = Field(default_factory=list)
    burst: bool = False
    created_at: str
    status: OutboxStatus = "pending"
    next_index: int = 0
    total_messages: int = 1
    next_attempt_at: str | None = None
    attempts_by_index: dict[str, int] = Field(default_factory=dict)
    last_error: str | None = None
    last_error_class: str | None = None
    last_request_id: str | None = None
    quota_limit: int | None = None
    quota_remaining: int | None = None
    quota_reset: int | None = None
    completed_at: str | None = None
    requeue_count: int = 0
    rate_limit_deferrals_by_index: dict[str, int] = Field(default_factory=dict)
    quarantine_reason: str | None = None
    # Only a data-minimised description of malformed persisted data belongs
    # here; never duplicate its arbitrary values, URLs, credentials or HTML.
    quarantined_payload: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _structural_invariants(self) -> "OutboxEvent":
        if not self.event_id.strip() or not self.tent_slug.strip() or not self.tent_name.strip():
            raise ValueError("outbox identifiers must be non-empty")
        try:
            parse_iso(self.created_at)
            if self.next_attempt_at is not None:
                parse_iso(self.next_attempt_at)
            if self.completed_at is not None:
                parse_iso(self.completed_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("outbox timestamps must be valid ISO timestamps") from exc
        if self.total_messages < 1 or not 0 <= self.next_index <= self.total_messages:
            raise ValueError("outbox cursor is outside the message range")
        if self.status == "pending" and self.next_index >= self.total_messages:
            raise ValueError("pending outbox event has no remaining message")
        if self.status == "delivered" and self.next_index != self.total_messages:
            raise ValueError("delivered outbox event has an incomplete cursor")
        if self.burst != (self.total_messages == 8):
            raise ValueError("outbox burst flag and message count disagree")
        if self.kind == "monitor_error" and (self.burst or self.total_messages != 1):
            raise ValueError("monitor error events must be single messages")
        for counters in (
            self.attempts_by_index,
            self.rate_limit_deferrals_by_index,
        ):
            for raw_index, count in counters.items():
                try:
                    index = int(raw_index)
                except (TypeError, ValueError) as exc:
                    raise ValueError("outbox counter index must be an integer") from exc
                if (
                    str(index) != str(raw_index)
                    or not 0 <= index < self.total_messages
                    or isinstance(count, bool)
                    or count < 0
                ):
                    raise ValueError("outbox counter is invalid")
        if (
            self.quota_limit is not None
            and self.quota_remaining is not None
            and self.quota_remaining > self.quota_limit
        ):
            raise ValueError("outbox quota metadata is inconsistent")
        return self


class PushoverQuotaState(_StateModel):
    limit: int | None = None
    remaining: int | None = None
    reset: int | None = None
    observed_at: str | None = None


class State(_StateModel):
    schema_version: int = SCHEMA_VERSION
    tents: dict[str, TentState] = Field(default_factory=dict)
    outbox: dict[str, OutboxEvent] = Field(default_factory=dict)
    pushover_quota: dict[str, PushoverQuotaState] = Field(default_factory=dict)
    workflow_last_run_at: str | None = None
    workflow_started_at: str | None = None
    workflow_finished_at: str | None = None
    workflow_duration_seconds: float | None = None
    producer_revision: str | None = None

    @model_validator(mode="after")
    def _supported_schema_and_outbox_keys(self) -> "State":
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported state schema {self.schema_version}; expected {SCHEMA_VERSION}"
            )
        mismatches = [
            key for key, event in self.outbox.items() if key != event.event_id
        ]
        if mismatches:
            raise ValueError("outbox map keys must match event_id")
        return self


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _looks_like_verified_v2_baseline(date_state: dict[str, Any]) -> bool:
    """Recognise evidence written by schema-v2's reliable probe path.

    A v1 snapshot that was merely wrapped in v2 has no canonical shift keys or
    structured successful diagnostics, so it intentionally stays unverified.
    """
    status = date_state.get("status", "unknown")
    observed = date_state.get("observed_status", status)
    shifts = date_state.get("shifts") or []
    shift_keys = date_state.get("shift_keys") or []
    diagnostics = date_state.get("diagnostics")
    if not isinstance(diagnostics, dict):
        diagnostics = {}

    # Non-empty canonical keys were only written by the reliable v2 apply path.
    if (
        status == "available"
        and shifts
        and shift_keys
        and len(shifts) == len(shift_keys)
        and len(set(shift_keys)) == len(shift_keys)
        and all(isinstance(value, str) and value.strip() for value in shift_keys)
    ):
        return True

    if observed != status or date_state.get("health") != "healthy":
        return False
    common = (
        diagnostics.get("health") == "healthy"
        and diagnostics.get("page_type") == "booking"
        and diagnostics.get("date_control_count") == 1
        and isinstance(diagnostics.get("plausible_date_option_count"), int)
        and diagnostics.get("plausible_date_option_count", 0) > 0
    )
    if not common:
        return False
    if status == "available":
        return bool(
            shifts
            and diagnostics.get("target_found") is True
            and diagnostics.get("target_enabled") is not False
            and diagnostics.get("shift_control_count") == 1
            and diagnostics.get("shift_control_found") is True
            and diagnostics.get("update_confirmed") is True
            and diagnostics.get("shift_count") == len(shifts)
            and not diagnostics.get("error_class")
        )
    if status == "unavailable":
        return diagnostics.get("target_found") is False
    return False


def _migration_diagnostics(
    existing: Any,
    *,
    previous_status: str,
    previous_shifts: list[Any],
    previous_observed_status: str,
    source_version: int,
    invalidate_current_observation: bool,
) -> dict[str, Any]:
    diagnostics = dict(existing) if isinstance(existing, dict) else {}
    diagnostics["migration"] = "legacy_snapshot_unverified"
    diagnostics["migration_source_schema"] = source_version
    diagnostics["migration_previous_status"] = previous_status
    diagnostics["migration_previous_shifts"] = [
        str(value)[:160] for value in previous_shifts[:30]
    ]
    diagnostics["migration_previous_observed_status"] = previous_observed_status
    if invalidate_current_observation:
        diagnostics["health"] = "degraded"
        diagnostics["error_class"] = "legacy_snapshot_unverified"
    return diagnostics


def _quarantine_event(
    map_key: str,
    raw_event: Any,
    reason_code: Literal["invalid_event", "outbox_not_mapping"],
) -> OutboxEvent:
    """Replace an untrusted outbox object with a value-free dead letter.

    The digest may depend on the raw object for stable identity, but no raw map
    key, event id, field name or value is copied into the persisted event.
    """
    try:
        serialised = json.dumps(raw_event, ensure_ascii=True, sort_keys=True)
    except (TypeError, ValueError):
        serialised = type(raw_event).__name__
    digest = sha256(
        f"quarantined-outbox|{map_key}|{serialised}".encode("utf-8")
    ).hexdigest()[:24]
    event_id = f"quarantine-{digest}"

    raw_dict = raw_event if isinstance(raw_event, dict) else {}
    original_event_id = raw_dict.get("event_id")
    field_name_lengths = sorted(len(str(key)) for key in raw_dict)[:80]
    metadata: dict[str, Any] = {
        "payload_is_mapping": isinstance(raw_event, dict),
        "field_count": len(raw_dict),
        "field_name_lengths": field_name_lengths,
        "allowed_field_names_present": sorted(
            key
            for key in OutboxEvent.model_fields
            if key in raw_dict
        ),
        "map_key_length": len(map_key),
        "event_id_is_string": isinstance(original_event_id, str),
        "event_id_length": (
            len(original_event_id) if isinstance(original_event_id, str) else None
        ),
        "event_id_key_matched": (
            isinstance(original_event_id, str) and map_key == original_event_id
        ),
    }
    safe_reason = (
        "persisted_outbox_not_mapping"
        if reason_code == "outbox_not_mapping"
        else "invalid_persisted_outbox_event"
    )

    return OutboxEvent(
        event_id=event_id,
        kind="availability",
        tent_slug="quarantined",
        tent_name="Quarantänisierter Outbox-Eintrag",
        reason="Persistierter Outbox-Eintrag wurde sicher quarantänisiert.",
        created_at="1970-01-01T00:00:00+00:00",
        status="dead_letter",
        next_attempt_at=None,
        last_error=safe_reason,
        last_error_class="invalid_persisted_event",
        quarantine_reason=safe_reason,
        quarantined_payload=metadata,
    )


def _validate_or_quarantine_outbox(raw_outbox: Any) -> dict[str, Any]:
    if not isinstance(raw_outbox, dict):
        quarantined = _quarantine_event(
            "<outbox>", raw_outbox, "outbox_not_mapping"
        )
        return {quarantined.event_id: quarantined.model_dump()}

    validated: dict[str, Any] = {}
    for raw_key, raw_event in raw_outbox.items():
        map_key = str(raw_key)
        try:
            event = OutboxEvent.model_validate(raw_event)
            if map_key != event.event_id:
                raise ValueError("outbox map key does not match event_id")
        except (ValidationError, TypeError, ValueError):
            quarantined = _quarantine_event(
                map_key,
                raw_event,
                "invalid_event",
            )
            # A cryptographic collision is fantastically unlikely. Still avoid
            # silently replacing an earlier quarantine record.
            while quarantined.event_id in validated:
                collision_digest = sha256(
                    f"{quarantined.event_id}|collision".encode("utf-8")
                ).hexdigest()[:24]
                quarantined.event_id = f"quarantine-{collision_digest}"
            validated[quarantined.event_id] = quarantined.model_dump()
        else:
            validated[event.event_id] = event.model_dump()
    return validated


def _migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate legacy snapshots without discarding any historical fields."""
    if not isinstance(raw, dict):
        raise ValueError("state root must be a JSON object")
    try:
        version = int(raw.get("schema_version", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("state schema_version must be an integer") from exc
    if version < 1:
        raise ValueError(f"unsupported state schema {version}")
    if version > SCHEMA_VERSION:
        raise ValueError(
            f"state schema {version} is newer than supported schema {SCHEMA_VERSION}"
        )

    migrated = deepcopy(raw)
    migrated["schema_version"] = SCHEMA_VERSION
    migrated["outbox"] = _validate_or_quarantine_outbox(
        migrated.setdefault("outbox", {})
    )
    migrated.setdefault("pushover_quota", {})
    migrated.setdefault("workflow_started_at", None)
    migrated.setdefault("workflow_finished_at", None)
    migrated.setdefault("workflow_duration_seconds", None)
    migrated.setdefault("producer_revision", None)
    tents = migrated.setdefault("tents", {})
    if not isinstance(tents, dict):
        raise ValueError("state tents must be a JSON object")
    for tent in tents.values():
        if not isinstance(tent, dict):
            raise ValueError("each tent state must be a JSON object")
        tent.setdefault("consecutive_degraded", 0)
        tent.setdefault(
            "consecutive_unhealthy",
            max(
                int(tent.get("consecutive_failures", 0) or 0),
                int(tent.get("consecutive_degraded", 0) or 0),
            ),
        )
        tent.setdefault("failure_incident_open", False)
        tent.setdefault("failure_incident_sequence", 0)
        if "failure_incident_kind" not in tent:
            if tent.get("failure_incident_open") and int(
                tent.get("consecutive_failures", 0) or 0
            ) > 0:
                tent["failure_incident_kind"] = "error"
            elif tent.get("failure_incident_open") and int(
                tent.get("consecutive_degraded", 0) or 0
            ) > 0:
                tent["failure_incident_kind"] = "degraded"
            else:
                tent["failure_incident_kind"] = None
        for date_state in tent.setdefault("dates", {}).values():
            if not isinstance(date_state, dict):
                raise ValueError("each tent date state must be a JSON object")
            legacy_status = date_state.get("status", "unknown")
            if version == 1 and legacy_status == "error":
                # Error was previously destructive. Preserve it as the observation,
                # but restore the reliable baseline to unknown.
                date_state["status"] = "unknown"
                date_state["observed_status"] = "error"
                date_state["health"] = "error"
            elif version == 1:
                date_state.setdefault("observed_status", legacy_status)
                # Old successful-looking snapshots have no control/update evidence.
                date_state.setdefault("health", "unknown")
            else:
                date_state.setdefault("observed_status", legacy_status)
                date_state.setdefault("health", "unknown")
            date_state.setdefault("shift_keys", [])
            date_state.setdefault("diagnostics", {})
            date_state.setdefault("consecutive_degraded", 0)
            date_state.setdefault("consecutive_errors", 0)
            date_state.setdefault("alert_sequence", 0)
            if version < SCHEMA_VERSION:
                reliable_at = (
                    date_state.get("last_check")
                    if date_state.get("observed_status") == date_state.get("status")
                    else date_state.get("last_change")
                )
                verified = bool(
                    version == 2
                    and _looks_like_verified_v2_baseline(date_state)
                    and isinstance(reliable_at, str)
                    and reliable_at
                )
                date_state["baseline_verified"] = verified
                date_state["last_reliable_at"] = reliable_at if verified else None
                if verified:
                    current_diagnostics = date_state.get("diagnostics")
                    if (
                        date_state.get("observed_status") == date_state.get("status")
                        and isinstance(current_diagnostics, dict)
                        and current_diagnostics.get("health") == "healthy"
                    ):
                        reliable_diagnostics = dict(current_diagnostics)
                    else:
                        reliable_diagnostics = {
                            "migration": "schema_v2_canonical_shift_provenance"
                        }
                    date_state["last_reliable_diagnostics"] = reliable_diagnostics
                else:
                    date_state["last_reliable_diagnostics"] = {}
                date_state["availability_evidence_lost"] = False
            else:
                date_state.setdefault("baseline_verified", False)
                date_state.setdefault("last_reliable_at", None)
                date_state.setdefault("last_reliable_diagnostics", {})
                date_state.setdefault("availability_evidence_lost", False)

            # A schema marker must never make an empty availability baseline
            # healthy. Keep its historical fields, but make the lack of proof
            # explicit so the first later real shift can alert.
            empty_available = (
                date_state.get("status") == "available"
                and not (date_state.get("shifts") or [])
            )
            if empty_available:
                date_state["baseline_verified"] = False

            observed = str(date_state.get("observed_status", "unknown"))
            baseline_verified = bool(date_state.get("baseline_verified"))
            baseline_status = str(date_state.get("status", "unknown"))
            if not baseline_verified and baseline_status in {"available", "unavailable"}:
                previous_shifts = list(date_state.get("shifts") or [])
                date_state["diagnostics"] = _migration_diagnostics(
                    date_state.get("diagnostics"),
                    previous_status=baseline_status,
                    previous_shifts=previous_shifts,
                    previous_observed_status=observed,
                    source_version=version,
                    invalidate_current_observation=observed
                    in {"available", "unavailable"},
                )
                # An unverified value is historical context, not a reliable
                # business baseline. Retaining it as ``available`` (especially
                # with an empty list) would violate the schema-v3 invariant.
                date_state["status"] = "unknown"
                date_state["shifts"] = []
                date_state["shift_keys"] = []
                date_state["last_reliable_at"] = None
                date_state["last_reliable_diagnostics"] = {}
                date_state["availability_evidence_lost"] = False
                if observed in {"available", "unavailable"}:
                    date_state["observed_status"] = "unknown"
                    date_state["health"] = "degraded"
            elif baseline_status in {"unknown", "error"} and (
                date_state.get("shifts") or date_state.get("shift_keys")
            ):
                previous_shifts = list(date_state.get("shifts") or [])
                diagnostics = dict(date_state.get("diagnostics") or {})
                diagnostics["migration_discarded_unreliable_shifts"] = [
                    str(value)[:160] for value in previous_shifts[:30]
                ]
                date_state["diagnostics"] = diagnostics
                date_state["shifts"] = []
                date_state["shift_keys"] = []
    return migrated


def load(path: Path) -> State:
    if not path.exists():
        return State()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return State.model_validate(_migrate(raw))


def save(path: Path, state: State) -> None:
    """Atomically replace the state file in its own directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Models are mutable while an observation or message part is applied.
    # Revalidate at the persistence boundary so no impossible baseline/cursor
    # can become durable if a future transition regresses.
    validated = State.model_validate(state.model_dump())
    payload = json.dumps(
        validated.model_dump(), indent=2, sort_keys=True, ensure_ascii=False
    )
    temp_path = path.with_name(f".{path.name}.tmp")
    try:
        with temp_path.open(mode="w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temp_path, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                # Windows virus scanners/indexers may briefly hold the old file.
                time.sleep(0.02 * (attempt + 1))
    finally:
        if temp_path.exists():
            temp_path.unlink()
