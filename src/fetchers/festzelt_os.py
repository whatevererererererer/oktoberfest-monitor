"""Safe Playwright probe for Festzelt-OS and compatible booking wizards.

The fetcher deliberately distinguishes a proven missing target date from an
unreadable page.  It uses one fresh page per target date, reacquires all
controls after every wizard rerender, and requires target-specific update
evidence before attributing any shift options.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date as date_type
from typing import Iterable
from urllib.parse import parse_qs, urlsplit

from ..config import FestzeltOsConfig
from ..probe import PageType, ProbeDiagnostics, ProbeResult

_MONTHS = {
    "januar": 1,
    "februar": 2,
    "marz": 3,
    "maerz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
}
_SHIFT_WORDS = re.compile(r"\b(mittag|nachmittag|abend|vormittag|ganztag)\b", re.I)
_SHIFT_SEMANTIC = re.compile(
    r"(?:shift|schicht|booking[_-]?list|reservation[_-]?time|time[_-]?slot|zeit)", re.I
)
_PLACEHOLDER = re.compile(
    r"^(?:[-–—]+|bitte(?:\s+ein(?:e|en)?)?"
    r"(?:\s+(?:schicht|zeit|reservierung|option|termin))?\s+(?:aus)?wahlen[.!:]?|"
    r"please\s+(?:select|choose)[.!:]?|auswahlen|select|choose|"
    r"schicht|zeit|placeholder)$",
    re.I,
)
_NEGATIVE_SHIFT_LABEL = re.compile(
    r"(?:\b(?:ausgebucht|unavailable|sold\s*out|fully\s*booked|loading|laden)\b|"
    r"\bwird\s+geladen\b|\bnicht\s+(?:verfugbar|verfuegbar|frei)\b|"
    r"\bkeine?\b.*\b(?:verfugbar|verfuegbar|frei|schicht(?:en)?|zeit(?:en)?|"
    r"reservierung(?:en)?|slot(?:s)?|platz(?:e)?)\b|"
    r"\bno\b.*\b(?:availability|slots?|times?|shifts?)\b)",
    re.I,
)
_NEGATIVE_SHIFT_VALUES = {
    "-",
    "--",
    "0",
    "none",
    "null",
    "placeholder",
    "loading",
    "unavailable",
    "sold-out",
    "sold_out",
    "ausgebucht",
}
_BOT_MARKERS = (
    "captcha",
    "cloudflare",
    "just a moment",
    "verify you are human",
    "human verification",
    "access denied",
    "bot protection",
    "turnstile",
)
_LOGIN_MARKERS = ("passwort", "password", "anmelden", "sign in", "log in")
_ERROR_MARKERS = (
    "403 forbidden",
    "502 bad gateway",
    "503 service unavailable",
    "internal server error",
    "service unavailable",
)
_BOT_FRAME_MARKERS = (
    "captcha",
    "cloudflare",
    "turnstile",
    "cdn-cgi/challenge",
    "challenge-platform",
)


@dataclass(frozen=True, slots=True)
class _Option:
    value: str
    text: str
    canonical_date: str | None
    disabled: bool
    date_conflict: bool = False


@dataclass(frozen=True, slots=True)
class _SelectSnapshot:
    options: tuple[_Option, ...]
    attributes: dict[str, str]
    value: str
    visible: bool
    enabled: bool


@dataclass(frozen=True, slots=True)
class _DateControl:
    locator: object
    options: tuple[_Option, ...]
    signature: tuple[tuple[str, str, bool], ...]
    livewire_bound: bool = False
    livewire_model: str | None = None
    selected_value: str = ""


@dataclass(frozen=True, slots=True)
class _ShiftControl:
    locator: object
    shifts: tuple[str, ...]
    signature: tuple[str, tuple[tuple[str, str, bool], ...]]


@dataclass(frozen=True, slots=True)
class _DateDiscovery:
    control: _DateControl | None
    page_type: PageType
    control_count: int
    plausible_count: int
    error_class: str | None = None


@dataclass(frozen=True, slots=True)
class _DateScan:
    controls: tuple[_DateControl, ...]
    raw_count: int
    plausible_count: int
    conflicting_option_count: int


@dataclass(frozen=True, slots=True)
class _UpdateFailure:
    page_type: PageType
    error_class: str


def _is_relevant_update_url(value: object) -> bool:
    """Match known wizard-update endpoints without retaining the URL."""

    try:
        path = urlsplit(str(value)).path.rstrip("/").casefold()
    except Exception:
        return False
    return path == "/livewire/update" or path.endswith("/livewire/update")


class _WizardUpdateMonitor:
    """Capture only causally paired, target-specific Livewire updates.

    A matching endpoint is insufficient evidence because a page can have
    unrelated Livewire traffic.  The request payload must contain an exact
    identifier for the selected target, and a response is accepted only when
    it points back to that captured Request object.
    """

    def __init__(
        self,
        page,
        *,
        target: str,
        option: _Option,
        livewire_model: str | None,
    ) -> None:
        self.page = page
        self._livewire_model = _clean_text(livewire_model)
        self._selection_tokens = frozenset(
            _clean_text(token)
            for token in (target, option.value, option.text)
            if _clean_text(token)
        )
        self._matched_request_ids: set[int] = set()
        self._successful_responses: dict[int, object] = {}
        self.failure: _UpdateFailure | None = None
        self.request_started = False
        self.succeeded = False
        self._listening_request = False
        self._listening_response = False
        self._listening_request_finished = False
        self._listening_request_failed = False

    @staticmethod
    def _payload_values(value: object) -> Iterable[str]:
        if isinstance(value, dict):
            for item in value.values():
                yield from _WizardUpdateMonitor._payload_values(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from _WizardUpdateMonitor._payload_values(item)
        elif value is not None:
            yield _clean_text(str(value))

    @staticmethod
    def _payload_keys(value: object) -> Iterable[str]:
        if isinstance(value, dict):
            for key, item in value.items():
                yield _clean_text(str(key))
                yield from _WizardUpdateMonitor._payload_keys(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from _WizardUpdateMonitor._payload_keys(item)

    def _update_payload_matches(self, value: object, *, in_updates: bool = False) -> bool:
        if isinstance(value, dict):
            if in_updates:
                values = set(self._payload_values(value))
                if values.intersection(self._selection_tokens):
                    if not self._livewire_model:
                        return True
                    names = values.union(self._payload_keys(value))
                    if self._livewire_model in names:
                        return True
            for key, item in value.items():
                child_in_updates = in_updates or _fold(str(key)) in {
                    "update",
                    "updates",
                }
                if self._update_payload_matches(item, in_updates=child_in_updates):
                    return True
        elif isinstance(value, (list, tuple)):
            for item in value:
                if self._update_payload_matches(item, in_updates=in_updates):
                    return True
        elif in_updates and not self._livewire_model:
            return _clean_text(str(value)) in self._selection_tokens
        return False

    def _request_matches_selection(self, request) -> bool:
        if not _is_relevant_update_url(getattr(request, "url", "")):
            return False
        method = getattr(request, "method", "POST")
        if callable(method):
            method = method()
        if _clean_text(str(method)).upper() != "POST":
            return False
        payload = getattr(request, "post_data", None)
        if callable(payload):
            payload = payload()
        if not isinstance(payload, str) or not payload:
            return False

        try:
            parsed: object = json.loads(payload)
        except (TypeError, ValueError):
            try:
                parsed = parse_qs(payload, keep_blank_values=True)
            except (TypeError, ValueError):
                return False
        return self._update_payload_matches(parsed)

    def _on_request(self, request) -> None:
        if not self.request_started and self._request_matches_selection(request):
            self._matched_request_ids.add(id(request))
            self.request_started = True

    def _on_response(self, response) -> None:
        try:
            request = getattr(response, "request", None)
            if (
                request is None
                or id(request) not in self._matched_request_ids
                or not _is_relevant_update_url(getattr(response, "url", ""))
            ):
                return
            status = int(getattr(response, "status", 0))
            self.request_started = True
        except Exception:
            return

        challenged = False
        try:
            header_value = getattr(response, "header_value", None)
            challenged = bool(
                callable(header_value)
                and str(header_value("cf-mitigated") or "").casefold() == "challenge"
            )
        except Exception:
            pass

        if challenged:
            self.failure = _UpdateFailure("bot", "shift_update_challenge")
        elif status in {401, 403, 429}:
            self.failure = _UpdateFailure("bot", f"shift_update_http_{status}")
        elif status >= 400:
            self.failure = _UpdateFailure("error", f"shift_update_http_{status}")
        elif 200 <= status < 300:
            # ``response`` means status and headers are available, not that
            # the body has finished.  Wait for the paired requestfinished
            # event rather than blocking this callback on Response.finished().
            self._successful_responses[id(request)] = response

    def _on_request_finished(self, request) -> None:
        response = self._successful_responses.get(id(request))
        if response is None:
            return
        self.request_started = True

        # Inspect only the response type and parsed JSON object after the body
        # is complete.  Raw HTML/body text is neither retained nor logged.
        content_type = _response_header(response, "content-type").casefold()
        media_type = content_type.partition(";")[0].strip()
        if media_type != "application/json" and not media_type.endswith("+json"):
            self.failure = _UpdateFailure(
                "error", "shift_update_response_not_json"
            )
            return
        content_length = _response_header(response, "content-length")
        try:
            if content_length and int(content_length) > 1_000_000:
                self.failure = _UpdateFailure(
                    "error", "shift_update_response_too_large"
                )
                return
        except ValueError:
            self.failure = _UpdateFailure(
                "error", "shift_update_response_invalid_length"
            )
            return
        parse_json = getattr(response, "json", None)
        if not callable(parse_json):
            self.failure = _UpdateFailure(
                "error", "shift_update_response_json_unreadable"
            )
            return
        try:
            payload = parse_json()
        except Exception:
            self.failure = _UpdateFailure(
                "error", "shift_update_response_invalid_json"
            )
            return
        if not isinstance(payload, (dict, list)):
            self.failure = _UpdateFailure(
                "error", "shift_update_response_invalid_json"
            )
            return
        self.succeeded = True

    def _on_request_failed(self, request) -> None:
        if id(request) in self._matched_request_ids:
            self.request_started = True
            error_class = (
                "shift_update_response_incomplete"
                if id(request) in self._successful_responses
                else "shift_update_network_error"
            )
            self.failure = _UpdateFailure("error", error_class)

    def start(self) -> None:
        on = getattr(self.page, "on", None)
        if not callable(on):
            return
        try:
            on("request", self._on_request)
            self._listening_request = True
        except Exception:
            pass
        try:
            on("response", self._on_response)
            self._listening_response = True
        except Exception:
            pass
        try:
            on("requestfinished", self._on_request_finished)
            self._listening_request_finished = True
        except Exception:
            pass
        try:
            on("requestfailed", self._on_request_failed)
            self._listening_request_failed = True
        except Exception:
            pass

    def stop(self) -> None:
        remove = getattr(self.page, "remove_listener", None)
        if not callable(remove):
            remove = getattr(self.page, "off", None)
        if not callable(remove):
            return
        if self._listening_request:
            try:
                remove("request", self._on_request)
            except Exception:
                pass
        if self._listening_response:
            try:
                remove("response", self._on_response)
            except Exception:
                pass
        if self._listening_request_finished:
            try:
                remove("requestfinished", self._on_request_finished)
            except Exception:
                pass
        if self._listening_request_failed:
            try:
                remove("requestfailed", self._on_request_failed)
            except Exception:
                pass


def _clean_text(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "").replace("\xa0", " ")
    return " ".join(value.split()).strip()


def _fold(value: str) -> str:
    value = _clean_text(value).casefold()
    return "".join(
        char for char in unicodedata.normalize("NFKD", value) if not unicodedata.combining(char)
    )


def _safe_iso(year: int, month: int, day: int) -> str | None:
    try:
        return date_type(year, month, day).isoformat()
    except ValueError:
        return None


def canonical_date(value: str | None) -> str | None:
    """Parse exact ISO, numeric German, or long German option labels."""

    text = _clean_text(value)
    match = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?",
        text,
    )
    if match:
        return _safe_iso(int(match[1]), int(match[2]), int(match[3]))

    folded = _fold(text)
    match = re.fullmatch(
        r"(?:(?:montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\s*,?\s*)?"
        r"(?:den\s+)?(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})",
        folded,
    )
    if match:
        return _safe_iso(int(match[3]), int(match[2]), int(match[1]))

    match = re.fullmatch(
        r"(?:(?:montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\s*,?\s*)?"
        r"(?:den\s+)?(\d{1,2})\.\s*([a-z]+)\s+(\d{4})",
        folded,
    )
    if match and match[2] in _MONTHS:
        return _safe_iso(int(match[3]), _MONTHS[match[2]], int(match[1]))
    return None


def _option_date(value: str, text: str) -> tuple[str | None, bool]:
    by_value = canonical_date(value)
    by_text = canonical_date(text)
    if by_value and by_text and by_value != by_text:
        return None, True
    return by_value or by_text, False


def _read_select_snapshot(select) -> _SelectSnapshot:
    """Read one control atomically instead of making N timed locator calls."""

    raw = select.evaluate(
        """el => {
            const wanted = new Set([
                'id', 'name', 'aria-label', 'data-testid', 'v-model'
            ]);
            const attributes = {};
            for (const name of el.getAttributeNames()) {
                if (wanted.has(name) || name === 'wire:model' ||
                        name.startsWith('wire:model.')) {
                    attributes[name] = el.getAttribute(name) || '';
                }
            }
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return {
                __wiesnSelectSnapshot: true,
                attributes,
                value: el.value || '',
                visible: style.visibility !== 'hidden' && style.display !== 'none' &&
                    rect.width > 0 && rect.height > 0,
                enabled: !el.matches(':disabled'),
                options: Array.from(el.options, option => ({
                    value: option.value || '',
                    text: option.textContent || '',
                    disabled: option.disabled || Boolean(option.closest('optgroup:disabled'))
                }))
            };
        }""",
        timeout=1000,
    )
    if not isinstance(raw, dict) or not isinstance(raw.get("options"), list):
        raise ValueError("invalid select snapshot")

    attributes_raw = raw.get("attributes")
    attributes = {
        _clean_text(str(name)): _clean_text(str(value))
        for name, value in (
            attributes_raw.items() if isinstance(attributes_raw, dict) else ()
        )
    }
    options: list[_Option] = []
    for item in raw["options"]:
        if not isinstance(item, dict):
            raise ValueError("invalid select option snapshot")
        value = _clean_text(str(item.get("value") or ""))
        text = _clean_text(str(item.get("text") or ""))
        option_date, date_conflict = _option_date(value, text)
        options.append(
            _Option(
                value=value,
                text=text,
                canonical_date=option_date,
                disabled=bool(item.get("disabled")),
                date_conflict=date_conflict,
            )
        )
    return _SelectSnapshot(
        options=tuple(options),
        attributes=attributes,
        value=_clean_text(str(raw.get("value") or "")),
        visible=bool(raw.get("visible")),
        enabled=bool(raw.get("enabled")),
    )


def _all_selects(page, selector: str | None):
    return page.locator(selector or "select")


def _scan_date_controls(
    page, cfg: FestzeltOsConfig, target_years: set[int]
) -> _DateScan:
    selects = _all_selects(page, cfg.date_selector)
    raw_count = selects.count()
    controls: list[_DateControl] = []
    plausible_count = 0
    conflicting_option_count = 0
    for index in range(raw_count):
        select = selects.nth(index)
        snapshot = _read_select_snapshot(select)
        if not snapshot.visible or not snapshot.enabled:
            continue
        options = snapshot.options
        # Two independently parseable but different dates in one option are
        # invalid structural evidence.  Treating that option as merely
        # unparseable could turn the requested day into a false unavailable.
        conflicting_option_count += sum(option.date_conflict for option in options)
        plausible = tuple(
            option
            for option in options
            if option.canonical_date
            and int(option.canonical_date[:4]) in target_years
            and int(option.canonical_date[5:7]) in {9, 10}
        )
        plausible_count += len(plausible)
        if not plausible:
            continue
        livewire_names = [
            name
            for name in snapshot.attributes
            if name == "wire:model" or name.startswith("wire:model.")
        ]
        livewire_model = (
            snapshot.attributes[livewire_names[0]] if len(livewire_names) == 1 else None
        )
        controls.append(
            _DateControl(
                locator=select,
                options=options,
                signature=tuple((o.value, o.text, o.disabled) for o in options),
                livewire_bound=bool(livewire_names),
                livewire_model=livewire_model,
                selected_value=snapshot.value,
            )
        )
    return _DateScan(
        controls=tuple(controls),
        raw_count=raw_count,
        plausible_count=plausible_count,
        conflicting_option_count=conflicting_option_count,
    )


def _attribute_text(owner: object, name: str) -> str:
    try:
        value = getattr(owner, name, "")
        if callable(value):
            value = value()
        return _clean_text(str(value or "")).casefold()
    except Exception:
        return ""


def _body_text(owner: object) -> str:
    try:
        return _clean_text(
            owner.locator("body").inner_text(timeout=1000)
        ).casefold()
    except Exception:
        return ""


def _page_frames(page) -> tuple[object, ...]:
    try:
        frames = getattr(page, "frames", ())
        if callable(frames):
            frames = frames()
        main_frame = getattr(page, "main_frame", None)
        if callable(main_frame):
            main_frame = main_frame()
        return tuple(frame for frame in (frames or ()) if frame is not main_frame)
    except Exception:
        return ()


def _page_type(page) -> PageType:
    try:
        title = _clean_text(page.title()).casefold()
    except Exception:
        title = ""
    body = _body_text(page)
    frames = _page_frames(page)
    frame_text = " ".join(
        " ".join(
            (
                _attribute_text(frame, "url"),
                _attribute_text(frame, "name"),
                _body_text(frame),
            )
        )
        for frame in frames
    )
    # Scan the complete visible text.  Challenges often append an overlay
    # after a long booking document, well beyond an arbitrary prefix.
    sample = f"{title} {body} {frame_text}"
    if any(marker in sample for marker in _BOT_MARKERS):
        return "bot"
    if any(
        marker in " ".join(
            (_attribute_text(frame, "url"), _attribute_text(frame, "name"))
        )
        for marker in _BOT_FRAME_MARKERS
        for frame in frames
    ):
        return "bot"
    if any(marker in sample for marker in _ERROR_MARKERS):
        return "error"
    if any(marker in sample for marker in _LOGIN_MARKERS):
        for owner in (page, *frames):
            try:
                if owner.locator('input[type="password"]').count() > 0:
                    return "login"
            except Exception:
                continue
    return "booking" if body or title else "unknown"


def _wait(page, milliseconds: int) -> None:
    page.wait_for_timeout(milliseconds)


def _wait_for_date_control(
    page, cfg: FestzeltOsConfig, target_years: set[int]
) -> _DateDiscovery:
    timeout_ms = max(cfg.date_control_timeout_ms, cfg.wait_extra_ms)
    deadline = time.monotonic() + timeout_ms / 1000
    stable_since: float | None = None
    last_signature = None
    last_controls: list[_DateControl] = []

    while time.monotonic() <= deadline:
        page_type = _page_type(page)
        if page_type in {"bot", "login", "error"}:
            return _DateDiscovery(None, page_type, 0, 0, f"{page_type}_page")
        try:
            scan = _scan_date_controls(page, cfg, target_years)
        except Exception:
            scan = _DateScan((), 0, 0, 0)
        if scan.conflicting_option_count:
            return _DateDiscovery(
                None,
                "error",
                len(scan.controls),
                scan.plausible_count,
                "date_option_conflict",
            )
        controls = list(scan.controls)
        last_controls = controls
        signature = tuple(control.signature for control in controls)
        now = time.monotonic()
        if controls and signature == last_signature:
            stable_since = stable_since if stable_since is not None else now
        elif controls:
            stable_since = now
            last_signature = signature
        else:
            stable_since = None
            last_signature = None

        stable = bool(controls) and (
            cfg.stable_for_ms == 0
            or (stable_since is not None and (now - stable_since) * 1000 >= cfg.stable_for_ms)
        )
        if stable and len(controls) == 1:
            plausible_count = scan.plausible_count
            return _DateDiscovery(
                controls[0], page_type, 1, plausible_count
            )
        _wait(page, cfg.poll_interval_ms)

    plausible_count = sum(
        1
        for control in last_controls
        for option in control.options
        if option.canonical_date
        and int(option.canonical_date[:4]) in target_years
        and int(option.canonical_date[5:7]) in {9, 10}
    )
    if len(last_controls) > 1:
        error_class = "ambiguous_date_control"
    elif len(last_controls) == 1:
        error_class = "date_control_unstable"
    else:
        error_class = "date_control_missing"
    return _DateDiscovery(
        None, _page_type(page), len(last_controls), plausible_count, error_class
    )


def _shift_labels(options: Iterable[_Option]) -> tuple[str, ...]:
    labels: list[str] = []
    seen: set[str] = set()
    for option in options:
        label = _clean_text(option.text)
        folded_label = _fold(label)
        folded_value = _fold(option.value)
        if (
            not label
            or not option.value
            or option.disabled
            or _PLACEHOLDER.fullmatch(folded_label)
            or _NEGATIVE_SHIFT_LABEL.search(folded_label)
            or folded_value in _NEGATIVE_SHIFT_VALUES
        ):
            continue
        key = folded_label
        if key not in seen:
            seen.add(key)
            labels.append(label)
    return tuple(labels)


def _scan_shift_controls(page, cfg: FestzeltOsConfig) -> list[_ShiftControl]:
    selects = _all_selects(page, cfg.shift_selector)
    controls: list[_ShiftControl] = []
    for index in range(selects.count()):
        select = selects.nth(index)
        snapshot = _read_select_snapshot(select)
        if not snapshot.visible or not snapshot.enabled:
            continue
        options = snapshot.options
        attrs = " ".join(snapshot.attributes.values())
        semantic = bool(_SHIFT_SEMANTIC.search(attrs))
        has_shift_labels = any(_SHIFT_WORDS.search(option.text) for option in options)
        if not semantic and not has_shift_labels:
            continue
        option_signature = tuple((o.value, o.text, o.disabled) for o in options)
        controls.append(
            _ShiftControl(
                locator=select,
                shifts=_shift_labels(options),
                signature=(_fold(attrs), option_signature),
            )
        )
    return controls


def _selected_date(control: _DateControl) -> str | None:
    value = control.selected_value
    direct = canonical_date(value)
    if direct:
        return direct
    matching = [option.canonical_date for option in control.options if option.value == value]
    return matching[0] if len(matching) == 1 else None


def _target_option(control: _DateControl, target: str) -> _Option | None:
    matches = [option for option in control.options if option.canonical_date == target]
    return matches[0] if len(matches) == 1 else None


def _arm_dom_update_observer(controls: list[_ShiftControl]) -> bool:
    """Observe select/option mutations without synthesizing any UI event."""

    if len(controls) != 1:
        return False
    try:
        return bool(
            controls[0].locator.evaluate(
                """el => {
                    const key = '__wiesnProbeMutation';
                    if (window[key]?.observer) window[key].observer.disconnect();
                    const state = { count: 0, observer: null };
                    state.observer = new MutationObserver(records => {
                        for (const record of records) {
                            const nodes = [...record.addedNodes, ...record.removedNodes];
                            const touchesControl = record.target === el || el.contains(record.target) ||
                                nodes.some(node => node === el || node.contains?.(el));
                            if (touchesControl) state.count += 1;
                        }
                    });
                    state.observer.observe(el.parentElement || el, {
                        childList: true,
                        subtree: true
                    });
                    window[key] = state;
                    return true;
                }"""
            )
        )
    except Exception:
        return False


def _dom_update_observed(page, observer_armed: bool) -> bool:
    if not observer_armed:
        return False
    try:
        return bool(
            page.evaluate(
                "() => Boolean(window.__wiesnProbeMutation?.count)"
            )
        )
    except Exception:
        return False


def _diag(
    status: str,
    *,
    page_type: PageType,
    date_control_count: int,
    plausible_count: int,
    target_found: bool,
    target_enabled: bool | None = None,
    shift_control_count: int = 0,
    update_confirmed: bool = False,
    shift_count: int = 0,
    error_class: str | None = None,
    detail: str | None = None,
) -> ProbeDiagnostics:
    health = "error" if status == "error" else "degraded" if status == "unknown" else "healthy"
    return ProbeDiagnostics(
        health=health,
        page_type=page_type,
        date_control_count=date_control_count,
        plausible_date_option_count=plausible_count,
        target_found=target_found,
        target_enabled=target_enabled,
        shift_control_count=shift_control_count,
        shift_control_found=shift_control_count == 1,
        update_confirmed=update_confirmed,
        shift_count=shift_count,
        error_class=error_class,
        detail=detail,
    )


def _error_from_discovery(discovery: _DateDiscovery) -> ProbeResult:
    return ProbeResult(
        "error",
        diagnostics=_diag(
            "error",
            page_type=discovery.page_type,
            date_control_count=discovery.control_count,
            plausible_count=discovery.plausible_count,
            target_found=False,
            error_class=discovery.error_class or "date_control_error",
        ),
    )


def _update_failure_result(
    failure: _UpdateFailure,
    discovery: _DateDiscovery,
    shift_control_count: int,
) -> ProbeResult:
    return ProbeResult(
        "error",
        diagnostics=_diag(
            "error",
            page_type=failure.page_type,
            date_control_count=1,
            plausible_count=discovery.plausible_count,
            target_found=True,
            target_enabled=True,
            shift_control_count=shift_control_count,
            update_confirmed=False,
            error_class=failure.error_class,
        ),
    )


def _probe_target(
    page,
    cfg: FestzeltOsConfig,
    target: str,
    target_years: set[int],
) -> ProbeResult:
    probe_started = time.monotonic()
    discovery = _wait_for_date_control(page, cfg, target_years)
    if discovery.error_class or discovery.control is None:
        return _error_from_discovery(discovery)

    control = discovery.control
    target_options = [option for option in control.options if option.canonical_date == target]
    if not target_options:
        # A stable partial list is not negative evidence: SPAs commonly append
        # options in phases.  Absence is therefore final only after the full
        # date-control window measured from the start of this target probe.
        absence_deadline = probe_started + max(
            cfg.date_control_timeout_ms, cfg.wait_extra_ms
        ) / 1000
        stable_signature = control.signature
        stable_since: float | None = time.monotonic() - cfg.stable_for_ms / 1000
        last_date_controls = [control]
        last_scan_failed = False
        while time.monotonic() < absence_deadline:
            _wait(page, cfg.poll_interval_ms)
            current_page_type = _page_type(page)
            if current_page_type in {"bot", "login", "error"}:
                return ProbeResult(
                    "error",
                    diagnostics=_diag(
                        "error",
                        page_type=current_page_type,
                        date_control_count=0,
                        plausible_count=0,
                        target_found=False,
                        error_class=f"{current_page_type}_while_confirming_absence",
                    ),
                )
            try:
                date_scan = _scan_date_controls(page, cfg, target_years)
                date_controls = list(date_scan.controls)
                last_scan_failed = False
            except Exception:
                date_controls = []
                last_scan_failed = True
                date_scan = None
            if date_scan is not None and date_scan.conflicting_option_count:
                return ProbeResult(
                    "error",
                    diagnostics=_diag(
                        "error",
                        page_type="error",
                        date_control_count=len(date_controls),
                        plausible_count=date_scan.plausible_count,
                        target_found=False,
                        error_class="date_option_conflict",
                    ),
                )
            last_date_controls = date_controls
            if len(date_controls) != 1:
                stable_signature = None
                stable_since = None
                continue
            refreshed = date_controls[0]
            refreshed_targets = [
                option for option in refreshed.options if option.canonical_date == target
            ]
            if refreshed_targets:
                discovery = _DateDiscovery(
                    control=refreshed,
                    page_type=current_page_type,
                    control_count=1,
                    plausible_count=sum(
                        1 for option in refreshed.options if option.canonical_date
                    ),
                )
                control = refreshed
                target_options = refreshed_targets
                break
            now = time.monotonic()
            if refreshed.signature != stable_signature:
                stable_signature = refreshed.signature
                stable_since = now
        if not target_options:
            final_page_type = _page_type(page)
            if final_page_type in {"bot", "login", "error"}:
                return ProbeResult(
                    "error",
                    diagnostics=_diag(
                        "error",
                        page_type=final_page_type,
                        date_control_count=len(last_date_controls),
                        plausible_count=0,
                        target_found=False,
                        error_class=f"{final_page_type}_while_confirming_absence",
                    ),
                )
            if last_scan_failed or len(last_date_controls) != 1:
                error_class = (
                    "control_scan_failed"
                    if last_scan_failed
                    else "ambiguous_date_control"
                    if len(last_date_controls) > 1
                    else "date_control_changed"
                )
                return ProbeResult(
                    "error",
                    diagnostics=_diag(
                        "error",
                        page_type=final_page_type,
                        date_control_count=len(last_date_controls),
                        plausible_count=sum(
                            1
                            for item in last_date_controls
                            for option in item.options
                            if option.canonical_date
                        ),
                        target_found=False,
                        error_class=error_class,
                    ),
                )
            final_control = last_date_controls[0]
            stable_long_enough = (
                cfg.stable_for_ms == 0
                or stable_since is not None
                and (time.monotonic() - stable_since) * 1000 >= cfg.stable_for_ms
            )
            if not stable_long_enough:
                return ProbeResult(
                    "unknown",
                    diagnostics=_diag(
                        "unknown",
                        page_type=final_page_type,
                        date_control_count=1,
                        plausible_count=sum(
                            1 for option in final_control.options if option.canonical_date
                        ),
                        target_found=False,
                        error_class="date_options_unstable",
                    ),
                )
            return ProbeResult(
                "unavailable",
                shifts=(),
                diagnostics=_diag(
                    "unavailable",
                    page_type=final_page_type,
                    date_control_count=1,
                    plausible_count=sum(
                        1 for option in final_control.options if option.canonical_date
                    ),
                    target_found=False,
                ),
            )
    if len(target_options) != 1:
        return ProbeResult(
            "error",
            diagnostics=_diag(
                "error",
                page_type=discovery.page_type,
                date_control_count=1,
                plausible_count=discovery.plausible_count,
                target_found=True,
                error_class="target_option_ambiguous",
            ),
        )
    target_option = target_options[0]
    if target_option.disabled:
        return ProbeResult(
            "unknown",
            diagnostics=_diag(
                "unknown",
                page_type=discovery.page_type,
                date_control_count=1,
                plausible_count=discovery.plausible_count,
                target_found=True,
                target_enabled=False,
                error_class="target_disabled",
            ),
        )

    requires_livewire_confirmation = control.livewire_bound
    initial_shift_controls = _scan_shift_controls(page, cfg)
    initial_signature = (
        initial_shift_controls[0].signature if len(initial_shift_controls) == 1 else None
    )
    observer_armed = _arm_dom_update_observer(initial_shift_controls)
    update_monitor = _WizardUpdateMonitor(
        page,
        target=target,
        option=target_option,
        livewire_model=control.livewire_model,
    )
    update_monitor.start()
    try:
        try:
            if target_option.value:
                control.locator.select_option(
                    value=target_option.value, timeout=cfg.shift_update_timeout_ms
                )
            else:
                control.locator.select_option(
                    label=target_option.text, timeout=cfg.shift_update_timeout_ms
                )
        except Exception:
            if update_monitor.failure is not None:
                return _update_failure_result(
                    update_monitor.failure, discovery, len(initial_shift_controls)
                )
            return ProbeResult(
                "error",
                diagnostics=_diag(
                    "error",
                    page_type=discovery.page_type,
                    date_control_count=1,
                    plausible_count=discovery.plausible_count,
                    target_found=True,
                    target_enabled=True,
                    shift_control_count=len(initial_shift_controls),
                    error_class="date_selection_failed",
                ),
            )

        if update_monitor.failure is not None:
            return _update_failure_result(
                update_monitor.failure, discovery, len(initial_shift_controls)
            )

        timeout_ms = max(cfg.shift_update_timeout_ms, cfg.shift_wait_ms)
        deadline = time.monotonic() + timeout_ms / 1000
        last_controls: list[_ShiftControl] = []
        selected_confirmed = False
        # A preselected target and its existing options are not sufficient proof.
        # Evidence must be observed after this native select_option call.
        update_confirmed = False
        date_control_count = 1
        last_scan_failed = False
        candidate_signature = None
        candidate_since: float | None = None
        response_completion_observed = not requires_livewire_confirmation

        while time.monotonic() <= deadline:
            if update_monitor.failure is not None:
                return _update_failure_result(
                    update_monitor.failure, discovery, len(last_controls)
                )
            try:
                date_scan = _scan_date_controls(page, cfg, target_years)
                date_controls = list(date_scan.controls)
                date_control_count = len(date_controls)
                last_scan_failed = False
                if date_scan.conflicting_option_count:
                    return ProbeResult(
                        "error",
                        diagnostics=_diag(
                            "error",
                            page_type="error",
                            date_control_count=date_control_count,
                            plausible_count=date_scan.plausible_count,
                            target_found=True,
                            target_enabled=True,
                            shift_control_count=len(last_controls),
                            error_class="date_option_conflict",
                        ),
                    )
                if len(date_controls) == 1:
                    refreshed_target = _target_option(date_controls[0], target)
                    selected_confirmed = (
                        refreshed_target is not None
                        and _selected_date(date_controls[0]) == target
                    )
                else:
                    selected_confirmed = False
                controls = _scan_shift_controls(page, cfg)
            except Exception:
                last_scan_failed = True
                selected_confirmed = False
                controls = []
            last_controls = controls
            if update_monitor.succeeded and not response_completion_observed:
                # Let the browser process the completed Livewire response and
                # then reacquire both controls.  Without this boundary an
                # identical stale list could be accepted in the early
                # ``response`` event before Livewire applies its payload.
                response_completion_observed = True
                candidate_signature = None
                candidate_since = None
                _wait(page, cfg.poll_interval_ms)
                continue
            if not last_scan_failed and date_control_count != 1:
                current_page_type = _page_type(page)
                if current_page_type in {"bot", "login", "error"}:
                    return ProbeResult(
                        "error",
                        diagnostics=_diag(
                            "error",
                            page_type=current_page_type,
                            date_control_count=date_control_count,
                            plausible_count=discovery.plausible_count,
                            target_found=True,
                            target_enabled=True,
                            shift_control_count=len(last_controls),
                            error_class=f"{current_page_type}_after_selection",
                        ),
                    )
            if len(controls) == 1:
                dom_changed = (
                    len(initial_shift_controls) != 1
                    or controls[0].signature != initial_signature
                    or _dom_update_observed(page, observer_armed)
                )
                # A causally paired target-triggered 2xx is sufficient even
                # when the server legitimately returns an identical shift
                # list.  Non-Livewire pages still require concrete DOM change.
                update_confirmed = update_confirmed or (
                    selected_confirmed
                    and (dom_changed or update_monitor.succeeded)
                )
                network_confirmed = (
                    not requires_livewire_confirmation
                    or update_monitor.succeeded and response_completion_observed
                )
                if (
                    selected_confirmed
                    and update_confirmed
                    and network_confirmed
                    and controls[0].shifts
                ):
                    now = time.monotonic()
                    if controls[0].signature != candidate_signature:
                        candidate_signature = controls[0].signature
                        candidate_since = now
                    if (
                        cfg.stable_for_ms == 0
                        or candidate_since is not None
                        and (now - candidate_since) * 1000 >= cfg.stable_for_ms
                    ):
                        candidate_page_type = _page_type(page)
                        if candidate_page_type in {"bot", "login", "error"}:
                            return ProbeResult(
                                "error",
                                diagnostics=_diag(
                                    "error",
                                    page_type=candidate_page_type,
                                    date_control_count=date_control_count,
                                    plausible_count=discovery.plausible_count,
                                    target_found=True,
                                    target_enabled=True,
                                    shift_control_count=1,
                                    error_class=(
                                        f"{candidate_page_type}_after_selection"
                                    ),
                                ),
                            )
                        return ProbeResult(
                            "available",
                            shifts=controls[0].shifts,
                            diagnostics=_diag(
                                "available",
                                page_type=discovery.page_type,
                                date_control_count=1,
                                plausible_count=discovery.plausible_count,
                                target_found=True,
                                target_enabled=True,
                                shift_control_count=1,
                                update_confirmed=True,
                                shift_count=len(controls[0].shifts),
                            ),
                        )
                else:
                    candidate_signature = None
                    candidate_since = None
            else:
                candidate_signature = None
                candidate_since = None
            _wait(page, cfg.poll_interval_ms)

        final_page_type = _page_type(page)
        if final_page_type in {"bot", "login", "error"}:
            return ProbeResult(
                "error",
                diagnostics=_diag(
                    "error",
                    page_type=final_page_type,
                    date_control_count=date_control_count,
                    plausible_count=discovery.plausible_count,
                    target_found=True,
                    target_enabled=True,
                    shift_control_count=len(last_controls),
                    error_class=f"{final_page_type}_after_selection",
                ),
            )
        if last_scan_failed or date_control_count != 1:
            return ProbeResult(
                "error",
                diagnostics=_diag(
                    "error",
                    page_type=final_page_type,
                    date_control_count=date_control_count,
                    plausible_count=discovery.plausible_count,
                    target_found=True,
                    target_enabled=True,
                    shift_control_count=len(last_controls),
                    error_class=(
                        "control_scan_failed" if last_scan_failed
                        else "date_control_changed"
                    ),
                ),
            )
        if len(last_controls) > 1:
            error_class = "ambiguous_shift_control"
        elif not last_controls:
            error_class = "shift_control_missing"
        elif not last_controls[0].shifts:
            error_class = "shift_options_empty"
        elif not selected_confirmed:
            error_class = "target_selection_unconfirmed"
        elif requires_livewire_confirmation and not update_monitor.succeeded:
            error_class = "shift_update_response_unconfirmed"
        else:
            error_class = "shift_update_unconfirmed"
        return ProbeResult(
            "unknown",
            diagnostics=_diag(
                "unknown",
                page_type=discovery.page_type,
                date_control_count=date_control_count,
                plausible_count=discovery.plausible_count,
                target_found=True,
                target_enabled=True,
                shift_control_count=len(last_controls),
                update_confirmed=update_confirmed and selected_confirmed,
                shift_count=(len(last_controls[0].shifts) if len(last_controls) == 1 else 0),
                error_class=error_class,
            ),
        )
    finally:
        update_monitor.stop()


def _response_header(response: object, name: str) -> str:
    try:
        header_value = getattr(response, "header_value", None)
        if callable(header_value):
            return _clean_text(str(header_value(name) or ""))
    except Exception:
        pass
    return ""


def _navigation_failure(response: object | None) -> _UpdateFailure | None:
    if response is None:
        return None
    try:
        status_value = getattr(response, "status", 0)
        if callable(status_value):
            status_value = status_value()
        status = int(status_value)
    except Exception:
        return _UpdateFailure("error", "navigation_status_unreadable")

    if _response_header(response, "cf-mitigated").casefold() == "challenge":
        return _UpdateFailure("bot", "navigation_challenge")
    if status == 401:
        return _UpdateFailure("login", "navigation_http_401")
    if status in {403, 429}:
        return _UpdateFailure("bot", f"navigation_http_{status}")
    if status >= 400:
        return _UpdateFailure("error", f"navigation_http_{status}")
    return None


def _navigate(
    page, cfg: FestzeltOsConfig
) -> tuple[bool, _UpdateFailure | None]:
    try:
        response = page.goto(
            cfg.url_template,
            wait_until=cfg.wait_until,
            timeout=cfg.navigation_timeout_ms,
        )
        return True, _navigation_failure(response)
    except Exception:
        return False, None


def _close_page(page) -> None:
    try:
        page.close()
    except Exception:
        pass


def fetch(
    cfg: FestzeltOsConfig, target_dates: list[str], browser
) -> dict[str, ProbeResult]:
    """Probe target dates without ever submitting a reservation form."""

    parsed_targets: dict[str, str] = {}
    for target in target_dates:
        parsed = canonical_date(target)
        if parsed != target:
            raise ValueError(f"target date must be canonical ISO: {target!r}")
        parsed_targets[target] = parsed
    target_years = {int(target[:4]) for target in parsed_targets}
    results: dict[str, ProbeResult] = {}

    try:
        context = browser.new_context(
            locale="de-DE",
            viewport={"width": 1280, "height": 1100},
        )
    except Exception:
        return {
            target: ProbeResult(
                "error",
                diagnostics=_diag(
                    "error",
                    page_type="unknown",
                    date_control_count=0,
                    plausible_count=0,
                    target_found=False,
                    error_class="browser_context_failed",
                ),
            )
            for target in target_dates
        }

    try:
        for target in target_dates:
            # Each target gets a newly navigated page.  This makes an
            # in-flight Friday response incapable of confirming Saturday and
            # avoids carrying wizard/session DOM state across target dates.
            try:
                page = context.new_page()
            except Exception:
                results[target] = ProbeResult(
                    "error",
                    diagnostics=_diag(
                        "error",
                        page_type="unknown",
                        date_control_count=0,
                        plausible_count=0,
                        target_found=False,
                        error_class="page_creation_failed",
                    ),
                )
                continue
            try:
                navigated, navigation_failure = _navigate(page, cfg)
                if not navigated:
                    results[target] = ProbeResult(
                        "error",
                        diagnostics=_diag(
                            "error",
                            page_type="unknown",
                            date_control_count=0,
                            plausible_count=0,
                            target_found=False,
                            error_class="navigation_failed",
                        ),
                    )
                    continue
                if navigation_failure is not None:
                    results[target] = ProbeResult(
                        "error",
                        diagnostics=_diag(
                            "error",
                            page_type=navigation_failure.page_type,
                            date_control_count=0,
                            plausible_count=0,
                            target_found=False,
                            error_class=navigation_failure.error_class,
                        ),
                    )
                    continue
                results[target] = _probe_target(page, cfg, target, target_years)
            finally:
                _close_page(page)
    finally:
        context.close()
    return results
