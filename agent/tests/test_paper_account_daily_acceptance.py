"""A-share account lifecycle through HTTP, isolated clocks, and a real SQLite file.

Only the market rows and clock are fixtures. Matching, T+1, fees, persistence,
revision checks and the HTTP request/response path are the production code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import paper_trade_routes as routes
from src.tools.paper_trade.accounts import PaperAccounts
from src.tools.paper_trade.tool import run_paper_trade_step

SYMBOL = "600522.SH"
BUY = {"symbol": SYMBOL, "side": "buy", "qty": 100}
SELL = {"symbol": SYMBOL, "side": "sell", "qty": 100}


class FixtureMarket:
    name = "fixture_raw"

    def __init__(self):
        self.available_through = "2026-10-29"

    def fetch(self, codes, start, end, **kwargs):
        dates = pd.to_datetime(["2026-10-27", "2026-10-28", "2026-10-29"])
        frame = pd.DataFrame({
            "open": [26.5, 26.65, 27.1], "high": [26.6, 26.8, 27.2],
            "low": [26.4, 26.6, 27.0], "close": [26.5, 26.65, 27.1],
            "volume": [10000, 12000, 11000],
        }, index=dates)
        frame = frame.loc[frame.index <= pd.Timestamp(self.available_through)]
        return {code: frame.copy() for code in codes}

    def get_trade_calendar(self, start, end):
        # Explicit synthetic observations, never used by the real data import.
        return pd.DataFrame([{"cal_date": day.strftime("%Y-%m-%d"), "is_open": 1}
                             for day in pd.date_range(start, end)])

    def get_stk_limit(self, *args):
        return pd.DataFrame()

    def get_stk_st_daily(self, *args):
        return pd.DataFrame()

    def get_stk_suspend(self, *args):
        return pd.DataFrame()

    def get_stk_limit_list(self, *args):
        return pd.DataFrame()


@pytest.fixture
def daily_app(tmp_path, monkeypatch):
    now = [datetime(2026, 10, 28, 7, 29, 59, tzinfo=timezone.utc)]
    market = FixtureMarket()
    database = tmp_path / "daily-accounts.db"

    def step(payload):
        return run_paper_trade_step(payload, loader=market, ch_client=None,
                                   calendar_client=market, limits_client=market,
                                   _orders_at_end=True)

    def service():
        return PaperAccounts(database, clock=lambda: now[0], step=step)

    monkeypatch.setattr(routes, "PaperAccounts", service)

    def client():
        app = FastAPI()
        routes.register_paper_trade_routes(app)
        app.dependency_overrides[routes.require_auth] = lambda: None
        app.dependency_overrides[routes.require_settings_write_auth] = lambda: None
        return TestClient(app)

    return now, market, database, service, client


def create(client):
    response = client.post("/api/paper-accounts", json={
        "label": "A-share daily acceptance", "market": "cn_a", "start": "today",
        "account_state": {"cash": 100000, "positions": []},
        "idempotency_key": "daily-acceptance",
    })
    assert response.status_code == 200
    return response.json()


def settle(client, account_id, revision, date, orders):
    return client.post(f"/api/paper-accounts/{account_id}/advance", json={
        "expected_revision": revision, "to_date": date, "orders": orders,
    })


def test_web_created_a_share_account_reopens_then_advances_next_session(daily_app):
    now, _, database, _, make_client = daily_app
    with make_client() as client:
        account = create(client)
        assert account["start_on"] == "2026-10-28"
        assert settle(client, account["id"], 0, "2026-10-28", [BUY]).status_code == 422
        assert client.get(f"/api/paper-accounts/{account['id']}/history").json() == []
        now[0] = datetime(2026, 10, 28, 7, 30, tzinfo=timezone.utc)
        assert settle(client, account["id"], 0, "2026-10-27", [BUY]).status_code == 422
        assert settle(client, account["id"], 0, "2026-10-29", [BUY]).status_code == 422
        first = settle(client, account["id"], 0, "2026-10-28", [BUY, SELL]).json()
        assert first["committed"] and first["account"]["revision"] == 1
        assert [fill["side"] for fill in first["result"]["fills"]] == ["buy"]
        assert first["result"]["rejected_orders"][0]["reason_code"] == "t_plus_1"
        assert first["account"]["account_state"]["cash"] == 97329.97

    # A separate interpreter has no Python object, fixture clock or HTTP state
    # from the first app; the durable ledger must still be exactly the same.
    script = ("import json,sys; from pathlib import Path; "
              "from src.tools.paper_trade.accounts import PaperAccounts; "
              "s=PaperAccounts(Path(sys.argv[1])); "
              "print(json.dumps({'account':s.get(sys.argv[2]),'history':s.history(sys.argv[2])}))")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    process = subprocess.run([sys.executable, "-c", script, str(database), account["id"]],
                             capture_output=True, text=True, check=True, timeout=15, env=env)
    restored = json.loads(process.stdout)
    assert restored == {"account": first["account"], "history": [first["result"]]}

    with make_client() as refreshed:
        assert refreshed.get(f"/api/paper-accounts/{account['id']}").json() == first["account"]
        assert settle(refreshed, account["id"], 0, "2026-10-28", [BUY]).status_code == 409
        assert settle(refreshed, account["id"], 1, "2026-10-28", [SELL]).status_code == 422
        now[0] = datetime(2026, 10, 29, 7, 29, 59, tzinfo=timezone.utc)
        assert settle(refreshed, account["id"], 1, "2026-10-29", [SELL]).status_code == 422
        now[0] = datetime(2026, 10, 29, 7, 30, tzinfo=timezone.utc)
        second = settle(refreshed, account["id"], 1, "2026-10-29", [SELL]).json()
        assert second["committed"] and second["account"]["revision"] == 2
        assert not second["result"]["rejected_orders"]
        assert second["result"]["fills"][0]["side"] == "sell"
        assert second["account"]["account_state"] == {"cash": 100033.59, "positions": []}
        assert refreshed.get(f"/api/paper-accounts/{account['id']}/history").json() == [first["result"], second["result"]]
        assert second["account"]["last_result"]["end_state"] == second["account"]["account_state"]


def test_source_lag_does_not_consume_day_and_can_retry_after_refresh(daily_app):
    now, market, _, service, make_client = daily_app
    market.available_through = "2026-10-27"
    with make_client() as client:
        account = create(client)
        now[0] = datetime(2026, 10, 28, 7, 30, tzinfo=timezone.utc)
        response = settle(client, account["id"], 0, "2026-10-28", [BUY]).json()
        assert response["committed"] is False
        assert response["result"]["code"] == "PAPER_DATA_NOT_READY"
        assert service().get(account["id"]) == account
        assert service().history(account["id"]) == []
        market.available_through = "2026-10-28"
        response = settle(client, account["id"], 0, "2026-10-28", [BUY]).json()
        assert response["committed"] and response["account"]["revision"] == 1
        assert len(service().history(account["id"])) == 1


def test_history_insert_failure_rolls_back_the_ledger_update(daily_app):
    now, _, database, service, make_client = daily_app
    import sqlite3

    with make_client() as client:
        account = create(client)
    now[0] = datetime(2026, 10, 28, 7, 30, tzinfo=timezone.utc)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TRIGGER reject_history BEFORE INSERT ON steps "
                   "BEGIN SELECT RAISE(ABORT, 'test storage failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="test storage failure"):
        service().advance(account["id"], expected_revision=0, to_date="2026-10-28", orders=[BUY])
    assert service().get(account["id"]) == account
    assert service().history(account["id"]) == []
