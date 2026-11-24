"""Metric types and clause boundaries must survive prose/table formatting."""
from __future__ import annotations

import json

import pytest

from src.agent.grounding import GroundingLedger


@pytest.fixture
def ledger(tmp_path):
    result = GroundingLedger(run_dir=tmp_path, user_message="分析 GC=F、GLD.US、^TNX、^VIX")
    result.ingest_tool_result(
        tool_name="get_market_data", arguments={"codes": ["GC=F", "^TNX", "^VIX"]},
        result=json.dumps({
            "GC=F": [{"trade_date": "2026-11-11", "open": 4020.0, "high": 4030.0,
                      "low": 4000.0, "close": 4024.0}],
            "^TNX": [{"trade_date": "2026-11-10", "close": 4.7},
                     {"trade_date": "2026-11-11", "close": 4.998000144958496}],
            "^VIX": [{"trade_date": "2026-11-11", "close": 18.44}],
            "_provenance": {symbol: {"source": "yahoo"} for symbol in ("GC=F", "^TNX", "^VIX")},
        }), call_id="quotes", success=True,
    )
    result.ingest_tool_result(
        tool_name="technical_indicators", arguments={"symbol": "^TNX"},
        result=json.dumps({"ok": True, "symbol": "^TNX", "latest_close": 4.998000144958496,
                           "latest_date": "2026-11-11",
                           "read_identity": {"provenance": {"source": "yahoo"}},
                           "indicators": {"rsi_14": 50.14, "bollinger": {"upper": 5.18, "lower": 4.8}}}),
        call_id="tnx-indicators", success=True,
    )
    return result


@pytest.mark.parametrize("text", [
    "| 情景 | 触发条件 | 路径 |\n|---|---|---|\n| 偏多 | 收复并站稳布林上轨 4,219.1；美债收益率回落至其布林下轨下方；出现实物买盘 | 向上修复，参照 200 日线 4,486.6 |",
    "收复并站稳布林上轨 4,219.1；美债收益率回落至其布林下轨下方。",
    "| 因子 | 状态 |\n|---|---|\n| ^TNX 10年美债收益率 | 已取得序列（RSI 50.14，20 日均线附近）——数值本次不引用 |",
])
def test_bond_yield_words_do_not_relabel_gold_levels_or_rsi_as_returns(ledger, text):
    # This gate must not claim the neighbouring levels are measured returns.
    # Validation of the levels themselves belongs to their own evidence gate.
    assert ledger._validate_analysis_claims(text) == []


@pytest.mark.parametrize("text", [
    "10年美债收益率 ^TNX：4.998%。",
    "10年美债利率：4.998%。",
    "10-year Treasury yield: 4.998%.",
    "^TNX yield: 4.998%.",
    "| 指标 | 数值 |\n|---|---|\n| 10年美债收益率 ^TNX | 4.998% |",
    "| Value | Metric |\n|---|---|\n| 4.998% | 10-year Treasury yield |",
    "| ^TNX 10年美债收益率 |\n|---|\n| 4.998% |",
    "2026-11-10 的10年美债收益率 ^TNX 为 4.7%。",
])
def test_treasury_yields_are_observed_percentage_point_quotes(ledger, text):
    assert ledger._validate_analysis_claims(text) == []


@pytest.mark.parametrize("text", [
    "10年美债收益率 ^TNX：8.5%。",
    "10年美债收益率 ^TNX：50.14%。",  # Same symbol's RSI is not a yield.
    "10年美债收益率 ^TNX：18.44%。",  # Another index's close is not a yield.
    "10年美债收益率 ^TNX：0.04998%。",  # No percent/fraction double conversion.
    "10-year Treasury yield: 499.8%.",
    "2026-11-10 的10年美债收益率 ^TNX 为 4.998%。",
    "2年美债收益率为 4.998%。",
    "日本10年国债收益率为 4.998%。",
    "美债收益率为 4.998%。",  # Maturity omitted, no unique authority.
    "10年美债实际收益率为 4.998%。",
    "10-year Treasury real yield: 4.998%.",
    "日本10年国债收益率 ^TNX 为 4.998%。",
    "美国2年国债收益率 ^TNX 为 4.998%。",
    "30年美债收益率 ^TNX 为 4.998%。",
    "Japanese 10-year government bond yield ^TNX: 4.998%.",
    "2-year Treasury yield ^TNX: 4.998%.",
    "美国国债收益率（2年期）^TNX 为 4.998%。",
])
def test_wrong_unknown_maturity_date_or_type_stays_blocked(ledger, text):
    issues = ledger._validate_analysis_claims(text)
    assert issues and all(item["kind"] == "yield" for item in issues)


@pytest.mark.parametrize("context", [
    "过去2年观察10年美债收益率 ^TNX，为4.998%",
    "过去5年观察10年美债收益率，为4.998%",
    "Over the past 2 years, the 10-year Treasury yield ^TNX was 4.998%",
    "过去2年观察 ^TNX 收益率，为4.998%",
])
def test_historical_window_does_not_change_the_bond_maturity(ledger, context):
    assert ledger._yield_value_observed(4.998, context)
    assert not ledger._validate_analysis_claims(context)


@pytest.mark.parametrize("text", [
    "^TNX 美债收益率 4.998%，策略年化收益率 30%。",
    "^TNX Treasury yield 4.998%; the strategy annualized return was 30%.",
    "^TNX 美债收益率 4.998% 策略年化收益率 30%。",
    "| 指标 | 数值 |\n|---|---|\n| 10年美债收益率 ^TNX | 4.998% |\n| 策略年化收益率 | 30% |",
])
def test_real_yield_cannot_launder_unverified_investment_return(ledger, text):
    issues = ledger._validate_analysis_claims(text)
    assert any(item["kind"] == "return" and item["value"] == "30%" for item in issues)
    assert not any(item.get("kind") == "yield" for item in issues)


def test_mixed_metrics_keep_separate_local_kinds(ledger):
    issues = ledger._validate_analysis_claims(
        "| 指标 | 备注 |\n|---|---|\n| 年化收益率 18.2% 最大回撤 9.4% | - |"
    )
    assert {(item["kind"], item["value"]) for item in issues} == {("return", "18.2%"), ("drawdown", "9.4%")}


def test_unverified_volatility_and_subjective_probabilities_remain_blocked(ledger):
    issues = ledger._validate_analysis_claims(
        "| 指标 | 数值 |\n|---|---|\n| 实现波动率 | 30.3% |\n| 主观概率 | 45% |"
    )
    assert {(item["kind"], item["value"]) for item in issues} == {("vol", "30.3%"), ("probability", "45%")}


def test_quote_from_other_maturity_cannot_authorize_ten_year_yield(tmp_path):
    ledger = GroundingLedger(run_dir=tmp_path, user_message="分析 ^FVX 和 ^TNX")
    ledger.ingest_tool_result(tool_name="get_market_data", arguments={"codes": ["^FVX"]},
                             result=json.dumps({"^FVX": [{"trade_date": "2026-11-11", "close": 4.998}]}),
                             call_id="five-year", success=True)
    assert ledger._validate_analysis_claims("10年美债收益率为4.998%。")
    assert not ledger._validate_analysis_claims("5年美债收益率为4.998%。")


@pytest.mark.parametrize("label,value", [("20日实现波动率", "**30.3% 年化**"),
                                         ("Realized volatility", "30.3% annualized")])
def test_annualization_modifier_inherits_the_adjacent_volatility_kind(ledger, label, value):
    ledger.ingest_tool_result(tool_name="portfolio_risk_xray", arguments={},
                             result=json.dumps({"annualized_vol": .303}), call_id="observed-vol", success=True)
    assert not ledger._validate_analysis_claims(f"| Metric | Value |\n|---|---|\n| {label} | {value} |")
    wrong = value.replace("30.3", "88.8")
    issues = ledger._validate_analysis_claims(f"| Metric | Value |\n|---|---|\n| {label} | {wrong} |")
    assert issues and {(item["kind"], item["value"]) for item in issues} == {("vol", "88.8%")}
    assert any(item["kind"] == "return" for item in ledger._validate_analysis_claims(
        f"| Metric | Value |\n|---|---|\n| {label} | 策略年化收益率 30.3% |"))


def test_complex_volatility_label_still_supplies_the_kind_of_annualized_value(ledger):
    issues = ledger._validate_analysis_claims(
        "| 指标 | 数值 |\n|---|---|\n| 20日实现波动率（由带宽推算：307.5÷4÷4,023.7×√252） | **30.3% 年化** |"
    )
    assert any(item["kind"] == "vol" and item["value"] == "30.3%" for item in issues)
    assert not any(item["kind"] == "return" for item in issues)
