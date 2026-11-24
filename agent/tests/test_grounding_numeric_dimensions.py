"""Numeric typography preserves the distinction between periods and quotes."""
from __future__ import annotations

import json

import pytest

from src.agent.grounding import GroundingLedger


def market_ledger(tmp_path):
    ledger = GroundingLedger(run_dir=tmp_path, user_message="Analyze GLD.US")
    ledger.ingest_tool_result(
        tool_name="get_market_data", arguments={"codes": ["GLD.US"]},
        result=json.dumps({"GLD.US": [{"trade_date": "2026-11-11", "close": 366.42}],
                           "_provenance": {"GLD.US": {"source": "yahoo"}}}),
        call_id="prices", success=True,
    )
    return ledger


@pytest.mark.parametrize("periods", [
    "20/50 日均线", "20／50 日均线", "２０／５０ 日均线",
    "20/50/200日均线", "２０／５０／２００均线", "20/50/200移动平均线",
    "20/50/200-day moving averages", "20／50／200 moving averages",
    "SMA(20/50/200)", "ＳＭＡ（２０／５０／２００）", "SMA20/50/200",
    "２０日／５０日／２００日均线", "20天/50天/200天均线",
])
def test_technical_windows_are_not_price_values(tmp_path, periods):
    draft = f"GLD.US 来源 yahoo，价格在10月底低点上方与{periods}之间来回。"
    assert GroundingLedger._numbers_without_dates_or_percent(draft) == []
    assert market_ledger(tmp_path).validate_final_answer(draft).valid


@pytest.mark.parametrize("tenor", [
    "10 年期", "１０ 年期", "10年", "１０年", "10-year", "10 years",
    "13 周期", "１３周", "3 个月期", "３个月", "13 weeks", "3 months",
])
def test_missing_instrument_maturity_is_not_an_unsourced_quote(tmp_path, tenor):
    draft = f"未能取得的部分：本次尝试获取 {tenor}美债收益率（^TNX）。"
    ledger = market_ledger(tmp_path)
    assert GroundingLedger._numbers_without_dates_or_percent(draft) == []
    assert not ledger._validate_unsourced_symbols(draft)


@pytest.mark.parametrize("draft", [
    "未取到10年期美债（^TNX），但^TNX现价99999美元",
    "未取到１０年期美债（^TNX）但现价９９９９９．５美元",
    "^TNX 10-year Treasury yield is 5.99%",
    "^TNX １０年期收益率为５．９９％",
    "TIP.US 20／50／200日均线尚未取到但现价99999美元",
    "TIP.US SMA(20/50/200)=99999",
])
def test_period_or_missing_data_language_cannot_hide_a_real_measurement(tmp_path, draft):
    ledger = market_ledger(tmp_path)
    assert ledger._has_instrument_figures(draft)
    assert ledger._validate_unsourced_symbols(draft)


@pytest.mark.parametrize("prices", [
    "20/50美元", "20／50美元", "２０／５０美元", "20/50日元",
    "20／50 USD", "20/50/200美元", "20／50／200 HKD",
])
def test_numeric_enumeration_with_currency_remains_a_price(tmp_path, prices):
    draft = f"GLD.US 来源 yahoo，价格为{prices}"
    values = GroundingLedger._numbers_without_dates_or_percent(draft)
    assert values[:2] == [20, 50]
    issues = market_ledger(tmp_path).validate_final_answer(draft).issues
    assert any(issue["code"] == "numeric_claim_conflict" and issue["value"] == 20
               for issue in issues), issues


@pytest.mark.parametrize("draft", [
    "GLD.US 价格在20／50日均线附近，但GLD.US现价99999美元",
    "GLD.US 价格在20/50/200均线附近且现价９９９９９．５美元",
    "GLD.US 价格参考SMA(20/50/200)但现价99999美元",
])
def test_a_quote_next_to_technical_windows_stays_checkable(tmp_path, draft):
    expected = 99999.5 if "．５" in draft else 99999
    assert GroundingLedger._numbers_without_dates_or_percent(draft) == [expected]
    issues = market_ledger(tmp_path).validate_final_answer(draft).issues
    assert any(issue["code"] == "numeric_claim_conflict" and issue["value"] == expected
               for issue in issues), issues


def test_typography_normalization_preserves_numeric_offsets():
    draft = "GLD.US价格在ＳＭＡ（２０／５０）附近，现价９９９９９．５美元"
    masked = GroundingLedger._masked_candidate_text(draft)
    start = draft.index("９９９９９．５")
    assert len(masked) == len(draft)
    assert masked[start:start + 7] == "99999.5"


@pytest.mark.parametrize("quote", [
    "1/2美元", "１／２美元", "１／２日元", "1/2 USD", "SMA２０／５０美元",
])
def test_currency_suffix_prevents_date_or_indicator_window_masking(tmp_path, quote):
    draft = f"GLD.US 价格{quote}"
    assert GroundingLedger._numbers_without_dates_or_percent(draft)
    ledger = market_ledger(tmp_path)
    assert ledger._validate_unsourced_symbols(f"未取到^TNX但其价格{quote}")
    if not quote.startswith("SMA"):
        issues = ledger.validate_final_answer(draft).issues
        assert any(issue["code"] == "numeric_claim_conflict" for issue in issues), issues


def test_fullwidth_grouped_quote_remains_one_number(tmp_path):
    issues = market_ledger(tmp_path).validate_final_answer("GLD.US现价９９，９９９．５美元").issues
    assert any(issue["code"] == "numeric_claim_conflict" and issue["value"] == 99999.5
               for issue in issues), issues


def test_provenance_separator_does_not_become_an_unsourced_fx_pair(tmp_path):
    draft = "| GLD.US | close（盘中观测） | 366.42 | 2026-11-11 | yahoo／USD/股 |"
    assert not market_ledger(tmp_path)._validate_unsourced_symbols(draft)


@pytest.mark.parametrize("windows", [
    "20/50/200 三条均线", "20/50/200 3条均线", "20、50、200 日三条均线",
    "20、50、200日均线", "20，50及200日均线", "20, 50 和 200日均线",
    "20/50/200 the three moving averages", "20, 50 and 200-day moving averages",
    "20, 50, and 200-day moving averages", "２０、５０、２００ 日三条均线",
])
def test_complete_technical_enumeration_survives_clause_splitting(tmp_path, windows):
    draft = f"GLD.US 来源yahoo，价格同时位于 {windows}之下。"
    ledger = market_ledger(tmp_path)
    assert GroundingLedger._numbers_without_dates_or_percent(draft) == []
    assert ledger.validate_final_answer(draft).valid


@pytest.mark.parametrize("quote", [
    "20/50美元且参考200日均线", "20、50美元且参考200日均线",
    "20, 50 USD and 200-day moving averages", "20、50以及200美元附近的均线",
    "20/50/200三条价格数据与均线", "20/50/200三条均线，GLD.US现价99999美元",
])
def test_a_technical_word_cannot_swallow_unrelated_quote_numbers(tmp_path, quote):
    draft = f"GLD.US 价格为{quote}"
    values = GroundingLedger._numbers_without_dates_or_percent(draft)
    assert values
    issues = market_ledger(tmp_path).validate_final_answer(draft).issues
    assert any(issue["code"] == "numeric_claim_conflict" for issue in issues), issues
