"""Quiet hours (#49): a daily window with no Instagram requests. Pure logic, no I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_WINDOW = re.compile(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})")


@dataclass(frozen=True)
class QuietWindow:
    """Local wall-clock window [start, end). It wraps midnight when end < start."""

    start: time
    end: time

    def contains(self, t: time) -> bool:
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end


def parse_quiet_hours(raw: str) -> QuietWindow | None:
    """"HH:MM-HH:MM" -> window; blank -> None (off). Raises ValueError on anything else,
    including start == end (a window covering the whole day would stop the service)."""
    raw = raw.strip()
    if not raw:
        return None
    m = _WINDOW.fullmatch(raw)
    if not m:
        raise ValueError("must look like 01:00-07:00")
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ValueError("hours must be 00-23 and minutes 00-59")
    start, end = time(h1, m1), time(h2, m2)
    if start == end:
        raise ValueError("start and end must differ (the window would cover the whole day)")
    return QuietWindow(start, end)


def parse_quiet_tz(raw: str) -> tzinfo | None:
    """IANA zone name -> tzinfo; blank -> None (system local time)."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError("must be an IANA zone name such as America/Toronto") from None


def _at(d: date, t: time, tz: tzinfo | None) -> datetime:
    """Wall-clock d+t in tz (None = system local) as an aware datetime. A time skipped by a DST
    jump lands just after the gap; an ambiguous one takes its first occurrence."""
    naive = datetime.combine(d, t)
    if tz is None:
        return naive.astimezone()
    return naive.replace(tzinfo=tz).astimezone(timezone.utc).astimezone(tz)  # normalise a gap time


def next_allowed(now: datetime, window: QuietWindow | None, tz: tzinfo | None = None) -> datetime:
    """The earliest moment >= `now` (aware) at which a check is allowed: `now` itself outside the
    window, else the window's end. DST is handled by working in wall-clock time."""
    if window is None:
        return now
    local = now.astimezone(tz)  # tz None = system local
    if not window.contains(local.time()):
        return now
    # Wrapping window: before midnight the end is tomorrow, after midnight it is today.
    end_day = local.date() + timedelta(days=1) if window.start > window.end and local.time() >= window.start else local.date()
    end = _at(end_day, window.end, tz)
    # A DST gap can shift the end back before `now` (never past a real window); stay monotonic.
    return max(end, now)
