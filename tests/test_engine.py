from datetime import date
from decimal import Decimal

import pytest

from securifi.engine.compliance import borrowing_base, run_compliance, stratify
from securifi.engine.daycount import Calendar, year_fraction
from securifi.engine.expressions import ExpressionError, MissingField, evaluate, referenced_names
from securifi.engine.interest import Movement, class_interest, fee_amount, undrawn_fee
from securifi.engine.mapping import apply_mapping, enrich_loan, parse_csv, typed_view
from securifi.engine.money import allocate_pro_rata
from securifi.engine.reconciliation import Actual, Expected, auto_match
from securifi.engine.schedule import generate_schedule
from securifi.engine.waterfall import WaterfallError, run_waterfall


def test_allocate_pro_rata_sums_exactly():
    parts = allocate_pro_rata(Decimal("100.00"), [Decimal(1), Decimal(1), Decimal(1)])
    assert sum(parts) == Decimal("100.00")
    assert sorted(parts) == [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")]


def test_year_fraction_conventions():
    assert year_fraction(date(2026, 1, 1), date(2026, 2, 1), "ACT/365F") == Decimal(31) / Decimal(365)
    assert year_fraction(date(2026, 1, 1), date(2026, 2, 1), "ACT/360") == Decimal(31) / Decimal(360)
    assert year_fraction(date(2026, 1, 31), date(2026, 2, 28), "30/360") == Decimal(28) / Decimal(360)


def test_schedule_business_day_adjustment():
    cal = Calendar(["2026-02-16"])
    periods = generate_schedule(first_collection_start=date(2026, 1, 1), count=3, payment_day=15, calendar=cal,
                                first_interest_start=date(2025, 12, 20))
    # 15 Feb 2026 is a Sunday, 16 Feb a holiday -> 17 Feb
    assert periods[0].collection_end == date(2026, 1, 31)
    assert periods[0].payment_date == date(2026, 2, 17)
    assert periods[0].interest_start == date(2025, 12, 20)
    assert periods[1].interest_start == periods[0].payment_date
    assert periods[0].determination_date < periods[0].payment_date


def test_expressions_are_decimal_and_safe():
    loan = {"ltv": Decimal("0.65"), "property_type": "office", "is_defaulted": False}
    assert evaluate("ltv <= 0.65 and property_type in ['office', 'industrial']", loan) is True
    assert evaluate("not is_defaulted", loan) is True
    assert referenced_names("ltv <= 0.65 and max(icr, 1) > 1") == {"ltv", "icr"}
    with pytest.raises(MissingField):
        evaluate("icr >= 1.5", loan)
    with pytest.raises(ExpressionError):
        evaluate("__import__('os')", loan)
    with pytest.raises(ExpressionError):
        evaluate("ltv.real", loan)
    assert evaluate("events['EOD']", {"events": {}}) is False


def test_mapping_and_enrichment():
    csv_text = "Loan Number,Closing Balance,LVR %,Expiry,Security Type,Sponsor Code\nL1,\"1,000,000\",60,31/12/2027,Office,S1\nL2,,50,31/12/2027,Retail,S2\n"
    mapping = {"fields": {
        "loan_id": {"source": "Loan Number"},
        "current_balance": {"source": "Closing Balance"},
        "ltv": {"source": "LVR %", "transform": "percent"},
        "maturity_date": {"source": "Expiry", "date_format": "%d/%m/%Y"},
        "property_type": {"source": "Security Type"},
    }}
    records, issues = apply_mapping(parse_csv(csv_text), "loan_tape", mapping)
    assert len(records) == 1
    assert issues[0].row == 2 and issues[0].field == "current_balance"
    rec = enrich_loan(records[0], date(2026, 1, 1))
    assert rec["ltv"] == Decimal("0.6")
    assert rec["property_type"] == "office"
    assert rec["sponsor_code"] == "S1"  # unmapped columns are kept
    assert rec["remaining_term_months"] > 0
    assert typed_view(rec)["maturity_date"] == date(2027, 12, 31)


LOANS = [
    {"loan_id": "L1", "current_balance": Decimal("40"), "property_type": "office", "ltv": Decimal("0.60"), "borrower_group": "S1"},
    {"loan_id": "L2", "current_balance": Decimal("30"), "property_type": "office", "ltv": Decimal("0.70"), "borrower_group": "S2"},
    {"loan_id": "L3", "current_balance": Decimal("20"), "property_type": "hotel", "ltv": Decimal("0.50"), "borrower_group": "S3"},
    {"loan_id": "L4", "current_balance": Decimal("10"), "property_type": "retail", "borrower_group": "S4"},
]


def test_compliance_eligibility_concentration_and_borrowing_base():
    criteria = [
        {"code": "EC1", "name": "Max LTV", "expression": "ltv <= 0.75"},
        {"code": "EC2", "name": "No hotels", "expression": "property_type != 'hotel'", "severity": "warning"},
    ]
    tests = [
        {"code": "PP1", "test_type": "concentration", "config": {"group_by": "property_type", "max": 0.5}},
        {"code": "PP2", "test_type": "weighted_average", "config": {"field": "ltv", "max": 0.6}, "treatment": "covenant", "event_code": "WA_LTV_BREACH"},
    ]
    classes = [
        {"name": "A", "rank": 1, "commitment": Decimal("100"), "advance_rate": Decimal("0.6"), "outstanding": Decimal("55")},
        {"name": "B", "rank": 2, "commitment": Decimal("20"), "advance_rate": Decimal("0.8"), "outstanding": Decimal("10")},
    ]
    out = run_compliance(LOANS, criteria, tests, classes)
    s = out["summary"]
    assert s["eligible_loans"] == 3  # L4 missing ltv
    assert s["eligible_balance"] == Decimal("90")
    # office 70/90 = 0.7777 > 0.5 -> excess 70 - 45 = 25
    pp1 = out["test_results"][0]
    assert not pp1["passed"] and pp1["excess_amount"] == Decimal("25.00")
    assert s["adjusted_eligible_balance"] == Decimal("65.00")
    # covenant breaches never reduce borrowing base
    assert out["test_results"][1]["excess_amount"] == 0
    assert s["breached_events"] == ["WA_LTV_BREACH"]
    bb = out["borrowing_base"]["classes"]
    assert bb[0]["max_funding"] == Decimal("39.00") and bb[0]["deficiency"] == Decimal("16.00")
    assert bb[1]["max_funding"] == Decimal("13.00")
    hotel = next(l for l in out["loan_results"] if l["loan_id"] == "L3")
    assert hotel["eligible"] and hotel["warnings"][0]["code"] == "EC2"


def test_borrowing_base_residual_class_unlimited():
    bb = borrowing_base(Decimal("100"), [{"name": "Z", "rank": 9, "commitment": Decimal("15"), "outstanding": Decimal("15")}])
    assert bb["classes"][0]["max_funding"] == Decimal("15") and bb["total_deficiency"] == 0


def test_stratify_buckets():
    rows = stratify(LOANS, "ltv", [0.55, 0.65, 0.75])
    labels = {r["bucket"]: r["count"] for r in rows}
    assert labels["(blank)"] == 1
    assert sum(labels.values()) == 4


def test_class_interest_with_midperiod_drawdown():
    moves = {
        "H1": [Movement(date(2026, 1, 1), Decimal("1000000")), Movement(date(2026, 1, 16), Decimal("500000"))],
        "H2": [Movement(date(2026, 1, 1), Decimal("1000000"))],
    }
    nc = {"name": "A", "margin_bps": 250, "day_count": "ACT/365F", "base_rate_floor": "0"}
    res = class_interest(nc, moves, date(2026, 1, 1), date(2026, 1, 31), Decimal("0.04"))
    rate = Decimal("0.065")
    expected = (Decimal(2000000) * rate * 15 / 365 + Decimal(2500000) * rate * 15 / 365).quantize(Decimal("0.01"))
    assert res["interest"] == expected
    assert sum(res["by_holder"].values()) == res["interest"]
    assert res["average_balance"] == Decimal("2250000.00")


def test_base_rate_floor():
    res = class_interest({"name": "A", "margin_bps": 200, "base_rate_floor": "0.01"},
                         {"H": [Movement(date(2026, 1, 1), Decimal(100))]}, date(2026, 1, 1), date(2027, 1, 1), Decimal("0.005"))
    assert res["floor_applied"] and res["all_in_rate"] == Decimal("0.03")


def test_fees():
    f = fee_amount({"code": "T", "calculation": "percent_per_annum", "rate_bps": 5, "tax_rate": "0.1", "minimum_amount": 1000},
                   date(2026, 1, 1), date(2026, 2, 1), basis_amount=Decimal("10000000"))
    assert f["net"] == Decimal("1000.00")  # minimum applies (calc = 424.66)
    assert f["amount"] == Decimal("1100.00")
    fee = undrawn_fee(Decimal("100"), [Movement(date(2026, 1, 1), Decimal("40"))], date(2026, 1, 1), date(2027, 1, 1), Decimal(10000), "ACT/365F")
    assert fee == Decimal("60.00")


WF = {
    "order": ["income", "principal"],
    "waterfalls": {
        "income": [
            {"type": "fee", "fee": "TRUSTEE"},
            {"type": "interest", "classes": ["A"]},
            {"type": "interest", "classes": ["B"]},
            {"type": "reserve", "account": "RES", "target": "class_balance['A'] * 0.01"},
            {"type": "transfer", "to": "principal", "when": "events['AMORTISATION']"},
            {"type": "residual", "payee": "Residual"},
        ],
        "principal": [
            {"type": "principal", "classes": ["A", "B"], "mode": "sequential"},
            {"type": "residual", "payee": "Seller"},
        ],
    },
}


def test_waterfall_shortfall_and_sequential_principal():
    out = run_waterfall(
        WF,
        available={"income": Decimal("150"), "principal": Decimal("1050")},
        fees_due={"TRUSTEE": Decimal("20")},
        interest_due={"A": Decimal("100"), "B": Decimal("50")},
        class_balance={"A": Decimal("1000"), "B": Decimal("500")},
        variables={"events": {}},
    )
    assert out["interest_paid"] == {"A": Decimal("100.00"), "B": Decimal("30.00")}
    assert out["unpaid_interest"] == {"B": Decimal("20.00")}
    assert out["principal_paid"] == {"A": Decimal("1000.00"), "B": Decimal("50.00")}
    assert out["undistributed"] == {}


def test_waterfall_condition_transfer():
    out = run_waterfall(
        WF,
        available={"income": Decimal("200"), "principal": Decimal("0")},
        fees_due={"TRUSTEE": Decimal("0")}, interest_due={"A": Decimal("10"), "B": Decimal("10")},
        class_balance={"A": Decimal("1000"), "B": Decimal("500")},
        reserve_balance={"RES": Decimal("5")},
        variables={"events": {"AMORTISATION": True}},
    )
    assert out["reserve_movements"] == {"RES": Decimal("5.00")}
    assert out["principal_paid"]["A"] == Decimal("175.00")


def test_waterfall_validation():
    with pytest.raises(WaterfallError):
        run_waterfall({"waterfalls": {"income": [{"type": "transfer", "to": "nowhere"}]}},
                      available={}, fees_due={}, interest_due={}, class_balance={})


def test_reconciliation_auto_match():
    exp = [Expected("e1", date(2026, 2, 15), Decimal("100"), "INV-1"), Expected("e2", date(2026, 2, 15), Decimal("50"))]
    act = [Actual("a1", date(2026, 2, 16), Decimal("50")), Actual("a2", date(2026, 2, 20), Decimal("100"), "ref INV-1")]
    assert sorted(auto_match(exp, act)) == [("e1", "a2", "amount+reference"), ("e2", "a1", "amount+date")]
