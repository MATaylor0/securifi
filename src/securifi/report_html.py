"""Render stored reports as self-contained, print-ready HTML (use the browser's print-to-PDF)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any

SECTION_TITLES = {
    "facility": "Facility",
    "period": "Period",
    "run": "Calculation run",
    "pool": "Pool summary (eligible pool)",
    "compliance": "Eligibility and portfolio compliance",
    "borrowing_base": "Borrowing base",
    "stratifications": "Pool stratifications",
    "collections": "Collections",
    "available_funds": "Available funds",
    "interest": "Note interest",
    "fees": "Fees and expenses",
    "waterfall": "Priority of payments",
    "note_balances": "Note balances",
    "noteholder_distributions": "Noteholder distributions",
    "carry_forward": "Amounts carried forward",
    "register": "Note register",
    "reconciliation": "Bank account reconciliation",
    "events": "Trigger events",
    "warnings": "Warnings",
    "summary": "Summary",
    "tests": "Portfolio tests",
    "rules_snapshot": "Rules applied",
}

_RATE_KEYS = ("rate", "share", "ltv", "ltc", "occupancy")
_SKIP_KEYS = {"segments", "by_holder", "details", "created_at", "updated_at"}


def _fmt(key: str, value: Any) -> str:
    if value is None:
        return "&ndash;"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (dict, list)):
        return _render(value, key)
    text = str(value)
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return escape(text)
    if any(k in key for k in _RATE_KEYS) and "bps" not in key and abs(number) <= 5:
        return f"{(number * 100).quantize(Decimal('0.0001')).normalize():f}%"
    money = any(k in key for k in ("amount", "balance", "paid", "due", "interest", "net", "tax", "commitment", "outstanding", "funding", "excess"))
    if money:
        return f"{number:,.2f}"
    if number == number.to_integral_value():
        return f"{number:,.0f}"
    if abs(number) < 10:
        return f"{number.normalize():f}"
    return f"{number:,.2f}"


def _label(key: str) -> str:
    return escape(key.replace("_", " ").capitalize())


def _render(value: Any, key: str = "") -> str:
    if isinstance(value, list):
        if not value:
            return "<p class='muted'>None</p>"
        if all(isinstance(v, dict) for v in value):
            cols: list[str] = []
            for row in value:
                for k in row:
                    if k not in cols and k not in _SKIP_KEYS:
                        cols.append(k)
            head = "".join(f"<th>{_label(c)}</th>" for c in cols)
            body = "".join("<tr>" + "".join(f"<td>{_fmt(c, row.get(c))}</td>" for c in cols) + "</tr>" for row in value)
            return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        return "<ul>" + "".join(f"<li>{_fmt(key, v)}</li>" for v in value) + "</ul>"
    if isinstance(value, dict):
        rows = "".join(f"<tr><th>{_label(k)}</th><td>{_fmt(k, v)}</td></tr>" for k, v in value.items() if k not in _SKIP_KEYS)
        return f"<table class='kv'>{rows}</table>"
    return f"<p>{_fmt(key, value)}</p>"


def render_report_html(report: dict[str, Any], organisation: str) -> str:
    content = report["content"]
    status = content.get("status", "")
    parts = []
    for key, value in content.items():
        if key == "status":
            continue
        if key == "stratifications":
            inner = "".join(f"<h3>{_label(s['field'])}</h3>{_render(s['rows'], s['field'])}" for s in value) or "<p class='muted'>None</p>"
        elif key == "register" and isinstance(value, dict):
            inner = "".join(f"<h3>Class {escape(str(c['note_class']))}</h3>{_render(c['holders'])}" for c in value.get("classes", []))
        elif key == "compliance" and isinstance(value, dict):
            inner = _render(value.get("summary", {})) + "<h3>Tests</h3>" + _render(value.get("tests", []))
        elif key == "borrowing_base" and isinstance(value, dict):
            inner = _render({k: v for k, v in value.items() if k != "classes"}) + _render(value.get("classes", []))
        else:
            inner = _render(value, key)
        parts.append(f"<section><h2>{escape(SECTION_TITLES.get(key, key.replace('_', ' ').title()))}</h2>{inner}</section>")
    watermark = "<div class='draft'>DRAFT &ndash; not finalised</div>" if status == "DRAFT" else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(report['title'])}</title>
<style>
:root {{ --ink:#14213d; --muted:#6b7280; --line:#d9dee7; --accent:#1d4ed8; --bg:#ffffff; }}
body {{ font: 13px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: var(--ink); background: var(--bg); margin: 32px; }}
header {{ border-bottom: 3px solid var(--ink); padding-bottom: 12px; margin-bottom: 20px; }}
header .org {{ color: var(--muted); text-transform: uppercase; letter-spacing: .08em; font-size: 11px; }}
h1 {{ font-size: 20px; margin: 4px 0; }} h2 {{ font-size: 15px; border-bottom: 1px solid var(--line); padding-bottom: 4px; margin-top: 28px; }}
h3 {{ font-size: 13px; margin: 14px 0 6px; color: var(--muted); }}
table {{ border-collapse: collapse; width: 100%; margin: 6px 0 12px; font-variant-numeric: tabular-nums; }}
th, td {{ border-bottom: 1px solid var(--line); padding: 4px 8px; text-align: left; vertical-align: top; }}
thead th {{ background: #f3f5f9; font-weight: 600; }} table.kv th {{ width: 30%; font-weight: 500; color: var(--muted); }}
td table {{ margin: 0; }} .muted {{ color: var(--muted); }}
.draft {{ background: #fef3c7; border: 1px solid #f59e0b; padding: 8px 12px; font-weight: 600; margin-bottom: 16px; }}
footer {{ margin-top: 32px; color: var(--muted); font-size: 11px; border-top: 1px solid var(--line); padding-top: 8px; }}
@media print {{ body {{ margin: 12mm; }} section {{ break-inside: avoid-page; }} }}
</style></head><body>
<header><div class="org">{escape(organisation)}</div><h1>{escape(report['title'])}</h1>
<div class="muted">Generated {escape(str(report.get('generated_at', '')))} &middot; Report {escape(report['id'])}</div></header>
{watermark}
{''.join(parts)}
<footer>Content hash (SHA-256): {escape(report['content_hash'])}</footer>
</body></html>"""
