"""Shared Eastmoney HTTP client: secid resolution + push2his kline fetch.

Eastmoney exposes free, no-auth quote endpoints but rate-limits aggressively by
source IP and will temporarily ban a bursting client, so every request here
routes through :mod:`backtest.loaders._http` for per-host throttling and session
reuse. This module is provider-internal plumbing shared by the Eastmoney-backed
loaders; it only knows Eastmoney's ``secid`` addressing scheme and the
``push2his`` kline JSON layout, not any loader's DataFrame conventions.

Eastmoney addresses every instrument by a ``secid`` of the form ``<market>.<code>``:

* A-shares — Shanghai (``.SH``, plus ``.BJ`` Beijing exchange) use market ``1``
  for SH and ``0`` for SZ/BJ.
* Hong Kong (``.HK``) uses market ``116`` with the numeric code zero-padded to
  five digits.
* US (``.US``) markets (NASDAQ ``105`` / NYSE ``106`` / AMEX ``107``) are not
  derivable from the ticker alone, so the market prefix is discovered once via
  Eastmoney's search/suggest endpoint — with Yahoo's exchange code as a
  fallback when that endpoint answers without a candidate — and cached.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from typing import Any

from backtest.loaders._http import resolve_min_interval, throttled_get, throttled_get_json

logger = logging.getLogger(__name__)

# Eastmoney kline endpoints. push2his serves historical bars; searchapi resolves
# a free-text ticker to its fully-qualified secid (needed for US tickers).
_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_SEARCH_URL = "https://searchapi.eastmoney.com/api/suggest/get"

# Throttle/session bucket shared by all Eastmoney calls.
_HOST_KEY = "eastmoney"
_MIN_INTERVAL_ENV = "VIBE_TRADING_EASTMONEY_MIN_INTERVAL"
_DEFAULT_MIN_INTERVAL = 1.0

# Eastmoney kline period codes (``klt``) keyed by our interval labels.
KLT_BY_INTERVAL: dict[str, int] = {
    "1D": 101,
    "1d": 101,
    "1W": 102,
    "1w": 102,
    "1M": 103,
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1H": 60,
    "60m": 60,
}

# push2his column selectors: fields1 picks the per-request meta, fields2 picks
# the per-bar columns in order: date, open, close, high, low, volume, amount.
_FIELDS1 = "f1,f2,f3,f4,f5,f6"
_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57"

# Process-level cache of resolved US secids so a repeated ticker never re-hits
# the search endpoints. Keyed by the upper-cased bare ticker (e.g. "AAPL").
#
# Values are ``(secid, expires_at)``. A resolved secid is kept for the life of
# the process (``math.inf``), while a *miss* expires after
# ``_US_SECID_MISS_TTL_S``: Eastmoney's suggest surface spent a period
# answering every query with the same candidate-less body, and caching that
# miss forever would have pinned US tickers to "unresolvable" long after the
# endpoint recovered.
_US_SECID_CACHE: dict[str, tuple[str | None, float]] = {}
_US_SECID_MISS_TTL_S = 900.0

# Which of the three US markets a ticker trades on is not derivable from the
# ticker, and Eastmoney's suggest endpoint — the surface that used to answer it
# — now returns a fixed candidate-less body for every query. Two independent
# surfaces still place a US ticker, and both were verified reachable from the
# environments where suggest is dead:
#
#   * Nasdaq's quote API reports an explicit ``exchange`` field.
#   * Tencent's quote line carries the venue as a symbol suffix.
#
# Nasdaq is preferred because the venue is a named field rather than a suffix we
# have to interpret; Tencent is the fallback this project already depends on and
# has never been observed to IP-ban.
_NASDAQ_INFO_URL = "https://api.nasdaq.com/api/quote/{ticker}/info"
_NASDAQ_HOST_KEY = "nasdaq"
_NASDAQ_MIN_INTERVAL_ENV = "VIBE_TRADING_NASDAQ_MIN_INTERVAL"
_DEFAULT_NASDAQ_MIN_INTERVAL = 1.0

_TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q=us{ticker}"
_TENCENT_HOST_KEY = "tencent"
_TENCENT_MIN_INTERVAL_ENV = "VIBE_TRADING_TENCENT_MIN_INTERVAL"
_DEFAULT_TENCENT_MIN_INTERVAL = 1.0

# Nasdaq ``exchange`` label -> Eastmoney US market prefix. Matched exactly first,
# then by prefix (Nasdaq appends a tier: ``NASDAQ-GS``, ``NASDAQ-CM``, ...).
_US_MARKET_BY_NASDAQ_EXCHANGE: dict[str, str] = {
    "NASDAQ": "105",
    "NYSE": "106",
    "AMEX": "107",
    "NYSE AMERICAN": "107",
}

# Tencent's US symbol suffix -> Eastmoney US market prefix.
_US_MARKET_BY_TENCENT_SUFFIX: dict[str, str] = {
    ".OQ": "105",  # NASDAQ
    ".N": "106",  # NYSE
    ".A": "107",  # NYSE American (formerly AMEX)
}

# A JSONP envelope is a JS callback identifier followed by a parenthesized body,
# optionally terminated by ';'. The identifier is restricted to legal JS names
# (incl. dotted namespaces like "jQuery.cb") so a plain JSON array/object body —
# which never starts this way — is left untouched.
_JSONP_WRAPPER = re.compile(
    r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\s*\((.*)\)\s*;?\s*$",
    re.DOTALL,
)


def _min_interval() -> float:
    """Resolve the per-call minimum Eastmoney request spacing in seconds."""
    return resolve_min_interval(_MIN_INTERVAL_ENV, _DEFAULT_MIN_INTERVAL)


def get_json(url: str, *, params: dict[str, Any]) -> Any:
    """Issue a throttled Eastmoney GET and decode the body as JSON.

    Most endpoints answer with bare JSON. The search surfaces -- the suggest
    endpoint behind US secid resolution and the article search behind stock
    news -- wrap theirs in a ``jQuery…({ ... })`` JSONP envelope, which
    ``response.json()`` rejects before any caller can see the body.

    When that happens the body is re-read as text and returned *raw*, because
    unwrapping belongs to the callers that know the payload shape: they run it
    through :func:`_strip_jsonp` (or the news tool's ``_decode_jsonp``), both of
    which accept a plain-JSON string too. Anything that is neither JSON nor a
    callback envelope -- an HTML block page, say -- still raises, now with the
    status and size attached instead of a bare "Expecting value" message.

    Args:
        url: Fully-qualified Eastmoney endpoint URL.
        params: Query parameters for the request.

    Returns:
        The decoded JSON payload, or the raw body when it is JSONP-wrapped.

    Raises:
        requests.RequestException: Network failure, propagated for the caller's
            retry policy.
        requests.HTTPError: Non-2xx response status.
        ValueError: Body is neither valid JSON nor a JSONP envelope.
    """
    try:
        return throttled_get_json(
            url,
            host_key=_HOST_KEY,
            min_interval=_min_interval(),
            params=params,
        )
    except ValueError as json_error:
        response = throttled_get(
            url,
            host_key=_HOST_KEY,
            min_interval=_min_interval(),
            params=params,
        )
        response.raise_for_status()
        body = response.text
        if _JSONP_WRAPPER.match(body.strip()) is None:
            raise ValueError(
                f"Eastmoney returned neither JSON nor JSONP from {url} "
                f"(HTTP {response.status_code}, {len(body)} bytes)"
            ) from json_error
        return body


def _resolve_a_share_secid(code: str, suffix: str) -> str | None:
    """Map an A-share ``code`` + exchange ``suffix`` to its Eastmoney secid.

    SH instruments live on market ``1``; SZ and BJ (Beijing exchange) on ``0``.
    """
    if suffix == "SH":
        return f"1.{code}"
    if suffix in ("SZ", "BJ"):
        return f"0.{code}"
    return None


def _parse_us_secid(payload: Any) -> str | None:
    """Extract a US ``<market>.<code>`` secid from a search/suggest payload.

    Eastmoney's suggest endpoint returns candidates under
    ``QuotationCodeTable.Data``, each carrying a ``QuoteID`` already in
    ``<market>.<code>`` form. The first US-market (105/106/107) candidate wins.

    Args:
        payload: Decoded JSON from the search endpoint.

    Returns:
        The first matching US secid, or ``None`` when no US candidate is found.
    """
    if not isinstance(payload, dict):
        return None
    table = payload.get("QuotationCodeTable")
    if not isinstance(table, dict):
        return None
    candidates = table.get("Data")
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        quote_id = candidate.get("QuoteID")
        if not isinstance(quote_id, str) or "." not in quote_id:
            continue
        market = quote_id.split(".", 1)[0]
        if market in ("105", "106", "107"):
            return quote_id
    return None


def _strip_jsonp(text: str) -> Any:
    """Decode a possibly JSONP-wrapped body to a Python object.

    The suggest endpoint sometimes wraps its JSON in a ``callback(...)`` envelope.
    A plain JSON body is parsed as-is first; only a body that actually matches a
    leading callback identifier is unwrapped. This avoids corrupting plain JSON
    whose string values contain parentheses (an earlier first-``(`` / last-``)``
    slice mangled such bodies and returned ``None``).

    Args:
        text: Raw response body.

    Returns:
        The decoded object, or ``None`` when nothing parseable is found.
    """
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        pass
    match = _JSONP_WRAPPER.match(stripped)
    if match is None:
        return None
    try:
        return json.loads(match.group(1).strip())
    except (ValueError, TypeError):
        return None


def _cached_us_secid(code: str) -> tuple[bool, str | None]:
    """Look up a cached US resolution.

    Args:
        code: Bare upper-cased US ticker.

    Returns:
        ``(hit, secid)`` — ``hit`` is False when the ticker was never resolved
        or its recorded miss has expired.
    """
    entry = _US_SECID_CACHE.get(code)
    if entry is None:
        return False, None
    secid, expires_at = entry
    if time.monotonic() >= expires_at:
        _US_SECID_CACHE.pop(code, None)
        return False, None
    return True, secid


def _us_market_from_nasdaq_label(label: Any) -> str | None:
    """Map a Nasdaq ``exchange`` label onto an Eastmoney US market prefix.

    Args:
        label: The ``data.exchange`` value (e.g. ``"NASDAQ-GS"``, ``"NYSE"``).

    Returns:
        ``"105"`` / ``"106"`` / ``"107"``, or ``None`` for a venue we cannot
        place (OTC, BATS, an unfamiliar label).
    """
    if not isinstance(label, str):
        return None
    normalized = label.strip().upper()
    if not normalized:
        return None
    exact = _US_MARKET_BY_NASDAQ_EXCHANGE.get(normalized)
    if exact:
        return exact
    # Ordered: "NYSE AMERICAN" would otherwise be captured by the NYSE arm.
    if normalized.startswith("NYSE AMERICAN"):
        return "107"
    if normalized.startswith("NASDAQ"):
        return "105"
    if normalized.startswith("NYSE"):
        return "106"
    if normalized.startswith("AMEX"):
        return "107"
    return None


def _us_market_from_tencent_body(body: str) -> str | None:
    """Pull the venue out of a Tencent US quote line.

    Tencent answers ``v_usAAPL="200~苹果~AAPL.OQ~338.98~...";`` — the third
    tilde-separated field carries the ticker with its venue suffix.

    Args:
        body: The GBK-decoded response body.

    Returns:
        ``"105"`` / ``"106"`` / ``"107"``, or ``None`` when unparseable.
    """
    fields = body.split("~")
    if len(fields) < 3:
        return None
    symbol = fields[2].strip().upper()
    if "." not in symbol:
        return None
    return _US_MARKET_BY_TENCENT_SUFFIX.get(symbol[symbol.rfind("."):])


def _resolve_us_market_via_nasdaq(code: str) -> str | None:
    """Place a US ticker using Nasdaq's own quote API.

    Args:
        code: Bare upper-cased US ticker (e.g. ``"AAPL"``).

    Returns:
        The ``<market>.<code>`` secid, or ``None`` when Nasdaq cannot place it.
    """
    try:
        payload = throttled_get_json(
            _NASDAQ_INFO_URL.format(ticker=code),
            host_key=_NASDAQ_HOST_KEY,
            min_interval=resolve_min_interval(
                _NASDAQ_MIN_INTERVAL_ENV, _DEFAULT_NASDAQ_MIN_INTERVAL
            ),
            params={"assetclass": "stocks"},
        )
    except Exception as exc:  # noqa: BLE001 - a fallback failure is non-fatal
        logger.warning("nasdaq US exchange lookup failed for %s: %s", code, exc)
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    market = _us_market_from_nasdaq_label((data or {}).get("exchange"))
    return f"{market}.{code}" if market else None


def _resolve_us_market_via_tencent(code: str) -> str | None:
    """Place a US ticker using Tencent's quote line.

    Args:
        code: Bare upper-cased US ticker (e.g. ``"AAPL"``).

    Returns:
        The ``<market>.<code>`` secid, or ``None`` when Tencent cannot place it.
    """
    try:
        response = throttled_get(
            _TENCENT_QUOTE_URL.format(ticker=code),
            host_key=_TENCENT_HOST_KEY,
            min_interval=resolve_min_interval(
                _TENCENT_MIN_INTERVAL_ENV, _DEFAULT_TENCENT_MIN_INTERVAL
            ),
        )
        response.raise_for_status()
        # Tencent serves GBK, and the payload is a JS assignment rather than
        # JSON, so it is decoded and split by hand.
        body = response.content.decode("gbk", errors="replace")
    except Exception as exc:  # noqa: BLE001 - a fallback failure is non-fatal
        logger.warning("tencent US market lookup failed for %s: %s", code, exc)
        return None
    market = _us_market_from_tencent_body(body)
    return f"{market}.{code}" if market else None


def _resolve_us_secid_fallback(code: str) -> str | None:
    """Place a US ticker without the (dead) Eastmoney suggest endpoint.

    Args:
        code: Bare upper-cased US ticker (e.g. ``"AAPL"``).

    Returns:
        The ``<market>.<code>`` secid from the first source that can place the
        ticker, or ``None`` when none can.
    """
    for source, resolve in (
        ("nasdaq", _resolve_us_market_via_nasdaq),
        ("tencent", _resolve_us_market_via_tencent),
    ):
        secid = resolve(code)
        if secid is not None:
            logger.info("resolved US secid for %s via %s: %s", code, source, secid)
            return secid
    return None


def _resolve_us_secid(code: str) -> str | None:
    """Resolve a US ticker to its Eastmoney secid, with caching.

    Tries Eastmoney's suggest endpoint first and falls back to the Nasdaq /
    Tencent exchange lookups when the suggest surface answers without a
    candidate.

    Args:
        code: Bare upper-cased US ticker (e.g. ``"AAPL"``).

    Returns:
        The resolved ``<market>.<code>`` secid, or ``None`` when unresolvable.
    """
    hit, cached = _cached_us_secid(code)
    if hit:
        return cached

    secid: str | None = None
    try:
        payload = get_json(
            _SEARCH_URL,
            params={"input": code, "type": "14", "count": "10"},
        )
        if isinstance(payload, str):
            payload = _strip_jsonp(payload)
        secid = _parse_us_secid(payload)
        if secid is None:
            # The suggest surface answers without a candidate table, so an
            # unresolved ticker is indistinguishable from a service that
            # stopped honouring the query. Say so, then try the fallbacks below.
            shape = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
            logger.warning(
                "eastmoney suggest returned no US candidate for %s (payload keys: %s)",
                code,
                shape,
            )
    except Exception as exc:  # noqa: BLE001 - an unresolved ticker is non-fatal
        logger.warning("eastmoney secid search failed for %s: %s", code, exc)
        secid = None

    if secid is None:
        secid = _resolve_us_secid_fallback(code)

    # A resolved secid is stable, so it is cached for the life of the process;
    # a miss expires so a recovered suggest endpoint is picked up without a
    # restart.
    _US_SECID_CACHE[code] = (
        secid,
        math.inf if secid is not None else time.monotonic() + _US_SECID_MISS_TTL_S,
    )
    return secid


def resolve_secid(symbol: str) -> str | None:
    """Map a Vibe-Trading symbol to its Eastmoney secid.

    Supported suffixes: ``.SH`` / ``.SZ`` / ``.BJ`` (A-share), ``.HK`` (Hong
    Kong, code zero-padded to five digits), ``.US`` (resolved via search and
    cached). A symbol with no recognized suffix, or a US ticker the search
    cannot place, returns ``None``.

    Args:
        symbol: Symbol such as ``"600519.SH"``, ``"00700.HK"`` or ``"AAPL.US"``.

    Returns:
        The Eastmoney secid (e.g. ``"1.600519"``), or ``None`` if unresolvable.
    """
    if not symbol or "." not in symbol:
        return None
    code, _, suffix = symbol.rpartition(".")
    code = code.strip().upper()
    suffix = suffix.strip().upper()
    if not code:
        return None

    if suffix in ("SH", "SZ", "BJ"):
        return _resolve_a_share_secid(code, suffix)
    if suffix == "HK":
        return f"116.{code.zfill(5)}"
    if suffix == "US":
        return _resolve_us_secid(code)
    return None


def _parse_kline_row(raw: str) -> dict[str, Any] | None:
    """Parse one comma-joined push2his kline row into an OHLCV dict.

    Column order follows ``fields2``: date, open, close, high, low, volume,
    amount.

    Args:
        raw: One row string from ``data.klines``.

    Returns:
        A dict ``{trade_date, open, high, low, close, volume, amount}``, or
        ``None`` when the row is malformed.
    """
    parts = raw.split(",")
    if len(parts) < 7:
        return None
    try:
        return {
            "trade_date": parts[0],
            "open": float(parts[1]),
            "close": float(parts[2]),
            "high": float(parts[3]),
            "low": float(parts[4]),
            "volume": float(parts[5]),
            "amount": float(parts[6]),
        }
    except (ValueError, TypeError):
        return None


def fetch_kline(
    secid: str,
    *,
    klt: int,
    fqt: int = 1,
    beg: str = "0",
    end: str = "20500101",
) -> list[dict]:
    """Fetch ascending OHLCV bars for one ``secid`` from push2his.

    Args:
        secid: Eastmoney secid (e.g. ``"1.600519"``).
        klt: Period code from :data:`KLT_BY_INTERVAL`.
        fqt: Adjustment mode (0 raw, 1 forward-adjusted, 2 back-adjusted).
        beg: Inclusive start date ``YYYYMMDD`` or ``"0"`` for earliest.
        end: Inclusive end date ``YYYYMMDD``.

    Returns:
        Ascending list of ``{trade_date, open, high, low, close, volume,
        amount}`` dicts. Empty when the payload carries no bars.

    Raises:
        requests.RequestException: Network failure.
        requests.HTTPError: Non-2xx response status.
        ValueError: Body is not valid JSON.
    """
    payload = get_json(
        _KLINE_URL,
        params={
            "secid": secid,
            "klt": str(klt),
            "fqt": str(fqt),
            "beg": beg,
            "end": end,
            "fields1": _FIELDS1,
            "fields2": _FIELDS2,
            "rev": "1",
            "lmt": "1000000",
        },
    )

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    klines = data.get("klines")
    if not isinstance(klines, list):
        return []

    rows: list[dict] = []
    for raw in klines:
        if not isinstance(raw, str):
            continue
        parsed = _parse_kline_row(raw)
        if parsed is not None:
            rows.append(parsed)
    return rows
