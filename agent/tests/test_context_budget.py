"""Context accounting includes schemas without exceeding model/cost limits."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from src.agent.context_budget import (
    CompactionBudget,
    ContextMeter,
    _catalog,
    estimate_tokens,
    is_context_overflow,
    parse_context_limit,
    resolve_window,
)
from src.config.env_schema import AgentTuningConfig


@pytest.mark.parametrize("model", [
    "deepseek-flash", "deepseek/deepseek-flash", "DEEPSEEK-FLASH",
    "deepseek-v4.1-flash", "deepseek-v4-1-flash-1017", "deepseek-v4-pro",
])
def test_documented_deepseek_aliases_use_the_exact_window(model):
    assert resolve_window("deepseek", model)[0] == 1_048_576


def test_scoped_catalog_and_unknown_model_fallback():
    assert resolve_window("openai-codex", "openai-codex/gpt-6-sol")[0] == 272_000
    assert resolve_window("openai_codex", "gpt-6-sol")[0] == 272_000
    assert resolve_window("openai", "gpt-6-sol") == (128_000, "default")
    assert resolve_window("custom", "unrecognised-model") == (128_000, "default")


def test_explicit_then_learned_limit_override_a_catalog_entry():
    assert resolve_window("deepseek", "deepseek-flash", env_window=64_000, learned_window=32_000) == (
        64_000, "env:VIBE_TRADING_CONTEXT_WINDOW",
    )
    assert resolve_window("deepseek", "deepseek-flash", learned_window=32_000) == (
        32_000, "learned:context_length_error",
    )


@pytest.mark.parametrize("value", [0, -1, True, "32000", 32000.5, float("nan"), float("inf")])
@pytest.mark.parametrize("argument", ["env_window", "learned_window"])
def test_invalid_explicit_or_learned_limit_is_not_silently_ignored(argument, value):
    with pytest.raises(ValueError, match="positive integer"):
        resolve_window("deepseek", "deepseek-flash", **{argument: value})


def test_catalog_is_packaged_data_with_attributed_patterns():
    for entry in _catalog()["models"]:
        assert entry["source"]
        assert entry["window"] >= 1_000
        re.compile(entry["pattern"])


def test_legacy_threshold_does_not_change_window_resolution(monkeypatch):
    monkeypatch.setenv("TOKEN_THRESHOLD", "1")
    assert resolve_window("deepseek", "deepseek-flash")[0] == 1_048_576


def test_large_healthy_research_prompt_is_preserved_under_the_cost_cap():
    budget = CompactionBudget(window=1_048_576, source="test", max_tokens=200_000, static_tokens=48_000)
    assert 154_000 < budget.micro_at < budget.collapse_at < budget.compact_at
    assert budget.total == 200_000
    assert not budget.static_overflow


@pytest.mark.parametrize("window,static,cap", [
    (128_000, 48_000, 200_000), (32_000, 48_000, 200_000),
    (4_096, 2_000, 200_000), (1_000, 900, 200_000),
    (1_048_576, 10_000, 5_000), (1, 0, 200_000),
])
def test_small_windows_and_large_static_prompts_never_expand_the_budget(window, static, cap):
    budget = CompactionBudget(window=window, source="test", max_tokens=cap, static_tokens=static)
    assert 0 <= budget.micro_at <= budget.collapse_at <= budget.compact_at <= min(window * 0.8, cap)
    if static >= budget.total:
        assert budget.conversation == 0
        assert budget.static_overflow
    else:
        assert budget.conversation + static == budget.total


@pytest.mark.parametrize("field,value", [
    ("window", 0), ("window", True), ("window", float("inf")),
    ("max_tokens", -1), ("max_tokens", "1000"),
    ("static_tokens", -1), ("static_tokens", False),
])
def test_bad_budget_values_fail_before_request_sizing(field, value):
    args = {"window": 128_000, "source": "test", "max_tokens": 200_000, "static_tokens": 1_000}
    args[field] = value
    with pytest.raises(ValueError):
        CompactionBudget(**args)


def test_chinese_text_is_not_estimated_as_four_characters_per_token():
    assert estimate_tokens("中" * 400) >= 400
    assert estimate_tokens("中" * 400) > 3 * estimate_tokens("a" * 400)
    assert estimate_tokens({"tool": {"schema": "s" * 4000}}) > estimate_tokens({})


def test_provider_anchor_tracks_growth_pruning_and_schema_loading():
    initial = [{"role": "system", "content": "system"}, {"role": "user", "content": "compare filings"}]
    meter = ContextMeter()
    assert meter.tokens(initial, 1_000) == estimate_tokens(initial) + 1_000
    meter.observe(50_000, initial, tool_schema_tokens=1_000)
    assert meter.tokens(initial, 1_000) == 50_000
    assert meter.tokens(initial, 4_000) == 53_000
    assert meter.tokens(initial, 0) == 49_000
    grown = initial + [{"role": "tool", "content": "filing " * 1000}]
    assert meter.tokens(grown, 4_000) > 53_000
    assert meter.tokens(initial[:1], 1_000) < 50_000
    # The real prompt includes the newly exposed schemas; observing it must
    # not add those schemas for a second time on the next request.
    meter.observe(58_000, grown, tool_schema_tokens=4_000)
    assert meter.tokens(grown, 4_000) == 58_000
    assert meter.tokens(grown, 1_000) == 55_000


def test_static_measurement_tracks_changed_system_prefix_and_schemas():
    original = [{"role": "system", "content": "short"}, {"role": "user", "content": "question"}]
    meter = ContextMeter()
    meter.observe(10_000, original, tool_schema_tokens=2_000)
    baseline = meter.static(original, 2_000)
    edited = [{"role": "system", "content": "longer system prefix " * 100}, original[1]]
    assert meter.static(edited, 2_000) > baseline
    assert meter.static(original, 5_000) == baseline + 3_000
    # Modifying the caller's list cannot mutate the stored calibration.
    original.append({"role": "tool", "content": "new observation"})
    assert meter.static(original, 2_000) == baseline


def test_missing_usage_does_not_erase_a_valid_anchor():
    meter = ContextMeter()
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for invalid in (None, 0, -1, True, "900", float("nan")):
        meter.observe(invalid, messages, tool_schema_tokens=100)
        assert meter.static_tokens is None
    meter.observe(700, messages, tool_schema_tokens=100)
    for invalid in (None, 0, -1, True, "900"):
        meter.observe(invalid, messages + [{"role": "user", "content": "ignored"}], tool_schema_tokens=200)
    assert meter.tokens(messages, 100) == 700


def test_zero_schema_count_is_valid_and_invalid_schema_counts_are_rejected():
    meter = ContextMeter()
    meter.observe(800, [{"role": "system", "content": "system"}], tool_schema_tokens=0)
    assert meter.static_tokens == 800
    assert meter.tokens([{"role": "system", "content": "system"}], 0) == 800
    for invalid in (-1, True, "100", float("nan")):
        with pytest.raises(ValueError):
            meter.tokens([], invalid)
        with pytest.raises(ValueError):
            meter.static([], invalid)


@pytest.mark.parametrize("text,limit", [
    ("This model's maximum context length is 131072 tokens; you requested 140000 tokens.", 131072),
    ("context window: 1,048,576 tokens; requested 1,090,000", 1048576),
    ("exceeded model token limit of 32,000; received 35000", 32000),
    ("context_length_exceeded", None),
    ("prompt is too long: 210000 tokens > 200000 maximum", None),
    ("context window is too small for the requested 140000 tokens", None),
])
def test_context_error_detection_and_safe_limit_parsing(text, limit):
    assert is_context_overflow(text)
    assert parse_context_limit(text) == limit


@pytest.mark.parametrize("text", ["Insufficient Balance", "usage limit reached", "rate limit exceeded", ""])
def test_unrelated_errors_do_not_trigger_context_compaction(text):
    assert not is_context_overflow(text)
    assert parse_context_limit(text) is None


def test_new_environment_controls_and_defaults(monkeypatch):
    for key in ("VIBE_TRADING_TOOL_LOADING", "VIBE_TRADING_CONTEXT_WINDOW", "VIBE_TRADING_CONTEXT_MAX_TOKENS"):
        monkeypatch.delenv(key, raising=False)
    config = AgentTuningConfig()
    assert config.vibe_trading_tool_loading == "lazy"
    assert config.vibe_trading_context_window is None
    assert config.vibe_trading_context_max_tokens == 200_000
    monkeypatch.setenv("VIBE_TRADING_TOOL_LOADING", "eager")
    monkeypatch.setenv("VIBE_TRADING_CONTEXT_WINDOW", "32768")
    monkeypatch.setenv("VIBE_TRADING_CONTEXT_MAX_TOKENS", "20000")
    config = AgentTuningConfig()
    assert config.vibe_trading_tool_loading == "eager"
    assert config.vibe_trading_context_window == 32_768
    assert config.vibe_trading_context_max_tokens == 20_000


@pytest.mark.parametrize("values", [
    {"VIBE_TRADING_TOOL_LOADING": "unknown"},
    {"VIBE_TRADING_CONTEXT_WINDOW": 0},
    {"VIBE_TRADING_CONTEXT_WINDOW": -1},
    {"VIBE_TRADING_CONTEXT_WINDOW": "invalid"},
    {"VIBE_TRADING_CONTEXT_WINDOW": True},
    {"VIBE_TRADING_CONTEXT_MAX_TOKENS": 0},
    {"VIBE_TRADING_CONTEXT_MAX_TOKENS": -1},
    {"VIBE_TRADING_CONTEXT_MAX_TOKENS": True},
])
def test_bad_context_configuration_is_rejected(values):
    with pytest.raises(ValidationError):
        AgentTuningConfig(**values)
