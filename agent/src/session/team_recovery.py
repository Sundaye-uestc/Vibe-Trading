"""Revalidate a recorded team interpretation offline, within its original evidence scope."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.agent.grounding import GroundingLedger
from src.agent.recovery_evidence import MAX_RESULT_BYTES, MAX_TRACE_BYTES, _result_text, _safe_file
from src.session.models import Attempt, AttemptStatus, Message


class TeamRecoveryError(ValueError):
    """This attempt cannot safely provide a recovered reply."""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _checked_path(root: Path, relative: str, limit: int) -> Path:
    path = _safe_file(root, relative)
    if path is None or path.stat().st_size > limit:
        raise TeamRecoveryError("Missing or invalid recovery evidence.")
    return path


def _draft(root: Path, event: dict) -> str:
    text = event.get("content")
    if not isinstance(text, str):
        rel = event.get("content_path", "")
        if not re.fullmatch(r"trace-blobs/[0-9a-f]{24}\.txt", rel):
            raise TeamRecoveryError("Invalid draft reference.")
        path = _checked_path(root, rel, MAX_RESULT_BYTES)
        text = path.read_text(encoding="utf-8")
        key = f"answer-rejected-{event['iter']}\0{text}"
        if path.stem != _hash(key)[:24] or event.get("content_size") != len(text):
            raise TeamRecoveryError("Draft sidecar differs from its recorded size or digest.")
    if not text or len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        raise TeamRecoveryError("Invalid draft size.")
    return text


def recover_recorded_interpretation(sessions_dir: Path, runs: Path, attempt: Attempt, fallback: Message) -> Message:
    """Return a new message. The caller alone can append it to the transcript.

    The old ledger binds the exact draft, report and displayed facts. Current
    validation replays only server-recorded observations. No file read or
    model output is treated as market evidence, and no tool is executed.
    """
    sid, aid = attempt.session_id, attempt.attempt_id
    run_id = fallback.metadata.get("run_id")
    if (not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]+", v) for v in (sid, aid, run_id))
            or attempt.status != AttemptStatus.COMPLETED or fallback.session_id != sid
            or fallback.role != "assistant" or fallback.linked_attempt_id != aid
            or fallback.metadata.get("degraded") is not True
            or not attempt.run_dir or Path(attempt.run_dir) != runs / run_id
            or not attempt.started_at or not attempt.completed_at):
        raise TeamRecoveryError("Only a completed, degraded team reply can be recovered.")
    root = sessions_dir / sid
    if root.is_symlink():
        raise TeamRecoveryError("Invalid source session.")
    request = json.loads(_checked_path(runs, f"{run_id}/req.json", MAX_RESULT_BYTES).read_text())
    if request.get("prompt") != attempt.prompt or request.get("context", {}).get("session_id") != sid:
        raise TeamRecoveryError("The source run does not match this session.")
    ledger_file = _checked_path(runs, f"{run_id}/artifacts/grounding_evidence.json", MAX_TRACE_BYTES)
    audit_text = ledger_file.read_text()
    recorded = json.loads(audit_text)
    receipt = recorded.get("team_report_receipt")
    validations = recorded.get("validations", [])
    if not receipt or not validations or recorded.get("historical_observations"):
        raise TeamRecoveryError("No independently replayable team audit was found.")
    candidates = {v["model_content_sha256"]: v for v in validations
                  if v.get("mode") == "verified_facts_and_interpretation" and v.get("valid") is False
                  and v.get("issues") and all(i.get("code") == "qualitative_revision_required" for i in v["issues"])
                  and v.get("model_content_sha256") == v.get("content_sha256") and v.get("facts_sha256")}
    if not candidates:
        raise TeamRecoveryError("No recorded interpretation meets the recovery criteria.")
    # The durable assistant trail links each original tool result to this
    # attempt. Session traces can include earlier attempts with reused IDs.
    trail = {(t.get("call_id"), t.get("tool"), t.get("status")) for t in fallback.tool_trail}
    trace = _checked_path(root, "trace.jsonl", MAX_TRACE_BYTES)
    begin = attempt.started_at
    end = datetime.fromisoformat(attempt.completed_at.replace("Z", "+00:00")).timestamp()
    calls = {}
    seen = set()
    restored = None
    validated_hashes = []
    with tempfile.TemporaryDirectory(prefix="vibe-team-recovery-") as isolated:
        ledger = GroundingLedger(run_dir=Path(isolated), user_message=attempt.prompt)
        ledger._request_time = datetime.fromtimestamp(begin, timezone.utc)
        for line in trace.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if not begin <= event.get("ts", 0) <= end:
                continue
            kind, tool, call_id = event.get("type"), event.get("tool"), event.get("call_id")
            if tool in {"search_symbol", "get_market_data", "run_swarm"}:
                if kind == "tool_call":
                    if call_id in calls:
                        raise TeamRecoveryError("Ambiguous call ID in the source attempt.")
                    calls[call_id] = (tool, event["args"])
                if kind == "tool_result":
                    if ((call_id, tool, event.get("status")) not in trail or call_id in seen
                            or calls.get(call_id, (None,))[0] != tool):
                        raise TeamRecoveryError("Tool record is not bound to the original reply.")
                    seen.add(call_id)
                    raw = _result_text(root, event)
                    if raw is None:
                        raise TeamRecoveryError("The stored tool result is incomplete.")
                    ledger.ingest_tool_result(tool_name=tool, arguments=calls[call_id][1], result=raw,
                                              call_id=call_id, success=event["status"] == "ok")
            if kind != "answer_rejected":
                continue
            body = _draft(root, event)
            digest = _hash(body)
            number = len(validated_hashes)
            if number >= len(validations) or validations[number].get("content_sha256") != digest:
                raise TeamRecoveryError("Rejected draft differs from its recorded validation.")
            validated_hashes.append(digest)
            original = candidates.get(digest)
            if not original:
                ledger.validate_final_answer(body)
                continue
            if (ledger._completed_team_report != receipt
                    or ledger.authorized_symbols != set(recorded["identity"]["authorized_symbols"])
                    or _hash(ledger.verified_facts_for_revision()) != original["facts_sha256"]):
                raise TeamRecoveryError("The report, identity or verified observations differ from the original audit.")
            checked = ledger.validate_final_answer(body, interpretation_only=True)
            if restored is None and checked.valid and checked.rendered_answer:
                restored = (checked.rendered_answer, digest, event["iter"])
        if not restored or len(validated_hashes) != len(validations):
            raise TeamRecoveryError("The available drafts could not be released under current validation.")

    answer, digest, iteration = restored
    prefix = "已从原评审恢复" if re.search(r"[\u3400-\u9fff]", attempt.prompt) else "Recovered from the original team review"
    return Message(session_id=sid, role="assistant", message_id=f"recovered_{aid}",
                   content=f"**{prefix}**\n\n{answer}",
                   metadata={"recovered_from_attempt": aid, "source_message_id": fallback.message_id,
                             "source_draft_sha256": digest, "source_iteration": iteration,
                             "rendered_answer_sha256": _hash(answer),
                             "team_report_receipt": receipt, "source_ledger_sha256": _hash(audit_text)})
