# Choice FINX Algo Trading Platform

An algorithmic trading platform for Choice FINX OpenAPI, built on the `kkunal` /
`choice_api` Python SDK.

> **Defaults to UAT.** Out of the box every Choice call goes to the sandbox at
> `https://uat.jiffy.in`. Switching to production is a deliberate act — set
> `CHOICE_ENV=PROD` — and carries obligations described in
> [Regulatory requirements](#regulatory-requirements).

---

## Documentation

* **[Documentation hub](docs/INDEX.md)** — environments, authentication, socket
  protocols, regulatory obligations
* **[Architecture](docs/ARCHITECTURE.md)** — what is built, what is planned,
  and how the pieces fit
* **[Roadmap](docs/ROADMAP.md)** — what to build next, ordered by value
* **[Next phase plan](next-phase-plan-updated.md)** — the current phase, scoped and sequenced
* **[Official PDF specifications](docs/pdf/)** — the authority for everything above

---

## Getting started

### 1. Install

```bash
pip install ./kkunal-1.2.0.tar.gz
pip install -r backend/requirements.txt -r engine/requirements.txt -r client-desktop/requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste as SECRET_KEY
```

The defaults are safe for local work: UAT endpoints, loopback binding, no CORS.
`.env` is git-ignored — never commit it.

### 3. Run

```bash
python client-desktop/app/cli.py
```

The launcher picks free ports, starts the backend as a subprocess, waits for it
to answer, then opens your browser.

To run the API alone:

```bash
cd backend && python -m app.main        # http://127.0.0.1:8000/docs
```

### 4. Sign in

1. Create a platform account in the browser.
2. Connect a broker account and pick a mode:

| Mode | Market data | Orders | Use it to |
| :--- | :--- | :--- | :--- |
| **Paper** (default) | Real, from Choice | Filled locally at the live price, never sent | Run strategies against real conditions with no risk to funds |
| **Live** | Real, from Choice | Sent to Choice | Trade for real |
| **Sandbox** | Sample data | Filled locally | Try the interface without any credentials |

Paper is the default, and live requires ticking an explicit acknowledgement.
A session that was not asked to trade live has no code path that reaches
Choice's order endpoint — see [Architecture §2](docs/ARCHITECTURE.md).

Paper P&L is tracked separately from your real holdings and the two are never
added together.

### 5. Admin access (optional)

Registration always creates a trader. Promote an account deliberately, from the
machine that owns the database — there is no self-service route to admin and no
default admin password:

```bash
python backend/scripts/manage_admin.py list
python backend/scripts/manage_admin.py grant you@example.com
```

Then open **http://127.0.0.1:9000/admin** while the app is running, for platform
counters, tenants and the audit trail. It has to be served by the app rather than
opened from disk: the page calls `/api/v1/...` relatively, which only resolves
when the API is on the same origin.

---

## Building the executable

```bash
python client-desktop/build_exe.py              # one-file (default)
python client-desktop/build_exe.py --onedir     # starts much faster
python client-desktop/verify_exe.py             # then check what you built
```

Run `verify_exe.py` after every build. `pytest` runs against the installed
packages and so cannot see a module PyInstaller failed to bundle: a `cryptography`
import the suite was perfectly happy with once shipped a binary that returned 500
on every broker call. Only driving the executable catches that.

The interface has its own check, which needs no build and no backend:

```bash
python frontend-user/verify_ui.py     # drives the UI in headless Edge
```

It renders every chart, drives the strategy builder, puts each starter template
through the engine's own validator, and measures every view at five widths for
sideways scroll, overflowing containers, controls too small to hit, and
unlabelled cards. It exists because the interesting UI faults do not throw: a
chart that draws nothing looks like a chart with nothing to draw, a header that
widens the page looks fine until you notice the scrollbar, and `pytest` never
opens the page.

Layout is measured with tables full of data — an empty page never overflows.

The build locates the Choice SDK by importing it, so no machine-specific paths
are involved. `CHOICE_API_PATH` points at a source checkout if the SDK is not
installed. The UI is copied from `frontend-user/` at build time, so
`client-desktop/static/` is a build artefact — edit the interface in
`frontend-user/index.html` only.

The binary is unsigned. Sign it before distributing, or every download raises a
SmartScreen warning:

```
signtool sign /fd SHA256 /tr <timestamp-url> /td SHA256 dist/ChoiceFinxTrader.exe
```

User data (the SQLite database) is written to `%LOCALAPPDATA%\ChoiceFinxTrader`,
not beside the executable, so the app works when installed under Program Files
and does not share a database between Windows accounts.

---

## Tests

```bash
python -m pytest
```

Each run uses its own temporary database, so the suite is repeatable and never
touches `algo.db`.

---

## Layout

```text
algo_kkunal/
├── backend/          FastAPI: auth, tenancy, strategies, orders, portfolio, admin
├── engine/           Choice gateway, DSL, backtester, strategy runner, risk manager
├── client-desktop/   Launcher, local UI server, API proxy, PyInstaller build
├── frontend-user/    The trading interface (single source)
├── frontend-admin/   Admin dashboard (served by the app at /admin)
└── docs/             Documentation hub and official PDF specifications
```

---

## How safety is enforced

| Concern | Behaviour |
| :--- | :--- |
| **Broker sessions** | One per user, expiring. There is no shared connection, so no user can read or trade another's account. |
| **Sandbox** | A `DEMO` session is simulated end to end. Orders never reach Choice, and sandbox data is labelled wherever it appears. |
| **Order results** | A rejected order returns a 4xx with the reason. Nothing reports success it has not verified. |
| **Order limits** | Quantity, price, notional value and a 10 orders/second cap are checked before anything leaves the process. |
| **Backtests** | Computed from real bars with costs and slippage. A run that cannot complete fails; there is no invented result. |
| **Pre-trade costs** | The order ticket shows brokerage, STT, exchange, SEBI, stamp duty and GST before you submit, plus the break-even price. Same model the backtester uses. |
| **Kill switch** | One control (or `Shift`+`H`) cancels every working order and stops the account trading for the day. |
| **Loss circuit breaker** | A breached daily-loss cap halts the account rather than refusing one order, so a retrying strategy cannot keep probing it. |
| **Fat-finger check** | An order above 25% of the portfolio or ₹2,00,000 needs a second confirmation, with the amount spelled out in words. |
| **Reconciliation** | The platform's order record is compared against Choice's, and divergence is surfaced. |
| **Strategy clarity** | Every strategy is rendered back as an English sentence derived from its definition, so it cannot drift from what the strategy does. |
| **Backtest honesty** | Results come with a plain-language verdict that states the drawdown and its duration, flags too few trades to judge, and lists what the model does not cover. |
| **Paper runs** | A strategy can be run on live prices with simulated fills. It refuses to start without real market data, halts on a feed gap or an expired key, and marks itself INTERRUPTED if the app is closed mid-run. |
| **Audit** | Logins, broker connections, orders, halts and strategy changes are recorded. |
| **Secrets** | No signing key is baked into the source. Production refuses to start without one. |

---

## Regulatory requirements

Before routing live orders, from the
[Choice OpenAPI Integration Guide](docs/pdf/Choice_OpenAPI_Integration_Guide.pdf):

* **Certify in UAT** and receive written confirmation from the Choice Open API
  team (§5.1).
* **Declare a static IP.** All API orders must originate from it; orders from
  any other address are rejected (§8).
  ⚠️ A desktop application places orders from each user's own machine, which
  cannot satisfy this. Order flow must move to a server with a declared
  address, with the desktop client proxying to it.
* **Complete vendor empanelment** with NSE/BSE/MCX if the platform serves
  multiple Choice clients (§3.2).
* **Register any strategy** at or above 10 orders/second (§9).
* **Retain 5 years** of API activity logs (§12).

---

## Troubleshooting

### "Choice said: Unauthorized, Token Expired"

**Your Choice API key has lapsed. Generate a new one in the Choice Open API
portal and paste it in.** Re-entering the old key will keep failing.

Choice gives two different rejections and they need different fixes:

| Choice says | What it means | What to do |
| :--- | :--- | :--- |
| `Token Expired` | The Client ID is recognised; the API key attached to it has expired | Reissue the API key |
| `VendorId Invalid or doesn't exists` | The Client ID is not known in this environment | Check the Client ID, and that `CHOICE_ENV` matches where it was issued (PROD vs UAT) |

Keys appear to have a daily lifetime, so expect this roughly once a day.

### "You are asked to connect your broker account again after a restart"

Broker sessions live in memory, so restarting the application drops them while
your platform sign-in survives in the browser — hence the odd combination of
still being signed in but disconnected from Choice.

Tick **"Remember this broker session on this machine"** when connecting to keep
it across a restart. Only the Choice session id is stored, encrypted; your API
key never is. Leave it off on a shared computer. The stored session is checked
against Choice before it is used, so an expired one asks you to reconnect
rather than silently failing later.

### "Incorrect email or password" for an account you are sure exists

Check which database you are talking to. They are deliberately separate:

| How you run it | Database |
| :--- | :--- |
| `ChoiceFinxTrader.exe` | `%LOCALAPPDATA%\ChoiceFinxTrader\algo.db` |
| From source | `algo.db` in the repository root |

An account created before the data directory moved out of the install folder
exists only in the second one. List what each holds — the script prints the
database it opened, so you can always tell which one you are looking at:

```bash
# the source database
python backend/scripts/manage_admin.py list

# the one the executable uses
DATABASE_URL="sqlite:///$LOCALAPPDATA/ChoiceFinxTrader/algo.db" \
  python backend/scripts/manage_admin.py list
```

### The dashboard shows nothing for an account that clearly has holdings

Open the **Health** tab and press *Run diagnostics*. It reports what Choice
actually returned — a pass/fail line per endpoint with the error text, plus the
session state. By default it shows field names and types only, never values, so
the output is safe to share when asking for a mapping to be corrected.

---

## Known gaps

* Live quotes are not shown in the interface while the account has no
  market-data entitlement from Choice.
* Paper runs poll touchline quotes every five seconds rather than consuming
  the FIX3.0 socket feed. Adequate at bar granularity; the socket is the
  upgrade for tick-level work.
* LIVE runs — a strategy submitting real orders — are not built, and stay
  blocked by the regulatory items below regardless.
* Backtests run inline; long ranges block a worker.
* SQLite suits the desktop build; a shared server needs PostgreSQL and Alembic.
* Special sessions (Muhurat) are not in the trading calendar; regular equity
  sessions only. The calendar covers 2025-2026 and reports later years as
  unknown rather than guessing.
* Realised gains are not split into STCG and LTCG — that needs purchase dates
  from retained trade history.
* **The order price unit is unconfirmed.** The API carries paisa and divides by
  100 before calling Choice. The supplied PDFs do not document the order
  schema. Verify against the live API docs in UAT before placing a limit
  order, and adjust `PRICE_UNIT_DIVISOR` in
  `backend/app/services/order_service.py` if Choice expects paisa.
