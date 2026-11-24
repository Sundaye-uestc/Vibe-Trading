"""Check locally packaged resources, import origins and the Windows runtime build."""

from __future__ import annotations

import runpy
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_GUARD = runpy.run_path(str(_ROOT / "scripts" / "check_packaged_runtime.py"))


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
