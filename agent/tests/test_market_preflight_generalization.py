"""A new instrument can be read after an actual, matching resolver result."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.agent.context import ContextBuilder
from src.agent.grounding import GroundingLedger
from src.agent.loop import AgentLoop
from src.agent.tools import BaseTool, ToolRegistry
from src.agent.trace import TraceWriter


class RecordingTool(BaseTool):
    parameters = {"type": "object", "properties": {}}
    repeatable = True

    def __init__(self, name, callback, *, identity_argument=None, readonly=True):
        self.name = name
        self.description = name
        self.is_readonly = readonly
        self.market_identity_argument = identity_argument
        self.cache_ttl = 300 if name == "get_market_data" else 0
        self.callback = callback

    def execute(self, **kwargs):
        return json.dumps(self.callback(**kwargs))


def call(name, call_id, **args):
    return SimpleNamespace(id=call_id, name=name, arguments=args)


@pytest.fixture
def make_reader(tmp_path, request):
    def build(candidates):
        history = []

        def resolve(query, **kwargs):
            history.append(("search_symbol", query))
            rows = candidates.get(query, [])
            return {"ok": True, "source": "symbol_search", "data": {
                "query": query, "candidates": rows, "count": len(rows), "sources": {"fixture": "ok"},
            }}

        def market(codes, **kwargs):
            history.append(("get_market_data", list(codes)))
            return {code: [{"trade_date": "2026-11-23", "close": 42}] for code in codes}

        def news(code, **kwargs):
            history.append(("get_stock_news", code))
            return {"ok": True, "data": []}

        exchange = Mock(return_value={"ok": True})
        registry = ToolRegistry()
        for tool in (RecordingTool("search_symbol", resolve), RecordingTool("get_market_data", market, identity_argument="codes"),
                     RecordingTool("get_stock_news", news, identity_argument="code"),
                     RecordingTool("place_order", exchange, readonly=False)):
            registry.register(tool)
        run = tmp_path / "isolated-run"
        run.mkdir()
        agent = AgentLoop(registry=registry, llm=SimpleNamespace(), max_iterations=3)
        agent.memory.run_dir = str(run)
        agent._grounding = GroundingLedger(run_dir=run, user_message="看看这个标的的行情")
        trace = TraceWriter(run)
        request.addfinalizer(trace.close)
        messages = []

        def process(*calls):
            agent._process_tool_calls(list(calls), ContextBuilder, messages, trace, [], len(messages) + 1)

        return process, history, messages, agent._grounding, exchange

    return build


def candidate(symbol, market, kind):
    return {"symbol": symbol, "name": "Fixture instrument", "market": market, "type": kind, "source": "fixture"}


@pytest.mark.parametrize("symbol,returned,market,kind", [
    ("609873.SH", "609873.SS", "cn", "equity"),
    ("ZXQ=F", "ZXQ=F", "futures", "future"),
    ("OBSX.US", "OBSX.US", "us", "etf"),
])
def test_preflight_resolves_and_fetches_unseen_instrument_once(make_reader, symbol, returned, market, kind):
    process, history, _, ledger, exchange = make_reader({symbol: [candidate(returned, market, kind)]})
    process(call("get_market_data", "first", codes=[symbol], interval="1D"))
    assert history == [("search_symbol", symbol), ("get_market_data", [symbol])]
    assert ledger.authorized_symbols == {symbol}
    process(call("get_market_data", "cached", codes=[symbol], interval="1D"))
    assert history == [("search_symbol", symbol), ("get_market_data", [symbol])]
    process(call("get_market_data", "second", codes=[symbol], interval="1W"))
    assert history[-1] == ("get_market_data", [symbol])
    assert history.count(("search_symbol", symbol)) == 1
    exchange.assert_not_called()


def test_news_consumer_obtains_a_real_resolution_before_reading(make_reader):
    symbol = "OBSX.US"
    process, history, _, _, _ = make_reader({symbol: [candidate(symbol, "us", "etf")]})
    process(call("get_stock_news", "news", code=symbol, scope="stock"))
    assert history == [("search_symbol", symbol), ("get_stock_news", symbol)]


def test_unique_exact_fund_name_resolves_to_provider_symbol(make_reader):
    name = "信澄样本基金"
    row = {**candidate("590987.SH", "cn", "etf"), "name": name}
    process, history, _, _, _ = make_reader({name: [row]})
    process(call("get_market_data", "named-fund", codes=[name]))
    assert history == [("search_symbol", name), ("get_market_data", ["590987.SH"])]


@pytest.mark.parametrize("query,candidates", [
    ("SWAP.US", [candidate("SWAP.TO", "ca", "equity")]),
    ("SHARED", [candidate("SHARED.US", "us", "equity"), candidate("SHARED.TO", "ca", "equity")]),
    ("UNSEEN", [candidate("UNRELATED.US", "us", "equity")]),
    ("MISSING.US", []),
])
def test_unresolved_or_wrong_venue_never_reaches_market_provider(make_reader, query, candidates):
    process, history, messages, ledger, exchange = make_reader({query: candidates})
    process(call("get_market_data", "withheld", codes=[query]))
    assert history == [("search_symbol", query)]
    assert not ledger.authorized_symbols
    assert any(m.get("tool_call_id") == "withheld" for m in messages)
    clarification = ledger.market_data_limitation()
    assert query in clarification
    if candidates:
        assert all(c["symbol"] in clarification for c in candidates)
    else:
        assert "当前数据源未找到" in clarification
    exchange.assert_not_called()


def test_same_model_batch_cannot_reuse_its_own_resolver_result(make_reader):
    symbol = "609873.SH"
    process, history, messages, _, _ = make_reader({symbol: [candidate(symbol, "cn", "equity")]})
    process(call("search_symbol", "resolve", query=symbol), call("get_market_data", "early", codes=[symbol]))
    assert history == [("search_symbol", symbol)]
    assert any(m.get("tool_call_id") == "early" for m in messages)
    process(call("get_market_data", "later", codes=[symbol]))
    assert history == [("search_symbol", symbol), ("get_market_data", [symbol])]


def test_trade_request_never_acquires_market_preflight_authority(make_reader):
    symbol = "OBSX.US"
    process, history, _, ledger, exchange = make_reader({symbol: [candidate(symbol, "us", "etf")]})
    process(call("get_market_data", "read-next-to-write", codes=[symbol]),
            call("place_order", "no-order", symbol=symbol))
    assert history == []
    assert not ledger.authorized_symbols
    exchange.assert_not_called()
