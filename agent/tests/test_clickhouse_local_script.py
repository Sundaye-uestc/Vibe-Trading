"""Local database lifecycle must not expose secrets or adopt unknown data."""

from __future__ import annotations

import json
import runpy
import stat
import types
from pathlib import Path
from xml.etree import ElementTree

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "clickhouse-local"


@pytest.fixture
def manager():
    return runpy.run_path(str(SCRIPT))


def test_configuration_is_private_loopback_and_has_no_anonymous_user(tmp_path, manager):
    config = manager["configure"](tmp_path / "db", 18123, 19000)
    root = tmp_path / "db"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for name in ("credentials.json", "users.xml", "config.xml"):
        assert stat.S_IMODE((root / name).stat().st_mode) == 0o600
    server = ElementTree.parse(root / "config.xml").getroot()
    assert server.findtext("listen_host") == "127.0.0.1"
    assert server.findtext("http_port") == "18123"
    assert server.findtext("access_control_improvements/settings_constraints_replace_previous") == "true"
    users = ElementTree.parse(root / "users.xml").getroot()
    assert users.find("users/default") is None
    assert users.findtext("users/paper_reader/grants/query") == "GRANT SELECT ON vibe_paper.*"
    assert users.findtext("users/paper_reader/networks/ip") == "127.0.0.1"
    assert users.findtext("profiles/paper_readonly/readonly") == "1"
    assert users.findtext("profiles/paper_readonly/constraints/readonly/min") == "1"
    assert users.findtext("profiles/paper_readonly/constraints/readonly/max") == "1"
    assert users.findtext("profiles/paper_readonly/max_execution_time") == "10"
    assert users.findtext("profiles/paper_readonly/constraints/max_execution_time/min") == "1"
    assert users.findtext("profiles/paper_readonly/constraints/max_execution_time/max") == "120"
    assert users.find("profiles/paper_readonly/constraints/max_execution_time/changeable_in_readonly") is not None
    assert users.find("profiles/paper_readonly/constraints/max_threads/readonly") is not None
    assert users.find("profiles/paper_readonly/constraints/max_memory_usage/readonly") is not None
    assert config["admin"]["password"] != config["reader"]["password"]
    assert config["reader"]["password"] not in (root / "users.xml").read_text()


def test_existing_configuration_reuses_credentials_without_rewrite(tmp_path, manager):
    first = manager["configure"](tmp_path / "db", 18123, 19000)
    before = {path.name: path.read_bytes() for path in (tmp_path / "db").glob("*.*")}
    assert manager["configure"](tmp_path / "db", 18123, 19000) == first
    assert {path.name: path.read_bytes() for path in (tmp_path / "db").glob("*.*")} == before
    with pytest.raises(RuntimeError, match="Ports differ"):
        manager["configure"](tmp_path / "db", 8123, 9000)


@pytest.mark.parametrize("kind", ["partial", "foreign", "linked", "data"])
def test_refuses_partial_or_unknown_instance_without_replacing_files(tmp_path, manager, kind):
    root = tmp_path / "db"
    root.mkdir()
    if kind == "data":
        (root / "data").mkdir()
        (root / "data" / "valuable").write_text("keep")
    else:
        for name in ("credentials.json", "config.xml", "users.xml"):
            (root / name).write_text(json.dumps({"managed_by": "other"}))
        if kind == "partial":
            (root / "users.xml").unlink()
        elif kind == "linked":
            (root / "users.xml").unlink()
            (root / "users.xml").symlink_to(root / "config.xml")
    before = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(RuntimeError):
        manager["configure"](root, 18123, 19000)
    assert {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


def test_foreign_pid_is_never_signalled(tmp_path, manager, monkeypatch):
    (tmp_path / "server.pid").write_text("1234")
    monkeypatch.setattr(manager["subprocess"], "run", lambda *a, **k:
                        types.SimpleNamespace(returncode=0, stdout="S /usr/bin/other-service"))
    signals = []
    monkeypatch.setattr(manager["os"], "kill", lambda *args: signals.append(args))
    with pytest.raises(RuntimeError, match="another process"):
        manager["stop"](tmp_path)
    assert signals == []


def test_stale_or_zombie_pid_is_stopped_without_signalling(tmp_path, manager, monkeypatch):
    (tmp_path / "server.pid").write_text("1234")
    monkeypatch.setattr(manager["subprocess"], "run", lambda *a, **k:
                        types.SimpleNamespace(returncode=0, stdout="Z (clickhouse)"))
    assert manager["managed_pid"](tmp_path) is None


def test_unrecognized_binary_is_not_executed_or_overwritten(tmp_path, manager, monkeypatch):
    monkeypatch.setattr(manager["platform"], "system", lambda: "Darwin")
    monkeypatch.setattr(manager["platform"], "machine", lambda: "arm64")
    binary = tmp_path / "bin" / "clickhouse"
    binary.parent.mkdir()
    binary.write_text("unknown file")
    monkeypatch.setattr(manager["subprocess"], "run", lambda *a, **k: pytest.fail("Do not run an unknown binary"))
    with pytest.raises(RuntimeError, match="differs"):
        manager["install"](tmp_path)
    assert binary.read_text() == "unknown file"
