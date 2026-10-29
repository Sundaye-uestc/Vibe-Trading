"""Opt-in read-only ClickHouse HTTP source for paper market-data snapshots.

No tables are created and no data is imported. Calendar completeness and raw
price checks remain the responsibility of the paper matching layer.
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlsplit, urlunsplit

import httpx
import pandas as pd

from src.config.accessor import get_env_config


class ClickHouseConnector:
    """Read configured tables with bound parameters and bounded requests."""

    def __init__(self, host=None, port=None, user=None, password=None, database=None):
        cfg = get_env_config().data
        host = str(host if host is not None else cfg.clickhouse_host).strip()
        if not host:
            raise ValueError("ClickHouse is not configured: set CLICKHOUSE_HOST and provide a complete trade_calendar")
        parsed = urlsplit(host if "://" in host else "http://" + host)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("CLICKHOUSE_HOST must be an HTTP(S) host without credentials, path or query")
        chosen_port = port if port is not None else cfg.clickhouse_port
        if chosen_port is None:
            chosen_port = parsed.port or (443 if parsed.scheme == "https" else 8123)
        if isinstance(chosen_port, bool) or not isinstance(chosen_port, int) or not 1 <= chosen_port <= 65535:
            raise ValueError("CLICKHOUSE_PORT must be between 1 and 65535")
        hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        self.url = urlunsplit((parsed.scheme, f"{hostname}:{chosen_port}", "/", "", ""))
        self.database = str(database if database is not None else cfg.clickhouse_database)
        self._auth = httpx.BasicAuth(user if user is not None else cfg.clickhouse_user,
                                    password if password is not None else cfg.clickhouse_password)
        self.timeout = cfg.clickhouse_timeout_seconds
        self._paper_managed: bool | None = None

    def query(self, sql: str, params: dict[str, str] | None = None) -> pd.DataFrame:
        """Run one SELECT with server-side bindings, never exposing error bodies."""
        if not isinstance(sql, str) or not re.match(r"\s*SELECT\b", sql, re.IGNORECASE) or ";" in sql:
            raise ValueError("ClickHouse connector accepts a single SELECT only")
        bindings = dict(params or {})
        if any(not re.fullmatch(r"param_[A-Za-z_][A-Za-z0-9_]*", name) for name in bindings):
            raise ValueError("Only named ClickHouse query parameters are accepted")
        settings = {"database": self.database, "readonly": "1",
                    "max_execution_time": str(max(1, int(self.timeout))), **bindings}
        statement = sql.rstrip() + " FORMAT JSON"
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False, follow_redirects=False) as client:
                response = client.post(self.url, params=settings, content=statement.encode("utf-8"),
                                       headers={"Content-Type": "text/plain; charset=utf-8"}, auth=self._auth)
            if response.status_code != 200:
                raise RuntimeError(f"ClickHouse query failed (HTTP {response.status_code})")
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("invalid ClickHouse result")
            columns = [column["name"] for column in payload.get("meta", [])]
            return pd.DataFrame(payload["data"], columns=columns or None)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"ClickHouse connection failed ({type(exc).__name__})") from None
        except (ValueError, KeyError, TypeError):
            raise RuntimeError("ClickHouse returned an invalid JSON result") from None

    def get_trade_calendar(self, start_date: str, end_date: str) -> pd.DataFrame:
        """Return explicit open/closed rows without inferring holidays."""
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if end < start:
            raise ValueError("Calendar end date precedes start date")
        return self.query(
            "SELECT exchange, cal_date, is_open, pretrade_date FROM trade_calendar "
            "WHERE exchange IN ('SSE', 'SZSE', 'BSE') "
            "AND replaceAll(toString(cal_date), '-', '') >= {start_date:String} "
            "AND replaceAll(toString(cal_date), '-', '') <= {end_date:String} ORDER BY cal_date",
            params={"param_start_date": start.strftime("%Y%m%d"), "param_end_date": end.strftime("%Y%m%d")},
        )

    def has_managed_paper_data(self) -> bool:
        """Recognise the importer contract without treating query errors as absence."""
        if self._paper_managed is None:
            frame = self.query(
                "SELECT name FROM system.tables WHERE database = currentDatabase() "
                "AND name IN ('paper_daily_status', 'paper_data_coverage')"
            )
            names = set(frame["name"].astype(str)) if "name" in frame else set()
            if names and names != {"paper_daily_status", "paper_data_coverage"}:
                raise RuntimeError("Paper data contract is incomplete")
            self._paper_managed = bool(names)
        return self._paper_managed

    def get_paper_daily_status(self, start_date: str, end_date: str, symbols: list[str]) -> pd.DataFrame | None:
        """Explicit observed ST/suspension states; None denotes a legacy source."""
        if not self.has_managed_paper_data():
            return None
        if not symbols:
            return pd.DataFrame()
        clauses = [f"ts_code = {{symbol{i}:String}}" for i in range(len(symbols))]
        params = {"param_start": start_date, "param_end": end_date,
                  **{f"param_symbol{i}": symbol for i, symbol in enumerate(symbols)}}
        return self.query(
            "SELECT ts_code, trade_date, is_st, is_suspended, source, observed_at "
            "FROM paper_daily_status WHERE trade_date >= {start:String} AND trade_date <= {end:String} "
            "AND (" + " OR ".join(clauses) + ") ORDER BY ts_code, trade_date LIMIT 100001",
            params=params,
        )

    def get_paper_data_coverage(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        """Read declared import coverage; unmarked external databases stay legacy."""
        if not self.has_managed_paper_data():
            return None
        return self.query(
            "SELECT dataset, ts_code, from_date, to_date, status, source, snapshot_id, observed_at "
            "FROM paper_data_coverage WHERE ts_code = {symbol:String} "
            "AND from_date <= {end:String} AND to_date >= {start:String} LIMIT 1001",
            params={"param_symbol": symbol, "param_start": start_date, "param_end": end_date},
        )
