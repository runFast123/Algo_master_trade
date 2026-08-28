# Architecture — Choice FINX Algo Trading Platform

This document describes **what is built**, and separately what is planned. Every
section is marked so the plan can never be mistaken for the implementation.

Broker access is exclusively through Choice FINX OpenAPI via the `kkunal` /
`choice_api` Python SDK. No other module imports `choice_api`, and no frontend
ever sees a Choice endpoint or credential.

---

## 1. Components

| Component | Status | Role |
| :--- | :--- | :--- |
| `backend/` | Built | FastAPI REST API: platform auth, tenancy, strategies, orders, portfolio, admin |
| `engine/` | Built | Choice gateway, DSL interpreter, backtester, strategy runner, risk manager |
| `client-desktop/` | Built | Windows launcher: local UI server, API proxy, backend subprocess |
| `frontend-user/` | Built | Single-file trading interface |
| `frontend-admin/` | Built | Single-file admin dashboard |
| Event bus (Redis/Kafka) | Planned | Fan-out for multi-worker live trading |
| Celery/RQ workers | Planned | Out-of-process backtest and live-run execution |
| PostgreSQL + Alembic | Planned | Replaces SQLite for multi-user server deployments |

### How the engine is wired

The engine is a Python package imported **in process** by the backend, not a
separate network service. The desktop build depends on it: one backend process
serves everything.

There is deliberately no engine HTTP surface. An earlier build shipped one —
including unauthenticated `/choice/login-totp` and `/runs/backtest` routes on an
open port — and nothing ever started it. Broker authentication and order flow
are per user and belong behind the backend's authentication, so the whole
service was removed rather than left listening.

```
Desktop exe
  ├── local UI server (127.0.0.1:9000)  ── serves frontend-user, proxies /api/*
  └── backend subprocess (127.0.0.1:8080)
        └── engine package (in process)
              └── choice_api SDK ──► Choice FINX OpenAPI
```

---

## 2. Session model

**One Choice session per platform user.** `ChoiceSessionRegistry` in
`engine/app/choice_gateway/client_manager.py` holds sessions keyed by user id;
they expire after `CHOICE_SESSION_TTL_SECONDS` of inactivity.

There is deliberately no process-wide "current client". A shared broker session
would let any signed-in user read and trade another user's account, and the
`get_choice_session` dependency exists to make that impossible: a user who has
not connected gets HTTP 409, never someone else's data.

Each session carries a mode, and the mode is authoritative. It answers two
questions that are deliberately kept separate — where market data comes from,
and whether an order actually leaves the building:

| Mode | Market data | Orders |
| :--- | :--- | :--- |
| `DISCONNECTED` | none — endpoints return 409 | rejected |
| `DEMO` | local sandbox fixtures | filled locally |
| `PAPER` | real, from Choice | filled locally at the live traded price |
| `LIVE` | real, from Choice | submitted to Choice |

Nothing infers the mode from connection state. That inference was the reason a
sandbox session could previously submit a live order.

**PAPER is the default** when connecting a real account. A session that was not
explicitly asked to trade live cannot send an order: `simulates_orders` is true
for both DEMO and PAPER, and `place_order` branches on it before any client is
obtained. Two properties express the distinction and are exposed on the session
status so the UI never has to infer it:

* `uses_broker_data` — PAPER and LIVE
* `mode.sends_real_orders` — LIVE only

Paper fills are tracked in `paper_positions` with average-price accounting and
running realised P&L, reported separately from the account's real holdings.
The two are never summed: one is the position the account actually holds, the
other is what paper trading would have made.

### Session lifetime, and the opt-in to survive a restart

`ChoiceSessionRegistry` is a plain dict guarded by a lock. By default nothing is
written to disk, so **a restart drops every broker session** while the platform
JWT — signed and held in the browser — survives.

A user can opt in per machine with "Remember this broker session"
(`remember: true` on connect). `engine/app/choice_gateway/session_store.py` then
keeps the Choice `SessionId`, environment and mode in an encrypted file under
`%LOCALAPPDATA%\ChoiceFinxTrader\sessions.json`. **The API key is never
persisted.** On the next sign-in the stored id is *validated against Choice*
before the session is reported as connected: a stored id proves nothing, since
the key it was issued against expires roughly daily, and a stale id presenting
as live would be worse than asking for a reconnect.

The record is cleared on sign-out, on explicit disconnect, on an environment
change, after eight hours, and whenever its HMAC fails.

> **What the encryption is worth.** The key derives from `SECRET_KEY`, which on
> a desktop install sits in a `.env` beside the data. That defeats a casual file
> read or a backup scraper; it does not defeat anyone with the machine. It is
> obfuscation, not secrecy, and the cipher is isolated in one module precisely
> so it can be swapped for OS-level key storage (DPAPI) when the threat model
> demands it.

When a user has *not* opted in, the connection banner says why they are being
asked to reconnect rather than leaving a restart looking like a fault.
`expires_in_seconds` on the session status warns before an *idle* expiry; it
cannot predict a restart, which is what the opt-in is for.

### Credential failures

Choice distinguishes two rejections and they need different actions from the
user, so `backend/app/core/errors.py` keeps them apart:

| Choice says | Meaning | What the app tells the user |
| :--- | :--- | :--- |
| `VendorId Invalid or doesn't exists` | Client ID unknown in this environment | Check the Client ID, and whether UAT/PROD matches |
| `Token Expired` | Client ID valid, API key lapsed | Reissue the API key in the Choice portal |

An expired key raises `ChoiceCredentialExpired`, a distinct type rather than a
message to match on, classified in `TimeoutChoiceClient.request` — the single
method every Choice call passes through. Callers branch on the type, so the
comparison lives in one place instead of being repeated per call site.

That same method records broker health on the session: `credential_state`
(OK / EXPIRED / REJECTED / UNKNOWN), the last success and failure times, and the
last error. `market_data_ok` is set the first time a quotes call either works or
is refused, so the interface can *state* whether the account has a market-data
entitlement rather than inferring it from a failure. All of it is surfaced on
`/auth/choice/status` and rendered in the Health tab.

The distinction matters: the app used to answer both with "sign in to Choice
again", which for an expired key is a loop that cannot succeed. Choice's own
wording is always appended verbatim, because it is the most specific thing
available.

---

## 3. Repository layout

```text
algo_kkunal/
├── backend/app/
│   ├── main.py              FastAPI app, CORS, exception handlers, startup banner
│   ├── config.py            Settings; no insecure defaults
│   ├── database.py          Engine + per-user data directory resolution
│   ├── db_migrate.py        Additive schema reconciliation for the SQLite build
│   ├── dependencies.py      get_current_user, get_current_admin, get_choice_session
│   ├── api/v1/              auth, users, strategies, orders, portfolio, market,
│   │                        admin, diagnostics
│   ├── core/                security (bcrypt, JWT), errors (gateway → HTTP mapping,
│   │                        and the credential-failure wording)
│   ├── models/              User, Tenant, Strategy, StrategyRun, Order, AuditLog
│   ├── repositories/        audit_repo — the audit writer
│   ├── schemas/             Pydantic request/response models with bounds
│   └── services/            auth, strategy, order, choice_oauth
├── engine/app/
│   ├── config.py            Choice environment, timeouts, risk and rate limits
│   ├── env_paths.py         Where .env is searched for, in priority order
│   ├── costs.py             Indian retail cost model, shared by backtests and
│   │                        the pre-trade preview
│   ├── market_calendar.py   NSE/BSE trading holidays
│   ├── choice_gateway/
│   │   ├── client_manager.py   ChoiceSession, registry, timeout/retry SDK client
│   │   ├── session_store.py    Opt-in encrypted persistence for a broker session
│   │   ├── errors.py           Typed failures carrying an HTTP status
│   │   ├── normalize.py        Presence-based field extraction, price scaling
│   │   ├── analytics.py        Day change, returns, concentration
│   │   ├── funds.py portfolio.py orders.py market.py historical.py scrip_master.py
│   │   ├── sockets_interactive.py   Order/trade push stream
│   │   └── sockets_pricefeed.py     Level 1/2 feed, address from logon response
│   └── strategy_engine/
│       ├── dsl.py              Indicators, conditions, up-front validation
│       ├── backtest_runner.py  Next-bar fills, costs, real metrics
│       ├── runner.py           Live and paper execution
│       ├── scheduler.py        Polls quotes and drives a paper run
│       ├── explain.py          A strategy DSL rendered as an English sentence
│       ├── verdict.py          A backtest rendered as plain language
│       └── risk_manager.py     Per-order and per-day limits, the halt breaker
├── client-desktop/
│   ├── app/                 cli, launcher, local_server, proxy, config
│   ├── build_exe.py         PyInstaller build; locates the SDK by import
│   └── static/              Build artefact, copied from frontend-user
├── frontend-user/index.html   The single UI source; hand-built SVG charts, no CDN
├── frontend-admin/index.html  Built from the same tokens and chart engine;
│                              served by the app at /admin, not opened from disk
└── docs/                    INDEX.md, ARCHITECTURE.md, pdf/
```

---

## 4. Request flow

```
Browser
  → local server (desktop)              serves UI, proxies /api/*
  → backend                             authenticates, authorises, records
  → get_choice_session(user)            resolves this user's broker session
  → engine gateway                      validates, rate-limits, calls Choice
  → choice_api SDK                      timeout + bounded retry
  → Choice FINX OpenAPI
```

Failures propagate as typed `ChoiceGatewayError` subclasses, each carrying the
status it should surface as (409 not connected, 401 session expired, 429 rate
limited, 400 rejected, 502 upstream). `register_exception_handlers` maps them,
so a broker failure never arrives inside a 200.

---

## 5. Order path

1. Pydantic bounds the request (side, type, product, quantity, price).
2. `orders.validate_order` re-checks and calls `RiskManager` for notional and
   daily-loss limits.
3. The session's token bucket enforces the 10 orders/second SEBI cap.
4. An `Order` row is written with status `SUBMITTED`.
5. DEMO simulates locally; LIVE calls Choice.
6. The row is updated to `ACCEPTED`, `SIMULATED` or `REJECTED`, with the reason.
7. An `AuditLog` entry records the outcome either way.

Rejected orders are persisted too — an order book that only shows successes
cannot be reconciled against the broker.

### Pre-trade costing and limits

`POST /orders/preview` prices an order before it is sent, using the same
`CostModel` a backtest uses (`engine/app/costs.py`). Nothing is submitted and no
rate-limit token is spent. Keeping one cost model means a strategy's assumptions
and a manual order's charges cannot disagree — previously only the backtester
knew what a trade cost.

`GET /orders/limits` reports the per-order ceiling, the daily loss cap and the
headroom left against it. A limit the trader can see is a safety feature; one
discovered by hitting it reads as a bug.

### The kill switch

`POST /orders/halt` cancels every working order and stops the account accepting
new ones for the rest of the day. Each cancellation is attempted independently,
because the whole point is that it works when things are already going wrong,
and the halt is applied even if the broker refuses some cancellations — stopping
new orders must not depend on Choice answering.

The daily-loss cap trips the same breaker rather than refusing one order at a
time, so a strategy retrying in a loop cannot keep probing the limit. Neither
halt is clearable from the API; both reset when the day rolls over.

> **Fixed during this work.** `validate_order` was called without `owner_key`,
> so both the daily-loss cap and the halt check were unreachable, and
> `record_realized_pnl` had no caller at all — the cap could only ever read
> zero. Paper fills now feed it, and a live account takes Choice's own realised
> figure (set, not added, so a refresh cannot book the same loss twice).

### Reconciliation

`GET /orders/reconcile` compares the local order record against Choice's order
book. The interface shows the local book, so a divergence means the interface is
wrong. Orders placed outside the platform are reported separately and are not a
fault.

### Price units

The API and UI carry limit prices as integer paisa. `PRICE_UNIT_DIVISOR` in
`backend/app/services/order_service.py` converts to whatever unit Choice
expects on `api/OpenAPI/V2/NewOrder`.

> **Unresolved.** The supplied PDFs do not document the order schema — it lives
> in the live API docs behind `finx.choiceindia.com/api/OpenAPI/Info`. Confirm
> the unit in UAT before placing a limit order. If Choice expects paisa, set
> `PRICE_UNIT_DIVISOR = 1`.

### Price scaling on the way in

Separately, and confirmed against live data: **portfolio prices arrive scaled.**
`LTP` and `ClosePrice` are both integers carrying the record's own
`PriceDivisor` (100 on equities, so they are in paise). `AvgBuyPrice` is already
a rupee decimal and must not be divided. `_scaled_price` in
`engine/app/choice_gateway/portfolio.py` is the only place this is applied; a
new price field must go through it.

A mismatch between the two scaled prices is **logged, never repaired**. An
earlier guard substituted the close for a last price it judged implausible,
which replaced a correct value with an unscaled one and displayed a ₹25 lakh
portfolio as ₹25 crore. Guessing which of two numbers is wrong is not something
this code is entitled to do — see AUDIT_REPORT.md, VAL-1.

---

## 6. Backtesting

`engine/app/strategy_engine/backtest_runner.py`. Results always come from a run
over real bars; there is no fallback that returns a plausible-looking result.

* The instrument is resolved to an explicit `(segment_id, token)` before any
  data is fetched.
* Signals are evaluated on a closed bar and filled at the **next** bar's open,
  with slippage — filling at the signal bar's close is not reachable live.
* Indian retail costs are modelled: brokerage with cap, STT, exchange and SEBI
  fees, stamp duty on the buy leg, GST.
* Position sizing respects available capital.
* Metrics: net P&L, return, win rate, average win/loss, profit factor, max
  drawdown, Sharpe, total charges, and the equity curve.
* `data_source` records `CHOICE_OPENAPI` or `SANDBOX_SYNTHETIC` on the run, and
  the UI labels a sandbox result as not indicative.

---

## 7. Data model

| Entity | Notes |
| :--- | :--- |
| `Tenant` | Organisation. Every strategy and order is scoped to one. |
| `User` | Belongs to a tenant; role `trader` or `admin`. |
| `Strategy` | JSON DSL, validated on write. |
| `StrategyRun` | BACKTEST / PAPER / LIVE, with metrics, logs and `data_source`. |
| `Order` | Every submission attempt, with mode, status and failure reason. |
| `AuditLog` | Logins, connections, orders, strategy changes. |

Tenant scoping is applied in every query, and `get_current_user` re-reads the
tenant from the database rather than trusting the JWT claim.

---

## 8. Security

| Control | Implementation |
| :--- | :--- |
| Passwords | bcrypt, 72-byte truncation handled; constant-time compare on unknown users |
| Tokens | HS256 JWT; no default signing key — production refuses to start without one |
| Broker credentials | Held in the session object only; never stored, never used as platform credentials |
| Partner OAuth | Single-use `state` bound to a signed-in user, plus AES decryption; disabled when unconfigured |
| CORS | Empty by default; credentials only enabled alongside an explicit origin list |
| Binding | Loopback by default for both the API and the desktop server |
| Rate limiting | Per-session token bucket on order submission |
| Audit | Every security-relevant action written to `AuditLog` |

---

## 9. Regulatory constraints

From the Choice OpenAPI Integration Guide. These bound the design; see
[INDEX.md §3](INDEX.md).

* **Environments.** `CHOICE_ENV` defaults to `UAT`. Production requires UAT
  certification by Choice.
* **Static IP.** Orders must originate from a declared static IP.
  **This conflicts with the current desktop topology**, which places orders
  from each user's own machine. Before production, order flow has to move to a
  server with a declared address, with the desktop client proxying to it — the
  proxy layer already exists for exactly this shape.
* **Empanelment.** A platform serving multiple Choice clients is Type B and
  needs exchange empanelment before production access.
* **Order rate.** 10 orders/second per strategy without exchange registration.
* **Retention.** 5 years of API activity logs.

---

## 10. What is not built

Honest list, so the plan is not read as the product.

* LIVE runs — a strategy that submits real orders — are not built. The runner
  supports the mode, but only PAPER can be started from the API, and live
  execution stays blocked behind the regulatory items in §9 regardless.
* The socket wrappers connect and subscribe correctly, but nothing yet feeds
  their ticks into a running strategy.
* Live quotes are not surfaced in the interface. The market gateway still
  prices simulated market orders, but the ticker and watchlist were removed
  while the account has no market-data entitlement.
* Realised gains are not split into STCG and LTCG. That needs purchase dates
  from retained trade history, which the holdings payload does not carry.
* Backtests run inline in the request. Long ranges will block a worker;
  out-of-process workers are the intended fix.
* SQLite has no concurrent-writer story. A multi-user server deployment needs
  PostgreSQL and Alembic.
* Muhurat and other special sessions are not modelled; the calendar covers
  regular equity sessions only.
* The desktop binary is unsigned.

---

## 8a. Paper strategy runs

`POST /strategies/{id}/start` begins a PAPER run; `/stop` ends it;
`/run-status` reports on it. Live only in the sense that prices are real —
`LiveStrategyRunner._submit` branches on mode before any client is obtained, so
a paper run has no code path to Choice's order endpoint.

**The feed is polled, not streamed.** `strategy_engine/scheduler.py` runs one
thread per run, fetching touchline quotes every five seconds. Every quote goes
to `on_tick` for stop and target monitoring; quotes are aggregated into bars on
wall-clock boundaries and handed to `on_bar`, which is where the strategy's
entry and exit conditions are evaluated. Choice publishes a FIX3.0 feed whose
address arrives in the logon response, and connecting to it would be lower
latency — but polling is a fraction of the code and entirely adequate at bar
granularity, which is what the DSL works in. The socket is an upgrade with a
clear seam, not a prerequisite.

### Three rules, all failing safe

| Condition | Behaviour |
| :--- | :--- |
| No real market data | The run **refuses to start**, with the reason |
| Quotes fail 3 times running | The run **halts** and records why |
| The API key expires mid-run | The run **halts immediately** |

A strategy with no prices cannot decide anything, so starting one would produce
something that looks alive and never acts. A strategy deciding on stale prices
is worse than one that stopped — and "prompt the user" assumes somebody is
watching, which is the single thing an automated runner exists to avoid. The
key lasts about a day while a run lasts hours, so a run *will* meet an expiry;
retrying cannot revive it, so it stops on the first occurrence rather than
after the failure threshold.

### The row and the runner

A run is two things kept in step: a `StrategyRun` row that survives a restart,
and a runner in memory that does not. When the process dies mid-run the row
still claims `RUNNING`, so `recover_orphaned_runs` marks such rows
`INTERRUPTED` at startup and says so in the log. Without it the interface would
show a strategy trading when nothing is — and a phantom position is worse than
a run that refuses to start.

A halt from the scheduler closes the row through the same path, so a run
stopped by a feed gap never sits at `RUNNING`.

Paper P&L stays in `paper_positions`, reported separately from real holdings
and never added to them.

---

## 9a. Diagnostics

`GET /api/v1/diagnostics/choice` reports **what Choice actually sent**, which is
the only question worth asking when an obviously funded account shows nothing.
It returns four keys:

| Key | Contents |
| :--- | :--- |
| `session` | mode, environment, Client ID, base URL, whether a session id and access token exist |
| `upstream` | one entry per Choice call: ok/failed, record count, envelope and record field names, the error text on failure |
| `normalized` | what the gateway made of each response |
| `hint` | a plain-language reading of the above |

By default only **field names and value types** are reported, never values, so
the output can be shared to get a mapping corrected without disclosing
balances or holdings. `?include_values=true` opts in explicitly.

The Health tab in the interface renders exactly these keys. It previously read
a `checks` key that this endpoint has never returned, so it silently degraded
to dumping raw JSON — the one screen built to explain a failed connection was
useless during a failed connection. A contract test now asserts the response
carries the keys the panel reads, because a frontend and an endpoint drifting
apart is invisible until the moment it matters.

---

## 10a. Branding

The Choice logo is **inlined as SVG** in both frontends, taken from
`choiceindia.com`. Inlined rather than linked for two reasons: the desktop app
must work with no network, and the wordmark has to change colour with the
theme. Choice ships it as `#0f1621`, which is 1.03:1 against the dark surface —
invisible — so that path is `fill="currentColor"` and inherits the theme's text
colour (17.2:1 dark, 18.9:1 light). The blue tagline keeps its exact brand
value `#2777f3` (4.48:1 dark, 4.18:1 light, both above the 3:1 WCAG asks of a
graphical element) and is hidden below 880px, where it is too small to read.

The tab icon is a plain lettermark in the brand blue, not a Choice logo
variant: the supplied logo is a wordmark with no icon glyph, and a wordmark is
illegible at 16px.

> "Choice" and "The Joy of Earning" are Choice International's trademarks.
> Using them in a partner application is a matter for your vendor agreement
> with Choice, not something the code can settle.

---

## 11. Design rules

1. Only `engine/app/choice_gateway/` imports `choice_api`.
2. Frontends and the desktop client consume platform APIs only.
3. A failure is raised, never returned as a success-shaped payload.
4. Sandbox data is labelled at every boundary it crosses.
5. Broker sessions are per user; nothing is shared across tenants.
6. An order is validated, rate-limited, persisted and audited — in that order.
