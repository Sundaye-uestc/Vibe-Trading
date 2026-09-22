"""Tests for eastmoney_client: secid resolution, kline parsing, error paths.

All HTTP is mocked at :func:`backtest.loaders._http.throttled_get_json` (imported
into the client module), so no test touches a live Eastmoney endpoint.
"""

from unittest.mock import patch

import pytest

from backtest.loaders import eastmoney_client as ec


@pytest.fixture(autouse=True)
def _clear_us_cache():
    """Reset the module-level US secid cache so tests stay independent."""
    ec._US_SECID_CACHE.clear()
    yield
    ec._US_SECID_CACHE.clear()


class TestResolveSecidAShare:
    """A-share / HK secid mapping is pure and needs no network."""

    def test_shanghai_uses_market_1(self):
        assert ec.resolve_secid("600519.SH") == "1.600519"

    def test_shenzhen_uses_market_0(self):
        assert ec.resolve_secid("000001.SZ") == "0.000001"

    def test_beijing_uses_market_0(self):
        assert ec.resolve_secid("830799.BJ") == "0.830799"

    def test_hong_kong_zero_pads_to_five(self):
        assert ec.resolve_secid("00700.HK") == "116.00700"
        assert ec.resolve_secid("700.HK") == "116.00700"

    def test_case_insensitive_suffix(self):
        assert ec.resolve_secid("600519.sh") == "1.600519"

    def test_unrecognized_suffix_returns_none(self):
        assert ec.resolve_secid("BTC-USD") is None
        assert ec.resolve_secid("AAPL") is None
        assert ec.resolve_secid("") is None
        assert ec.resolve_secid(".SH") is None


class TestResolveSecidUS:
    """US tickers resolve via the search endpoint and cache the result."""

    def test_resolves_and_caches_us_secid(self):
        payload = {
            "QuotationCodeTable": {
                "Data": [
                    {"Code": "AAPL", "QuoteID": "105.AAPL"},
                ]
            }
        }
        with patch.object(ec, "throttled_get_json", return_value=payload) as mock_get:
            assert ec.resolve_secid("AAPL.US") == "105.AAPL"
            # Second call is served from cache: no further HTTP.
            assert ec.resolve_secid("AAPL.US") == "105.AAPL"
        assert mock_get.call_count == 1

    def test_handles_jsonp_wrapped_search_body(self):
        body = 'cb({"QuotationCodeTable":{"Data":[{"QuoteID":"106.BRK"}]}})'
        with patch.object(ec, "throttled_get_json", return_value=body):
            assert ec.resolve_secid("BRK.US") == "106.BRK"

    def test_plain_json_body_with_parens_in_values(self):
        """A non-JSONP body whose strings contain parens must parse intact.

        Regression for B8-em-jsonp: the old first-``(`` / last-``)`` slice
        corrupted plain JSON like ``"Apple Inc. (AAPL)"`` and returned ``None``.
        """
        body = (
            '{"QuotationCodeTable":{"Data":'
            '[{"QuoteID":"105.AAPL","Name":"Apple Inc. (AAPL) [NASDAQ]"}]}}'
        )
        with patch.object(ec, "throttled_get_json", return_value=body):
            assert ec.resolve_secid("AAPL.US") == "105.AAPL"

    def test_no_us_candidate_returns_none(self):
        payload = {"QuotationCodeTable": {"Data": [{"QuoteID": "1.600519"}]}}
        with patch.object(ec, "throttled_get_json", return_value=payload), patch.object(
            ec, "_resolve_us_market_via_nasdaq", return_value=None
        ), patch.object(ec, "_resolve_us_market_via_tencent", return_value=None):
            assert ec.resolve_secid("NOPE.US") is None

    def test_search_failure_returns_none_without_raising(self):
        with patch.object(
            ec, "throttled_get_json", side_effect=RuntimeError("banned")
        ), patch.object(
            ec, "_resolve_us_market_via_nasdaq", return_value=None
        ), patch.object(ec, "_resolve_us_market_via_tencent", return_value=None):
            assert ec.resolve_secid("AAPL.US") is None


class TestUsMarketLabels:
    """Pure parsers that turn a venue label into an Eastmoney market prefix."""

    def test_nasdaq_tiers_all_map_to_105(self):
        for label in ("NASDAQ", "NASDAQ-GS", "NASDAQ-GM", "NASDAQ-CM"):
            assert ec._us_market_from_nasdaq_label(label) == "105", label

    def test_nyse_and_amex_map_to_106_and_107(self):
        assert ec._us_market_from_nasdaq_label("NYSE") == "106"
        assert ec._us_market_from_nasdaq_label("AMEX") == "107"

    def test_nyse_american_is_amex_not_nyse(self):
        """The AMEX arm must win: a prefix match on NYSE would give 106."""
        assert ec._us_market_from_nasdaq_label("NYSE American") == "107"

    def test_label_matching_is_case_and_space_insensitive(self):
        assert ec._us_market_from_nasdaq_label("  nasdaq-gs ") == "105"

    def test_unplaceable_labels_return_none(self):
        for label in ("PNK", "OTC", "BATS", "", None, 7):
            assert ec._us_market_from_nasdaq_label(label) is None, label

    def test_tencent_suffixes_map_to_their_venue(self):
        cases = {".OQ": "105", ".N": "106", ".A": "107"}
        for suffix, market in cases.items():
            body = f'v_usAAPL="200~苹果~AAPL{suffix}~338.98~336.13";'
            assert ec._us_market_from_tencent_body(body) == market, suffix

    def test_tencent_unparseable_bodies_return_none(self):
        for body in ("", "not a quote", 'v_usAAPL="200~only-two";', 'v_usAAPL="200~x~AAPL~1";'):
            assert ec._us_market_from_tencent_body(body) is None, body


class TestResolveSecidUSFallback:
    """When suggest answers without a candidate, Nasdaq/Tencent place the ticker.

    The suggest endpoint is the surface that used to answer this, and it now
    returns the same candidate-less body for every query.
    """

    #: The fixed candidate-less body the suggest endpoint serves for every query.
    _DEAD_SUGGEST_BODY = {"passportWeb": {"uid": "0"}}

    def test_nasdaq_places_the_ticker_and_tencent_is_not_consulted(self):
        with patch.object(
            ec, "throttled_get_json", return_value=self._DEAD_SUGGEST_BODY
        ), patch.object(
            ec, "_resolve_us_market_via_nasdaq", return_value="105.AAPL"
        ), patch.object(ec, "_resolve_us_market_via_tencent") as tencent:
            assert ec.resolve_secid("AAPL.US") == "105.AAPL"

        tencent.assert_not_called()

    def test_tencent_places_the_ticker_when_nasdaq_cannot(self):
        with patch.object(
            ec, "throttled_get_json", return_value=self._DEAD_SUGGEST_BODY
        ), patch.object(
            ec, "_resolve_us_market_via_nasdaq", return_value=None
        ), patch.object(
            ec, "_resolve_us_market_via_tencent", return_value="106.BRK"
        ):
            assert ec.resolve_secid("BRK.US") == "106.BRK"

    def test_both_sources_failing_returns_none_without_raising(self):
        with patch.object(
            ec, "throttled_get_json", return_value=self._DEAD_SUGGEST_BODY
        ), patch.object(
            ec, "_resolve_us_market_via_nasdaq", return_value=None
        ), patch.object(ec, "_resolve_us_market_via_tencent", return_value=None):
            assert ec.resolve_secid("XYZ.US") is None

    def test_a_working_suggest_endpoint_skips_the_fallbacks(self):
        payload = {"QuotationCodeTable": {"Data": [{"QuoteID": "105.AAPL"}]}}
        with patch.object(ec, "throttled_get_json", return_value=payload), patch.object(
            ec, "_resolve_us_market_via_nasdaq"
        ) as nasdaq, patch.object(ec, "_resolve_us_market_via_tencent") as tencent:
            assert ec.resolve_secid("AAPL.US") == "105.AAPL"

        nasdaq.assert_not_called()
        tencent.assert_not_called()

    def test_miss_is_cached_only_until_ttl_expires(self):
        """A miss must not be pinned forever — a recovered endpoint re-resolves."""
        payload = {"QuotationCodeTable": {"Data": [{"QuoteID": "105.AAPL"}]}}
        with patch.object(
            ec, "throttled_get_json", return_value=self._DEAD_SUGGEST_BODY
        ) as mock_dead, patch.object(
            ec, "_resolve_us_market_via_nasdaq", return_value=None
        ), patch.object(ec, "_resolve_us_market_via_tencent", return_value=None):
            assert ec.resolve_secid("AAPL.US") is None
            assert ec.resolve_secid("AAPL.US") is None
        assert mock_dead.call_count == 1  # second call served from the miss entry

        # Age the recorded miss past its TTL; the endpoint is healthy again.
        ec._US_SECID_CACHE["AAPL"] = (None, 0.0)
        with patch.object(ec, "throttled_get_json", return_value=payload):
            assert ec.resolve_secid("AAPL.US") == "105.AAPL"


class TestFetchKline:
    """push2his payload parsing into ascending OHLCV dicts."""

    def test_parses_ohlcv_amount_rows(self):
        payload = {
            "data": {
                "code": "600519",
                "klines": [
                    "2024-01-02,100.0,110.0,112.0,99.0,1000,123456.0",
                    "2024-01-03,110.0,108.0,111.0,107.0,2000,234567.0",
                ],
            }
        }
        with patch.object(ec, "throttled_get_json", return_value=payload) as mock_get:
            rows = ec.fetch_kline("1.600519", klt=ec.KLT_BY_INTERVAL["1D"])

        assert rows == [
            {
                "trade_date": "2024-01-02",
                "open": 100.0,
                "close": 110.0,
                "high": 112.0,
                "low": 99.0,
                "volume": 1000.0,
                "amount": 123456.0,
            },
            {
                "trade_date": "2024-01-03",
                "open": 110.0,
                "close": 108.0,
                "high": 111.0,
                "low": 107.0,
                "volume": 2000.0,
                "amount": 234567.0,
            },
        ]
        # secid + klt flow through to the request params.
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["secid"] == "1.600519"
        assert kwargs["params"]["klt"] == "101"

    def test_empty_data_returns_empty_list(self):
        with patch.object(ec, "throttled_get_json", return_value={"data": None}):
            assert ec.fetch_kline("1.600519", klt=101) == []

    def test_malformed_row_is_skipped_not_fatal(self):
        payload = {
            "data": {
                "klines": [
                    "garbage,row",
                    "2024-01-03,110.0,108.0,111.0,107.0,2000,234567.0",
                ]
            }
        }
        with patch.object(ec, "throttled_get_json", return_value=payload):
            rows = ec.fetch_kline("1.600519", klt=101)
        assert len(rows) == 1
        assert rows[0]["trade_date"] == "2024-01-03"

    def test_http_error_propagates(self):
        with patch.object(
            ec, "throttled_get_json", side_effect=RuntimeError("HTTP 429")
        ):
            with pytest.raises(RuntimeError, match="429"):
                ec.fetch_kline("1.600519", klt=101)


class TestStripJsonp:
    """_strip_jsonp must prefer raw JSON and only unwrap real JSONP."""

    def test_plain_json_with_parens_parses_unchanged(self):
        # Parens inside string values must not be stripped.
        body = '{"name":"Berkshire (BRK.A)","ids":["106.BRK","x(y)z"]}'
        assert ec._strip_jsonp(body) == {
            "name": "Berkshire (BRK.A)",
            "ids": ["106.BRK", "x(y)z"],
        }

    def test_plain_json_array_parses_unchanged(self):
        assert ec._strip_jsonp('["a(b)", "c"]') == ["a(b)", "c"]

    def test_real_jsonp_wrapper_is_stripped(self):
        body = 'cb({"QuoteID":"106.BRK","name":"foo (bar)"})'
        assert ec._strip_jsonp(body) == {"QuoteID": "106.BRK", "name": "foo (bar)"}

    def test_dotted_callback_identifier_is_stripped(self):
        body = 'jQuery.cb123(["x", "y"]);'
        assert ec._strip_jsonp(body) == ["x", "y"]

    def test_unparseable_body_returns_none(self):
        assert ec._strip_jsonp("not json at all (oops") is None


class TestKltMapping:
    """Interval-to-klt table covers the documented periods."""

    def test_intraday_and_daily_codes(self):
        assert ec.KLT_BY_INTERVAL["1m"] == 1
        assert ec.KLT_BY_INTERVAL["5m"] == 5
        assert ec.KLT_BY_INTERVAL["1H"] == 60
        assert ec.KLT_BY_INTERVAL["60m"] == 60
        assert ec.KLT_BY_INTERVAL["1D"] == 101
        assert ec.KLT_BY_INTERVAL["1W"] == 102
        assert ec.KLT_BY_INTERVAL["1M"] == 103
