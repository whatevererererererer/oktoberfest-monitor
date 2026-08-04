from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.events import apply_probe_result, canonical_shift_key
from src.state import State, TentDateState, TentState, load, save


def cfg(slug: str = "test") -> SimpleNamespace:
    return SimpleNamespace(
        slug=slug,
        name="Testzelt",
        booking_url="https://example.com/book",
    )


def probe(
    status: str, shifts: list[str] | None = None, **diagnostics
) -> SimpleNamespace:
    return SimpleNamespace(status=status, shifts=shifts or [], diagnostics=diagnostics)


class StateMigrationTests(unittest.TestCase):
    def test_legacy_state_migrates_without_losing_history(self) -> None:
        raw = {
            "workflow_last_run_at": "2026-08-01T00:00:00+00:00",
            "tents": {
                "x": {
                    "consecutive_failures": 7,
                    "last_error": "old-error",
                    "dates": {
                        "2026-09-25": {
                            "status": "available",
                            "shifts": ["Mittag"],
                            "last_change": "old-change",
                        }
                    },
                }
            },
        }
        with tempfile.TemporaryDirectory(dir=Path.cwd() / "work") as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            state = load(path)
        self.assertEqual(state.schema_version, 2)
        self.assertEqual(state.tents["x"].consecutive_failures, 7)
        date_state = state.tents["x"].dates["2026-09-25"]
        self.assertEqual(date_state.status, "available")
        self.assertEqual(date_state.shifts, ["Mittag"])
        self.assertEqual(date_state.last_change, "old-change")
        self.assertEqual(date_state.health, "unknown")

    def test_atomic_save_round_trip(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd() / "work") as directory:
            path = Path(directory) / "nested" / "state.json"
            state = State(workflow_last_run_at="2026-08-04T00:00:00+00:00")
            save(path, state)
            self.assertEqual(load(path), state)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


class EventTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = State()
        self.tent = self.state.tents.setdefault("test", TentState())
        self.date = "2026-09-26"

    def apply(self, result: SimpleNamespace, timestamp: str):
        return apply_probe_result(
            state=self.state,
            cfg=cfg(),
            tent_state=self.tent,
            iso_date=self.date,
            result=result,
            timestamp=timestamp,
        )

    def test_unavailable_to_available_enqueues_once(self) -> None:
        self.apply(probe("unavailable"), "2026-08-04T08:00:00+00:00")
        applied = self.apply(probe("available", ["Mittag"]), "2026-08-04T08:01:00+00:00")
        self.assertIsNotNone(applied.event)
        self.assertEqual(applied.event.total_messages, 1)
        again = self.apply(probe("available", ["Mittag"]), "2026-08-04T08:02:00+00:00")
        self.assertIsNone(again.event)

    def test_empty_legacy_available_to_shift_is_alerted(self) -> None:
        self.tent.dates[self.date] = TentDateState(status="available", shifts=[])
        applied = self.apply(probe("available", ["Abend"]), "2026-08-04T08:00:00+00:00")
        self.assertEqual(applied.event.reason, "shifts_added")
        self.assertTrue(applied.event.burst)

    def test_reorder_and_cosmetic_labels_do_not_duplicate(self) -> None:
        self.tent.dates[self.date] = TentDateState(
            status="available",
            shifts=["Mittag", "Nachmittag"],
            shift_keys=["mittag", "nachmittag"],
        )
        applied = self.apply(
            probe("available", [" nachmittag 15:30 Uhr ", "MITTAG (11:00 Uhr)"]),
            "2026-08-04T08:00:00+00:00",
        )
        self.assertIsNone(applied.event)

    def test_removal_then_readdition_alerts_again(self) -> None:
        self.tent.dates[self.date] = TentDateState(
            status="available", shifts=["Mittag", "Abend"], shift_keys=["mittag", "abend"]
        )
        removed = self.apply(probe("available", ["Mittag"]), "2026-08-04T08:00:00+00:00")
        self.assertIsNone(removed.event)
        added = self.apply(probe("available", ["Mittag", "Abend"]), "2026-08-04T08:01:00+00:00")
        self.assertEqual(added.event.new_shifts, ["Abend"])

    def test_reavailability_gets_new_event_id(self) -> None:
        first = self.apply(probe("available", ["Mittag"]), "2026-08-04T08:00:00+00:00").event
        self.apply(probe("unavailable"), "2026-08-04T08:01:00+00:00")
        second = self.apply(probe("available", ["Mittag"]), "2026-08-04T08:02:00+00:00").event
        self.assertNotEqual(first.event_id, second.event_id)

    def test_unknown_and_error_preserve_reliable_baseline(self) -> None:
        self.tent.dates[self.date] = TentDateState(
            status="available", shifts=["Mittag"], shift_keys=["mittag"]
        )
        unknown = self.apply(probe("unknown"), "2026-08-04T08:00:00+00:00")
        date_state = self.tent.dates[self.date]
        self.assertEqual((date_state.status, date_state.shifts), ("available", ["Mittag"]))
        self.assertEqual((unknown.observed_status, unknown.health), ("unknown", "degraded"))
        error = self.apply(probe("error"), "2026-08-04T08:01:00+00:00")
        self.assertEqual((date_state.status, date_state.shifts), ("available", ["Mittag"]))
        self.assertEqual((error.observed_status, error.health), ("error", "error"))

    def test_available_without_shifts_is_defensively_degraded(self) -> None:
        applied = self.apply(probe("available", []), "2026-08-04T08:00:00+00:00")
        self.assertEqual((applied.observed_status, applied.health), ("unknown", "degraded"))
        self.assertEqual(len(self.state.outbox), 0)

    def test_last_change_tracks_shift_only_change(self) -> None:
        self.tent.dates[self.date] = TentDateState(
            status="available", shifts=["Mittag"], shift_keys=["mittag"], last_change="old"
        )
        self.apply(probe("available", ["Mittag", "Abend"]), "2026-08-04T08:00:00+00:00")
        self.assertEqual(self.tent.dates[self.date].last_change, "2026-08-04T08:00:00+00:00")

    def test_shift_canonicalization(self) -> None:
        self.assertEqual(canonical_shift_key(" Mittag (11:00 Uhr) "), "mittag")
        self.assertEqual(canonical_shift_key("ABEND 18:30 Uhr"), "abend")
        self.assertEqual(
            canonical_shift_key("Mittag / Nachmittag"), "mittag+nachmittag"
        )

    def test_combined_saturday_shift_is_not_suppressed_by_plain_mittag(self) -> None:
        self.tent.dates[self.date] = TentDateState(
            status="available", shifts=["Mittag"], shift_keys=["mittag"]
        )
        applied = self.apply(
            probe("available", ["Mittag", "Mittag / Nachmittag"]),
            "2026-08-04T08:00:00+00:00",
        )
        self.assertIsNotNone(applied.event)
        self.assertEqual(applied.event.new_shifts, ["Mittag / Nachmittag"])
        self.assertTrue(applied.event.burst)


if __name__ == "__main__":
    unittest.main()
