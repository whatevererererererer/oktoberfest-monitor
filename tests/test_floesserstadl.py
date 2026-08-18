from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import httpx

from src.config import FloesserstadlConfig
from src.fetchers.floesserstadl import extract_form_fields, fetch, parse_form_fields

TARGET = "2026-09-26"


def fields(*, midday: bool = True, evening: bool = False) -> list[dict]:
    midday_options = [
        "Ich möchte keinen Mittagstisch",
        "Freitag, 25.09.26 – Mittagstisch 11:00 – 16:30 Uhr",
    ]
    evening_options = [
        "Ich möchte keinen Abendtisch",
        "Freitag, 25.09.26 – Abendtisch 17:30 bis 23:00 Uhr",
    ]
    if midday:
        midday_options.append("Samstag, 26.09.26 - Mittagstisch")
    if evening:
        evening_options.append("Samstag, 26.09.26 - Abendtisch")
    return [
        {
            "title": "Reservierung am Mittag",
            "description": "Verfügbare Reservierungstage am Mittag von 11:00 – 16:30 Uhr",
            "type": "select",
            "options": midday_options,
        },
        {
            "title": "Reservierung am Abend",
            "description": "Verfügbare Reservierungstage am Abend von 17:30 – 23:00 Uhr",
            "type": "select",
            "options": evening_options,
        },
    ]


class FloesserstadlParserTests(unittest.TestCase):
    def test_target_midday_is_available(self) -> None:
        result = parse_form_fields(fields(), TARGET)
        self.assertEqual(result.status, "available")
        self.assertEqual(result.shifts, ("Mittag (11:00–16:30)",))

    def test_both_target_shifts_are_available(self) -> None:
        result = parse_form_fields(fields(evening=True), TARGET)
        self.assertEqual(result.shifts, ("Mittag (11:00–16:30)", "Abend (17:30–23:00)"))

    def test_sold_out_shift_may_contain_only_its_sentinel(self) -> None:
        payload = fields()
        payload[1]["options"] = ["Ich möchte keinen Abendtisch"]
        result = parse_form_fields(payload, TARGET)
        self.assertEqual(result.status, "available")
        self.assertEqual(result.shifts, ("Mittag (11:00–16:30)",))

    def test_all_sentinel_only_fields_are_unknown_without_date_evidence(self) -> None:
        payload = fields()
        payload[0]["options"] = ["Ich möchte keinen Mittagstisch"]
        payload[1]["options"] = ["Ich möchte keinen Abendtisch"]
        result = parse_form_fields(payload, TARGET)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.diagnostics.error_class, "reservation_form_no_dates")

    def test_exact_sentinel_is_required_once_per_shift(self) -> None:
        cases = []
        missing = fields()
        missing[0]["options"] = missing[0]["options"][1:]
        cases.append(missing)
        suffixed = fields()
        suffixed[0]["options"][0] += " – Warteliste"
        cases.append(suffixed)
        duplicated = fields()
        duplicated[0]["options"].insert(1, "Ich möchte keinen Mittagstisch")
        cases.append(duplicated)
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(parse_form_fields(payload, TARGET).status, "unknown")

    def test_option_requires_matching_german_weekday(self) -> None:
        payload = fields()
        payload[0]["options"][-1] = "Freitag, 26.09.26 - Mittagstisch"
        self.assertEqual(parse_form_fields(payload, TARGET).status, "unknown")

    def test_only_exact_optional_shift_times_are_accepted(self) -> None:
        valid_cases = (
            "Samstag, 26.09.26 – Mittagstisch 11:00 – 16:30 Uhr",
            "Samstag, 26.09.26 - Mittagstisch 11:00 bis 16:30 Uhr",
        )
        for option in valid_cases:
            payload = fields()
            payload[0]["options"][-1] = option
            with self.subTest(option=option):
                self.assertEqual(parse_form_fields(payload, TARGET).status, "available")

        invalid_cases = (
            "Samstag, 26.09.26 - Mittagstisch 12:00 – 16:30 Uhr",
            "Samstag, 26.09.26 - Mittagstisch 11:00 – 16:30",
            "Samstag, 26.09.26 - Mittagstisch 11:00 – 16:30 Uhr Warteliste",
        )
        for option in invalid_cases:
            payload = fields()
            payload[0]["options"][-1] = option
            with self.subTest(option=option):
                self.assertEqual(parse_form_fields(payload, TARGET).status, "unknown")

    def test_unknown_suffixes_fail_closed(self) -> None:
        for suffix in ("AUSGEBUCHT", "Warteliste", "nicht verfügbar"):
            payload = fields()
            payload[0]["options"][-1] += f" – {suffix}"
            with self.subTest(suffix=suffix):
                self.assertEqual(parse_form_fields(payload, TARGET).status, "unknown")

    def test_additional_afternoon_reservation_shift_fails_closed(self) -> None:
        payload = fields(midday=False)
        payload.append(
            {
                "title": "Reservierung am Nachmittag",
                "description": (
                    "Verfügbare Reservierungstage am Nachmittag von 16:30 – 17:30 Uhr"
                ),
                "type": "select",
                "options": [
                    "Ich möchte keinen Nachmittagstisch",
                    "Samstag, 26.09.26 - Nachmittagstisch",
                ],
            }
        )
        result = parse_form_fields(payload, TARGET)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(
            result.diagnostics.error_class, "reservation_form_schema_invalid"
        )

    def test_additional_radio_reservation_shift_fails_closed(self) -> None:
        payload = fields(midday=False)
        payload.append(
            {
                "title": "Reservierung am Nachmittag",
                "description": (
                    "Verfügbare Reservierungstage am Nachmittag von 16:30 – 17:30 Uhr"
                ),
                "type": "radio",
                "options": [
                    "Ich möchte keinen Nachmittagstisch",
                    "Samstag, 26.09.26 - Nachmittagstisch",
                ],
            }
        )
        result = parse_form_fields(payload, TARGET)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(
            result.diagnostics.error_class, "reservation_form_schema_invalid"
        )

    def test_each_additional_reservation_signal_fails_closed(self) -> None:
        cases = (
            {
                "title": "Reservierung am Spätabend",
                "description": "Sonderauswahl",
                "type": "select",
                "options": ["Option auswählen"],
            },
            {
                "title": "Sonderauswahl",
                "description": "Verfügbare Reservierungstage für Sondertische",
                "type": "select",
                "options": ["Option auswählen"],
            },
            {
                "title": "Sonderauswahl",
                "description": "Zusatzwunsch",
                "type": "select",
                "options": ["Samstag, 26.09.2026 - Sondertisch"],
            },
        )
        for field in cases:
            payload = fields()
            payload.append(field)
            with self.subTest(field=field):
                self.assertEqual(parse_form_fields(payload, TARGET).status, "unknown")

    def test_irrelevant_area_select_is_ignored(self) -> None:
        payload = fields()
        payload.append(
            {
                "title": "Bevorzugter Bereich",
                "description": "",
                "type": "select",
                "options": ["Mitte", "Seite Nord", "Seite Süd"],
            }
        )
        result = parse_form_fields(payload, TARGET)
        self.assertEqual(result.status, "available")
        self.assertEqual(result.shifts, ("Mittag (11:00–16:30)",))

    def test_valid_fields_without_target_are_unavailable(self) -> None:
        result = parse_form_fields(fields(midday=False), TARGET)
        self.assertEqual(result.status, "unavailable")

    def test_missing_duplicate_or_stale_fields_are_unknown(self) -> None:
        cases = [
            fields()[:1],
            fields() + [fields()[0]],
            [{**field, "type": "text"} for field in fields()],
            [
                {
                    **field,
                    "options": [option.replace("26.09.26", "26.09.25") for option in field["options"]],
                }
                for field in fields()
            ],
            [
                {
                    **field,
                    "options": [
                        option + " – AUSGEBUCHT" if "26.09.26" in option else option
                        for option in field["options"]
                    ],
                }
                for field in fields()
            ],
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(parse_form_fields(payload, TARGET).status, "unknown")

    def test_target_outside_festival_is_unknown(self) -> None:
        self.assertEqual(parse_form_fields(fields(), "2027-09-26").status, "unknown")

    def test_extract_requires_one_payload(self) -> None:
        raw = json.dumps(fields(), ensure_ascii=False)
        self.assertEqual(extract_form_fields(f'<script>{{"formFields":{raw}}}</script>'), fields())
        with self.assertRaises(ValueError):
            extract_form_fields(f'{{"formFields":{raw}}}{{"formFields":{raw}}}')


class FloesserstadlFetchTests(unittest.TestCase):
    def test_fetch_performs_exactly_one_get(self) -> None:
        calls: list[str] = []
        raw = json.dumps(fields(), ensure_ascii=False)

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            return httpx.Response(200, text=f'<script>{{"formFields":{raw}}}</script>')

        cfg = FloesserstadlConfig(url_template="https://official.example/reservierung")
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = fetch(cfg, TARGET, client)
        self.assertEqual(result.status, "available")
        self.assertEqual(calls, ["GET"])

    def test_oversized_declared_content_length_is_rejected_before_read(self) -> None:
        class MustNotRead(httpx.SyncByteStream):
            def __init__(self) -> None:
                self.was_read = False

            def __iter__(self):
                self.was_read = True
                raise AssertionError("oversized declared body must not be read")
                yield b""  # pragma: no cover

        stream = MustNotRead()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Length": "2000001"},
                stream=stream,
            )

        cfg = FloesserstadlConfig(url_template="https://official.example/reservierung")
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(ValueError, "content length exceeds limit"):
                fetch(cfg, TARGET, client)
        self.assertFalse(stream.was_read)

    def test_oversized_streamed_body_is_rejected_without_content_length(self) -> None:
        class OversizedBody(httpx.SyncByteStream):
            def __iter__(self):
                yield b"x" * 1_000_000
                yield b"x" * 1_000_000
                yield b"x"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=OversizedBody())

        cfg = FloesserstadlConfig(url_template="https://official.example/reservierung")
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(ValueError, "body exceeds limit"):
                fetch(cfg, TARGET, client)

    def test_slow_drip_exceeding_total_deadline_is_rejected(self) -> None:
        class Clock:
            now = 0.0

            def monotonic(self) -> float:
                return self.now

        class SlowBody(httpx.SyncByteStream):
            def __init__(self, clock: Clock) -> None:
                self.clock = clock

            def __iter__(self):
                self.clock.now = 5.0
                yield b"first"
                self.clock.now = 20.1
                yield b"second"

        clock = Clock()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=SlowBody(clock))

        cfg = FloesserstadlConfig(url_template="https://official.example/reservierung")
        with patch(
            "src.fetchers.floesserstadl.time.monotonic", side_effect=clock.monotonic
        ):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaisesRegex(TimeoutError, "total deadline exceeded"):
                    fetch(cfg, TARGET, client)

    def test_invalid_utf8_is_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"\xff")

        cfg = FloesserstadlConfig(url_template="https://official.example/reservierung")
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(ValueError, "not valid UTF-8"):
                fetch(cfg, TARGET, client)


if __name__ == "__main__":
    unittest.main()
