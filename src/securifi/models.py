"""ORM models.

Grouped by area: identity & governance, facility structure, note register, data ingestion,
rules & compliance, bank accounts, waterfall & period runs, reporting.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, JSONDoc, Money, new_id, utcnow


def _id(prefix: str):
    return mapped_column(String(32), primary_key=True, default=lambda: new_id(prefix))


def _fk(target: str, nullable: bool = False, index: bool = True):
    return mapped_column(String(32), ForeignKey(target), nullable=nullable, index=index)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------- identity & governance


class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[str] = _id("usr")
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32))
    # None = all facilities; otherwise a list of facility ids the user may act on.
    facility_scope: Mapped[list[str] | None] = mapped_column(JSONDoc, nullable=True)
    is_service_account: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ApiKey(TimestampMixin, Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = _id("key")
    user_id: Mapped[str] = _fk("users.id")
    name: Mapped[str] = mapped_column(String(255))
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    # Optional narrowing of the user's permissions for this key (e.g. a read-only AI agent key).
    scopes: Mapped[list[str] | None] = mapped_column(JSONDoc, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEvent(Base):
    """Append-only, hash-chained record of every operation invoked through any entry point."""

    __tablename__ = "audit_events"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(32), unique=True, default=lambda: new_id("aud"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    actor_label: Mapped[str] = mapped_column(String(255))
    api_key_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    channel: Mapped[str] = mapped_column(String(16), index=True)
    operation: Mapped[str] = mapped_column(String(128), index=True)
    mutating: Mapped[bool] = mapped_column(Boolean, default=False)
    facility_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    outcome: Mapped[str] = mapped_column(String(16), index=True)  # success | dry_run | denied | invalid | failed
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONDoc, nullable=True)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONDoc, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_refs: Mapped[list[str] | None] = mapped_column(JSONDoc, nullable=True)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class AuditChainHead(Base):
    """Single-row lock that serialises appends to the audit hash chain."""

    __tablename__ = "audit_chain_head"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_hash: Mapped[str] = mapped_column(String(64))
    last_seq: Mapped[int] = mapped_column(Integer, default=0)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    actor_id: Mapped[str | None] = mapped_column(String(32))
    operation: Mapped[str] = mapped_column(String(128))
    result: Mapped[dict[str, Any]] = mapped_column(JSONDoc)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------- facility structure


class Facility(TimestampMixin, Base):
    """A warehouse facility or securitisation trust."""

    __tablename__ = "facilities"
    id: Mapped[str] = _id("fac")
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    facility_type: Mapped[str] = mapped_column(String(32))  # warehouse | term_trust | repo | bilateral
    status: Mapped[str] = mapped_column(String(32), default="active")
    currency: Mapped[str] = mapped_column(String(3), default="AUD")
    trust_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    trustee: Mapped[str | None] = mapped_column(String(255), nullable=True)
    security_trustee: Mapped[str | None] = mapped_column(String(255), nullable=True)
    trust_manager: Mapped[str | None] = mapped_column(String(255), nullable=True)
    originator: Mapped[str | None] = mapped_column(String(255), nullable=True)
    servicer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    closing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    revolving_period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    final_maturity_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    facility_limit: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    day_count: Mapped[str] = mapped_column(String(16), default="ACT/365F")
    business_day_convention: Mapped[str] = mapped_column(String(32), default="modified_following")
    holidays: Mapped[list[str]] = mapped_column(JSONDoc, default=list)
    # Schedule and operating settings (payment day, frequency, accounts, approval policy ...).
    settings: Mapped[dict[str, Any]] = mapped_column(JSONDoc, default=dict)
    # Custom variables usable in waterfall conditions and expressions.
    variables: Mapped[dict[str, Any]] = mapped_column(JSONDoc, default=dict)


class LegalDocument(TimestampMixin, Base):
    __tablename__ = "legal_documents"
    id: Mapped[str] = _id("doc")
    facility_id: Mapped[str] = _fk("facilities.id")
    title: Mapped[str] = mapped_column(String(255))
    doc_type: Mapped[str] = mapped_column(String(64))
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    executed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class NoteClass(TimestampMixin, Base):
    __tablename__ = "note_classes"
    __table_args__ = (UniqueConstraint("facility_id", "name"),)
    id: Mapped[str] = _id("ncl")
    facility_id: Mapped[str] = _fk("facilities.id")
    name: Mapped[str] = mapped_column(String(64))
    rank: Mapped[int] = mapped_column(Integer)
    commitment: Mapped[Decimal] = mapped_column(Money)
    rate_type: Mapped[str] = mapped_column(String(16), default="floating")
    base_rate_index: Mapped[str | None] = mapped_column(String(32), nullable=True)
    margin_bps: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    fixed_rate: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    base_rate_floor: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    day_count: Mapped[str | None] = mapped_column(String(16), nullable=True)
    advance_rate: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    is_residual: Mapped[bool] = mapped_column(Boolean, default=False)
    legal_final_maturity: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")


class Noteholder(TimestampMixin, Base):
    __tablename__ = "noteholders"
    id: Mapped[str] = _id("nh")
    name: Mapped[str] = mapped_column(String(255))
    short_name: Mapped[str] = mapped_column(String(64), unique=True)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lei: Mapped[str | None] = mapped_column(String(32), nullable=True)
    registration_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_details: Mapped[dict[str, Any]] = mapped_column(JSONDoc, default=dict)


class RegisterEntry(Base):
    """Immutable note register ledger. Positions are always derived from entries."""

    __tablename__ = "register_entries"
    id: Mapped[str] = _id("reg")
    facility_id: Mapped[str] = _fk("facilities.id")
    note_class_id: Mapped[str] = _fk("note_classes.id")
    noteholder_id: Mapped[str] = _fk("noteholders.id")
    entry_type: Mapped[str] = mapped_column(String(32))  # commitment | drawdown | repayment | transfer_in | transfer_out
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    period_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FeeDefinition(TimestampMixin, Base):
    __tablename__ = "fees"
    __table_args__ = (UniqueConstraint("facility_id", "code"),)
    id: Mapped[str] = _id("fee")
    facility_id: Mapped[str] = _fk("facilities.id")
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    payee: Mapped[str] = mapped_column(String(255))
    calculation: Mapped[str] = mapped_column(String(32))  # percent_per_annum | fixed_per_annum | fixed_per_period
    basis: Mapped[str | None] = mapped_column(String(32), nullable=True)
    basis_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rate_bps: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    day_count: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tax_rate: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    minimum_amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Period(TimestampMixin, Base):
    __tablename__ = "periods"
    __table_args__ = (UniqueConstraint("facility_id", "sequence"),)
    id: Mapped[str] = _id("per")
    facility_id: Mapped[str] = _fk("facilities.id")
    sequence: Mapped[int] = mapped_column(Integer)
    collection_start: Mapped[date] = mapped_column(Date)
    collection_end: Mapped[date] = mapped_column(Date)
    interest_start: Mapped[date] = mapped_column(Date)
    interest_end: Mapped[date] = mapped_column(Date)
    determination_date: Mapped[date] = mapped_column(Date)
    payment_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | calculated | approved | finalised


class RateFixing(Base):
    __tablename__ = "rate_fixings"
    __table_args__ = (UniqueConstraint("index_name", "fixing_date"),)
    id: Mapped[str] = _id("fix")
    index_name: Mapped[str] = mapped_column(String(32), index=True)
    fixing_date: Mapped[date] = mapped_column(Date)
    rate: Mapped[Decimal] = mapped_column(Money)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FacilityEvent(TimestampMixin, Base):
    """Trigger events: amortisation events, events of default, review events, etc."""

    __tablename__ = "facility_events"
    id: Mapped[str] = _id("evt")
    facility_id: Mapped[str] = _fk("facilities.id")
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | cleared
    declared_on: Mapped[date] = mapped_column(Date)
    cleared_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


# --------------------------------------------------------------------------- data ingestion


class DataMapping(TimestampMixin, Base):
    __tablename__ = "data_mappings"
    id: Mapped[str] = _id("map")
    facility_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("facilities.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    dataset: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict[str, Any]] = mapped_column(JSONDoc, default=dict)


class IngestionBatch(Base):
    __tablename__ = "ingestion_batches"
    id: Mapped[str] = _id("bat")
    facility_id: Mapped[str] = _fk("facilities.id")
    dataset: Mapped[str] = mapped_column(String(32))
    as_of_date: Mapped[date] = mapped_column(Date)
    period_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mapping_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    accepted_count: Mapped[int] = mapped_column(Integer)
    rejected_count: Mapped[int] = mapped_column(Integer)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSONDoc, default=list)
    totals: Mapped[dict[str, Any]] = mapped_column(JSONDoc, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="loaded")  # loaded | superseded
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LoanRecord(Base):
    __tablename__ = "loan_records"
    __table_args__ = (Index("ix_loan_batch_loan", "batch_id", "loan_id"),)
    id: Mapped[str] = _id("ln")
    batch_id: Mapped[str] = _fk("ingestion_batches.id")
    facility_id: Mapped[str] = _fk("facilities.id")
    loan_id: Mapped[str] = mapped_column(String(64))
    as_of_date: Mapped[date] = mapped_column(Date)
    current_balance: Mapped[Decimal] = mapped_column(Money)
    data: Mapped[dict[str, Any]] = mapped_column(JSONDoc)


class CollectionRecord(Base):
    __tablename__ = "collection_records"
    id: Mapped[str] = _id("col")
    batch_id: Mapped[str] = _fk("ingestion_batches.id")
    facility_id: Mapped[str] = _fk("facilities.id")
    period_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    loan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONDoc)


# --------------------------------------------------------------------------- rules & compliance


class EligibilityCriterion(TimestampMixin, Base):
    __tablename__ = "eligibility_criteria"
    __table_args__ = (UniqueConstraint("facility_id", "code"),)
    id: Mapped[str] = _id("ec")
    facility_id: Mapped[str] = _fk("facilities.id")
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    expression: Mapped[str] = mapped_column(Text)
    applies_when: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(String(16), default="exclude")  # exclude | warning
    document_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    clause_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class PortfolioTest(TimestampMixin, Base):
    __tablename__ = "portfolio_tests"
    __table_args__ = (UniqueConstraint("facility_id", "code"),)
    id: Mapped[str] = _id("pt")
    facility_id: Mapped[str] = _fk("facilities.id")
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    test_type: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict[str, Any]] = mapped_column(JSONDoc, default=dict)
    treatment: Mapped[str] = mapped_column(String(16), default="limit")  # limit | covenant | information
    event_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    document_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    clause_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class ComplianceRun(Base):
    __tablename__ = "compliance_runs"
    id: Mapped[str] = _id("cr")
    facility_id: Mapped[str] = _fk("facilities.id")
    batch_id: Mapped[str] = _fk("ingestion_batches.id")
    as_of_date: Mapped[date] = mapped_column(Date)
    period_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    purpose: Mapped[str] = mapped_column(String(32), default="adhoc")
    summary: Mapped[dict[str, Any]] = mapped_column(JSONDoc)
    borrowing_base: Mapped[dict[str, Any] | None] = mapped_column(JSONDoc, nullable=True)
    pool: Mapped[dict[str, Any]] = mapped_column(JSONDoc)
    test_results: Mapped[list[dict[str, Any]]] = mapped_column(JSONDoc)
    rules_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONDoc)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LoanEligibilityResult(Base):
    __tablename__ = "loan_eligibility_results"
    id: Mapped[str] = _id("ler")
    run_id: Mapped[str] = _fk("compliance_runs.id")
    loan_id: Mapped[str] = mapped_column(String(64))
    balance: Mapped[Decimal] = mapped_column(Money)
    eligible: Mapped[bool] = mapped_column(Boolean)
    failures: Mapped[list[dict[str, Any]]] = mapped_column(JSONDoc, default=list)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSONDoc, default=list)


# --------------------------------------------------------------------------- bank accounts


class BankAccount(TimestampMixin, Base):
    __tablename__ = "bank_accounts"
    __table_args__ = (UniqueConstraint("facility_id", "code"),)
    id: Mapped[str] = _id("acc")
    facility_id: Mapped[str] = _fk("facilities.id")
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    account_type: Mapped[str] = mapped_column(String(32))  # collection | payment | reserve | liquidity | expense | other
    bank_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    branch_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    account_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="AUD")
    opening_balance: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    opening_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class BankTransaction(Base):
    __tablename__ = "bank_transactions"
    id: Mapped[str] = _id("btx")
    account_id: Mapped[str] = _fk("bank_accounts.id")
    batch_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    transaction_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    statement_balance: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="unmatched")  # unmatched | matched | ignored
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExpectedCashflow(Base):
    __tablename__ = "expected_cashflows"
    id: Mapped[str] = _id("ecf")
    facility_id: Mapped[str] = _fk("facilities.id")
    account_id: Mapped[str] = _fk("bank_accounts.id")
    period_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    expected_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Money)
    description: Mapped[str] = mapped_column(String(512))
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32))  # collections | waterfall | drawdown | manual
    source_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="unmatched")
    matched_transaction_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    match_rule: Mapped[str | None] = mapped_column(String(32), nullable=True)
    matched_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------- waterfall & period runs


class WaterfallDefinition(Base):
    __tablename__ = "waterfall_definitions"
    __table_args__ = (UniqueConstraint("facility_id", "version"),)
    id: Mapped[str] = _id("wf")
    facility_id: Mapped[str] = _fk("facilities.id")
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONDoc)
    available_funds: Mapped[dict[str, Any]] = mapped_column(JSONDoc)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PeriodRun(Base):
    __tablename__ = "period_runs"
    id: Mapped[str] = _id("run")
    facility_id: Mapped[str] = _fk("facilities.id")
    period_id: Mapped[str] = _fk("periods.id")
    run_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="calculated")  # calculated | approved | finalised | superseded | rejected
    compliance_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    waterfall_definition_id: Mapped[str] = mapped_column(String(32))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONDoc)
    outputs: Mapped[dict[str, Any]] = mapped_column(JSONDoc)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    prepared_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalised_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    finalised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[str] = _id("rpt")
    facility_id: Mapped[str] = _fk("facilities.id")
    period_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    period_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    report_type: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    is_final: Mapped[bool] = mapped_column(Boolean, default=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONDoc)
    content_hash: Mapped[str] = mapped_column(String(64))
    generated_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
