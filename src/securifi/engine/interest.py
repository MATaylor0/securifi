"""Interest and fee accrual.

Accrual is computed on the actual balance outstanding on each day of the interest period,
so drawdowns and repayments part way through a period (common in warehouses) are accrued
correctly. The period is split into segments at every balance change and each segment is
accrued with the facility's day count convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .daycount import DayCount, year_fraction
from .money import ZERO, D, allocate_pro_rata, bps_to_rate, round_money


@dataclass(frozen=True)
class Movement:
    effective_date: date
    amount: Decimal  # positive increases principal outstanding


def balance_segments(movements: Iterable[Movement], start: date, end: date) -> list[tuple[date, date, Decimal]]:
    """Return [(seg_start, seg_end, balance)] covering [start, end).

    Movements dated on or before ``start`` form the opening balance; a movement dated
    ``d`` inside the period changes the balance from ``d`` onward.
    """
    moves = sorted(movements, key=lambda m: m.effective_date)
    balance = sum((m.amount for m in moves if m.effective_date <= start), ZERO)
    inside = [m for m in moves if start < m.effective_date < end]
    segments: list[tuple[date, date, Decimal]] = []
    cursor = start
    for m in inside:
        if m.effective_date > cursor:
            segments.append((cursor, m.effective_date, balance))
            cursor = m.effective_date
        balance += m.amount
    if cursor < end:
        segments.append((cursor, end, balance))
    return segments


def accrue(segments: Sequence[tuple[date, date, Decimal]], rate: Decimal, day_count: DayCount | str) -> Decimal:
    return sum((bal * rate * year_fraction(s, e, day_count) for s, e, bal in segments), ZERO)


def average_balance(segments: Sequence[tuple[date, date, Decimal]]) -> Decimal:
    days = sum((e - s).days for s, e, _ in segments)
    if not days:
        return ZERO
    return round_money(sum((bal * (e - s).days for s, e, bal in segments), ZERO) / days)


def coupon_rate(note_class: Mapping[str, Any], base_rate: Decimal | None) -> dict[str, Decimal]:
    if note_class.get("rate_type", "floating") == "fixed":
        fixed = D(note_class.get("fixed_rate"), ZERO)
        return {"base_rate": ZERO, "margin": ZERO, "floor_applied": False, "all_in_rate": fixed}
    if base_rate is None:
        raise ValueError(f"no base rate fixing for class {note_class.get('name')}")
    floor = note_class.get("base_rate_floor")
    effective_base = base_rate
    floor_applied = False
    if floor is not None and base_rate < D(floor):
        effective_base, floor_applied = D(floor), True
    margin = bps_to_rate(note_class.get("margin_bps"))
    return {"base_rate": base_rate, "effective_base_rate": effective_base, "margin": margin,
            "floor_applied": floor_applied, "all_in_rate": effective_base + margin}


def class_interest(
    note_class: Mapping[str, Any],
    holder_movements: Mapping[str, Sequence[Movement]],
    start: date,
    end: date,
    base_rate: Decimal | None,
) -> dict[str, Any]:
    """Interest due for one note class, split across noteholders."""
    day_count = note_class.get("day_count", DayCount.ACT_365F.value)
    rate = coupon_rate(note_class, base_rate)
    all_moves = [m for moves in holder_movements.values() for m in moves]
    class_segments = balance_segments(all_moves, start, end)
    if note_class.get("is_residual"):
        total = ZERO
    else:
        total = round_money(accrue(class_segments, rate["all_in_rate"], day_count))
    holder_raw = {
        h: accrue(balance_segments(m, start, end), rate["all_in_rate"], day_count) for h, m in holder_movements.items()
    }
    holders = sorted(holder_raw)
    shares = allocate_pro_rata(total, [holder_raw[h] for h in holders]) if holders else []
    return {
        "class": note_class["name"],
        "day_count": day_count,
        "days": (end - start).days,
        "opening_balance": class_segments[0][2] if class_segments else ZERO,
        "average_balance": average_balance(class_segments),
        **rate,
        "interest": total,
        "by_holder": dict(zip(holders, shares)),
        "segments": [{"from": s, "to": e, "balance": b} for s, e, b in class_segments],
    }


def undrawn_fee(
    commitment: Decimal,
    movements: Sequence[Movement],
    start: date,
    end: date,
    rate_bps: Decimal,
    day_count: DayCount | str,
) -> Decimal:
    segments = [(s, e, max(ZERO, commitment - bal)) for s, e, bal in balance_segments(movements, start, end)]
    return round_money(accrue(segments, bps_to_rate(rate_bps), day_count))


def fee_amount(
    fee: Mapping[str, Any],
    start: date,
    end: date,
    *,
    basis_segments: Sequence[tuple[date, date, Decimal]] | None = None,
    basis_amount: Decimal | None = None,
) -> dict[str, Any]:
    """Fee due for a period.

    ``calculation``: ``percent_per_annum`` (rate_bps on a basis), ``fixed_per_annum`` or
    ``fixed_per_period``. Tax (e.g. GST) is added on top when ``tax_rate`` is set.
    """
    calc = fee.get("calculation", "percent_per_annum")
    day_count = fee.get("day_count", DayCount.ACT_365F.value)
    if calc == "fixed_per_period":
        net = D(fee.get("amount"), ZERO)
    elif calc == "fixed_per_annum":
        net = D(fee.get("amount"), ZERO) * year_fraction(start, end, day_count)
    elif calc == "percent_per_annum":
        rate = bps_to_rate(fee.get("rate_bps"))
        if basis_segments is not None:
            net = accrue(basis_segments, rate, day_count)
        else:
            net = D(basis_amount, ZERO) * rate * year_fraction(start, end, day_count)
    else:
        raise ValueError(f"unknown fee calculation '{calc}'")
    net = round_money(net)
    minimum = fee.get("minimum_amount")
    if minimum is not None and net < D(minimum):
        net = round_money(D(minimum))
    tax = round_money(net * D(fee.get("tax_rate"), ZERO))
    return {"code": fee["code"], "net": net, "tax": tax, "amount": net + tax}
