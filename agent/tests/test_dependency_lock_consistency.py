"""Catch dependency declarations missing from fresh and packaged installs."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
LOCKS = ("requirements-lock.txt", "desktop/electron/requirements-windows-lock.txt")


def _requirements(path: Path) -> list[tuple[Requirement, list[str]]]:
    entries = []
    logical_line = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        logical_line += " " + line.rstrip("\\").strip()
        if line.endswith("\\"):
            continue
        requirement, *hashes = logical_line.strip().split(" --hash=")
        entries.append((Requirement(requirement.strip()), hashes))
        logical_line = ""
    assert not logical_line, f"Unfinished requirement in {path}"
    return entries


def _core_requirements() -> list[Requirement]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return [Requirement(value) for value in project["project"]["dependencies"]]


def test_core_dependency_declarations_match():
    project = {canonicalize_name(req.name): req for req in _core_requirements()}
    text_file = {
        canonicalize_name(req.name): req
        for req, _hashes in _requirements(ROOT / "agent/requirements.txt")
    }
    assert project == text_file, "Keep pyproject.toml and agent/requirements.txt constraints synchronized"


@pytest.mark.parametrize("filename", LOCKS)
def test_every_locked_distribution_is_exactly_pinned_and_hashed(filename):
    entries = _requirements(ROOT / filename)
    assert entries
    for req, hashes in entries:
        specifiers = list(req.specifier)
        assert len(specifiers) == 1 and specifiers[0].operator == "==", str(req)
        assert "*" not in specifiers[0].version, str(req)
        assert hashes and all(re.fullmatch(r"sha256:[0-9a-f]{64}", digest) for digest in hashes), str(req)


def test_windows_overlay_dependencies_are_present_in_hashed_lock():
    """Cross-compilation must retain dependencies selected by native Windows."""
    source = ROOT / "desktop/electron/requirements-windows.in"
    environment = {**default_environment(), "sys_platform": "win32", "platform_system": "Windows",
                   "platform_machine": "AMD64", "python_version": "3.12", "python_full_version": "3.12.10"}
    locked = {canonicalize_name(req.name): next(iter(req.specifier)).version
              for req, _ in _requirements(ROOT / LOCKS[1])}
    includes = []
    for raw in source.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            includes.append((source.parent / line[3:].strip()).resolve())
            continue
        req = Requirement(line)
        if req.marker is None or req.marker.evaluate(environment):
            name = canonicalize_name(req.name)
            assert name in locked, f"Windows lock is missing {req}"
            assert req.specifier.contains(locked[name]), f"Windows lock does not satisfy {req}"
    assert (ROOT / "agent/requirements.txt").resolve() in includes


@pytest.mark.parametrize("filename,platform,python_version", [
    ("requirements-lock.txt", "linux", "3.11"),
    ("requirements-lock.txt", "linux", "3.12"),
    ("requirements-lock.txt", "darwin", "3.12"),
    ("desktop/electron/requirements-windows-lock.txt", "win32", "3.12"),
])
def test_locks_cover_core_dependencies_for_their_target(filename, platform, python_version):
    environment = {
        **default_environment(),
        "sys_platform": platform,
        "os_name": "nt" if platform == "win32" else "posix",
        "platform_system": {"linux": "Linux", "darwin": "Darwin", "win32": "Windows"}[platform],
        "platform_machine": "AMD64" if platform == "win32" else "x86_64",
        "python_version": python_version,
        "python_full_version": f"{python_version}.0",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
    }
    locked = {
        canonicalize_name(req.name): next(iter(req.specifier)).version
        for req, _hashes in _requirements(ROOT / filename)
        if req.marker is None or req.marker.evaluate(environment)
    }
    for req in _core_requirements():
        if req.marker is not None and not req.marker.evaluate(environment):
            continue
        name = canonicalize_name(req.name)
        assert name in locked, f"{filename} is missing {req}"
        assert req.specifier.contains(locked[name]), f"{filename}: {name}=={locked[name]} does not satisfy {req}"
