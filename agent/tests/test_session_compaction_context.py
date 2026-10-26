"""Long-session handoff and failed-research evidence survive compaction."""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from src.session.events import EventBus
from src.session.models import Attempt, Message
from src.session.service import SessionBusyError, SessionService
from src.session.store import SessionStore


@pytest.fixture
def context(tmp_path, monkeypatch):
    index = SimpleNamespace(
        indexed_session_ids=lambda: set(),
        index_session=lambda *args: None,
        index_message=lambda *args: None,
    )
    monkeypatch.setattr("src.session.service.get_shared_index", lambda: index)
    service = SessionService(SessionStore(tmp_path / "sessions"), EventBus(), tmp_path / "runs")
    session = service.create_session(title="compaction regression")
    prompts = []

    class FakeLLM:
        def __init__(self, **kwargs):
            pass

        def chat(self, messages, **kwargs):
            prompts.append(messages[0]["content"])
            return SimpleNamespace(content="Keep the user's research constraints.")

    monkeypatch.setattr("src.providers.chat.ChatLLM", FakeLLM)
    return service, session, prompts


def append_turns(service, session, start=0, count=8):
    for index in range(start, start + count):
        service.store.append_message(Message(
            session_id=session.session_id,
            role="user" if index % 2 == 0 else "assistant",
            content=f"TURN_{index:04d}",
        ))


def test_actual_attempt_keeps_summary_after_more_than_100_messages(context, monkeypatch):
    service, session, _ = context
    append_turns(service, session)
    asyncio.run(service.compact_session(session.session_id))
    append_turns(service, session, start=8, count=110)
    service.store.append_message(Message(session_id=session.session_id, role="user", content="CURRENT_PROMPT"))
    captured = []

    async def run(attempt, *, messages, **kwargs):
        captured.extend(service._convert_messages_to_history(messages))
        return {"status": "success", "content": "done"}

    monkeypatch.setattr(service, "_run_with_agent", run)
    attempt = Attempt(session_id=session.session_id, prompt="CURRENT_PROMPT")
    service.store.create_attempt(attempt)
    asyncio.run(service._run_attempt(session, attempt))

    assert "research constraints" in captured[0]["content"]
    assert captured[-1]["content"] == "TURN_0117"
    assert "CURRENT_PROMPT" not in str(captured)
    assert "TURN_0000" not in str(captured)
    # UI pagination remains unchanged; internal context loading is separate.
    assert len(service.get_messages(session.session_id)) == 100


def test_repeated_compaction_uses_latest_summary_and_only_unfolded_tail(context):
    service, session, prompts = context
    append_turns(service, session)
    asyncio.run(service.compact_session(session.session_id))
    append_turns(service, session, start=8, count=6)
    before = service.store._messages_file(session.session_id).read_text()
    asyncio.run(service.compact_session(session.session_id))

    assert len(prompts) == 2
    assert "research constraints" in prompts[1]
    assert all(f"TURN_{index:04d}" not in prompts[1] for index in range(4))
    assert all(f"TURN_{index:04d}" in prompts[1] for index in range(4, 10))
    assert all(f"TURN_{index:04d}" not in prompts[1] for index in range(10, 14))
    assert service.store._messages_file(session.session_id).read_text().startswith(before)
    service.store.append_message(Message(session_id=session.session_id, role="user", content="continue"))
    history = service._convert_messages_to_history(service.store.get_messages(session.session_id, limit=None))
    assert [entry["content"] for entry in history[1:]] == [f"TURN_{index:04d}" for index in range(10, 14)]


def test_compact_without_new_turns_does_not_resummarise_folded_originals(context):
    service, session, prompts = context
    append_turns(service, session)
    asyncio.run(service.compact_session(session.session_id))
    before = service.store._messages_file(session.session_id).read_text()
    assert asyncio.run(service.compact_session(session.session_id))["status"] == "noop"
    assert len(prompts) == 1
    assert service.store._messages_file(session.session_id).read_text() == before


def test_first_compaction_includes_older_than_500_messages(context):
    service, session, prompts = context
    append_turns(service, session, count=510)
    asyncio.run(service.compact_session(session.session_id))
    assert "TURN_0000" in "\n".join(prompts)
    assert "TURN_0505" in "\n".join(prompts)
    assert "TURN_0506" not in "\n".join(prompts)


def test_failure_evidence_survives_even_if_summary_omits_it(context):
    service, session, prompts = context
    service.store.append_message(Message(session_id=session.session_id, role="user", content="research AAPL"))
    service.store.append_message(Message(
        session_id=session.session_id, role="assistant", content="Execution failed",
        metadata={"status": "failed", "recovery_context": {
            "run_id": "run-evidence-42", "artifacts": ["artifacts/evidence.json"], "tool_count": 1,
        }},
        tool_trail=[{"tool": "get_market_data", "status": "ok", "preview": "AAPL close=195"}],
    ))
    append_turns(service, session, start=2, count=6)
    asyncio.run(service.compact_session(session.session_id))

    assert "run-evidence-42" in prompts[0]
    assert "AAPL close=195" in prompts[0]
    marker = service.store.get_messages(session.session_id)[-1]
    assert marker.metadata["recovery_evidence"][0]["artifacts"] == ["artifacts/evidence.json"]
    service.store.append_message(Message(session_id=session.session_id, role="user", content="continue"))
    history = service._convert_messages_to_history(service.store.get_messages(session.session_id, limit=None))
    assert "run-evidence-42" in history[0]["content"]
    assert "artifacts/evidence.json" in history[0]["content"]
    assert "AAPL close=195" in history[0]["content"]

    append_turns(service, session, start=8, count=6)
    asyncio.run(service.compact_session(session.session_id))
    latest = service.store.get_messages(session.session_id)[-1]
    assert latest.metadata["recovery_evidence"] == marker.metadata["recovery_evidence"]
    assert "run-evidence-42" in prompts[1]


def test_compacted_recovery_index_is_bounded(context):
    service, session, _ = context
    messages = [Message(
        session_id=session.session_id, role="assistant", content="failed",
        metadata={"status": "failed", "recovery_context": {"run_id": f"run-{index}", "artifacts": ["artifacts/a.json"]}},
        tool_trail=[{"tool": "read_file", "status": "ok", "preview": "x" * 5000} for _ in range(50)],
    ) for index in range(100)]
    evidence = service._collect_compacted_evidence(None, messages)
    assert len(evidence) <= 8
    assert len(json.dumps(evidence, ensure_ascii=False)) <= 12000
    assert evidence[-1]["run_id"] == "run-99"
    assert evidence[-1]["artifacts"] == ["artifacts/a.json"]


def test_legacy_marker_without_boundary_keeps_only_following_messages():
    messages = [
        {"role": "user", "content": "folded"},
        {"role": "system", "content": "legacy handoff", "metadata": {"type": "compaction"}},
        {"role": "assistant", "content": "keep"},
        {"role": "user", "content": "current"},
    ]
    history = SessionService._convert_messages_to_history(messages)
    assert "legacy handoff" in history[0]["content"]
    assert history[1]["content"] == "keep"
    assert "folded" not in str(history)


@pytest.mark.parametrize("failure", ["empty", "exception"])
def test_unsuccessful_summary_never_advances_boundary_or_leaves_busy(context, monkeypatch, failure):
    service, session, _ = context
    append_turns(service, session)
    asyncio.run(service.compact_session(session.session_id))
    append_turns(service, session, start=8, count=6)
    before = service.store._messages_file(session.session_id).read_text()

    class FailedLLM:
        def __init__(self, **kwargs):
            pass

        def chat(self, *args, **kwargs):
            if failure == "exception":
                raise RuntimeError("provider unavailable")
            return SimpleNamespace(content="")

    monkeypatch.setattr("src.providers.chat.ChatLLM", FailedLLM)
    if failure == "exception":
        with pytest.raises(RuntimeError, match="provider unavailable"):
            asyncio.run(service.compact_session(session.session_id))
    else:
        assert asyncio.run(service.compact_session(session.session_id))["status"] == "noop"
    assert session.session_id not in service._inflight
    assert service.store._messages_file(session.session_id).read_text() == before


def test_compaction_reserves_session_until_summary_finishes(context, monkeypatch):
    service, session, _ = context
    append_turns(service, session)
    entered = threading.Event()
    release = threading.Event()

    class SlowLLM:
        def __init__(self, **kwargs):
            pass

        def chat(self, *args, **kwargs):
            entered.set()
            assert release.wait(timeout=5)
            return SimpleNamespace(content="summary")

    monkeypatch.setattr("src.providers.chat.ChatLLM", SlowLLM)

    async def scenario():
        task = asyncio.create_task(service.compact_session(session.session_id))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            with pytest.raises(SessionBusyError):
                await service.send_message(session.session_id, "overlap")
            with pytest.raises(SessionBusyError):
                await service.compact_session(session.session_id)
        finally:
            release.set()
            await task
        assert session.session_id not in service._inflight

    asyncio.run(scenario())
    assert all(message.content != "overlap" for message in service.store.get_messages(session.session_id))
