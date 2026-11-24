"""Index names, section/table labels and ordered quote pairs establish provenance."""
import json

import pytest

from src.agent.grounding import GroundingLedger


@pytest.fixture
def ledger(tmp_path):
    ledger = GroundingLedger(run_dir=tmp_path, user_message="比较 ^IXIC、^NDX 和 ^GSPC")
    rows = {
        "^IXIC": [22617.73, 24089.56], "^NDX": [25028.98, 26746.10],
        "^GSPC": [6910.39, 7237.17],
    }
    for symbol, closes in rows.items():
        result = {
            symbol: [{"trade_date": day, "open": close - 250, "close": close}
                     for day, close in zip(("2026-11-20", "2026-11-23"), closes)],
            "_provenance": {symbol: {"source": "yahoo"}},
        }
        ledger.ingest_tool_result(tool_name="get_market_data", arguments={"codes": [symbol]},
                                  result=json.dumps(result), call_id=symbol, success=True)
    return ledger


def issues(ledger, text):
    return [item for item in ledger._validate_price_claims(text)
            if item["code"].startswith("numeric_claim_")]


@pytest.mark.parametrize("name,quote", [
    ("纳斯达克综合指数", "24089.56"), ("纳指综合", "24089.56"), ("纳斯达克综指", "24089.56"),
    ("纳指100", "26746.10"), ("标普500", "7237.17"), ("标准普尔500指数", "7237.17"),
])
def test_explicit_chinese_index_names_bind_only_their_quotes(ledger, name, quote):
    assert not issues(ledger, f"收盘价方面，{name}收盘{quote}。")
    wrong_quote = "7237.17" if quote != "7237.17" else "26746.10"
    assert issues(ledger, f"{name}收盘{wrong_quote}。")


@pytest.mark.parametrize("name", ["纳指", "纳斯达克100ETF", "标普500指数基金"])
def test_short_or_related_product_name_does_not_pick_an_index(ledger, name):
    assert issues(ledger, f"{name}收盘26746.10。")


def test_generic_table_date_label_carries_symbol_and_date(ledger):
    template = "| ^IXIC 日期 | 收盘 |\n|---|---:|\n| 2026-11-23 | {} |"
    assert not issues(ledger, template.format("24089.56"))
    assert issues(ledger, template.format("22617.73"))  # same symbol, wrong day
    assert issues(ledger, template.format("26746.10"))  # right day, different symbol


def test_price_table_handles_field_header_with_ticker_and_date(ledger):
    template = "| ^IXIC 11/23 字段 | 数值 |\n|---|---:|\n| open | {} |"
    assert not issues(ledger, template.format("23839.56"))
    assert issues(ledger, template.format("24089.56"))  # close on this date
    assert issues(ledger, template.format("22367.73"))  # open on earlier date


def test_multi_instrument_columns_cannot_exchange_values(ledger):
    template = ("| 日期 | ^IXIC 收盘 | ^GSPC 收盘 |\n|---|---:|---:|\n"
                "| 2026-11-23 | {} | {} |")
    assert not issues(ledger, template.format("24089.56", "7237.17"))
    assert len(issues(ledger, template.format("7237.17", "24089.56"))) == 2


def test_multi_date_columns_preserve_their_own_dates(ledger):
    table = ("| 指数 | 11/20 收盘 | 11/23 收盘 |\n|---|---:|---:|\n"
             "| ^IXIC | {} | {} |")
    assert not issues(ledger, table.format("22617.73", "24089.56"))
    assert len(issues(ledger, table.format("24089.56", "22617.73"))) == 2


@pytest.mark.parametrize("heading", ["### ^IXIC 价格", "**纳斯达克综指价格**"])
def test_named_section_stops_at_the_next_heading(ledger, heading):
    initial = heading + "\n\n| 日期 | 收盘 |\n|---|---:|\n| 2026-11-23 | 24089.56 |\n\n收盘为24089.56。"
    assert not issues(ledger, initial)
    assert issues(ledger, initial + "\n\n## 比较\n\n收盘为24089.56。")
    assert issues(ledger, initial + "\n\n### ^NDX 行情\n\n收盘为24089.56。")


def test_respective_prices_use_symbol_order_field_and_date(ledger):
    template = "（^NDX 与 ^GSPC 的 11-20 收盘分别为 {} 与 {}，同源同口径。）"
    assert not issues(ledger, template.format("25,028.98", "6,910.39"))
    assert len(issues(ledger, template.format("6,910.39", "25,028.98"))) == 2
    assert len(issues(ledger, template.format("26,746.10", "7,237.17"))) == 2
    assert issues(ledger, template.format("25,028.98", "6,910.39") + "^NDX 价格99999。")
