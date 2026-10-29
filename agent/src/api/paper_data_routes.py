"""Manual operator-only paper-data updates; browser supplies no destinations."""

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from src.api.security import require_auth, require_settings_write_auth
from src.data_ingest.paper_update import PaperDataService, UpdateBusy, validate_symbols


class WatchlistRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbols: list[StrictStr] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def canonical_symbols(self):
        self.symbols = validate_symbols(self.symbols)
        return self


class UpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _write(function, *args):
    try:
        return function(*args)
    except UpdateBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def register_paper_data_routes(app: FastAPI):
    @app.get("/api/paper-data", dependencies=[Depends(require_auth)])
    def get_status():
        return PaperDataService().status()

    @app.put("/api/paper-data/watchlist", dependencies=[Depends(require_auth), Depends(require_settings_write_auth)])
    def save_watchlist(body: WatchlistRequest):
        return _write(PaperDataService().save_watchlist, body.symbols)

    @app.post("/api/paper-data/update", dependencies=[Depends(require_auth), Depends(require_settings_write_auth)])
    def update_data(body: UpdateRequest):
        return _write(PaperDataService().update)
