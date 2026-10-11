"""Regression tests for the Settings channel configuration surface."""

from pathlib import Path

from fastapi.testclient import TestClient

import api_server


def test_channel_config_masks_secrets_and_exposes_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    (tmp_path / "agent.json").write_text(
        '{"channels":{"email":{"enabled":false,"smtp_host":"smtp.example",'
        '"smtp_password":"super-secret"}}}\n', encoding="utf-8"
    )
    client = TestClient(api_server.app, client=("127.0.0.1", 50000))
    response = client.get("/channels/config")
    assert response.status_code == 200
    email = response.json()["channels"]["email"]
    assert email["secrets"]["smtp_password"]["set"] is True
    assert "super-secret" not in response.text
    assert any(field["key"] == "smtp_host" for field in email["fields"])


def test_channel_config_put_preserves_masked_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    config = tmp_path / "agent.json"
    config.write_text(
        '{"channels":{"email":{"enabled":false,"smtp_password":"keep-me"}}}\n',
        encoding="utf-8",
    )
    client = TestClient(api_server.app, client=("127.0.0.1", 50000))
    response = client.put("/channels/config/email", json={"config": {"smtp_host": "smtp.example"}})
    assert response.status_code == 200
    assert '"smtp_password": "keep-me"' in config.read_text(encoding="utf-8")
    assert '"smtp_host": "smtp.example"' in config.read_text(encoding="utf-8")


def test_channel_config_rejects_unknown_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    client = TestClient(api_server.app, client=("127.0.0.1", 50000))
    response = client.put("/channels/config/nope", json={"config": {"enabled": True}})
    assert response.status_code == 404
