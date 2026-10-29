# Daily paper trading

Open **Paper trading** in the Web sidebar (`/paper-trading`). Create an account
with initial cash and positions, choose today or the next session, then settle
completed daily bars with explicit orders or a strategy file. The page displays
cash, positions, fills, rejections, data provenance, warnings and NAV history.
It supports every registered UI language. No broker order is sent.

Saved accounts start no earlier than creation in the market's time zone.
“Next session” sets the earliest eligible date after today; weekends are skipped
and holidays wait for available session data. Settlement waits until 30 minutes
after the regular local close (conservative on early-close days). There is no
background scheduler. Failure leaves the ledger unchanged. Revision checks
prevent concurrent requests from committing the same step. Ledger and history
live in `<runtime_root>/paper-trading/accounts.db`.

## CLI

Create `initial-state.json` with your simulated ledger:

```json
{"cash": 100000, "positions": [{"symbol": "AAPL.US", "qty": 10, "avg_cost": 330}]}
```

```bash
vibe-trading paper create --label 'US research' --market us --state initial-state.json --start today
vibe-trading paper list
vibe-trading paper show ACCOUNT_ID
vibe-trading paper advance ACCOUNT_ID --revision 0 --to-date YYYY-MM-DD --orders orders.json
vibe-trading paper history ACCOUNT_ID
```

`orders.json` is an array, e.g. `[{"symbol":"AAPL.US","side":"buy","qty":10}]`.
Use `[]` for valuation only. Replace `--orders` with `--strategy-path PATH` to run
a strategy. Explicit orders execute on `--to-date`; all intervening dates are
checked and valued. The revision is returned by `show` and changes on commit.
A nonzero CLI exit indicates an error or a step that did not commit.

## API

All routes require the existing API authentication and browser-origin policy.
State-changing POST routes also use the local/authenticated write gate;
the read-only preflight POST requires authentication without that write gate.

| Method and path | Purpose |
| --- | --- |
| `GET /api/paper-accounts` | List accounts |
| `POST /api/paper-accounts` | Create: `label`, `market`, `account_state`, optional `start` (`today` / `next_session`), `fee_model` and `idempotency_key` |
| `GET /api/paper-accounts/{id}` | Read ledger and revision |
| `GET /api/paper-accounts/{id}/history` | Read committed results |
| `POST /api/paper-accounts/{id}/advance` | `expected_revision`, `to_date`, and exactly one of `orders` / `strategy_path` |
| `POST /api/paper-trade/step` | Pure caller-owned payload; no saved account |
| `POST /api/paper-trade/preflight` | Read-only data checks: `market`, `from_date`, `to_date`, optional `symbols` |

Account creation accepts an optional `idempotency_key` (1–128 ASCII letters,
digits, `.`, `_`, `:` or `-`, starting with a letter or digit). Retry the same
creation intent with the same key and parameters to receive the existing
account's current state, even after a response is lost or the server restarts.
Reusing a key with different parameters returns HTTP 409. The Web form retains
the key across retries and refreshes the account list after request errors.
Clients that omit the key retain the original create-a-new-account behavior.

### Check data before settlement

The Web page's data check reads the same raw-price supply chain used by paper
settlement. It does not create accounts, change revisions, run strategies or
place orders. Check at most 10 market-qualified symbols (`600000.SH`, `00700.HK`,
`AAPL.US`) over 31 calendar days. With no symbols it checks calendar/configuration
only; price and event coverage are explicitly unchecked.

The response contains `status` (`ready`, `warning`, `blocked`), `ready`, per-source
`checks`, per-symbol missing/invalid price dates, provenance and warnings. Missing
calendar dates include closed days that have no explicit calendar row. Event
tables must be readable, but a successful query or zero event rows cannot prove
the requested period has been imported completely. Such checks remain warnings;
the operator must verify ingestion coverage. HK/US lack an authoritative calendar
in this engine and retain that warning even when prices are available.
Preflight results do not guarantee an order can fill or that the market has closed.
The normal settlement checks still run against freshly fetched data.

The API authenticates the read-only POST and validates browser origins. Each
check runs in a separate process, with a 45-second overall deadline and at most
two concurrent checks per API process. A timed-out worker is terminated; reduce
the range or symbol count before retrying. Completed connection, calendar and
event-table checks are retained when a later source query times out.
Diagnostic errors never include source
credentials, request URLs or remote error bodies.

Advance returns `{account, result, committed}`. Data/strategy errors return
`committed: false`; HTTP 409 means the account changed and must be reloaded.
After a network timeout, read the account before retrying: the server may have
committed. The Web client reloads it automatically on a request error.

The pure `paper_trade_step` agent/MCP tool and `vibe-trading paper step payload.json`
remain stateless and accept historical dates for reproducible research. Their
orders mode requires a single date. Saved accounts enforce forward-only dates.

## Strategy contract

Place a Python file under `<runtime_root>/strategies/`. The `SignalEngine` class
is required; `UNIVERSE` supplies symbols for cash-only accounts.

```python
UNIVERSE = ("AAPL.US",)

class SignalEngine:
    def generate_signals(self, ctx, trade_date):
        history = ctx.history("AAPL.US")
        if len(history) < 20 or ctx.account_state.positions:
            return []
        if history["close"].iloc[-1] > history["close"].iloc[-20:].mean():
            return [{"symbol": "AAPL.US", "side": "buy", "qty": 1}]
        return []
```

History contains **only dates strictly before execution**. Orders fill at that
day's close. The strategy receives the private ledger after previous days'
fills. The spawned child receives no current/future bars and retains its instance
across dates in one call. Source validation and process timeouts apply. Any
strategy failure discards the whole call, including earlier uncommitted results.

## Data and model limits

Execution requires raw prices. Tencent's unadjusted endpoint serves A-shares/HK;
raw-capable providers such as Sina serve US stocks. These paths are separate
from adjusted backtest feeds. Adjusted/unknown price calibers are rejected.
A-shares also require an available, complete ClickHouse trading calendar;
missing dates return `PAPER_DATA_NOT_READY`, not guessed holidays. ClickHouse
historical bars retain priority on overlapping dates.

## Optional ClickHouse configuration for A-shares

Use an existing data source that you maintain. The application does not create
tables, import market history, seed a calendar, or replace missing trading days
with weekdays. Put these settings in the same local `.env` used by the server
and restart the backend after changing them:

```dotenv
CLICKHOUSE_HOST=http://127.0.0.1
CLICKHOUSE_PORT=8123
CLICKHOUSE_USER=paper_reader
CLICKHOUSE_PASSWORD=
CLICKHOUSE_DATABASE=ashare
CLICKHOUSE_TIMEOUT_SECONDS=10
```

The host is **empty by default**, so an unconfigured installation makes no
ClickHouse connection. Use your own read-only account and password; the example
does not provision that account. HTTPS is supported. The host must contain no
URL credentials, path, query or fragment; credentials are sent separately. All
queries use bound parameters and request ClickHouse `readonly=1`.

The configured database must expose the tables/columns below (compatible views
are also suitable):

| Table | Required columns | Data contract |
| --- | --- | --- |
| `trade_calendar` | `exchange`, `cal_date`, `is_open`, `pretrade_date` | Explicit rows for **every calendar date** in the settlement range, including closed days with `is_open=0`. `cal_date` may be a Date, `YYYY-MM-DD`, or `YYYYMMDD`. Only A-share exchanges SSE/SZSE/BSE are read; one complete exchange is sufficient, but duplicate dates across exchanges must agree. |
| `stk_factor_pro` | `ts_code`, `trade_date`, `open`, `high`, `low`, `close`, `vol` | Unadjusted execution OHLC, never `close_hfq` or a qfq series. The historical leg takes priority where it overlaps the raw network feed. |
| `stk_limit` | `ts_code`, `trade_date`, `up_limit`, `down_limit` | Observed daily price bands. A missing row uses the disclosed board/ST heuristic only when a valid previous close exists. |
| `stk_st_daily` | `ts_code`, `trade_date`, `type`, `type_name` | Risk-warning rows for the specific symbol and date. |
| `stk_suspend` | `ts_code`, `trade_date`, `suspend_type`, `suspend_timing` | Suspension/resumption observations (`S`/`R`). |
| `stk_limit_list_d` | `ts_code`, `trade_date`, `limit` | Optional per-date lock-side rows (`U`/`D`/`Z`); the column is quoted by the query. |

For the price and limit tables, `trade_date` must be a ClickHouse Date or an ISO
`YYYY-MM-DD` string because their predicates bind ISO dates. Compact `YYYYMMDD`
is supported for `trade_calendar.cal_date` only. Numeric columns must have their
documented raw-price meaning and contain finite values; the connector does not
infer adjustment from a price series.

Missing/unreachable configuration, incomplete or contradictory calendar rows,
unavailable execution prices and unresolved split-like discontinuities do not
advance a saved account or consume its revision. Fix the source and retry from
the freshly read account state. Empty event tables and missing event rows must
mean the absence of that event, not an unimported period; unavailable tables or
query errors must be repaired rather than populated with invented rows.

Price/volume ratios cannot establish a split. Split-like discontinuities return
`PAPER_DATA_NOT_READY` without multiplying holdings. Other unresolved
discontinuities reject affected orders and disclose the issue. Automatic action
reconciliation and cash dividends are not implemented. NAV is not total return.
A-shares are included in the raw-price discontinuity checks.

Matching is daily close, all-or-none, with simplified fees, price limits, board
lots and T+1 checks. HK lot fallback is disclosed. Invalid OHLC prevents fills;
valuation uses a finite prior close or the disclosed average-cost fallback.
This is not intraday or broker-execution simulation. The 5-minute, settlement,
margin, options and cross-market phases in #1938 remain follow-up work.
