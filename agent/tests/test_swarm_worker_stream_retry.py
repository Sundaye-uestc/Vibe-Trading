"""Tests for the single stream retry on ProviderStreamError in run_worker.

``ChatLLM.stream_chat`` used to silently fall back to non-streaming ``chat()``
on any exception; it now raises ``ProviderStreamError``. A swarm worker that
previously survived a transient mid-stream hiccup (connection reset) via the
silent fallback would now fail outright, so ``run_worker`` retries the stream
exactly once for ``ProviderStreamError`` — and only for it — before the
existing failure path.
"""

from __future__ import annotations

from pathlib import Path
import threading
from unittest.mock import patch

import pytest

from src.providers.chat import LLMResponse, ProviderStreamError
from src.swarm.models import SwarmAgentSpec, SwarmTask, WorkerResult
import src.swarm.worker as worker_mod
from src.swarm.worker import run_worker

# Substantive prose so _classify_deliverable accepts the tool-less worker.
FINAL_TEXT = (
    "# BTC-USDT — Short-Term View\n\n"
    "Spot 81,704.6 (2026-05-05). 7d range 77,750-82,842.\n\n"
    "**Recommendation: accumulate on dips to 79k; invalidation below 77.5k.**\n"
    "Position 3% NAV, stop 76,900, target 86,000. Funding 0.035%/8h elevated\n"
    "but not extreme; exchange reserves declining (bullish)."
)


class _EmptyRegistry:
    """Minimal stand-in for the swarm ToolRegistry (worker needs no tools)."""

    def get_definitions(self) -> list[dict]:
        """Return an empty tool-definition list.

        Returns:
            Empty list — the scripted LLM never requests a tool call.
        """
        return []


class _FlakyChatLLM:
    """Scripted ChatLLM whose stream_chat raises queued errors, then succeeds."""

    def __init__(self, errors: list[Exception], final: LLMResponse) -> None:
        """Initialize the flaky stub.

        Args:
            errors: Exceptions raised by successive ``stream_chat`` calls,
                consumed in order before any success.
            final: Response returned once the error queue is drained.
        """
        self._errors = list(errors)
        self._final = final
        self.calls = 0

    def __call__(self, *args, **kwargs) -> "_FlakyChatLLM":
        """Support ``ChatLLM(model_name=...)`` constructor-style patching.

        Returns:
            The shared stub instance.
        """
        return self

    def close(self) -> None:
        """No-op: the stub owns no HTTP client."""
        return self

    def stream_chat(self, messages, tools=None, on_text_chunk=None, timeout=None) -> LLMResponse:
        """Raise the next queued error or return the final response.

        Args:
            messages: Conversation messages (ignored).
            tools: Tool definitions (ignored).
            on_text_chunk: Streaming callback (ignored).
            timeout: Per-call timeout (ignored).

        Returns:
            The scripted final ``LLMResponse``.

        Raises:
            Exception: The next queued error, if any remain.
        """
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._final


def _stream_error(usage_metadata=None) -> ProviderStreamError:
    """Build a ProviderStreamError mimicking a transient mid-stream reset.

    Returns:
        ProviderStreamError wrapping a ``ConnectionResetError``.
    """
    return ProviderStreamError(
        provider="openrouter",
        model="test-model",
        original=ConnectionResetError("connection reset by peer"),
        usage_metadata=usage_metadata,
    )


def _run(monkeypatch, tmp_path: Path, llm: _FlakyChatLLM, cancel_event=None) -> WorkerResult:
    """Run a tool-less worker against the given scripted LLM.

    Args:
        monkeypatch: pytest monkeypatch fixture (zeroes the retry sleep).
        tmp_path: Scratch run directory.
        llm: The scripted ChatLLM stub.

    Returns:
        The WorkerResult from ``run_worker``.
    """
    monkeypatch.setattr(worker_mod, "_STREAM_RETRY_DELAY_S", 0.0)
    agent = SwarmAgentSpec(
        id="analyst",
        role="Synthesis analyst",
        system_prompt="You synthesize upstream findings.",
        tools=[],
        skills=[],
        max_iterations=3,
        timeout_seconds=60,
    )
    task = SwarmTask(id="t1", agent_id="analyst", prompt_template="Summarize.")
    with (
        patch.object(worker_mod, "build_swarm_registry", lambda *a, **k: _EmptyRegistry()),
        patch.object(worker_mod, "ChatLLM", llm),
    ):
        return run_worker(
            agent_spec=agent,
            task=task,
            upstream_summaries={},
            user_vars={},
            run_dir=tmp_path,
            cancel_event=cancel_event,
        )


def test_single_stream_failure_is_retried_and_worker_succeeds(monkeypatch, tmp_path):
    """One ProviderStreamError then success → worker completes (2 calls)."""
    llm = _FlakyChatLLM([_stream_error()], LLMResponse(content=FINAL_TEXT))

    result = _run(monkeypatch, tmp_path, llm)

    assert result.status == "completed"
    assert result.error is None
    assert llm.calls == 2


def test_double_stream_failure_fails_worker(monkeypatch, tmp_path):
    """Two consecutive ProviderStreamErrors → existing failure path (no 3rd try)."""
    llm = _FlakyChatLLM(
        [_stream_error(), _stream_error()], LLMResponse(content=FINAL_TEXT)
    )

    result = _run(monkeypatch, tmp_path, llm)

    assert result.status == "failed"
    assert "LLM call failed at iteration 0" in (result.error or "")
    assert llm.calls == 2


def test_non_stream_error_is_not_retried(monkeypatch, tmp_path):
    """A non-ProviderStreamError fails immediately without a retry."""
    llm = _FlakyChatLLM([ValueError("boom")], LLMResponse(content=FINAL_TEXT))

    result = _run(monkeypatch, tmp_path, llm)

    assert result.status == "failed"
    assert "LLM call failed at iteration 0" in (result.error or "")
    assert llm.calls == 1


def _bad_request_error() -> ProviderStreamError:
    """Build a ProviderStreamError mimicking a deterministic 400 rejection.

    Returns:
        ProviderStreamError whose original exception carries status_code=400.
    """
    original = Exception("invalid temperature: only 1 is allowed for this model")
    original.status_code = 400  # type: ignore[attr-defined]
    return ProviderStreamError(
        provider="moonshot", model="kimi-k2.6", original=original
    )


def test_non_retryable_4xx_fails_without_retry(monkeypatch, tmp_path):
    """A deterministic 4xx ProviderStreamError fails immediately (1 call)."""
    llm = _FlakyChatLLM([_bad_request_error()], LLMResponse(content=FINAL_TEXT))

    result = _run(monkeypatch, tmp_path, llm)

    assert result.status == "failed"
    assert "LLM call failed at iteration 0" in (result.error or "")
    assert llm.calls == 1


def test_failed_request_usage_and_successful_retry_are_each_counted_once(monkeypatch, tmp_path):
    llm = _FlakyChatLLM(
        [_stream_error({"input_tokens": 17, "output_tokens": 5})],
        LLMResponse(content=FINAL_TEXT, usage_metadata={"input_tokens": 29, "output_tokens": 11}),
    )
    result = _run(monkeypatch, tmp_path, llm)
    assert result.status == "completed"
    assert llm.calls == 2
    assert (result.input_tokens, result.output_tokens) == (46, 16)


def test_final_failure_keeps_usage_from_both_requests(monkeypatch, tmp_path):
    llm = _FlakyChatLLM(
        [_stream_error({"input_tokens": 17, "output_tokens": 5}),
         _stream_error({"input_tokens": 29, "output_tokens": 11})],
        LLMResponse(content=FINAL_TEXT),
    )
    result = _run(monkeypatch, tmp_path, llm)
    assert result.status == "failed"
    assert llm.calls == 2
    assert (result.input_tokens, result.output_tokens) == (46, 16)


def test_nonretryable_failure_keeps_reported_usage(monkeypatch, tmp_path):
    error = _bad_request_error()
    error.usage_metadata = {"input_tokens": 17, "output_tokens": 5}
    llm = _FlakyChatLLM([error], LLMResponse(content=FINAL_TEXT))
    result = _run(monkeypatch, tmp_path, llm)
    assert result.status == "failed"
    assert llm.calls == 1
    assert (result.input_tokens, result.output_tokens) == (17, 5)


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": 0, "output_tokens": 0},
                                   {"input_tokens": True, "output_tokens": -1},
                                   {"input_tokens": "unknown", "output_tokens": float("nan")}])
def test_failed_requests_never_invent_usage(monkeypatch, tmp_path, usage):
    llm = _FlakyChatLLM([_stream_error(usage), _stream_error(usage)], LLMResponse(content=FINAL_TEXT))
    result = _run(monkeypatch, tmp_path, llm)
    assert result.status == "failed"
    assert (result.input_tokens, result.output_tokens) == (0, 0)


def test_cancel_during_retry_backoff_does_not_count_a_nonexistent_response(monkeypatch, tmp_path):
    cancel_event = threading.Event()

    class CancellingLLM(_FlakyChatLLM):
        def stream_chat(self, messages, **kwargs):
            self.calls += 1
            cancel_event.set()
            raise _stream_error({"input_tokens": 17, "output_tokens": 5})

    llm = CancellingLLM([], LLMResponse(content=FINAL_TEXT))
    result = _run(monkeypatch, tmp_path, llm, cancel_event=cancel_event)
    assert result.status == "cancelled"
    assert llm.calls == 1
    assert (result.input_tokens, result.output_tokens) == (17, 5)
