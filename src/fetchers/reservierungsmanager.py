"""Structured, read-only adapter for official Reservierungsmanager widgets."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
from selectolax.parser import HTMLParser

from ..config import ReservierungsmanagerConfig
from ..probe import ProbeDiagnostics, ProbeResult
from .api import DEFAULT_HEADERS

_MAX_LANDING_BYTES = 1_000_000
_MAX_API_BYTES = 2_000_000
_MAX_REQUEST_SECONDS = 20.0
_MAX_FETCH_SECONDS = 60.0
_GATEWAY_NAME = "window.logbyte.gateway"
_PORTAL_SCRIPT_URL = "https://widget.reservierungsmanager.de/dist/latest/portal.js"
_INLINE_GATEWAY_RE = re.compile(
    r"\s*window\.logbyte\.gateway\s*\(\s*\{(?P<body>[\s\S]{1,8192})\}"
    r"\s*\)\s*;\s*"
)
_JS_IDENTIFIER_RE = re.compile(r"[A-Za-z_$][\w$]*")
_TOKEN_VALUE_RE = re.compile(r"[A-Za-z0-9._~-]{20,512}")
_EVENT_IDS_VALUE_RE = re.compile(
    r"[0-9]{1,12}(?:\s*,\s*[0-9]{1,12}){0,63}"
)
_DOM_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_:.-]{0,99}")
_GATEWAY_FIELDS = frozenset(
    {"widget", "src", "font", "authToken", "eventID", "view", "theme", "lang"}
)
_ISO_MIDNIGHT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T00:00:00(?:\.0+)?$")
_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3])[0-5]\d$")
_NAME_TIME_RE = re.compile(
    r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)\s*(?:-|–|bis)\s*"
    r"([01]?\d|2[0-3]):([0-5]\d)(?!\d)",
    re.IGNORECASE,
)
_FESTIVAL_START = date(2026, 9, 19)
_FESTIVAL_END = date(2026, 10, 4)
_SHIFT_PATTERNS = (
    ("Vormittag", re.compile(r"(?<!\w)(?:vormittags?|früh)(?!\w)")),
    ("Mittag", re.compile(r"(?<!\w)mittags?(?!\w)")),
    ("Nachmittag", re.compile(r"(?<!\w)nachmittags?(?!\w)")),
    ("Abend", re.compile(r"(?<!\w)abends?(?!\w)")),
    (
        "Ganztag",
        re.compile(r"(?<!\w)(?:ganztags?|ganztägig|ganzer\s+tag|ganzen\s+tag)(?!\w)"),
    ),
)


class WidgetSchemaError(ValueError):
    """The public widget or API no longer matches the validated contract."""


@dataclass(frozen=True, slots=True)
class WidgetConfig:
    token: str
    event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ParsedWidgetConfig:
    widget: str
    dom_id: str
    theme: str
    lang: str
    token: str
    event_ids: tuple[str, ...]


def _strip_js_comments(source: str) -> str:
    output: list[str] = []
    index = 0
    quote: str | None = None
    escaped = False
    while index < len(source):
        char = source[index]
        if quote is not None:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            if end < 0:
                break
            output.append("\n")
            index = end + 1
            continue
        if source.startswith("<!--", index) or source.startswith("-->", index):
            marker_length = 4 if source.startswith("<!--", index) else 3
            end = source.find("\n", index + marker_length)
            if end < 0:
                break
            output.append("\n")
            index = end + 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise WidgetSchemaError("unterminated JavaScript comment")
            output.extend("\n" if value == "\n" else " " for value in source[index : end + 2])
            index = end + 2
            continue
        output.append(char)
        if char in {"'", '"', "`"}:
            quote = char
        index += 1
    if quote is not None:
        raise WidgetSchemaError("unterminated JavaScript string")
    return "".join(output)


def _has_inert_ancestor(node) -> bool:
    ancestor = node.parent
    while ancestor is not None:
        if ancestor.tag in {"template", "noscript"}:
            return True
        ancestor = ancestor.parent
    return False


def _parse_js_string(body: str, position: int) -> tuple[str, int]:
    if position >= len(body) or body[position] not in {"'", '"'}:
        raise WidgetSchemaError("dynamic gateway value")
    quote = body[position]
    position += 1
    value: list[str] = []
    while position < len(body):
        char = body[position]
        if char == quote:
            parsed = "".join(value)
            if not parsed:
                raise WidgetSchemaError("empty gateway value")
            return parsed, position + 1
        # The live widget values need no escapes. Rejecting them keeps this a
        # deliberately tiny literal grammar instead of a partial JS parser.
        if char == "\\" or ord(char) < 0x20:
            raise WidgetSchemaError("escaped gateway value")
        value.append(char)
        position += 1
    raise WidgetSchemaError("unterminated gateway value")


def _parse_gateway_body(body: str) -> _ParsedWidgetConfig:
    fields: dict[str, str] = {}
    position = 0
    while True:
        while position < len(body) and body[position].isspace():
            position += 1
        if position >= len(body):
            break
        match = _JS_IDENTIFIER_RE.match(body, position)
        if match is None:
            raise WidgetSchemaError("invalid gateway property")
        field = match.group(0)
        if field not in _GATEWAY_FIELDS:
            raise WidgetSchemaError(f"unknown gateway property {field}")
        if field in fields:
            raise WidgetSchemaError(f"duplicate {field}")
        position = match.end()
        while position < len(body) and body[position].isspace():
            position += 1
        if position >= len(body) or body[position] != ":":
            raise WidgetSchemaError("invalid gateway property")
        position += 1
        while position < len(body) and body[position].isspace():
            position += 1
        if field == "src":
            function_name = "document.getElementById"
            if not body.startswith(function_name, position):
                raise WidgetSchemaError("dynamic gateway source")
            position += len(function_name)
            while position < len(body) and body[position].isspace():
                position += 1
            if position >= len(body) or body[position] != "(":
                raise WidgetSchemaError("invalid gateway source")
            position += 1
            while position < len(body) and body[position].isspace():
                position += 1
            value, position = _parse_js_string(body, position)
            while position < len(body) and body[position].isspace():
                position += 1
            if position >= len(body) or body[position] != ")":
                raise WidgetSchemaError("invalid gateway source")
            position += 1
            if not _DOM_ID_RE.fullmatch(value):
                raise WidgetSchemaError("invalid gateway DOM id")
        else:
            value, position = _parse_js_string(body, position)
        fields[field] = value
        while position < len(body) and body[position].isspace():
            position += 1
        if position >= len(body):
            break
        if body[position] != ",":
            raise WidgetSchemaError("invalid gateway separator")
        position += 1
        if not body[position:].strip():
            break

    for field in ("widget", "src", "authToken", "theme", "lang"):
        if field not in fields:
            raise WidgetSchemaError(f"missing {field}")
    for field in ("widget", "theme", "lang", "view", "font"):
        if field in fields and len(fields[field]) > 100:
            raise WidgetSchemaError(f"invalid {field}")
    token = fields["authToken"]
    if not _TOKEN_VALUE_RE.fullmatch(token):
        raise WidgetSchemaError("invalid widget token")
    raw_event_ids = fields.get("eventID")
    if raw_event_ids is not None and not _EVENT_IDS_VALUE_RE.fullmatch(raw_event_ids):
        raise WidgetSchemaError("invalid event ids")
    event_ids = (
        tuple(part.strip() for part in raw_event_ids.split(","))
        if raw_event_ids is not None
        else ()
    )
    if len(event_ids) != len(set(event_ids)):
        raise WidgetSchemaError("duplicate event ids")
    return _ParsedWidgetConfig(
        widget=fields["widget"],
        dom_id=fields["src"],
        theme=fields["theme"],
        lang=fields["lang"],
        token=token,
        event_ids=event_ids,
    )


def extract_widget_config(html: str, *, expected_theme: str) -> WidgetConfig:
    candidates: list[_ParsedWidgetConfig] = []
    tree = HTMLParser(html)
    document_order = {
        node: index for index, node in enumerate(tree.root.traverse())
    }
    portal_loaders = [
        script
        for script in tree.css("script")
        if script.attributes == {"src": _PORTAL_SCRIPT_URL}
        and not _has_inert_ancestor(script)
        and not script.text(deep=True, separator="", strip=False).strip()
    ]
    if len(portal_loaders) != 1:
        raise WidgetSchemaError("missing or ambiguous widget loader")
    portal_loader = portal_loaders[0]
    for script in tree.css("script"):
        source = script.text(deep=True, separator="", strip=False)
        if "window.logbyte.gateway" not in source:
            continue
        # Only the live-confirmed, attribute-free inline classic-script shape
        # is executable evidence. Data/template scripts, external scripts with
        # ignored inline text, and nomodule/type variants are intentionally inert.
        if script.attributes:
            continue
        if _has_inert_ancestor(script):
            continue
        if document_order[portal_loader] >= document_order[script]:
            raise WidgetSchemaError("widget loader is not ready")
        cleaned = _strip_js_comments(source)
        match = _INLINE_GATEWAY_RE.fullmatch(cleaned)
        if match is None:
            raise WidgetSchemaError("non-literal gateway script")
        parsed = _parse_gateway_body(match.group("body"))
        if (
            parsed.widget == "WidgetRequestEvent"
            and parsed.theme == expected_theme
            and parsed.lang == "DE"
        ):
            render_targets = [
                node
                for node in tree.css("[id]")
                if node.attributes.get("id") == parsed.dom_id
            ]
            if len(render_targets) != 1:
                raise WidgetSchemaError("missing or ambiguous widget render target")
            if (
                render_targets[0].tag != "div"
                or document_order[render_targets[0]] >= document_order[script]
            ):
                raise WidgetSchemaError("widget render target is not ready")
            if _has_inert_ancestor(render_targets[0]):
                raise WidgetSchemaError("inert widget render target")
            candidates.append(parsed)
    if len(candidates) != 1:
        raise WidgetSchemaError("missing or ambiguous target gateway")
    candidate = candidates[0]
    return WidgetConfig(token=candidate.token, event_ids=candidate.event_ids)


def _unknown(error_class: str) -> ProbeResult:
    return ProbeResult(
        "unknown",
        diagnostics=ProbeDiagnostics(
            health="degraded", page_type="booking", error_class=error_class
        ),
    )


def _parse_positive_int(value: Any, field: str) -> int:
    if not isinstance(value, str) or not value.isdigit() or int(value) <= 0:
        raise WidgetSchemaError(f"invalid {field}")
    return int(value)


def _parse_day_key(raw: Any) -> str:
    if not isinstance(raw, str):
        raise WidgetSchemaError("invalid date key")
    match = _ISO_MIDNIGHT_RE.fullmatch(raw)
    if not match:
        raise WidgetSchemaError("invalid date key")
    iso_date = match.group(1)
    parsed = date.fromisoformat(iso_date)
    if parsed.isoformat() != iso_date or not (_FESTIVAL_START <= parsed <= _FESTIVAL_END):
        raise WidgetSchemaError("invalid date key")
    return iso_date


def _parse_times(raw: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list) or not raw or len(raw) > 16:
        raise WidgetSchemaError("empty times")
    parsed: list[tuple[str, str]] = []
    for pair in raw:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(value, str) and _HHMM_RE.fullmatch(value) for value in pair)
        ):
            raise WidgetSchemaError("invalid time pair")
        start, end = pair
        if start >= end:
            raise WidgetSchemaError("invalid time range")
        parsed.append((start, end))
    if len(parsed) != len(set(parsed)):
        raise WidgetSchemaError("duplicate time range")
    return tuple(parsed)


def _shift_category(ticket_name: str) -> str:
    normalized = ticket_name.casefold()
    shifts = [label for label, pattern in _SHIFT_PATTERNS if pattern.search(normalized)]
    if len(shifts) != 1:
        raise WidgetSchemaError("ambiguous shift name")
    return shifts[0]


def _time_is_consistent(category: str, start: str, end: str) -> bool:
    start_minutes = int(start[:2]) * 60 + int(start[2:])
    end_minutes = int(end[:2]) * 60 + int(end[2:])
    rules = {
        "Vormittag": start_minutes < 12 * 60 and end_minutes <= 16 * 60,
        "Mittag": start_minutes < 14 * 60 and end_minutes <= 17 * 60 + 30,
        "Nachmittag": 13 * 60 <= start_minutes < 18 * 60 and end_minutes <= 21 * 60,
        "Abend": start_minutes >= 16 * 60,
        "Ganztag": start_minutes <= 13 * 60 and end_minutes >= 17 * 60,
    }
    return rules[category]


def _shift_name(ticket_name: str, start: str, end: str) -> str:
    normalized = ticket_name.casefold()
    shift = _shift_category(ticket_name)
    if not _time_is_consistent(shift, start, end):
        raise WidgetSchemaError("shift time contradicts ticket name")
    named_times = _NAME_TIME_RE.findall(ticket_name)
    if len(named_times) > 1:
        raise WidgetSchemaError("ambiguous time in ticket name")
    if named_times:
        start_hour, start_minute, end_hour, end_minute = named_times[0]
        named_start = f"{int(start_hour):02d}{start_minute}"
        named_end = f"{int(end_hour):02d}{end_minute}"
        if (named_start, named_end) != (start, end):
            raise WidgetSchemaError("ticket/API time mismatch")
    location = "Zelt" if re.search(r"\b(?:im\s+)?zelt\b", normalized) else None
    start_label = f"{start[:2]}:{start[2:]}"
    end_label = f"{end[:2]}:{end[2:]}"
    suffix = f", {location}" if location else ""
    return f"{shift} ({start_label}–{end_label}{suffix})"


def parse_event_days_payload(
    payload: Any,
    iso_date: str,
    *,
    allowed_event_ids: tuple[str, ...] = (),
    include_name_regex: str | None = None,
    exclude_name_regex: str | None = None,
) -> ProbeResult:
    """Validate the complete response and derive one target-date observation."""

    if not isinstance(payload, dict) or payload.get("error") is not False:
        return _unknown("event_days_schema_invalid")
    raw_result = payload.get("result")
    if not isinstance(raw_result, list) or not raw_result or len(raw_result) > 100:
        return _unknown("event_days_empty")

    allowed = set(allowed_event_ids)
    name_pattern = re.compile(include_name_regex, re.IGNORECASE) if include_name_regex else None
    exclude_pattern = re.compile(exclude_name_regex, re.IGNORECASE) if exclude_name_regex else None
    plausible_dates: set[str] = set()
    target_shifts: list[str] = []
    included_ticket_count = 0
    irrelevant_ticket_count = 0
    seen_ticket_ids: set[str] = set()
    try:
        if bool(name_pattern) != bool(exclude_pattern):
            raise WidgetSchemaError("incomplete ticket-name classification")
        target_date = date.fromisoformat(iso_date)
        if not (_FESTIVAL_START <= target_date <= _FESTIVAL_END):
            raise WidgetSchemaError("target date outside festival")
        for ticket in raw_result:
            if not isinstance(ticket, dict):
                raise WidgetSchemaError("invalid ticket")
            ticket_id = ticket.get("ticketTypeId")
            if not isinstance(ticket_id, (str, int)) or isinstance(ticket_id, bool):
                raise WidgetSchemaError("invalid ticket id")
            ticket_id = str(ticket_id)
            if ticket_id in seen_ticket_ids:
                raise WidgetSchemaError("duplicate ticket id")
            seen_ticket_ids.add(ticket_id)
            if allowed and ticket_id not in allowed:
                # The endpoint may append a coupon event unrelated to the
                # explicitly requested event IDs. Its dates need not follow
                # the Oktoberfest schema and cannot invalidate relevant rows.
                irrelevant_ticket_count += 1
                continue
            ticket_name = ticket.get("ticketTypeName")
            if (
                not isinstance(ticket_name, str)
                or not ticket_name.strip()
                or len(ticket_name) > 200
            ):
                raise WidgetSchemaError("invalid ticket name")
            if name_pattern:
                included = bool(name_pattern.search(ticket_name))
                excluded = bool(exclude_pattern and exclude_pattern.search(ticket_name))
                if included == excluded:
                    raise WidgetSchemaError("unclassified ticket name")
                if excluded:
                    irrelevant_ticket_count += 1
                    continue
            minimum = _parse_positive_int(
                ticket.get("ticketMinPerson"), "minimum party size"
            )
            maximum = _parse_positive_int(
                ticket.get("ticketMaxPerson"), "maximum party size"
            )
            if maximum < minimum:
                raise WidgetSchemaError("invalid party-size range")
            raw_days = ticket.get("availableDays")
            if not isinstance(raw_days, list) or not raw_days:
                raise WidgetSchemaError("invalid available days")
            seen_ticket_dates: set[str] = set()
            parsed_days: list[tuple[str, tuple[tuple[str, str], ...]]] = []
            for day in raw_days:
                if not isinstance(day, dict) or len(day) != 1:
                    raise WidgetSchemaError("invalid available day")
                raw_date, raw_times = next(iter(day.items()))
                event_date = _parse_day_key(raw_date)
                if event_date in seen_ticket_dates:
                    raise WidgetSchemaError("duplicate ticket date")
                seen_ticket_dates.add(event_date)
                parsed_times = _parse_times(raw_times)
                parsed_days.append((event_date, parsed_times))

            included_ticket_count += 1
            _shift_category(ticket_name)
            for event_date, parsed_times in parsed_days:
                plausible_dates.add(event_date)
                labels = tuple(
                    _shift_name(ticket_name, start, end)
                    for start, end in parsed_times
                )
                if event_date == iso_date:
                    target_shifts.extend(labels)
    except (ValueError, re.error):
        return _unknown("event_days_schema_invalid")

    if included_ticket_count == 0:
        if irrelevant_ticket_count:
            return ProbeResult(
                "unavailable",
                shifts=(),
                diagnostics=ProbeDiagnostics(
                    health="healthy",
                    page_type="booking",
                    date_control_count=1,
                    plausible_date_option_count=0,
                    target_found=False,
                    shift_count=0,
                    unavailable_confirmed=True,
                ),
            )
        return _unknown("event_days_no_matching_tickets")
    evidence_dates = plausible_dates
    if not evidence_dates:
        return _unknown("event_days_no_matching_tickets")
    shifts = tuple(dict.fromkeys(target_shifts))
    if len(shifts) > 32:
        return _unknown("event_days_schema_invalid")
    common = dict(
        page_type="booking",
        date_control_count=1,
        plausible_date_option_count=len(evidence_dates),
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
        shifts=shifts,
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


def _get_bounded(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    limit: int,
    allow_unauthorized: bool = False,
    overall_deadline: float,
) -> tuple[int, bytes]:
    started = time.monotonic()
    request_deadline = min(started + _MAX_REQUEST_SECONDS, overall_deadline)
    remaining = request_deadline - started
    if remaining <= 0:
        raise WidgetSchemaError("fetch deadline exceeded")
    with client.stream(
        "GET",
        url,
        headers=headers,
        follow_redirects=False,
        timeout=remaining,
    ) as response:
        if time.monotonic() > request_deadline:
            raise WidgetSchemaError("response deadline exceeded")
        if response.status_code == 401 and allow_unauthorized:
            return 401, b""
        response.raise_for_status()
        raw_length = response.headers.get("Content-Length")
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError as exc:
                raise WidgetSchemaError("invalid content length") from exc
            if content_length < 0 or content_length > limit:
                raise WidgetSchemaError("response too large")
        content = bytearray()
        for chunk in response.iter_bytes():
            if time.monotonic() > request_deadline:
                raise WidgetSchemaError("response deadline exceeded")
            content.extend(chunk)
            if len(content) > limit:
                raise WidgetSchemaError("response too large")
        if time.monotonic() > request_deadline:
            raise WidgetSchemaError("response deadline exceeded")
        return response.status_code, bytes(content)


def _endpoint(cfg: ReservierungsmanagerConfig, event_ids: tuple[str, ...]) -> str:
    has_placeholder = "{event_ids}" in cfg.event_days_endpoint
    if has_placeholder != bool(event_ids):
        raise WidgetSchemaError("event id configuration mismatch")
    return cfg.event_days_endpoint.replace("{event_ids}", ",".join(event_ids))


def fetch(
    cfg: ReservierungsmanagerConfig,
    iso_date: str,
    client: httpx.Client,
) -> ProbeResult:
    """Issue GETs only; a single 401 refreshes the public widget token once."""

    overall_deadline = time.monotonic() + _MAX_FETCH_SECONDS
    for attempt in range(2):
        _, landing_body = _get_bounded(
            client,
            cfg.landing_url,
            headers=DEFAULT_HEADERS,
            limit=_MAX_LANDING_BYTES,
            overall_deadline=overall_deadline,
        )
        try:
            landing_html = landing_body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise WidgetSchemaError("invalid landing-page encoding") from exc
        widget = extract_widget_config(
            landing_html,
            expected_theme=cfg.expected_theme,
        )
        endpoint = _endpoint(cfg, widget.event_ids)
        status_code, api_body = _get_bounded(
            client,
            endpoint,
            headers={
                **DEFAULT_HEADERS,
                "Authorization": f"Bearer {widget.token}",
                "Language": "DE",
            },
            limit=_MAX_API_BYTES,
            allow_unauthorized=True,
            overall_deadline=overall_deadline,
        )
        if status_code == 401 and attempt == 0:
            continue
        if status_code == 401:
            raise WidgetSchemaError("widget authorization failed")
        try:
            payload = json.loads(api_body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WidgetSchemaError("invalid API JSON") from exc
        return parse_event_days_payload(
            payload,
            iso_date,
            allowed_event_ids=widget.event_ids,
            include_name_regex=cfg.include_name_regex,
            exclude_name_regex=cfg.exclude_name_regex,
        )
    raise WidgetSchemaError("widget authorization failed")
