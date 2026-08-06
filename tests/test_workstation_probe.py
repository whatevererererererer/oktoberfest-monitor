from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from src.probe import ProbeDiagnostics, ProbeResult
import src.workstation_probe as workstation_probe
from src.workstation_probe import (
    EXIT_INCONCLUSIVE,
    EXIT_NEEDS_MANUAL_ACTION,
    EXIT_OK,
    ROOT,
    SingleInstance,
    WORKSTATION_PORTAL_URLS,
    WORKSTATION_TARGET_DATES,
    WORKSTATION_TENT_SLUGS,
    classify_results,
    exit_code_for_status,
    launch_visible_context,
    overall_status,
    run_selected_probes,
    save_report,
    select_configs,
    validate_external_data_path,
    workstation_paths,
)


def available(*shifts: str) -> ProbeResult:
    return ProbeResult(
        "available",
        shifts=tuple(shifts),
        diagnostics=ProbeDiagnostics(
            health="healthy",
            page_type="booking",
            date_control_count=1,
            plausible_date_option_count=10,
            target_found=True,
            target_enabled=True,
            shift_control_count=1,
            shift_control_found=True,
            update_confirmed=True,
            shift_count=len(shifts),
        ),
    )


def unavailable() -> ProbeResult:
    return ProbeResult(
        "unavailable",
        shifts=(),
        diagnostics=ProbeDiagnostics(
            health="healthy",
            page_type="booking",
            date_control_count=1,
            plausible_date_option_count=10,
            target_found=False,
        ),
    )


def bot(error_class: str = "navigation_http_403") -> ProbeResult:
    return ProbeResult(
        "error",
        diagnostics=ProbeDiagnostics(
            health="error", page_type="bot", error_class=error_class
        ),
    )


def degraded() -> ProbeResult:
    return ProbeResult(
        "unknown",
        diagnostics=ProbeDiagnostics(
            health="degraded",
            page_type="booking",
            error_class="shift_options_empty",
        ),
    )


def config(
    slug: str,
    *,
    enabled: bool = True,
    mode: str = "festzelt_os",
    dates: tuple[str, ...] = WORKSTATION_TARGET_DATES,
    booking_url: str | None = None,
    probe_url: str | None = None,
):
    expected_url = WORKSTATION_PORTAL_URLS.get(slug, "https://unexpected.example")
    return SimpleNamespace(
        slug=slug,
        name=slug,
        enabled=enabled,
        mode=mode,
        dates=list(dates),
        booking_url=booking_url or expected_url,
        festzelt_os=(
            SimpleNamespace(url_template=probe_url or expected_url)
            if mode == "festzelt_os"
            else None
        ),
    )


def all_configs():
    return [config(slug) for slug in WORKSTATION_TENT_SLUGS]


def tent_report(slug: str, status: str = "healthy") -> dict[str, object]:
    return {
        "slug": slug,
        "status": status,
        "dates": {date: {} for date in WORKSTATION_TARGET_DATES},
    }


class WorkstationSelectionTests(unittest.TestCase):
    def test_default_allowlist_can_be_selected_in_requested_order(self) -> None:
        configs = [config("poschner"), config("fischer-vroni"), config("paulaner")]
        selected = select_configs(
            configs, ["fischer-vroni", "paulaner", "poschner"]
        )
        self.assertEqual(
            [item.slug for item in selected],
            ["fischer-vroni", "paulaner", "poschner"],
        )

    def test_partial_reordered_or_unknown_selection_fails_closed(self) -> None:
        configs = all_configs()
        with self.assertRaisesRegex(ValueError, "requires exactly"):
            select_configs(configs, ["fischer-vroni"])
        with self.assertRaisesRegex(ValueError, "requires exactly"):
            select_configs(configs, list(reversed(WORKSTATION_TENT_SLUGS)))
        with self.assertRaisesRegex(ValueError, "requires exactly"):
            select_configs(configs, ["fischer-vroni", "paulaner", "loewenbraeu"])

    def test_duplicate_missing_disabled_and_wrong_mode_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            select_configs(
                [config("fischer-vroni"), config("paulaner"), config("paulaner")],
                WORKSTATION_TENT_SLUGS,
            )

        configs = all_configs()
        configs[1].enabled = False
        with self.assertRaisesRegex(ValueError, "disabled"):
            select_configs(configs, WORKSTATION_TENT_SLUGS)

        configs = all_configs()
        configs[2].mode = "manual"
        configs[2].festzelt_os = None
        with self.assertRaisesRegex(ValueError, "unsupported mode"):
            select_configs(configs, WORKSTATION_TENT_SLUGS)

    def test_saturday_date_and_both_urls_are_fixed(self) -> None:
        configs = all_configs()
        configs[0].dates = ["2026-09-25", "2026-09-26"]
        with self.assertRaisesRegex(ValueError, "unexpected dates"):
            select_configs(configs, WORKSTATION_TENT_SLUGS)

        configs = all_configs()
        configs[1].booking_url = "https://example.invalid"
        with self.assertRaisesRegex(ValueError, "unexpected booking URL"):
            select_configs(configs, WORKSTATION_TENT_SLUGS)

        configs = all_configs()
        configs[2].festzelt_os.url_template = "https://example.invalid"
        with self.assertRaisesRegex(ValueError, "unexpected probe URL"):
            select_configs(configs, WORKSTATION_TENT_SLUGS)

    def test_browser_profile_and_data_must_stay_outside_repository(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside"):
            validate_external_data_path(ROOT / "profile", label="profile")
        external = ROOT.parent / "wiesn-profile-test"
        self.assertEqual(
            validate_external_data_path(external, label="profile"), external.resolve()
        )

    def test_workstation_paths_are_the_single_localappdata_profile_pair(self) -> None:
        local_app_data = ROOT.parent / "wiesn-localappdata-test"
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}):
            data_dir, profile_dir = workstation_paths()
        self.assertEqual(data_dir, (local_app_data / "WiesnMonitor").resolve())
        self.assertEqual(profile_dir, (data_dir / "ChromeProfile").resolve())


class WorkstationClassificationTests(unittest.TestCase):
    def test_conclusive_results_are_healthy(self) -> None:
        self.assertEqual(
            classify_results([available("Mittag"), unavailable()]), "healthy"
        )
        self.assertEqual(exit_code_for_status("healthy"), EXIT_OK)

    def test_bot_or_challenge_needs_manual_action(self) -> None:
        self.assertEqual(
            classify_results([available("Mittag"), bot()]),
            "needs_manual_action",
        )
        self.assertEqual(
            exit_code_for_status("needs_manual_action"),
            EXIT_NEEDS_MANUAL_ACTION,
        )

    def test_degraded_or_empty_result_is_inconclusive(self) -> None:
        self.assertEqual(classify_results([degraded()]), "inconclusive")
        self.assertEqual(classify_results([]), "inconclusive")
        self.assertEqual(exit_code_for_status("inconclusive"), EXIT_INCONCLUSIVE)

    def test_overall_status_prioritizes_manual_action(self) -> None:
        reports = [tent_report(slug) for slug in WORKSTATION_TENT_SLUGS]
        reports[1]["status"] = "needs_manual_action"
        self.assertEqual(
            overall_status(reports),
            "needs_manual_action",
        )

    def test_overall_status_requires_all_three_tents_and_exact_saturday_date(self) -> None:
        reports = [tent_report(slug) for slug in WORKSTATION_TENT_SLUGS]
        self.assertEqual(overall_status(reports), "healthy")
        self.assertEqual(overall_status(reports[:-1]), "inconclusive")
        reports[0]["dates"] = {}
        self.assertEqual(overall_status(reports), "inconclusive")


class WorkstationRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = ROOT / f".test-workstation-{uuid.uuid4().hex}"
        self.temporary.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.temporary, ignore_errors=True)

    def test_only_requested_tents_and_saturday_are_reported(self) -> None:
        configs = [config("fischer-vroni"), config("paulaner")]
        calls: list[tuple[object, list[str], object]] = []
        sleeps: list[float] = []

        def fetch(cfg, dates, context):
            calls.append((cfg, dates, context))
            return {date: available("Mittag") for date in dates}

        reports = run_selected_probes(
            configs,
            "context",
            fetch=fetch,
            jitter=True,
            sleep=sleeps.append,
        )

        self.assertEqual([item["slug"] for item in reports], ["fischer-vroni", "paulaner"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call_[1] == configs[0].dates for call_ in calls))
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(1.0 <= delay <= 3.0 for delay in sleeps))
        self.assertTrue(all(item["status"] == "healthy" for item in reports))
        self.assertEqual(set(reports[0]["dates"]), {"2026-09-26"})

    def test_probe_exception_is_sanitized_and_remaining_tent_continues(self) -> None:
        configs = [config("fischer-vroni"), config("paulaner")]
        count = 0

        def fetch(_cfg, dates, _context):
            nonlocal count
            count += 1
            if count == 1:
                raise RuntimeError("secret page content must not be retained")
            return {date: unavailable() for date in dates}

        reports = run_selected_probes(
            configs, object(), fetch=fetch, jitter=False
        )
        first = json.dumps(reports[0])
        self.assertNotIn("secret page content", first)
        self.assertIn("RuntimeError", first)
        self.assertEqual(reports[0]["status"], "inconclusive")
        self.assertIsNone(reports[0]["dates"]["2026-09-26"]["shifts"])
        self.assertEqual(reports[1]["status"], "healthy")

    def test_report_history_is_bounded_and_latest_is_atomic_json(self) -> None:
        report_dir = self.temporary / "reports"
        for index in range(4):
            save_report(
                report_dir,
                {"overall_status": "healthy", "index": index},
                keep=2,
            )
            # Timestamps have one-second resolution; make the historical
            # filename unique without slowing the suite.
            historical = report_dir / f"probe-manual-{index}.json"
            historical.write_text("{}\n", encoding="utf-8")
        save_report(
            report_dir,
            {"overall_status": "healthy", "index": 4},
            keep=2,
        )
        latest = json.loads((report_dir / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(latest["index"], 4)
        self.assertLessEqual(len(list(report_dir.glob("probe-*.json"))), 2)
        self.assertFalse((report_dir / ".latest.json.tmp").exists())

    def test_single_instance_lock_rejects_overlap(self) -> None:
        lock_path = self.temporary / "probe.lock"
        with SingleInstance(lock_path):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                with SingleInstance(lock_path):
                    self.fail("overlapping lock unexpectedly acquired")


class WorkstationMainTests(unittest.TestCase):
    def test_visible_context_is_installed_chrome_without_identity_override(self) -> None:
        playwright = Mock()
        context = launch_visible_context(playwright, ROOT.parent / "profile")
        self.assertIs(
            context,
            playwright.chromium.launch_persistent_context.return_value,
        )
        kwargs = playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual(kwargs["channel"], "chrome")
        self.assertFalse(kwargs["headless"])
        self.assertNotIn("user_agent", kwargs)

    def test_main_runs_exact_contract_closes_resources_and_preserves_state(self) -> None:
        fake_sync_api = ModuleType("playwright.sync_api")
        sync_playwright = Mock()
        fake_sync_api.sync_playwright = sync_playwright
        starter = sync_playwright.return_value
        playwright = starter.start.return_value
        context = Mock()
        reports = [tent_report(slug) for slug in WORKSTATION_TENT_SLUGS]
        state_path = ROOT / "state" / "state.json"
        before = state_path.read_bytes()

        with (
            patch.dict(sys.modules, {"playwright.sync_api": fake_sync_api}),
            patch.object(
                workstation_probe,
                "workstation_paths",
                return_value=(ROOT.parent / "data", ROOT.parent / "profile"),
            ),
            patch.object(workstation_probe, "load_tents", return_value=all_configs()),
            patch.object(workstation_probe, "_configure_logging"),
            patch.object(
                workstation_probe,
                "SingleInstance",
                return_value=nullcontext(),
            ),
            patch.object(
                workstation_probe,
                "launch_visible_context",
                return_value=context,
            ) as launch,
            patch.object(
                workstation_probe,
                "run_selected_probes",
                return_value=reports,
            ) as run,
            patch.object(workstation_probe, "save_report") as save,
        ):
            code = workstation_probe.main(["--dry-run", "--no-jitter"])

        self.assertEqual(code, EXIT_OK)
        launch.assert_called_once_with(playwright, ROOT.parent / "profile")
        self.assertEqual(
            [cfg.slug for cfg in run.call_args.args[0]],
            list(WORKSTATION_TENT_SLUGS),
        )
        self.assertFalse(run.call_args.kwargs["jitter"])
        save.assert_not_called()
        context.close.assert_called_once_with()
        playwright.stop.assert_called_once_with()
        self.assertEqual(state_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
