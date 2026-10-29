"""Validation of explicit per-day observations supplied by managed imports.

Missing event rows alone cannot prove a normal trading day. The importer also
publishes each observed zero/one status, which this read-only seam validates.
Legacy sources without the contract remain explicitly unverified.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .calendar import _normalize_date


class PaperDataUnavailable(ValueError):
    """Managed source has no trustworthy coverage for the requested step."""


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Provenance must be a non-empty string")
    return value.strip()


def _observed(value: Any) -> datetime:
    stamp = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("Observation timestamp must include a timezone")
    return stamp


def verify_event_rows(observations: dict, records: list[dict], kind: str) -> None:
    """Match event views against explicit normal/suspended/ST observations."""
    found = set()
    for row in records:
        key = str(row["ts_code"]), _normalize_date(row["trade_date"])
        if key not in observations:
            raise PaperDataUnavailable("Event row has no managed daily observation")
        if kind == "suspensions":
            flag = str(row.get("suspend_type", "")).strip().upper()
            if flag not in {"S", "R"}:
                raise PaperDataUnavailable("Invalid managed suspension event")
            active = flag == "S"
        else:
            active = True
        if active:
            found.add(key)
    field = "is_st" if kind == "st_status" else "is_suspended"
    if any(item[field] != (key in found) for key, item in observations.items()):
        raise PaperDataUnavailable("A-share event view contradicts the managed daily observation")


def daily_status(connector: Any, symbols, start: str, end: str, trading_days) -> dict | None:
    reader = getattr(connector, "get_paper_daily_status", None)
    if not callable(reader):
        return None
    try:
        frame = reader(start, end, list(symbols))
        if frame is None:
            return None
        if len(frame) > 100000:
            raise ValueError("Too many rows")
        observations = {}
        for record in frame.to_dict("records"):
            symbol, day = str(record["ts_code"]), _normalize_date(record["trade_date"])
            if symbol not in symbols or not start <= day <= end:
                raise ValueError("Status outside requested range")
            bits = tuple(str(record[name]).strip() for name in ("is_st", "is_suspended"))
            if any(value not in {"0", "1", "False", "True"} for value in bits):
                raise ValueError("Invalid observed status")
            source = _text(record["source"])
            stamp = _observed(record["observed_at"])
            if stamp.date().isoformat() < day:
                raise ValueError("Invalid source or observation time")
            if (symbol, day) in observations:
                raise ValueError("Duplicate daily status")
            observations[symbol, day] = {
                "is_st": bits[0] in {"1", "True"}, "is_suspended": bits[1] in {"1", "True"},
                "source": source, "observed_at": stamp.isoformat(),
            }
        if trading_days is not None and any((symbol, day) not in observations for symbol in symbols for day in trading_days):
            raise ValueError("Missing daily observations")
        return observations
    except Exception as exc:
        raise PaperDataUnavailable("Managed daily ST/suspension observations are missing, invalid or unreadable; import the requested dates before settlement") from exc


def raw_coverage(connector: Any, symbol: str, start: str, end: str) -> bool:
    """Verify the union of complete snapshots, never a min/max that hides gaps.

    Incremental imports describe adjacent ranges rather than repeatedly
    publishing the entire history. Every requested calendar date must belong
    to a complete range. Actual calendar, status and bar rows are checked by
    ``ClickHouseBarsClient.covers_raw_range`` before this can skip a network leg.
    """
    reader = getattr(connector, "get_paper_data_coverage", None)
    if not callable(reader):
        return False
    try:
        frame = reader(symbol, start, end)
        if frame is None:
            return False
        if len(frame) > 1000:
            raise ValueError("Too many coverage rows")
        start, end = _normalize_date(start), _normalize_date(end)
        if start > end:
            raise ValueError("Coverage start follows end")
        matched: list[tuple[int, int]] = []
        for row in frame.to_dict("records"):
            if row.get("dataset") != "raw_prices":
                continue
            if str(row["ts_code"]) != symbol:
                raise ValueError("Coverage symbol mismatch")
            first, last = _normalize_date(row["from_date"]), _normalize_date(row["to_date"])
            if first > last:
                raise ValueError("Reversed coverage interval")
            if last < start or first > end:
                continue
            observed = _observed(row["observed_at"])
            _text(row["source"])
            _text(row["snapshot_id"])
            if observed.date().isoformat() < last:
                raise ValueError("Invalid coverage provenance")
            if row["status"] != "complete":
                return False
            matched.append((date.fromisoformat(max(first, start)).toordinal(),
                            date.fromisoformat(min(last, end)).toordinal()))
        next_day = date.fromisoformat(start).toordinal()
        last_day = date.fromisoformat(end).toordinal()
        for first, last in sorted(matched):
            if first > next_day:
                return False
            next_day = max(next_day, last + 1)
        return next_day > last_day
    except Exception as exc:
        raise PaperDataUnavailable("Managed raw-price coverage is invalid or unreadable") from exc
