"""Tent-level fetcher: loads one page, probes per-date status AND shift options.

Drives any Festzelt-OS-style booking page (Filament/Livewire-rendered or Nuxt SPA)
where dates are in a <select> with German weekday/month text and selecting a date
populates a second <select> with shift options (Mittag / Nachmittag / Abend / Vormittag).

Returns: {iso_date: (status, [shifts])}
- status = "available" iff iso_date appears in the date <select>
- shifts = labels of options in the booking_list_id <select> after date is selected
"""
from __future__ import annotations

import re
from datetime import date as date_type

from ..config import FestzeltOsConfig
from ..state import Availability

GERMAN_MONTHS = {
    1: "Januar", 2: "Februar", 3: "März", 4: "April",
    5: "Mai", 6: "Juni", 7: "Juli", 8: "August",
    9: "September", 10: "Oktober", 11: "November", 12: "Dezember",
}
GERMAN_WEEKDAYS_LONG = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"
)


def _de_label(iso_date: str) -> tuple[str, str]:
    """Return (value-style 'YYYY-MM-DD', text-style 'Weekday, D. Month YYYY')."""
    d = date_type.fromisoformat(iso_date)
    return iso_date, f"{GERMAN_WEEKDAYS_LONG[d.weekday()]}, {d.day}. {GERMAN_MONTHS[d.month]} {d.year}"


def fetch(cfg: FestzeltOsConfig, target_dates: list[str], browser) -> dict[str, tuple[Availability, list[str]]]:
    ctx = browser.new_context(user_agent=USER_AGENT, locale="de-DE", viewport={"width": 1280, "height": 1100})
    out: dict[str, tuple[Availability, list[str]]] = {d: ("unavailable", []) for d in target_dates}
    try:
        page = ctx.new_page()
        page.goto(cfg.url_template, wait_until=cfg.wait_until, timeout=45000)
        page.wait_for_timeout(cfg.wait_extra_ms)

        # find the <select> whose options look like Wiesn dates — match by value
        # (server-rendered Festzelt-OS uses ISO values "2026-09-25"; SPAs may use
        # German text values "Freitag, 25. September 2026"). Either signal works.
        date_sel_idx = page.evaluate("""() => {
            const sels = document.querySelectorAll('select');
            for (let i = 0; i < sels.length; i++) {
                const opts = Array.from(sels[i].options);
                if (opts.some(o =>
                    /^2026-(09|10)-\\d{2}$/.test(o.value) ||
                    /September|Oktober/.test(o.textContent) ||
                    /\\d{1,2}\\.0?9\\.2026|\\d{1,2}\\.10\\.2026/.test(o.textContent)
                )) return i;
            }
            return -1;
        }""")
        if date_sel_idx < 0:
            return out  # no date select rendered — treat all as unavailable

        # collect option (value, text) pairs once
        date_options = page.evaluate(f"""() => Array.from(document.querySelectorAll('select')[{date_sel_idx}].options)
            .filter(o => o.value)
            .map(o => ({{ v: o.value, t: o.textContent.trim() }}))""")

        # build a lookup from text label OR value to canonical iso_date
        for iso in target_dates:
            iso_v, de_t = _de_label(iso)
            match = next((o for o in date_options if o["v"] == iso_v or o["t"] == de_t), None)
            if not match:
                out[iso] = ("unavailable", [])
                continue
            try:
                page.evaluate(f"""(v) => {{
                    const s = document.querySelectorAll('select')[{date_sel_idx}];
                    s.value = v;
                    s.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}""", match["v"])
                page.wait_for_timeout(cfg.shift_wait_ms)
                shifts = page.evaluate("""() => {
                    const sels = document.querySelectorAll('select');
                    for (const s of sels) {
                        const opts = Array.from(s.options).filter(o => o.value);
                        const txts = opts.map(o => o.textContent.trim());
                        if (txts.some(t => /Mittag|Abend|Nachmittag|Vormittag|Ganztag/.test(t))) return txts;
                    }
                    return [];
                }""")
                out[iso] = ("available", list(shifts) if shifts else [])
            except Exception:
                out[iso] = ("error", [])
    finally:
        ctx.close()
    return out
