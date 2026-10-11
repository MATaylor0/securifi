"""Self-description, so AI agents and integrators can discover what the platform can do."""

from __future__ import annotations

from ...engine.compliance import TEST_TYPES
from ...engine.expressions import FUNCTIONS
from ...engine.fields import catalogue_as_dict
from ...engine.waterfall import STEP_TYPES
from ..context import OpContext
from ..registry import NoInput, OpInput, all_operations, get_operation, operation
from ..errors import NotFound


@operation("catalog.operations", summary="List every operation with its permission and input schema", permission="facility.read", mutating=False, tags=("catalog",))
def operations(ctx: OpContext, _: NoInput) -> list:
    return [
        {"name": op.name, "summary": op.summary, "permission": op.permission, "mutating": op.mutating,
         "tags": list(op.tags), "allowed": ctx.actor.can(op.permission)}
        for op in all_operations()
    ]


class DescribeInput(OpInput):
    name: str


@operation("catalog.describe", summary="Full description and JSON schema for one operation", permission="facility.read", input=DescribeInput, mutating=False, tags=("catalog",))
def describe(ctx: OpContext, p: DescribeInput) -> dict:
    op = get_operation(p.name)
    if op is None:
        raise NotFound(f"unknown operation '{p.name}'")
    return op.describe()


@operation("catalog.fields", summary="Canonical data fields for loan tapes, collections and bank statements", permission="facility.read", mutating=False, tags=("catalog",))
def fields(ctx: OpContext, _: NoInput) -> dict:
    return catalogue_as_dict()


@operation("catalog.rule_language", summary="Reference for rule expressions, portfolio test types and waterfall steps", permission="facility.read", mutating=False, tags=("catalog",))
def rule_language(ctx: OpContext, _: NoInput) -> dict:
    return {
        "expressions": {
            "syntax": "Python-style boolean/arithmetic expressions over canonical or custom field names. Numbers are exact decimals.",
            "operators": ["and", "or", "not", "==", "!=", "<", "<=", ">", ">=", "in", "not in", "+", "-", "*", "/", "x if c else y"],
            "functions": sorted(FUNCTIONS),
            "missing_data": "A criterion that reads a missing/blank field fails with reason 'data missing: <field>'.",
            "examples": [
                "ltv <= 0.65",
                "property_type in ['office', 'industrial', 'logistics']",
                "lien_position == 'first' and not is_defaulted",
                "loan_stage != 'construction' or presales_coverage >= 1.0",
                "remaining_term_months >= 6 and arrears_days < 30",
            ],
        },
        "portfolio_test_types": TEST_TYPES,
        "portfolio_test_treatments": {
            "limit": "Breach amount is excluded from the borrowing base as excess concentration",
            "covenant": "Breach is flagged (and may raise an event) but does not change the borrowing base",
            "information": "Reported only",
        },
        "waterfall_step_types": list(STEP_TYPES),
        "waterfall_variables": ["available", "fees_due", "interest_due", "class_balance", "reserve_balance", "events",
                                "tests", "pool_balance", "eligible_balance", "adjusted_eligible_balance",
                                "borrowing_base_deficiency", "is_revolving", "period_sequence", "<facility variables>"],
    }
