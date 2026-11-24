"""A rejected secondary claim must not erase the requested daily move."""
import json
from datetime import datetime, timezone

import pytest

from src.agent.grounding import GroundingLedger


def ledger(tmp_path, question="昨天 ^IXIC 的涨幅如何？"):
    result = GroundingLedger(run_dir=tmp_path, user_message=question)
    result._request_time = datetime(2026, 11, 24, 9, 44, tzinfo=timezone.utc)
    return result


FRIDAY = {"trade_date": "2026-11-20", "close": 22617.73046875}
MONDAY = {"trade_date": "2026-11-23", "close": 24089.560546875, "open": 24269.75}


def ingest(book, rows, *, call_id="daily", symbol="^IXIC", interval="1D", success=True, currency=None):
    provenance = {"source": "yahoo"}
    if currency:
        provenance["quote_currency"] = currency
    book.ingest_tool_result(tool_name="get_market_data", call_id=call_id,
                            arguments={"codes": [symbol], "interval": interval}, success=success,
                            result=json.dumps({symbol: rows, "_provenance": {symbol: provenance}}))


def test_verified_result_survives_rejected_attribution(tmp_path):
    book = ledger(tmp_path)
    ingest(book, [FRIDAY, MONDAY])
    rejected = book.validate_final_answer("^IXIC 收盘 99999 点，来源 yahoo。")
    assert not rejected.valid
    for text in (book.safe_fallback(), book.correction_prompt(rejected)):
        assert all(x in text for x in ("+6.51%", "2026-11-20", "2026-11-23", "24,089.56", "yahoo"))
    fallback = book.safe_fallback()
    assert "高开" in fallback and "99999" not in fallback
    assert "未来预测" not in fallback


def test_first_rejection_can_release_a_dated_daily_move_and_new_interpretation(tmp_path):
    book = ledger(tmp_path)
    ingest(book, [FRIDAY, MONDAY])
    assert not book.verified_facts_for_revision()
    rejected = book.validate_final_answer("^IXIC 收盘 99999 点，来源 yahoo。")
    facts = book.verified_facts_for_revision()
    assert "+6.51%" in facts
    assert facts in book.correction_prompt(rejected, interpretation_only=True)
    body = "反弹主要出现在开盘时，随后回吐了部分涨幅。是否能守住跳空区域，仍需新行情确认。"
    revised = book.validate_final_answer(body, interpretation_only=True)
    assert revised.valid
    assert revised.rendered_answer == f"{facts}\n\n{body}"
    assert "99999" not in revised.rendered_answer


def test_comparator_cannot_substitute_for_the_requested_index(tmp_path):
    book = ledger(tmp_path, "昨天纳指和 ^GSPC 的涨幅如何？")
    ingest(book, [FRIDAY, MONDAY], symbol="^GSPC")
    assert not book.daily_move_summary()


@pytest.mark.parametrize("symbol,currency", [
    ("600123.SH", "CNY"), ("01234.HK", "HKD"), ("ARBITRARY.TO", "CAD"),
    ("935741.KQ", "KRW"), ("002751.SZ", "CNY"),
])
def test_listed_market_uses_its_own_clock_and_currency(tmp_path, symbol, currency):
    book = ledger(tmp_path, f"昨天 {symbol} 涨幅如何")
    # Asia's Nov 23 has finished, North America's hasn't at 01:00 UTC.
    book._request_time = datetime(2026, 11, 24, 1, tzinfo=timezone.utc)
    ingest(book, [FRIDAY, MONDAY], symbol=symbol)
    if symbol.endswith(".TO"):
        assert not book.daily_move_summary()
    else:
        assert f"24,089.56 {currency}" in book.daily_move_summary()


def test_crypto_weekend_return_requires_previous_calendar_day(tmp_path):
    book = ledger(tmp_path, "昨天 ETH-USDT 涨了多少")
    book._request_time = datetime(2026, 11, 23, 1, tzinfo=timezone.utc)
    sat = {"trade_date": "2026-11-21", "close": 0.000025}
    sun = {"trade_date": "2026-11-22", "close": 0.00003}
    ingest(book, [sat, sun], symbol="ETH-USDT")
    text = book.daily_move_summary()
    assert "2026-11-21" in text and "+20.00%" in text and "USDT" in text
    assert "0.00 USDT" not in text


@pytest.mark.parametrize("query,symbol,name,kind,venue,unit", [
    ("瑞士测试", "XTEST.SW", "瑞士测试", "equity", "EBS", "CHF"),
    ("测试黄金期货", "QZ=F", "测试黄金期货", "future", "CMX", ""),
    ("GBP/JPY", "GBPJPY=X", "GBP/JPY", "currency", "CCY", "JPY"),
    ("Example Index", "^ANYINDEX", "Example Index", "index", "other", ""),
])
def test_dated_fx_future_and_unlisted_instrument_catalog_names(tmp_path, query, symbol, name, kind, venue, unit):
    book = ledger(tmp_path, f"昨天{query}涨幅如何？")
    book._request_time = datetime(2026, 11, 24, 13, tzinfo=timezone.utc)
    book.ingest_tool_result(tool_name="search_symbol", arguments={"query": query}, call_id="resolve", success=True,
                            result=json.dumps({"ok": True, "data": {"query": query, "candidates": [
                                {"symbol": symbol, "name": name, "type": kind, "exchange": venue, "source": "yahoo"}
                            ]}}))
    ingest(book, [FRIDAY, MONDAY], symbol=symbol, currency=unit)
    summary = book.daily_move_summary()
    assert symbol in summary and "2026-11-20" in summary and "+6.51%" in summary
    assert "USD" not in summary
    assert "区间变动" in summary
    assert unit in summary


def test_intraday_timestamp_despite_daily_request_cannot_supply_a_close(tmp_path):
    book = ledger(tmp_path, "昨天 600123.SH 涨了多少")
    ingest(book, [FRIDAY, dict(MONDAY, trade_date="2026-11-23T10:30:00")], symbol="600123.SH")
    assert not book.daily_move_summary()


def test_same_symbol_secondary_lookup_by_short_substring_is_not_a_subject(tmp_path):
    book = ledger(tmp_path, "What was the performance of gold yesterday?")
    book.ingest_tool_result(tool_name="search_symbol", arguments={"query": "F"}, call_id="resolve", success=True,
                            result=json.dumps({"ok": True, "data": {"query": "F", "candidates": [
                                {"symbol": "F.US", "name": "Ford", "market": "us", "type": "equity", "source": "yahoo"}
                            ]}}))
    ingest(book, [FRIDAY, MONDAY], symbol="F.US")
    assert not book.daily_move_summary()


def test_tool_result_only_receives_calculations_from_its_own_daily_series(tmp_path):
    book = ledger(tmp_path)
    ingest(book, [FRIDAY, MONDAY], call_id="prices")
    ingest(book, [dict(FRIDAY, close=100), dict(MONDAY, close=200)], interval="1W", call_id="weeks")
    assert "收盘低于开盘" in book.daily_move_summary(call_id="prices")
    assert not book.daily_move_summary(call_id="weeks")


@pytest.mark.parametrize("change", ["wrong_interval", "gap", "future_bar", "unhandled", "conflict",
                                    "old_data", "weekly", "two_calls", "failed", "duplicate", "not_a_move"])
def test_requires_verified_pair_in_requested_session(tmp_path, change):
    book = ledger(tmp_path, "预测 ^IXIC 下周走势" if change == "not_a_move" else "昨天 ^IXIC 涨了多少")
    rows = [FRIDAY.copy(), MONDAY.copy()]
    if change == "gap":
        rows[0]["trade_date"] = "2026-11-19"
    if change == "old_data":
        rows[1]["trade_date"] = "2026-11-21"
    if change == "future_bar":
        book._request_time = datetime(2026, 11, 24, 0, 1, tzinfo=timezone.utc)
    if change == "conflict":
        ingest(book, [dict(MONDAY, close=1)], call_id="other")
    if change == "duplicate":
        rows.append(MONDAY)
    if change == "two_calls":
        ingest(book, rows[:1], call_id="other")
        rows = rows[1:]
    ingest(book, rows, interval={"wrong_interval": "5m", "weekly": "1W"}.get(change, "1D"),
           symbol="^GSPC" if change == "unhandled" else "^IXIC", success=change != "failed")
    assert not book.daily_move_summary()
