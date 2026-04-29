from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

Availability = Literal["available", "unavailable", "unknown", "error"]


class TentDateState(BaseModel):
    status: Availability = "unknown"
    last_check: str | None = None
    last_change: str | None = None
    shifts: list[str] = Field(default_factory=list)


class TentState(BaseModel):
    dates: dict[str, TentDateState] = Field(default_factory=dict)
    consecutive_failures: int = 0
    last_success_at: str | None = None
    last_error: str | None = None


class State(BaseModel):
    tents: dict[str, TentState] = Field(default_factory=dict)
    workflow_last_run_at: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: Path) -> State:
    if not path.exists():
        return State()
    return State.model_validate_json(path.read_text(encoding="utf-8"))


def save(path: Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.model_dump(), indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
