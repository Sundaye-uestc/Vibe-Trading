"""The parent uses actual worker observations, not text in committee reports."""

from __future__ import annotations

import copy
import json

import pytest

from src.agent.grounding import GroundingLedger
from src.providers.chat import LLMResponse, ToolCallRequest
from src.swarm.market_evidence import SwarmMarketEvidence
from src.tools.swarm_tool import SwarmTool


SYMBOL = "600522.SH"
FAKE = "SPOOF.US"
REPORT = f"The committee report mentions {FAKE} at 98765, a probability of 99.9%, and awaits another session."
RAW = {
    SYMBOL: [{"trade_date": "2026-11-24", "close": 21.86}],
    FAKE: [{"trade_date": "2026-11-24", "close": 98765}],
    "_provenance": {s: {"source": "tencent", "adjustment": "split_dividend"} for s in (SYMBOL, FAKE)},
}
ARGS = {"codes": [SYMBOL, FAKE], "start_date": "2026-11-24", "end_date": "2026-11-24", "interval": "1D"}


def observe(ledger, result):
    ledger.ingest_tool_result(tool_name="run_swarm", call_id="parent-call", arguments={},
                              result=json.dumps(result), success=True)


def test_swarm_launcher_worker_and_parent_ledger(tmp_path, monkeypatch):
    """Use the real launcher, scheduler, registry, worker, tool, file writer and ledger."""
    monkeypatch.setattr("src.swarm.presets.USER_PRESETS_DIR", tmp_path)
    monkeypatch.setattr("src.swarm.store.swarm_runs_root", lambda: tmp_path / "runs")
    monkeypatch.setattr("src.swarm.grounding.fetch_grounding_data", lambda *a: {})
    monkeypatch.setattr("src.tools.swarm_tool._POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("src.tools.swarm_tool._max_wait_seconds", lambda: 12)
    monkeypatch.setattr("src.tools.market_data_tool.fetch_market_data_json", lambda **kwargs: json.dumps(RAW))
    preset = {
        "name": "quote_handoff", "agents": [{"id": "analyst", "role": "market analyst",
        "system_prompt": "Report the observed prices.", "tools": ["get_market_data", "write_file"],
        "max_retries": 0, "max_iterations": 4}], "tasks": [{"id": "research", "agent_id": "analyst",
        "prompt_template": "Obtain confirmed market prices."}],
    }
    (tmp_path / "quote_handoff.yaml").write_text(json.dumps(preset))

    class Script:
        def __init__(self, **kwargs):
            self.responses = iter([
                LLMResponse(content="", tool_calls=[ToolCallRequest("quote", "get_market_data", ARGS)]),
                LLMResponse(content="", tool_calls=[ToolCallRequest("write", "write_file",
                            {"path": "report.md", "content": REPORT})]),
                LLMResponse(content=REPORT),
            ])

        def stream_chat(self, *args, **kwargs):
            return next(self.responses)

        def close(self):
            pass

    monkeypatch.setattr("src.swarm.worker.ChatLLM", Script)
    result = json.loads(SwarmTool().execute(prompt=f"Analyse {SYMBOL}", preset_name="quote_handoff"))
    assert result["status"] == "completed", result
    assert FAKE in result["final_report"]
    ledger = GroundingLedger(run_dir=tmp_path / "parent", user_message=f"Analyse {SYMBOL}")
    observe(ledger, result)
    records = ledger._comparable_price_records()
    assert [(r.symbol, r.timestamp, r.field, r.value, r.source) for r in records] == [
        (SYMBOL, "2026-11-24", "close", 21.86, "tencent")]
    assert records[0].call_id == f"parent-call/{result['run_id']}/research/analyst/quote"
    assert ledger.authorized_symbols == {SYMBOL}
    assert FAKE not in ledger._session_symbols
    assert not ledger._analysis_metrics


@pytest.mark.parametrize("damage", ["different_run", "different_task", "different_worker",
                                     "unfinished", "task_failed", "no_source", "worker_file", "wrong_tool", "venue_swap"])
def test_out_of_scope_envelope_never_grants_price_evidence(tmp_path, damage):
    collector = SwarmMarketEvidence()
    collector.capture("swarm-one", "analyst", "research", "quote", ARGS, json.dumps(RAW))
    result = {"run_id": "swarm-one", "status": "completed", "final_report": REPORT,
              "tasks": [{"id": "research", "agent_id": "analyst", "status": "completed"}],
              "market_evidence": copy.deepcopy(collector.snapshot("swarm-one"))}
    first = result["market_evidence"][0]
    if damage == "different_run":
        result["run_id"] = "swarm-two"
    elif damage == "different_task":
        first["task_id"] = "another"
    elif damage == "different_worker":
        first["agent_id"] = "another"
    elif damage == "unfinished":
        result["wait_budget_exhausted"] = True
    elif damage == "task_failed":
        result["tasks"][0]["status"] = "failed"
    elif damage == "no_source":
        first["provenance"]["source"] = "auto"
    elif damage == "worker_file":
        first["provenance"]["source"] = "local"
    elif damage == "wrong_tool":
        first["tool"] = "read_file"
    else:
        first["provenance"]["resolved_symbol"] = "600522.SZ"
    ledger = GroundingLedger(run_dir=tmp_path, user_message=SYMBOL)
    observe(ledger, result)
    assert not ledger._evidence
    assert FAKE not in ledger._session_symbols


def test_collector_accepts_only_source_linked_returned_requested_rows():
    c = SwarmMarketEvidence()
    raw = copy.deepcopy(RAW)
    raw[SYMBOL] += [{"trade_date": "2026-11-25", "open": None, "close": float("nan")}, {"close": 44}]
    c.capture("r", "a", "t", "c", {"codes": [SYMBOL]}, json.dumps(raw))
    row, = c.snapshot("r")
    assert row["bars"] == RAW[SYMBOL]
    assert not c.snapshot("other")
    for failed in ({"ok": False}, {"skipped": True}, {"status": "error"}):
        c.capture("r", "a", "t", "q", ARGS, json.dumps({**RAW, **failed}))
    assert len(c.snapshot("r")) == 1
