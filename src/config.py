from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class ApiConfig(BaseModel):
    endpoint: str
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    payload_template: str | None = None
    query_template: dict[str, str] | None = None
    unavailable_when: str | None = None
    available_when: str | None = None


class HtmlConfig(BaseModel):
    url_template: str
    selector: str | None = None
    unavailable_regex: str | None = None
    available_regex: str | None = None
    match_html: bool = False  # if true, regex against raw HTML; else against stripped text


class HashConfig(BaseModel):
    url_template: str
    selector: str | None = None


class TentConfig(BaseModel):
    slug: str
    name: str
    booking_url: str
    mode: Literal["api", "html", "hash", "manual"]
    dates: list[str]
    enabled: bool = True
    notes: str | None = None
    api: ApiConfig | None = None
    html: HtmlConfig | None = None
    hash: HashConfig | None = None


def load_tents(tents_dir: Path) -> list[TentConfig]:
    configs: list[TentConfig] = []
    for path in sorted(tents_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        configs.append(TentConfig.model_validate(data))
    return configs
