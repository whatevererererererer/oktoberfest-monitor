"""Safe Playwright probe for Festzelt-OS and compatible booking wizards.

The fetcher deliberately distinguishes a proven missing target date from an
unreadable page.  It uses one page per tent to keep load low, but reacquires
all controls after every wizard rerender and requires target-specific update
evidence before attributing any shift options.
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date as date_type
from typing import Iterable
from urllib.parse import urlsplit

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
    r"^(?:-|--|bitte\s+(?:aus)?w[aä]hlen|ausw[aä]hlen|select|choose|schicht|zeit)$", re.I
)
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


@dataclass(frozen=True, slots=True)
class _Option:
    value: str
    text: str
    canonical_date: str | None
    disabled: bool


@dataclass(frozen=True, slots=True)
class _DateControl:
    locator: object
    options: tuple[_Option, ...]
    signature: tuple[tuple[str, str, bool], ...]
    livewire_bound: bool = False


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
    """Capture only a privacy-safe failure class for target-triggered updates."""

    def __init__(self, page) -> None:
        self.page = page
        self.failure: _UpdateFailure | None = None
        self.request_started = False
        self.succeeded = False
        self._listening_request = False
        self._listening_response = False
        self._listening_request_failed = False

    def _on_request(self, request) -> None:
        if _is_relevant_update_url(getattr(request, "url", "")):
            self.request_started = True

    def _on_response(self, response) -> None:
        try:
            if not _is_relevant_update_url(getattr(response, "url", "")):
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
            self.succeeded = True

    def _on_request_failed(self, request) -> None:
        if _is_relevant_update_url(getattr(request, "url", "")):
            self.request_started = True
            self.failure = _UpdateFailure("error", "shift_update_network_error")

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


def _option_date(value: str, text: str) -> str | None:
    by_value = canonical_date(value)
    by_text = canonical_date(text)
    if by_value and by_text and by_value != by_text:
        return None
    return by_value or by_text


def _read_options(select) -> tuple[_Option, ...]:
    options = select.locator("option")
    result: list[_Option] = []
    for index in range(options.count()):
        option = options.nth(index)
        value = _clean_text(option.get_attribute("value", timeout=1000))
        text = _clean_text(option.inner_text(timeout=1000))
        result.append(
            _Option(
                value=value,
                text=text,
                canonical_date=_option_date(value, text),
                disabled=bool(option.is_disabled(timeout=1000)),
            )
        )
    return tuple(result)


def _attributes(select) -> str:
    names = (
        "id",
        "name",
        "aria-label",
        "data-testid",
        "wire:model",
        "wire:model.live",
        "wire:model.change",
        "v-model",
    )
    return " ".join(_clean_text(select.get_attribute(name, timeout=1000)) for name in names)


def _is_livewire_bound(select) -> bool:
    try:
        return bool(
            select.evaluate(
                "el => el.getAttributeNames().some(name => "
                "name === 'wire:model' || name.startsWith('wire:model.'))"
            )
        )
    except Exception:
        pass
    for name in ("wire:model", "wire:model.live", "wire:model.change"):
        try:
            if _clean_text(select.get_attribute(name, timeout=1000)):
                return True
        except Exception:
            continue
    return False


def _all_selects(page, selector: str | None):
    return page.locator(selector or "select")


def _scan_date_controls(
    page, cfg: FestzeltOsConfig, target_years: set[int]
) -> tuple[list[_DateControl], int]:
    selects = _all_selects(page, cfg.date_selector)
    raw_count = selects.count()
    controls: list[_DateControl] = []
    for index in range(raw_count):
        select = selects.nth(index)
        options = _read_options(select)
        plausible = tuple(
            option
            for option in options
            if option.canonical_date
            and int(option.canonical_date[:4]) in target_years
            and int(option.canonical_date[5:7]) in {9, 10}
        )
        if not plausible:
            continue
        controls.append(
            _DateControl(
                locator=select,
                options=options,
                signature=tuple((o.value, o.text, o.disabled) for o in options),
                livewire_bound=_is_livewire_bound(select),
            )
        )
    return controls, raw_count


def _page_type(page) -> PageType:
    try:
        title = _clean_text(page.title()).casefold()
    except Exception:
        title = ""
    try:
        body = _clean_text(page.locator("body").inner_text(timeout=1000)).casefold()
    except Exception:
        body = ""
    sample = f"{title} {body[:4000]}"
    if any(marker in sample for marker in _BOT_MARKERS):
        return "bot"
    if any(marker in sample for marker in _ERROR_MARKERS):
        return "error"
    if any(marker in sample for marker in _LOGIN_MARKERS):
        try:
            if page.locator('input[type="password"]').count() > 0:
                return "login"
        except Exception:
            pass
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
            controls, _ = _scan_date_controls(page, cfg, target_years)
        except Exception:
            controls = []
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
        if stable:
            plausible_count = sum(
                1
                for control in controls
                for option in control.options
                if option.canonical_date
                and int(option.canonical_date[:4]) in target_years
                and int(option.canonical_date[5:7]) in {9, 10}
            )
            if len(controls) == 1:
                return _DateDiscovery(
                    controls[0], page_type, 1, plausible_count
                )
            return _DateDiscovery(
                None,
                page_type,
                len(controls),
                plausible_count,
                "ambiguous_date_control",
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
        if not label or not option.value or option.disabled or _PLACEHOLDER.fullmatch(label):
            continue
        key = _fold(label)
        if key not in seen:
            seen.add(key)
            labels.append(label)
    return tuple(labels)


def _scan_shift_controls(page, cfg: FestzeltOsConfig) -> list[_ShiftControl]:
    selects = _all_selects(page, cfg.shift_selector)
    controls: list[_ShiftControl] = []
    for index in range(selects.count()):
        select = selects.nth(index)
        options = _read_options(select)
        attrs = _attributes(select)
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
    value = _clean_text(control.locator.input_value(timeout=1000))
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
    discovery = _wait_for_date_control(page, cfg, target_years)
    if discovery.error_class or discovery.control is None:
        return _error_from_discovery(discovery)

    control = discovery.control
    target_options = [option for option in control.options if option.canonical_date == target]
    if not target_options:
        # Negative evidence needs a longer stability window than positive
        # target discovery: some SPAs append date options in phases.
        absence_stable_ms = max(cfg.stable_for_ms, cfg.wait_extra_ms)
        absence_deadline = time.monotonic() + max(
            cfg.date_control_timeout_ms, absence_stable_ms
        ) / 1000
        stable_signature = control.signature
        stable_since = time.monotonic()
        last_date_control_count = 1
        while time.monotonic() <= absence_deadline:
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
                date_controls, _ = _scan_date_controls(page, cfg, target_years)
            except Exception:
                date_controls = []
            last_date_control_count = len(date_controls)
            if len(date_controls) != 1:
                stable_since = time.monotonic()
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
            if (now - stable_since) * 1000 >= absence_stable_ms:
                return ProbeResult(
                    "unavailable",
                    shifts=(),
                    diagnostics=_diag(
                        "unavailable",
                        page_type=current_page_type,
                        date_control_count=1,
                        plausible_count=sum(
                            1 for option in refreshed.options if option.canonical_date
                        ),
                        target_found=False,
                    ),
                )
        else:
            status = "error" if last_date_control_count != 1 else "unknown"
            return ProbeResult(
                status,
                diagnostics=_diag(
                    status,
                    page_type=_page_type(page),
                    date_control_count=last_date_control_count,
                    plausible_count=discovery.plausible_count,
                    target_found=False,
                    error_class=(
                        "date_control_changed"
                        if last_date_control_count != 1
                        else "date_options_unstable"
                    ),
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
    update_monitor = _WizardUpdateMonitor(page)
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

        while time.monotonic() <= deadline:
            if update_monitor.failure is not None:
                return _update_failure_result(
                    update_monitor.failure, discovery, len(last_controls)
                )
            try:
                date_controls, _ = _scan_date_controls(page, cfg, target_years)
                date_control_count = len(date_controls)
                last_scan_failed = False
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
                changed = (
                    len(initial_shift_controls) != 1
                    or controls[0].signature != initial_signature
                    or _dom_update_observed(page, observer_armed)
                )
                update_confirmed = update_confirmed or (selected_confirmed and changed)
                network_confirmed = (
                    not requires_livewire_confirmation or update_monitor.succeeded
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


def _navigate(page, cfg: FestzeltOsConfig) -> bool:
    try:
        page.goto(
            cfg.url_template,
            wait_until=cfg.wait_until,
            timeout=cfg.navigation_timeout_ms,
        )
        return True
    except Exception:
        return False


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
        page = context.new_page()
        try:
            if not _navigate(page, cfg):
                failure = ProbeResult(
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
                return {target: failure for target in target_dates}

            for target in target_dates:
                # _probe_target resolves the date control and its options anew;
                # no Locator from the previous target survives a DOM rerender.
                results[target] = _probe_target(page, cfg, target, target_years)
        finally:
            _close_page(page)
    finally:
        context.close()
    return results
