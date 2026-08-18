from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import random
import re
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

from .config import TentConfig, load_tents
from .events import AppliedProbe, apply_probe_result, enqueue_monitor_error
from .fetchers import api as api_fetcher
from .fetchers import festzelt_os as festzelt_os_fetcher
from .fetchers import floesserstadl as floesserstadl_fetcher
from .fetchers import hash as hash_fetcher
from .fetchers import headless as headless_fetcher
from .fetchers import html as html_fetcher
from .fetchers import kaefer as kaefer_fetcher
from .fetchers import reservierungsmanager as reservierungsmanager_fetcher
from .outbox import deliver_next, requeue_dead_letter
from .probe import ProbeDiagnostics, ProbeResult
from .state import State, TentState, load, now_iso, save

ROOT = Path(__file__).resolve().parents[1]
TENTS_DIR = ROOT / "tents"
STATE_PATH = ROOT / "state" / "state.json"

FAILURE_THRESHOLD = 3
PROBE_BATCH_SIZE = 3
MONITOR_ERROR_MESSAGE_LIMIT = 1024
FLOESSER_HTTP_WALL_SECONDS = 25.0
RESERVIERUNGSMANAGER_HTTP_WALL_SECONDS = 65.0

_ERROR_REASON_LABELS = {
    "date_and_shift_evidence_unavailable": "Datum und Schichten konnten nicht sicher gemeinsam geprüft werden",
    "manual_mode": "Dieses Portal benötigt eine manuelle Prüfung",
    "shift_update_challenge": "Bot-Schutz/Challenge beim Laden der Schichtauswahl",
    "shift_update_response_not_json": "Schicht-Update lieferte keine gültige JSON-Antwort",
    "shift_update_response_too_large": "Antwort des Schicht-Updates war unerwartet groß",
    "shift_update_response_invalid_length": "Länge der Schicht-Update-Antwort war ungültig",
    "shift_update_response_json_unreadable": "JSON-Antwort des Schicht-Updates war nicht lesbar",
    "shift_update_response_invalid_json": "Schicht-Update lieferte ungültiges JSON",
    "shift_update_response_incomplete": "Antwort des Schicht-Updates blieb unvollständig",
    "shift_update_network_error": "Netzwerkfehler beim Schicht-Update",
    "navigation_challenge": "Bot-Schutz/Challenge beim Öffnen der Buchungsseite",
    "navigation_status_unreadable": "HTTP-Status der Buchungsseite war nicht lesbar",
    "browser_unavailable": "Prüf-Browser konnte nicht gestartet werden",
    "browser_context_failed": "Browser-Sitzung konnte nicht gestartet werden",
    "page_creation_failed": "Browserseite konnte nicht erstellt werden",
    "navigation_failed": "Buchungsseite konnte nicht geladen werden",
    "probe_exception": "Technischer Fehler während der Portalprüfung",
    "missing_probe_batch": "Ergebnis des Portal-Prüflaufs fehlte",
    "missing_probe_result": "Ergebnis für das Zieldatum fehlte",
    "invalid_probe_result": "Prüfergebnis hatte ein ungültiges Format",
    "date_control_error": "Datumsauswahl konnte nicht gelesen werden",
    "date_control_missing": "Datumsauswahl wurde nicht gefunden",
    "ambiguous_date_control": "Datumsauswahl war nicht eindeutig",
    "date_control_unstable": "Datumsauswahl blieb während der Prüfung instabil",
    "date_control_changed": "Datumsauswahl änderte sich während der Prüfung",
    "control_scan_failed": "Datums- und Schichtauswahl konnten nicht gelesen werden",
    "date_option_conflict": "Datumsauswahl war widersprüchlich",
    "date_options_unstable": "Datumsauswahl änderte sich während der Prüfung",
    "target_option_ambiguous": "Zieldatum war nicht eindeutig",
    "target_disabled": "Zieldatum war deaktiviert und konnte nicht sicher geprüft werden",
    "date_selection_failed": "Zieldatum konnte nicht ausgewählt werden",
    "target_selection_unconfirmed": "Auswahl des Zieldatums wurde nicht bestätigt",
    "available_without_shifts": "Verfügbarkeit wurde ohne lesbare Schichten gemeldet",
    "inconsistent_available_diagnostics": "Verfügbarkeitsdaten waren widersprüchlich",
    "inconsistent_unavailable_diagnostics": "Nichtverfügbarkeitsdaten waren widersprüchlich",
    "shift_control_missing": "Schichtauswahl wurde nicht gefunden",
    "ambiguous_shift_control": "Schichtauswahl war nicht eindeutig",
    "shift_evidence_unavailable": "Schichten konnten nicht sicher belegt werden",
    "shift_options_empty": "Schichtauswahl war leer oder nicht lesbar",
    "shift_update_response_unconfirmed": "Aktualisierung der Schichtauswahl wurde nicht bestätigt",
    "shift_update_unconfirmed": "Aktualisierung der Schichtauswahl blieb unbestätigt",
    "bot_page": "Bot-Schutz/Challenge statt der Buchungsseite",
    "login_page": "Login-Seite statt der Buchungsseite",
    "error_page": "Fehlerseite statt der Buchungsseite",
    "bot_while_confirming_absence": "Bot-Schutz/Challenge beim Bestätigen der Nichtverfügbarkeit",
    "login_while_confirming_absence": "Login-Seite beim Bestätigen der Nichtverfügbarkeit",
    "error_while_confirming_absence": "Fehlerseite beim Bestätigen der Nichtverfügbarkeit",
    "bot_after_selection": "Bot-Schutz/Challenge nach Auswahl des Zieldatums",
    "login_after_selection": "Login-Seite nach Auswahl des Zieldatums",
    "error_after_selection": "Fehlerseite nach Auswahl des Zieldatums",
    "bot_during_navigation": "Bot-Schutz/Challenge beim Öffnen der Buchungsseite",
    "login_during_navigation": "Login-Seite beim Öffnen der Buchungsseite",
    "error_during_navigation": "Fehlerseite beim Öffnen der Buchungsseite",
    "event_days_schema_invalid": "Reservierungsdaten des Widgets hatten ein unerwartetes Format",
    "event_days_empty": "Reservierungs-Widget lieferte keine auswertbaren Termine",
    "event_days_no_matching_tickets": "Reservierungs-Widget lieferte keine eindeutig passenden Zelt-Termine",
    "slot_schema_invalid": "Käfer-Slotdaten hatten ein unerwartetes Format",
    "target_slots_incomplete": "Käfer-Slotdaten für das Zieldatum waren unvollständig",
    "reservation_form_schema_invalid": "Reservierungsformular hatte ein unerwartetes Format",
    "reservation_form_no_dates": "Reservierungsformular enthielt keine plausiblen Wiesn-Termine",
}
_HTTP_ERROR_CLASS = re.compile(r"(navigation|shift_update)_http_([1-5][0-9]{2})")
_PAGE_TYPE_REASON_LABELS = {
    "bot": "Bot-Schutz/Challenge auf der Buchungsseite",
    "login": "Login-Seite statt der Buchungsseite",
    "error": "Fehlerseite des Buchungsportals",
    "booking": "Buchungsseite konnte nicht eindeutig ausgewertet werden",
    "unknown": "Technische Portalprüfung lieferte keinen eindeutigen Seitentyp",
}

log = logging.getLogger("wiesn")


def _http_failure_reason(scope: str, status: int) -> str:
    target = "Buchungsseite" if scope == "navigation" else "Schicht-Update"
    if status == 401:
        return f"{target} verlangte eine Anmeldung (HTTP 401)"
    if status == 403:
        return f"Zugriff auf {target} wurde abgelehnt (HTTP 403)"
    if status == 429:
        return f"Rate-Limit von {target} wurde erreicht (HTTP 429)"
    if status >= 500:
        return f"{target} meldete einen Serverfehler (HTTP {status})"
    return f"{target} meldete einen HTTP-Fehler ({status})"


def _probe_failure_reason(diagnostics: dict[str, object]) -> str:
    raw_error_class = diagnostics.get("error_class")
    error_class = raw_error_class if isinstance(raw_error_class, str) else None
    explanation = _ERROR_REASON_LABELS.get(error_class or "")
    if explanation is not None:
        return f"{explanation} (Code: {error_class})"
    http_match = _HTTP_ERROR_CLASS.fullmatch(error_class or "")
    if http_match is not None:
        explanation = _http_failure_reason(
            http_match.group(1),
            int(http_match.group(2)),
        )
        return f"{explanation} (Code: {error_class})"
    raw_page_type = diagnostics.get("page_type")
    page_type = raw_page_type if isinstance(raw_page_type, str) else "unknown"
    return _PAGE_TYPE_REASON_LABELS.get(
        page_type,
        _PAGE_TYPE_REASON_LABELS["unknown"],
    )


def _bounded_monitor_error_message(header: str, reason_lines: list[str]) -> str:
    if len(header) > MONITOR_ERROR_MESSAGE_LIMIT:
        return "Wiesn-Monitor: Fehlerdetails konnten nicht sicher formatiert werden."

    message = header
    omitted = "Weitere Fehlergründe wurden aus Platzgründen ausgelassen."
    for line in reason_lines:
        candidate = f"{message}\n{line}"
        if len(candidate) <= MONITOR_ERROR_MESSAGE_LIMIT:
            message = candidate
            continue
        with_omission = f"{message}\n{omitted}"
        if len(with_omission) <= MONITOR_ERROR_MESSAGE_LIMIT:
            message = with_omission
        break
    return message


def _monitor_error_details(
    *,
    cfg: TentConfig,
    tent_state: TentState,
    results: list[AppliedProbe],
    incident_kind: str,
    incident_count: int,
) -> str:
    reason_lines: list[str] = []
    for iso_date, result in zip(cfg.dates, results, strict=True):
        if result.observed_status not in {"unknown", "error"}:
            continue
        date_state = tent_state.dates.get(iso_date)
        diagnostics = date_state.diagnostics if date_state is not None else {}
        reason_lines.append(
            f"Grund {iso_date}: {_probe_failure_reason(diagnostics)}"
        )

    incident_label = "Fehler" if incident_kind == "error" else "Prüfung unklar"
    header = f"{cfg.name}: {incident_label} seit {incident_count} Läufen"
    return _bounded_monitor_error_message(header, reason_lines)


def _legacy_probe(status: str, *, source: str) -> ProbeResult:
    """Conservatively adapt legacy fetchers that cannot correlate shifts."""
    # A marker or unchanged hash cannot prove that a valid date control exists,
    # so neither side of that legacy binary result is reliable business state.
    return ProbeResult(
        "unknown",
        diagnostics=ProbeDiagnostics(
            health="degraded",
            page_type="booking",
            error_class="date_and_shift_evidence_unavailable",
            detail=f"{source}:{status}",
        ),
    )


def _error_probe(error_class: str, detail: str | None = None) -> ProbeResult:
    return ProbeResult(
        "error",
        diagnostics=ProbeDiagnostics(
            health="error",
            page_type="unknown",
            error_class=error_class,
            detail=(detail or "")[:200] or None,
        ),
    )


def _run_bounded_http_adapter(
    action: Callable[[], ProbeResult], *, timeout_seconds: float
) -> ProbeResult:
    """Bound synchronous HTTP headers with an isolated, daemonized worker.

    HTTPX timeouts are per I/O phase, so a peer can otherwise keep response
    headers alive indefinitely one byte at a time. The worker never mutates
    state, and its dedicated client is never reused after a wall-clock timeout.
    """

    outcome: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            value: object = action()
            item = (True, value)
        except Exception as exc:
            item = (False, exc)
        try:
            outcome.put_nowait(item)
        except queue.Full:
            pass

    thread = threading.Thread(
        target=worker,
        name="bounded-http-probe",
        daemon=True,
    )
    thread.start()
    try:
        succeeded, value = outcome.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        raise TimeoutError("HTTP probe wall-clock deadline exceeded") from exc
    if not succeeded:
        assert isinstance(value, Exception)
        raise value
    assert isinstance(value, ProbeResult)
    return value


def _check_one(
    cfg: TentConfig,
    iso_date: str,
    client: httpx.Client,
    prev_hash: str | None,
    browser=None,
) -> tuple[ProbeResult, str | None]:
    if cfg.mode == "api":
        assert cfg.api
        return _legacy_probe(api_fetcher.fetch(cfg.api, iso_date, client), source="api"), None
    if cfg.mode == "html":
        assert cfg.html
        return _legacy_probe(html_fetcher.fetch(cfg.html, iso_date, client), source="html"), None
    if cfg.mode == "hash":
        assert cfg.hash
        value = hash_fetcher.fetch_hash(cfg.hash, iso_date, client)
        if prev_hash is None:
            return _legacy_probe("unknown", source="hash-baseline"), value
        status = "available" if value != prev_hash else "unavailable"
        return _legacy_probe(status, source="hash"), value
    if cfg.mode == "headless":
        assert cfg.headless
        if browser is None:
            return _error_probe("browser_unavailable"), None
        return _legacy_probe(
            headless_fetcher.fetch(cfg.headless, iso_date, browser), source="headless"
        ), None
    if cfg.mode == "kaefer":
        assert cfg.kaefer
        if browser is None:
            return _error_probe("browser_unavailable"), None
        return kaefer_fetcher.fetch(cfg.kaefer, iso_date, browser), None
    if cfg.mode == "floesserstadl":
        assert cfg.floesserstadl
        fetch_function = floesserstadl_fetcher.fetch

        def fetch_floesserstadl() -> ProbeResult:
            with httpx.Client(timeout=15, follow_redirects=False) as isolated_client:
                return fetch_function(
                    cfg.floesserstadl, iso_date, isolated_client
                )

        return _run_bounded_http_adapter(
            fetch_floesserstadl,
            timeout_seconds=FLOESSER_HTTP_WALL_SECONDS,
        ), None
    if cfg.mode == "reservierungsmanager":
        assert cfg.reservierungsmanager
        fetch_function = reservierungsmanager_fetcher.fetch

        def fetch_reservierungsmanager() -> ProbeResult:
            with httpx.Client(timeout=15, follow_redirects=False) as isolated_client:
                return fetch_function(
                    cfg.reservierungsmanager, iso_date, isolated_client
                )

        return _run_bounded_http_adapter(
            fetch_reservierungsmanager,
            timeout_seconds=RESERVIERUNGSMANAGER_HTTP_WALL_SECONDS,
        ), None
    if cfg.mode == "manual":
        return ProbeResult(
            "unknown",
            diagnostics=ProbeDiagnostics(
                health="degraded",
                page_type="unknown",
                error_class="manual_mode",
            ),
        ), None
    raise ValueError(f"unknown mode {cfg.mode}")


def _probe_error_batch(
    cfg: TentConfig, error_class: str, detail: str | None = None
) -> dict[str, ProbeResult]:
    return {
        iso_date: _error_probe(error_class, detail)
        for iso_date in cfg.dates
    }


def _probe_festzelt_worker(
    configs: tuple[TentConfig, ...], *, jitter: bool
) -> dict[str, dict[str, ProbeResult]]:
    """Probe all Festzelt-OS tents sequentially with one browser."""

    playwright = None
    browser = None
    results: dict[str, dict[str, ProbeResult]] = {}
    try:
        try:
            playwright, browser = headless_fetcher.launch_browser()
        except Exception as exc:
            detail = type(exc).__name__
            log.error(
                "could not launch browser for %d festzelt probes: %s",
                len(configs),
                detail,
            )
            return {
                cfg.slug: _probe_error_batch(cfg, "browser_unavailable", detail)
                for cfg in configs
            }

        for cfg in configs:
            if jitter:
                time.sleep(random.uniform(1.0, 3.0))
            try:
                assert cfg.festzelt_os
                fetched = festzelt_os_fetcher.fetch(
                    cfg.festzelt_os, cfg.dates, browser
                )
                if not isinstance(fetched, dict):
                    raise TypeError("probe batch is not a mapping")
                results[cfg.slug] = fetched
            except Exception as exc:
                detail = type(exc).__name__
                log.warning("%s: probe failed: %s", cfg.slug, detail)
                results[cfg.slug] = _probe_error_batch(
                    cfg, "probe_exception", detail
                )
        return results
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass


def _probe_festzelt_tents(
    configs: list[TentConfig], *, jitter: bool
) -> dict[str, dict[str, ProbeResult]]:
    """Run one low-load Festzelt-OS browser session in config order."""

    if not configs:
        return {}
    return _probe_festzelt_worker(tuple(configs), jitter=jitter)


def _select_probe_batch(
    configs: list[TentConfig], cursor: str | None
) -> tuple[list[TentConfig], str | None]:
    """Select one deterministic, non-wrapping batch and its durable cursor.

    The cursor names the first tent due on the next run. If that configuration
    was removed or disabled, restart at the first enabled tent rather than
    guessing from a stale numeric offset. The final (possibly short) batch does
    not wrap, so every enabled tent is visited exactly once per rotation.
    """

    slugs = [cfg.slug for cfg in configs]
    if len(slugs) != len(set(slugs)):
        raise ValueError("enabled tent slugs must be unique for probe rotation")

    if not configs:
        return [], None

    start = 0
    if cursor is not None:
        start = next(
            (index for index, cfg in enumerate(configs) if cfg.slug == cursor),
            0,
        )
    selected = configs[start : start + PROBE_BATCH_SIZE]
    next_index = start + len(selected)
    next_cursor = (
        configs[next_index].slug if next_index < len(configs) else configs[0].slug
    )
    return selected, next_cursor


def _record_tent_health(
    *,
    state: State,
    cfg: TentConfig,
    tent_state: TentState,
    results: list[AppliedProbe],
    timestamp: str,
) -> None:
    statuses = [result.observed_status for result in results]
    any_error = "error" in statuses
    any_degraded = "unknown" in statuses
    any_unhealthy = any_error or any_degraded

    if any_unhealthy:
        tent_state.consecutive_unhealthy += 1
    else:
        tent_state.consecutive_unhealthy = 0

    if any_error:
        tent_state.consecutive_failures += 1
        tent_state.consecutive_degraded = (
            tent_state.consecutive_degraded + 1 if any_degraded else 0
        )
        tent_state.last_error = timestamp
        incident_kind = "error"
    elif any_degraded:
        tent_state.consecutive_failures = 0
        tent_state.consecutive_degraded += 1
        incident_kind = "degraded"
    else:
        tent_state.consecutive_failures = 0
        tent_state.consecutive_degraded = 0
        tent_state.last_success_at = timestamp
        tent_state.failure_incident_open = False
        tent_state.failure_incident_kind = None
        return

    incident_count = tent_state.consecutive_unhealthy
    opens_incident = incident_count >= FAILURE_THRESHOLD and not tent_state.failure_incident_open
    escalates_incident = (
        tent_state.failure_incident_open
        and incident_kind == "error"
        and tent_state.failure_incident_kind != "error"
    )
    if opens_incident or escalates_incident:
        tent_state.failure_incident_sequence += 1
        enqueue_monitor_error(
            state=state,
            tent_slug=cfg.slug,
            tent_name=cfg.name,
            booking_url=cfg.booking_url,
            details=_monitor_error_details(
                cfg=cfg,
                tent_state=tent_state,
                results=results,
                incident_kind=incident_kind,
                incident_count=incident_count,
            ),
            incident_number=tent_state.failure_incident_sequence,
            timestamp=timestamp,
        )
        tent_state.failure_incident_open = True
        tent_state.failure_incident_kind = incident_kind


def _apply_observation(
    *,
    state: State,
    cfg: TentConfig,
    tent_state: TentState,
    iso_date: str,
    result: object,
    timestamp: str,
) -> tuple[ProbeResult | object, AppliedProbe]:
    """Normalize a malformed fetcher result without aborting later tents."""

    try:
        applied = apply_probe_result(
            state=state,
            cfg=cfg,
            tent_state=tent_state,
            iso_date=iso_date,
            result=result,
            timestamp=timestamp,
        )
        return result, applied
    except Exception as exc:
        log.warning(
            "%s/%s: invalid probe result: %s", cfg.slug, iso_date, type(exc).__name__
        )
        fallback = _error_probe("invalid_probe_result", type(exc).__name__)
        applied = apply_probe_result(
            state=state,
            cfg=cfg,
            tent_state=tent_state,
            iso_date=iso_date,
            result=fallback,
            timestamp=timestamp,
        )
        return fallback, applied


def probe_run(
    *,
    dry_run: bool = False,
    state_path: Path = STATE_PATH,
    tents_dir: Path = TENTS_DIR,
    jitter: bool = True,
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    persisted = load(state_path)
    # Dry-run is a true simulation: mutate only an in-memory copy and never save.
    state = State.model_validate(persisted.model_dump()) if dry_run else persisted
    run_started_monotonic = time.monotonic()
    run_timestamp = now_iso()
    state.workflow_last_run_at = run_timestamp
    state.workflow_started_at = run_timestamp
    state.workflow_finished_at = None
    state.workflow_duration_seconds = None
    state.producer_revision = os.environ.get("MONITOR_PRODUCER_REVISION") or None

    enabled_tents = [tent for tent in load_tents(tents_dir) if tent.enabled]
    tents, next_rotation_cursor = _select_probe_batch(
        enabled_tents, state.probe_rotation_cursor
    )
    log.info(
        "checking rotation batch of %d/%d tents: %s",
        len(tents),
        len(enabled_tents),
        ", ".join(tent.slug for tent in tents) or "none",
    )
    festzelt_batches = _probe_festzelt_tents(
        [tent for tent in tents if tent.mode == "festzelt_os"], jitter=jitter
    )

    # Legacy headless configs retain a separate main-thread browser. Festzelt-OS
    # tents have already been probed sequentially with their own browser above.
    needs_browser = any(tent.mode in {"headless", "kaefer"} for tent in tents)
    playwright = None
    browser = None
    if needs_browser:
        try:
            playwright, browser = headless_fetcher.launch_browser()
        except Exception as exc:
            log.error("could not launch browser: %s", type(exc).__name__)

    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            for cfg in tents:
                if jitter and cfg.mode != "festzelt_os":
                    time.sleep(random.uniform(0.2, 0.6))
                tent_state = state.tents.setdefault(cfg.slug, TentState())
                applied_results: list[AppliedProbe] = []

                if cfg.mode == "festzelt_os":
                    batch = festzelt_batches.get(
                        cfg.slug, _probe_error_batch(cfg, "missing_probe_batch")
                    )
                    for iso_date in cfg.dates:
                        result = batch.get(
                            iso_date, _error_probe("missing_probe_result")
                        )
                        observation_timestamp = now_iso()
                        result, applied = _apply_observation(
                            state=state,
                            cfg=cfg,
                            tent_state=tent_state,
                            iso_date=iso_date,
                            result=result,
                            timestamp=observation_timestamp,
                        )
                        applied_results.append(applied)
                        log.info(
                            "%s/%s observed=%s health=%s shifts=%s diagnostics=%s",
                            cfg.slug,
                            iso_date,
                            applied.observed_status,
                            applied.health,
                            list(getattr(result, "shifts", None) or ()),
                            tent_state.dates[iso_date].diagnostics,
                        )
                else:
                    for iso_date in cfg.dates:
                        date_state = tent_state.dates.get(iso_date)
                        prev_hash = None
                        if cfg.mode == "hash" and date_state:
                            stored_hash = date_state.diagnostics.get("content_hash")
                            if isinstance(stored_hash, str) and stored_hash:
                                prev_hash = stored_hash
                        try:
                            result, new_hash = _check_one(
                                cfg, iso_date, client, prev_hash, browser=browser
                            )
                        except Exception as exc:
                            result, new_hash = _error_probe(
                                "probe_exception", type(exc).__name__
                            ), None
                        observation_timestamp = now_iso()
                        result, applied = _apply_observation(
                            state=state,
                            cfg=cfg,
                            tent_state=tent_state,
                            iso_date=iso_date,
                            result=result,
                            timestamp=observation_timestamp,
                        )
                        applied_results.append(applied)
                        log.info(
                            "%s/%s observed=%s health=%s shifts=%s diagnostics=%s",
                            cfg.slug,
                            iso_date,
                            applied.observed_status,
                            applied.health,
                            list(getattr(result, "shifts", None) or ()),
                            tent_state.dates[iso_date].diagnostics,
                        )
                        if cfg.mode == "hash" and new_hash:
                            tent_state.dates[iso_date].diagnostics["content_hash"] = new_hash

                _record_tent_health(
                    state=state,
                    cfg=cfg,
                    tent_state=tent_state,
                    results=applied_results,
                    timestamp=now_iso(),
                )
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass

    state.workflow_finished_at = now_iso()
    state.workflow_duration_seconds = round(
        max(0.0, time.monotonic() - run_started_monotonic), 3
    )
    # Advance only after the complete batch was applied. In production this
    # value becomes authoritative only when the workflow's fail-closed Git
    # checkpoint succeeds; an abort before save/checkpoint retries this batch.
    state.probe_rotation_cursor = next_rotation_cursor

    if dry_run:
        pending = [event.event_id for event in state.outbox.values() if event.status == "pending"]
        log.info("dry-run: state not written; simulated pending events=%s", pending)
    else:
        save(state_path, state)
    return 0


def run(*, dry_run: bool = False) -> int:
    """Backward-compatible entry point for a probe-only run."""
    return probe_run(dry_run=dry_run)


def main() -> int:
    parser = argparse.ArgumentParser()
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--probe", action="store_true", help="probe and enqueue only")
    operation.add_argument(
        "--deliver-next", action="store_true", help="send at most one durable outbox part"
    )
    operation.add_argument(
        "--requeue-event",
        metavar="EVENT_ID",
        help="resume one explicitly reviewed dead-letter event",
    )
    parser.add_argument("--dry-run", action="store_true", help="send nothing and persist nothing")
    parser.add_argument("--max-wait-seconds", type=float, default=35)
    parser.add_argument("--state-path", type=Path, default=STATE_PATH)
    args = parser.parse_args()

    if args.deliver_next:
        if args.dry_run:
            parser.error("--deliver-next and --dry-run cannot be combined")
        enabled_tent_slugs = frozenset(
            tent.slug for tent in load_tents(TENTS_DIR) if tent.enabled
        )
        outcome = deliver_next(
            args.state_path,
            max_wait_seconds=max(0, args.max_wait_seconds),
            enabled_tent_slugs=enabled_tent_slugs,
        )
        print(json.dumps(outcome.__dict__, sort_keys=True))
        if outcome.fatal:
            return 2
        if outcome.status in {"idle", "deferred"}:
            return 3
        return 0
    if args.requeue_event:
        if args.dry_run:
            parser.error("--requeue-event and --dry-run cannot be combined")
        try:
            event = requeue_dead_letter(args.state_path, args.requeue_event)
        except ValueError as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                {
                    "event_id": event.event_id,
                    "next_index": event.next_index,
                    "status": event.status,
                },
                sort_keys=True,
            )
        )
        return 0
    return probe_run(dry_run=args.dry_run, state_path=args.state_path)


if __name__ == "__main__":
    sys.exit(main())
