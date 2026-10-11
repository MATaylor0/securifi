# Securifi

Trust management and warehouse facility operations for **commercial real estate debt**.

Securifi is the system of record a CRE lender's treasury or trust management team uses to run its
warehouse facilities and securitisation trusts: the note register, fees and interest, collection and
interest periods, eligibility criteria and portfolio parameters, the borrowing base, trust bank account
reconciliation, the priority of payments and month-end reporting to financiers.

It is **not a servicing system**. Loan tapes (pool cuts), collections reports and bank statements are
ingested from the servicing platform and bank, mapped to a canonical CRE data model, and tested against
rules written to mirror the legal documents.

It is **AI native**: every capability is one operation in a single registry, exposed identically through
the web UI, a REST API, an MCP server (for Claude and any other MCP client) and a CLI, with one shared
pathway for permissions, validation and a tamper-evident audit log.

It is **single tenant and portable**: one deployment per business, on its own infrastructure, configured
entirely through environment variables. It runs as one container against PostgreSQL (SQLite for local use).

## What it does

| Area | Capabilities |
| --- | --- |
| Facility structure | Warehouses, term trusts, repo and bilateral facilities; trustee / manager / servicer parties; revolving period and maturities; holiday calendars; legal document register |
| Note register | Note classes with seniority, commitments, floating (BBSW / SOFR / ...) or fixed coupons, margins, base-rate floors, advance rates. Immutable ledger of commitments, drawdowns, repayments and transfers per noteholder |
| Drawdowns | Pro-rata allocation across funders, commitment checks, and a borrowing base test before funding (block / warn / off), with an audited override for approvers |
| Data ingestion | Loan tapes, collections reports and bank statements as CSV or JSON. Saved per-servicer mappings (column renames, %, date formats, value maps, defaults). Unmapped columns are kept as custom fields. Row-level data quality issues. Derived LTV, LTC, remaining term, seasoning, valuation age |
| Eligibility criteria | Loan-level rules in a safe expression language (`ltv <= 0.65 and lien_position == 'first'`), conditional on loan type (`applies_when`), exclude or warn, each mapped to a document clause. Missing data is reported as such |
| Portfolio parameters | Concentration by any field with per-group limits, filtered concentrations, top-N sponsors, largest exposure, weighted averages, counts, aggregates. Treated as **limits** (excess concentration leaves the borrowing base), **covenants** (flag and raise an event) or information |
| Borrowing base | Eligible balance less excess concentrations, cumulative advance rates per class, availability and deficiency |
| Periods | Generated collection / interest / determination / payment schedules with business-day conventions |
| Interest & fees | Daily accrual on actual register balances (mid-period drawdowns handled), ACT/365F, ACT/360, ACT/ACT, 30/360, 30E/360; fixings on period start; floors. Fees on pool balance, notes outstanding, class balance, commitments, undrawn commitment or facility limit; fixed fees; minimums; GST |
| Waterfall | Configurable income / principal priority of payments: fees, interest (pro rata within a step), sequential or pro-rata principal with limits, reserve funding and draws, payments, transfers between waterfalls, residual. Conditions on trigger events, test results and variables. Shortfalls carried forward |
| Month end | Calculate → approve (four-eyes, enforced) → finalise. Finalising posts note repayments to the register and creates expected payments for reconciliation; the period is locked |
| Bank accounts | Collection, payment, reserve and liquidity accounts; statement import; expected cash flows from collections, drawdowns and the waterfall; auto-matching (reference, amount and date) and manual matching; balance vs statement checks |
| Reporting | Investor report (pool, stratifications, eligibility, tests, borrowing base, interest, fees, waterfall, note balances, noteholder distributions, reconciliation, events) and borrowing base certificates; stored with a SHA-256 content hash, rendered as print-ready HTML, DRAFT-marked until the run is finalised |
| Governance | Roles (admin, trust manager, analyst, agent, auditor, viewer), API keys with optional narrower scopes and expiry, per-facility access scoping, hash-chained audit log of every call (including denied, invalid and dry-run calls), dry runs, idempotency keys |

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

securifi admin bootstrap --email you@example.com   # prints your first API key
securifi demo                                      # loads a demo CRE warehouse
securifi serve                                     # http://localhost:8000  (UI, /docs, /mcp)
```

Sign in to the UI with the API key. The demo facility has two senior funders, a mezzanine funder
and a seller note, a 12-loan CRE tape, eight eligibility criteria, eight portfolio parameters, a
two-waterfall priority of payments and a 12-month schedule, ready for the period 1 month-end run.

Run the tests (SQLite by default; set `SECURIFI_TEST_DATABASE_URL` to run against PostgreSQL):

```bash
pytest
```

## One registry, four entry points

```
            Web UI        REST API        MCP server        CLI      (+ system jobs)
               \              |               |              /
                └──────────── executor.execute(op, params, actor, channel) ────────────┘
                        1. resolve operation in the registry
                        2. check permission (role ∩ key scopes, facility scope)
                        3. validate params against the operation's schema
                        4. run the handler in a transaction
                        5. append a hash-chained audit event in the same transaction
                        6. commit, or roll back (dry run / failure) and audit the outcome
```

Operations are declared once in `src/securifi/ops/handlers/` with a typed input model, the permission
they need and whether they change data. Everything else is generated from the registry:

* **REST**: `POST /api/v1/ops/<operation>` for every operation, documented in OpenAPI at `/docs`.
  `?dry_run=true` previews any change; `Idempotency-Key` makes retries safe.
* **MCP**: every operation is a tool (`facility.create` → `facility_create`), filtered to what the key may
  do, with `dry_run` on every mutating tool. Resources (field catalogue, operation catalogue, month-end
  guide) and prompts (`month_end_close`, `onboard_facility`, `explain_breach`) help agents work well.
* **UI**: forms are generated from operation schemas, and the Operations console exposes every operation.
* **CLI**: `securifi ops list`, `securifi ops describe <op>`, `securifi call <op> --set k=v --file-param content=tape.csv`.

### Connecting an AI client

Over HTTP (the API server exposes streamable HTTP at `/mcp`):

```json
{ "mcpServers": { "securifi": { "url": "https://securifi.example.com/mcp",
  "headers": { "Authorization": "Bearer sfk_..." } } } }
```

Or locally over stdio:

```json
{ "mcpServers": { "securifi": { "command": "securifi", "args": ["mcp"],
  "env": { "SECURIFI_API_KEY": "sfk_...", "SECURIFI_DATABASE_URL": "postgresql://..." } } } }
```

Give agents their own service-account user with the `agent` role (preparation work only; no
configuration, approval or finalisation), optionally narrowed further with key scopes. Their actions
appear in the audit log with channel `mcp`.

## Month-end close

1. `data.ingest_loan_tape` - month-end pool cut
2. `data.ingest_collections` - servicer collections for the period
3. `rates.set_fixing` - base rate fixings
4. `compliance.run` - eligibility, portfolio tests, borrowing base
5. `bank.import_statement`, `reconciliation.auto_match`, `reconciliation.status`
6. `period_run.calculate` - interest, fees, waterfall
7. `period_run.approve` - by someone other than the preparer
8. `period_run.finalise` - posts repayments and expected payments; locks the period
9. `report.investor` - the financier report

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `SECURIFI_DATABASE_URL` | `sqlite:///./var/securifi.db` | `postgresql://user:pass@host/db` in production |
| `SECURIFI_ENVIRONMENT` | `development` | `production` refuses SQLite unless `SECURIFI_ALLOW_SQLITE_IN_PRODUCTION=true` |
| `SECURIFI_ORGANISATION_NAME` | `Securifi` | Shown in the UI and on reports |
| `SECURIFI_MCP_HTTP_ENABLED` | `true` | Serve MCP at `/mcp` |
| `SECURIFI_CORS_ORIGINS` | *(none)* | Comma-separated origins for browser clients on other domains |
| `SECURIFI_AUTO_CREATE_SCHEMA` | `true` | Create missing tables at start-up |
| `SECURIFI_API_KEY` | | Used by `securifi call`, `securifi mcp` |

## Deployment

* **Docker**: `docker build -t securifi .` then `docker compose up -d` (app + PostgreSQL), and
  `docker compose exec app securifi admin bootstrap --email you@example.com`.
* **Kubernetes**: `deploy/k8s/securifi.yaml` (non-root, read-only root filesystem, health probes).
  Put the database URL in a secret and terminate TLS at the ingress.
* The app is stateless; scale horizontally behind a load balancer. Back up the database - it holds
  the register, the audit log and every stored report.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and the rule and waterfall languages.

## Status and roadmap

This is the foundation: the domain engine, the operation registry, the four entry points and the
month-end lifecycle work end to end and are covered by tests on SQLite and PostgreSQL. Next steps
before a production rollout:

* Database migrations (Alembic) instead of `create_all`
* SSO (OIDC / SAML) for UI users alongside API keys
* Scheduled jobs (nightly ingestion from SFTP / servicer APIs, scheduled compliance runs) on the `system` channel
* Excel / PDF export of reports and loan-level eligibility files
* Hedging (swap / cap) cash flows, multi-currency facilities and post-enforcement waterfalls
* Regulatory reporting templates (e.g. RBA securitisation data) and an investor portal
