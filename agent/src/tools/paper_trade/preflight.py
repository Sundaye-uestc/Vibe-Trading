"""Read-only, bounded diagnostics for the daily paper-trading data contract.

This is not settlement and never constructs PaperAccounts or executes a strategy.
Queryable event tables cannot prove ingestion completeness: their status remains
a warning until the operator has established that their requested coverage is real.
"""

from __future__ import annotations

import datetime as dt
import multiprocessing
import re
import threading
import time
from dataclasses import asdict
from typing import Any

import numpy as np

from . import calendar, data
from .coverage import PaperDataUnavailable, daily_status, verify_event_rows

MAX_DAYS = 31
MAX_SYMBOLS = 10
TIMEOUT_SECONDS = 45
_AUTO = object()
_CAPACITY = threading.BoundedSemaphore(2)
_SYMBOL_PATTERNS = {
    "cn_a": r"[0-9]{6}\.(SH|SZ|BJ)",
    "hk": r"[0-9]{1,5}\.HK",
    "us": r"[A-Z][A-Z0-9.-]{0,14}\.US",
}
_EVENT_TABLES = (
    ("limits", "stk_limit", "up_limit, down_limit"),
    ("st_status", "stk_st_daily", "type, type_name"),
    ("suspensions", "stk_suspend", "suspend_type, suspend_timing"),
    ("limit_events", "stk_limit_list_d", "`limit`"),
)


def validate_request(market: str, from_date: str, to_date: str, symbols: list[str]) -> dict:
    if market not in _SYMBOL_PATTERNS:
        raise ValueError("Unsupported paper market")
    for value in (from_date, to_date):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("Dates must use YYYY-MM-DD")
    try:
        start, end = dt.date.fromisoformat(from_date), dt.date.fromisoformat(to_date)
    except ValueError:
        raise ValueError("Invalid calendar date") from None
    if not 0 <= (end - start).days < MAX_DAYS:
        raise ValueError(f"Check a forward range of at most {MAX_DAYS} calendar days")
    # data.fetch_market_data reserves 200 days of lookback.
    if start < dt.date.min + dt.timedelta(days=data.LOOKBACK_CALENDAR_SPAN_DAYS):
        raise ValueError("Start date does not allow the required price lookback")
    if not isinstance(symbols, list) or len(symbols) > MAX_SYMBOLS:
        raise ValueError(f"Check at most {MAX_SYMBOLS} symbols at a time")
    normalized = []
    for symbol in symbols:
        if not isinstance(symbol, str) or not re.fullmatch(_SYMBOL_PATTERNS[market], symbol.strip().upper()):
            raise ValueError("Use market-qualified symbols, e.g. 600000.SH, 00700.HK or AAPL.US")
        normalized.append(symbol.strip().upper())
    return {"market": market, "from_date": from_date, "to_date": to_date,
            "symbols": sorted(set(normalized))}


def _check(key: str, status: str, message: str, missing_dates=()) -> dict:
    return {"key": key, "status": status, "message": message, "missing_dates": list(missing_dates)}


def _report(request: dict, checks: list[dict], *, bars=(), provenance=None, warnings=()) -> dict:
    statuses = {item["status"] for item in checks}
    status = "blocked" if "blocked" in statuses else "warning" if statuses & {"warning", "not_checked"} else "ready"
    return {**request, "ready": status == "ready", "status": status, "checks": checks,
            "bars": list(bars), "provenance": provenance or {
                "source": "unavailable", "bars_as_of": None, "ch_as_of": None, "network_as_of": None,
            }, "warnings": list(warnings)}


def _days(start: str, end: str) -> list[str]:
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    return [(first + dt.timedelta(days=n)).isoformat() for n in range((last - first).days + 1)]


def _connect() -> tuple[Any, dict]:
    from src.config.accessor import get_env_config
    from src.clickhouse_connector import ClickHouseConnector

    try:
        if not get_env_config().data.clickhouse_host.strip():
            return None, _check("connection", "blocked", "ClickHouse is not configured. Configure the data connection and supply the documented calendar and event tables.")
        connector = ClickHouseConnector()
        connector.query("SELECT 1")
    except Exception:  # details can contain credentials, URLs and server bodies
        return None, _check("connection", "blocked", "Cannot read ClickHouse. Check its configuration, credentials and network connection.")
    return connector, _check("connection", "ready", "Authenticated read-only ClickHouse query succeeded.")


def _calendar_check(request: dict, connector: Any) -> tuple[dict, list[str] | None]:
    if request["market"] != "cn_a":
        return _check("calendar", "warning", "This market has no authoritative exchange calendar in the paper engine. Missing weekday bars do not confirm a holiday."), calendar.hk_us_candidate_days(request["from_date"], request["to_date"])
    all_days = _days(request["from_date"], request["to_date"])
    if connector is None:
        return _check("calendar", "blocked", "A complete A-share calendar is required, including explicit closed days. No weekday fallback is used.", all_days), None
    try:
        open_map = calendar._cn_a_open_map(connector, request["from_date"], request["to_date"])
    except Exception:
        return _check("calendar", "blocked", "Calendar rows could not be read or contain invalid/conflicting dates or open flags."), None
    missing = [day for day in all_days if day not in open_map]
    if missing:
        return _check("calendar", "blocked", "Calendar coverage is incomplete. Supply explicit open/closed rows for the missing dates.", missing), None
    trading = [day for day in all_days if open_map[day]]
    return _check("calendar", "ready", f"Calendar covers all {len(all_days)} dates; {len(trading)} open sessions."), trading


def _event_checks(request: dict, connector: Any, trading: list[str] | None) -> list[dict]:
    if request["market"] != "cn_a":
        return [_check(key, "not_checked", "A-share-specific table; not applicable to this market.") for key, _, _ in _EVENT_TABLES]
    if connector is None:
        return [_check(key, "blocked", f"Cannot read {table} without a connected ClickHouse source.") for key, table, _ in _EVENT_TABLES]
    if not request["symbols"]:
        return [_check(key, "not_checked", "Enter symbols to check the event and price-limit tables.") for key, _, _ in _EVENT_TABLES]
    try:
        observations = daily_status(connector, request["symbols"], request["from_date"], request["to_date"], trading)
        observation_error = None
    except PaperDataUnavailable:
        observations = None
        observation_error = "Managed daily status coverage is missing, inconsistent or unreadable. Import the requested symbols and dates."
    params = {"param_start": request["from_date"], "param_end": request["to_date"]}
    clauses = []
    for i, symbol in enumerate(request["symbols"]):
        params[f"param_symbol{i}"] = symbol
        clauses.append(f"ts_code = {{symbol{i}:String}}")
    results = []
    for key, table, columns in _EVENT_TABLES:
        if key in {"st_status", "suspensions"} and observation_error:
            results.append(_check(key, "blocked", observation_error))
            continue
        sql = (f"SELECT ts_code, trade_date, {columns} FROM {table} "
               "WHERE trade_date >= {start:String} AND trade_date <= {end:String} AND ("
               + " OR ".join(clauses) + ") LIMIT 10001")
        try:
            frame = connector.query(sql, params=params)
            if len(frame) > 10000:
                raise ValueError("Event response exceeded bound")
            # Query success alone is insufficient: reject malformed rows rather
            # than declaring an unusable source available.
            observed = set()
            for record in frame.to_dict("records"):
                day = calendar._normalize_date(record["trade_date"])
                symbol = str(record["ts_code"])
                if symbol not in request["symbols"] or not request["from_date"] <= day <= request["to_date"]:
                    raise ValueError("Event row lies outside requested snapshot")
                if key == "limits":
                    up, down = record["up_limit"], record["down_limit"]
                    if (isinstance(up, bool) or isinstance(down, bool)
                            or not np.isfinite(float(up)) or not np.isfinite(float(down))
                            or not 0 < float(down) <= float(up)):
                        raise ValueError("Invalid price band")
                    if (symbol, day) in observed:
                        raise ValueError("Duplicate price band")
                elif key == "suspensions" and str(record["suspend_type"]).strip().upper() not in {"S", "R"}:
                    raise ValueError("Invalid suspension flag")
                elif key == "limit_events" and str(record["limit"]).strip() not in {"U", "D", "Z"}:
                    raise ValueError("Invalid lock-side flag")
                observed.add((symbol, day))
            if key in {"st_status", "suspensions"} and observations is not None:
                verify_event_rows(observations, frame.to_dict("records"), key)
                if trading is None:
                    results.append(_check(key, "warning", "Daily provider observations are available, but the calendar is incomplete."))
                else:
                    sources = ", ".join(sorted({row["source"] for row in observations.values()}))
                    results.append(_check(key, "ready", f"Explicit daily provider observations cover the requested dates, including normal/no-event states. Source: {sources}."))
            elif key == "limits" and trading is not None:
                missing = [day for day in trading if any((symbol, day) not in observed for symbol in request["symbols"])]
                if not missing:
                    results.append(_check(key, "ready", "Observed daily price bands cover the requested symbols and open dates."))
                else:
                    results.append(_check(key, "warning", "Daily price bands are missing for at least one symbol on these dates. Settlement may use disclosed board/ST heuristics when a valid previous close exists.", missing))
            else:
                results.append(_check(key, "warning", f"{table} is readable ({len(frame)} rows). Query success and empty rows do not prove ingestion coverage; verify that the source covers the requested period."))
        except Exception:
            results.append(_check(key, "blocked", f"{table} is unavailable or returned invalid rows. Check the documented table and data contract."))
    return results


def inspect_data(market: str, from_date: str, to_date: str, symbols: list[str], *, connector=_AUTO, loader=data._AUTO, _progress=None) -> dict:
    """Inspect injected or production sources without touching a paper ledger."""
    request = validate_request(market, from_date, to_date, symbols)
    checks = []
    if market == "cn_a":
        if connector is _AUTO:
            connector, connection = _connect()
        else:
            connection = _check("connection", "ready" if connector is not None else "blocked", "Using the supplied read-only source." if connector is not None else "ClickHouse is unavailable.")
        checks.append(connection)
    else:
        connector = None
        checks.append(_check("connection", "not_checked", "ClickHouse is not required for this market."))
    if _progress is not None:
        _progress(_report(request, checks))
    cal_check, trading = _calendar_check(request, connector)
    checks.append(cal_check)
    if _progress is not None:
        _progress(_report(request, checks))
    checks.extend(_event_checks(request, connector, trading))
    if _progress is not None:
        _progress(_report(request, checks))
    if not request["symbols"]:
        checks.append(_check("prices", "not_checked", "Enter symbols to verify unadjusted execution prices."))
        return _report(request, checks)
    try:
        result = data.fetch_market_data(market, request["symbols"], from_date, to_date,
                                       loader=loader,
                                       ch_client=data.ClickHouseBarsClient(connector) if connector is not None else None)
    except Exception:
        checks.append(_check("prices", "blocked", "Raw execution prices could not be read from the configured sources."))
        return _report(request, checks)
    bars = []
    missing_all = set()
    invalid_all = set()
    for symbol in request["symbols"]:
        served = result.data[symbol]
        frame = served.frame
        missing = sorted(set(trading or ()) - served.bar_dates)
        invalid = set(day for day in served.untradable_dates if from_date <= day <= to_date)
        if not frame.empty:
            impossible = ((frame["high"] < frame["low"])
                          | (frame["open"] > frame["high"]) | (frame["open"] < frame["low"])
                          | (frame["close"] > frame["high"]) | (frame["close"] < frame["low"]))
            invalid.update(day.strftime("%Y-%m-%d") for day in frame.index[impossible]
                           if from_date <= day.strftime("%Y-%m-%d") <= to_date)
        invalid = sorted(invalid)
        missing_all.update(missing)
        invalid_all.update(invalid)
        bars.append({"symbol": symbol, "as_of": frame.index[-1].strftime("%Y-%m-%d") if not frame.empty else None,
                     "missing_dates": missing, "invalid_dates": invalid})
    # No calendar means missing dates are unknown, never an empty successful
    # coverage check. Empty/all-invalid frames are always blocked.
    if any(item["as_of"] is None for item in bars) or invalid_all:
        prices = _check("prices", "blocked", "Raw prices are absent or invalid for one or more symbols. Review per-symbol coverage.", sorted(missing_all))
    elif trading is None:
        prices = _check("prices", "warning", "Raw prices were read, but coverage cannot be established until the calendar is complete.")
    elif missing_all:
        prices = _check("prices", "warning", "Some expected daily bars are missing. This may reflect suspension, a holiday in a market without a calendar, or missing data; confirm the cause before settlement.", sorted(missing_all))
    else:
        prices = _check("prices", "ready", "Unadjusted execution bars cover the requested symbols and checked dates.")
    checks.append(prices)
    return _report(request, checks, bars=bars, provenance=asdict(result.provenance), warnings=result.warnings)


def _failed_check(request: dict, previous: dict | None, message: str) -> dict:
    previous = previous or {}
    return _report(request, [*previous.get("checks", []), _check("execution", "blocked", message)],
                   bars=previous.get("bars", ()), provenance=previous.get("provenance"),
                   warnings=previous.get("warnings", ()))


def _worker(connection, request):
    previous = None

    def progress(snapshot):
        nonlocal previous
        previous = snapshot
        # Only three bounded snapshots are emitted, before slow source calls.
        # They contain diagnostics, never raw frames or credentials.
        connection.send(("progress", snapshot))

    try:
        connection.send(inspect_data(**request, _progress=progress))
    except Exception:
        connection.send(_failed_check(request, previous, "Data inspection failed. Check the source configuration and retry."))
    finally:
        connection.close()


def run_preflight(market: str, from_date: str, to_date: str, symbols: list[str], *, timeout_seconds=TIMEOUT_SECONDS, _target=_worker) -> dict:
    """Bound the entire network check, including hung third-party loaders.

The child is joined/terminated before releasing the slot; timed-out requests
cannot leave background source calls accumulating in the API process.
"""
    request = validate_request(market, from_date, to_date, symbols)
    if not _CAPACITY.acquire(blocking=False):
        return _report(request, [_check("execution", "blocked", "Data checks are busy. Wait for the active checks to finish and retry.")])
    reader = writer = process = previous = None
    try:
        deadline = time.monotonic() + timeout_seconds
        context = multiprocessing.get_context("spawn")
        reader, writer = context.Pipe(duplex=False)
        process = context.Process(target=_target, args=(writer, request), daemon=True)
        process.start()
        writer.close()
        while reader.poll(max(0, deadline - time.monotonic())):
            try:
                message = reader.recv()
            except EOFError:
                return _failed_check(request, previous, "Data inspection stopped before producing a result. Retry with a smaller range.")
            if isinstance(message, tuple) and len(message) == 2 and message[0] == "progress":
                previous = message[1]
                if time.monotonic() < deadline:
                    continue
                break
            return message
        return _failed_check(request, previous, f"Data inspection exceeded {timeout_seconds:g} seconds. Completed checks are retained; check source connectivity or reduce the symbols/date range.")
    except Exception:
        return _failed_check(request, previous, "Data inspection could not complete. Check the server runtime and retry.")
    finally:
        if process is not None and process.pid is not None:
            process.join(timeout=0.2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            process.close()
        if reader is not None:
            reader.close()
        if writer is not None:
            writer.close()
        _CAPACITY.release()
