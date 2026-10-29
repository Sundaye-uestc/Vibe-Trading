"""The fork's CI must test and package changes without publishing artifacts."""

from __future__ import annotations

import re
import runpy
from pathlib import Path
from types import ModuleType

import pytest
import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
_ROOT = _WORKFLOW.parents[2]
_GUARD = runpy.run_path(str(_ROOT / "scripts" / "check_packaged_runtime.py"))


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
    assert 'python -m pip install --no-deps --target "$RUNNER_TEMP/vibe-wheel-smoke" dist/vibe_trading_ai-*.whl' in commands
    assert 'python -I scripts/check_packaged_runtime.py "$RUNNER_TEMP/vibe-wheel-smoke"' in commands
    assert job["env"]["VIBE_TRADING_RUN_LIVE_E2E"] == "0"
    assert not job.get("continue-on-error") and "if" not in job
    for step in job["steps"]:
        assert not step.get("continue-on-error") and "if" not in step


@pytest.fixture
def packaged_resources(tmp_path):
    for name in _GUARD["RESOURCES"]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'{"example": true}' if name.endswith(".json") else b"test resource")
    return tmp_path


def test_packaged_resources_accept_complete_installation(packaged_resources) -> None:
    _GUARD["check_resources"](packaged_resources)


@pytest.mark.parametrize("resource", _GUARD["RESOURCES"])
@pytest.mark.parametrize("damage", ["missing", "empty"])
def test_packaging_guard_rejects_missing_or_empty_assets(packaged_resources, resource, damage) -> None:
    """A missing runtime policy is just as fatal as a missing report font."""
    target = packaged_resources / resource
    if damage == "missing":
        target.unlink()
    else:
        target.write_bytes(b"")
    with pytest.raises(RuntimeError, match="Missing or empty packaged resource"):
        _GUARD["check_resources"](packaged_resources)


@pytest.mark.parametrize("payload", [b"{broken json", b"[]", b"{}", b"\xff"])
def test_packaging_guard_rejects_unreadable_runtime_json(packaged_resources, payload) -> None:
    (packaged_resources / "src/agent/tool_loading.json").write_bytes(payload)
    with pytest.raises(RuntimeError, match="Invalid packaged JSON"):
        _GUARD["check_resources"](packaged_resources)


@pytest.mark.parametrize("namespace", [False, True])
def test_import_origin_guard_rejects_editable_source_fallback(tmp_path, namespace) -> None:
    installed = tmp_path / "installed"
    source = tmp_path / "checkout"
    module = ModuleType("src.example")
    if namespace:
        module.__path__ = [str(installed / "src")]
    else:
        module.__file__ = str(installed / "src" / "example.py")
    _GUARD["check_module_origin"](module, installed)
    if namespace:
        module.__path__.append(str(source / "src"))
    else:
        module.__file__ = str(source / "src" / "example.py")
    with pytest.raises(RuntimeError, match="outside packaged runtime"):
        _GUARD["check_module_origin"](module, installed)


def test_windows_checks_the_same_installed_runtime_resources() -> None:
    script = (_ROOT / "desktop/electron/scripts/build-backend.ps1").read_text(encoding="utf-8")
    assert "& $runtimePython -I (Join-Path $repoRoot 'scripts\\check_packaged_runtime.py') $sitePackages" in script
    assert "throw 'Embedded backend packaged-resource verification failed.'" in script
    workflow = yaml.safe_load((_ROOT / ".github/workflows/desktop-windows.yml").read_text(encoding="utf-8"))
    triggers = workflow.get(True, workflow.get("on"))
    for event in ("push", "pull_request"):
        assert "scripts/check_packaged_runtime.py" in triggers[event]["paths"]
