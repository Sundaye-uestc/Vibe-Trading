"""Offline operator import boundary: validate completely before one atomic publish."""

from __future__ import annotations

import copy
import json
import re
from datetime import date, timedelta

import httpx
import pytest

from src.data_ingest.paper_snapshot import SnapshotWriter, _validate_envelope, make_snapshot, validate_scope


@pytest.fixture
def source():
    return {
        "symbols": ["600522.SH"],
        "start": "2026-10-23",
        "end": "2026-10-26",
        "observed_at": "2026-10-29T05:00:00+00:00",
        "calendar_records": [
            {
                "calendar_date": (date(2026, 9, 22) + timedelta(days=offset)).isoformat(),
                "is_trading_day": int(
                    (date(2026, 9, 22) + timedelta(days=offset)).isoformat()
                    in {"2026-10-22", "2026-10-23", "2026-10-26"}
                ),
            }
            for offset in range(35)
        ],
        "daily_records": {
            "600522.SH": [
                {
                    "date": day,
                    "code": "sh.600522",
                    "open": "27.49",
                    "high": "27.57",
                    "low": "26.56",
                    "close": "26.66",
                    "preclose": "27.93",
                    "volume": "89107538",
                    "amount": "2403185959.13",
                    "adjustflag": "3",
                    "tradestatus": "1",
                    "isST": "0",
                }
                for day in ("2026-10-23", "2026-10-26")
            ]
        },
    }


def test_complete_snapshot_preserves_explicit_closed_days_raw_and_status(source):
    snapshot = make_snapshot(**source)
    assert [row["is_open"] for row in snapshot["calendar"]] == [1, 0, 0, 1]
    assert [row["pretrade_date"] for row in snapshot["calendar"]] == [
        "2026-10-22",
        "2026-10-23",
        "2026-10-23",
        "2026-10-23",
    ]
    assert snapshot["daily"][0]["vol"] == 891075.38
    assert snapshot["daily"][0]["volume_shares"] == 89107538
    assert snapshot["daily"][0]["adjustment"] == "raw"
    assert snapshot["daily"][0]["is_st"] == snapshot["daily"][0]["is_suspended"] == 0
    assert {r["dataset"] for r in snapshot["coverage"] if r["status"] == "complete"} == {
        "raw_prices",
        "st_status",
        "suspensions",
    }
    assert {r["dataset"] for r in snapshot["coverage"] if r["status"] == "unavailable"} == {"limits", "limit_events"}
    _validate_envelope(snapshot)


@pytest.mark.parametrize("mutation", ["missing_closed", "duplicate", "conflict", "bad_flag", "no_previous"])
def test_calendar_incomplete_or_ambiguous_is_rejected(source, mutation):
    rows = source["calendar_records"]
    if mutation == "missing_closed":
        rows.pop(2)
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(rows[-1]))
    elif mutation == "conflict":
        rows.append({**rows[-1], "is_trading_day": "0"})
    elif mutation == "bad_flag":
        rows[-1]["is_trading_day"] = "1.5"
    else:
        rows.pop(0)
    with pytest.raises(ValueError):
        make_snapshot(**source)


@pytest.mark.parametrize(
    "field,value",
    [
        ("adjustflag", "2"),
        ("code", "sh.600000"),
        ("date", "2026-10-24"),
        ("open", "NaN"),
        ("close", "Infinity"),
        ("high", "26"),
        ("volume", "-1"),
        ("open", True),
        ("volume", "100.25"),
        ("tradestatus", ""),
        ("isST", "2"),
    ],
)
def test_invalid_price_or_status_never_imports(source, field, value):
    source["daily_records"]["600522.SH"][0][field] = value
    with pytest.raises(ValueError):
        make_snapshot(**source)


def test_missing_stock_day_is_not_equated_with_suspension(source):
    source["daily_records"]["600522.SH"].pop()
    with pytest.raises(ValueError, match="coverage is incomplete"):
        make_snapshot(**source)


def test_suspension_and_st_come_from_explicit_flags(source):
    row = source["daily_records"]["600522.SH"][0]
    row.update(tradestatus="0", isST="1", volume="0", amount="0")
    snapshot = make_snapshot(**source)
    assert snapshot["daily"][0]["is_suspended"] == snapshot["daily"][0]["is_st"] == 1


def test_new_observation_has_new_identity_but_exact_file_reimport_is_stable(source):
    first = make_snapshot(**source)
    source["observed_at"] = "2026-10-29T06:00:00+00:00"
    second = make_snapshot(**source)
    assert first["snapshot_id"] != second["snapshot_id"]
    assert second["snapshot_id"] == make_snapshot(**source)["snapshot_id"]
    second["daily"][0]["close"] = 26.67
    with pytest.raises(ValueError, match="changed after validation"):
        _validate_envelope(second)


@pytest.mark.parametrize(
    "symbols,start,end",
    [
        (["600522.SH"] * 2, "2026-10-23", "2026-10-26"),
        (["AAPL.US"], "2026-10-23", "2026-10-26"),
        (["600522.SH"], "2026-10-23", "2026-10-22"),
        (["600522.SH"], "2025-01-01", "2026-10-22"),
    ],
)
def test_scope_is_small_and_explicit(symbols, start, end):
    with pytest.raises(ValueError):
        validate_scope(symbols, start, end)


class Backend:
    def __init__(self):
        self.queries = []
        self.published = set()
        self.fail_insert = False
        self.objects = []
        self.columns = [
            {"name": name, "type": kind}
            for name, kind in (("snapshot_id", "String"), ("published_at", "UInt64"), ("payload", "String"))
        ]

    def handle(self, request):
        sql = request.url.params["query"]
        self.queries.append((sql, request.content.decode()))
        assert "secret" not in str(request.url)
        if "system.tables" in sql:
            return httpx.Response(200, json={"data": self.objects})
        if "system.columns" in sql:
            return httpx.Response(200, json={"data": self.columns})
        if sql.startswith("CREATE TABLE"):
            self.objects = [
                {
                    "name": "paper_market_snapshots",
                    "engine": "ReplacingMergeTree",
                    "comment": re.search(r"COMMENT '([^']+)'", sql)[1],
                }
            ]
        if sql.startswith("SELECT count()"):
            return httpx.Response(200, json={"data": [{"n": int(request.url.params["param_id"] in self.published)}]})
        if sql.startswith("INSERT"):
            if self.fail_insert:
                return httpx.Response(503, text="secret response body")
            body = json.loads(request.content)
            self.published.add(body["snapshot_id"])
        return httpx.Response(200, text="")


def writer(backend):
    return SnapshotWriter(
        "http://127.0.0.1:8123",
        "writer",
        "secret",
        "paper_test",
        client=httpx.Client(base_url="http://127.0.0.1:8123", transport=httpx.MockTransport(backend.handle)),
    )


def test_one_atomic_insert_and_idempotent_retry(source):
    backend = Backend()
    client = writer(backend)
    snapshot = make_snapshot(**source)
    client.initialize()
    assert client.publish(snapshot)["imported"] is True
    assert client.publish(snapshot)["imported"] is False
    inserts = [(sql, body) for sql, body in backend.queries if sql.startswith("INSERT")]
    assert len(inserts) == 1
    published = json.loads(json.loads(inserts[0][1])["payload"])
    assert published["calendar"] and published["daily"] and published["coverage"]
    assert not any(sql.startswith(("DROP", "ALTER", "DELETE", "TRUNCATE")) for sql, _ in backend.queries)


def test_failed_upload_does_not_publish_or_destroy_previous_snapshot(source):
    backend = Backend()
    client = writer(backend)
    snapshot = make_snapshot(**source)
    backend.published.add("prior-snapshot")
    backend.fail_insert = True
    with pytest.raises(RuntimeError, match=r"\(HTTP 503\)") as exc:
        client.publish(snapshot)
    assert "secret" not in str(exc.value)
    assert backend.published == {"prior-snapshot"}
    backend.fail_insert = False
    assert client.publish(snapshot)["imported"]
    assert "prior-snapshot" in backend.published


def test_validation_happens_before_any_database_query(source):
    backend = Backend()
    client = writer(backend)
    snapshot = make_snapshot(**source)
    snapshot["coverage"][0]["status"] = "unavailable"
    with pytest.raises(ValueError):
        client.publish(snapshot)
    assert not backend.queries


def test_existing_unrelated_database_is_never_changed():
    backend = Backend()
    backend.objects = [{"name": "users"}]
    with pytest.raises(ValueError, match="unrelated"):
        writer(backend).initialize()
    assert len(backend.queries) == 1 and backend.queries[0][0].startswith("SELECT")


@pytest.mark.parametrize(
    "url,database",
    [
        ("http://user:secret@localhost", "paper_test"),
        ("http://localhost/?q=1", "paper_test"),
        ("file:///tmp/db", "paper_test"),
        ("http://localhost", "default"),
        ("http://localhost", "paper_x;DROP"),
    ],
)
def test_write_endpoint_and_database_are_constrained(url, database):
    with pytest.raises(ValueError):
        SnapshotWriter(url, "writer", "secret", database)


@pytest.mark.parametrize(
    "end,observed", [("2026-10-29", "2026-10-29T07:29:59+00:00"), ("2026-10-30", "2026-10-29T08:00:00+00:00")]
)
def test_incomplete_or_future_daily_boundary_is_rejected(source, end, observed):
    source.update(end=end, observed_at=observed)
    with pytest.raises(ValueError, match="completed daily session"):
        make_snapshot(**source)


def test_early_calendar_range_does_not_overflow():
    with pytest.raises(ValueError, match="lookback"):
        validate_scope(["600522.SH"], "0001-01-01", "0001-01-02")


def test_same_named_unowned_tables_are_not_reused():
    backend = Backend()
    backend.objects = [{"name": "paper_market_snapshots", "comment": "not this application"}]
    with pytest.raises(ValueError, match="signature"):
        writer(backend).initialize()
    assert len(backend.queries) == 1


def test_normalized_bool_tampering_is_not_accepted(source):
    snapshot = make_snapshot(**source)
    snapshot["daily"][0]["is_suspended"] = False
    with pytest.raises(ValueError):
        _validate_envelope(snapshot)


def test_owned_schema_can_be_reopened_but_column_changes_are_rejected():
    backend = Backend()
    client = writer(backend)
    client.initialize()
    client.initialize()
    backend.columns[-1]["type"] = "UInt64"
    before = len(backend.queries)
    with pytest.raises(ValueError, match="incompatible columns"):
        client.initialize()
    assert all(sql.startswith("SELECT") for sql, _ in backend.queries[before:])


def test_missing_previous_trading_date_cannot_silently_use_older_open_day(source):
    source["calendar_records"][0]["is_trading_day"] = 1
    source["calendar_records"] = [row for row in source["calendar_records"] if row["calendar_date"] != "2026-10-22"]
    with pytest.raises(ValueError, match="Calendar evidence is incomplete"):
        make_snapshot(**source)


def test_saved_snapshot_requires_original_calendar_evidence(source):
    snapshot = make_snapshot(**source)
    assert len(snapshot["calendar_evidence"]) == len(snapshot["calendar"]) + 31
    del snapshot["calendar_evidence"][-5]
    # The saved compatibility calendar still claims a plausible previous date;
    # it must not be used to manufacture replacement evidence during validation.
    assert snapshot["calendar"][0]["pretrade_date"] == "2026-10-22"
    with pytest.raises(ValueError, match="Calendar evidence is incomplete"):
        _validate_envelope(snapshot)


def test_tampered_claimed_pretrade_is_checked_against_original_evidence(source):
    snapshot = make_snapshot(**source)
    snapshot["calendar"][0]["pretrade_date"] = "2026-09-22"
    with pytest.raises(ValueError, match="changed after validation"):
        _validate_envelope(snapshot)


def test_same_observation_in_different_timezones_has_one_snapshot_identity(source):
    first = make_snapshot(**source)
    source["observed_at"] = "2026-10-29T13:00:00+08:00"
    second = make_snapshot(**source)
    assert second == first


def test_v1_snapshot_cannot_bypass_required_source_evidence(source):
    snapshot = make_snapshot(**source)
    snapshot["schema_version"] = 1
    snapshot.pop("calendar_evidence")
    with pytest.raises(ValueError, match="schema/source"):
        _validate_envelope(snapshot)


def test_views_prioritize_source_time_and_ignore_pre_evidence_snapshots():
    from src.data_ingest.paper_snapshot import _view_queries

    queries = _view_queries("paper_test")
    for name in ("paper_calendar_rows", "paper_daily_rows"):
        sql = queries[name]
        assert "tuple(source_observed_at, published_at)" in sql
        assert "parseDateTime64BestEffort(JSONExtractString(payload, 'observed_at'), 6, 'UTC')" in sql
        assert "WHERE JSONExtractUInt(payload, 'schema_version') = 2" in sql
    assert "WHERE JSONExtractUInt(payload, 'schema_version') = 2" in queries["paper_coverage_rows"]


class BatchBackend(Backend):
    def handle(self, request):
        sql = request.url.params["query"]
        if sql.startswith("SELECT snapshot_id"):
            self.queries.append((sql, request.content.decode()))
            return httpx.Response(200, json={"data": [{"snapshot_id": key} for key in self.published]})
        if sql.startswith("INSERT"):
            self.queries.append((sql, request.content.decode()))
            if self.fail_insert:
                return httpx.Response(503, text="secret response body")
            rows = [json.loads(line) for line in request.content.splitlines()]
            self.published.update(row["snapshot_id"] for row in rows)
            return httpx.Response(200, text="")
        return super().handle(request)


def test_manual_batch_validates_every_snapshot_before_one_insert(source):
    first = make_snapshot(**source)
    source["observed_at"] = "2026-10-29T06:00:00+00:00"
    second = make_snapshot(**source)
    backend = BatchBackend()
    client = writer(backend)
    assert client.publish_many([first, second]) == 2
    inserts = [body for sql, body in backend.queries if sql.startswith("INSERT")]
    assert len(inserts) == 1 and len(inserts[0].splitlines()) == 2
    assert client.publish_many([first, second]) == 0
    assert len([sql for sql, _ in backend.queries if sql.startswith("INSERT")]) == 1
    backend.queries.clear()
    second["daily"][0]["close"] = 999
    with pytest.raises(ValueError):
        client.publish_many([first, second])
    assert not backend.queries


def test_failed_batch_insert_preserves_preexisting_data(source):
    backend = BatchBackend()
    backend.published = {"previous-valid-snapshot"}
    backend.fail_insert = True
    with pytest.raises(RuntimeError, match="HTTP 503"):
        writer(backend).publish_many([make_snapshot(**source)])
    assert backend.published == {"previous-valid-snapshot"}
