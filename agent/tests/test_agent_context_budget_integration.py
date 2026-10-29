"""Runtime integration of deferred tools, bounded prompts and readable results."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import src.agent.loop as loop_mod
from src.agent.context import ContextBuilder
from src.agent.loop import AgentLoop, _context_collapse, _microcompact
from src.agent.memory import WorkspaceMemory
from src.agent.result_artifacts import deliver_tool_result
from src.agent.tools import BaseTool, ToolRegistry
from src.agent.trace import TraceWriter
from src.config.accessor import reset_env_config
from src.config.paths import get_runs_dir
from src.providers.chat import LLMResponse, LLMRuntimeSnapshot, ProviderStreamError, ToolCallRequest
from src.tools.read_file_tool import ReadFileTool
from src.tools.redaction import redact_tool_result


class Probe(BaseTool):
    deterministic = True
    repeatable = True
    parameters = {"type": "object", "properties": {}}

    def __init__(self, name="probe", content='{"status":"ok"}', description="Probe"):
        self.name, self.content, self.description = name, content, description
        self.calls = 0

    def execute(self, **kwargs):
        self.calls += 1
        return self.content


class Model:
    model_name = "deepseek-flash"
    runtime_snapshot = LLMRuntimeSnapshot("deepseek", "deepseek-flash", "")

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.summaries = []

    def stream_chat(self, messages, tools=None, **kwargs):
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result

    def chat(self, messages, **kwargs):
        self.summaries.append(copy.deepcopy(messages))
        return LLMResponse(content="A compact handoff.")


@pytest.fixture
def create_agent(monkeypatch, tmp_path):
    monkeypatch.delenv("TOKEN_THRESHOLD", raising=False)
    # These legacy attributes are virtual module __getattr__ values. setattr
    # reads them first, warming EnvConfig before a test sets its window, then
    # leaves a concrete attribute behind on undo and masks the bug by order.
    monkeypatch.delitem(vars(loop_mod), "TOKEN_THRESHOLD", raising=False)
    monkeypatch.setitem(vars(loop_mod), "STALL_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(ContextBuilder, "build_messages", lambda self, text, history=None:
                        [{"role": "system", "content": "Brief assistant."}, *(history or []), {"role": "user", "content": text}])
    root = get_runs_dir() / tmp_path.name
    root.mkdir(parents=True)

    def make(model, registry=None, max_iterations=4):
        reset_env_config()
        return AgentLoop(registry if registry is not None else ToolRegistry(), model,
                         memory=WorkspaceMemory(run_dir=str(root)), max_iterations=max_iterations)
    return make


def test_run_loads_only_requested_schemas_and_resets_for_next_question(create_agent):
    registry = ToolRegistry()
    for index in range(20):
        registry.register(Probe(f"probe_{index}", description="Details " * 400))
    model = Model([
        LLMResponse(tool_calls=[ToolCallRequest("load", "load_tools", {"names": ["probe_19"]})]),
        LLMResponse(tool_calls=[ToolCallRequest("work", "probe_19", {})]),
        LLMResponse(content="Finished."), LLMResponse(content="New question finished."),
    ])
    agent = create_agent(model, registry)
    assert agent.run("hello")["status"] == "success"
    def names(request):
        return [item["function"]["name"] for item in request[1]]
    assert names(model.requests[0]) == ["load_tools"]
    assert names(model.requests[1]) == ["load_tools", "probe_19"]
    assert registry.get("probe_19").calls == 1
    assert sum(tool.calls for tool in registry._tools.values()) == 1
    assert len(agent.registry.get_definitions()) == 21
    assert agent.run("hello again")["status"] == "success"
    assert names(model.requests[3]) == ["load_tools"]
    assert "load_tools" not in registry


def test_large_window_does_not_summarise_at_the_old_40k_estimate(create_agent):
    model = Model([LLMResponse(content="Finished.")])
    agent = create_agent(model)
    history = [{"role": "user", "content": "x" * 180_000}]
    assert agent.run("hello", history=history)["status"] == "success"
    assert not model.summaries
    assert history[0]["content"] in model.requests[0][0][1]["content"]


@pytest.mark.parametrize("max_iterations,expected_calls", [(2, 0), (1, 1)])
def test_static_schema_budget_uses_the_actual_request_including_text_only_final(
    create_agent, monkeypatch, max_iterations, expected_calls,
):
    monkeypatch.setenv("VIBE_TRADING_CONTEXT_WINDOW", "1000")
    registry = ToolRegistry()
    registry.register(Probe(description="Schema text " * 1000))
    model = Model([LLMResponse(content="Finished.")])
    result = create_agent(model, registry, max_iterations).run("hello")
    assert len(model.requests) == expected_calls
    assert not model.summaries
    if expected_calls:
        assert result["status"] == "success" and model.requests[0][1] is None
    else:
        assert result["status"] == "failed" and "context_budget_exceeded" in result["reason"]


def test_reported_context_overflow_compacts_once_and_retries_without_losing_the_request(create_agent, monkeypatch):
    monkeypatch.setenv("VIBE_TRADING_CONTEXT_WINDOW", "10000")
    original = ValueError("maximum context length is 4096 tokens")
    original.status_code = 400
    model = Model([ProviderStreamError(provider="deepseek", model="deepseek-flash", original=original),
                   LLMResponse(content="Recovered.")])
    agent = create_agent(model)
    result = agent.run("CURRENT_REQUEST", history=[{"role": "user", "content": "old " * 5000}])
    assert result["status"] == "success" and len(model.requests) == 2
    assert agent._learned_context_window == 4096 and model.summaries
    assert "CURRENT_REQUEST" in json.dumps(model.requests[1][0])
    assert agent._prompt_tokens(model.requests[1][0]) <= 3276


def test_microcompact_preserves_all_new_observations_until_a_model_has_seen_them(monkeypatch):
    monkeypatch.setattr("src.agent.loop.KEEP_RECENT", 1)
    messages = [{"role": "tool", "tool_call_id": str(i), "content": "data" * 500} for i in range(8)]
    pending = {str(i) for i in range(3, 8)}
    _microcompact(messages, target_tokens=100, measure=lambda ms: sum(len(m["content"]) for m in ms),
                  preserve_tool_call_ids=pending)
    assert all(messages[i]["content"] == "data" * 500 for i in range(3, 8))
    assert messages[0]["content"] != "data" * 500


def test_large_cached_observation_is_readable_without_refetching(create_agent):
    raw = json.dumps({"status": "ok", "text": "数据" * 12000, "api_key": "synthetic-private-key"}, ensure_ascii=False)
    registry = ToolRegistry()
    probe = Probe(content=raw)
    registry.register(probe)
    registry.register(ReadFileTool())
    model = Model([LLMResponse(tool_calls=[ToolCallRequest("first", "probe", {})]),
                   LLMResponse(tool_calls=[ToolCallRequest("again", "probe", {})]), LLMResponse(content="Finished.")])
    agent = create_agent(model, registry)
    assert agent.run("hello")["status"] == "success" and probe.calls == 1
    first = json.loads(next(m["content"] for m in model.requests[1][0] if m.get("tool_call_id") == "first"))
    replay = json.loads(next(m["content"] for m in model.requests[2][0] if m.get("tool_call_id") == "again"))
    assert first["_result_file"] == replay["_result_file"]
    info = first["_result_file"]
    parts = [json.loads(registry.execute("read_file", {
        "path": str(Path(info["parts_dir"]) / f"{index}.txt"), "run_dir": agent.memory.run_dir,
    }))["content"] for index in range(1, info["part_count"] + 1)]
    assert "".join(parts) == redact_tool_result(raw)
    assert "synthetic-private-key" not in Path(info["path"]).read_text()


def test_result_locators_survive_repeated_compaction_and_refuse_tampered_files(create_agent):
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    agent = create_agent(Model([]), registry)
    run_dir = Path(agent.memory.run_dir)
    receipt = deliver_tool_result("observation " * 2000, run_dir, registry)
    info = json.loads(receipt)["_result_file"]
    messages = [{"role": "system", "content": "system"}, {"role": "tool", "content": receipt},
                {"role": "user", "content": "next question"}]
    with_trace = TraceWriter(run_dir / "trace")
    try:
        for _ in range(2):
            agent._auto_compact(messages, run_dir, with_trace, tail_token_budget=1)
            assert info["path"] in messages[1]["content"]
    finally:
        with_trace.close()
    Path(info["path"]).write_text("changed")
    assert not agent._result_artifact_index([], run_dir)


def test_collapse_keeps_valid_json_receipt_and_complete_file_locator(create_agent):
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    agent = create_agent(Model([]), registry)
    receipt = deliver_tool_result("observation " * 2000, agent.memory.run_dir, registry)
    messages = [{"role": "system", "content": "system"}, {"role": "tool", "content": receipt},
                *[{"role": "user", "content": "later"} for _ in range(8)]]
    _context_collapse(messages)
    assert json.loads(messages[1]["content"])["_result_file"] == json.loads(receipt)["_result_file"]
