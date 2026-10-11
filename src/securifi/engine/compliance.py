"""Eligibility criteria, portfolio parameters and borrowing base calculation.

Pure functions over plain data so the same logic is used by every entry point and can be
unit tested without a database.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .expressions import ExpressionError, MissingField, evaluate
from .money import ZERO, D, dsum, round_money

TEST_TYPES = {
    "concentration": "Share of the pool in each group (e.g. property_type, borrower_group, property_state) must not exceed a limit",
    "filtered_concentration": "Share of the pool matching a filter (e.g. construction loans) must be within a limit",
    "top_n_concentration": "Share of the pool in the N largest groups (e.g. top 3 sponsors) must not exceed a limit",
    "largest_exposure": "Largest loan or group balance must not exceed an absolute amount or pool share",
    "weighted_average": "Balance-weighted average of a field (e.g. ltv, icr) must be within a limit",
    "count": "Number of loans or distinct groups must be within a limit (diversity tests)",
    "aggregate": "Sum of a field over (optionally filtered) loans must be within a limit",
}


@dataclass
class LoanResult:
    loan_id: str
    balance: Decimal
    eligible: bool
    failures: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "loan_id": self.loan_id,
            "balance": self.balance,
            "eligible": self.eligible,
            "failures": self.failures,
            "warnings": self.warnings,
        }


@dataclass
class TestResult:
    code: str
    name: str
    test_type: str
    treatment: str
    passed: bool
    value: Any
    limit: Any
    excess_amount: Decimal = ZERO
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    event_code: str | None = None
    clause_ref: str | None = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "test_type": self.test_type,
            "treatment": self.treatment,
            "passed": self.passed,
            "value": self.value,
            "limit": self.limit,
            "excess_amount": self.excess_amount,
            "details": self.details,
            "error": self.error,
            "event_code": self.event_code,
            "clause_ref": self.clause_ref,
        }


def _matches(expression: str | None, loan: Mapping[str, Any]) -> bool:
    if not expression:
        return True
    try:
        return bool(evaluate(expression, loan))
    except MissingField:
        return False


def check_eligibility(loans: Sequence[Mapping[str, Any]], criteria: Sequence[Mapping[str, Any]]) -> list[LoanResult]:
    results: list[LoanResult] = []
    for loan in loans:
        res = LoanResult(loan_id=str(loan.get("loan_id")), balance=D(loan.get("current_balance"), ZERO), eligible=True)
        for c in criteria:
            entry = {"code": c["code"], "name": c.get("name"), "clause_ref": c.get("clause_ref")}
            try:
                if c.get("applies_when") and not evaluate(c["applies_when"], loan):
                    continue
                passed = bool(evaluate(c["expression"], loan))
                reason = None if passed else "criterion not satisfied"
            except MissingField as exc:
                passed, reason = False, f"data missing: {exc.name}"
            except ExpressionError as exc:
                passed, reason = False, f"rule error: {exc}"
            if passed:
                continue
            entry["reason"] = reason
            if c.get("severity", "exclude") == "warning":
                res.warnings.append(entry)
            else:
                res.failures.append(entry)
                res.eligible = False
        results.append(res)
    return results


def _measure(loan: Mapping[str, Any], measure: str) -> Decimal:
    value = loan.get(measure)
    return D(value, ZERO) if value is not None else ZERO


def _compare(value: Decimal, cfg: Mapping[str, Any]) -> tuple[bool, Any]:
    ok = True
    limit: dict[str, Any] = {}
    if cfg.get("max") is not None:
        limit["max"] = D(cfg["max"])
        ok = ok and value <= limit["max"]
    if cfg.get("min") is not None:
        limit["min"] = D(cfg["min"])
        ok = ok and value >= limit["min"]
    return ok, limit


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    return (numerator / denominator).quantize(Decimal("0.000001")) if denominator else ZERO


def run_portfolio_test(test: Mapping[str, Any], pool: Sequence[Mapping[str, Any]], all_loans: Sequence[Mapping[str, Any]]) -> TestResult:
    cfg = dict(test.get("config") or {})
    ttype = test["test_type"]
    base = TestResult(
        code=test["code"], name=test.get("name", test["code"]), test_type=ttype,
        treatment=test.get("treatment", "limit"), passed=True, value=None, limit=None,
        event_code=test.get("event_code"), clause_ref=test.get("clause_ref"),
    )
    loans = all_loans if cfg.get("basis") == "total" else pool
    measure = cfg.get("measure", "current_balance")
    denominator = dsum(_measure(l, measure) for l in loans)
    try:
        if ttype == "concentration":
            group_by = cfg["group_by"]
            default_limit = D(cfg["max"]) if cfg.get("max") is not None else None
            overrides = {k: D(v) for k, v in (cfg.get("limits") or {}).items()}
            groups: dict[str, Decimal] = defaultdict(lambda: ZERO)
            for loan in loans:
                if _matches(cfg.get("filter"), loan):
                    groups[str(loan.get(group_by) if loan.get(group_by) is not None else "(blank)")] += _measure(loan, measure)
            breakdown = []
            worst = ZERO
            for group, amount in sorted(groups.items(), key=lambda kv: kv[1], reverse=True):
                share = _ratio(amount, denominator)
                limit = overrides.get(group, default_limit)
                excess = ZERO
                breach = False
                if limit is not None and share > limit:
                    breach = True
                    excess = round_money(amount - limit * denominator)
                    base.excess_amount += excess
                worst = max(worst, share)
                breakdown.append({"group": group, "amount": amount, "share": share, "limit": limit, "breach": breach, "excess": excess})
            base.passed = not any(b["breach"] for b in breakdown)
            base.value = worst
            base.limit = {"max": default_limit, "overrides": overrides} if overrides else {"max": default_limit}
            base.details = {"group_by": group_by, "groups": breakdown, "denominator": denominator}
        elif ttype == "filtered_concentration":
            amount = dsum(_measure(l, measure) for l in loans if _matches(cfg["filter"], l))
            share = _ratio(amount, denominator)
            base.passed, base.limit = _compare(share, cfg)
            base.value = share
            if "max" in base.limit and share > base.limit["max"]:
                base.excess_amount = round_money(amount - base.limit["max"] * denominator)
            base.details = {"filter": cfg["filter"], "amount": amount, "denominator": denominator}
        elif ttype == "top_n_concentration":
            group_by, n = cfg["group_by"], int(cfg.get("n", 1))
            groups = defaultdict(lambda: ZERO)
            for loan in loans:
                groups[str(loan.get(group_by))] += _measure(loan, measure)
            top = sorted(groups.items(), key=lambda kv: kv[1], reverse=True)[:n]
            amount = dsum(v for _, v in top)
            share = _ratio(amount, denominator)
            base.passed, base.limit = _compare(share, cfg)
            base.value = share
            if "max" in base.limit and share > base.limit["max"]:
                base.excess_amount = round_money(amount - base.limit["max"] * denominator)
            base.details = {"top": [{"group": g, "amount": v, "share": _ratio(v, denominator)} for g, v in top], "denominator": denominator}
        elif ttype == "largest_exposure":
            group_by = cfg.get("group_by", "loan_id")
            groups = defaultdict(lambda: ZERO)
            for loan in loans:
                groups[str(loan.get(group_by))] += _measure(loan, measure)
            name, amount = max(groups.items(), key=lambda kv: kv[1]) if groups else ("", ZERO)
            if cfg.get("max_pct") is not None:
                share = _ratio(amount, denominator)
                limit_amount = D(cfg["max_pct"]) * denominator
                base.value, base.limit = share, {"max": D(cfg["max_pct"])}
            else:
                limit_amount = D(cfg["max"])
                base.value, base.limit = amount, {"max": limit_amount}
            base.passed = amount <= limit_amount
            base.excess_amount = dsum(round_money(v - limit_amount) for v in groups.values() if v > limit_amount)
            base.details = {"group_by": group_by, "largest": name, "amount": amount}
        elif ttype == "weighted_average":
            fld = cfg["field"]
            weight = cfg.get("weight", "current_balance")
            num, den, missing = ZERO, ZERO, []
            for loan in loans:
                if not _matches(cfg.get("filter"), loan):
                    continue
                if loan.get(fld) is None:
                    missing.append(loan.get("loan_id"))
                    continue
                w = _measure(loan, weight)
                num += D(loan[fld]) * w
                den += w
            value = (num / den).quantize(Decimal("0.0001")) if den else ZERO
            base.passed, base.limit = _compare(value, cfg)
            base.value = value
            base.details = {"field": fld, "weight": weight, "loans_missing_field": missing}
        elif ttype == "count":
            subset = [l for l in loans if _matches(cfg.get("filter"), l)]
            distinct = cfg.get("distinct")
            value = Decimal(len({str(l.get(distinct)) for l in subset}) if distinct else len(subset))
            base.passed, base.limit = _compare(value, cfg)
            base.value = value
            base.details = {"distinct": distinct}
        elif ttype == "aggregate":
            fld = cfg.get("field", "current_balance")
            value = dsum(_measure(l, fld) for l in loans if _matches(cfg.get("filter"), l))
            base.passed, base.limit = _compare(value, cfg)
            base.value = value
            if "max" in base.limit and value > base.limit["max"]:
                base.excess_amount = round_money(value - base.limit["max"])
        else:
            raise ExpressionError(f"unknown test type '{ttype}'")
    except (KeyError, ExpressionError, ValueError) as exc:
        base.passed = False
        base.error = f"configuration error: {exc}"
    if base.treatment != "limit":
        # Covenants and information tests flag breaches but do not reduce the borrowing base.
        base.details["excess_if_limit"] = base.excess_amount
        base.excess_amount = ZERO
    return base


def borrowing_base(
    adjusted_eligible_balance: Decimal,
    classes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Maximum permitted funding per note class.

    ``advance_rate`` is the cumulative attachment of a class, e.g. Class A 0.70 and Class B
    0.85 means senior funding up to 70% and senior + mezzanine up to 85% of the adjusted
    eligible balance. Classes without an advance rate (residual / seller notes) are only
    limited by their commitment.
    """
    rows = []
    senior_allowed = ZERO
    total_deficiency = ZERO
    for c in sorted(classes, key=lambda c: int(c.get("rank", 0))):
        commitment = D(c.get("commitment"), ZERO)
        outstanding = D(c.get("outstanding"), ZERO)
        if c.get("advance_rate") is None:
            allowed = commitment
            cap = None
        else:
            cap = round_money(adjusted_eligible_balance * D(c["advance_rate"]))
            allowed = max(ZERO, min(commitment, cap - senior_allowed))
        senior_allowed += allowed
        headroom = allowed - outstanding
        deficiency = max(ZERO, -headroom) if c.get("advance_rate") is not None else ZERO
        total_deficiency += deficiency
        rows.append({
            "class": c["name"], "rank": c.get("rank"), "advance_rate": c.get("advance_rate"),
            "commitment": commitment, "outstanding": outstanding, "cumulative_cap": cap,
            "max_funding": allowed, "available_to_draw": max(ZERO, headroom), "deficiency": deficiency,
        })
    return {"adjusted_eligible_balance": adjusted_eligible_balance, "classes": rows, "total_deficiency": total_deficiency}


def pool_summary(loans: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = dsum(_measure(l, "current_balance") for l in loans)

    def wa(fld: str) -> Decimal | None:
        num = den = ZERO
        for l in loans:
            if l.get(fld) is None:
                continue
            w = _measure(l, "current_balance")
            num += D(l[fld]) * w
            den += w
        return (num / den).quantize(Decimal("0.0001")) if den else None

    return {
        "loan_count": len(loans),
        "total_balance": total,
        "total_commitment": dsum(_measure(l, "commitment_amount") for l in loans),
        "average_balance": round_money(total / len(loans)) if loans else ZERO,
        "largest_balance": max((_measure(l, "current_balance") for l in loans), default=ZERO),
        "wa_ltv": wa("ltv"),
        "wa_icr": wa("icr"),
        "wa_interest_rate": wa("interest_rate"),
        "wa_margin_bps": wa("margin_bps"),
        "wa_remaining_term_months": wa("remaining_term_months"),
        "borrower_groups": len({str(l.get("borrower_group") or l.get("borrower_name")) for l in loans}),
    }


def stratify(loans: Sequence[Mapping[str, Any]], fld: str, buckets: Sequence[Any] | None = None, measure: str = "current_balance") -> list[dict[str, Any]]:
    """Group loans by a categorical field, or by numeric ``buckets`` (upper bounds) for numeric fields."""
    total = dsum(_measure(l, measure) for l in loans)
    groups: dict[str, dict[str, Any]] = {}
    bounds = [D(b) for b in buckets] if buckets else None

    def label_for(value: Any) -> str:
        if value is None:
            return "(blank)"
        if bounds is None:
            return str(value)
        v = D(value)
        lower = None
        for b in bounds:
            if v <= b:
                return f"{lower if lower is not None else '-inf'} < x <= {b}"
            lower = b
        return f"> {bounds[-1]}"

    for loan in loans:
        key = label_for(loan.get(fld))
        g = groups.setdefault(key, {"bucket": key, "count": 0, "amount": ZERO})
        g["count"] += 1
        g["amount"] += _measure(loan, measure)
    rows = list(groups.values())
    if bounds is None:
        rows.sort(key=lambda r: r["amount"], reverse=True)
    for r in rows:
        r["share"] = _ratio(r["amount"], total)
    return rows


def run_compliance(
    loans: Sequence[Mapping[str, Any]],
    criteria: Sequence[Mapping[str, Any]],
    tests: Sequence[Mapping[str, Any]],
    classes: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    loan_results = check_eligibility(loans, criteria)
    eligible_ids = {r.loan_id for r in loan_results if r.eligible}
    pool = [l for l in loans if str(l.get("loan_id")) in eligible_ids]
    test_results = [run_portfolio_test(t, pool, loans) for t in tests]
    eligible_balance = dsum(r.balance for r in loan_results if r.eligible)
    total_excess = dsum(t.excess_amount for t in test_results)
    adjusted = max(ZERO, eligible_balance - total_excess)
    bb = borrowing_base(adjusted, classes) if classes else None
    breached_events = sorted({t.event_code for t in test_results if not t.passed and t.event_code})
    return {
        "loan_results": [r.to_dict() for r in loan_results],
        "test_results": [t.to_dict() for t in test_results],
        "summary": {
            "total_loans": len(loans),
            "total_balance": dsum(_measure(l, "current_balance") for l in loans),
            "eligible_loans": len(eligible_ids),
            "eligible_balance": eligible_balance,
            "ineligible_balance": dsum(r.balance for r in loan_results if not r.eligible),
            "excess_concentration": total_excess,
            "adjusted_eligible_balance": adjusted,
            "tests_passed": sum(1 for t in test_results if t.passed),
            "tests_failed": sum(1 for t in test_results if not t.passed),
            "breached_events": breached_events,
            "borrowing_base_deficiency": bb["total_deficiency"] if bb else ZERO,
        },
        "borrowing_base": bb,
        "pool": pool_summary(pool),
    }
