"""A lost create response must not duplicate virtual accounts or lose intent."""

import json
import multiprocessing
import sqlite3
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import paper_trade_routes as routes
from src.tools.paper_trade.accounts import AccountConflict, PaperAccounts


def _clock():
    return datetime(2026, 10, 20, 22, tzinfo=timezone.utc)


def _create(service, **overrides):
    payload = {
        "label": "Research",
        "market": "us",
        "account_state": {"cash": 1000, "positions": []},
        "idempotency_key": "create-a5dbe184-3ef4-421b-bacc-31870bd5e3dc",
    }
    payload.update(overrides)
    return service.create(**payload)


def _create_in_process(path):
    return _create(PaperAccounts(path, clock=_clock, step=lambda _: None))


@pytest.fixture
def service(tmp_path):
    return PaperAccounts(tmp_path / "accounts.db", clock=_clock, step=lambda _: None)


def test_retry_after_reopening_returns_same_account_and_original_creation_date(service):
    original = _create(service)
    reopened = PaperAccounts(
        service.path,
        clock=lambda: datetime(2026, 10, 21, 22, tzinfo=timezone.utc),
        step=lambda _: None,
    )
    assert _create(reopened) == original
    assert reopened.list() == [original]


def test_replay_returns_current_ledger_without_replacing_it(service):
    original = _create(service)
    service.step = lambda payload: json.dumps(
        {
            "end_state": {"cash": 900, "positions": []},
            "nav_series": [{"date": "2026-10-20", "nav": 900}],
        }
    )
    advanced = service.advance(
        original["id"], expected_revision=0, to_date="2026-10-20", orders=[]
    )
    assert _create(service) == advanced["account"]
    assert _create(service)["revision"] == 1
    assert len(service.history(original["id"])) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"label": "Another account"},
        {"market": "hk"},
        {"account_state": {"cash": 2000}},
        {"start": "next_session"},
        {"fee_model": {"commission_rate": 0.002}},
        {"account_state": {"cash": 1000, "positions": [{"symbol": "AAPL.US", "qty": 1, "avg_cost": 10}]}},
    ],
)
def test_conflicting_intent_cannot_reuse_key(service, changes):
    original = _create(service)
    with pytest.raises(AccountConflict, match="different parameters"):
        _create(service, **changes)
    assert service.list() == [original]


def test_equivalent_json_representations_are_same_intent(service):
    original = _create(
        service,
        account_state={"cash": 1000, "positions": [{"symbol": "AAPL.US", "qty": 1, "avg_cost": 10}]},
        fee_model={"commission_rate": 0.001, "commission_min": 1},
    )
    assert _create(
        service,
        label=" Research ",
        account_state={"positions": [{"avg_cost": 10.0, "qty": 1.0, "symbol": "AAPL.US"}], "cash": 1000.0},
        fee_model={"commission_min": 1.0, "commission_rate": 0.001},
    ) == original


def test_omitted_defaults_are_same_intent(service):
    original = _create(service)
    assert _create(service, account_state={"cash": 1000}, fee_model={}) == original


def test_no_key_preserves_intentional_duplicate_creation(service):
    first = _create(service, idempotency_key=None)
    second = _create(service, idempotency_key=None)
    third = _create(service, idempotency_key="a-new-intent")
    assert len({first["id"], second["id"], third["id"]}) == 3
    assert len(service.list()) == 3


@pytest.mark.parametrize("key", ["", "a" * 129, " leading", "trailing ", "中文", "a/b", "a\n", "_first", 123, True])
def test_invalid_key_does_not_create_an_account(service, key):
    with pytest.raises(ValueError, match="idempotency_key"):
        _create(service, idempotency_key=key)
    assert service.list() == []


@pytest.mark.parametrize("key", ["a", "A" * 128, "B5-cf.a_b:123"])
def test_supported_key_forms(service, key):
    assert _create(service, idempotency_key=key) == _create(service, idempotency_key=key)


def test_concurrent_connections_replay_one_creation(service):
    barrier = Barrier(6)

    def create():
        instance = PaperAccounts(service.path, clock=_clock, step=lambda _: None)
        barrier.wait(timeout=10)
        return _create(instance)

    with ThreadPoolExecutor(max_workers=6) as pool:
        accounts = list(pool.map(lambda _: create(), range(6)))
    assert all(account == accounts[0] for account in accounts)
    assert service.list() == [accounts[0]]


def test_concurrent_processes_share_durable_creation_key(service):
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        first, second = list(pool.map(_create_in_process, [service.path, service.path], timeout=30))
    assert first == second
    assert service.list() == [first]


def test_concurrent_conflicting_intents_only_create_winner(service):
    barrier = Barrier(2)

    def create(label):
        instance = PaperAccounts(service.path, clock=_clock, step=lambda _: None)
        barrier.wait(timeout=10)
        try:
            return _create(instance, label=label)
        except AccountConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ["first", "second"]))
    assert results.count("conflict") == 1
    assert len(service.list()) == 1
    assert service.list()[0] in results


def test_key_and_account_commit_or_rollback_together(service):
    with sqlite3.connect(service.path) as db:
        db.execute(
            "CREATE TRIGGER fail_key BEFORE INSERT ON account_create_requests "
            "BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected storage failure"):
        _create(service)
    assert service.list() == []
    with sqlite3.connect(service.path) as db:
        assert db.execute("SELECT COUNT(*) FROM account_create_requests").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_key")
    assert _create(service) == _create(service)
    assert len(service.list()) == 1


def test_existing_database_migrates_without_rewriting_accounts_or_history(tmp_path):
    path = tmp_path / "legacy.db"
    original = {"id": "existing", "label": "untouched", "revision": 4}
    history = {"nav_series": [{"nav": 123}]}
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
        db.execute("CREATE TABLE steps (account_id TEXT, revision INTEGER, document TEXT NOT NULL, PRIMARY KEY(account_id, revision))")
        db.execute("INSERT INTO accounts VALUES (?, ?)", (original["id"], json.dumps(original)))
        db.execute("INSERT INTO steps VALUES (?, ?, ?)", (original["id"], 4, json.dumps(history)))
    migrated = PaperAccounts(path, clock=_clock, step=lambda _: None)
    created = _create(migrated)
    reopened = PaperAccounts(path, clock=_clock, step=lambda _: None)
    assert reopened.get("existing") == original
    assert reopened.history("existing") == [history]
    assert _create(reopened) == created
    assert len(reopened.list()) == 2


@pytest.fixture
def client(service, monkeypatch):
    monkeypatch.setattr(routes, "PaperAccounts", lambda: service)
    app = FastAPI()
    routes.register_paper_trade_routes(app)
    app.dependency_overrides[routes.require_auth] = lambda: None
    app.dependency_overrides[routes.require_settings_write_auth] = lambda: None
    return TestClient(app)


def _body(**changes):
    return {
        "label": "API account",
        "market": "us",
        "account_state": {"cash": 1000},
        "idempotency_key": "web-create-123",
        **changes,
    }


def test_api_retries_after_committed_response_is_lost(client, service):
    lose_response = True

    @client.app.middleware("http")
    async def drop_first_response(request, call_next):
        nonlocal lose_response
        response = await call_next(request)
        if lose_response and request.method == "POST":
            lose_response = False
            raise ConnectionError("response lost after creation committed")
        return response

    with pytest.raises(ConnectionError, match="response lost"):
        client.post("/api/paper-accounts", json=_body())
    assert len(service.list()) == 1
    response = client.post("/api/paper-accounts", json=_body())
    assert response.status_code == 200
    assert service.list() == [response.json()]


def test_api_conflict_is_409_and_replay_does_not_change_state(client, service):
    first = client.post("/api/paper-accounts", json=_body())
    assert first.status_code == 200
    conflict = client.post("/api/paper-accounts", json=_body(label="Changed"))
    assert conflict.status_code == 409
    assert "different parameters" in conflict.json()["detail"]
    assert client.post("/api/paper-accounts", json=_body()).json() == first.json()
    assert service.list() == [first.json()]


@pytest.mark.parametrize("key", ["", "a" * 129, "a\nb", 123, True])
def test_api_rejects_invalid_keys_before_writing(client, service, key):
    assert client.post("/api/paper-accounts", json=_body(idempotency_key=key)).status_code == 422
    assert service.list() == []
