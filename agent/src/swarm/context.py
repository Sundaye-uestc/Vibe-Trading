"""Bound worker prompts without removing observations before they are seen."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from src.agent.context_budget import CompactionBudget, ContextMeter, estimate_tokens, parse_context_limit, resolve_window
from src.agent.result_artifacts import readable_result_workspace, save_result_parts
from src.config.accessor import get_env_config
from src.tools.redaction import redact_tool_result

logger = logging.getLogger(__name__)
_OFFLOADED = "[Worker context result saved: "


class WorkerContext:
    """Measure exact exposed schemas and preserve each fresh tool batch.

    A result is eligible for offloading only after a successful model request
    has received it. Offloading creates readable, redacted parts rather than
    replacing evidence with a bare ``[cleared]`` marker.
    """

    def __init__(self, *, model: str, artifact_dir: Path, registry, max_tokens_override: int | None = None):
        cfg = get_env_config()
        self.window, self.window_source = resolve_window(
            cfg.llm.langchain_provider, model or cfg.llm.langchain_model_name,
            env_window=cfg.agent_tuning.vibe_trading_context_window,
        )
        self.max_tokens = cfg.agent_tuning.vibe_trading_context_max_tokens
        if max_tokens_override is not None:
            self.max_tokens = max(1, min(self.max_tokens, int(max_tokens_override)))
        self.meter = ContextMeter()
        self.readable_root = readable_result_workspace(artifact_dir, registry)
        self.pending: set[str] = set()

    def budget(self, messages: list[dict], tools: list[dict] | None) -> CompactionBudget:
        return CompactionBudget(
            window=self.window, source=self.window_source, max_tokens=self.max_tokens,
            static_tokens=self.meter.static(messages, estimate_tokens(tools or [])),
        )

    def tokens(self, messages: list[dict], tools: list[dict] | None) -> int:
        return self.meter.tokens(messages, estimate_tokens(tools or []))

    def observe(self, messages: list[dict], tools: list[dict] | None, usage: dict | None) -> None:
        """Anchor a successful request before appending its answer/new schemas."""
        input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        self.meter.observe(input_tokens, messages, tool_schema_tokens=estimate_tokens(tools or []))
        self.pending.clear()

    def learn_limit(self, error: str) -> None:
        """A provider-reported limit can tighten, never enlarge, this run."""
        limit = parse_context_limit(error)
        if limit is not None and limit < self.window:
            self.window, self.window_source = limit, "learned:context_length_error"

    def compact(self, messages: list[dict], tools: list[dict] | None, *, force: bool = False) -> list[dict[str, Any]]:
        """Move old observations to parts only when doing so reduces pressure."""
        budget = self.budget(messages, tools)
        target = budget.micro_at
        if force:
            target = min(target, self.tokens(messages, tools) // 2)
        if self.readable_root is None or self.tokens(messages, tools) <= target:
            return []
        root = self.readable_root
        receipts = []
        for message in messages:
            if self.tokens(messages, tools) <= target:
                break
            content = message.get("content")
            call_id = message.get("tool_call_id")
            if (message.get("role") != "tool" or call_id in self.pending
                    or not isinstance(content, str) or content.startswith(_OFFLOADED)
                    or len(content) < 128):
                continue
            cleaned = redact_tool_result(content)
            digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
            try:
                paths = save_result_parts(cleaned, root / "context-results" / digest, root)
            except (OSError, ValueError, UnicodeError):
                logger.warning("Could not preserve worker context result", exc_info=True)
                continue
            marker = (
                f"{_OFFLOADED}{json.dumps(paths, ensure_ascii=False)}] "
                "This observation was already returned and remains available. "
                "Read these parts in order with the local file tools; do not refetch external data "
                "only because its earlier result moved out of context."
            )
            if len(marker) >= len(content):
                continue
            message["content"] = marker
            receipts.append({"tool_call_id": call_id, "paths": paths, "characters": len(cleaned)})
        return receipts
