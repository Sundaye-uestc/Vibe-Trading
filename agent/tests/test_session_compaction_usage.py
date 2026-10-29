"""Manual compression obeys the same goal budget as automatic compression."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.goal import AuditRow, GoalStatus, GoalStore
from src.providers.chat import ProviderStreamError
from tests import test_session_compaction_context as shared

context = shared.context
append_turns = shared.append_turns


@pytest.fixture
def goal_store(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_TRADING_GOAL_DB_PATH", str(tmp_path / "compact-goals.db"))
    return GoalStore()


def _summary_responses(monkeypatch, responses):
    calls = []
    queued = iter(responses)

    class Summarizer:
        def __init__(self, **kwargs):
            pass

        def chat(self, messages):
            calls.append(messages)
            response = next(queued)
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr("src.providers.chat.ChatLLM", Summarizer)
    return calls


def test_exhausted_goal_rejects_manual_compaction_without_a_call(context, goal_store):
    service, session, prompts = context
    append_turns(service, session)
    goal = goal_store.replace_goal(session_id=session.session_id, objective="research", criteria=["reply"], token_budget=5)
    goal_store.account_usage(session_id=session.session_id, goal_id=goal.goal_id, expected_goal_id=goal.goal_id, token_delta=5)
    before = service.store._messages_file(session.session_id).read_text()
    result = asyncio.run(service.compact_session(session.session_id))
    assert result["status"] == "noop" and result["reason"] == "goal_token_budget_exhausted"
    assert not prompts
    assert service.store._messages_file(session.session_id).read_text() == before
    assert session.session_id not in service._inflight


@pytest.mark.parametrize("chunks,expected_status", [(["part one", "part two"], "noop"), (["only part"], "compacted")])
def test_compaction_crossing_budget_preserves_only_complete_handoff(context, goal_store, monkeypatch, chunks, expected_status):
    service, session, _ = context
    append_turns(service, session)
    goal = goal_store.replace_goal(session_id=session.session_id, objective="research", criteria=["reply"], token_budget=5)
    calls = _summary_responses(monkeypatch, [SimpleNamespace(content="summary", usage_metadata={"total_tokens": 7})])
    monkeypatch.setattr("src.agent.loop._summary_chunks", lambda messages: chunks)
    events = []
    monkeypatch.setattr(service.event_bus, "emit", lambda sid, kind, data: events.append((kind, data)))
    before = service.store._messages_file(session.session_id).read_text()
    result = asyncio.run(service.compact_session(session.session_id))
    assert len(calls) == 1 and result["status"] == expected_status
    assert result["usage_metadata"]["total_tokens"] == 7
    updated = goal_store.get_goal(goal.goal_id)
    assert updated.tokens_used == 7 and updated.turns_used == 0
    assert any(kind == "goal.updated" for kind, _ in events)
    if expected_status == "noop":
        assert result["reason"] == "goal_token_budget_exhausted"
        assert service.store._messages_file(session.session_id).read_text() == before
    else:
        marker = service.store.get_messages(session.session_id)[-1]
        assert marker.metadata["type"] == "compaction"
        assert marker.content == "summary" and marker.metadata["usage_metadata"]["total_tokens"] == 7


def test_manual_compaction_without_goal_records_actual_and_unreported_usage(context, goal_store, monkeypatch):
    service, session, _ = context
    append_turns(service, session)
    _summary_responses(monkeypatch, [
        SimpleNamespace(content="first", usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}),
        SimpleNamespace(content="full summary", usage_metadata=None),
    ])
    monkeypatch.setattr("src.agent.loop._summary_chunks", lambda messages: ["part one", "part two"])
    result = asyncio.run(service.compact_session(session.session_id))
    assert result["status"] == "compacted"
    assert result["usage_metadata"] == {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                                        "calls": 1, "unreported_calls": 1}
    marker = service.store.get_messages(session.session_id)[-1]
    assert marker.metadata["usage_metadata"] == result["usage_metadata"]
    assert goal_store.get_current_snapshot(session.session_id) is None


def test_failed_summary_accounts_reported_usage_without_advancing_boundary(context, goal_store, monkeypatch):
    service, session, _ = context
    append_turns(service, session)
    goal = goal_store.replace_goal(session_id=session.session_id, objective="research", criteria=["reply"], token_budget=100)
    _summary_responses(monkeypatch, [ProviderStreamError(provider="fixture", model="fixture",
        original=ConnectionResetError("reset"), usage_metadata={"total_tokens": 9})])
    before = service.store._messages_file(session.session_id).read_text()
    with pytest.raises(ProviderStreamError):
        asyncio.run(service.compact_session(session.session_id))
    updated = goal_store.get_goal(goal.goal_id)
    assert updated.tokens_used == 9 and updated.turns_used == 0
    assert service.store._messages_file(session.session_id).read_text() == before
    assert session.session_id not in service._inflight


@pytest.mark.parametrize("terminal", [GoalStatus.COMPLETE, GoalStatus.CANCELLED])
def test_finished_goal_does_not_block_manual_compaction(context, goal_store, terminal):
    service, session, prompts = context
    append_turns(service, session)
    goal = goal_store.replace_goal(session_id=session.session_id, objective="research", criteria=["reply"], token_budget=5)
    goal_store.account_usage(session_id=session.session_id, goal_id=goal.goal_id, expected_goal_id=goal.goal_id, token_delta=5)
    criterion = goal_store.list_criteria(goal.goal_id)[0]
    audit = [AuditRow(criterion_id=criterion.criterion_id, result="not_applicable_user_accepted",
                      evidence_ids=[], notes="User accepted fixture closure")]
    goal_store.update_status(session_id=session.session_id, goal_id=goal.goal_id, expected_goal_id=goal.goal_id, status=terminal, audit=audit)
    assert goal_store.get_current_snapshot(session.session_id) is None
    result = asyncio.run(service.compact_session(session.session_id))
    assert result["status"] == "compacted" and len(prompts) == 1
    assert goal_store.get_goal(goal.goal_id).tokens_used == 5
