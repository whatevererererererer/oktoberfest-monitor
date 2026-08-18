from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from src.events import apply_probe_result, canonical_shift_key
from src.state import SCHEMA_VERSION, State, TentDateState, TentState, load, save


def cfg(slug: str = "test") -> SimpleNamespace:
    return SimpleNamespace(
        slug=slug,
        name="Testzelt",
        booking_url="https://example.com/book",
    )


def probe(
    status: str, shifts: list[str] | None = None, **diagnostics
) -> SimpleNamespace:
    """Generic/legacy result: the adapter, not Festzelt diagnostics, is trusted."""
    return SimpleNamespace(status=status, shifts=shifts or [], diagnostics=diagnostics)


class StructuredResult(SimpleNamespace):
    def diagnostic_dict(self) -> dict[str, object]:
        return dict(self.diagnostics)


def structured_probe(
    status: str,
    shifts: list[str] | None = None,
    **overrides: object,
) -> StructuredResult:
    labels = list(shifts or [])
    diagnostics: dict[str, object] = {
        "health": "healthy" if status in {"available", "unavailable"} else "degraded",
        "page_type": "booking",
        "date_control_count": 1,
        "plausible_date_option_count": 10,
        "target_found": status != "unavailable",
        "target_enabled": True if status == "available" else None,
        "shift_control_count": 1 if status == "available" else 0,
        "shift_control_found": status == "available",
        "update_confirmed": status == "available",
        "shift_count": len(labels),
        "error_class": None,
    }
    diagnostics.update(overrides)
    return StructuredResult(status=status, shifts=labels, diagnostics=diagnostics)


def verified_available(*shifts: str) -> TentDateState:
    labels = list(shifts)
    keys = [canonical_shift_key(label) for label in labels]
    return TentDateState(
        status="available",
        observed_status="available",
        health="healthy",
        shifts=labels,
        shift_keys=keys,
        baseline_verified=True,
        last_reliable_at="2026-08-04T07:00:00+00:00",
        last_reliable_diagnostics={"health": "healthy"},
    )


class StateMigrationTests(unittest.TestCase):
    def write_and_load(self, raw: object) -> State:
        path = Path(__file__).parent / f".state-events-{uuid4().hex}.json"
        try:
            path.write_text(json.dumps(raw), encoding="utf-8")
            return load(path)
        finally:
            path.unlink(missing_ok=True)

    def test_legacy_state_migrates_without_losing_historical_context(self) -> None:
        raw = {
            "workflow_last_run_at": "2026-08-01T00:00:00+00:00",
            "future_minor_metadata": {"keep": True},
            "tents": {
                "x": {
                    "consecutive_failures": 7,
                    "last_error": "old-error",
                    "historical_note": "keep-me",
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
        state = self.write_and_load(raw)
        self.assertEqual(state.schema_version, SCHEMA_VERSION)
        self.assertIsNone(state.probe_rotation_cursor)
        self.assertEqual(state.tents["x"].consecutive_failures, 7)
        self.assertEqual(state.tents["x"].model_extra["historical_note"], "keep-me")
        self.assertEqual(state.model_extra["future_minor_metadata"], {"keep": True})
        date_state = state.tents["x"].dates["2026-09-25"]
        self.assertEqual(date_state.status, "unknown")
        self.assertEqual(date_state.shifts, [])
        self.assertFalse(date_state.baseline_verified)
        self.assertEqual(date_state.last_change, "old-change")
        self.assertEqual(
            date_state.diagnostics["migration_previous_status"], "available"
        )
        self.assertEqual(
            date_state.diagnostics["migration_previous_shifts"], ["Mittag"]
        )

    def test_v2_structured_success_migrates_as_verified_baseline(self) -> None:
        raw = {
            "schema_version": 2,
            "tents": {
                "x": {
                    "dates": {
                        "2026-09-25": {
                            "status": "available",
                            "observed_status": "available",
                            "health": "healthy",
                            "shifts": ["Mittag"],
                            "shift_keys": ["mittag"],
                            "last_check": "2026-08-04T08:00:00+00:00",
                            "diagnostics": structured_probe(
                                "available", ["Mittag"]
                            ).diagnostics,
                        }
                    }
                }
            },
        }
        date_state = self.write_and_load(raw).tents["x"].dates["2026-09-25"]
        self.assertTrue(date_state.baseline_verified)
        self.assertEqual(date_state.status, "available")
        self.assertEqual(date_state.shifts, ["Mittag"])
        self.assertEqual(date_state.last_reliable_at, raw["tents"]["x"]["dates"]["2026-09-25"]["last_check"])
        self.assertEqual(date_state.last_reliable_diagnostics["health"], "healthy")

    def test_v2_error_after_canonical_shift_baseline_does_not_erase_it(self) -> None:
        raw = {
            "schema_version": 2,
            "tents": {
                "x": {
                    "dates": {
                        "2026-09-25": {
                            "status": "available",
                            "observed_status": "error",
                            "health": "error",
                            "shifts": ["Mittag"],
                            "shift_keys": ["mittag"],
                            "last_change": "2026-08-03T08:00:00+00:00",
                            "last_check": "2026-08-04T08:00:00+00:00",
                            "diagnostics": {
                                "health": "error",
                                "page_type": "bot",
                                "error_class": "bot_page",
                            },
                        }
                    }
                }
            },
        }
        date_state = self.write_and_load(raw).tents["x"].dates["2026-09-25"]
        self.assertTrue(date_state.baseline_verified)
        self.assertEqual((date_state.status, date_state.shifts), ("available", ["Mittag"]))
        self.assertEqual((date_state.observed_status, date_state.health), ("error", "error"))
        self.assertEqual(
            date_state.last_reliable_diagnostics["migration"],
            "schema_v2_canonical_shift_provenance",
        )

    def test_v2_empty_available_is_not_a_healthy_baseline(self) -> None:
        raw = {
            "schema_version": 2,
            "tents": {
                "x": {
                    "dates": {
                        "2026-09-25": {
                            "status": "available",
                            "observed_status": "available",
                            "health": "healthy",
                            "shifts": [],
                            "shift_keys": [],
                            "diagnostics": {"health": "healthy"},
                        }
                    }
                }
            },
        }
        date_state = self.write_and_load(raw).tents["x"].dates["2026-09-25"]
        self.assertEqual((date_state.status, date_state.observed_status), ("unknown", "unknown"))
        self.assertEqual(date_state.health, "degraded")
        self.assertFalse(date_state.baseline_verified)
        self.assertEqual(
            date_state.diagnostics["error_class"], "legacy_snapshot_unverified"
        )

    def test_v2_unverified_bot_baseline_becomes_unknown_but_error_survives(self) -> None:
        raw = {
            "schema_version": 2,
            "tents": {
                "x": {
                    "dates": {
                        "2026-09-25": {
                            "status": "unavailable",
                            "observed_status": "error",
                            "health": "error",
                            "shifts": [],
                            "diagnostics": {
                                "health": "error",
                                "page_type": "bot",
                                "error_class": "bot_page",
                            },
                        }
                    }
                }
            },
        }
        date_state = self.write_and_load(raw).tents["x"].dates["2026-09-25"]
        self.assertEqual(date_state.status, "unknown")
        self.assertEqual((date_state.observed_status, date_state.health), ("error", "error"))
        self.assertEqual(date_state.diagnostics["error_class"], "bot_page")
        self.assertEqual(
            date_state.diagnostics["migration_previous_status"], "unavailable"
        )

    def test_v2_open_error_incident_migrates_without_realerting(self) -> None:
        raw = {
            "schema_version": 2,
            "tents": {
                "bot": {
                    "consecutive_failures": 12,
                    "consecutive_degraded": 0,
                    "failure_incident_open": True,
                    "failure_incident_sequence": 1,
                    "dates": {},
                }
            },
        }
        tent = self.write_and_load(raw).tents["bot"]
        self.assertEqual(tent.consecutive_unhealthy, 12)
        self.assertEqual(tent.failure_incident_kind, "error")
        self.assertTrue(tent.failure_incident_open)

    def test_future_schema_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "newer than supported"):
            self.write_and_load({"schema_version": SCHEMA_VERSION + 1})

    def test_in_memory_available_baseline_requires_shifts_and_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires shifts and shift_keys"):
            TentDateState(status="available", shifts=[])

    def test_verified_baseline_requires_reliable_provenance(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires reliable provenance"):
            TentDateState(
                status="available",
                shifts=["Mittag"],
                shift_keys=["mittag"],
                baseline_verified=True,
            )

    def test_one_invalid_outbox_entry_is_quarantined_without_losing_state(self) -> None:
        raw = {
            "schema_version": 2,
            "tents": {"x": {"consecutive_failures": 4}},
            "outbox": {
                "broken": {
                    "event_id": "broken",
                    "kind": "not-a-kind",
                    "secretish_unknown_field": "must-not-be-copied",
                }
            },
        }
        state = self.write_and_load(raw)
        self.assertEqual(state.tents["x"].consecutive_failures, 4)
        self.assertEqual(len(state.outbox), 1)
        event = next(iter(state.outbox.values()))
        self.assertEqual(event.status, "dead_letter")
        self.assertEqual(event.last_error_class, "invalid_persisted_event")
        self.assertEqual(event.quarantined_payload["field_count"], 3)
        self.assertEqual(
            event.quarantined_payload["allowed_field_names_present"],
            ["event_id", "kind"],
        )
        serialised = json.dumps(event.model_dump(), ensure_ascii=False)
        self.assertNotIn("secretish_unknown_field", serialised)
        self.assertNotIn("must-not-be-copied", serialised)

    def test_outbox_map_key_mismatch_is_quarantined(self) -> None:
        raw = {
            "schema_version": 2,
            "outbox": {
                "map-key": {
                    "event_id": "payload-id",
                    "tent_slug": "x",
                    "tent_name": "X",
                    "created_at": "2026-08-04T08:00:00+00:00",
                }
            },
        }
        event = next(iter(self.write_and_load(raw).outbox.values()))
        self.assertEqual(event.status, "dead_letter")
        self.assertEqual(event.quarantined_payload["map_key_length"], len("map-key"))
        self.assertEqual(
            event.quarantined_payload["event_id_length"], len("payload-id")
        )
        self.assertFalse(event.quarantined_payload["event_id_key_matched"])
        self.assertNotIn("map-key", json.dumps(event.model_dump()))
        self.assertNotIn("payload-id", json.dumps(event.model_dump()))

    def test_quarantine_never_persists_untrusted_identifier_or_field_name(self) -> None:
        sentinel = "TOKEN_SENTINEL_DO_NOT_PERSIST_7f83c1"
        raw = {
            "schema_version": 2,
            "outbox": {
                f"map-{sentinel}": {
                    "event_id": f"event-{sentinel}",
                    "kind": "invalid-kind",
                    "tent_slug": sentinel,
                    "tent_name": sentinel,
                    f"unknown-{sentinel}": sentinel,
                }
            },
        }
        state = self.write_and_load(raw)
        serialised = json.dumps(state.model_dump(), ensure_ascii=False, sort_keys=True)
        self.assertNotIn(sentinel, serialised)
        self.assertEqual(len(state.outbox), 1)
        key, event = next(iter(state.outbox.items()))
        self.assertEqual(key, event.event_id)
        self.assertTrue(key.startswith("quarantine-"))
        self.assertEqual(event.quarantined_payload["field_count"], 5)
        self.assertEqual(
            event.quarantined_payload["allowed_field_names_present"],
            ["event_id", "kind", "tent_name", "tent_slug"],
        )

    def test_atomic_save_round_trip(self) -> None:
        path = Path(__file__).parent / f".state-events-{uuid4().hex}.json"
        try:
            state = State(
                workflow_last_run_at="2026-08-04T00:00:00+00:00",
                probe_rotation_cursor="tent-d",
            )
            save(path, state)
            self.assertEqual(load(path), state)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])
        finally:
            path.unlink(missing_ok=True)
            path.with_name(f".{path.name}.tmp").unlink(missing_ok=True)

    def test_save_revalidates_mutated_state_before_replacing_file(self) -> None:
        path = Path(__file__).parent / f".state-events-{uuid4().hex}.json"
        try:
            state = State()
            save(path, state)
            before = path.read_bytes()
            date_state = TentDateState()
            # Assignment validation is intentionally disabled while transitions
            # are assembled; the persistence boundary still fails closed.
            date_state.status = "available"
            tent_state = TentState()
            tent_state.dates["2026-09-25"] = date_state
            state.tents["broken"] = tent_state
            with self.assertRaises(ValueError):
                save(path, state)
            self.assertEqual(path.read_bytes(), before)
        finally:
            path.unlink(missing_ok=True)
            path.with_name(f".{path.name}.tmp").unlink(missing_ok=True)


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

    def test_unverified_legacy_empty_availability_to_shift_is_alerted(self) -> None:
        self.tent.dates[self.date] = TentDateState(
            status="unknown",
            observed_status="unknown",
            diagnostics={"migration_previous_status": "available", "migration_previous_shifts": []},
        )
        applied = self.apply(probe("available", ["Abend"]), "2026-08-04T08:00:00+00:00")
        self.assertEqual(applied.event.reason, "available")
        self.assertTrue(applied.event.burst)
        self.assertTrue(self.tent.dates[self.date].baseline_verified)

    def test_reorder_and_cosmetic_labels_do_not_duplicate(self) -> None:
        self.tent.dates[self.date] = verified_available("Mittag", "Nachmittag")
        applied = self.apply(
            probe("available", [" nachmittag 15:30 Uhr ", "MITTAG (11:00 Uhr)"]),
            "2026-08-04T08:00:00+00:00",
        )
        self.assertIsNone(applied.event)

    def test_removal_then_readdition_alerts_again(self) -> None:
        self.tent.dates[self.date] = verified_available("Mittag", "Abend")
        removed = self.apply(probe("available", ["Mittag"]), "2026-08-04T08:00:00+00:00")
        self.assertIsNone(removed.event)
        added = self.apply(probe("available", ["Mittag", "Abend"]), "2026-08-04T08:01:00+00:00")
        self.assertEqual(added.event.new_shifts, ["Abend"])

    def test_reavailability_gets_new_event_id(self) -> None:
        first = self.apply(probe("available", ["Mittag"]), "2026-08-04T08:00:00+00:00").event
        self.apply(probe("unavailable"), "2026-08-04T08:01:00+00:00")
        second = self.apply(probe("available", ["Mittag"]), "2026-08-04T08:02:00+00:00").event
        self.assertNotEqual(first.event_id, second.event_id)

    def test_unknown_and_error_preserve_reliable_baseline_and_provenance(self) -> None:
        self.tent.dates[self.date] = verified_available("Mittag")
        baseline_diagnostics = dict(
            self.tent.dates[self.date].last_reliable_diagnostics
        )
        unknown = self.apply(
            probe("unknown", error_class="date_options_unstable"),
            "2026-08-04T08:00:00+00:00",
        )
        date_state = self.tent.dates[self.date]
        self.assertEqual((date_state.status, date_state.shifts), ("available", ["Mittag"]))
        self.assertEqual((unknown.observed_status, unknown.health), ("unknown", "degraded"))
        self.assertEqual(date_state.last_reliable_diagnostics, baseline_diagnostics)
        error = self.apply(
            probe("error", error_class="bot_page"), "2026-08-04T08:01:00+00:00"
        )
        self.assertEqual((date_state.status, date_state.shifts), ("available", ["Mittag"]))
        self.assertEqual((error.observed_status, error.health), ("error", "error"))
        self.assertEqual(date_state.last_reliable_diagnostics, baseline_diagnostics)

    def test_available_without_shifts_is_defensively_degraded_with_error_class(self) -> None:
        applied = self.apply(probe("available", []), "2026-08-04T08:00:00+00:00")
        date_state = self.tent.dates[self.date]
        self.assertEqual((applied.observed_status, applied.health), ("unknown", "degraded"))
        self.assertEqual(date_state.diagnostics["health"], "degraded")
        self.assertEqual(
            date_state.diagnostics["error_class"], "available_without_shifts"
        )
        self.assertEqual(len(self.state.outbox), 0)

    def test_shift_evidence_loss_reconfirms_same_shift_exactly_once(self) -> None:
        self.tent.dates[self.date] = verified_available("Abend")
        degraded = self.apply(
            probe("unknown", error_class="shift_options_empty"),
            "2026-08-04T08:00:00+00:00",
        )
        self.assertIsNone(degraded.event)
        self.assertTrue(self.tent.dates[self.date].availability_evidence_lost)
        recovered = self.apply(
            probe("available", ["Abend"]), "2026-08-04T08:01:00+00:00"
        )
        self.assertEqual(recovered.event.reason, "availability_reconfirmed")
        self.assertTrue(recovered.event.burst)
        self.assertFalse(self.tent.dates[self.date].availability_evidence_lost)
        again = self.apply(
            probe("available", ["Abend"]), "2026-08-04T08:02:00+00:00"
        )
        self.assertIsNone(again.event)
        self.assertEqual(len(self.state.outbox), 1)

    def test_new_adapter_evidence_loss_reconfirms_after_recovery(self) -> None:
        for error_class in (
            "event_days_empty",
            "reservation_form_no_dates",
            "target_slots_incomplete",
        ):
            with self.subTest(error_class=error_class):
                self.state = State()
                self.tent = self.state.tents.setdefault("test", TentState())
                self.tent.dates[self.date] = verified_available("Mittag")
                degraded = self.apply(
                    probe("unknown", error_class=error_class),
                    "2026-08-04T08:00:00+00:00",
                )
                self.assertIsNone(degraded.event)
                self.assertTrue(
                    self.tent.dates[self.date].availability_evidence_lost
                )
                recovered = self.apply(
                    probe("available", ["Mittag"]),
                    "2026-08-04T08:01:00+00:00",
                )
                self.assertEqual(
                    recovered.event.reason, "availability_reconfirmed"
                )

    def test_non_shift_degradation_does_not_reconfirm_same_shift(self) -> None:
        self.tent.dates[self.date] = verified_available("Mittag")
        self.apply(
            probe("unknown", error_class="date_options_unstable"),
            "2026-08-04T08:00:00+00:00",
        )
        self.assertFalse(self.tent.dates[self.date].availability_evidence_lost)
        recovered = self.apply(
            probe("available", ["Mittag"]), "2026-08-04T08:01:00+00:00"
        )
        self.assertIsNone(recovered.event)

    def test_bot_or_error_recovery_with_same_shift_does_not_duplicate(self) -> None:
        self.tent.dates[self.date] = verified_available("Abend")
        self.apply(
            probe("error", error_class="bot_page"),
            "2026-08-04T08:00:00+00:00",
        )
        self.assertFalse(self.tent.dates[self.date].availability_evidence_lost)
        recovered = self.apply(
            probe("available", ["Abend"]), "2026-08-04T08:01:00+00:00"
        )
        self.assertIsNone(recovered.event)

    def test_structured_available_with_inconsistent_evidence_is_degraded(self) -> None:
        result = structured_probe(
            "available", ["Abend"], update_confirmed=False
        )
        applied = self.apply(result, "2026-08-04T08:00:00+00:00")
        date_state = self.tent.dates[self.date]
        self.assertEqual((applied.observed_status, applied.health), ("unknown", "degraded"))
        self.assertEqual(
            date_state.diagnostics["error_class"],
            "inconsistent_available_diagnostics",
        )
        self.assertEqual(self.state.outbox, {})

    def test_structured_unavailable_without_valid_date_control_is_degraded(self) -> None:
        result = structured_probe(
            "unavailable", [], date_control_count=0, plausible_date_option_count=0
        )
        applied = self.apply(result, "2026-08-04T08:00:00+00:00")
        self.assertEqual((applied.observed_status, applied.health), ("unknown", "degraded"))
        self.assertEqual(self.tent.dates[self.date].status, "unknown")

    def test_structured_confirmed_empty_feed_is_reliable_unavailable(self) -> None:
        result = structured_probe(
            "unavailable",
            [],
            plausible_date_option_count=0,
            unavailable_confirmed=True,
        )
        applied = self.apply(result, "2026-08-04T08:00:00+00:00")
        self.assertEqual(
            (applied.observed_status, applied.health),
            ("unavailable", "healthy"),
        )
        self.assertTrue(self.tent.dates[self.date].baseline_verified)

    def test_reliable_observation_updates_separate_provenance(self) -> None:
        diagnostics = structured_probe("available", ["Mittag"])
        self.apply(diagnostics, "2026-08-04T08:00:00+00:00")
        date_state = self.tent.dates[self.date]
        self.assertTrue(date_state.baseline_verified)
        self.assertEqual(date_state.last_reliable_at, "2026-08-04T08:00:00+00:00")
        self.assertEqual(date_state.last_reliable_diagnostics["shift_count"], 1)

    def test_last_change_tracks_shift_only_change(self) -> None:
        self.tent.dates[self.date] = verified_available("Mittag")
        self.tent.dates[self.date].last_change = "old"
        self.apply(probe("available", ["Mittag", "Abend"]), "2026-08-04T08:00:00+00:00")
        self.assertEqual(self.tent.dates[self.date].last_change, "2026-08-04T08:00:00+00:00")

    def test_shift_canonicalization(self) -> None:
        self.assertEqual(canonical_shift_key(" Mittag (11:00 Uhr) "), "mittag")
        self.assertEqual(canonical_shift_key("ABEND 18:30 Uhr"), "abend")
        self.assertEqual(canonical_shift_key("Mittag / Nachmittag"), "mittag+nachmittag")

    def test_combined_saturday_shift_is_not_suppressed_by_plain_mittag(self) -> None:
        self.tent.dates[self.date] = verified_available("Mittag")
        applied = self.apply(
            probe("available", ["Mittag", "Mittag / Nachmittag"]),
            "2026-08-04T08:00:00+00:00",
        )
        self.assertIsNotNone(applied.event)
        self.assertEqual(applied.event.new_shifts, ["Mittag / Nachmittag"])
        self.assertTrue(applied.event.burst)

    def test_event_id_is_stable_across_new_shift_order(self) -> None:
        first_state = State()
        first_tent = TentState(dates={self.date: verified_available("Mittag")})
        second_state = State()
        second_tent = TentState(dates={self.date: verified_available("Mittag")})
        first = apply_probe_result(
            state=first_state,
            cfg=cfg(),
            tent_state=first_tent,
            iso_date=self.date,
            result=probe("available", ["Mittag", "Abend", "Nachmittag"]),
            timestamp="2026-08-04T08:00:00+00:00",
        ).event
        second = apply_probe_result(
            state=second_state,
            cfg=cfg(),
            tent_state=second_tent,
            iso_date=self.date,
            result=probe("available", ["Nachmittag", "Mittag", "Abend"]),
            timestamp="2026-08-04T08:00:00+00:00",
        ).event
        self.assertEqual(first.event_id, second.event_id)


if __name__ == "__main__":
    unittest.main()
