"""Expose tool argument schemas on demand without changing execution policy."""

from __future__ import annotations

import copy
import json
import logging
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any

from src.agent.tools import BaseTool, ToolRegistry

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def loading_policy() -> dict[str, Any]:
    """Read the packaged discovery policy shared by main and swarm runs."""
    return json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))


class DeferredToolRegistry(ToolRegistry):
    """Keep per-question schema exposure separate from the source registry.

    Source tool instances and their guards are retained. Normal execution is
    delegated to the source registry, including custom ``execute`` checks;
    loading schemas never bypasses or grants an execution permission.
    """

    def __init__(self, source: ToolRegistry) -> None:
        super().__init__()
        while isinstance(source, DeferredToolRegistry):
            source = source.source
        if "load_tools" in source:
            raise ValueError("load_tools is reserved for run-local schema discovery")
        self.source = source
        self._tools = dict(source._tools)
        self._import_failures = dict(source.import_failures)
        self._registration_failures = dict(source.registration_failures)
        self._lock = RLock()
        self._loader = LoadToolsTool(self)
        self.register(self._loader)
        self.reset_exposure()

    def _source_definitions(self) -> dict[str, dict[str, Any]]:
        # Respect schema customizations/filtering on source registries too.
        # Only schema exposure is deferred, not the source's public contract.
        definitions = {}
        for definition in self.source.get_definitions():
            function = definition.get("function", {})
            name = function.get("name")
            if isinstance(name, str) and name in self._tools and name != "load_tools":
                definitions[name] = definition
        return definitions

    def get_definitions(self) -> list[dict[str, Any]]:
        """Return the full manifest-facing catalog, regardless of loaded state."""
        return [*self.source.get_definitions(), self._loader.to_openai_schema()]

    def reset_exposure(self) -> None:
        """Start a new question with only the initial discovery/file tools."""
        with self._lock:
            available = self._source_definitions()
            self._loaded = dict.fromkeys([
                *(name for name in loading_policy()["initial_tools"] if name in available),
                "load_tools",
            ])

    def request_definitions(self) -> list[dict[str, Any]]:
        """Return exact loaded schemas, keeping their cache-friendly order."""
        with self._lock:
            definitions = self._source_definitions()
            definitions["load_tools"] = self._loader.to_openai_schema()
            return [copy.deepcopy(definitions[name]) for name in self._loaded if name in definitions]

    def catalog(self) -> str:
        """List every available tool by name and a bounded discovery hint."""
        limit = loading_policy()["catalog_description_chars"]
        lines = []
        for name, definition in self._source_definitions().items():
            hint = " ".join(str(definition["function"].get("description") or "").split())
            if len(hint) > limit:
                hint = hint[:limit].rstrip() + "…"
            lines.append(f"- {name}: {hint}" if hint else f"- {name}")
        return "\n".join(lines)

    def expose(self, names: list[str]) -> dict[str, Any]:
        """Atomically load known names without executing their tools."""
        if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names):
            return {"status": "error", "error": "names must be a non-empty list of exact tool names"}
        with self._lock:
            available = self._source_definitions()
            available["load_tools"] = self._loader.to_openai_schema()
            unknown = list(dict.fromkeys(name for name in names if name not in available))
            if unknown:
                return {"status": "error", "unknown_tools": unknown, "available_tools": list(available)}
            loaded = list(dict.fromkeys(names))
            self._loaded.update(dict.fromkeys(loaded))
        return {
            "status": "ok",
            "loaded": loaded,
            "message": (
                "Full schemas are available in the next request. Request these tools through "
                "the host's tool-call protocol in your next reply. Loading has not executed them."
            ),
        }

    def execute(self, name: str, params: dict[str, Any]) -> str:
        """Execute ordinary calls through the source's original dispatch/guards."""
        if name == "load_tools":
            return super().execute(name, params)
        return self.source.execute(name, params)


class LoadToolsTool(BaseTool):
    """Run-local schema discovery; deliberately absent from global discovery."""

    name = "load_tools"
    description = (
        "Load full argument schemas for exact names in the complete tool catalog. "
        "Select all tools needed for the next steps together, then request them through "
        "the host's tool-call protocol in the next turn. This does not execute any tool."
    )
    parameters = {
        "type": "object",
        "properties": {"names": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
        "required": ["names"],
    }
    repeatable = True

    @classmethod
    def check_available(cls) -> bool:
        return False

    def __init__(self, registry: DeferredToolRegistry) -> None:
        self.registry = registry

    def execute(self, names: list[str], **kwargs: Any) -> str:
        return json.dumps(self.registry.expose(names), ensure_ascii=False)


def prepare_registry(registry: ToolRegistry) -> ToolRegistry:
    """Create fresh run-local exposure for sufficiently large lazy registries.

    Eager mode and small/fake registries retain their existing API and dispatch.
    Calling this again for a new question never shares the previous loaded set.
    """
    from src.config.accessor import get_env_config

    source = registry.source if isinstance(registry, DeferredToolRegistry) else registry
    if not isinstance(source, ToolRegistry):
        return registry
    mode = getattr(get_env_config().agent_tuning, "vibe_trading_tool_loading", "lazy")
    if mode != "lazy" or len(source) < loading_policy()["minimum_tools"]:
        return source
    if "load_tools" in source:
        logger.warning("Keeping eager tool schemas because the registry already defines load_tools")
        return source
    return DeferredToolRegistry(source)


def request_definitions(registry: ToolRegistry) -> list[dict[str, Any]]:
    """Return request-facing schemas while preserving the full registry API."""
    if isinstance(registry, DeferredToolRegistry):
        return registry.request_definitions()
    definitions = getattr(registry, "get_definitions", None)
    return definitions() if callable(definitions) else []
