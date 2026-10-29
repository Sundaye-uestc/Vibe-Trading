"""Free valuation fallback preserves date, source and alpha-runner contracts."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backtest import runner
from backtest.loaders import valuation_loader as vl
from src.factors.registry import Registry
from src.factors import bench_runner, bench_runner_strict, compare_runner
from src.tools import alpha_bench_tool as tool
from tests.factors.test_valuation_baostock_fallback import _FakeBaostock, _install_fake_baostock


DATES = pd.date_range("2024-01-02", periods=3)


def test_fallback_fills_individual_symbols_dates_and_fields_without_overwriting(monkeypatch):
    def primary(**kw):
        if kw["ts_code"] == "000001.SZ":
            return pd.DataFrame({"trade_date": ["20240102", "20240104"], "pb": [1.0, None], "ps_ttm": [8.0, 9.0]})
        return pd.DataFrame({"trade_date": ["20240102", "20240103", "20240104"], "pb": [10.0, 11.0, 12.0]})

    fake = _FakeBaostock({
        "sz.000001": [{"date": d.strftime("%Y-%m-%d"), "pbMRQ": str(i + 2), "psTTM": "2"} for i, d in enumerate(DATES)],
        "sh.600000": [{"date": d.strftime("%Y-%m-%d"), "pbMRQ": "999", "psTTM": "3"} for d in DATES],
    })
    _install_fake_baostock(monkeypatch, fake)
    panels = vl.load_valuation_panel(symbols=["000001.SZ", "600000.SH"], fields=["pb", "ps_ttm", "dv_ttm"],
                                    start="2024-01-02", end="2024-01-04", index=DATES,
                                    api=SimpleNamespace(daily_basic=primary))
    np.testing.assert_allclose(panels["pb"]["000001.SZ"], [1, 3, 4])
    np.testing.assert_allclose(panels["pb"]["600000.SH"], [10, 11, 12])
    np.testing.assert_allclose(panels["ps_ttm"]["000001.SZ"], [8, 2, 9])
    assert panels["dv_ttm"].isna().all().all()
    assert panels["pb"].attrs["valuation"]["source_counts"] == {"tushare_daily_basic": 4, "baostock": 2}
    assert panels["dv_ttm"].attrs["valuation"]["unit"] == "percent"
    assert panels["pb"].attrs["valuation"]["available_at"] == "trade_date_close"


def test_future_rows_missing_dates_and_conflicting_duplicates_are_not_filled(monkeypatch):
    monkeypatch.setattr(vl, "_baostock_available", lambda: False)
    api = SimpleNamespace(daily_basic=lambda **kw: pd.DataFrame({
        "trade_date": ["20240103", "20240103", "20240104", "20240105"],
        "pb": [2, 3, 4, 99],
    }))
    result = vl.load_valuation_panel(symbols=["000001.SZ"], fields=["pb"], start="2024-01-02",
                                     end="2024-01-04", index=DATES, api=api)["pb"]
    assert result["000001.SZ"].iloc[:2].isna().all()
    assert result["000001.SZ"].iloc[2] == 4


def test_timezone_labels_align_daily_values_but_intraday_never_gets_eod_values(monkeypatch):
    monkeypatch.setattr(vl, "_baostock_available", lambda: False)
    index = pd.DatetimeIndex(["2024-01-02 00:00", "2024-01-02 10:00"], tz="Asia/Shanghai")
    api = SimpleNamespace(daily_basic=lambda **kw: pd.DataFrame({"trade_date": ["20240102"], "pb": [2]}))
    result = vl.load_valuation_panel(symbols=["000001.SZ"], fields=["pb"], start="2024-01-02",
                                     end="2024-01-04", index=index, api=api)["pb"]
    assert result.index.equals(index)
    assert result.iloc[0, 0] == 2 and pd.isna(result.iloc[1, 0])


def test_bad_primary_values_are_missing_and_baostock_zero_is_preserved(monkeypatch):
    api = SimpleNamespace(daily_basic=lambda **kw: pd.DataFrame({"trade_date": DATES,
                                                                "pb": [True, np.inf, -np.inf]}))
    fake = _FakeBaostock({"sz.000001": [{"date": d.strftime("%Y-%m-%d"), "pbMRQ": "0"} for d in DATES]})
    _install_fake_baostock(monkeypatch, fake)
    result = vl.load_valuation_panel(symbols=["000001.SZ"], fields=["pb"], start="2024-01-02",
                                     end="2024-01-04", index=DATES, api=api)["pb"]
    assert result.eq(0).all().all()
    assert result.attrs["valuation"]["source_counts"] == {"tushare_daily_basic": 0, "baostock": 3}


def test_non_equities_do_not_construct_any_provider(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No provider should be touched")
    monkeypatch.setattr(vl, "_resolve_tushare_api", forbidden)
    monkeypatch.setattr(vl, "_baostock_available", forbidden)
    result = vl.load_valuation_panel(symbols=["000300.SH", "510050.SH", "159915.SZ", "AAPL.US", "00700.HK"],
                                     fields=["pb"], start="2024-01-02", end="2024-01-04", index=DATES)
    assert result["pb"].isna().all().all()


def test_unsupported_fallback_fields_never_open_baostock(monkeypatch):
    monkeypatch.setattr(vl, "_resolve_tushare_api", lambda _: None)
    monkeypatch.setattr(vl, "_baostock_available", lambda: pytest.fail("no fallback fields"))
    result = vl.load_valuation_panel(symbols=["000001.SZ"], fields=["pe", "dv_ttm"], start="2024-01-02",
                                     end="2024-01-04", index=DATES)
    assert all(frame.isna().all().all() for frame in result.values())


def test_iterator_and_logout_failures_do_not_discard_other_symbols(monkeypatch):
    class Broken(_FakeBaostock):
        def query_history_k_data_plus(self, code, fields, **kwargs):
            if code.startswith("sz."):
                return SimpleNamespace(error_code="0", next=lambda: (_ for _ in ()).throw(ConnectionError("closed")))
            return super().query_history_k_data_plus(code, fields, **kwargs)

        def logout(self):
            raise RuntimeError("logout failed")

    monkeypatch.setattr(vl, "_resolve_tushare_api", lambda _: None)
    _install_fake_baostock(monkeypatch, Broken({"sh.600000": [{"date": "2024-01-02", "pbMRQ": "2"}]}))
    result = vl.load_valuation_panel(symbols=["000001.SZ", "600000.SH"], fields=["pb"], start="2024-01-02",
                                     end="2024-01-04", index=DATES)["pb"]
    assert result["000001.SZ"].isna().all() and result.loc[DATES[0], "600000.SH"] == 2


@pytest.fixture
def bench_inputs(monkeypatch):
    dates = pd.date_range("2024-01-01", periods=40)
    codes = ["000001.SZ", "600000.SH", "600519.SH", "000002.SZ", "600036.SH", "601318.SH"]
    t = np.arange(len(dates), dtype=float)
    close = pd.DataFrame({code: 10 + j + np.sin(t / (j + 2)) for j, code in enumerate(codes)}, index=dates)
    pb = pd.DataFrame({code: 1 + j + np.cos(t / (j + 3)) / 5 for j, code in enumerate(codes)}, index=dates)
    panel = {"close": close, "_meta": {"survivorship_bias": False}}
    requests = []

    def load(**kwargs):
        requests.append(kwargs)
        result = {}
        for field in kwargs["fields"]:
            frame = pb.copy() if field != "dv_ttm" else pb * np.nan
            frame.attrs["valuation"] = {"sources": ["baostock"] if field != "dv_ttm" else [],
                                         "unit": "percent" if field == "dv_ttm" else "ratio"}
            result[field] = frame
        return result

    monkeypatch.setattr(vl, "load_valuation_panel", load)
    monkeypatch.setattr(tool, "_load_universe_panel", lambda *a, **kw: panel)
    monkeypatch.setattr(bench_runner, "_load_universe_panel", lambda *a, **kw: panel)
    monkeypatch.setattr(bench_runner_strict, "_load_universe_panel", lambda *a, **kw: panel)
    return Registry(), panel, requests


def test_bench_loader_preserves_membership_mask_source_and_input(bench_inputs):
    registry, panel, requests = bench_inputs
    panel["close"].iloc[0, 0] = np.nan
    result = tool._prepare_valuation_panel(registry, ["fund_pb"], panel, "csi300", "2024-2024")
    assert "val:pb" not in panel and "valuation" not in panel["_meta"]
    assert pd.isna(result["val:pb"].iloc[0, 0])
    assert result["_meta"]["valuation"]["val:pb"]["sources"] == ["baostock"]
    assert requests[0]["fields"] == ["pb"]


def test_bench_tool_and_compare_use_valuation_inputs(bench_inputs, tmp_path):
    registry, _, requests = bench_inputs
    result = tool.run_alpha_bench(alpha_id="fund_pb", universe="csi300", period="2024-2024", output_dir=str(tmp_path))
    assert result["status"] == "ok" and result["n_alphas_tested"] == 1
    assert result["meta"]["valuation"]["val:pb"]["sources"] == ["baostock"]
    compared = compare_runner.compare_alphas(["fund_pb", "fund_ps_ttm"], "csi300", "2024-2024", registry=registry)
    assert compared["status"] == "ok" and compared["n_compared"] == 2
    assert any(set(request["fields"]) == {"pb", "ps_ttm"} for request in requests)


def test_missing_dividend_field_skips_only_that_factor(bench_inputs):
    registry, _, _ = bench_inputs
    result = bench_runner.run_bench("fundamental", "csi300", "2024-2024", registry=registry,
                                    only=["fund_pb", "fund_ps_ttm", "fund_dividend_yield"])
    assert result["status"] == "ok"
    assert result["n_alphas_tested"] == 2 and result["n_skipped"] == 1


def test_strict_bench_injects_selected_valuation_field(bench_inputs):
    registry, _, requests = bench_inputs
    # Keep this focused on panel integration; restrict the registry's selection.
    original = registry.list
    registry.list = lambda **kwargs: [aid for aid in original(**kwargs) if aid == "fund_pb"]
    result = bench_runner_strict.run_bench_strict("fundamental", "csi300", "2024-2024", registry=registry,
                                                random_control=True, n_random_seeds=2)
    assert result["status"] == "ok" and result["n_alphas_tested"] == 1
    assert requests[0]["fields"] == ["pb"]


def test_backtest_values_follow_observed_close_and_keep_provenance(monkeypatch):
    monkeypatch.setattr(vl, "_baostock_available", lambda: False)
    monkeypatch.setattr(vl, "_resolve_tushare_api", lambda _: SimpleNamespace(daily_basic=lambda **kw:
        pd.DataFrame({"trade_date": DATES, "pb": [1, 2, 3]})))
    original = pd.DataFrame({"close": [10, np.nan, 11]}, index=DATES)
    result = runner._maybe_inject_fundamentals_for_factor_panel({"000001.SZ": original}, {
        "start_date": "2024-01-02", "end_date": "2024-01-04", "selected_factors": [{"columns_required": ["val:pb"]}],
    })
    assert "val:pb" not in original
    assert result["000001.SZ"]["val:pb"].iloc[[0, 2]].tolist() == [1, 3]
    assert pd.isna(result["000001.SZ"]["val:pb"].iloc[1])
    assert result["000001.SZ"].attrs["valuation"]["val:pb"]["sources"] == ["tushare_daily_basic"]
