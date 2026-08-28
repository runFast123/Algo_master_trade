# What to build next

Written 12 August 2026, after the audit and the price-scaling fix. Ordered by
value per unit of effort, not by how interesting it is to build.

> **Status, 12 August 2026.** Everything below except the strategy runner
> (§1) and the strategy-authoring items in §5 has been **built and tested** —
> see "Delivered" at the end. The runner was deferred deliberately.

Everything here is judged against what the code actually does today. Where a
feature is blocked by something outside the code — an entitlement, a
certification — it says so rather than pretending it is a coding task.

---

## 1. The gap that matters more than everything else

**Strategies cannot be run.** `engine/app/strategy_engine/runner.py` is 563
lines of working execution logic — order routing, position tracking, stop and
target handling — and nothing calls it. There is no `POST /strategies/{id}/start`.

Today the product can define a strategy, backtest it, and show you a portfolio.
It cannot trade one. For an algorithmic trading platform that is the whole
premise, and it is the single largest gap between what this is and what it is
meant to be.

### Ship it in PAPER first

This is the important part. **Paper mode sidesteps every blocker at once:**

| Blocker | Applies to paper? |
| :--- | :--- |
| Static-IP origination (OPEN-1) | No — no order leaves the process |
| UAT certification (OPEN-3) | No |
| Vendor empanelment | No |
| Order price unit unconfirmed (OPEN-2) | No — fills are local |

A paper strategy runner needs real *market data* but no *order permission*. It
is shippable now, it is genuinely useful, and it is how a trader builds
confidence in a strategy before risking money. Live execution then becomes a
mode flip on a proven path rather than a new feature.

**What it needs**

1. `POST /strategies/{id}/start` and `/stop`, writing a `StrategyRun` with
   mode PAPER.
2. A scheduler — a single asyncio task per run is enough at desktop scale.
   Out-of-process workers only matter for a shared server.
3. A run monitor in the UI: live position, orders placed, P&L against the
   strategy's own baseline, and a stop button that is always reachable.
4. Restart recovery. The desktop app *will* be closed mid-run; a run that
   silently dies and leaves a phantom position is worse than one that refuses
   to start.

**Do not skip 4.** It is the difference between a demo and something a person
can leave running.

---

## 2. KPIs you are already paying for and throwing away

Every item here uses data the app **already fetches on every refresh**. No new
API calls, no entitlement, no permission. This is the cheapest work in the
document.

### Day change — the single most-looked-at number in any broker app

`close_price` is now computed correctly on every holding *and consumed by
nothing*. Day P&L is `(current_price - close_price) × quantity`.

Right now the dashboard shows total return since purchase. A trader opening the
app wants to know **what happened today**, and the app cannot tell them —
despite having the number in hand. Conflating the two is also how a portfolio
up 18% overall hides the fact that it fell 3% this morning.

Add it as a KPI card and a per-row column, and split the header into
*today* and *total*.

### Realised vs unrealised

`funds.py` already normalises `realized_pnl` and `unrealized_pnl`, and neither
appears in the interface. These answer different questions — booked profit is
money, paper profit is an opinion — and traders treat them completely
differently at tax time.

### Margin headroom, as a number rather than a bar

`used_margin`, `AvailableMargin` and `total_collateral` are all normalised.
The margin meter shows a bar; it should show **how much more you can deploy**
and what fraction of your limit is collateral rather than cash.

### Concentration risk

From holdings alone: largest position as a share of the portfolio, and the top
three combined. A book where one name is 60% of the value is a different risk
profile from an evenly spread one, and nothing on the current dashboard
distinguishes them.

### Cost basis quality

Per holding: absolute and percentage return, and distance from the day's close.
Sorting by *worst performer* is how people find what to cut.

---

## 3. Things already built, trapped in the wrong place

### `CostModel` → a pre-trade cost preview

`backtest_runner.py` contains a complete Indian retail cost model — brokerage
with cap, STT, exchange and SEBI fees, stamp duty on the buy leg, GST. It is
used only for backtests.

The order ticket should show **what this trade will actually cost** before you
submit: charges, effective price, and break-even. The model exists; it needs
lifting into a shared module and calling from the ticket. This is a few hours
of work for a feature most retail platforms charge for.

It also closes an honesty gap: backtests already subtract costs, so a backtest
is currently *more* truthful about fees than the live order form is.

### `risk_manager` → a visible risk budget

`validate_order` enforces `MAX_ORDER_VALUE` and `MAX_ORDER_QUANTITY`, and
`record_realized_pnl` tracks a daily loss figure. All of it is invisible until
it rejects something.

Show the limits and the headroom left against them, and let the user set them.
A limit you only discover by hitting it feels like a bug; a limit you can see
feels like a safety feature.

---

## 4. Safety, which is what "industry level" actually means

An institutional desk is not distinguished by nicer charts. It is distinguished
by what happens when something goes wrong.

### Persist the broker session

`ChoiceSessionRegistry` is an in-memory dict, so restarting the app drops every
broker connection while the platform sign-in survives in the browser. The user
is left signed in but disconnected, with nothing explaining why — and with a
key that expires daily, reconnecting is not free.

The work is small; the care is in the storage. A Choice session id is a live
credential, so it has to be encrypted at rest with a key that is not sitting
beside it, and cleared on sign-out. Worth doing before anything runs unattended,
because a strategy that dies on restart and cannot reconnect is worse than one
that never started.

### Kill switch

One control that halts every running strategy and cancels all open orders. Not
buried in a menu. When a strategy misbehaves, the time spent looking for the
stop button is the expensive part.

### A daily loss circuit breaker

`risk_manager` already tracks realised P&L per day. Wire it to a hard stop: at
a configured daily loss, live trading disables itself and requires a deliberate
re-enable. This is standard on any professional desk and the tracking half is
already written.

### Fat-finger confirmation

An order whose notional exceeds a set share of the portfolio should require a
second confirmation showing the value in words. Quantity fields and typos are a
well-documented way to lose money quickly.

### Reconciliation

The platform persists every order locally, and Choice has its own record. They
should be compared, with divergence surfaced. A missed fill, a silent rejection
or a partially executed order shows up here first — and today nothing looks.

This matters more than it sounds: the local order book is currently the only
thing the UI shows, so if it ever drifts from Choice's, the interface would be
confidently wrong. That is exactly the failure mode the price-scaling bug had.

---

## 5. Making it understandable

The user's own words: *easy to use as well as understand.*

### The strategy DSL is raw JSON

Strategies are authored as a JSON document with `indicators`,
`entry_conditions`, `exit_conditions` and `actions`. Anyone who is not
comfortable with JSON cannot write one, and anyone who is will still make
schema mistakes.

Two levels of fix, in order of cost:

1. **Plain-English translation.** Render the JSON back as a sentence —
   *"Buy 1 share when RSI(14) falls below 30; sell when it rises above 70."*
   Cheap, and it catches misunderstandings immediately.
2. **A form-based builder.** Dropdowns for indicator, operator and threshold,
   with the JSON as an "advanced" view. The DSL already validates up front,
   so the builder can be a thin layer over validation that already exists.

### Explain backtest results

The metrics are correct and dense — profit factor, Sharpe, max drawdown. A
short plain-language verdict alongside them ("*profitable but with a 22%
drawdown; you would have needed to sit through a four-month losing stretch*")
makes them usable by someone who does not already know the vocabulary.

Also label what a backtest **cannot** tell you. The runner already models costs
and next-bar fills honestly; saying so out loud builds more trust than a big
green number.

### Empty states that teach

The "no data fetched" confusion earlier in this project was a real cost. Every
empty table should say *why* it is empty and what to do next — not connected,
no holdings, market closed, entitlement missing — because those four look
identical today.

### A guided first run

Account → connect broker → **paper by default** → build a strategy → backtest →
run. The pieces exist; nothing sequences them.

---

## 6. Operational and practical

* **Export to CSV** — holdings, orders and trades. Needed for accounting and
  for anyone who wants to analyse in a spreadsheet. Trivial to build, asked for
  constantly.
* **Indian tax view** — realised gains split into STCG and LTCG by the
  12-month holding boundary. Requires purchase dates, so it depends on trade
  history being retained. High value for Indian retail and genuinely rare in
  small platforms.
* **A diagnostics panel.** `backend/scripts/diagnose_choice.py` was what
  actually solved the "no data" problem. That belongs in the app: session age,
  Choice connectivity, rate-limit headroom, last successful sync, and the raw
  shape of the last response. It turns a support conversation into a screenshot.
* **Session expiry warning** before the session dies, not after a call fails.
* **Exchange holiday calendar.** `is_indian_market_hours` covers weekday
  session times only, so the app currently believes the market is open on
  Republic Day.

---

## 7. Blocked, and honestly so

These are not coding tasks. They gate real features and should not be planned
around as if effort alone would clear them.

| Blocker | What it blocks |
| :--- | :--- |
| **Market-data entitlement** from Choice | Live quotes, watchlist, real-time charts, **and any live strategy run** — a strategy with no prices cannot make a decision |
| **Static-IP origination** (OPEN-1) | Live order flow from the desktop topology. Needs a server tier with the desktop proxying to it |
| **UAT certification** (OPEN-3) | Production order access |
| **`PRICE_UNIT_DIVISOR`** (OPEN-2) | Limit orders. Confirm in UAT before placing one |
| **Code signing** | Distribution without a SmartScreen warning |

The market-data entitlement is the big one. It gates the watchlist, the charts,
*and* the strategy runner — which makes it the highest-value phone call
available, worth more than any single item in this document.

---

## Suggested order

1. ~~Day change, realised/unrealised, margin headroom.~~ **Done.**
2. ~~Pre-trade cost preview.~~ **Done.**
3. ~~Kill switch and the daily loss breaker.~~ **Done.**
4. **Persist broker sessions** — small, and it stops every restart costing a
   reconnection against a key that expires daily.
5. **Paper strategy runner** — the feature that makes this a trading platform.
   Start it before the market-data call is resolved so the plumbing is ready.
6. **Plain-English strategy rendering**, then the visual builder.
7. Live execution, once the regulatory items clear.

Item 4 is an afternoon and removes a daily annoyance. Item 5 is the one that
changes what the product *is*.


---

## Delivered — 12 August 2026

Built in one pass after this document was written. The strategy runner (§1) and
the DSL/authoring work in §5 were deliberately left for later.

| Item | Where |
| :--- | :--- |
| Day change, per holding and portfolio-wide | `engine/app/choice_gateway/analytics.py` |
| Realised vs unrealised, margin headroom, collateral | funds tile |
| Concentration: largest position, top 3, HHI | analytics + KPI tile |
| Pre-trade cost preview with break-even | `engine/app/costs.py`, `POST /orders/preview` |
| Visible risk budget and headroom | `GET /orders/limits`, risk strip |
| Kill switch | `POST /orders/halt`, `Shift`+`H` |
| Daily-loss circuit breaker | `risk_manager.halt` |
| Fat-finger confirmation with amount in words | order ticket |
| Reconciliation against the broker | `GET /orders/reconcile`, Health view |
| CSV export | `/orders/export.csv`, `/portfolio/holdings/export.csv` |
| Trading holiday calendar | `engine/app/market_calendar.py` |
| Session-expiry warning | `expires_in_seconds` on session status |
| Empty states that name their cause | `emptyState()` |
| Expired-key vs unknown-vendor errors kept apart | `backend/app/core/errors.py` |
| Diagnostics panel, rendering the endpoint's real payload | Health view + contract test |

Four latent defects surfaced while building these, all fixed and written up as
FEAT-1 and FEAT-2 in AUDIT_REPORT.md:

* `validate_order` never received `owner_key`, so the daily-loss cap **and**
  the halt check were unreachable code.
* `record_realized_pnl` had no caller, so the cap could only ever read zero.
* `cancel_all_open` and `reconcile` referenced four names that a cleanup pass
  had removed from the imports. Both short-circuit in DEMO/PAPER, so the
  `NameError` would have fired only on a live account, the first time anyone
  used the kill switch.
* Exposure was rendered into a KPI tile that the redesign had removed.

Two of those are the same lesson: **a code path that only runs against a real
broker needs a test that drives it with a fake one.** Seven now do.

Tests at that date: 152 passing (was 114), plus 29 UI render checks, a sign-in path
harness and 24 endpoint probes. `pyflakes` runs clean across `backend/`,
`engine/`, `client-desktop/` and both test trees.

**13 August.** A "cannot log in" report turned out to be an expired Choice API
key, not a defect — platform login was working throughout, proven by two
successful `USER_LOGIN` entries in the audit trail. The audit around it did find
one real defect: the Health panel read a `checks` key the diagnostics endpoint
has never returned, so the screen built to explain a failed connection quietly
degraded to a JSON dump. Both are written up as LOGIN-1 and UI-1 in
AUDIT_REPORT.md.

---

## Delivered — 13 August 2026 (next-phase items 1-6, 8-10)

Built against [`next-phase-plan-updated.md`](../next-phase-plan-updated.md).
Item 7, the paper strategy runner, is **not** built: it needs live market data,
and the account has no entitlement. Nothing was substituted for that data — see
the plan's §3 for why synthetic ticks behind a paper run were rejected.

| Plan item | What shipped |
| :--- | :--- |
| §5.1 Credential lifecycle | `ChoiceCredentialExpired` classified once, in the single method every Choice call passes through; broker health (`credential_state`, last success/failure, last error) recorded there and surfaced; reconnect prefills Client ID and mobile, never the key |
| §5.2 Session persistence | `session_store.py` — opt-in, encrypted, HMAC-verified, 8-hour cap, validated against Choice before use, cleared on sign-out / disconnect / environment change |
| §5.3 User journey | First-run checklist derived from real state, dismissible; empty states extended to strategies |
| §5.4 Two databases | `manage_admin.py databases` and `copy-user --to desktop|source`; the sign-in failure names which database was consulted |
| §5.5 Step 1 | `explain.py` — every strategy rendered as a sentence, shown in the list. **Step 2 deliberately not started**: the decision criterion is whether Step 1 proves insufficient |
| §5.6 Backtest verdict | `max_drawdown_bars` added to the metrics, then `verdict.py` — past tense, only computed metrics, thin samples flagged, limitations stated |
| §5.7 Health remainder | Broker status card, explicit market-data entitlement flag, one-click diagnostics export (field names only) |
| §5.9 Admin | Tenant orders / paper P&L / last activity / connected sessions, per-tenant force-halt, regulatory checklist that reports "unverified" rather than an unearned tick |
| §5.10 Design | Spacing scale applied to structural rules; KPI grid no longer leaves an empty cell; topbar contained so the document never scrolls sideways |

**Two defects found while building.** The KPI grid put five tiles in a
three-column layout at 1440px, leaving a visibly empty cell — five divides into
five, two or one, never three. And the topbar had `flex: none` children with no
overflow rule, so at 375px it widened the document; measured horizontal
overflow is now 0 at 1440, 1024, 768 and 375.

Tests: **191 passing** (was 152). `pyflakes` clean, every module imports, 20/20
routes reject anonymous, 7/7 tenant isolation, 17/17 invariants, 8/8 authz on
the new endpoints — including that `plain_english` is derived and cannot be
injected through the API.

---

## Delivered — 13 August 2026, later: the paper strategy runner

Action 0 resolved in the user's favour: diagnostics against the live account
reported `live_quotes: true`, so the runner was no longer blocked and was built
the same day.

| Piece | Where |
| :--- | :--- |
| Start / stop / status | `POST /strategies/{id}/start`, `/stop`, `GET /run-status` |
| The feed | `strategy_engine/scheduler.py` — one thread per run, polling touchline every 5s, aggregating wall-clock-aligned bars |
| Persistence and recovery | `StrategyRun` rows; `recover_orphaned_runs()` at startup marks runs orphaned by a restart as `INTERRUPTED` |
| UI | Run/stop from the strategy list, live position, realised P&L, recent fills, an always-reachable stop |

**The three safety rules, all failing safe:** no market data means the run
refuses to start; three consecutive quote failures halt it; an expired API key
halts it immediately, because retrying cannot revive an expired key.

Nine tests drive those paths with a controlled quote source, including one that
asserts a paper run can never obtain a broker client — the session it is handed
raises if anything tries.

Tests: **200 passing** (was 191).

### Still not built

LIVE runs. The runner supports the mode; only PAPER can be started from the
API, and live execution remains blocked by static-IP origination and UAT
certification regardless.
