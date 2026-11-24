"""Exact chart metadata can establish an instrument absent from autocomplete."""

import json

import pytest
import requests

from src.tools import symbol_search_tool as ss


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(ss, "_search_selected_connector", lambda *a, **kw: ([], None, None))
    monkeypatch.setattr(ss, "_search_eastmoney", lambda q: ([], "ok"))
    monkeypatch.setattr(ss, "_search_tencent", lambda q: ([], "ok"))
    monkeypatch.setattr(ss, "_enrich_us_cik", lambda rows: (rows, ss._NO_US))

    def resolve(query, *, quotes=(), meta=None, search_error=False, chart_status=None):
        calls = []

        def get(url, *, params, host_key, min_interval):
            assert host_key == ss.yahoo_client.HOST_KEY and min_interval >= 0
            calls.append(url)
            if url.endswith("/search"):
                if search_error:
                    raise requests.Timeout("search timed out")
                return {"quotes": list(quotes)}
            assert "/v8/finance/chart/" in url
            if chart_status:
                response = requests.Response()
                response.status_code = chart_status
                raise requests.HTTPError(response=response)
            return {"chart": {"result": [{"meta": meta, "timestamp": [1],
                                          "indicators": {"quote": [{"close": [9999]}]}}]}}

        monkeypatch.setattr(ss.yahoo_client, "throttled_get_json", get)
        data = json.loads(ss.SymbolSearchTool().execute(query=query))["data"]
        return data, calls
    return resolve


@pytest.mark.parametrize("query,native,kind,exchange,returned", [
    ("GC=F", "GC=F", "FUTURE", "CMX", "GC=F"),
    ("^FTSE", "^FTSE", "INDEX", "FGI", "^FTSE"),
    ("BRK.B.US", "BRK-B", "EQUITY", "NYQ", "BRK.B.US"),
    ("0700.HK", "0700.HK", "EQUITY", "HKG", "00700.HK"),
    ("ABC.L", "ABC.L", "EQUITY", "LSE", "ABC.L"),
    ("600519.SH", "600519.SS", "EQUITY", "SHH", "600519.SH"),
    ("XAUUSD=X", "XAUUSD=X", "CURRENCY", "CCY", "XAUUSD=X"),
])
def test_verified_chart_identity_after_search_miss(run, query, native, kind, exchange, returned):
    meta = {"symbol": native, "instrumentType": kind, "exchangeName": exchange, "shortName": "fixture"}
    data, calls = run(query, quotes=[{"symbol": "TRACKER", "quoteType": "ETF", "exchange": "PCX"}], meta=meta)
    assert calls[-1].endswith("/chart/" + native)
    candidate, = data["candidates"]
    assert (candidate["symbol"], candidate["type"], candidate["exchange"], candidate["source"]) == (
        returned, kind.lower(), exchange, "yahoo",
    )
    assert candidate["name"] == "fixture"


@pytest.mark.parametrize("query,meta", [
    ("GC=F", {"symbol": "GCG27.CMX", "instrumentType": "FUTURE", "exchangeName": "CMX"}),
    ("GC=F", {"symbol": "GC=F", "instrumentType": "EQUITY", "exchangeName": "NYQ"}),
    ("ABC.US", {"symbol": "ABC", "instrumentType": "EQUITY", "exchangeName": "LSE"}),
    ("ABC.TO", {"symbol": "ABC.TO", "instrumentType": "EQUITY", "exchangeName": "VAN"}),
    ("GC=F", {"symbol": "GC=F", "instrumentType": "FUTURE"}),
    ("GC=F", None),
])
def test_numbers_and_lookalikes_do_not_establish_identity(run, query, meta):
    data, _ = run(query, meta=meta)
    assert data["candidates"] == []


def test_exact_search_needs_no_chart_and_free_text_cannot_probe_chart(run):
    exact = {"symbol": "CL=F", "quoteType": "FUTURE", "exchange": "NYM"}
    data, calls = run("CL=F", quotes=[exact])
    assert len(calls) == 1 and data["candidates"][0]["symbol"] == "CL=F"
    data, calls = run("Crude oil futures")
    assert len(calls) == 1 and not data["candidates"]


def test_lookup_status_distinguishes_confirmed_absence_and_server_error(run):
    data, _ = run("ABC=F", chart_status=404)
    assert data["sources"]["yahoo"] == "ok" and not data["candidates"]
    data, _ = run("ABC=F", chart_status=503)
    assert "failed" in data["sources"]["yahoo"] and not data["candidates"]
    data, _ = run("GC=F", search_error=True, meta={"symbol": "GC=F", "instrumentType": "FUTURE", "exchangeName": "CMX"})
    assert data["candidates"][0]["symbol"] == "GC=F"
