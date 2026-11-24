"""Only a local identity gate changes the presentation of a failed tool step."""

import json
import pytest

from src.agent.context import ContextBuilder
from src.agent.trace import TraceWriter
from src.session.service import SessionService
from tests.test_agent_grounding import _build_direct_agent, _resolver_payload, _tool_call


@pytest.mark.parametrize("fault,expected", [(False, "no_data"), (True, "error")])
def test_empty_market_read_cannot_become_a_success_or_prevent_retry(tmp_path, fault, expected):
    agent, _, market, _, trace = _build_direct_agent(tmp_path, _resolver_payload())
    history = []
    agent._event_callback = lambda kind, data: (
        SessionService._record_tool_trail_event(history, kind, data)
        if kind in {"tool_call", "tool_result"} else None
    )
    messages = []
    market.cache_ttl = 300
    agent._process_tool_calls([_tool_call("name", "search_symbol", query="机器人ETF")],
                              ContextBuilder, messages, trace, [], 1)
    market.result = json.dumps({"_unresolved": ["562500.SH"], **({"_source_error": True} if fault else {})})
    for call_id in ("missing", "retry"):
        agent._process_tool_calls([_tool_call(call_id, "get_market_data", codes=["562500.SH"])],
                                  ContextBuilder, messages, trace, [], 2)
        assert json.loads(messages[-1]["content"])["ok"] is False
    trace.close()
    assert market.calls == 2
    assert [row.get("outcome") for row in history if row["tool"] == "get_market_data"] == [expected, expected]
    assert not agent._grounding._evidence


def test_partial_market_data_keeps_bars_and_missing_symbols():
    from src.agent.loop import _market_result_for_agent

    raw = '{"REAL.US":[{"close":20}],"_unresolved":["OTHER.US"]}'
    assert _market_result_for_agent(raw) == raw


def test_preflight_and_source_errors_remain_distinguishable_in_events_and_history(tmp_path):
    agent, _, market, _, trace = _build_direct_agent(tmp_path, _resolver_payload())
    events = []
    history = []

    def emit(kind, data):
        if kind in {"tool_call", "tool_result"}:
            events.append((kind, data))
            SessionService._record_tool_trail_event(history, kind, data)

    agent._event_callback = emit
    messages = []
    agent._process_tool_calls([_tool_call("blocked", "get_market_data", codes=["562500.SH"])],
                              ContextBuilder, messages, trace, [], 1)
    assert market.calls == 0
    gate_payload = messages[-1]["content"]
    assert json.loads(gate_payload)["status"] == "error"
    agent._process_tool_calls([_tool_call("resolve", "search_symbol", query="机器人ETF")],
                              ContextBuilder, messages, trace, [], 2)
    # Deliberately return the exact same error envelope from the data source.
    # Only the local preflight call earns the special display outcome.
    market.result = gate_payload
    agent._process_tool_calls([_tool_call("source", "get_market_data", codes=["562500.SH"])],
                              ContextBuilder, messages, trace, [], 3)
    trace.close()
    assert market.calls == 1
    for records in ([data for kind, data in events if kind == "tool_result"], history):
        outcomes = {row["call_id"]: (row["status"], row.get("outcome")) for row in records}
        assert outcomes == {"blocked": ("error", "needs_resolution"),
                            "resolve": ("ok", None), "source": ("error", "error")}
    calls = {r["call_id"]: r for r in TraceWriter.read(tmp_path / "run") if r["type"] == "tool_call"}
    assert calls["blocked"]["blocked"]
    assert not calls["source"].get("blocked")
    assert not any(row.tool == "get_market_data" for row in agent._grounding._evidence)
