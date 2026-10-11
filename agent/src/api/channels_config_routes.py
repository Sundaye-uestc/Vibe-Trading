"""Authenticated API for editing channel configuration from the Settings UI.

The runtime historically exposed only ``/channels/status``.  This module keeps
the web form intentionally small: it returns adapter defaults and field
metadata, masks credentials, and atomically merges one channel section into
the operator's JSON config.  Secrets are never returned in clear text.
"""

from __future__ import annotations

import sys
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.channels import config as channels_config
from src.channels.config_meta import channel_field_hints, is_secret_key, split_values_secrets
from src.channels.registry import discover_channel_names, inspect_channel, inspect_channels, load_channel_class
from src.config import writer as config_writer
from src.config.paths import get_config_path


class ChannelConfigUpdateRequest(BaseModel):
    """Merge patch for a single ``channels.<name>`` section."""

    model_config = ConfigDict(extra="allow")

    config: dict[str, Any] = Field(default_factory=dict)


class ChannelTestRequest(BaseModel):
    """Optional unsaved channel values for a connection test."""

    model_config = ConfigDict(extra="allow")

    config: dict[str, Any] | None = None


AuthDep = Callable[..., Awaitable[Any] | Any]


def _host():
    return sys.modules.get("api_server") or sys.modules.get("agent.api_server")


def _stored(name: str) -> dict[str, Any]:
    section = channels_config.load_channels_config().get(name)
    return dict(section) if isinstance(section, dict) else {}


def _defaults(name: str) -> dict[str, Any]:
    try:
        cls = load_channel_class(name)
        raw = cls.default_config()
        return dict(raw) if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _effective(name: str) -> dict[str, Any]:
    return {**_defaults(name), **_stored(name)}


def _entry(name: str) -> dict[str, Any]:
    status = inspect_channel(name).to_dict()
    runtime = getattr(_host(), "_channel_runtime", None)
    if runtime is not None:
        live = (runtime.status().get("channels") or {}).get(name)
        if isinstance(live, dict):
            status.update(live)
    values, secrets = split_values_secrets(name, _effective(name))
    try:
        supports_test = bool(getattr(load_channel_class(name), "supports_connection_test", False))
    except Exception:
        supports_test = False
    return {
        "display_name": status.get("display_name") or name,
        "available": bool(status.get("available", False)),
        "configured": bool(_stored(name)),
        "enabled": bool(_stored(name).get("enabled", False)),
        "loaded": bool(status.get("loaded", False)),
        "running": bool(status.get("running", False)),
        "install_hint": str(status.get("install_hint") or ""),
        "error": str(status.get("error") or ""),
        "supports_test": supports_test,
        "fields": channel_field_hints(name),
        "values": values,
        "secrets": secrets,
    }


def _clear_flags(extra: dict[str, Any] | None) -> list[str]:
    return [
        key[6:]
        for key, value in (extra or {}).items()
        if key.startswith("clear_") and len(key) > 6 and bool(value)
    ]


def _patch(name: str, body: dict[str, Any]) -> dict[str, Any]:
    # Empty secret inputs mean "keep existing".  This lets the UI render a
    # masked credential without accidentally erasing it on Save.
    return {
        key: value
        for key, value in body.items()
        if not (is_secret_key(name, key) and isinstance(value, str) and not value.strip())
    }


def _validate(name: str, merged: dict[str, Any]) -> None:
    try:
        cls = load_channel_class(name)
        cls(merged, __import__("src.channels.bus.queue", fromlist=["MessageBus"]).MessageBus())
    except ValidationError as exc:
        fields = [".".join(str(part) for part in item.get("loc", ())) for item in exc.errors()]
        raise HTTPException(status_code=422, detail={"code": "validation_error", "fields": fields}) from None
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail={"code": "validation_error", "message": str(exc)[:200]}) from None


def register_channels_config_routes(
    app: FastAPI,
    require_auth: AuthDep | None = None,
    require_settings_write_auth: AuthDep | None = None,
) -> None:
    """Register GET/PUT/test channel configuration endpoints."""
    host = _host()
    if host is None:
        raise RuntimeError("api_server must be imported before registering channel config routes")
    require_auth = require_auth or host.require_auth
    require_settings_write_auth = require_settings_write_auth or host.require_settings_write_auth

    @app.get("/channels/config", dependencies=[Depends(require_auth)])
    async def get_channels_config():
        names = sorted(discover_channel_names())
        runtime = getattr(host, "_channel_runtime", None)
        return {
            "config_path": str(get_config_path()),
            "writable": get_config_path().suffix.lower() == ".json",
            "runtime_running": bool(runtime and runtime.status().get("running")),
            "channels": {name: _entry(name) for name in names},
        }

    @app.put("/channels/config/{name}", dependencies=[Depends(require_settings_write_auth)])
    async def put_channel_config(name: str, payload: ChannelConfigUpdateRequest):
        if name not in set(discover_channel_names()):
            raise HTTPException(status_code=404, detail=f"Unknown channel: {name}")
        stored = _stored(name)
        patch = _patch(name, payload.config)
        merged = {**stored, **patch}
        for key in _clear_flags(payload.model_extra):
            merged.pop(key, None)
        _validate(name, merged)
        try:
            config_writer.update_channel_section(name, patch, clears=_clear_flags(payload.model_extra))
        except config_writer.ConfigNotWritableError as exc:
            raise HTTPException(status_code=400, detail={"code": "config_not_writable", "message": str(exc)}) from None
        return {"channel": _entry(name), "applied": "saved"}

    @app.post("/channels/{name}/test", dependencies=[Depends(require_settings_write_auth)])
    async def test_channel(name: str, payload: ChannelTestRequest):
        if name not in set(discover_channel_names()):
            raise HTTPException(status_code=404, detail=f"Unknown channel: {name}")
        stored = _stored(name)
        body = _patch(name, payload.config or {})
        merged = {**stored, **body}
        for key in _clear_flags(payload.model_extra):
            merged.pop(key, None)
        try:
            cls = load_channel_class(name)
            instance = cls(merged, __import__("src.channels.bus.queue", fromlist=["MessageBus"]).MessageBus())
            test = getattr(instance, "test_connection", None)
            if not callable(test):
                return {"ok": False, "code": "unsupported", "detail": "connection test is not supported"}
            result = await test()
            return {"ok": bool(result.get("ok")), **result}
        except Exception as exc:
            return {"ok": False, "code": "invalid_credentials", "detail": type(exc).__name__}
