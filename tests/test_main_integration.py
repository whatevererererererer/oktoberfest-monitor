from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.main import _record_tent_health, probe_run
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


class MainIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "work")
        self.path = Path(self.temp.name) / "state.json"
        self.browser = Mock()
        self.playwright = Mock()

    def tearDown(self) -> None:
        self.temp.cleanup()

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
        ):
            self.assertEqual(probe_run(state_path=self.path, jitter=False), 0)

        self.assertFalse(send.called)
        state = load(self.path)
        self.assertEqual(len(state.outbox), 4)
        self.assertTrue(all(event.status == "pending" for event in state.outbox.values()))

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
        for index in range(3):
            _record_tent_health(
                state=state,
                cfg=cfg,
                tent_state=tent,
                results=[degraded(), degraded()],
                timestamp=f"2026-08-04T08:0{index}:00+00:00",
            )
        self.assertEqual(tent.consecutive_degraded, 3)
        self.assertTrue(tent.failure_incident_open)
        self.assertEqual(len(state.outbox), 1)

        _record_tent_health(
            state=state,
            cfg=cfg,
            tent_state=tent,
            results=[available("Mittag"), available("Mittag")],
            timestamp="2026-08-04T08:04:00+00:00",
        )
        self.assertFalse(tent.failure_incident_open)
        self.assertEqual((tent.consecutive_degraded, tent.consecutive_failures), (0, 0))

        _record_tent_health(
            state=state,
            cfg=cfg,
            tent_state=tent,
            results=[error(), error()],
            timestamp="2026-08-04T08:05:00+00:00",
        )
        self.assertEqual(tent.consecutive_failures, 1)

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
