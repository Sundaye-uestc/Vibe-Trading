"""Repeated numeric failures should still permit a useful, checked answer."""

import hashlib
import json

import pytest

from src.agent.grounding import GroundingLedger
from src.agent.loop import AgentLoop
from src.agent.memory import WorkspaceMemory
from src.agent.recovery_evidence import PriorToolObservation
from src.agent.tools import ToolRegistry
from src.agent.trace import TraceWriter
from src.providers.chat import LLMResponse


BAD = "GLD.US 的实测年化波动率为88.8%。"
COMMENT = "价格已有所回升。若能守住此前的交易区间，修复情景会得到支持；若再次回落，则仍需观察。"
GOOD = (
    "未来三个月先观察能否企稳，再判断反弹能否延续。\n\n"
    "GLD.US（yahoo，USD）2026-11-10 收盘价369.36。"
    "这里使用黄金ETF作为代理，其每股价格不能当作现货黄金报价。\n\n"
    "如果价格持续回升并站稳此前交易区间，修复情景得到支持；"
    "如果反弹无法维持并继续走低，则偏弱情景得到支持。"
    "需要后续行情确认，现有数据不足以给出各情景的统计概率。"
)


class RevisionLLM:
    """Replay two bad drafts; respond to the actual bounded rewrite turn."""

    def __init__(self, *, cooperate=True, revised=GOOD):
        self.cooperate = cooperate
        self.revised = revised
        self.requests = []

    def stream_chat(self, messages, on_text_chunk=None, **kwargs):
        self.requests.append(messages.copy())
        rewrite = "[REBUILD THE ANSWER]" in messages[-1]["content"]
        content = self.revised if rewrite and self.cooperate else BAD
        if on_text_chunk:
            on_text_chunk(content)
        return LLMResponse(content=content)

    def chat(self, messages, **kwargs):
        return LLMResponse(content="")


def run_revision(tmp_path, *, cooperate=True, max_iterations=6, revised=GOOD):
    observation = PriorToolObservation(
        tool="get_market_data", arguments={"codes": ["GLD.US"]},
        result=json.dumps({
            "GLD.US": [{"trade_date": "2026-11-10", "close": 369.36}],
            "_provenance": {"GLD.US": {"source": "yahoo"}},
        }), call_id="recorded-price", success=True,
        observed_at="2026-11-11T15:53:56Z", attempt_id="recorded-attempt",
    )
    events = []
    llm = RevisionLLM(cooperate=cooperate, revised=revised)
    agent = AgentLoop(
        registry=ToolRegistry(), llm=llm,
        memory=WorkspaceMemory(run_dir=str(tmp_path)),
        max_iterations=max_iterations,
        event_callback=lambda kind, data: events.append((kind, data)),
    )
    result = agent.run("分析GLD.US未来三个月走势", prior_observations=(observation,))
    return result, llm, events


def test_repeated_metric_failure_rebuilds_and_releases_only_checked_answer(tmp_path):
    result, llm, events = run_revision(tmp_path)
    assert result["status"] == "success"
    assert not result.get("degraded")
    assert result["content"] == GOOD
    assert len(llm.requests) == 3
    assert "[REBUILD THE ANSWER]" not in llm.requests[1][-1]["content"]
    assert "[REBUILD THE ANSWER]" in llm.requests[2][-1]["content"]
    # The rewrite starts from user intent and tool facts, not the rejected
    # assistant drafts. They are retained only in the audit trace.
    assert any(message["role"] == "assistant" and message["content"] == BAD
               for message in llm.requests[1])
    assert not any(message["role"] == "assistant" and message["content"] == BAD
                   for message in llm.requests[2])
    assert any("369.36" in message["content"] and "tool observations" in message["content"]
               for message in llm.requests[2])
    visible = "".join(data["delta"] for kind, data in events if kind == "text_delta")
    assert "88.8" not in visible
    assert GOOD in visible
    ledger = json.loads((tmp_path / "artifacts/grounding_evidence.json").read_text())
    assert [item["valid"] for item in ledger["validations"]] == [False, False, True]


@pytest.mark.parametrize("max_iterations", [1, 3, 6])
def test_uncooperative_rewrite_still_respects_budget_and_rejects_bad_numbers(tmp_path, max_iterations):
    result, llm, events = run_revision(tmp_path, cooperate=False, max_iterations=max_iterations)
    assert result.get("degraded") is True
    assert len(llm.requests) <= max_iterations
    assert "88.8" not in result["content"]
    assert "369.36" in result["content"]
    assert "88.8" not in "".join(data["delta"] for kind, data in events if kind == "text_delta")
    assert TraceWriter.read(tmp_path)[-1].get("degraded") is True


def test_rewrite_assembles_checked_interpretation_and_server_observations(tmp_path):
    result, llm, events = run_revision(tmp_path, revised=COMMENT)
    assert not result.get("degraded")
    answer = result["content"]
    assert answer.endswith(COMMENT)
    assert "369.36" in answer and "2026-11-10" in answer and "yahoo" in answer
    assert [data["delta"] for kind, data in events if kind == "text_delta"] == [answer]
    log = json.loads((tmp_path / "artifacts/grounding_evidence.json").read_text())
    audit = log["validations"][-1]
    assert audit["mode"] == "verified_facts_and_interpretation"
    assert audit["content_sha256"] == hashlib.sha256(answer.encode()).hexdigest()
    assert audit["model_content_sha256"] == hashlib.sha256(COMMENT.encode()).hexdigest()
    assert len(llm.requests) == 3


@pytest.mark.parametrize("revised", [
    "已核验：GLD.US 昨天收益率 99%，上涨幅度相当可观。",
    "GLD.US 昨天上涨百分之九十九，涨幅已经足以确认上涨趋势。",
    "抱歉，我无法提供任何分析，因为目前实在没有足以使用的数据。",
    "| 已核实 | 价格 |\n|---|---:|\n| GLD.US | 99999 |",
])
def test_revision_does_not_treat_llm_numbers_or_a_refusal_as_verified_analysis(tmp_path, revised):
    result, _, _ = run_revision(tmp_path, revised=revised)
    assert result.get("degraded") is True
    assert revised not in result["content"]


@pytest.mark.parametrize("question", [
    "分析 GLD.US 并下单", "请把 GLD.US 昨天涨幅写入文件", "帮我回测 GLD.US 趋势策略",
    "Schedule an email about GLD.US performance", "Generate a report for yesterday's AAPL.US rally",
])
def test_verified_facts_do_not_satisfy_execution_or_artifact_tasks(tmp_path, question):
    ledger = GroundingLedger(run_dir=tmp_path, user_message=question)
    ledger.ingest_tool_result(tool_name="get_market_data", call_id="daily", success=True,
                              arguments={"codes": ["GLD.US"]},
                              result=json.dumps({"GLD.US": [{"trade_date": "2026-11-10", "close": 369.36}],
                                                 "_provenance": {"GLD.US": {"source": "yahoo"}}}))
    ledger.validate_final_answer(BAD)
    ledger.validate_final_answer(BAD)
    assert not ledger.verified_facts_for_revision()
