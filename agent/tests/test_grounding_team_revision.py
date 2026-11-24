"""A completed research team is a structural handoff to the answer writer."""

import hashlib
import json

import pytest

from src.agent.grounding import GroundingLedger
from src.agent.loop import AgentLoop
from src.agent.memory import WorkspaceMemory
from src.agent.tools import BaseTool, ToolRegistry
from src.providers.chat import LLMResponse, ToolCallRequest


QUESTION = "[智能体团队模式] 使用 investment_committee 预设评估在当前市场环境下对 600522.SH 进行做多还是做空"
REPORT = {
    "run_id": "swarm-fixture", "status": "completed", "wait_budget_exhausted": False,
    "preset": "investment_committee", "error": None,
    "final_report": "团队倾向有条件做多，实测波动率为88.8%。",
    "tasks": [{"status": "completed", "error": None}],
}
BARS = {"600522.SH": [{"trade_date": "2026-11-24", "close": 21.86}],
        "_provenance": {"600522.SH": {"source": "tencent"}}}
BAD = "报告中给出的实测波动率为88.8%。"
COMMENT = "委员会倾向有条件做多。多方看重业务改善，风险委员关注兑现进度；若改善迟迟未能实现，这一倾向就应重新审视。"


class ResultTool(BaseTool):
    def __init__(self, name, result):
        self.name, self.result = name, result

    def execute(self, **kwargs):
        return json.dumps(self.result, ensure_ascii=False)


class CommitteeLLM:
    def __init__(self):
        self.requests = []

    def stream_chat(self, messages, on_text_chunk=None, **kwargs):
        self.requests.append(messages.copy())
        turn = len(self.requests)
        if turn == 1:
            return LLMResponse(tool_calls=[ToolCallRequest("report", "run_swarm", {"prompt": QUESTION})])
        if turn == 2:
            return LLMResponse(tool_calls=[ToolCallRequest("quote", "get_market_data", {"codes": ["600522.SH"]})])
        repair_requested = any("Return the team's assessment" in (message.get("content") or "")
                               for message in messages if message["role"] == "user")
        draft = COMMENT if repair_requested else BAD
        if on_text_chunk:
            on_text_chunk(draft)
        return LLMResponse(content=draft)


def ingest(book, *, payload=REPORT, success=True, prices=True):
    book.ingest_tool_result(tool_name="run_swarm", arguments={}, result=json.dumps(payload),
                            call_id="report", success=success)
    if prices:
        book.ingest_tool_result(tool_name="get_market_data", arguments={"codes": ["600522.SH"]},
                                result=json.dumps(BARS), call_id="quote", success=True)
    rejected = book.validate_final_answer(BAD)
    assert not rejected.valid


def test_team_long_short_question_survives_unsupported_numbers_through_normal_loop(tmp_path):
    agent_tools = ToolRegistry()
    agent_tools.register(ResultTool("run_swarm", REPORT))
    agent_tools.register(ResultTool("get_market_data", BARS))
    llm = CommitteeLLM()
    visible = []
    agent = AgentLoop(registry=agent_tools, llm=llm, memory=WorkspaceMemory(run_dir=str(tmp_path)),
                      event_callback=lambda event, data: visible.append((event, data)), max_iterations=6)
    result = agent.run(QUESTION)
    assert result["status"] == "success" and not result.get("degraded")
    assert result["content"].endswith(COMMENT)
    assert "21.86" in result["content"] and "88.8" not in result["content"]
    assert [data["delta"] for event, data in visible if event == "text_delta"] == [result["content"]]
    audit = json.loads((tmp_path / "artifacts/grounding_evidence.json").read_text())
    assert audit["team_report_receipt"]["report_sha256"] == hashlib.sha256(REPORT["final_report"].encode()).hexdigest()
    assert [v["valid"] for v in audit["validations"]] == [False, True]
    assert audit["validations"][-1]["mode"] == "verified_facts_and_interpretation"
    assert audit["validations"][-1]["content_sha256"] == hashlib.sha256(result["content"].encode()).hexdigest()


def test_report_handoff_works_without_recognizing_prompt_language_or_preset(tmp_path):
    book = GroundingLedger(run_dir=tmp_path, user_message="Évaluer la position sur 600522.SH.")
    ingest(book, payload={**REPORT, "preset": "new_user_defined_team"})
    assert "21.86" in book.verified_facts_for_revision()


@pytest.mark.parametrize("change,success", [
    ({"status": "running"}, True), ({"wait_budget_exhausted": True}, True),
    ({"final_report": "  "}, True), ({"error": "failed"}, True), ({"run_id": ""}, True),
    ({"tasks": [{"status": "failed"}]}, True), ({}, False),
])
def test_failed_or_undelivered_report_cannot_authorize_team_repair(tmp_path, change, success):
    book = GroundingLedger(run_dir=tmp_path, user_message=QUESTION)
    ingest(book, payload={**REPORT, **change}, success=success)
    book.validate_final_answer(BAD)
    assert not book.verified_facts_for_revision()


def test_no_price_still_allows_recovery_before_attributing_an_opinion(tmp_path):
    book = GroundingLedger(run_dir=tmp_path, user_message=QUESTION)
    ingest(book, prices=False)
    assert not book.verified_facts_for_revision()
    book.validate_final_answer(BAD)
    assert book.verified_facts_for_revision() == "研究团队已提交最终报告。"
    assert "88.8" not in book.verified_facts_for_revision()


@pytest.mark.parametrize("additional", ["并下单", "并保存为文件", " then submit a buy order"])
def test_team_completion_does_not_replace_requested_action(tmp_path, additional):
    book = GroundingLedger(run_dir=tmp_path, user_message=QUESTION + additional)
    ingest(book)
    assert not book.verified_facts_for_revision()


@pytest.mark.parametrize("text", [
    "委员会认为毛利率每回落一个百分点都会压缩利润，风险席位将盈利兑现列为后续观察条件。",
    "委员会认为毛利率每变动１个百分点会影响净利润，多方的论证取决于盈利韧性。",
    "The committee described the sensitivity of earnings to each one percentage point decline in margin.",
])
def test_sensitivity_denominator_does_not_claim_an_observed_change(tmp_path, text):
    book = GroundingLedger(run_dir=tmp_path, user_message=QUESTION)
    ingest(book)
    checked = book.validate_final_answer(text, interpretation_only=True)
    assert checked.valid, checked.issues
    assert checked.rendered_answer.endswith(text)
    assert "21.86" in checked.rendered_answer


@pytest.mark.parametrize("text, offending", [
    ("委员会称本季度毛利率实际回落了一个百分点，这项变化支持了空方意见。", "一个百分点"),
    ("毛利率每下降一个百分点，净利润就下降两位数百分比，委员会据此提高了风险权重。", "两位数百分比"),
    ("毛利率每下降一个百分点，净利润下降88.8%，委员会据此提高了风险权重。", "88.8%"),
    ("委员会称公司的毛利率每年下降一个百分点，因此更倾向等待确认。", "一个百分点"),
    ("Each one percentage point decline in margin produces double-digit returns in the observed data.", "double-digit returns"),
])
def test_quantified_outcomes_survive_unit_mask_and_get_specific_feedback(tmp_path, text, offending):
    book = GroundingLedger(run_dir=tmp_path, user_message=QUESTION)
    ingest(book)
    checked = book.validate_final_answer(f"600522.SH：{text}", interpretation_only=True)
    assert not checked.valid and checked.rendered_answer is None
    issue = next(i for i in checked.issues if i["code"] == "qualitative_revision_required")
    assert issue["detail"] == "quantitative_phrase" and issue["value"] == offending
    assert offending in issue["claim"]
    assert offending in book.correction_prompt(checked, interpretation_only=True)


def test_repair_feedback_identifies_both_a_measurement_and_its_quantified_effect(tmp_path):
    book = GroundingLedger(run_dir=tmp_path, user_message=QUESTION)
    ingest(book)
    draft = "毛利率回落一个百分点，对净利润的冲击接近双位数百分比，委员会将此视为主要风险。"
    result = book.validate_final_answer(draft, interpretation_only=True)
    issue = next(i for i in result.issues if i.get("detail") == "quantitative_phrase")
    assert issue["rejected_phrases"] == ["一个百分点", "双位数百分比"]
    prompt = book.correction_prompt(result, interpretation_only=True)
    assert '["一个百分点", "双位数百分比"]' in prompt
