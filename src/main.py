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


def run(*, dry_run: bool = False) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = load(STATE_PATH)
    state.workflow_last_run_at = now_iso()

    tents = [t for t in load_tents(TENTS_DIR) if t.enabled]
    log.info("checking %d tents", len(tents))

    aggregate_errors: list[str] = []
    needs_headless = any(t.mode == "headless" for t in tents)
    pw_ctx = None
    browser = None
    if needs_headless:
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
            for iso_date in cfg.dates:
                ds = tent_state.dates.setdefault(iso_date, TentDateState())
                prev_status = ds.status

                # For mode=hash we stash the previous hash in last_change misuse-free:
                # use a dedicated field on TentDateState would be cleaner, but mode=hash
                # is a fallback — store the hash in last_change as "hash:<sha>".
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

                ds.last_check = now_iso()
                if cfg.mode == "hash" and new_hash:
                    ds.last_change = f"hash:{new_hash}"

                if new_status != prev_status:
                    log.info("%s/%s: %s -> %s", cfg.slug, iso_date, prev_status, new_status)
                    if cfg.mode != "hash":
                        ds.last_change = now_iso()

                ds.status = new_status

                # Trigger: transition into "available"
                if new_status == "available" and prev_status in ("unavailable", "unknown"):
                    log.info("%s/%s: notifying", cfg.slug, iso_date)
                    if dry_run:
                        log.info("dry-run: would notify %s/%s", cfg.slug, iso_date)
                    else:
                        try:
                            alert_available(
                                tent_name=cfg.name,
                                tent_slug=cfg.slug,
                                iso_date=iso_date,
                                booking_url=cfg.booking_url,
                            )
                        except Exception as e:
                            log.error("pushover failed for %s/%s: %s", cfg.slug, iso_date, e)
                            aggregate_errors.append(f"pushover: {e}")

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

    # Aggregate-failure alert: tents that have failed FAILURE_THRESHOLD+ times in a row.
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

    return 0 if not aggregate_errors else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="don't send notifications")
    args = parser.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
