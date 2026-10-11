"""Canonical data model for data ingested from servicing systems.

Securifi is not a servicing system. Servicers deliver loan tapes (pool cuts), collections
reports and bank statements in their own layouts; a data mapping translates each layout
into these canonical fields so eligibility criteria and portfolio tests can be written once
against stable names. Any column not in the catalogue is kept as a custom attribute and can
still be used in rules.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class FieldType(str, Enum):
    STRING = "string"
    DECIMAL = "decimal"  # amounts and plain numbers
    RATIO = "ratio"  # 0.65 means 65%
    INTEGER = "integer"
    DATE = "date"
    BOOLEAN = "boolean"
    ENUM = "enum"


class Dataset(str, Enum):
    LOAN_TAPE = "loan_tape"
    COLLECTIONS = "collections"
    BANK_STATEMENT = "bank_statement"


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: FieldType
    description: str
    required: bool = False
    choices: tuple[str, ...] = field(default_factory=tuple)
    derived: bool = False

    def to_dict(self) -> dict:
        data = asdict(self)
        data["type"] = self.type.value
        data["choices"] = list(self.choices)
        return data


PROPERTY_TYPES = (
    "office", "retail", "industrial", "logistics", "residential", "multifamily", "hotel",
    "mixed_use", "land", "healthcare", "student_accommodation", "self_storage", "data_centre",
    "childcare", "agriculture", "other",
)
LOAN_STAGES = ("investment", "construction", "development", "land", "bridge")
LOAN_PURPOSES = ("acquisition", "refinance", "construction", "land_bank", "bridge", "capex", "equity_release", "other")
LIEN_POSITIONS = ("first", "second", "mezzanine", "unsecured")

_F = FieldType

LOAN_TAPE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("loan_id", _F.STRING, "Unique loan identifier from the servicing system", required=True),
    FieldSpec("facility_loan_ref", _F.STRING, "Reference used under the facility, if different"),
    FieldSpec("borrower_name", _F.STRING, "Borrowing entity"),
    FieldSpec("borrower_group", _F.STRING, "Sponsor / obligor group used for single-obligor limits"),
    FieldSpec("guarantor", _F.STRING, "Principal guarantor"),
    FieldSpec("property_name", _F.STRING, "Primary security property"),
    FieldSpec("property_type", _F.ENUM, "Primary asset class of the security", choices=PROPERTY_TYPES),
    FieldSpec("property_state", _F.STRING, "State / region of the security property"),
    FieldSpec("property_country", _F.STRING, "Country of the security property"),
    FieldSpec("property_postcode", _F.STRING, "Postcode of the security property"),
    FieldSpec("number_of_properties", _F.INTEGER, "Number of properties securing the loan"),
    FieldSpec("loan_stage", _F.ENUM, "Investment, construction, development, land or bridge", choices=LOAN_STAGES),
    FieldSpec("loan_purpose", _F.ENUM, "Purpose of the loan", choices=LOAN_PURPOSES),
    FieldSpec("lien_position", _F.ENUM, "Ranking of security", choices=LIEN_POSITIONS),
    FieldSpec("currency", _F.STRING, "Loan currency (ISO 4217)"),
    FieldSpec("origination_date", _F.DATE, "Settlement / first drawdown date"),
    FieldSpec("maturity_date", _F.DATE, "Current scheduled maturity date"),
    FieldSpec("extension_options", _F.INTEGER, "Remaining borrower extension options"),
    FieldSpec("current_balance", _F.DECIMAL, "Outstanding principal balance at the cut-off date", required=True),
    FieldSpec("commitment_amount", _F.DECIMAL, "Total facility limit to the borrower"),
    FieldSpec("undrawn_amount", _F.DECIMAL, "Undrawn commitment (derived if absent)"),
    FieldSpec("capitalised_interest", _F.DECIMAL, "Capitalised interest included in balance"),
    FieldSpec("interest_rate_type", _F.ENUM, "Floating or fixed", choices=("floating", "fixed")),
    FieldSpec("base_rate_index", _F.STRING, "Reference rate index e.g. BBSW1M, SOFR"),
    FieldSpec("margin_bps", _F.DECIMAL, "Margin over base rate in basis points"),
    FieldSpec("interest_rate", _F.RATIO, "Current all-in interest rate"),
    FieldSpec("interest_reserve_balance", _F.DECIMAL, "Interest reserve / prepaid interest held"),
    FieldSpec("valuation_amount", _F.DECIMAL, "Most recent independent valuation"),
    FieldSpec("valuation_date", _F.DATE, "Date of the most recent valuation"),
    FieldSpec("valuation_basis", _F.ENUM, "As-is or as-if-complete", choices=("as_is", "as_if_complete")),
    FieldSpec("total_project_cost", _F.DECIMAL, "Total development cost for construction loans"),
    FieldSpec("ltv", _F.RATIO, "Loan to value (derived from balance / valuation if absent)"),
    FieldSpec("ltc", _F.RATIO, "Loan to cost (derived for construction loans if absent)"),
    FieldSpec("icr", _F.DECIMAL, "Interest cover ratio (times)"),
    FieldSpec("dscr", _F.DECIMAL, "Debt service cover ratio (times)"),
    FieldSpec("debt_yield", _F.RATIO, "Net operating income / loan balance"),
    FieldSpec("net_operating_income", _F.DECIMAL, "Annual net operating income"),
    FieldSpec("occupancy", _F.RATIO, "Physical or economic occupancy"),
    FieldSpec("wale_years", _F.DECIMAL, "Weighted average lease expiry in years"),
    FieldSpec("presales_coverage", _F.RATIO, "Qualifying pre-sales / debt (development loans)"),
    FieldSpec("construction_completion", _F.RATIO, "Percentage complete (construction loans)"),
    FieldSpec("arrears_days", _F.INTEGER, "Days past due"),
    FieldSpec("arrears_amount", _F.DECIMAL, "Amount past due"),
    FieldSpec("is_defaulted", _F.BOOLEAN, "Loan is in default as defined by the servicer"),
    FieldSpec("is_impaired", _F.BOOLEAN, "Loan is impaired / specifically provisioned"),
    FieldSpec("in_forbearance", _F.BOOLEAN, "Loan is subject to hardship, forbearance or standstill"),
    FieldSpec("covenant_breach", _F.BOOLEAN, "Loan-level financial covenant is in breach"),
    FieldSpec("is_related_party", _F.BOOLEAN, "Borrower is a related party of the originator"),
    FieldSpec("servicer", _F.STRING, "Servicer of the loan"),
    FieldSpec("remaining_term_months", _F.DECIMAL, "Months from the cut-off date to maturity", derived=True),
    FieldSpec("seasoning_months", _F.DECIMAL, "Months since origination at the cut-off date", derived=True),
    FieldSpec("valuation_age_months", _F.DECIMAL, "Age of the valuation at the cut-off date", derived=True),
)

COLLECTIONS_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("loan_id", _F.STRING, "Loan identifier (blank for trust-level items)"),
    FieldSpec("interest_collected", _F.DECIMAL, "Interest received in the collection period"),
    FieldSpec("fees_collected", _F.DECIMAL, "Fees received from borrowers (income)"),
    FieldSpec("scheduled_principal", _F.DECIMAL, "Scheduled principal received"),
    FieldSpec("prepayments", _F.DECIMAL, "Unscheduled principal and full repayments"),
    FieldSpec("recoveries", _F.DECIMAL, "Recoveries on previously defaulted loans"),
    FieldSpec("losses", _F.DECIMAL, "Principal written off in the period"),
    FieldSpec("further_advances", _F.DECIMAL, "Principal advanced to borrowers in the period"),
    FieldSpec("other_income", _F.DECIMAL, "Other income e.g. break costs, default interest"),
    FieldSpec("servicer_fee_deducted", _F.DECIMAL, "Servicing fee netted from remittance"),
    FieldSpec("remittance_date", _F.DATE, "Date funds were remitted to the collection account"),
)

BANK_STATEMENT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("transaction_date", _F.DATE, "Value date", required=True),
    FieldSpec("amount", _F.DECIMAL, "Signed amount (credit positive)", required=True),
    FieldSpec("description", _F.STRING, "Narrative"),
    FieldSpec("reference", _F.STRING, "Bank / payment reference"),
    FieldSpec("balance", _F.DECIMAL, "Running balance after the transaction"),
)

CATALOGUE: dict[Dataset, tuple[FieldSpec, ...]] = {
    Dataset.LOAN_TAPE: LOAN_TAPE_FIELDS,
    Dataset.COLLECTIONS: COLLECTIONS_FIELDS,
    Dataset.BANK_STATEMENT: BANK_STATEMENT_FIELDS,
}


def field_map(dataset: Dataset | str) -> dict[str, FieldSpec]:
    return {f.name: f for f in CATALOGUE[Dataset(dataset)]}


def catalogue_as_dict() -> dict[str, list[dict]]:
    return {ds.value: [f.to_dict() for f in specs] for ds, specs in CATALOGUE.items()}
