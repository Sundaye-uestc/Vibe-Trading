"""Tencent Finance real-time A-share quotes — never IP-banned, zero auth."""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

_Q_URL = "https://qt.gtimg.cn/q="
_MAX_CODES = 50


def _tencent_quote(codes: list[str]) -> dict[str, dict]:
    """Batch-fetch Tencent Finance real-time quotes. No IP-ban risk."""
    prefixed = []
    for c in codes:
        c = c.strip()
        if c.startswith(("6", "9")):
            prefixed.append(f"sh{c}")
        elif c.startswith("8"):
            prefixed.append(f"bj{c}")
        else:
            prefixed.append(f"sz{c}")

    url = _Q_URL + ",".join(prefixed)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    resp = urllib.request.urlopen(req, timeout=10)
    data = resp.read().decode("gbk")

    result = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]
        result[code] = {
            "name": vals[1],
            "price": float(vals[3]) if vals[3] else 0,
            "last_close": float(vals[4]) if vals[4] else 0,
            "open": float(vals[5]) if vals[5] else 0,
            "change_amt": float(vals[31]) if vals[31] else 0,
            "change_pct": float(vals[32]) if vals[32] else 0,
            "high": float(vals[33]) if vals[33] else 0,
            "low": float(vals[34]) if vals[34] else 0,
            "amount_wan": float(vals[37]) if vals[37] else 0,
            "turnover_pct": float(vals[38]) if vals[38] else 0,
            "pe_ttm": float(vals[39]) if vals[39] else 0,
            "amplitude_pct": float(vals[43]) if vals[43] else 0,
            "mcap_yi": float(vals[44]) if vals[44] else 0,
            "float_mcap_yi": float(vals[45]) if vals[45] else 0,
            "pb": float(vals[46]) if vals[46] else 0,
            "limit_up": float(vals[47]) if vals[47] else 0,
            "limit_down": float(vals[48]) if vals[48] else 0,
            "vol_ratio": float(vals[49]) if vals[49] else 0,
            "pe_static": float(vals[52]) if vals[52] else 0,
        }
    return result


class TencentQuoteTool(BaseTool):
    """Fetch real-time A-share quotes from Tencent Finance (HTTP, never IP-banned)."""

    name = "tencent_quote"
    cache_ttl = 300.0  # read-only fetch; identical args are stable within a run
    description = (
        "Get real-time A-share quotes from Tencent Finance (HTTP, zero auth, NO IP-ban risk). "
        "Returns price, PE(TTM), PB, market cap, float market cap, turnover rate, amplitude, "
        "limit up/down prices, volume ratio, and PE(static) for each stock. "
        "Also supports major indices (000001=SSE Composite, 000300=CSI 300, 399006=ChiNext) "
        "and ETFs (510050=SSE 50 ETF, 510300=CSI 300 ETF). "
        "PREFER this over Eastmoney for price/valuation data — Tencent never bans IPs. "
        "Use for: real-time quote snapshots, PE/PB valuation checks, market-cap lookups, "
        "batch stock comparison, index/ETF tracking."
    )
    parameters = {
        "type": "object",
        "properties": {
            "codes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of 6-digit A-share codes (e.g. ['688017', '600519', '000001']). "
                    "Also accepts index codes (000001=SSE, 000300=CSI300, 399006=ChiNext) "
                    f"and ETF codes (510050, 510300). Max {_MAX_CODES} per call."
                ),
            },
        },
        "required": ["codes"],
    }

    def execute(self, **kwargs: Any) -> str:
        codes = kwargs.get("codes", [])
        if not codes or not isinstance(codes, list):
            return json.dumps({"ok": False, "error": "'codes' must be a non-empty list of strings"}, ensure_ascii=False)

        codes = [str(c).strip() for c in codes[: _MAX_CODES]]
        if not codes:
            return json.dumps({"ok": False, "error": "no valid codes provided"}, ensure_ascii=False)

        try:
            quotes = _tencent_quote(codes)
        except Exception as exc:
            logger.warning("tencent_quote failed: %s", exc)
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

        return json.dumps(
            {
                "ok": True,
                "market": "a",
                "source": "tencent",
                "data": {"count": len(quotes), "quotes": quotes},
            },
            ensure_ascii=False,
        )
