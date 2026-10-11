"""Translate servicer files into canonical records.

A mapping is plain JSON so it can be authored in the UI, over the API or by an AI agent::

    {
      "fields": {
        "loan_id":       {"source": "Loan Number"},
        "current_balance": {"source": "Closing Balance"},
        "ltv":           {"source": "LVR %", "transform": "percent"},
        "maturity_date": {"source": "Expiry", "date_format": "%d/%m/%Y"},
        "property_type": {"source": "Security Type", "value_map": {"Commercial Office": "office"}},
        "lien_position": {"default": "first"}
      },
      "keep_unmapped": true
    }

Without an explicit mapping, source columns are matched to canonical names after
normalisation ("Current Balance" -> "current_balance").
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .fields import Dataset, FieldSpec, FieldType, field_map
from .money import D

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d-%b-%Y", "%m/%d/%Y", "%Y%m%d")
_TRUE = {"y", "yes", "true", "1", "t"}
_FALSE = {"n", "no", "false", "0", "f", ""}


@dataclass
class Issue:
    row: int
    field: str
    message: str
    severity: str = "error"  # error rows are rejected; warnings are loaded

    def to_dict(self) -> dict:
        return {"row": self.row, "field": self.field, "message": self.message, "severity": self.severity}


def normalise_name(name: str) -> str:
    name = re.sub(r"[%#]", lambda m: {"%": " pct", "#": " no"}[m.group()], name.strip())
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def parse_csv(text: str) -> list[dict[str, str]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]


def parse_date(value: Any, fmt: str | None = None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for candidate in ((fmt,) if fmt else _DATE_FORMATS):
        try:
            return datetime.strptime(text, candidate).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError as exc:
        raise ValueError(f"unrecognised date {value!r}") from exc


def coerce(value: Any, spec_type: FieldType, rule: Mapping[str, Any] | None = None) -> Any:
    rule = rule or {}
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    transform = rule.get("transform")
    if isinstance(value, str):
        value = value.strip()
        if spec_type in (FieldType.DECIMAL, FieldType.RATIO):
            value = re.sub(r"^[A-Z]{3}|[$€£\s]", "", value)
    if spec_type == FieldType.DECIMAL:
        return D(value) * D(rule.get("multiplier", 1))
    if spec_type == FieldType.RATIO:
        if isinstance(value, str) and value.endswith("%"):
            return D(value[:-1]) / 100
        number = D(value)
        if transform == "percent":
            return number / 100
        return number
    if spec_type == FieldType.INTEGER:
        return int(D(value).to_integral_value())
    if spec_type == FieldType.DATE:
        return parse_date(value, rule.get("date_format")).isoformat()
    if spec_type == FieldType.BOOLEAN:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError(f"not a boolean: {value!r}")
    text = str(value)
    if transform == "lower" or spec_type == FieldType.ENUM:
        text = normalise_name(text) if spec_type == FieldType.ENUM else text.lower()
    elif transform == "upper":
        text = text.upper()
    return text


def _resolve_source(row: Mapping[str, Any], rule: Mapping[str, Any], name: str, normalised: dict[str, str]) -> Any:
    if "source" in rule:
        source = rule["source"]
        if source in row:
            return row[source]
        key = normalised.get(normalise_name(source))
        return row.get(key) if key else None
    key = normalised.get(name)
    return row.get(key) if key else None


def apply_mapping(
    rows: Iterable[Mapping[str, Any]],
    dataset: Dataset | str,
    mapping: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[Issue]]:
    """Return (records, issues). Rows with errors are excluded from ``records``."""
    specs = field_map(dataset)
    mapping = mapping or {}
    rules: Mapping[str, Mapping[str, Any]] = mapping.get("fields", {})
    keep_unmapped = mapping.get("keep_unmapped", True)
    records: list[dict[str, Any]] = []
    issues: list[Issue] = []
    for index, row in enumerate(rows, start=1):
        normalised = {normalise_name(k): k for k in row.keys()}
        used_sources: set[str] = set()
        record: dict[str, Any] = {}
        row_errors = False
        targets = set(specs) | set(rules)
        for name in targets:
            rule = rules.get(name, {})
            spec = specs.get(name) or FieldSpec(name, FieldType(rule.get("type", "string")), "custom attribute")
            raw = _resolve_source(row, rule, name, normalised)
            if "source" in rule:
                used_sources.add(normalise_name(rule["source"]))
            elif name in normalised:
                used_sources.add(name)
            if raw is not None and "value_map" in rule:
                raw = rule["value_map"].get(str(raw).strip(), raw)
            if (raw is None or raw == "") and "default" in rule:
                raw = rule["default"]
            try:
                value = coerce(raw, spec.type, rule)
            except (ValueError, ArithmeticError) as exc:
                issues.append(Issue(index, name, str(exc)))
                row_errors = True
                continue
            if value is None:
                if spec.required:
                    issues.append(Issue(index, name, "required field is missing"))
                    row_errors = True
                continue
            if spec.type == FieldType.ENUM and spec.choices and value not in spec.choices:
                issues.append(Issue(index, name, f"value {value!r} is not one of the standard choices", "warning"))
            record[name] = value
        if keep_unmapped:
            for norm, original in normalised.items():
                if norm in used_sources or norm in record or norm == "":
                    continue
                raw = row[original]
                if raw not in (None, ""):
                    record[norm] = _auto_type(raw)
        if not row_errors:
            records.append(record)
    return records, issues


def _auto_type(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", text.replace(",", "")):
        return D(text)
    return text


def enrich_loan(record: dict[str, Any], as_of: date) -> dict[str, Any]:
    """Derive standard analytics fields that rules commonly reference."""
    from .expressions import _months_between

    balance = record.get("current_balance")
    commitment = record.get("commitment_amount")
    if commitment is not None and balance is not None and "undrawn_amount" not in record:
        record["undrawn_amount"] = max(Decimal(0), D(commitment) - D(balance))
    valuation = record.get("valuation_amount")
    if "ltv" not in record and valuation and balance is not None and D(valuation) > 0:
        record["ltv"] = (D(balance) / D(valuation)).quantize(Decimal("0.0001"))
    cost = record.get("total_project_cost")
    if "ltc" not in record and cost and D(cost) > 0:
        exposure = commitment if commitment is not None else balance
        if exposure is not None:
            record["ltc"] = (D(exposure) / D(cost)).quantize(Decimal("0.0001"))
    if record.get("maturity_date"):
        record["remaining_term_months"] = _months_between(as_of, date.fromisoformat(record["maturity_date"])).quantize(Decimal("0.01"))
    if record.get("origination_date"):
        record["seasoning_months"] = _months_between(date.fromisoformat(record["origination_date"]), as_of).quantize(Decimal("0.01"))
    if record.get("valuation_date"):
        record["valuation_age_months"] = _months_between(date.fromisoformat(record["valuation_date"]), as_of).quantize(Decimal("0.01"))
    return record


def typed_view(record: Mapping[str, Any], dataset: Dataset | str = Dataset.LOAN_TAPE) -> dict[str, Any]:
    """Convert a stored (JSON) record into Python types for rule evaluation."""
    specs = field_map(dataset)
    out: dict[str, Any] = {}
    for key, value in record.items():
        spec = specs.get(key)
        if value is None:
            out[key] = None
        elif spec is None:
            out[key] = _auto_type(value) if isinstance(value, str) else (D(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value)
        elif spec.type in (FieldType.DECIMAL, FieldType.RATIO):
            out[key] = D(value)
        elif spec.type == FieldType.INTEGER:
            out[key] = D(value)
        elif spec.type == FieldType.DATE:
            out[key] = date.fromisoformat(str(value))
        else:
            out[key] = value
    return out
