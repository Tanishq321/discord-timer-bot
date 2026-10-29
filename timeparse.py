"""Turn human-friendly text into times, durations and repeat rules.

WHEN examples (case-insensitive):
    in 10m / 2h30m / 1d 4h / in 3 days / 90s
    18:30 / 6:30 pm / 6pm / noon / midnight      (next time it comes around)
    today 9pm / tomorrow 9am / tomorrow at 21:00
    friday 8pm / next friday 8pm / mon 9:00
    in 3 days at noon
    2026-11-02 18:30 / 02-11-2026 6:30 pm / 02/11/2026 18:30
    5 november 8pm / nov 5 20:00 / december 25 at 10am   (via dateparser)

REPEAT examples:
    once, daily, weekly, monthly, hourly, weekdays, weekends,
    every 2h, every 30m, every 3 days, mon,thu, every mon & fri
"""

import calendar
import re
from datetime import datetime, timedelta

try:
    import dateparser  # optional, handles "5 november 8pm" etc.
except ImportError:  # pragma: no cover
    dateparser = None

HELP_TEXT = (
    "Try `in 30m`, `18:30`, `6:30 pm`, `tomorrow 9am`, `friday 8pm`, "
    "`in 3 days at noon`, `5 november 8pm` or `2026-11-02 18:30`."
)
REPEAT_HELP = (
    "Try `once`, `daily`, `weekly`, `monthly`, `hourly`, `weekdays`, `weekends`, "
    "`every 2h`, `every 3 days` or days like `mon,thu`."
)

_UNITS = {
    **dict.fromkeys(["s", "sec", "secs", "second", "seconds"], 1),
    **dict.fromkeys(["m", "min", "mins", "minute", "minutes"], 60),
    **dict.fromkeys(["h", "hr", "hrs", "hour", "hours"], 3600),
    **dict.fromkeys(["d", "day", "days"], 86400),
    **dict.fromkeys(["w", "wk", "wks", "week", "weeks"], 604800),
}
_WEEKDAYS = {
    **dict.fromkeys(["mon", "monday"], 0),
    **dict.fromkeys(["tue", "tues", "tuesday"], 1),
    **dict.fromkeys(["wed", "weds", "wednesday"], 2),
    **dict.fromkeys(["thu", "thur", "thurs", "thursday"], 3),
    **dict.fromkeys(["fri", "friday"], 4),
    **dict.fromkeys(["sat", "saturday"], 5),
    **dict.fromkeys(["sun", "sunday"], 6),
}
_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_TIME_FORMATS = ("%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p", "%H.%M")
_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y")
MIN_REPEAT_SECONDS = 300


# ---------------------------------------------------------------- durations
def parse_duration(text: str):
    """'2h30m', 'in 3 days', '1 hour and 10 minutes' -> seconds, or None if it isn't a duration."""
    s = text.strip().lower()
    s = re.sub(r"^in\s+", "", s)
    s = re.sub(r"\band\b|,", " ", s)
    if not re.fullmatch(r"\s*(?:\d+\s*[a-z]+\s*)+", s):
        return None
    total = 0
    for n, unit in re.findall(r"(\d+)\s*([a-z]+)", s):
        if unit not in _UNITS:
            return None
        total += int(n) * _UNITS[unit]
    return total


def parse_duration_list(text: str) -> list[int]:
    """'1h, 10m' -> [3600, 600] (largest first). Raises ValueError."""
    parts = [p for p in re.split(r"[,;/]|\band\b", text) if p.strip()]
    out = set()
    for p in parts:
        secs = parse_duration(p)
        if not secs:
            raise ValueError(f"`{p.strip()}` isn't a duration. Use things like `1d`, `1h`, `10m`.")
        out.add(secs)
    return sorted(out, reverse=True)


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            n, seconds = divmod(seconds, size)
            parts.append(f"{n}{unit}")
    return " ".join(parts) or "0s"


# ---------------------------------------------------------------- when
def _parse_time_of_day(text: str):
    t = text.strip().upper()
    t = re.sub(r"^AT\s+", "", t)
    if t == "NOON":
        return datetime.strptime("12:00", "%H:%M").time()
    if t == "MIDNIGHT":
        return datetime.strptime("00:00", "%H:%M").time()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(t, fmt).time()
        except ValueError:
            continue
    return None


def _at(day, t, tz):
    return datetime.combine(day, t, tzinfo=tz)


def parse_when(text: str, now: datetime) -> datetime:
    """Return the moment `text` refers to, in now's timezone. Raises ValueError."""
    raw = " ".join(text.strip().split())
    low = raw.lower()
    tz = now.tzinfo
    if not raw:
        raise ValueError("Please tell me when. " + HELP_TEXT)

    # 1) Durations: "in 10m", "2h30m", "in 3 days"
    secs = parse_duration(low)
    if secs is not None:
        if secs <= 0:
            raise ValueError("Duration must be greater than zero.")
        return now + timedelta(seconds=secs)

    # 2) "in 3 days at noon" / "in 2 weeks at 9am"
    m = re.fullmatch(r"in\s+(\d+)\s*(d|days?|w|weeks?)\s+(?:at\s+)?(.+)", low)
    if m:
        t = _parse_time_of_day(m.group(3))
        if t is None:
            raise ValueError(f"Couldn't read the time `{m.group(3)}`. " + HELP_TEXT)
        days = int(m.group(1)) * (7 if m.group(2).startswith("w") else 1)
        return _at((now + timedelta(days=days)).date(), t, tz)

    # 3) "today 9pm" / "tomorrow at 9am"
    m = re.fullmatch(r"(today|tonight|tomorrow|tmrw|tmr)\s+(?:at\s+)?(.+)", low)
    if m:
        t = _parse_time_of_day(m.group(2))
        if t is None:
            raise ValueError(f"Couldn't read the time `{m.group(2)}`. " + HELP_TEXT)
        day = now.date() if m.group(1) in ("today", "tonight") else (now + timedelta(days=1)).date()
        return _at(day, t, tz)

    # 4) "friday 8pm" / "next friday at 8pm"
    m = re.fullmatch(r"(next\s+|this\s+)?([a-z]+)\s+(?:at\s+)?(.+)", low)
    if m and m.group(2) in _WEEKDAYS:
        t = _parse_time_of_day(m.group(3))
        if t is None:
            raise ValueError(f"Couldn't read the time `{m.group(3)}`. " + HELP_TEXT)
        target = _WEEKDAYS[m.group(2)]
        ahead = (target - now.weekday()) % 7
        dt = _at((now + timedelta(days=ahead)).date(), t, tz)
        if dt <= now or (m.group(1) or "").startswith("next") and ahead == 0:
            dt += timedelta(days=7)
        return dt

    # 5) Full date + time in numeric formats
    up = raw.upper()
    for dfmt in _DATE_FORMATS:
        for tfmt in _TIME_FORMATS:
            try:
                return datetime.strptime(up, f"{dfmt} {tfmt}").replace(tzinfo=tz)
            except ValueError:
                continue

    # 6) Time only -> next occurrence
    t = _parse_time_of_day(raw)
    if t is not None:
        dt = _at(now.date(), t, tz)
        return dt if dt > now else dt + timedelta(days=1)

    # 7) Anything else ("5 november 8pm", "dec 25 at 10am") -> dateparser
    if dateparser is not None and re.search(r"\d", raw):
        dt = dateparser.parse(raw, settings={
            "TIMEZONE": getattr(tz, "key", "UTC"),
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now.replace(tzinfo=None),
        })
        if dt is not None:
            return dt.astimezone(tz)

    raise ValueError(f"I couldn't understand `{text}`. " + HELP_TEXT)


# ---------------------------------------------------------------- repeats
def _parse_days(text: str):
    words = [w for w in re.split(r"[\s,&/+]+|\band\b", text) if w and w not in ("every", "on", "and")]
    if not words or any(w not in _WEEKDAYS for w in words):
        return None
    return sorted({_WEEKDAYS[w] for w in words})


def parse_repeat(text: str | None) -> str:
    """Return a repeat spec: once | daily | weekly | monthly | days:0,3 | every:<seconds>."""
    s = " ".join((text or "").strip().lower().split())
    if s in ("", "once", "none", "no", "never", "one time", "one-time"):
        return "once"
    if s in ("daily", "every day", "everyday", "each day"):
        return "daily"
    if s in ("weekly", "every week", "each week"):
        return "weekly"
    if s in ("monthly", "every month", "each month"):
        return "monthly"
    if s in ("hourly", "every hour"):
        return "every:3600"
    if s in ("weekdays", "every weekday", "mon-fri", "monday to friday"):
        return "days:0,1,2,3,4"
    if s in ("weekends", "every weekend", "sat,sun", "sat-sun"):
        return "days:5,6"

    days = _parse_days(s)
    if days:
        if len(days) == 7:
            return "daily"
        return "days:" + ",".join(map(str, days))

    m = re.fullmatch(r"every\s+(.+)", s)
    if m:
        secs = parse_duration(m.group(1)) or parse_duration("1 " + m.group(1))  # "every day" handled; "every 2h"
        if secs:
            if secs < MIN_REPEAT_SECONDS:
                raise ValueError("Repeats must be at least 5 minutes apart.")
            if secs == 86400:
                return "daily"
            if secs == 604800:
                return "weekly"
            return f"every:{secs}"

    raise ValueError(f"I don't understand the repeat `{text}`. " + REPEAT_HELP)


def describe_repeat(spec: str) -> str:
    if spec in ("once", "", None):
        return "once"
    if spec == "daily":
        return "every day"
    if spec == "weekly":
        return "every week"
    if spec == "monthly":
        return "every month"
    if spec.startswith("days:"):
        days = [int(d) for d in spec[5:].split(",")]
        if days == [0, 1, 2, 3, 4]:
            return "every weekday"
        if days == [5, 6]:
            return "every weekend day"
        return "every " + ", ".join(_DAY_NAMES[d] for d in days)
    if spec.startswith("every:"):
        return "every " + format_duration(int(spec[6:]))
    return spec


def align_first(dt: datetime, spec: str) -> datetime:
    """For day-of-week repeats, move the first run onto an allowed day."""
    if spec.startswith("days:"):
        days = {int(d) for d in spec[5:].split(",")}
        while dt.weekday() not in days:
            dt += timedelta(days=1)
    return dt


def next_occurrence(fire_at: int, spec: str, tz, now: int, day_of_month: int | None = None):
    """Next run strictly after `now` (wall-clock aware), or None for one-off timers."""
    if spec in ("once", "", None):
        return None
    if spec.startswith("every:"):
        step = int(spec[6:])
        k = max(1, (now - fire_at) // step + 1)
        return fire_at + k * step

    dt = datetime.fromtimestamp(fire_at, tz)
    if spec in ("daily", "weekly"):
        step = timedelta(days=1 if spec == "daily" else 7)
        dt += step
        while dt.timestamp() <= now:
            dt += step
        return int(dt.timestamp())

    if spec.startswith("days:"):
        days = {int(d) for d in spec[5:].split(",")}
        dt += timedelta(days=1)
        while dt.weekday() not in days or dt.timestamp() <= now:
            dt += timedelta(days=1)
        return int(dt.timestamp())

    if spec == "monthly":
        dom = day_of_month or dt.day
        y, m = dt.year, dt.month
        while True:
            m += 1
            if m > 12:
                y, m = y + 1, 1
            d = min(dom, calendar.monthrange(y, m)[1])
            cand = dt.replace(year=y, month=m, day=d)
            if cand.timestamp() > now:
                return int(cand.timestamp())

    return None
