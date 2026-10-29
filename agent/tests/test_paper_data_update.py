"""Manual updates are incremental, authenticated and safe across process failures."""

from __future__ import annotations

import json
import multiprocessing
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import paper_data_routes as routes
from src.data_ingest import paper_update as update
from src.data_ingest.paper_update import PaperDataService, UpdateBusy

END = "2026-10-28"
NOW = datetime(2026, 10, 29, 5, tzinfo=timezone.utc)
SYMBOL = "600522.SH"


class Reader:
    url = "http://127.0.0.1:8123/"
    database = "vibe_paper"
    timeout = 10

    def __init__(self):
        self.dates = pd.date_range("2026-04-11", END).strftime("%Y-%m-%d").tolist()
        self.missing, self.missing_calendar = set(), set()
        self.bad_price = None
        self.ranges = [(self.dates[0], END)]

    def has_managed_paper_data(self):
        return True

    def get_trade_calendar(self, start, end):
        return pd.DataFrame([{"cal_date": day, "is_open": 1} for day in self.dates if day not in self.missing_calendar])

    def get_paper_daily_status(self, start, end, symbols):
        return pd.DataFrame(
            [
                {
                    "ts_code": symbol,
                    "trade_date": day,
                    "is_st": 0,
                    "is_suspended": 0,
                    "source": "baostock",
                    "observed_at": NOW.isoformat(),
                }
                for symbol in symbols
                for day in self.dates
                if day not in self.missing
            ]
        )

    def query(self, sql, params=None):
        symbols = [value for key, value in params.items() if key.startswith("param_symbol")]
        if "FROM stk_factor_pro" in sql:
            return pd.DataFrame(
                [
                    {
                        "ts_code": symbol,
                        "trade_date": day,
                        "open": 10,
                        "high": 11,
                        "low": 9,
                        "close": float("nan") if day == self.bad_price else 10,
                    }
                    for symbol in symbols
                    for day in self.dates
                    if day not in self.missing
                ]
            )
        assert "FROM paper_data_coverage" in sql
        return pd.DataFrame(
            [
                {
                    "dataset": "raw_prices",
                    "ts_code": symbol,
                    "from_date": first,
                    "to_date": last,
                    "status": "complete",
                    "source": "baostock",
                    "snapshot_id": "fixture-id",
                    "observed_at": NOW.isoformat(),
                }
                for symbol in symbols
                for first, last in self.ranges
            ]
        )


@pytest.fixture
def reader(monkeypatch):
    reader = Reader()
    monkeypatch.setattr(update, "ClickHouseConnector", lambda: reader)
    monkeypatch.setattr(
        update,
        "writer_settings",
        lambda reader: {"url": reader.url, "user": "writer", "password": "hidden", "database": reader.database},
    )
    return reader


def test_closed_boundary_does_not_guess_weekends_or_accept_partial_today():
    assert update.latest_closed_date(datetime(2026, 10, 29, 7, 29, tzinfo=timezone.utc)) == "2026-10-28"
    assert update.latest_closed_date(datetime(2026, 10, 29, 7, 30, tzinfo=timezone.utc)) == "2026-10-29"
    assert update.latest_closed_date(datetime(2026, 11, 1, 8, tzinfo=timezone.utc)) == "2026-11-01"


def test_status_reports_verified_suffix_and_does_not_fill_a_gap(reader):
    status = update.inspect_coverage([SYMBOL], END)
    assert status["coverage"][0]["status"] == "ready" and not status["update_available"]
    reader.missing = {"2026-10-20"}
    status = update.inspect_coverage([SYMBOL], END)
    row = status["coverage"][0]
    assert row["missing_dates"] == ["2026-10-20"] and row["missing_count"] == 1
    assert row["from_date"] == "2026-10-21" and row["to_date"] == END
    assert status["update_available"]


@pytest.mark.parametrize("kind", ["calendar", "price", "coverage"])
def test_status_does_not_claim_readiness_from_minmax_dates(reader, kind):
    if kind == "calendar":
        reader.missing_calendar.add("2026-10-20")
    elif kind == "price":
        reader.bad_price = "2026-10-20"
    else:
        reader.ranges = [(reader.dates[0], "2026-10-19"), ("2026-10-21", END)]
    assert update.inspect_coverage([SYMBOL], END)["coverage"][0]["missing_dates"] == ["2026-10-20"]


def test_status_bounds_missing_dates_and_hides_errors(reader, monkeypatch):
    reader.missing = set(reader.dates)
    row = update.inspect_coverage([SYMBOL], END)["coverage"][0]
    assert row["missing_count"] == 201 and len(row["missing_dates"]) == 31
    monkeypatch.setattr(reader, "query", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("private-password")))
    result = update.inspect_coverage([SYMBOL], END)
    assert result["status_code"] == "source_unavailable" and "private" not in json.dumps(result)


def test_watchlist_survives_new_instance(tmp_path, reader):
    service = PaperDataService(tmp_path / "updates.db", now=lambda: NOW)
    assert service.status()["status_code"] == "watchlist_empty"
    service.save_watchlist([SYMBOL])
    assert PaperDataService(service.path, now=lambda: NOW).status()["watchlist"] == [SYMBOL]


@pytest.mark.parametrize("symbols", [[], [SYMBOL] * 2, ["AAPL.US"], ["600522.sh"], ["600522.SH;DROP"], [True]])
def test_watchlist_rejects_invalid_scope(tmp_path, symbols):
    with pytest.raises(ValueError):
        PaperDataService(tmp_path / "updates.db").save_watchlist(symbols)


def running(service, *, lease=90):
    job = {
        "id": "pending",
        "status": "running",
        "code": "updating",
        "started_at": NOW.isoformat(),
        "finished_at": None,
        "updated_symbols": [],
        "imported_snapshots": 0,
    }
    with service._db() as db:
        db.execute("INSERT INTO jobs VALUES (?,?,?)", (job["id"], json.dumps(job), NOW.timestamp() + lease))


def test_running_job_blocks_other_service_and_watchlist_edits(tmp_path, reader):
    service = PaperDataService(tmp_path / "updates.db", now=lambda: NOW)
    service.save_watchlist([SYMBOL])
    running(service)
    second = PaperDataService(service.path, now=lambda: NOW)
    with pytest.raises(UpdateBusy):
        second.save_watchlist(["000001.SZ"])
    with pytest.raises(UpdateBusy):
        second.update()
    assert second.status()["watchlist"] == [SYMBOL] and not second.status()["update_available"]


def test_expired_job_recovers_after_restart(tmp_path, reader):
    service = PaperDataService(tmp_path / "updates.db", now=lambda: NOW)
    running(service, lease=-1)
    assert service.status()["last_job"]["code"] == "interrupted"
    assert service.save_watchlist([SYMBOL])["watchlist"] == [SYMBOL]


class Pipe:
    def __init__(self):
        self.messages = []

    def send(self, value):
        self.messages.append(value)

    def close(self):
        pass


def fake_writer(monkeypatch, published):
    monkeypatch.setattr(
        update,
        "SnapshotWriter",
        lambda **kw: SimpleNamespace(
            verify_initialized=lambda: None,
            publish_many=lambda snapshots: published.append(list(snapshots)) or len(snapshots),
            close=lambda: None,
        ),
    )


def test_only_missing_tail_is_fetched_and_all_symbols_publish_together(reader, monkeypatch):
    reader.missing = {END}
    fetched, published = [], []
    fake_writer(monkeypatch, published)
    monkeypatch.setattr(
        update,
        "fetch_snapshot",
        lambda symbols, start, end, **kw: fetched.append((symbols, start, end)) or {"symbols": symbols},
    )
    pipe = Pipe()
    update._update_worker(pipe, [SYMBOL, "000001.SZ"], END)
    assert fetched == [([SYMBOL], END, END), (["000001.SZ"], END, END)]
    assert len(published) == 1 and len(published[0]) == 2 and pipe.messages[-1]["code"] == "updated"


def test_one_failed_symbol_keeps_entire_batch_unpublished(reader, monkeypatch):
    reader.missing = {END}
    published = []
    fake_writer(monkeypatch, published)

    def fetch(symbols, *args, **kwargs):
        if symbols == ["000001.SZ"]:
            raise ValueError("source daily data incomplete")
        return {"symbols": symbols}

    monkeypatch.setattr(update, "fetch_snapshot", fetch)
    pipe = Pipe()
    update._update_worker(pipe, [SYMBOL, "000001.SZ"], END)
    assert not published and pipe.messages[-1]["code"] == "data_not_ready"


def test_completed_range_never_fetches(reader, monkeypatch):
    fake_writer(monkeypatch, [])
    monkeypatch.setattr(update, "fetch_snapshot", lambda *a, **kw: pytest.fail("No source fetch expected"))
    pipe = Pipe()
    update._update_worker(pipe, [SYMBOL], END)
    assert pipe.messages[-1]["code"] == "up_to_date"


def hanging_worker(pipe, symbols, end):
    pipe.send({"progress": update._base(symbols, end, True, "ready")})
    time.sleep(30)


def successful_worker(pipe, symbols, end):
    pipe.send(
        {
            "code": "updated",
            "status": update._base(symbols, end, True, "ready"),
            "updated_symbols": symbols,
            "imported_snapshots": 1,
        }
    )
    pipe.close()


def test_spawn_timeout_cleans_child_and_preserves_uncertain_job(tmp_path, reader):
    service = PaperDataService(tmp_path / "updates.db", now=lambda: NOW, worker=hanging_worker, timeout=1.5)
    service.save_watchlist([SYMBOL])
    before = {p.pid for p in multiprocessing.active_children()}
    result = service.update()
    assert result["last_job"]["code"] == "update_timeout" and result["last_job"]["outcome_unknown"]
    assert {p.pid for p in multiprocessing.active_children()} == before
    assert PaperDataService(service.path, now=lambda: NOW).status()["last_job"]["code"] == "update_timeout"
    service.save_watchlist(["000001.SZ"])


def test_spawn_success_job_survives_restart(tmp_path, reader):
    service = PaperDataService(tmp_path / "updates.db", now=lambda: NOW, worker=successful_worker)
    service.save_watchlist([SYMBOL])
    result = service.update()
    assert result["last_job"]["status"] == "success"
    assert PaperDataService(service.path, now=lambda: NOW).status()["last_job"] == result["last_job"]


def private_config(tmp_path, monkeypatch, url="http://127.0.0.1:8123", database="vibe_paper"):
    path = tmp_path / "writer.env"
    path.write_text(
        f"CLICKHOUSE_INGEST_URL={url}\nCLICKHOUSE_INGEST_DATABASE={database}\nCLICKHOUSE_INGEST_USER=writer\nCLICKHOUSE_INGEST_PASSWORD=private-secret\n"
    )
    path.chmod(0o600)
    monkeypatch.setattr(
        update,
        "get_env_config",
        lambda: SimpleNamespace(
            data=SimpleNamespace(vibe_trading_paper_writer_env=str(path), clickhouse_user="paper_reader")
        ),
    )
    return path


def test_private_writer_config_matches_reader(tmp_path, monkeypatch):
    private_config(tmp_path, monkeypatch)
    assert update.writer_settings(Reader())["user"] == "writer"


@pytest.mark.parametrize("kind", ["url", "database", "symlink", "permissions", "same_account", "parent_symlink"])
def test_writer_refuses_external_or_untrusted_destination(tmp_path, monkeypatch, kind):
    path = private_config(
        tmp_path,
        monkeypatch,
        "https://external.invalid" if kind == "url" else "http://127.0.0.1:8123",
        "paper_other" if kind == "database" else "vibe_paper",
    )
    if kind == "symlink":
        actual = tmp_path / "actual.env"
        path.rename(actual)
        path.symlink_to(actual)
    elif kind == "permissions":
        path.chmod(0o644)
    elif kind == "same_account":
        path.write_text(path.read_text().replace("USER=writer", "USER=paper_reader"))
    elif kind == "parent_symlink":
        link = tmp_path / "linked"
        link.symlink_to(tmp_path, target_is_directory=True)
        monkeypatch.setattr(
            update,
            "get_env_config",
            lambda: SimpleNamespace(
                data=SimpleNamespace(
                    vibe_trading_paper_writer_env=str(link / "writer.env"), clickhouse_user="paper_reader"
                )
            ),
        )
    with pytest.raises(ValueError):
        update.writer_settings(Reader())


def test_api_rejects_extra_paths_dates_credentials_and_cross_origin(tmp_path, reader, monkeypatch):
    service = PaperDataService(tmp_path / "updates.db", now=lambda: NOW)
    monkeypatch.setattr(routes, "PaperDataService", lambda: service)
    app = FastAPI()
    routes.register_paper_data_routes(app)
    app.dependency_overrides[routes.require_auth] = lambda: None
    app.dependency_overrides[routes.require_settings_write_auth] = lambda: None
    client = TestClient(app)
    for body in (
        {"writer_env": "/tmp/private"},
        {"url": "http://external.invalid"},
        {"to_date": "2099-01-01"},
        {"symbols": [SYMBOL]},
    ):
        assert client.post("/api/paper-data/update", json=body).status_code == 422
    assert client.put("/api/paper-data/watchlist", json={"symbols": [SYMBOL]}).status_code == 200
    app.dependency_overrides.clear()
    import api_server

    monkeypatch.setattr(api_server, "_API_KEY", "fixture-api-key")
    assert TestClient(app, client=("203.0.113.10", 9000)).get("/api/paper-data").status_code == 401
    local = TestClient(app, client=("127.0.0.1", 9000))
    for method, path, body in [
        ("post", "/api/paper-data/update", {}),
        ("put", "/api/paper-data/watchlist", {"symbols": [SYMBOL]}),
    ]:
        assert (
            getattr(local, method)(
                path, json=body, headers={"Authorization": "Bearer fixture-api-key", "Origin": "https://evil.invalid"}
            ).status_code
            == 403
        )


def watchdog_process():
    update._start_watchdog(0.2)
    time.sleep(10)


def test_orphan_worker_watchdog_exits_before_lease_can_expire():
    process = multiprocessing.get_context("spawn").Process(target=watchdog_process)
    process.start()
    process.join(timeout=5)
    try:
        assert process.exitcode == 124
    finally:
        if process.is_alive():
            process.terminate()
            process.join()
        process.close()


def test_worker_rechecks_persisted_lease_before_publish(tmp_path, reader, monkeypatch):
    service = PaperDataService(tmp_path / "updates.db", now=lambda: NOW)
    running(service, lease=-1)
    reader.missing = {END}
    published = []
    fake_writer(monkeypatch, published)
    monkeypatch.setattr(update, "fetch_snapshot", lambda *a, **kw: {"symbols": [SYMBOL]})
    pipe = Pipe()
    update._update_worker(pipe, [SYMBOL], END, (str(service.path), "pending"))
    assert not published and pipe.messages[-1]["code"] == "update_failed"


def lost_response_worker(pipe, symbols, end):
    pipe.send({"progress": update._base(symbols, end, True, "ready")})
    pipe.close()


def test_uncertain_response_reconciles_coverage_without_repeating_write(tmp_path, reader, monkeypatch):
    service = PaperDataService(tmp_path / "updates.db", now=lambda: NOW, worker=lost_response_worker)
    service.save_watchlist([SYMBOL])
    refreshed = update._base([SYMBOL], END, True, "ready")
    refreshed["coverage"][0].update(latest_bar_date=END, missing_count=0, status="ready")
    calls = []
    monkeypatch.setattr(
        update, "reconcile_coverage", lambda symbols, end, prior: calls.append((symbols, end)) or refreshed
    )
    result = service.update()
    assert calls == [([SYMBOL], END)]
    assert result["coverage"][0]["latest_bar_date"] == END
    assert result["last_job"]["status"] == "failed" and result["last_job"]["outcome_unknown"]
    with service._db() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def locked_edit_worker(path, queue):
    from pathlib import Path

    try:
        PaperDataService(Path(path), now=lambda: NOW).save_watchlist(["000001.SZ"])
    except UpdateBusy:
        queue.put("blocked")
    else:
        queue.put("unsafe")


def test_lease_excludes_a_separate_api_process(tmp_path, reader):
    service = PaperDataService(tmp_path / "updates.db", now=lambda: NOW)
    service.save_watchlist([SYMBOL])
    running(service)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=locked_edit_worker, args=(str(service.path), queue))
    process.start()
    try:
        assert queue.get(timeout=5) == "blocked"
        process.join(timeout=5)
        assert process.exitcode == 0
        assert service.status()["watchlist"] == [SYMBOL]
    finally:
        if process.is_alive():
            process.terminate()
            process.join()
        process.close()
        queue.close()
