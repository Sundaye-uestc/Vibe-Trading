"""Rate thresholds are future conditions; neighbouring current quotes are facts."""
import json

import pytest

from src.agent.grounding import GroundingLedger


@pytest.fixture
def ledger(tmp_path):
    ledger = GroundingLedger(run_dir=tmp_path, user_message="分析 GLD.US 与 ^TNX")
    ledger.ingest_tool_result(
        tool_name="get_market_data", arguments={"codes": ["^TNX"]},
        result=json.dumps({"^TNX": [
            {"trade_date": "2026-10-30", "high": 5.049, "close": 4.929},
            {"trade_date": "2026-11-06", "close": 4.859},
            {"trade_date": "2026-11-11", "close": 4.998},
        ]}), call_id="rates", success=True,
    )
    return ledger


@pytest.mark.parametrize("claim", [
    "若10Y名义收益率回落至4.80%以下，则修复情景增强。",
    "推翻条件很明确：美元指数回落、10Y 名义收益率回落至4.80%以下。",
    "条件：^TNX收益率上破5.049%；当前10Y名义收益率 ^TNX 为4.998%。",
    "| 触发条件 | 含义 |\n|---|---|\n| 10Y名义收益率回落至4.80%以下 | 修复增强 |",
    "情景C前提：收益率5.049%之上）与当前观测值差距很小——^TNX当前4.998%距5.049%很近。",
    "If 10Y nominal yield falls to 4.80%, consider the recovery scenario.",
    "^TNX 10Y名义收益率4.998%。",
    "收益率4.998%（^TNX）。",
    "^TNX 2026-10-30 美债收益率高点5.049%。",
])
def test_explicit_future_rate_thresholds_are_not_portfolio_returns(ledger, claim):
    assert ledger._validate_analysis_claims(claim) == []


@pytest.mark.parametrize("claim", [
    "10Y名义收益率4.80%以下。",  # No conditional frame, instrument unspecified.
    "条件：关注美债 ^TNX，最新10Y名义收益率4.80%以下。",
    "条件：关注美债 ^TNX，10Y名义收益率现为4.80%以下。",
    "条件：关注美债 ^TNX，10Y名义收益率4.80%以下（最新数据）。",
    "条件：^TNX收益率已回落至4.80%以下。",
    "条件：^TNX收益率在4.80%以下（已见高点）。",
    "条件：^TNX收益率上破5.049%，当前10Y名义收益率 ^TNX 为8.5%。",
    "情景C前提：收益率5.049%之上）——^TNX当前8.5%距5.049%很近。",
    "若10Y名义收益率回落至4.80%以下。^TNX收益率回落至4.80%。",
    "^TNX收益率回落至４．８０％以下（备注）。下一句若收益率反弹则另行判断。",
    "若^TNX美债收益率回落至2026-10-30高点8.5%以下。",
    "^TNX 2026-11-11 美债收益率高点5.049%。",  # Wrong date for the high.
    "^TNX当前10Y实际收益率4.998%。",  # Nominal data does not certify real yields.
    "^TNX当前5Y名义收益率4.998%。",  # Another tenor cannot borrow ^TNX.
    "日本10Y名义收益率 ^TNX 4.998%。",
    "^TNX 当前收益率8.5%。",
    "若^TNX收益率降至4.80%以下，策略年化收益率30%。",
    "策略：收益率30%（^TNX）。",
])
def test_condition_label_cannot_launder_observed_yield_or_investment_return(ledger, claim):
    assert ledger._validate_analysis_claims(claim)


def test_real_recorded_repair_sentence_keeps_each_rate_role(ledger):
    text = ("- 关注度最高的原因：情景 C 的两个外部前提（美元 102.893 之上、"
            "收益率 5.049% 之上）与当前观测值差距很小——^TNX 当前 4.998% 距 5.049% "
            "仅约一个百分点的百分之一量级，而情景 B 还需要 GLD 站上 382.99 的明显涨幅。")
    assert ledger._validate_analysis_claims(text) == []
    assert ledger._validate_analysis_claims(text.replace("当前 4.998%", "当前 8.5%"))


def rate_table(values="4.998%（周 bar 未完成）；前两周4.929% / 4.859%",
               dates="2026-11-11 / 10-30 / 11-06"):
    return f"| 指标 | 观测值 | 日期 |\n|---|---|---|\n| 10Y美债收益率^TNX | {values} | {dates} |"


@pytest.mark.parametrize("dates", [
    "2026-11-11 / 10-30 / 11-06",
    "2026-11-11 /10-30 /11-06",
    "2026-11-11 / 2026-10-30 / 2026-11-06",
    "2026/11/11 / 10/30 / 11/06",
])
def test_ordered_rate_values_bind_to_their_respective_dates(ledger, dates):
    assert ledger._validate_analysis_claims(rate_table(dates=dates)) == []


@pytest.mark.parametrize("values,dates", [
    ("4.998%；前两周4.929% / 4.859%", "2026-11-11 / 11-06 / 10-30"),
    ("4.998%；前两周4.859% / 4.929%", "2026-11-11 / 10-30 / 11-06"),
    ("4.998%；前两周4.929% / 4.859%", "2025-11-11 / 10-30 / 11-06"),
    ("4.998%；前两周4.929% / 4.859%", "2026-11-11 / 10-30"),
    ("4.998%；前两周4.929% / 4.859%", "2026-01-11 / 12-30 / 12-06"),
    ("4.998%；前两周4.929% / 4.859%", "11-11 / 10-30 / 11-06"),
])
def test_date_lists_cannot_borrow_the_first_or_any_matching_date(ledger, values, dates):
    issues = ledger._validate_analysis_claims(rate_table(values, dates))
    assert issues
    assert all(item["kind"] == "yield" for item in issues)
    assert "one value/full-date pair" in issues[0]["message"]


@pytest.mark.parametrize("claim", [
    "需要看到10Y收益率从4.998%明显回落。",
    "下方情景需要收益率有效突破5%并伴随美元续强。",
    "## 未来三个月的情景\n长端收益率在4.8%–5.0%区间反复。",
])
def test_report_unique_treasury_identity_supplies_only_the_missing_subject(ledger, claim):
    content = "美债10Y名义收益率 ^TNX 当前4.998%。\n" + claim
    assert ledger._validate_analysis_claims(content) == []


@pytest.mark.parametrize("claim", [
    "需要看到10Y收益率从8.5%明显回落。",
    "日本10Y收益率为4.998%。",
    "5Y收益率为4.998%。",
    "长端实际收益率为4.998%。",
    "策略收益率为4.998%。",
    "下方情景需要策略收益率突破5%。",
    "## 未来情景\n若长端收益率突破5%。\n## 已观测事实\n长端收益率在4.8%–5.0%区间反复。",
])
def test_inherited_yield_subject_cannot_override_local_type_or_observation(ledger, claim):
    content = "美债10Y名义收益率 ^TNX 当前4.998%。\n" + claim
    assert ledger._validate_analysis_claims(content)


def test_multiple_report_treasuries_leave_bare_issuer_unresolved(ledger):
    content = "本文观察^TNX与^FVX。\n10Y收益率为4.998%。"
    assert ledger._validate_analysis_claims(content)
