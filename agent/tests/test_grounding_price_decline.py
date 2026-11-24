"""A sourced two-price decline is not a strategy's maximum drawdown."""
from __future__ import annotations

import json

import pytest

from src.agent.grounding import GroundingLedger


@pytest.fixture
def ledger(tmp_path):
    ledger = GroundingLedger(run_dir=tmp_path, user_message="分析 GLD.US、GC=F")
    ledger.ingest_tool_result(
        tool_name="get_market_data", arguments={"codes": ["GLD.US", "GC=F"]},
        result=json.dumps({
            "GLD.US": [
                {"trade_date": "2026-01-30", "high": 509.70, "close": 480.0},
                {"trade_date": "2026-02-27", "high": 490.0, "close": 483.75},
                {"trade_date": "2026-11-09", "close": 379.53},
                {"trade_date": "2026-11-10", "close": 369.3599853515625},
            ],
            "GC=F": [{"trade_date": "2026-11-10", "close": 3000.0}],
            "_provenance": {s: {"source": "yahoo"} for s in ("GLD.US", "GC=F")},
        }), call_id="quotes", success=True,
    )
    return ledger


@pytest.mark.parametrize("claim", [
    "GLD.US 自收盘高点回撤 **−23.65%**（369.36 / 483.75 − 1）。",
    "GLD.US 自收盘高点回撤 -23.65%（369.36 / 483.75 - 1）。",
    "GLD.US 自年内最高回撤 27.53%（369.36 / 509.70 − 1）。",
    "GLD.US 自收盘高点回撤（369.36 / 483.75 − 1）×100% = −23.65%。",
    "GLD.US drawdown from the closing high: -23.65% (369.36 / 483.75 - 1).",
    "GLD.US drawdown from the observed high: 27.53% (369.36 / 509.70 - 1).",
    "自收盘高点回撤 −23.65%（369.36 / 483.75 − 1）。",  # Unique same-symbol operands.
    "GLD.US 自收盘高点回撤 −23.6%（369.36 / 483.75 − 1）。",
    "GLD.US 当前价 369.36，自收盘高点回撤 −23.65%（369.36 / 483.75 − 1）。",
    "高点依次下移。自收盘高点回撤−23.65%（369.36 / 483.75 − 1）。10-30见底后开始反弹。",
    "２０２５－０１－０１高点依次下移。自收盘高点回撤−２３．６５％（３６９．３６／４８３．７５−１）。１０－３０见底后开始反弹。",
    "别的日期１０－３０见底。GLD.US自收盘高点回撤−２３．６５％（３６９．３６／４８３．７５−１）。２０２５－１１－１１发生别的事件。",
    "GLD.US ２０２６－１１－１０当前价３６９．３６，自收盘高点回撤−２３．６５％（３６９．３６／４８３．７５−１）。２０２５－１１－０９另有事件。",
    "GLD.US 2026-02-27 高点 483.75，2026-11-10 当前价 369.36，自收盘高点回撤 −23.65%（369.36 / 483.75 − 1）。",
    "GLD.US 高点 483.75（2026-02-27），当前价 369.36（2026-11-10），自收盘高点回撤 −23.65%（369.36 / 483.75 − 1）。",
    "GLD.US 02-27高点 483.75，11-10收盘 369.36，自收盘高点回撤 −23.65%（369.36 / 483.75 − 1）。",
    "| 指标 | 数值 |\n|---|---|\n| GLD.US 自收盘高点回撤 | −23.65%（369.36 / 483.75 − 1） |",
    "| 指标 | 说明 |\n|---|---|\n| GLD.US 自收盘高点回撤 −23.65%（369.36 / 483.75 − 1） | 数据来源 yahoo |",
])
def test_observed_price_declines_pass_without_backtest(ledger, claim):
    assert ledger._validate_analysis_claims(claim) == []


@pytest.mark.parametrize("claim,detail", [
    ("GLD.US 自收盘高点483.75回撤−23.65%", "explicit_current_and_high_ratio_required"),
    ("基准价 GLD.US 369.36。\n自收盘高点483.75回撤−23.65%", "explicit_current_and_high_ratio_required"),
    ("GLD.US 自收盘高点回撤−22.65%（369.36 / 483.75 − 1）", "decline_percentage_does_not_match_displayed_inputs"),
    ("GLD.US 自收盘高点回撤+23.65%（369.36 / 483.75 − 1）", "decline_percentage_does_not_match_displayed_inputs"),
    ("GLD.US 自收盘高点回撤−23.65%（483.75 / 369.36 − 1）", "price_decline_requires_positive_current_not_above_high"),
    ("GLD.US 自收盘高点回撤−23.65%（369.36 / 483.75 − 1）*2", "unsupported_price_decline_formula"),
    ("GLD.US 自收盘高点回撤−23.65%（369.36 / 483.75 − 1）×100+2", "unsupported_price_decline_formula"),
    ("GLD.US 自收盘高点回撤−23.65%（2*369.36 / 483.75 − 1）", "unsupported_price_decline_formula"),
    ("GLD.US 自收盘高点回撤−23.65%（369.36 / 483.75 − 1）和−40%", "one_explicit_decline_percentage_required"),
    ("GLD.US 自收盘高点回撤−23.65%（369.36 / 483.75 − 1）另回撤44.4", "additional_unverified_drawdown_measurement"),
    ("GC=F 自收盘高点回撤−23.65%（369.36 / 483.75 − 1）", "current_and_high_need_observations_for_one_same_instrument"),
    ("GLD.US 与 GC=F 自高点回撤−23.65%（369.36 / 483.75 − 1）", "price_decline_instrument_is_ambiguous"),
    ("GLD.US 自收盘高点回撤−27.53%（369.36 / 509.70 − 1）", "current_and_high_need_observations_for_one_same_instrument"),
    ("GLD.US 2025-02-27 高点 483.75，2026-11-10 当前价 369.36，自收盘高点回撤−23.65%（369.36 / 483.75 − 1）", "price_input_date_has_no_matching_observation"),
    ("GLD.US 2026-02-27 高点 483.75，2026-11-09 当前价 369.36，自收盘高点回撤−23.65%（369.36 / 483.75 − 1）", "price_input_date_has_no_matching_observation"),
    ("GLD.US 2026-11-1 当前价 369.36，自收盘高点回撤−23.65%（369.36 / 483.75 − 1）", "price_input_date_has_no_matching_observation"),
    ("GLD.US 高点 483.75（2026-11-10），当前价 369.36，自收盘高点回撤−23.65%（369.36 / 483.75 − 1）", "price_input_date_has_no_matching_observation"),
    ("GLD.US 11-09收盘 369.36，自收盘高点回撤−23.65%（369.36 / 483.75 − 1）", "price_input_date_has_no_matching_observation"),
    ("GLD.US ２０２６－１１－０９当前价３６９．３６，自收盘高点回撤−２３．６５％（３６９．３６／４８３．７５−１）。２０２６－１１－１０另有事件。", "price_input_date_has_no_matching_observation"),
])
def test_price_decline_needs_its_own_explicit_correct_inputs(ledger, claim, detail):
    issues = ledger._validate_analysis_claims(claim)
    assert len(issues) == 1
    assert issues[0]["kind"] == "drawdown"
    assert issues[0]["detail"] == detail
    assert "current / high - 1" in issues[0]["message"]


@pytest.mark.parametrize("claim", [
    "GLD.US 最大回撤−23.65%（369.36 / 483.75 − 1）",
    "GLD.US 策略自收盘高点回撤−23.65%（369.36 / 483.75 − 1）",
    "GLD.US 投资组合自高点回撤−23.65%（369.36 / 483.75 − 1）",
    "GLD.US 策略净值， 自高点回撤−23.65%（369.36 / 483.75 − 1）",
    "GLD.US maximum drawdown from the closing high: −23.65% (369.36 / 483.75 − 1)",
    "GLD.US portfolio drawdown from the high: 23.65% (369.36 / 483.75 - 1)",
    "GLD.US 自收盘高点回撤−23.65%（369.36 / 483.75 − 1）；最大回撤23.65%",
    "GLD.US 自收盘高点回撤−23.65%（369.36 / 483.75 − 1）；波动率23.65%",
])
def test_price_decline_never_certifies_a_path_statistic_or_sibling_metric(ledger, claim):
    assert ledger._validate_analysis_claims(claim)


def test_matching_operands_cannot_be_borrowed_from_two_symbols(ledger):
    claim = "自高点回撤 87.69%（369.36 / 3000.0 − 1）"
    issues = ledger._validate_analysis_claims(claim)
    assert issues[0]["detail"] == "current_and_high_need_observations_for_one_same_instrument"


def test_operand_identity_must_be_unique_if_no_instrument_is_written(ledger):
    ledger.ingest_tool_result(
        tool_name="get_market_data", arguments={"codes": ["OTHER.US"]},
        result=json.dumps({"OTHER.US": [
            {"trade_date": "2026-01-01", "close": 483.75},
            {"trade_date": "2026-11-10", "close": 369.36},
        ]}), call_id="unrelated", success=True,
    )
    issues = ledger._validate_analysis_claims("自高点回撤−23.65%（369.36 / 483.75 − 1）")
    assert issues[0]["detail"] == "current_and_high_need_observations_for_one_same_instrument"


def test_same_line_separate_highs_are_checked_individually(ledger):
    claim = ("GLD.US 自收盘高点回撤−23.65%（369.36 / 483.75 − 1）；"
             "自年内最高回撤−20.53%（369.36 / 509.70 − 1）")
    issues = ledger._validate_analysis_claims(claim)
    assert len(issues) == 1
    assert "20.53" in issues[0]["claim"]


@pytest.mark.parametrize("explicit_dates", [True, False])
def test_later_high_cannot_ground_an_earlier_decline(tmp_path, explicit_dates):
    ledger = GroundingLedger(run_dir=tmp_path, user_message="GLD.US")
    ledger.ingest_tool_result(
        tool_name="get_market_data", arguments={"codes": ["GLD.US"]},
        result=json.dumps({"GLD.US": [
            {"trade_date": "2026-11-11", "close": 100.0},
            {"trade_date": "2026-11-12", "close": 200.0},
        ]}), call_id="reversed", success=True,
    )
    prefix = "GLD.US 2026-11-12 高点200，2026-11-11 当前100，" if explicit_dates else "GLD.US "
    issues = ledger._validate_analysis_claims(prefix + "自收盘高点回撤−50%（100 / 200 − 1）")
    assert issues[0]["detail"] == "observed_high_must_not_follow_current_price_date"
