from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from src.notify import (
    BURST_DELAYS,
    MAX_RATE_LIMIT_DEFERRALS_PER_PART,
    DeliveryResult,
    PushoverDeliveryError,
)
from src.outbox import deliver_next, requeue_dead_letter
from src.state import OutboxEvent, State, load, save


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value = datetime.fromtimestamp(
            self.value.timestamp() + seconds, timezone.utc
        )

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


def event(event_id: str = "event", *, burst: bool = True) -> OutboxEvent:
    shift = "Abend" if burst else "Mittag"
    return OutboxEvent(
        event_id=event_id,
        tent_slug="test",
        tent_name="Testzelt",
        iso_date="2026-09-26",
        booking_url="https://example.com/book",
        reason="available",
        shifts=[shift],
        new_shifts=[shift],
        burst=burst,
        total_messages=8 if burst else 1,
        created_at="2026-08-04T08:00:00+00:00",
        next_attempt_at="2026-08-04T08:00:00+00:00",
    )


def error_event(event_id: str = "error") -> OutboxEvent:
    return OutboxEvent(
        event_id=event_id,
        kind="monitor_error",
        tent_slug="test",
        tent_name="Testzelt",
        booking_url="https://example.com/book",
        reason="Probe fehlgeschlagen",
        created_at="2026-08-04T08:00:00+00:00",
        next_attempt_at="2026-08-04T08:00:00+00:00",
    )


class OutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        # A unique file directly in the repository root works in the managed
        # Windows sandbox and in a clean checkout. Newly created subdirectories
        # can inherit restrictive ACLs in that sandbox.
        descriptor, raw_path = tempfile.mkstemp(suffix=".json", dir=Path.cwd())
        os.close(descriptor)
        self.path = Path(raw_path)
        self.path.unlink()
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.path.unlink(missing_ok=True)
        self.path.with_name(f".{self.path.name}.tmp").unlink(missing_ok=True)

    def write(self, *events: OutboxEvent) -> None:
        save(self.path, State(outbox={item.event_id: item for item in events}))

    @staticmethod
    def quarantined(state: State) -> OutboxEvent:
        matches = [
            item for item in state.outbox.values()
            if item.last_error_class == "poison_event"
        ]
        if len(matches) != 1:
            raise AssertionError(f"expected one quarantined event, got {len(matches)}")
        return matches[0]

    @staticmethod
    def success(item: OutboxEvent, **kwargs) -> DeliveryResult:
        return DeliveryResult(f"request-{item.next_index}", 10000, 9999, 1800000000)

    def deliver(self, sender=None, *, max_wait_seconds: float = 120):
        return deliver_next(
            self.path,
            max_wait_seconds=max_wait_seconds,
            sender=sender or self.success,
            now_fn=self.clock.now,
            sleep_fn=self.clock.sleep,
        )

    def test_exact_burst_gap_sequence_and_progress(self) -> None:
        self.write(event())
        outcomes = [self.deliver() for _ in range(8)]
        self.assertEqual(self.clock.sleeps, list(BURST_DELAYS))
        self.assertEqual([item.part_index for item in outcomes], list(range(8)))
        stored = load(self.path).outbox["event"]
        self.assertEqual((stored.status, stored.next_index), ("delivered", 8))

    def test_burst_deadlines_start_when_sender_returns(self) -> None:
        self.write(event())
        started: list[datetime] = []
        completed: list[datetime] = []

        def slow_sender(item: OutboxEvent, *, now: datetime):
            started.append(now)
            self.clock.advance(7)
            completed.append(self.clock.now())
            return self.success(item)

        for _ in range(8):
            self.deliver(slow_sender)

        self.assertEqual(self.clock.sleeps, list(BURST_DELAYS))
        actual_gaps = [
            int((started[index] - completed[index - 1]).total_seconds())
            for index in range(1, 8)
        ]
        self.assertEqual(actual_gaps, list(BURST_DELAYS))

    def test_subsecond_acknowledgement_never_shortens_burst_gap(self) -> None:
        self.clock.value = datetime(
            2026, 8, 4, 8, 0, 0, 900000, tzinfo=timezone.utc
        )
        item = event()
        item.created_at = self.clock.now().isoformat()
        item.next_attempt_at = item.created_at
        self.write(item)

        self.deliver()
        stored = load(self.path).outbox["event"]
        self.assertEqual(
            stored.next_attempt_at,
            "2026-08-04T08:00:05.900000+00:00",
        )
        self.deliver()
        self.assertEqual(self.clock.sleeps, [5])

    def test_partial_burst_resumes_at_failed_part(self) -> None:
        self.write(event())
        for _ in range(3):
            self.deliver()
        calls: list[int] = []

        def flaky(item: OutboxEvent, **kwargs):
            calls.append(item.next_index)
            if len(calls) == 1:
                raise PushoverDeliveryError("http_503", failure_class="retryable")
            return self.success(item)

        failed = self.deliver(flaky)
        self.assertEqual(
            (failed.status, load(self.path).outbox["event"].next_index),
            ("retry_scheduled", 3),
        )
        resumed = self.deliver(flaky)
        self.assertEqual(
            (resumed.part_index, load(self.path).outbox["event"].next_index),
            (3, 4),
        )
        self.assertEqual(calls, [3, 3])

    def test_ack_before_local_checkpoint_can_repeat_only_current_part(self) -> None:
        self.write(event())
        for _ in range(3):
            self.deliver()
        calls: list[int] = []

        def accepted(item: OutboxEvent, **kwargs):
            calls.append(item.next_index)
            return self.success(item)

        with patch("src.outbox.save", side_effect=OSError("checkpoint failed")):
            with self.assertRaises(OSError):
                self.deliver(accepted)
        self.assertEqual(load(self.path).outbox["event"].next_index, 3)
        self.deliver(accepted)
        self.assertEqual(calls, [3, 3])
        self.assertEqual(load(self.path).outbox["event"].next_index, 4)

    def test_terminal_4xx_dead_letters_without_retry(self) -> None:
        self.write(event(burst=False))

        def reject(item: OutboxEvent, **kwargs):
            raise PushoverDeliveryError(
                "http_400", failure_class="terminal", status_code=400
            )

        outcome = self.deliver(reject)
        self.assertTrue(outcome.fatal)
        stored = load(self.path).outbox["event"]
        self.assertEqual(stored.status, "dead_letter")
        self.assertEqual(stored.attempts_by_index, {"0": 1})

    def test_429_is_deferred_without_consuming_normal_attempt(self) -> None:
        self.write(event(burst=False))
        reset = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)

        def limited(item: OutboxEvent, **kwargs):
            raise PushoverDeliveryError(
                "http_429", failure_class="rate_limited", retry_at=reset
            )

        self.assertEqual(self.deliver(limited).status, "rate_limited")
        stored = load(self.path).outbox["event"]
        self.assertEqual(stored.attempts_by_index, {})
        self.assertEqual(stored.rate_limit_deferrals_by_index, {"0": 1})
        deferred = self.deliver(max_wait_seconds=35)
        self.assertEqual(deferred.status, "deferred")
        self.assertGreater(deferred.wait_seconds, 35)

    def test_success_after_429_has_one_normal_attempt(self) -> None:
        self.write(event(burst=False))
        calls = 0

        def once_limited(item: OutboxEvent, *, now: datetime):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PushoverDeliveryError(
                    "http_429",
                    failure_class="rate_limited",
                    retry_at=now + timedelta(seconds=5),
                )
            return self.success(item)

        self.assertEqual(self.deliver(once_limited).status, "rate_limited")
        self.assertEqual(self.deliver(once_limited).status, "delivered")
        stored = load(self.path).outbox["event"]
        self.assertEqual(stored.attempts_by_index, {"0": 1})
        self.assertEqual(stored.rate_limit_deferrals_by_index, {"0": 1})

    def test_429_deferrals_are_bounded_without_blocking_other_events(self) -> None:
        first = event("a", burst=False)
        self.write(first)

        def always_limited(item: OutboxEvent, *, now: datetime):
            raise PushoverDeliveryError(
                "http_429",
                failure_class="rate_limited",
                retry_at=now + timedelta(seconds=5),
            )

        outcomes = [
            self.deliver(always_limited)
            for _ in range(MAX_RATE_LIMIT_DEFERRALS_PER_PART)
        ]
        self.assertEqual(outcomes[-1].status, "dead_letter")
        self.assertFalse(outcomes[-1].fatal)
        stored = load(self.path).outbox
        self.assertEqual(stored["a"].attempts_by_index, {})
        self.assertEqual(
            stored["a"].rate_limit_deferrals_by_index["0"],
            MAX_RATE_LIMIT_DEFERRALS_PER_PART,
        )
        # A quarantined rate-limited event is skipped on subsequent calls.
        state = load(self.path)
        state.outbox["b"] = error_event("b")
        save(self.path, state)
        self.assertEqual(self.deliver().event_id, "b")

    def test_rate_limit_minimum_is_relative_to_sender_return(self) -> None:
        self.write(event(burst=False))

        def delayed_429(item: OutboxEvent, *, now: datetime):
            self.clock.advance(20)
            raise PushoverDeliveryError(
                "http_429",
                failure_class="rate_limited",
                retry_at=now + timedelta(seconds=1),
            )

        self.deliver(delayed_429)
        stored = load(self.path).outbox["event"]
        self.assertEqual(
            stored.next_attempt_at,
            "2026-08-04T08:00:25+00:00",
        )

    def test_relative_retry_after_starts_at_failure_and_positive_quota_does_not_gate(self) -> None:
        self.write(event("a", burst=False), event("b", burst=False))

        def delayed_429(item: OutboxEvent, *, now: datetime):
            if item.event_id == "a":
                self.clock.advance(20)
                raise PushoverDeliveryError(
                    "http_429",
                    failure_class="rate_limited",
                    retry_at=now + timedelta(seconds=30),
                    retry_after_seconds=30,
                    quota_remaining=5,
                    quota_reset=int((now + timedelta(minutes=2)).timestamp()),
                )
            return self.success(item)

        self.assertEqual(self.deliver(delayed_429).status, "rate_limited")
        stored = load(self.path)
        self.assertEqual(
            stored.outbox["a"].next_attempt_at,
            "2026-08-04T08:00:50+00:00",
        )
        self.assertEqual(stored.pushover_quota["availability"].remaining, 5)
        self.assertEqual(self.deliver(delayed_429).event_id, "b")

    def test_huge_429_reset_is_replaced_by_safe_relative_deferral(self) -> None:
        self.write(event(burst=False))

        def invalid_reset(item: OutboxEvent, **kwargs):
            raise PushoverDeliveryError(
                "http_429",
                failure_class="rate_limited",
                retry_after_seconds=5,
                quota_remaining=0,
                quota_reset=10**100,
            )

        outcome = self.deliver(invalid_reset)
        self.assertEqual(outcome.status, "rate_limited")
        stored = load(self.path)
        self.assertEqual(
            stored.outbox["event"].next_attempt_at,
            "2026-08-04T08:00:05+00:00",
        )
        self.assertEqual(
            stored.pushover_quota["availability"].reset,
            int((self.clock.now() + timedelta(seconds=5)).timestamp()),
        )

    def test_retryable_failure_is_bounded(self) -> None:
        self.write(event(burst=False))

        def timeout(item: OutboxEvent, **kwargs):
            raise PushoverDeliveryError("timeout", failure_class="retryable")

        outcomes = [self.deliver(timeout) for _ in range(5)]
        self.assertEqual(outcomes[-1].status, "dead_letter")
        self.assertTrue(outcomes[-1].fatal)
        self.assertEqual(load(self.path).outbox["event"].attempts_by_index["0"], 5)

    def test_due_burst_continuation_precedes_unstarted_event(self) -> None:
        self.write(event("a"), event("b"), event("c", burst=False))
        order: list[tuple[str, int]] = []

        def slow_sender(item: OutboxEvent, **kwargs):
            order.append((item.event_id, item.next_index))
            self.clock.advance(7)
            return self.success(item)

        self.deliver(slow_sender)  # a[0], a[1] due at t=12
        self.deliver(slow_sender)  # b[0], finishes at t=14
        self.deliver(slow_sender)  # a[1] is due; c[0] must not jump ahead
        self.assertEqual(order, [("a", 0), ("b", 0), ("a", 1)])

    def test_multiple_bursts_and_normal_event_make_bounded_progress(self) -> None:
        self.write(event("a"), event("b"), event("c", burst=False))
        starts: dict[str, list[datetime]] = {"a": [], "b": [], "c": []}
        completions: dict[str, list[datetime]] = {"a": [], "b": [], "c": []}

        def slow_sender(item: OutboxEvent, *, now: datetime):
            starts[item.event_id].append(now)
            self.clock.advance(7)
            completions[item.event_id].append(self.clock.now())
            return self.success(item)

        for _ in range(17):
            self.deliver(slow_sender)

        stored = load(self.path).outbox
        self.assertEqual(
            {key: value.status for key, value in stored.items()},
            {"a": "delivered", "b": "delivered", "c": "delivered"},
        )
        for event_id in ("a", "b"):
            actual = [
                (starts[event_id][index] - completions[event_id][index - 1]).total_seconds()
                for index in range(1, 8)
            ]
            self.assertEqual(len(actual), len(BURST_DELAYS))
            self.assertTrue(
                all(gap >= required for gap, required in zip(actual, BURST_DELAYS))
            )

    def test_quota_gate_is_channel_separated(self) -> None:
        self.write(event("a"), event("b", burst=False), error_event("c"))

        def exhaust_availability(item: OutboxEvent, **kwargs):
            if item.kind == "availability":
                reset = int((self.clock.now() + timedelta(minutes=2)).timestamp())
                return DeliveryResult("request", 10000, 0, reset)
            return self.success(item)

        self.assertEqual(self.deliver(exhaust_availability).event_id, "a")
        # b uses the exhausted availability token; c uses the independent error token.
        self.assertEqual(self.deliver(exhaust_availability).event_id, "c")
        deferred = self.deliver(exhaust_availability, max_wait_seconds=35)
        self.assertEqual((deferred.status, deferred.event_id), ("deferred", "a"))

    def test_structurally_invalid_event_is_quarantined_before_send(self) -> None:
        poison = event("a", burst=False)
        good = event("b", burst=False)
        state = State(outbox={"a": poison, "b": good})
        # Mutate only after Pydantic validation to exercise the delivery guard.
        state.outbox["a"].next_index = 9
        sender = Mock(side_effect=self.success)

        with patch("src.outbox.load", return_value=state):
            outcome = self.deliver(sender)

        self.assertEqual(outcome.status, "quarantined")
        self.assertFalse(outcome.fatal)
        sender.assert_not_called()
        stored = load(self.path).outbox
        quarantined = self.quarantined(State(outbox=stored))
        self.assertEqual(quarantined.status, "dead_letter")
        self.assertEqual(self.deliver().event_id, "b")

    def test_policy_inconsistent_availability_events_are_quarantined(self) -> None:
        disabled_friday = event("disabled-friday", burst=False)
        disabled_friday.iso_date = "2026-09-25"

        saturday_evening_single = event("evening-single", burst=False)
        saturday_evening_single.shifts = ["Abend"]
        saturday_evening_single.new_shifts = ["Abend"]

        saturday_noon_burst = event("noon-burst")
        saturday_noon_burst.shifts = ["Mittag"]
        saturday_noon_burst.new_shifts = ["Mittag"]

        nonexistent_new_shift = event("nonexistent")
        nonexistent_new_shift.new_shifts = ["Nachmittag"]

        empty_new_shifts = event("empty-new-shifts")
        empty_new_shifts.new_shifts = []

        unknown_reason = event("unknown-reason")
        unknown_reason.reason = "made_up_reason"

        cases = [
            (disabled_friday, "target_date_disabled"),
            (saturday_evening_single, "notification_policy_mismatch"),
            (saturday_noon_burst, "notification_policy_mismatch"),
            (nonexistent_new_shift, "new_shifts_not_in_shifts"),
            (empty_new_shifts, "new_shift_label_invalid"),
            (unknown_reason, "availability_reason_unknown"),
        ]
        for item, expected_reason in cases:
            with self.subTest(event_id=item.event_id):
                self.write(item)
                sender = Mock(side_effect=self.success)
                outcome = self.deliver(sender)
                self.assertEqual(outcome.status, "quarantined")
                sender.assert_not_called()
                quarantined = self.quarantined(load(self.path))
                self.assertEqual(quarantined.quarantine_reason, expected_reason)

    def test_event_is_quarantined_if_tent_was_disabled_before_delivery(self) -> None:
        item = event("disabled-before-send", burst=False)
        self.write(item)
        sender = Mock(side_effect=self.success)

        outcome = deliver_next(
            self.path,
            max_wait_seconds=120,
            sender=sender,
            now_fn=self.clock.now,
            sleep_fn=self.clock.sleep,
            enabled_tent_slugs=frozenset({"another-tent"}),
        )

        self.assertEqual(outcome.status, "quarantined")
        sender.assert_not_called()
        self.assertEqual(
            self.quarantined(load(self.path)).quarantine_reason,
            "tent_disabled",
        )

    def test_enabled_allowlist_does_not_block_monitor_error(self) -> None:
        item = error_event("disabled-monitor-error")
        self.write(item)

        outcome = deliver_next(
            self.path,
            max_wait_seconds=120,
            sender=self.success,
            now_fn=self.clock.now,
            sleep_fn=self.clock.sleep,
            enabled_tent_slugs=frozenset(),
        )

        self.assertEqual(outcome.status, "delivered")

    def test_monitor_error_with_invalid_booking_url_is_quarantined(self) -> None:
        item = error_event()
        item.booking_url = "javascript:alert(1)"
        self.write(item)
        sender = Mock(side_effect=self.success)

        outcome = self.deliver(sender)

        self.assertEqual(outcome.status, "quarantined")
        sender.assert_not_called()
        self.assertEqual(
            self.quarantined(load(self.path)).quarantine_reason,
            "booking_url_invalid",
        )

    def test_legacy_monitor_error_without_booking_url_remains_deliverable(self) -> None:
        item = error_event()
        item.booking_url = None
        self.write(item)

        outcome = self.deliver()

        self.assertEqual(outcome.status, "delivered")
        stored = load(self.path).outbox[item.event_id]
        self.assertEqual((stored.status, stored.next_index), ("delivered", 1))

    def test_key_mismatch_is_quarantined_with_minimal_metadata(self) -> None:
        poison = event("original", burst=False)
        state = State(outbox={"original": poison})
        # Mutate after State validation to emulate a bad in-memory producer.
        state.outbox = {"different": poison}

        with patch("src.outbox.load", return_value=state):
            outcome = self.deliver()

        self.assertEqual(outcome.status, "quarantined")
        self.assertTrue((outcome.event_id or "").startswith("quarantine-"))
        stored = self.quarantined(load(self.path))
        self.assertEqual(
            stored.quarantined_payload,
            {"stage": "validation", "event_id_key_matched": False},
        )

    def test_runtime_quarantine_never_persists_raw_identifiers_or_extras(self) -> None:
        map_sentinel = "PUSHOVER_TOKEN_MAP_SENTINEL"
        event_sentinel = "PUSHOVER_TOKEN_EVENT_SENTINEL"
        extra_sentinel = "PUSHOVER_TOKEN_EXTRA_SENTINEL"
        poison = event("original", burst=False)
        state = State(outbox={"original": poison})
        poison.event_id = event_sentinel
        poison.__pydantic_extra__["untrusted_extra"] = extra_sentinel
        state.outbox = {map_sentinel: poison}

        with patch("src.outbox.load", return_value=state):
            outcome = self.deliver()

        self.assertEqual(outcome.status, "quarantined")
        serialised = self.path.read_text(encoding="utf-8")
        for sentinel in (map_sentinel, event_sentinel, extra_sentinel):
            self.assertNotIn(sentinel, serialised)

    def test_unexpected_sender_exception_is_quarantined_without_secret_text(self) -> None:
        self.write(event("a", burst=False), event("b", burst=False))

        def poison_sender(item: OutboxEvent, **kwargs):
            raise ValueError("sensitive payload details")

        outcome = self.deliver(poison_sender)
        self.assertEqual(outcome.status, "quarantined")
        self.assertFalse(outcome.fatal)
        stored = self.quarantined(load(self.path))
        self.assertNotIn("sensitive", stored.last_error or "")
        self.assertEqual(stored.quarantine_reason, "ValueError")
        self.assertEqual(self.deliver().event_id, "b")

    def test_invalid_sender_result_is_quarantined(self) -> None:
        self.write(event(burst=False))
        outcome = self.deliver(lambda item, **kwargs: None)
        self.assertEqual(outcome.status, "quarantined")
        self.assertEqual(self.quarantined(load(self.path)).status, "dead_letter")

    def test_dead_letter_can_be_explicitly_requeued_at_current_part(self) -> None:
        item = event()
        item.status = "dead_letter"
        item.next_index = 3
        item.attempts_by_index = {"3": 5}
        item.rate_limit_deferrals_by_index = {"3": 4}
        item.completed_at = "2026-08-04T08:00:00+00:00"
        item.quarantine_reason = "reviewed"
        self.write(item)

        requeued = requeue_dead_letter(
            self.path, "event", timestamp="2026-08-04T08:00:00+00:00"
        )
        self.assertEqual((requeued.status, requeued.next_index), ("pending", 3))
        self.assertEqual(requeued.requeue_count, 1)
        self.assertNotIn("3", requeued.attempts_by_index)
        self.assertNotIn("3", requeued.rate_limit_deferrals_by_index)
        self.assertIsNone(requeued.quarantine_reason)

        outcome = self.deliver()
        self.assertEqual(outcome.part_index, 3)
        self.assertEqual(load(self.path).outbox["event"].next_index, 4)


if __name__ == "__main__":
    unittest.main()
