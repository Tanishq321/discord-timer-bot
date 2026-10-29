"""Turn human-friendly time text into a timezone-aware datetime.

Accepted examples (case-insensitive):
    in 10m          10m          2h30m        1d 4h        90s
    18:30           6:30 pm      6pm          (today, or tomorrow if already past)
    tomorrow 9am    tomorrow 21:00
    2026-11-02 18:30            02-11-2026 18:30         02/11/2026 6:30 pm
"""

import re
from datetime import datetime, timedelta

_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_DURATION_RE = re.compile(r"^(?:in\s+)?((?:\d+\s*[dhms]\s*)+)$")

_TIME_FORMATS = ("%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p")
_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y")

HELP_TEXT = (
    "Try `in 30m`, `2h15m`, `18:30`, `6:30 pm`, `tomorrow 9am`, "
    "or a full date like `2026-11-02 18:30` / `02-11-2026 6:30 pm`."
)


def _parse_time_of_day(text: str):
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def parse_when(text: str, now: datetime) -> datetime:
    """Return the moment `text` refers to, in now's timezone. Raises ValueError."""
    raw = " ".join(text.strip().split())  # collapse extra spaces
    low = raw.lower()

    # 1) Relative durations: "in 10m", "2h30m", "1d 4h"
    m = _DURATION_RE.match(low)
    if m:
        seconds = sum(int(n) * _UNIT_SECONDS[u]
                      for n, u in re.findall(r"(\d+)\s*([dhms])", m.group(1)))
        if seconds <= 0:
            raise ValueError("Duration must be greater than zero.")
        return now + timedelta(seconds=seconds)

    up = raw.upper()  # strptime's %p wants AM/PM

    # 2) "tomorrow <time>"
    if low.startswith("tomorrow"):
        t = _parse_time_of_day(up[len("tomorrow"):].strip())
        if t is None:
            raise ValueError("Couldn't read the time after 'tomorrow'. " + HELP_TEXT)
        day = (now + timedelta(days=1)).date()
        return datetime.combine(day, t, tzinfo=now.tzinfo)

    # 3) Full date + time
    for dfmt in _DATE_FORMATS:
        for tfmt in _TIME_FORMATS:
            try:
                dt = datetime.strptime(up, f"{dfmt} {tfmt}")
                return dt.replace(tzinfo=now.tzinfo)
            except ValueError:
                continue

    # 4) Time only -> next occurrence
    t = _parse_time_of_day(up)
    if t is not None:
        dt = datetime.combine(now.date(), t, tzinfo=now.tzinfo)
        if dt <= now:
            dt += timedelta(days=1)
        return dt

    raise ValueError(f"I couldn't understand `{text}`. " + HELP_TEXT)
