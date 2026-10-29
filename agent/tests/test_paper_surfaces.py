"""Authenticated HTTP and CLI/MCP entrances share the isolated paper engine."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import Client

from src.api import paper_trade_routes as routes
from src.tools.paper_trade.accounts import PaperAccounts


@pytest.fixture
def account_service(tmp_path, monkeypatch):
    calls = []

    def settle(payload):
        calls.append(payload)
        return json.dumps({
            "schema_version": "pt-v1.1", "tool": "paper_trade_step",
            "end_state": payload["account_state"], "fills": [], "rejections": [],
            "nav_series": [{"trade_date": payload["to_date"], "nav": payload["account_state"]["cash"]}],
        })

    service = PaperAccounts(tmp_path / "accounts.db", clock=lambda: datetime(2026, 10, 20, 22, tzinfo=timezone.utc), step=settle)
    monkeypatch.setattr(routes, "PaperAccounts", lambda: service)
    return service, calls


def app_with_routes(*, auth_override=False):
    app = FastAPI()
    routes.register_paper_trade_routes(app)
    if auth_override:
        app.dependency_overrides[routes.require_auth] = lambda: None
        app.dependency_overrides[routes.require_settings_write_auth] = lambda: None
    return app


def test_real_cli_dispatch_reads_and_advances_the_same_account_as_http(account_service, tmp_path, monkeypatch, capsys):
    from cli import _legacy
    from cli.commands import paper_trade

    service, calls = account_service
    monkeypatch.setattr(paper_trade, "PaperAccounts", lambda: service)
    state = tmp_path / "initial-state.json"
    state.write_text(json.dumps({"cash": 500, "positions": []}), encoding="utf-8")
    assert _legacy.main(["paper", "create", "--label", "中文模拟账户", "--market", "us", "--state", str(state)]) == 0
    account = json.loads(capsys.readouterr().out)
    client = TestClient(app_with_routes(auth_override=True))
    assert client.get("/api/paper-accounts").json()[0]["id"] == account["id"]
    assert client.get(f'/api/paper-accounts/{account["id"]}').json()["label"] == "中文模拟账户"

    orders = tmp_path / "orders.json"
    orders.write_text("[]", encoding="utf-8")
    assert _legacy.main(["paper", "advance", account["id"], "--revision", "0", "--to-date", "2026-10-20", "--orders", str(orders)]) == 0
    advanced = json.loads(capsys.readouterr().out)
    assert advanced["committed"] is True and advanced["account"]["revision"] == 1
    assert calls[0]["orders"] == []
    assert _legacy.main(["paper", "show", account["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["revision"] == 1
    assert _legacy.main(["paper", "history", account["id"]]) == 0
    assert json.loads(capsys.readouterr().out) == client.get(f'/api/paper-accounts/{account["id"]}/history').json()
    assert _legacy.main(["paper", "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == account["id"]


def test_paper_api_validation_conflict_and_data_failure_do_not_advance(account_service):
    service, _ = account_service
    client = TestClient(app_with_routes(auth_override=True))
    created = client.post("/api/paper-accounts", json={"label": "test", "market": "us", "account_state": {"cash": 100, "positions": []}})
    assert created.status_code == 200
    account = created.json()
    url = f'/api/paper-accounts/{account["id"]}/advance'
    assert client.post(url, json={"expected_revision": True, "to_date": "2026-10-20", "orders": []}).status_code == 422
    assert client.post(url, json={"expected_revision": 7, "to_date": "2026-10-20", "orders": []}).status_code == 409
    assert client.post(url, json={"expected_revision": 0, "to_date": "2026-10-20", "orders": [], "strategy_path": "anything.py"}).status_code == 422
    service.step = lambda payload: json.dumps({"code": "PAPER_DATA_NOT_READY", "message": "raw bars unavailable"})
    result = client.post(url, json={"expected_revision": 0, "to_date": "2026-10-20", "orders": []})
    assert result.status_code == 200 and result.json()["committed"] is False
    assert service.get(account["id"])["revision"] == 0
    assert client.get("/api/paper-accounts/unknown").status_code == 404
    assert client.get("/api/paper-accounts/invalid%0Aid").status_code == 400
    assert client.post("/api/paper-accounts", json={"label": "bad", "market": "crypto", "account_state": {"cash": 1}}).status_code == 422


@pytest.mark.parametrize("path", ["/api/paper-accounts", "/api/paper-accounts/account/advance", "/api/paper-trade/step"])
def test_paper_writes_reject_cross_origin_even_with_an_api_key(account_service, monkeypatch, path):
    import api_server

    service, _ = account_service
    monkeypatch.setattr(api_server, "_API_KEY", "paper-test-auth")
    client = TestClient(app_with_routes(), client=("127.0.0.1", 50000))
    result = client.post(path, json={}, headers={"Authorization": "Bearer paper-test-auth", "Origin": "https://attacker.invalid"})
    assert result.status_code == 403
    assert "Cross-site" in result.json()["detail"]
    assert service.list() == []


def test_paper_reads_and_writes_require_auth_for_remote_clients(account_service, monkeypatch):
    import api_server

    monkeypatch.setattr(api_server, "_API_KEY", "paper-test-auth")
    client = TestClient(app_with_routes(), client=("203.0.113.20", 50000))
    for path in ["/api/paper-accounts", "/api/paper-accounts/unknown", "/api/paper-accounts/unknown/history"]:
        assert client.get(path).status_code == 401
    assert client.post("/api/paper-trade/step", json={}).status_code == 401
    assert client.get("/api/paper-accounts", headers={"Authorization": "Bearer paper-test-auth"}).status_code == 200


def test_blocking_paper_operation_leaves_the_api_event_loop_responsive(account_service, monkeypatch):
    service, _ = account_service
    started, release = threading.Event(), threading.Event()

    def slow_create(**kwargs):
        started.set()
        assert release.wait(5)
        return {"id": "created"}

    monkeypatch.setattr(service, "create", slow_create)
    app = app_with_routes(auth_override=True)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        creation = pool.submit(client.post, "/api/paper-accounts", json={"label": "x", "market": "us", "account_state": {"cash": 1}})
        try:
            assert started.wait(2)
            probe = pool.submit(client.get, "/ping")
            assert probe.result(timeout=2).json() == {"ok": True}
        finally:
            release.set()
        assert creation.result(timeout=2).status_code == 200


def test_mcp_paper_wrapper_accepts_structured_strings_and_preserves_error_channel(monkeypatch):
    import mcp_server
    from src.tools.paper_trade import tool

    received = []

    def step(payload):
        received.append(payload)
        return json.dumps({"code": "PAPER_DATA_NOT_READY", "message": "fixture gap"})

    monkeypatch.setattr(tool, "run_paper_trade_step", step)

    async def run():
        async with Client(mcp_server.mcp) as client:
            names = {item.name for item in await client.list_tools()}
            assert "paper_trade_step" in names
            return await client.call_tool("paper_trade_step", {
                "account_id": "fixture", "market": "us", "account_state": '{"cash":100,"positions":[]}',
                "from_date": "2026-10-20", "to_date": "2026-10-20", "orders": "[]",
            })

    result = asyncio.run(run())
    assert not result.is_error
    assert json.loads(result.content[0].text)["code"] == "PAPER_DATA_NOT_READY"
    assert received[0]["account_state"] == {"cash": 100, "positions": []}
    assert received[0]["orders"] == [] and "strategy_path" not in received[0]


def test_cli_pure_step_and_malformed_json_return_nonzero(tmp_path, capsys):
    from cli import _legacy

    payload = tmp_path / "payload.json"
    payload.write_text('{"market":"unsupported"}', encoding="utf-8")
    assert _legacy.main(["paper", "step", str(payload)]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "INVALID_REQUEST"
    payload.write_text("{invalid", encoding="utf-8")
    assert _legacy.main(["paper", "step", str(payload)]) == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_main_application_registers_paper_routes_without_duplicate_endpoints():
    import api_server

    paths = [getattr(route, "path", None) for route in api_server.app.routes]
    assert paths.count("/api/paper-accounts") == 2
    assert paths.count("/api/paper-trade/step") == 1
