"""Generation of collection / interest / payment period schedules."""

from __future__ import annotations

import calendar as _cal
from dataclasses import dataclass
from datetime import date, timedelta

from .daycount import BusinessDayConvention, Calendar


def add_months(d: date, months: int, day: int | None = None) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    target_day = day if day is not None else d.day
    last = _cal.monthrange(year, month)[1]
    return date(year, month, min(target_day, last))


@dataclass(frozen=True)
class PeriodDates:
    sequence: int
    collection_start: date
    collection_end: date  # inclusive
    determination_date: date
    payment_date: date
    interest_start: date
    interest_end: date  # exclusive (accrual stops the day before payment)


def generate_schedule(
    *,
    first_collection_start: date,
    count: int,
    frequency_months: int = 1,
    payment_day: int = 15,
    payment_lag_months: int = 1,
    determination_offset_business_days: int = 5,
    first_interest_start: date | None = None,
    business_day_convention: BusinessDayConvention | str = BusinessDayConvention.MODIFIED_FOLLOWING,
    calendar: Calendar | None = None,
    start_sequence: int = 1,
) -> list[PeriodDates]:
    """Build ``count`` consecutive periods.

    Collection periods run for ``frequency_months`` from ``first_collection_start``.
    Each payment date falls on ``payment_day`` of the month ``payment_lag_months`` after
    the collection period's last month, adjusted for business days. Interest accrues from
    the previous payment date (or ``first_interest_start``) up to the payment date.
    """
    if count < 1:
        raise ValueError("count must be at least 1")
    if frequency_months < 1:
        raise ValueError("frequency_months must be at least 1")
    cal = calendar or Calendar()
    periods: list[PeriodDates] = []
    interest_start = first_interest_start or first_collection_start
    cstart = first_collection_start
    for i in range(count):
        next_start = add_months(cstart, frequency_months)
        cend = next_start - timedelta(days=1)
        unadjusted_payment = add_months(date(cend.year, cend.month, 1), payment_lag_months, payment_day)
        payment = cal.adjust(unadjusted_payment, business_day_convention)
        determination = cal.add_business_days(payment, -determination_offset_business_days)
        periods.append(
            PeriodDates(
                sequence=start_sequence + i,
                collection_start=cstart,
                collection_end=cend,
                determination_date=determination,
                payment_date=payment,
                interest_start=interest_start,
                interest_end=payment,
            )
        )
        interest_start = payment
        cstart = next_start
    return periods
