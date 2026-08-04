from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.notify import DeliveryResult, PushoverDeliveryError
from src.outbox import deliver_next, requeue_dead_letter
from src.state import OutboxEvent, State, load, save


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value = datetime.fromtimestamp(self.value.timestamp() + seconds, timezone.utc)


def event(event_id: str = "event", *, burst: bool = True) -> OutboxEvent:
    return OutboxEvent(
        event_id=event_id,
        tent_slug="test",
        tent_name="Testzelt",
        iso_date="2026-09-26",
        booking_url="https://example.com/book",
        reason="available",
        shifts=["Abend"],
        new_shifts=["Abend"],
        burst=burst,
        total_messages=8 if burst else 1,
        created_at="2026-08-04T08:00:00+00:00",
        next_attempt_at="2026-08-04T08:00:00+00:00",
    )


class OutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd() / "work")
        self.path = Path(self.temp.name) / "state.json"
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, *events: OutboxEvent) -> None:
        save(self.path, State(outbox={item.event_id: item for item in events}))

    @staticmethod
    def success(item: OutboxEvent, **kwargs) -> DeliveryResult:
        return DeliveryResult(f"request-{item.next_index}", 10000, 9999, 1800000000)

    def deliver(self, sender=None):
        return deliver_next(
            self.path,
            max_wait_seconds=120,
            sender=sender or self.success,
            now_fn=self.clock.now,
            sleep_fn=self.clock.sleep,
        )

    def test_exact_burst_gap_sequence_and_progress(self) -> None:
        self.write(event())
        outcomes = [self.deliver() for _ in range(8)]
        self.assertEqual(self.clock.sleeps, [5, 5, 5, 30, 5, 5, 5])
        self.assertEqual([item.part_index for item in outcomes], list(range(8)))
        stored = load(self.path).outbox["event"]
        self.assertEqual((stored.status, stored.next_index), ("delivered", 8))

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
        self.assertEqual((failed.status, load(self.path).outbox["event"].next_index), ("retry_scheduled", 3))
        resumed = self.deliver(flaky)
        self.assertEqual((resumed.part_index, load(self.path).outbox["event"].next_index), (3, 4))
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
            raise PushoverDeliveryError("http_400", failure_class="terminal", status_code=400)

        outcome = self.deliver(reject)
        self.assertTrue(outcome.fatal)
        self.assertEqual(load(self.path).outbox["event"].status, "dead_letter")

    def test_429_is_deferred(self) -> None:
        self.write(event(burst=False))
        reset = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)

        def limited(item: OutboxEvent, **kwargs):
            raise PushoverDeliveryError(
                "http_429", failure_class="rate_limited", retry_at=reset
            )

        self.assertEqual(self.deliver(limited).status, "rate_limited")
        deferred = self.deliver()
        self.assertEqual(deferred.status, "deferred")
        self.assertGreater(deferred.wait_seconds, 35)

    def test_retryable_failure_is_bounded(self) -> None:
        self.write(event(burst=False))

        def timeout(item: OutboxEvent, **kwargs):
            raise PushoverDeliveryError("timeout", failure_class="retryable")

        outcomes = [self.deliver(timeout) for _ in range(5)]
        self.assertEqual(outcomes[-1].status, "dead_letter")
        self.assertTrue(outcomes[-1].fatal)
        self.assertEqual(load(self.path).outbox["event"].attempts_by_index["0"], 5)

    def test_multiple_events_share_schedule_without_losing_progress(self) -> None:
        first = event("a", burst=True)
        second = event("b", burst=True)
        self.write(first, second)
        one = self.deliver()
        two = self.deliver()
        self.assertEqual({one.event_id, two.event_id}, {"a", "b"})
        self.assertEqual(self.clock.sleeps, [])
        stored = load(self.path).outbox
        self.assertEqual((stored["a"].next_index, stored["b"].next_index), (1, 1))

    def test_dead_letter_can_be_explicitly_requeued_at_current_part(self) -> None:
        item = event()
        item.status = "dead_letter"
        item.next_index = 3
        item.attempts_by_index = {"3": 5}
        item.completed_at = "2026-08-04T08:00:00+00:00"
        self.write(item)

        requeued = requeue_dead_letter(
            self.path, "event", timestamp="2026-08-04T08:00:00+00:00"
        )
        self.assertEqual((requeued.status, requeued.next_index), ("pending", 3))
        self.assertEqual(requeued.requeue_count, 1)
        self.assertNotIn("3", requeued.attempts_by_index)

        outcome = self.deliver()
        self.assertEqual(outcome.part_index, 3)
        self.assertEqual(load(self.path).outbox["event"].next_index, 4)


if __name__ == "__main__":
    unittest.main()
