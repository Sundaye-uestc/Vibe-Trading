"""Legacy committee replies can be appended exactly once after offline revalidation."""

import asyncio
import hashlib
import json
from unittest.mock import Mock

import pytest

from src.agent.grounding import GroundingLedger
from src.agent.trace import TraceWriter
from src.session.events import EventBus
from src.session.models import Attempt, Message, Session
from src.session.service import SessionBusyError, SessionService
from src.session.store import SessionStore
from src.session.team_recovery import TeamRecoveryError


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    sid, aid, rid = "example", "original", "run_original"
    prompt = "评估 600522.SH 做多还是做空"
    store = SessionStore(tmp_path / "sessions")
    index = Mock()
    index.indexed_session_ids.return_value = set()
    monkeypatch.setattr("src.session.service.get_shared_index", lambda: index)
    svc = SessionService(store=store, event_bus=EventBus(), runs_dir=tmp_path / "runs")
    session = Session(session_id=sid, last_attempt_id=aid)
    store.create_session(session)
    run = svc.runs_dir / rid
    run.mkdir(parents=True)
    (run / "req.json").write_text(json.dumps({"prompt": prompt, "context": {"session_id": sid}}))
    attempt = Attempt(session_id=sid, attempt_id=aid, run_dir=str(run), prompt=prompt)
    attempt.mark_running()
    ledger = GroundingLedger(run_dir=run, user_message=prompt)
    team = {"status": "completed", "run_id": "swarm_original", "error": None,
            "tasks": [{"status": "completed", "error": None}], "final_report": "投委会倾向有条件做多。"}
    quotes = {"600522.SH": [{"trade_date": "2026-11-24", "close": 21.86}],
              "_provenance": {"600522.SH": {"source": "tencent"}}}
    trail = []
    writer = TraceWriter(store.base_dir / sid)
    for i, (tool, inputs, payload) in enumerate([
        ("run_swarm", {"prompt": prompt}, team),
        ("get_market_data", {"codes": ["600522.SH"], "interval": "1D"}, quotes),
    ], 1):
        cid = f"call_{i}"
        raw = json.dumps(payload, ensure_ascii=False)
        writer.write({"type": "tool_call", "iter": i, "tool": tool, "call_id": cid, "args": inputs})
        writer.write_tool_result(cid, raw, tool, "ok", 0, i)
        trail.append({"tool": tool, "call_id": cid, "status": "ok"})
        ledger.ingest_tool_result(tool_name=tool, arguments=inputs, result=raw, call_id=cid, success=True)
    invalid = "在本研究中测得年化波动率 88.8%。"
    q = ledger.validate_final_answer(invalid)
    assert not q.valid
    writer.write({"type": "answer_rejected", "iter": 3, "content": invalid, "issues": q.issues})
    facts = ledger.verified_facts_for_revision()
    path = run / "artifacts/grounding_evidence.json"
    audit = json.loads(path.read_text())
    drafts = ["委员会倾向有条件做多。做多的前提是盈利兑现，而现金流质量仍需核实。",
              "另一版委员会总结。多头和空头在盈利质量的判断上存在分歧，风险委员关注未来的订单兑现。"]
    for i, draft in enumerate(drafts, 4):
        issues = [{"code": "qualitative_revision_required"}]
        writer.write({"type": "answer_rejected", "iter": i, "content": draft, "issues": issues})
        audit["validations"].append({"mode": "verified_facts_and_interpretation", "valid": False,
            "model_content_sha256": sha(draft), "content_sha256": sha(draft),
            "facts_sha256": sha(facts), "issues": issues})
    path.write_text(json.dumps(audit))
    writer.close()
    attempt.mark_completed()
    store.create_attempt(attempt)
    original = Message(session_id=sid, role="assistant", content="部分完成", linked_attempt_id=aid,
                       metadata={"degraded": True, "run_id": rid}, tool_trail=trail)
    store.append_message(original)
    return svc, sid, aid, drafts, path


def test_recovers_earliest_passing_draft_once_with_source_and_attempt_intact(recorded):
    svc, sid, aid, drafts, ledger = recorded
    root = svc.store.base_dir / sid
    attempt = root / "attempts" / aid / "attempt.json"
    saved = (attempt.read_bytes(), ledger.read_bytes(), (root / "messages.jsonl").read_bytes())
    result = asyncio.run(svc.recover_team_reply(sid, aid))
    assert result.metadata["source_iteration"] == 4
    assert result.metadata["source_draft_sha256"] == sha(drafts[0])
    assert "21.86" in result.content and result.content.endswith(drafts[0])
    assert asyncio.run(svc.recover_team_reply(sid, aid)).message_id == result.message_id
    assert len(svc.get_messages(sid)) == 2
    assert (attempt.read_bytes(), ledger.read_bytes()) == saved[:2]
    assert (root / "messages.jsonl").read_bytes().startswith(saved[2])


@pytest.mark.parametrize("damage", ["facts", "receipt", "blob", "advanced", "busy", "wrong_session"])
def test_source_drift_and_concurrent_turn_never_append(recorded, damage):
    svc, sid, aid, _, path = recorded
    root = svc.store.base_dir / sid
    if damage in {"facts", "receipt"}:
        data = json.loads(path.read_text())
        if damage == "facts":
            data["validations"][-1]["facts_sha256"] = "0" * 64
        else:
            data["team_report_receipt"]["call_id"] = "wrong"
        path.write_text(json.dumps(data))
    elif damage == "blob":
        data = [json.loads(s) for s in (root / "trace.jsonl").read_text().splitlines()]
        data[-1]["content"] += " 内容被替换"
        (root / "trace.jsonl").write_text("\n".join(map(json.dumps, data)))
    elif damage == "advanced":
        svc.store.append_message(Message(session_id=sid, role="user", content="新的问题"))
    elif damage == "wrong_session":
        (path.parent.parent / "req.json").write_text(json.dumps({"prompt": "其他", "context": {"session_id": "other"}}))
    else:
        svc._reserve_session(sid)
    original = (root / "messages.jsonl").read_bytes()
    with pytest.raises((TeamRecoveryError, SessionBusyError)):
        asyncio.run(svc.recover_team_reply(sid, aid))
    assert (root / "messages.jsonl").read_bytes() == original


@pytest.mark.parametrize("bad", ["wrong_hash", "wrong_size", "escape", "symlink"])
def test_offloaded_draft_requires_original_content_address_and_size(recorded, bad):
    svc, sid, aid, _, _ = recorded
    root = svc.store.base_dir / sid
    path = root / "trace.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    row = rows[-1]
    body = row.pop("content")
    expected = sha(f"answer-rejected-{row['iter']}\0{body}")[:24]
    blob = root / "trace-blobs" / ((("a" * 24) if bad == "wrong_hash" else expected) + ".txt")
    blob.parent.mkdir(exist_ok=True)
    if bad == "symlink":
        outside = root.parent / "outside"
        outside.write_text(body)
        blob.symlink_to(outside)
    else:
        blob.write_text(body)
    row["content_path"] = str(blob.relative_to(root)) if bad != "escape" else "../outside"
    row["content_size"] = len(body) + (bad == "wrong_size")
    path.write_text("\n".join(map(json.dumps, rows)))
    with pytest.raises(TeamRecoveryError):
        asyncio.run(svc.recover_team_reply(sid, aid))
    assert len(svc.get_messages(sid)) == 1
