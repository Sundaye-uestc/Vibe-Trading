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

Use a data source that you maintain. The runtime does not automatically create
tables, import market history, seed a calendar, or replace missing trading days
with weekdays. An explicit local import workflow is documented below. Put these settings in the same local `.env` used by the server
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
The connector also sets `max_execution_time` to its configured timeout. A
strict read-only server profile must allow that setting to change within a
bounded range. The local helper keeps `readonly=1` and SELECT-only grants,
allowing only `max_execution_time` in 1–120 seconds via
`changeable_in_readonly`; other resource and write settings remain locked.

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

## Explicit local BaoStock import

### Daily updates from the Web page

For A-share accounts, **Update A-share data** lets you save 1–10 SH/SZ symbols,
read their coverage and missing dates, then explicitly update completed daily
data. Saving a watchlist does not fetch data. New symbols receive a 200-calendar-day
lookback; subsequent updates fetch from the first missing date through the
completed-date boundary. The boundary is 15:30 Asia/Shanghai, but a provider may
publish later: incomplete source data leaves the previous snapshots available.
Separate complete snapshots may jointly cover a range; gaps are never inferred
from only the earliest/latest dates.

Enable updates with `VIBE_TRADING_PAPER_WRITER_ENV` in the server's local `.env`,
pointing to an absolute private dotenv file (mode `0600` on Unix). It uses the
same `CLICKHOUSE_INGEST_*` keys shown below and must match the reader's server
and database. Use a separate account with SELECT on the managed database and
INSERT only on `paper_market_snapshots`; the Web update path verifies existing
schema and never creates tables. Do not reuse the admin account. In this local
deployment, the file is `.vibe-dev/clickhouse/manual-update.env`; its reader and
admin settings remain separate.

`GET /api/paper-data` reads local coverage, `PUT /api/paper-data/watchlist` saves
the list, and `POST /api/paper-data/update` with `{}` performs one explicit update.
Both writes require the existing authenticated/local write gate and browser
origin checks. The browser cannot supply credentials, destination, file path or
arbitrary import dates. A persistent cross-process lease serializes updates and
watchlist changes. All source snapshots validate before one bounded synchronous
batch insert. If a response is lost, **Refresh status** reads the actual coverage;
a timeout does not imply the database rolled back. The UI does not automatically
retry a write. No scheduled market-data refresh is enabled by this feature.

Accounts created in the Web UI still begin on their creation date or next
session. Refreshing data does not settle an account: choose an eligible completed
date and submit orders (or an empty list for valuation) separately. The saved
revision protects against duplicate settlement.

### Command-line imports

Use `scripts/import-paper-data.py` from the checkout's Python environment to
fetch and validate a bounded snapshot before publishing it to a dedicated local
ClickHouse database (`vibe_paper`, or a `paper_*` database). This operator command
is separate from the agent's SELECT-only reader connection; it is never invoked
by a model tool. It refuses unrelated existing tables and incompatible managed
schema signatures and does not drop or truncate data.

```bash
.venv/bin/python scripts/import-paper-data.py \
  --symbols 600522.SH --start 2026-04-01 --end 2026-10-28 \
  --output .vibe-dev/data/600522-20261028.json

.venv/bin/python scripts/import-paper-data.py \
  --snapshot .vibe-dev/data/600522-20261028.json \
  --writer-env .vibe-dev/clickhouse/ingest.env
```

The first command only downloads evidence. The second validates that saved
snapshot again and publishes it. Alternatively include `--writer-env` on the
first command. Output files are never overwritten. Use a private dotenv file
for these writer-only variables; do not put credentials in command arguments:

```dotenv
CLICKHOUSE_INGEST_URL=http://127.0.0.1:8123
CLICKHOUSE_INGEST_USER=your_local_ingest_user
CLICKHOUSE_INGEST_PASSWORD=your_local_ingest_password
CLICKHOUSE_INGEST_DATABASE=vibe_paper
```

The writer account needs CREATE/SELECT/INSERT privileges in its dedicated
database and metadata reads. Give the application's separate account only
SELECT. The local deployment's credentials and downloaded snapshots belong in
ignored runtime paths, never in Git. This script does not provision accounts.

Each fetch accepts 1–10 SH/SZ symbols and at most 366 calendar days. BaoStock's
`query_trade_dates` supplies explicit open/closed rows (including weekends and
holidays); every date in an additional 31-day calendar lookback must also be
explicit. The complete source calendar is saved as `calendar_evidence`, and
validation derives the previous trading date from that evidence rather than
trusting the compatibility view's claimed previous date. `query_history_k_data_plus` is requested with
`adjustflag=3`, `tradestatus` and `isST`. Every open date must have a complete
raw-price/status row for every requested symbol; omissions and ambiguous or
invalid rows reject the import. The end date must have completed by 15:30
Asia/Shanghai at observation time. Volume is retained in shares and converted
to board lots for the execution view. No adjusted-price reconstruction is used.
See the [BaoStock API documentation](https://www.baostock.com/) for source field
semantics; the stored snapshot also records its source URL and observation time.

Publication is one atomic schema-v2 JSON envelope in `paper_market_snapshots`,
identified by a SHA-256 of its data and normalized UTC observation timestamp. Calendar, price, status and coverage views derive from
that envelope. Re-importing the same saved snapshot is idempotent; failed uploads do not replace
prior snapshots. A new source observation has a distinct identity even if its
values match an older observation. Overlapping imports select the newest source
observation time (normalized to UTC), using publish order only to break ties.
An older file imported later cannot replace a newer correction. Price and
status stay together in the selected row. Schema-v1 snapshots lacking the
original calendar evidence are excluded by v2 views; the importer rejects them
and refuses to silently migrate an older or unknown database schema. Preserve
old evidence files, explicitly review any required view/schema migration and
fetch a new v2 snapshot instead of fabricating missing source evidence.

The views `paper_daily_status` and `paper_data_coverage` mark this as a managed
source. Explicit `is_st=0` / `is_suspended=0` rows distinguish observed normal
trading from absent event imports. The reader cross-checks those rows with the
calendar, raw bars and derived event views. It skips the network price leg only
when verified coverage includes the full requested lookback (200 calendar days
before the settlement start). Import enough history for this condition; a
shorter snapshot must not be presented as complete longer-range history.

BaoStock does not supply the exchange's daily limit-price or lock-side event
observations in this workflow. Their coverage is explicitly `unavailable` and
the compatibility views remain empty. The existing board/ST limit heuristic is
used with its original provenance warning; no rows are invented to make the
preflight green. The initial 600522.SH snapshot through 2026-10-28 covers only
that symbol and period. Later dates or other symbols require a fresh explicit
import; nothing here schedules data refreshes.

### Local server lifecycle

On macOS Apple Silicon, the opt-in helper downloads a pinned official ClickHouse
binary, verifies its digest and starts a loopback-only instance:

```bash
python3 scripts/clickhouse-local start
python3 scripts/clickhouse-local status
python3 scripts/clickhouse-local stop
```

It preserves the database on stop and does not install a login/startup agent.
State, logs and private accounts remain under `.vibe-dev/clickhouse/`. The current
local setup also has private `ingest.env` (writer) and `readonly.env` (application
reader) files there. The lifecycle helper owns `credentials.json`, `config.xml`
and `users.xml`; preserve them together with the data directory.

To reconnect the application, copy only the `CLICKHOUSE_*` reader settings from
`readonly.env` into the application's active local `.env` (normally
`~/.vibe-trading/.env`), preserving unrelated settings, then restart the backend.
Do not use the admin/writer account for application queries. The helper never
edits the application's `.env` automatically. On other operating systems,
provision a supported ClickHouse server separately and use the same importer and
read-only configuration contract.
