"""Exact benchmark names must resolve before fuzzy provider suggestions."""

import json
from unittest.mock import patch

import pytest

from src.agent.grounding import GroundingLedger
from src.tools import symbol_search_tool as ss
from src.us_indices import exact_us_index_symbol


@pytest.mark.parametrize("query,symbol", [
    ("nasdaq composite index", "^IXIC"),
    ("纳斯达克综合指数", "^IXIC"),
    ("纳指", "^IXIC"),
    ("ixic", "^IXIC"),
    ("^IXIC", "^IXIC"),
    ("^IXIC Nasdaq Composite", "^IXIC"),
    ("NASDAQ Composite（^IXIC）", "^IXIC"),
    ("NDX", "^NDX"),
    ("纳指１００", "^NDX"),
    ("NASDAQ-100 index", "^NDX"),
    ("标普500", "^GSPC"),
    ("Dow Jones Industrial Average", "^DJI"),
])
def test_exact_known_index_resolves_without_fuzzy_or_quote_service(query, symbol):
    with patch.object(ss, "_search_selected_connector", side_effect=AssertionError("unexpected provider lookup")):
        data = json.loads(ss.SymbolSearchTool().execute(query=query, limit=1))["data"]
    assert data["query"] == query
    assert data["count"] == 1
    assert data["sources"] == {"index_catalog": "ok"}
    assert data["candidates"][0]["symbol"] == symbol
    assert data["candidates"][0]["type"] == "index"
    assert data["candidates"][0]["source"] == "index_catalog"


@pytest.mark.parametrize("query", [
    "Nasdaq", "Nasdaq Inc", "Nasdaq Stock Market", "Fidelity Nasdaq Composite Index",
    "Nasdaq Composite Index CAD", "纳指ETF", "纳斯达克100期货", "IXIC-USD",
    "IXIC.HK", "^IXIC Nasdaq 100", "纳斯达克综合指数 (^NDX)", "NDX options",
    "Nasdaq Composite and Dow Jones Industrial Average", "S&P 500 Equal Weight",
])
def test_exact_lookup_does_not_conflate_other_products_or_requests(query):
    assert exact_us_index_symbol(query) is None


@pytest.mark.parametrize("prompt,status", [
    ("美股纳斯达克综合指数昨日涨幅？", "locked"),
    ("只看港股，纳指", "ambiguous"),
])
def test_catalog_result_obeys_market_constraint(tmp_path, prompt, status):
    ledger = GroundingLedger(run_dir=tmp_path, user_message=prompt)
    args = {"query": "纳斯达克综合指数"}
    result = ss.SymbolSearchTool().execute(**args)
    ledger.ingest_tool_result(tool_name="search_symbol", arguments=args, result=result,
                              call_id="search-index", success=True)
    assert ledger.identity_status == status
    assert ("^IXIC" in ledger.authorized_symbols) == (status == "locked")
