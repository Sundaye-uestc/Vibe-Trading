"""HSGT (沪深港通) northbound real-time minute flow from Tonghuashun."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

_HSGT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "Chrome/117.0.0.0 Safari/537.36"
    ),
    "Host": "data.hexin.cn",
    "Referer": "https://data.hexin.cn/",
}

_CACHE_FILE = ".northbound_daily.csv"


def _northbound_cache_path() -> Path:
    """Cache path for northbound daily snapshots under the agent runs directory."""
    p = Path(__file__).resolve().parents[2] / "data" / _CACHE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _hsgt_realtime() -> pd.DataFrame:
    """Fetch real-time HSGT minute flow (262 data points, 09:10–15:00)."""
    url = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
    r = requests.get(url, headers=_HSGT_HEADERS, timeout=10)
    d = r.json()
    times = d.get("time", [])
    hgt = d.get("hgt", [])
    sgt = d.get("sgt", [])

    n = len(times)
    return pd.DataFrame({
        "time": times,
        "hgt_yi": hgt[:n] + [None] * (n - len(hgt)) if n > len(hgt) else hgt,
        "sgt_yi": sgt[:n] + [None] * (n - len(sgt)) if n > len(sgt) else sgt,
    })


def _load_northbound_history(n: int = 20) -> pd.DataFrame:
    """Read last N days of cached northbound data."""
    path = _northbound_cache_path()
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
        return df.tail(n)
    except Exception:
        return pd.DataFrame()


class HsgtRealtimeTool(BaseTool):
    """HSGT northbound real-time minute flow (沪股通/深股通)."""

    name = "hsgt_realtime"
    description = (
        "Fetch real-time Shanghai/Shenzhen-HK Stock Connect (沪深股通) northbound capital flow "
        "minute-by-minute data from Tonghuashun (同花顺, zero auth). "
        "Returns ~262 data points per day (09:10–15:00 including auction period) "
        "with cumulative net buy amounts in 亿元 for HGT (沪股通) and SGT (深股通). "
        "Also loads locally-cached daily history (up to 20 days). "
        "Use for: tracking foreign capital inflows/outflows in real-time, "
        "detecting northbound sentiment shifts, comparing HGT vs SGT flow patterns."
    )
    parameters = {
        "type": "object",
        "properties": {
            "include_history": {
                "type": "boolean",
                "description": "If true, also return cached daily history (up to 20 days). Default: false.",
                "default": False,
            },
        },
        "required": [],
    }

    def execute(self, **kwargs: Any) -> str:
        include_history = kwargs.get("include_history", False)

        try:
            df = _hsgt_realtime()
        except Exception as exc:
            logger.warning("hsgt_realtime failed: %s", exc)
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

        if df.empty:
            return json.dumps(
                {"ok": True, "market": "a", "source": "tonghuashun", "data": {"minute_points": 0, "flow": []}},
                ensure_ascii=False,
            )

        # Get the latest cumulative values (last non-null row)
        last_valid = df.dropna(subset=["hgt_yi", "sgt_yi"]).tail(1)
        latest = {}
        if not last_valid.empty:
            row = last_valid.iloc[0]
            latest = {
                "time": str(row["time"]),
                "hgt_yi": float(row["hgt_yi"]) if pd.notna(row["hgt_yi"]) else None,
                "sgt_yi": float(row["sgt_yi"]) if pd.notna(row["sgt_yi"]) else None,
            }

        # Return tail of minute data (last 30 points for readability)
        minute_data = df.tail(30).to_dict(orient="records")
        for r in minute_data:
            for k, v in r.items():
                if pd.isna(v):
                    r[k] = None
                elif isinstance(v, float):
                    r[k] = round(v, 2)

        result: dict[str, Any] = {
            "ok": True,
            "market": "a",
            "source": "tonghuashun",
            "data": {
                "total_minute_points": len(df),
                "latest_cumulative": latest,
                "recent_minutes": minute_data,
            },
        }

        if include_history:
            try:
                hist = _load_northbound_history(20)
                if not hist.empty:
                    result["data"]["daily_history"] = hist.to_dict(orient="records")
            except Exception:
                pass

        return json.dumps(result, ensure_ascii=False)
