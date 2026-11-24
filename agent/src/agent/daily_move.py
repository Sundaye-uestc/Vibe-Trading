"""Compute a requested move from dated closes in the same observed daily series."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Sequence
from zoneinfo import ZoneInfo

from src.us_indices import US_INDEX_NAMES

if TYPE_CHECKING:
    from src.agent.grounding import EvidenceRecord, IdentityRecord


# Venue suffixes describe a trading calendar, independent of the particular
# security. The day must already have ended locally; this intentionally does
# not guess early closes or exchange holidays.
_LISTING_ZONES = {
    "US": "America/New_York", "SH": "Asia/Shanghai", "SZ": "Asia/Shanghai",
    "BJ": "Asia/Shanghai", "HK": "Asia/Hong_Kong", "TO": "America/Toronto",
    "V": "America/Vancouver", "L": "Europe/London", "DE": "Europe/Berlin",
    "PA": "Europe/Paris", "KS": "Asia/Seoul", "KQ": "Asia/Seoul",
    "NS": "Asia/Kolkata", "BO": "Asia/Kolkata", "VN": "Asia/Ho_Chi_Minh",
    "T": "Asia/Tokyo", "AX": "Australia/Sydney",
}
_FUTURES_ZONES = {
    "CMX": "America/Chicago", "COMEX": "America/Chicago", "CME": "America/Chicago",
    "NYM": "America/Chicago", "NYMEX": "America/Chicago", "CBT": "America/Chicago",
}


def _session_window(symbol: str, identity: IdentityRecord, as_of: datetime) -> tuple[str, str, bool] | None:
    target = as_of.astimezone(timezone.utc).date() - timedelta(days=1)
    kind = identity.instrument_type
    listing_zone = _LISTING_ZONES.get(symbol.rsplit(".", 1)[1]) if "." in symbol else None
    if symbol in US_INDEX_NAMES:
        listing_zone = "America/New_York"
    if kind == "crypto":
        # Crypto trades seven days a week. Only an adjacent pair of daily
        # date labels can establish a day-over-day move.
        zone = "UTC"
    elif kind == "forex":
        zone = "America/New_York"  # Allow the New York roll to finish.
    elif kind == "future":
        zone = _FUTURES_ZONES.get((identity.venue or "").upper())
    else:
        zone = listing_zone
    # With no exchange timezone, only use a dated close once that date has
    # ended even in UTC-12. Some provider indices have no venue metadata.
    local_zone = ZoneInfo(zone) if zone else timezone(-timedelta(hours=12))
    if target >= as_of.astimezone(local_zone).date():
        return None
    if listing_zone and target.weekday() >= 5:
        return None
    weekend_span = target.weekday() == 0 and kind != "crypto"
    previous = target - timedelta(days=3 if weekend_span else 1)
    return previous.isoformat(), target.isoformat(), bool(weekend_span and not listing_zone)


def _price_text(value: float, high_precision: bool) -> str:
    if high_precision and value != 0:
        return format(value, ",.8g")
    return format(value, ",.2f")


_INDEX_SUBJECTS = (
    ("^NDX", r"(?:纳斯达克|纳指|nasdaq)[\s-]*(?:100|一百)"),
    ("^IXIC", r"(?:纳指|纳斯达克|nasdaq)(?![\s-]*(?:100|一百|etf|期货))"),
    ("^GSPC", r"标普[\s-]*(?:500|五百)|s&p\s*500"),
    ("^DJI", r"道琼斯|道指|dow jones"),
)


def named_indices(question: str) -> set[str]:
    return {symbol for symbol, pattern in _INDEX_SUBJECTS if re.search(pattern, question, re.I)}


def requested_daily_move(question: str) -> bool:
    return bool(re.search(r"昨天|昨日|yesterday", question, re.I)
                and re.search(r"涨|跌|涨跌幅|表现|走势|return|gain|move|rally|rise|fall|drop|perform", question, re.I))


def render_daily_moves(question: str, as_of: datetime, records: Sequence[EvidenceRecord],
                       daily_calls: set[str], identities: dict[str, IdentityRecord], requested: set[str]) -> str:
    """Require the requested, finished daily date and a same-series baseline.

    Across a holiday or missing weekday, two rows alone do not establish a
    one-session return. The ordinary evidence path still handles those cases.
    """
    if not daily_calls or not requested:
        return ""
    windows = {symbol: window for symbol, identity in identities.items()
               if (window := _session_window(symbol, identity, as_of)) is not None}
    groups = defaultdict(lambda: defaultdict(list))
    observed_closes = defaultdict(set)
    for r in records:
        if (r.call_id not in daily_calls or r.tool != "get_market_data" or r.status != "observed"
                or r.symbol not in windows or not r.source or r.source in {"auto", "unknown"}
                or r.field not in {"open", "close"} or r.value is None or not r.timestamp):
            continue
        start, end, _ = windows[r.symbol]
        # Daily loaders expose date labels at midnight. An actual intraday
        # timestamp from a mismatched response is not a settlement observation.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T]00:00:00(?:\.0+)?(?:Z|[+-]\d{2}:?\d{2})?)?", r.timestamp):
            continue
        day = r.timestamp[:10]
        if day in (start, end):
            groups[(r.symbol, r.call_id, r.source, r.currency)][(day, r.field)].append(r.value)
            if r.field == "close":
                observed_closes[(r.symbol, day)].add(r.value)

    # Keep each return within one call, and suppress a symbol if independent
    # reads disagree on either close. Never choose a pair to fit model prose.
    pairs = defaultdict(list)
    conflicting = set()
    for (symbol, _, source, currency), fields in groups.items():
        start, end, _ = windows[symbol]
        left, right = fields[(start, "close")], fields[(end, "close")]
        if not left or not right:
            continue
        if len(left) != 1 or len(right) != 1 or not all(math.isfinite(x) and x > 0 for x in (left[0], right[0])):
            conflicting.add(symbol)
            continue
        opens = fields[(end, "open")]
        opening = opens[0] if len(opens) == 1 and math.isfinite(opens[0]) and opens[0] > 0 else None
        pairs[symbol].append((left[0], right[0], opening, source, currency))

    zh = bool(re.search(r"[\u3400-\u9fff]", question))
    by_symbol = {}
    for symbol, observations in pairs.items():
        start, end, interval_only = windows[symbol]
        if symbol in conflicting or any(len(observed_closes[(symbol, day)]) != 1 for day in (start, end)):
            continue
        if len({row[4] for row in observations}) != 1:
            continue
        a, b, opening, source, currency = observations[-1]
        pct = (b / a - 1) * 100
        if not math.isfinite(pct):
            continue
        name = US_INDEX_NAMES.get(symbol)
        label = symbol
        if name:
            label = f"{name[0]}（{symbol}）" if zh else f"{name[1]} ({symbol})"
        unit = (" 点" if zh else " points") if name else (f" {currency}" if currency else "")
        precision = identities[symbol].instrument_type in {"forex", "crypto"} or min(a, b) < 1
        prev_close, close, change = (_price_text(v, precision) for v in (a, b, abs(b-a)))
        if zh:
            direction = "上涨" if b > a else "下跌" if b < a else "变动"
            line = (f"{label} {end} 收于 {close}{unit}，较 {start} 的 {prev_close}{unit}"
                    f"{direction} {change}{unit}（**{pct:+.2f}%**）。")
        else:
            signed = ("+" if b >= a else "-") + change
            line = f"{label} closed at {close}{unit} on {end}, a change of {signed}{unit} (**{pct:+.2f}%**) from {prev_close}{unit} on {start}."
        if interval_only:
            line += " 此处为两个所示日期之间的区间变动。" if zh else " This is the return between the two dates shown."
        if opening is not None and not interval_only and len({r[2] for r in observations}) == 1:
            opening_text = _price_text(opening, precision)
            if opening > b > a:
                line += (f"当日高开于 {opening_text}{unit}，收盘低于开盘，回吐了部分涨幅。" if zh else
                         f" It opened at {opening_text}{unit} and gave back part of the gain by the close.")
            elif opening < b < a:
                line += (f"当日低开于 {opening_text}{unit}，收盘高于开盘，收复了部分跌幅。" if zh else
                         f" It opened at {opening_text}{unit} and recovered part of the loss by the close.")
        by_symbol[symbol] = line + (f"（{source} 日线）" if zh else f" ({source}, daily bars)")
    if not requested.issubset(by_symbol):
        return ""
    ordered = sorted(by_symbol, key=lambda symbol: symbol not in requested)
    return "\n\n".join(by_symbol[symbol] for symbol in ordered[:8])
