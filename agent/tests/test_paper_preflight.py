"""Data diagnostics must be bounded, truthful and unable to mutate a ledger."""

import json
import multiprocessing
import time

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import paper_trade_routes as routes
from src.tools.paper_trade import preflight


REQUEST = {"market": "cn_a", "from_date": "2026-10-19", "to_date": "2026-10-20", "symbols": ["600000.SH"]}


class Connector:
    def __init__(self, *, calendar_rows=None, fail_table=None, table_rows=None):
        self.calendar_rows = calendar_rows if calendar_rows is not None else [
            {"cal_date": "20261019", "is_open": 1}, {"cal_date": "20261020", "is_open": 1}]
        self.fail_table = fail_table
        self.table_rows = table_rows or {}
        self.queries = []

    def get_trade_calendar(self, start, end):
        return pd.DataFrame(self.calendar_rows)

    def query(self, sql, params=None):
        self.queries.append((sql, params))
        assert sql.startswith("SELECT ")
        if self.fail_table and self.fail_table in sql:
            raise RuntimeError("secret-password https://secret-host/internal")
        for table, rows in self.table_rows.items():
            if f"FROM {table} " in sql:
                return pd.DataFrame(rows)
        if "FROM stk_factor_pro " in sql:
            return pd.DataFrame()
        if "FROM stk_limit " in sql:
            return pd.DataFrame([{"ts_code": "600000.SH", "trade_date": day,
                                  "up_limit": 11, "down_limit": 9}
                                 for day in ("2026-10-19", "2026-10-20")])
        return pd.DataFrame()


class Loader:
    name = "fixture_raw"

    def __init__(self, *, dates=("2026-10-19", "2026-10-20"), price=10):
        self.dates = dates
        self.price = price

    def fetch(self, symbols, start, end, **kwargs):
        return {symbol: pd.DataFrame({"open": self.price, "high": 11, "low": 9,
                                     "close": 10, "volume": 100}, index=pd.to_datetime(self.dates))
                for symbol in symbols}


def check(result, key):
    return next(item for item in result["checks"] if item["key"] == key)


def test_complete_bars_calendar_but_event_queries_do_not_prove_coverage():
    connector = Connector()
    result = preflight.inspect_data(**REQUEST, connector=connector, loader=Loader())
    assert result["status"] == "warning" and result["ready"] is False
    for key in ("connection", "calendar", "prices", "limits"):
        assert check(result, key)["status"] == "ready"
    assert check(result, "suspensions")["status"] == "warning"
    assert "do not prove ingestion coverage" in check(result, "suspensions")["message"]
    assert result["bars"] == [{"symbol": "600000.SH", "as_of": "2026-10-20", "missing_dates": [], "invalid_dates": []}]
    assert result["provenance"]["source"] == "fixture_raw"
    assert len(connector.queries) == 5  # Four batched event queries, one history query.
    for sql, params in connector.queries[:4]:
        assert "600000.SH" not in sql
        assert params["param_symbol0"] == "600000.SH"


def test_calendar_gap_reports_exact_date_and_does_not_claim_price_coverage():
    result = preflight.inspect_data(**REQUEST, connector=Connector(calendar_rows=[
        {"cal_date": "2026-10-19", "is_open": 1}]), loader=Loader())
    assert result["status"] == "blocked"
    assert check(result, "calendar")["missing_dates"] == ["2026-10-20"]
    assert check(result, "prices")["status"] == "warning"


@pytest.mark.parametrize("rows", [
    [{"cal_date": "2026-10-19", "is_open": 1}, {"cal_date": "2026-10-19", "is_open": 0}],
    [{"cal_date": "2026-10-19", "is_open": 1.5}],
    [{"cal_date": "2026-13-99", "is_open": 1}],
])
def test_invalid_or_conflicting_calendar_never_becomes_ready(rows):
    result = preflight.inspect_data(**REQUEST, connector=Connector(calendar_rows=rows), loader=Loader())
    assert check(result, "calendar")["status"] == "blocked"


def test_closed_days_need_calendar_rows_but_do_not_need_bars():
    result = preflight.inspect_data(**REQUEST, connector=Connector(calendar_rows=[
        {"cal_date": "20261019", "is_open": 1}, {"cal_date": "20261020", "is_open": 0}]),
        loader=Loader(dates=("2026-10-19",)))
    assert check(result, "calendar")["status"] == "ready"
    assert check(result, "prices")["status"] == "ready"


def test_missing_raw_bars_reports_symbol_and_dates_without_inventing_suspensions():
    result = preflight.inspect_data(**REQUEST, connector=Connector(), loader=Loader(dates=("2026-10-19",)))
    assert check(result, "prices")["status"] == "warning"
    assert result["bars"][0]["missing_dates"] == ["2026-10-20"]
    assert "confirm the cause" in check(result, "prices")["message"]


@pytest.mark.parametrize("price", [float("nan"), float("inf"), 0, -1, 15])
def test_invalid_prices_are_blocked_and_json_finite(price):
    result = preflight.inspect_data(**REQUEST, connector=Connector(), loader=Loader(price=price))
    assert check(result, "prices")["status"] == "blocked"
    assert result["bars"][0]["invalid_dates"] == ["2026-10-19", "2026-10-20"]
    json.dumps(result, allow_nan=False)


def test_empty_universe_checks_calendar_without_network_or_false_green():
    class UnexpectedLoader:
        def fetch(self, *args, **kwargs):
            pytest.fail("No price fetch without symbols")

    result = preflight.inspect_data(**{**REQUEST, "symbols": []}, connector=Connector(), loader=UnexpectedLoader())
    assert check(result, "calendar")["status"] == "ready"
    assert check(result, "prices")["status"] == "not_checked"
    assert result["status"] == "warning" and not result["ready"]


def test_unconfigured_clickhouse_without_symbols_never_makes_network_calls(monkeypatch):
    from types import SimpleNamespace
    from src.config import accessor
    from src import clickhouse_connector

    monkeypatch.setattr(accessor, "get_env_config", lambda: SimpleNamespace(data=SimpleNamespace(clickhouse_host="")))
    monkeypatch.setattr(clickhouse_connector, "ClickHouseConnector", lambda: pytest.fail("Must not connect"))
    result = preflight.inspect_data(**{**REQUEST, "symbols": []})
    assert check(result, "connection")["status"] == "blocked"
    assert check(result, "calendar")["missing_dates"] == ["2026-10-19", "2026-10-20"]


@pytest.mark.parametrize("table,key", [("stk_limit", "limits"), ("stk_st_daily", "st_status"),
                                       ("stk_suspend", "suspensions"), ("stk_limit_list_d", "limit_events")])
def test_unavailable_tables_block_and_never_echo_secret_errors(table, key):
    result = preflight.inspect_data(**REQUEST, connector=Connector(fail_table=table), loader=Loader())
    assert check(result, key)["status"] == "blocked"
    assert "secret" not in json.dumps(result)


def test_absent_limits_disclose_heuristic_and_dates():
    result = preflight.inspect_data(**REQUEST, connector=Connector(table_rows={"stk_limit": []}), loader=Loader())
    assert check(result, "limits")["status"] == "warning"
    assert check(result, "limits")["missing_dates"] == ["2026-10-19", "2026-10-20"]
    assert "heuristics" in check(result, "limits")["message"]


@pytest.mark.parametrize("table,row,key", [
    ("stk_limit", {"up_limit": float("nan"), "down_limit": 9}, "limits"),
    ("stk_limit", {"up_limit": 8, "down_limit": 9}, "limits"),
    ("stk_suspend", {"suspend_type": "unknown"}, "suspensions"),
    ("stk_limit_list_d", {"limit": "bad"}, "limit_events"),
])
def test_malformed_event_rows_are_not_accepted(table, row, key):
    result = preflight.inspect_data(**REQUEST, connector=Connector(table_rows={table: [
        {"ts_code": "600000.SH", "trade_date": "2026-10-20", **row}]}), loader=Loader())
    assert check(result, key)["status"] == "blocked"


def test_hk_us_calendar_is_explicitly_unknown_even_with_good_bars():
    result = preflight.inspect_data(**{**REQUEST, "market": "us", "symbols": ["AAPL.US"]}, loader=Loader())
    assert check(result, "calendar")["status"] == "warning"
    assert check(result, "prices")["status"] == "ready"
    assert check(result, "suspensions")["status"] == "not_checked"
    assert result["status"] == "warning"


def test_production_adjusted_source_is_rejected_without_claiming_raw_prices(monkeypatch):
    class AdjustedLoader(Loader):
        name = "tencent"

        def fetch(self, *args, **kwargs):
            frames = super().fetch(*args, **kwargs)
            for frame in frames.values():
                frame.attrs["adjustment"] = "qfq"
            return frames

    monkeypatch.setattr(preflight.data, "_resolve_network_loader", lambda *a, **kw: AdjustedLoader())
    result = preflight.inspect_data(**REQUEST, connector=Connector())
    assert check(result, "prices")["status"] == "blocked"
    assert result["bars"][0]["as_of"] is None
    assert any("raw prices" in message and "frame rejected" in message for message in result["warnings"])


@pytest.mark.parametrize("override", [
    {"market": "crypto"}, {"from_date": "2026-10-21"}, {"from_date": "2026-09-01"},
    {"from_date": "20261019"}, {"from_date": "2026-02-30"}, {"from_date": "0001-01-01", "to_date": "0001-01-02"},
    {"symbols": ["600000.SH"] * 11}, {"symbols": ["600000.SH;SELECT"]}, {"symbols": ["AAPL.US"]},
    {"symbols": [1]}, {"symbols": "600000.SH"},
])
def test_invalid_requests_rejected_before_source_access(override):
    with pytest.raises(ValueError):
        preflight.inspect_data(**{**REQUEST, **override})


def test_symbol_normalization_is_deterministic():
    assert preflight.validate_request(**{**REQUEST, "symbols": [" 600000.sh", "600000.SH"]})["symbols"] == ["600000.SH"]


def fast_worker(pipe, request):
    pipe.send({**request, "test_worker": True})
    pipe.close()


def slow_worker(pipe, request):
    time.sleep(30)


def blocked_price_worker(pipe, request):
    class BlockedLoader:
        name = "blocked_fixture"

        def fetch(self, *args, **kwargs):
            time.sleep(30)

    preflight._connect = lambda: (None, preflight._check("connection", "blocked", "ClickHouse is not configured."))
    preflight.data._resolve_network_loader = lambda *args, **kwargs: BlockedLoader()
    preflight._worker(pipe, request)


def test_hung_price_source_retains_completed_diagnostics_on_timeout():
    before = {process.pid for process in multiprocessing.active_children()}
    result = preflight.run_preflight(**REQUEST, timeout_seconds=5, _target=blocked_price_worker)
    assert result["status"] == "blocked" and not result["ready"]
    assert check(result, "connection")["message"] == "ClickHouse is not configured."
    assert check(result, "calendar")["missing_dates"] == ["2026-10-19", "2026-10-20"]
    assert check(result, "suspensions")["status"] == "blocked"
    assert "exceeded" in check(result, "execution")["message"]
    assert result["provenance"] == {"source": "unavailable", "bars_as_of": None,
                                     "ch_as_of": None, "network_as_of": None}
    assert {process.pid for process in multiprocessing.active_children()} == before


def test_spawn_timeout_terminates_worker_and_releases_capacity():
    before = {process.pid for process in multiprocessing.active_children()}
    started = time.monotonic()
    result = preflight.run_preflight(**REQUEST, timeout_seconds=0.1, _target=slow_worker)
    assert check(result, "execution")["status"] == "blocked"
    assert "exceeded" in check(result, "execution")["message"]
    assert time.monotonic() - started < 5
    assert {process.pid for process in multiprocessing.active_children()} == before
    assert preflight.run_preflight(**REQUEST, timeout_seconds=10, _target=fast_worker)["test_worker"] is True


def test_capacity_rejects_without_creating_more_workers():
    assert preflight._CAPACITY.acquire(blocking=False)
    assert preflight._CAPACITY.acquire(blocking=False)
    try:
        result = preflight.run_preflight(**REQUEST)
        assert "busy" in check(result, "execution")["message"]
    finally:
        preflight._CAPACITY.release()
        preflight._CAPACITY.release()


def app():
    application = FastAPI()
    routes.register_paper_trade_routes(application)
    return application


def test_http_check_is_authenticated_validated_and_does_not_open_ledger(monkeypatch):
    import api_server

    monkeypatch.setattr(api_server, "_API_KEY", "test-preflight-key")
    monkeypatch.setattr(routes, "PaperAccounts", lambda: pytest.fail("Must not open account DB"))
    received = []
    monkeypatch.setattr(routes, "run_preflight", lambda **request: received.append(request) or {"ready": False})
    client = TestClient(app(), client=("203.0.113.20", 50000))
    headers = {"Authorization": "Bearer test-preflight-key"}
    assert client.post("/api/paper-trade/preflight", json=REQUEST).status_code == 401
    assert client.post("/api/paper-trade/preflight", json=REQUEST, headers={**headers, "Origin": "https://attacker.invalid"}).status_code == 403
    assert client.post("/api/paper-trade/preflight", json={**REQUEST, "symbols": [4]}, headers=headers).status_code == 422
    assert client.post("/api/paper-trade/preflight", json={**REQUEST, "strategy_path": "bad.py"}, headers=headers).status_code == 422
    assert client.post("/api/paper-trade/preflight", json={**REQUEST, "symbols": ["600000.sh"]}, headers=headers).json() == {"ready": False}
    assert received == [REQUEST]
