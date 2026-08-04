from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from pathlib import Path

import httpx

from .config import TentConfig, load_tents
from .events import apply_probe_result, enqueue_monitor_error
from .fetchers import api as api_fetcher
from .fetchers import festzelt_os as festzelt_os_fetcher
from .fetchers import hash as hash_fetcher
from .fetchers import headless as headless_fetcher
from .fetchers import html as html_fetcher
from .outbox import deliver_next, requeue_dead_letter
from .probe import ProbeDiagnostics, ProbeResult
from .state import State, TentState, load, now_iso, save

ROOT = Path(__file__).resolve().parents[1]
TENTS_DIR = ROOT / "tents"
STATE_PATH = ROOT / "state" / "state.json"

FAILURE_THRESHOLD = 3

log = logging.getLogger("wiesn")


def _legacy_probe(status: str, *, source: str) -> ProbeResult:
    """Conservatively adapt legacy fetchers that cannot correlate shifts."""
    if status == "unavailable":
        return ProbeResult(
            "unavailable",
            shifts=(),
            diagnostics=ProbeDiagnostics(
                health="healthy", page_type="booking", error_class=None, detail=source
            ),
        )
    # `available` without a date-correlated shift is not actionable under the
    # v2 invariant. Keep it degraded until that mode gains shift evidence.
    return ProbeResult(
        "unknown",
        diagnostics=ProbeDiagnostics(
            health="degraded",
            page_type="booking",
            error_class="shift_evidence_unavailable",
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


def _record_tent_health(
    *,
    state: State,
    cfg: TentConfig,
    tent_state: TentState,
    results: list[ProbeResult],
    timestamp: str,
) -> None:
    any_error = any(result.status == "error" for result in results)
    any_degraded = any(result.status == "unknown" for result in results)
    if any_error:
        tent_state.consecutive_failures += 1
        tent_state.consecutive_degraded = 0
        tent_state.last_error = timestamp
        incident_count = tent_state.consecutive_failures
        incident_kind = "error"
    elif any_degraded:
        tent_state.consecutive_failures = 0
        tent_state.consecutive_degraded += 1
        incident_count = tent_state.consecutive_degraded
        incident_kind = "degraded"
    else:
        tent_state.consecutive_failures = 0
        tent_state.consecutive_degraded = 0
        tent_state.last_success_at = timestamp
        tent_state.failure_incident_open = False
        return

    if incident_count >= FAILURE_THRESHOLD and not tent_state.failure_incident_open:
        tent_state.failure_incident_sequence += 1
        affected = [
            date
            for date, result in zip(cfg.dates, results, strict=True)
            if result.status in {"unknown", "error"}
        ]
        enqueue_monitor_error(
            state=state,
            tent_slug=cfg.slug,
            tent_name=cfg.name,
            details=(
                f"{cfg.name}: {incident_kind} seit {incident_count} Läufen "
                f"({', '.join(affected)})"
            ),
            incident_number=tent_state.failure_incident_sequence,
            timestamp=timestamp,
        )
        tent_state.failure_incident_open = True


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
    run_timestamp = now_iso()
    state.workflow_last_run_at = run_timestamp

    tents = [tent for tent in load_tents(tents_dir) if tent.enabled]
    log.info("checking %d tents", len(tents))
    needs_browser = any(tent.mode in ("headless", "festzelt_os") for tent in tents)
    playwright = None
    browser = None
    browser_error: Exception | None = None
    if needs_browser:
        try:
            playwright, browser = headless_fetcher.launch_browser()
        except Exception as exc:
            browser_error = exc
            log.error("could not launch browser: %s", type(exc).__name__)

    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            for cfg in tents:
                if jitter:
                    time.sleep(random.uniform(0.2, 0.6))
                tent_state = state.tents.setdefault(cfg.slug, TentState())
                results: list[ProbeResult] = []

                if cfg.mode == "festzelt_os":
                    if browser is None:
                        detail = type(browser_error).__name__ if browser_error else None
                        batch = {
                            date: _error_probe("browser_unavailable", detail)
                            for date in cfg.dates
                        }
                    else:
                        assert cfg.festzelt_os
                        try:
                            batch = festzelt_os_fetcher.fetch(
                                cfg.festzelt_os, cfg.dates, browser
                            )
                        except Exception as exc:
                            log.warning("%s: probe failed: %s", cfg.slug, type(exc).__name__)
                            batch = {
                                date: _error_probe(
                                    "probe_exception", type(exc).__name__
                                )
                                for date in cfg.dates
                            }
                    for iso_date in cfg.dates:
                        result = batch.get(
                            iso_date, _error_probe("missing_probe_result")
                        )
                        results.append(result)
                        applied = apply_probe_result(
                            state=state,
                            cfg=cfg,
                            tent_state=tent_state,
                            iso_date=iso_date,
                            result=result,
                            timestamp=run_timestamp,
                        )
                        log.info(
                            "%s/%s observed=%s health=%s shifts=%s diagnostics=%s",
                            cfg.slug,
                            iso_date,
                            applied.observed_status,
                            applied.health,
                            list(result.shifts or ()),
                            result.diagnostic_dict(),
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
                        results.append(result)
                        apply_probe_result(
                            state=state,
                            cfg=cfg,
                            tent_state=tent_state,
                            iso_date=iso_date,
                            result=result,
                            timestamp=run_timestamp,
                        )
                        if cfg.mode == "hash" and new_hash:
                            tent_state.dates[iso_date].diagnostics["content_hash"] = new_hash

                _record_tent_health(
                    state=state,
                    cfg=cfg,
                    tent_state=tent_state,
                    results=results,
                    timestamp=run_timestamp,
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
        outcome = deliver_next(
            args.state_path, max_wait_seconds=max(0, args.max_wait_seconds)
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
