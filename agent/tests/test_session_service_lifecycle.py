"""Session lifecycle invariants: one run per session, honest terminal states."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pytest

from src.session.events import EventBus
from src.session.models import Attempt, AttemptStatus
from src.session.service import SessionBusyError, SessionService
from src.session.store import SessionStore


class _DummyIndex:
    def index_session(self, session_id: str, title: str) -> None:
        del session_id, title

    def index_message(self, session_id: str, role: str, content: str) -> None:
        del session_id, role, content


class _RecordingIndex:
    """Records index mutations so the service's index sync can be asserted."""

    def __init__(self) -> None:
        self.sessions: dict[str, str] = {}
        self.removed: list[str] = []

    def index_session(self, session_id: str, title: str) -> None:
        self.sessions[session_id] = title

    def index_message(self, session_id: str, role: str, content: str) -> None:
        del session_id, role, content

    def remove_session(self, session_id: str) -> int:
        self.removed.append(session_id)
        self.sessions.pop(session_id, None)
        return 0

    def indexed_session_ids(self) -> set[str]:
        return set(self.sessions)


def _service_with_index(
    tmp_path: Path, monkeypatch
) -> tuple[SessionService, _RecordingIndex]:
    index = _RecordingIndex()
    monkeypatch.setattr("src.session.service.get_shared_index", lambda: index)
    service = SessionService(
        store=SessionStore(tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )
    return service, index


# ---------------------------------------------------------------------------
# Search-index sync
#
# The index is what cross-session search reads, so a delete or rename that only
# touches the store leaves search showing sessions that are gone, or titles that
# no longer exist.
# ---------------------------------------------------------------------------


class TestSearchIndexSync:
    def test_rename_refreshes_the_indexed_title(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        service, index = _service_with_index(tmp_path, monkeypatch)
        session = service.create_session(title="Old title")
        assert index.sessions[session.session_id] == "Old title"

        updated = service.set_session_title(session.session_id, "New title")

        assert updated is not None
        assert index.sessions[session.session_id] == "New title"
        assert service.get_session(session.session_id).title == "New title"

    def test_rename_of_a_missing_session_returns_none(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        service, index = _service_with_index(tmp_path, monkeypatch)

        assert service.set_session_title("missing", "x") is None
        assert index.sessions == {}

    def test_delete_removes_the_session_from_the_index(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        service, index = _service_with_index(tmp_path, monkeypatch)
        session = service.create_session(title="Doomed")

        assert service.delete_session(session.session_id) is True

        assert index.removed == [session.session_id]
        assert session.session_id not in index.sessions

    def test_failed_delete_leaves_the_index_alone(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        service, index = _service_with_index(tmp_path, monkeypatch)
        kept = service.create_session(title="Kept")

        assert service.delete_session("missing") is False

        assert index.removed == []
        assert kept.session_id in index.sessions


# ---------------------------------------------------------------------------
# Startup index consistency check
# ---------------------------------------------------------------------------


class TestStartupIndexConsistencyCheck:
    def test_warns_about_a_session_the_index_has_but_disk_does_not(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        index = _RecordingIndex()
        index.sessions["ghost"] = "Gone"
        monkeypatch.setattr("src.session.service.get_shared_index", lambda: index)

        with caplog.at_level(logging.WARNING, logger="src.session.service"):
            SessionService(
                store=SessionStore(tmp_path / "sessions"),
                event_bus=EventBus(),
                runs_dir=tmp_path / "runs",
            )

        assert "ghost" in caplog.text

    def test_stays_quiet_when_every_indexed_session_is_on_disk(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        store = SessionStore(tmp_path / "sessions")
        service, _ = _service_with_index(tmp_path, monkeypatch)
        service.create_session(title="Present")

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="src.session.service"):
            SessionService(
                store=store,
                event_bus=EventBus(),
                runs_dir=tmp_path / "runs",
            )

        assert caplog.text == ""


def _service(tmp_path: Path, monkeypatch) -> SessionService:
    monkeypatch.setattr("src.session.service.get_shared_index", lambda: _DummyIndex())
    return SessionService(
        store=SessionStore(tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )


def _stub_agent(service: SessionService, monkeypatch, result: dict, *, gate=None):
    """Replace _run_with_agent with a stub returning ``result``.

    Args:
        service: Service under test.
        monkeypatch: pytest monkeypatch fixture.
        result: Result dict the fake agent returns.
        gate: Optional asyncio.Event the fake agent waits on before returning,
            used to hold a run in flight while a second send is attempted.
    """

    async def _fake(attempt, messages=None, **kwargs):
        del attempt, messages, kwargs
        if gate is not None:
            await gate.wait()
        return dict(result)

    monkeypatch.setattr(service, "_run_with_agent", _fake)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_second_send_is_refused_while_the_first_run_is_in_flight(tmp_path, monkeypatch):
    """The claim is taken synchronously, so no second attempt is ever created."""

    async def scenario() -> None:
        service = _service(tmp_path, monkeypatch)
        session = service.create_session(title="busy")
        gate = asyncio.Event()
        _stub_agent(service, monkeypatch, {"status": "success", "content": "ok"}, gate=gate)

        first = await service.send_message(session.session_id, "one")
        assert first["attempt_id"]

        with pytest.raises(SessionBusyError):
            await service.send_message(session.session_id, "two")

        # The refused send must not have persisted a message or an attempt.
        assert [m.content for m in service.store.get_messages(session.session_id)] == ["one"]
        stored = service.store.get_session(session.session_id)
        assert stored.last_attempt_id == first["attempt_id"]

        gate.set()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if session.session_id not in service._inflight:
                break
        assert session.session_id not in service._inflight

    asyncio.run(scenario())


def test_claim_is_released_after_the_run_finishes(tmp_path, monkeypatch):
    """A sequential second send succeeds once the first run reaches a terminal state."""

    async def scenario() -> None:
        service = _service(tmp_path, monkeypatch)
        session = service.create_session(title="sequential")
        _stub_agent(service, monkeypatch, {"status": "success", "content": "ok"})

        await service.send_message(session.session_id, "one")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if session.session_id not in service._inflight:
                break
        assert session.session_id not in service._inflight

        second = await service.send_message(session.session_id, "two")
        assert second["attempt_id"]

    asyncio.run(scenario())


def test_claim_is_released_when_the_agent_raises(tmp_path, monkeypatch):
    """An exception inside the run must not strand the session as busy."""

    async def scenario() -> None:
        service = _service(tmp_path, monkeypatch)
        session = service.create_session(title="boom")

        async def _explode(attempt, messages=None, **kwargs):
            del attempt, messages, kwargs
            raise RuntimeError("agent exploded")

        monkeypatch.setattr(service, "_run_with_agent", _explode)

        await service.send_message(session.session_id, "one")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if session.session_id not in service._inflight:
                break
        assert session.session_id not in service._inflight
        stored = service.store.get_session(session.session_id)
        attempt = service.store.get_attempt(session.session_id, stored.last_attempt_id)
        assert attempt.status == AttemptStatus.FAILED

    asyncio.run(scenario())


def test_non_user_roles_never_claim_the_session(tmp_path, monkeypatch):
    """System/assistant messages create no attempt, so they must not block sends."""

    async def scenario() -> None:
        service = _service(tmp_path, monkeypatch)
        session = service.create_session(title="notes")
        _stub_agent(service, monkeypatch, {"status": "success", "content": "ok"})

        await service.send_message(session.session_id, "note", role="system")
        assert session.session_id not in service._inflight
        assert await service.send_message(session.session_id, "real")

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Terminal states
# ---------------------------------------------------------------------------


def test_cancelled_run_is_cancelled_not_failed(tmp_path, monkeypatch):
    """A cooperative cancel gets its own status, event and reply text."""

    async def scenario() -> None:
        service = _service(tmp_path, monkeypatch)
        session = service.create_session(title="cancel")
        seen: list[str] = []
        service.event_bus.emit = lambda sid, event, data: seen.append(event)  # type: ignore[assignment]
        _stub_agent(
            service,
            monkeypatch,
            {"status": "cancelled", "reason": "cancelled by user"},
        )

        await service.send_message(session.session_id, "go")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if session.session_id not in service._inflight:
                break

        stored = service.store.get_session(session.session_id)
        attempt = service.store.get_attempt(session.session_id, stored.last_attempt_id)
        assert attempt.status == AttemptStatus.CANCELLED
        assert "attempt.cancelled" in seen
        assert "attempt.failed" not in seen
        reply = service.store.get_messages(session.session_id)[-1]
        assert reply.content == "Run cancelled."

    asyncio.run(scenario())


def test_metrics_reach_the_attempt_and_the_reply(tmp_path, monkeypatch):
    """Loaded metrics were dropped on the floor before reaching the reply."""

    async def scenario() -> None:
        service = _service(tmp_path, monkeypatch)
        session = service.create_session(title="metrics")
        _stub_agent(
            service,
            monkeypatch,
            {"status": "success", "content": "done", "metrics": {"sharpe": 1.25}},
        )

        await service.send_message(session.session_id, "backtest")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if session.session_id not in service._inflight:
                break

        stored = service.store.get_session(session.session_id)
        attempt = service.store.get_attempt(session.session_id, stored.last_attempt_id)
        assert attempt.metrics == {"sharpe": 1.25}
        reply = service.store.get_messages(session.session_id)[-1]
        assert reply.metadata["metrics"] == {"sharpe": 1.25}

    asyncio.run(scenario())


def test_empty_successful_answer_says_so():
    """An empty answer must not be dressed up as a finished strategy run."""
    attempt = Attempt(session_id="s" * 12, prompt="p")
    attempt.mark_completed(summary="")
    message = SessionService._format_result_message(attempt)
    assert "without producing any text output" in message
    assert "Strategy execution completed" not in message


# ---------------------------------------------------------------------------
# History window
# ---------------------------------------------------------------------------


def test_one_oversized_message_does_not_empty_the_history_window():
    """The newest turn survives truncated instead of the window collapsing."""
    messages = [
        type("M", (), {"role": "user", "content": "older turn"})(),
        type("M", (), {"role": "assistant", "content": "x" * 20000})(),
        type("M", (), {"role": "user", "content": "current turn is dropped"})(),
    ]
    history = SessionService._convert_messages_to_history(messages)
    assert history, "an oversized newest message wiped the entire window"
    assert history[-1]["content"].endswith("[... truncated]")
    assert len(history[-1]["content"]) <= 12000 + len("\n[... truncated]")


def test_normal_history_is_untouched():
    """Messages inside the budget are passed through unchanged."""
    messages = [
        type("M", (), {"role": "user", "content": "first"})(),
        type("M", (), {"role": "assistant", "content": "second"})(),
        type("M", (), {"role": "user", "content": "current turn is dropped"})(),
    ]
    history = SessionService._convert_messages_to_history(messages)
    assert [m["content"] for m in history] == ["first", "second"]


def test_claim_is_released_when_pre_run_bookkeeping_fails(tmp_path, monkeypatch):
    """A failure before the agent even starts must not brick the session.

    mark_running/update_attempt/emit used to run outside the try, so a disk
    error there stranded the claim and every later send returned 409.
    """

    async def scenario() -> None:
        service = _service(tmp_path, monkeypatch)
        session = service.create_session(title="bookkeeping")
        _stub_agent(service, monkeypatch, {"status": "success", "content": "ok"})

        original = service.store.update_attempt
        calls = {"n": 0}

        def _fail_first(attempt):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("no space left on device")
            return original(attempt)

        monkeypatch.setattr(service.store, "update_attempt", _fail_first)

        await service.send_message(session.session_id, "one")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if session.session_id not in service._inflight:
                break
        assert session.session_id not in service._inflight
        # And the session is usable again.
        monkeypatch.setattr(service.store, "update_attempt", original)
        assert await service.send_message(session.session_id, "two")

    asyncio.run(scenario())


def test_cancel_before_the_agent_loop_exists_releases_the_claim(tmp_path, monkeypatch):
    """cancel_current must work while the registry is still being built.

    _active_loops is only populated once construction finishes, so a run that
    hangs earlier (e.g. MCP discovery) previously held the claim forever.
    """

    async def scenario() -> None:
        service = _service(tmp_path, monkeypatch)
        session = service.create_session(title="hung")
        never = asyncio.Event()
        _stub_agent(service, monkeypatch, {"status": "success"}, gate=never)

        await service.send_message(session.session_id, "one")
        await asyncio.sleep(0.02)
        assert session.session_id in service._inflight
        assert service.cancel_current(session.session_id) is True

        for _ in range(100):
            await asyncio.sleep(0.01)
            if session.session_id not in service._inflight:
                break
        assert session.session_id not in service._inflight

        stored = service.store.get_session(session.session_id)
        attempt = service.store.get_attempt(session.session_id, stored.last_attempt_id)
        assert attempt.status == AttemptStatus.CANCELLED

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Start-time provenance
# ---------------------------------------------------------------------------


def test_attempt_started_event_carries_wall_clock_start(tmp_path, monkeypatch):
    """A client that (re)connects mid-attempt resumes its elapsed clock from
    the real start, so the event must carry it rather than leave the client to
    guess from its own reconnect time."""

    async def scenario() -> None:
        service = _service(tmp_path, monkeypatch)
        session = service.create_session(title="started-at")
        seen: list[tuple[str, dict]] = []
        service.event_bus.emit = lambda sid, event, data: seen.append((event, data))  # type: ignore[assignment]
        _stub_agent(service, monkeypatch, {"status": "success", "content": "ok"})

        before = time.time()
        await service.send_message(session.session_id, "go")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if session.session_id not in service._inflight:
                break
        after = time.time()

        started = [data for event, data in seen if event == "attempt.started"]
        assert len(started) == 1
        stored = service.store.get_session(session.session_id)
        assert started[0]["attempt_id"] == stored.last_attempt_id
        assert isinstance(started[0]["started_at"], float)
        assert before <= started[0]["started_at"] <= after

    asyncio.run(scenario())


def test_completed_reply_and_terminal_event_carry_attempt_timing(tmp_path, monkeypatch):
    """History hydration needs the attempt's real start: the first tool call is
    only a lower bound (the model thinks before it reaches for a tool, and a
    pure-text turn has no tools), so the reply persists ``started_at`` next to
    ``elapsed_ms`` and the terminal event carries both ends."""

    async def scenario() -> None:
        service = _service(tmp_path, monkeypatch)
        session = service.create_session(title="timing")
        seen: list[tuple[str, dict]] = []
        service.event_bus.emit = lambda sid, event, data: seen.append((event, data))  # type: ignore[assignment]
        _stub_agent(service, monkeypatch, {"status": "success", "content": "ok"})

        before = time.time()
        await service.send_message(session.session_id, "go")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if session.session_id not in service._inflight:
                break
        after = time.time()

        started = next(data for event, data in seen if event == "attempt.started")
        completed = next(data for event, data in seen if event == "attempt.completed")
        reply = service.store.get_messages(session.session_id)[-1]

        assert reply.role == "assistant"
        assert reply.metadata["started_at"] == started["started_at"]
        assert before <= reply.metadata["started_at"] <= after
        assert reply.metadata["elapsed_ms"] >= 0
        assert completed["started_at"] == started["started_at"]
        assert completed["started_at"] <= completed["ended_at"] <= after
        assert completed["elapsed_ms"] == reply.metadata["elapsed_ms"]

    asyncio.run(scenario())


def test_attempt_records_and_round_trips_its_wall_clock_start():
    """``started_at`` outlives the event ring buffer only if it is persisted
    with the attempt; legacy attempt files without it must still load."""
    attempt = Attempt(session_id="s1", prompt="go")
    assert attempt.started_at is None

    before = time.time()
    attempt.mark_running()
    assert isinstance(attempt.started_at, float)
    assert before <= attempt.started_at <= time.time()

    restored = Attempt.from_dict(attempt.to_dict())
    assert restored.started_at == attempt.started_at
    assert restored.status == AttemptStatus.RUNNING

    legacy = attempt.to_dict()
    del legacy["started_at"]
    assert Attempt.from_dict(legacy).started_at is None
