"""The four entry points share one registry, one permission check and one audit trail."""

import json

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session

from securifi import db
from securifi.interfaces import cli
from securifi.interfaces.api import create_app
from securifi.interfaces.mcp_server import build_server
from securifi.ops.auth import authenticate
from securifi.ops.registry import all_operations


@pytest.fixture()
def client(users, database):
    app = create_app(database)
    with TestClient(app) as c:
        yield c


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_every_operation_has_a_route_and_schema(client, users):
    spec = client.get("/openapi.json").json()
    for op in all_operations():
        assert f"/api/v1/ops/{op.name}" in spec["paths"], op.name
    catalogue = client.get("/api/v1/operations", headers=auth(users["viewer"])).json()
    assert {o["name"] for o in catalogue} == {op.name for op in all_operations()}
    assert any(o["allowed"] is False for o in catalogue)


def test_api_auth_permissions_and_audit(client, users):
    assert client.post("/api/v1/ops/facility.list").status_code == 401
    assert client.post("/api/v1/ops/facility.list", headers=auth("sfk_wrong")).status_code == 401
    r = client.post("/api/v1/ops/facility.create", headers=auth(users["viewer"]), json={"code": "F", "name": "F"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied"
    r = client.post("/api/v1/ops/facility.create", headers={**auth(users["admin"]), "x-securifi-client": "ui"},
                    json={"code": "F1", "name": "Facility one"})
    assert r.status_code == 200 and r.json()["data"]["code"] == "F1"
    r = client.post("/api/v1/ops/facility.create?dry_run=true", headers=auth(users["admin"]), json={"code": "F2", "name": "x"})
    assert r.json()["dry_run"] is True
    r = client.post("/api/v1/ops/facility.create", headers=auth(users["admin"]), json={"code": "F3"})
    assert r.status_code == 422 and r.json()["error"]["details"][0]["field"] == "name"
    events = client.post("/api/v1/ops/audit.query", headers=auth(users["admin"]), json={"operation": "facility.create"}).json()["data"]
    assert [(e["outcome"], e["channel"]) for e in events] == [("invalid", "api"), ("dry_run", "api"), ("success", "ui"), ("denied", "api")]
    assert client.post("/api/v1/ops/audit.verify", headers=auth(users["admin"])).json()["data"]["valid"]


def test_ui_and_health(client):
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").json()["status"] == "ready"
    page = client.get("/")
    assert page.status_code == 200 and "Securifi" in page.text


def test_report_html(client, users):
    from securifi import demo
    from securifi.ops.context import Channel
    from securifi.ops.executor import execute

    with db.session_scope() as s:
        admin = authenticate(s, users["admin"])
    info = demo.run(admin, Channel.SYSTEM)
    rep = execute("report.borrowing_base_certificate", {"facility_id": info["facility_id"]}, admin, Channel.API)
    assert rep.ok
    html = client.get(f"/api/v1/reports/{rep.data['id']}/html", headers=auth(users["viewer"]))
    assert html.status_code == 200 and "Borrowing Base Certificate" in html.text


def test_mcp_tools_are_filtered_and_executed(users):
    with db.session_scope() as s:
        agent = authenticate(s, users["agent"])
        admin = authenticate(s, users["admin"])

    async def scenario():
        async with create_connected_server_and_client_session(build_server(lambda _s: agent)) as session:
            tools = {t.name: t for t in (await session.list_tools()).tools}
            assert "facility_list" in tools and "period_run_approve" not in tools and "facility_create" not in tools
            assert "dry_run" in tools["compliance_run"].inputSchema["properties"]
            res = await session.call_tool("facility_list", {})
            assert not res.isError and res.structuredContent["data"] == []
            denied = await session.call_tool("facility_create", {"code": "X", "name": "x"})
            assert denied.isError and denied.structuredContent["error"]["code"] == "permission_denied"
            resources = await session.list_resources()
            assert any(str(r.uri) == "securifi://guides/month-end" for r in resources.resources)
            prompt = await session.get_prompt("month_end_close", {"facility": "CRE-WH-1", "period": "3"})
            assert "period 3" in prompt.messages[0].content.text
        async with create_connected_server_and_client_session(build_server(lambda _s: admin)) as session:
            res = await session.call_tool("facility_create", {"code": "M1", "name": "Via MCP", "dry_run": True})
            assert res.structuredContent["dry_run"] is True

    anyio.run(scenario)


def test_mcp_over_http(client, users):
    headers = {**auth(users["agent"]), "accept": "application/json, text/event-stream", "content-type": "application/json"}
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
    init = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
    assert init.status_code == 200, init.text
    r = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                   "params": {"name": "auth_whoami", "arguments": {}}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["structuredContent"]["data"]["role"] == "agent"
    assert body["result"]["structuredContent"]["data"]["channel"] == "mcp"


def test_cli_call(users, database, capsys, monkeypatch):
    monkeypatch.setenv("SECURIFI_API_KEY", users["admin"])
    url = database
    assert cli.main(["--database-url", url, "call", "facility.create", "--set", "code=CLI-1", "--set", "name=From CLI"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["data"]["code"] == "CLI-1"
    assert cli.main(["--database-url", url, "call", "facility.get", "--json", '{"facility_id": "NOPE"}']) == 2
    capsys.readouterr()
    assert cli.main(["--database-url", url, "ops", "list", "--tag", "periods"]) == 0
    assert "period_run.finalise" in capsys.readouterr().out
