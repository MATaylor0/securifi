"""Bank reconciliation: match bank statement lines to expected cash flows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Sequence


@dataclass(frozen=True)
class Expected:
    id: str
    date: date
    amount: Decimal
    reference: str | None = None


@dataclass(frozen=True)
class Actual:
    id: str
    date: date
    amount: Decimal
    reference: str | None = None
    description: str | None = None


def auto_match(expected: Sequence[Expected], actual: Sequence[Actual], date_tolerance_days: int = 3) -> list[tuple[str, str, str]]:
    """Return [(expected_id, actual_id, rule)] one-to-one matches.

    Pass 1 matches equal amounts whose reference appears in the bank reference/narrative.
    Pass 2 matches equal amounts within the date tolerance, closest date first.
    """
    used_actual: set[str] = set()
    used_expected: set[str] = set()
    matches: list[tuple[str, str, str]] = []

    def text(a: Actual) -> str:
        return f"{a.reference or ''} {a.description or ''}".lower()

    for e in expected:
        if not e.reference:
            continue
        for a in actual:
            if a.id in used_actual or a.amount != e.amount:
                continue
            if e.reference.lower() in text(a) and abs((a.date - e.date).days) <= max(date_tolerance_days, 10):
                matches.append((e.id, a.id, "amount+reference"))
                used_actual.add(a.id)
                used_expected.add(e.id)
                break

    for e in expected:
        if e.id in used_expected:
            continue
        candidates = [
            a for a in actual
            if a.id not in used_actual and a.amount == e.amount and abs((a.date - e.date).days) <= date_tolerance_days
        ]
        if candidates:
            best = min(candidates, key=lambda a: abs((a.date - e.date).days))
            matches.append((e.id, best.id, "amount+date"))
            used_actual.add(best.id)
            used_expected.add(e.id)
    return matches
