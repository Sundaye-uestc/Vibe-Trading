"""Sina Finance (新浪财经) — A-share financial statements (balance/income/cash flow)."""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"

_REPORT_TYPES = {
    "lrb": "利润表",
    "fzb": "资产负债表",
    "llb": "现金流量表",
}


class SinaFinancialReportTool(BaseTool):
    """Fetch A-share financial statements from Sina Finance (新浪财经, HTTP, zero auth)."""

    name = "sina_financial_report"
    description = (
        "Fetch A-share financial statements from Sina Finance (新浪财经, HTTP, zero auth). "
        "Supports three report types: 'lrb' (利润表/income statement), "
        "'fzb' (资产负债表/balance sheet), 'llb' (现金流量表/cash flow statement). "
        "Returns up to 8 recent periods with item values and YoY changes where available. "
        "Use for: fundamental analysis, financial health checks, DuPont decomposition, "
        "revenue/earnings quality assessment, cross-period financial comparison."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "6-digit A-share code (e.g. '600519', '000001').",
            },
            "report_type": {
                "type": "string",
                "enum": ["lrb", "fzb", "llb"],
                "description": "Report type: 'lrb'=利润表(income), 'fzb'=资产负债表(balance), 'llb'=现金流量表(cash flow). Default: 'lrb'.",
                "default": "lrb",
            },
            "num": {
                "type": "integer",
                "description": "Number of recent periods (max 8). Default: 8.",
                "default": 8,
            },
        },
        "required": ["code"],
    }

    def execute(self, **kwargs: Any) -> str:
        code = str(kwargs.get("code", "")).strip()
        if not code or len(code) != 6 or not code.isdigit():
            return json.dumps({"ok": False, "error": "'code' must be a 6-digit A-share code"}, ensure_ascii=False)

        report_type = str(kwargs.get("report_type", "lrb")).strip()
        if report_type not in ("lrb", "fzb", "llb"):
            return json.dumps({"ok": False, "error": f"report_type must be one of: lrb, fzb, llb"}, ensure_ascii=False)

        num = min(int(kwargs.get("num", 8)), 8)

        prefix = "sh" if code.startswith("6") else "sz"
        paper_code = f"{prefix}{code}"

        try:
            url = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
            params = {
                "paperCode": paper_code,
                "source": report_type,
                "type": "0",
                "page": "1",
                "num": str(num),
            }
            r = requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=15)
            data = r.json()

            report_list = data.get("result", {}).get("data", {}).get("report_list", {}) or {}

            rows = []
            for period in sorted(report_list.keys(), reverse=True)[:num]:
                obj = report_list[period]
                rec = {"报告期": f"{period[:4]}-{period[4:6]}-{period[6:8]}"}
                for it in obj.get("data", []) or []:
                    title = it.get("item_title", "")
                    if not title or it.get("item_value") is None:
                        continue
                    rec[title] = it.get("item_value")
                    tongbi = it.get("item_tongbi")
                    if tongbi not in (None, ""):
                        rec[title + "_同比"] = tongbi
                rows.append(rec)
        except Exception as exc:
            logger.warning("sina_financial_report failed for %s: %s", code, exc)
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

        return json.dumps(
            {
                "ok": True,
                "market": "a",
                "source": "sina",
                "data": {
                    "code": code,
                    "report_type": report_type,
                    "report_type_name": _REPORT_TYPES.get(report_type, report_type),
                    "periods": len(rows),
                    "reports": rows,
                },
            },
            ensure_ascii=False,
        )
