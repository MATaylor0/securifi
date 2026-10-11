"""Trust bank accounts, bank statement import and reconciliation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field
from sqlalchemy import select

from ...db import utcnow
from ...engine.fields import Dataset
from ...engine.money import ZERO, D
from ...engine.reconciliation import Actual, Expected, auto_match
from ...models import BankAccount, BankTransaction, ExpectedCashflow, IngestionBatch
from ..context import OpContext
from ..errors import Conflict, NotFound, ValidationFailed
from ..registry import OpInput, operation
from .common import get_entity, get_facility, to_dict
from .data import _IngestBase, _prepare


def _mask(number: str | None) -> str | None:
    return f"***{number[-4:]}" if number else None


def _account_dict(a: BankAccount) -> dict:
    return {**to_dict(a, exclude=("account_number",)), "account_number": _mask(a.account_number)}


def _account(ctx: OpContext, facility_id: str, code: str) -> BankAccount:
    acct = ctx.session.execute(select(BankAccount).where(BankAccount.facility_id == facility_id,
                                                         (BankAccount.code == code) | (BankAccount.id == code))).scalar_one_or_none()
    if acct is None:
        raise NotFound(f"bank account '{code}' not found")
    ctx.touch(acct.id)
    return acct


class AccountCreate(OpInput):
    facility_id: str
    code: str = Field(description="e.g. 'COLLECTION', 'RESERVE'")
    name: str
    account_type: Literal["collection", "payment", "reserve", "liquidity", "expense", "other"]
    bank_name: str | None = None
    branch_code: str | None = Field(None, description="BSB / sort code")
    account_number: str | None = None
    currency: str = "AUD"
    opening_balance: Decimal = Decimal(0)
    opening_date: date | None = None


@operation("account.create", summary="Add a trust bank account", permission="facility.configure", input=AccountCreate, mutating=True, tags=("accounts",))
def account_create(ctx: OpContext, p: AccountCreate) -> dict:
    f = get_facility(ctx, p.facility_id)
    if ctx.session.execute(select(BankAccount).where(BankAccount.facility_id == f.id, BankAccount.code == p.code)).scalar_one_or_none():
        raise Conflict(f"account {p.code} already exists")
    a = BankAccount(**{**p.model_dump(), "facility_id": f.id})
    ctx.session.add(a)
    ctx.session.flush()
    settings = dict(f.settings or {})
    if p.account_type == "collection" and not settings.get("collection_account"):
        settings["collection_account"] = p.code
    if p.account_type in ("payment", "collection") and not settings.get("payments_account"):
        settings["payments_account"] = p.code
    f.settings = settings
    ctx.touch(a.id)
    return _account_dict(a)


class FacilityRef(OpInput):
    facility_id: str


@operation("account.list", summary="List bank accounts with book and statement balances", permission="facility.read", input=FacilityRef, mutating=False, tags=("accounts",))
def account_list(ctx: OpContext, p: FacilityRef) -> list:
    f = get_facility(ctx, p.facility_id)
    out = []
    for a in ctx.session.execute(select(BankAccount).where(BankAccount.facility_id == f.id).order_by(BankAccount.code)).scalars():
        out.append({**_account_dict(a), **_balances(ctx, a)})
    return out


def _balances(ctx: OpContext, a: BankAccount, as_of: date | None = None) -> dict:
    q = select(BankTransaction).where(BankTransaction.account_id == a.id)
    if as_of:
        q = q.where(BankTransaction.transaction_date <= as_of)
    txns = list(ctx.session.execute(q.order_by(BankTransaction.transaction_date, BankTransaction.created_at)).scalars())
    ledger = a.opening_balance + sum((t.amount for t in txns), ZERO)
    stated = next((t.statement_balance for t in reversed(txns) if t.statement_balance is not None), None)
    return {"bank_balance": ledger, "last_statement_balance": stated,
            "statement_difference": (stated - ledger) if stated is not None else None,
            "last_transaction_date": txns[-1].transaction_date if txns else None}


class StatementImport(_IngestBase):
    account: str = Field(description="Account code or id")
    statement_date: date


@operation("bank.import_statement", summary="Import bank statement lines for a trust account", permission="data.ingest", input=StatementImport, mutating=True, tags=("accounts",))
def import_statement(ctx: OpContext, p: StatementImport) -> dict:
    f = get_facility(ctx, p.facility_id)
    acct = _account(ctx, f.id, p.account)
    records, issues, digest, _ = _prepare(ctx, p, Dataset.BANK_STATEMENT, f.id)
    errors = {i["row"] for i in issues if i["severity"] == "error"}
    batch = IngestionBatch(facility_id=f.id, dataset="bank_statement", as_of_date=p.statement_date, account_id=acct.id,
                           source_name=p.source_name, mapping_id=p.mapping_id, content_hash=digest,
                           row_count=len(records) + len(errors), accepted_count=len(records), rejected_count=len(errors),
                           issues=issues, totals={"net": sum((D(r["amount"]) for r in records), ZERO)}, created_by=ctx.actor.id)
    ctx.session.add(batch)
    ctx.session.flush()
    for r in records:
        ctx.session.add(BankTransaction(account_id=acct.id, batch_id=batch.id, transaction_date=date.fromisoformat(r["transaction_date"]),
                                        amount=D(r["amount"]), description=r.get("description"), reference=r.get("reference"),
                                        statement_balance=D(r["balance"]) if r.get("balance") is not None else None))
    ctx.session.flush()
    ctx.touch(batch.id)
    return {**to_dict(batch, exclude=("issues",)), "issues": issues[:200], **_balances(ctx, acct)}


class ExpectedAdd(OpInput):
    facility_id: str
    account: str
    expected_date: date
    amount: Decimal = Field(description="Positive for receipts, negative for payments")
    description: str
    reference: str | None = None
    period_id: str | None = None


@operation("cashflow.add_expected", summary="Add an expected receipt or payment to reconcile against", permission="reconciliation.perform", input=ExpectedAdd, mutating=True, tags=("accounts",))
def add_expected(ctx: OpContext, p: ExpectedAdd) -> dict:
    f = get_facility(ctx, p.facility_id)
    acct = _account(ctx, f.id, p.account)
    e = ExpectedCashflow(facility_id=f.id, account_id=acct.id, period_id=p.period_id, expected_date=p.expected_date, amount=p.amount,
                         description=p.description, reference=p.reference, source_type="manual")
    ctx.session.add(e)
    ctx.session.flush()
    ctx.touch(e.id)
    return to_dict(e)


class AutoMatch(OpInput):
    facility_id: str
    account: str | None = Field(None, description="Default: all accounts")
    date_tolerance_days: int = Field(3, ge=0, le=31)


@operation("reconciliation.auto_match", summary="Automatically match bank lines to expected cash flows", permission="reconciliation.perform", input=AutoMatch, mutating=True, tags=("accounts",))
def reconcile_auto(ctx: OpContext, p: AutoMatch) -> dict:
    f = get_facility(ctx, p.facility_id)
    accounts = [_account(ctx, f.id, p.account)] if p.account else list(ctx.session.execute(select(BankAccount).where(BankAccount.facility_id == f.id)).scalars())
    matched = []
    for acct in accounts:
        expected = list(ctx.session.execute(select(ExpectedCashflow).where(ExpectedCashflow.account_id == acct.id, ExpectedCashflow.status == "unmatched")).scalars())
        actual = list(ctx.session.execute(select(BankTransaction).where(BankTransaction.account_id == acct.id, BankTransaction.status == "unmatched")).scalars())
        pairs = auto_match([Expected(e.id, e.expected_date, e.amount, e.reference) for e in expected],
                           [Actual(a.id, a.transaction_date, a.amount, a.reference, a.description) for a in actual], p.date_tolerance_days)
        by_e = {e.id: e for e in expected}
        by_a = {a.id: a for a in actual}
        for eid, aid, rule in pairs:
            _link(ctx, by_e[eid], by_a[aid], rule)
            matched.append({"account": acct.code, "expected_id": eid, "transaction_id": aid, "amount": by_e[eid].amount, "rule": rule})
    return {"matched": matched, "count": len(matched)}


def _link(ctx: OpContext, e: ExpectedCashflow, t: BankTransaction, rule: str) -> None:
    e.status, e.matched_transaction_id, e.match_rule, e.matched_by, e.matched_at = "matched", t.id, rule, ctx.actor.id, utcnow()
    t.status = "matched"
    ctx.touch(e.id, t.id)


class ManualMatch(OpInput):
    expected_id: str
    transaction_id: str
    note: str | None = None


@operation("reconciliation.match", summary="Manually match a bank line to an expected cash flow", permission="reconciliation.perform", input=ManualMatch, mutating=True, tags=("accounts",))
def reconcile_manual(ctx: OpContext, p: ManualMatch) -> dict:
    e = get_entity(ctx, ExpectedCashflow, p.expected_id, label="expected cash flow")
    t = ctx.session.get(BankTransaction, p.transaction_id)
    if t is None or t.account_id != e.account_id:
        raise NotFound("transaction not found on the same account")
    if e.status == "matched" or t.status != "unmatched":
        raise Conflict("already matched")
    if t.amount != e.amount and not p.note:
        raise ValidationFailed(f"amounts differ ({t.amount} vs {e.amount}); provide a note to match anyway")
    _link(ctx, e, t, "manual")
    return {"expected": to_dict(e), "transaction": to_dict(t)}


class Unmatch(OpInput):
    expected_id: str


@operation("reconciliation.unmatch", summary="Reverse a match", permission="reconciliation.perform", input=Unmatch, mutating=True, tags=("accounts",))
def reconcile_unmatch(ctx: OpContext, p: Unmatch) -> dict:
    e = get_entity(ctx, ExpectedCashflow, p.expected_id, label="expected cash flow")
    if e.status != "matched":
        raise Conflict("not matched")
    t = ctx.session.get(BankTransaction, e.matched_transaction_id)
    if t:
        t.status = "unmatched"
    e.status, e.matched_transaction_id, e.match_rule, e.matched_by, e.matched_at = "unmatched", None, None, None, None
    return to_dict(e)


class TxnIgnore(OpInput):
    transaction_id: str
    reason: str


@operation("reconciliation.ignore", summary="Mark a bank line as not requiring a match (e.g. internal transfer)", permission="reconciliation.perform", input=TxnIgnore, mutating=True, tags=("accounts",))
def reconcile_ignore(ctx: OpContext, p: TxnIgnore) -> dict:
    t = ctx.session.get(BankTransaction, p.transaction_id)
    if t is None:
        raise NotFound("transaction not found")
    acct = ctx.session.get(BankAccount, t.account_id)
    ctx.bind_facility(acct.facility_id)
    t.status = "ignored"
    t.description = f"{t.description or ''} [ignored: {p.reason}]".strip()
    ctx.touch(t.id)
    return to_dict(t)


class ReconStatus(OpInput):
    facility_id: str
    as_of_date: date | None = None


@operation("reconciliation.status", summary="Reconciliation position per account: balances and unmatched items", permission="facility.read", input=ReconStatus, mutating=False, tags=("accounts",))
def reconcile_status(ctx: OpContext, p: ReconStatus) -> list:
    f = get_facility(ctx, p.facility_id)
    out = []
    for a in ctx.session.execute(select(BankAccount).where(BankAccount.facility_id == f.id).order_by(BankAccount.code)).scalars():
        eq = select(ExpectedCashflow).where(ExpectedCashflow.account_id == a.id, ExpectedCashflow.status == "unmatched")
        tq = select(BankTransaction).where(BankTransaction.account_id == a.id, BankTransaction.status == "unmatched")
        if p.as_of_date:
            eq = eq.where(ExpectedCashflow.expected_date <= p.as_of_date)
            tq = tq.where(BankTransaction.transaction_date <= p.as_of_date)
        unmatched_expected = [to_dict(e) for e in ctx.session.execute(eq.order_by(ExpectedCashflow.expected_date)).scalars()]
        unmatched_txns = [to_dict(t) for t in ctx.session.execute(tq.order_by(BankTransaction.transaction_date)).scalars()]
        out.append({
            "account": a.code, "name": a.name, "account_type": a.account_type, **_balances(ctx, a, p.as_of_date),
            "unmatched_expected": unmatched_expected, "unmatched_transactions": unmatched_txns,
            "unmatched_expected_total": sum((D(e["amount"]) for e in unmatched_expected), ZERO),
            "unmatched_transactions_total": sum((D(t["amount"]) for t in unmatched_txns), ZERO),
            "reconciled": not unmatched_expected and not unmatched_txns,
        })
    return out
