"""Configuration field types must preserve identifiers and credential masking."""

from types import SimpleNamespace

from src.channels import config_meta


def test_explicit_field_types_distinguish_identifiers_and_numbers():
    qq = {field["key"]: field for field in config_meta.channel_field_hints("qq")}
    websocket = {field["key"]: field for field in config_meta.channel_field_hints("websocket")}
    assert qq["app_id"]["type"] == "text"
    assert qq["download_chunk_size"]["type"] == "number"
    assert websocket["port"]["type"] == "number"
    assert websocket["ping_interval_s"]["type"] == "number"
    assert websocket["token_ttl_s"]["type"] == "number"
    assert websocket["token_ttl_s"]["secret"] is False


def test_inferred_types_follow_default_types_without_coercing_numeric_strings(monkeypatch):
    monkeypatch.setattr(config_meta, "load_channel_class", lambda _name: SimpleNamespace(
        default_config=lambda: {
            "enabled": False, "app_id": "00123", "port": 587, "timeout": 2.5,
            "use_tls": True, "allow_from": [], "api_key": "00123",
        },
    ))
    hints = {field["key"]: field for field in config_meta.channel_field_hints("test-adapter")}
    assert {key: field["type"] for key, field in hints.items()} == {
        "app_id": "text", "port": "number", "timeout": "number",
        "use_tls": "bool", "allow_from": "list", "api_key": "password",
    }
    assert hints["api_key"]["secret"] is True
