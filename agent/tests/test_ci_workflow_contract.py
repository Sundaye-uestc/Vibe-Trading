"""The fork's CI must test and package changes without publishing artifacts."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict:
    parsed = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), "workflow did not parse as a mapping"
    return parsed


def _commands(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_ci_runs_for_main_and_pull_requests_without_path_exclusions() -> None:
    parsed = _workflow()
    # PyYAML's YAML 1.1 loader treats an unquoted `on:` key as boolean True.
    triggers = parsed[True] if True in parsed else parsed["on"]
    assert {"push", "pull_request", "workflow_dispatch"} <= triggers.keys()
    assert "main" in triggers["push"]["branches"]
    for event in ("push", "pull_request"):
        settings = triggers[event] or {}
        assert "paths" not in settings and "paths-ignore" not in settings


def test_ci_has_read_only_permissions_and_no_publish_steps() -> None:
    parsed = _workflow()
    assert parsed["permissions"] == {"contents": "read"}
    assert "${{ secrets." not in _WORKFLOW.read_text(encoding="utf-8")
    for name, job in parsed["jobs"].items():
        permissions = job.get("permissions", parsed["permissions"])
        assert isinstance(permissions, dict) and set(permissions.values()) <= {"read", "none"}, name
        assert not re.search(
            r"\b(?:git\s+push|docker\s+push|npm\s+publish|twine\s+upload|gh\s+release\s+create)\b",
            _commands(job),
        ), name
        for step in job["steps"]:
            assert step.get("with", {}).get("push") not in (True, "true"), name
            assert not str(step.get("uses", "")).startswith("docker/"), name


def test_external_actions_are_pinned_to_full_commit_shas() -> None:
    actions = [
        step["uses"]
        for job in _workflow()["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]
    assert actions
    for action in actions:
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", action), action


def test_frontend_tests_and_build_cover_both_supported_node_generations() -> None:
    job = _workflow()["jobs"]["frontend"]
    assert set(job["strategy"]["matrix"]["node"]) == {"22", "26"}
    assert job["defaults"]["run"]["working-directory"] == "frontend"
    assert any(
        str(step.get("uses", "")).startswith("actions/setup-node@")
        and step["with"]["node-version"] == "${{ matrix.node }}"
        for step in job["steps"]
    )
    for command in ("npm ci", "npm run test:run", "npm run build"):
        step = next(step for step in job["steps"] if step.get("run", "").strip() == command)
        assert not step.get("continue-on-error") and "if" not in step
    assert "NODE_OPTIONS" not in job.get("env", {})


def test_backend_checks_locked_install_full_test_suite_and_wheel() -> None:
    job = _workflow()["jobs"]["backend"]
    commands = _commands(job)
    assert "python -m pip install --require-hashes -r requirements-lock.txt" in commands
    assert "python -m pip install --no-deps -e ." in commands
    assert "python -m pip check" in commands
    assert "python -m pytest -q agent/tests" in commands
    assert "python -m pip wheel --no-deps --wheel-dir dist ." in commands
    assert job["env"]["VIBE_TRADING_RUN_LIVE_E2E"] == "0"
    assert not job.get("continue-on-error") and "if" not in job
    for step in job["steps"]:
        assert not step.get("continue-on-error") and "if" not in step


@pytest.mark.parametrize("scenario", ["complete", "missing_font", "missing_license", "empty_font"])
def test_wheel_resource_guard_rejects_missing_or_empty_assets(tmp_path, scenario) -> None:
    """Execute CI's actual embedded check against small synthetic wheels."""
    job = _workflow()["jobs"]["backend"]
    step = next(step for step in job["steps"] if "pip wheel" in step.get("run", ""))
    match = re.search(r"python - <<'PY'\n(.*?)\nPY(?:\n|$)", step["run"], re.DOTALL)
    assert match, "wheel resource verification script missing"
    dist = tmp_path / "dist"
    dist.mkdir()
    with ZipFile(dist / "vibe_trading_ai-test.whl", "w") as wheel:
        for name in ("VibeReportCJK-Regular.ttf", "OFL.txt", "README.md"):
            if scenario == "missing_font" and name.endswith(".ttf"):
                continue
            if scenario == "missing_license" and name == "OFL.txt":
                continue
            content = b"" if scenario == "empty_font" and name.endswith(".ttf") else b"test resource"
            wheel.writestr(f"src/channels/fonts/{name}", content)
    result = subprocess.run(
        [sys.executable, "-c", match.group(1)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10,
    )
    if scenario == "complete":
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert "packaged resource" in result.stderr
