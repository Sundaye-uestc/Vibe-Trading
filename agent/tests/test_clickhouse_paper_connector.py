"""The optional paper-data transport is explicit, read-only and bounded."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

import src.clickhouse_connector as connector


def mock_http(monkeypatch, handler):
    original = httpx.Client

    def client(**kwargs):
        assert kwargs["timeout"] > 0
        assert kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(connector, "httpx", SimpleNamespace(
        Client=client, BasicAuth=httpx.BasicAuth, HTTPError=httpx.HTTPError,
    ))


def test_no_host_never_probes_an_implicit_database(monkeypatch):
    monkeypatch.delenv("CLICKHOUSE_HOST", raising=False)
    with pytest.raises(ValueError, match="not configured"):
        connector.ClickHouseConnector()


def test_select_uses_bound_parameters_readonly_setting_and_auth(monkeypatch):
    observed = []

    def handler(request):
        observed.append(request)
        return httpx.Response(200, json={"meta": [{"name": "value"}], "data": [{"value": 1}]})

    mock_http(monkeypatch, handler)
    client = connector.ClickHouseConnector(host="https://database.invalid", user="reader", password="fixture-password")
    injected = "600000.SH' OR 1=1"
    frame = client.query("SELECT value FROM prices WHERE code={code:String}", params={"param_code": injected})
    assert frame.to_dict("records") == [{"value": 1}]
    request = observed[0]
    assert request.url.port is None or request.url.port == 443
    assert request.url.params["readonly"] == "1"
    assert request.url.params["param_code"] == injected
    assert injected not in request.content.decode()
    assert request.content.decode().endswith(" FORMAT JSON")
    assert request.headers["authorization"].startswith("Basic ")


@pytest.mark.parametrize("sql,params", [("DROP TABLE prices", {}), ("SELECT 1; DROP TABLE prices", {}),
                                        ("SELECT 1", {"readonly": "0"})])
def test_writes_and_setting_overrides_fail_before_transport(monkeypatch, sql, params):
    def handler(request):
        raise AssertionError("Invalid query must not be sent")
    mock_http(monkeypatch, handler)
    with pytest.raises(ValueError):
        connector.ClickHouseConnector(host="localhost").query(sql, params=params)


@pytest.mark.parametrize("status", [302, 401, 403, 500])
def test_error_bodies_and_redirects_never_expose_secrets(monkeypatch, status):
    def handler(request):
        return httpx.Response(status, text="fixture-password private database details",
                              headers={"Location": "https://different.invalid"})
    mock_http(monkeypatch, handler)
    with pytest.raises(RuntimeError) as caught:
        connector.ClickHouseConnector(host="localhost", password="fixture-password").query("SELECT 1")
    assert "fixture-password" not in str(caught.value)
    assert "private database details" not in str(caught.value)
    assert f"HTTP {status}" in str(caught.value)


def test_calendar_keeps_explicit_closed_days_and_does_not_fill_missing_days(monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [
            {"exchange": "SSE", "cal_date": "20261024", "is_open": 0, "pretrade_date": "20261023"},
            {"exchange": "SSE", "cal_date": "20261026", "is_open": 1, "pretrade_date": "20261023"},
        ]})
    mock_http(monkeypatch, handler)
    rows = connector.ClickHouseConnector(host="localhost").get_trade_calendar("2026-10-24", "2026-10-26")
    assert list(rows["is_open"]) == [0, 1]
    assert len(rows) == 2  # October 25 was absent; it must stay absent.
    assert seen[0].url.params["param_start_date"] == "20261024"
    assert "exchange IN ('SSE', 'SZSE', 'BSE')" in seen[0].content.decode()


@pytest.mark.parametrize("host", ["ftp://host", "http://user:secret@host", "http://host/query", "http://host?password=secret"])
def test_endpoint_rejects_embedded_credentials_and_non_host_urls(host):
    with pytest.raises(ValueError):
        connector.ClickHouseConnector(host=host)
