from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

import src.main as main_module
from src.events import AppliedProbe, apply_probe_result
from src.main import (
    _error_probe,
    _legacy_probe,
    _record_tent_health,
    _select_probe_batch,
    probe_run,
)
from src.probe import ProbeDiagnostics, ProbeResult
from src.state import State, TentDateState, TentState, load, save

ROOT = Path(__file__).resolve().parents[1]


def config(slug: str) -> SimpleNamespace:
    return SimpleNamespace(
        slug=slug,
        name=f"Zelt {slug}",
        booking_url=f"https://example.com/{slug}",
        enabled=True,
        mode="festzelt_os",
        dates=["2026-09-25", "2026-09-26"],
        festzelt_os=SimpleNamespace(),
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


def degraded() -> ProbeResult:
    return ProbeResult(
        "unknown",
        diagnostics=ProbeDiagnostics(
            health="degraded", page_type="booking", error_class="shift_options_empty"
        ),
    )


def error() -> ProbeResult:
    return ProbeResult(
        "error",
        diagnostics=ProbeDiagnostics(
            health="error", page_type="bot", error_class="bot_page"
        ),
    )


def applied(result: ProbeResult) -> AppliedProbe:
    return AppliedProbe(None, result.diagnostics.health, result.status)


class MainIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        descriptor, name = tempfile.mkstemp(
            prefix=".test-main-state-", suffix=".json", dir=ROOT
        )
        os.close(descriptor)
        self.path = Path(name)
        self.path.unlink()
        self.browser = Mock()
        self.playwright = Mock()

    def tearDown(self) -> None:
        self.path.unlink(missing_ok=True)
        self.path.with_name(f".{self.path.name}.tmp").unlink(missing_ok=True)

    def initial_state(self, slugs: list[str]) -> None:
        state = State()
        for slug in slugs:
            state.tents[slug] = TentState(
                dates={
                    date: TentDateState(status="unavailable", observed_status="unavailable")
                    for date in ("2026-09-25", "2026-09-26")
                }
            )
        save(self.path, state)

    def test_legacy_unavailable_without_date_control_evidence_is_degraded(self) -> None:
        result = _legacy_probe("unavailable", source="html")
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.diagnostics.health, "degraded")
        self.assertEqual(
            result.diagnostics.error_class, "date_and_shift_evidence_unavailable"
        )

    def test_structured_non_festzelt_modes_create_actionable_events(self) -> None:
        configs = []
        for slug, mode in (
            ("bartls", "floesserstadl"),
            ("kaefer", "kaefer"),
            ("knoedelei", "reservierungsmanager"),
        ):
            configs.append(
                SimpleNamespace(
                    slug=slug,
                    name=f"Zelt {slug}",
                    booking_url=f"https://example.com/{slug}",
                    enabled=True,
                    mode=mode,
                    dates=["2026-09-26"],
                    **{mode: SimpleNamespace()},
                )
            )
        self.initial_state([cfg.slug for cfg in configs])

        with self.assertLogs("wiesn", level="INFO") as captured:
            with (
                patch("src.main.load_tents", return_value=configs),
                patch(
                    "src.main.headless_fetcher.launch_browser",
                    return_value=(self.playwright, self.browser),
                ),
                patch(
                    "src.main.floesserstadl_fetcher.fetch",
                    return_value=available("Mittag (11:00–16:30)"),
                ) as floesserstadl,
                patch(
                    "src.main.kaefer_fetcher.fetch",
                    return_value=available("Mittag (11:30–15:00)"),
                ) as kaefer,
                patch(
                    "src.main.reservierungsmanager_fetcher.fetch",
                    return_value=available("Mittag (11:00–14:00)"),
                ) as reservierungsmanager,
            ):
                self.assertEqual(probe_run(state_path=self.path, jitter=False), 0)

        state = load(self.path)
        self.assertEqual(len(state.outbox), 3)
        self.assertEqual(
            {event.tent_slug for event in state.outbox.values()},
            {cfg.slug for cfg in configs},
        )
        self.assertTrue(all(not event.burst for event in state.outbox.values()))
        floesserstadl.assert_called_once()
        kaefer.assert_called_once()
        reservierungsmanager.assert_called_once()
        self.browser.close.assert_called_once_with()
        self.playwright.stop.assert_called_once_with()
        joined_logs = "\n".join(captured.output)
        for slug in ("bartls", "kaefer", "knoedelei"):
            self.assertIn(
                f"{slug}/2026-09-26 observed=available health=healthy",
                joined_logs,
            )

    def test_http_adapter_wallclock_uses_an_isolated_daemon_client(self) -> None:
        release = threading.Event()
        finished = threading.Event()
        captured_clients = []

        def stuck_fetch(config, iso_date, client):
            captured_clients.append(client)
            try:
                release.wait(1)
                return available("Mittag")
            finally:
                finished.set()

        cfg = SimpleNamespace(
            mode="floesserstadl",
            floesserstadl=SimpleNamespace(),
        )
        shared_client = Mock()
        isolated_client = MagicMock()
        client_context = MagicMock()
        client_context.__enter__.return_value = isolated_client
        client_context.__exit__.return_value = False
        started = time.monotonic()
        try:
            with (
                patch(
                    "src.main.FLOESSER_HTTP_WALL_SECONDS",
                    0.05,
                ),
                patch(
                    "src.main.floesserstadl_fetcher.fetch",
                    side_effect=stuck_fetch,
                ),
                patch("src.main.httpx.Client", return_value=client_context),
            ):
                with self.assertRaisesRegex(TimeoutError, "wall-clock"):
                    main_module._check_one(
                        cfg,
                        "2026-09-26",
                        shared_client,
                        None,
                    )
        finally:
            release.set()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(finished.wait(1))
        self.assertEqual(len(captured_clients), 1)
        self.assertIsNot(captured_clients[0], shared_client)
        self.assertIs(captured_clients[0], isolated_client)
        client_context.__exit__.assert_called_once()

    def test_new_adapter_failure_codes_have_operator_facing_explanations(self) -> None:
        expected = {
            "event_days_schema_invalid": "Reservierungsdaten des Widgets",
            "event_days_empty": "keine auswertbaren Termine",
            "event_days_no_matching_tickets": "keine eindeutig passenden Zelt-Termine",
            "slot_schema_invalid": "Käfer-Slotdaten",
            "target_slots_incomplete": "Zieldatum waren unvollständig",
            "reservation_form_schema_invalid": "Reservierungsformular",
            "reservation_form_no_dates": "keine plausiblen Wiesn-Termine",
        }
        for error_class, fragment in expected.items():
            with self.subTest(error_class=error_class):
                reason = main_module._probe_failure_reason(
                    {"page_type": "booking", "error_class": error_class}
                )
                self.assertIn(fragment, reason)
                self.assertIn(f"Code: {error_class}", reason)

    def test_missing_rotation_cursor_safely_restarts_at_first_enabled_tent(self) -> None:
        configs = [config(slug) for slug in ("a", "b", "c", "d")]
        selected, cursor = _select_probe_batch(configs, "removed-tent")
        self.assertEqual([cfg.slug for cfg in selected], ["a", "b", "c"])
        self.assertEqual(cursor, "d")
        self.assertEqual(_select_probe_batch([], "removed-tent"), ([], None))

    def test_duplicate_enabled_slugs_fail_closed_before_rotation(self) -> None:
        configs = [config(slug) for slug in ("a", "b", "c", "a", "d")]
        with self.assertRaisesRegex(ValueError, "enabled tent slugs must be unique"):
            _select_probe_batch(configs, None)

    def test_probe_enqueues_multiple_tents_without_sending(self) -> None:
        configs = [config("a"), config("b")]
        self.initial_state(["a", "b"])

        def fetch(_cfg, dates, _browser):
            return {date: available("Mittag") for date in dates}

        with (
            patch("src.main.load_tents", return_value=configs),
            patch("src.main.headless_fetcher.launch_browser", return_value=(self.playwright, self.browser)),
            patch("src.main.festzelt_os_fetcher.fetch", side_effect=fetch),
            patch("src.main.time.sleep"),
            patch("src.notify.send_event_part") as send,
            patch.dict(
                os.environ,
                {"MONITOR_PRODUCER_REVISION": "abc123"},
                clear=False,
            ),
        ):
            self.assertEqual(probe_run(state_path=self.path, jitter=False), 0)

        self.assertFalse(send.called)
        state = load(self.path)
        self.assertEqual(len(state.outbox), 4)
        self.assertTrue(all(event.status == "pending" for event in state.outbox.values()))
        self.assertEqual(state.producer_revision, "abc123")
        self.assertIsNotNone(state.workflow_started_at)
        self.assertIsNotNone(state.workflow_finished_at)
        self.assertIsNotNone(state.workflow_duration_seconds)

    def test_festzelt_probes_are_sequential_in_config_order(self) -> None:
        configs = [config(chr(ord("a") + index)) for index in range(6)]
        selected = configs[:3]
        self.initial_state([cfg.slug for cfg in configs])
        skipped_before = {
            cfg.slug: load(self.path).tents[cfg.slug].model_dump()
            for cfg in configs[3:]
        }
        main_thread = threading.get_ident()
        resources: list[tuple[object, object]] = []
        resource_errors: list[str] = []
        fetch_threads: list[int] = []
        fetch_order: list[str] = []
        apply_threads: list[int] = []
        apply_order: list[tuple[str, str]] = []
        active = 0
        max_active = 0

        class Browser:
            def __init__(self) -> None:
                self.creator = threading.get_ident()
                self.closed = False

            def close(self) -> None:
                if threading.get_ident() != self.creator:
                    resource_errors.append("browser closed from another thread")
                self.closed = True

        class Playwright:
            def __init__(self, creator: int) -> None:
                self.creator = creator
                self.stopped = False

            def stop(self) -> None:
                if threading.get_ident() != self.creator:
                    resource_errors.append("playwright stopped from another thread")
                self.stopped = True

        def launch_browser():
            browser = Browser()
            playwright = Playwright(browser.creator)
            resources.append((playwright, browser))
            return playwright, browser

        def fetch(cfg, dates, browser):
            nonlocal active, max_active
            if threading.get_ident() != browser.creator:
                resource_errors.append("browser used from another thread")
            active += 1
            max_active = max(max_active, active)
            fetch_threads.append(threading.get_ident())
            fetch_order.append(
                next(item.slug for item in configs if item.festzelt_os is cfg)
            )
            try:
                return {date: available("Mittag") for date in dates}
            finally:
                active -= 1

        real_apply = main_module._apply_observation

        def record_apply(**kwargs):
            apply_threads.append(threading.get_ident())
            apply_order.append((kwargs["cfg"].slug, kwargs["iso_date"]))
            return real_apply(**kwargs)

        with (
            patch("src.main.load_tents", return_value=configs),
            patch("src.main.headless_fetcher.launch_browser", side_effect=launch_browser),
            patch("src.main.festzelt_os_fetcher.fetch", side_effect=fetch),
            patch("src.main._apply_observation", side_effect=record_apply),
            patch("src.main.random.uniform", return_value=2.0) as uniform,
            patch("src.main.time.sleep") as sleep,
        ):
            self.assertEqual(probe_run(state_path=self.path, jitter=True), 0)

        self.assertEqual(max_active, 1)
        self.assertEqual(len(resources), 1)
        self.assertEqual(fetch_threads, [main_thread] * len(selected))
        self.assertEqual(fetch_order, [cfg.slug for cfg in selected])
        self.assertEqual(
            uniform.call_args_list,
            [call(1.0, 3.0)] * len(selected),
        )
        self.assertEqual(
            sleep.call_args_list,
            [call(2.0)] * len(selected),
        )
        self.assertEqual(resources[0][1].creator, main_thread)
        self.assertTrue(all(playwright.stopped for playwright, _ in resources))
        self.assertTrue(all(browser.closed for _, browser in resources))
        self.assertEqual(resource_errors, [])
        self.assertEqual(set(apply_threads), {main_thread})
        self.assertEqual(
            apply_order,
            [(cfg.slug, iso_date) for cfg in selected for iso_date in cfg.dates],
        )
        state = load(self.path)
        self.assertEqual(state.probe_rotation_cursor, "d")
        self.assertTrue(
            all(
                date_state.observed_status == "available"
                for cfg in selected
                for date_state in state.tents[cfg.slug].dates.values()
            )
        )
        self.assertEqual(
            {
                cfg.slug: state.tents[cfg.slug].model_dump()
                for cfg in configs[3:]
            },
            skipped_before,
        )

    def test_durable_rotation_covers_eleven_tents_without_touching_skipped_state(self) -> None:
        configs = [config(chr(ord("a") + index)) for index in range(11)]
        self.initial_state([cfg.slug for cfg in configs])
        initial = load(self.path)
        skipped_before = initial.tents["d"].model_dump()
        fetch_order: list[str] = []

        def fetch(cfg, dates, _browser):
            fetch_order.append(
                next(item.slug for item in configs if item.festzelt_os is cfg)
            )
            return {date: available("Mittag") for date in dates}

        expected_batches = [
            (["a", "b", "c"], "d"),
            (["d", "e", "f"], "g"),
            (["g", "h", "i"], "j"),
            (["j", "k"], "a"),
        ]
        with (
            patch("src.main.load_tents", return_value=configs),
            patch(
                "src.main.headless_fetcher.launch_browser",
                return_value=(self.playwright, self.browser),
            ),
            patch("src.main.festzelt_os_fetcher.fetch", side_effect=fetch),
        ):
            for expected_slugs, expected_cursor in expected_batches:
                offset = len(fetch_order)
                self.assertEqual(probe_run(state_path=self.path, jitter=False), 0)
                self.assertEqual(fetch_order[offset:], expected_slugs)
                self.assertEqual(
                    load(self.path).probe_rotation_cursor, expected_cursor
                )
                if expected_slugs == ["a", "b", "c"]:
                    self.assertEqual(
                        load(self.path).tents["d"].model_dump(), skipped_before
                    )

        self.assertEqual(fetch_order, [cfg.slug for cfg in configs])
        state = load(self.path)
        self.assertTrue(
            all(
                date_state.observed_status == "available"
                for tent in state.tents.values()
                for date_state in tent.dates.values()
            )
        )

    def test_unpersisted_batch_retries_same_tents_after_restart(self) -> None:
        configs = [config(chr(ord("a") + index)) for index in range(5)]
        self.initial_state([cfg.slug for cfg in configs])
        fetch_order: list[str] = []

        def fetch(cfg, dates, _browser):
            fetch_order.append(
                next(item.slug for item in configs if item.festzelt_os is cfg)
            )
            return {date: available("Mittag") for date in dates}

        with (
            patch("src.main.load_tents", return_value=configs),
            patch(
                "src.main.headless_fetcher.launch_browser",
                return_value=(self.playwright, self.browser),
            ),
            patch("src.main.festzelt_os_fetcher.fetch", side_effect=fetch),
            patch("src.main.save", side_effect=OSError("checkpoint unavailable")),
        ):
            with self.assertRaisesRegex(OSError, "checkpoint unavailable"):
                probe_run(state_path=self.path, jitter=False)

        self.assertEqual(fetch_order, ["a", "b", "c"])
        self.assertIsNone(load(self.path).probe_rotation_cursor)

        with (
            patch("src.main.load_tents", return_value=configs),
            patch(
                "src.main.headless_fetcher.launch_browser",
                return_value=(self.playwright, self.browser),
            ),
            patch("src.main.festzelt_os_fetcher.fetch", side_effect=fetch),
        ):
            self.assertEqual(probe_run(state_path=self.path, jitter=False), 0)

        self.assertEqual(fetch_order, ["a", "b", "c", "a", "b", "c"])
        self.assertEqual(load(self.path).probe_rotation_cursor, "d")

    def test_sequential_fetch_errors_are_isolated_and_resources_are_closed(self) -> None:
        configs = [config("ok"), config("raised"), config("invalid")]
        self.initial_state([cfg.slug for cfg in configs])
        resources: list[tuple[Mock, Mock]] = []

        def launch_browser():
            playwright = Mock()
            browser = Mock()
            resources.append((playwright, browser))
            return playwright, browser

        def fetch(cfg, dates, _browser):
            if cfg is configs[1].festzelt_os:
                raise RuntimeError("synthetic failure")
            if cfg is configs[2].festzelt_os:
                return []
            return {date: available("Mittag") for date in dates}

        with (
            patch("src.main.load_tents", return_value=configs),
            patch("src.main.headless_fetcher.launch_browser", side_effect=launch_browser),
            patch("src.main.festzelt_os_fetcher.fetch", side_effect=fetch),
        ):
            self.assertEqual(probe_run(state_path=self.path, jitter=False), 0)

        self.assertEqual(len(resources), 1)
        for playwright, browser in resources:
            browser.close.assert_called_once_with()
            playwright.stop.assert_called_once_with()
        state = load(self.path)
        self.assertEqual(
            state.tents["ok"].dates[configs[0].dates[0]].observed_status,
            "available",
        )
        for slug in ("raised", "invalid"):
            for date_state in state.tents[slug].dates.values():
                self.assertEqual(date_state.observed_status, "error")
                self.assertEqual(
                    date_state.diagnostics["error_class"], "probe_exception"
                )

    def test_worker_launch_failure_is_recorded_without_state_thread_failure(self) -> None:
        cfg = config("launch-error")
        self.initial_state([cfg.slug])
        with (
            patch("src.main.load_tents", return_value=[cfg]),
            patch(
                "src.main.headless_fetcher.launch_browser",
                side_effect=OSError("synthetic launch failure"),
            ),
            patch("src.main.festzelt_os_fetcher.fetch") as fetch,
        ):
            self.assertEqual(probe_run(state_path=self.path, jitter=False), 0)

        fetch.assert_not_called()
        for date_state in load(self.path).tents[cfg.slug].dates.values():
            self.assertEqual(date_state.observed_status, "error")
            self.assertEqual(
                date_state.diagnostics["error_class"], "browser_unavailable"
            )
            self.assertEqual(date_state.diagnostics["detail"], "OSError")

    def test_legacy_headless_mode_keeps_main_thread_browser_lifecycle(self) -> None:
        cfg = SimpleNamespace(
            slug="headless",
            name="Headless",
            booking_url="https://example.com/headless",
            enabled=True,
            mode="headless",
            dates=["2026-09-25"],
            headless=SimpleNamespace(),
        )
        self.initial_state([cfg.slug])
        main_thread = threading.get_ident()
        fetch_threads: list[int] = []

        def fetch(_cfg, _date, browser):
            self.assertIs(browser, self.browser)
            fetch_threads.append(threading.get_ident())
            return "unavailable"

        with (
            patch("src.main.load_tents", return_value=[cfg]),
            patch(
                "src.main.headless_fetcher.launch_browser",
                return_value=(self.playwright, self.browser),
            ) as launch,
            patch("src.main.headless_fetcher.fetch", side_effect=fetch),
        ):
            self.assertEqual(probe_run(state_path=self.path, jitter=False), 0)

        launch.assert_called_once_with()
        self.browser.close.assert_called_once_with()
        self.playwright.stop.assert_called_once_with()
        self.assertEqual(fetch_threads, [main_thread])
        date_state = load(self.path).tents[cfg.slug].dates[cfg.dates[0]]
        self.assertEqual(date_state.observed_status, "unknown")
        self.assertEqual(
            date_state.diagnostics["error_class"],
            "date_and_shift_evidence_unavailable",
        )

    def test_dry_run_does_not_modify_state_file(self) -> None:
        cfg = config("a")
        self.initial_state(["a"])
        before = self.path.read_bytes()
        with (
            patch("src.main.load_tents", return_value=[cfg]),
            patch("src.main.headless_fetcher.launch_browser", return_value=(self.playwright, self.browser)),
            patch(
                "src.main.festzelt_os_fetcher.fetch",
                return_value={date: available("Abend") for date in cfg.dates},
            ),
            patch("src.main.time.sleep"),
        ):
            self.assertEqual(probe_run(dry_run=True, state_path=self.path, jitter=False), 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_degraded_error_and_recovery_counters(self) -> None:
        state = State()
        cfg = config("a")
        tent = state.tents.setdefault("a", TentState())
        degraded_diagnostics = degraded().diagnostics.model_dump(mode="json")
        tent.dates = {
            iso_date: TentDateState(diagnostics=dict(degraded_diagnostics))
            for iso_date in cfg.dates
        }
        for index in range(3):
            _record_tent_health(
                state=state,
                cfg=cfg,
                tent_state=tent,
                results=[applied(degraded()), applied(degraded())],
                timestamp=f"2026-08-04T08:0{index}:00+00:00",
            )
        self.assertEqual(tent.consecutive_degraded, 3)
        self.assertTrue(tent.failure_incident_open)
        self.assertEqual(len(state.outbox), 1)
        self.assertEqual(
            next(iter(state.outbox.values())).booking_url,
            cfg.booking_url,
        )
        self.assertEqual(
            next(iter(state.outbox.values())).reason,
            "Zelt a: Prüfung unklar seit 3 Läufen\n"
            "Grund 2026-09-25: Schichtauswahl war leer oder nicht lesbar "
            "(Code: shift_options_empty)\n"
            "Grund 2026-09-26: Schichtauswahl war leer oder nicht lesbar "
            "(Code: shift_options_empty)",
        )

        _record_tent_health(
            state=state,
            cfg=cfg,
            tent_state=tent,
            results=[applied(available("Mittag")), applied(available("Mittag"))],
            timestamp="2026-08-04T08:04:00+00:00",
        )
        self.assertFalse(tent.failure_incident_open)
        self.assertEqual((tent.consecutive_degraded, tent.consecutive_failures), (0, 0))

        _record_tent_health(
            state=state,
            cfg=cfg,
            tent_state=tent,
            results=[applied(error()), applied(error())],
            timestamp="2026-08-04T08:05:00+00:00",
        )
        self.assertEqual(tent.consecutive_failures, 1)

    def test_monitor_error_explains_bot_challenge_without_leaking_detail(self) -> None:
        state = State()
        cfg = config("a")
        cfg.dates = ["2026-09-26"]
        tent = state.tents.setdefault("a", TentState())
        challenge = ProbeResult(
            "error",
            diagnostics=ProbeDiagnostics(
                health="error",
                page_type="bot",
                error_class="shift_update_challenge",
                detail="must-not-appear",
            ),
        )
        tent.dates["2026-09-26"] = TentDateState(
            diagnostics=challenge.diagnostics.model_dump(mode="json")
        )

        for index in range(3):
            _record_tent_health(
                state=state,
                cfg=cfg,
                tent_state=tent,
                results=[applied(challenge)],
                timestamp=f"2026-08-07T01:2{index}:00+00:00",
            )

        event = next(iter(state.outbox.values()))
        self.assertEqual(event.booking_url, cfg.booking_url)
        self.assertEqual(
            event.reason,
            "Zelt a: Fehler seit 3 Läufen\n"
            "Grund 2026-09-26: "
            "Bot-Schutz/Challenge beim Laden der Schichtauswahl "
            "(Code: shift_update_challenge)",
        )
        self.assertNotIn("must-not-appear", event.reason)

    def test_monitor_error_omits_unsafe_diagnostic_code(self) -> None:
        state = State()
        cfg = config("a")
        cfg.dates = ["2026-09-26"]
        tent = state.tents.setdefault("a", TentState())
        malformed = AppliedProbe(None, "error", "error")
        tent.dates["2026-09-26"] = TentDateState(
            diagnostics={
                "page_type": "bot",
                "error_class": "unsafe\nsecret=value",
            }
        )

        for index in range(3):
            _record_tent_health(
                state=state,
                cfg=cfg,
                tent_state=tent,
                results=[malformed],
                timestamp=f"2026-08-07T02:2{index}:00+00:00",
            )

        reason = next(iter(state.outbox.values())).reason
        self.assertIn("Bot-Schutz/Challenge auf der Buchungsseite", reason)
        self.assertNotIn("unsafe", reason)
        self.assertNotIn("secret", reason)

    def test_monitor_error_explains_safe_http_class(self) -> None:
        state = State()
        cfg = config("a")
        cfg.dates = ["2026-09-26"]
        tent = state.tents.setdefault("a", TentState())
        blocked = AppliedProbe(None, "error", "error")
        tent.dates["2026-09-26"] = TentDateState(
            diagnostics={
                "page_type": "bot",
                "error_class": "shift_update_http_403",
            }
        )

        for index in range(3):
            _record_tent_health(
                state=state,
                cfg=cfg,
                tent_state=tent,
                results=[blocked],
                timestamp=f"2026-08-07T03:2{index}:00+00:00",
            )

        reason = next(iter(state.outbox.values())).reason
        self.assertIn("Schicht-Update wurde abgelehnt (HTTP 403)", reason)
        self.assertIn("Code: shift_update_http_403", reason)

    def test_monitor_error_uses_diagnostics_from_latest_applied_probe(self) -> None:
        state = State()
        cfg = config("a")
        cfg.dates = ["2026-09-26"]
        tent = state.tents.setdefault("a", TentState())
        observations = [
            _error_probe("browser_unavailable"),
            _error_probe("browser_unavailable"),
            ProbeResult(
                "error",
                diagnostics=ProbeDiagnostics(
                    health="error",
                    page_type="bot",
                    error_class="shift_update_challenge",
                ),
            ),
        ]

        for index, observation in enumerate(observations):
            applied_observation = apply_probe_result(
                state=state,
                cfg=cfg,
                tent_state=tent,
                iso_date="2026-09-26",
                result=observation,
                timestamp=f"2026-08-07T04:2{index}:00+00:00",
            )
            _record_tent_health(
                state=state,
                cfg=cfg,
                tent_state=tent,
                results=[applied_observation],
                timestamp=f"2026-08-07T04:2{index}:00+00:00",
            )

        reason = next(iter(state.outbox.values())).reason
        self.assertIn("shift_update_challenge", reason)
        self.assertNotIn("browser_unavailable", reason)

    def test_monitor_error_message_is_bounded_at_complete_reason_lines(self) -> None:
        state = State()
        cfg = config("a")
        cfg.dates = [f"2026-09-{day:02d}-{index:02d}" for index, day in enumerate(range(1, 31))]
        tent = state.tents.setdefault("a", TentState())
        degraded_result = applied(degraded())
        for iso_date in cfg.dates:
            tent.dates[iso_date] = TentDateState(
                diagnostics=degraded().diagnostics.model_dump(mode="json")
            )

        for index in range(3):
            _record_tent_health(
                state=state,
                cfg=cfg,
                tent_state=tent,
                results=[degraded_result for _ in cfg.dates],
                timestamp=f"2026-08-07T05:2{index}:00+00:00",
            )

        reason = next(iter(state.outbox.values())).reason
        self.assertLessEqual(len(reason), 1024)
        self.assertTrue(
            reason.endswith("Weitere Fehlergründe wurden aus Platzgründen ausgelassen.")
        )
        self.assertNotIn("Grund 2026-09-30-29", reason)

    def test_alternating_degraded_and_error_share_unhealthy_streak(self) -> None:
        state = State()
        cfg = config("a")
        tent = state.tents.setdefault("a", TentState())
        observations = [
            [applied(degraded()), applied(degraded())],
            [applied(error()), applied(error())],
            [applied(degraded()), applied(degraded())],
        ]
        for index, results in enumerate(observations):
            _record_tent_health(
                state=state,
                cfg=cfg,
                tent_state=tent,
                results=results,
                timestamp=f"2026-08-04T08:0{index}:00+00:00",
            )

        self.assertEqual(tent.consecutive_unhealthy, 3)
        self.assertTrue(tent.failure_incident_open)
        self.assertEqual(len(state.outbox), 1)

    def test_defensive_available_without_shifts_is_degraded_at_tent_level(self) -> None:
        cfg = config("a")
        self.initial_state(["a"])
        invalid = SimpleNamespace(
            status="available",
            shifts=[],
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
                shift_count=0,
            ),
        )
        with (
            patch("src.main.load_tents", return_value=[cfg]),
            patch(
                "src.main.headless_fetcher.launch_browser",
                return_value=(self.playwright, self.browser),
            ),
            patch(
                "src.main.festzelt_os_fetcher.fetch",
                return_value={date: invalid for date in cfg.dates},
            ),
            patch("src.main.time.sleep"),
        ):
            self.assertEqual(probe_run(state_path=self.path, jitter=False), 0)

        state = load(self.path)
        tent = state.tents["a"]
        self.assertEqual(tent.consecutive_degraded, 1)
        self.assertEqual(tent.consecutive_unhealthy, 1)
        self.assertIsNone(tent.last_success_at)
        for date_state in tent.dates.values():
            self.assertEqual(date_state.observed_status, "unknown")
            self.assertEqual(date_state.health, "degraded")
            self.assertEqual(
                date_state.diagnostics["error_class"], "available_without_shifts"
            )

    def test_one_malformed_date_result_does_not_abort_remaining_dates(self) -> None:
        cfg = config("a")
        self.initial_state(["a"])
        batch = {
            cfg.dates[0]: SimpleNamespace(shifts=["Mittag"]),
            cfg.dates[1]: available("Mittag"),
        }
        with (
            patch("src.main.load_tents", return_value=[cfg]),
            patch(
                "src.main.headless_fetcher.launch_browser",
                return_value=(self.playwright, self.browser),
            ),
            patch("src.main.festzelt_os_fetcher.fetch", return_value=batch),
            patch("src.main.time.sleep"),
        ):
            self.assertEqual(probe_run(state_path=self.path, jitter=False), 0)

        tent = load(self.path).tents["a"]
        self.assertEqual(tent.dates[cfg.dates[0]].observed_status, "error")
        self.assertEqual(tent.dates[cfg.dates[1]].observed_status, "available")
        self.assertEqual(tent.dates[cfg.dates[1]].shifts, ["Mittag"])

    def test_hash_mode_reuses_diagnostic_baseline(self) -> None:
        cfg = SimpleNamespace(
            slug="hash-tent",
            name="Hash Tent",
            booking_url="https://example.com/hash",
            enabled=True,
            mode="hash",
            dates=["2026-09-25"],
            hash=SimpleNamespace(),
        )
        state = State(
            tents={
                cfg.slug: TentState(
                    dates={
                        cfg.dates[0]: TentDateState(
                            diagnostics={"content_hash": "old-hash"}
                        )
                    }
                )
            }
        )
        save(self.path, state)

        with (
            patch("src.main.load_tents", return_value=[cfg]),
            patch("src.main.hash_fetcher.fetch_hash", return_value="new-hash"),
            patch("src.main.time.sleep"),
        ):
            self.assertEqual(probe_run(state_path=self.path, jitter=False), 0)

        date_state = load(self.path).tents[cfg.slug].dates[cfg.dates[0]]
        self.assertEqual(date_state.diagnostics["content_hash"], "new-hash")
        self.assertEqual(date_state.diagnostics["detail"], "hash:available")


if __name__ == "__main__":
    unittest.main()
