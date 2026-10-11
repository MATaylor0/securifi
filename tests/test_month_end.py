"""End-to-end: set up a CRE warehouse, ingest data, test compliance and run month end."""

from decimal import Decimal

from securifi import db, demo
from securifi.ops import audit
from securifi.ops.auth import authenticate
from securifi.ops.context import Channel
from securifi.ops.executor import execute


def actor(raw):
    with db.session_scope() as s:
        return authenticate(s, raw)


def call(a, name, /, channel=Channel.API, **params):
    res = execute(name, params, a, channel)
    assert res.ok, res.error
    return res.data


def test_full_month_end_cycle(users):
    admin = actor(users["admin"])
    analyst = actor(users["analyst"])
    manager = actor(users["trust_manager"])
    info = demo.run(admin, Channel.CLI)
    fid = info["facility_id"]

    cr = call(analyst, "compliance.get", run_id=info["compliance_run_id"])
    summary = cr["summary"]
    # CRE-1004 hotel LTV 70.6% > 70%, CRE-1008 second ranking, CRE-1011 45 days in arrears
    assert summary["eligible_loans"] == 9
    assert Decimal(summary["excess_concentration"]) > 0  # office / sponsor / largest loan limits
    results = call(analyst, "compliance.loan_results", run_id=cr["id"], only_ineligible=True)
    assert {r["loan_id"] for r in results} == {"CRE-1004", "CRE-1008", "CRE-1011"}

    positions = call(analyst, "register.positions", facility_id=fid)
    a = next(c for c in positions["classes"] if c["note_class"] == "A")
    assert len(a["holders"]) == 2
    assert Decimal(a["holders"][0]["outstanding"]) + Decimal(a["holders"][1]["outstanding"]) == Decimal(a["outstanding"])

    run = call(analyst, "period_run.calculate", facility_id=fid, period=1, adjustments={"income": "1250.00"})
    out = run["outputs"]
    interest_a = next(r for r in out["interest"] if r["class"] == "A")
    assert interest_a["days"] == 28 and Decimal(interest_a["all_in_rate"]) == Decimal("0.0637")
    wf = out["waterfall"]
    assert wf["undistributed"] == {}
    assert Decimal(wf["interest_paid"]["A"]) == Decimal(interest_a["total_due"])
    assert Decimal(wf["reserve_movements"]["RESERVE"]) > 0

    # four-eyes: the preparer cannot approve; viewers/analysts lack permission
    denied = execute("period_run.approve", {"run_id": run["id"]}, analyst, Channel.MCP)
    assert not denied.ok and denied.error["code"] == "permission_denied"
    approved = call(manager, "period_run.approve", run_id=run["id"])
    assert approved["status"] == "approved"
    final = call(manager, "period_run.finalise", run_id=run["id"])
    assert final["status"] == "finalised"
    assert final["expected_cashflows"]

    report = call(analyst, "report.investor", facility_id=fid, period=1)
    assert report["is_final"] and report["content"]["status"] == "FINAL"
    assert report["content"]["noteholder_distributions"]

    # period 2 picks up from finalised balances
    call(analyst, "data.ingest_collections", facility_id=fid, period=2, rows=[{"loan_id": "CRE-1001", "interest_collected": "120000"}])
    run2 = call(analyst, "period_run.calculate", facility_id=fid, period=2)
    assert run2["inputs"]["carried_forward_from_run"] == run["id"]

    with db.session_scope() as s:
        assert audit.verify_chain(s)["valid"]
    log = call(admin, "audit.query", operation="period_run.approve")
    assert {e["outcome"] for e in log} == {"success", "denied"}
    assert {e["channel"] for e in log} == {"api", "mcp"}


def test_drawdown_blocked_by_borrowing_base(users):
    admin = actor(users["admin"])
    info = demo.run(admin)
    res = execute("register.drawdown", {"facility_id": info["facility_id"], "note_class": "A", "amount": "50000000",
                                        "effective_date": "2026-03-01"}, admin, Channel.API)
    assert not res.ok and res.error["code"] == "business_rule_violation"
    override = execute("register.drawdown", {"facility_id": info["facility_id"], "note_class": "A", "amount": "1000000",
                                             "effective_date": "2026-03-01", "override_reason": "credit approved"},
                       actor(users["analyst"]), Channel.API)
    assert not override.ok and override.error["code"] == "permission_denied"


def test_dry_run_rolls_back_but_is_audited(users):
    admin = actor(users["admin"])
    res = execute("facility.create", {"code": "DRY", "name": "Dry run"}, admin, Channel.MCP, dry_run=True)
    assert res.ok and res.dry_run
    assert call(admin, "facility.list") == []
    events = call(admin, "audit.query", operation="facility.create")
    assert events[0]["outcome"] == "dry_run"


def test_validation_and_idempotency(users):
    admin = actor(users["admin"])
    bad = execute("facility.create", {"code": "X1", "name": "x", "unexpected": 1}, admin, Channel.API)
    assert not bad.ok and bad.error["code"] == "validation_failed"
    first = execute("facility.create", {"code": "X1", "name": "x"}, admin, Channel.API, idempotency_key="k1")
    again = execute("facility.create", {"code": "X1", "name": "x"}, admin, Channel.API, idempotency_key="k1")
    assert first.ok and again.ok and first.data["id"] == again.data["id"]


def test_facility_scope_enforced(users):
    admin = actor(users["admin"])
    f1 = call(admin, "facility.create", code="F1", name="One")
    call(admin, "facility.create", code="F2", name="Two")
    user = call(admin, "admin.user_create", email="scoped@example.com", name="Scoped", role="viewer", facility_scope=[f1["id"]])
    key = call(admin, "admin.apikey_create", user_id=user["id"], name="k")
    scoped = actor(key["api_key"])
    assert [f["code"] for f in call(scoped, "facility.list")] == ["F1"]
    res = execute("facility.get", {"facility_id": "F2"}, scoped, Channel.UI)
    assert not res.ok and res.error["code"] == "permission_denied"
