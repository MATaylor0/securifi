"""Role-based access control shared by every entry point."""

from __future__ import annotations

PERMISSIONS: dict[str, str] = {
    "facility.read": "View facilities, registers, data, results and reports",
    "facility.configure": "Create and change facilities, note classes, fees, accounts, periods and waterfalls",
    "register.write": "Record commitments, drawdowns, repayments and transfers on the note register",
    "data.ingest": "Load loan tapes, collections and bank statements; manage data mappings",
    "rules.configure": "Create and change eligibility criteria and portfolio parameters",
    "compliance.run": "Run eligibility, portfolio and borrowing base tests",
    "period.prepare": "Calculate period runs (interest, fees, waterfall)",
    "period.approve": "Approve, reject and finalise period runs; override borrowing base checks",
    "events.manage": "Declare and clear trigger events",
    "reconciliation.perform": "Match bank transactions to expected cash flows",
    "reports.generate": "Generate investor and facility reports",
    "market_data.write": "Record base rate fixings",
    "audit.read": "Read and verify the audit log",
    "admin.users": "Manage users and API keys",
}

_ALL = frozenset(PERMISSIONS)

ROLES: dict[str, frozenset[str]] = {
    "admin": _ALL,
    "trust_manager": _ALL - {"admin.users"},
    "analyst": frozenset({
        "facility.read", "data.ingest", "rules.configure", "compliance.run", "period.prepare",
        "reconciliation.perform", "reports.generate", "market_data.write",
    }),
    # Default role for AI agents: can do the preparation work but never approve, finalise or configure.
    "agent": frozenset({
        "facility.read", "data.ingest", "compliance.run", "period.prepare", "reconciliation.perform", "reports.generate",
    }),
    "auditor": frozenset({"facility.read", "audit.read"}),
    "viewer": frozenset({"facility.read"}),
}

ROLE_DESCRIPTIONS = {
    "admin": "Full access including user and key management",
    "trust_manager": "Operates facilities end to end, including approvals (four-eyes enforced)",
    "analyst": "Prepares data, rules, tests and period runs; cannot approve or finalise",
    "agent": "AI agent / automation: preparation work only, no configuration or approvals",
    "auditor": "Read-only access plus the audit log",
    "viewer": "Read-only access",
}


def permissions_for(role: str, scopes: list[str] | None = None) -> frozenset[str]:
    granted = ROLES.get(role, frozenset())
    if scopes is not None:
        granted = granted & frozenset(scopes)
    return granted
