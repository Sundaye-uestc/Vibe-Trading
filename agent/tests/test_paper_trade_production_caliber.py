"""Production loader resolution must not silently reprice a cash ledger."""

import pandas as pd
import pytest

from src.tools.paper_trade import data


@pytest.mark.parametrize(
    "market,source,adjustment,accepted",
    [
        ("us", "yfinance", None, False),
        ("us", "sina", None, True),
        ("hk", "tencent", None, False),
        ("hk", "tencent_raw", "raw", True),
        ("hk", "tencent", "split_dividend", False),
        ("us", "unverified", None, False),
        ("cn_a", "tencent", "split_dividend", False),
    ],
)
def test_resolved_loader_requires_raw_prices(
    monkeypatch, market, source, adjustment, accepted
):
    frame = pd.DataFrame(
        {
            "open": [100.0],
            "high": [100.0],
            "low": [100.0],
            "close": [100.0],
            "volume": [1000.0],
        },
        index=pd.to_datetime(["2026-10-20"]),
    )
    if adjustment:
        frame.attrs["adjustment"] = adjustment

    class Loader:
        name = source

        def fetch(self, *args, **kwargs):
            return {"TEST": frame}

    monkeypatch.setattr(data, "_resolve_network_loader", lambda *a, **kw: Loader())
    result = data.fetch_market_data(
        market,
        ("TEST",),
        "2026-10-20",
        "2026-10-20",
        ch_client=None,
    )
    assert data.assess_readiness(result, "2026-10-20").ready is accepted
    if not accepted:
        assert any("requires raw prices" in warning for warning in result.warnings)


def test_tencent_execution_adapter_reads_only_the_raw_endpoint(monkeypatch):
    from backtest.loaders.tencent_loader import DataLoader
    from src.tools.paper_trade.raw_loader import TencentRawLoader
    calls = []

    def fetch(self, code, start, end, forward_adjusted=True):
        calls.append(forward_adjusted)
        return pd.DataFrame({"close": [11.]}, index=pd.to_datetime(["2026-10-20"]))

    monkeypatch.setattr(DataLoader, "_fetch_one", fetch)
    frame = TencentRawLoader().fetch(["000001.SZ"], "2026-10-19", "2026-10-20")["000001.SZ"]
    assert calls == [False]
    assert frame.attrs["adjustment"] == "raw"


def test_missing_calendar_is_retryable_not_a_closed_day():
    import json
    from src.tools.paper_trade.tool import run_paper_trade_step

    class IncompleteCalendar:
        def get_trade_calendar(self, *args):
            return pd.DataFrame(columns=["cal_date", "is_open"])

    document = json.loads(run_paper_trade_step({"account_id": "a", "market": "cn_a",
        "account_state": {"cash": 1000}, "from_date": "2026-10-20", "to_date": "2026-10-20", "orders": []},
        calendar_client=IncompleteCalendar(), limits_client=None, ch_client=None, loader=None))
    assert document["code"] == "PAPER_DATA_NOT_READY"
    assert "incomplete" in document["message"]


def test_unconfigured_cn_calendar_stops_before_any_price_fetch(monkeypatch):
    import json
    from src.tools.paper_trade import tool

    monkeypatch.setattr(tool, "_default_cn_a_connector", lambda: None)
    monkeypatch.setattr(tool.data, "fetch_market_data", lambda *args, **kwargs:
                        pytest.fail("No guessed A-share calendar or price-only execution"))
    result = json.loads(tool.run_paper_trade_step({
        "account_id": "offline", "market": "cn_a", "account_state": {"cash": 1000},
        "from_date": "2026-10-20", "to_date": "2026-10-20", "orders": [],
    }))
    assert result["code"] == "PAPER_DATA_NOT_READY"
    assert "calendar unavailable" in result["message"]


@pytest.mark.parametrize("rows", [
    [{"cal_date": "2026-10-20", "is_open": 1}, {"cal_date": "2026-10-20", "is_open": 0}],
    [{"cal_date": "2026-10-20", "is_open": 1.5}],
    [{"cal_date": "2026-02-30", "is_open": 1}],
])
def test_bad_calendar_snapshot_is_not_ready_before_prices_are_read(monkeypatch, rows):
    import json
    from src.tools.paper_trade import tool

    class Calendar:
        def get_trade_calendar(self, *args):
            return pd.DataFrame(rows)

    monkeypatch.setattr(tool.data, "fetch_market_data", lambda *args, **kwargs:
                        pytest.fail("An inconsistent calendar must never reach matching"))
    result = json.loads(tool.run_paper_trade_step({
        "account_id": "offline", "market": "cn_a", "account_state": {"cash": 1000},
        "from_date": "2026-10-20", "to_date": "2026-10-20", "orders": [],
    }, calendar_client=Calendar(), limits_client=None, ch_client=None, loader=None))
    assert result["code"] == "PAPER_DATA_NOT_READY"
