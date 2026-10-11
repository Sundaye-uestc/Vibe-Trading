"""Recovery-turn evidence handoff regressions.

Failed long runs must leave a bounded tool/evidence index in the transcript so
the next user turn can continue from collected data instead of seeing only an
error string.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from src.session.events import EventBus
from src.session.models import Attempt, Message, Session
from src.session.service import SessionService
from src.session.store import SessionStore


class _DummyIndex:
    def index_session(self, session_id: str, title: str) -> None:
        del session_id, title

    def index_message(self, session_id: str, role: str, content: str) -> None:
        del session_id, role, content


def _service(tmp_path: Path, monkeypatch) -> SessionService:
    monkeypatch.setattr("src.session.service.get_shared_index", lambda: _DummyIndex())
    return SessionService(
        store=SessionStore(tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )


def test_failed_attempt_persists_bounded_evidence_for_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    session = Session(session_id="recovery0001", title="recovery")
    service.store.create_session(session)
    attempt = Attempt(session_id=session.session_id, prompt="research AAPL")
    service.store.create_attempt(attempt)

    run_dir = tmp_path / "runs" / "failed-run"
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "artifacts" / "run_card.json").write_text("{}", encoding="utf-8")
    trail = [
        {
            "tool": "get_market_data",
            "status": "ok",
            "arguments": {"symbol": "AAPL", "interval": "1D"},
            "preview": "AAPL close=195.00",
        }
    ]

    async def _failed(*args, **kwargs):
        del args, kwargs
        return {
            "status": "failed",
            "reason": "empty_model_response",
            "run_dir": str(run_dir),
            "tool_trail": trail,
        }

    monkeypatch.setattr(service, "_run_with_agent", _failed)
    asyncio.run(service._run_attempt(session, attempt))

    stored = service.store.get_messages(session.session_id)
    reply = stored[-1]
    assert reply.metadata["status"] == "failed"
    assert reply.metadata["run_id"] == "failed-run"
    assert reply.metadata["recovery_context"]["artifacts"] == [
        "artifacts/run_card.json"
    ]
    assert str(tmp_path) not in json.dumps(reply.metadata["recovery_context"])
    assert reply.tool_trail == trail

    # Add the next prompt exactly as _run_attempt does. The current turn is
    # excluded by the history converter, while the failure evidence survives.
    stored.append(
        Message(
            session_id=session.session_id,
            role="user",
            content="continue from the collected evidence",
        )
    )
    history = service._convert_messages_to_history(stored)
    evidence = history[-1]["content"]
    assert "[Previous attempt evidence]" in evidence
    assert "run_id=failed-run" in evidence
    assert "get_market_data [ok]" in evidence
    assert "AAPL close=195.00" in evidence
    assert "symbol" in evidence


def test_recovery_evidence_is_bounded() -> None:
    trail = [
        {
            "tool": f"tool-{index}",
            "status": "ok",
            "arguments": {"payload": "x" * 5000},
            "preview": "y" * 5000,
        }
        for index in range(100)
    ]
    bounded = SessionService._bound_recovery_tool_trail(trail)
    assert len(bounded) == 64
    assert len(bounded[0]["arguments"]["_truncated"]) == 2_000
    assert len(bounded[-1]["preview"]) == 1_000
