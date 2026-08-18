"""Read Bartls Flößerstadl's public reservation options without submitting."""
from __future__ import annotations

import json
import re
import time
import unicodedata
from datetime import date, datetime
from typing import Any

import httpx

from ..config import FloesserstadlConfig
from ..probe import ProbeDiagnostics, ProbeResult
from .api import DEFAULT_HEADERS

_MAX_HTML_BYTES = 2_000_000
_TOTAL_DEADLINE_SECONDS = 20.0
_FORM_FIELDS_RE = re.compile(r'"formFields"\s*:')
_DATE_TOKEN_RE = re.compile(
    r"(?<!\d)(?P<date>\d{2}\.\d{2}\.(?:\d{4}|\d{2}))(?!\d)"
)
_FESTIVAL_START = date(2026, 9, 19)
_FESTIVAL_END = date(2026, 10, 4)
_WEEKDAYS = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)
_FIELDS = {
    "Reservierung am Mittag": ("Mittag", "Mittagstisch", "11:00", "16:30"),
    "Reservierung am Abend": ("Abend", "Abendtisch", "17:30", "23:00"),
}


def _unknown(error_class: str) -> ProbeResult:
    return ProbeResult(
        "unknown",
        diagnostics=ProbeDiagnostics(
            health="degraded", page_type="booking", error_class=error_class
        ),
    )


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def _option_pattern(option_word: str, start: str, end: str) -> re.Pattern[str]:
    weekdays = "|".join(map(re.escape, _WEEKDAYS))
    return re.compile(
        rf"(?P<weekday>{weekdays}), "
        rf"(?P<date>\d{{2}}\.\d{{2}}\.\d{{2}}) "
        rf"(?:-|\u2013) {re.escape(option_word)}"
        rf"(?: {re.escape(start)} (?:-|\u2013|bis) {re.escape(end)} Uhr)?"
    )


def _has_festival_date_option(options: Any) -> bool:
    if not isinstance(options, list):
        return False
    for option in options:
        if not isinstance(option, str):
            continue
        for match in _DATE_TOKEN_RE.finditer(_normalized(option)):
            token = match.group("date")
            date_format = "%d.%m.%Y" if len(token) == 10 else "%d.%m.%y"
            try:
                parsed = datetime.strptime(token, date_format).date()
            except ValueError:
                continue
            if _FESTIVAL_START <= parsed <= _FESTIVAL_END:
                return True
    return False


def _is_reservation_like_field(field: dict[str, Any]) -> bool:
    title = field.get("title")
    if isinstance(title, str) and _normalized(title).casefold().startswith(
        "reservierung am"
    ):
        return True
    description = field.get("description")
    if isinstance(description, str) and _normalized(description).casefold().startswith(
        "verfügbare reservierungstage"
    ):
        return True
    return _has_festival_date_option(field.get("options"))


def _raise_if_deadline_exceeded(started_at: float) -> None:
    if time.monotonic() - started_at > _TOTAL_DEADLINE_SECONDS:
        raise TimeoutError("reservation page total deadline exceeded")


def _declared_content_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        length = int(raw)
    except ValueError as exc:
        raise ValueError("invalid reservation page content length") from exc
    if length < 0:
        raise ValueError("invalid reservation page content length")
    return length


def _read_bounded_html(
    cfg: FloesserstadlConfig,
    client: httpx.Client,
) -> str:
    started_at = time.monotonic()
    with client.stream(
        "GET",
        cfg.url_template,
        headers=DEFAULT_HEADERS,
        timeout=_TOTAL_DEADLINE_SECONDS,
    ) as response:
        response.raise_for_status()
        _raise_if_deadline_exceeded(started_at)
        declared_length = _declared_content_length(response)
        if declared_length is not None and declared_length > _MAX_HTML_BYTES:
            raise ValueError("reservation page content length exceeds limit")

        body = bytearray()
        # Do not ask httpx to coalesce small chunks: checking every delivered
        # chunk is what makes the monotonic deadline effective against a peer
        # that keeps a per-read timeout alive with a slow byte drip.
        for chunk in response.iter_bytes():
            _raise_if_deadline_exceeded(started_at)
            if len(body) + len(chunk) > _MAX_HTML_BYTES:
                raise ValueError("reservation page body exceeds limit")
            body.extend(chunk)
        _raise_if_deadline_exceeded(started_at)

    try:
        return bytes(body).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("reservation page is not valid UTF-8") from exc


def extract_form_fields(html: str) -> Any:
    matches = list(_FORM_FIELDS_RE.finditer(html))
    if len(matches) != 1:
        raise ValueError("ambiguous formFields payload")
    remainder = html[matches[0].end() :].lstrip()
    fields, _ = json.JSONDecoder().raw_decode(remainder)
    return fields


def parse_form_fields(payload: Any, iso_date: str) -> ProbeResult:
    if not isinstance(payload, list) or not payload:
        return _unknown("reservation_form_schema_invalid")
    try:
        target_date = date.fromisoformat(iso_date)
        if not (_FESTIVAL_START <= target_date <= _FESTIVAL_END):
            raise ValueError("target outside festival")
        selected: dict[str, dict[str, Any]] = {}
        for raw in payload:
            if not isinstance(raw, dict):
                raise ValueError("invalid field")
            title = raw.get("title")
            if title not in _FIELDS:
                if _is_reservation_like_field(raw):
                    raise ValueError("unexpected reservation field")
                continue
            if title in selected:
                raise ValueError("duplicate reservation field")
            selected[title] = raw
        if set(selected) != set(_FIELDS):
            raise ValueError("missing reservation field")

        plausible_dates: set[str] = set()
        shifts: list[str] = []
        for title, (shift, option_word, start, end) in _FIELDS.items():
            raw = selected[title]
            if raw.get("type") != "select":
                raise ValueError("reservation field is not a select")
            description = raw.get("description")
            if not isinstance(description, str):
                raise ValueError("missing reservation description")
            expected = f"Verfügbare Reservierungstage am {shift} von {start} – {end} Uhr"
            if _normalized(description).replace(" - ", " – ") != expected:
                raise ValueError("unexpected reservation description")
            options = raw.get("options")
            if not isinstance(options, list) or not options:
                raise ValueError("missing reservation options")
            field_dates: set[str] = set()
            sentinel = f"Ich möchte keinen {option_word}"
            sentinel_seen = False
            option_pattern = _option_pattern(option_word, start, end)
            for option in options:
                if not isinstance(option, str) or not option.strip():
                    raise ValueError("invalid reservation option")
                normalized = _normalized(option)
                if normalized == sentinel:
                    if sentinel_seen:
                        raise ValueError("duplicate reservation sentinel")
                    sentinel_seen = True
                    continue
                match = option_pattern.fullmatch(normalized)
                if match is None:
                    raise ValueError("invalid reservation date option")
                parsed = datetime.strptime(match.group("date"), "%d.%m.%y").date()
                if not (_FESTIVAL_START <= parsed <= _FESTIVAL_END):
                    raise ValueError("reservation date outside festival")
                if match.group("weekday") != _WEEKDAYS[parsed.weekday()]:
                    raise ValueError("reservation weekday does not match date")
                value = parsed.isoformat()
                if value in field_dates:
                    raise ValueError("duplicate reservation date")
                field_dates.add(value)
            if not sentinel_seen:
                raise ValueError("missing reservation sentinel")
            plausible_dates.update(field_dates)
            if iso_date in field_dates:
                shifts.append(f"{shift} ({start}–{end})")
    except (TypeError, ValueError):
        return _unknown("reservation_form_schema_invalid")

    if not plausible_dates:
        return _unknown("reservation_form_no_dates")

    common = dict(
        page_type="booking",
        date_control_count=1,
        plausible_date_option_count=len(plausible_dates),
    )
    if not shifts:
        return ProbeResult(
            "unavailable",
            shifts=(),
            diagnostics=ProbeDiagnostics(
                health="healthy", target_found=False, **common
            ),
        )
    return ProbeResult(
        "available",
        shifts=tuple(shifts),
        diagnostics=ProbeDiagnostics(
            health="healthy",
            target_found=True,
            target_enabled=True,
            shift_control_count=1,
            shift_control_found=True,
            update_confirmed=True,
            shift_count=len(shifts),
            **common,
        ),
    )


def fetch(
    cfg: FloesserstadlConfig,
    iso_date: str,
    client: httpx.Client,
) -> ProbeResult:
    html = _read_bounded_html(cfg, client)
    try:
        fields = extract_form_fields(html)
    except (json.JSONDecodeError, ValueError):
        return _unknown("reservation_form_schema_invalid")
    return parse_form_fields(fields, iso_date)
