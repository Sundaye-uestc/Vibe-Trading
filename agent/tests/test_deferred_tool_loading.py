"""Schema loading reduces requests while retaining discovery and dispatch guards."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from src.agent.context import ContextBuilder
from src.agent.memory import WorkspaceMemory
from src.agent.tool_loading import DeferredToolRegistry, LoadToolsTool, prepare_registry, request_definitions
from src.agent.tools import BaseTool, ToolRegistry


class ProbeTool(BaseTool):
    # Instance names keep these fake tools out of global subclass discovery.
    name = ""

    def __init__(self, name, *, readonly=True):
        self.name = name
        self.description = f"Capability for {name}: " + "description " * 40
        self.parameters = {
            "type": "object",
            "properties": {"value": {"type": "string", "description": "Argument " * 40}},
            "required": ["value"],
        }
        self.is_readonly = readonly
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return json.dumps({"status": "ok", **kwargs})


def registry_with_tools(size=20, cls=ToolRegistry):
    registry = cls()
    for name in ["search_symbol", "read_file", *[f"probe_{index}" for index in range(size - 2)]]:
        registry.register(ProbeTool(name))
    return registry


def names(definitions):
    return [definition["function"]["name"] for definition in definitions]


@pytest.fixture(autouse=True)
def lazy_config(monkeypatch):
    from src.config.accessor import get_env_config, reset_env_config

    monkeypatch.setenv("VIBE_TRADING_TOOL_LOADING", "lazy")
    reset_env_config()
    return get_env_config()


def test_loading_exposes_exact_schemas_without_execution_or_source_mutation():
    source = registry_with_tools()
    original = source.get_definitions()
    deferred = prepare_registry(source)
    initial = request_definitions(deferred)
    assert names(initial) == ["search_symbol", "read_file", "load_tools"]
    assert len(json.dumps(initial)) < len(json.dumps(original)) / 3
    assert source.get_definitions() == original
    assert "load_tools" not in source

    result = json.loads(deferred.execute("load_tools", {"names": ["probe_7", "probe_1", "probe_7"]}))
    assert result["loaded"] == ["probe_7", "probe_1"]
    assert all(not tool.calls for tool in source._tools.values())
    exposed = request_definitions(deferred)
    assert exposed[:len(initial)] == initial
    assert names(exposed)[-2:] == ["probe_7", "probe_1"]
    assert exposed[-1] == next(item for item in original if item["function"]["name"] == "probe_1")
    assert names(deferred.get_definitions()) == [*names(original), "load_tools"]


@pytest.mark.parametrize("bad", [[], "probe_1", [1], [None], ["probe_1", "missing"], [""]])
def test_invalid_load_is_atomic(bad):
    deferred = prepare_registry(registry_with_tools())
    initial = request_definitions(deferred)
    response = json.loads(deferred.execute("load_tools", {"names": bad}))
    assert response["status"] == "error"
    assert request_definitions(deferred) == initial


def test_loaded_state_is_isolated_between_questions_and_eager_mode(lazy_config):
    source = registry_with_tools()
    first = prepare_registry(source)
    first.expose(["probe_4"])
    second = prepare_registry(first)
    assert "probe_4" in names(request_definitions(first))
    assert "probe_4" not in names(request_definitions(second))
    assert first.source is source and second.source is source
    first.reset_exposure()
    assert request_definitions(first) == request_definitions(second)
    lazy_config.agent_tuning.vibe_trading_tool_loading = "eager"
    assert prepare_registry(first) is source


def test_small_fake_and_reserved_name_registries_retain_their_contract():
    small = registry_with_tools(15)
    assert prepare_registry(small) is small
    fake = SimpleNamespace(get_definitions=lambda: [{"type": "sentinel"}])
    assert prepare_registry(fake) is fake
    assert request_definitions(fake) == [{"type": "sentinel"}]
    assert request_definitions(SimpleNamespace()) == []
    collision = registry_with_tools()
    collision.register(ProbeTool("load_tools"))
    assert prepare_registry(collision) is collision
    with pytest.raises(ValueError, match="reserved"):
        DeferredToolRegistry(collision)


def test_source_dispatch_guard_is_never_bypassed_by_schema_loading():
    class GuardedRegistry(ToolRegistry):
        def execute(self, name, params):
            if tool := self.get(name):
                if not tool.is_readonly:
                    return json.dumps({"status": "error", "error": "write permission denied"})
            return super().execute(name, params)

    source = registry_with_tools(cls=GuardedRegistry)
    write = ProbeTool("place_order", readonly=False)
    source.register(write)
    deferred = prepare_registry(source)
    assert deferred.get("place_order") is write
    assert json.loads(deferred.execute("load_tools", {"names": ["place_order"]}))["status"] == "ok"
    assert json.loads(deferred.execute("place_order", {"value": "never"}))["error"] == "write permission denied"
    assert not write.calls
    # Deferred exposure is not a new execution allowlist: ordinary dispatch
    # retains the source behavior even when a model knows an unloaded schema.
    assert json.loads(deferred.execute("probe_3", {"value": "readonly"}))["value"] == "readonly"
    assert source.get("probe_3").calls == [{"value": "readonly"}]


def test_source_schema_filters_and_diagnostics_are_preserved():
    class FilteredRegistry(ToolRegistry):
        def get_definitions(self):
            return [item for item in super().get_definitions() if item["function"]["name"] != "probe_2"]

    source = registry_with_tools(cls=FilteredRegistry)
    source.record_import_failure("unavailable_tool", "missing optional dependency")
    deferred = prepare_registry(source)
    assert "probe_2" not in names(deferred.get_definitions())
    assert "probe_2:" not in deferred.catalog()
    assert deferred.expose(["probe_2"])["status"] == "error"
    assert deferred.import_failures == source.import_failures
    assert deferred.execute("unavailable", {}) == source.execute("unavailable", {})


def test_request_schemas_cannot_mutate_the_source_or_future_requests():
    source = registry_with_tools()
    deferred = prepare_registry(source)
    definitions = request_definitions(deferred)
    definitions[0]["function"]["parameters"]["required"].clear()
    assert request_definitions(deferred)[0]["function"]["parameters"]["required"] == ["value"]
    assert source.get("search_symbol").parameters["required"] == ["value"]


def test_concurrent_loading_keeps_all_names_once():
    source = registry_with_tools()
    deferred = prepare_registry(source)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(deferred.expose, [[f"probe_{i}", "probe_0"] for i in range(10)]))
    assert all(result["status"] == "ok" for result in results)
    exposed = names(request_definitions(deferred))
    assert all(f"probe_{i}" in exposed for i in range(10))
    assert len(exposed) == len(set(exposed))


def test_every_tool_remains_discoverable_in_context_without_its_arguments():
    source = registry_with_tools()
    deferred = prepare_registry(source)
    context = ContextBuilder(deferred, WorkspaceMemory())
    catalog = context._format_tool_descriptions()
    assert "load_tools(names=[...])" in catalog
    assert "Loading does not execute a tool or grant new permissions" in catalog
    for name in source.tool_names:
        assert f"- {name}:" in catalog
    assert "Argument " not in catalog
    assert "search_symbol" in names(request_definitions(deferred))


def test_real_registry_shrinks_requests_without_exposing_disabled_shell_tools():
    # Use the real discovered tool set and real contracts, without executing a
    # tool or contacting a provider. Do not replace registry dispatch with mocks.
    from src.tools import build_registry

    source = build_registry(include_shell_tools=False)
    assert len(source) >= 16
    deferred = prepare_registry(source)
    assert isinstance(deferred, DeferredToolRegistry)
    assert len(json.dumps(request_definitions(deferred))) < len(json.dumps(source.get_definitions())) / 2
    assert "bash" not in deferred.tool_names
    assert "bash" not in names(request_definitions(deferred))
    assert deferred.expose(["bash"])["status"] == "error"
    assert "load_tools" not in source.tool_names
    assert LoadToolsTool.check_available() is False
    catalog = deferred.catalog()
    assert all(f"- {name}:" in catalog or f"- {name}\n" in catalog for name in source.tool_names)
