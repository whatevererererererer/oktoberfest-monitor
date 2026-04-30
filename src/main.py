from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from pathlib import Path

import httpx

from .config import TentConfig, load_tents
from .fetchers import api as api_fetcher
from .fetchers import festzelt_os as festzelt_os_fetcher
from .fetchers import hash as hash_fetcher
from .fetchers import headless as headless_fetcher
from .fetchers import html as html_fetcher
from .notify import alert_available, alert_error
from .state import State, TentDateState, TentState, load, now_iso, save

ROOT = Path(__file__).resolve().parents[1]
TENTS_DIR = ROOT / "tents"
STATE_PATH = ROOT / "state" / "state.json"

FAILURE_THRESHOLD = 3

log = logging.getLogger("wiesn")


def _check_one(cfg: TentConfig, iso_date: str, client: httpx.Client, prev_hash: str | None, browser=None):
    if cfg.mode == "api":
        assert cfg.api, f"{cfg.slug}: mode=api requires api block"
        return api_fetcher.fetch(cfg.api, iso_date, client), None
    if cfg.mode == "html":
        assert cfg.html, f"{cfg.slug}: mode=html requires html block"
        return html_fetcher.fetch(cfg.html, iso_date, client), None
    if cfg.mode == "hash":
        assert cfg.hash, f"{cfg.slug}: mode=hash requires hash block"
        h = hash_fetcher.fetch_hash(cfg.hash, iso_date, client)
        if prev_hash is None:
            return "unknown", h
        return ("available" if h != prev_hash else "unavailable"), h
    if cfg.mode == "headless":
        assert cfg.headless, f"{cfg.slug}: mode=headless requires headless block"
        assert browser is not None, "headless mode requires a Playwright browser"
        return headless_fetcher.fetch(cfg.headless, iso_date, browser), None
    if cfg.mode == "manual":
        return "unknown", None
    raise ValueError(f"unknown mode {cfg.mode}")


def _process_result(
    cfg: TentConfig,
    tent_state: TentState,
    iso_date: str,
    new_status: str,
    new_shifts: list[str] | None,
    *,
    dry_run: bool,
    aggregate_errors: list,
) -> None:
    """Update state for one (tent, date), fire Pushover on transitions or shift changes."""
    ds = tent_state.dates.setdefault(iso_date, TentDateState())
    prev_status = ds.status
    prev_shifts = list(ds.shifts)

    ds.last_check = now_iso()

    if new_status != prev_status:
        log.info("%s/%s: %s -> %s", cfg.slug, iso_date, prev_status, new_status)
        if cfg.mode != "hash":
            ds.last_change = now_iso()
    ds.status = new_status
    if new_shifts is not None:
        ds.shifts = list(new_shifts)

    became_available = new_status == "available" and prev_status in ("unavailable", "unknown")
    shifts_added: list[str] = []
    # Only consider shift changes when we have a non-empty prior baseline,
    # otherwise the first post-migration run would spuriously alert.
    if (
        not became_available
        and new_status == "available"
        and prev_status == "available"
        and new_shifts is not None
        and prev_shifts
    ):
        added = [s for s in new_shifts if s not in prev_shifts]
        if added:
            shifts_added = added
            log.info("%s/%s: shifts added %s (now %s)", cfg.slug, iso_date, added, new_shifts)

    if became_available or shifts_added:
        reason = "shifts_added" if shifts_added and not became_available else "available"
        log.info("%s/%s: notifying (%s)", cfg.slug, iso_date, reason)
        if dry_run:
            log.info(
                "dry-run: would notify %s/%s reason=%s shifts=%s new=%s",
                cfg.slug, iso_date, reason, new_shifts, shifts_added,
            )
            return
        try:
            alert_available(
                tent_name=cfg.name,
                tent_slug=cfg.slug,
                iso_date=iso_date,
                booking_url=cfg.booking_url,
                shifts=new_shifts or [],
                new_shifts=shifts_added or None,
                reason=reason,
            )
        except Exception as e:
            log.error("pushover failed for %s/%s: %s", cfg.slug, iso_date, e)
            aggregate_errors.append(f"pushover: {e}")


def run(*, dry_run: bool = False) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = load(STATE_PATH)
    state.workflow_last_run_at = now_iso()

    tents = [t for t in load_tents(TENTS_DIR) if t.enabled]
    log.info("checking %d tents", len(tents))

    aggregate_errors: list[str] = []
    needs_browser = any(t.mode in ("headless", "festzelt_os") for t in tents)
    pw_ctx = None
    browser = None
    if needs_browser:
        try:
            pw_ctx, browser = headless_fetcher.launch_browser()
        except Exception as e:
            log.error("could not launch playwright browser: %s", e)
            aggregate_errors.append(f"playwright launch: {e}")

    with httpx.Client(timeout=15, follow_redirects=True) as client:
        for cfg in tents:
            tent_state = state.tents.setdefault(cfg.slug, TentState())
            time.sleep(random.uniform(1.0, 3.0))
            tent_failed_this_run = False

            if cfg.mode == "festzelt_os":
                assert cfg.festzelt_os, f"{cfg.slug}: mode=festzelt_os requires block"
                if browser is None:
                    log.warning("%s: no browser available, skipping", cfg.slug)
                    tent_failed_this_run = True
                else:
                    try:
                        results = festzelt_os_fetcher.fetch(cfg.festzelt_os, cfg.dates, browser)
                        for iso_date, (status, shifts) in results.items():
                            if status == "error":
                                tent_failed_this_run = True
                                aggregate_errors.append(f"{cfg.slug}/{iso_date}: shift probe error")
                                continue
                            _process_result(
                                cfg, tent_state, iso_date, status, shifts,
                                dry_run=dry_run, aggregate_errors=aggregate_errors,
                            )
                    except Exception as e:
                        log.warning("%s: fetch failed: %s", cfg.slug, e)
                        tent_failed_this_run = True
                        aggregate_errors.append(f"{cfg.slug}: {e}")
            else:
                for iso_date in cfg.dates:
                    ds = tent_state.dates.setdefault(iso_date, TentDateState())
                    prev_hash = None
                    if cfg.mode == "hash" and ds.last_change and ds.last_change.startswith("hash:"):
                        prev_hash = ds.last_change[5:]
                    try:
                        new_status, new_hash = _check_one(cfg, iso_date, client, prev_hash, browser=browser)
                    except Exception as e:
                        log.warning("%s/%s: fetch failed: %s", cfg.slug, iso_date, e)
                        ds.status = "error"
                        ds.last_check = now_iso()
                        tent_failed_this_run = True
                        aggregate_errors.append(f"{cfg.slug}/{iso_date}: {e}")
                        continue
                    if cfg.mode == "hash" and new_hash:
                        ds.last_change = f"hash:{new_hash}"
                    _process_result(
                        cfg, tent_state, iso_date, new_status, None,
                        dry_run=dry_run, aggregate_errors=aggregate_errors,
                    )

            if tent_failed_this_run:
                tent_state.consecutive_failures += 1
                tent_state.last_error = now_iso()
            else:
                tent_state.consecutive_failures = 0
                tent_state.last_success_at = now_iso()

    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
    if pw_ctx is not None:
        try:
            pw_ctx.stop()
        except Exception:
            pass

    save(STATE_PATH, state)

    persistently_broken = [
        slug for slug, ts in state.tents.items() if ts.consecutive_failures >= FAILURE_THRESHOLD
    ]
    if persistently_broken and not dry_run:
        try:
            alert_error(
                summary=f"{len(persistently_broken)} tent(s) consistently failing",
                details="\n".join(persistently_broken),
            )
        except Exception as e:
            log.error("error-pushover failed: %s", e)

    # Always return 0 for transient tent-level errors. Persistent failures are
    # escalated via the Pushover error app (consecutive_failures >= threshold).
    # Returning 1 only when nothing succeeded would still cause spam, so 0 always.
    if aggregate_errors:
        log.info("run had %d non-fatal errors; exiting 0 anyway", len(aggregate_errors))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="don't send notifications")
    args = parser.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
