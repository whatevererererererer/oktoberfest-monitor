"""Read-only structured probe for Käfer's official 2026 reservation app."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

from ..config import KaeferConfig
from ..probe import ProbeDiagnostics, ProbeResult
from .headless import SAFARI_MACOS_USER_AGENT

_EXPECTED_SLOTS = {
    0: (1049, "11:30:00", "15:00:00", "Mittag"),
    1: (1050, "15:30:00", "19:00:00", "Nachmittag"),
}
_EXPECTED_AREAS = ("Haus innen", "Überdachter Freisitz")
_MAX_SLOT_RESPONSE_BYTES = 1_000_000


def _unknown(error_class: str) -> ProbeResult:
    return ProbeResult(
        "unknown",
        diagnostics=ProbeDiagnostics(
            health="degraded", page_type="booking", error_class=error_class
        ),
    )


def _nonnegative_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("invalid count")
    return value


def _strict_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("invalid integer")
    return value


def _sizes(value: Any) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, str):
        raise ValueError("invalid table sizes")
    raw = [part.strip() for part in value.split(",") if part.strip()]
    if not raw or any(not part.isdigit() for part in raw):
        raise ValueError("invalid table sizes")
    sizes = tuple(int(part) for part in raw)
    if any(size <= 0 or size > 200 for size in sizes) or len(sizes) != len(set(sizes)):
        raise ValueError("invalid table sizes")
    return sizes


def _row_date(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid slot date")
    parsed = datetime.fromisoformat(value)
    if parsed.time().isoformat() != "00:00:00":
        raise ValueError("invalid slot date")
    return parsed.date().isoformat()


def parse_slot_payload(payload: Any, iso_date: str) -> ProbeResult:
    if not isinstance(payload, list) or not payload:
        return _unknown("slot_schema_invalid")
    try:
        target = date.fromisoformat(iso_date).isoformat()
        dates: set[str] = set()
        target_rows: dict[int, dict[str, Any]] = {}
        for raw in payload:
            if not isinstance(raw, dict):
                raise ValueError("invalid slot")
            row_date = _row_date(raw.get("rDatum"))
            dates.add(row_date)
            if row_date != target:
                continue
            time_id = _strict_int(raw.get("zeit_ID"))
            if time_id not in _EXPECTED_SLOTS or time_id in target_rows:
                raise ValueError("unexpected target slot")
            target_rows[time_id] = raw
        if set(target_rows) != set(_EXPECTED_SLOTS):
            return _unknown("target_slots_incomplete")

        shifts: list[str] = []
        day_counts: set[int] = set()
        total_area_count = 0
        for time_id, (
            expected_slot_id,
            expected_start,
            expected_end,
            label,
        ) in _EXPECTED_SLOTS.items():
            raw = target_rows[time_id]
            if _strict_int(raw.get("slot_id")) != expected_slot_id:
                raise ValueError("unexpected target slot id")
            if raw.get("res_ab") != expected_start or raw.get("res_bis") != expected_end:
                raise ValueError("unexpected target time")
            inside_count = _nonnegative_int(raw.get("anz"))
            outside_count = _nonnegative_int(raw.get("anzBereich"))
            day_count = _nonnegative_int(raw.get("anzDat"))
            day_counts.add(day_count)
            total_area_count += inside_count + outside_count
            inside_sizes = _sizes(raw.get("tische"))
            outside_sizes = _sizes(raw.get("tische1"))
            if raw.get("bereich") != _EXPECTED_AREAS[0] or raw.get("bereich1") != _EXPECTED_AREAS[1]:
                raise ValueError("unexpected area label")
            areas: list[str] = []
            if inside_count:
                if not inside_sizes:
                    raise ValueError("inside capacity lacks sizes")
                areas.append(_EXPECTED_AREAS[0])
            if outside_count:
                if not outside_sizes:
                    raise ValueError("outside capacity lacks sizes")
                areas.append(_EXPECTED_AREAS[1])
            if areas:
                start = expected_start[:5]
                end = expected_end[:5]
                shifts.append(f"{label} ({start}–{end}, {' / '.join(areas)})")

        if len(day_counts) != 1:
            raise ValueError("inconsistent day capacity")
        day_count = next(iter(day_counts))
        if (day_count == 0) != (total_area_count == 0):
            raise ValueError("contradictory capacity")
    except (TypeError, ValueError):
        return _unknown("slot_schema_invalid")

    common = dict(
        page_type="booking",
        date_control_count=1,
        plausible_date_option_count=len(dates),
        target_found=True,
        target_enabled=True,
        shift_control_count=1,
        shift_control_found=True,
        update_confirmed=True,
    )
    if not shifts:
        return ProbeResult(
            "unavailable",
            shifts=(),
            diagnostics=ProbeDiagnostics(
                health="healthy",
                shift_count=0,
                unavailable_confirmed=True,
                **common,
            ),
        )
    return ProbeResult(
        "available",
        shifts=tuple(shifts),
        diagnostics=ProbeDiagnostics(
            health="healthy", shift_count=len(shifts), **common
        ),
    )


def _normalized_endpoint(value: str) -> tuple[str, str, int | None, str]:
    parsed = urlparse(value)
    path = re.sub(r"/+", "/", parsed.path)
    return parsed.scheme, parsed.hostname or "", parsed.port, path


def fetch(cfg: KaeferConfig, iso_date: str, browser) -> ProbeResult:
    expected_endpoint = _normalized_endpoint(cfg.slot_endpoint)
    context = browser.new_context(
        user_agent=SAFARI_MACOS_USER_AGENT,
        locale="de-DE",
        viewport={"width": 1280, "height": 1100},
        service_workers="block",
    )
    blocked_methods: set[str] = set()

    def enforce_read_only(route, request) -> None:
        try:
            method = str(request.method).upper()
        except Exception:
            method = "<UNKNOWN>"
        if method == "GET":
            route.continue_()
            return
        blocked_methods.add((method or "<EMPTY>")[:16])
        route.abort()

    try:
        # Browser routing does not see requests handled by service workers, so
        # they are disabled above before this strict GET-only boundary is added.
        context.route("**/*", enforce_read_only)
        page = context.new_page()
        try:
            # Register both waiters before navigation. The response event
            # identifies the exact endpoint; the request-finished event has its
            # own deadline and proves that this exact GET completed before the
            # otherwise unbounded body accessor.
            with page.expect_request_finished(
                lambda request: (
                    request.method == "GET"
                    and _normalized_endpoint(request.url) == expected_endpoint
                ),
                timeout=cfg.slot_timeout_ms,
            ) as finished_info:
                with page.expect_response(
                    lambda response: (
                        response.request.method == "GET"
                        and _normalized_endpoint(response.url) == expected_endpoint
                    ),
                    timeout=cfg.slot_timeout_ms,
                ) as response_info:
                    page.goto(
                        cfg.url_template,
                        wait_until=cfg.wait_until,
                        timeout=cfg.navigation_timeout_ms,
                    )
                response = response_info.value
                if blocked_methods:
                    raise ValueError("non-GET request blocked")
                # Header checks run while the bounded request-finished waiter
                # is still armed, so an oversized declared body is rejected
                # without first waiting for its complete download.
                if response.status != 200:
                    raise ValueError(
                        f"slot endpoint returned HTTP {response.status}"
                    )
                content_type = response.headers.get("content-type", "").casefold()
                if not content_type.startswith("application/json"):
                    raise ValueError("slot endpoint did not return JSON")
                raw_content_length = response.headers.get("content-length")
                if raw_content_length is not None:
                    if not raw_content_length.isdigit():
                        raise ValueError(
                            "slot response content length is invalid"
                        )
                    if int(raw_content_length) > _MAX_SLOT_RESPONSE_BYTES:
                        raise ValueError("slot response too large")
            if finished_info.value is not response.request:
                raise ValueError("slot response completion mismatch")
        except Exception as exc:
            if blocked_methods:
                raise ValueError("non-GET request blocked") from exc
            raise
        if blocked_methods:
            raise ValueError("non-GET request blocked")
        try:
            body = response.body()
            if len(body) > _MAX_SLOT_RESPONSE_BYTES:
                raise ValueError("slot response too large")
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("slot response JSON is invalid") from exc
        except Exception as exc:
            if blocked_methods:
                raise ValueError("non-GET request blocked") from exc
            raise
        if blocked_methods:
            raise ValueError("non-GET request blocked")
        return parse_slot_payload(payload, iso_date)
    finally:
        context.close()
