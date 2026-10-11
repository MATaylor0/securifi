# Architecture

## Layers

```
src/securifi/
  engine/          Pure domain logic. No database, no I/O, fully unit tested.
    money.py         Decimal helpers, pro-rata allocation that sums exactly to the cent
    daycount.py      Day count conventions, business-day calendars
    schedule.py      Collection / interest / payment period generation
    expressions.py   Safe rule expression language
    fields.py        Canonical CRE data model (loan tape, collections, bank statements)
    mapping.py       Servicer file layout -> canonical records, enrichment
    compliance.py    Eligibility, portfolio tests, borrowing base, stratifications
    interest.py      Daily-balance interest and fee accrual
    waterfall.py     Configurable priority of payments
    reconciliation.py  Bank line matching
  models.py        SQLAlchemy models (portable across PostgreSQL and SQLite)
  ops/
    registry.py      The single registry of operations
    executor.py      The single execution pathway (auth, validation, transaction, audit)
    permissions.py   Roles and permissions
    auth.py          API keys
    audit.py         Hash-chained audit log
    handlers/        Operation definitions, grouped by domain
  interfaces/
    api.py           REST API + UI hosting + MCP over HTTP
    mcp_server.py    MCP tools / resources / prompts generated from the registry
    cli.py           Command line
    ui/static/       Dependency-free web UI (no CDN; works air-gapped)
  report_html.py   Print-ready HTML rendering of stored reports
  demo.py          Demo facility built entirely through operations
```

Handlers translate between the database and the engine; the engine never sees ORM objects, so
calculations can be reasoned about and tested in isolation.

## Principles

* **One registry, one pathway.** No entry point has logic of its own. A capability added to the
  registry is immediately available in the UI, API, MCP and CLI, with the same permission and audit.
* **Exact arithmetic.** All amounts and rates are `Decimal`. Expression literals are parsed as
  decimals, and pro-rata splits use the largest-remainder method so allocations always sum exactly.
* **Ledgers, not balances.** Note positions are derived from an immutable register ledger; bank
  balances from statement lines. Interest accrues on daily register balances.
* **Reproducibility.** Compliance runs store a snapshot of the rule versions they used. Period runs
  store their full inputs and outputs. Reports store their content and a SHA-256 hash.
* **Controls in the platform, not the process.** Four-eyes approval, finalised periods that cannot be
  recalculated, borrowing base checks on drawdowns, facility scoping and narrowed agent keys are
  enforced by the operations themselves.
* **Tamper evidence.** Each audit event carries the hash of the previous one. `audit.verify` recomputes
  the chain and reports the first altered or missing event.

## Rule expression language

Python-style expressions evaluated by a whitelist interpreter (no attribute access, imports or arbitrary
calls). Names refer to canonical or custom loan fields. Comparisons with dates work with ISO strings.

```
ltv <= 0.65
property_type in ['office', 'industrial', 'logistics']
lien_position == 'first' and not is_defaulted
loan_stage != 'construction' or presales_coverage >= 1.0
months_between(valuation_date, date('2026-06-30')) <= 12
```

Functions: `abs min max round len lower upper startswith contains date days_between months_between
coalesce is_null decimal`. A rule that reads a missing field fails with `data missing: <field>`, so
gaps in servicer data are visible rather than silently passing.

## Portfolio tests

| Type | Config |
| --- | --- |
| `concentration` | `group_by`, `max`, optional `limits` per group, `filter`, `measure`, `basis` (`eligible` / `total`) |
| `filtered_concentration` | `filter`, `max` and/or `min` |
| `top_n_concentration` | `group_by`, `n`, `max` |
| `largest_exposure` | optional `group_by`, `max` (amount) or `max_pct` |
| `weighted_average` | `field`, optional `weight`, `filter`, `max` and/or `min` |
| `count` | optional `distinct`, `filter`, `min` and/or `max` |
| `aggregate` | `field`, optional `filter`, `min` and/or `max` |

Treatment `limit` removes the excess from the borrowing base (excess amounts from separate tests are
added together, single pass). `covenant` flags the breach and its `event_code` without changing the
borrowing base. Where a facility's documents define concentration excess differently (iterative or
ordered application), that is a new test type in `engine/compliance.py`.

## Waterfall definition

```json
{
  "order": ["income", "principal"],
  "waterfalls": {
    "income": [
      {"type": "fee", "fee": "TRUSTEE", "clause_ref": "Trust Deed 9.1(a)"},
      {"type": "interest", "classes": ["A"]},
      {"type": "reserve", "account": "RESERVE", "target": "class_balance['A'] * reserve_pct"},
      {"type": "transfer", "to": "principal", "when": "events['AMORTISATION_EVENT']"},
      {"type": "residual", "payee": "Residual unitholder"}
    ],
    "principal": [
      {"type": "principal", "classes": ["A", "B"], "mode": "sequential", "limit": "borrowing_base_deficiency"},
      {"type": "residual", "payee": "Originator"}
    ]
  }
}
```

`available_funds` maps collections fields to each waterfall (`{"income": {"plus": [...], "minus": [...]}}`).
Conditions and amounts can read `available`, `fees_due`, `interest_due`, `class_balance`,
`reserve_balance`, `events`, `tests`, `pool_balance`, `eligible_balance`, `adjusted_eligible_balance`,
`borrowing_base_deficiency`, `is_revolving`, `period_sequence` and facility `variables`.

## Security model

* API keys are random 256-bit tokens, stored only as SHA-256 hashes, revocable and optionally expiring.
* Permissions = role permissions ∩ key scopes; facility access can be restricted per user.
* Sensitive fields (payment details, account numbers, secrets) are redacted from audit parameters;
  large payloads (file contents) are stored as length + hash.
* Unexpected errors return a generic message with the request id; the detail is in the audit log.
* Production mode refuses to start on SQLite.
