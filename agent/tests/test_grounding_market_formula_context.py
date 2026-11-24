"""Regressions for structural numerals in a multi-index daily market note."""

import json

import pytest

from src.agent.grounding import GroundingLedger


@pytest.fixture
def ledger(tmp_path):
    ledger = GroundingLedger(run_dir=tmp_path, user_message="比较 ^IXIC 与 ^NDX")
    quotes = {"^IXIC": 24089.56, "^NDX": 26746.10}
    ledger.ingest_tool_result(
        tool_name="get_market_data", arguments={"codes": list(quotes)},
        result=json.dumps({
            **{symbol: [{"trade_date": "2026-11-23", "close": close}]
               for symbol, close in quotes.items()},
            "_provenance": {symbol: {"source": "yahoo"} for symbol in quotes},
        }),
        call_id="daily-bars", success=True,
    )
    return ledger


@pytest.mark.parametrize("note", [
    "涨幅计算式为 `(当日收盘 / 前一交易日收盘 - 1) × 100`",
    "计算式为（当日收盘价 ÷ 上一交易日收盘价 − １）×１００％",
    "Return formula: (close / previous close - 1) * 100",
    "今天 11-24 09:44 UTC 美股尚未开盘",
    "11-24 09:44:02 EST 美股尚未开盘",
    "按上面 18 个日收盘价计算。",
    "Using 18 daily closing prices.",
])
def test_context_numbers_do_not_become_unattributed_index_quotes(ledger, note):
    assert ledger._direct_price_values(note) == []
    assert ledger.validate_final_answer(note).valid


@pytest.mark.parametrize("note", [
    "^IXIC 11-23 的最低为 23794.96",
    "^IXIC 11-23 的收盘 23794.96",
    "^IXIC 11-24 09:44 UTC 现价 23794.96",
    "^IXIC 基于18个日收盘价，现价23794.96",
    "^IXIC 计算式（当日收盘 / 前一交易日收盘 - 1）×100，现价23794.96",
    "^IXIC 计算式（当日收盘 / 前一交易日收盘 - 1）×100 = 23794.96",
])
def test_reported_price_next_to_a_date_sample_or_formula_is_still_checked(ledger, note):
    assert ledger._direct_price_values(note) == [23794.96]
    assert any(issue.get("value") == 23794.96 for issue in ledger.validate_final_answer(note).issues)


@pytest.mark.parametrize(("note", "remaining"), [
    ("价格区间 11-23 美元", [11, 23]),
    ("(当日收盘 / 前一交易日收盘 - 7) × 100", [7, 100]),
    ("(当日收盘 / 前一交易日收盘 - 1) × 99999", [99999]),
    ("(收盘 / 99999 - 1) × 100", [99999, 1, 100]),
    ("(收盘 / 前收 - 1) × 100 美元", [100]),
    ("收盘价 18 美元", [18]),
])
def test_literal_quotes_and_modified_formulas_keep_their_numerals(ledger, note, remaining):
    assert ledger._numbers_without_dates_or_percent(note) == remaining


def test_count_is_not_evidence_that_an_unknown_index_was_observed(ledger):
    result = ledger.validate_final_answer("取得 ^GSPC 的18个日收盘价样本")
    assert any(issue["code"] == "unsourced_symbol_figures" for issue in result.issues)


@pytest.mark.parametrize(("formula", "expected"), [
    ("^NDX：26746.10 / 25028.98 − 1 = **+6.87%**", "6.86%"),
    ("(26,746.10 ÷ 25,028.98 - 1) × 100 = −6.86%", "6.86%"),
    ("(90.4 / 100 - 1) × 100 = +9.60%", "-9.60%"),
    ("(100.15 / 100 - 1) × 100 = 0.1%", "0.2%"),
    ("(１００．１／１００－１) × １００ = ＋０．１１％", "0.10%"),
    ("(100 / 0 - 1) = 100%", "zero"),
])
def test_wrong_signed_return_is_rejected_at_printed_precision(ledger, formula, expected):
    result = ledger.validate_final_answer(formula)
    issue = next(issue for issue in result.issues if issue.get("field") == "return_formula")
    assert issue["code"] == "numeric_claim_conflict"
    assert expected in issue["message"]


@pytest.mark.parametrize("formula", [
    "^NDX 单日：(26,746.10 ÷ 25,028.98 − 1) × 100 = **+6.86%**",
    "26746.10 / 25028.98 − 1 = **+6.9%**",
    "派生计算结果：(100 / 200 - 1) = -50%",
    "99 / 100 − 1 = -1%",
    "Return = (100 / 100 - 1) * 100 = 0.00%",
])
def test_correct_explicit_percent_return_passes_arithmetic_gate(ledger, formula):
    assert not ledger._return_arithmetic_conflicts(formula)
    assert not ledger._derivation_conflicts(formula, None)


@pytest.mark.parametrize("formula", [
    "fees + (100/50 - 1) = 200%",
    "2**3/100 - 1 = -92%",
    "(2 * 100 / 50 - 1) × 100 = 300%",
    "11/23 收盘，11/20 收盘，+6.87%",
    "26746.10 / 25028.98 − 1。另一个指数涨幅 = 6.87%",
    "26746.10 / 25028.98 − 1\n| 别的指标 = 6.87% |",
    "(当日收盘 / 前一交易日收盘 - 1) × 100 = 6.87%",
])
def test_only_a_complete_local_equality_is_compared(ledger, formula):
    assert not ledger._return_arithmetic_conflicts(formula)
