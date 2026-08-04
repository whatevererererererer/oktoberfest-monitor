from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = 2

Availability = Literal["available", "unavailable", "unknown", "error"]
Health = Literal["healthy", "degraded", "error", "unknown"]
OutboxStatus = Literal["pending", "delivered", "dead_letter"]
OutboxKind = Literal["availability", "monitor_error"]


class TentDateState(BaseModel):
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


class TentState(BaseModel):
    dates: dict[str, TentDateState] = Field(default_factory=dict)
    consecutive_failures: int = 0
    consecutive_degraded: int = 0
    last_success_at: str | None = None
    last_error: str | None = None
    failure_incident_open: bool = False
    failure_incident_sequence: int = 0


class OutboxEvent(BaseModel):
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


class State(BaseModel):
    schema_version: int = SCHEMA_VERSION
    tents: dict[str, TentState] = Field(default_factory=dict)
    outbox: dict[str, OutboxEvent] = Field(default_factory=dict)
    workflow_last_run_at: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate legacy snapshots without discarding any historical fields."""
    version = int(raw.get("schema_version", 1))
    if version >= SCHEMA_VERSION:
        return raw

    migrated = dict(raw)
    migrated["schema_version"] = SCHEMA_VERSION
    migrated.setdefault("outbox", {})
    tents = migrated.setdefault("tents", {})
    for tent in tents.values():
        tent.setdefault("consecutive_degraded", 0)
        tent.setdefault("failure_incident_open", False)
        tent.setdefault("failure_incident_sequence", 0)
        for date_state in tent.setdefault("dates", {}).values():
            legacy_status = date_state.get("status", "unknown")
            if legacy_status == "error":
                # Error was previously destructive. Preserve it as the observation,
                # but restore the reliable baseline to unknown.
                date_state["status"] = "unknown"
                date_state["observed_status"] = "error"
                date_state["health"] = "error"
            else:
                date_state.setdefault("observed_status", legacy_status)
                # Old successful-looking snapshots have no control/update evidence.
                date_state.setdefault("health", "unknown")
            date_state.setdefault("shift_keys", [])
            date_state.setdefault("diagnostics", {"migration": "legacy_snapshot_unverified"})
            date_state.setdefault("consecutive_degraded", 0)
            date_state.setdefault("consecutive_errors", 0)
            date_state.setdefault("alert_sequence", 0)
    return migrated


def load(path: Path) -> State:
    if not path.exists():
        return State()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return State.model_validate(_migrate(raw))


def save(path: Path, state: State) -> None:
    """Atomically replace the state file in its own directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        state.model_dump(), indent=2, sort_keys=True, ensure_ascii=False
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
