"""Day count conventions and business day calendars."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from typing import Iterable


class DayCount(str, Enum):
    ACT_360 = "ACT/360"
    ACT_365F = "ACT/365F"
    ACT_ACT_ISDA = "ACT/ACT"
    THIRTY_360 = "30/360"
    THIRTY_E_360 = "30E/360"


def year_fraction(start: date, end: date, convention: DayCount | str) -> Decimal:
    """Accrual fraction for [start, end)."""
    convention = DayCount(convention)
    if end <= start:
        return Decimal(0)
    if convention == DayCount.ACT_360:
        return Decimal((end - start).days) / Decimal(360)
    if convention == DayCount.ACT_365F:
        return Decimal((end - start).days) / Decimal(365)
    if convention == DayCount.ACT_ACT_ISDA:
        total = Decimal(0)
        cursor = start
        while cursor < end:
            year_end = date(cursor.year + 1, 1, 1)
            segment_end = min(year_end, end)
            days_in_year = 366 if _is_leap(cursor.year) else 365
            total += Decimal((segment_end - cursor).days) / Decimal(days_in_year)
            cursor = segment_end
        return total
    d1, d2 = start.day, end.day
    if convention == DayCount.THIRTY_360:
        if d1 == 31:
            d1 = 30
        if d2 == 31 and d1 == 30:
            d2 = 30
    else:  # 30E/360
        d1 = min(d1, 30)
        d2 = min(d2, 30)
    days = 360 * (end.year - start.year) + 30 * (end.month - start.month) + (d2 - d1)
    return Decimal(days) / Decimal(360)


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


class BusinessDayConvention(str, Enum):
    NONE = "none"
    FOLLOWING = "following"
    MODIFIED_FOLLOWING = "modified_following"
    PRECEDING = "preceding"


class Calendar:
    """Weekend + explicit holiday calendar (holidays are configured per facility)."""

    def __init__(self, holidays: Iterable[date | str] = ()):
        self.holidays = {h if isinstance(h, date) else date.fromisoformat(h) for h in holidays}

    def is_business_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def adjust(self, d: date, convention: BusinessDayConvention | str) -> date:
        convention = BusinessDayConvention(convention)
        if convention == BusinessDayConvention.NONE or self.is_business_day(d):
            return d
        if convention == BusinessDayConvention.PRECEDING:
            return self._roll(d, -1)
        rolled = self._roll(d, 1)
        if convention == BusinessDayConvention.MODIFIED_FOLLOWING and rolled.month != d.month:
            return self._roll(d, -1)
        return rolled

    def add_business_days(self, d: date, n: int) -> date:
        step = 1 if n >= 0 else -1
        remaining = abs(n)
        cursor = d
        while remaining:
            cursor += timedelta(days=step)
            if self.is_business_day(cursor):
                remaining -= 1
        return cursor

    def _roll(self, d: date, step: int) -> date:
        cursor = d
        while not self.is_business_day(cursor):
            cursor += timedelta(days=step)
        return cursor
