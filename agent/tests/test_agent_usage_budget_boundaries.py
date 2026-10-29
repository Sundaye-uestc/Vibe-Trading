"""Reported costs stop further work without discarding a finished answer."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.agent.trace import TraceWriter
from src.goal import AuditRow, GoalStatus, GoalStore
from src.providers.chat import LLMResponse, ProviderStreamError, ToolCallRequest
from tests.test_agent_goal_context import _agent


def usage(count):
    return {"input_tokens": count - 1, "output_tokens": 1, "total_tokens": count}


def failed_request(tokens=None):
    return ProviderStreamError(provider="fixture", model="fixture", original=ConnectionResetError("reset"),
                               usage_metadata=usage(tokens) if tokens else None)


@pytest.fixture
def setup_case(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_TRADING_GOAL_DB_PATH", str(tmp_path / "goals.db"))
    monkeypatch.setattr("src.agent.loop.STREAM_RETRY_DELAY_S", 0)
    monkeypatch.setattr("src.agent.loop.STALL_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr("src.agent.loop.GOAL_MAX_CONTINUATIONS", 0)

    def create(replies, budget=None, summary=None):
        store = GoalStore()
        if budget:
            store.replace_goal(session_id="test-session", objective="A plain greeting", criteria=["reply"], token_budget=budget)
        queue = iter(replies)
        llm = SimpleNamespace(model_name="fixture", calls=0, summary_calls=0)

        def stream(*args, **kwargs):
            llm.calls += 1
            response = next(queue)
            if isinstance(response, Exception):
                raise response
            return response() if callable(response) else response

        def compact(*args, **kwargs):
            llm.summary_calls += 1
            return summary

        llm.stream_chat = stream
        llm.chat = compact
        agent = _agent(llm, tmp_path / "run")
        events = []
        agent._event_callback = lambda kind, data: events.append((kind, data))
        return SimpleNamespace(agent=agent, llm=llm, store=store, events=events, run_dir=tmp_path / "run")

    return create


def test_budget_crossed_by_complete_answer_keeps_the_answer(setup_case):
    case = setup_case([LLMResponse(content="Finished answer.", usage_metadata=usage(15))], budget=10)
    result = case.agent.run("hello", session_id="test-session")
    assert result["status"] == "success" and result["content"] == "Finished answer."
    assert case.llm.calls == 1
    assert case.store.get_current_snapshot("test-session")["goal"]["tokens_used"] == 15


def test_budget_crossed_by_tool_request_never_executes_it(setup_case, monkeypatch):
    case = setup_case([LLMResponse(tool_calls=[ToolCallRequest("call", "probe", {})], usage_metadata=usage(15))], budget=10)
    executed = []
    monkeypatch.setattr(case.agent, "_process_tool_calls", lambda *a, **k: executed.append(True))
    result = case.agent.run("hello", session_id="test-session")
    assert result["status"] == "failed" and "goal_token_budget_exhausted" in result["reason"]
    assert not executed and case.llm.calls == 1


def test_already_exhausted_goal_makes_no_provider_call(setup_case):
    case = setup_case([], budget=10)
    goal = case.store.get_current_snapshot("test-session")["goal"]
    case.store.account_usage(session_id="test-session", goal_id=goal["goal_id"], expected_goal_id=goal["goal_id"], token_delta=10)
    result = case.agent.run("hello", session_id="test-session")
    assert case.llm.calls == 0 and "goal_token_budget_exhausted" in result["reason"]


def test_failed_attempt_and_success_are_each_accounted_once(setup_case):
    case = setup_case([failed_request(7), LLMResponse(content="Finished answer.", usage_metadata=usage(11))], budget=100)
    result = case.agent.run("hello", session_id="test-session")
    summary = json.loads((case.run_dir / "llm_usage.json").read_text())
    assert result["status"] == "success"
    assert summary["totals"]["total_tokens"] == 18 and summary["totals"]["calls"] == 2
    assert summary["per_iteration"][0]["purpose"] == "failed_stream"
    goal = case.store.get_current_snapshot("test-session")["goal"]
    assert goal["tokens_used"] == 18 and goal["turns_used"] == 1


def test_failed_request_reaching_budget_blocks_its_retry(setup_case):
    case = setup_case([failed_request(12)], budget=10)
    result = case.agent.run("hello", session_id="test-session")
    assert case.llm.calls == 1 and "goal_token_budget_exhausted" in result["reason"]


def test_unreported_failure_does_not_emit_live_missing_bill_signal(setup_case):
    case = setup_case([failed_request(), LLMResponse(content="done", usage_metadata=usage(5))])
    prior = case.agent._event_callback
    # A spending monitor can treat this signal as a fail-closed halt.
    case.agent._event_callback = lambda kind, data: (prior(kind, data), case.agent.cancel() if kind == "llm_usage_unavailable" else None)
    result = case.agent.run("hello")
    assert result["status"] == "success"
    assert not any(kind == "llm_usage_unavailable" for kind, _ in case.events)
    summary = json.loads((case.run_dir / "llm_usage.json").read_text())
    assert summary["totals"]["unreported_calls"] == 1


def test_cancelled_response_usage_is_kept_but_tools_are_not_executed(setup_case, monkeypatch):
    case = None

    def cancel_and_return():
        case.agent.cancel()
        return LLMResponse(tool_calls=[ToolCallRequest("call", "probe", {})], usage_metadata=usage(9))

    case = setup_case([cancel_and_return])
    executed = []
    monkeypatch.setattr(case.agent, "_process_tool_calls", lambda *a, **k: executed.append(True))
    result = case.agent.run("hello")
    assert result["status"] == "cancelled" and not executed
    assert json.loads((case.run_dir / "llm_usage.json").read_text())["totals"]["total_tokens"] == 9


def test_compaction_usage_can_exhaust_goal_before_main_model_call(setup_case, monkeypatch):
    monkeypatch.setattr("src.agent.loop.TOKEN_THRESHOLD", 1)
    case = setup_case([], budget=10, summary=LLMResponse(content="summary", usage_metadata=usage(12)))
    result = case.agent.run("hello", history=[{"role": "user", "content": "old"}, {"role": "assistant", "content": "old answer"}], session_id="test-session")
    assert case.llm.summary_calls == 1 and case.llm.calls == 0
    assert "goal_token_budget_exhausted" in result["reason"]
    summary = json.loads((case.run_dir / "llm_usage.json").read_text())
    assert summary["totals"]["total_tokens"] == 12
    assert summary["per_iteration"][0]["purpose"] == "compaction"


def test_unreported_compaction_is_recorded_without_halting(setup_case):
    case = setup_case([], summary=LLMResponse(content="summary"))
    trace = TraceWriter(case.run_dir / "trace")
    try:
        case.agent._auto_compact([
            {"role": "system", "content": "system"}, {"role": "user", "content": "older"},
            {"role": "assistant", "content": "older response"}, {"role": "user", "content": "newer"},
        ], case.run_dir, trace)
    finally:
        trace.close()
    assert not any(kind == "llm_usage_unavailable" for kind, _ in case.events)
    assert json.loads((case.run_dir / "llm_usage.json").read_text())["totals"]["unreported_calls"] == 1


@pytest.mark.parametrize("terminal", [GoalStatus.COMPLETE, GoalStatus.CANCELLED])
def test_finished_goal_does_not_block_an_ordinary_followup(setup_case, terminal):
    case = setup_case([LLMResponse(content="A fresh answer.", usage_metadata=usage(8))], budget=5)
    goal = case.store.get_current_snapshot("test-session")["goal"]
    goal_id = goal["goal_id"]
    case.store.account_usage(session_id="test-session", goal_id=goal_id, expected_goal_id=goal_id, token_delta=5)
    criterion = case.store.list_criteria(goal_id)[0]
    audit = [AuditRow(criterion_id=criterion.criterion_id, result="not_applicable_user_accepted",
                      evidence_ids=[], notes="User accepted fixture closure")]
    case.store.update_status(session_id="test-session", goal_id=goal_id, expected_goal_id=goal_id, status=terminal, audit=audit)
    result = case.agent.run("new conversation", session_id="test-session")
    assert result["status"] == "success" and case.llm.calls == 1
    assert case.store.get_goal(goal_id).tokens_used == 5
