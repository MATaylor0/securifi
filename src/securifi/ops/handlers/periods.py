"""Period schedules, waterfall definitions and the month-end period run lifecycle.

    period_run.calculate  (period.prepare)  interest, fees, available funds and waterfall
    period_run.approve    (period.approve)  four-eyes: approver must differ from preparer
    period_run.finalise   (period.approve)  posts principal repayments to the register,
                                            creates expected payments for reconciliation
                                            and locks the period
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import Field
from sqlalchemy import func, select

from ...db import utcnow
from ...engine.daycount import Calendar
from ...engine.interest import balance_segments, class_interest, fee_amount
from ...engine.mapping import _auto_type
from ...engine.money import ZERO, D, allocate_pro_rata
from ...engine.schedule import generate_schedule
from ...engine.waterfall import WaterfallError, run_waterfall, validate_definition
from ...models import (
    BankAccount, ComplianceRun, ExpectedCashflow, Facility, FacilityEvent, FeeDefinition, IngestionBatch, NoteClass,
    Noteholder, Period, PeriodRun, WaterfallDefinition,
)
from ..context import OpContext
from ..errors import BusinessRuleViolation, Conflict, NotFound, ValidationFailed
from ..registry import OpInput, operation
from .common import get_entity, get_facility, movements_by_holder, note_classes, positions, to_dict
from .data import _period
from .facility import fixing_on
from .register import _entry

DEFAULT_AVAILABLE_FUNDS = {
    "income": {"plus": ["interest_collected", "fees_collected", "other_income"], "minus": ["servicer_fee_deducted"]},
    "principal": {"plus": ["scheduled_principal", "prepayments", "recoveries"], "minus": ["further_advances"]},
}


class PeriodGenerate(OpInput):
    facility_id: str
    first_collection_start: date
    count: int = Field(ge=1, le=600)
    first_interest_start: date | None = Field(None, description="Default: facility closing date, else first collection start")


@operation("period.generate", summary="Generate collection / interest / payment periods from the facility settings", permission="facility.configure", input=PeriodGenerate, mutating=True, tags=("periods",))
def period_generate(ctx: OpContext, p: PeriodGenerate) -> list:
    f = get_facility(ctx, p.facility_id)
    s = f.settings or {}
    last = ctx.session.execute(select(Period).where(Period.facility_id == f.id).order_by(Period.sequence.desc()).limit(1)).scalar_one_or_none()
    if last and p.first_collection_start <= last.collection_end:
        raise Conflict(f"periods already exist up to {last.collection_end}")
    first_interest = p.first_interest_start or (last.interest_end if last else None) or f.closing_date or p.first_collection_start
    schedule = generate_schedule(
        first_collection_start=p.first_collection_start, count=p.count,
        frequency_months=s.get("frequency_months", 1), payment_day=s.get("payment_day", 15),
        payment_lag_months=s.get("payment_lag_months", 1),
        determination_offset_business_days=s.get("determination_offset_business_days", 5),
        first_interest_start=first_interest, business_day_convention=f.business_day_convention,
        calendar=Calendar(f.holidays or []), start_sequence=(last.sequence + 1) if last else 1,
    )
    out = []
    for pd in schedule:
        period = Period(facility_id=f.id, sequence=pd.sequence, collection_start=pd.collection_start, collection_end=pd.collection_end,
                        interest_start=pd.interest_start, interest_end=pd.interest_end, determination_date=pd.determination_date,
                        payment_date=pd.payment_date)
        ctx.session.add(period)
        ctx.session.flush()
        out.append(to_dict(period))
    return out


class FacilityRef(OpInput):
    facility_id: str


@operation("period.list", summary="List periods and their status", permission="facility.read", input=FacilityRef, mutating=False, tags=("periods",))
def period_list(ctx: OpContext, p: FacilityRef) -> list:
    f = get_facility(ctx, p.facility_id)
    return [to_dict(x) for x in ctx.session.execute(select(Period).where(Period.facility_id == f.id).order_by(Period.sequence)).scalars()]


class WaterfallSet(OpInput):
    facility_id: str
    definition: dict[str, Any] = Field(description="{'order': [...], 'waterfalls': {'income': [steps], 'principal': [steps]}} - see catalog.rule_language")
    available_funds: dict[str, Any] | None = Field(None, description="How collections map to each waterfall: {'income': {'plus': [...], 'minus': [...]}}")
    notes: str | None = None


@operation("waterfall.set", summary="Publish a new version of the facility's priority of payments", permission="facility.configure", input=WaterfallSet, mutating=True, tags=("waterfall",))
def waterfall_set(ctx: OpContext, p: WaterfallSet) -> dict:
    f = get_facility(ctx, p.facility_id)
    problems = validate_definition(p.definition)
    fee_codes = {x.code for x in ctx.session.execute(select(FeeDefinition).where(FeeDefinition.facility_id == f.id)).scalars()}
    class_names = {c.name for c in note_classes(ctx, f.id)}
    for name, steps in (p.definition.get("waterfalls") or {}).items():
        for i, step in enumerate(steps, start=1):
            if step.get("type") == "fee" and step.get("fee") not in fee_codes:
                problems.append(f"{name} step {i}: unknown fee '{step.get('fee')}'")
            for c in step.get("classes") or ([step["class"]] if step.get("class") else []):
                if c not in class_names:
                    problems.append(f"{name} step {i}: unknown note class '{c}'")
    available = p.available_funds or DEFAULT_AVAILABLE_FUNDS
    for wf_name in available:
        if wf_name not in (p.definition.get("waterfalls") or {}):
            problems.append(f"available_funds references unknown waterfall '{wf_name}'")
    if problems:
        raise ValidationFailed("invalid waterfall", details=problems)
    current = ctx.session.execute(select(func.max(WaterfallDefinition.version)).where(WaterfallDefinition.facility_id == f.id)).scalar() or 0
    for old in ctx.session.execute(select(WaterfallDefinition).where(WaterfallDefinition.facility_id == f.id, WaterfallDefinition.is_active.is_(True))).scalars():
        old.is_active = False
    wf = WaterfallDefinition(facility_id=f.id, version=current + 1, definition=p.definition, available_funds=available,
                             notes=p.notes, created_by=ctx.actor.id)
    ctx.session.add(wf)
    ctx.session.flush()
    ctx.touch(wf.id)
    return to_dict(wf)


@operation("waterfall.get", summary="The facility's active waterfall definition", permission="facility.read", input=FacilityRef, mutating=False, tags=("waterfall",))
def waterfall_get(ctx: OpContext, p: FacilityRef) -> dict:
    f = get_facility(ctx, p.facility_id)
    wf = _active_waterfall(ctx, f)
    return to_dict(wf)


def _active_waterfall(ctx: OpContext, f: Facility) -> WaterfallDefinition:
    wf = ctx.session.execute(select(WaterfallDefinition).where(WaterfallDefinition.facility_id == f.id, WaterfallDefinition.is_active.is_(True))).scalar_one_or_none()
    if wf is None:
        raise NotFound("no waterfall defined; use waterfall.set")
    return wf


class RunCalculate(OpInput):
    facility_id: str
    period: int | str = Field(description="Period sequence or id")
    compliance_run_id: str | None = Field(None, description="Default: latest compliance run as at the collection period end")
    adjustments: dict[str, Decimal] = Field(default_factory=dict, description="Manual additions to available funds per waterfall, e.g. {'income': 1250.00} for bank interest")
    base_rate_overrides: dict[str, Decimal] = Field(default_factory=dict, description="Override the base rate per class name")
    allow_missing_collections: bool = False
    notes: str | None = None


def _class_dict(c: NoteClass) -> dict[str, Any]:
    return {"name": c.name, "rate_type": c.rate_type, "fixed_rate": c.fixed_rate, "margin_bps": c.margin_bps,
            "base_rate_floor": c.base_rate_floor, "day_count": c.day_count, "is_residual": c.is_residual}


def _previous_finalised(ctx: OpContext, f: Facility, period: Period) -> PeriodRun | None:
    return ctx.session.execute(select(PeriodRun).join(Period, Period.id == PeriodRun.period_id).where(
        PeriodRun.facility_id == f.id, PeriodRun.status == "finalised", Period.sequence < period.sequence
    ).order_by(Period.sequence.desc()).limit(1)).scalar_one_or_none()


def _reserve_balances(ctx: OpContext, f: Facility) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    runs = list(ctx.session.execute(select(PeriodRun).where(PeriodRun.facility_id == f.id, PeriodRun.status == "finalised")).scalars())
    for a in ctx.session.execute(select(BankAccount).where(BankAccount.facility_id == f.id, BankAccount.account_type.in_(("reserve", "liquidity")))).scalars():
        moves = sum((D(r.outputs["waterfall"]["reserve_movements"].get(a.code, 0)) for r in runs), ZERO)
        out[a.code] = a.opening_balance + moves
    return out


@operation("period_run.calculate", summary="Calculate interest, fees and the waterfall for a period", permission="period.prepare", input=RunCalculate, mutating=True, tags=("periods",))
def period_run_calculate(ctx: OpContext, p: RunCalculate) -> dict:
    """Builds the full month-end calculation and stores it as a new run awaiting approval.

    Any earlier unfinalised run for the period is superseded. Interest is accrued daily on
    register balances over the interest period, fixings are taken on the first day of the
    interest period, and unpaid interest and fees from the prior period are carried forward.
    """
    f = get_facility(ctx, p.facility_id)
    period = _period(ctx, f.id, p.period)
    if period.status == "finalised":
        raise Conflict(f"period {period.sequence} is already finalised")
    prev = ctx.session.execute(select(Period).where(Period.facility_id == f.id, Period.sequence == period.sequence - 1)).scalar_one_or_none()
    if prev is not None and prev.status != "finalised":
        raise BusinessRuleViolation(f"period {prev.sequence} must be finalised first")
    wf = _active_waterfall(ctx, f)
    warnings: list[str] = []

    # Collections
    batch = ctx.session.execute(select(IngestionBatch).where(
        IngestionBatch.facility_id == f.id, IngestionBatch.dataset == "collections", IngestionBatch.period_id == period.id,
        IngestionBatch.status == "loaded")).scalar_one_or_none()
    if batch is None and not p.allow_missing_collections:
        raise BusinessRuleViolation(f"no collections loaded for period {period.sequence} (set allow_missing_collections to proceed)")
    collections = {k: D(v) for k, v in (batch.totals if batch else {}).items()}
    available: dict[str, Decimal] = {}
    for name, cfg in wf.available_funds.items():
        amount = sum((collections.get(k, ZERO) for k in cfg.get("plus", [])), ZERO) - sum((collections.get(k, ZERO) for k in cfg.get("minus", [])), ZERO)
        available[name] = amount + D(p.adjustments.get(name), ZERO)
    for name in p.adjustments:
        if name not in available:
            available[name] = D(p.adjustments[name])

    # Compliance / pool
    cq = select(ComplianceRun).where(ComplianceRun.facility_id == f.id)
    cq = cq.where(ComplianceRun.id == p.compliance_run_id) if p.compliance_run_id else cq.where(ComplianceRun.as_of_date <= period.collection_end)
    cr = ctx.session.execute(cq.order_by(ComplianceRun.as_of_date.desc(), ComplianceRun.created_at.desc()).limit(1)).scalar_one_or_none()
    if cr is None:
        warnings.append("no compliance run found; pool-based fees and borrowing base variables are zero")
    summary = cr.summary if cr else {}

    # Interest
    previous_run = _previous_finalised(ctx, f, period)
    carried = previous_run.outputs.get("carry_forward", {}) if previous_run else {}
    classes = note_classes(ctx, f.id)
    interest_rows, interest_due, class_moves = [], {}, {}
    for c in classes:
        moves = movements_by_holder(ctx, c.id)
        class_moves[c.name] = [m for ms in moves.values() for m in ms]
        base_rate = None
        fixing = None
        if c.rate_type == "floating" and not c.is_residual:
            if c.name in p.base_rate_overrides:
                base_rate = D(p.base_rate_overrides[c.name])
            else:
                fixing = fixing_on(ctx, c.base_rate_index, period.interest_start)
                if fixing is None:
                    raise BusinessRuleViolation(f"no {c.base_rate_index} fixing on or before {period.interest_start} for class {c.name}")
                base_rate = fixing.rate
                if (period.interest_start - fixing.fixing_date).days > 7:
                    warnings.append(f"{c.base_rate_index} fixing for class {c.name} is dated {fixing.fixing_date}")
        row = class_interest(_class_dict(c), moves, period.interest_start, period.interest_end, base_rate)
        row["fixing_date"] = fixing.fixing_date if fixing else None
        row["base_rate_index"] = c.base_rate_index
        row["carried_forward"] = D(carried.get("interest", {}).get(c.name), ZERO)
        row["total_due"] = row["interest"] + row["carried_forward"]
        interest_due[c.name] = row["total_due"]
        interest_rows.append(row)

    # Fees
    fee_rows, fees_due = [], {}
    by_name = {c.name: c for c in classes}
    for fee in ctx.session.execute(select(FeeDefinition).where(FeeDefinition.facility_id == f.id, FeeDefinition.active.is_(True)).order_by(FeeDefinition.code)).scalars():
        fd = {"code": fee.code, "calculation": fee.calculation, "rate_bps": fee.rate_bps, "amount": fee.amount, "day_count": fee.day_count,
              "tax_rate": fee.tax_rate, "minimum_amount": fee.minimum_amount}
        segs, basis_amount = None, None
        selected = [by_name[fee.basis_class]] if fee.basis_class else classes
        if fee.basis == "pool_balance":
            basis_amount = D(summary.get("total_balance"), ZERO)
        elif fee.basis == "facility_limit":
            basis_amount = f.facility_limit or ZERO
        elif fee.basis == "class_commitment":
            basis_amount = sum((c.commitment for c in selected), ZERO)
        elif fee.basis in ("notes_outstanding", "class_outstanding"):
            segs = balance_segments([m for c in selected for m in class_moves[c.name]], period.interest_start, period.interest_end)
        elif fee.basis == "undrawn_commitment":
            commitment = sum((c.commitment for c in selected), ZERO)
            segs = [(s, e, max(ZERO, commitment - b)) for s, e, b in
                    balance_segments([m for c in selected for m in class_moves[c.name]], period.interest_start, period.interest_end)]
        res = fee_amount(fd, period.interest_start, period.interest_end, basis_segments=segs, basis_amount=basis_amount)
        res.update({"name": fee.name, "payee": fee.payee, "basis": fee.basis, "basis_amount": basis_amount,
                    "carried_forward": D(carried.get("fees", {}).get(fee.code), ZERO)})
        res["total_due"] = res["amount"] + res["carried_forward"]
        fees_due[fee.code] = res["total_due"]
        fee_rows.append(res)

    # Waterfall
    outstanding_pos = positions(ctx, f.id, period.payment_date)
    class_balance = {c.name: sum((h["outstanding"] for h in outstanding_pos.get(c.id, {}).values()), ZERO) for c in classes}
    events = {e.code: True for e in ctx.session.execute(select(FacilityEvent).where(
        FacilityEvent.facility_id == f.id, FacilityEvent.status == "active", FacilityEvent.declared_on <= period.determination_date)).scalars()}
    tests = {t["code"]: bool(t["passed"]) for t in (cr.test_results if cr else [])}
    variables = {
        **{k: _auto_type(v) for k, v in (f.variables or {}).items()},
        "events": events, "tests": tests, "period_sequence": Decimal(period.sequence),
        "is_revolving": bool(f.revolving_period_end and period.payment_date <= f.revolving_period_end),
        "pool_balance": D(summary.get("total_balance"), ZERO),
        "eligible_balance": D(summary.get("eligible_balance"), ZERO),
        "adjusted_eligible_balance": D(summary.get("adjusted_eligible_balance"), ZERO),
        "borrowing_base_deficiency": D(summary.get("borrowing_base_deficiency"), ZERO),
    }
    reserves = _reserve_balances(ctx, f)
    try:
        result = run_waterfall(wf.definition, available=available, fees_due=fees_due, interest_due=interest_due,
                               class_balance=class_balance, reserve_balance=reserves, variables=variables)
    except (WaterfallError, ValueError, LookupError) as exc:
        raise BusinessRuleViolation(f"waterfall could not be run: {exc}") from exc
    if result["undistributed"]:
        warnings.append(f"funds left undistributed: {result['undistributed']} (add a residual step?)")

    # Allocate class payments to noteholders
    holder_names = {h.id: h.short_name for h in ctx.session.execute(select(Noteholder)).scalars()}
    holder_payments = []
    for c, row in zip(classes, interest_rows):
        paid_int = result["interest_paid"].get(c.name, ZERO)
        due_by_holder = row["by_holder"]
        holders = sorted(set(due_by_holder) | set(outstanding_pos.get(c.id, {})))
        int_split = allocate_pro_rata(paid_int, [D(due_by_holder.get(h), ZERO) for h in holders]) if holders else []
        paid_prin = result["principal_paid"].get(c.name, ZERO)
        prin_split = allocate_pro_rata(paid_prin, [outstanding_pos.get(c.id, {}).get(h, {"outstanding": ZERO})["outstanding"] for h in holders]) if holders else []
        for h, i_amt, p_amt in zip(holders, int_split, prin_split):
            if i_amt or p_amt or D(due_by_holder.get(h), ZERO):
                holder_payments.append({"note_class": c.name, "noteholder_id": h, "noteholder": holder_names.get(h, h),
                                        "interest_due": D(due_by_holder.get(h), ZERO), "interest_paid": i_amt, "principal_paid": p_amt})

    run_number = (ctx.session.execute(select(func.max(PeriodRun.run_number)).where(PeriodRun.period_id == period.id)).scalar() or 0) + 1
    for old in ctx.session.execute(select(PeriodRun).where(PeriodRun.period_id == period.id, PeriodRun.status.in_(("calculated", "approved")))).scalars():
        old.status = "superseded"
    inputs = {
        "period": to_dict(period), "collections_batch_id": batch.id if batch else None, "collections": collections,
        "adjustments": p.adjustments, "compliance_run_id": cr.id if cr else None, "pool_summary": summary,
        "class_balances_at_payment_date": class_balance, "reserve_balances": reserves, "events": sorted(events),
        "waterfall_version": wf.version, "carried_forward_from_run": previous_run.id if previous_run else None,
    }
    outputs = {
        "interest": interest_rows, "fees": fee_rows, "available_funds": available, "waterfall": result,
        "holder_payments": holder_payments,
        "carry_forward": {"interest": result["unpaid_interest"], "fees": result["unpaid_fees"]},
        "note_balances": [{"note_class": c.name, "opening": class_balance[c.name],
                           "principal_paid": result["principal_paid"].get(c.name, ZERO),
                           "closing": class_balance[c.name] - result["principal_paid"].get(c.name, ZERO)} for c in classes],
        "warnings": warnings,
    }
    run = PeriodRun(facility_id=f.id, period_id=period.id, run_number=run_number, status="calculated", compliance_run_id=cr.id if cr else None,
                    waterfall_definition_id=wf.id, inputs=inputs, outputs=outputs, notes=p.notes, prepared_by=ctx.actor.id)
    ctx.session.add(run)
    period.status = "calculated"
    ctx.session.flush()
    ctx.touch(run.id, period.id)
    return _run_dict(run)


def _run_dict(run: PeriodRun) -> dict:
    return to_dict(run)


class RunRef(OpInput):
    run_id: str


@operation("period_run.get", summary="Period run detail (inputs, calculations, waterfall, holder payments)", permission="facility.read", input=RunRef, mutating=False, tags=("periods",))
def period_run_get(ctx: OpContext, p: RunRef) -> dict:
    return _run_dict(get_entity(ctx, PeriodRun, p.run_id, label="period run"))


class RunList(OpInput):
    facility_id: str
    period: int | str | None = None


@operation("period_run.list", summary="List period runs", permission="facility.read", input=RunList, mutating=False, tags=("periods",))
def period_run_list(ctx: OpContext, p: RunList) -> list:
    f = get_facility(ctx, p.facility_id)
    q = select(PeriodRun, Period.sequence).join(Period, Period.id == PeriodRun.period_id).where(PeriodRun.facility_id == f.id)
    if p.period is not None:
        q = q.where(PeriodRun.period_id == _period(ctx, f.id, p.period).id)
    return [{"id": r.id, "period_sequence": seq, "run_number": r.run_number, "status": r.status, "prepared_by": r.prepared_by,
             "prepared_at": r.prepared_at, "approved_by": r.approved_by, "finalised_at": r.finalised_at,
             "warnings": r.outputs.get("warnings", [])}
            for r, seq in ctx.session.execute(q.order_by(Period.sequence.desc(), PeriodRun.run_number.desc()))]


class RunDecision(OpInput):
    run_id: str
    comment: str | None = None


@operation("period_run.approve", summary="Approve a calculated period run (four-eyes)", permission="period.approve", input=RunDecision, mutating=True, tags=("periods",))
def period_run_approve(ctx: OpContext, p: RunDecision) -> dict:
    run = get_entity(ctx, PeriodRun, p.run_id, label="period run")
    if run.status != "calculated":
        raise Conflict(f"run is {run.status}, only calculated runs can be approved")
    f = ctx.session.get(Facility, run.facility_id)
    if run.prepared_by == ctx.actor.id and not (f.settings or {}).get("allow_self_approval"):
        raise BusinessRuleViolation("four-eyes control: the preparer cannot approve their own run")
    run.status, run.approved_by, run.approved_at = "approved", ctx.actor.id, utcnow()
    if p.comment:
        run.notes = f"{run.notes or ''}\nApproval: {p.comment}".strip()
    ctx.session.get(Period, run.period_id).status = "approved"
    return _run_dict(run)


class RunReject(OpInput):
    run_id: str
    reason: str


@operation("period_run.reject", summary="Reject a calculated or approved run", permission="period.approve", input=RunReject, mutating=True, tags=("periods",))
def period_run_reject(ctx: OpContext, p: RunReject) -> dict:
    run = get_entity(ctx, PeriodRun, p.run_id, label="period run")
    if run.status not in ("calculated", "approved"):
        raise Conflict(f"run is {run.status}")
    run.status = "rejected"
    run.notes = f"{run.notes or ''}\nRejected: {p.reason}".strip()
    ctx.session.get(Period, run.period_id).status = "open"
    return _run_dict(run)


@operation("period_run.finalise", summary="Finalise an approved run: post register repayments, create expected payments, lock the period", permission="period.approve", input=RunDecision, mutating=True, tags=("periods",))
def period_run_finalise(ctx: OpContext, p: RunDecision) -> dict:
    run = get_entity(ctx, PeriodRun, p.run_id, label="period run")
    if run.status != "approved":
        raise Conflict(f"run is {run.status}; it must be approved first")
    f = ctx.session.get(Facility, run.facility_id)
    period = ctx.session.get(Period, run.period_id)
    classes = {c.name: c for c in note_classes(ctx, f.id)}
    posted = []
    for hp in run.outputs["holder_payments"]:
        amount = D(hp["principal_paid"])
        if amount > 0:
            e = _entry(ctx, f, classes[hp["note_class"]], hp["noteholder_id"], "repayment", amount, period.payment_date,
                       f"Period {period.sequence} distribution", period_run_id=run.id)
            posted.append(to_dict(e))

    accounts = {a.code: a for a in ctx.session.execute(select(BankAccount).where(BankAccount.facility_id == f.id)).scalars()}
    pay_acct = accounts.get((f.settings or {}).get("payments_account") or "")
    expected = []

    def expect(acct: BankAccount | None, amount: Decimal, description: str, reference: str | None = None) -> None:
        if acct is None or amount == 0:
            return
        e = ExpectedCashflow(facility_id=f.id, account_id=acct.id, period_id=period.id, expected_date=period.payment_date,
                             amount=amount, description=description, reference=reference, source_type="waterfall", source_ref=run.id)
        ctx.session.add(e)
        expected.append({"account": acct.code, "amount": amount, "description": description})

    wf = run.outputs["waterfall"]
    fee_payees = {r["code"]: r["payee"] for r in run.outputs["fees"]}
    for code, amount in wf["fees_paid"].items():
        expect(pay_acct, -D(amount), f"Fee {code} to {fee_payees.get(code, code)}", code)
    for hp in run.outputs["holder_payments"]:
        total = D(hp["interest_paid"]) + D(hp["principal_paid"])
        expect(pay_acct, -total, f"Class {hp['note_class']} distribution to {hp['noteholder']}", hp["noteholder"])
    for other in wf["other_payments"]:
        expect(pay_acct, -D(other["amount"]), f"{other.get('label') or 'Payment'} to {other['payee']}")
    for code, movement in wf["reserve_movements"].items():
        movement = D(movement)
        expect(pay_acct, -movement, f"Transfer {'to' if movement > 0 else 'from'} {code}")
        expect(accounts.get(code), movement, f"Reserve {'funding' if movement > 0 else 'draw'} period {period.sequence}")

    run.status, run.finalised_by, run.finalised_at = "finalised", ctx.actor.id, utcnow()
    period.status = "finalised"
    ctx.touch(run.id, period.id)
    return {**_run_dict(run), "register_entries": posted, "expected_cashflows": expected,
            "warnings": [] if pay_acct else ["no payments_account configured; no expected payments created"]}
