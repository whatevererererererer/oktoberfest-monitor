"""Visible Windows sidecar for the three bot-affected reservation portals.

This module is intentionally separate from ``src.main``.  It never touches the
Git-backed production state or the Pushover outbox.  Its only durable output is
a small privacy-safe workstation report below ``%LOCALAPPDATA%``.

The browser is installed Google Chrome in headed mode with a dedicated profile.
There are no stealth flags, CAPTCHA solvers, form submissions, or booking steps.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .config import TentConfig, load_tents
from .fetchers import festzelt_os as festzelt_os_fetcher
from .probe import ProbeResult
from .targets import TARGET_DATES

ROOT = Path(__file__).resolve().parents[1]
TENTS_DIR = ROOT / "tents"

# These are the three pages with historical, interactive target-date evidence.
# Other active tents can also encounter bot protection, but expanding this
# headed sidecar requires an explicit code/config review.
WORKSTATION_TENT_SLUGS = (
    "fischer-vroni",
    "paulaner",
    "poschner",
)
WORKSTATION_TARGET_DATES = TARGET_DATES
WORKSTATION_PORTAL_URLS = {
    "fischer-vroni": "https://reservierung.fischer-vroni.de/reservation",
    "paulaner": "https://reservierung.paulanerfestzelt.de/reservierung",
    "poschner": "https://reservierung.poschners.de/reservierung",
}

EXIT_OK = 0
EXIT_NEEDS_MANUAL_ACTION = 10
EXIT_INCONCLUSIVE = 20
EXIT_SETUP_ERROR = 30
REPORT_SCHEMA_VERSION = 1

log = logging.getLogger("wiesn.workstation")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "WiesnMonitor"
    # Primarily useful for tests/non-Windows diagnostics.  The supported target
    # remains Windows 11, where LOCALAPPDATA is always present.
    return Path.home() / "AppData" / "Local" / "WiesnMonitor"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def validate_external_data_path(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if _is_within(resolved, ROOT):
        raise ValueError(f"{label} must be outside the project directory")
    return resolved


def workstation_paths() -> tuple[Path, Path]:
    """Return the one supported data/profile pair for the local sidecar."""

    data_dir = validate_external_data_path(
        default_data_dir(), label="data directory"
    )
    profile_dir = validate_external_data_path(
        data_dir / "ChromeProfile", label="profile directory"
    )
    return data_dir, profile_dir


def select_configs(
    configs: Iterable[TentConfig], requested_slugs: Sequence[str]
) -> list[TentConfig]:
    requested = list(requested_slugs)
    if tuple(requested) != WORKSTATION_TENT_SLUGS:
        raise ValueError(
            "workstation probe requires exactly: "
            + ", ".join(WORKSTATION_TENT_SLUGS)
        )

    config_items = list(configs)
    for slug in WORKSTATION_TENT_SLUGS:
        if sum(cfg.slug == slug for cfg in config_items) != 1:
            raise ValueError(f"expected exactly one tent configuration: {slug}")
    by_slug = {cfg.slug: cfg for cfg in config_items}
    missing = [slug for slug in requested if slug not in by_slug]
    if missing:
        raise ValueError("missing tent configuration(s): " + ", ".join(missing))

    selected = [by_slug[slug] for slug in requested]
    for cfg in selected:
        if not cfg.enabled:
            raise ValueError(f"workstation tent is disabled: {cfg.slug}")
        if cfg.mode != "festzelt_os" or cfg.festzelt_os is None:
            raise ValueError(f"workstation tent has unsupported mode: {cfg.slug}")
        if tuple(cfg.dates) != WORKSTATION_TARGET_DATES:
            raise ValueError(f"workstation tent has unexpected dates: {cfg.slug}")
        expected_url = WORKSTATION_PORTAL_URLS[cfg.slug]
        if cfg.booking_url != expected_url:
            raise ValueError(f"workstation tent has unexpected booking URL: {cfg.slug}")
        if cfg.festzelt_os.url_template != expected_url:
            raise ValueError(f"workstation tent has unexpected probe URL: {cfg.slug}")
    return selected


def _needs_manual_action(result: ProbeResult) -> bool:
    diagnostics = result.diagnostics
    error_class = (diagnostics.error_class or "").casefold()
    return diagnostics.page_type == "bot" or any(
        marker in error_class
        for marker in ("bot", "captcha", "challenge", "http_403", "http_429")
    )


def classify_results(results: Iterable[ProbeResult]) -> str:
    items = list(results)
    if not items:
        return "inconclusive"
    if any(_needs_manual_action(item) for item in items):
        return "needs_manual_action"
    if all(
        item.status in {"available", "unavailable"}
        and item.diagnostics.health == "healthy"
        for item in items
    ):
        return "healthy"
    return "inconclusive"


def _serialize_result(result: ProbeResult) -> dict[str, object]:
    return {
        "status": result.status,
        "shifts": None if result.shifts is None else list(result.shifts),
        "diagnostics": result.diagnostic_dict(),
    }


def run_selected_probes(
    configs: Sequence[TentConfig],
    context,
    *,
    fetch: Callable[..., dict[str, ProbeResult]] = festzelt_os_fetcher.fetch_in_context,
    jitter: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    for cfg in configs:
        if jitter:
            sleep(random.uniform(1.0, 3.0))
        assert cfg.festzelt_os is not None
        try:
            fetched = fetch(cfg.festzelt_os, cfg.dates, context)
            if not isinstance(fetched, dict):
                raise TypeError("probe result is not a mapping")
            results = [fetched[date] for date in cfg.dates]
            if not all(isinstance(item, ProbeResult) for item in results):
                raise TypeError("probe result contains invalid entries")
            dates = {
                target: _serialize_result(fetched[target]) for target in cfg.dates
            }
            status = classify_results(results)
        except Exception as exc:
            # Never persist exception text: third-party libraries can include
            # URLs or page snippets.  The class is enough for local diagnosis.
            log.warning("%s failed with %s", cfg.slug, type(exc).__name__)
            status = "inconclusive"
            dates = {
                target: {
                    "status": "error",
                    "shifts": None,
                    "diagnostics": {
                        "health": "error",
                        "page_type": "unknown",
                        "error_class": "probe_exception",
                        "detail": type(exc).__name__,
                    },
                }
                for target in cfg.dates
            }
        reports.append(
            {
                "slug": cfg.slug,
                "name": cfg.name,
                "status": status,
                "dates": dates,
            }
        )
    return reports


def overall_status(tents: Sequence[dict[str, object]]) -> str:
    if len(tents) != len(WORKSTATION_TENT_SLUGS):
        return "inconclusive"
    if tuple(str(tent.get("slug")) for tent in tents) != WORKSTATION_TENT_SLUGS:
        return "inconclusive"
    if any(
        not isinstance(tent.get("dates"), dict)
        or tuple(tent["dates"].keys()) != WORKSTATION_TARGET_DATES
        for tent in tents
    ):
        return "inconclusive"
    statuses = {str(tent.get("status")) for tent in tents}
    if "needs_manual_action" in statuses:
        return "needs_manual_action"
    if statuses == {"healthy"}:
        return "healthy"
    return "inconclusive"


def exit_code_for_status(status: str) -> int:
    if status == "healthy":
        return EXIT_OK
    if status == "needs_manual_action":
        return EXIT_NEEDS_MANUAL_ACTION
    if status == "inconclusive":
        return EXIT_INCONCLUSIVE
    return EXIT_SETUP_ERROR


def _atomic_write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def save_report(report_dir: Path, report: dict[str, object], *, keep: int = 50) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    history_path = report_dir / f"probe-{stamp}.json"
    _atomic_write_json(history_path, report)
    _atomic_write_json(report_dir / "latest.json", report)
    historical = sorted(report_dir.glob("probe-*.json"), reverse=True)
    for obsolete in historical[max(1, keep) :]:
        obsolete.unlink(missing_ok=True)


class SingleInstance(AbstractContextManager["SingleInstance"]):
    """Advisory single-process lock that releases automatically after a crash."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def __enter__(self) -> "SingleInstance":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        if os.fstat(self._handle.fileno()).st_size == 0:
            self._handle.write(b"0")
            self._handle.flush()
        self._handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            self._handle.close()
            self._handle = None
            raise RuntimeError("another workstation probe is already running") from exc
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def _configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "workstation-probe.log",
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.handlers.clear()
    log.addHandler(handler)
    log.addHandler(logging.StreamHandler())
    log.setLevel(logging.INFO)


def _warm_up_profile(configs: Sequence[TentConfig], context) -> None:
    print(
        "Chrome-Profil-Ersteinrichtung: Lösen Sie eine angezeigte Challenge "
        "ausschließlich selbst. Keine Reservierung absenden."
    )
    for cfg in configs:
        assert cfg.festzelt_os is not None
        page = context.new_page()
        try:
            try:
                page.goto(
                    cfg.festzelt_os.url_template,
                    wait_until=cfg.festzelt_os.wait_until,
                    timeout=cfg.festzelt_os.navigation_timeout_ms,
                )
            except Exception:
                # Keep the visible page open; the user can still inspect it.
                pass
            input(
                f"{cfg.name}: Seite prüfen/Challenge ggf. manuell lösen, "
                "danach hier EINGABE drücken ... "
            )
        finally:
            try:
                page.close()
            except Exception:
                pass


def _setup_error_report(started_at: str, exc: BaseException) -> dict[str, object]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "overall_status": "setup_error",
        "error_class": type(exc).__name__,
        "tents": [],
    }


def launch_visible_context(playwright, profile_dir: Path):
    """Launch the supported, visible installed-Chrome persistent context."""

    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        channel="chrome",
        headless=False,
        locale="de-DE",
        viewport={"width": 1280, "height": 1100},
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Visible, read-only Chrome check for the three workstation tents"
    )
    parser.add_argument(
        "--warm-up",
        action="store_true",
        help="pause visibly on each page for legitimate manual challenge handling",
    )
    parser.add_argument("--dry-run", action="store_true", help="do not write reports")
    parser.add_argument("--no-jitter", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    started_monotonic = time.monotonic()
    started_at = _utc_now()
    try:
        data_dir, profile_dir = workstation_paths()
        configs = select_configs(load_tents(TENTS_DIR), WORKSTATION_TENT_SLUGS)
        _configure_logging(data_dir / "Logs")

        from playwright.sync_api import sync_playwright

        with SingleInstance(data_dir / "workstation-probe.lock"):
            playwright = sync_playwright().start()
            context = None
            try:
                # A dedicated Chrome profile is mandatory.  Do not point this
                # at the user's normal Chrome data directory.
                context = launch_visible_context(playwright, profile_dir)
                if args.warm_up:
                    _warm_up_profile(configs, context)
                tents = run_selected_probes(
                    configs,
                    context,
                    jitter=not args.no_jitter,
                )
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                playwright.stop()

        status = overall_status(tents)
        report: dict[str, object] = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "started_at": started_at,
            "finished_at": _utc_now(),
            "duration_seconds": round(
                max(0.0, time.monotonic() - started_monotonic), 3
            ),
            "overall_status": status,
            "browser_channel": "chrome",
            "headed": True,
            "production_state_modified": False,
            "notifications_sent": False,
            "tents": tents,
        }
        if not args.dry_run:
            save_report(data_dir / "Reports", report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return exit_code_for_status(status)
    except (KeyboardInterrupt, EOFError):
        report = _setup_error_report(started_at, KeyboardInterrupt())
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return EXIT_SETUP_ERROR
    except Exception as exc:
        # Deliberately avoid traceback/page data in unattended logs.
        report = _setup_error_report(started_at, exc)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return EXIT_SETUP_ERROR


if __name__ == "__main__":
    sys.exit(main())
