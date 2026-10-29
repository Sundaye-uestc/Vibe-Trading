"""Validated BaoStock snapshots for local A-share paper execution.

The write boundary publishes one complete immutable JSON envelope. Compatibility
views derive calendar, raw prices and status from that envelope; no partially
uploaded snapshot can become visible. Limits are not supplied by BaoStock and
are explicitly marked unavailable rather than invented.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

import httpx

_SOURCE_URL = "https://www.baostock.com/mainContent?fileName=history_A_stock_k_data.md"
_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,tradestatus,isST"
_MAX_DAYS = 366
_MAX_SYMBOLS = 10
_SCHEMA_VERSION = 2


def _day(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Dates must be YYYY-MM-DD")
    return date.fromisoformat(value).isoformat()


def validate_scope(symbols: list[str], start: str, end: str) -> list[str]:
    start, end = _day(start), _day(end)
    if date.fromisoformat(start) < date.min + timedelta(days=31):
        raise ValueError("Start date needs 31 days of calendar lookback")
    if not 0 <= (date.fromisoformat(end) - date.fromisoformat(start)).days < _MAX_DAYS:
        raise ValueError("Snapshot range must contain 1–366 calendar days")
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= _MAX_SYMBOLS:
        raise ValueError("Supply 1–10 SH/SZ equity symbols")
    if any(not isinstance(symbol, str) or not re.fullmatch(r"\d{6}\.(SH|SZ)", symbol) for symbol in symbols):
        raise ValueError("Use canonical SH/SZ symbols, e.g. 600522.SH")
    if len(set(symbols)) != len(symbols):
        raise ValueError("Duplicate symbols are not permitted")
    return sorted(symbols)


def _number(value: Any, *, positive: bool = True) -> float:
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        raise ValueError("Missing/boolean market numeric value")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid market numeric value") from None
    if not number.is_finite() or (number <= 0 if positive else number < 0):
        raise ValueError("Market numeric values must be finite and non-negative (prices positive)")
    result = float(number)
    if not math.isfinite(result):
        raise ValueError("Market numeric value exceeds supported range")
    return result


def _flag(value: Any) -> int:
    if str(value) not in {"0", "1"}:
        raise ValueError("Market status must be an explicit 0/1")
    return int(value)


def _date_range(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [(first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)]


def make_snapshot(
    *,
    symbols: list[str],
    start: str,
    end: str,
    calendar_records: list[dict],
    daily_records: dict[str, list[dict]],
    observed_at: str | None = None,
) -> dict:
    """Validate all source rows before producing an importable envelope."""
    symbols = validate_scope(symbols, start, end)
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    stamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Observation timestamp must include a timezone")
    observed_at = stamp.astimezone(timezone.utc).isoformat()
    local_stamp = stamp.astimezone(ZoneInfo("Asia/Shanghai"))
    completed = local_stamp.date() - timedelta(days=int((local_stamp.hour, local_stamp.minute) < (15, 30)))
    if date.fromisoformat(end) > completed:
        raise ValueError("Snapshot must end on a completed daily session (after 15:30 Asia/Shanghai)")
    calendar_map: dict[str, int] = {}
    for row in calendar_records:
        day, opened = _day(row["calendar_date"]), _flag(row["is_trading_day"])
        if day in calendar_map:
            raise ValueError("Duplicate calendar date")
        calendar_map[day] = opened
    scope = _date_range(start, end)
    evidence_start = (date.fromisoformat(start) - timedelta(days=31)).isoformat()
    evidence_scope = _date_range(evidence_start, end)
    if set(calendar_map) != set(evidence_scope):
        raise ValueError(
            "Calendar evidence is incomplete; every date in the 31-day lookback and import range must be explicit"
        )
    previous = max((day for day, opened in calendar_map.items() if opened and day < start), default=None)
    if previous is None:
        raise ValueError("Calendar must include an observed open day before the imported range")
    calendar = []
    for day in scope:
        calendar.append({"exchange": "SSE", "cal_date": day, "is_open": calendar_map[day], "pretrade_date": previous})
        if calendar_map[day]:
            previous = day
    open_dates = {day for day in scope if calendar_map[day]}
    if set(daily_records) != set(symbols):
        raise ValueError("Daily data must match the requested symbol set")
    daily = []
    for symbol in symbols:
        seen = set()
        native = symbol[-2:].lower() + "." + symbol[:6]
        for row in daily_records[symbol]:
            day = _day(row["date"])
            if row.get("code") != native or row.get("adjustflag") != "3":
                raise ValueError("Source symbol mismatch or non-raw adjustment flag")
            if day not in open_dates or day in seen:
                raise ValueError("Daily rows must be unique observed open dates in the requested range")
            seen.add(day)
            prices = {field: _number(row[field]) for field in ("open", "high", "low", "close", "preclose")}
            if (
                prices["low"] > min(prices["open"], prices["close"])
                or prices["high"] < max(prices["open"], prices["close"])
                or prices["low"] > prices["high"]
            ):
                raise ValueError("OHLC bounds are inconsistent")
            volume = _number(row["volume"], positive=False)
            if Decimal(str(row["volume"])) % 1:
                raise ValueError("BaoStock volume must be whole shares")
            daily.append(
                {
                    "ts_code": symbol,
                    "trade_date": day,
                    **prices,
                    "vol": volume / 100,
                    "volume_shares": volume,
                    "amount": _number(row["amount"], positive=False),
                    "is_st": _flag(row["isST"]),
                    "is_suspended": 1 - _flag(row["tradestatus"]),
                    "adjustment": "raw",
                }
            )
        if seen != open_dates:
            raise ValueError(f"Daily prices/status coverage is incomplete for {symbol}")
    daily.sort(key=lambda row: (row["ts_code"], row["trade_date"]))
    snapshot = {
        "schema_version": _SCHEMA_VERSION,
        "source": "baostock",
        "source_url": _SOURCE_URL,
        "observed_at": observed_at,
        "start": start,
        "end": end,
        "symbols": symbols,
        "calendar_evidence": [{"calendar_date": day, "is_trading_day": calendar_map[day]} for day in evidence_scope],
        "calendar": calendar,
        "daily": daily,
        "coverage": [
            {
                "dataset": dataset,
                "ts_code": symbol,
                "from_date": start,
                "to_date": end,
                "status": "complete" if dataset in {"raw_prices", "st_status", "suspensions"} else "unavailable",
            }
            for symbol in symbols
            for dataset in ("raw_prices", "st_status", "suspensions", "limits", "limit_events")
        ],
    }
    content = snapshot
    snapshot["snapshot_id"] = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()
    return snapshot


def _rows(result: Any) -> list[dict]:
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock query failed ({result.error_code})")
    rows = []
    while result.next():
        rows.append(dict(zip(result.fields, result.get_row_data(), strict=True)))
        if len(rows) > 5000:
            raise ValueError("Source response exceeds bounded snapshot size")
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock result iteration failed ({result.error_code})")
    return rows


def fetch_snapshot(symbols: list[str], start: str, end: str, *, timeout: float = 15) -> dict:
    """Fetch a small explicit source range using the existing bounded TCP guard."""
    symbols = validate_scope(symbols, start, end)
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("Source timeout must be within (0, 60] seconds")
    import baostock as bs
    from backtest.loaders.baostock_loader import _baostock_socket_guard

    earlier = (date.fromisoformat(start) - timedelta(days=31)).isoformat()
    with _baostock_socket_guard(timeout) as acquired:
        if not acquired:
            raise RuntimeError("BaoStock session is busy")
        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"BaoStock login failed ({login.error_code})")
        try:
            calendar = _rows(bs.query_trade_dates(start_date=earlier, end_date=end))
            daily = {
                symbol: _rows(
                    bs.query_history_k_data_plus(
                        symbol[-2:].lower() + "." + symbol[:6],
                        _FIELDS,
                        start_date=start,
                        end_date=end,
                        frequency="d",
                        adjustflag="3",
                    )
                )
                for symbol in symbols
            }
        finally:
            bs.logout()
    return make_snapshot(symbols=symbols, start=start, end=end, calendar_records=calendar, daily_records=daily)


class SnapshotWriter:
    """Explicit write-only operator boundary, never used by agent tools.

    Only ``vibe_paper`` or dedicated ``paper_*`` databases are accepted. Existing databases with
    unrelated objects are refused. The application continues to use its separate
    SELECT-only reader credentials and connector.
    """

    OBJECTS = {
        "paper_market_snapshots",
        "paper_calendar_rows",
        "paper_daily_rows",
        "paper_coverage_rows",
        "trade_calendar",
        "stk_factor_pro",
        "paper_daily_status",
        "paper_data_coverage",
        "stk_st_daily",
        "stk_suspend",
        "stk_limit",
        "stk_limit_list_d",
    }

    def __init__(self, url: str, user: str, password: str, database: str, *, client=None):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Supply an HTTP(S) ClickHouse origin without URL credentials")
        if database != "vibe_paper" and not re.fullmatch(r"paper_[a-zA-Z0-9_]{1,48}", database):
            raise ValueError("Imports require the dedicated vibe_paper or a paper_* database")
        self.database = database
        self.client = client or httpx.Client(
            base_url=url.rstrip("/"), auth=(user, password), timeout=30, trust_env=False, follow_redirects=False
        )

    def close(self):
        self.client.close()

    def _sql(self, statement: str, *, data: str = "", params=None):
        try:
            response = self.client.post("/", params={"query": statement, **(params or {})}, content=data.encode())
            if response.status_code != 200:
                raise RuntimeError(f"Snapshot database request failed (HTTP {response.status_code})")
            return response.text
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Snapshot database connection failed ({type(exc).__name__})") from None

    def verify_initialized(self, *, allow_empty: bool = False):
        """Read the managed signature; web updates never create or change DDL."""
        db = self.database
        objects = self._sql(
            "SELECT name, comment, engine, partition_key FROM system.tables WHERE database = {db:String} FORMAT JSON",
            params={"param_db": db},
        )
        rows = json.loads(objects)["data"]
        names = {row["name"] for row in rows}
        if not allow_empty and names != self.OBJECTS:
            raise ValueError("Managed paper database is missing required objects")
        if names - self.OBJECTS:
            raise ValueError("Refusing an existing database containing unrelated tables")
        expected_marker = (
            "vibe-paper-snapshot-v2-"
            + hashlib.sha256(json.dumps(_view_queries(db), sort_keys=True).encode()).hexdigest()
        )
        if names and not any(
            row["name"] == "paper_market_snapshots"
            and row.get("comment") == expected_marker
            and row.get("engine") == "ReplacingMergeTree"
            and not row.get("partition_key")
            for row in rows
        ):
            raise ValueError("Existing database does not have the expected managed-schema signature")
        if names:
            columns = json.loads(
                self._sql(
                    "SELECT name, type FROM system.columns WHERE database = {db:String} "
                    "AND table = 'paper_market_snapshots' ORDER BY position FORMAT JSON",
                    params={"param_db": db},
                )
            )["data"]
            if [(row["name"], row["type"]) for row in columns] != [
                ("snapshot_id", "String"),
                ("published_at", "UInt64"),
                ("payload", "String"),
            ]:
                raise ValueError("Existing snapshot table has incompatible columns")
        return expected_marker

    def initialize(self):
        db = self.database
        expected_marker = self.verify_initialized(allow_empty=True)
        self._sql(f"CREATE DATABASE IF NOT EXISTS {db}")
        self._sql(
            f"CREATE TABLE IF NOT EXISTS {db}.paper_market_snapshots (snapshot_id String, published_at UInt64, payload String) ENGINE=ReplacingMergeTree(published_at) ORDER BY snapshot_id COMMENT '{expected_marker}'"
        )
        for name, query in _view_queries(db).items():
            self._sql(f"CREATE VIEW IF NOT EXISTS {db}.{name} AS {query}")

    def publish(self, snapshot: dict) -> dict:
        """Publish only a fully validated, untampered snapshot; retries are idempotent."""
        _validate_envelope(snapshot)
        db = self.database
        existing = self._sql(
            f"SELECT count() AS n FROM {db}.paper_market_snapshots WHERE snapshot_id = {{id:String}} FORMAT JSON",
            params={"param_id": snapshot["snapshot_id"]},
        )
        if int(json.loads(existing)["data"][0]["n"]) > 0:
            return {"snapshot_id": snapshot["snapshot_id"], "imported": False, "daily_rows": len(snapshot["daily"])}
        envelope = {
            "snapshot_id": snapshot["snapshot_id"],
            "published_at": time.time_ns(),
            "payload": json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
        }
        self._sql(
            f"INSERT INTO {db}.paper_market_snapshots FORMAT JSONEachRow",
            data=json.dumps(envelope, ensure_ascii=False, allow_nan=False) + "\n",
        )
        return {"snapshot_id": snapshot["snapshot_id"], "imported": True, "daily_rows": len(snapshot["daily"])}

    def publish_many(self, snapshots: list[dict]) -> int:
        """Publish one bounded update batch in one synchronous MergeTree insert.

        All fetches and envelope checks finish before this method is called.
        One JSONEachRow block publishes the complete set atomically. A lost
        response can be reconciled by status and retried without duplicate rows.
        """
        if not snapshots:
            return 0
        if len(snapshots) > 10:
            raise ValueError("At most ten snapshots per update")
        for snapshot in snapshots:
            _validate_envelope(snapshot)
        unique = {snapshot["snapshot_id"]: snapshot for snapshot in snapshots}
        params = {f"param_id{i}": key for i, key in enumerate(unique)}
        clauses = ",".join(f"{{id{i}:String}}" for i in range(len(unique)))
        existing = json.loads(self._sql(
            f"SELECT snapshot_id FROM {self.database}.paper_market_snapshots "
            f"WHERE snapshot_id IN ({clauses}) FORMAT JSON", params=params,
        ))["data"]
        seen = {row["snapshot_id"] for row in existing}
        rows = [{"snapshot_id": key, "published_at": time.time_ns(),
                 "payload": json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False)}
                for key, snapshot in unique.items() if key not in seen]
        if not rows:
            return 0
        payload = "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows)
        if len(payload.encode()) > 8_000_000:
            raise ValueError("Update batch exceeds supported size")
        self._sql(
            f"INSERT INTO {self.database}.paper_market_snapshots "
            "SETTINGS async_insert=0, max_execution_time=5, input_format_parallel_parsing=0, "
            "max_insert_block_size=100, min_insert_block_size_rows=100, "
            "min_insert_block_size_bytes=10000000 FORMAT JSONEachRow", data=payload,
        )
        return len(rows)


def _validate_envelope(snapshot: dict):
    # Reconstruct source-shaped records through the same strict validator. The
    # persisted envelope is not trusted just because it is valid JSON.
    if snapshot.get("schema_version") != _SCHEMA_VERSION or snapshot.get("source") != "baostock":
        raise ValueError("Unknown snapshot schema/source")
    records = snapshot["calendar_evidence"]
    daily = {symbol: [] for symbol in snapshot["symbols"]}
    for row in snapshot["daily"]:
        symbol = row["ts_code"]
        if row.get("adjustment") != "raw":
            raise ValueError("Snapshot prices must be raw")
        daily[symbol].append(
            {
                "date": row["trade_date"],
                "code": symbol[-2:].lower() + "." + symbol[:6],
                **{key: row[key] for key in ("open", "high", "low", "close", "preclose", "amount")},
                "volume": row["volume_shares"],
                "adjustflag": "3",
                "tradestatus": str(1 - row["is_suspended"]),
                "isST": str(row["is_st"]),
            }
        )
    verified = make_snapshot(
        symbols=snapshot["symbols"],
        start=snapshot["start"],
        end=snapshot["end"],
        calendar_records=records,
        daily_records=daily,
        observed_at=snapshot["observed_at"],
    )
    if json.dumps(verified, sort_keys=True, allow_nan=False) != json.dumps(snapshot, sort_keys=True, allow_nan=False):
        raise ValueError("Snapshot payload or digest was changed after validation")


def _view_queries(db: str) -> dict[str, str]:
    snapshots = f"(SELECT snapshot_id, published_at, payload FROM {db}.paper_market_snapshots FINAL WHERE JSONExtractUInt(payload, 'schema_version') = 2)"
    source = "JSONExtractString(payload, 'source') AS source"
    observed = "JSONExtractString(payload, 'observed_at') AS observed_at"
    observation_order = (
        "parseDateTime64BestEffort(JSONExtractString(payload, 'observed_at'), 6, 'UTC') AS source_observed_at"
    )
    # Row-level argMax selects an entire tuple (or JSON object), so status and
    # price cannot come from different revisions of the same symbol/date.
    return {
        "paper_calendar_rows": f"SELECT JSONExtractString(row, 'exchange') AS exchange, toDate(JSONExtractString(row, 'cal_date')) AS cal_date, argMax(row, tuple(source_observed_at, published_at)) AS record FROM (SELECT published_at, {observation_order}, arrayJoin(JSONExtractArrayRaw(payload, 'calendar')) AS row FROM {snapshots}) GROUP BY exchange, cal_date",
        "trade_calendar": f"SELECT exchange, cal_date, JSONExtractUInt(record, 'is_open') AS is_open, toDate(JSONExtractString(record, 'pretrade_date')) AS pretrade_date FROM {db}.paper_calendar_rows",
        "paper_daily_rows": f"SELECT JSONExtractString(row, 'ts_code') AS ts_code, toDate(JSONExtractString(row, 'trade_date')) AS trade_date, argMax(tuple(row, source, observed_at, snapshot_id), tuple(source_observed_at, published_at)) AS latest FROM (SELECT published_at, snapshot_id, {source}, {observed}, {observation_order}, arrayJoin(JSONExtractArrayRaw(payload, 'daily')) AS row FROM {snapshots}) GROUP BY ts_code, trade_date",
        "stk_factor_pro": "SELECT ts_code, trade_date, "
        + ", ".join(
            f"JSONExtractFloat(latest.1, '{key}') AS {key}"
            for key in ("open", "high", "low", "close", "preclose", "vol")
        )
        + f" FROM {db}.paper_daily_rows",
        "paper_daily_status": f"SELECT ts_code, trade_date, JSONExtractUInt(latest.1, 'is_st') AS is_st, JSONExtractUInt(latest.1, 'is_suspended') AS is_suspended, latest.2 AS source, latest.3 AS observed_at, latest.4 AS snapshot_id FROM {db}.paper_daily_rows",
        "paper_coverage_rows": f"SELECT JSONExtractString(row, 'dataset') AS dataset, JSONExtractString(row, 'ts_code') AS ts_code, toDate(JSONExtractString(row, 'from_date')) AS from_date, toDate(JSONExtractString(row, 'to_date')) AS to_date, JSONExtractString(row, 'status') AS status, source, observed_at, snapshot_id FROM (SELECT snapshot_id, {source}, {observed}, arrayJoin(JSONExtractArrayRaw(payload, 'coverage')) AS row FROM {snapshots})",
        "paper_data_coverage": f"SELECT * FROM {db}.paper_coverage_rows",
        "stk_st_daily": f"SELECT ts_code, trade_date, 'ST' AS type, 'BaoStock observed isST=1' AS type_name FROM {db}.paper_daily_status WHERE is_st = 1",
        "stk_suspend": f"SELECT ts_code, trade_date, 'S' AS suspend_type, 'daily' AS suspend_timing FROM {db}.paper_daily_status WHERE is_suspended = 1",
        "stk_limit": "SELECT CAST('' AS String) AS ts_code, toDate('1970-01-01') AS trade_date, toFloat64(0) AS up_limit, toFloat64(0) AS down_limit WHERE 0",
        "stk_limit_list_d": "SELECT CAST('' AS String) AS ts_code, toDate('1970-01-01') AS trade_date, CAST('' AS String) AS `limit` WHERE 0",
    }
