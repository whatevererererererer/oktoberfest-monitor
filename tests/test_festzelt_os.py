from __future__ import annotations

import json
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest.mock import Mock, patch

from pydantic import ValidationError

from src.config import FestzeltOsConfig, TentConfig, load_tents
from src.fetchers.festzelt_os import SAFARI_MACOS_USER_AGENT, canonical_date, fetch
from src.fetchers.headless import launch_browser
from src.targets import TARGET_DATES


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
    visible: bool = True
    enabled: bool = True


@dataclass
class Scenario:
    selects: object
    body: str = "Reservierung"
    title: str = "Reservierung"
    navigation_error: bool = False
    navigation_status: int = 200
    navigation_headers: dict[str, str] = field(default_factory=dict)
    frames: list[object] = field(default_factory=list)
    on_select: object | None = None


@dataclass
class FakeResponse:
    url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    request: object | None = None
    json_payload: object = field(default_factory=lambda: {"components": []})
    json_error: bool = False

    def header_value(self, name: str) -> str | None:
        wanted = name.casefold()
        return next(
            (value for key, value in self.headers.items() if key.casefold() == wanted),
            None,
        )

    def json(self) -> object:
        if self.json_error:
            raise ValueError("invalid JSON")
        return self.json_payload


@dataclass
class FakeRequest:
    url: str
    method: str = "POST"
    post_data: str | None = None


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


class FakeFrame:
    def __init__(self, body: str, *, url: str = "about:blank", name: str = "") -> None:
        self.body = body
        self.url = url
        self.name = name

    def locator(self, selector: str):
        if selector == "body":
            return FakeText(self.body)
        if selector == 'input[type="password"]':
            return FakeCollection(
                [object()] if "password" in self.body.casefold() else []
            )
        return FakeCollection([])


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

    def is_visible(self, **_kwargs) -> bool:
        return self.definition.visible

    def is_enabled(self, **_kwargs) -> bool:
        return self.definition.enabled

    def evaluate(self, script: str, **_kwargs):
        if "__wiesnSelectSnapshot" in script:
            return {
                "__wiesnSelectSnapshot": True,
                "attributes": dict(self.definition.attrs),
                "value": self.input_value(),
                "visible": self.definition.visible,
                "enabled": self.definition.enabled,
                "options": [
                    {
                        "value": option.value,
                        "text": option.text,
                        "disabled": option.disabled,
                    }
                    for option in self._options()
                ],
            }
        if "getAttributeNames" in script:
            if "el.getAttribute(name)" in script:
                return next(
                    (
                        value
                        for name, value in self.definition.attrs.items()
                        if name == "wire:model" or name.startswith("wire:model.")
                    ),
                    None,
                )
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
        if any(
            name == "wire:model" or name.startswith("wire:model.")
            for name in self.definition.attrs
        ):
            model = next(
                value
                for name, value in self.definition.attrs.items()
                if name == "wire:model" or name.startswith("wire:model.")
            )
            self.page.emit_livewire_request(chosen.value, model=model)
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
        self.last_livewire_request: FakeRequest | None = None
        self.closed = False
        self.navigation_count = 0

    @property
    def frames(self) -> list[object]:
        return self.scenario.frames

    def goto(self, *_args, **_kwargs) -> FakeResponse:
        self.navigation_count += 1
        if self.scenario.navigation_error:
            raise RuntimeError("navigation failed")
        return FakeResponse(
            "https://example.test/reservation",
            self.scenario.navigation_status,
            self.scenario.navigation_headers,
        )

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

    def emit_livewire_request(
        self,
        selected_value: str,
        *,
        model: str = "data.reservation_date",
    ) -> FakeRequest:
        request = FakeRequest(
            "https://example.test/livewire/update",
            post_data=json.dumps({"updates": {model: selected_value}}),
        )
        self.last_livewire_request = request
        for handler in list(self.listeners.get("request", [])):
            handler(request)
        return request

    def emit_livewire_response(
        self,
        status: int,
        *,
        headers: dict[str, str] | None = None,
        request: FakeRequest | None = None,
        finish: bool = True,
        finish_error: bool = False,
        content_type: str = "application/json; charset=utf-8",
        json_error: bool = False,
    ) -> FakeResponse:
        request = request or self.last_livewire_request
        response_headers = {"content-type": content_type}
        response_headers.update(headers or {})
        response = FakeResponse(
            "https://example.test/livewire/update",
            status,
            response_headers,
            request,
            json_error=json_error,
        )
        self.emit_response(response)
        if finish_error and request is not None:
            self.emit_request_failed(request)
        elif finish and request is not None:
            self.emit_request_finished(request)
        return response

    def emit_request_finished(self, request: FakeRequest) -> None:
        for handler in list(self.listeners.get("requestfinished", [])):
            handler(request)

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
    visible: bool = True,
    enabled: bool = True,
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
    return SelectDef(key, options, attrs, initial, visible, enabled)


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

    def test_browser_context_uses_historical_safari_macos_profile(self) -> None:
        _, context = self.run_fetch([Scenario([date_select()])])
        self.assertEqual(
            context.creation_kwargs,
            {
                "user_agent": SAFARI_MACOS_USER_AGENT,
                "locale": "de-DE",
                "viewport": {"width": 1280, "height": 1100},
            },
        )
        self.assertIn("Macintosh; Intel Mac OS X 14_5", SAFARI_MACOS_USER_AGENT)
        self.assertIn("Version/17.5 Safari/605.1.15", SAFARI_MACOS_USER_AGENT)

    def test_valid_control_without_target_is_unavailable(self) -> None:
        only_other_day = [
            OptionDef("", "Bitte auswählen"),
            OptionDef("2026-09-24", "Donnerstag, 24. September 2026"),
        ]
        result, context = self.run_fetch([Scenario([date_select(only_other_day)])])
        self.assertEqual(result["2026-09-25"].status, "unavailable")
        self.assertEqual(len(context.pages), 1)
        self.assertGreaterEqual(self.clock.value, self.cfg.date_control_timeout_ms / 1000)

    def test_conflicting_parseable_date_value_and_text_is_structure_error(self) -> None:
        contradictory = [
            OptionDef("", "Bitte auswÃ¤hlen"),
            OptionDef("2026-09-24", "Donnerstag, 24. September 2026"),
            OptionDef("2026-09-25", "Samstag, 26. September 2026"),
        ]
        result, context = self.run_fetch(
            [Scenario([date_select(contradictory)])]
        )
        probe = result["2026-09-25"]
        self.assertEqual(probe.status, "error")
        self.assertEqual(probe.diagnostics.page_type, "error")
        self.assertEqual(probe.diagnostics.error_class, "date_option_conflict")
        self.assertEqual(context.pages[0].actions, [])

    def test_transient_duplicate_date_controls_are_tolerated_until_deadline(self) -> None:
        only_other_day = [
            OptionDef("", "Bitte auswählen"),
            OptionDef("2026-09-24", "Donnerstag, 24. September 2026"),
        ]

        def controls(page: FakePage):
            valid = date_select(only_other_day)
            if page.tick < 6:
                return [valid, date_select(only_other_day, key="duplicate")]
            return [valid]

        result, _ = self.run_fetch([Scenario(controls)])
        self.assertEqual(result["2026-09-25"].status, "unavailable")
        self.assertGreaterEqual(self.clock.value, self.cfg.date_control_timeout_ms / 1000)

    def test_hidden_or_disabled_date_controls_are_not_evidence(self) -> None:
        visible = date_select()
        hidden_duplicate = date_select(key="hidden", visible=False)
        disabled_duplicate = date_select(key="disabled", enabled=False)
        shifts = lambda page: (
            [OptionDef("lunch", "Mittag")] if page.selected_at else []
        )
        result, _ = self.run_fetch(
            [
                Scenario(
                    [visible, hidden_duplicate, disabled_duplicate, shift_select(shifts)],
                    on_select=lambda page, _key, _value: setattr(page, "dom_updated", True),
                )
            ]
        )
        self.assertEqual(result["2026-09-25"].status, "available")

        self.clock.value = 0
        hidden_only, _ = self.run_fetch(
            [Scenario([date_select(visible=False), date_select(key="off", enabled=False)])]
        )
        self.assertEqual(hidden_only["2026-09-25"].status, "error")
        self.assertEqual(
            hidden_only["2026-09-25"].diagnostics.error_class,
            "date_control_missing",
        )

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

    def test_negative_shift_sentinels_with_values_are_never_availability(self) -> None:
        sentinels = (
            OptionDef("prompt", "Bitte auswählen"),
            OptionDef("123", "Bitte Schicht auswählen"),
            OptionDef("none-1", "Keine Schichten verfügbar"),
            OptionDef("real-looking", "Keine Reservierung verfügbar"),
            OptionDef("none-2", "Keine Plätze frei"),
            OptionDef("sold", "Mittag (ausgebucht)"),
            OptionDef("loading-id", "Loading..."),
            OptionDef("no-slots", "No slots available"),
            OptionDef("unavailable", "Mittag"),
        )
        for sentinel in sentinels:
            with self.subTest(label=sentinel.text, value=sentinel.value):
                self.clock.value = 0
                result, _ = self.run_fetch(
                    [
                        Scenario(
                            [date_select(), shift_select([sentinel])],
                            on_select=lambda page, _key, _value: setattr(
                                page, "dom_updated", True
                            ),
                        )
                    ]
                )
                probe = result["2026-09-25"]
                self.assertEqual(probe.status, "unknown")
                self.assertEqual(probe.diagnostics.error_class, "shift_options_empty")

    def test_sentinels_are_removed_without_hiding_real_shifts(self) -> None:
        options = [
            OptionDef("none", "Keine Schichten verfügbar"),
            OptionDef("lunch", "Mittag"),
            OptionDef("sold", "Abend (ausgebucht)"),
        ]
        result, _ = self.run_fetch(
            [
                Scenario(
                    [date_select(), shift_select(options)],
                    on_select=lambda page, _key, _value: setattr(
                        page, "dom_updated", True
                    ),
                )
            ]
        )
        self.assertEqual(result["2026-09-25"].shifts, ("Mittag",))

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

    def test_bot_marker_after_first_4000_body_characters_is_detected(self) -> None:
        body = f"{'Reservierung ' * 500} Verify you are human"
        result, _ = self.run_fetch([Scenario([date_select()], body=body)])
        probe = result["2026-09-25"]
        self.assertEqual((probe.status, probe.diagnostics.page_type), ("error", "bot"))
        self.assertEqual(probe.diagnostics.error_class, "bot_page")

    def test_challenge_inside_iframe_is_detected_without_interaction(self) -> None:
        frame = FakeFrame(
            "Complete the CAPTCHA",
            url="https://challenge.example/cdn-cgi/challenge-platform/widget",
        )
        result, context = self.run_fetch(
            [Scenario([date_select()], frames=[frame])]
        )
        probe = result["2026-09-25"]
        self.assertEqual((probe.status, probe.diagnostics.page_type), ("error", "bot"))
        self.assertEqual(probe.diagnostics.error_class, "bot_page")
        self.assertEqual(context.pages[0].actions, [])

    def test_navigation_http_status_and_challenge_header_are_not_booking(self) -> None:
        scenarios = (
            (403, {}, "bot", "navigation_http_403"),
            (503, {}, "error", "navigation_http_503"),
            (200, {"cf-mitigated": "challenge"}, "bot", "navigation_challenge"),
        )
        for status, headers, page_type, error_class in scenarios:
            with self.subTest(status=status, headers=headers):
                self.clock.value = 0
                result, context = self.run_fetch(
                    [
                        Scenario(
                            [date_select()],
                            navigation_status=status,
                            navigation_headers=headers,
                        )
                    ]
                )
                probe = result["2026-09-25"]
                self.assertEqual((probe.status, probe.diagnostics.page_type), ("error", page_type))
                self.assertEqual(probe.diagnostics.error_class, error_class)
                self.assertEqual(context.pages[0].actions, [])

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
            page.emit_livewire_response(403)

        result, context = self.run_fetch(
            [Scenario([date_select(livewire=True)], on_select=rejected)]
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
                page.emit_livewire_response(403)
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

    def test_livewire_identical_list_accepts_paired_success_response(self) -> None:
        shifts = [OptionDef("lunch", "Mittag")]

        def completed(page: FakePage, _key: str, _value: str) -> None:
            page.emit_livewire_response(200)

        result, _ = self.run_fetch(
            [
                Scenario(
                    [date_select(livewire=True), shift_select(shifts)],
                    on_select=completed,
                )
            ]
        )
        self.assertEqual(result["2026-09-25"].status, "available")

    def test_livewire_2xx_waits_for_request_finished_and_dom_turn(self) -> None:
        cfg = self.cfg.model_copy(update={"stable_for_ms": 0})
        finished_at_tick: list[int] = []

        def shifts(page: FakePage) -> list[OptionDef]:
            if (
                page.tick >= 2
                and page.last_livewire_request is not None
                and not finished_at_tick
            ):
                finished_at_tick.append(page.tick)
                page.emit_request_finished(page.last_livewire_request)
            return [OptionDef("lunch", "Mittag")]

        def early_headers(page: FakePage, _key: str, _value: str) -> None:
            page.emit_livewire_response(200, finish=False)

        browser = FakeBrowser(
            [
                Scenario(
                    [date_select(livewire=True), shift_select(shifts)],
                    on_select=early_headers,
                )
            ],
            self.clock,
        )
        result = fetch(cfg, ["2026-09-25"], browser)
        self.assertEqual(result["2026-09-25"].status, "available")
        self.assertEqual(finished_at_tick, [2])
        self.assertGreater(browser.context.pages[0].tick, finished_at_tick[0])

    def test_livewire_2xx_with_incomplete_body_is_technical_error(self) -> None:
        shifts = [OptionDef("lunch", "Mittag")]

        def incomplete(page: FakePage, _key: str, _value: str) -> None:
            page.emit_livewire_response(200, finish_error=True)

        result, _ = self.run_fetch(
            [
                Scenario(
                    [date_select(livewire=True), shift_select(shifts)],
                    on_select=incomplete,
                )
            ]
        )
        probe = result["2026-09-25"]
        self.assertEqual((probe.status, probe.diagnostics.page_type), ("error", "error"))
        self.assertEqual(probe.diagnostics.error_class, "shift_update_response_incomplete")

    def test_livewire_2xx_html_body_cannot_confirm_stale_shifts(self) -> None:
        stale = [OptionDef("lunch", "Mittag")]

        def html_error(page: FakePage, _key: str, _value: str) -> None:
            page.emit_livewire_response(200, content_type="text/html; charset=utf-8")

        result, _ = self.run_fetch(
            [
                Scenario(
                    [date_select(livewire=True), shift_select(stale)],
                    on_select=html_error,
                )
            ]
        )
        probe = result["2026-09-25"]
        self.assertEqual((probe.status, probe.diagnostics.page_type), ("error", "error"))
        self.assertEqual(probe.diagnostics.error_class, "shift_update_response_not_json")
        self.assertIsNone(probe.shifts)

    def test_livewire_2xx_invalid_json_cannot_confirm_stale_shifts(self) -> None:
        stale = [OptionDef("lunch", "Mittag")]

        def invalid_json(page: FakePage, _key: str, _value: str) -> None:
            page.emit_livewire_response(200, json_error=True)

        result, _ = self.run_fetch(
            [
                Scenario(
                    [date_select(livewire=True), shift_select(stale)],
                    on_select=invalid_json,
                )
            ]
        )
        probe = result["2026-09-25"]
        self.assertEqual((probe.status, probe.diagnostics.page_type), ("error", "error"))
        self.assertEqual(probe.diagnostics.error_class, "shift_update_response_invalid_json")
        self.assertIsNone(probe.shifts)

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

    def test_foreign_livewire_response_cannot_confirm_or_fail_target(self) -> None:
        shifts = [OptionDef("lunch", "Mittag")]
        for status in (200, 403):
            with self.subTest(status=status):
                self.clock.value = 0

                def foreign(page: FakePage, _key: str, _value: str) -> None:
                    page.dom_updated = True
                    unrelated = page.emit_livewire_request("2026-09-24")
                    page.emit_livewire_response(status, request=unrelated)

                result, _ = self.run_fetch(
                    [
                        Scenario(
                            [date_select(livewire=True), shift_select(shifts)],
                            on_select=foreign,
                        )
                    ]
                )
                probe = result["2026-09-25"]
                self.assertEqual(probe.status, "unknown")
                self.assertEqual(
                    probe.diagnostics.error_class,
                    "shift_update_response_unconfirmed",
                )

    def test_livewire_5xx_is_a_fast_technical_error(self) -> None:
        def rejected(page: FakePage, _key: str, _value: str) -> None:
            page.emit_livewire_response(503)

        result, context = self.run_fetch(
            [Scenario([date_select(livewire=True)], on_select=rejected)]
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
            page.emit_livewire_response(
                200, headers={"cf-mitigated": "challenge"}
            )

        result, _ = self.run_fetch(
            [Scenario([date_select(livewire=True)], on_select=challenged)]
        )
        probe = result["2026-09-25"]
        self.assertEqual(probe.status, "error")
        self.assertEqual(probe.diagnostics.page_type, "bot")
        self.assertEqual(probe.diagnostics.error_class, "shift_update_challenge")

    def test_both_targets_share_one_navigated_page(self) -> None:
        def selects(page: FakePage):
            selected = page.values.get("dates", "")
            shifts = []
            if selected:
                shifts = [OptionDef("slot", "Mittag" if selected.endswith("25") else "Abend")]
            return [date_select(), shift_select(shifts)]

        def rerender(page: FakePage, _key: str, _value: str) -> None:
            page.dom_updated = True

        result, context = self.run_fetch(
            [Scenario(selects, on_select=rerender)],
            ["2026-09-25", "2026-09-26"],
        )
        self.assertEqual(result["2026-09-25"].shifts, ("Mittag",))
        self.assertEqual(result["2026-09-26"].shifts, ("Abend",))
        self.assertEqual(len(context.pages), 1)
        self.assertTrue(all(page.closed for page in context.pages))
        self.assertEqual(context.pages[0].navigation_count, 1)
        self.assertEqual(len(context.pages[0].actions), 2)

    def test_open_first_target_request_cannot_confirm_second_target(self) -> None:
        identical = [OptionDef("lunch", "Mittag")]
        first_request: list[FakeRequest] = []

        def reply(page: FakePage, _key: str, value: str) -> None:
            if value.endswith("25"):
                assert page.last_livewire_request is not None
                first_request.append(page.last_livewire_request)
                page.dom_updated = True
                return
            page.dom_updated = True
            # Friday's request was deliberately left open. Its response now
            # arrives while Saturday's target-specific monitor is active.
            page.emit_livewire_response(200, request=first_request[0])

        browser = FakeBrowser(
            [
                Scenario(
                    [date_select(livewire=True), shift_select(identical)],
                    on_select=reply,
                )
            ],
            self.clock,
        )
        result = fetch(self.cfg, ["2026-09-25", "2026-09-26"], browser)
        context = browser.context
        for target in ("2026-09-25", "2026-09-26"):
            self.assertEqual(result[target].status, "unknown")
            self.assertEqual(
                result[target].diagnostics.error_class,
                "shift_update_response_unconfirmed",
            )
        self.assertEqual(len(context.pages), 1)
        self.assertEqual(context.pages[0].navigation_count, 1)
        self.assertEqual(len(context.pages[0].actions), 2)
        self.assertTrue(
            all(not handlers for handlers in context.pages[0].listeners.values())
        )

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
    def test_existing_yaml_files_are_valid_and_saturday_only(self) -> None:
        tents = load_tents(Path(__file__).resolve().parents[1] / "tents")
        self.assertGreaterEqual(len(tents), 1)
        self.assertTrue(all(tuple(tent.dates) == TARGET_DATES for tent in tents))

    def test_current_inventory_enables_every_automatable_in_scope_tent(self) -> None:
        tents = load_tents(Path(__file__).resolve().parents[1] / "tents")
        by_slug = {tent.slug: tent for tent in tents}
        self.assertEqual(len(tents), 19)
        self.assertEqual(
            {tent.slug for tent in tents if not tent.enabled},
            {"gloeckle-wirt"},
        )
        self.assertEqual(by_slug["armbrustschuetzen"].mode, "festzelt_os")
        self.assertEqual(by_slug["augustiner"].mode, "festzelt_os")
        self.assertEqual(by_slug["hacker"].mode, "festzelt_os")
        self.assertEqual(by_slug["kaefer"].mode, "kaefer")
        self.assertEqual(by_slug["muenchner-knoedelei"].mode, "reservierungsmanager")
        self.assertEqual(by_slug["ammer"].mode, "reservierungsmanager")
        self.assertEqual(by_slug["bartls-floesserstadl"].mode, "floesserstadl")
        self.assertNotIn("muenchner-stubn", by_slug)

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


class BrowserLaunchTests(unittest.TestCase):
    def test_playwright_is_stopped_when_chromium_launch_fails(self) -> None:
        playwright = Mock()
        playwright.chromium.launch.side_effect = RuntimeError("launch blocked")
        starter = Mock()
        starter.start.return_value = playwright
        sync_api = ModuleType("playwright.sync_api")
        sync_api.sync_playwright = Mock(return_value=starter)
        package = ModuleType("playwright")
        package.sync_api = sync_api

        with patch.dict(
            sys.modules,
            {"playwright": package, "playwright.sync_api": sync_api},
        ):
            with self.assertRaisesRegex(RuntimeError, "launch blocked"):
                launch_browser()

        playwright.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
