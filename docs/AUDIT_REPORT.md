# Code, Logic and Security Audit — with remediation

**Date:** 12 August 2026
**Scope:** `backend/` · `engine/` · `client-desktop/` · `frontend-user/` · `frontend-admin/` · `docs/`
**Codebase at audit:** ~2,640 lines of Python
**Broker SDK:** `kkunal` / `choice_api` 1.2.0

---

## Summary

Every finding below was **reproduced by running the code**, not inferred from reading it.
All 27 code findings have since been **fixed and re-verified** the same way.

| | Count |
| :--- | ---: |
| Findings fixed and verified | 40 |
| Items needing your decision or credentials | 4 |
| Tests passing (was 6, one failing) | 212 |
| Regression checks passing | 26 |

The two defects that let an unauthenticated request take over a broker session are
closed, sandbox orders can no longer reach production, a rejected order now returns
an error instead of a success, and backtests compute real numbers from real bars.

---

## How this was verified

**During the audit**

- Booted the API and ran a scripted two-tenant scenario through it: registration,
  Choice login, portfolio reads, order placement, cross-tenant access, forged tokens.
- Ran both test suites — 1 failed, 5 passed. `pytest` was listed in
  `requirements.txt` but not installed in the environment.
- Launched `ChoiceFinxTrader.exe` in both modes and drove a full login-to-portfolio
  flow through its proxy.
- Extracted and read all four PDF specifications, then diffed their contents against
  `docs/INDEX.md` and the implementation.

**After the fixes**

- Re-ran the same scripted scenario as a 26-check regression probe — all pass.
- Rebuilt the test suite from 6 tests to 61 — all pass, run twice to prove
  repeatability.
- Rebuilt the executable and drove a full sign-up → connect → order → backtest
  journey through the new binary.

> **Side effect worth knowing.** The original order-placement probe caused three live
> requests to Choice's production endpoint
> `finxomne.choiceindia.com/api/OpenAPI/V2/NewOrder`. All three were rejected with
> `401 Unauthorized`, so no order reached an exchange — but the fact that a
> *demo-mode* session produced them is finding **LOG-1**.
>
> No probe ever wrote to `algo.db`; every run used an isolated throwaway database.

---

## Security findings

### SEC-1 — Anyone who could reach the API could mint a session and hijack the broker connection
**Was:** Critical · **Now:** Fixed

`GET /api/v1/auth/choice-oauth-callback` required no authentication and trusted its
query string completely. It created a user, issued a working JWT, and overwrote the
process-wide Choice session with attacker-supplied values.

```
GET /api/v1/auth/choice-oauth-callback?cid=ATTACKER&sid=ATTACKER_SID
→ HTTP 200  {"access_token":"eyJhbGciOiJIUzI1NiIs…","role":"trader"}
```

The token worked on every authenticated endpoint. The overwritten session also
disconnected whoever was legitimately logged in. The Partner Product spec says these
parameters arrive **AES-encrypted with a vendor-specific key** — the decryption and
verification that would make this endpoint safe were never written.

**Fix:** `POST /auth/choice/oauth/start` issues a single-use HMAC `state` bound to the
signed-in user. `GET /auth/choice/oauth/callback` verifies that state before
decrypting anything, then AES-decrypts `cid`/`sid`/`accessToken` as the spec
requires. Without `CHOICE_OAUTH_AES_KEY` the flow is disabled rather than falling
back to plaintext.
*Files:* `backend/app/api/v1/auth.py`, `backend/app/services/choice_oauth.py`

---

### SEC-2 — All users shared one broker session, so tenancy was cosmetic on every money endpoint
**Was:** Critical · **Now:** Fixed

`choice_client_manager` was a single module-level object. Whoever authenticated with
Choice most recently owned it, and every other user's portfolio, funds and order
calls ran against that session regardless of tenant.

```
Alice (Firm A) logs into Choice
Bob   (Firm B) GET /api/v1/portfolio/funds
→ 200 {"mode":"DEMO_MODE","AvailableMargin":250000.0}   ← Alice's session
```

Database-level tenancy was enforced correctly for strategies and users, which made
this more dangerous: the product looked multi-tenant while the broker layer was a
single global.

**Fix:** `ChoiceSessionRegistry` holds one `ChoiceSession` per user id, with
inactivity expiry. Gateways take a session argument; the `get_choice_session`
dependency returns `409` for a user who has not connected.
*Files:* `engine/app/choice_gateway/client_manager.py`, `backend/app/dependencies.py`

---

### SEC-3 — Credentials hardcoded in source, no version control or secrets hygiene
**Was:** High · **Now:** Fixed

```
backend/app/config.py:17   SECRET_KEY = "SUPER_SECRET_KEY_CHANGE_IN_PRODUCTION_1234567890"
client_manager.py:24        vendor_id="M09984", api_key="API_KEY_12345"
client_manager.py:197       "Authorization": f"SessionId {… or 'SESS_M09984'}"
```

There was no `.env`, no `.env.example`, no `.gitignore`, and the directory was not a
git repository at all.

**Fix:** No signing key in source. Production refuses to start without `SECRET_KEY`;
development generates an ephemeral per-process key and warns. Hardcoded vendor
credentials removed. `.env.example` and `.gitignore` added; dependency versions
bounded in all three `requirements.txt` files.

---

### SEC-4 — Orders reached the broker with no validation, limits or risk checks
**Was:** High · **Now:** Fixed

```
quantity = -500       → forwarded to Choice NewOrder
quantity = 1000000000 → forwarded to Choice NewOrder
```

`RiskManager.validate_order()` existed and was called by nothing. No per-tenant
exposure cap, no notional limit, no daily loss cap, no rate limiting.

**Fix:** Four layers — Pydantic field bounds, then `validate_order`, then
`RiskManager` notional and daily-loss caps, then a per-session token bucket
enforcing the SEBI 10 orders/second ceiling.
*Files:* `backend/app/schemas/order.py`, `engine/app/choice_gateway/orders.py`,
`engine/app/strategy_engine/risk_manager.py`

---

### SEC-5 — No audit trail, against a 5-year retention requirement
**Was:** High · **Now:** Fixed

The `AuditLog` model and `audit_repo.log()` were fully written and called from
nowhere. Order submissions, logins and strategy changes left no record.

**Fix:** Audit entries written for registration, login, broker connect/disconnect,
order placed, order rejected, strategy created/updated/deleted, backtest completed
and role changes. Exposed at `GET /admin/audit`.

---

### SEC-6 — Permissive transport and session settings
**Was:** Medium · **Now:** Fixed

`allow_origins=["*"]` with `allow_credentials=True` — a combination browsers reject
outright. Running `python app/main.py` bound `0.0.0.0`, exposing the API (including
SEC-1) to the whole local network.

**Fix:** CORS is empty by default and credentials are only enabled alongside an
explicit origin list. Both the API and the desktop server bind loopback by default.
Token lifetime reduced to 8 hours.

---

### SEC-7 — A broker API key repurposed as an account password
**Was:** Medium · **Now:** Fixed

Choice logins auto-created `choice_<vendor_id>@choice.com` with the password set to
the first 16 characters of the API key — mixing a broker credential into the local
auth system with a fully predictable account name.

**Fix:** Connecting a broker account is now a separate action from signing in to the
platform. The Choice session attaches to an already-authenticated user; no account
is auto-created and no API key is used as platform credentials.

---

### What was already correct

Confirmed by probe and kept: bcrypt password hashing with 72-byte truncation handled;
JWT verification rejecting tokens signed with the wrong key (`401`); admin endpoints
rejecting non-admin roles (`403`); cross-tenant strategy reads returning `404`;
tenant scoping on `/users` and `/strategies`.

---

## Trading-logic findings

These mattered most, because each one caused the system to report a false state to a
trader.

### LOG-1 — Demo mode did not sandbox orders; it sent them to production
**Was:** Critical · **Now:** Fixed

Funds and portfolio checked `mode == 'DEMO_MODE'` and returned sample data. Order
placement checked only `is_live_connected()`, which demo login set to `True`. The one
operation that moves money was the one operation demo mode did not intercept.

```
login vendor_id="DEMO"  → _is_connected = True, mode = "DEMO_MODE"
place_order()           → skips PAPER_TRADING branch
                        → POST https://finxomne.choiceindia.com/api/OpenAPI/V2/NewOrder
```

In the probe this failed with `401` only because the demo session carried no valid
credentials. If a real session were established first and demo entered afterwards —
the same global object, per SEC-2 — the guard would not re-engage and a "demo" order
would become a live one.

**Fix:** An explicit `SessionMode` (`DISCONNECTED` / `DEMO` / `LIVE`) is
authoritative. A `DEMO` session is simulated end to end and has no code path to
Choice. Nothing infers the mode from connection state.

---

### LOG-2 — A rejected order was shown to the trader as placed
**Was:** Critical · **Now:** Fixed

The gateway returned `{"status":"FAILED"}`, the API wrapped it in **HTTP 200**, and
the UI inspected neither — it read a field that did not exist and printed a
fabricated order ID.

```
backend  → 200 {"status":"FAILED","message":"…order placement failed: 401…"}
frontend → showToast(`Order Placed on Choice OpenAPI! ID: ${data.client_order_no || 'ORD_1001'}`)
         → "Order Placed on Choice OpenAPI! ID: ORD_1001"
```

The `catch` branch was worse: on a network failure it announced "Order submitted to
Choice trading engine!". A trader would believe they held a position they did not
hold, in both directions.

**Fix:** Typed `ChoiceGatewayError` subclasses each carry the status they should
surface as (409 not connected, 401 session expired, 429 rate limited, 400 rejected,
502 upstream), mapped by an exception handler. The UI checks the response and reports
*"Order not placed — &lt;reason&gt;"*.
*Files:* `engine/app/choice_gateway/errors.py`, `backend/app/core/errors.py`,
`frontend-user/index.html`

---

### LOG-3 — Every backtest returned the same fabricated result
**Was:** High · **Now:** Fixed

The engine ran as a separate HTTP service that nothing started. When the call failed,
`engine_client` returned a hardcoded profitable result and marked the run
`COMPLETED` — identical numbers for every strategy, symbol and date range.

```
POST /api/v1/strategies/{id}/backtest
→ "status":"COMPLETED"
   "return_pct":15.0  "win_rate":66.7  "sharpe_ratio":1.85  "total_trades":12
   "logs":["…","DSL evaluation complete. 12 trades executed."]
```

The fabricated logs claiming twelve executed trades are what made this dangerous
rather than merely broken.

**Fix:** `backend/app/services/engine_client.py` deleted. Backtests run in process
against the user's own session. A run that cannot complete is marked `FAILED` with
the reason and the error surfaces.

---

### LOG-4 — The real backtester always loaded RELIANCE, whatever symbol was requested
**Was:** High · **Now:** Fixed

The call site passed four positional arguments, so `segment_id` and `token` silently
took their defaults:

```
def get_historical_ohlcv(symbol, timeframe, start_date, end_date, segment_id=1, token="2885")
call:   get_historical_ohlcv(symbol, timeframe, start_date, end_date)
                                                            ↑ token never passed
```

The synthetic fallback had the same flaw from the other direction — `base_price` was
2500 for RELIANCE and 1500 for everything else, so INFY and TCS backtested against an
identical price series.

**Fix:** `resolve_instrument` produces an explicit `(segment_id, token)` before any
data is fetched, and both are required parameters. Sandbox base prices are per
instrument.

---

### LOG-5 — An account with zero margin displayed ₹2,50,000 of buying power
**Was:** High · **Now:** Fixed

The normaliser chained `or` across candidate keys, and `0.0` is falsy in Python, so a
genuine zero balance fell through to the demo constant:

```
_normalize_funds({"NetLimit":0.0, "AvailableMargin":0.0, "UsedMargin":0.0})
→ {"NetLimit":250000.0, "AvailableMargin":250000.0, "total_collateral":250000.0}
```

The same pattern ran through the holdings, positions and order normalisers.

**Fix:** `engine/app/choice_gateway/normalize.py` picks the first key that is
*present*, not the first that is truthy. A field Choice did not report is `null`; a
genuine zero stays zero.

---

### LOG-6 — Placed orders were never recorded, so the order book was permanently empty
**Was:** High · **Now:** Fixed

`POST /orders/` called the gateway and returned; no `Order` row was ever written.
`GET /orders/` joined through `StrategyRun` → `Strategy`, and since nothing populated
that chain it returned `[]` forever.

**Fix:** Every attempt is persisted before the broker call and updated after it —
`ACCEPTED`, `SIMULATED` or `REJECTED`, with the failure reason. `Order` gained
`tenant_id`, `user_id`, `execution_mode`, `source` and `failure_reason`;
`strategy_run_id` is nullable so manual orders are recorded too.
*Files:* `backend/app/models/order.py`, `backend/app/services/order_service.py`

---

### LOG-7 — The market-data cache ignored which instruments were requested
**Was:** Medium · **Now:** Fixed

```
cache primed with ["RELIANCE-ONLY"]
get_multiple_touchline("1_26000")   → {"data":["RELIANCE-ONLY"]}
```

Because the cache was class-level it was also shared across all users.

**Fix:** The cache lives on the session and is keyed by the exact tokens requested.

---

### LOG-8 — Unrecognised values silently became plausible wrong values
**Was:** Medium · **Now:** Fixed

An unknown buy/sell code was classified as SELL rather than raising:

```
_normalize_order({"BuySell":"X","TradingSymbol":"INFY","Qty":10})
→ {"side":"SELL", "status":"EXECUTED", …}
```

The same shape recurred in the DSL: an unknown indicator type was silently assigned
the close price, and `BOLLINGER` emitted only `_upper`/`_middle`/`_lower`, so a
condition on the bare indicator name evaluated `False` forever and the strategy
simply never traded.

**Fix:** An unrecognised side returns `None` and logs a warning. Unknown indicator
types and unknown condition fields raise `DSLError`, and strategies are validated
when saved so mistakes surface in the editor.

---

### LOG-9 — Backtest results would not have been trustworthy even once wired up
**Was:** Medium · **Now:** Fixed

- No brokerage, STT, stamp duty, exchange fees or slippage.
- Entry and exit both executed at the close of the bar that generated the signal.
- Position size ignored available capital.
- RSI used a simple rolling mean rather than Wilder's smoothing.
- No Sharpe or max drawdown was computed.

**Fix:** Signals evaluate on a closed bar and fill at the **next** bar's open with
slippage. A `CostModel` applies brokerage with cap, STT, exchange and SEBI fees,
stamp duty on the buy leg, and GST. Position sizing respects capital. RSI uses
Wilder's smoothing. Metrics now include max drawdown, Sharpe, profit factor, average
win/loss, total charges and the equity curve.

---

### LOG-10 — The Backtest tab and Admin dashboard were not connected to anything
**Was:** Medium · **Now:** Fixed

The Backtest tab posted to `/api/v1/runs/backtest`, a route mounted only on the
engine service and absent from the backend, so it 404'd through the proxy — and it
sent no `Authorization` header. Either way the UI printed "Engine completed!". The
Admin dashboard contained **zero** network calls; "Acme Quant Firm" and "Beta Trading
Capital" were hardcoded table rows.

**Fix:** The Backtest tab calls `POST /strategies/{id}/backtest` with auth and renders
real metrics. The admin dashboard signs in and reads `/admin/stats`,
`/admin/tenants` and `/admin/audit`.

---

### LOG-11 — The failing test, and what the passing ones were asserting
**Was:** Medium · **Now:** Fixed

```
engine/tests/test_dsl.py:31
AssertionError: assert np.True_ is True          1 failed, 5 passed
```

More concerning: `test_strategies.py` asserted `win_rate > 0` on a backtest, which
passed only because of the fabricated fallback in LOG-3. The suite was certifying the
mock. Tests also wrote to the real `algo.db` with fixed email addresses, so they were
not repeatable.

**Fix:** `evaluate_condition` returns a Python `bool`. The suite is now 61 tests, each
run isolated in its own temporary database with unique addresses per test.

---

## Documentation vs. the specifications

The four PDFs are the authority. `docs/INDEX.md` summarised them, and several
summaries were wrong in ways that had already produced bugs. **All eight are now
corrected.**

| ID | `INDEX.md` / `ARCHITECTURE.md` said | The specification says | Impact |
| :-- | :--- | :--- | :--- |
| DOC-1 | OAuth callback params listed as plain values | Partner guide §6: all values arrive **AES-encrypted, vendor-specific key**; only `baseUrl` is plaintext | Caused SEC-1; the OAuth flow also could not work against real Choice |
| DOC-2 | Price feed at `wss://brd.choiceindia.co.in:4520` | Handler IP and port read from the logon response fields `OdinBcastIP` / `OdinBcastPort` | Hardcoding an endpoint the spec says is dynamic |
| DOC-3 | "Zlib compressed, 5-byte length prefix" | 1 marker byte (`5`=compressed, `2`=uncompressed) **then** a 5-byte ASCII length | Off-by-one framing; a parser built from this would desync |
| DOC-4 | Logon: `63=FIX3.0\|64=101\|66=…\|67=<vendor_id>\|68=…\|400=11\|` | Tag `65` (message length) and tag `401` (auth type) are both required; tag `67` is User Id, not vendor id | Sample message would be rejected |
| DOC-5 | ORD_NRML statuses 1, 2, 3, 4, 6 | Seven codes — omitted `5` Open/Pending (the most common, and the value in the spec's own sample) and `7` Modified | Pending orders unclassifiable |
| DOC-6 | "Send `2` every 30s" | Timeout *is* 30s, checked every 1s — the heartbeat must be sent well inside that window | Sending at exactly 30s races the disconnect |
| DOC-7 | No mention anywhere of UAT, static IP, empanelment or order-rate limits | Integration guide §4, §8, §9, §12: UAT at `uat.jiffy.in`, static IP mandatory, vendor empanelment mandatory, 10 orders/sec cap, 5-year log retention | The entire regulatory frame was missing from the plan |
| DOC-8 | `ARCHITECTURE.md` specified React, PostgreSQL, Redis/Kafka, Celery workers, Alembic, a separate engine service | None existed. Frontends are single vanilla HTML files, storage is SQLite via `create_all()`, and the backend imports the engine in process | The plan read as built; it described roughly Phase 1–3 of 6 |

Minor, also fixed: `ARCHITECTURE.md:6` contained a stray `3m` on its own line;
`scrip_master.py` filed NIFTY50 under segment 2 while `market.py` queried the same
index as segment 1; the mock quote table gave SENSEX token 26000 — the same token it
assigned NIFTY 50, on the wrong exchange.

---

## Regulatory and engineering standards

| Requirement | Source | Then | Now |
| :--- | :--- | :--- | :--- |
| Test in UAT before routing live orders | Integration guide §10, §11.2 | No environment switch. `CHOICE_ENV` declared and read by nothing; every call went to production | Defaults to **UAT**. Startup banner states the environment; production logs a static-IP warning |
| Orders from a declared static IP | §8 | Not addressed | Documented as a blocking architectural conflict — see OPEN-1 |
| Vendor empanelment | §3.2, §4.2 | Not addressed | Documented — see OPEN-3 |
| Cap of 10 orders/second | §9 | No rate limiting anywhere | Per-session token bucket |
| Timeouts, back-off, treat 4xx/5xx as actionable | §11.1, §11.2 | `requests.request()` with no timeout; no retries; failures returned as `"status":"SUCCESS"` | `TimeoutChoiceClient` with 15s timeout and bounded exponential back-off on 5xx and network errors only |
| Reproducible builds | Practice | All dependencies `>=` with no bounds; no CI, no version control | Version ranges bounded; `.gitignore` added |
| Live/paper trading | `ARCHITECTURE.md` §5.3 | `runner.py` logged ticks and returned; socket wrappers never connected | Runner does order routing, position tracking, stop/target handling; socket wrappers connect and subscribe. No API route starts a run yet — listed under "What is not built" |
| Single source for the UI | Practice | `frontend-user/index.html` (1,165 lines) and `client-desktop/static/index.html` (1,149) had diverged; the exe shipped the static copy | One source in `frontend-user/`, staged into the bundle at build time |

---

## The executable

### Verified working — both launch paths

```
ChoiceFinxTrader.exe --run-backend 8099   → 200 {"message":"Welcome to Algo Trading Platform Backend"}
ChoiceFinxTrader.exe                      → UI on 9000, backend on 8080, browser opened
  POST /api/v1/auth/register              → JWT issued
  GET  /api/v1/portfolio/funds            → 409 before connecting (correct)
  POST /api/v1/auth/choice/connect        → sandbox session established
  GET  /api/v1/portfolio/funds            → 200 {"mode":"DEMO", …}
  POST /api/v1/orders/ (quantity -5)      → 422
  POST /api/v1/orders/ (valid)            → 200, status SIMULATED
  POST /api/v1/strategies/{id}/backtest   → 200, real metrics, SANDBOX_SYNTHETIC
```

### EXE-2 — The import error in the checked-in logs was stale
`cli_test_2.log` recorded `cannot import name 'settings' from 'app.config'` — the
desktop and backend `app` packages colliding. The current launcher spawns the backend
as a subprocess and did not reproduce it. Both log files were leftovers from an
earlier revision and have been deleted.

### EXE-3 — The database was written next to the executable
**Was:** High · **Now:** Fixed

When frozen, the DB path resolved to `os.path.dirname(sys.executable)`, landing beside
the binary. Installed to `C:\Program Files` that directory is not writable and the app
would fail to start; on a shared machine every user shared one database, including
password hashes.

**Fix:** User data goes to `%LOCALAPPDATA%\ChoiceFinxTrader` (verified after rebuild).

### EXE-4 — The build was not reproducible on any other machine
**Was:** Medium · **Now:** Fixed

`build_exe.py` and the generated `.spec` hardcoded
`C:\Users\kaival.trapasia\Desktop\indicator_lib`.

**Fix:** The SDK is located by importing it, with `CHOICE_API_PATH` as an override.
The generated `.spec` is git-ignored.

### EXE-5 — Distribution properties
**Needs you.** UPX is now disabled (a common antivirus false-positive trigger on an
unsigned one-file bundle), a Windows version resource is added, and `--onedir` is
available for much faster cold start. **Code signing needs your certificate** — the
build prints the exact `signtool` command on success.

---

## What changed, at a glance

| Area | Before | Now |
| :--- | :--- | :--- |
| Broker sessions | One process-wide client shared by everyone | `ChoiceSessionRegistry` keyed by user id, with expiry; unconnected users get 409 |
| Sandbox | Demo set `_is_connected`, and orders checked only that | Explicit `SessionMode`; a `DEMO` session has no code path to Choice |
| Order results | HTTP 200 carrying `"status":"FAILED"` | Typed errors → real status codes; UI reports the failure |
| Order safety | Anything forwarded, including negative quantities | Pydantic bounds → `validate_order` → `RiskManager` → 10/sec token bucket |
| Order records | Nothing written | Every attempt persisted, including rejections, each audited |
| Partner OAuth | Unauthenticated endpoint minting JWTs from raw params | Single-use state + AES decryption; disabled when unconfigured |
| Backtests | Fixed 15% / 66.7% / 12 trades; always RELIANCE | Real run over the requested instrument, with costs and provenance |
| Normalisation | `or`-chaining turned a real zero into a placeholder | Presence-based extraction; `null` for unreported, zero for zero |
| DSL | Unknown indicator became the close price | Validated on save; Wilder's RSI; crossovers; Python bools |
| Sockets | Stubs that never connected | Working per-user wrappers; feed address from the logon response |
| Secrets | Key hardcoded; no `.env`, `.gitignore` or VCS | No key in source; production refuses to start without one |
| Environment | Production only | Defaults to UAT, with a startup banner |
| Network | No timeout or retry; failures returned as `"SUCCESS"` | 15s timeout, bounded back-off on 5xx and network errors |
| Desktop build | DB beside the exe; hardcoded SDK path; UPX | `%LOCALAPPDATA%`; SDK by import; UPX off; version resource |
| Interfaces | Two divergent UI copies; dead backtest tab; static admin page | One UI source; both dashboards read live data |
| Tests | 6 tests, 1 failing, writing to the real DB | 61 tests, all passing, isolated per run |

---

## Still needs you

These are decisions and credentials, not code.

### OPEN-1 — Static-IP origination conflicts with the desktop topology *(blocking)*

The Integration Guide requires every API order to originate from a declared static IP.
A desktop application places orders from each user's own connection, so the exchange
will reject them. This cannot be fixed inside the app: order flow has to leave from a
server whose address is declared with Choice, with the desktop client proxying to it —
the proxy layer already exists for exactly that shape.

*Documented in `README.md`, `docs/INDEX.md` §3 and `docs/ARCHITECTURE.md` §9.*

### OPEN-2 — Confirm the order price unit in UAT *(blocking)*

The API carries limit prices as integer paisa and divides by 100 before calling
Choice. The supplied PDFs do not document the order schema — it lives behind
`finx.choiceindia.com/api/OpenAPI/Info`. Getting this wrong is a 100× error on every
limit order.

Verify in UAT and set `PRICE_UNIT_DIVISOR = 1` in
`backend/app/services/order_service.py` if Choice expects paisa. The constant carries
this note inline.

Related: the SDK hardcodes `ClientOrderNo: 123456` for every order, which removes any
per-order idempotency key. Worth raising with Choice.

### OPEN-3 — Vendor empanelment and UAT certification *(before go-live)*

A platform serving multiple Choice clients is Type B, which requires NSE/BSE/MCX
empanelment and written UAT sign-off from the Choice Open API team before production
credentials are issued. The code now defaults to UAT so this cannot be skipped by
accident.

### OPEN-4 — Code-sign the executable *(before distribution)*

The binary is unsigned, so SmartScreen warns on every download. UPX is now off, which
removes the other common antivirus trigger, but signing needs your certificate.

---

## Not built

Listed so the plan is not read as the product. Also in `docs/ARCHITECTURE.md` §10.

- Live and paper trading exist in the engine — order routing, position tracking,
  stop and target handling — but no API route starts a run yet.
- Socket clients connect and subscribe, but their ticks are not yet fed to a running
  strategy.
- Backtests run inline in the request; long ranges will block a worker.
- SQLite suits the desktop build; a shared server needs PostgreSQL and Alembic.
- No exchange holiday calendar — weekday session hours only.

---

## Files added during remediation

| File | Purpose |
| :--- | :--- |
| `.env.example` | Configuration template with every setting documented |
| `.gitignore` | Excludes secrets, databases, build artefacts |
| `pytest.ini` | Test discovery and warning filters |
| `engine/app/choice_gateway/errors.py` | Typed gateway failures carrying HTTP status |
| `engine/app/choice_gateway/normalize.py` | Presence-based field extraction |
| `backend/app/core/errors.py` | Maps gateway errors onto HTTP responses |
| `backend/app/db_migrate.py` | Additive schema reconciliation for the SQLite build |
| `backend/app/services/order_service.py` | Validate → record → submit → record outcome |
| `backend/app/services/choice_oauth.py` | State binding and AES decryption |
| `backend/scripts/manage_admin.py` | Grant/revoke the admin role (no self-service path) |
| `backend/tests/conftest.py` | Isolated per-run database and fixtures |
| `backend/tests/test_orders.py` | Order validation, sandbox isolation, persistence, audit |

Removed: `backend/app/services/engine_client.py` (the fabricated backtest fallback),
`engine/app/api/v1/choice_auth.py` and `engine/app/api/v1/runs.py` (unauthenticated
routes on an open port), `cli_test.log`, `cli_test_2.log`.

---

## Final system audit — 12 August 2026

A last pass across the whole system after the day's changes. Everything below
was executed, not reasoned about.

| Check | Result |
| :--- | :--- |
| Module import integrity (backend, engine, desktop, scripts) | all clean |
| References to deleted modules in code | none |
| Original regression probe | 26 / 26 pass |
| Test suite | 101 pass |
| Anonymous access to protected routes | 20 / 20 refused |
| Admin routes against a plain trader | 4 / 4 refused (403) |
| Tenant isolation (read, update, delete, backtest, runs, orders, users) | 7 / 7 blocked |
| Malformed order payloads | 11 / 11 refused |
| Malformed backtest payloads | 6 / 6 refused |
| Error envelopes leaking internals | none |
| Rate limiter trips beyond its rate | yes |
| Paper accounting invariants (weighted average, realised P&L, position close) | correct |
| Idle session purged | yes |
| Front-end interactive paths exercised | 22, zero JS errors |
| Palette re-validation, both themes, all pairs | all pass |
| Backtest metric invariants | all hold |

**Executable** (`13:06`): starts clean on PROD, serves the terminal at `/` and
the admin dashboard at `/admin`, and a full journey through it behaves —
`409` before connecting, sandbox session, holdings, a market order filled at
₹2,504.50 and recorded `SIMULATED`, a malformed order refused `422`, a backtest
returning `COMPLETED SANDBOX_SYNTHETIC` with 3 trades and every invariant
holding, admin blocked for a trader, and the admin API answering for a promoted
admin across stats, tenants, audit and health.

Two things reported as failures during the audit were **defects in the probe,
not the code**, and are recorded here so they are not mistaken for real ones: a
package walk that imported the models under two names and tripped SQLAlchemy's
duplicate-table guard, and a shell quoting error. Both were confirmed as
harness artefacts before being dismissed.

**Fixed in this pass:** `ARCHITECTURE.md` still described the engine HTTP
service and `ENGINE_IN_PROCESS`, both removed earlier in the day, and listed the
repository layer as general query helpers when only the audit writer remains.
`README.md` did not say the admin dashboard is served at `/admin`. Both
reconciled.

---

## Post-audit fixes

Changes made after the audit, in response to problems hit in real use.

### Connecting a Choice account failed with "String should have at most 256 characters"

`api_key` was bounded at 256 characters. A real Choice API key can be a signed
bearer token well past that, so a valid key was rejected before the request left the
browser.

**Fix:** the bound is now 4096 characters — enough for any bearer credential, still
tight enough to reject obvious junk. Credential fields are also stripped before
validation, so a key copied from the FinX portal with a trailing newline works.
Validation errors now name the field as it appears on screen ("API key: …") instead
of the JSON key.
*Files:* `backend/app/schemas/auth.py`, `frontend-user/index.html`

### Paper trading mode added

Previously the only way to avoid risking real money was sandbox mode, which
replaces market data with fixtures — useful for trying the interface, useless
for evaluating a strategy.

**Added:** a `PAPER` session mode that signs in to Choice for real, reads real
quotes, holdings and history, and fills orders locally at the live traded price
without ever submitting them.

The session mode now answers two separate questions — where data comes from
(`uses_broker_data`: PAPER and LIVE) and whether an order leaves the process
(`mode.sends_real_orders`: LIVE only). `place_order` branches on
`simulates_orders` before obtaining a client, so a paper session has no code
path to the broker's order endpoint; a test asserts this by installing a client
that raises on any attribute access.

**Paper is the default** when connecting a real account. Live requires an
explicit mode *and* a ticked acknowledgement in the UI, and DEMO credentials
force sandbox regardless of the mode requested. Paper fills are tracked with
average-price accounting and running realised P&L, reported separately from
real holdings and never summed with them.
*Files:* `engine/app/choice_gateway/client_manager.py`,
`engine/app/choice_gateway/orders.py`, `backend/app/schemas/auth.py`,
`backend/app/services/auth_service.py`, `frontend-user/index.html`,
`backend/tests/test_paper_mode.py`

A related defect surfaced while testing it: `executed_price` recorded the
*submitted* price, so every market order — submitted at 0 — was stored as
having filled at zero. It now records the actual fill price.

### Over-engineering audit and second UI pass

`/ponytail-audit` was run across the repo. Its top finding was fair: 563 lines of
live-trading infrastructure — `runner.py` and both socket wrappers — with no
caller, written on the theory that live execution would need them. Those were
kept deliberately as the foundation for a listed remaining item; everything else
it found was cut.

**Removed:** the engine HTTP service (nothing starts it — the engine is imported
in-process), four of five repositories (zero callers; only `audit_repo` is used),
the `BacktestRunner` class that only delegated to `run_backtest`, an uncalled
`sparkline()`, and five config keys nothing read. `db_migrate.py` went from 130
lines to 64 by dropping a generic column-type mapper written for one real case.
About 250 lines, no behaviour change, 101 tests still passing.

**Interface, second pass:**

* **Light theme**, validated rather than inverted — categorical
  `#2a78d6,#eb6834,#1baf7a` and diverging `#2a78d6,#e34948` against a white card
  surface, all gates passing. Declared under both `prefers-color-scheme` and
  `[data-theme]` so the OS setting and the in-app toggle each win where they
  should, with no colour defined only inside a media block.
* **Density control** — compact mode tightens row and card padding through
  tokens, without a second layout.
* **Sortable tables**, sorting the data rather than the DOM rows so a refresh
  keeps the chosen order.
* **Skeleton rows** while the first load runs, so the layout does not jump.
* **Keyboard shortcuts** — `1`–`4` for views, `N` new order, `R` refresh,
  `T` theme, `D` density, `?` help, `Esc` close. Suppressed while typing.
* **Order ticket** showing the live traded price, order value, and the margin
  left after the order — with an explicit warning when it would go negative, or
  when a market order cannot be priced.
* **Admin dashboard** rebuilt on the same tokens and chart engine, with activity
  by action, order outcomes, sortable tenants and a filterable audit trail.

Two defects were found by rendering and measuring rather than by eye:

* Action names truncated in the chart gutter (`BACKTEST_COMP…`). The gutter is
  now sized by the caller and labels are humanised to "Backtest completed".
* **A value label placed inside a bar could not reach 4.5:1 in any theme.**
  Measured: white on the dark-theme red is 2.96:1, and on light-theme blue *no*
  ink passes — dark reaches only 4.28:1. Rather than pick a losing compromise,
  both charts now size their gutters from the widest label so a value never sits
  on a fill at all.

### Interface rebuilt as a terminal

The dashboard was replaced. The previous one was a set of cards with a number in
each — no context, no comparison, and three CDN dependencies that fail whenever
the desktop app is offline.

**Design thesis: the chrome is monochrome; colour belongs to data and state.**
Nav, cards, buttons and rules are built from a single neutral ramp, so when a
number turns red it is unmistakable. That is the opposite of the previous
approach, where gradient furniture competed with the figures.

**Charts are hand-built SVG.** Chart.js and the webfont CDN links are gone, which
removes the offline failure and gives exact control over the marks. Every chart
follows the same spec: 2px strokes, 4px rounded data-ends anchored to the
baseline, a 2px surface gap between adjacent fills, recessive grid and axes,
selective direct labels, and a crosshair or per-mark tooltip on every plotted
form.

| Panel | Form | Why that form |
| :--- | :--- | :--- |
| Portfolio value, margin, P&L, exposure | Stat tiles with context | Each pairs a figure with what it means — margin carries a utilisation meter, P&L a signed delta and an up/down count |
| Allocation by position value | Horizontal bars, sorted | Magnitude with identity; rank read by position, one hue because there is no second distinction to make |
| P&L contribution | Diverging bars around zero | Polarity — winners and losers in one frame, sign carried in the label as well as the colour |
| Equity curve | Line with baseline at starting capital | Change over time, with the endpoint emphasised |
| Drawdown | Separate chart, same time axis | **Never a second y-axis.** Two measures of different scale are two charts |
| Trade outcomes | Diverging columns in sequence | Discrete results, not a distribution |

The palette is the validated instance from the data-visualisation method —
categorical slots blue/orange/aqua and a blue↔red diverging pair — checked with
the validator against the actual card surface rather than chosen by eye:

```
categorical  #3987e5,#d95926,#199e70   vs #101216  → ALL CHECKS PASS
diverging    #3987e5,#e66767           vs #101216  → ALL CHECKS PASS
                                         worst CVD ΔE 19.2, normal-vision 29.0
```

Profit and loss never rely on colour alone: every delta carries an arrow and a
sign, and status colours are reserved — a series never borrows them.

Two defects were caught by rendering it and looking, which the validator cannot
see: the largest negative bar's value label collided with its category label and
erased it, and the header wrapped to three lines below 1000px. Both fixed.

### Confirmed Choice response shapes

Captured from a live production account with
`backend/scripts/diagnose_choice.py`. None of this is documented in the SDK or
the supplied PDFs, and every earlier mapping was guesswork. Field names are
real; values here are illustrative.

Every endpoint uses the envelope `{"Status", "Response", "Reason"}`.

**FundsViewNew** — `Response.FundsViewNew.{...}`

| Choice field | Mapped to | Notes |
| :--- | :--- | :--- |
| `FundsAvailable` | `AvailableMargin` | what can be deployed now |
| `MarginUtilized` | `used_margin` | |
| `LedgerBalance` | `ledger_balance` | |
| `MarginAgainstAssets` + `PledgeValue` | `total_collateral` | summed |
| `RealizedPnL` / `UnRealizedPnL` | `realized_pnl` / `unrealized_pnl` | |

Also present and unused: `TodaysBalance`, `TodaysPayIn`, `TodaysPayOut`,
`DPCharges`, `FutureBillCrDr`, `FutureCrDr`, `BuyMargin`, `EarlyPayIn`,
`OpenMargin`, `DpCFS`, `PoolCFS`, `SarCFS`, `OptionCFS`,
`TodaysHoldingSellBenefit`, `DPBill`, `DPC`.

**FundsView** (older endpoint) — `Response.FundsView.{...}`, with
`MarginAvailable`, `MarginUsed`, `CashAvailable`, `Collateral`,
`RealisedProfit`, `UnRealisedProfit`. Note the two endpoints disagree on the
spelling of "realised".

**Holdings** — `Response.lDictStockViewHoldingData`, **an object keyed by
ISIN**, not a list. Each record carries `Symbol`, `SecName`, `Token`,
`SegmentId`, `LTP`, `ClosePrice`, `AvgBuyPrice`, **`PriceDivisor`**, `Qty`,
`SellQty`, `MarketLot`.

> **`PriceDivisor` is not optional.** `LTP` is a scaled integer and
> `PriceDivisor` is its scale — 250450 with a divisor of 100 is ₹2,504.50.
> Using `LTP` directly gives a price 100× too high and corrupts every
> valuation. A sanity check falls back to `ClosePrice` when the scaled price is
> more than 10× away from it.
>
> This is also the clearest evidence yet on the open order-price question
> (OPEN-2): Choice expresses prices as scaled integers, which points towards
> `NewOrder` expecting paise. Still to be confirmed against the order endpoint
> itself before a limit order is placed.

**NetPosition** — `Response.NetPositions`, a list.
**OrderBook** — `Response.Orders`, a list.
**UserProfile** — `Response.{ClientId, Name, BOCode, DPCode, Depository,
POAStatus, MobileNo, EmailID}`.

**MarketStatus** — `Response.lstMktStatus`, keyed by segment id and then by
market type, each leaf `{MktType, Status}`. Flattened by `_parse_segment_status`.

**MultipleTouchline** — returns `Status: "Fail"` with the explanation as a
**plain string in `Response`**, not in `Reason`. `failure_reason` now reads
both.

These shapes are locked in by `engine/tests/test_choice_payloads.py`.

### A connected account showed no funds, holdings or quotes

Login succeeded and the session reported connected, but every panel was empty:
margin showed as unknown, holdings as none, the ticker as "No quotes available".

The cause was the payload unwrappers. They looked for records under
``Response``/``data``/``Data`` **at the top level only**. Choice nests
differently per endpoint — `{"Response": {"Holdings": [...]}}` is common — so
a wrapped list produced no records at all. Worse, that outcome is
indistinguishable from a genuinely empty account, so the interface confidently
reported nothing rather than admitting it had not understood the reply.

**Fix:**

* `unwrap_list` now searches depth-first for the first list of records, so a
  wrapped collection is found wherever it sits.
* `unwrap_dict` unwraps single-key wrappers and merges a list of records
  (segment-wise funds, for instance) into one mapping.
* Field lookup is case-insensitive and parses numeric strings, including
  Indian-format ones such as `"1,50,000.50"`.
* The candidate key lists for funds, holdings, positions and quotes were
  widened considerably.
* An explicit `Status: Failure` now raises instead of being unwrapped into an
  empty result.
* When a funds payload arrives but nothing maps, the gateway raises and names
  the fields it received rather than reporting a zero balance.

**Added:** `GET /api/v1/diagnostics/choice`, which reports what each upstream
endpoint returned — envelope keys, record counts, field names and a structural
summary — alongside what the normalisers made of it. Values are excluded unless
`include_values=true`, so the output can be shared to correct a mapping without
disclosing balances or positions.
*Files:* `engine/app/choice_gateway/normalize.py`, `funds.py`, `portfolio.py`,
`market.py`, `backend/app/api/v1/diagnostics.py`

The interface also stated "Not connected" under holdings when a request had in
fact succeeded and returned no rows; it now says so.

### Login failed against production credentials

A production Client ID could not connect, because `CHOICE_ENV` defaults to UAT
and the sandbox does not recognise production credentials. The `kkunal` login
flow itself was being driven correctly — `ChoiceClient.login()` runs
LoginTOTP → GetClientLoginTOTP → ValidateTOTP through the timeout wrapper and
captures `SessionId`, `AccessToken`, `OdinBcastIP` and `OdinBcastPort` — so the
only thing wrong was which server it was talking to.

**Fix:** a live configuration file at `%LOCALAPPDATA%\ChoiceFinxTrader\.env`
sets `CHOICE_ENV=PROD` with a generated `SECRET_KEY`. This is found by both the
executable and a source run, and survives a rebuild.

It also exposed a defect that would have stopped the application from starting
at all: the backend and engine settings classes both read that one file, and
`pydantic-settings` rejects unknown keys by default — so each refused the
other's settings and raised at import time. Both now use `extra="ignore"`, with
a regression test covering a shared file.
*Files:* `backend/app/config.py`, `engine/app/config.py`

### "Choice rejected the session" gave no usable reason

The error surfaced only the generic message; Choice's own explanation sat in a
field the UI never read. With `CHOICE_ENV` defaulting to UAT, the common case
was production credentials being sent to the sandbox, which is not something
the message helped anyone work out.

**Fix:** the handler appends Choice's own wording, and when the rejection says
the vendor is unknown it names the environment being used and the setting to
change. The response also carries `reason`, `upstream` and `choice_environment`
as separate fields.
*File:* `backend/app/core/errors.py`

### `.env` was not found when the app was launched from elsewhere

`pydantic-settings` resolves `env_file` relative to the process working directory,
which for a desktop launch is wherever the executable happened to be started from —
so a `.env` was easy to write and impossible to find.

**Fix:** configuration is searched in a defined order — `$CHOICE_ENV_FILE`, the
working directory, beside the executable, then `%LOCALAPPDATA%\ChoiceFinxTrader\.env`.
The backend logs which file it loaded at startup, and logs the search path when it
finds none.
*Files:* `engine/app/env_paths.py`, `backend/app/config.py`, `engine/app/config.py`,
`backend/app/main.py`

---

## VAL-1 — The portfolio was displayed a hundredfold too large

Found 12 August 2026 from a live account, after the final audit, when the
dashboard showed **₹25.42 Cr and +11,785.76% against 33 up · 0 down**. A
portfolio where nothing is down is not a portfolio; it is a scaling error.

Choice sends `LTP` as a scaled integer alongside a `PriceDivisor`. A guard
compared the divided last price against `ClosePrice` and, on a large mismatch,
**substituted the close** — on the assumption that the close was already in
rupees. It is not. Both prices are scaled by the same divisor, so the guard was
comparing a divided number with an undivided one, declared the correct value
implausible, and replaced it with a value 100× too large. The tripwire meant to
catch a bad price was the thing producing one.

The account's own logs disprove the assumption outright. Two instruments
settle it, because their last price and close are identical:

| Instrument | Reported close | ÷ 100 | Last price |
| :--- | ---: | ---: | ---: |
| LIQUIDBEES-EQ | 100000 | 1000.00 | 1000.00 |
| NITINFIRE-Z | 182 | 1.82 | 1.82 |

All ten sampled pairs fall within a normal daily move once scaled; none do
otherwise. The warning fired ~33 times per refresh — once per holding — which
was the real signal: a divisor wrong for every instrument on the book is not a
per-instrument data problem.

**Fix:** `ClosePrice` is read through the same `_scaled_price` helper as `LTP`,
and the guard now only warns. It never substitutes one price for another —
surfacing a suspect number is right, silently publishing a wrong one is not.

Worked through on RELIANCE-EQ (last 131490, close 132390, divisor 100):
₹1,314.90 against a ₹1,323.90 close, down slightly on the day. Previously it
valued at ₹1,32,390 a share.

Because every KPI derives from `current_price`, one fix corrects portfolio
value, cost, P&L, return %, the up/down counts, allocation and exposure
together. The account should now read roughly **₹25.42 L against ₹21.39 L of
cost (≈ +18.8%)**, with a realistic mix of winners and losers.

*File:* `engine/app/choice_gateway/portfolio.py`
*Tests:* added to `engine/tests/test_choice_payloads.py`, built from the
logged production values — per-instrument scaling, the ratio invariant, the
no-substitution rule, and an end-to-end assertion that a portfolio of this
shape values in lakhs rather than crores. Suite at the time: 114 passing.

One existing test asserted the fallback, and its fixture mixed a scaled `LTP`
with an unscaled `ClosePrice` — the same mistake as the code, which is why the
bug survived a green suite. Both were corrected rather than deleted.

## VAL-2 — The same bug, unfired, in the quote path

Found by asking where else a Choice price is read. `_normalize_quote` in
`market.py` read `Ltp` with `pick_float` and no divisor — and that value is
what `_market_fill_price` uses as the **fill price for a simulated market
order**. Every paper and sandbox market order would have booked at 100× the
market, silently corrupting paper P&L.

It had not fired only because quotes currently fail at Choice for want of a
market-data entitlement (OPEN-5). It would have surfaced the day that was
granted — as a paper-trading bug, which is the hardest kind to notice, because
paper P&L has nothing to reconcile against.

A third read, `_normalize_order` in `orders.py`, took an order's `Price` the
same way. Whether Choice scales that field is unconfirmed — but `scaled_price`
returns a record without a divisor untouched, so routing it through costs
nothing if the price is already in rupees and prevents an order book showing
prices a hundredfold out if it is not.

**Fix:** the scaling logic moved out of `portfolio.py` into
`normalize.scaled_price`, and all three modules now use it. A record carrying
no divisor is returned unchanged, so this is safe on payloads already in
rupees.

*Files:* `engine/app/choice_gateway/normalize.py`, `market.py`, `portfolio.py`,
`orders.py`

A third test guards the rule rather than the instance: it scans
`engine/app/choice_gateway/*.py` and fails if any module reads `LTP`,
`ClosePrice` or `LastPrice` through `pick_float` instead of `scaled_price`.
Three occurrences of one mistake is a pattern, and a pattern is worth a test
that catches the next one. Four dead imports left behind by the refactor were
removed at the same time (`orders.py`, `scrip_master.py`,
`sockets_pricefeed.py`); the gateway now has none.

---

## SDK-1 — `kkunal` claims a paise conversion it does not perform *(latent, not ours)*

In `choice_api/websockets_feed.py`, `_format_market_data` is preceded by the
comment `# Prices in paisa to rupees` and then does `float(val)` with no
division, for `LTP`, `Open`, `Close`, `High`, `Low`, `ATP` and `LowerCircuit`.
The comment describes an intent the code does not carry out.

Harmless today — nothing consumes the price feed yet (see *Not built*). It
becomes a live defect the moment socket ticks are fed to a running strategy,
which is exactly the item on the roadmap. Two notes:

* The SDK **does** apply `PriceDivisor` correctly in `historical.py`, so
  backtests run on rupee prices. That path was checked and is sound.
* The fix belongs in the SDK, not here. Until it lands, anything consuming
  `websockets_feed` must scale the prices itself.

*Not fixed:* the SDK is a separate vendored library
(`Desktop/indicator_lib/choice_api`) and changing it silently would leave your
copy diverging from the package you install. Flagged for your decision.

---

## DOC-1 — The README pointed at an API docs URL that 404s

Caught while verifying the rebuilt binary. `README.md` sent you to
`http://127.0.0.1:8000/api/v1/docs`. Only `openapi_url` is overridden in
`main.py`, so FastAPI still serves Swagger at its default `/docs`; the
documented path returns 404. Corrected to `/docs`. (`/redoc` and
`/api/v1/openapi.json` both work.)

---

## Audit — 13 August 2026

Triggered by "I cannot log in". The login report turned out not to be a code
defect; the audit around it found one real defect in code shipped the day
before.

### LOGIN-1 — The Choice API key had expired *(not a code fault)*

The reported symptom was the connect modal refusing with:

```
Choice rejected the session. Sign in to Choice again.
Choice said: Unauthorized, Token Expired
```

**Platform login was never broken.** Three independent checks: the API
(register 201, login 200, wrong password 401), the client-side sign-in path
driven with stubbed responses, and — decisively — the audit log itself, which
records two successful `USER_LOGIN` entries that morning at 03:24:18 and
03:26:36.

The failure is in the *broker* connection, and Choice's own wording separates
the two cases. The SDK sends the API key as a `Bearer` header on every call
(`client.py::get_headers`). An unrecognised vendor returns
`VendorId Invalid or doesn't exists` — proven with a deliberately invalid
vendor. This account got `Token Expired`, so the Client ID **M09984 is
recognised and the key attached to it has lapsed.** The last successful
connect was the previous day at 11:15, roughly sixteen hours earlier, which
fits a key with a daily lifetime.

**Fix:** none possible in code — the key has to be reissued from the Choice
Open API portal. What *was* wrong is that the app told the user to "sign in to
Choice again", which sends them round a loop that cannot succeed. An expired
token now gets its own message naming the actual remedy.
*File:* `backend/app/core/errors.py` · *Tests:* 4

### UI-1 — The diagnostics panel hid the very answer it exists to give

`/diagnostics/choice` returns `{session, upstream, normalized, hint}`. The
Health panel, written the day before, read `r.checks || r.results || []` —
neither of which the endpoint has ever returned. It did not crash; it fell
through to rendering an empty table plus a collapsed "Raw response" blob.

The one screen built to explain a failed Choice connection was therefore
useless during exactly the failure above, which is how it went unnoticed: the
panel *looked* fine on a healthy sandbox session.

**Fix:** the panel renders the real payload — the hint, a pass/fail line per
Choice endpoint with the error text, and the session table. A contract test
now asserts the response carries the keys the panel reads, so the two cannot
drift apart again.
*Files:* `frontend-user/index.html` · *Test:*
`backend/tests/test_auth.py::test_diagnostics_returns_the_shape_the_health_panel_reads`

### Everything else checked clean

| Check | Result |
| :--- | :--- |
| `pyflakes` over backend, engine, client-desktop, both test trees | clean |
| Every module imports | 0 failures |
| Test suite, run twice | 152 passing (at that date; 191 now) |
| Authorisation on every protected route | 20/20 reject anonymous |
| Admin routes against a plain trader | rejected |
| Tenant isolation across every scoped resource | 7/7 |
| Malformed orders / backtests refused | 11/11 and 6/6 |
| Rate limiter, paper accounting, session expiry | pass |
| New-feature endpoints (preview, limits, halt, reconcile, CSV) | 24/24 |
| Frontend JS parse, handler and element resolution | both pages clean |
| Render harness | 29/29 |
| Sign-in path harness | clean |
| Docs referencing files that do not exist | 0 real (5 are deleted-file history or the SDK) |
| Functions with no caller | 4, all in the socket feed and strategy runner — both documented as built-but-unwired |

### Worth knowing, not a defect

**Broker sessions do not survive a restart.** `ChoiceSessionRegistry` is an
in-memory dict with no persistence, so every rebuild or restart of the
executable drops every Choice session while the platform JWT (stored in the
browser) survives. The user stays signed in and is asked to reconnect the
broker with no explanation of why. Persisting them means storing a live broker
credential at rest, so it is listed rather than done.

**Accounts are split across two databases.** The executable reads
`%LOCALAPPDATA%\ChoiceFinxTrader\algo.db`; running from source reads
`algo_kkunal/algo.db`. Accounts created before the data directory moved
(`admin@acmequant.com`, `realtrader@choice.com`, …) exist only in the latter
and cannot sign in to the executable. Correct behaviour, surprising result.

---

## PHASE-1 — Two layout faults found while applying the spacing scale

Neither was reported; both surfaced from rendering the interface at the
breakpoints the design checklist names (1440 / 1024 / 768 / 375).

**The KPI row left an empty cell.** Five tiles were laid out in a
three-column grid below 1500px, so at 1440 — an ordinary laptop width — the
last row held two tiles and a gap that reads as a card that failed to load.
Five divides evenly into five, two or one, and into nothing else. The
five-column form now holds to 1200px, where each tile is still ~222px wide,
and drops straight to two.

**The topbar widened the document.** `.topbar > * { flex: none }` with no
overflow rule meant that at 375px the navigation pushed the page wider than
the viewport, so the whole document scrolled sideways and hid content. The bar
now scrolls inside itself below 900px. Measured horizontal overflow is **0px at
1440, 1024, 768 and 375** — measured in a real browser, not judged by eye.

*Files:* `frontend-user/index.html`, `frontend-admin/index.html`

---

## BUILD-1 — The build failed after several minutes on a lock nothing was holding

A build failed with `PermissionError` on `dist/ChoiceFinxTrader.exe` *at the
end* of the run, after the pre-flight check had passed at the start and with no
`ChoiceFinxTrader` process running. Checked immediately afterwards, the file was
writable.

PyInstaller deletes the previous binary as its final step. Antivirus routinely
takes a transient read lock on a freshly written executable, so a scan starting
mid-build holds the file exactly when PyInstaller wants to remove it. The
earlier fix — probing writability up front — cannot catch this, because at that
moment the file genuinely is writable.

**Fix:** the previous binary is moved aside during the pre-flight, so there is
nothing left for PyInstaller to delete at the end. It is kept as
`ChoiceFinxTrader.exe.previous` until the build succeeds and removed only then,
because a failed build should not leave the user with no working executable;
if the build fails, the message names the file to rename back.

*File:* `client-desktop/build_exe.py`

---

## HIST-1 — Every real-data backtest was sending an interval Choice cannot parse

Found while diagnosing why historical bars were refused on the live account.

`get_historical_ohlcv` passed our own timeframe vocabulary — `"1m"`, `"1d"`,
`"1w"` — straight through to the SDK as the chart `Interval`. Choice's chart
endpoint takes **TradingView-style intervals**: bare minutes as numbers, `D`
for daily, `W` for weekly. `"1d"` is not a value it recognises.

Every backtest against real Choice data was therefore malformed. It went
unnoticed because the account has never successfully fetched historical data —
all four recorded runs fall back to `SANDBOX_SYNTHETIC`, so the request was
never exercised against a working endpoint.

**Fix:** an explicit map from our vocabulary to Choice's, applied at the single
call site, with an unsupported timeframe raising rather than being forwarded.
The diagnostics probe now sends `D` as well, so a parameter fault can no longer
masquerade as a missing entitlement.

*File:* `engine/app/choice_gateway/historical.py`

> **Still open.** With a valid interval the endpoint may still refuse: it sits
> under `api/OpenGraph/ChartData`, a different path from the rest of the API,
> and may carry its own entitlement. Re-running diagnostics answers it. Until
> it works, backtests cannot use real bars.

---

## DIAG-1 — The diagnostics panel reported a working feed that has never worked

The capability panel said **"Live quotes: Yes"**. Quotes have never succeeded on
this account. Everything built on that answer — including the advice that the
paper strategy runner was unblocked — was wrong.

`_probe` marked a call `ok` whenever it did not raise:

```python
try:
    raw = fetch()
except Exception as exc:
    return {"ok": False, ...}
return {"ok": True, ...}          # never inspected the envelope
```

**Choice reports most failures as HTTP 200 with a failure envelope** —
`{"Status": "Fail", "Reason": "Index was outside the bounds of the array"}` —
which the SDK returns without raising. The gateway has always checked that
envelope with `is_failure`; the diagnostics probe did not. The one screen built
to tell the truth about the connection was the one place that skipped the check.

**Fix:** `_probe` checks `is_failure(raw)` as well as the exception, and reports
Choice's own reason. Capabilities are derived from the corrected probes.

*File:* `backend/app/api/v1/diagnostics.py`

### What this cost, and the wrong turns it caused

Three separate attempts were made to work around a "Choice rejects a
single-instrument quote request" theory, built on the observation that the
two-pair diagnostics call "succeeded" and the one-pair runner call failed. The
two-pair call had been failing the whole time; the probe simply reported it as
working. The padding was removed — it fixed nothing, and the two-pair form
fails identically.

The response filtering introduced alongside it was **kept**: Choice has been
observed returning rows for instruments that were not requested, and
`_market_fill_price` takes the first quote carrying a price, so an unrequested
row would fill an order at the wrong instrument's price. That is correct
regardless of why a stray row appears.

**The lesson is the one this codebase keeps relearning:** a check that treats
"the server answered" as "the call worked" produces confident, wrong answers —
and a diagnostic that lies is worse than no diagnostic, because everything
downstream inherits the lie.

---

## SYM-1 — Every symbol-resolved backtest sent a 300-instrument list as its token

The backtest failure read:

```
Could not fetch historical data for RELIANCE (token [{'Token': '2885', ...},
{'Token': '144404', ... 'SecDesc': 'RELIANCE26SEP1380CE'}, ... ])
```

`ScripMaster.get_token` is documented to return **a list of every matching row**
when no segment is given — for RELIANCE, the share plus several hundred options
and futures. `get_token` in the gateway then did `str(token)`, turning that list
into a "token" thousands of characters long and sending it to Choice.

**This is why historical data appeared unavailable.** It was diagnosed twice as
a possible entitlement gap; it was never that. Both earlier suspicions were
wrong, and the same request also carried the `"1d"`-instead-of-`"D"` interval
fault recorded as HIST-1 — two independent faults in one call, each capable of
masking the other.

**Fix:** when the SDK returns a list, one instrument is chosen — cash segment
first, then the `EQ` series, then an exact description match. "RELIANCE" now
resolves to `2885`, the share, rather than `RELIANCE26SEP1380CE`. A derivative
is returned only when nothing else matches, so a deliberate futures lookup
still works, and the choice is logged because silently picking one instrument
out of hundreds should be visible.

*File:* `engine/app/choice_gateway/scrip_master.py` · *Tests:* 5

---

## Over-engineering audit — 13 August 2026

Repo-wide pass for speculative code, hand-rolled equivalents and dead
dependencies. Ranked biggest cut first.

| Cut | What | Why |
| :--- | :--- | :--- |
| **~45 lines** | A hand-rolled SHA-256 keystream cipher plus manual HMAC in `session_store.py` → **Fernet** | `cryptography` was already a declared dependency (49.0 installed, pulled in by `python-jose[cryptography]`). Writing a cipher when a real one is installed is how you get something weaker in more lines. Fernet is AES-128-CBC with an HMAC-SHA256 signature, so tampering fails authentication instead of yielding plausible nonsense. |
| **4 dependencies** | `fastapi`, `uvicorn`, `websockets`, `websocket-client`, `httpx` from `engine/requirements.txt` | The engine imports none of them. The web framework outlived the engine HTTP service deleted in an earlier audit; websocket transport lives inside the SDK; the engine uses `requests`, not `httpx`. |
| **3 definitions** | `UserCreate` schema, `RunRegistry.for_owner`, `PaperRunScheduler.active_ids` | Never referenced anywhere, including tests and both frontends. The last one was added the same day and never called — speculative flexibility. |
| **~60 lines** | The `MultipleTouchline` padding workaround | Built on a false reading of a broken diagnostic (DIAG-1). It fixed nothing, so it went. The response *filtering* introduced with it was kept: Choice has returned rows nobody requested, and a stray row would fill an order at the wrong instrument's price. |

### Deliberately left alone

* **`sockets_interactive.describe_order_status` and `sockets_pricefeed.subscribe`** —
  the only unreferenced code remaining. Both belong to the socket subsystem
  documented as connect-and-subscribe-but-not-yet-wired. They are the seam for
  replacing quote polling with the live feed, and cutting them would mean
  rewriting them.
* **Twenty helper functions duplicated between the two frontends** (`money`,
  `esc`, chart primitives). Sharing them needs a build step, and a bundler is a
  larger cost than the duplication it removes for two single-file pages that
  must run offline.
* **The Fernet key derivation from `SECRET_KEY`.** Encryption at rest was
  explicitly asked for in the phase plan. It is documented honestly: the
  ciphertext is sound, but the key sits on the same machine, so it defeats a
  backup scraper rather than someone at the keyboard.

**net: -4 dependencies, roughly -110 lines, 212 tests still passing.**

---

## BUILD-2 — A cleanup shipped a binary that crashed on every broker call

Replacing the hand-rolled cipher in `session_store.py` with Fernet was correct in
the source and passed 212 tests. It still shipped a broken executable:
PyInstaller bundles `cryptography.fernet` only when it is named explicitly, so
the frozen app raised `ModuleNotFoundError` on `/auth/choice/status` and
`/auth/choice/connect` — every broker call, 500.

The regression is not the interesting part. **`verify_exe.py` reported 27/27 while
this was broken**, because no check in it ever reached the session store. A
verification suite that cannot fail for a whole subsystem is not evidence about
that subsystem, and reporting its total as a pass was the more serious error.

- Fixed: `cryptography.fernet` added to the hidden imports, `cryptography` to
  `--collect-submodules` — `client-desktop/build_exe.py`.
- Fixed: `verify_exe.py` now drives `/auth/choice/status`, a `remember=true`
  connect, and a planted undecryptable record.

**Tests run against the installed package; the binary is a different
environment.** Any change to imports needs exercising in the executable itself.

## STORE-1 — "Remember my session" had never worked in the desktop app

Found by the check written for BUILD-2, and the reason that check was worth
writing at all.

`_cipher()` derived its key from `SECRET_KEY` and raised when there was none.
`engine/app/config.py` defines no `SECRET_KEY`, and `backend/app/config.py`
generates a **random one per process** when it is unset — which the desktop app
always is. So `save()` caught the error, logged a warning, and returned. The user
ticked "remember me", nothing was written, and after a restart they were
disconnected with no explanation: precisely the failure the module was built to
prevent, reintroduced by its own key handling.

Two rounds of the check were themselves wrong before they caught this — the
first ran with a live in-memory session that short-circuits `load()`, so it
proved nothing. Same short-circuit that has produced false passes twice before
in this audit.

- Fixed: `_local_key()` keeps a generated key beside the store when no
  `SECRET_KEY` is configured; `SECRET_KEY` still wins where a deployment sets
  one. Refusal is now limited to the case where no key can be kept at all.
- The threat model is unchanged from what the module already documented: this
  stops a file read or a backup scraper, not someone at the keyboard. DPAPI
  remains the upgrade.
- `test_nothing_is_written_without_a_signing_key` asserted the broken behaviour
  and was replaced by three tests, including one that the key survives a restart.

## UI-2 — Every chart's empty state was dead code

Found while adding charts, not by looking for bugs.

`index.html` declared `emptyState` twice: `emptyState(host, title, detail)` in the
chart engine, and `emptyState(what)` for empty tables further down. Both are
hoisted, the later declaration wins, so all four chart callers were invoking the
*table* version — passing a DOM node where a noun was expected, getting an HTML
string back, and discarding it. The host was never written to.

The visible effect was a chart with no data drawing nothing at all: no message,
no error, just a blank card. Precisely the case where a reader most needs to be
told whether the account is empty, not connected, or still loading.

- Fixed: the chart-engine function is now `chartEmpty()`, with a comment saying
  why the two names are kept apart.
- The five new empty-state checks in `frontend-user/verify_charts.py` fail
  against the old code.

**No linter would have caught this.** Both functions were used; neither was
undefined. Only rendering the thing shows it.

## Charts added — 13 August 2026

Three, all from data already on the wire — no new endpoint, no new dependency,
and reusing the existing SVG engine rather than adding a chart library:

- **Today's movers** (Overview) — day P&L per holding. Total P&L answers "am I
  up"; a holding can be far ahead overall and still be what fell this morning.
- **Return distribution** (Backtest) — a histogram of per-trade return. Win rate
  reads identically for a strategy taking many small gains against rare large
  losses and for one doing the reverse; this is where they stop looking alike.
  Captioned in words, because a distribution is only self-evident to someone who
  already reads distributions.
- **Result by month** (Backtest) — closed-trade P&L grouped on exit date.

Motion was added to the same engine: bars grow from the axis they are measured
against and lines draw along their own path, one pass on load, with hovered
marks lifting out of a dimmed field. The existing `prefers-reduced-motion` rule
collapses all of it.

`frontend-user/verify_charts.py` drives the engine and the page's render
functions in headless Edge — 23 checks, and each one was mutation-tested to
confirm it fails when the thing it covers is broken.

## Strategy page rebuilt as a builder — 13 August 2026

The page asked a trader to hand-write the DSL in a JSON textarea. `explain.py`
had already called this out in its own docstring: *"if the sentence makes the
DSL legible enough to edit confidently, a visual builder may not be needed at
all. That decision wants evidence."* The evidence arrived — the page was
unusable for anyone who does not write JSON.

**What it is now.** Each rule is a sentence assembled from three controls:
`[field] [comparison] [number or another field]`. Indicators are chosen inside
the field picker with their period inline, rather than declared separately and
referenced by name — which removes the step where a misspelt reference produced
a strategy that silently never traded.

- Four starter templates (RSI dip, moving-average crossover, MACD momentum,
  Bollinger bounce). The page opens on one rather than on a blank form.
- A live plain-English reading of the rules, beside the builder.
- `POST /strategies/preview` validates and explains a draft without saving.
- The JSON is still there, collapsed, and still editable. Importing rules the
  builder cannot represent leaves the builder alone rather than half-importing.

**The preview is served by the engine, not reimplemented in the browser.** Two
implementations of "what does this strategy do" that can disagree is the exact
fault this audit has spent the day removing; a preview that quietly disagrees
with the validator is worse than no preview. A test asserts the two agree.

**Known limit:** conditions combine with AND only, because that is what
`evaluate_conditions_all` does. The builder states "every condition must be
true" and prints the joiner between rows rather than implying it. OR would be
an engine change, and is not pretended at in the UI.

### Two name collisions, one of them live

`previewTimer` was declared twice once the builder was added — caught by
`node --check`, because `let` collides loudly. `emptyState` (UI-2 above) was
declared twice as a `function`, which does not collide: the later declaration
silently wins.

`verify_ui.py` now scans for duplicate top-level function names, since that is
the half of this fault class no tool reports.

## FMT-1 — The header widened the document between 900px and 1240px

The topbar's own comment called this out: *"a page that scrolls sideways hides
content and is the one layout fault worth never shipping."* It then gated the
fix behind `@media (max-width: 900px)`, leaving a 340px band where the nav no
longer fits and scrolling has not yet been turned on. Every view overflowed by
49–64px on a 1156px window — a common laptop width.

`overflow-x: auto` shows no scrollbar when the content fits, so the media query
was never doing anything a plain declaration does not. **The fix deletes the
breakpoint** rather than adding another one.

## FMT-2 — Grid columns refused to shrink, so tables took the page sideways

A grid item's `min-width` defaults to `auto`: it will not shrink below the
widest thing inside it. The Positions view put an eight-column table in a
half-width grid column, so the column pushed past the viewport — and
`.tbl-wrap`'s own `overflow-x: auto` never engaged, because the wrapper was
never made narrow enough to need it. 220px of horizontal scroll at 492px.

Fixed with `min-width: 0` on grid children. No current view still puts a wide
table in a grid column, so the layout sweep now *injects* one to keep the rule
covered — an unexercised rule is an unverified one.

## FMT-3 — Card headers with three actions could not wrap

`.card-head` was a non-wrapping flex row. The diagnostics card carries a title
and three buttons, which overflowed its card by 23px on a narrow window. Now
`flex-wrap: wrap`.

### How these were found

Not by looking. `verify_ui.py` now renders every view at five widths with
representative data in it, and fails on: the document scrolling sideways, any
element overflowing a container that is not a scroller, controls under 24px
tall, and unlabelled cards. It names the offending element and how far past the
edge it reached.

An empty page never overflows, so the sweep fills every table first — a page
audited empty is audited in the one state nobody uses it in.

## Positions and Overview — 13 August 2026

**Positions.** Holdings was an eight-column table in half a screen, with no
totals: the reader was left summing rupees by eye, which is the arithmetic the
page exists to do.

- Both tables are full width now.
- A totals row under Holdings, and four summary tiles above it.
- Overall return is weighted by capital invested, **not** the mean of the
  per-holding percentages. A big position up 10% beside a small one down 50% is
  +4.5%; averaging says −20%.
- The day figure says when it covers only part of the book ("1 of 2 holdings
  priced today") rather than presenting a partial total as the whole day.
- The order log filters to Rejected or Working. A filter matching nothing says
  so rather than claiming the account has no orders.

**Overview.** The charts showed the largest 8 and dropped the rest silently —
someone holding twenty names saw eight bars and could reasonably conclude they
held eight. Each now states what it left out and how much of the total the bars
account for: *"top 8 of 12 · 75% of the total"*.

## A11Y-1 — Status tags failed contrast in both themes

Found by a check written while restyling the KPI cards, not by looking.

Coloured text on a tinted wash of the same hue is the pattern that quietly
drops under 4.5:1, and four tags were under it:

| Tag | Theme | Ratio |
|---|---|---|
| `SIMULATED` (info) | light | 3.89:1 |
| `SELL` / `REJECTED` (down) | dark | 4.30:1 |
| `SELL` (down) | light | 4.49:1 |
| Diagnostics "Not checked" | light | 3.94:1 |

The cause is that `--up` / `--down` / `--warn` / `--info` were validated against
the flat card surface and then reused as text on a *lighter, tinted* ground.

**Fix:** separate `--*-accent` tokens per theme, used only for text on a soft
background — the same split the shadcn badge makes with
`--color-success-accent` and friends. The data palette is untouched, so charts
and P&L figures keep the colours that were validated for them.

`verify_ui.py` now composites each badge's translucent background down to the
first opaque ancestor and computes the WCAG ratio, **in both themes explicitly**
— headless Edge reports one `prefers-color-scheme`, so testing whatever it
happens to report leaves half the palette unchecked.

## KPI cards restyled — 13 August 2026

Applied across all thirteen tiles (five on Overview, four on Positions, four on
Backtest), in the app's own CSS:

- The change is a **tinted badge** beside the figure rather than loose text, so
  "how much" and "which way" read as one phrase. The arrow glyph stays — colour
  is never the only signal.
- The footer is **ruled off and pinned to the bottom**, so a row of tiles rules
  at one height however the label above wraps, and it names its baseline with
  the value in the foreground weight: *"Vs cost: ₹4.2L"*.

Three delta spans were wrapping a delta inside another delta, which would have
rendered a pill inside a pill; those are now plain slots.

**Not adopted:** the source card's per-card overflow menu (Settings, Add Alert,
Pin, Share, Remove). None of those exist as features here, and a menu of items
that do nothing is worse than no menu.

## LOG-1 — A browser hanging up printed a stack trace labelled ERROR

Every startup logged:

```
ERROR asyncio: Exception in callback _ProactorBasePipeTransport._call_connection_lost()
ConnectionResetError: [WinError 10054] An existing connection was forcibly closed
```

Nothing was wrong. It appears once, between opening the browser and the first
request, and every request after it succeeded. Windows' asyncio proactor raises
`ConnectionResetError` from the callback that tears a connection down, and
asyncio logs any unhandled callback exception at ERROR with a traceback.

**The cause was not established.** Three candidates were tested against a bare
server and none reproduced it:

| Hypothesis | Result |
|---|---|
| Readiness probe reads `status` but never the body | not reproduced |
| Client connects and drops the socket (RST, with and without a request) | not reproduced |
| Proxy builds a new `httpx.AsyncClient` per request | not reproduced |

The remaining likely trigger is the browser's own speculative connection, which
is not reproducible outside a real browser.

**So this fixes the reporting, not the cause, and says so.** A filter on the
`asyncio` logger drops the record only when the exception is
`ConnectionResetError` / `ConnectionAbortedError` / `BrokenPipeError` *and* the
message names `_call_connection_lost`. A reset from anywhere else, or any other
exception during teardown, still surfaces — there are tests for both, because
the risk of a filter like this is that it grows to hide something real.

Installed in the launcher process only: the browser talks to that server, and
the backend is reached through the proxy, which reads full responses.

**Why it is worth fixing at all.** In an application where a real fault costs
money, a benign line formatted as an error trains the reader to skim past the
line that matters.

### The desktop tests now run with everything else

`client-desktop/tests` was added to `pytest.ini`. It collected fine alone and
failed in the full suite: the desktop client and the backend both provide a
top-level `app` package — the documented reason the launcher spawns the backend
as a separate process — and the backend's wins. The test loads the desktop
package under a name of its own, so one `pytest` runs all 226.

## DATA-1 — Invented intraday bars were labelled as real market data

**Severity: the highest in this report.** Everything else here produced a wrong
number or a confusing screen. This one produced a *convincing* number.

The Marketstack integration added a fallback: when the subscription had no
intraday endpoint, `_synthesize_intraday_from_eod` expanded each daily bar into
a modelled trading day and returned it with:

```python
"is_real_market_data": True,
"derived_from": "EOD_BARS",
```

Run against one real daily bar, it produced:

- 25 fifteen-minute bars from a single day
- **three direction changes across an entire trading day** — a real one has dozens
- identical volume in every bar
- a path of smooth monotonic ramps open → high → low → close

A momentum or trend strategy scores extremely well on that shape, because there
are almost no reversals to lose money on. And the interface reinforced it: the
provenance banner only warned on `SANDBOX_SYNTHETIC`, so a `MARKETSTACK` run
displayed *"Choice OpenAPI historical data — costs and slippage are modelled"* —
the wrong provider, and an assurance the bars were real.

The end state was a backtest that looked like verified exchange data, scored
well because of the synthesis model, and could have been traded with real money.

**Fixed by deletion.** The synthesiser is gone (96 lines). An intraday request
is served from the intraday endpoint or refused, naming the way forward:

> Marketstack has no 15m data for RELIANCE. Intraday bars need a Marketstack
> plan that includes the intraday endpoint; on an end-of-day plan, run this
> backtest on a Daily timeframe instead.

A backtest that cannot run is recoverable. One that is quietly wrong is not.

### The banner now names the provider

`SOURCE_NOTES` maps each source to its own wording, and **an unrecognised source
warns** rather than inheriting the reassuring branch. Any source added later
reads as unverified until it is listed — the safe direction to be wrong in.

### Tests

Four in `test_marketstack.py`, including one asserting the method no longer
exists, since its output was indistinguishable from real data to everything
downstream. Four in `verify_ui.py` covering each banner branch. Both fixes were
mutation-tested: reinstating the substitution fails the engine test, and
restoring the old banner default fails the UI check.

## A11Y-2 — Keyboard users had no focus ring on any input

Found by measuring, after the UX rule database flagged focus states as a
priority-1 check.

`.field input:focus { … outline: none; }` out-specifies the global
`:focus-visible` rule, so the ring never appeared. Measured in headless Edge:

```
field input, focused     outline-style=none    width=3px
button, focused          outline-style=solid   width=2px
```

Every input in the app — the login form, the order ticket, the strategy builder
— left a keyboard user with no indication of where they were. Buttons were
fine, which is why it was invisible in casual use.

**Fixed:** the suppression is now scoped to `:focus:not(:focus-visible)`, so
pointer focus stays clean and keyboard focus shows the ring.

### Three more from the same pass

- **No `<h1>`.** Headings started at `<h2>`. The product name is now the
  document heading, styled to sit inside the brand lockup.
- **No skip link.** The topbar carries a brand, five nav buttons, a mode chip
  and five actions — about a dozen tabs before reaching data. A skip link now
  appears on first Tab and is invisible otherwise.
- **The toast was visual only.** It carries order confirmations, rejections and
  halt alerts, and none of it reached a screen reader. It is now
  `role="status"`, switching between `aria-live="polite"` for confirmations and
  `assertive` for failures — a rejected order interrupts, a save waits its turn.

All four are enforced in `verify_ui.py` (43 layout/accessibility checks per
width) and mutation-tested: reinstating the bare `outline: none`, removing the
skip link, dropping the live region and downgrading the `h1` each fail the run.

### On the two skills used

`ui-ux-pro-max` is stack-agnostic and its priority-1 and -2 categories mapped
directly onto this codebase. `ui-styling` is React/Tailwind/shadcn throughout;
only its accessibility reference applied, and it named the same four patterns.
No design system was regenerated — this app already documents one in its own
CSS header, and the skill's own guidance is not to overwrite prior decisions.

## UX-1 — Nothing refreshed on its own

There was no `setInterval` anywhere in the interface. Portfolio, orders, funds
and risk figures were fetched on sign-in, after placing an order, and when the
Refresh button was pressed — and at no other time. Watching a position meant
pressing Refresh, repeatedly.

**Fixed:** a 20-second poll, plus an immediate refresh when the tab is brought
back to the front. Two hazards had to be handled first, because either would
have made an auto-refresh worse than no auto-refresh:

- **Skeleton rows.** `refreshAll` replaced populated tables with grey
  placeholders. On a timer that reads as the data being lost, every twenty
  seconds. Background refreshes now run `quiet`, leaving the rows in place;
  the first load still shows skeletons, where there is nothing to preserve.
- **The strategy select.** `loadStrategies` rebuilds it with `innerHTML`, which
  discards the selection. On a timer that silently reset the strategy chosen
  for a backtest while it was being set up. The choice is now preserved.

Also gated: a hidden tab does not poll — spending a rate-limited broker
allowance on data nobody is reading — and a refresh already in flight is never
stacked, so a slow response cannot queue several and fire them together.

**A freshness readout** sits in the header: "just now", "18s ago", and marked
stale past three intervals. Without it, live figures and frozen ones look
identical, and a stale number that looks live is worse than an obviously stale
one.

### The first cadence was too expensive, and was cut

Twenty seconds across all five endpoints is **fifteen requests a minute**, on
every view, against a broker API with a rate limit — and most of it was data
that had not changed. Three changes, largest saving first:

- **Only what the open view shows.** Reading the strategy builder does not
  require re-fetching holdings. `VIEW_LOADERS` maps each view to its endpoints;
  the daily-loss check runs everywhere, because it raises the halt alert.
- **Only what moves on its own.** The strategy list changes when one is saved,
  which already reloads it. It never needed a timer and is off the poll
  entirely.
- **A minute rather than twenty seconds**, with Off / 1 min / 5 min in the
  header, persisted.

| | Before | After |
|---|---|---|
| Overview / Positions | 15 req/min | **4 req/min** |
| Strategy / Backtest / Health | 15 req/min | **1 req/min** |
| Tab hidden | 15 req/min | 0 |
| Auto refresh off | n/a | 0 |

The manual Refresh still fetches everything, and switching to a data view
refreshes it once on arrival — the poll skipped it while it was hidden.

The request counts are asserted, not estimated: the checks count loader calls
per tick and fail if a view fetches more than it shows.

### The harness broke while testing this

Three findings about `verify_ui.py` itself, all found by the failures they
caused:

1. Top-level `await` is illegal in a classic `<script>`, so the refresh checks
   killed their whole phase. `GUARD` is now an async IIFE and wraps every
   harness.
2. Rewriting `GUARD`, an escaped `"\n"` collapsed into a real newline —
   an unterminated string, so the guard meant to *report* failures became the
   thing that failed, in all five phases at once. The stack-splitting line it
   needed the escape for is gone; the message alone is enough.
3. `const before` collided with an identifier declared earlier in the same
   harness. `node --check` on the guarded body found it in seconds; the browser
   had just reported nothing.

The "nothing was reported — the code under test threw" fallback is what made
all three visible rather than silently passing.

### Verification

Five mutations, all detected: skeletons on every refresh, the in-flight guard
removed, hidden tabs polling, the select selection dropped, and the stale
marker disabled.

The select check was **vacuous on first writing** — `loadStrategies` fetches
before it rebuilds the select, and on a `file://` page the fetch throws, so the
select was never touched and the check passed regardless. It now stubs `api`
so the code under test actually runs.

## ENV-1 — Six places still reported the deployment default after the environment became per-session

Making the Choice server a per-connection choice moved the truth, and several
places kept reading the old location. Every one of them would have told a user
one thing while the app did another.

| Where | What it did | Severity |
|---|---|---|
| `orders.py` order log | Recorded the install default, so an order sent to UAT was logged as PROD | **Records integrity** |
| `session_store.save` | Stored the install default, so a UAT session came back claiming PROD | High |
| `ChoiceSession.restore` | Discarded any session whose environment differed from the default — i.e. every deliberately-chosen one — and restored `base_url` without `environment`, so the app reported one server while calling the other | High |
| `errors.py` auth hint | "This deployment is pointed at production…" — the wrong advice for a user on UAT | Medium |
| `diagnostics.py` | Reported the default, when diagnostics exist to say what is *actually* happening | Medium |
| Startup banner, admin | Implied every session used the default | Low, relabelled "default" |

**Fixed.** Failures now carry the server they came from (`_stamp`), restore
resolves `base_url` from the validated stored environment rather than a stored
URL, and a stored environment this build does not recognise is discarded —
because `base_url` and the reported environment disagreeing is the one outcome
that must never happen.

### Verified at the level that matters

Not "does it report the right string", but **which URL the HTTP client is built
with**:

```
UAT   -> https://uat.jiffy.in            matches the chosen server: True
PROD  -> https://finxomne.choiceindia.com matches the chosen server: True
none  -> deployment default              True
```

Seven tests cover routing, reporting, the store round trip, restore, and
rejecting an unknown stored environment. Four mutations detected.

## UX-2 — "Portfolio value" excluded open positions, silently

Holdings and positions are different things, and the app never said so.
`state.holdings` alone feeds the portfolio value; `/portfolio/positions` was
rendered straight to a table and never entered the summary. Someone holding a
large intraday position saw a portfolio figure that excluded it, with nothing
on screen indicating a scope rather than a total.

The data separation was already correct — paper fills never touch the real
positions table, and holdings and positions come from different Choice
endpoints. The fault was entirely in what was *said*.

**Fixed:**

- Holdings: *"Shares settled into your demat account. Yours until you sell them,
  carried over every night."*
- Positions: *"Bought or sold today and not yet settled — intraday and
  derivatives. These are **not** counted in the figures above."*
- The positions card names the excluded amount: *"1 open · ₹5.3K not in
  portfolio value"*
- The portfolio tile names the exclusion: *"Vs cost: ₹1.0K · 1 holding ·
  excludes 1 open position"*

### An ordering bug in the fix itself

`loadPortfolio` fetches holdings first and positions second. Computed inline,
the exclusion note ran before the position list existed and only appeared on the
*next* refresh — right sometimes, which is worse than absent. Extracted into
`renderPortfolioScope()` and called after positions arrive.

The check for it was **vacuous at first**: it called `renderHoldings` directly
rather than the load path where the ordering lives, so deleting the fix changed
nothing. Rewritten to drive `loadPortfolio` with a stubbed fetch.

### And a fifth escape collapse

A `\b` word boundary written for a JS regex through a Python heredoc became a
literal backspace character (`0x08`) in the test file. The check passed on the
plural and failed on the singular for reasons nothing on screen explained.

Replaced with substring comparisons. **Every backslash written for JavaScript
inside a Python string is a chance for the two escape layers to disagree**, and
this session has now lost time to that five separate times. String methods
where a regex is not essential.

## MODE-1 — Paper and live were a connect-time decision, not a choice

There was no mode-switch endpoint at all. Changing between paper and live meant
a full reconnect — re-typing the API key and completing the TOTP flow — even
though the session already held a valid key and session id.

**Now `POST /auth/choice/mode` switches either way on a connected session.**
Someone signed in with their own credentials decides whether their own orders
are real; making that a reconnect was friction without a matching benefit, and
it pushed people to keep their API key somewhere convenient, which costs more
security than the friction bought.

The two directions are deliberately **not symmetric**:

| Direction | Requires | Why |
|---|---|---|
| Live → Paper | nothing | Reducing what a session may do proves nothing by being slow. Someone who wants to stop risking money must never have to find a credential first. |
| Paper → Live | an explicit confirmation | Not a second password — the user authenticated minutes ago — but a deliberate act, so a mis-click cannot turn simulated orders into real ones. |

**A sandbox session can never become live**, whatever is sent. It has no Choice
login behind it, so "live" would mean orders with nowhere to go and a user who
believes they are trading for real when they are not.

The broker session, market data, holdings and Choice session id are untouched by
either switch. Only the permission to submit changes.

### In the interface

One control beside the mode chip, labelled with the *action* rather than the
state, because the chip already says the state: **Go live** in paper, **Switch
to paper** in live, hidden entirely for a sandbox session.

Both confirmations state what will change rather than asking "are you sure":

> **Going live** — "Orders from this session will be submitted to your broker
> using real money. Your strategies, holdings and market data do not change.
> You can switch back to paper at any time, in one click."
>
> **Leaving live** — "Positions already open at your broker are not touched —
> close those with the broker, or by placing orders, before switching."

That last line matters: it is the first thing anyone would wonder, and the
answer is not obvious.

### Verified

Ten tests and four mutations detected: going live without confirming, a sandbox
session escalating, the safe direction being gated too, and the browser dialog
being ignored.

One check caught a real wording fault — the phrase "real money" had been split
across a line break in the template literal, so the sentence read correctly on
screen but the guarantee was not the one being tested. Rewrapped.

## RISK-1 — Paper losses halted real trading

**The most serious finding since the intraday synthesiser.** Reproduced before
fixing:

```
paper realised    :   -10,000.00
risk manager sees :   -10,000.00   (cap 5,000)
a REAL order is REFUSED: Daily loss limit of 5,000.00 reached;
                         no further orders will be sent today.
```

Simulated losses and real losses shared one ledger. A losing paper strategy —
which this app actively encourages people to run first — consumed the allowance
protecting real funds and tripped the kill switch, halting the account for the
rest of the day. The message read *"Daily loss limit reached"* and gave no hint
that none of it was real.

This was latent while switching modes required a full reconnect. Making the
switch one click, earlier the same day, is what turned it into something a user
would hit.

**Fixed:** two ledgers.

| | Governs | Trips the kill switch |
|---|---|---|
| `_realized` — money actually lost at the broker | real orders | yes |
| `_simulated` — what a paper strategy would have lost | paper orders | **no** |

A paper order that breaches its budget is refused with *"Paper daily loss limit
reached. Real trading is unaffected."* — because someone reading that at 2pm
needs to know instantly whether their real account is stopped.

The kill switch is deliberately not tripped by a simulated loss: halting is
shared, so tripping it there would stop real orders through the back door — the
same bug wearing a hat.

Which budget an order spends is decided by the session, not by a caller
remembering a flag: `place_order` passes `simulated=session.simulates_orders`.
A test asserts that at the call site, because passing the flag explicitly in a
test proves the risk manager works and says nothing about whether the gateway
ever tells it the truth.

The risk strip shows both figures side by side, never summed.

### Verified

Twelve tests, four mutations detected: merging the ledgers, booking paper fills
to the real one, letting a paper loss trip the kill switch, and the gateway
hard-coding the flag.

**One test was measuring nothing.** `orders.py` binds `risk_manager` at import
while `client_manager.py` imports it inside the function, so patching only the
module left the two halves of a test talking to different managers — it
reported a pass either way. The fixture now patches both bindings, and the
suite was run three times to confirm the result is stable rather than
order-dependent.

### Confirmed safe, and worth stating

- **A running paper strategy cannot be flipped to real orders.** `_submit`
  branches on the *run's* own mode, fixed when the run starts, not the
  session's. Switching the session to live mid-run leaves the strategy
  simulated.
- **The broker's own realised P&L** still writes to the real ledger, and only
  when the session is live.

### A known limit, stated rather than papered over

Strategy paper fills never reach `record_simulated_fill` — the runner's PAPER
branch returns a fill without going through the order gateway — so the paper
budget covers manual paper orders only, not strategy runs. Left alone: a
runaway paper strategy costs nothing real, and the runner already halts on feed
gaps, credential expiry and repeated failures.

## RISK-2 — A halt was too broad, then briefly too narrow

Found by double-checking RISK-1. Once the kill switch tripped, every order was
refused, simulated ones included:

```
kill switch tripped: True
paper testing after the halt: BLOCKED - Trading is halted for today
```

**The first fix was wrong**, and the exe verification caught it where the unit
tests did not: scoping every halt to real orders made the kill switch a
**no-op for a paper user**. A kill switch that does nothing is worse than one
that is too broad — the unit tests passed because I had written them to the
same incomplete model as the code.

The real distinction is not real-versus-simulated. It is *why the halt was
raised*:

| Halt | Means | Stops |
|---|---|---|
| Kill switch, or an administrator | "stop what I am doing" | everything, paper included |
| Daily loss cap on real money | "protect my funds" | real orders only |

Simulated orders risk nothing, so a loss-cap halt has no reason to block them —
and blocking them prevents the one useful thing to do after a bad day. But
somebody pressing the button means all of it.

`halt()` now carries a scope, and the kill-switch wording says plainly that it
stops orders "real or simulated" and that running strategies will stop.

The paper budget still applies while halted: "paper keeps working" is not
"paper has no limits".

Six tests covering both halt kinds in both directions.

## LEAK-1 — Finished strategy runs were never released

`RunRegistry` only ever added. A process left open accumulated every runner it
had created, each holding a session reference and its order history.

Safe to prune because `run_status` reads the database row first and consults the
registry only for a *live* run's metrics — `_close_run` has already persisted
the final figures by then. Verified before changing anything.

Terminal runs are dropped as new ones are added. `IDLE` is deliberately not
terminal: a runner is registered before it is started, and pruning one mid-setup
would lose a run about to trade.

**The first version referenced `RunState.HALTED`, which does not exist** — the
states are IDLE, RUNNING, STOPPED, FAILED, and "HALTED" is a database status for
the row rather than anything a runner holds. Test collection caught it
immediately. Three mutations detected, including pruning running and idle runs.

## Double check — every claim re-proved

Run against the current source rather than trusted from earlier in the session:

```
[PASS] UAT: client built against the chosen server
[PASS] PROD: client built against the chosen server
[PASS] a paper loss leaves real trading alone
[PASS] a paper loss does stop further paper orders
[PASS] a paper loss does not trip the kill switch
[PASS] a real loss stops real orders
[PASS] a halt still leaves paper testing available
[PASS] a running paper strategy stays simulated after a switch
[PASS] a profitable short books +200

9/9 claims re-proved
```

Also confirmed clean this pass: licensing is genuinely inert in an unlicensed
build (`check()` returns `disabled`, the startup gate passes through), the
broker's own realised P&L still writes to the real ledger and only when live,
and per-owner state in the risk manager and session registry is keyed and
cleared correctly.

## Housekeeping

- `client-desktop/dist/algo.db` is now orphaned, since the exe writes to
  `%LOCALAPPDATA%\ChoiceFinxTrader`. Left in place in case it holds accounts you want.
- `pytest` and `pypdf` were installed during the audit. `pytest` is a declared
  dependency; `pypdf` was only used to read the specifications and can be removed.
- The project is still not under version control. `git init` before the next change
  would be worth the thirty seconds.
