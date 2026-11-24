"""Exhaustion can retain a sourced ruling, never an unchecked assistant draft."""

import json

import pytest

from src.agent.grounding import GroundingLedger
from src.agent.loop import AgentLoop
from src.agent.memory import WorkspaceMemory
from src.agent.team_excerpt import team_decision_excerpt
from src.agent.tools import ToolRegistry
from src.providers.chat import LLMResponse
from tests.test_grounding_team_revision import BAD, BARS, QUESTION, REPORT, CommitteeLLM, ResultTool


STANCE = "**我裁定：只有盈利改善得到后续财报验证，委员会才会支持逐步参与；在此之前保持谨慎。**"
SOURCE = f"# 团队报告\n\n## 12. 结论段（决策陈述）\n\n{STANCE}\n\n**行情**：声称收盘价为99999元。"


def book(tmp_path, source, *, completed=True):
    ledger = GroundingLedger(run_dir=tmp_path, user_message=QUESTION)
    ledger.ingest_tool_result(tool_name="run_swarm", arguments={}, result=json.dumps({
        **REPORT, "final_report": source, "status": "completed" if completed else "failed",
    }), call_id="final-report", success=completed)
    ledger.ingest_tool_result(tool_name="get_market_data", arguments={"codes": ["600522.SH"]},
                              result=json.dumps(BARS), call_id="price", success=True)
    ledger.validate_final_answer("报告给出的年化波动率为88.8%。")
    return ledger


def test_exhaustion_quotes_exact_ruling_conditions_without_releasing_rejected_draft(tmp_path):
    ledger = book(tmp_path, SOURCE)
    rejected = ledger.validate_final_answer("团队实测年化波动率为88.8%。", interpretation_only=True)
    assert not rejected.valid
    fallback = ledger.safe_fallback(allow_team_excerpt=True)
    assert f"> {STANCE}" in fallback
    assert "完整解读未通过校验" in fallback and "查看团队原稿" in fallback
    assert "21.86" in fallback and "2026-11-24" in fallback
    assert "99999" not in fallback and "88.8" not in fallback
    assert not ledger._validations[-1]["valid"]
    assert STANCE not in ledger.safe_fallback()


def test_incomplete_team_cannot_supply_a_verdict(tmp_path):
    assert STANCE not in book(tmp_path, SOURCE, completed=False).safe_fallback(allow_team_excerpt=True)


def test_stubborn_writer_releases_only_partial_attributed_ruling_once(tmp_path):
    class StubbornWriter(CommitteeLLM):
        def stream_chat(self, messages, on_text_chunk=None, **kwargs):
            if len(self.requests) < 2:
                return super().stream_chat(messages, on_text_chunk, **kwargs)
            self.requests.append(messages.copy())
            if on_text_chunk:
                on_text_chunk(BAD)
            return LLMResponse(content=BAD)

    tools = ToolRegistry()
    tools.register(ResultTool("run_swarm", {**REPORT, "final_report": SOURCE}))
    tools.register(ResultTool("get_market_data", BARS))
    events = []
    agent = AgentLoop(tools, StubbornWriter(), memory=WorkspaceMemory(run_dir=str(tmp_path)),
                      max_iterations=7, event_callback=lambda event, data: events.append((event, data)))
    result = agent.run(QUESTION)
    assert result.get("degraded") is True
    assert f"> {STANCE}" in result["content"] and "88.8" not in result["content"]
    assert [data["delta"] for event, data in events if event == "text_delta"] == [result["content"]]


@pytest.mark.parametrize("decision", [
    "**我裁定：明天收盘价升至9999元即可买入。**",
    "**我裁定：本次上涨概率为百分之九十九，应当加仓。**",
    "**我裁定：对完全无关的 OTHER.US 作出有条件参与的判断。**",
    "**我裁定：`执行交易` 后资金已经转出。**",
])
def test_unsafe_or_unbound_source_choices_link_to_draft_instead(tmp_path, decision):
    result = book(tmp_path, SOURCE.replace(STANCE, decision)).safe_fallback(allow_team_excerpt=True)
    assert decision not in result and "查看团队原稿" in result and "21.86" in result


@pytest.mark.parametrize("source", [
    "## Bull case\n\n" + STANCE,
    "```markdown\n" + SOURCE + "\n```",
    SOURCE + "\n\n## Final decision\n\n**Decision: Withhold judgment.**",
    SOURCE + "\n\n## 更正\n\n委员会撤回上述判断。",
    f"## 最终裁决\n\n{STANCE}\n但须等待下一份财报。",
    f"## 最终裁决\n\n{STANCE}\n\n但须等待下一份财报。",
    "| Key | Final decision |\n|---|---|\n| Direction | Remain cautious |\n| Unless | Rates fall |",
    "| Key | Final decision |\n|---|---|\n| Direction | Remain cautious |\n\nBut only if funded.",
])
def test_never_quotes_examples_or_truncates_a_continuation(source):
    assert not team_decision_excerpt(source)


def test_terminal_dedicated_table_field_is_returned_verbatim():
    field = "Conditional participation only after liquidity improves."
    table = "| Key | Final decision |\n|---|---|\n| Direction | " + field + " |"
    assert team_decision_excerpt(table) == field
