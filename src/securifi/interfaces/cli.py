"""Command line entry point.

    securifi db init                          create the schema
    securifi admin bootstrap --email ...      first admin user + API key (only when no users exist)
    securifi serve                            API + web UI + MCP over HTTP
    securifi mcp                              MCP over stdio (for desktop AI clients)
    securifi ops list | describe <op>         browse the operation registry
    securifi call <op> [--json ...] [--set k=v] [--file-param content=path] [--dry-run]
    securifi demo                             load the demo CRE warehouse

``call`` runs operations locally through the shared executor, authenticated by
SECURIFI_API_KEY (or --api-key), so CLI activity is permissioned and audited like any other.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .. import __version__, db
from ..config import get_settings


def _actor(args: argparse.Namespace):
    from ..ops.auth import authenticate

    key = getattr(args, "api_key", None) or os.environ.get("SECURIFI_API_KEY")
    with db.session_scope() as s:
        return authenticate(s, key)


def _parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _print(data: Any) -> None:
    from ..db import json_dumps

    print(json_dumps(data, indent=2))


def cmd_db_init(args: argparse.Namespace) -> int:
    db.create_schema()
    print("schema created")
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select

    from ..models import User
    from ..ops import audit
    from ..ops.auth import issue_key

    db.create_schema()
    with db.session_scope() as s:
        if s.execute(select(func.count()).select_from(User)).scalar_one():
            print("users already exist; use admin.user_create with an admin key", file=sys.stderr)
            return 1
        user = User(email=args.email, name=args.name or args.email, role="admin")
        s.add(user)
        s.flush()
        key, raw = issue_key(s, user, "bootstrap")
        audit.append(s, actor_id=None, actor_label="system", channel="system", operation="admin.bootstrap", mutating=True,
                     outcome="success", params={"email": args.email}, entity_refs=[user.id, key.id])
    print(json.dumps({"user_id": user.id, "email": user.email, "api_key": raw,
                      "note": "Store this key securely; it will not be shown again."}, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    import anyio

    from .mcp_server import run_stdio

    db.create_schema()
    actor = _actor(args)
    anyio.run(run_stdio, actor)
    return 0


def cmd_ops_list(args: argparse.Namespace) -> int:
    from ..ops.registry import all_operations

    for op in all_operations():
        if args.tag and args.tag not in op.tags:
            continue
        flag = "W" if op.mutating else "R"
        print(f"{flag}  {op.name:<38} {op.permission:<24} {op.summary}")
    return 0


def cmd_ops_describe(args: argparse.Namespace) -> int:
    from ..ops.registry import get_operation

    op = get_operation(args.name)
    if op is None:
        print(f"unknown operation {args.name}", file=sys.stderr)
        return 1
    _print(op.describe())
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    from ..ops.context import Channel
    from ..ops.executor import execute

    params: dict[str, Any] = json.loads(args.json) if args.json else {}
    if args.params_file:
        params.update(json.loads(Path(args.params_file).read_text()))
    for item in args.set or []:
        key, _, value = item.partition("=")
        params[key] = _parse_value(value)
    for item in args.file_param or []:
        key, _, path = item.partition("=")
        params[key] = Path(path).read_text()
    result = execute(args.operation, params, _actor(args), Channel.CLI, dry_run=args.dry_run, idempotency_key=args.idempotency_key)
    _print(result.to_dict())
    return 0 if result.ok else 2


def cmd_demo(args: argparse.Namespace) -> int:
    from .. import demo

    db.create_schema()
    actor = _actor(args) if (args.api_key or os.environ.get("SECURIFI_API_KEY")) else None
    _print(demo.run(actor) if actor else demo.run())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="securifi", description="Securifi trust management platform")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--database-url", help="Override SECURIFI_DATABASE_URL")
    sub = parser.add_subparsers(dest="command", required=True)

    p_db = sub.add_parser("db", help="Database management").add_subparsers(dest="db_command", required=True)
    p_db.add_parser("init", help="Create the schema").set_defaults(func=cmd_db_init)

    p_admin = sub.add_parser("admin", help="Administration").add_subparsers(dest="admin_command", required=True)
    b = p_admin.add_parser("bootstrap", help="Create the first admin user and API key")
    b.add_argument("--email", required=True)
    b.add_argument("--name")
    b.set_defaults(func=cmd_bootstrap)

    s = sub.add_parser("serve", help="Run the API, web UI and MCP HTTP endpoint")
    s.add_argument("--host", default=os.environ.get("SECURIFI_HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("SECURIFI_PORT", "8000")))
    s.set_defaults(func=cmd_serve)

    m = sub.add_parser("mcp", help="Run the MCP server over stdio")
    m.add_argument("--api-key")
    m.set_defaults(func=cmd_mcp)

    ops = sub.add_parser("ops", help="Browse the operation registry").add_subparsers(dest="ops_command", required=True)
    ol = ops.add_parser("list")
    ol.add_argument("--tag")
    ol.set_defaults(func=cmd_ops_list)
    od = ops.add_parser("describe")
    od.add_argument("name")
    od.set_defaults(func=cmd_ops_describe)

    c = sub.add_parser("call", help="Invoke an operation")
    c.add_argument("operation")
    c.add_argument("--json", help="Parameters as a JSON object")
    c.add_argument("--params-file", help="Parameters from a JSON file")
    c.add_argument("--set", action="append", metavar="KEY=VALUE", help="Set one parameter (value parsed as JSON if possible)")
    c.add_argument("--file-param", action="append", metavar="KEY=PATH", help="Set a parameter to a file's text, e.g. content=tape.csv")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--idempotency-key")
    c.add_argument("--api-key")
    c.set_defaults(func=cmd_call)

    d = sub.add_parser("demo", help="Load the demo facility")
    d.add_argument("--api-key")
    d.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    get_settings()
    db.configure(args.database_url)
    from ..ops.errors import OpError

    try:
        return args.func(args)
    except OpError as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()}), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
