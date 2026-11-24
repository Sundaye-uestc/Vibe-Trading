"""Provider-driven resolution for instruments outside the builtin name catalog."""

import json

import pytest

from src.agent.grounding import GroundingLedger
from src.instrument_query import exact_candidate_match
from src.tools import symbol_search_tool as search


def install_sources(monkeypatch, eastmoney, yahoo):
    seen = {}

    def query_em(query):
        seen["eastmoney"] = query
        return eastmoney, "ok"

    def query_yahoo(query, **_):
        seen["yahoo"] = query
        return yahoo, "ok"

    monkeypatch.setattr(search, "_search_selected_connector", lambda *args: ([], None, None))
    monkeypatch.setattr(search, "_search_eastmoney", query_em)
    monkeypatch.setattr(search, "_search_tencent", lambda query: ([], "ok"))
    monkeypatch.setattr(search, "_search_yahoo", query_yahoo)
    monkeypatch.setattr(search, "_enrich_us_cik", lambda rows: (rows, search._NO_US))
    return seen


@pytest.mark.parametrize("query,native,expected,provider_query", [
    ("US.XYZZ", "XYZZ.US", "XYZZ.US", "XYZZ"),
    ("BRK.B.US", "BRK-B.US", "BRK.B.US", "BRK-B"),
    ("sh600123", "600123.SH", "600123.SH", "600123.SS"),
    ("SZ.002751", "002751.SZ", "002751.SZ", "002751.SZ"),
    ("HK.1234", "01234.HK", "01234.HK", "1234.HK"),
    ("RIVAL.TO", "RIVAL.TO", "RIVAL.TO", "RIVAL.TO"),
    ("935741.KQ", "935741.KQ", "935741.KQ", "935741.KQ"),
    ("ABC.L", "ABC.L", "ABC.L", "ABC.L"),
    ("^UNSEEN", "^UNSEEN", "^UNSEEN", "^UNSEEN"),
    ("MXX=F", "MXX=F", "MXX=F", "MXX=F"),
    ("CHF/SEK", "CHFSEK=X", "CHFSEK=X", "CHFSEK=X"),
])
def test_qualified_query_looks_up_native_symbol_before_limiting(monkeypatch, tmp_path, query, native, expected, provider_query):
    row = {"symbol": native, "market": "us" if native.endswith(".US") else "global", "source": "yahoo"}
    seen = install_sources(monkeypatch, [{"symbol": "OTHER.US", "source": "eastmoney"}], [row])
    answer = search.SymbolSearchTool().execute(query=query, limit=1)
    assert seen["yahoo"] == provider_query
    data = json.loads(answer)["data"]
    assert [c["symbol"] for c in data["candidates"]] == [expected]
    book = GroundingLedger(run_dir=tmp_path, user_message=f"Look up {query}")
    book.ingest_tool_result(tool_name="search_symbol", arguments={"query": query}, result=answer,
                            call_id="search", success=True)
    assert book.identity_status == "locked"
    assert book.authorized_symbols == {expected}


def test_missing_global_index_does_not_lock_proxy_returned_by_both_sources(monkeypatch, tmp_path):
    row = {"symbol": "TRACK.US", "name": "Tracks the Index", "type": "ETF"}
    install_sources(monkeypatch, [dict(row, source="eastmoney")], [dict(row, source="yahoo")])
    answer = search.SymbolSearchTool().execute(query="^ABSENT")
    assert json.loads(answer)["data"]["count"] == 0
    answer = search.SymbolSearchTool().execute(query="Example regional index")
    book = GroundingLedger(run_dir=tmp_path, user_message="Analyze the regional benchmark")
    book.ingest_tool_result(tool_name="search_symbol", arguments={"query": "Example regional index"},
                            result=answer, call_id="index", success=True)
    assert book.identity_status == "ambiguous"
    assert book.authorized_symbols == set()


@pytest.mark.parametrize("limit", [1, 10])
def test_bare_ticker_ambiguity_survives_source_corroboration_and_display_limit(monkeypatch, tmp_path, limit):
    listing = {"symbol": "XYZ.US", "name": "Example", "source": "eastmoney", "cik": "0001"}
    foreign = {"symbol": "XYZ.L", "name": "Example", "source": "yahoo"}
    install_sources(monkeypatch, [listing], [dict(listing, source="yahoo"), foreign])
    answer = search.SymbolSearchTool().execute(query="XYZ", limit=limit)
    book = GroundingLedger(run_dir=tmp_path, user_message="Look up XYZ")
    book.ingest_tool_result(tool_name="search_symbol", arguments={"query": "XYZ"}, result=answer,
                            call_id="search", success=True)
    assert book.identity_status == "ambiguous"
    assert not book.authorized_symbols


def test_bare_index_ticker_does_not_collide_with_derivative_crypto_base(monkeypatch, tmp_path):
    install_sources(monkeypatch, [], [
        {"symbol": "BMARK-USD", "name": "Index token", "type": "cryptocurrency", "source": "yahoo"},
        {"symbol": "^BMARK", "name": "Benchmark", "type": "index", "source": "yahoo"},
    ])
    answer = search.SymbolSearchTool().execute(query="BMARK", limit=1)
    book = GroundingLedger(run_dir=tmp_path, user_message="Get the index quote")
    book.ingest_tool_result(tool_name="search_symbol", arguments={"query": "BMARK"}, result=answer,
                            call_id="index", success=True)
    assert book.authorized_symbols == {"^BMARK"}


def test_bare_crypto_base_does_not_identify_a_pair():
    pair = {"symbol": "ALT-USDC", "market": "crypto"}
    assert exact_candidate_match("ALTUSDC", pair)
    assert exact_candidate_match("ALT/USDC", pair)
    assert not exact_candidate_match("ALT", pair)


def test_yahoo_us_etf_market_is_bound_to_the_exchange():
    assert search._from_yahoo_symbol("FUND", {"quoteType": "ETF", "exchange": "PCX"}) == ("FUND.US", "us")
    assert search._from_yahoo_symbol("FUND", {"quoteType": "ETF", "exchange": "OTHER"}) == ("FUND", "global")
