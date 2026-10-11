"""REST API entry point (also serves the web UI and the MCP streamable HTTP endpoint).

Each registered operation is published as ``POST /api/v1/ops/<operation>`` with its JSON
schema in the OpenAPI document. Requests are executed by the shared executor, so the API
has no business logic of its own.
"""

import contextlib
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.routing import Route

from .. import __version__, db
from ..config import get_settings
from ..ops.auth import authenticate
from ..ops.context import Actor, Channel
from ..ops.errors import OpError
from ..ops.executor import execute
from ..ops.registry import Operation, all_operations
from ..report_html import render_report_html

STATIC = Path(__file__).parent / "ui" / "static"


def _raw_key(request_headers: Any) -> str | None:
    return request_headers.get("authorization") or request_headers.get("x-api-key")


def _authenticate(raw: str | None) -> Actor:
    with db.session_scope() as session:
        return authenticate(session, raw)


async def current_actor(request: Request) -> Actor:
    return await run_in_threadpool(_authenticate, _raw_key(request.headers))


def _channel(request: Request) -> Channel:
    # The bundled web UI identifies itself; everything else is an API client.
    return Channel.UI if request.headers.get("x-securifi-client") == "ui" else Channel.API


def _error(exc: OpError, request_id: str | None = None) -> JSONResponse:
    return JSONResponse({"ok": False, "error": exc.to_dict(), "request_id": request_id}, status_code=exc.status)


class MCPEndpoint:
    """ASGI wrapper: authenticates the bearer key, then hands the request to the MCP session manager."""

    def __init__(self, manager):
        self.manager = manager

    async def __call__(self, scope, receive, send):
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        try:
            actor = await run_in_threadpool(_authenticate, _raw_key(headers))
        except OpError as exc:
            response = _error(exc)
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["securifi_actor"] = actor
        await self.manager.handle_request(scope, receive, send)


def create_app(database_url: str | None = None) -> FastAPI:
    settings = get_settings()
    db.configure(database_url)
    if settings.auto_create_schema:
        db.create_schema()

    mcp_manager = None
    if settings.mcp_http_enabled:
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        from .mcp_server import build_server, http_actor

        mcp_manager = StreamableHTTPSessionManager(app=build_server(http_actor), stateless=True, json_response=True)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        if mcp_manager is not None:
            async with mcp_manager.run():
                yield
        else:
            yield

    app = FastAPI(
        title="Securifi API",
        version=__version__,
        description="Trust management and warehouse facility operations for commercial real estate debt. "
                    "Authenticate with `Authorization: Bearer <api key>`. Every operation is also available as an MCP tool at /mcp.",
        lifespan=lifespan,
    )
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "POST"],
                           allow_headers=["authorization", "content-type", "idempotency-key", "x-api-key", "x-securifi-client"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(OpError)
    async def op_error_handler(_request: Request, exc: OpError):
        return _error(exc)

    @app.get("/healthz", tags=["system"])
    def healthz():
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["system"])
    def readyz():
        from sqlalchemy import text

        try:
            with db.session_scope() as s:
                s.execute(text("SELECT 1"))
        except Exception as exc:  # pragma: no cover - depends on infra
            return JSONResponse({"status": "unavailable", "error": type(exc).__name__}, status_code=503)
        return {"status": "ready"}

    @app.get("/api/v1/info", tags=["system"])
    def info():
        return {"organisation": settings.organisation_name, "environment": settings.environment, "version": __version__,
                "mcp_http": settings.mcp_http_enabled}

    @app.get("/api/v1/operations", tags=["system"])
    async def operations(request: Request):
        actor = await current_actor(request)
        return [{**op.describe(), "allowed": actor.can(op.permission)} for op in all_operations()]

    def add_operation_route(op: Operation) -> None:
        async def endpoint(request: Request):
            actor = await current_actor(request)
            raw = await request.body()
            try:
                params = await request.json() if raw.strip() else {}
            except ValueError:
                return JSONResponse({"ok": False, "error": {"code": "validation_failed", "message": "body must be JSON"}}, status_code=422)
            if not isinstance(params, dict):
                return JSONResponse({"ok": False, "error": {"code": "validation_failed", "message": "body must be a JSON object"}}, status_code=422)
            dry_run = request.query_params.get("dry_run", "").lower() in ("1", "true", "yes")
            result = await run_in_threadpool(
                execute, op.name, params, actor, _channel(request), dry_run=dry_run,
                idempotency_key=request.headers.get("idempotency-key"),
                request_id=request.headers.get("x-request-id") or uuid.uuid4().hex,
            )
            return JSONResponse(result.to_dict(), status_code=200 if result.ok else result.status)

        endpoint.__name__ = op.name.replace(".", "_")
        app.add_api_route(
            f"/api/v1/ops/{op.name}", endpoint, methods=["POST"], name=op.name, summary=op.summary,
            description=(op.description or op.summary) + f"\n\n**Permission:** `{op.permission}`" +
                        ("\n\nSupports `?dry_run=true` and an `Idempotency-Key` header." if op.mutating else ""),
            tags=[op.tags[0] if op.tags else "ops"],
            openapi_extra={"requestBody": {"required": False, "content": {"application/json": {"schema": op.input_schema()}}}},
        )

    for op in all_operations():
        add_operation_route(op)

    @app.get("/api/v1/reports/{report_id}/html", tags=["reports"], response_class=HTMLResponse)
    async def report_html(report_id: str, request: Request):
        actor = await current_actor(request)
        result = await run_in_threadpool(execute, "report.get", {"report_id": report_id}, actor, _channel(request))
        if not result.ok:
            return JSONResponse(result.to_dict(), status_code=result.status)
        return HTMLResponse(render_report_html(result.data, settings.organisation_name))

    if mcp_manager is not None:
        app.router.routes.append(Route("/mcp", endpoint=MCPEndpoint(mcp_manager), methods=["GET", "POST", "DELETE"]))

    if STATIC.exists():
        app.mount("/static", StaticFiles(directory=STATIC), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(STATIC / "index.html")

    return app
