"""Failed lookup counts and contextual dates never become market prices."""
from __future__ import annotations

import json

import pytest

from src.agent.grounding import GroundingLedger, _infer_currency


def market_ledger(tmp_path, symbol="GLD.US"):
    ledger = GroundingLedger(run_dir=tmp_path, user_message=f"Analyze {symbol}")
    ledger.ingest_tool_result(
        tool_name="get_market_data",
        arguments={"codes": [symbol]},
        result=json.dumps({
            symbol: [
                {"trade_date": "2026-10-30", "open": 356, "low": 354.67,
                 "high": 360, "close": 358},
                {"trade_date": "2026-11-06", "open": 359, "low": 358.94,
                 "high": 361, "close": 360},
            ],
            "_provenance": {symbol: {"source": "yahoo"}},
        }),
        call_id="prices", success=True,
    )
    return ledger


@pytest.mark.parametrize("draft", [
    '未取到现货 XAU/USD 与 COMEX 期货（search_symbol("XAUUSD")返回0条）',
    '取不到的现货 XAU/USD 与 COMEX 期货（`search_symbol("XAUUSD")` 返回 0 条）不做盎司换算',
    'XAU/USD was not retrieved (search_symbol("XAUUSD") returned 0 results).',
])
def test_failed_lookup_count_does_not_claim_an_unknown_instrument_price(tmp_path, draft):
    ledger = market_ledger(tmp_path)
    assert GroundingLedger._numbers_without_dates_or_percent(draft) == []
    assert ledger.validate_final_answer(draft).valid


@pytest.mark.parametrize("draft", [
    "返回了10条结果", "查询命中 23 条记录", "搜索到 2 个结果",
    "返回0个候选", "返回 0 个候选标的", "search returned 0 candidates",
    "search returned 10 results", "search found 23 records",
])
def test_result_count_is_not_a_price(draft):
    assert GroundingLedger._numbers_without_dates_or_percent(draft) == []


@pytest.mark.parametrize("draft", [
    "未取到 XAU/USD（返回0条）但现价99999美元",
    "未取到 XAU/USD（返回0条），但 XAU/USD 现价99999美元",
    "XAU/USD not retrieved (returned 0 results) but its price is 99999 USD",
    "未取到 XAU/USD（返回0个候选）但现价99999美元",
])
def test_missing_data_statement_does_not_exempt_a_real_quote(tmp_path, draft):
    ledger = market_ledger(tmp_path)
    assert GroundingLedger._numbers_without_dates_or_percent(draft) == [99999]
    issues = ledger.validate_final_answer(draft).issues
    assert any(issue["code"] == "unsourced_symbol_figures" for issue in issues)
    assert any(issue["code"] == "numeric_claim_unavailable" and issue["value"] == 99999
               and issue["symbol"] == "XAU-USD"
               for issue in issues)


@pytest.mark.parametrize("draft", [
    "XAU/USD RSI 99", "TIP.US RSI 99", "TIP.US 目标价99999美元",
    "TIP.US 回报率 20%", "未取到 TIP.US（返回0条）但目标价99999美元",
    "TIP.US MACD is 1.5", "TIP.US confidence score 90",
])
def test_unknown_instrument_metrics_are_not_hidden_by_price_masks(tmp_path, draft):
    issues = market_ledger(tmp_path).validate_final_answer(draft).issues
    assert any(issue["code"] == "unsourced_symbol_figures" for issue in issues)


@pytest.mark.parametrize("draft", ["XAU/USD 现价0美元", "GLD.US price is 0 USD"])
def test_zero_price_without_query_count_label_remains_checkable(tmp_path, draft):
    ledger = market_ledger(tmp_path)
    assert GroundingLedger._numbers_without_dates_or_percent(draft) == [0]
    assert not ledger.validate_final_answer(draft).valid


@pytest.mark.parametrize("draft, expected", [
    ("GLD.US 截至11-06最低358.94美元", [358.94]),
    ("GLD.US 当周（截至 11-06）最低358.94美元", [358.94]),
    ("GLD.US 354.67（10-30低点）", [354.67]),
    ("GLD.US 354.67（10-30 前低）", [354.67]),
    ("GLD.US as of 11-06: price 358.94 USD", [358.94]),
])
def test_short_dates_have_explicit_date_context(draft, expected):
    assert GroundingLedger._numbers_without_dates_or_percent(draft) == expected
    masked = GroundingLedger._masked_candidate_text(draft)
    assert len(masked) == len(draft)
    start = draft.index(str(expected[0]))
    assert masked[start:start + len(str(expected[0]))] == str(expected[0])


@pytest.mark.parametrize("draft", [
    "GLD.US 价格11-30美元", "GLD.US 价格10-30", "GLD.US price 11-30 USD",
    "GLD.US 截至11-30美元", "GLD.US as of 11-30 USD",
    "GLD.US 截至11-30 USDT", "GLD.US as of 11-30 KRW",
])
def test_ambiguous_or_currency_bearing_dash_ranges_remain_checkable(draft):
    numbers = GroundingLedger._numbers_without_dates_or_percent(draft)
    assert len(numbers) == 2 and numbers[-1] == 30


def test_decimal_endpoint_cannot_be_masked_as_an_as_of_date():
    assert GroundingLedger._numbers_without_dates_or_percent("GLD.US 截至11-30.5美元") == [11, 30.5]


@pytest.mark.parametrize("draft", [
    "GLD.US 价格11-30美元", "GLD.US 价格10-30", "GLD.US price 11-30 USD",
])
def test_false_price_range_is_still_rejected(tmp_path, draft):
    assert not market_ledger(tmp_path).validate_final_answer(draft).valid


def test_date_annotation_cannot_hide_a_conflicting_quote(tmp_path):
    ledger = market_ledger(tmp_path)
    for draft in (
        "GLD.US source yahoo 当周（截至11-06）最低99999美元",
        "GLD.US source yahoo 股价99999美元（10-30低点）",
    ):
        issues = ledger.validate_final_answer(draft).issues
        assert any(issue["code"] == "numeric_claim_conflict" and issue["value"] == 99999
                   for issue in issues)


@pytest.mark.parametrize("draft", [
    "GLD.US source yahoo 99999美元（10-30低点）",
    "GLD.US source yahoo 高点为99999美元",
    "GLD.US source yahoo 99999 USD（10-30高点）",
    "GLD.US source yahoo 低点 $99999",
])
def test_currency_bearing_extreme_is_a_price_without_an_extra_price_verb(tmp_path, draft):
    issues = market_ledger(tmp_path).validate_final_answer(draft).issues
    assert any(issue["code"] == "numeric_claim_conflict" and issue["value"] == 99999
               for issue in issues)


@pytest.mark.parametrize("draft", [
    "GLD.US RSI 低点20", "GLD.US RSI 高点99", "GLD.US RSI 20（10-30低点）",
    "GLD.US 预算99999美元而RSI处于低点20",
    "GLD.US RSI高点99与预算99999美元不相关",
])
def test_indicator_extreme_does_not_become_a_price(tmp_path, draft):
    issues = market_ledger(tmp_path).validate_final_answer(draft).issues
    assert not any(issue["code"].startswith("numeric_claim_") for issue in issues)


@pytest.mark.parametrize("symbol, expected", [
    ("DX-Y.NYB", None), ("ABC-FOO", None), ("ABC/Y.NYB", None),
    ("BRK-B.US", "USD"), ("BBD-B.TO", "CAD"),
    ("USD-CAD", "CAD"), ("EUR/USD", "USD"), ("EURCAD=X", "CAD"),
    ("BTC-USDT", "USDT"), ("BTC-USDC", "USDC"), ("ETH/BTC", "BTC"),
    ("XAU-USD", "USD"),
])
def test_pair_currency_requires_a_recognized_quote_asset(symbol, expected):
    assert _infer_currency(symbol) == expected


def test_index_venue_suffix_does_not_leak_into_evidence_currency(tmp_path):
    ledger = market_ledger(tmp_path, symbol="DX-Y.NYB")
    assert ledger._evidence
    assert all(record.currency is None for record in ledger._evidence)
