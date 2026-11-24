"""Canonical market-data symbols and exact lookup aliases for US benchmarks."""

from __future__ import annotations

import re
import unicodedata


US_INDEX_NAMES = {
    "^IXIC": ("纳斯达克综合指数", "Nasdaq Composite"),
    "^NDX": ("纳斯达克100指数", "Nasdaq 100"),
    "^GSPC": ("标普500指数", "S&P 500"),
    "^DJI": ("道琼斯工业平均指数", "Dow Jones Industrial Average"),
}

_INDEX_ALIASES = {
    "^IXIC": ("纳指", "纳综指", "纳斯达克综指", "纳斯达克综合", "Nasdaq Composite Index"),
    "^NDX": ("纳指100", "纳指100指数", "纳斯达克100", "Nasdaq-100", "Nasdaq 100 Index", "Nasdaq-100 Index"),
    "^GSPC": ("标普500", "标准普尔500指数", "S&P 500 Index", "S&P500", "SP500"),
    "^DJI": ("道指", "道琼斯指数", "道琼斯工业指数", "DJIA", "Dow Jones Industrial Average Index"),
}


def _alias_key(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


_SYMBOL_BY_ALIAS = {
    _alias_key(alias): symbol
    for symbol, names in US_INDEX_NAMES.items()
    for alias in (symbol, symbol[1:], *names, *_INDEX_ALIASES[symbol])
}


def exact_us_index_symbol(query: str) -> str | None:
    """Resolve a whole name or ticker, including a matching ticker/name pair."""
    key = _alias_key(query)
    if key in _SYMBOL_BY_ALIAS:
        return _SYMBOL_BY_ALIAS[key]
    # Search prompts often include both a symbol and a name. Consume the
    # entire query so e.g. a tracking fund or CAD variant retains its identity.
    match = re.fullmatch(r"(\^?[a-z]+) (.+)", key)
    if match:
        ticker, name = match.groups()
        symbol = "^" + ticker.removeprefix("^").upper()
        if _SYMBOL_BY_ALIAS.get(name) == symbol:
            return symbol
    match = re.fullmatch(r"(.+?)\s*\((\^?[a-z]+)\)", key)
    if match:
        name, ticker = match.groups()
        symbol = "^" + ticker.removeprefix("^").upper()
        if _SYMBOL_BY_ALIAS.get(name) == symbol:
            return symbol
    return None
