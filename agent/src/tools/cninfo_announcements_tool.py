"""Cninfo (巨潮资讯网) exchange filings — SSE/SZSE/BSE announcements."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import requests

from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"

# The announcement query 403s over HTTPS while the identical request over HTTP
# returns 200 + JSON, so the scheme is treated as a transport detail to fall
# back on rather than a fixed choice. HTTPS is tried first so the plaintext hop
# disappears the moment the site stops refusing it.
_QUERY_PATH = "/new/hisAnnouncement/query"
_QUERY_BASES = ("https://www.cninfo.com.cn", "http://www.cninfo.com.cn")
_ORGID_MAP_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"

# Module-level orgId cache (loaded once, reused)
_CNINFO_ORGID_MAP: dict[str, str] = {}


def _cninfo_orgid(code: str) -> str:
    """Resolve real orgId from official mapping table; fall back to hard-coded rule."""
    global _CNINFO_ORGID_MAP
    if not _CNINFO_ORGID_MAP:
        try:
            r = requests.get(
                _ORGID_MAP_URL,
                headers={"User-Agent": _UA},
                timeout=15,
            )
            _CNINFO_ORGID_MAP = {s["code"]: s["orgId"] for s in r.json().get("stockList", [])}
        except Exception as e:
            logger.warning("cninfo orgId mapping failed, using fallback: %s", e)

    org = _CNINFO_ORGID_MAP.get(code)
    if org:
        return org
    # Fallback for stocks not in the dynamic mapping
    if code.startswith("6"):
        return f"gssh0{code}"
    elif code.startswith(("8", "4")):
        return f"gsbj0{code}"
    return f"gssz0{code}"


def _cninfo_ts_to_date(ts: Any) -> str:
    """Convert cninfo announcementTime (Unix ms) to date string."""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
    return str(ts)[:10] if ts else ""


def _cninfo_query(payload: dict[str, str]) -> dict[str, Any]:
    """POST the announcement query, falling back to HTTP when HTTPS is refused.

    Args:
        payload: Form fields for ``hisAnnouncement/query``.

    Returns:
        The decoded response object.

    Raises:
        ValueError: Every candidate scheme failed. The message names each scheme
            that was tried and the last transport error, so a caller (or the
            model reading the tool result) can tell a block from a schema change.
    """
    attempts: list[str] = []
    for base in _QUERY_BASES:
        try:
            response = requests.post(
                f"{base}{_QUERY_PATH}",
                data=payload,
                headers={
                    "User-Agent": _UA,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": f"{base}/new/disclosure",
                    "Origin": base,
                },
                timeout=15,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            attempts.append(f"{base} -> {type(exc).__name__}: {exc}")
            logger.warning("cninfo announcement query over %s failed: %s", base, exc)
            continue
        if isinstance(body, dict):
            return body
        attempts.append(f"{base} -> expected a JSON object, got {type(body).__name__}")

    raise ValueError(
        "cninfo announcement query failed on every scheme: " + "; ".join(attempts)
    )


class CninfoAnnouncementsTool(BaseTool):
    """Search SSE/SZSE/BSE exchange filings via Cninfo (巨潮资讯网)."""

    name = "cninfo_announcements"
    cache_ttl = 300.0  # read-only fetch; identical args are stable within a run
    description = (
        "Search official exchange filings (SSE, SZSE, BSE) for an A-share stock via "
        "Cninfo (巨潮资讯网, zero auth). Returns title, type, date, and URL for each filing. "
        "Covers annual/interim/quarterly reports, material announcements, IPO prospectuses, "
        "restructuring filings, and more. Uses dynamic orgId resolution (6,198 stocks mapped) "
        "so even 601xxx/688xxx stocks return correct results. "
        "Use for: digging into official company filings, verifying claims from news/research, "
        "tracking corporate events (M&A, restructuring, dividends), regulatory compliance checks."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "6-digit A-share stock code (e.g. '688017', '600519', '000001').",
            },
            "page_size": {
                "type": "integer",
                "description": "Number of announcements to return (max 50). Default: 30.",
                "default": 30,
            },
        },
        "required": ["code"],
    }

    def execute(self, **kwargs: Any) -> str:
        code = str(kwargs.get("code", "")).strip()
        if not code or len(code) != 6 or not code.isdigit():
            return json.dumps({"ok": False, "error": "'code' must be a 6-digit A-share code"}, ensure_ascii=False)

        page_size = min(int(kwargs.get("page_size", 30)), 50)

        try:
            org_id = _cninfo_orgid(code)
            payload = {
                "stock": f"{code},{org_id}",
                "tabName": "fulltext",
                "pageSize": str(page_size),
                "pageNum": "1",
                "column": "",
                "category": "",
                "plate": "",
                "seDate": "",
                "searchkey": "",
                "secid": "",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            }
            d = _cninfo_query(payload)

            rows = []
            for item in d.get("announcements", []) or []:
                rows.append({
                    "title": item.get("announcementTitle", ""),
                    "type": item.get("announcementTypeName", ""),
                    "date": _cninfo_ts_to_date(item.get("announcementTime")),
                    "url": f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={item.get('announcementId', '')}",
                })
        except Exception as exc:
            logger.warning("cninfo_announcements failed for %s: %s", code, exc)
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

        return json.dumps(
            {
                "ok": True,
                "market": "a",
                "source": "cninfo",
                "data": {"code": code, "count": len(rows), "announcements": rows},
            },
            ensure_ascii=False,
        )
