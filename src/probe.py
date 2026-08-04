"""Structured, privacy-preserving results returned by reservation probes."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterator, Literal

ProbeStatus = Literal["available", "unavailable", "unknown", "error"]
ProbeHealth = Literal["healthy", "degraded", "error"]
PageType = Literal["booking", "bot", "login", "error", "unknown"]


@dataclass(frozen=True, slots=True)
class ProbeDiagnostics:
    """Small diagnostics only; never store page source, cookies, or form values."""

    health: ProbeHealth
    page_type: PageType = "unknown"
    date_control_count: int = 0
    plausible_date_option_count: int = 0
    target_found: bool = False
    target_enabled: bool | None = None
    shift_control_count: int = 0
    shift_control_found: bool = False
    update_confirmed: bool = False
    shift_count: int = 0
    error_class: str | None = None
    detail: str | None = None

    def model_dump(self, *, mode: str = "python") -> dict[str, object]:
        """Pydantic-compatible serialization hook used by state integration."""

        if mode not in {"python", "json"}:
            raise ValueError(f"unsupported dump mode: {mode!r}")
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One target date's observation and its supporting diagnostics.

    ``__iter__`` temporarily preserves the old ``status, shifts`` unpacking
    contract while callers migrate to the named fields.
    """

    status: ProbeStatus
    # None means "not reliably observed" and instructs state handling to
    # preserve the last good list. An empty tuple is a proven unavailable day.
    shifts: tuple[str, ...] | None = None
    diagnostics: ProbeDiagnostics = field(
        default_factory=lambda: ProbeDiagnostics(health="degraded")
    )

    def __post_init__(self) -> None:
        if self.status == "available" and not self.shifts:
            raise ValueError("available probe results require at least one shift")
        if self.status == "unavailable" and self.shifts != ():
            raise ValueError("unavailable probe results require a proven empty shift tuple")
        if self.status in {"unknown", "error"} and self.shifts is not None:
            raise ValueError("unknown/error probe results must preserve shifts with None")
        expected_health: ProbeHealth = (
            "error" if self.status == "error" else "degraded" if self.status == "unknown" else "healthy"
        )
        if self.diagnostics.health != expected_health:
            raise ValueError(
                f"status={self.status!r} requires diagnostics.health={expected_health!r}"
            )

    def __iter__(self) -> Iterator[object]:
        yield self.status
        yield None if self.shifts is None else list(self.shifts)

    def diagnostic_dict(self) -> dict[str, object]:
        return asdict(self.diagnostics)
