"""Local metric typing and dated two-point returns must not suppress other claims."""
import json

import pytest

from src.agent.grounding import GroundingLedger


@pytest.fixture
def ledger(tmp_path):
    result = GroundingLedger(run_dir=tmp_path, user_message="GLD.US")
    result.ingest_tool_result(
        tool_name="get_market_data", arguments={"codes": ["GLD.US"]},
        result=json.dumps({"GLD.US": [
            {"trade_date": "2026-01-01", "close": 100.0},
            {"trade_date": "2026-01-02", "close": 110.0},
            {"trade_date": "2026-02-01", "close": 200.0},
            {"trade_date": "2026-02-02", "close": 250.0},
            {"trade_date": "2026-02-03", "close": 225.0},
            {"trade_date": "2025-03-07", "close": 268.3900146484375},
            {"trade_date": "2025-06-06", "close": 305.17999267578125},
            {"trade_date": "2026-02-27", "close": 483.75},
            {"trade_date": "2026-05-29", "close": 417.29},
        ], "_provenance": {"GLD.US": {"source": "yahoo"}}}),
        call_id="prices", success=True,
    )
    return result


@pytest.mark.parametrize("claim", [
    "非回撤统计量）：以2026-01-01价格100.0与2026-01-02价格110.0计。",
    "不是回撤统计量：价格100.0。",
    "These are prices, not a drawdown statistic: GLD.US close 100.0 and 110.0.",
    "| 类型 | 数值 |\n|---|---|\n| 非回撤统计量 | 100.0与110.0 |",
])
def test_negated_metric_type_does_not_relabel_following_prices(ledger, claim):
    assert ledger._validate_analysis_claims(claim) == []


@pytest.mark.parametrize("claim", [
    "非回撤统计量：最大回撤88.8%。",
    "非回撤统计量：收益率88.8%。",
    "非回撤统计量：回撤88.8%。",
    "不是回撤统计量：波动率88.8%。",
    "不是回撤统计量（最大回撤88.8%）。",
    "not a drawdown statistic: maximum drawdown 88.8%.",
    "回撤统计量为88.8%。",
])
def test_only_the_negated_marker_is_masked_not_its_other_metrics(ledger, claim):
    assert ledger._validate_analysis_claims(claim)


def test_negated_metric_type_does_not_disable_price_evidence(ledger):
    assert any(issue["code"] == "numeric_claim_conflict" for issue in
               ledger.validate_final_answer("GLD.US yahoo USD 非回撤统计量：价格9999.0。").issues)


def table(endpoints, value):
    return f"GLD.US yahoo USD\n| 起点 → 终点 | 收益率 |\n|---|---|\n| {endpoints} | {value} |"


@pytest.mark.parametrize("endpoints,value", [
    ("2026-01-01 100.0 → 2026-01-02 110.0", "+10.00%"),
    ("2026-02-02 250.0 → 2026-02-03 225.0", "−10.0%"),
    ("2025-03-07 268.39 → 2025-06-06 305.18", "+13.71%"),
    ("2025-03-07 268.39 → 2025-06-06 305.18", "+13.7%"),
    ("2026-02-27 483.75 → 2026-05-29 417.29", "−13.74%"),
])
def test_adjacent_dated_observed_closes_ground_their_arithmetic_return(ledger, endpoints, value):
    assert ledger._validate_analysis_claims(table(endpoints, value)) == []


@pytest.mark.parametrize("endpoints,value,detail", [
    ("2026-01-01 100.0 → 2026-01-02 110.0", "+25.0%", "return_percentage_does_not_match_adjacent_inputs"),
    ("2026-02-02 250.0 → 2026-02-03 225.0", "+10.0%", "return_percentage_does_not_match_adjacent_inputs"),
    ("2026-02-27 483.75 → 2026-05-29 417.29", "−9.80%", "return_percentage_does_not_match_adjacent_inputs"),
    ("2025-01-01 100.0 → 2026-01-02 110.0", "+10.0%", "dated_closes_for_one_same_instrument_required"),
    ("2026-01-01 100.0 → 2026-01-03 110.0", "+10.0%", "dated_closes_for_one_same_instrument_required"),
    ("2026-01-02 110.0 → 2026-01-01 100.0", "−9.09%", "return_endpoint_dates_reversed"),
    ("100.0 → 110.0", "+10.0%", "explicit_start_end_prices_and_full_dates_required"),
    ("OTHER.US 2026-01-01 100.0 → 2026-01-02 110.0", "+10.0%", "dated_closes_for_one_same_instrument_required"),
])
def test_wrong_return_price_date_sign_or_instrument_remains_blocked(ledger, endpoints, value, detail):
    issues = ledger._validate_analysis_claims(table(endpoints, value))
    assert len(issues) == 1
    assert issues[0]["detail"] == detail
    assert "end / start - 1" in issues[0]["message"]


def test_four_column_table_does_not_borrow_the_other_pairs_return(ledger):
    content = ("GLD.US\n| 起点 → 终点 | 收益率 | 起点 → 终点 | 收益率 |\n|---|---|---|---|\n"
               "| 2026-01-01 100.0 → 2026-01-02 110.0 | +25.0% | "
               "2026-02-01 200.0 → 2026-02-02 250.0 | +10.0% |")
    issues = ledger._validate_analysis_claims(content)
    assert len(issues) == 2
    assert all(issue["detail"] == "return_percentage_does_not_match_adjacent_inputs" for issue in issues)


def test_one_correct_return_does_not_hide_another_wrong_percentage(ledger):
    issues = ledger._validate_analysis_claims(table("2026-01-01 100.0 → 2026-01-02 110.0", "+10.0%、+25.0%"))
    assert len(issues) == 1
    assert issues[0]["value"] == "+25.0%"


@pytest.mark.parametrize("label,value", [
    ("年化收益率", "+10.0%"),
    ("收益率", "+10.0%（年化）"),
    ("策略收益率", "+10.0%"),
    ("超额收益率", "+10.0%"),
])
def test_two_prices_do_not_prove_annualized_or_portfolio_returns(ledger, label, value):
    content = table("2026-01-01 100.0 → 2026-01-02 110.0", value).replace("| 收益率 |", f"| {label} |")
    assert ledger._validate_analysis_claims(content)
