"""Eligibility criteria, portfolio parameters, compliance runs and stratifications."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field
from sqlalchemy import select

from ...engine import compliance as engine
from ...engine.expressions import ExpressionError, compile_expression, referenced_names
from ...engine.fields import Dataset, field_map
from ...models import ComplianceRun, EligibilityCriterion, IngestionBatch, LegalDocument, LoanEligibilityResult, PortfolioTest
from ..context import OpContext
from ..errors import Conflict, NotFound, ValidationFailed
from ..registry import OpInput, operation
from .common import class_outstanding, get_entity, get_facility, note_classes, to_dict
from .data import latest_loan_batch, load_loans

Treatment = Literal["limit", "covenant", "information"]
TestType = Literal["concentration", "filtered_concentration", "top_n_concentration", "largest_exposure",
                   "weighted_average", "count", "aggregate"]


def _check_expression(ctx: OpContext, facility_id: str, expression: str | None, label: str) -> list[str]:
    if not expression:
        return []
    try:
        compile_expression(expression)
        names = referenced_names(expression)
    except ExpressionError as exc:
        raise ValidationFailed(f"{label}: {exc}") from exc
    known = set(field_map(Dataset.LOAN_TAPE))
    batch = latest_loan_batch(ctx, facility_id)
    if batch is not None:
        sample = load_loans(ctx, batch.id)[:200]
        for loan in sample:
            known.update(loan.keys())
    unknown = sorted(names - known)
    return [f"{label} references fields not in the catalogue or latest tape: {', '.join(unknown)}"] if unknown else []


def _check_document(ctx: OpContext, facility_id: str, document_id: str | None) -> None:
    if document_id:
        doc = ctx.session.get(LegalDocument, document_id)
        if doc is None or doc.facility_id != facility_id:
            raise NotFound(f"document '{document_id}' not found")


class CriterionCreate(OpInput):
    facility_id: str
    code: str = Field(description="e.g. 'EC-07'")
    name: str
    expression: str = Field(description="Must be true for an eligible loan, e.g. \"ltv <= 0.65\"")
    applies_when: str | None = Field(None, description="Only test loans matching this, e.g. \"loan_stage == 'construction'\"")
    severity: Literal["exclude", "warning"] = "exclude"
    description: str | None = None
    document_id: str | None = None
    clause_ref: str | None = Field(None, description="Clause in the legal documents, e.g. 'Facility Agreement cl. 12.1(f)'")


@operation("criterion.create", summary="Add an eligibility criterion mapped to the legal documents", permission="rules.configure", input=CriterionCreate, mutating=True, tags=("rules",))
def criterion_create(ctx: OpContext, p: CriterionCreate) -> dict:
    f = get_facility(ctx, p.facility_id)
    _check_document(ctx, f.id, p.document_id)
    if ctx.session.execute(select(EligibilityCriterion).where(EligibilityCriterion.facility_id == f.id, EligibilityCriterion.code == p.code)).scalar_one_or_none():
        raise Conflict(f"criterion {p.code} already exists; use criterion.update")
    warnings = _check_expression(ctx, f.id, p.expression, "expression") + _check_expression(ctx, f.id, p.applies_when, "applies_when")
    c = EligibilityCriterion(**{**p.model_dump(), "facility_id": f.id})
    ctx.session.add(c)
    ctx.session.flush()
    ctx.touch(c.id)
    return {**to_dict(c), "warnings": warnings}


class CriterionUpdate(OpInput):
    facility_id: str
    code: str
    name: str | None = None
    expression: str | None = None
    applies_when: str | None = None
    severity: Literal["exclude", "warning"] | None = None
    description: str | None = None
    clause_ref: str | None = None
    document_id: str | None = None
    active: bool | None = None


@operation("criterion.update", summary="Change an eligibility criterion (versioned)", permission="rules.configure", input=CriterionUpdate, mutating=True, tags=("rules",))
def criterion_update(ctx: OpContext, p: CriterionUpdate) -> dict:
    f = get_facility(ctx, p.facility_id)
    c = ctx.session.execute(select(EligibilityCriterion).where(EligibilityCriterion.facility_id == f.id, EligibilityCriterion.code == p.code)).scalar_one_or_none()
    if c is None:
        raise NotFound(f"criterion {p.code} not found")
    _check_document(ctx, f.id, p.document_id)
    warnings = _check_expression(ctx, f.id, p.expression, "expression") + _check_expression(ctx, f.id, p.applies_when, "applies_when")
    for field in ("name", "expression", "applies_when", "severity", "description", "clause_ref", "document_id", "active"):
        v = getattr(p, field)
        if v is not None:
            setattr(c, field, v)
    c.version += 1
    ctx.touch(c.id)
    return {**to_dict(c), "warnings": warnings}


class RuleList(OpInput):
    facility_id: str
    include_inactive: bool = False


@operation("criterion.list", summary="List eligibility criteria", permission="facility.read", input=RuleList, mutating=False, tags=("rules",))
def criterion_list(ctx: OpContext, p: RuleList) -> list:
    f = get_facility(ctx, p.facility_id)
    q = select(EligibilityCriterion).where(EligibilityCriterion.facility_id == f.id)
    if not p.include_inactive:
        q = q.where(EligibilityCriterion.active.is_(True))
    return [to_dict(c) for c in ctx.session.execute(q.order_by(EligibilityCriterion.code)).scalars()]


class TestCreate(OpInput):
    facility_id: str
    code: str = Field(description="e.g. 'PP-03'")
    name: str
    test_type: TestType
    config: dict[str, Any] = Field(description="Type specific, e.g. {'group_by': 'property_type', 'max': 0.4, 'limits': {'hotel': 0.1}}")
    treatment: Treatment = "limit"
    event_code: str | None = Field(None, description="Event to flag when breached, e.g. 'REVIEW_EVENT'")
    description: str | None = None
    document_id: str | None = None
    clause_ref: str | None = None


def _validate_test_config(ctx: OpContext, facility_id: str, test_type: str, config: dict[str, Any]) -> list[str]:
    required = {
        "concentration": ["group_by"], "filtered_concentration": ["filter"], "top_n_concentration": ["group_by", "n"],
        "weighted_average": ["field"], "largest_exposure": [], "count": [], "aggregate": [],
    }[test_type]
    missing = [k for k in required if k not in config]
    if missing:
        raise ValidationFailed(f"{test_type} config needs: {', '.join(missing)}")
    if test_type == "largest_exposure" and "max" not in config and "max_pct" not in config:
        raise ValidationFailed("largest_exposure config needs 'max' or 'max_pct'")
    if test_type not in ("concentration", "largest_exposure") and "max" not in config and "min" not in config:
        raise ValidationFailed(f"{test_type} config needs 'max' and/or 'min'")
    return _check_expression(ctx, facility_id, config.get("filter"), "filter")


@operation("test.create", summary="Add a portfolio parameter / concentration limit / covenant test", permission="rules.configure", input=TestCreate, mutating=True, tags=("rules",))
def test_create(ctx: OpContext, p: TestCreate) -> dict:
    f = get_facility(ctx, p.facility_id)
    _check_document(ctx, f.id, p.document_id)
    if ctx.session.execute(select(PortfolioTest).where(PortfolioTest.facility_id == f.id, PortfolioTest.code == p.code)).scalar_one_or_none():
        raise Conflict(f"test {p.code} already exists; use test.update")
    warnings = _validate_test_config(ctx, f.id, p.test_type, p.config)
    t = PortfolioTest(**{**p.model_dump(), "facility_id": f.id})
    ctx.session.add(t)
    ctx.session.flush()
    ctx.touch(t.id)
    return {**to_dict(t), "warnings": warnings}


class TestUpdate(OpInput):
    facility_id: str
    code: str
    name: str | None = None
    config: dict[str, Any] | None = None
    treatment: Treatment | None = None
    event_code: str | None = None
    description: str | None = None
    clause_ref: str | None = None
    active: bool | None = None


@operation("test.update", summary="Change a portfolio test (versioned)", permission="rules.configure", input=TestUpdate, mutating=True, tags=("rules",))
def test_update(ctx: OpContext, p: TestUpdate) -> dict:
    f = get_facility(ctx, p.facility_id)
    t = ctx.session.execute(select(PortfolioTest).where(PortfolioTest.facility_id == f.id, PortfolioTest.code == p.code)).scalar_one_or_none()
    if t is None:
        raise NotFound(f"test {p.code} not found")
    warnings = _validate_test_config(ctx, f.id, t.test_type, p.config) if p.config is not None else []
    for field in ("name", "config", "treatment", "event_code", "description", "clause_ref", "active"):
        v = getattr(p, field)
        if v is not None:
            setattr(t, field, v)
    t.version += 1
    ctx.touch(t.id)
    return {**to_dict(t), "warnings": warnings}


@operation("test.list", summary="List portfolio tests", permission="facility.read", input=RuleList, mutating=False, tags=("rules",))
def test_list(ctx: OpContext, p: RuleList) -> list:
    f = get_facility(ctx, p.facility_id)
    q = select(PortfolioTest).where(PortfolioTest.facility_id == f.id)
    if not p.include_inactive:
        q = q.where(PortfolioTest.active.is_(True))
    return [to_dict(t) for t in ctx.session.execute(q.order_by(PortfolioTest.code)).scalars()]


class ExpressionCheck(OpInput):
    facility_id: str
    expression: str
    sample_size: int = Field(20, ge=0, le=500, description="Evaluate against this many loans from the latest tape")


@operation("rules.try_expression", summary="Validate an expression and preview it against the latest loan tape", permission="facility.read", input=ExpressionCheck, mutating=False, tags=("rules",))
def try_expression(ctx: OpContext, p: ExpressionCheck) -> dict:
    from ...engine.expressions import MissingField, evaluate

    f = get_facility(ctx, p.facility_id)
    warnings = _check_expression(ctx, f.id, p.expression, "expression")
    batch = latest_loan_batch(ctx, f.id)
    results = []
    counts = {"true": 0, "false": 0, "missing_data": 0, "error": 0}
    if batch is not None:
        for loan in load_loans(ctx, batch.id):
            try:
                value = evaluate(p.expression, loan)
                counts["true" if value else "false"] += 1
                outcome: Any = value
            except MissingField as exc:
                counts["missing_data"] += 1
                outcome = f"missing: {exc.name}"
            except ExpressionError as exc:
                counts["error"] += 1
                outcome = f"error: {exc}"
            if len(results) < p.sample_size:
                results.append({"loan_id": loan.get("loan_id"), "result": outcome})
    return {"valid": True, "fields": sorted(referenced_names(p.expression)), "warnings": warnings,
            "batch_id": batch.id if batch else None, "counts": counts, "sample": results}


class ComplianceRunInput(OpInput):
    facility_id: str
    batch_id: str | None = Field(None, description="Loan tape batch (default: latest)")
    as_of_date: date | None = Field(None, description="Date for note balances (default: tape cut-off)")
    period_id: str | None = None
    purpose: Literal["adhoc", "drawdown", "period_end", "pre_acquisition"] = "adhoc"


def rules_snapshot(ctx: OpContext, facility_id: str) -> tuple[list[dict], list[dict]]:
    criteria = [
        {"code": c.code, "name": c.name, "expression": c.expression, "applies_when": c.applies_when, "severity": c.severity,
         "clause_ref": c.clause_ref, "version": c.version}
        for c in ctx.session.execute(select(EligibilityCriterion).where(
            EligibilityCriterion.facility_id == facility_id, EligibilityCriterion.active.is_(True)).order_by(EligibilityCriterion.code)).scalars()
    ]
    tests = [
        {"code": t.code, "name": t.name, "test_type": t.test_type, "config": t.config, "treatment": t.treatment,
         "event_code": t.event_code, "clause_ref": t.clause_ref, "version": t.version}
        for t in ctx.session.execute(select(PortfolioTest).where(
            PortfolioTest.facility_id == facility_id, PortfolioTest.active.is_(True)).order_by(PortfolioTest.code)).scalars()
    ]
    return criteria, tests


@operation("compliance.run", summary="Run eligibility criteria, portfolio tests and the borrowing base on a loan tape", permission="compliance.run", input=ComplianceRunInput, mutating=True, tags=("compliance",))
def compliance_run(ctx: OpContext, p: ComplianceRunInput) -> dict:
    """Results are stored with a snapshot of the rule versions used, so any run can be reproduced."""
    f = get_facility(ctx, p.facility_id)
    batch = get_entity(ctx, IngestionBatch, p.batch_id, f.id, "batch") if p.batch_id else latest_loan_batch(ctx, f.id)
    if batch is None or batch.dataset != "loan_tape":
        raise NotFound("no loan tape loaded for this facility")
    as_of = p.as_of_date or batch.as_of_date
    loans = load_loans(ctx, batch.id)
    criteria, tests = rules_snapshot(ctx, f.id)
    outstanding = class_outstanding(ctx, f.id, as_of)
    classes = [{"name": c.name, "rank": c.rank, "commitment": c.commitment, "advance_rate": c.advance_rate,
                "outstanding": outstanding.get(c.id, Decimal(0))} for c in note_classes(ctx, f.id)]
    result = engine.run_compliance(loans, criteria, tests, classes)
    run = ComplianceRun(
        facility_id=f.id, batch_id=batch.id, as_of_date=as_of, period_id=p.period_id, purpose=p.purpose,
        summary=result["summary"], borrowing_base=result["borrowing_base"], pool=result["pool"],
        test_results=result["test_results"], rules_snapshot={"criteria": criteria, "tests": tests}, created_by=ctx.actor.id,
    )
    ctx.session.add(run)
    ctx.session.flush()
    for r in result["loan_results"]:
        ctx.session.add(LoanEligibilityResult(run_id=run.id, loan_id=r["loan_id"], balance=r["balance"], eligible=r["eligible"],
                                              failures=r["failures"], warnings=r["warnings"]))
    ctx.touch(run.id, batch.id)
    ineligible = [r for r in result["loan_results"] if not r["eligible"]]
    return {"id": run.id, "batch_id": batch.id, "as_of_date": as_of, "summary": result["summary"],
            "borrowing_base": result["borrowing_base"], "pool": result["pool"], "test_results": result["test_results"],
            "ineligible_loans": ineligible[:100]}


class RunRef(OpInput):
    run_id: str


@operation("compliance.get", summary="Compliance run results", permission="facility.read", input=RunRef, mutating=False, tags=("compliance",))
def compliance_get(ctx: OpContext, p: RunRef) -> dict:
    return to_dict(get_entity(ctx, ComplianceRun, p.run_id, label="compliance run"))


class LoanResultsQuery(OpInput):
    run_id: str
    only_ineligible: bool = False
    criterion: str | None = Field(None, description="Only loans failing this criterion code")


@operation("compliance.loan_results", summary="Loan-level eligibility results for a compliance run", permission="facility.read", input=LoanResultsQuery, mutating=False, tags=("compliance",))
def compliance_loan_results(ctx: OpContext, p: LoanResultsQuery) -> list:
    get_entity(ctx, ComplianceRun, p.run_id, label="compliance run")
    q = select(LoanEligibilityResult).where(LoanEligibilityResult.run_id == p.run_id)
    if p.only_ineligible:
        q = q.where(LoanEligibilityResult.eligible.is_(False))
    rows = [to_dict(r, exclude=("id", "run_id")) for r in ctx.session.execute(q.order_by(LoanEligibilityResult.loan_id)).scalars()]
    if p.criterion:
        rows = [r for r in rows if any(x["code"] == p.criterion for x in r["failures"] + r["warnings"])]
    return rows


class ComplianceList(OpInput):
    facility_id: str
    limit: int = Field(20, ge=1, le=200)


@operation("compliance.list", summary="Recent compliance runs with headline results", permission="facility.read", input=ComplianceList, mutating=False, tags=("compliance",))
def compliance_list(ctx: OpContext, p: ComplianceList) -> list:
    f = get_facility(ctx, p.facility_id)
    runs = ctx.session.execute(select(ComplianceRun).where(ComplianceRun.facility_id == f.id)
                               .order_by(ComplianceRun.created_at.desc()).limit(p.limit)).scalars()
    return [{"id": r.id, "as_of_date": r.as_of_date, "purpose": r.purpose, "batch_id": r.batch_id,
             "created_at": r.created_at, "summary": r.summary} for r in runs]


class StratifyInput(OpInput):
    facility_id: str
    field: str = Field(description="e.g. property_type, property_state, ltv, remaining_term_months")
    buckets: list[Decimal] | None = Field(None, description="Upper bounds for numeric fields, e.g. [0.5, 0.6, 0.65, 0.7]")
    batch_id: str | None = None
    eligible_only_run_id: str | None = Field(None, description="Restrict to loans eligible in this compliance run")
    measure: str = "current_balance"


@operation("pool.stratify", summary="Stratify the pool by any field", permission="facility.read", input=StratifyInput, mutating=False, tags=("compliance",))
def pool_stratify(ctx: OpContext, p: StratifyInput) -> dict:
    f = get_facility(ctx, p.facility_id)
    batch = get_entity(ctx, IngestionBatch, p.batch_id, f.id, "batch") if p.batch_id else latest_loan_batch(ctx, f.id)
    if batch is None:
        raise NotFound("no loan tape loaded")
    loans = load_loans(ctx, batch.id)
    if p.eligible_only_run_id:
        get_entity(ctx, ComplianceRun, p.eligible_only_run_id, f.id, "compliance run")
        eligible = {r.loan_id for r in ctx.session.execute(select(LoanEligibilityResult).where(
            LoanEligibilityResult.run_id == p.eligible_only_run_id, LoanEligibilityResult.eligible.is_(True))).scalars()}
        loans = [l for l in loans if l.get("loan_id") in eligible]
    return {"batch_id": batch.id, "field": p.field, "rows": engine.stratify(loans, p.field, p.buckets, p.measure)}
