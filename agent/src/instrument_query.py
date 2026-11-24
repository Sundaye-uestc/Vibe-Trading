"""Exact provider-query spellings and candidate comparisons for instrument search."""

import re
from typing import Any, Mapping

from src.market_data import canonical_fx_pair
from src.us_indices import exact_us_index_symbol


def qualified_symbol(query: str) -> str | None:
    """Return the canonical symbol when the whole query specifies a listing."""
    symbol = query.strip().upper()
    prefixed = re.fullmatch(r"(SH|SS|SZ|BJ|HK)\.?([0-9]{3,6})|US\.([A-Z][A-Z0-9.&-]*)", symbol)
    if prefixed:
        venue, code, us_code = prefixed.groups()
        symbol = f"{us_code}.US" if us_code else f"{code}.{venue}"
    if re.fullmatch(r"\d{6}\.SS", symbol):
        return symbol[:-3] + ".SH"
    if re.fullmatch(r"\d{1,5}\.HK", symbol):
        return symbol[:-3].zfill(5) + ".HK"
    if re.fullmatch(r"\^[A-Z0-9.&-]+|[A-Z0-9]+=[FX]", symbol):
        return symbol
    if re.fullmatch(r"[A-Z0-9][A-Z0-9.&-]*\.(?:SH|SS|SZ|BJ|HK|US|NS|BO|KS|KQ|VN|TO|V|L|DE|PA|T|AX)", symbol):
        return symbol
    return None


def exact_candidate_match(query: str, candidate: Mapping[str, Any]) -> bool:
    """Distinguish a listing's bare ticker from a different instrument's base.

    A quote leg, class-share designation or futures suffix is part of the
    ticker. Stripping those made e.g. FTSE-USD an 'exact' rival for ^FTSE.
    """
    symbol = str(candidate.get("symbol") or "").upper()
    canonical = qualified_symbol(symbol) or symbol
    explicit = qualified_symbol(query) or canonical_fx_pair(query) or exact_us_index_symbol(query)
    if explicit:
        return explicit == canonical
    if candidate.get("market") == "crypto" or candidate.get("type") == "cryptocurrency":
        pair = re.fullmatch(r"([A-Z0-9]+)[-/]([A-Z0-9]+)", canonical)
        if pair:
            base, quote = pair.groups()
            return query.strip().upper() in {f"{base}/{quote}", f"{base}-{quote}", f"{base}{quote}"}
    term = query.strip().casefold()
    if canonical.startswith("^"):
        root = canonical[1:]
    elif qualified_symbol(canonical) and "." in canonical:
        root = canonical.rsplit(".", 1)[0]
    else:
        root = canonical
    return term in {canonical.casefold(), root.casefold(), str(candidate.get("name") or "").strip().casefold()}


def matches_instrument_kind(query: str, candidate: Mapping[str, Any]) -> bool:
    """Explicit product requests must not resolve to lookalike tracker funds."""
    # A fund's name can contain the word 'index'; only an explicit end label
    # identifies the requested product, and an explicit fund name stays free.
    term = query.strip().casefold()
    kind = str(candidate.get("type") or "").casefold()
    symbol = str(candidate.get("symbol") or "").upper()
    if re.search(r"(?:指数|\bindex)$", term) and not re.search(r"基金|\b(?:fund|etf)\b", term):
        return kind in {"index", "指数"} or symbol.startswith("^")
    if re.search(r"(?:期货|\bfutures?)$", term):
        return kind in {"future", "futures", "期货"} or symbol.endswith("=F")
    if re.search(r"etf$", term):
        return "etf" in kind or "etf" in str(candidate.get("name") or "").casefold()
    return True
