"""Note register: commitments, drawdowns (fundings), repayments and transfers."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import Field
from sqlalchemy import select

from ...engine.money import ZERO, allocate_pro_rata
from ...models import BankAccount, ComplianceRun, ExpectedCashflow, Facility, NoteClass, Noteholder, RegisterEntry
from ..context import OpContext
from ..errors import BusinessRuleViolation, PermissionDenied, ValidationFailed
from ..registry import OpInput, operation
from .common import get_facility, get_note_class, get_noteholder, note_classes, positions, register_entries, to_dict


def _entry(ctx: OpContext, facility: Facility, nc: NoteClass, holder_id: str, entry_type: str, amount: Decimal,
           effective_date: date, reference: str | None, notes: str | None = None, period_run_id: str | None = None) -> RegisterEntry:
    e = RegisterEntry(facility_id=facility.id, note_class_id=nc.id, noteholder_id=holder_id, entry_type=entry_type,
                      amount=amount, effective_date=effective_date, reference=reference, notes=notes,
                      period_run_id=period_run_id, created_by=ctx.actor.id)
    ctx.session.add(e)
    ctx.session.flush()
    ctx.touch(e.id)
    return e


class Commit(OpInput):
    facility_id: str
    note_class: str
    noteholder: str = Field(description="Noteholder id or short name")
    amount: Decimal = Field(description="Change in commitment (negative to reduce)")
    effective_date: date
    reference: str | None = None


@operation("register.commit", summary="Record a noteholder's commitment (or change to it) to a note class", permission="register.write", input=Commit, mutating=True, tags=("register",))
def register_commit(ctx: OpContext, p: Commit) -> dict:
    f = get_facility(ctx, p.facility_id)
    nc = get_note_class(ctx, f, p.note_class)
    nh = get_noteholder(ctx, p.noteholder)
    pos = positions(ctx, f.id)
    class_commitments = sum((h["commitment"] for h in pos.get(nc.id, {}).values()), ZERO)
    holder = pos.get(nc.id, {}).get(nh.id, {"commitment": ZERO, "outstanding": ZERO})
    if holder["commitment"] + p.amount < holder["outstanding"]:
        raise BusinessRuleViolation("commitment cannot be reduced below the holder's outstanding principal")
    if class_commitments + p.amount > nc.commitment:
        raise BusinessRuleViolation(f"holder commitments would exceed class {nc.name} commitment of {nc.commitment}")
    return to_dict(_entry(ctx, f, nc, nh.id, "commitment", p.amount, p.effective_date, p.reference))


class Drawdown(OpInput):
    facility_id: str
    note_class: str
    amount: Decimal = Field(gt=0)
    effective_date: date
    reference: str | None = None
    allocation: dict[str, Decimal] | None = Field(None, description="Explicit split {noteholder: amount}; default is pro rata to commitments")
    compliance_run_id: str | None = Field(None, description="Borrowing base run to test against (default: latest on or before the date)")
    override_reason: str | None = Field(None, description="Override a borrowing base failure (needs period.approve)")


@operation("register.drawdown", summary="Fund a drawdown on a note class, checked against commitments and the borrowing base", permission="register.write", input=Drawdown, mutating=True, tags=("register",))
def register_drawdown(ctx: OpContext, p: Drawdown) -> dict:
    """Allocates the drawdown across noteholders and records it on the register.

    Checks: class commitment, each holder's undrawn commitment, and the borrowing base from
    the latest compliance run (ingest the updated loan tape including the loans being funded
    and run ``compliance.run`` with purpose 'drawdown' first). The facility setting
    ``drawdown_borrowing_base_check`` controls whether a failure blocks, warns or is ignored.
    """
    f = get_facility(ctx, p.facility_id)
    nc = get_note_class(ctx, f, p.note_class)
    pos = positions(ctx, f.id).get(nc.id, {})
    outstanding = sum((h["outstanding"] for h in pos.values()), ZERO)
    if outstanding + p.amount > nc.commitment:
        raise BusinessRuleViolation(f"drawdown would take class {nc.name} to {outstanding + p.amount}, above its commitment of {nc.commitment}")

    if p.allocation:
        alloc = {get_noteholder(ctx, k).id: v for k, v in p.allocation.items()}
        if sum(alloc.values(), ZERO) != p.amount:
            raise ValidationFailed("allocation must sum to the drawdown amount")
    else:
        holders = sorted(h for h, v in pos.items() if v["commitment"] > 0)
        if not holders:
            raise BusinessRuleViolation(f"no noteholder commitments recorded for class {nc.name}")
        shares = allocate_pro_rata(p.amount, [pos[h]["commitment"] for h in holders])
        alloc = dict(zip(holders, shares))
    for holder_id, amount in alloc.items():
        h = pos.get(holder_id, {"commitment": ZERO, "outstanding": ZERO})
        if h["outstanding"] + amount > h["commitment"]:
            raise BusinessRuleViolation(f"allocation to {holder_id} exceeds its undrawn commitment")

    mode = (f.settings or {}).get("drawdown_borrowing_base_check", "enforce")
    bb_check: dict = {"mode": mode}
    if mode != "off":
        q = select(ComplianceRun).where(ComplianceRun.facility_id == f.id)
        q = q.where(ComplianceRun.id == p.compliance_run_id) if p.compliance_run_id else q.where(ComplianceRun.as_of_date <= p.effective_date)
        run = ctx.session.execute(q.order_by(ComplianceRun.created_at.desc()).limit(1)).scalar_one_or_none()
        problem = None
        if run is None or not run.borrowing_base:
            problem = "no borrowing base calculation available; run compliance.run first"
        else:
            row = next((r for r in run.borrowing_base["classes"] if r["class"] == nc.name), None)
            max_funding = Decimal(str(row["max_funding"])) if row else ZERO
            # Re-test against the current register, not the outstanding at the time of the run.
            available = max(ZERO, max_funding - outstanding)
            bb_check.update({"compliance_run_id": run.id, "as_of_date": run.as_of_date, "max_funding": max_funding,
                             "outstanding_before": outstanding, "available": available})
            if p.amount > available:
                problem = f"drawdown of {p.amount} exceeds borrowing base availability of {available} for class {nc.name}"
        if problem:
            bb_check["problem"] = problem
            if mode == "enforce":
                if not p.override_reason:
                    raise BusinessRuleViolation(problem, details=bb_check)
                if not ctx.actor.can("period.approve"):
                    raise PermissionDenied("overriding a borrowing base failure requires period.approve")
                bb_check["override_reason"] = p.override_reason
                bb_check["overridden_by"] = ctx.actor.label

    entries = [_entry(ctx, f, nc, holder_id, "drawdown", amount, p.effective_date, p.reference,
                      notes=f"override: {p.override_reason}" if bb_check.get("override_reason") else None)
               for holder_id, amount in alloc.items() if amount > 0]
    account_code = (f.settings or {}).get("payments_account")
    if account_code:
        acct = ctx.session.execute(select(BankAccount).where(BankAccount.facility_id == f.id, BankAccount.code == account_code)).scalar_one_or_none()
        if acct:
            ctx.session.add(ExpectedCashflow(facility_id=f.id, account_id=acct.id, expected_date=p.effective_date, amount=p.amount,
                                             description=f"Drawdown proceeds class {nc.name}", reference=p.reference,
                                             source_type="drawdown", source_ref=entries[0].id if entries else None))
    return {"note_class": nc.name, "amount": p.amount, "entries": [to_dict(e) for e in entries], "borrowing_base_check": bb_check}


class Repay(OpInput):
    facility_id: str
    note_class: str
    amount: Decimal = Field(gt=0)
    effective_date: date
    reference: str | None = None
    cancel_commitment: bool = Field(False, description="Also cancel the repaid amount of commitment")


@operation("register.repay", summary="Record a voluntary principal repayment outside the waterfall", permission="register.write", input=Repay, mutating=True, tags=("register",))
def register_repay(ctx: OpContext, p: Repay) -> dict:
    f = get_facility(ctx, p.facility_id)
    nc = get_note_class(ctx, f, p.note_class)
    pos = positions(ctx, f.id, p.effective_date).get(nc.id, {})
    holders = sorted(h for h, v in pos.items() if v["outstanding"] > 0)
    outstanding = sum((pos[h]["outstanding"] for h in holders), ZERO)
    if p.amount > outstanding:
        raise BusinessRuleViolation(f"repayment exceeds outstanding principal of {outstanding}")
    shares = allocate_pro_rata(p.amount, [pos[h]["outstanding"] for h in holders])
    entries = []
    for h, amt in zip(holders, shares):
        entries.append(_entry(ctx, f, nc, h, "repayment", amt, p.effective_date, p.reference))
        if p.cancel_commitment:
            entries.append(_entry(ctx, f, nc, h, "commitment", -amt, p.effective_date, p.reference, notes="commitment cancelled on repayment"))
    return {"note_class": nc.name, "amount": p.amount, "entries": [to_dict(e) for e in entries]}


class Transfer(OpInput):
    facility_id: str
    note_class: str
    from_noteholder: str
    to_noteholder: str
    principal: Decimal = Field(ge=0)
    commitment: Decimal = Field(Decimal(0), ge=0, description="Commitment transferred with the notes")
    effective_date: date
    reference: str | None = None


@operation("register.transfer", summary="Transfer notes (and commitment) between noteholders", permission="register.write", input=Transfer, mutating=True, tags=("register",))
def register_transfer(ctx: OpContext, p: Transfer) -> dict:
    f = get_facility(ctx, p.facility_id)
    nc = get_note_class(ctx, f, p.note_class)
    src, dst = get_noteholder(ctx, p.from_noteholder), get_noteholder(ctx, p.to_noteholder)
    if src.id == dst.id:
        raise ValidationFailed("cannot transfer to the same noteholder")
    pos = positions(ctx, f.id).get(nc.id, {}).get(src.id, {"commitment": ZERO, "outstanding": ZERO})
    if p.principal > pos["outstanding"] or p.commitment > pos["commitment"]:
        raise BusinessRuleViolation("transferor does not hold enough notes / commitment")
    if pos["commitment"] - p.commitment < pos["outstanding"] - p.principal:
        raise BusinessRuleViolation("transfer would leave the transferor with principal above its commitment")
    entries = []
    if p.principal:
        entries.append(_entry(ctx, f, nc, src.id, "transfer_out", p.principal, p.effective_date, p.reference))
        entries.append(_entry(ctx, f, nc, dst.id, "transfer_in", p.principal, p.effective_date, p.reference))
    if p.commitment:
        entries.append(_entry(ctx, f, nc, src.id, "commitment", -p.commitment, p.effective_date, p.reference, notes="transfer"))
        entries.append(_entry(ctx, f, nc, dst.id, "commitment", p.commitment, p.effective_date, p.reference, notes="transfer"))
    return {"entries": [to_dict(e) for e in entries]}


class PositionsQuery(OpInput):
    facility_id: str
    as_of_date: date | None = Field(None, description="Inclusive; default is all entries")


@operation("register.positions", summary="Note register positions by class and noteholder", permission="facility.read", input=PositionsQuery, mutating=False, tags=("register",))
def register_positions(ctx: OpContext, p: PositionsQuery) -> dict:
    f = get_facility(ctx, p.facility_id)
    pos = positions(ctx, f.id, p.as_of_date)
    holders = {h.id: h for h in ctx.session.execute(select(Noteholder)).scalars()}
    classes = []
    for nc in note_classes(ctx, f.id):
        rows = pos.get(nc.id, {})
        total_out = sum((r["outstanding"] for r in rows.values()), ZERO)
        classes.append({
            "note_class": nc.name, "rank": nc.rank, "class_commitment": nc.commitment, "outstanding": total_out,
            "undrawn": nc.commitment - total_out,
            "holders": [
                {"noteholder_id": hid, "noteholder": holders[hid].short_name if hid in holders else hid,
                 "commitment": r["commitment"], "outstanding": r["outstanding"],
                 "share": (r["outstanding"] / total_out).quantize(Decimal("0.000001")) if total_out else ZERO}
                for hid, r in sorted(rows.items())
            ],
        })
    return {"facility": f.code, "as_of_date": p.as_of_date, "classes": classes}


class EntriesQuery(OpInput):
    facility_id: str
    as_of_date: date | None = None
    note_class: str | None = None


@operation("register.entries", summary="Note register ledger entries", permission="facility.read", input=EntriesQuery, mutating=False, tags=("register",))
def register_entries_list(ctx: OpContext, p: EntriesQuery) -> list:
    f = get_facility(ctx, p.facility_id)
    entries = register_entries(ctx, f.id, p.as_of_date)
    if p.note_class:
        nc = get_note_class(ctx, f, p.note_class)
        entries = [e for e in entries if e.note_class_id == nc.id]
    return [to_dict(e) for e in entries]
