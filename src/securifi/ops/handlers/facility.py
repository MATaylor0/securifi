"""Facility structure: facilities, legal documents, note classes, noteholders, fees, events and rates."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from ...engine.daycount import BusinessDayConvention, DayCount
from ...engine.money import ZERO
from ...models import (
    BankAccount, ComplianceRun, Facility, FacilityEvent, FeeDefinition, LegalDocument, NoteClass, Noteholder,
    Period, RateFixing,
)
from ..context import OpContext
from ..errors import Conflict, NotFound, ValidationFailed
from ..registry import NoInput, OpInput, operation
from .common import class_outstanding, get_facility, get_note_class, note_classes, positions, to_dict

FacilityType = Literal["warehouse", "term_trust", "repo", "bilateral", "other"]


class FacilitySettings(BaseModel):
    payment_day: int = Field(15, ge=1, le=31, description="Day of month payments fall due")
    frequency_months: int = Field(1, ge=1, le=12, description="Collection / payment frequency in months")
    payment_lag_months: int = Field(1, ge=0, le=3, description="Months after collection period end that payment falls")
    determination_offset_business_days: int = Field(5, ge=0, le=20)
    collection_account: str | None = Field(None, description="Bank account code that receives collections")
    payments_account: str | None = Field(None, description="Bank account code waterfall payments are made from")
    allow_self_approval: bool = Field(False, description="Permit the preparer to approve their own period run (not recommended)")
    drawdown_borrowing_base_check: Literal["enforce", "warn", "off"] = "enforce"


class FacilityCreate(OpInput):
    code: str = Field(min_length=2, max_length=64, description="Short unique code, e.g. 'CRE-WH-1'")
    name: str
    facility_type: FacilityType = "warehouse"
    currency: str = Field("AUD", min_length=3, max_length=3)
    trust_name: str | None = None
    trustee: str | None = None
    security_trustee: str | None = None
    trust_manager: str | None = None
    originator: str | None = None
    servicer: str | None = None
    closing_date: date | None = None
    revolving_period_end: date | None = Field(None, description="End of availability / revolving period")
    final_maturity_date: date | None = None
    facility_limit: Decimal | None = None
    day_count: DayCount = DayCount.ACT_365F
    business_day_convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING
    holidays: list[date] = Field(default_factory=list)
    settings: FacilitySettings = Field(default_factory=FacilitySettings)
    variables: dict[str, Any] = Field(default_factory=dict, description="Custom values for waterfall conditions, e.g. {'reserve_pct': 0.01}")


@operation("facility.create", summary="Create a warehouse facility or trust", permission="facility.configure", input=FacilityCreate, mutating=True)
def facility_create(ctx: OpContext, p: FacilityCreate) -> dict:
    if ctx.session.execute(select(Facility).where(Facility.code == p.code)).scalar_one_or_none():
        raise Conflict(f"facility code '{p.code}' already exists")
    data = p.model_dump()
    data["day_count"] = p.day_count.value
    data["business_day_convention"] = p.business_day_convention.value
    data["holidays"] = [h.isoformat() for h in p.holidays]
    facility = Facility(**data)
    ctx.session.add(facility)
    ctx.session.flush()
    ctx.bind_facility(facility.id)
    ctx.touch(facility.id)
    return to_dict(facility)


class FacilityUpdate(OpInput):
    facility_id: str
    name: str | None = None
    status: Literal["active", "amortising", "terminated", "draft"] | None = None
    trustee: str | None = None
    security_trustee: str | None = None
    trust_manager: str | None = None
    servicer: str | None = None
    revolving_period_end: date | None = None
    final_maturity_date: date | None = None
    facility_limit: Decimal | None = None
    holidays: list[date] | None = None
    settings: dict[str, Any] | None = Field(None, description="Settings to merge into the current settings")
    variables: dict[str, Any] | None = Field(None, description="Variables to merge into the current variables")


@operation("facility.update", summary="Update facility details, settings or variables", permission="facility.configure", input=FacilityUpdate, mutating=True)
def facility_update(ctx: OpContext, p: FacilityUpdate) -> dict:
    f = get_facility(ctx, p.facility_id)
    for field in ("name", "status", "trustee", "security_trustee", "trust_manager", "servicer",
                  "revolving_period_end", "final_maturity_date", "facility_limit"):
        value = getattr(p, field)
        if value is not None:
            setattr(f, field, value)
    if p.holidays is not None:
        f.holidays = [h.isoformat() for h in p.holidays]
    if p.settings:
        merged = {**(f.settings or {}), **p.settings}
        try:
            f.settings = FacilitySettings.model_validate(merged).model_dump()
        except Exception as exc:
            raise ValidationFailed(f"invalid settings: {exc}") from exc
    if p.variables:
        f.variables = {**(f.variables or {}), **p.variables}
    return to_dict(f)


@operation("facility.list", summary="List facilities with headline balances", permission="facility.read", mutating=False)
def facility_list(ctx: OpContext, _: NoInput) -> list:
    out = []
    for f in ctx.session.execute(select(Facility).order_by(Facility.code)).scalars():
        if not ctx.actor.can_access_facility(f.id):
            continue
        classes = note_classes(ctx, f.id)
        outstanding = class_outstanding(ctx, f.id)
        out.append({
            "id": f.id, "code": f.code, "name": f.name, "facility_type": f.facility_type, "status": f.status,
            "currency": f.currency, "facility_limit": f.facility_limit,
            "total_commitment": sum((c.commitment for c in classes), ZERO),
            "total_outstanding": sum(outstanding.values(), ZERO),
            "revolving_period_end": f.revolving_period_end,
        })
    return out


class FacilityRef(OpInput):
    facility_id: str = Field(description="Facility id or code")


@operation("facility.get", summary="Facility overview: structure, balances, accounts, latest test results and events", permission="facility.read", input=FacilityRef, mutating=False)
def facility_get(ctx: OpContext, p: FacilityRef) -> dict:
    f = get_facility(ctx, p.facility_id)
    s = ctx.session
    pos = positions(ctx, f.id)
    classes = []
    for c in note_classes(ctx, f.id):
        holders = pos.get(c.id, {})
        classes.append({
            **to_dict(c),
            "outstanding": sum((h["outstanding"] for h in holders.values()), ZERO),
            "holder_commitments": sum((h["commitment"] for h in holders.values()), ZERO),
        })
    latest_cr = s.execute(select(ComplianceRun).where(ComplianceRun.facility_id == f.id)
                          .order_by(ComplianceRun.created_at.desc()).limit(1)).scalar_one_or_none()
    next_period = s.execute(select(Period).where(Period.facility_id == f.id, Period.status != "finalised")
                            .order_by(Period.sequence).limit(1)).scalar_one_or_none()
    return {
        "facility": to_dict(f),
        "note_classes": classes,
        "fees": [to_dict(x) for x in s.execute(select(FeeDefinition).where(FeeDefinition.facility_id == f.id)).scalars()],
        "accounts": [to_dict(x, exclude=("account_number",)) for x in s.execute(select(BankAccount).where(BankAccount.facility_id == f.id)).scalars()],
        "documents": [to_dict(x) for x in s.execute(select(LegalDocument).where(LegalDocument.facility_id == f.id)).scalars()],
        "active_events": [to_dict(x) for x in s.execute(select(FacilityEvent).where(FacilityEvent.facility_id == f.id, FacilityEvent.status == "active")).scalars()],
        "latest_compliance": {"id": latest_cr.id, "as_of_date": latest_cr.as_of_date, "summary": latest_cr.summary,
                              "borrowing_base": latest_cr.borrowing_base} if latest_cr else None,
        "next_period": to_dict(next_period) if next_period else None,
    }


class DocumentAdd(OpInput):
    facility_id: str
    title: str
    doc_type: Literal["facility_agreement", "trust_deed", "security_trust_deed", "note_deed", "fee_letter",
                      "servicing_agreement", "master_definitions", "side_letter", "other"]
    version: str | None = None
    executed_date: date | None = None
    reference: str | None = Field(None, description="Location in the document management system")
    notes: str | None = None


@operation("document.add", summary="Register a legal document that rules and waterfall steps reference", permission="facility.configure", input=DocumentAdd, mutating=True, tags=("facility",))
def document_add(ctx: OpContext, p: DocumentAdd) -> dict:
    f = get_facility(ctx, p.facility_id)
    doc = LegalDocument(**{**p.model_dump(), "facility_id": f.id})
    ctx.session.add(doc)
    ctx.session.flush()
    ctx.touch(doc.id)
    return to_dict(doc)


class NoteClassCreate(OpInput):
    facility_id: str
    name: str = Field(description="e.g. 'A', 'B', 'Z' or 'Senior'")
    rank: int = Field(ge=1, description="1 = most senior")
    commitment: Decimal = Field(ge=0)
    rate_type: Literal["floating", "fixed"] = "floating"
    base_rate_index: str | None = Field(None, description="e.g. BBSW1M, BBSY1M, SOFR, SONIA")
    margin_bps: Decimal = Decimal(0)
    fixed_rate: Decimal | None = None
    base_rate_floor: Decimal | None = Field(None, description="Floor on the base rate, e.g. 0")
    day_count: DayCount | None = None
    advance_rate: Decimal | None = Field(None, ge=0, le=1, description="Cumulative advance rate against adjusted eligible balance; omit for residual classes")
    is_residual: bool = False
    legal_final_maturity: date | None = None


@operation("note_class.create", summary="Add a note class / tranche to a facility", permission="facility.configure", input=NoteClassCreate, mutating=True, tags=("notes",))
def note_class_create(ctx: OpContext, p: NoteClassCreate) -> dict:
    f = get_facility(ctx, p.facility_id)
    if p.rate_type == "floating" and not p.base_rate_index and not p.is_residual:
        raise ValidationFailed("floating rate classes need a base_rate_index")
    data = p.model_dump()
    data["facility_id"] = f.id
    data["day_count"] = (p.day_count or DayCount(f.day_count)).value
    nc = NoteClass(**data)
    ctx.session.add(nc)
    ctx.session.flush()
    ctx.touch(nc.id)
    return to_dict(nc)


class NoteClassUpdate(OpInput):
    facility_id: str
    note_class: str = Field(description="Class id or name")
    commitment: Decimal | None = None
    margin_bps: Decimal | None = None
    base_rate_floor: Decimal | None = None
    advance_rate: Decimal | None = None
    status: Literal["active", "repaid", "cancelled"] | None = None


@operation("note_class.update", summary="Change a note class's commitment, margin, floor or advance rate", permission="facility.configure", input=NoteClassUpdate, mutating=True, tags=("notes",))
def note_class_update(ctx: OpContext, p: NoteClassUpdate) -> dict:
    f = get_facility(ctx, p.facility_id)
    nc = get_note_class(ctx, f, p.note_class)
    for field in ("commitment", "margin_bps", "base_rate_floor", "advance_rate", "status"):
        value = getattr(p, field)
        if value is not None:
            setattr(nc, field, value)
    return to_dict(nc)


class NoteholderCreate(OpInput):
    name: str
    short_name: str = Field(description="Unique short code, e.g. 'BANK-A'")
    entity_type: str | None = None
    lei: str | None = None
    registration_number: str | None = Field(None, description="ABN / ACN / company number")
    contact_email: str | None = None
    payment_details: dict[str, Any] = Field(default_factory=dict, description="Settlement instructions (redacted in the audit log)")


@operation("noteholder.create", summary="Create a noteholder / funder", permission="facility.configure", input=NoteholderCreate, mutating=True, tags=("notes",))
def noteholder_create(ctx: OpContext, p: NoteholderCreate) -> dict:
    if ctx.session.execute(select(Noteholder).where(Noteholder.short_name == p.short_name)).scalar_one_or_none():
        raise Conflict(f"noteholder '{p.short_name}' already exists")
    nh = Noteholder(**p.model_dump())
    ctx.session.add(nh)
    ctx.session.flush()
    ctx.touch(nh.id)
    return to_dict(nh, exclude=("payment_details",))


@operation("noteholder.list", summary="List noteholders", permission="facility.read", mutating=False, tags=("notes",))
def noteholder_list(ctx: OpContext, _: NoInput) -> list:
    return [to_dict(n, exclude=("payment_details",)) for n in ctx.session.execute(select(Noteholder).order_by(Noteholder.short_name)).scalars()]


class FeeCreate(OpInput):
    facility_id: str
    code: str = Field(description="Code referenced by waterfall fee steps, e.g. 'TRUSTEE'")
    name: str
    payee: str
    calculation: Literal["percent_per_annum", "fixed_per_annum", "fixed_per_period"]
    basis: Literal["pool_balance", "notes_outstanding", "class_outstanding", "class_commitment",
                   "undrawn_commitment", "facility_limit"] | None = Field(None, description="Required for percent_per_annum")
    basis_class: str | None = Field(None, description="Note class for class_* and undrawn_commitment bases (omit for all classes)")
    rate_bps: Decimal | None = None
    amount: Decimal | None = None
    day_count: DayCount | None = None
    tax_rate: Decimal | None = Field(None, description="e.g. 0.1 for 10% GST")
    minimum_amount: Decimal | None = Field(None, description="Minimum fee per period")


@operation("fee.create", summary="Define a fee (trustee, manager, servicer, commitment, line fee ...)", permission="facility.configure", input=FeeCreate, mutating=True, tags=("fees",))
def fee_create(ctx: OpContext, p: FeeCreate) -> dict:
    f = get_facility(ctx, p.facility_id)
    if p.calculation == "percent_per_annum" and (p.basis is None or p.rate_bps is None):
        raise ValidationFailed("percent_per_annum fees need basis and rate_bps")
    if p.calculation != "percent_per_annum" and p.amount is None:
        raise ValidationFailed("fixed fees need amount")
    if p.basis_class:
        get_note_class(ctx, f, p.basis_class)
    data = p.model_dump()
    data["facility_id"] = f.id
    data["day_count"] = (p.day_count or DayCount(f.day_count)).value
    fee = FeeDefinition(**data)
    ctx.session.add(fee)
    ctx.session.flush()
    ctx.touch(fee.id)
    return to_dict(fee)


class FeeUpdate(OpInput):
    facility_id: str
    code: str
    rate_bps: Decimal | None = None
    amount: Decimal | None = None
    minimum_amount: Decimal | None = None
    tax_rate: Decimal | None = None
    payee: str | None = None
    active: bool | None = None


@operation("fee.update", summary="Change a fee's rate, amount, payee or active flag", permission="facility.configure", input=FeeUpdate, mutating=True, tags=("fees",))
def fee_update(ctx: OpContext, p: FeeUpdate) -> dict:
    f = get_facility(ctx, p.facility_id)
    fee = ctx.session.execute(select(FeeDefinition).where(FeeDefinition.facility_id == f.id, FeeDefinition.code == p.code)).scalar_one_or_none()
    if fee is None:
        raise NotFound(f"fee '{p.code}' not found")
    for field in ("rate_bps", "amount", "minimum_amount", "tax_rate", "payee", "active"):
        value = getattr(p, field)
        if value is not None:
            setattr(fee, field, value)
    ctx.touch(fee.id)
    return to_dict(fee)


class EventDeclare(OpInput):
    facility_id: str
    code: str = Field(description="e.g. AMORTISATION_EVENT, EVENT_OF_DEFAULT, REVIEW_EVENT")
    name: str
    declared_on: date
    source: str | None = Field(None, description="e.g. the portfolio test or notice that triggered it")
    notes: str | None = None


@operation("event.declare", summary="Declare a trigger event (amortisation, default, review ...)", permission="events.manage", input=EventDeclare, mutating=True, tags=("events",))
def event_declare(ctx: OpContext, p: EventDeclare) -> dict:
    f = get_facility(ctx, p.facility_id)
    existing = ctx.session.execute(select(FacilityEvent).where(
        FacilityEvent.facility_id == f.id, FacilityEvent.code == p.code, FacilityEvent.status == "active")).scalar_one_or_none()
    if existing:
        raise Conflict(f"event {p.code} is already active")
    ev = FacilityEvent(**{**p.model_dump(), "facility_id": f.id})
    ctx.session.add(ev)
    ctx.session.flush()
    ctx.touch(ev.id)
    return to_dict(ev)


class EventClear(OpInput):
    facility_id: str
    code: str
    cleared_on: date
    notes: str | None = None


@operation("event.clear", summary="Clear (cure or waive) an active trigger event", permission="events.manage", input=EventClear, mutating=True, tags=("events",))
def event_clear(ctx: OpContext, p: EventClear) -> dict:
    f = get_facility(ctx, p.facility_id)
    ev = ctx.session.execute(select(FacilityEvent).where(
        FacilityEvent.facility_id == f.id, FacilityEvent.code == p.code, FacilityEvent.status == "active")).scalar_one_or_none()
    if ev is None:
        raise NotFound(f"no active event {p.code}")
    ev.status, ev.cleared_on = "cleared", p.cleared_on
    if p.notes:
        ev.notes = f"{ev.notes or ''}\n{p.notes}".strip()
    ctx.touch(ev.id)
    return to_dict(ev)


@operation("event.list", summary="List trigger events for a facility", permission="facility.read", input=FacilityRef, mutating=False, tags=("events",))
def event_list(ctx: OpContext, p: FacilityRef) -> list:
    f = get_facility(ctx, p.facility_id)
    return [to_dict(e) for e in ctx.session.execute(select(FacilityEvent).where(FacilityEvent.facility_id == f.id)
                                                    .order_by(FacilityEvent.declared_on.desc())).scalars()]


class FixingSet(OpInput):
    index_name: str = Field(description="e.g. BBSW1M")
    fixing_date: date
    rate: Decimal = Field(description="Decimal rate, e.g. 0.0435 for 4.35%")
    source: str | None = None


@operation("rates.set_fixing", summary="Record (or correct) a base rate fixing", permission="market_data.write", input=FixingSet, mutating=True, tags=("rates",))
def rates_set_fixing(ctx: OpContext, p: FixingSet) -> dict:
    fx = ctx.session.execute(select(RateFixing).where(RateFixing.index_name == p.index_name, RateFixing.fixing_date == p.fixing_date)).scalar_one_or_none()
    previous = None
    if fx is None:
        fx = RateFixing(**p.model_dump())
        ctx.session.add(fx)
    else:
        previous = fx.rate
        fx.rate, fx.source = p.rate, p.source
    ctx.session.flush()
    ctx.touch(fx.id)
    return {**to_dict(fx), "previous_rate": previous}


class FixingList(OpInput):
    index_name: str | None = None
    limit: int = Field(100, ge=1, le=1000)


@operation("rates.list", summary="List base rate fixings", permission="facility.read", input=FixingList, mutating=False, tags=("rates",))
def rates_list(ctx: OpContext, p: FixingList) -> list:
    q = select(RateFixing)
    if p.index_name:
        q = q.where(RateFixing.index_name == p.index_name)
    return [to_dict(r) for r in ctx.session.execute(q.order_by(RateFixing.fixing_date.desc()).limit(p.limit)).scalars()]


def fixing_on(ctx: OpContext, index_name: str, on: date) -> RateFixing | None:
    return ctx.session.execute(select(RateFixing).where(RateFixing.index_name == index_name, RateFixing.fixing_date <= on)
                               .order_by(RateFixing.fixing_date.desc()).limit(1)).scalar_one_or_none()
