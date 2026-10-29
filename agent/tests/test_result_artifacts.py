"""Large observations remain fully readable through the existing tool path."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

import pytest

from src.agent import result_artifacts as artifacts
from src.agent.tools import BaseTool, ToolRegistry
from src.config.limits import TOOL_RESULT_LIMIT
from src.tools.bash_tool import BashTool
from src.tools.read_file_tool import ReadFileTool
from src.tools.redaction import redact_tool_result


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    root = tmp_path / '科研 "报告"'
    root.mkdir()
    return root


def registry_with_reader():
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    return registry


def large_result():
    return json.dumps({
        "status": "ok", "_provenance": {"source": "fixture", "currency": "CNY"},
        "TUSHARE_TOKEN": "fixture-private-value",
        "coverage": {"returned_rows": 800, "total_rows": 1200, "truncated": True},
        "token_usage": {"total_input_tokens": 100, "token_source": "provider"},
        "data": [{"code": "000001", "index": i, "close": i + 0.25,
                  "note": 'quoted "value"\n中文资料'} for i in range(800)],
    }, ensure_ascii=False)


def assert_fallback(delivery):
    assert "TRUNCATED:" in delivery
    assert '"_result_file"' not in delivery
    assert "fixture-private-value" not in delivery
    assert len(delivery) <= TOOL_RESULT_LIMIT


@pytest.mark.parametrize("content", ["short plain result", '{"price": 12.5}', '{"api_key": "fixture-private-value"}'])
def test_small_results_keep_the_redacted_contract_without_writing(workspace, content):
    result = artifacts.deliver_tool_result(content, workspace, registry_with_reader())
    assert result == redact_tool_result(content)
    assert not list(workspace.iterdir())


@pytest.mark.parametrize("line_ending", [None, "\n", "\r\n", "\r"])
def test_original_and_parts_survive_actual_read_file_and_delivery(workspace, line_ending):
    registry = registry_with_reader()
    raw = (f"财务观察{line_ending}price=12.5 CNY{line_ending}" * 1500) if line_ending else large_result()
    safe = redact_tool_result(raw)
    receipt_text = artifacts.deliver_tool_result(raw, workspace, registry)
    assert len(receipt_text.encode("utf-8")) <= TOOL_RESULT_LIMIT
    receipt = json.loads(receipt_text)
    info = receipt["_result_file"]
    original = Path(info["path"])
    assert original.read_bytes().decode("utf-8") == safe
    assert info["format"] == ("text" if line_ending else "json")
    assert info["sha256"] == hashlib.sha256(safe.encode()).hexdigest()
    assert info["size_bytes"] == len(safe.encode())
    assert "Source row limits/sampling still apply" in receipt["_delivery"]
    if not line_ending:
        assert receipt["_provenance"]["source"] == "fixture"
        assert json.loads(safe)["coverage"]["truncated"] is True

    parts = []
    for index in range(1, info["part_count"] + 1):
        raw_read = registry.execute("read_file", {
            "path": str(Path(info["parts_dir"]) / f"{index}.txt"), "run_dir": str(workspace),
        })
        # Exercise the second redaction/delivery pass used by the runtime, not
        # just direct file reads that would miss recursive offloading.
        delivered_read = artifacts.deliver_tool_result(raw_read, workspace, registry)
        assert len(delivered_read.encode("utf-8")) <= TOOL_RESULT_LIMIT
        decoded = json.loads(delivered_read)
        assert decoded["status"] == "ok" and "_result_file" not in decoded
        parts.append(decoded["content"])
    assert "".join(parts) == safe
    assert "fixture-private-value" not in original.read_text() + receipt_text
    assert len(list((workspace / "tool-results").iterdir())) == 1


@pytest.mark.parametrize("line_ending", ["\n", "\r\n", "\r"])
def test_read_file_line_limit_preserves_original_terminators(workspace, line_ending):
    path = workspace / "lines.txt"
    path.write_bytes(line_ending.join(["first", "second", "third"]).encode())
    read = json.loads(ReadFileTool().execute(path=str(path), run_dir=str(workspace), limit=2))
    assert read["content"] == f"first{line_ending}second{line_ending}"


def test_repeated_and_concurrent_results_reuse_immutable_files(workspace):
    registry = registry_with_reader()
    raw = large_result()
    first = artifacts.deliver_tool_result(raw, workspace, registry)
    path = Path(json.loads(first)["_result_file"]["path"])
    before = path.stat().st_mtime_ns
    with ThreadPoolExecutor(max_workers=4) as executor:
        outputs = list(executor.map(lambda _: artifacts.deliver_tool_result(raw, workspace, registry), range(8)))
    assert outputs == [first] * 8
    assert path.stat().st_mtime_ns == before
    assert not list(workspace.rglob(".result-*"))


class NamedReader(BaseTool):
    name = "read_file"

    def execute(self, **kwargs):
        raise AssertionError("An unrelated reader must not be invoked")


class RestrictedReader(ReadFileTool):
    def execute(self, **kwargs):
        return '{"status":"error","error":"this reader cannot access the local workspace"}'


@pytest.mark.parametrize("reader", [None, NamedReader(), RestrictedReader()])
def test_missing_or_overridden_reader_does_not_claim_local_read_access(workspace, reader):
    registry = ToolRegistry()
    if reader:
        registry.register(reader)
    assert_fallback(artifacts.deliver_tool_result(large_result(), workspace, registry))
    assert not list(workspace.iterdir())


def test_shell_can_recover_an_allowed_result_without_advertising_read_file(workspace):
    registry = ToolRegistry()
    registry.register(BashTool())
    receipt = json.loads(artifacts.deliver_tool_result(large_result(), workspace, registry))
    assert "Use bash" in receipt["_delivery"]
    assert "Use read_file" not in receipt["_delivery"]
    assert Path(receipt["_result_file"]["path"]).is_file()


def test_shell_presence_never_bypasses_run_directory_allowlist(workspace):
    registry = ToolRegistry()
    registry.register(BashTool())
    outside = workspace.parent.parent / f"{workspace.parent.name}-outside"
    outside.mkdir()
    assert_fallback(artifacts.deliver_tool_result(large_result(), outside, registry))
    assert not list(outside.iterdir())


def test_missing_run_directory_is_not_created(workspace):
    missing = workspace / "not-a-running-task"
    assert_fallback(artifacts.deliver_tool_result(large_result(), missing, registry_with_reader()))
    assert not missing.exists()


def test_sensitive_workspace_name_is_not_leaked_as_an_artifact_locator(workspace):
    sensitive = workspace / "TOKEN=private-workspace-value"
    sensitive.mkdir()
    delivered = artifacts.deliver_tool_result(large_result(), sensitive, registry_with_reader())
    assert_fallback(delivered)
    assert "private-workspace-value" not in delivered
    assert not list(sensitive.iterdir())


@pytest.mark.parametrize("which", ["result", "part"])
def test_modified_artifacts_are_never_replaced_or_claimed(workspace, which):
    registry = registry_with_reader()
    receipt = json.loads(artifacts.deliver_tool_result(large_result(), workspace, registry))
    info = receipt["_result_file"]
    path = Path(info["path"]) if which == "result" else Path(info["parts_dir"]) / "1.txt"
    path.write_text("changed by a different writer")
    assert_fallback(artifacts.deliver_tool_result(large_result(), workspace, registry))
    assert path.read_text() == "changed by a different writer"


@pytest.mark.parametrize("which", ["folder", "result", "part"])
def test_symlink_aliases_are_refused_without_touching_the_target(workspace, which):
    registry = registry_with_reader()
    raw = large_result()
    digest = hashlib.sha256(redact_tool_result(raw).encode()).hexdigest()
    outside = workspace.parent / "other-task"
    outside.mkdir()
    if which == "folder":
        link = workspace / "tool-results"
        target = outside
    else:
        link = workspace / "tool-results" / digest
        link = link / "result.json" if which == "result" else link / "parts" / "1.txt"
        link.parent.mkdir(parents=True)
        target = outside / "do-not-change.txt"
        target.write_text("external observation")
    try:
        link.symlink_to(target, target_is_directory=which == "folder")
    except (OSError, NotImplementedError):
        pytest.skip("host cannot create symbolic links")
    before = {p.name: p.read_bytes() for p in outside.iterdir() if p.is_file()}
    assert_fallback(artifacts.deliver_tool_result(raw, workspace, registry))
    assert {p.name: p.read_bytes() for p in outside.iterdir() if p.is_file()} == before


def test_failed_write_falls_back_without_a_false_file_receipt(workspace, monkeypatch):
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(artifacts, "_write_original", fail)
    assert_fallback(artifacts.deliver_tool_result(large_result(), workspace, registry_with_reader()))


def test_large_metadata_stays_in_the_original_with_a_bounded_receipt(workspace):
    original = json.loads(large_result())
    original["_provenance"] = {"source": "中文" * 6000}
    receipt_text = artifacts.deliver_tool_result(json.dumps(original, ensure_ascii=False), workspace, registry_with_reader())
    assert len(receipt_text.encode("utf-8")) <= TOOL_RESULT_LIMIT
    receipt = json.loads(receipt_text)
    assert "_metadata_location" in receipt
    saved = json.loads(Path(receipt["_result_file"]["path"]).read_text())
    assert saved["_provenance"] == original["_provenance"]
