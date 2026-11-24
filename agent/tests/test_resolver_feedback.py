"""The model sees the resolver decision that will govern the next tool turn."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agent.context import ContextBuilder
from src.agent.trace import TraceWriter
from src.tools.read_file_tool import ReadFileTool
from tests.test_agent_grounding import _build_direct_agent, _resolver_payload, _tool_call


def turn(agent, trace, *calls):
    messages = []
    agent._process_tool_calls(list(calls), ContextBuilder, messages, trace, [], 1)
    return [json.loads(msg["content"]) for msg in messages]


def test_cached_search_keeps_the_lock_and_the_original_observation(tmp_path):
    agent, resolver, market, _, trace = _build_direct_agent(tmp_path, _resolver_payload())
    resolver.cache_ttl = 300
    for call_id in ("original", "repeat"):
        view, = turn(agent, trace, _tool_call(call_id, "search_symbol", query="机器人ETF"))
        assert view["_resolution"] == {
            "query_status": "locked", "resolved_symbol": "562500.SH",
            "run_status": "locked", "available_for_next_call": ["562500.SH"],
        }
        assert agent._grounding.resolution_for_query("机器人ETF").source_tool_call_id == "original"
    turn(agent, trace, _tool_call("prices", "get_market_data", codes=["562500.SH"]))
    trace.close()
    assert resolver.calls == market.calls == 1
    entries = TraceWriter.read(tmp_path / "run")
    raw = next(row["result"] for row in entries if row["type"] == "tool_result" and row["call_id"] == "original")
    assert "_resolution" not in json.loads(raw)
    assert any(row["type"] == "tool_result_cached" and row["call_id"] == "repeat" for row in entries)


def test_failed_later_search_prevents_old_cached_candidates_from_restoring_a_lock(tmp_path):
    original = _resolver_payload()
    agent, resolver, market, _, trace = _build_direct_agent(tmp_path, original)
    resolver.cache_ttl = 300
    turn(agent, trace, _tool_call("ok", "search_symbol", query="机器人ETF", limit=1))
    resolver.result = json.dumps({"ok": False, "error": "resolver unavailable"})
    turn(agent, trace, _tool_call("bad", "search_symbol", query="机器人ETF", limit=2))
    failed, = turn(agent, trace, _tool_call("repeat", "search_symbol", query="机器人ETF", limit=1))
    assert failed["_resolution"]["query_status"] == "invalidated"
    assert failed["_resolution"]["available_for_next_call"] == []
    turn(agent, trace, _tool_call("prices", "get_market_data", codes=["562500.SH"]))
    trace.close()
    assert resolver.calls == 3
    assert market.calls == 0


@pytest.mark.parametrize("conflicting", [False, True])
def test_parallel_search_results_share_the_final_batch_verdict(tmp_path, conflicting):
    agent, resolver, _, _, trace = _build_direct_agent(tmp_path, "")
    payloads = {"primary": _resolver_payload(query="primary")}
    other_query = "^NDX" if conflicting else "other index"
    candidates = ([{"symbol": "STALE.US", "name": "Other"}] if conflicting else
                  [{"symbol": "^NDX", "name": "NASDAQ 100"}, {"symbol": "^IXIC", "name": "NASDAQ Composite"}])
    payloads[other_query] = _resolver_payload(query=other_query, candidates=candidates)
    resolver.execute = lambda query, **_: payloads[query]
    primary, other = turn(agent, trace, _tool_call("one", "search_symbol", query="primary"),
                          _tool_call("two", "search_symbol", query=other_query))
    trace.close()
    assert primary["_resolution"]["query_status"] == "locked"
    assert other["_resolution"]["query_status"] == ("conflicting" if conflicting else "ambiguous")
    assert other["_resolution"]["resolved_symbol"] is None
    allowed = [] if conflicting else ["562500.SH"]
    assert all(view["_resolution"]["available_for_next_call"] == allowed for view in (primary, other))


def test_offloaded_resolver_receipt_carries_the_verdict_but_original_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    payload = json.loads(_resolver_payload())
    payload["_diagnostics"] = "x" * 50000
    raw = json.dumps(payload, ensure_ascii=False)
    agent, _, _, _, trace = _build_direct_agent(tmp_path, raw)
    agent.registry.register(ReadFileTool())
    view, = turn(agent, trace, _tool_call("resolve", "search_symbol", query="机器人ETF"))
    trace.close()
    assert view["_resolution"]["resolved_symbol"] == "562500.SH"
    assert json.loads(Path(view["_result_file"]["path"]).read_text()) == payload
