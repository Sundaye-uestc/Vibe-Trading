"""Exercise the HTTP SDK fix through the application's real ASGI assembly."""

from __future__ import annotations

import time

from starlette.testclient import TestClient

from mcp_server import _build_network_app


HEADERS = {"Accept": "application/json, text/event-stream"}
INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "session-reclamation-test", "version": "1"},
    },
}


def _manager(app):
    return next(route.endpoint.session_manager for route in app.routes
                if getattr(route, "path", None) == "/mcp")


def _wait_until_removed(manager, session_id):
    deadline = time.monotonic() + 3
    while session_id in manager._server_instances and time.monotonic() < deadline:
        time.sleep(0.01)
    assert session_id not in manager._server_instances


def test_http_sessions_have_limits_and_deletion_releases_capacity():
    app = _build_network_app("streamable-http", ["127.0.0.1"])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        manager = _manager(app)
        assert not manager.stateless
        assert 0 < manager.session_idle_timeout <= 1800
        assert 0 < manager.max_sessions <= 10_000
        manager.max_sessions = 1

        first = client.post("/mcp", headers=HEADERS, json=INITIALIZE)
        assert first.status_code == 200
        session_id = first.headers["mcp-session-id"]
        assert session_id in manager._server_instances
        refused = client.post("/mcp", headers=HEADERS, json=INITIALIZE)
        assert refused.status_code == 503

        deleted = client.delete("/mcp", headers={**HEADERS, "mcp-session-id": session_id})
        assert deleted.status_code == 200
        _wait_until_removed(manager, session_id)

        replacement = client.post("/mcp", headers=HEADERS, json=INITIALIZE)
        assert replacement.status_code == 200
        assert replacement.headers["mcp-session-id"] != session_id
        assert len(manager._server_instances) == 1


def test_idle_http_session_is_reclaimed_and_cannot_be_reused():
    app = _build_network_app("streamable-http", ["127.0.0.1"])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        manager = _manager(app)
        manager.session_idle_timeout = 0.05
        response = client.post("/mcp", headers=HEADERS, json=INITIALIZE)
        assert response.status_code == 200
        session_id = response.headers["mcp-session-id"]
        _wait_until_removed(manager, session_id)
        expired = client.post(
            "/mcp", headers={**HEADERS, "mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
        )
        assert expired.status_code == 404
