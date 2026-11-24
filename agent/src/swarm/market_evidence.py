"""Bounded OHLC observations captured at the worker's tool execution boundary."""

from __future__ import annotations

import json
import math
import re
import threading
from datetime import datetime
from typing import Any, Mapping


MAX_SERIES = 16
MAX_BARS = 32
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_OHLC = ("open", "high", "low", "close", "volume")


def _observed_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def checked_bars(rows: Any) -> list[dict]:
    """Copy only finite observed fields from dated rows; never default a missing field to zero."""
    if not isinstance(rows, list):
        return []
    output = []
    for row in rows[-MAX_BARS:]:
        if not isinstance(row, dict) or not isinstance(row.get("trade_date"), str):
            continue
        stamp = row["trade_date"]
        try:
            datetime.fromisoformat(stamp)
        except ValueError:
            continue
        fields = {key: row[key] for key in _OHLC if _observed_number(row.get(key))}
        if fields.keys() - {"volume"}:
            output.append({"trade_date": stamp, **fields})
    return output


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def verified_source(provenance: Any, symbol: str) -> bool:
    """A worker's own local data file cannot supply independent market evidence."""
    return (isinstance(provenance, dict) and valid_id(provenance.get("source"))
            and provenance["source"].casefold() not in {"auto", "unknown", "local"}
            and not provenance.get("venue_fallback")
            and provenance.get("resolved_symbol", symbol) == symbol)


class SwarmMarketEvidence:
    """One launcher-owned collection; workers cannot edit it by writing files."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._series: list[dict] = []

    def capture(self, run_id: str, agent_id: str, task_id: str, call_id: str,
                args: Mapping[str, Any], raw_result: str) -> None:
        if not all(valid_id(v) for v in (run_id, agent_id, task_id, call_id)):
            return
        if not isinstance(args.get("codes"), list):
            return
        try:
            result = json.loads(raw_result)
        except (ValueError, TypeError):
            return
        if not isinstance(result, dict) or not isinstance(result.get("_provenance"), dict):
            return
        if (result.get("ok") is False or result.get("success") is False or result.get("skipped")
                or str(result.get("status", "")).casefold() in {"error", "failed", "cancelled", "failure"}):
            return
        for symbol in args["codes"][:MAX_SERIES]:
            if not isinstance(symbol, str):
                continue
            prov = result["_provenance"].get(symbol)
            if not verified_source(prov, symbol):
                continue
            panel = result.get(symbol)
            bars = checked_bars(panel.get("data") if isinstance(panel, dict) else panel)
            if not bars:
                continue
            item = {"run_id": run_id, "agent_id": agent_id, "task_id": task_id, "call_id": call_id,
                    "tool": "get_market_data", "symbol": symbol, "interval": str(args.get("interval", "1D")),
                    "provenance": {key: prov[key] for key in ("source", "currency_conversion", "quote_currency",
                                                             "adjustment", "volume_unit") if valid_id(prov.get(key))},
                    "bars": bars}
            with self._lock:
                if len(self._series) >= MAX_SERIES:
                    return
                self._series.append(item)

    def snapshot(self, run_id: str) -> list[dict]:
        with self._lock:
            return [series for series in self._series if series["run_id"] == run_id]
