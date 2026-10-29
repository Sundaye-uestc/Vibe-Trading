"""Worker observations remain usable while prompt growth follows model limits."""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent.tools import BaseTool, ToolRegistry
from src.providers.chat import LLMResponse, ProviderStreamError, ToolCallRequest
from src.swarm.context import WorkerContext
from src.swarm.models import SwarmAgentSpec, SwarmTask
from src.swarm import worker
from src.tools.read_file_tool import ReadFileTool


FINAL = "The requested observations have been collected, checked and retained for the final analysis. " * 3


class ObservationTool(BaseTool):
    name = ""
    description = "Read an observation."
    parameters = {"type": "object", "properties": {"index": {"type": "integer"}, "size": {"type": "integer"}}}

    def __init__(self, name="observation"):
        self.name = name
        self.calls = []

    def execute(self, index=0, size=100, **kwargs):
        self.calls.append(index)
        return json.dumps({"status": "ok", "index": index, "data": f"observation-{index}:" + "x" * size,
                           "api_key": "fixture-only-not-a-real-key"})


class Model:
    model_name = "deepseek-flash"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.seen = []
        self.schemas = []

    def stream_chat(self, messages, tools=None, **kwargs):
        self.seen.append(copy.deepcopy(messages))
        self.schemas.append(copy.deepcopy(tools))
        response = next(self.responses)
        if callable(response):
            response = response()
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        pass


def tool_batch(count=1, size=100, start=0):
    return LLMResponse(tool_calls=[
        ToolCallRequest(id=f"call-{i}", name="observation", arguments={"index": i, "size": size})
        for i in range(start, start + count)
    ])


@pytest.fixture
def setup_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    monkeypatch.setenv("VIBE_TRADING_CONTEXT_WINDOW", "100000")
    monkeypatch.setenv("VIBE_TRADING_CONTEXT_MAX_TOKENS", "200000")
    monkeypatch.setattr(worker, "build_worker_prompt", lambda *args, **kwargs: "Worker system prompt.")
    registry = ToolRegistry()
    registry.register(ObservationTool())
    registry.register(ReadFileTool())
    monkeypatch.setattr(worker, "build_swarm_registry", lambda *args, **kwargs: registry)

    def run(responses, *, iterations=6, cancel=None):
        model = Model(responses)
        monkeypatch.setattr(worker, "ChatLLM", lambda **kwargs: model)
        events = []
        result = worker.run_worker(
            SwarmAgentSpec(id="analyst", role="Analyst", system_prompt="Read observations.",
                          tools=registry.tool_names, max_iterations=iterations),
            SwarmTask(id="task", agent_id="analyst", prompt_template="Analyze the observations."),
            {}, {}, tmp_path, event_callback=events.append, cancel_event=cancel,
        )
        return result, model, events

    return run, registry


def test_wide_batch_is_seen_intact_before_pressure_can_offload_it(setup_worker, monkeypatch):
    run, _ = setup_worker
    monkeypatch.setenv("VIBE_TRADING_CONTEXT_MAX_TOKENS", "8000")
    result, model, events = run([tool_batch(6, 4500), tool_batch(1, 1000, start=6), LLMResponse(content=FINAL)])
    assert result.status == "completed"
    received = [message for message in model.seen[1] if message["role"] == "tool"]
    assert len(received) == 6
    assert all(len(json.loads(message["content"])["data"]) > 4500 for message in received)
    third = [message for message in model.seen[2] if message["role"] == "tool"]
    assert json.loads(third[-1]["content"])["index"] == 6
    assert any(message["content"].startswith("[Worker context result saved:") for message in third[:-1])
    moved = [event for event in events if event.type == "worker_context_offloaded"]
    assert moved
    for receipt in moved[0].data["results"]:
        restored = "".join(Path(path).read_text() for path in receipt["paths"])
        assert "fixture-only-not-a-real-key" not in restored
        assert json.loads(restored)["data"].startswith("observation-")


def test_unseen_batch_that_cannot_fit_is_not_silently_cleared(setup_worker, monkeypatch, tmp_path):
    run, _ = setup_worker
    monkeypatch.setenv("VIBE_TRADING_CONTEXT_MAX_TOKENS", "3000")
    result, model, events = run([tool_batch(6, 3000)])
    assert result.status == "token_limit" and len(model.seen) == 1
    messages = json.loads(next(tmp_path.rglob("messages.json")).read_text())
    received = [message for message in messages if message["role"] == "tool"]
    assert len(received) == 6
    assert all(len(json.loads(message["content"])["data"]) > 3000 for message in received)
    assert not any(event.type == "worker_context_offloaded" for event in events)


def test_schema_size_counts_toward_a_small_model_window(setup_worker, monkeypatch):
    run, registry = setup_worker
    monkeypatch.setenv("VIBE_TRADING_CONTEXT_WINDOW", "1000")
    registry.get("observation").description = "Schema description " * 2000
    result, model, events = run([])
    assert result.status == "token_limit" and not model.seen
    limited = next(event for event in events if event.type == "worker_token_limit")
    assert limited.data["static_tokens"] > limited.data["limit"]


def test_default_context_uses_model_window_instead_of_old_60k_cap(setup_worker, monkeypatch):
    run, _ = setup_worker
    monkeypatch.delenv("VIBE_TRADING_CONTEXT_WINDOW")
    monkeypatch.setattr(worker, "build_worker_prompt", lambda *args, **kwargs: "a" * 280_000)
    result, model, _ = run([LLMResponse(content=FINAL)], iterations=1)
    assert len(model.seen) == 1
    # The unchanged data-agent contract still refuses a report without tool
    # evidence; it is the obsolete 60K context limit that must no longer fire.
    assert result.status == "incomplete" and "output contract" in result.error


def test_explicit_legacy_test_limit_still_caps_worker(setup_worker, monkeypatch):
    run, _ = setup_worker
    monkeypatch.setattr(worker, "_MAX_TOKEN_ESTIMATE", 1)
    result, model, _ = run([])
    assert result.status == "token_limit" and not model.seen


def test_large_tool_result_is_a_readable_receipt_without_permission_changes(setup_worker):
    run, registry = setup_worker
    result, model, _ = run([tool_batch(1, 30_000), LLMResponse(content=FINAL)])
    assert result.status == "completed"
    receipt = json.loads(next(message["content"] for message in model.seen[1] if message["role"] == "tool"))
    artifact = receipt["_result_file"]
    full = Path(artifact["path"]).read_text()
    assert len(json.loads(full)["data"]) > 30_000
    assert "fixture-only-not-a-real-key" not in full
    parts = []
    root = Path(artifact["path"]).parents[2]
    for index in range(1, artifact["part_count"] + 1):
        reply = json.loads(registry.execute("read_file", {
            "path": str(Path(artifact["parts_dir"]) / f"{index}.txt"), "run_dir": str(root),
        }))
        parts.append(reply["content"])
    assert "".join(parts) == full
    assert "bash" not in registry.tool_names


def overflow(limit=4000):
    original = type("LimitError", (Exception,), {"status_code": 400})(f"maximum context length is {limit} tokens")
    return ProviderStreamError(provider="fixture", model="fixture", original=original,
                               usage_metadata={"input_tokens": 7, "output_tokens": 1, "total_tokens": 8})


def test_overflow_recovers_once_by_moving_only_seen_observations(setup_worker):
    run, registry = setup_worker
    result, model, events = run([
        tool_batch(3, 4000), tool_batch(1, 100, start=3), overflow(),
        tool_batch(1, 100, start=4), overflow(2000),
    ], iterations=7)
    assert result.status == "token_limit" and len(model.seen) == 5
    retries = [event for event in events if event.type == "worker_context_offloaded"
               and event.data.get("reason") == "context_overflow"]
    assert len(retries) == 1
    retry_messages = [message for message in model.seen[3] if message["role"] == "tool"]
    assert json.loads(retry_messages[-1]["content"])["index"] == 3
    assert any(message["content"].startswith("[Worker context result saved:") for message in retry_messages[:-1])
    assert registry.get("observation").calls == [0, 1, 2, 3, 4]


def test_overflow_without_seen_evidence_does_not_retry_or_discard_new_batch(setup_worker):
    run, registry = setup_worker
    result, model, _ = run([tool_batch(4, 1000), overflow()])
    assert result.status == "token_limit" and len(model.seen) == 2
    assert all(not message["content"].startswith("[Worker context result saved:")
               for message in model.seen[1] if message["role"] == "tool")
    assert registry.get("observation").calls == [0, 1, 2, 3]


def test_cancelled_overflow_does_not_start_recovery_request(setup_worker):
    run, _ = setup_worker
    cancel = threading.Event()

    def cancel_and_fail():
        cancel.set()
        return overflow()

    result, model, _ = run([cancel_and_fail], cancel=cancel)
    assert result.status == "cancelled" and len(model.seen) == 1
    assert result.input_tokens == 7 and result.output_tokens == 1


def test_overflow_does_not_retry_after_the_worker_deadline(setup_worker, monkeypatch):
    run, _ = setup_worker
    clock = [0.0]
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda seconds: None))

    def expired_request():
        clock[0] = 10_000
        return overflow()

    result, model, _ = run([tool_batch(3, 4000), tool_batch(1, 100, start=3), expired_request])
    assert result.status == "timeout" and len(model.seen) == 3
    assert "time budget exhausted" in result.error


def test_worker_does_not_treat_an_overridden_reader_as_local(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    registry = ToolRegistry()
    registry.register(ObservationTool("read_file"))
    context = WorkerContext(model="deepseek-flash", artifact_dir=tmp_path, registry=registry)
    assert context.readable_root is None


def test_large_worker_registry_loads_schemas_without_counting_discovery_as_data(setup_worker):
    run, registry = setup_worker
    for index in range(18):
        registry.register(ObservationTool(f"additional_{index}"))
    load = LLMResponse(tool_calls=[ToolCallRequest(id="load", name="load_tools", arguments={"names": ["observation"]})])
    result, model, _ = run([load, tool_batch(), LLMResponse(content=FINAL)])
    assert result.status == "completed"
    first_names = [definition["function"]["name"] for definition in model.schemas[0]]
    second_names = [definition["function"]["name"] for definition in model.schemas[1]]
    assert "load_tools" in first_names and "observation" not in first_names
    assert "observation" in second_names
    assert "additional_17" in model.seen[0][0]["content"]
    assert registry.get("observation").calls == [0]
    assert "load_tools" not in registry.tool_names
