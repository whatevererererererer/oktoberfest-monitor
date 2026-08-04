from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

import httpx

from .config import TentConfig, load_tents
from .events import AppliedProbe, apply_probe_result, enqueue_monitor_error
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
MAX_FESTZELT_WORKERS = 4

log = logging.getLogger("wiesn")


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
    """Probe one deterministic shard with thread-owned Playwright resources."""

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
                time.sleep(random.uniform(0.2, 0.6))
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
    """Run at most four isolated browser workers and join all of them."""

    if not configs:
        return {}

    worker_count = min(MAX_FESTZELT_WORKERS, len(configs))
    shards: list[list[TentConfig]] = [[] for _ in range(worker_count)]
    for index, cfg in enumerate(configs):
        shards[index % worker_count].append(cfg)

    batches: dict[str, dict[str, ProbeResult]] = {}
    with ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="festzelt-probe"
    ) as executor:
        futures = [
            executor.submit(
                _probe_festzelt_worker, tuple(shard), jitter=jitter
            )
            for shard in shards
        ]
        # Consume shard results in submission order. State is intentionally not
        # touched until every worker has stopped and the main-thread loop below
        # applies tent results in configuration order.
        for shard, future in zip(shards, futures, strict=True):
            try:
                batches.update(future.result())
            except Exception as exc:
                detail = type(exc).__name__
                log.error("festzelt worker failed: %s", detail)
                for cfg in shard:
                    batches[cfg.slug] = _probe_error_batch(
                        cfg, "probe_exception", detail
                    )
    return batches


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
        affected = [
            date
            for date, result in zip(cfg.dates, results, strict=True)
            if result.observed_status in {"unknown", "error"}
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

    tents = [tent for tent in load_tents(tents_dir) if tent.enabled]
    log.info("checking %d tents", len(tents))
    festzelt_batches = _probe_festzelt_tents(
        [tent for tent in tents if tent.mode == "festzelt_os"], jitter=jitter
    )

    # Legacy headless configs retain one main-thread browser. Festzelt-OS
    # workers never share it (or any Playwright object) across threads.
    needs_browser = any(tent.mode == "headless" for tent in tents)
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
