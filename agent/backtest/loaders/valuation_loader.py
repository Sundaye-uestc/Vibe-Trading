"""Daily CN valuation panels with a bounded, no-auth BaoStock fallback.

Values describe the close of their own trade date. They are never forward- or
back-filled, and they are not intraday/filing-time fundamental observations.
BaoStock's MRQ P/B can use a different financial basis from Tushare P/B; every
returned frame records the actual source counts and this distinction.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np
import pandas as pd

from backtest.loaders._symbol_utils import _is_etf_listed
from backtest.loaders.base import validate_date_range
from backtest.loaders.tushare import TUSHARE_TOKEN_PLACEHOLDERS, _call_with_backoff, _is_index

logger = logging.getLogger(__name__)

VALUATION_FIELDS = ("pe", "pe_ttm", "pb", "ps_ttm", "dv_ttm")
BAOSTOCK_VALUATION_FIELDS = {"pe_ttm": "peTTM", "pb": "pbMRQ", "ps_ttm": "psTTM"}


def validate_valuation_fields(fields: Iterable[str]) -> list[str]:
    """Validate the valuation namespace before making any provider request."""
    fields = list(fields)
    unknown = [field for field in fields if field not in VALUATION_FIELDS]
    if unknown:
        raise ValueError(f"unsupported val: field(s) {unknown}; supported fields: {list(VALUATION_FIELDS)}")
    return list(dict.fromkeys(fields))


def _canonical_symbol(code: str) -> str:
    code = str(code).strip().upper()
    match = re.fullmatch(r"(SH|SZ)\.(\d{6})", code)
    return f"{match[2]}.{match[1]}" if match else code


def _is_cn_equity(code: str) -> bool:
    code = _canonical_symbol(code)
    return bool(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code)) and not (_is_etf_listed(code) or _is_index(code))


def _build_api() -> Any:
    from src.config.accessor import get_env_config

    token = get_env_config().data.tushare_token.strip()
    if token in TUSHARE_TOKEN_PLACEHOLDERS:
        raise RuntimeError("TUSHARE_TOKEN not configured")
    import tushare as ts

    return ts.pro_api(token)


def _resolve_tushare_api(api: Any | None) -> Any | None:
    if api is not None:
        return api
    try:
        return _build_api()
    except Exception as exc:  # noqa: BLE001 - optional credentials/package
        logger.info("Tushare valuation unavailable (%s); trying BaoStock", type(exc).__name__)
        return None


def _baostock_available() -> bool:
    try:
        import baostock  # noqa: F401
        return True
    except ImportError:
        return False


def _to_baostock_code(code: str) -> str | None:
    code = _canonical_symbol(code)
    if _is_cn_equity(code) and code.endswith((".SH", ".SZ")):
        symbol, suffix = code.split(".")
        return f"{suffix.lower()}.{symbol}"
    return None


def _aligned_series(frame: pd.DataFrame, date_column: str, field: str,
                    index: pd.DatetimeIndex, start: str, end: str) -> pd.Series:
    """Align exact dates only; conflicting duplicate observations stay missing."""
    empty = pd.Series(np.nan, index=index, dtype="float64")
    if not isinstance(frame, pd.DataFrame) or date_column not in frame or field not in frame:
        return empty
    dates = pd.to_datetime(frame[date_column].astype(str), format="mixed", errors="coerce")
    raw = frame[field]
    values = pd.to_numeric(raw.where(~raw.map(lambda value: isinstance(value, (bool, np.bool_)))), errors="coerce")
    values = values.where(np.isfinite(values))
    rows = pd.DataFrame({"date": dates, "value": values}).dropna(subset=["date"])
    rows = rows[rows["date"].between(pd.Timestamp(start), pd.Timestamp(end))]
    if rows.empty:
        return empty

    def unambiguous(group: pd.Series) -> float:
        observations = group.dropna().unique()
        return float(observations[0]) if len(observations) == 1 else np.nan

    source = rows.groupby("date")["value"].agg(unambiguous)
    # Keep local date labels but never map an end-of-day observation onto an
    # intraday timestamp; only exact midnight daily index labels match.
    target = index.tz_localize(None) if index.tz is not None else index
    return pd.Series(source.reindex(target).to_numpy(), index=index, dtype="float64")


def _load_baostock_valuation(cn_symbols: Sequence[str], fields: Sequence[str],
                            start: str, end: str, index: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
    from backtest.loaders.baostock_loader import _baostock_socket_guard, _read_timeout

    result = {field: pd.DataFrame(np.nan, index=index, columns=list(cn_symbols)) for field in fields}
    wanted = [field for field in fields if field in BAOSTOCK_VALUATION_FIELDS]
    if not wanted:
        return result
    import baostock as bs

    columns = ["date", *(BAOSTOCK_VALUATION_FIELDS[field] for field in wanted)]
    try:
        with _baostock_socket_guard(_read_timeout()) as acquired:
            if not acquired:
                return result
            login = bs.login()
            if login.error_code != "0":
                logger.warning("BaoStock valuation login failed (%s)", login.error_code)
                return result
            try:
                for code in cn_symbols:
                    symbol = _to_baostock_code(code)
                    if symbol is None:
                        continue
                    try:
                        response = bs.query_history_k_data_plus(
                            symbol, ",".join(columns), start_date=start, end_date=end, frequency="d",
                        )
                        if response.error_code != "0":
                            continue
                        rows = []
                        while response.next():
                            rows.append(response.get_row_data())
                        frame = pd.DataFrame(rows, columns=columns)
                        for field in wanted:
                            result[field][code] = _aligned_series(
                                frame, "date", BAOSTOCK_VALUATION_FIELDS[field], index, start, end,
                            )
                    except Exception as exc:  # noqa: BLE001 - isolate symbol/iterator failures
                        logger.warning("BaoStock valuation failed for %s (%s)", code, type(exc).__name__)
            finally:
                try:
                    bs.logout()
                except Exception as exc:  # noqa: BLE001 - guard still closes/restores the socket
                    logger.warning("BaoStock valuation logout failed (%s)", type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - optional source outages retain missing data
        logger.warning("BaoStock valuation unavailable (%s)", type(exc).__name__)
    return result


def load_valuation_panel(*, symbols: Sequence[str], fields: Iterable[str], start: str, end: str,
                         index: pd.DatetimeIndex, api: Any | None = None) -> dict[str, pd.DataFrame]:
    """Load exact-date valuations; fill only unsupported/missing primary cells.

    P/E, P/B and P/S are ratios; dividend yield is in percent units. BaoStock
    supplies pe_ttm, pb (MRQ basis) and ps_ttm only. Dynamic pe and dv_ttm, BJ
    symbols, non-CN securities and absent dates are never approximated.
    JSON-safe ``frame.attrs['valuation']`` records actual sources/coverage.
    """
    checked = validate_valuation_fields(fields)
    validate_date_range(start, end)
    if not checked:
        return {}
    symbols = list(dict.fromkeys(symbols))
    index = pd.DatetimeIndex(index)
    result = {field: pd.DataFrame(np.nan, index=index, columns=symbols) for field in checked}
    primary = {field: pd.DataFrame(False, index=index, columns=symbols) for field in checked}
    secondary = {field: pd.DataFrame(False, index=index, columns=symbols) for field in checked}
    cn_symbols = [code for code in symbols if _is_cn_equity(code)]
    api = _resolve_tushare_api(api) if cn_symbols else None
    warnings: list[str] = []
    if cn_symbols and api is None:
        warnings.append("Tushare daily_basic unavailable; only supported BaoStock fields can be filled.")
    if api is not None:
        for code in cn_symbols:
            try:
                frame = _call_with_backoff(api.daily_basic, ts_code=_canonical_symbol(code),
                                           start_date=start.replace("-", ""), end_date=end.replace("-", ""),
                                           fields="ts_code,trade_date," + ",".join(checked))
                for field in checked:
                    result[field][code] = _aligned_series(frame, "trade_date", field, index, start, end)
                    primary[field][code] = result[field][code].notna()
            except Exception as exc:  # noqa: BLE001 - permission/outage fallback per symbol
                logger.warning("Tushare valuation failed for %s (%s)", code, type(exc).__name__)
                warnings.append(f"Tushare daily_basic failed for {code}; missing cells may use BaoStock.")

    fallback_symbols = [code for code in cn_symbols if _to_baostock_code(code) is not None and any(
        field in BAOSTOCK_VALUATION_FIELDS and result[field][code].isna().any() for field in checked
    )]
    fallback_fields = [field for field in checked if field in BAOSTOCK_VALUATION_FIELDS and any(
        result[field][code].isna().any() for code in fallback_symbols
    )]
    if fallback_fields and _baostock_available():
        fallback = _load_baostock_valuation(fallback_symbols, fallback_fields, start, end, index)
        for field in fallback_fields:
            frame = fallback[field].reindex(index=index, columns=symbols)
            fill = result[field].isna() & frame.notna()
            result[field] = result[field].where(~fill, frame)
            secondary[field] = fill
    elif fallback_fields:
        warnings.append("BaoStock is not installed; missing valuations remain unavailable.")

    for field in checked:
        source_counts = {"tushare_daily_basic": int(primary[field].sum().sum()),
                         "baostock": int(secondary[field].sum().sum())}
        result[field].attrs["valuation"] = {
            "field": field, "unit": "percent" if field == "dv_ttm" else "ratio",
            "available_at": "trade_date_close", "filled_across_dates": False,
            "sources": [name for name, count in source_counts.items() if count],
            "source_counts": source_counts,
            "source_counts_by_symbol": {code: {"tushare_daily_basic": int(primary[field][code].sum()),
                                               "baostock": int(secondary[field][code].sum())} for code in symbols},
            "missing_cells": int(result[field].isna().sum().sum()),
            "provider_field": {"tushare_daily_basic": field, "baostock": BAOSTOCK_VALUATION_FIELDS.get(field)},
            "warnings": warnings.copy(),
            "methodology": "Exact trade-date observations only; BaoStock pbMRQ and Tushare pb may use different financial bases.",
        }
    return result
