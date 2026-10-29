"""Explicit manual updates of a bounded A-share watchlist.

The API accepts symbols only. Writer credentials and destination are selected by
the operator, and must match the configured reader. A short database lease
serializes watchlist edits and updates across API processes. Network work runs
in a terminable child; only one complete validated batch is ever published.
"""

from __future__ import annotations

import json
import math
import multiprocessing
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from dotenv import dotenv_values

from src.clickhouse_connector import ClickHouseConnector
from src.config.accessor import get_env_config
from src.config.paths import get_runtime_root
from src.data_ingest.paper_snapshot import SnapshotWriter, fetch_snapshot
from src.tools.paper_trade.calendar import _cn_a_open_map, _normalize_date
from src.tools.paper_trade.coverage import _observed, _text, daily_status

LOOKBACK_DAYS = 200
UPDATE_TIMEOUT = 50.0
LEASE_SECONDS = 90


class UpdateBusy(ValueError):
    """Another update owns the current watchlist."""


def validate_symbols(symbols):
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 10:
        raise ValueError("Choose 1–10 SH/SZ symbols")
    if any(not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}\.(SH|SZ)", symbol) for symbol in symbols):
        raise ValueError("Use canonical SH/SZ symbols such as 600522.SH")
    if len(set(symbols)) != len(symbols):
        raise ValueError("Duplicate symbols are not permitted")
    return sorted(symbols)


def latest_closed_date(now: datetime) -> str:
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    # This is a completed calendar-date boundary, not a guessed exchange day.
    return (local.date() - timedelta(days=int((local.hour, local.minute) < (15, 30)))).isoformat()


def _origin(url: str) -> tuple[str, str, int]:
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
        raise ValueError("Invalid writer origin")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def writer_settings(reader: ClickHouseConnector) -> dict:
    """Read only the server's fixed private file; never infer writer authority."""
    value = get_env_config().data.vibe_trading_paper_writer_env.strip()
    if not value:
        raise ValueError("Manual updates are not configured")
    path = Path(value).expanduser()
    if (
        not path.is_absolute()
        or any(part.is_symlink() for part in (path, *path.parents))
        or not path.is_file()
        or path.stat().st_size > 16384
    ):
        raise ValueError("Invalid private writer configuration")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Writer configuration must be private")
    config = dotenv_values(path)
    names = {
        "url": "CLICKHOUSE_INGEST_URL",
        "user": "CLICKHOUSE_INGEST_USER",
        "password": "CLICKHOUSE_INGEST_PASSWORD",
        "database": "CLICKHOUSE_INGEST_DATABASE",
    }
    result = {key: config.get(name) for key, name in names.items()}
    if any(not isinstance(value, str) or not value.strip() for value in result.values()):
        raise ValueError("Incomplete writer configuration")
    if _origin(result["url"]) != _origin(reader.url) or result["database"] != reader.database:
        raise ValueError("Writer destination must match the application reader")
    if result["user"] == get_env_config().data.clickhouse_user:
        raise ValueError("Use a separate writer account")
    return result


def _base(symbols: list[str], end: str, configured: bool, code: str) -> dict:
    return {
        "configured": configured,
        "update_available": False,
        "status_code": code,
        "watchlist": symbols,
        "latest_closed_date": end,
        "coverage": [
            {
                "symbol": symbol,
                "from_date": None,
                "to_date": None,
                "latest_bar_date": None,
                "missing_dates": [],
                "missing_count": 0,
                "status": "unavailable",
            }
            for symbol in symbols
        ],
        "last_job": None,
        "limits": {"max_symbols": 10, "lookback_days": LOOKBACK_DAYS},
    }


def inspect_coverage(symbols: list[str], end: str, *, connector=None) -> dict:
    """Read local database coverage; no upstream market-data requests."""
    configured = False
    try:
        reader = connector or ClickHouseConnector()
        reader.timeout = min(reader.timeout, 3)
        writer_settings(reader)
        configured = True
        if not reader.has_managed_paper_data():
            raise ValueError("Managed snapshot database required")
        if not symbols:
            return _base(symbols, end, configured, "watchlist_empty")
        start = (date.fromisoformat(end) - timedelta(days=LOOKBACK_DAYS)).isoformat()
        days = [(date.fromisoformat(start) + timedelta(days=i)).isoformat() for i in range(LOOKBACK_DAYS + 1)]
        calendar = _cn_a_open_map(reader, start, end)
        observations = daily_status(reader, symbols, start, end, None)
        if observations is None:
            raise ValueError("Managed daily observations required")
        clauses = " OR ".join(f"ts_code={{symbol{i}:String}}" for i in range(len(symbols)))
        params = {
            "param_start": start,
            "param_end": end,
            **{f"param_symbol{i}": symbol for i, symbol in enumerate(symbols)},
        }
        bars = reader.query(
            "SELECT ts_code, trade_date, open, high, low, close FROM stk_factor_pro "
            "WHERE trade_date >= {start:String} AND trade_date <= {end:String} AND ("
            + clauses
            + ") ORDER BY ts_code, trade_date LIMIT 10001",
            params=params,
        )
        coverage = reader.query(
            "SELECT dataset, ts_code, from_date, to_date, status, source, snapshot_id, observed_at "
            "FROM paper_data_coverage WHERE dataset='raw_prices' "
            "AND from_date <= {end:String} AND to_date >= {start:String} AND (" + clauses + ") LIMIT 10001",
            params=params,
        )
        if len(bars) > 10000 or len(coverage) > 10000:
            raise ValueError("Coverage response too large")
        prices = {}
        duplicate = set()
        for row in bars.to_dict("records"):
            key = str(row["ts_code"]), _normalize_date(row["trade_date"])
            if key in prices:
                duplicate.add(key)
            values = [row[name] for name in ("open", "high", "low", "close")]
            valid = all(not isinstance(v, bool) and math.isfinite(float(v)) and float(v) > 0 for v in values)
            if valid:
                op, high, low, close = map(float, values)
                valid = low <= min(op, close) <= max(op, close) <= high
            prices[key] = valid
        ranges = {symbol: [] for symbol in symbols}
        for row in coverage.to_dict("records"):
            symbol = row["ts_code"]
            if symbol not in ranges or row["status"] != "complete":
                raise ValueError("Invalid raw coverage declaration")
            first, last = _normalize_date(row["from_date"]), _normalize_date(row["to_date"])
            _text(row["source"])
            _text(row["snapshot_id"])
            if first > last or _observed(row["observed_at"]).date().isoformat() < last:
                raise ValueError("Invalid raw coverage provenance")
            ranges[symbol].append((first, last))
        result = _base(symbols, end, configured, "ready")
        result["coverage"] = []
        for symbol in symbols:
            missing = [
                day
                for day in days
                if day not in calendar
                or not any(first <= day <= last for first, last in ranges[symbol])
                or (
                    calendar[day]
                    and (
                        (symbol, day) not in observations
                        or not prices.get((symbol, day), False)
                        or (symbol, day) in duplicate
                    )
                )
            ]
            valid_days = [day for day in days if day not in missing]
            # Surface the latest contiguous verified suffix, never min/max
            # across a hole. Missing dates remain explicit alongside it.
            suffix = []
            if valid_days:
                cursor = date.fromisoformat(valid_days[-1])
                while cursor.isoformat() in valid_days:
                    suffix.append(cursor.isoformat())
                    cursor -= timedelta(days=1)
            price_dates = [day for (code, day), valid in prices.items() if code == symbol and valid]
            result["coverage"].append(
                {
                    "symbol": symbol,
                    "from_date": min(suffix) if suffix else None,
                    "to_date": max(suffix) if suffix else None,
                    "latest_bar_date": max(price_dates) if price_dates else None,
                    "missing_dates": missing[:31],
                    "missing_count": len(missing),
                    "status": "missing" if missing else "ready",
                }
            )
        result["update_available"] = any(row["missing_count"] for row in result["coverage"])
        return result
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return _base(symbols, end, configured, "source_unavailable" if configured else "not_configured")


def _start_watchdog(seconds: float = UPDATE_TIMEOUT + 2):
    watchdog = threading.Timer(seconds, lambda: os._exit(124))
    watchdog.daemon = True
    watchdog.start()
    return watchdog


def _update_worker(pipe, symbols: list[str], end: str, guard=None):
    """All work is contained in one child and one final batch insert."""
    writer = None
    # A parent killed during a request cannot leave an orphan writer beyond
    # the lease. The publish gate also rejects a lease lost during suspension.
    watchdog = None
    if guard is not None:
        watchdog = _start_watchdog()
    try:
        reader = ClickHouseConnector()
        settings = writer_settings(reader)
        http = httpx.Client(
            base_url=settings["url"].rstrip("/"),
            auth=(settings["user"], settings["password"]),
            timeout=5,
            trust_env=False,
            follow_redirects=False,
        )
        writer = SnapshotWriter(**settings, client=http)
        writer.verify_initialized()
        status = inspect_coverage(symbols, end, connector=reader)
        pipe.send({"progress": status})
        if status["status_code"] != "ready":
            pipe.send({"code": "update_failed", "status": status})
            return
        plans = [(item["symbol"], item["missing_dates"][0]) for item in status["coverage"] if item["missing_count"]]
        if not plans:
            pipe.send({"code": "up_to_date", "status": status, "updated_symbols": [], "imported_snapshots": 0})
            return
        snapshots = []
        for symbol, start in plans:
            # The first gap through the cutoff is bounded to 201 dates; this
            # fills holes while one new date normally means a one-day fetch.
            snapshots.append(fetch_snapshot([symbol], start, end, timeout=8))
        if guard is not None:
            database, job_id = guard
            with sqlite3.connect(database, timeout=2) as db:
                row = db.execute("SELECT document,lease_until FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or json.loads(row[0])["status"] != "running" or row[1] - time.time() < 15:
                raise RuntimeError("Update lease no longer belongs to this worker")
        imported = writer.publish_many(snapshots)
        refreshed = inspect_coverage(symbols, end, connector=reader)
        pipe.send(
            {
                "code": "updated",
                "status": refreshed,
                "updated_symbols": [symbol for symbol, _ in plans],
                "imported_snapshots": imported,
            }
        )
    except ValueError:
        pipe.send({"code": "data_not_ready"})
    except Exception:
        pipe.send({"code": "update_failed"})
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if writer is not None:
            writer.close()
        pipe.close()


def _status_worker(pipe, symbols, end):
    try:
        pipe.send(inspect_coverage(symbols, end))
    finally:
        pipe.close()


def reconcile_coverage(symbols, end, prior, *, timeout=3):
    """After an uncertain write, try a bounded read before returning its status."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_status_worker, args=(sender, symbols, end), daemon=True)
    try:
        process.start()
        sender.close()
        if receiver.poll(timeout):
            return receiver.recv()
    except (EOFError, OSError, ValueError, RuntimeError):
        pass
    finally:
        if process.pid is not None:
            process.join(timeout=0.1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=0.5)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.5)
            process.close()
        receiver.close()
        sender.close()
    return prior


class PaperDataService:
    def __init__(self, path=None, *, now=None, worker=_update_worker, timeout=UPDATE_TIMEOUT):
        self.path = path or get_runtime_root() / "paper-trading" / "data-updates.db"
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.worker, self.timeout = worker, timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), symbols TEXT NOT NULL)"
            )
            db.execute("INSERT OR IGNORE INTO settings VALUES (1, '[]')")
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, document TEXT NOT NULL, lease_until REAL NOT NULL)"
            )

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _state(self, db):
        symbols = json.loads(db.execute("SELECT symbols FROM settings WHERE id=1").fetchone()[0])
        row = db.execute("SELECT document,lease_until FROM jobs ORDER BY rowid DESC LIMIT 1").fetchone()
        job = json.loads(row[0]) if row else None
        if job and job["status"] == "running" and row[1] < self.now().timestamp():
            job.update(status="failed", code="interrupted", finished_at=self.now().isoformat())
            db.execute("UPDATE jobs SET document=?,lease_until=0 WHERE id=?", (json.dumps(job), job["id"]))
        return symbols, job

    @staticmethod
    def _attach(status, job):
        status["last_job"] = job
        if job and job["status"] == "running":
            status["update_available"] = False
        return status

    def status(self):
        with self._db() as db:
            symbols, job = self._state(db)
        return self._attach(inspect_coverage(symbols, latest_closed_date(self.now())), job)

    def save_watchlist(self, symbols):
        symbols = validate_symbols(symbols)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            _, job = self._state(db)
            if job and job["status"] == "running":
                raise UpdateBusy("A data update is running; wait before changing the watchlist")
            db.execute("UPDATE settings SET symbols=? WHERE id=1", (json.dumps(symbols),))
        return self.status()

    def update(self):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            symbols, previous = self._state(db)
            if previous and previous["status"] == "running":
                raise UpdateBusy("A data update is already running")
            validate_symbols(symbols)
            now = self.now()
            job = {
                "id": uuid.uuid4().hex,
                "status": "running",
                "code": "updating",
                "started_at": now.isoformat(),
                "finished_at": None,
                "updated_symbols": [],
                "imported_snapshots": 0,
            }
            db.execute("INSERT INTO jobs VALUES (?,?,?)", (job["id"], json.dumps(job), now.timestamp() + LEASE_SECONDS))
        end = latest_closed_date(now)
        status = _base(symbols, end, False, "source_unavailable")
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        args = (
            (sender, symbols, end, (str(self.path), job["id"]))
            if self.worker is _update_worker
            else (sender, symbols, end)
        )
        process = context.Process(target=self.worker, args=args, daemon=True)
        result = {"code": "update_failed"}
        try:
            process.start()
            sender.close()
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                if not receiver.poll(max(0, deadline - time.monotonic())):
                    result = {"code": "update_timeout"}
                    break
                message = receiver.recv()
                if "progress" in message:
                    status = message["progress"]
                    continue
                result = message
                status = result.get("status", status)
                break
            else:
                result = {"code": "update_timeout"}
        except (EOFError, OSError, ValueError, RuntimeError):
            result = {"code": "update_failed"}
        finally:
            if process.pid is not None:
                process.join(timeout=0.2)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=1)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1)
                process.close()
            receiver.close()
            sender.close()
        code = result["code"]
        if code in {"update_timeout", "update_failed"}:
            status = reconcile_coverage(symbols, end, status)
        job.update(
            status="success" if code == "updated" else "noop" if code == "up_to_date" else "failed",
            code=code,
            finished_at=self.now().isoformat(),
            outcome_unknown=code in {"update_timeout", "update_failed"},
            updated_symbols=result.get("updated_symbols", []),
            imported_snapshots=result.get("imported_snapshots", 0),
        )
        with self._db() as db:
            db.execute("UPDATE jobs SET document=?,lease_until=0 WHERE id=?", (json.dumps(job), job["id"]))
        return self._attach(status, job)
