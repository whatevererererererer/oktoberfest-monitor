from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import ValidationError

from src.config import FestzeltOsConfig, TentConfig, load_tents
from src.fetchers.festzelt_os import canonical_date, fetch


@dataclass
class OptionDef:
    value: str
    text: str
    disabled: bool = False


@dataclass
class SelectDef:
    key: str
    options: object
    attrs: dict[str, str] = field(default_factory=dict)
    initial: str = ""


@dataclass
class Scenario:
    selects: object
    body: str = "Reservierung"
    title: str = "Reservierung"
    navigation_error: bool = False
    on_select: object | None = None


@dataclass
class FakeResponse:
    url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)

    def header_value(self, name: str) -> str | None:
        return self.headers.get(name)


@dataclass
class FakeRequest:
    url: str


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds / 1000


class FakeOption:
    def __init__(self, option: OptionDef) -> None:
        self.option = option

    def get_attribute(self, name: str, **_kwargs):
        return self.option.value if name == "value" else None

    def inner_text(self, **_kwargs) -> str:
        return self.option.text

    def is_disabled(self, **_kwargs) -> bool:
        return self.option.disabled


class FakeCollection:
    def __init__(self, items) -> None:
        self.items = list(items)

    def count(self) -> int:
        return len(self.items)

    def nth(self, index: int):
        return self.items[index]


class FakeText:
    def __init__(self, text: str) -> None:
        self.text = text

    def inner_text(self, **_kwargs) -> str:
        return self.text


class FakeSelect:
    def __init__(self, page: "FakePage", definition: SelectDef) -> None:
        self.page = page
        self.definition = definition

    def _options(self) -> list[OptionDef]:
        source = self.definition.options
        return list(source(self.page) if callable(source) else source)

    def locator(self, selector: str) -> FakeCollection:
        if selector != "option":
            return FakeCollection([])
        return FakeCollection(FakeOption(option) for option in self._options())

    def get_attribute(self, name: str, **_kwargs):
        return self.definition.attrs.get(name)

    def input_value(self, **_kwargs) -> str:
        return self.page.values.get(self.definition.key, self.definition.initial)

    def evaluate(self, script: str):
        if "getAttributeNames" in script:
            return any(
                name == "wire:model" or name.startswith("wire:model.")
                for name in self.definition.attrs
            )
        return self.page.evaluate(script)

    def select_option(self, *, value=None, label=None, **_kwargs):
        matching = [
            option
            for option in self._options()
            if (value is not None and option.value == value)
            or (label is not None and option.text == label)
        ]
        if len(matching) != 1 or matching[0].disabled:
            raise RuntimeError("option cannot be selected")
        chosen = matching[0]
        self.page.values[self.definition.key] = chosen.value
        self.page.selected_at[self.definition.key] = self.page.tick
        self.page.actions.append((self.definition.key, chosen.value, chosen.text))
        if self.page.scenario.on_select:
            self.page.scenario.on_select(self.page, self.definition.key, chosen.value)


class FakePage:
    def __init__(self, scenario: Scenario, clock: FakeClock) -> None:
        self.scenario = scenario
        self.clock = clock
        self.tick = 0
        self.values: dict[str, str] = {}
        self.selected_at: dict[str, int] = {}
        self.actions: list[tuple[str, str, str]] = []
        self.observer_armed = False
        self.dom_updated = False
        self.observer_script: str | None = None
        self.listeners: dict[str, list[object]] = {}
        self.closed = False

    def goto(self, *_args, **_kwargs) -> None:
        if self.scenario.navigation_error:
            raise RuntimeError("navigation failed")

    def title(self) -> str:
        return self.scenario.title

    def _select_defs(self) -> list[SelectDef]:
        source = self.scenario.selects
        return list(source(self) if callable(source) else source)

    def locator(self, selector: str):
        if selector == "body":
            return FakeText(self.scenario.body)
        if selector == 'input[type="password"]':
            return FakeCollection([object()] if "password" in self.scenario.body.casefold() else [])
        definitions = self._select_defs()
        if selector == "select":
            return FakeCollection(FakeSelect(self, definition) for definition in definitions)
        if selector.startswith("#"):
            wanted = selector[1:]
            return FakeCollection(
                FakeSelect(self, definition)
                for definition in definitions
                if definition.attrs.get("id") == wanted
            )
        return FakeCollection([])

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.tick += 1
        self.clock.advance(milliseconds)

    def evaluate(self, script: str):
        if "MutationObserver" in script:
            self.observer_armed = True
            self.dom_updated = False
            self.observer_script = script
            return True
        if "__wiesnProbeMutation" in script:
            return self.observer_armed and self.dom_updated
        raise AssertionError("unexpected evaluate call")

    def on(self, event: str, handler) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler) -> None:
        handlers = self.listeners.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    def emit_response(self, response: FakeResponse) -> None:
        for handler in list(self.listeners.get("response", [])):
            handler(response)

    def emit_request_failed(self, request: FakeRequest) -> None:
        for handler in list(self.listeners.get("requestfailed", [])):
            handler(request)

    def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, scenarios: list[Scenario], clock: FakeClock) -> None:
        self.scenarios = scenarios
        self.clock = clock
        self.pages: list[FakePage] = []
        self.closed = False
        self.creation_kwargs: dict[str, object] = {}

    def new_page(self) -> FakePage:
        if len(self.pages) >= len(self.scenarios):
            raise AssertionError("fetcher opened more pages than expected")
        page = FakePage(self.scenarios[len(self.pages)], self.clock)
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, scenarios: list[Scenario], clock: FakeClock) -> None:
        self.context = FakeContext(scenarios, clock)

    def new_context(self, **kwargs) -> FakeContext:
        self.context.creation_kwargs = kwargs
        return self.context


DATES = [
    OptionDef("", "Bitte auswählen"),
    OptionDef("2026-09-24", "Donnerstag, 24. September 2026"),
    OptionDef("2026-09-25", "Freitag, 25. September 2026"),
    OptionDef("2026-09-26", "Samstag, 26. September 2026"),
]


def date_select(
    options=None,
    *,
    initial="",
    key="dates",
    disabled_target=False,
    livewire: bool | str = False,
) -> SelectDef:
    options = list(options or DATES)
    if disabled_target:
        options = [
            OptionDef(option.value, option.text, option.value == "2026-09-25")
            for option in options
        ]
    attrs = {"id": key, "name": "reservation_date"}
    if livewire:
        modifier = "wire:model.live" if livewire is True else livewire
        attrs[modifier] = "data.reservation_date"
    return SelectDef(key, options, attrs, initial)


def shift_select(options, *, key="shifts") -> SelectDef:
    return SelectDef(key, options, {"id": key, "name": "booking_list_id"})


class FestzeltFetcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.cfg = FestzeltOsConfig(
            url_template="https://example.test/reservation",
            wait_extra_ms=0,
            shift_wait_ms=0,
            date_control_timeout_ms=100,
            shift_update_timeout_ms=100,
            poll_interval_ms=10,
            stable_for_ms=20,
        )
        self.monotonic = patch(
            "src.fetchers.festzelt_os.time.monotonic", side_effect=self.clock
        )
        self.monotonic.start()

    def tearDown(self) -> None:
        self.monotonic.stop()

    def run_fetch(self, scenarios, dates=None):
        browser = FakeBrowser(scenarios, self.clock)
        result = fetch(self.cfg, dates or ["2026-09-25"], browser)
        return result, browser.context

    def test_missing_date_control_is_error(self) -> None:
        result, _ = self.run_fetch([Scenario([])])
        probe = result["2026-09-25"]
        self.assertEqual(probe.status, "error")
        self.assertEqual(probe.diagnostics.error_class, "date_control_missing")

    def test_browser_context_does_not_spoof_a_different_browser(self) -> None:
        _, context = self.run_fetch([Scenario([date_select()])])
        self.assertNotIn("user_agent", context.creation_kwargs)

    def test_valid_control_without_target_is_unavailable(self) -> None:
        only_other_day = [
            OptionDef("", "Bitte auswählen"),
            OptionDef("2026-09-24", "Donnerstag, 24. September 2026"),
        ]
        result, context = self.run_fetch([Scenario([date_select(only_other_day)])])
        self.assertEqual(result["2026-09-25"].status, "unavailable")
        self.assertEqual(len(context.pages), 1)

    def test_target_appended_after_initial_stability_is_not_false_unavailable(self) -> None:
        cfg = self.cfg.model_copy(update={"wait_extra_ms": 60})
        only_other_day = [
            OptionDef("", "Bitte auswÃ¤hlen"),
            OptionDef("2026-09-24", "Donnerstag, 24. September 2026"),
        ]

        def selects(page: FakePage):
            dates = only_other_day if page.tick < 5 else DATES
            shifts = (
                [OptionDef("lunch", "Mittag")]
                if "dates" in page.selected_at
                else []
            )
            return [date_select(dates), shift_select(shifts)]

        def updated(page: FakePage, _key: str, _value: str) -> None:
            page.dom_updated = True

        browser = FakeBrowser([Scenario(selects, on_select=updated)], self.clock)
        result = fetch(cfg, ["2026-09-25"], browser)
        self.assertEqual(result["2026-09-25"].status, "available")
        self.assertEqual(result["2026-09-25"].shifts, ("Mittag",))

    def test_target_without_shift_control_is_unknown(self) -> None:
        result, _ = self.run_fetch([Scenario([date_select()])])
        probe = result["2026-09-25"]
        self.assertEqual(probe.status, "unknown")
        self.assertEqual(probe.diagnostics.error_class, "shift_control_missing")

    def test_delayed_shift_options_become_available(self) -> None:
        def shifts(page: FakePage):
            selected = page.selected_at.get("dates")
            if selected is None or page.tick - selected < 2:
                return [OptionDef("", "Bitte auswählen")]
            return [OptionDef("", "Bitte auswählen"), OptionDef("lunch", "Mittag")]

        result, context = self.run_fetch(
            [Scenario([date_select(), shift_select(shifts)])]
        )
        probe = result["2026-09-25"]
        self.assertEqual(probe.status, "available")
        self.assertEqual(probe.shifts, ("Mittag",))
        self.assertEqual(context.pages[0].actions, [("dates", "2026-09-25", "Freitag, 25. September 2026")])

    def test_shift_options_that_remain_empty_are_degraded(self) -> None:
        empty = [OptionDef("", "Bitte auswählen")]
        result, _ = self.run_fetch(
            [Scenario([date_select(), shift_select(empty)])]
        )
        probe = result["2026-09-25"]
        self.assertEqual(probe.status, "unknown")
        self.assertEqual(probe.diagnostics.error_class, "shift_options_empty")

    def test_unchanged_options_from_previous_date_are_not_accepted(self) -> None:
        stale = [OptionDef("lunch", "Mittag")]
        result, _ = self.run_fetch(
            [
                Scenario(
                    [date_select(initial="2026-09-24"), shift_select(stale)]
                ),
            ]
        )
        probe = result["2026-09-25"]
        self.assertEqual(probe.status, "unknown")
        self.assertEqual(probe.diagnostics.error_class, "shift_update_unconfirmed")
        self.assertEqual(probe.diagnostics.shift_count, 1)

    def test_identical_labels_are_accepted_only_after_concrete_dom_update(self) -> None:
        identical = [OptionDef("lunch", "Mittag")]
        page = Scenario(
            [date_select(initial="2026-09-24"), shift_select(identical)],
            on_select=lambda page, _key, _value: setattr(page, "dom_updated", True),
        )
        result, context = self.run_fetch([page])
        probe = result["2026-09-25"]
        self.assertEqual(probe.status, "available")
        self.assertEqual(probe.shifts, ("Mittag",))
        self.assertTrue(probe.diagnostics.update_confirmed)
        observer_script = context.pages[0].observer_script or ""
        self.assertIn("childList: true", observer_script)
        self.assertNotIn("attributes: true", observer_script)

    def test_bot_page_after_selection_is_an_error(self) -> None:
        def selects(page: FakePage):
            return [] if getattr(page, "lost_controls", False) else [date_select()]

        def become_bot(page: FakePage, _key: str, _value: str) -> None:
            page.lost_controls = True
            page.scenario.title = "Just a moment"
            page.scenario.body = "Cloudflare CAPTCHA"

        result, _ = self.run_fetch([Scenario(selects, on_select=become_bot)])
        probe = result["2026-09-25"]
        self.assertEqual((probe.status, probe.diagnostics.page_type), ("error", "bot"))
        self.assertEqual(probe.diagnostics.error_class, "bot_after_selection")

    def test_bot_overlay_with_retained_stale_controls_is_an_error(self) -> None:
        stale = [OptionDef("lunch", "Mittag")]

        def overlay(page: FakePage, _key: str, _value: str) -> None:
            page.dom_updated = True
            page.scenario.title = "Just a moment"
            page.scenario.body = "Cloudflare CAPTCHA"

        result, _ = self.run_fetch(
            [
                Scenario(
                    [date_select(initial="2026-09-24"), shift_select(stale)],
                    on_select=overlay,
                )
            ]
        )
        probe = result["2026-09-25"]
        self.assertEqual((probe.status, probe.diagnostics.page_type), ("error", "bot"))
        self.assertEqual(probe.diagnostics.error_class, "bot_after_selection")

    def test_date_control_disappearing_after_selection_is_an_error(self) -> None:
        def selects(page: FakePage):
            return [] if getattr(page, "lost_controls", False) else [date_select()]

        def lose_control(page: FakePage, _key: str, _value: str) -> None:
            page.lost_controls = True

        result, _ = self.run_fetch([Scenario(selects, on_select=lose_control)])
        probe = result["2026-09-25"]
        self.assertEqual(probe.status, "error")
        self.assertEqual(probe.diagnostics.error_class, "date_control_changed")

    def test_multiple_plausible_date_selects_are_error(self) -> None:
        result, _ = self.run_fetch(
            [Scenario([date_select(key="first"), date_select(key="second")])]
        )
        self.assertEqual(result["2026-09-25"].status, "error")
        self.assertEqual(
            result["2026-09-25"].diagnostics.error_class,
            "ambiguous_date_control",
        )

    def test_configured_selector_still_has_to_be_plausible_and_unique(self) -> None:
        cfg = self.cfg.model_copy(update={"date_selector": "#dates"})
        other = SelectDef("size", [OptionDef("10", "10 Personen")], {"id": "size"})
        scenarios = [Scenario([other, date_select(), shift_select(
            lambda page: [OptionDef("lunch", "Mittag")] if "dates" in page.selected_at else []
        )])]
        browser = FakeBrowser(scenarios, self.clock)
        result = fetch(cfg, ["2026-09-25"], browser)
        self.assertEqual(result["2026-09-25"].status, "available")

    def test_disabled_target_is_unknown_without_selecting_it(self) -> None:
        result, context = self.run_fetch(
            [Scenario([date_select(disabled_target=True)])]
        )
        probe = result["2026-09-25"]
        self.assertEqual(probe.status, "unknown")
        self.assertEqual(probe.diagnostics.error_class, "target_disabled")
        self.assertEqual(context.pages[0].actions, [])

    def test_bot_error_and_login_pages_are_errors(self) -> None:
        for body, expected in (
            ("Just a moment... Verify you are human", "bot_page"),
            ("503 Service Unavailable", "error_page"),
            ("Login password", "login_page"),
        ):
            with self.subTest(expected=expected):
                self.clock.value = 0
                result, _ = self.run_fetch([Scenario([], body=body)])
                self.assertEqual(result["2026-09-25"].status, "error")
                self.assertEqual(result["2026-09-25"].diagnostics.error_class, expected)

    def test_navigation_timeout_then_next_run_recovers(self) -> None:
        failed, _ = self.run_fetch([Scenario([], navigation_error=True)])
        self.assertEqual(failed["2026-09-25"].status, "error")
        self.assertEqual(failed["2026-09-25"].diagnostics.error_class, "navigation_failed")

        shifts = lambda page: (
            [OptionDef("lunch", "Mittag")] if "dates" in page.selected_at else []
        )
        recovered, _ = self.run_fetch(
            [Scenario([date_select(), shift_select(shifts)])]
        )
        self.assertEqual(recovered["2026-09-25"].status, "available")

    def test_livewire_403_is_a_fast_bot_error_and_listener_is_removed(self) -> None:
        def rejected(page: FakePage, _key: str, _value: str) -> None:
            page.emit_response(
                FakeResponse("https://example.test/livewire/update", 403)
            )

        result, context = self.run_fetch(
            [Scenario([date_select()], on_select=rejected)]
        )
        probe = result["2026-09-25"]
        self.assertEqual(probe.status, "error")
        self.assertEqual(probe.diagnostics.page_type, "bot")
        self.assertEqual(probe.diagnostics.error_class, "shift_update_http_403")
        self.assertLess(context.pages[0].tick, 5)
        self.assertTrue(all(not handlers for handlers in context.pages[0].listeners.values()))

    def test_transient_stale_dom_replacement_does_not_beat_late_403(self) -> None:
        stale = [OptionDef("lunch", "Mittag")]

        def shifts(page: FakePage):
            if (
                page.tick >= 8
                and page.selected_at
                and not getattr(page, "failure_emitted", False)
            ):
                page.failure_emitted = True
                page.emit_response(
                    FakeResponse("https://example.test/livewire/update", 403)
                )
            return stale

        def transient_rerender(page: FakePage, _key: str, _value: str) -> None:
            page.dom_updated = True

        result, _ = self.run_fetch(
            [
                Scenario(
                    [
                        date_select(
                            initial="2026-09-24", livewire="wire:model.defer"
                        ),
                        shift_select(shifts),
                    ],
                    on_select=transient_rerender,
                )
            ]
        )
        probe = result["2026-09-25"]
        self.assertEqual(probe.status, "error")
        self.assertEqual(probe.diagnostics.error_class, "shift_update_http_403")

    def test_livewire_available_requires_successful_update_response(self) -> None:
        shifts = [OptionDef("lunch", "Mittag")]

        def completed(page: FakePage, _key: str, _value: str) -> None:
            page.dom_updated = True
            page.emit_response(FakeResponse("https://example.test/livewire/update", 200))

        result, _ = self.run_fetch(
            [
                Scenario(
                    [date_select(livewire=True), shift_select(shifts)],
                    on_select=completed,
                )
            ]
        )
        self.assertEqual(result["2026-09-25"].status, "available")

    def test_livewire_dom_update_without_response_stays_degraded(self) -> None:
        shifts = [OptionDef("lunch", "Mittag")]

        def incomplete(page: FakePage, _key: str, _value: str) -> None:
            page.dom_updated = True

        result, _ = self.run_fetch(
            [
                Scenario(
                    [date_select(livewire=True), shift_select(shifts)],
                    on_select=incomplete,
                )
            ]
        )
        probe = result["2026-09-25"]
        self.assertEqual(probe.status, "unknown")
        self.assertEqual(
            probe.diagnostics.error_class, "shift_update_response_unconfirmed"
        )

    def test_livewire_5xx_is_a_fast_technical_error(self) -> None:
        def rejected(page: FakePage, _key: str, _value: str) -> None:
            page.emit_response(
                FakeResponse("https://example.test/livewire/update", 503)
            )

        result, context = self.run_fetch(
            [Scenario([date_select()], on_select=rejected)]
        )
        probe = result["2026-09-25"]
        self.assertEqual(probe.status, "error")
        self.assertEqual(probe.diagnostics.page_type, "error")
        self.assertEqual(probe.diagnostics.error_class, "shift_update_http_503")
        self.assertLess(context.pages[0].tick, 5)

    def test_irrelevant_analytics_and_challenge_requests_are_ignored(self) -> None:
        shifts = lambda page: (
            [OptionDef("lunch", "Mittag")] if page.selected_at else []
        )

        def side_requests(page: FakePage, _key: str, _value: str) -> None:
            page.emit_response(
                FakeResponse("https://analytics.example/collect", 403)
            )
            page.emit_response(
                FakeResponse(
                    "https://example.test/cdn-cgi/challenge-platform/scripts/jsd/main.js",
                    403,
                    {"cf-mitigated": "challenge"},
                )
            )

        result, _ = self.run_fetch(
            [
                Scenario(
                    [date_select(), shift_select(shifts)],
                    on_select=side_requests,
                )
            ]
        )
        self.assertEqual(result["2026-09-25"].status, "available")

    def test_livewire_challenge_header_is_a_bot_error(self) -> None:
        def challenged(page: FakePage, _key: str, _value: str) -> None:
            page.emit_response(
                FakeResponse(
                    "https://example.test/livewire/update",
                    200,
                    {"cf-mitigated": "challenge"},
                )
            )

        result, _ = self.run_fetch(
            [Scenario([date_select()], on_select=challenged)]
        )
        probe = result["2026-09-25"]
        self.assertEqual(probe.status, "error")
        self.assertEqual(probe.diagnostics.page_type, "bot")
        self.assertEqual(probe.diagnostics.error_class, "shift_update_challenge")

    def test_sequential_targets_reacquire_date_locator_after_rerender(self) -> None:
        def selects(page: FakePage):
            stage = getattr(page, "stage", 0)
            date_key = "date-initial" if stage == 0 else f"date-rerender-{stage}"
            selected = getattr(page, "current_target", "")
            shifts = []
            if selected:
                shifts = [
                    OptionDef(
                        "slot",
                        "Mittag" if selected == "2026-09-25" else "Abend",
                    )
                ]
            return [date_select(key=date_key, initial=selected), shift_select(shifts)]

        def rerender(page: FakePage, _key: str, value: str) -> None:
            page.current_target = value
            page.stage = getattr(page, "stage", 0) + 1
            page.values[f"date-rerender-{page.stage}"] = value
            page.dom_updated = True

        result, context = self.run_fetch(
            [Scenario(selects, on_select=rerender)],
            ["2026-09-25", "2026-09-26"],
        )
        self.assertEqual(result["2026-09-25"].shifts, ("Mittag",))
        self.assertEqual(result["2026-09-26"].shifts, ("Abend",))
        self.assertEqual(len(context.pages), 1)
        self.assertEqual(
            [action[0] for action in context.pages[0].actions],
            ["date-initial", "date-rerender-1"],
        )

    def test_second_target_cannot_reuse_identical_stale_first_target_options(self) -> None:
        def shifts(page: FakePage):
            return (
                [OptionDef("lunch", "Mittag")]
                if getattr(page, "first_loaded", False)
                else []
            )

        def select_date(page: FakePage, _key: str, value: str) -> None:
            if value == "2026-09-25":
                page.first_loaded = True
                page.dom_updated = True
            # Saturday deliberately leaves Friday's options untouched and
            # produces no relevant shift-control mutation.

        result, context = self.run_fetch(
            [Scenario([date_select(), shift_select(shifts)], on_select=select_date)],
            ["2026-09-25", "2026-09-26"],
        )
        self.assertEqual(result["2026-09-25"].status, "available")
        self.assertEqual(result["2026-09-26"].status, "unknown")
        self.assertEqual(
            result["2026-09-26"].diagnostics.error_class,
            "shift_update_unconfirmed",
        )
        self.assertEqual(len(context.pages), 1)

    def test_date_control_and_options_may_appear_late(self) -> None:
        shifts = lambda page: (
            [OptionDef("lunch", "Mittag")] if page.selected_at else []
        )

        def delayed(page: FakePage):
            return [] if page.tick < 2 else [date_select(), shift_select(shifts)]

        result, _ = self.run_fetch([Scenario(delayed)])
        self.assertEqual(result["2026-09-25"].status, "available")

    def test_exact_supported_date_formats_and_nbsp(self) -> None:
        variants = (
            "2026-09-25",
            "25.09.2026",
            "Freitag, 25. September 2026",
            "Freitag,\xa025.\xa0September\xa02026",
            "Freitag, den 25. 09. 2026",
            "2026-09-25T00:00:00+02:00",
        )
        for value in variants:
            with self.subTest(value=value):
                self.assertEqual(canonical_date(value), "2026-09-25")
        self.assertIsNone(canonical_date("25. September 2026 Resttext"))
        self.assertEqual(canonical_date("2026-09-25T00:00:00"), "2026-09-25")


class ConfigValidationTests(unittest.TestCase):
    def test_existing_yaml_files_remain_valid(self) -> None:
        tents = load_tents(Path(__file__).resolve().parents[1] / "tents")
        self.assertGreaterEqual(len(tents), 1)

    def test_unknown_keys_and_missing_mode_block_are_rejected(self) -> None:
        base = {
            "slug": "test",
            "name": "Test",
            "booking_url": "https://example.test",
            "mode": "festzelt_os",
            "dates": ["2026-09-25"],
        }
        with self.assertRaises(ValidationError):
            TentConfig.model_validate({**base, "unexpected": True})
        with self.assertRaises(ValidationError):
            TentConfig.model_validate(base)

    def test_legacy_wait_fields_are_still_accepted(self) -> None:
        cfg = FestzeltOsConfig(
            url_template="https://example.test",
            wait_extra_ms=7000,
            shift_wait_ms=2500,
        )
        self.assertEqual(cfg.wait_extra_ms, 7000)
        self.assertEqual(cfg.shift_wait_ms, 2500)


if __name__ == "__main__":
    unittest.main()
