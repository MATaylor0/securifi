"""Decimal helpers. All monetary and rate arithmetic uses ``Decimal``; floats never touch money."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, getcontext
from typing import Any, Iterable, Sequence

getcontext().prec = 34

ZERO = Decimal("0")
CENT = Decimal("0.01")
BPS = Decimal("10000")


def D(value: Any, default: Decimal | None = None) -> Decimal:
    """Coerce a value to Decimal. Floats are converted via ``str`` to avoid binary artefacts."""
    if value is None or value == "":
        if default is not None:
            return default
        raise ValueError("cannot convert empty value to Decimal")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("cannot convert bool to Decimal")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    text = str(value).strip().replace(",", "")
    if text.startswith("(") and text.endswith(")"):  # accounting negative
        text = "-" + text[1:-1]
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc


def round_money(value: Decimal, places: Decimal = CENT) -> Decimal:
    return value.quantize(places, rounding=ROUND_HALF_UP)


def bps_to_rate(bps: Any) -> Decimal:
    return D(bps, ZERO) / BPS


def allocate_pro_rata(total: Decimal, weights: Sequence[Decimal], places: Decimal = CENT) -> list[Decimal]:
    """Split ``total`` across ``weights`` so the rounded parts sum exactly to ``total``.

    Uses the largest-remainder method: each share is truncated to ``places`` and the
    leftover units are handed to the shares with the largest remainders.
    """
    total = round_money(total, places)
    if not weights:
        return []
    weight_sum = sum(weights, ZERO)
    if weight_sum == ZERO:
        # Nothing to weight by: split evenly.
        weights = [Decimal(1)] * len(weights)
        weight_sum = Decimal(len(weights))
    raw = [total * w / weight_sum for w in weights]
    floored = [r.quantize(places, rounding="ROUND_DOWN") for r in raw]
    remainder_units = int(((total - sum(floored, ZERO)) / places).to_integral_value())
    order = sorted(range(len(raw)), key=lambda i: (raw[i] - floored[i]), reverse=True)
    step = places if remainder_units >= 0 else -places
    for i in order[: abs(remainder_units)]:
        floored[i] += step
    return floored


def dsum(values: Iterable[Decimal]) -> Decimal:
    return sum(values, ZERO)
