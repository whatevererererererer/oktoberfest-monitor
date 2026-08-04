from __future__ import annotations

import re
from datetime import date as date_type


def _shift_matches(label: str, expected: str) -> bool:
    """Match a shift name as a word, allowing labels such as 'Mittag (11:00)'."""
    return re.search(rf"(?<!\w){re.escape(expected)}(?!\w)", label, re.IGNORECASE) is not None


def _contains_only_allowed_shift_names(label: str, allowed: set[str]) -> bool:
    known = {"Vormittag", "Mittag", "Nachmittag", "Abend", "Ganztag"}
    found = {name for name in known if _shift_matches(label, name)}
    return bool(found) and found <= allowed


def needs_notification_burst(iso_date: str, newly_available_shifts: list[str]) -> bool:
    """Return whether newly available shifts deserve the repeated alert pattern."""
    weekday = date_type.fromisoformat(iso_date).weekday()
    if weekday == 5:  # Saturday: everything except Mittag
        return any(
            not _contains_only_allowed_shift_names(shift, {"Mittag"})
            for shift in newly_available_shifts
        )
    if weekday == 4:  # Friday: everything except Mittag and Nachmittag
        return any(
            not _contains_only_allowed_shift_names(
                shift, {"Mittag", "Nachmittag"}
            )
            for shift in newly_available_shifts
        )
    return False
