"""Local forward paper accounts shared by CLI and Web API; no broker writes.

The pure step tool remains available for caller-owned ledgers/replays. This
service owns wall-clock eligibility and commits each successful step atomically.
"""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import uuid
from contextlib import contextmanager
from functools import partial
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from src.config.paths import get_runtime_root
from .schema import MARKETS, parse_account_state, parse_request

# Daily bars may remain provisional shortly after the regular market close.
_MARKET_TIME = {
    "cn_a": ("Asia/Shanghai", 15, 30),
    "hk": ("Asia/Hong_Kong", 16, 30),
    "us": ("America/New_York", 16, 30),
}
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class AccountConflict(ValueError):
    """A concurrent update or conflicting creation request cannot be applied."""


class PaperAccounts:
    """SQLite ledger with optimistic concurrency across processes and surfaces."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        clock: Callable | None = None,
        step: Callable | None = None,
    ):
        self.path = path or get_runtime_root() / "paper-trading" / "accounts.db"
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if step is None:
            from .tool import run_paper_trade_step

            step = partial(run_paper_trade_step, _orders_at_end=True)
        self.step = step
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, document TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS steps (account_id TEXT, revision INTEGER, document TEXT NOT NULL, "
                "PRIMARY KEY(account_id, revision))"
            )
            # Additive migration: existing ledgers need no rewrite. The key and
            # account are committed together, so a lost response is safe to retry.
            db.execute(
                "CREATE TABLE IF NOT EXISTS account_create_requests ("
                "idempotency_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, "
                "account_id TEXT NOT NULL REFERENCES accounts(id))"
            )

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _now(self, market):
        return self.clock().astimezone(ZoneInfo(_MARKET_TIME[market][0]))

    def create(
        self,
        *,
        label: str,
        market: str,
        account_state: dict,
        start: str = "today",
        fee_model: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Create a ledger eligible today or at the next observed trading session.

        Args:
            label: Human-readable account name.
            market: cn_a, hk, or us.
            account_state: Initial cash and positions, in the market's currency.
            start: today or next_session; holidays are resolved by actual bars.
            fee_model: Optional overrides supported by the selected market.
            idempotency_key: Optional durable key for one creation intent. Retry
                with the same parameters to receive the account's current state.
        """
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str)
            or re.fullmatch(IDEMPOTENCY_KEY_PATTERN, idempotency_key) is None
        ):
            raise ValueError(
                "idempotency_key must contain 1–128 ASCII letters, digits, '.', '_', ':' or '-', "
                "and start with a letter or digit"
            )
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise ValueError("label must contain 1–80 characters")
        if market not in MARKETS or start not in ("today", "next_session"):
            raise ValueError("invalid market or start mode")
        state = parse_account_state(account_state)
        if state.cash < 0 or any(
            p.qty <= 0 or p.avg_cost <= 0 or not p.symbol.strip()
            for p in state.positions
        ):
            raise ValueError(
                "Initial cash must be non-negative; positions require positive quantity and average cost"
            )
        if len({p.symbol for p in state.positions}) != len(state.positions):
            raise ValueError("Initial positions must have unique symbols")
        today = self._now(market).date()
        earliest = today + timedelta(days=int(start == "next_session"))
        while earliest.weekday() >= 5:
            earliest += timedelta(days=1)
        document = {
            "id": uuid.uuid4().hex,
            "label": label.strip(),
            "market": market,
            "currency": {"cn_a": "CNY", "hk": "HKD", "us": "USD"}[market],
            "created_at": self.clock().isoformat(),
            "start": start,
            "start_on": earliest.isoformat(),
            "last_date": None,
            "revision": 0,
            "initial_state": asdict(state),
            "account_state": asdict(state),
            "fee_model": fee_model if fee_model is not None else {},
            "last_result": None,
        }
        # Reuse the tool's strict numeric/fee validation at every entrance.
        parsed = parse_request(
            {
                "account_id": document["id"],
                "market": market,
                "account_state": account_state,
                "from_date": document["start_on"],
                "to_date": document["start_on"],
                "orders": [],
                "fee_model": document["fee_model"],
            }
        )
        if any(value < 0 for value in document["fee_model"].values()):
            raise ValueError("Fees must be non-negative")
        # Compare the caller's validated intent, excluding generated dates/IDs.
        # A retry the next day still refers to the original creation. Numeric
        # JSON representations and object key ordering do not change the intent.
        intent = {
            "label": document["label"],
            "market": market,
            "account_state": asdict(state),
            "start": start,
            "fee_model": parsed.fee_model,
        }
        request_hash = hashlib.sha256(
            json.dumps(intent, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        document = json.loads(json.dumps(document))
        with self._connect() as db:
            # Serialize the lookup + inserts across processes. A unique-key
            # constraint alone would leave racing callers with an IntegrityError.
            db.execute("BEGIN IMMEDIATE")
            if idempotency_key is not None:
                previous = db.execute(
                    "SELECT request_hash, account_id FROM account_create_requests "
                    "WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if previous is not None:
                    if previous[0] != request_hash:
                        raise AccountConflict(
                            "Creation key already used with different parameters; "
                            "reload accounts or use a new key for a new account"
                        )
                    saved = db.execute(
                        "SELECT document FROM accounts WHERE id = ?", (previous[1],)
                    ).fetchone()
                    if saved is None:
                        raise RuntimeError("Saved creation request refers to a missing account")
                    return json.loads(saved[0])
            db.execute(
                "INSERT INTO accounts VALUES (?, ?)",
                (document["id"], json.dumps(document)),
            )
            if idempotency_key is not None:
                db.execute(
                    "INSERT INTO account_create_requests VALUES (?, ?, ?)",
                    (idempotency_key, request_hash, document["id"]),
                )
        return document

    def list(self) -> list[dict]:
        with self._connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT document FROM accounts ORDER BY rowid DESC"
                )
            ]

    def get(self, account_id: str) -> dict:
        with self._connect() as db:
            row = db.execute(
                "SELECT document FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        if row is None:
            raise KeyError("paper account not found")
        return json.loads(row[0])

    def history(self, account_id: str) -> list[dict]:
        self.get(account_id)
        with self._connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT document FROM steps WHERE account_id = ? ORDER BY revision",
                    (account_id,),
                )
            ]

    def advance(
        self,
        account_id: str,
        *,
        expected_revision: int,
        to_date: str,
        orders: list | None = None,
        strategy_path: str | None = None,
    ) -> dict:
        """Match completed daily bars and atomically commit, refusing stale callers.

        Args:
            account_id: Saved account identifier.
            expected_revision: Revision last read by the caller.
            to_date: Last completed execution day (YYYY-MM-DD).
            orders: Explicit orders for a single day, including [] for valuation.
            strategy_path: Strategy under the configured runtime strategies folder.
        """
        account = self.get(account_id)
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision != account["revision"]
        ):
            raise AccountConflict("Account changed; reload before advancing")
        now = self._now(account["market"])
        zone, hour, minute = _MARKET_TIME[account["market"]]
        latest = now.date() - timedelta(
            days=int((now.hour, now.minute) < (hour, minute))
        )
        earliest = account["start_on"]
        if account["last_date"]:
            earliest = (
                datetime.fromisoformat(account["last_date"]).date() + timedelta(days=1)
            ).isoformat()
        if earliest > latest.isoformat():
            raise ValueError(
                f"No completed session to settle yet. Execution dates start at {earliest}; "
                f"a session can be settled from {hour:02d}:{minute:02d} {zone} time"
            )
        if (
            not isinstance(to_date, str)
            or not earliest <= to_date <= latest.isoformat()
        ):
            raise ValueError(
                f"Execution date must be between {earliest} and {latest.isoformat()}; "
                "later dates wait for a completed session"
            )
        payload = {
            "account_id": account_id,
            "market": account["market"],
            "account_state": account["account_state"],
            "fee_model": account["fee_model"],
            "from_date": earliest,
            "to_date": to_date,
        }
        if orders is not None:
            payload["orders"] = orders
        if strategy_path is not None:
            payload["strategy_path"] = strategy_path
        parse_request(payload)
        result = json.loads(self.step(payload))
        if "code" in result:
            return {"account": account, "result": result, "committed": False}
        # Closed dates must not consume the account's first eligible session.
        if not result.get("nav_series"):
            return {"account": account, "result": result, "committed": False}
        previous = json.dumps(account)
        account.update(
            account_state=result["end_state"],
            last_date=to_date,
            revision=expected_revision + 1,
            last_result=result,
        )
        with self._connect() as db:
            updated = db.execute(
                "UPDATE accounts SET document = ? WHERE id = ? AND document = ?",
                (json.dumps(account), account_id, previous),
            ).rowcount
            if updated != 1:
                raise AccountConflict(
                    "Account advanced concurrently; result discarded, reload"
                )
            db.execute(
                "INSERT INTO steps VALUES (?, ?, ?)",
                (account_id, account["revision"], json.dumps(result)),
            )
        return {"account": account, "result": result, "committed": True}
