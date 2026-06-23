"""Tonghuashun (同花顺) hot-stock reason tags — daily strong stocks with sector attribution."""

from __future__ import annotations

import json
import logging
from datetime import date as _date
from typing import Any

import pandas as pd
import requests

from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "Chrome/117.0.0.0 Safari/537.36"
)


def _ths_hot_reason(date_str: str | None = None) -> pd.DataFrame:
    """Fetch today's strong stocks with manually curated sector-attribution tags."""
    if date_str is None:
        date_str = _date.today().strftime("%Y-%m-%d")

    url = (
        f"http://zx.10jqka.com.cn/event/api/getharden/"
        f"date/{date_str}/orderby/date/orderway/desc/charset/GBK/"
    )
    r = requests.get(url, headers={"User-Agent": _UA}, timeout=10)
    data = r.json()
    if data.get("errocode", 0) != 0:
        raise RuntimeError(f"同花顺热点错误: {data.get('errormsg', '')}")

    rows = data.get("data") or []
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    rename_map = {
        "name": "名称",
        "code": "代码",
        "reason": "题材归因",
        "close": "收盘价",
        "zhangdie": "涨跌额",
        "zhangfu": "涨幅%",
        "huanshou": "换手率%",
        "chengjiaoe": "成交额",
        "chengjiaoliang": "成交量",
        "ddejingliang": "大单净量",
        "market": "市场",
    }
    df = df.rename(columns=rename_map)
    return df


class ThsHotReasonTool(BaseTool):
    """Tonghuashun daily strong stocks with sector-attribution reason tags."""

    name = "ths_hot_reason"
    description = (
        "Fetch today's strongest A-share stocks with manual sector-attribution tags (题材归因) "
        "from Tonghuashun (同花顺, zero auth, ~73ms, ~125 stocks/day). "
        "This is the go-to tool for understanding WHAT is moving and WHY — each stock comes with "
        "editor-curated reason tags like '算力租赁+Token工厂+AI政务'. "
        "Returns: code, name, close price, change%, turnover%, volume, DDE net flow, market, "
        "and the critical 'reason' field with attribution tags. "
        "Use for: daily market pulse, theme/sector heat detection, discovering emerging narratives, "
        "pre-filtering candidates before deep-dive research."
    )
    parameters = {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "Date in 'YYYY-MM-DD' format. Defaults to today if omitted.",
            },
        },
        "required": [],
    }

    def execute(self, **kwargs: Any) -> str:
        date_str = kwargs.get("date")

        try:
            df = _ths_hot_reason(date_str)
        except Exception as exc:
            logger.warning("ths_hot_reason failed: %s", exc)
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

        if df.empty:
            return json.dumps(
                {"ok": True, "market": "a", "source": "tonghuashun", "data": {"count": 0, "stocks": []}},
                ensure_ascii=False,
            )

        # Extract key columns for the response
        cols = ["代码", "名称", "涨幅%", "题材归因", "换手率%", "成交额", "大单净量", "市场"]
        available_cols = [c for c in cols if c in df.columns]
        stocks = df[available_cols].head(200).to_dict(orient="records")

        return json.dumps(
            {
                "ok": True,
                "market": "a",
                "source": "tonghuashun",
                "data": {"count": len(stocks), "stocks": stocks},
            },
            ensure_ascii=False,
        )
