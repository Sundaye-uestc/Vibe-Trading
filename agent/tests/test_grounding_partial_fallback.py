"""Failed metrics must not erase sourced facts or become a false price alert."""

import json
import pytest

from src.agent.grounding import GroundingLedger, ValidationResult, _timestamp_matches_claim_date


@pytest.mark.parametrize("claim", ["2026-11-11", "**2026/11/11**", "2026年11月11日", "2026-11-11(周三)盘中"])
def test_full_date_never_borrows_another_year(claim):
    assert _timestamp_matches_claim_date("2026-11-11T00:00:00", claim)
    assert not _timestamp_matches_claim_date("2024-11-11T00:00:00", claim)


def test_single_digit_day_is_not_a_timestamp_prefix():
    assert not _timestamp_matches_claim_date("2026-11-11", "2026-11-1")
    assert _timestamp_matches_claim_date("2026-11-01", "2026-11-1")
    assert _timestamp_matches_claim_date("2026-11-11", "11-11(盘中)")


def test_price_table_cannot_borrow_previous_years_matching_price(tmp_path):
    ledger = ledger_with_prices(tmp_path)
    ledger.ingest_tool_result(tool_name="get_market_data", arguments={"codes": ["GC=F"]},
        result=json.dumps({"GC=F": [{"trade_date": "2024-11-11", "close": 2600}],
            "_provenance": {"GC=F": {"source": "yahoo"}}}), call_id="old-price", success=True)
    result = ledger.validate_final_answer("| 项目 | 数值 | 日期 | 来源 |\n|---|---:|---|---|\n| GC=F 收盘 | 2600 | 2026-11-11 | yahoo |")
    assert not result.valid
    assert any(item.get("code") == "numeric_claim_conflict" for item in result.issues)


def ledger_with_prices(tmp_path, *, prompt="分析 GC=F 的未来走势"):
    ledger = GroundingLedger(run_dir=tmp_path, user_message=prompt)
    ledger.ingest_tool_result(tool_name="get_market_data", arguments={"codes": ["GC=F"]},
        result=json.dumps({"GC=F": [{"trade_date": "2026-11-11", "open": 3900,
            "high": 4200, "low": 3800, "close": 4000}],
            "_provenance": {"GC=F": {"source": "yahoo"}}}), call_id="prices", success=True)
    return ledger


def test_analysis_failure_keeps_sourced_facts_without_claiming_price_conflict(tmp_path):
    ledger = ledger_with_prices(tmp_path)
    result = ledger.validate_final_answer("本次测得年化波动率为88.8%。")
    assert not result.valid
    text = ledger.safe_fallback()
    assert "波动率" in text and "缺少" in text
    assert "GC=F" in text and "4000" in text and "2026-11-11" in text and "yahoo" in text
    assert "88.8" not in text
    assert "价格或推导结果" not in text and "买入价" not in text


def test_only_latest_unresolved_issue_controls_the_explanation(tmp_path):
    ledger = ledger_with_prices(tmp_path)
    assert not ledger.validate_final_answer("GC=F 当前价格为99999。").valid
    assert not ledger.validate_final_answer("本次回测夏普比率为8.88。").valid
    text = ledger.safe_fallback()
    assert "夏普" in text and "价格或推导结果" not in text
    assert "99999" not in text and "8.88" not in text


def test_genuine_price_conflict_still_has_an_accurate_reason(tmp_path):
    ledger = ledger_with_prices(tmp_path)
    assert not ledger.validate_final_answer("GC=F 当前价格为99999。").valid
    text = ledger.safe_fallback()
    assert "价格或推导结果与对应工具证据不一致" in text
    assert "4000" in text and "99999" not in text


def test_unlocked_instrument_observations_are_not_released_as_verified_facts(tmp_path):
    ledger = ledger_with_prices(tmp_path)
    ledger.ingest_tool_result(tool_name="get_market_data", arguments={"codes": ["SPY.US"]},
        result=json.dumps({"SPY.US": [{"trade_date": "2026-11-11", "close": 600}]}),
        call_id="not-locked", success=True)
    ledger.validate_final_answer("本次测得年化波动率为88.8%。")
    assert "SPY.US" not in ledger.safe_fallback()


def test_english_analysis_fallback_does_not_claim_a_price_conflict(tmp_path):
    ledger = ledger_with_prices(tmp_path, prompt="Analyze GC=F")
    ledger.validate_final_answer("Measured volatility was 88.8%.")
    text = ledger.safe_fallback()
    assert "vol" in text and "4000" in text and "not a forecast" in text
    assert "prices conflicted" not in text and "entry price" not in text


def test_correction_feedback_is_scoped_to_the_rejected_claim(tmp_path):
    ledger = ledger_with_prices(tmp_path)
    issue = {"code": "analysis_claim_unavailable", "kind": "return", "symbol": "GLD.US",
             "value": "45%", "claim": "GLD.US measured return 45%", "message": "Unsupported return."}
    text = ledger.correction_prompt(ValidationResult(False, [issue]))
    assert "GLD.US/return" in text and "GLD.US measured return 45%" in text
    assert "Retain verified facts" in text
    assert "REMOVE these rejected value(s) entirely" not in text
    assert "rejected repeatedly" not in text
