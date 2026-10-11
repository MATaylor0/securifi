"""Ingestion of servicer data: loan tapes (pool cuts) and collections reports."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, model_validator
from sqlalchemy import func, select

from ...db import to_jsonable
from ...engine.expressions import ExpressionError, MissingField, evaluate
from ...engine.fields import COLLECTIONS_FIELDS, Dataset, FieldType
from ...engine.mapping import apply_mapping, enrich_loan, parse_csv, typed_view
from ...engine.money import ZERO, D
from ...models import BankAccount, CollectionRecord, DataMapping, ExpectedCashflow, IngestionBatch, LoanRecord, Period
from ..context import OpContext
from ..errors import Conflict, NotFound, ValidationFailed
from ..registry import OpInput, operation
from .common import get_entity, get_facility, to_dict


class MappingCreate(OpInput):
    name: str
    dataset: Literal["loan_tape", "collections", "bank_statement"]
    facility_id: str | None = Field(None, description="Omit for a mapping shared by all facilities (e.g. per servicer)")
    config: dict[str, Any] = Field(description='{"fields": {"<canonical>": {"source": "<column>", "transform": "percent", "date_format": "%d/%m/%Y", "value_map": {}, "default": ...}}, "keep_unmapped": true}')


@operation("mapping.create", summary="Save a mapping from a servicer file layout to canonical fields", permission="data.ingest", input=MappingCreate, mutating=True, tags=("data",))
def mapping_create(ctx: OpContext, p: MappingCreate) -> dict:
    facility_id = get_facility(ctx, p.facility_id).id if p.facility_id else None
    fields = p.config.get("fields", {})
    if not isinstance(fields, dict):
        raise ValidationFailed("config.fields must be an object")
    for name, rule in fields.items():
        if not isinstance(rule, dict):
            raise ValidationFailed(f"mapping for '{name}' must be an object")
        if "type" in rule and rule["type"] not in {t.value for t in FieldType}:
            raise ValidationFailed(f"unknown type '{rule['type']}' for '{name}'")
    m = DataMapping(facility_id=facility_id, name=p.name, dataset=p.dataset, config=p.config)
    ctx.session.add(m)
    ctx.session.flush()
    ctx.touch(m.id)
    return to_dict(m)


class MappingList(OpInput):
    facility_id: str | None = None
    dataset: Literal["loan_tape", "collections", "bank_statement"] | None = None


@operation("mapping.list", summary="List data mappings", permission="facility.read", input=MappingList, mutating=False, tags=("data",))
def mapping_list(ctx: OpContext, p: MappingList) -> list:
    q = select(DataMapping)
    if p.facility_id:
        f = get_facility(ctx, p.facility_id)
        q = q.where((DataMapping.facility_id == f.id) | (DataMapping.facility_id.is_(None)))
    if p.dataset:
        q = q.where(DataMapping.dataset == p.dataset)
    rows = ctx.session.execute(q.order_by(DataMapping.name)).scalars()
    return [to_dict(m) for m in rows if ctx.actor.can_access_facility(m.facility_id)]


class _IngestBase(OpInput):
    facility_id: str
    content: str | None = Field(None, description="CSV text (comma, semicolon, tab or pipe delimited)")
    rows: list[dict[str, Any]] | None = Field(None, description="Alternatively, rows as JSON objects")
    mapping_id: str | None = None
    mapping: dict[str, Any] | None = Field(None, description="Inline mapping (same shape as mapping.create config)")
    source_name: str | None = Field(None, description="File name or source system")
    allow_duplicate: bool = Field(False, description="Load even if identical content was loaded before")

    @model_validator(mode="after")
    def _one_source(self):
        if (self.content is None) == (self.rows is None):
            raise ValueError("provide exactly one of 'content' or 'rows'")
        return self


def _prepare(ctx: OpContext, p: _IngestBase, dataset: Dataset, facility_id: str) -> tuple[list[dict], list, str, dict | None]:
    raw_rows = parse_csv(p.content) if p.content is not None else [dict(r) for r in p.rows or []]
    digest = hashlib.sha256((p.content if p.content is not None else json.dumps(to_jsonable(p.rows), sort_keys=True)).encode()).hexdigest()
    if not p.allow_duplicate:
        dup = ctx.session.execute(select(IngestionBatch).where(
            IngestionBatch.facility_id == facility_id, IngestionBatch.dataset == dataset.value,
            IngestionBatch.content_hash == digest, IngestionBatch.status == "loaded")).scalar_one_or_none()
        if dup:
            raise Conflict(f"identical {dataset.value} content already loaded as batch {dup.id}", details={"batch_id": dup.id})
    mapping = p.mapping
    if p.mapping_id:
        m = ctx.session.get(DataMapping, p.mapping_id)
        if m is None or m.dataset != dataset.value or m.facility_id not in (None, facility_id):
            raise NotFound(f"{dataset.value} mapping '{p.mapping_id}' not found")
        mapping = m.config
    if not raw_rows:
        raise ValidationFailed("no rows found in the input")
    records, issues = apply_mapping(raw_rows, dataset, mapping)
    return records, [i.to_dict() for i in issues], digest, mapping


class LoanTapeIngest(_IngestBase):
    as_of_date: date = Field(description="Cut-off date of the pool cut")
    reject_on_errors: bool = Field(False, description="Reject the whole file if any row has errors")


@operation("data.ingest_loan_tape", summary="Load a loan tape / pool cut from the servicing system", permission="data.ingest", input=LoanTapeIngest, mutating=True, tags=("data",))
def ingest_loan_tape(ctx: OpContext, p: LoanTapeIngest) -> dict:
    """Maps, validates and enriches each row (LTV, remaining term, seasoning ...).

    Rows with errors are reported and skipped (or the whole file is rejected with
    ``reject_on_errors``). A later tape with the same cut-off date supersedes the earlier one.
    """
    f = get_facility(ctx, p.facility_id)
    records, issues, digest, _ = _prepare(ctx, p, Dataset.LOAN_TAPE, f.id)
    errors = [i for i in issues if i["severity"] == "error"]
    if errors and p.reject_on_errors:
        raise ValidationFailed(f"{len(errors)} row errors; file rejected", details=issues[:200])
    seen: set[str] = set()
    for r in records:
        if r["loan_id"] in seen:
            raise ValidationFailed(f"duplicate loan_id {r['loan_id']} in file")
        seen.add(r["loan_id"])
    for prior in ctx.session.execute(select(IngestionBatch).where(
            IngestionBatch.facility_id == f.id, IngestionBatch.dataset == "loan_tape",
            IngestionBatch.as_of_date == p.as_of_date, IngestionBatch.status == "loaded")).scalars():
        prior.status = "superseded"
    total = sum((D(r["current_balance"]) for r in records), ZERO)
    batch = IngestionBatch(
        facility_id=f.id, dataset="loan_tape", as_of_date=p.as_of_date, source_name=p.source_name, mapping_id=p.mapping_id,
        content_hash=digest, row_count=len(records) + len({i["row"] for i in errors}), accepted_count=len(records),
        rejected_count=len({i["row"] for i in errors}), issues=issues, totals={"current_balance": total, "loan_count": len(records)},
        created_by=ctx.actor.id,
    )
    ctx.session.add(batch)
    ctx.session.flush()
    for r in records:
        enrich_loan(r, p.as_of_date)
        ctx.session.add(LoanRecord(batch_id=batch.id, facility_id=f.id, loan_id=r["loan_id"], as_of_date=p.as_of_date,
                                   current_balance=D(r["current_balance"]), data=to_jsonable(r)))
    ctx.touch(batch.id)
    return {**to_dict(batch, exclude=("issues",)), "issues": issues[:200], "issue_count": len(issues)}


_COLLECTION_DECIMALS = [f.name for f in COLLECTIONS_FIELDS if f.type == FieldType.DECIMAL]


class CollectionsIngest(_IngestBase):
    period: int | str = Field(description="Period sequence number or id the collections belong to")
    create_expected_remittance: bool = Field(True, description="Add the net remittance as an expected receipt on the collection account")


@operation("data.ingest_collections", summary="Load a servicer collections report for a collection period", permission="data.ingest", input=CollectionsIngest, mutating=True, tags=("data",))
def ingest_collections(ctx: OpContext, p: CollectionsIngest) -> dict:
    f = get_facility(ctx, p.facility_id)
    period = _period(ctx, f.id, p.period)
    if period.status == "finalised":
        raise Conflict(f"period {period.sequence} is finalised")
    records, issues, digest, _ = _prepare(ctx, p, Dataset.COLLECTIONS, f.id)
    for prior in ctx.session.execute(select(IngestionBatch).where(
            IngestionBatch.facility_id == f.id, IngestionBatch.dataset == "collections",
            IngestionBatch.period_id == period.id, IngestionBatch.status == "loaded")).scalars():
        prior.status = "superseded"
    totals = {k: sum((D(r.get(k), ZERO) for r in records), ZERO) for k in _COLLECTION_DECIMALS}
    errors = {i["row"] for i in issues if i["severity"] == "error"}
    batch = IngestionBatch(
        facility_id=f.id, dataset="collections", as_of_date=period.collection_end, period_id=period.id, source_name=p.source_name,
        mapping_id=p.mapping_id, content_hash=digest, row_count=len(records) + len(errors), accepted_count=len(records),
        rejected_count=len(errors), issues=issues, totals=totals, created_by=ctx.actor.id,
    )
    ctx.session.add(batch)
    ctx.session.flush()
    for r in records:
        ctx.session.add(CollectionRecord(batch_id=batch.id, facility_id=f.id, period_id=period.id, loan_id=r.get("loan_id"), data=to_jsonable(r)))
    remittance = net_remittance(totals)
    expected = None
    account_code = (f.settings or {}).get("collection_account")
    if p.create_expected_remittance and account_code:
        acct = ctx.session.execute(select(BankAccount).where(BankAccount.facility_id == f.id, BankAccount.code == account_code)).scalar_one_or_none()
        if acct:
            # Replace an earlier expected remittance for this period that has not yet been matched.
            for old in ctx.session.execute(select(ExpectedCashflow).where(
                    ExpectedCashflow.account_id == acct.id, ExpectedCashflow.period_id == period.id,
                    ExpectedCashflow.source_type == "collections", ExpectedCashflow.status == "unmatched")).scalars():
                ctx.session.delete(old)
            dates = [date.fromisoformat(r["remittance_date"]) for r in records if r.get("remittance_date")]
            expected = ExpectedCashflow(
                facility_id=f.id, account_id=acct.id, period_id=period.id, expected_date=max(dates) if dates else period.collection_end,
                amount=remittance, description=f"Servicer remittance period {period.sequence}", source_type="collections", source_ref=batch.id)
            ctx.session.add(expected)
    ctx.touch(batch.id)
    return {**to_dict(batch, exclude=("issues",)), "issues": issues[:200], "net_remittance": remittance,
            "expected_cashflow_created": expected is not None}


def net_remittance(totals: dict[str, Decimal]) -> Decimal:
    t = lambda k: D(totals.get(k), ZERO)  # noqa: E731
    return (t("interest_collected") + t("fees_collected") + t("other_income") + t("scheduled_principal") + t("prepayments")
            + t("recoveries") - t("servicer_fee_deducted") - t("further_advances"))


def _period(ctx: OpContext, facility_id: str, ref: int | str) -> Period:
    q = select(Period).where(Period.facility_id == facility_id)
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        q = q.where(Period.sequence == int(ref))
    else:
        q = q.where(Period.id == ref)
    period = ctx.session.execute(q).scalar_one_or_none()
    if period is None:
        raise NotFound(f"period '{ref}' not found")
    ctx.touch(period.id)
    return period


class BatchList(OpInput):
    facility_id: str
    dataset: Literal["loan_tape", "collections", "bank_statement"] | None = None
    include_superseded: bool = False


@operation("data.batches", summary="List ingestion batches with row counts and data quality issues", permission="facility.read", input=BatchList, mutating=False, tags=("data",))
def batches(ctx: OpContext, p: BatchList) -> list:
    f = get_facility(ctx, p.facility_id)
    q = select(IngestionBatch).where(IngestionBatch.facility_id == f.id)
    if p.dataset:
        q = q.where(IngestionBatch.dataset == p.dataset)
    if not p.include_superseded:
        q = q.where(IngestionBatch.status == "loaded")
    return [{**to_dict(b, exclude=("issues",)), "issue_count": len(b.issues or [])}
            for b in ctx.session.execute(q.order_by(IngestionBatch.created_at.desc())).scalars()]


class BatchGet(OpInput):
    batch_id: str


@operation("data.batch_get", summary="Ingestion batch detail including every data quality issue", permission="facility.read", input=BatchGet, mutating=False, tags=("data",))
def batch_get(ctx: OpContext, p: BatchGet) -> dict:
    return to_dict(get_entity(ctx, IngestionBatch, p.batch_id, label="batch"))


def latest_loan_batch(ctx: OpContext, facility_id: str, as_of: date | None = None) -> IngestionBatch | None:
    q = select(IngestionBatch).where(IngestionBatch.facility_id == facility_id, IngestionBatch.dataset == "loan_tape",
                                     IngestionBatch.status == "loaded")
    if as_of:
        q = q.where(IngestionBatch.as_of_date <= as_of)
    return ctx.session.execute(q.order_by(IngestionBatch.as_of_date.desc(), IngestionBatch.created_at.desc()).limit(1)).scalar_one_or_none()


def load_loans(ctx: OpContext, batch_id: str) -> list[dict[str, Any]]:
    rows = ctx.session.execute(select(LoanRecord).where(LoanRecord.batch_id == batch_id).order_by(LoanRecord.loan_id)).scalars()
    return [typed_view(r.data) for r in rows]


class LoanQuery(OpInput):
    facility_id: str
    batch_id: str | None = Field(None, description="Default: latest loan tape")
    filter: str | None = Field(None, description="Rule expression, e.g. \"property_type == 'office' and ltv > 0.6\"")
    fields: list[str] | None = Field(None, description="Fields to return (default all)")
    limit: int = Field(500, ge=1, le=10000)


@operation("data.loans", summary="Query loans in a loan tape, optionally filtered by a rule expression", permission="facility.read", input=LoanQuery, mutating=False, tags=("data",))
def loans(ctx: OpContext, p: LoanQuery) -> dict:
    f = get_facility(ctx, p.facility_id)
    batch = get_entity(ctx, IngestionBatch, p.batch_id, f.id, "batch") if p.batch_id else latest_loan_batch(ctx, f.id)
    if batch is None:
        raise NotFound("no loan tape loaded")
    out = []
    for loan in load_loans(ctx, batch.id):
        if p.filter:
            try:
                if not evaluate(p.filter, loan):
                    continue
            except MissingField:
                continue
            except ExpressionError as exc:
                raise ValidationFailed(f"invalid filter: {exc}") from exc
        out.append({k: v for k, v in loan.items() if not p.fields or k in p.fields or k == "loan_id"})
        if len(out) >= p.limit:
            break
    return {"batch_id": batch.id, "as_of_date": batch.as_of_date, "count": len(out), "loans": out}


class CollectionsSummary(OpInput):
    facility_id: str
    period: int | str


@operation("data.collections_summary", summary="Totals of the collections loaded for a period", permission="facility.read", input=CollectionsSummary, mutating=False, tags=("data",))
def collections_summary(ctx: OpContext, p: CollectionsSummary) -> dict:
    f = get_facility(ctx, p.facility_id)
    period = _period(ctx, f.id, p.period)
    batch = ctx.session.execute(select(IngestionBatch).where(
        IngestionBatch.facility_id == f.id, IngestionBatch.dataset == "collections", IngestionBatch.period_id == period.id,
        IngestionBatch.status == "loaded")).scalar_one_or_none()
    if batch is None:
        raise NotFound(f"no collections loaded for period {period.sequence}")
    count = ctx.session.execute(select(func.count()).select_from(CollectionRecord).where(CollectionRecord.batch_id == batch.id)).scalar_one()
    return {"period": period.sequence, "batch_id": batch.id, "records": count, "totals": batch.totals,
            "net_remittance": net_remittance({k: D(v) for k, v in batch.totals.items()})}
