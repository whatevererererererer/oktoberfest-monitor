from __future__ import annotations

import re
from datetime import date as date_type


def _shift_matches(label: str, expected: str) -> bool:
    """Match a shift name as a word, allowing labels such as 'Mittag (11:00)'."""
    return re.search(rf"(?<!\w){re.escape(expected)}(?!\w)", label, re.IGNORECASE) is not None


def needs_notification_burst(iso_date: str, newly_available_shifts: list[str]) -> bool:
    """Return whether newly available shifts deserve the repeated alert pattern."""
    weekday = date_type.fromisoformat(iso_date).weekday()
    if weekday == 5:  # Saturday: everything except Mittag
        return any(not _shift_matches(shift, "Mittag") for shift in newly_available_shifts)
    if weekday == 4:  # Friday: everything except Mittag and Nachmittag
        return any(
            not _shift_matches(shift, "Mittag")
            and not _shift_matches(shift, "Nachmittag")
            for shift in newly_available_shifts
        )
    return False
