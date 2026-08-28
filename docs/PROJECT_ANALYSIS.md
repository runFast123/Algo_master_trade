# How this project works — a read-through

Written 21 August 2026, from the code rather than from the other documents in
`docs/`. Where this disagrees with an older document, the code is what I read.

The purpose here is orientation: what the thing is, what happens when it runs,
which decisions are load-bearing, and where the real edges are.

---

## 1. What it is, in one paragraph

A **desktop algorithmic-trading platform for Indian retail equities**, built on
the Choice FINX OpenAPI (a brokerage). A user runs a single Windows executable;
it starts a local web UI and a local API, connects to their own Choice account,
and lets them watch a portfolio, place orders by hand, author strategies in a
JSON DSL, backtest them against historical bars, and run them on paper against
live prices. Live (real-money) automated runs are supported by the engine but
**deliberately not reachable from the API** — they are blocked on regulatory
items, not on code.

Everything is local-first: the database, the credentials and the strategy
history stay on the user's machine.

---

## 2. Layout

Five deployable pieces plus docs. Line counts are the actual files.

| Tree | Lines | What it is |
|---|---|---|
| `engine/` | ~4,700 | The domain layer: Choice gateway, DSL, backtester, live runner, risk manager, costs. **No HTTP surface at all.** |
| `backend/` | ~3,000 | FastAPI: auth, REST routes, SQLAlchemy models, services. Imports `engine` in-process. |
| `frontend-user/index.html` | 5,352 | The entire trader UI — one file, no build step, no framework. |
| `frontend-admin/index.html` | 1,292 | Operator console. |
| `client-desktop/` | ~950 | Launcher, local server, `/api` proxy, licence client, PyInstaller build. |
| `licence-server/` | ~300 | Optional Vercel serverless activation service. |
| `frontend-user/verify_ui.py` | 1,674 | Headless-Edge UI test harness (not shipped in the binary). |
| `docs/` | ~4,500 | Architecture, roadmap, three audit write-ups, API-gap survey. |

`kkunal-1.2.0.tar.gz` at the root is the vendor's Choice SDK, importable as
`choice_api`. It is a dependency, not part of this codebase — and several of the
fixes in `docs/AUDIT_2026_08_18.md` exist specifically to work *around* it.

### The one structural rule worth knowing

`engine/` never imports `backend/`, and `engine/` has no routes. The backend
calls engine functions directly in-process. There is no second HTTP hop, no
message queue, no worker pool. At desktop scale that is correct, and it is why
the whole thing fits in one executable.

Both `client-desktop/app/` and `backend/app/` define a top-level package called
`app`. That collision is real, and it is the reason the backend runs as a
**separate process** rather than being imported — see the docstring at
[cli.py:1](client-desktop/app/cli.py:1). It is a packaging constraint that
became an architectural one.

---

## 3. What happens when the user double-clicks the exe

```
ChoiceFinxTrader.exe
  │
  ├─ check_licence_or_exit()            # no-op unless a licence URL was baked in
  ├─ find_available_port() ×2           # 9000 (UI) and 8080 (backend), or next free
  ├─ start_backend()                    # subprocess: `sys.executable --run-backend 8080`
  │     └─ uvicorn → backend/app/main.py
  │           ├─ sync_schema()          # create/patch SQLite tables at import
  │           └─ recover_orphaned_runs()# RUNNING rows from a killed process → INTERRUPTED
  ├─ wait_for_backend()                 # poll /health up to 60s
  ├─ open_browser_when_ready()          # default browser → http://127.0.0.1:9000
  └─ uvicorn on 127.0.0.1:9000          # local_server.py
        ├─  /            → bundled frontend-user/index.html
        ├─  /admin       → frontend-admin/index.html
        └─  /api/{path}  → proxied to 127.0.0.1:8080
```

Two servers, two ports, one process tree. The UI is served same-origin with the
API, which is why `CORS_ORIGINS` is empty by default — there is no cross-origin
request to allow.

The proxy exists purely so the browser sees one origin. It opens a TCP
connection per call, which `docs/SYSTEM_REVIEW.md` correctly flags as measurable
overhead for a polling UI, and correctly declines to call a correctness fault.

---

## 4. Two authentication layers that are deliberately not the same thing

This is the first thing to internalise, because conflating them would be a
security hole and the code goes out of its way to keep them apart.

**Layer 1 — the platform account.** Email + bcrypt password → HS256 JWT, 8-hour
expiry. Multi-tenant: every row carries a `tenant_id` and every query filters on
it. `get_current_user` re-reads the tenant **from the database rather than
trusting the token's claim** ([dependencies.py](backend/app/dependencies.py)).

The signing key is worth a note. `resolved_secret_key()` prefers `SECRET_KEY`
from the environment (required, ≥32 chars, in production), and otherwise
generates an install-level key **once** into `user_config_dir()/secret.key`.
Before that, the desktop build minted a random key per process, so every restart
invalidated every browser token — users were "signed out every time".

**Layer 2 — the Choice broker session.** Entirely separate. A `ChoiceSession`
lives in an in-memory `ChoiceSessionRegistry` keyed by user id, 8-hour idle TTL,
**never shared between users**. Four ways to establish one:

1. `connect` — one-shot TOTP login
2. `request-otp` → `validate-otp` — the three-step flow
3. Partner OAuth — `oauth/start` issues a single-use state bound to the user;
   `oauth/callback` consumes it and AES-decrypts `cid`/`sid`/`accessToken`
4. `restore()` — from the encrypted on-disk store, if the user opted in

A Choice API key is **never** platform credentials, and a platform login never
implies a broker connection. `GET /auth/choice/status` reports the two
independently, and the UI has separate affordances for each.

---

## 5. Session mode — the single most load-bearing decision in the codebase

`SessionMode` is a four-value enum on the session, and it is **authoritative**.
Nothing anywhere infers behaviour from "do we have a client object" or "are we
connected".

| Mode | Market data | Fills | `uses_broker_data` | `sends_real_orders` |
|---|---|---|---|---|
| `DISCONNECTED` | none | none | ✗ | ✗ |
| `DEMO` | sandbox fixtures | simulated locally | ✗ | ✗ |
| `PAPER` | **real, from Choice** | simulated locally | ✓ | ✗ |
| `LIVE` | real | **real orders** | ✓ | ✓ |

Two derived properties carry the whole design:

- `uses_broker_data` — "may I read Choice's market data?" True for PAPER and LIVE.
- `sends_real_orders` — "may money move?" **True for LIVE only.**

Gating on the wrong one is a recurring bug class in this repo's history, and
both directions have bitten: the price feed gated on `is_live` and so refused to
start in PAPER (the mode that most needs it), and the earlier design inferred
"live" from the presence of credentials, which let a sandbox session reach the
real order endpoint.

Mode changes go through `AuthService.set_mode`, which is deliberately asymmetric:
**to paper is immediate** (reducing what a session may do should never require
finding a credential), **to live requires an explicit `confirm: true`** in the
request body. A DEMO session can never become LIVE at all — there is no Choice
login behind it, so "live" would mean orders with nowhere to go.

The mode is persisted on change, not only on login. Without that, switching
LIVE→PAPER left `"LIVE"` on disk, and since `restore()` is reached automatically
from the status endpoint the UI calls on load, a user who chose paper could be
put silently back into live — bypassing the confirmation that guards exactly
that direction.

---

## 6. Session persistence

Opt-in ("remember me"). [session_store.py](engine/app/choice_gateway/session_store.py)
writes Fernet-encrypted records to `%LOCALAPPDATA%\ChoiceFinxTrader\sessions.json`.

- Stored: `SessionId`, environment, mode, vendor id, base url.
- **Never stored: the API key, the password, anything about orders.**
- 8-hour cap, because a stored session is worthless once Choice's own has lapsed.
- Fernet signs as well as encrypts, so a tampered record fails to decrypt rather
  than yielding a plausible-looking session id.
- On load, `restore()` **validates against Choice** with a real `get_funds_view_new()`
  call before reporting connected.

The module's own docstring is honest about the threat model: the key lives on the
same machine as the ciphertext, so this defeats a file read or a backup scraper,
not someone at the keyboard. The cipher is isolated in one module precisely so
DPAPI can replace it later.

There is a nice piece of reasoning in `_local_key()`: deriving the store key from
the backend's signing key looked safer and was worse, because the desktop build
generates that key per process — each restart would have been unable to read
what the last one wrote, which is silently identical to not persisting at all.

---

## 7. The order path

One path, whether the order came from a human clicking Buy or from a strategy.

```
POST /api/v1/orders/
  │
  ├─ Pydantic bounds on the request                    (schemas/order.py)
  ├─ get_choice_session()  → 409 if not connected      (dependencies.py)
  │
  └─ OrderService.place_order                          (services/order_service.py)
        ├─ write Order row, status SUBMITTED           ← recorded BEFORE the call
        │
        ├─ orders_gateway.validate_order
        │     ├─ side / order type / product allowlists
        │     ├─ reference price (quoted, for market orders)
        │     └─ risk_manager.validate_order
        │           ├─ halt breaker?
        │           ├─ notional ≤ ₹5,00,000
        │           ├─ quantity ≤ 100,000
        │           └─ daily loss < ₹1,00,000
        │
        ├─ token bucket: 10 orders/sec                 (SEBI cap)
        │
        ├─ DEMO / PAPER → _simulate_order              ← never touches the network
        │     └─ fill price from a real quote, or REFUSE
        │
        └─ LIVE → POST api/OpenAPI/V2/NewOrder
              └─ own client_order_no (ms timestamp + counter)
        │
        └─ row → REJECTED (+ reason) | SIMULATED | ACCEPTED   + audit log
```

Details in that path that are there for a reason:

- **The row is written before the broker call.** A crash mid-flight leaves
  evidence, not a gap.
- **Own client order numbers.** The SDK hardcodes `"ClientOrderNo": 123456` on
  every order. Cancellation finds an order *by that field*, so two live orders
  meant cancelling one could withdraw the other. Placement bypasses the SDK's
  helper and builds its own payload.
- **`_market_fill_price` refuses rather than invents.** If no quote can be
  obtained, a simulated market order is rejected. The earlier version fell back
  to fixture prices, and against a live account those were 38%–89% wrong
  (RELIANCE at 2504.50 when Choice's own record said 1324.10). A paper fill at a
  fiction is worse than no paper fill, because it looks like a working system.
- **Only GET is retried.** Choice's POSTs carry no idempotency key, so a read
  timeout on `NewOrder` retried is a duplicate position, and on `ProcessPayout` a
  duplicate withdrawal. A timeout means *"we do not know"*, not *"it failed"*.
- **`TERMINAL_STATUSES` is an inverted allowlist.** An unrecognised broker status
  is assumed *still working*. Trying to cancel a finished order fails loudly;
  skipping a live one does so silently. The live order book has only ever come
  back empty on this account, so the status strings are genuinely unverified.
- **`cancel_one` matches any of three identifiers and refuses when more than one
  row matches.** Ambiguity is reported, never resolved by guessing.
- **Amend and cancel write back to the local row.** The UI, the CSV export and
  reconciliation all read the local book; leaving it stale meant an order amended
  from 10 @ 2400 to 200 @ 2500 reported 10 @ 2400 forever.

`PRICE_UNIT_DIVISOR = 100.0` in `order_service.py` is the standing open question
(**OPEN-2**): whether Choice's `NewOrder` wants paise or rupees is unconfirmed.
Note that `/orders/preview` deliberately does *not* use that constant — paisa to
rupees for display is always /100, and conflating a display conversion with a
protocol one is what inflated a portfolio 100×.

---

## 8. Risk management

[risk_manager.py](engine/app/strategy_engine/risk_manager.py), a module-level
singleton keyed by `owner_key` (the user id).

- **Token bucket, 10 orders/sec** — the SEBI/NSE cap for unregistered strategies.
- **Per-order notional ₹5,00,000** and **quantity 100,000**.
- **Daily realised-loss cap ₹1,00,000**, with `_roll_day()` clearing at midnight.
- **Two separate ledgers**, `_realized` and `_simulated`. This matters: without
  it, a paper strategy losing money would halt real trading.
- **Halt has a `scope`.** `scope="all"` is the kill switch and stops everything;
  `scope="real"` is what a real-money loss-cap breach triggers, and it leaves
  paper work running. Neither is clearable through the API — a breaker you can
  reset from the thing that tripped it is not a breaker.

`POST /orders/halt` applies the halt **first**, then attempts cancellation, then
returns even if cancellation failed. Stopping new orders must not depend on the
broker answering. `cancel_all_open` attempts each order independently so one
refusal cannot block the rest, and it now raises on a `{"Status":"Fail"}`
order-book envelope rather than reading it as "no working orders" — the one
moment the platform must not be optimistic.

There is a per-tenant force-halt for operators at `POST /admin/tenants/{id}/halt`.

`_realized` is fed by `refresh_realized_pnl()` after every real fill. Before
that, its only writer was the funds endpoint the dashboard polls — so the
circuit breaker advertised on the Health panel depended on somebody having the
page open.

---

## 9. Market data — and what is actually available

This is where the product's real constraint lives, and it is not a code problem.

Diagnostics against the live account (Client ID M09984, PROD, 18 Aug 2026):

| Endpoint | Result |
|---|---|
| FundsViewNew / FundsView | works |
| Holdings | works — 33 rows with `LTP`, `ClosePrice`, `PriceDivisor`, `MarketLot` |
| NetPosition, MarketStatus, OrderBook, TradeBook, UserProfile | work |
| **MultipleTouchline** | **fails** — `Index was outside the bounds of the array.` |
| **HistoricalData** | **fails** — `Choice rejected the session.` |
| DISStatus | `No data found` |
| Broadcast price feed | address supplied — `brd.choiceindia.co.in:4520` |

The two failures are the two things a trading platform most needs. Both have
workarounds in the code, and both workarounds are honest about what they are:

- **Quotes** fall back to pricing from the holdings/positions snapshot, tagged
  `source: "holdings_snapshot"` so nothing downstream mistakes it for a live
  tick. Rows Choice never priced (`priced_from_close`) are excluded — publishing
  those asserts the instrument traded and finished exactly flat. Anything the
  account does not hold cannot be priced, and the call **refuses**.
- **History** falls back to Marketstack v2 (`AUTO` provider), with pagination
  through `offset` because one call caps at 1000 rows — a 5-minute backtest over
  a month was silently measuring about thirteen trading days.

Two subtleties in that fallback worth remembering:

- `scaled_price()` is mandatory on every Choice price. Prices arrive as scaled
  integers with `PriceDivisor` alongside. Reading one directly is what displayed
  a portfolio a hundredfold too large.
- Marketstack resolves a bare `INFY` to the **New York ADR at ~$11.62**, not
  Infosys at ₹1,139.90 — and `IEX` to Idex Corporation, a different company. The
  bare-ticker candidate was removed and `_foreign_listing()` skips any non-INR
  match.

Market hours are IST 09:15–15:30 **with a trading-holiday calendar**, and
`get_market_status` says *why* it is shut — "Closed" alone reads identically at
4pm, on a Sunday, and on Republic Day.

### The price feed socket

`sockets_pricefeed.py` (170 lines) wraps Choice's Odin broadcast feed —
FIX3.0 pipe-delimited, zlib-compressed, over a WebSocket, on a different host
and protocol from the REST API. Choice **did** hand out a feed address for this
account, which is evidence of entitlement.

It has **no callers**. Nothing consumes ticks; the paper scheduler polls REST
instead. This is the highest-value unbuilt item in the repo, because one answer
resolves three separate problems: paper trading blocked by the dead REST quote
endpoint, "I have to refresh the page again and again", and API load from
polling. `subscribe_best_five` (5-level depth) is also wrapped and unused.

`sockets_interactive.py` — which pushes order and trade confirmations — is in the
same state: complete, and with no backend route or UI consumer.

---

## 10. The strategy DSL

A JSON document with four keys:

```json
{
  "indicators": { "rsi": {"type": "RSI", "length": 14},
                  "sma20": {"type": "SMA", "length": 20} },
  "entry_conditions": [
    {"any": [ {"field": "rsi",   "operator": "<", "value": 30},
              {"field": "close", "operator": "crosses_above", "value": "sma20"} ]}
  ],
  "exit_conditions": [ {"field": "rsi", "operator": ">", "value": 70} ],
  "actions": { "buy_qty": 10, "target_pct": 3, "stop_loss_pct": 1.5 }
}
```

Eight indicators: SMA, EMA, RSI, BOLLINGER, MACD, ATR, VWAP, STOCH. Six
comparisons plus `crosses_above` / `crosses_below`. Conditions nest through
`any` (OR) and `all` (AND) groups, so "A and (B or C)" is *written* rather than
inferred from precedence rules that would have to be invented.

Three rules run through [dsl.py](engine/app/strategy_engine/dsl.py):

1. **An unknown indicator or a misspelt field is an error, not a silent no-op.**
   Previously an unknown indicator got assigned the close price and a misspelt
   field evaluated `False` forever — so a broken strategy looked exactly like a
   strategy that simply never traded.
2. **`validate()` recurses into groups.** A misspelt field nested inside an `any`
   would otherwise pass at save time and fail at run time, which is the failure
   the validator exists to prevent.
3. **An empty condition list, and an empty group, never fire.** A strategy that
   specifies nothing should not trade on everything.

RSI uses Wilder's smoothing (the charting-platform standard; a rolling mean gives
materially different values and would disagree with the chart the trader is
reading). The flat-series case returns **50**, not 0 — the `.where` clauses were
ordered so a series with neither gains nor losses read as maximally oversold, and
`rsi < 30` is the most common strategy in this product, so it fired on every flat
bar. A stale repeated price produces precisely such a series.

`explain.py` renders any definition back as an English sentence, generated by the
engine that runs it, so the description cannot drift from the behaviour.
`POST /strategies/preview` validates and explains a draft **without saving** — a
draft in progress is expected to be invalid, so that is reported as a state
rather than rejected.

---

## 11. Backtesting

[backtest_runner.py](engine/app/strategy_engine/backtest_runner.py):

- Resolve the instrument, fetch OHLCV, compute indicators over the whole frame.
- **Fills at the next bar's open**, not the signal bar's close. Signalling and
  filling on the same close is the standard way to backtest a return that cannot
  be achieved.
- Slippage (5 bps, against the trader) and the **full Indian retail cost model**
  on every leg: brokerage 0.03% capped ₹20, STT, exchange 0.00345%, SEBI, stamp
  duty (buy side only), GST 18% on brokerage+exchange.
- `CostModel.for_product()` picks the right schedule — intraday is a *different*
  schedule, not a discount: STT on the sell leg only and at a quarter the rate.
- Target and stop checked on close; open position liquidated at the final close.
- Metrics: win rate, profit factor, Sharpe, max drawdown **and its duration in
  bars**, plus `_period_consistency` over four segments.
- `verdict.py` writes a past-tense plain-language summary from the computed
  metrics only, flags thin samples, and states its own limitations.

Two honesty features worth calling out:

- **Provenance travels with the result.** Every run records `data_source`
  (`CHOICE_OPENAPI`, `MARKETSTACK`, `SANDBOX_SYNTHETIC`) *and the window actually
  covered*, and the UI states it above the metrics. A 3.6-year request that
  returned 1.0 year used to report success with Sharpe and drawdown computed over
  a period nobody asked for.
- **Period consistency is explicitly not called walk-forward.** There is no
  parameter search here, so nothing is fitted on one stretch and tested on the
  next; the card says so on screen. Naming it walk-forward would claim a rigour
  this does not have.

---

## 12. Paper runs

`POST /strategies/{id}/start` → [paper_run_service.py](backend/app/services/paper_run_service.py).

Before anything starts: refuse sandbox sessions (a paper run needs *real* data),
validate the timeframe, enforce one live run per strategy, and — the important
one — **fetch a live quote for the specific instrument** and refuse with a 409 if
it cannot be priced. A strategy with no prices cannot decide anything, and
discovering that after it is "running" is worse than refusing.

Then `PaperRunScheduler` starts one daemon thread per run:

- Poll touchline every **5 seconds**.
- `_BarBuilder` aggregates ticks into wall-clock-aligned OHLCV bars
  (`int(at // seconds)`), emitting a bar only when its bucket closes.
- Closed bar → `runner.on_bar` → indicators → entry/exit → `_submit`.
- Every tick → `runner.on_tick` → target/stop monitoring.

**Three halt conditions, all failing safe:**

| Trigger | Why |
|---|---|
| No market data at start | refuses to start at all |
| 3 consecutive feed failures | a strategy trading on stale prices is worse than one stopped |
| `ChoiceCredentialExpired` | immediately — retrying cannot revive an expired key |

The halt reason is recorded *before* the state change, so the UI never shows
"HALTED" with no explanation.

Two things the PAPER branch of `LiveStrategyRunner._submit` gets right, both
after being wrong:

- It calls `orders_gateway.validate_order(simulated=True)`. It used to build its
  fill dict directly, so it never reached the risk manager: the kill switch
  stopped manual paper orders while a paper strategy carried on trading, and
  neither the loss cap nor the notional cap applied to a strategy's own orders.
- It applies the **same `CostModel` the backtest does**. It used to book
  `(price − entry) × qty` with no slippage and no charges. On 200 round trips of
  100 shares at ₹500 that is ~₹39,400 of costs the paper run never subtracted —
  enough to turn a losing strategy into a winning one on screen, which is the
  number someone would promote to live on.

`recover_orphaned_runs()` at startup marks stale `RUNNING` rows `INTERRUPTED`.
The desktop app *will* be closed mid-run, and a run that silently dies leaving a
phantom position is worse than one that refuses to start.

**LIVE runs cannot be started from the API.** The runner supports the mode;
`paper_run_service` only ever creates PAPER runs.

---

## 13. Data model

SQLite by default (`%LOCALAPPDATA%\ChoiceFinxTrader\algo.db` when frozen,
`algo.db` at the repo root from source), swappable to Postgres via
`DATABASE_URL`. `BaseModel` gives every table a UUID string id and timestamps.

```
Tenant ──< User ──< Strategy ──< StrategyRun ──< Order
                                                  ↑
                                    (manual orders: run_id NULL)
AuditLog  (actor_id, tenant_id, action, entity_type, entity_id, details)
```

Six tables. `sync_schema()` runs at import and patches missing columns, so there
is no Alembic and no migration step for a desktop user.

The audit log is the compliance artefact: logins, registrations, broker
connections, mode changes, every order placed/modified/cancelled/rejected,
halts, tenant halts, position conversions, and money movement — including
`FUNDS_WITHDRAWAL_REFUSED` on the failure path, because a run of refusals is
what a compromised token looks like. Choice's Integration Guide §12 requires
five years of retention, which the app records but cannot itself enforce.

The two-databases problem is real and handled explicitly: the executable and a
source checkout use different files, so an account created in one is genuinely
absent from the other. The sign-in failure message **names which database was
consulted** while staying identical whether the account is missing or the
password is wrong.

---

## 14. The frontend

One 5,352-line HTML file. No framework, no bundler, no npm. Charts are
hand-rolled SVG (`barChartH`, `lineChart`, `histogram`, `divergingBarH`).

Five views: **Overview** (KPIs, allocation, movers, recent orders), **Positions**
(holdings, positions, order log, working orders, exports), **Strategy** (visual
builder + JSON view + run monitor), **Backtest**, **Health** (broker status,
diagnostics, reconciliation, account card, market status).

Polling, not push: `autoRefreshTick` on a user-settable interval, with terminal
runs answered from their last result rather than re-fetched forever (six stopped
runs used to cost 72 requests a minute for news that could not come).

Three UI details that are load-bearing rather than cosmetic:

- **`jsq()` vs `esc()`.** `esc()` escapes for HTML, but the parser decodes an
  attribute *before* the handler is compiled as JavaScript — so an escaped quote
  came back live and closed the string. A strategy named `Kunal's momentum`
  silently disabled every button on its row, and strategy names are free user
  input. `jsq()` escapes for JavaScript first, then for HTML.
- **`canAmend()` withholds the Amend action** when the order book does not report
  enough of the order to resend it unchanged. The defaults it replaced
  (`RL_LIMIT`, `CNC`) meant a user changing only the quantity could **delete
  their own stop-loss**, or convert an intraday position to delivery.
- **The instrument's own segment wins** over the dropdown. A token is unique only
  *within* a segment: 2885 in segment 1 is Reliance equity, in segment 2 an
  unrelated derivative.

The fat-finger confirmation states the order value in words, and when no price
can be established at all it questions the *quantity* instead of silently
checking nothing.

`verify_ui.py` drives all of this in headless Edge — 392 checks at last count —
and runs `node --check` over each interpolated JS harness, after an escape
collapse once made a whole test phase silently unparseable and gave two false
all-clears.

---

## 15. Licensing and distribution

Licensing is **off** unless a URL was baked in at build time
(`build_exe.py --licence-server URL` writes a generated `_build.py`). When on:

- `install_id()` is a random UUID, **not machine-derived** — no hardware
  fingerprint.
- Three states: active / revoked / unreachable, with a **7-day grace period**.
- `check()` never raises, and the heartbeat **never stops a running app
  mid-session**. It reports; it does not enforce.

The licence server itself is a small Vercel serverless app.

Build: PyInstaller one-file exe (`ChoiceFinxTrader.spec`), followed by
`verify_exe.py` — 45 checks against the *compiled binary*, not the source. That
harness earned its place: `sync_ui()` was copying `verify_ui.py` into the
shipped bundle, compiling the test tooling into the product.

The binary is **unsigned**, so downloads raise SmartScreen. That is on the
compliance checklist as `failing`, not hidden.

---

## 16. Where it actually stands

### Works, end to end

Platform auth and multi-tenancy · Choice connection by all four methods ·
encrypted session persistence · portfolio, positions, funds with derived
analytics · manual orders (market/limit/SL/SL-limit, CNC/MIS/NRML/CO/BO) with
amend, cancel, kill switch and reconciliation · instrument search over the full
scrip master with lot validation · pre-trade cost preview · money in and out ·
position conversion · the DSL with validation and English readback · backtesting
with provenance, costs, next-bar fills and a plain-language verdict · paper runs
with the three safe-halt rules and restart recovery · admin console with a
regulatory checklist · diagnostics that report field names and types only.

### Blocked on Choice, not on code

| Item | Blocks |
|---|---|
| `MultipleTouchline` returns a .NET array-bounds exception | live quotes; paper runs on anything not held |
| `HistoricalData` rejects the session | backtests from Choice (Marketstack covers it) |
| Price feed socket entitlement unconfirmed | push updates, market depth |
| `PRICE_UNIT_DIVISOR` unconfirmed (**OPEN-2**) | confident limit orders |
| UAT certification (**OPEN-3**) | production order access |
| Static-IP origination (**OPEN-1**) | live order flow from a desktop at all |
| Vendor empanelment | serving multiple Choice clients |
| Code signing | distribution without SmartScreen |

**OPEN-1 is architectural, not a checkbox.** A desktop build places orders from
each user's machine, which cannot satisfy a static-IP mandate. Satisfying it
means moving order flow to a server tier with the desktop proxying to it — and
the current topology already has the proxy seam where that would go.

### Genuine product gaps

- **Multi-leg / options.** The largest. The DSL is single-instrument and
  long-only. Needs an instrument master, greeks, margin modelling and multi-leg
  execution — and it changes the shape of the DSL, the order path *and* the risk
  manager. Deliberately not started halfway.
- **Short entries.** The paper book handles shorts correctly (signed arithmetic,
  reversals, partial closes) but no strategy can *open* one.
- **Push feed.** 5-second REST polling. Fine at bar granularity, not for
  scalping — and both sockets are already wrapped and waiting.
- **Walk-forward.** Period consistency is reported; nothing is fitted
  out-of-sample, so any verdict reflects one period.
- **Alerts beyond desktop notifications.** No email or Telegram.
- **Corporate actions.** Marketstack `splits`/`dividends` unused, so a split
  silently corrupts average price and return percentages.

---

## 17. The pattern behind the code

Reading the whole thing, the same instinct shows up in almost every module, and
it is worth naming because it explains choices that would otherwise look
paranoid:

> **A wrong number that looks plausible is worse than an error.**

It is the reason `_market_fill_price` refuses instead of substituting a price;
the reason a missing close is `None` and never `0`; the reason `get_lot_size`
returns `None` rather than `1` ("1" waves any quantity through, "unknown" can be
said out loud); the reason an unrecognised order status is assumed *open*; the
reason flat RSI is 50; the reason every backtest carries its provenance and the
window it actually covered; and the reason the compliance checklist reports
`"unknown"` instead of an unearned green tick.

The corollary shows up in the test strategy: nearly every audit finding in
`docs/AUDIT_2026_08_18.md` was a fault that **threw no exception and passed every
suite**. Hence mutation testing — remove the guard, and the suite must fail —
which caught five *vacuous* checks in a single session, each of which had been
reporting itself as verified.

Second pattern, narrower but just as consistent: **code that only runs against a
real broker needs a test that drives it with a fake one.** Three separate
`NameError`-class bugs sat in `cancel_all_open` and `reconcile` for weeks because
those functions short-circuit in DEMO and PAPER — they would have fired on a live
account, the first time anyone reached for the kill switch.

---

## 18. If you are about to change something

| You want to touch | Read first |
|---|---|
| Anything order-related | [orders.py](engine/app/choice_gateway/orders.py) (862 lines) then [order_service.py](backend/app/services/order_service.py) |
| Session, mode or auth | [client_manager.py](engine/app/choice_gateway/client_manager.py) (812 lines) |
| Limits or the kill switch | [risk_manager.py](engine/app/strategy_engine/risk_manager.py) |
| Strategy semantics | [dsl.py](engine/app/strategy_engine/dsl.py) then [explain.py](engine/app/strategy_engine/explain.py) — keep them in step |
| Anything about a price | [normalize.py](engine/app/choice_gateway/normalize.py) — `scaled_price` is not optional |
| Costs | [costs.py](engine/app/costs.py) — one model, shared by backtest, paper run and preview |
| The UI | [index.html](frontend-user/index.html), then run `verify_ui.py` |
| Anything shipped | `client-desktop/verify_exe.py` checks the binary, not the source |

The comments in this codebase are unusually load-bearing. Most of them are not
describing *what* the line does — they are recording a specific failure and why
the obvious alternative was rejected. Deleting one as noise throws away the only
record of a bug that cost real money to find.
