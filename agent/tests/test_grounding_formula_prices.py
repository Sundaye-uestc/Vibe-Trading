"""Formula coefficients, option prices and technical observations keep their types."""
from __future__ import annotations

import json

import pytest

from src.agent.grounding import GroundingLedger


def market_ledger(tmp_path, *, symbol="GC=F", prices=(100, 200)):
    ledger = GroundingLedger(run_dir=tmp_path, user_message=f"Analyze {symbol}")
    ledger.ingest_tool_result(tool_name="get_market_data", arguments={"codes": [symbol]},
                             result=json.dumps({symbol: [{"trade_date": f"2026-11-{i + 1:02}",
                                 "open": p, "high": p, "low": p, "close": p} for i, p in enumerate(prices)],
                                 "_provenance": {symbol: {"source": "yahoo"}}}),
                             call_id="prices", success=True)
    return ledger


@pytest.mark.parametrize("text", [
    "概率×中枢的加权值 ≈ 0.45×4100 + 0.30×3620 + 0.25×4560 = **约 4070 美元（较现价 +1.2%）**",
    "probability weighted price: 0.45 * 4100 + 0.30 * 3620 + 0.25 * 4560 = about **4070** USD",
])
def test_closed_weighted_formula_does_not_claim_its_coefficients_as_prices(text):
    assert GroundingLedger._numbers_without_dates_or_percent(text) == [4100, 3620, 4560, 4070]
    assert GroundingLedger._direct_price_values(text) == [4100, 3620, 4560, 4070]


@pytest.mark.parametrize("text", [
    "AAPL.US price was 0.45 USD",
    "主观概率另论，AAPL.US 股价报收 0.45 USD",
    "probability is not evidence: price was 0.45 * 100 = 45 USD",
    "概率加权价格 0.45×4100 + 0.30×3620",  # Not a closed equation.
])
def test_bare_small_quotes_and_unclosed_formulas_remain_checkable(text):
    assert .45 in GroundingLedger._numbers_without_dates_or_percent(text)


def test_weight_mask_does_not_hide_a_penny_quote_beside_the_formula(tmp_path):
    ledger = market_ledger(tmp_path)
    draft = "GC=F source yahoo, weighted price 0.5 * 100 + 0.5 * 200 = 150 and price was 0.45 USD"
    issues = ledger.validate_final_answer(draft).issues
    assert any(issue.get("code") == "numeric_claim_conflict" and issue.get("value") == .45 for issue in issues)


def test_small_observed_stock_price_is_still_verified(tmp_path):
    ledger = market_ledger(tmp_path, symbol="PENNY.US", prices=(.45, .46))
    assert ledger.validate_final_answer("PENNY.US source yahoo, price was 0.45 USD").valid
    assert not ledger.validate_final_answer("PENNY.US source yahoo, price was 0.30 USD").valid


def test_weighted_result_requires_correct_arithmetic_and_observed_input(tmp_path):
    ledger = market_ledger(tmp_path)
    records = ledger._comparable_price_records()
    assert ledger._is_explicit_derivation("GC=F weighted price 0.5 * 100 + 0.5 * 200 = **约 150**", records, "GC=F")
    assert ledger._is_explicit_derivation("GC=F weighted price 0.5 * 100 + 0.5 * 200 = approx. **150**", records, "GC=F")
    assert not ledger._is_explicit_derivation("GC=F weighted price 0.5 * 300 + 0.5 * 500 = 400", records, "GC=F")
    assert not ledger.validate_final_answer("GC=F source yahoo, weighted price 0.5 * 100 + 0.5 * 200 = 200 USD").valid
    assert any(issue.get("field") == "derivation" for issue in ledger._validations[-1]["issues"])
    assert not ledger.validate_final_answer("GC=F source yahoo, calculated price (100 + 200) / 2 = 150 and close was 9999 USD").valid


def options(ledger, *, ticker="GLD.US", returned="GLD.US", extras=None):
    data = {"ticker": returned, "expiration": 1802995200,
            "calls": [{"strike": 1000, "last_price": .45, "bid": .01, "ask": .5, "implied_volatility": .247}],
            "puts": [{"strike": 1000, "last_price": .3, "bid": .02, "ask": .35}],
            **(extras or {})}
    ledger.ingest_tool_result(tool_name="get_options_chain", arguments={"ticker": ticker},
                             result=json.dumps({"ok": True, "source": "yahoo", "data": data}),
                             call_id="options", success=True)


def test_option_premiums_and_strikes_do_not_become_underlying_prices(tmp_path):
    ledger = market_ledger(tmp_path, symbol="GLD.US", prices=(368, 369))
    options(ledger)
    assert {r.value for r in ledger._comparable_price_records()} == {368, 369}
    assert any(r.field == "data.calls[0].implied_volatility" for r in ledger._evidence)
    for value in (.45, .3, .01, 1000):
        assert not ledger.validate_final_answer(f"GLD.US source yahoo, price was {value} USD").valid


@pytest.mark.parametrize("extras", [
    {"underlying_price": 368.09},
    {"quote": {"regularMarketPrice": 368.09}},
])
def test_explicit_underlying_quotes_keep_authority(tmp_path, extras):
    ledger = GroundingLedger(run_dir=tmp_path, user_message="Analyze GLD.US")
    options(ledger, ticker="GLD", returned="GLD", extras=extras)
    records = ledger._comparable_price_records()
    assert [(r.symbol, r.field, r.value) for r in records] == [("GLD.US", "price", 368.09)]


@pytest.mark.parametrize("returned", ["TSLA.US", "GLD.TO"])
def test_foreign_option_underlying_cannot_authorize_a_requested_quote(tmp_path, returned):
    ledger = GroundingLedger(run_dir=tmp_path, user_message="Analyze GLD.US")
    options(ledger, returned=returned, extras={"underlying_price": 368.09})
    assert not ledger._comparable_price_records()


def test_nested_foreign_quote_does_not_hide_behind_matching_chain_ticker(tmp_path):
    ledger = GroundingLedger(run_dir=tmp_path, user_message="Analyze GLD.US")
    options(ledger, extras={"quote": {"symbol": "TSLA.US", "regularMarketPrice": 368.09}})
    assert not ledger._comparable_price_records()


def technical(ledger, returned):
    ledger.ingest_tool_result(tool_name="technical_indicators", arguments={"symbol": "AAPL"},
        result=json.dumps({"ok": True, "symbol": returned, "latest_date": "2026-11-11",
                           "latest_close": 210.5, "read_identity": {"provenance": {"source": "yahoo"}},
                           "indicators": {"bollinger": {"upper": 220.8}, "rsi_14": 50.14}}),
        call_id="technical", success=True)


@pytest.mark.parametrize("returned", ["AAPL", "AAPL.US"])
def test_technical_observations_preserve_source_date_and_same_symbol_alias(tmp_path, returned):
    ledger = GroundingLedger(run_dir=tmp_path, user_message="Analyze AAPL.US")
    technical(ledger, returned)
    assert ledger._evidence
    assert all(r.source == "yahoo" and r.timestamp == "2026-11-11" and r.symbol == "AAPL.US" for r in ledger._evidence)
    assert [(r.field, r.value) for r in ledger._comparable_price_records()] == [("close", 210.5)]


@pytest.mark.parametrize("returned", ["TSLA.US", "AAPL.TO", None])
def test_technical_identity_mismatch_has_no_numeric_authority(tmp_path, returned):
    ledger = GroundingLedger(run_dir=tmp_path, user_message="Analyze AAPL.US")
    technical(ledger, returned)
    assert not ledger._evidence


@pytest.mark.parametrize("header,label", [
    ("项目 | 数值 | 口径/截至 | 来源", "COMEX 黄金 GC=F 收盘"),
    ("Metric | Value | As of | Source", "GC=F closing price"),
])
def test_generic_quote_row_is_bound_to_its_own_value_date_and_symbol(tmp_path, header, label):
    ledger = market_ledger(tmp_path, prices=(4023.7, 4024))
    template = f"| {header} |\n|---|---|---|---|\n| {label} | **VALUE USD** | 2026-11-01; other observation 4024.0 | yahoo |"
    assert ledger.validate_final_answer(template.replace("VALUE", "4023.7")).valid
    bad = ledger.validate_final_answer(template.replace("VALUE", "99,999"))
    assert not bad.valid
    assert any(issue.get("value") == 99999 and issue.get("symbol") == "GC=F"
               and issue.get("field") == "close" and issue.get("date") == "2026-11-01" for issue in bad.issues)


def test_generic_quote_does_not_match_other_date_or_other_instrument(tmp_path):
    ledger = market_ledger(tmp_path, prices=(100, 200))
    ledger.ingest_tool_result(tool_name="get_market_data", arguments={"codes": ["GLD.US"]},
                             result=json.dumps({"GLD.US": [{"trade_date": "2026-11-01", "close": 368}]}),
                             call_id="gld", success=True)
    for value in (200, 368):
        result = ledger.validate_final_answer(f"| 项目 | 数值 | 日期 | 来源 |\n|---|---|---|---|\n| GC=F 收盘 | {value} USD | 2026-11-01 | yahoo |")
        assert any(issue.get("code") == "numeric_claim_conflict" for issue in result.issues)


@pytest.mark.parametrize("header,label", [
    ("symbol | price", "GC=F"),
    ("项目 | 数值", "GC=F 价格"),
    ("项目 | 数值", "GC=F 预测目标价"),
])
def test_table_formula_cannot_hide_wrong_math_behind_observed_output(tmp_path, header, label):
    ledger = market_ledger(tmp_path)
    table = f"source yahoo, GC=F USD\n| {header} |\n|---|---|\n| {label} | 0.5 * 100 + 0.5 * 200 = 200 |"
    result = ledger.validate_final_answer(table)
    assert any(issue.get("field") == "derivation" and issue.get("computed_value") == 150 for issue in result.issues)
    assert ledger.validate_final_answer(table.replace("= 200", "= 150")).valid
    unsupported = ledger.validate_final_answer(table.replace("100 + 0.5 * 200 = 200", "300 + 0.5 * 500 = 400"))
    assert any(issue.get("code") == "numeric_claim_unavailable" for issue in unsupported.issues)


def test_generic_option_and_risk_labels_are_not_spot_quote_rows(tmp_path):
    ledger = market_ledger(tmp_path, symbol="GLD.US", prices=(368, 369))
    table = ("| 项目 | 数值 | 来源 |\n|---|---|---|\n"
             "| GLD.US option price | 0.45 USD | yahoo |\n"
             "| GLD.US 波动价格区间解释 | 1.23 | yahoo |")
    assert not ledger._validate_price_tables(table)[0]
