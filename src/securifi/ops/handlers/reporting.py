"""Investor reports and borrowing base certificates.

Every generated report is stored with a SHA-256 of its content, so what was sent to
financiers can always be reproduced and evidenced.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field
from sqlalchemy import select

from ...db import to_jsonable
from ...engine.compliance import stratify
from ...models import ComplianceRun, Facility, FacilityEvent, PeriodRun, Report
from ..context import OpContext
from ..errors import NotFound
from ..registry import OpInput, operation
from .accounts import reconcile_status, ReconStatus
from .common import get_entity, get_facility, to_dict
from .data import _period, load_loans
from .register import PositionsQuery, register_positions

DEFAULT_STRATS = [
    {"field": "property_type"},
    {"field": "property_state"},
    {"field": "loan_stage"},
    {"field": "ltv", "buckets": ["0.5", "0.55", "0.6", "0.65", "0.7", "0.75"]},
    {"field": "remaining_term_months", "buckets": ["6", "12", "24", "36", "60"]},
    {"field": "arrears_days", "buckets": ["0", "30", "60", "90"]},
]


def _store(ctx: OpContext, f: Facility, report_type: str, title: str, content: dict, is_final: bool,
           period_id: str | None = None, period_run_id: str | None = None) -> Report:
    payload = to_jsonable(content)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    r = Report(facility_id=f.id, period_id=period_id, period_run_id=period_run_id, report_type=report_type, title=title,
               is_final=is_final, content=payload, content_hash=digest, generated_by=ctx.actor.id)
    ctx.session.add(r)
    ctx.session.flush()
    ctx.touch(r.id)
    return r


def _header(f: Facility) -> dict:
    return {k: getattr(f, k) for k in ("code", "name", "facility_type", "currency", "trust_name", "trustee", "security_trustee",
                                       "trust_manager", "originator", "servicer", "closing_date", "revolving_period_end",
                                       "final_maturity_date", "facility_limit")}


class InvestorReportInput(OpInput):
    facility_id: str
    period: int | str
    run_id: str | None = Field(None, description="Default: the finalised run, else the latest calculated/approved run (marked DRAFT)")
    stratifications: list[dict] | None = Field(None, description="[{'field': 'property_type'}, {'field': 'ltv', 'buckets': [0.6, 0.7]}]")


@operation("report.investor", summary="Generate the periodic investor / financier report", permission="reports.generate", input=InvestorReportInput, mutating=True, tags=("reports",))
def report_investor(ctx: OpContext, p: InvestorReportInput) -> dict:
    """Sections: facility, period, pool summary & stratifications, eligibility, portfolio tests,
    borrowing base, note interest, fees, waterfall (sources and uses), note balances,
    noteholder distributions, bank reconciliation and trigger events."""
    f = get_facility(ctx, p.facility_id)
    period = _period(ctx, f.id, p.period)
    if p.run_id:
        run = get_entity(ctx, PeriodRun, p.run_id, f.id, "period run")
    else:
        run = ctx.session.execute(select(PeriodRun).where(PeriodRun.period_id == period.id, PeriodRun.status == "finalised")).scalar_one_or_none() \
            or ctx.session.execute(select(PeriodRun).where(PeriodRun.period_id == period.id, PeriodRun.status.in_(("calculated", "approved")))
                                   .order_by(PeriodRun.run_number.desc()).limit(1)).scalar_one_or_none()
    if run is None:
        raise NotFound(f"no period run for period {period.sequence}; run period_run.calculate first")
    cr = ctx.session.get(ComplianceRun, run.compliance_run_id) if run.compliance_run_id else None
    strats = []
    if cr is not None:
        loans = load_loans(ctx, cr.batch_id)
        for spec in p.stratifications or DEFAULT_STRATS:
            strats.append({"field": spec["field"], "rows": stratify(loans, spec["field"], spec.get("buckets"))})
    is_final = run.status == "finalised"
    content = {
        "status": "FINAL" if is_final else "DRAFT",
        "facility": _header(f),
        "period": to_dict(period),
        "run": {"id": run.id, "run_number": run.run_number, "status": run.status, "prepared_by": run.prepared_by,
                "approved_by": run.approved_by, "finalised_at": run.finalised_at},
        "pool": cr.pool if cr else None,
        "compliance": {"run_id": cr.id, "as_of_date": cr.as_of_date, "summary": cr.summary, "tests": cr.test_results} if cr else None,
        "borrowing_base": cr.borrowing_base if cr else None,
        "stratifications": strats,
        "interest": [{k: v for k, v in row.items() if k not in ("segments", "by_holder")} for row in run.outputs["interest"]],
        "fees": run.outputs["fees"],
        "available_funds": run.outputs["available_funds"],
        "collections": run.inputs["collections"],
        "waterfall": run.outputs["waterfall"]["lines"],
        "note_balances": run.outputs["note_balances"],
        "noteholder_distributions": run.outputs["holder_payments"],
        "carry_forward": run.outputs["carry_forward"],
        "register": register_positions(ctx, PositionsQuery(facility_id=f.id, as_of_date=period.payment_date)),
        "reconciliation": [{k: v for k, v in a.items() if k not in ("unmatched_expected", "unmatched_transactions")}
                           for a in reconcile_status(ctx, ReconStatus(facility_id=f.id, as_of_date=period.payment_date))],
        "events": [to_dict(e) for e in ctx.session.execute(select(FacilityEvent).where(FacilityEvent.facility_id == f.id)).scalars()],
        "warnings": run.outputs.get("warnings", []),
    }
    title = f"{f.name} - Investor Report - Period {period.sequence} ({period.payment_date.isoformat()})"
    r = _store(ctx, f, "investor_report", title, content, is_final, period.id, run.id)
    return {"id": r.id, "title": r.title, "is_final": r.is_final, "content_hash": r.content_hash, "content": r.content}


class BBCInput(OpInput):
    facility_id: str
    compliance_run_id: str | None = Field(None, description="Default: latest compliance run")


@operation("report.borrowing_base_certificate", summary="Generate a borrowing base certificate from a compliance run", permission="reports.generate", input=BBCInput, mutating=True, tags=("reports",))
def report_bbc(ctx: OpContext, p: BBCInput) -> dict:
    f = get_facility(ctx, p.facility_id)
    if p.compliance_run_id:
        cr = get_entity(ctx, ComplianceRun, p.compliance_run_id, f.id, "compliance run")
    else:
        cr = ctx.session.execute(select(ComplianceRun).where(ComplianceRun.facility_id == f.id).order_by(ComplianceRun.created_at.desc()).limit(1)).scalar_one_or_none()
    if cr is None:
        raise NotFound("no compliance run; run compliance.run first")
    content = {
        "status": "FINAL", "facility": _header(f), "as_of_date": cr.as_of_date, "compliance_run_id": cr.id,
        "summary": cr.summary, "borrowing_base": cr.borrowing_base, "tests": cr.test_results, "pool": cr.pool,
        "rules_snapshot": cr.rules_snapshot,
    }
    r = _store(ctx, f, "borrowing_base_certificate", f"{f.name} - Borrowing Base Certificate - {cr.as_of_date.isoformat()}", content, True)
    return {"id": r.id, "title": r.title, "content_hash": r.content_hash, "content": r.content}


class ReportRef(OpInput):
    report_id: str


@operation("report.get", summary="Retrieve a stored report", permission="facility.read", input=ReportRef, mutating=False, tags=("reports",))
def report_get(ctx: OpContext, p: ReportRef) -> dict:
    return to_dict(get_entity(ctx, Report, p.report_id, label="report"))


class ReportList(OpInput):
    facility_id: str
    report_type: Literal["investor_report", "borrowing_base_certificate"] | None = None


@operation("report.list", summary="List stored reports", permission="facility.read", input=ReportList, mutating=False, tags=("reports",))
def report_list(ctx: OpContext, p: ReportList) -> list:
    f = get_facility(ctx, p.facility_id)
    q = select(Report).where(Report.facility_id == f.id)
    if p.report_type:
        q = q.where(Report.report_type == p.report_type)
    return [to_dict(r, exclude=("content",)) for r in ctx.session.execute(q.order_by(Report.generated_at.desc())).scalars()]
