"""MCP entry point: every registered operation is exposed as an MCP tool.

Any MCP-capable AI client (Claude, IDE agents, internal agent frameworks) can operate the
platform with exactly the same permissions, validation and audit trail as a human in the UI.
Tools are filtered to what the calling key is permitted to do; mutating tools accept a
``dry_run`` flag so an agent can preview a change before committing it.

Transports:
  * stdio  - ``securifi mcp`` (the key comes from SECURIFI_API_KEY)
  * HTTP   - streamable HTTP at ``/mcp`` on the API server (Authorization: Bearer <key>)
"""

from __future__ import annotations

import copy
import json
from typing import Any, Callable

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from pydantic import AnyUrl

from .. import __version__
from ..db import json_dumps
from ..engine.fields import catalogue_as_dict
from ..ops.context import Actor, Channel
from ..ops.executor import execute
from ..ops.registry import Operation, all_operations, get_operation

INSTRUCTIONS = """Securifi is a trust management and warehouse facility platform for commercial real estate debt.
Every tool maps to one platform operation and runs with the permissions of your API key; all calls are audited.
Start with facility_list / facility_get. Use catalog_rule_language before writing eligibility criteria,
portfolio tests or waterfalls, and catalog_fields for canonical loan tape fields. Mutating tools accept
dry_run=true to preview the result without saving it - prefer a dry run first for anything consequential.
Approvals and finalisation are deliberately reserved for human approvers (four-eyes control)."""

RESOURCES: dict[str, tuple[str, str, Callable[[], str]]] = {}


def _resource(uri: str, name: str, description: str):
    def wrap(fn: Callable[[], str]) -> Callable[[], str]:
        RESOURCES[uri] = (name, description, fn)
        return fn
    return wrap


@_resource("securifi://catalog/fields", "Canonical data fields", "Loan tape, collections and bank statement fields")
def _fields() -> str:
    return json.dumps(catalogue_as_dict(), indent=2)


@_resource("securifi://catalog/operations", "Operation catalogue", "Every operation, its permission and input schema")
def _ops() -> str:
    return json_dumps([op.describe() for op in all_operations()], indent=2)


@_resource("securifi://guides/month-end", "Month-end close guide", "The standard month-end sequence of operations")
def _month_end() -> str:
    return MONTH_END_GUIDE


MONTH_END_GUIDE = """# Month-end close (per facility, per period)

1. data_ingest_loan_tape        - load the month-end pool cut (as_of_date = collection period end)
2. data_ingest_collections      - load the servicer collections report for the period
3. rates_set_fixing             - make sure base rate fixings exist for the interest period start
4. compliance_run               - purpose 'period_end'; review failures with compliance_loan_results
5. bank_import_statement        - load collection / reserve account statements
6. reconciliation_auto_match    - then reconciliation_status for anything unmatched
7. period_run_calculate         - interest, fees, waterfall; review warnings and the waterfall lines
8. period_run_approve           - a different person from the preparer (four-eyes)
9. period_run_finalise          - posts note repayments and expected payments
10. report_investor             - produce the financier report (FINAL once the run is finalised)
"""

PROMPTS = {
    "month_end_close": (
        "Walk through the month-end close for a facility period",
        [types.PromptArgument(name="facility", description="Facility code", required=True),
         types.PromptArgument(name="period", description="Period sequence number", required=True)],
        lambda a: f"Run the month-end close for facility {a.get('facility')} period {a.get('period')}. "
                  f"Follow this sequence, checking each result before moving on and stopping to report any data quality "
                  f"issues, eligibility failures, test breaches or reconciliation breaks:\n\n{MONTH_END_GUIDE}\n"
                  "Use dry_run=true before each mutating step. Do not approve or finalise - summarise what an approver must review.",
    ),
    "onboard_facility": (
        "Set up a new warehouse facility from its legal documents",
        [types.PromptArgument(name="summary", description="Term sheet / facility agreement summary", required=True)],
        lambda a: "Set up a new facility from these terms. Create the facility, legal documents, note classes, noteholders and "
                  "commitments, fees, bank accounts, eligibility criteria and portfolio tests (with clause references), "
                  "the waterfall and the period schedule. Read catalog_rule_language first, dry-run each step, and list any "
                  f"terms you could not map.\n\nTerms:\n{a.get('summary', '')}",
    ),
    "explain_breach": (
        "Explain the latest compliance breaches for a facility",
        [types.PromptArgument(name="facility", description="Facility code", required=True)],
        lambda a: f"For facility {a.get('facility')}, get the latest compliance run (compliance_list then compliance_get). "
                  "For each failed test and ineligible loan explain the cause, the clause reference, the borrowing base impact, "
                  "and what would cure it.",
    ),
}


def tool_schema(op: Operation) -> dict[str, Any]:
    schema = copy.deepcopy(op.input_schema())
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    if op.mutating:
        schema["properties"]["dry_run"] = {"type": "boolean", "default": False,
                                           "description": "Validate and calculate without saving anything"}
    return schema


def tool_for(op: Operation) -> types.Tool:
    description = op.summary
    if op.description:
        description += "\n\n" + op.description
    description += f"\n\nPermission: {op.permission}." + (" Changes data (supports dry_run)." if op.mutating else " Read only.")
    return types.Tool(
        name=op.tool_name, title=op.summary, description=description, inputSchema=tool_schema(op),
        annotations=types.ToolAnnotations(title=op.summary, readOnlyHint=not op.mutating,
                                          destructiveHint=False if not op.mutating else None, openWorldHint=False),
    )


def build_server(resolve_actor: Callable[[Server], Actor]) -> Server:
    """``resolve_actor`` returns the caller for the current request (fixed for stdio, per request for HTTP)."""
    server: Server = Server("securifi", version=__version__, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        actor = resolve_actor(server)
        return [tool_for(op) for op in all_operations() if actor.can(op.permission)]

    @server.call_tool(validate_input=False)  # validation happens in the shared executor (and is audited)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        actor = resolve_actor(server)
        op = get_operation(name)
        args = dict(arguments or {})
        dry_run = bool(args.pop("dry_run", False))
        result = await anyio.to_thread.run_sync(
            lambda: execute(op.name if op else name, args, actor, Channel.MCP, dry_run=dry_run))
        payload = result.to_dict()
        return types.CallToolResult(content=[types.TextContent(type="text", text=json_dumps(payload, indent=2))],
                                    structuredContent=json.loads(json_dumps(payload)), isError=not result.ok)

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return [types.Resource(uri=AnyUrl(uri), name=name, description=desc, mimeType="application/json" if "catalog" in uri else "text/markdown")
                for uri, (name, desc, _) in RESOURCES.items()]

    @server.read_resource()
    async def read_resource(uri: AnyUrl) -> str:
        resolve_actor(server)
        entry = RESOURCES.get(str(uri))
        if entry is None:
            raise ValueError(f"unknown resource {uri}")
        return entry[2]()

    @server.list_prompts()
    async def list_prompts() -> list[types.Prompt]:
        return [types.Prompt(name=n, description=d, arguments=args) for n, (d, args, _) in PROMPTS.items()]

    @server.get_prompt()
    async def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
        if name not in PROMPTS:
            raise ValueError(f"unknown prompt {name}")
        desc, _, render = PROMPTS[name]
        return types.GetPromptResult(description=desc, messages=[
            types.PromptMessage(role="user", content=types.TextContent(type="text", text=render(arguments or {})))])

    return server


async def run_stdio(actor: Actor) -> None:
    from mcp.server.stdio import stdio_server

    server = build_server(lambda _s: actor)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def http_actor(server: Server) -> Actor:
    """Actor for streamable-HTTP requests, placed on the request state by the API's auth wrapper."""
    request = server.request_context.request
    actor = getattr(getattr(request, "state", None), "securifi_actor", None)
    if actor is None:
        raise PermissionError("unauthenticated")
    return actor
