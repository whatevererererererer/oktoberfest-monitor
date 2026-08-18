from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def reject_blank_strings(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("blank strings are not allowed")
        return value


def _validate_http_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("must be an absolute http(s) URL")
    return value


class ApiConfig(StrictConfigModel):
    endpoint: str
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    payload_template: str | None = None
    query_template: dict[str, str] | None = None
    unavailable_when: str | None = None
    available_when: str | None = None


class HtmlConfig(StrictConfigModel):
    url_template: str
    selector: str | None = None
    unavailable_regex: str | None = None
    available_regex: str | None = None
    match_html: bool = False  # if true, regex against raw HTML; else against stripped text


class HashConfig(StrictConfigModel):
    url_template: str
    selector: str | None = None


class HeadlessConfig(StrictConfigModel):
    url_template: str
    wait_until: Literal["load", "domcontentloaded", "networkidle"] = "domcontentloaded"
    wait_extra_ms: int = 4000
    selector: str | None = None
    available_regex: str | None = None
    unavailable_regex: str | None = None


class FestzeltOsConfig(StrictConfigModel):
    """Tent-level batch fetcher: detects per-date status + shift options."""
    url_template: str
    wait_until: Literal["load", "domcontentloaded", "networkidle"] = "domcontentloaded"
    navigation_timeout_ms: int = Field(default=45000, ge=1000, le=120000)
    date_control_timeout_ms: int = Field(default=12000, ge=100, le=60000)
    shift_update_timeout_ms: int = Field(default=12000, ge=100, le=60000)
    poll_interval_ms: int = Field(default=100, ge=10, le=1000)
    stable_for_ms: int = Field(default=500, ge=0, le=5000)
    date_selector: str | None = None
    shift_selector: str | None = None
    # Accepted for all existing YAML files. They no longer cause an unconditional
    # sleep; their values only widen the condition-based deadline.
    wait_extra_ms: int = Field(default=5000, ge=0, le=60000)
    shift_wait_ms: int = Field(default=2500, ge=0, le=60000)

    @field_validator("url_template")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return _validate_http_url(value)


class KaeferConfig(StrictConfigModel):
    """Read Käfer's public slot feed through its official browser application."""

    url_template: str
    slot_endpoint: str
    wait_until: Literal["load", "domcontentloaded", "networkidle"] = "domcontentloaded"
    navigation_timeout_ms: int = Field(default=45000, ge=1000, le=120000)
    slot_timeout_ms: int = Field(default=15000, ge=100, le=60000)

    @field_validator("url_template", "slot_endpoint")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return _validate_http_url(value)


class ReservierungsmanagerConfig(StrictConfigModel):
    """Official Reservierungsmanager widget and its read-only event-day feed."""

    landing_url: str
    event_days_endpoint: str
    expected_theme: str
    include_name_regex: str | None = None
    exclude_name_regex: str | None = None

    @field_validator("landing_url")
    @classmethod
    def valid_landing_url(cls, value: str) -> str:
        parsed = urlparse(_validate_http_url(value))
        if parsed.scheme != "https":
            raise ValueError("widget landing URL must use https")
        return value

    @field_validator("event_days_endpoint")
    @classmethod
    def valid_event_days_endpoint(cls, value: str) -> str:
        parsed = urlparse(_validate_http_url(value))
        if parsed.scheme != "https" or parsed.hostname != "api.reservierungsmanager.de":
            raise ValueError(
                "event-day endpoint must use https://api.reservierungsmanager.de"
            )
        return value

    @field_validator("include_name_regex", "exclude_name_regex")
    @classmethod
    def valid_name_regex(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError("invalid ticket-name regex") from exc
        return value

    @model_validator(mode="after")
    def paired_name_filters(self) -> Self:
        if bool(self.include_name_regex) != bool(self.exclude_name_regex):
            raise ValueError("include/exclude ticket-name regexes must be paired")
        return self


class FloesserstadlConfig(StrictConfigModel):
    """Server-rendered Squarespace reservation options for Bartls Flößerstadl."""

    url_template: str

    @field_validator("url_template")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return _validate_http_url(value)


class TentConfig(StrictConfigModel):
    slug: str
    name: str
    booking_url: str
    mode: Literal[
        "api",
        "html",
        "hash",
        "headless",
        "festzelt_os",
        "floesserstadl",
        "kaefer",
        "reservierungsmanager",
        "manual",
    ]
    dates: list[str]
    enabled: bool = True
    notes: str | None = None
    api: ApiConfig | None = None
    html: HtmlConfig | None = None
    hash: HashConfig | None = None
    headless: HeadlessConfig | None = None
    festzelt_os: FestzeltOsConfig | None = None
    floesserstadl: FloesserstadlConfig | None = None
    kaefer: KaeferConfig | None = None
    reservierungsmanager: ReservierungsmanagerConfig | None = None

    @field_validator("booking_url")
    @classmethod
    def valid_booking_url(cls, value: str) -> str:
        return _validate_http_url(value)

    @field_validator("dates")
    @classmethod
    def valid_dates(cls, values: list[str]) -> list[str]:
        if not values:
            raise ValueError("at least one target date is required")
        if len(values) != len(set(values)):
            raise ValueError("target dates must be unique")
        for value in values:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError(f"target date must use ISO YYYY-MM-DD: {value!r}")
        return values

    @model_validator(mode="after")
    def matching_mode_block(self) -> Self:
        block = getattr(self, self.mode, None)
        if self.mode != "manual" and block is None:
            raise ValueError(f"mode={self.mode!r} requires a matching {self.mode!r} block")
        return self


def load_tents(tents_dir: Path) -> list[TentConfig]:
    configs: list[TentConfig] = []
    for path in sorted(tents_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        configs.append(TentConfig.model_validate(data))
    return configs
