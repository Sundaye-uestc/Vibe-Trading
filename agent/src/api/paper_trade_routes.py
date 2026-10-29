"""Authenticated paper accounts; sync endpoints run blocking work off the event loop."""

from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from src.api.security import require_auth, require_settings_write_auth
from src.api.helpers import _validate_path_param
from src.tools.paper_trade.accounts import AccountConflict, IDEMPOTENCY_KEY_PATTERN, PaperAccounts
from src.tools.paper_trade.preflight import run_preflight, validate_request


class PreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market: Literal["cn_a", "hk", "us"]
    from_date: StrictStr
    to_date: StrictStr
    symbols: list[StrictStr] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def valid_snapshot(self):
        self.symbols = validate_request(self.market, self.from_date, self.to_date, self.symbols)["symbols"]
        return self


class CreateAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    market: Literal["cn_a", "hk", "us"]
    account_state: dict
    start: Literal["today", "next_session"] = "today"
    fee_model: dict = Field(default_factory=dict)
    idempotency_key: str | None = Field(
        default=None, strict=True, min_length=1, max_length=128, pattern=IDEMPOTENCY_KEY_PATTERN
    )


class AdvanceAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(strict=True, ge=0)
    to_date: str
    orders: list[dict] | None = Field(default=None, max_length=1000)
    strategy_path: str | None = None

    @model_validator(mode="after")
    def exactly_one_order_source(self):
        if (self.orders is None) == (self.strategy_path is None):
            raise ValueError("Exactly one of orders or strategy_path is required")
        return self


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, "Paper account not found") from exc
    except AccountConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def register_paper_trade_routes(app: FastAPI):
    from src.api.paper_data_routes import register_paper_data_routes

    register_paper_data_routes(app)
    @app.post("/api/paper-trade/preflight", dependencies=[Depends(require_auth)])
    def data_preflight(body: PreflightRequest):
        return run_preflight(**body.model_dump())

    @app.get("/api/paper-accounts", dependencies=[Depends(require_auth)])
    def list_accounts():
        return PaperAccounts().list()

    @app.post(
        "/api/paper-accounts", dependencies=[Depends(require_auth), Depends(require_settings_write_auth)]
    )
    def create_account(body: CreateAccount):
        return _call(PaperAccounts().create, **body.model_dump())

    @app.get("/api/paper-accounts/{account_id}", dependencies=[Depends(require_auth)])
    def get_account(account_id: str):
        _validate_path_param(account_id, "account_id")
        return _call(PaperAccounts().get, account_id)

    @app.get(
        "/api/paper-accounts/{account_id}/history", dependencies=[Depends(require_auth)]
    )
    def account_history(account_id: str):
        _validate_path_param(account_id, "account_id")
        return _call(PaperAccounts().history, account_id)

    @app.post(
        "/api/paper-accounts/{account_id}/advance",
        dependencies=[Depends(require_auth), Depends(require_settings_write_auth)],
    )
    def advance_account(account_id: str, body: AdvanceAccount):
        _validate_path_param(account_id, "account_id")
        return _call(PaperAccounts().advance, account_id, **body.model_dump())

    @app.post(
        "/api/paper-trade/step", dependencies=[Depends(require_auth), Depends(require_settings_write_auth)]
    )
    def pure_step(body: dict):
        import json
        from src.tools.paper_trade.tool import run_paper_trade_step

        return json.loads(run_paper_trade_step(body))
