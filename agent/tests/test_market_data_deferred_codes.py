"""Mixed read-only quote requests use only identities known before the batch."""

import json
from types import SimpleNamespace

import pytest

from src.agent.context import ContextBuilder
from src.agent.grounding import GroundingLedger
from src.agent.loop import AgentLoop
from src.agent.tools import BaseTool, ToolRegistry
from src.agent.trace import TraceWriter
from src.providers.chat import ToolCallRequest
from src.tools.symbol_search_tool import SymbolSearchTool


class Quotes(BaseTool):
    market_identity_argument = "codes"
    repeatable = True
    cache_ttl = 300

    def __init__(self):
        self.name = "get_market_data"
        self.calls = []

    def execute(self, **kwargs):
        codes = kwargs["codes"]
        self.calls.append(codes)
        return json.dumps({code: [{"trade_date": "2026-11-23", "close": 100}] for code in codes})


@pytest.fixture
def run(tmp_path):
    quotes = Quotes()
    registry = ToolRegistry()
    registry.register(quotes)
    resolver = SymbolSearchTool()
    catalog_lookup = resolver.execute
    def lookup(query, **kwargs):
        if query == "^NDX":
            return json.dumps({"ok": True, "data": {"query": query, "candidates": [], "sources": {"fixture": "ok"}}})
        return catalog_lookup(query=query, **kwargs)
    resolver.execute = lookup
    registry.register(resolver)
    loop = AgentLoop(registry=registry, llm=SimpleNamespace())
    loop.memory.run_dir = str(tmp_path)
    loop._grounding = GroundingLedger(run_dir=tmp_path, user_message="昨天纳指涨幅如何？")
    trace = TraceWriter(tmp_path)
    messages = []

    def batch(*calls):
        loop._process_tool_calls(list(calls), ContextBuilder, messages, trace, [], 1)
        return json.loads(messages[-1]["content"])

    yield loop, quotes, batch, tmp_path
    trace.close()


def resolve(call_id, name):
    return ToolCallRequest(call_id, "search_symbol", {"query": name})


def read(call_id, *codes):
    return ToolCallRequest(call_id, "get_market_data", {"codes": list(codes)})


def test_mixed_read_does_not_taint_evidence_or_cache(run):
    loop, quotes, batch, root = run
    batch(resolve("resolve-ixic", "Nasdaq Composite Index"))
    original = read("first", "^IXIC", "^NDX")
    result = batch(original)
    assert result["_deferred_codes"] == ["^NDX"] and "^IXIC" in result
    assert original.arguments["codes"] == ["^IXIC", "^NDX"]
    assert quotes.calls == [["^IXIC"]]
    assert loop._grounding.authorized_symbols == {"^IXIC"}
    assert "^NDX" not in loop._grounding._session_symbols
    assert not any(r.symbol == "^NDX" for r in loop._grounding._evidence)
    entries = TraceWriter.read(root)
    event = next(r for r in entries if r["type"] == "market_data_scope")
    assert event["requested"] == original.arguments["codes"]
    assert event["eligible"] == ["^IXIC"] and event["deferred"] == ["^NDX"]
    call = next(r for r in entries if r["type"] == "tool_call" and r["call_id"] == "first")
    assert call["args"]["codes"] == ["^IXIC"]

    cached_mixed = batch(read("second", "^IXIC", "^NDX"))
    assert cached_mixed["_deferred_codes"] == ["^NDX"]
    assert "_deferred_codes" not in batch(read("third", "^IXIC"))
    assert quotes.calls == [["^IXIC"]]
    batch(resolve("resolve-ndx", "Nasdaq 100"))
    full = batch(read("fourth", "^IXIC", "^NDX"))
    assert "^IXIC" in full and "^NDX" in full and "_deferred_codes" not in full
    assert quotes.calls == [["^IXIC"], ["^IXIC", "^NDX"]]


def test_same_batch_lookup_cannot_authorize_a_new_read(run):
    _, quotes, batch, _ = run
    result = batch(resolve("resolve", "纳指"), read("quote", "^IXIC", "^NDX"))
    assert result["error_code"] == "identity_required"
    assert quotes.calls == []


def test_single_unlocked_code_remains_blocked(run):
    _, quotes, batch, _ = run
    batch(resolve("resolve", "纳指"))
    assert batch(read("ndx", "^NDX"))["error_code"] == "identity_mismatch"
    assert quotes.calls == []


def test_same_batch_comparator_is_deferred_even_if_resolution_finishes_first(run):
    _, quotes, batch, _ = run
    batch(resolve("main", "纳指"))
    result = batch(resolve("compare", "Nasdaq 100"), read("mixed", "^IXIC", "^NDX"))
    assert result["_deferred_codes"] == ["^NDX"]
    assert quotes.calls == [["^IXIC"]]
