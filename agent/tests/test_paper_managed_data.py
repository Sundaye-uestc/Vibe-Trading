"""Observed zeros are evidence; missing managed rows must never look normal."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from src.clickhouse_connector import ClickHouseConnector
from src.tools.paper_trade import data, preflight
from src.tools.paper_trade.coverage import PaperDataUnavailable, daily_status, raw_coverage
from src.tools.paper_trade.limits_cn_a import ClickHouseLimitsClient
from src.tools.paper_trade.tool import run_paper_trade_step

DAY = "2026-10-28"
SYMBOL = "600522.SH"


class Managed:
    def __init__(self):
        self.rows = [{"ts_code": SYMBOL, "trade_date": DAY, "is_st": 0, "is_suspended": 0,
                      "source": "baostock", "observed_at": "2026-10-29T10:00:00+08:00"}]
        self.st = []
        self.suspended = []
        self.coverage = [{"dataset": "raw_prices", "ts_code": SYMBOL, "from_date": "2026-04-01",
                          "to_date": DAY, "status": "complete", "source": "baostock",
                          "snapshot_id": "digest", "observed_at": "2026-10-29T10:00:00+08:00"}]

    def get_paper_daily_status(self, start, end, symbols):
        return pd.DataFrame(self.rows)

    def get_paper_data_coverage(self, *args):
        return pd.DataFrame(self.coverage)

    def get_trade_calendar(self, start, end):
        return pd.DataFrame([{"cal_date": DAY, "is_open": 1}])

    def query(self, sql, params=None):
        if "FROM stk_st_daily " in sql:
            return pd.DataFrame(self.st)
        if "FROM stk_suspend " in sql:
            return pd.DataFrame(self.suspended)
        return pd.DataFrame()


def test_observed_zero_status_makes_preflight_distinguish_normal_from_unimported():
    connector = Managed()
    request = {"market": "cn_a", "symbols": [SYMBOL], "from_date": DAY, "to_date": DAY}
    checks = {item["key"]: item for item in preflight._event_checks(request, connector, [DAY])}
    assert checks["st_status"]["status"] == "ready"
    assert checks["suspensions"]["status"] == "ready"
    assert "baostock" in checks["st_status"]["message"]
    assert checks["limits"]["status"] == "warning"
    assert checks["limit_events"]["status"] == "warning"
    connector.rows = []
    checks = {item["key"]: item for item in preflight._event_checks(request, connector, [DAY])}
    assert checks["st_status"]["status"] == "blocked"
    assert checks["suspensions"]["status"] == "blocked"


@pytest.mark.parametrize("damage", ["missing", "duplicate", "invalid_flag", "invalid_time", "wrong_symbol", "missing_source"])
def test_invalid_daily_status_never_confirms_event_absence(damage):
    connector = Managed()
    if damage == "missing":
        connector.rows = []
    elif damage == "duplicate":
        connector.rows *= 2
    elif damage == "invalid_flag":
        connector.rows[0]["is_st"] = "0.5"
    elif damage == "invalid_time":
        connector.rows[0]["observed_at"] = "2020-01-01"
    elif damage == "wrong_symbol":
        connector.rows[0]["ts_code"] = "000001.SZ"
    else:
        connector.rows[0]["source"] = ""
    with pytest.raises(PaperDataUnavailable):
        daily_status(connector, [SYMBOL], DAY, DAY, [DAY])


def test_legacy_clients_remain_unverified_and_do_not_skip_network():
    assert daily_status(object(), [SYMBOL], DAY, DAY, [DAY]) is None
    assert not raw_coverage(object(), SYMBOL, DAY, DAY)


@pytest.mark.parametrize("value", [None, float("nan"), "", "   "])
def test_daily_source_metadata_requires_a_real_nonempty_string(value):
    connector = Managed()
    connector.rows[0]["source"] = value
    with pytest.raises(PaperDataUnavailable):
        daily_status(connector, [SYMBOL], DAY, DAY, [DAY])


@pytest.mark.parametrize("field", ["source", "snapshot_id"])
@pytest.mark.parametrize("value", [None, float("nan"), "", "   "])
def test_raw_coverage_provenance_rejects_missing_or_nonstring_metadata(field, value):
    connector = Managed()
    connector.coverage[0][field] = value
    with pytest.raises(PaperDataUnavailable):
        raw_coverage(connector, SYMBOL, DAY, DAY)


@pytest.mark.parametrize("value", [None, float("nan"), "2026-10-29", "2026-10-29T10:00:00"])
def test_observation_time_requires_timezone_in_both_managed_contracts(value):
    connector = Managed()
    connector.rows[0]["observed_at"] = value
    connector.coverage[0]["observed_at"] = value
    with pytest.raises(PaperDataUnavailable):
        daily_status(connector, [SYMBOL], DAY, DAY, [DAY])
    with pytest.raises(PaperDataUnavailable):
        raw_coverage(connector, SYMBOL, DAY, DAY)


def test_failed_table_discovery_is_not_legacy(monkeypatch):
    connector = ClickHouseConnector(host="localhost")
    monkeypatch.setattr(connector, "query", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("unreachable")))
    with pytest.raises(RuntimeError):
        connector.has_managed_paper_data()


@pytest.mark.parametrize("names,expected", [([], False), (["paper_daily_status", "paper_data_coverage"], True)])
def test_explicit_contract_discovery(monkeypatch, names, expected):
    connector = ClickHouseConnector(host="localhost")
    calls = []
    def query(sql, params=None):
        calls.append(sql)
        return pd.DataFrame({"name": names})
    monkeypatch.setattr(connector, "query", query)
    assert connector.has_managed_paper_data() is expected
    assert "database = currentDatabase()" in calls[0]


def test_half_installed_contract_fails_closed(monkeypatch):
    connector = ClickHouseConnector(host="localhost")
    monkeypatch.setattr(connector, "query", lambda *args, **kwargs: pd.DataFrame({"name": ["paper_daily_status"]}))
    with pytest.raises(RuntimeError, match="incomplete"):
        connector.has_managed_paper_data()


@pytest.mark.parametrize("field,table", [("is_st", "st"), ("is_suspended", "suspended")])
def test_managed_positive_event_requires_matching_event_view(field, table):
    connector = Managed()
    connector.rows[0][field] = 1
    client = ClickHouseLimitsClient(connector)
    with pytest.raises(PaperDataUnavailable):
        client.validate_coverage([SYMBOL], DAY, DAY, [DAY])
    row = {"ts_code": SYMBOL, "trade_date": DAY, "type": "ST", "type_name": "ST",
           "suspend_type": "S", "suspend_timing": "daily"}
    setattr(connector, table, [row])
    client.validate_coverage([SYMBOL], DAY, DAY, [DAY])


def test_managed_status_gap_returns_in_band_failure_before_fetching_prices():
    connector = Managed()
    connector.rows = []
    class NoFetch:
        name = "never"
        def fetch(self, *args, **kwargs): raise AssertionError("prices should not be fetched")
    result = json.loads(run_paper_trade_step({
        "account_id": "test", "market": "cn_a", "from_date": DAY, "to_date": DAY,
        "account_state": {"cash": 100000, "positions": []},
        "orders": [{"symbol": SYMBOL, "side": "buy", "qty": 100}],
    }, loader=NoFetch(), calendar_client=connector, limits_client=ClickHouseLimitsClient(connector), ch_client=None))
    assert result["code"] == "PAPER_DATA_NOT_READY"


def test_complete_managed_ch_snapshot_skips_network_without_test_loader_override(monkeypatch):
    frame = pd.DataFrame({"open": [10.0], "high": [11.0], "low": [9.0], "close": [10.0], "vol": [100.0]}, index=pd.to_datetime([DAY]))
    class Client:
        def get_bars(self, symbol, start, end): return frame
        def covers_raw_range(self, symbol, start, end, actual): return symbol == SYMBOL and end == DAY
    monkeypatch.setattr(data, "default_ch_client", lambda: Client())
    monkeypatch.setattr(data, "_resolve_network_loader", lambda *args, **kwargs: pytest.fail("Managed complete snapshot must avoid network"))
    result = data.fetch_market_data("cn_a", [SYMBOL], DAY, DAY)
    assert result.provenance.source == "clickhouse"
    assert result.provenance.network_as_of is None


def test_claimed_complete_coverage_requires_actual_bars_and_full_calendar():
    connector = Managed()
    client = data.ClickHouseBarsClient(connector)
    frame = pd.DataFrame({"close": [26.65]}, index=pd.to_datetime([DAY]))
    assert client.covers_raw_range(SYMBOL, DAY, DAY, frame)
    with pytest.raises(PaperDataUnavailable):
        client.covers_raw_range(SYMBOL, "2026-10-27", DAY, frame)
    with pytest.raises(PaperDataUnavailable):
        client.covers_raw_range(SYMBOL, DAY, DAY, frame.iloc[:0])


@pytest.mark.parametrize("ranges,covered", [
    ([("2026-10-26", "2026-10-26"), ("2026-10-27", "2026-10-28")], True),
    ([("2026-10-27", "2026-10-28"), ("2026-10-25", "2026-10-27")], True),
    ([("2026-10-26", "2026-10-28"), ("2026-10-27", "2026-10-27")], True),
    ([("2026-10-26", "2026-10-26"), ("2026-10-28", "2026-10-28")], False),
    ([("2026-10-27", "2026-10-28")], False),
    ([("2026-10-26", "2026-10-27")], False),
])
def test_incremental_coverage_union_requires_every_date(ranges, covered):
    connector = Managed()
    template = connector.coverage[0]
    connector.coverage = [{**template, "from_date": first, "to_date": last,
                           "snapshot_id": f"snapshot-{i}"}
                          for i, (first, last) in enumerate(ranges)]
    assert raw_coverage(connector, SYMBOL, "2026-10-26", DAY) is covered


def test_incremental_declared_union_still_requires_calendar_status_and_actual_prices():
    connector = Managed()
    connector.coverage = [{**connector.coverage[0], "from_date": day, "to_date": day}
                          for day in ("2026-10-26", "2026-10-27", DAY)]
    connector.rows = [{**connector.rows[0], "trade_date": day}
                      for day in ("2026-10-26", "2026-10-27", DAY)]
    connector.get_trade_calendar = lambda start, end: pd.DataFrame([
        {"cal_date": day, "is_open": 1} for day in ("2026-10-26", "2026-10-27", DAY)])
    frame = pd.DataFrame({"close": [26.66, 25.85, 26.65]},
                         index=pd.to_datetime(["2026-10-26", "2026-10-27", DAY]))
    client = data.ClickHouseBarsClient(connector)
    assert client.covers_raw_range(SYMBOL, "2026-10-26", DAY, frame)
    with pytest.raises(PaperDataUnavailable, match="incomplete"):
        client.covers_raw_range(SYMBOL, "2026-10-26", DAY, frame.drop(pd.Timestamp("2026-10-27")))
    connector.rows.pop(1)
    with pytest.raises(PaperDataUnavailable, match="incomplete"):
        client.covers_raw_range(SYMBOL, "2026-10-26", DAY, frame)


def test_incomplete_increment_does_not_gain_authority_from_a_complete_overlap():
    connector = Managed()
    connector.coverage.append({**connector.coverage[0], "from_date": DAY,
                               "status": "unavailable"})
    assert not raw_coverage(connector, SYMBOL, DAY, DAY)


def test_reversed_increment_is_an_invalid_source_not_a_coverage_gap():
    connector = Managed()
    connector.coverage[0].update(from_date=DAY, to_date="2026-10-26")
    with pytest.raises(PaperDataUnavailable):
        raw_coverage(connector, SYMBOL, "2026-10-26", DAY)
