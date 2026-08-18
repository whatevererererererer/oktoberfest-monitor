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
    # A structured feed may explicitly prove that no relevant target offer
    # exists, or expose the target while proving that its capacity is zero.
    # DOM-based Festzelt-OS probes prove absence through plausible date options.
    unavailable_confirmed: bool = False
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
        if self.status == "available":
            correlated = (
                self.diagnostics.page_type == "booking"
                and self.diagnostics.date_control_count == 1
                and self.diagnostics.plausible_date_option_count > 0
                and self.diagnostics.target_found
                and self.diagnostics.target_enabled is True
                and self.diagnostics.shift_control_count == 1
                and self.diagnostics.shift_control_found
                and self.diagnostics.update_confirmed
                and self.diagnostics.shift_count == len(self.shifts or ())
            )
            if not correlated:
                raise ValueError(
                    "available probe results require date-correlated shift evidence"
                )
        if self.status == "unavailable":
            proven_absent = (
                self.diagnostics.page_type == "booking"
                and self.diagnostics.date_control_count == 1
                and not self.diagnostics.target_found
                and (
                    self.diagnostics.plausible_date_option_count > 0
                    or self.diagnostics.unavailable_confirmed
                )
            )
            proven_zero_capacity = (
                self.diagnostics.page_type == "booking"
                and self.diagnostics.date_control_count == 1
                and self.diagnostics.plausible_date_option_count > 0
                and self.diagnostics.target_found
                and self.diagnostics.target_enabled is True
                and self.diagnostics.shift_control_count == 1
                and self.diagnostics.shift_control_found
                and self.diagnostics.update_confirmed
                and self.diagnostics.shift_count == 0
                and self.diagnostics.unavailable_confirmed
            )
            if not (proven_absent or proven_zero_capacity):
                raise ValueError(
                    "unavailable probe results require an absent target or confirmed zero capacity"
                )

    def __iter__(self) -> Iterator[object]:
        yield self.status
        yield None if self.shifts is None else list(self.shifts)

    def diagnostic_dict(self) -> dict[str, object]:
        return asdict(self.diagnostics)
