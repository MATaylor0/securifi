"""Demo data: a CRE warehouse facility taken through a full month-end cycle.

Everything is created by calling operations through the executor (as the system actor),
exactly as the UI, API, MCP and CLI would.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .ops.context import Actor, Channel, system_actor
from .ops.executor import execute

LOAN_TAPE = """Loan Number,Borrower,Sponsor,Property,Asset Class,State,Stage,Lien,Settlement Date,Expiry,Closing Balance,Limit,Valuation,Val Date,LVR %,ICR,Arrears Days,Default Flag
CRE-1001,Harbour Office Pty Ltd,Atlas Group,1 Harbour St,Office,NSW,investment,first,15/03/2025,15/03/2028,18500000,20000000,32000000,01/02/2026,57.8,2.10,0,N
CRE-1002,Westgate Logistics Trust,Westgate Capital,Westgate DC,Logistics,VIC,investment,first,01/06/2025,01/06/2028,22000000,22000000,37500000,01/12/2025,58.7,2.45,0,N
CRE-1003,Northshore Retail Pty Ltd,Atlas Group,Northshore Plaza,Retail,NSW,investment,first,10/09/2025,10/09/2027,9800000,10000000,15000000,01/09/2025,65.3,1.85,0,N
CRE-1004,Bayside Hotels Pty Ltd,Coastal Hospitality,Bayside Hotel,Hotel,QLD,investment,first,20/11/2025,20/11/2027,12000000,12000000,17000000,01/11/2025,70.6,1.60,0,N
CRE-1005,Riverbank Residences Pty Ltd,Riverbank Dev,Riverbank Towers,Residential,QLD,construction,first,01/08/2025,01/02/2028,14500000,26000000,40000000,01/08/2025,36.3,,0,N
CRE-1006,Southern Industrial Pty Ltd,Southern Property,Dandenong Estate,Industrial,VIC,investment,first,05/05/2025,05/05/2029,16000000,16000000,27000000,01/05/2025,59.3,2.30,0,N
CRE-1007,Metro Office Trust,Metro REIT,200 Collins St,Office,VIC,investment,first,12/12/2025,12/12/2028,25000000,25000000,38000000,01/12/2025,65.8,1.95,0,N
CRE-1008,Parkside Mixed Use Pty Ltd,Parkside Group,Parkside Quarter,Mixed Use,WA,investment,second,01/10/2025,01/10/2027,7000000,7000000,20000000,01/10/2025,35.0,1.70,0,N
CRE-1009,Atlas Industrial Pty Ltd,Atlas Group,Atlas Park,Industrial,NSW,investment,first,01/02/2026,01/02/2029,11000000,11000000,19000000,15/01/2026,57.9,2.20,0,N
CRE-1010,Gold Coast Land Pty Ltd,Coastal Hospitality,Surfers Site,Land,QLD,land,first,15/01/2026,15/07/2027,6500000,6500000,10000000,15/01/2026,65.0,,0,N
CRE-1011,Eastern Medical Pty Ltd,Eastern Health Property,Eastern Medical Centre,Healthcare,NSW,investment,first,01/04/2025,01/04/2028,8200000,8200000,14000000,01/04/2025,58.6,2.05,45,N
CRE-1012,Central Office Pty Ltd,Metro REIT,88 Queen St,Office,QLD,investment,first,01/03/2025,01/03/2027,13000000,13000000,19500000,01/03/2025,66.7,1.75,0,N
"""

LOAN_MAPPING = {
    "fields": {
        "loan_id": {"source": "Loan Number"},
        "borrower_name": {"source": "Borrower"},
        "borrower_group": {"source": "Sponsor"},
        "property_name": {"source": "Property"},
        "property_type": {"source": "Asset Class"},
        "property_state": {"source": "State"},
        "loan_stage": {"source": "Stage"},
        "lien_position": {"source": "Lien"},
        "origination_date": {"source": "Settlement Date", "date_format": "%d/%m/%Y"},
        "maturity_date": {"source": "Expiry", "date_format": "%d/%m/%Y"},
        "current_balance": {"source": "Closing Balance"},
        "commitment_amount": {"source": "Limit"},
        "valuation_amount": {"source": "Valuation"},
        "valuation_date": {"source": "Val Date", "date_format": "%d/%m/%Y"},
        "ltv": {"source": "LVR %", "transform": "percent"},
        "icr": {"source": "ICR"},
        "arrears_days": {"source": "Arrears Days"},
        "is_defaulted": {"source": "Default Flag"},
    },
    "keep_unmapped": True,
}

COLLECTIONS = """loan_id,interest_collected,scheduled_principal,prepayments,fees_collected,servicer_fee_deducted,remittance_date
CRE-1001,123000.00,0,0,2500,3100,2026-03-05
CRE-1002,146000.00,0,0,0,3600,2026-03-05
CRE-1003,68500.00,0,0,0,1600,2026-03-05
CRE-1004,92000.00,0,0,0,2000,2026-03-05
CRE-1005,0,0,0,0,2400,2026-03-05
CRE-1006,104000.00,250000,0,0,2600,2026-03-05
CRE-1007,170500.00,0,0,0,4100,2026-03-05
CRE-1008,57000.00,0,0,0,1150,2026-03-05
CRE-1009,71000.00,0,0,0,1800,2026-03-05
CRE-1010,46000.00,0,0,0,1100,2026-03-05
CRE-1011,0,0,0,0,1350,2026-03-05
CRE-1012,90000.00,0,1500000,0,2150,2026-03-05
"""

WATERFALL = {
    "order": ["income", "principal"],
    "waterfalls": {
        "income": [
            {"type": "fee", "fee": "TRUSTEE", "clause_ref": "Trust Deed cl. 9.1(a)"},
            {"type": "fee", "fee": "MANAGER", "clause_ref": "Trust Deed cl. 9.1(b)"},
            {"type": "fee", "fee": "COMMITMENT", "clause_ref": "Fee Letter cl. 2"},
            {"type": "interest", "classes": ["A"], "clause_ref": "Trust Deed cl. 9.1(c)"},
            {"type": "interest", "classes": ["B"], "clause_ref": "Trust Deed cl. 9.1(d)"},
            {"type": "reserve", "account": "RESERVE", "target": "class_balance['A'] * reserve_pct", "clause_ref": "Trust Deed cl. 9.1(e)"},
            {"type": "transfer", "to": "principal", "when": "events['AMORTISATION_EVENT']", "label": "Excess income to principal during amortisation"},
            {"type": "interest", "classes": ["Z"]},
            {"type": "residual", "payee": "Residual Income Unitholder", "clause_ref": "Trust Deed cl. 9.1(h)"},
        ],
        "principal": [
            {"type": "principal", "classes": ["A", "B"], "mode": "sequential", "limit": "borrowing_base_deficiency",
             "when": "is_revolving and not events['AMORTISATION_EVENT']", "label": "Cure borrowing base deficiency"},
            {"type": "principal", "classes": ["A", "B", "Z"], "mode": "sequential",
             "when": "not is_revolving or events['AMORTISATION_EVENT']", "label": "Amortisation"},
            {"type": "residual", "payee": "Originator (principal available for new loans)"},
        ],
    },
}


def run(actor: Actor | None = None, channel: Channel = Channel.SYSTEM, code: str = "CRE-WH-1") -> dict[str, Any]:
    actor = actor or system_actor()
    log: list[dict[str, Any]] = []

    def op(name: str, /, **params: Any) -> Any:
        res = execute(name, params, actor, channel)
        log.append({"op": name, "ok": res.ok})
        if not res.ok:
            raise RuntimeError(f"{name} failed: {res.error}")
        return res.data

    fac = op("facility.create", code=code, name="Southern Cross CRE Warehouse Trust", facility_type="warehouse", currency="AUD",
             trust_name="Southern Cross CRE Warehouse Trust No.1", trustee="Perpetual Trust Services Ltd (as trustee)",
             security_trustee="P.T. Ltd", trust_manager="Securifi Trust Management Pty Ltd", originator="Southern Cross Capital",
             servicer="Southern Cross Servicing", closing_date="2026-01-15", revolving_period_end="2027-01-15",
             final_maturity_date="2029-01-15", facility_limit="200000000", day_count="ACT/365F",
             holidays=["2026-01-26", "2026-04-03", "2026-04-06", "2026-04-25", "2026-06-08", "2026-12-25", "2026-12-28"],
             settings={"payment_day": 15, "frequency_months": 1, "payment_lag_months": 1, "determination_offset_business_days": 5},
             variables={"reserve_pct": "0.005"})
    fid = fac["id"]
    fa = op("document.add", facility_id=fid, title="Senior Facility Agreement", doc_type="facility_agreement", version="Executed", executed_date="2026-01-15")
    op("document.add", facility_id=fid, title="Trust Deed and Master Definitions", doc_type="trust_deed", executed_date="2026-01-15")

    op("note_class.create", facility_id=fid, name="A", rank=1, commitment="150000000", base_rate_index="BBSW1M", margin_bps="225",
       base_rate_floor="0", advance_rate="0.70")
    op("note_class.create", facility_id=fid, name="B", rank=2, commitment="25000000", base_rate_index="BBSW1M", margin_bps="550",
       base_rate_floor="0", advance_rate="0.85")
    op("note_class.create", facility_id=fid, name="Z", rank=3, commitment="40000000", rate_type="fixed", fixed_rate="0.10", is_residual=False)

    for short, name in (("SENIOR-BANK", "Senior Bank Ltd"), ("MEZZ-FUND", "Mezzanine Credit Fund"), ("ORIGINATOR", "Southern Cross Capital")):
        op("noteholder.create", name=name, short_name=short, entity_type="company", contact_email=f"ops@{short.lower()}.example")
    op("noteholder.create", name="Second Senior Bank", short_name="SENIOR-BANK-2", entity_type="company")
    op("register.commit", facility_id=fid, note_class="A", noteholder="SENIOR-BANK", amount="100000000", effective_date="2026-01-15")
    op("register.commit", facility_id=fid, note_class="A", noteholder="SENIOR-BANK-2", amount="50000000", effective_date="2026-01-15")
    op("register.commit", facility_id=fid, note_class="B", noteholder="MEZZ-FUND", amount="25000000", effective_date="2026-01-15")
    op("register.commit", facility_id=fid, note_class="Z", noteholder="ORIGINATOR", amount="40000000", effective_date="2026-01-15")

    op("fee.create", facility_id=fid, code="TRUSTEE", name="Trustee fee", payee="Perpetual Trust Services Ltd", calculation="percent_per_annum",
       basis="pool_balance", rate_bps="3", tax_rate="0.1", minimum_amount="2500")
    op("fee.create", facility_id=fid, code="MANAGER", name="Trust manager fee", payee="Securifi Trust Management Pty Ltd",
       calculation="percent_per_annum", basis="pool_balance", rate_bps="5", tax_rate="0.1")
    op("fee.create", facility_id=fid, code="COMMITMENT", name="Class A commitment fee", payee="Class A noteholders",
       calculation="percent_per_annum", basis="undrawn_commitment", basis_class="A", rate_bps="75")

    op("account.create", facility_id=fid, code="COLLECTION", name="Collection Account", account_type="collection", bank_name="Big Four Bank",
       branch_code="062-000", account_number="12345678", opening_date="2026-01-15")
    op("account.create", facility_id=fid, code="RESERVE", name="Liquidity Reserve Account", account_type="reserve", bank_name="Big Four Bank",
       branch_code="062-000", account_number="87654321", opening_date="2026-01-15")

    rules = [
        ("EC-01", "Maximum LTV 70% (investment)", "ltv <= 0.70", "loan_stage == 'investment'", "cl. 12.1(a)"),
        ("EC-02", "Maximum LTC 65% (construction)", "ltv <= 0.65", "loan_stage in ['construction', 'development']", "cl. 12.1(b)"),
        ("EC-03", "First ranking security", "lien_position == 'first'", None, "cl. 12.1(c)"),
        ("EC-04", "Not in arrears more than 30 days", "arrears_days <= 30", None, "cl. 12.1(d)"),
        ("EC-05", "Not defaulted", "not is_defaulted", None, "cl. 12.1(e)"),
        ("EC-06", "Minimum ICR 1.75x (investment)", "icr >= 1.75", "loan_stage == 'investment'", "cl. 12.1(f)"),
        ("EC-07", "Remaining term at least 6 months", "remaining_term_months >= 6", None, "cl. 12.1(g)"),
        ("EC-08", "Australian security property", "property_state in ['NSW', 'VIC', 'QLD', 'WA', 'SA', 'ACT', 'TAS', 'NT']", None, "cl. 12.1(h)"),
    ]
    for code_, name, expr, when, clause in rules:
        op("criterion.create", facility_id=fid, code=code_, name=name, expression=expr, applies_when=when, document_id=fa["id"],
           clause_ref=f"Facility Agreement {clause}")
    tests = [
        ("PP-01", "Property type concentration", "concentration", {"group_by": "property_type", "max": 0.35, "limits": {"hotel": 0.10, "land": 0.05}}, "limit", None),
        ("PP-02", "Single sponsor concentration", "concentration", {"group_by": "borrower_group", "max": 0.25}, "limit", None),
        ("PP-03", "Construction and land exposure", "filtered_concentration", {"filter": "loan_stage in ['construction', 'land']", "max": 0.20}, "limit", None),
        ("PP-04", "State concentration (ex NSW/VIC)", "filtered_concentration", {"filter": "property_state not in ['NSW', 'VIC']", "max": 0.40}, "limit", None),
        ("PP-05", "Weighted average LTV", "weighted_average", {"field": "ltv", "max": 0.65}, "covenant", "REVIEW_EVENT"),
        ("PP-06", "Weighted average ICR", "weighted_average", {"field": "icr", "min": 1.9, "filter": "loan_stage == 'investment'"}, "covenant", "REVIEW_EVENT"),
        ("PP-07", "Minimum number of sponsors", "count", {"distinct": "borrower_group", "min": 6}, "covenant", None),
        ("PP-08", "Largest loan", "largest_exposure", {"max_pct": 0.15}, "limit", None),
    ]
    for code_, name, ttype, config, treatment, event in tests:
        op("test.create", facility_id=fid, code=code_, name=name, test_type=ttype, config=config, treatment=treatment, event_code=event,
           document_id=fa["id"], clause_ref="Facility Agreement Schedule 3")

    op("waterfall.set", facility_id=fid, definition=WATERFALL, notes="As per Trust Deed cl. 9 (pre-enforcement)")
    periods = op("period.generate", facility_id=fid, first_collection_start="2026-02-01", count=12, first_interest_start="2026-02-16")

    mapping = op("mapping.create", name="Southern Cross Servicing loan tape v3", dataset="loan_tape", config=LOAN_MAPPING)
    tape = op("data.ingest_loan_tape", facility_id=fid, content=LOAN_TAPE, mapping_id=mapping["id"], as_of_date="2026-02-16",
              source_name="SCS_LoanTape_20260216.csv")
    cr = op("compliance.run", facility_id=fid, purpose="drawdown")

    op("rates.set_fixing", index_name="BBSW1M", fixing_date="2026-02-16", rate="0.0412", source="ASX BBSW")
    op("rates.set_fixing", index_name="BBSW1M", fixing_date="2026-03-16", rate="0.0405", source="ASX BBSW")
    bb = {r["class"]: r for r in cr["borrowing_base"]["classes"]}
    for cls in ("A", "B"):
        amount = Decimal(bb[cls]["available_to_draw"]) // 1000000 * 1000000
        op("register.drawdown", facility_id=fid, note_class=cls, amount=str(amount), effective_date="2026-02-16", reference="Initial funding")
    op("register.drawdown", facility_id=fid, note_class="Z", amount="25000000", effective_date="2026-02-16", reference="Initial funding")

    op("data.ingest_collections", facility_id=fid, period=1, content=COLLECTIONS, source_name="SCS_Collections_202602.csv")
    month_end = op("compliance.run", facility_id=fid, purpose="period_end", as_of_date="2026-02-28")
    return {"facility_id": fid, "code": code, "periods": len(periods), "loan_tape_batch": tape["id"],
            "compliance_run_id": month_end["id"], "operations": len(log)}
