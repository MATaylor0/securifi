"""Configurable priority of payments (waterfall) engine.

A waterfall definition is JSON so each facility can mirror its own legal documents::

    {
      "order": ["income", "principal"],
      "waterfalls": {
        "income": [
          {"type": "fee", "fee": "TRUSTEE", "label": "Trustee fee"},
          {"type": "fee", "fee": "MANAGER"},
          {"type": "interest", "classes": ["A"]},
          {"type": "interest", "classes": ["B"]},
          {"type": "reserve", "account": "RESERVE", "target": "class_balance['A'] * 0.01"},
          {"type": "transfer", "to": "principal", "when": "events['AMORTISATION_EVENT']"},
          {"type": "residual", "payee": "Residual income unitholder"}
        ],
        "principal": [
          {"type": "principal", "classes": ["A", "B"], "mode": "sequential",
           "limit": "borrowing_base_deficiency", "when": "not events['AMORTISATION_EVENT']"},
          {"type": "principal", "classes": ["A", "B"], "mode": "sequential"},
          {"type": "residual", "payee": "Seller"}
        ]
      }
    }

Step types: ``fee``, ``interest``, ``principal``, ``reserve`` (fund to target),
``reserve_draw`` (add reserve funds to available), ``amount`` (pay an expression amount to a
payee), ``transfer`` (move funds to a later waterfall) and ``residual``. Any step can carry a
``when`` condition. Expressions can read ``available``, ``fees_due``, ``interest_due``,
``class_balance``, ``reserve_balance``, ``events`` and any facility variables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from .expressions import evaluate
from .money import ZERO, D, allocate_pro_rata, round_money

STEP_TYPES = ("fee", "interest", "principal", "reserve", "reserve_draw", "amount", "transfer", "residual")


class WaterfallError(ValueError):
    pass


@dataclass
class WaterfallState:
    available: dict[str, Decimal]
    fees_due: dict[str, Decimal]
    interest_due: dict[str, Decimal]
    class_balance: dict[str, Decimal]
    reserve_balance: dict[str, Decimal]
    variables: dict[str, Any] = field(default_factory=dict)
    lines: list[dict[str, Any]] = field(default_factory=list)
    fees_paid: dict[str, Decimal] = field(default_factory=dict)
    interest_paid: dict[str, Decimal] = field(default_factory=dict)
    principal_paid: dict[str, Decimal] = field(default_factory=dict)
    reserve_movements: dict[str, Decimal] = field(default_factory=dict)
    other_payments: list[dict[str, Any]] = field(default_factory=list)

    def scope(self, waterfall: str) -> dict[str, Any]:
        return {
            **self.variables,
            "available": self.available.get(waterfall, ZERO),
            "fees_due": dict(self.fees_due),
            "interest_due": dict(self.interest_due),
            "class_balance": dict(self.class_balance),
            "reserve_balance": dict(self.reserve_balance),
            "fees_due_total": sum(self.fees_due.values(), ZERO),
            "interest_due_total": sum(self.interest_due.values(), ZERO),
        }


def validate_definition(definition: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    order = definition.get("order") or list((definition.get("waterfalls") or {}).keys())
    waterfalls = definition.get("waterfalls") or {}
    if not waterfalls:
        problems.append("definition has no waterfalls")
    for name in order:
        if name not in waterfalls:
            problems.append(f"order references unknown waterfall '{name}'")
    for name, steps in waterfalls.items():
        for i, step in enumerate(steps, start=1):
            t = step.get("type")
            where = f"{name} step {i}"
            if t not in STEP_TYPES:
                problems.append(f"{where}: unknown type '{t}'")
            if t == "fee" and not step.get("fee"):
                problems.append(f"{where}: fee step needs 'fee'")
            if t in ("interest", "principal") and not (step.get("classes") or step.get("class")):
                problems.append(f"{where}: needs 'classes'")
            if t in ("reserve", "reserve_draw") and not step.get("account"):
                problems.append(f"{where}: needs 'account'")
            if t == "transfer":
                target = step.get("to")
                if target not in waterfalls:
                    problems.append(f"{where}: transfer to unknown waterfall '{target}'")
                elif name in order and target in order and order.index(target) <= order.index(name):
                    problems.append(f"{where}: transfers may only move funds to a later waterfall")
            if t == "amount" and not step.get("amount"):
                problems.append(f"{where}: amount step needs 'amount'")
    return problems


def _expr_amount(expr: Any, state: WaterfallState, waterfall: str) -> Decimal:
    if expr is None:
        return ZERO
    if isinstance(expr, (int, float, Decimal)):
        return round_money(D(expr))
    return round_money(max(ZERO, D(evaluate(str(expr), state.scope(waterfall)))))


def run_waterfall(
    definition: Mapping[str, Any],
    *,
    available: Mapping[str, Decimal],
    fees_due: Mapping[str, Decimal],
    interest_due: Mapping[str, Decimal],
    class_balance: Mapping[str, Decimal],
    reserve_balance: Mapping[str, Decimal] | None = None,
    variables: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    problems = validate_definition(definition)
    if problems:
        raise WaterfallError("; ".join(problems))
    waterfalls = definition["waterfalls"]
    order = definition.get("order") or list(waterfalls.keys())
    state = WaterfallState(
        available={k: round_money(D(v)) for k, v in available.items()},
        fees_due={k: round_money(D(v)) for k, v in fees_due.items()},
        interest_due={k: round_money(D(v)) for k, v in interest_due.items()},
        class_balance={k: round_money(D(v)) for k, v in class_balance.items()},
        reserve_balance={k: round_money(D(v)) for k, v in (reserve_balance or {}).items()},
        variables=dict(variables or {}),
    )
    opening_available = dict(state.available)
    for name in order:
        state.available.setdefault(name, ZERO)
        for number, step in enumerate(waterfalls[name], start=1):
            _run_step(state, name, number, step)
    unpaid_fees = {k: v for k, v in state.fees_due.items() if v > 0}
    unpaid_interest = {k: v for k, v in state.interest_due.items() if v > 0}
    return {
        "available_funds": opening_available,
        "lines": state.lines,
        "fees_paid": state.fees_paid,
        "interest_paid": state.interest_paid,
        "principal_paid": state.principal_paid,
        "reserve_movements": state.reserve_movements,
        "other_payments": state.other_payments,
        "unpaid_fees": unpaid_fees,
        "unpaid_interest": unpaid_interest,
        "closing_class_balance": state.class_balance,
        "closing_reserve_balance": state.reserve_balance,
        "undistributed": {k: v for k, v in state.available.items() if v != 0},
    }


def _record(state: WaterfallState, waterfall: str, number: int, step: Mapping[str, Any], target: str,
            due: Decimal, paid: Decimal, before: Decimal, skipped: str | None = None) -> None:
    state.lines.append({
        "waterfall": waterfall,
        "step": number,
        "type": step["type"],
        "label": _line_label(step, target),
        "target": target,
        "due": due,
        "paid": paid,
        "shortfall": max(ZERO, due - paid),
        "available_before": before,
        "available_after": state.available[waterfall],
        "skipped": skipped,
        "clause_ref": step.get("clause_ref"),
    })


def _line_label(step: Mapping[str, Any], target: str) -> str:
    label = step.get("label")
    if not label:
        return _default_label(step, target)
    if len(_classes(step)) > 1:
        return f"{label} (Class {target})"
    return label


def _default_label(step: Mapping[str, Any], target: str) -> str:
    t = step["type"]
    return {
        "fee": f"Fee: {target}",
        "interest": f"Interest: Class {target}",
        "principal": f"Principal: Class {target}",
        "reserve": f"Fund reserve: {target}",
        "reserve_draw": f"Draw reserve: {target}",
        "amount": f"Payment: {target}",
        "transfer": f"Transfer to {target}",
        "residual": f"Residual: {target}",
    }[t]


def _pay(state: WaterfallState, waterfall: str, amount: Decimal) -> Decimal:
    paid = min(state.available[waterfall], max(ZERO, amount))
    state.available[waterfall] -= paid
    return paid


def _run_step(state: WaterfallState, waterfall: str, number: int, step: Mapping[str, Any]) -> None:
    t = step["type"]
    before = state.available[waterfall]
    if step.get("when") and not evaluate(step["when"], state.scope(waterfall)):
        target = step.get("fee") or step.get("account") or step.get("to") or step.get("payee") or ",".join(_classes(step)) or t
        _record(state, waterfall, number, step, target, ZERO, ZERO, before, skipped="condition not met")
        return
    if t == "fee":
        code = step["fee"]
        due = state.fees_due.get(code, ZERO)
        paid = _pay(state, waterfall, due)
        state.fees_due[code] = due - paid
        state.fees_paid[code] = state.fees_paid.get(code, ZERO) + paid
        _record(state, waterfall, number, step, code, due, paid, before)
    elif t == "interest":
        classes = _classes(step)
        dues = [state.interest_due.get(c, ZERO) for c in classes]
        total_due = sum(dues, ZERO)
        pot = min(state.available[waterfall], total_due)
        shares = allocate_pro_rata(pot, dues) if total_due else [ZERO] * len(classes)
        for c, due, share in zip(classes, dues, shares):
            line_before = state.available[waterfall]
            paid = _pay(state, waterfall, share)
            state.interest_due[c] = due - paid
            state.interest_paid[c] = state.interest_paid.get(c, ZERO) + paid
            _record(state, waterfall, number, step, c, due, paid, line_before)
    elif t == "principal":
        classes = _classes(step)
        limit = _expr_amount(step["limit"], state, waterfall) if step.get("limit") is not None else None
        budget = state.available[waterfall] if limit is None else min(limit, state.available[waterfall])
        if step.get("mode", "sequential") == "pro_rata":
            balances = [state.class_balance.get(c, ZERO) for c in classes]
            pot = min(budget, sum(balances, ZERO))
            allocations = allocate_pro_rata(pot, balances) if pot else [ZERO] * len(classes)
        else:
            allocations = []
            remaining = budget
            for c in classes:
                take = min(remaining, state.class_balance.get(c, ZERO))
                allocations.append(take)
                remaining -= take
        for c, alloc in zip(classes, allocations):
            line_before = state.available[waterfall]
            balance = state.class_balance.get(c, ZERO)
            paid = _pay(state, waterfall, alloc)
            state.class_balance[c] = balance - paid
            state.principal_paid[c] = state.principal_paid.get(c, ZERO) + paid
            _record(state, waterfall, number, step, c, min(balance, limit) if limit is not None else balance, paid, line_before)
    elif t == "reserve":
        account = step["account"]
        target = _expr_amount(step.get("target"), state, waterfall)
        current = state.reserve_balance.get(account, ZERO)
        due = max(ZERO, target - current)
        paid = _pay(state, waterfall, due)
        state.reserve_balance[account] = current + paid
        state.reserve_movements[account] = state.reserve_movements.get(account, ZERO) + paid
        _record(state, waterfall, number, step, account, due, paid, before)
    elif t == "reserve_draw":
        account = step["account"]
        current = state.reserve_balance.get(account, ZERO)
        wanted = _expr_amount(step.get("amount"), state, waterfall) if step.get("amount") is not None else current
        drawn = min(current, wanted)
        state.reserve_balance[account] = current - drawn
        state.reserve_movements[account] = state.reserve_movements.get(account, ZERO) - drawn
        state.available[waterfall] += drawn
        _record(state, waterfall, number, step, account, wanted, drawn, before)
    elif t == "amount":
        payee = step.get("payee", "payee")
        due = _expr_amount(step["amount"], state, waterfall)
        paid = _pay(state, waterfall, due)
        state.other_payments.append({"payee": payee, "label": step.get("label"), "amount": paid, "waterfall": waterfall})
        _record(state, waterfall, number, step, payee, due, paid, before)
    elif t == "transfer":
        target = step["to"]
        amount = _expr_amount(step["amount"], state, waterfall) if step.get("amount") is not None else state.available[waterfall]
        moved = _pay(state, waterfall, amount)
        state.available[target] = state.available.get(target, ZERO) + moved
        _record(state, waterfall, number, step, target, amount, moved, before)
    elif t == "residual":
        payee = step.get("payee", "Residual beneficiary")
        paid = _pay(state, waterfall, state.available[waterfall])
        state.other_payments.append({"payee": payee, "label": step.get("label") or "Residual", "amount": paid, "waterfall": waterfall, "residual": True})
        _record(state, waterfall, number, step, payee, paid, paid, before)


def _classes(step: Mapping[str, Any]) -> list[str]:
    if step.get("classes"):
        return list(step["classes"])
    if step.get("class"):
        return [step["class"]]
    return []
