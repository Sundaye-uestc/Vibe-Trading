"""Measure model context and keep every compaction threshold inside its budget.

The provider's reported input count anchors the last request. Until the next
response arrives, estimated changes in messages and tool schemas account for
growth, pruning and newly loaded tools. These estimates schedule compaction;
they are deliberately separate from billing/goal usage accounting.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

_CATALOG_PATH = Path(__file__).resolve().parents[1] / "providers" / "context_windows.json"

USABLE_FRACTION = 0.8
MICRO_FRACTION = 0.8
COLLAPSE_FRACTION = 0.9

_LIMIT_RE = re.compile(
    r"(?:maximum context length|context length|context window|token limit|maximum of)"
    r"\s*(?:\(tokens?\)\s*)?(?:(?:is|of)\s+|[:=]\s*)?(\d[\d,]{3,})",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    return json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))


def _token_count(value: Any, name: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a {qualifier} integer")
    return value


def estimate_tokens(value: Any) -> int:
    """Estimate JSON tokens without treating CJK text as four chars/token.

    ASCII is approximated at four characters per token; each non-ASCII code
    point counts as one. This remains an estimate, not a tokenizer or a usage
    bill, and is replaced by a provider count as soon as one is observed.
    """
    serialized = json.dumps(value, default=str, ensure_ascii=False)
    non_ascii = sum(ord(char) > 127 for char in serialized)
    return (len(serialized) - non_ascii + 3) // 4 + non_ascii


def resolve_window(
    provider: str,
    model: str,
    *,
    env_window: int | None = None,
    learned_window: int | None = None,
) -> tuple[int, str]:
    """Resolve override, observed limit, documented model window, then default.

    Invalid explicit values are rejected rather than silently enlarging a
    requested budget. Legacy ``TOKEN_THRESHOLD`` handling belongs to the
    calling loop; this module never reads it from the process environment.
    """
    if env_window is not None:
        return _token_count(env_window, "env_window"), "env:VIBE_TRADING_CONTEXT_WINDOW"
    if learned_window is not None:
        return _token_count(learned_window, "learned_window"), "learned:context_length_error"
    catalog = _catalog()
    provider = (provider or "").strip().lower().replace("_", "-")
    model = (model or "").strip()
    for entry in catalog["models"]:
        if entry.get("provider") and entry["provider"] != provider:
            continue
        if re.search(entry["pattern"], model, re.IGNORECASE):
            return _token_count(entry["window"], "catalog window"), f"catalog:{entry['pattern']}"
    return _token_count(catalog["default_window"], "default window"), "default"


def is_context_overflow(error_text: str) -> bool:
    """Recognise context errors without mistaking a spending limit for one."""
    lowered = (error_text or "").lower()
    return any(pattern in lowered for pattern in _catalog()["overflow_error_patterns"])


def parse_context_limit(error_text: str) -> int | None:
    """Read an explicit model limit, not the larger requested-token count."""
    match = _LIMIT_RE.search(error_text or "")
    if not match:
        return None
    value = int(match.group(1).replace(",", ""))
    return value if value >= 1_000 else None


@dataclass(frozen=True)
class CompactionBudget:
    """Prompt thresholds after reserving reply space and applying a cost cap.

    ``static_tokens`` includes the leading system prompt and exposed schemas.
    There is no minimum conversation allowance: granting one when the static
    prompt already fills the window would silently defeat the provider limit.
    ``static_overflow`` lets callers report that compaction cannot make enough
    room by editing conversation history alone.
    """

    window: int
    source: str
    max_tokens: int
    static_tokens: int

    def __post_init__(self) -> None:
        _token_count(self.window, "window")
        _token_count(self.max_tokens, "max_tokens")
        _token_count(self.static_tokens, "static_tokens", allow_zero=True)

    @property
    def total(self) -> int:
        return min(int(self.window * USABLE_FRACTION), self.max_tokens)

    @property
    def conversation(self) -> int:
        return max(0, self.total - self.static_tokens)

    @property
    def static_overflow(self) -> bool:
        return self.static_tokens >= self.total

    @property
    def micro_at(self) -> int:
        return min(self.total, self.static_tokens + int(self.conversation * MICRO_FRACTION))

    @property
    def collapse_at(self) -> int:
        return min(self.total, self.static_tokens + int(self.conversation * COLLAPSE_FRACTION))

    @property
    def compact_at(self) -> int:
        return self.total


class ContextMeter:
    """Anchor prompt sizing to reported input while tracking subsequent edits.

    Call ``observe`` with the exact messages and schema count used for the
    request that just completed, before appending its answer or loading tools.
    A new meter should be created for each independent question/model.
    """

    def __init__(self, estimator: Callable[[Any], int] = estimate_tokens) -> None:
        self._estimate = estimator
        self._anchor: tuple[int, int] | None = None
        self._anchor_schema_tokens: int | None = None
        self.static_tokens: int | None = None
        self._static_schema_tokens: int | None = None
        self._static_message_tokens: int | None = None

    def _estimated(self, value: Any) -> int:
        return _token_count(self._estimate(value), "estimated tokens", allow_zero=True)

    def observe(self, input_tokens: int | None, messages: list, *, tool_schema_tokens: int | None = None) -> None:
        """Record provider-reported input, ignoring missing/invalid usage."""
        if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens <= 0:
            return
        if tool_schema_tokens is not None:
            _token_count(tool_schema_tokens, "tool_schema_tokens", allow_zero=True)
        self._anchor = (input_tokens, self._estimated(messages))
        self._anchor_schema_tokens = tool_schema_tokens
        if self.static_tokens is None:
            body_tokens = self._estimated(messages[1:]) if messages[1:] else 0
            self.static_tokens = max(0, input_tokens - body_tokens)
            self._static_schema_tokens = tool_schema_tokens
            self._static_message_tokens = self._estimated(messages[:1])

    def tokens(self, messages: list, tool_schema_tokens: int) -> int:
        """Estimate the next request including changes in exposed schemas."""
        _token_count(tool_schema_tokens, "tool_schema_tokens", allow_zero=True)
        if self._anchor is None:
            return self._estimated(messages) + tool_schema_tokens
        reported, estimated = self._anchor
        schema_delta = 0 if self._anchor_schema_tokens is None else tool_schema_tokens - self._anchor_schema_tokens
        return max(0, reported + self._estimated(messages) - estimated + schema_delta)

    def static(self, messages: list, tool_schema_tokens: int) -> int:
        """Size static context, adjusting measured size for prefix/schema edits."""
        _token_count(tool_schema_tokens, "tool_schema_tokens", allow_zero=True)
        if self.static_tokens is None:
            return self._estimated(messages[:1]) + tool_schema_tokens
        schema_delta = 0 if self._static_schema_tokens is None else tool_schema_tokens - self._static_schema_tokens
        prefix_delta = self._estimated(messages[:1]) - (self._static_message_tokens or 0)
        return max(0, self.static_tokens + schema_delta + prefix_delta)
