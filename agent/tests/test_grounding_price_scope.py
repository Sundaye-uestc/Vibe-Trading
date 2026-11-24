"""A nearby ticker owns its quote; another instrument's value cannot rescue it."""
from __future__ import annotations

import json

import pytest

from src.agent.grounding import GroundingLedger, ValidationResult


def mixed_ledger(tmp_path, *, second_venue=False):
    ledger = GroundingLedger(run_dir=tmp_path, user_message="Analyze GLD.US, ^VIX and ^TNX")
    prices = {
        "GLD.US": [354.67, 369.26, 369.36, 372.52, 386.50],
        "^TNX": [4.859, 4.998, 5.049], "^VIX": [17.84, 28.18],
        "DX-Y.NYB": [102.30, 102.893],
    }
    if second_venue:
        prices["GLD.TO"] = prices["GLD.US"]
    for symbol, values in prices.items():
        ledger.ingest_tool_result(
            tool_name="get_market_data", arguments={"codes": [symbol]},
            result=json.dumps({
                symbol: [{"trade_date": f"2026-11-{index + 1:02}", "close": value}
                         for index, value in enumerate(values)],
                "_provenance": {symbol: {"source": "yahoo"}},
            }), call_id=symbol, success=True,
        )
    return ledger


@pytest.mark.parametrize("draft", [
    "^VIX 在 2026-11-05 收 28.18，是样本内近月最高；当周 GLD 于 2026-11-06 收 372.52、最低下探 369.26，没有出现避险买盘推升。",
    "GLD 周线收盘跌破 2026-10-30 的 354.67，且同时看到美元指数站上 2026-11-11 的 102.893 之上、^TNX 上破 2026-10-30 周线高点 5.049%。",
    "GLD price 369.36 and ^TNX price 4.998",
    "GLD 收盘369.36美元，^TNX 收盘4.998，美元指数收盘102.893。",
    "DXY price 102.893, GLD price 369.36, VIX price 28.18",
])
def test_mixed_aliases_bind_quotes_to_local_subjects(tmp_path, draft):
    issues = mixed_ledger(tmp_path)._validate_price_claims(draft)
    assert not any(issue["code"].startswith("numeric_claim_") for issue in issues), issues


@pytest.mark.parametrize("draft, symbol, value", [
    ("GLD price 4.998 and ^TNX price 4.998", "GLD.US", 4.998),
    ("GLD price 369.36 and ^TNX price 369.36", "^TNX", 369.36),
    ("^VIX 收盘28.18，GLD 当周下跌、最低下探28.18美元", "GLD.US", 28.18),
    ("美元指数价格369.36，GLD 价格369.36美元", "DX-Y.NYB", 369.36),
])
def test_another_instruments_observation_cannot_authorize_a_false_quote(tmp_path, draft, symbol, value):
    issues = mixed_ledger(tmp_path)._validate_price_claims(draft)
    assert any(issue["code"] == "numeric_claim_conflict" and issue["symbol"] == symbol
               and issue["value"] == value for issue in issues), issues


def test_ambiguous_bare_ticker_does_not_choose_a_matching_venue(tmp_path):
    issues = mixed_ledger(tmp_path, second_venue=True)._validate_price_claims("GLD 价格369.36美元")
    assert any(issue["code"] == "numeric_claim_unavailable" and issue["symbol"] is None
               for issue in issues), issues


def test_ambiguous_price_subject_requires_rewriting_not_refetching(tmp_path):
    ledger = mixed_ledger(tmp_path)
    issues = ledger._validate_price_claims("收盘354.67，^TNX 收盘4.998")
    assert any(issue.get("detail") == "price_subject_ambiguous" for issue in issues)
    assert ledger.recovery_action(ValidationResult(False, issues)) is None


@pytest.mark.parametrize("instrument", ["DX-Y.NYB 美元指数（Yahoo/yfinance）", "DX-Y.NYB", "DXY"])
@pytest.mark.parametrize("value,valid", [(102.893, True), (369.36, False)])
def test_index_table_uses_its_exact_symbol_not_another_instruments_price(tmp_path, instrument, value, valid):
    ledger = mixed_ledger(tmp_path)
    draft = ("| 观察项 | 字段 | 数值 | 日期 | 单位 |\n|---|---|---:|---|---|\n"
             f"| {instrument} | 收盘（当周未完成bar） | {value} | 2026-11-02 | 指数点 |")
    issues = ledger._validate_price_claims(draft)
    numeric = [issue for issue in issues if issue["code"].startswith("numeric_claim_")]
    assert bool(numeric) is not valid, numeric
    if not valid:
        assert numeric[0]["symbol"] == "DX-Y.NYB"


def test_explicit_venue_resolves_a_shared_root(tmp_path):
    issues = mixed_ledger(tmp_path, second_venue=True)._validate_price_claims("GLD.US 价格369.36美元")
    assert not any(issue["code"].startswith("numeric_claim_") for issue in issues), issues


def test_unknown_bare_ticker_cannot_inherit_an_observed_price(tmp_path):
    issues = mixed_ledger(tmp_path)._validate_price_claims("GLD price 369.36, TIP price 369.36 USD")
    assert any(issue["code"] == "numeric_claim_unavailable" and issue["symbol"] == "TIP"
               for issue in issues), issues


def test_a_later_ticker_cannot_bind_an_earlier_unlabelled_quote(tmp_path):
    issues = mixed_ledger(tmp_path)._validate_price_claims("收盘354.67，^TNX 收盘4.998")
    assert any(issue["code"] == "numeric_claim_unavailable" and issue["symbol"] is None
               and issue["value"] == 354.67 for issue in issues), issues


def test_currency_unit_does_not_name_the_dollar_index(tmp_path):
    ledger = mixed_ledger(tmp_path)
    mentions = ledger._price_symbol_mentions("GLD 价格369.36美元", ledger._comparable_price_records())
    assert {item[2] for item in mentions} == {"GLD.US"}


def test_ambiguous_dollar_subject_does_not_become_an_etf_currency_unit(tmp_path):
    issues = mixed_ledger(tmp_path)._validate_price_claims("关注度最高的条件：美元102.893之上")
    assert any(issue["code"] == "numeric_claim_unavailable" and issue["symbol"] is None
               for issue in issues), issues


def test_single_subject_continues_across_wrapped_paragraph_lines(tmp_path):
    draft = "GLD当周回落\n最低下探369.26\n\n^VIX价格28.18"
    issues = mixed_ledger(tmp_path)._validate_price_claims(draft)
    assert not any(issue["code"].startswith("numeric_claim_") for issue in issues), issues


@pytest.mark.parametrize("draft", [
    "GLD当周回落\n\n最低下探369.26\n^VIX价格28.18",
    "GLD当周回落，^VIX仍高企\n最低下探369.26",
])
def test_new_or_mixed_paragraph_requires_a_subject(tmp_path, draft):
    issues = mixed_ledger(tmp_path)._validate_price_claims(draft)
    assert any(issue["code"] == "numeric_claim_unavailable" and issue["symbol"] is None
               and issue["value"] == 369.26 for issue in issues), issues


def test_sentence_boundary_ends_the_dollar_index_subject(tmp_path):
    draft = "需要看到美元指数两周走强势头终止。技术上第一个确认点是周收盘重新站上386.50。"
    issues = mixed_ledger(tmp_path)._validate_price_claims(draft)
    numeric = [issue for issue in issues if issue["code"].startswith("numeric_claim_")]
    assert numeric and all(issue["symbol"] is None for issue in numeric), numeric
    assert numeric[0]["code"] == "numeric_claim_unavailable"


def test_explicit_gld_after_a_sentence_boundary_restores_local_binding(tmp_path):
    draft = "需要看到美元指数两周走强势头终止。技术上GLD周收盘重新站上386.50。"
    issues = mixed_ledger(tmp_path)._validate_price_claims(draft)
    assert not any(issue["code"].startswith("numeric_claim_") for issue in issues), issues
