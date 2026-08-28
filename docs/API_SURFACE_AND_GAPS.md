# What data and APIs we already have — and what we are not using yet

Written 18 Aug 2026. The question behind this document: *what can we build so a
user never has to open their Choice account for a basic need?*

Every claim here was read out of the code, not remembered. Paths are given so
each one can be checked.

---

## Status — built 18 Aug 2026

The survey below is kept as written. This section records what has since been
implemented against it.

**Done and verified** (390 Python tests, 384 UI checks, 41 checks against the
compiled binary):

| Gap | What now exists |
|---|---|
| #1 Order ticket traded four stocks | Instrument search over the full scrip master, via a native `<datalist>` |
| #2 No stop-loss | `SL_MKT` / `SL_LIMIT` with a trigger-price field, plus `CO` and `BO` products |
| #3 No modify | `POST /orders/modify`, and a **Working orders** table with Amend and Cancel |
| #5 Money in and out | `POST /portfolio/funds/add` and `/withdraw`, `GET /funds/check-vpa`, with a Move funds dialog |
| #6 Position conversion | `POST /portfolio/convert`, offered per position on the Positions view |
| #7 eDIS | `GET /portfolio/edis` |
| #9 No profile page | `GET /market/profile`, shown in an **Account** card on Health |
| #10 F&O quantity in shares | `GET /market/instrument/{token}` returns the lot; the ticket validates multiples |
| §2a Feed entitlement unknown | `GET /market/feed` reports whether Choice supplied a feed address |
| — (found on the way) | `POST /orders/{n}/cancel` — a single cancel; only the all-or-nothing kill switch existed |
| — (found on the way) | The broker's own order book was never shown anywhere in the UI |

**Fixed while building:**

- The price feed gated on `is_live` (sends real orders) instead of
  `uses_broker_data` (has broker market data), so it refused to start in PAPER
  — the mode that most needs it. See §2a.
- `subscribe_best_five` was unreachable; depth is now a subscription option and
  tracked under its own key so it cannot collide with touchline.
- `_normalize_order` dropped `GatewayOrderNo`, `segment_id`, `token`,
  `order_type` and `product_type`, which are exactly the fields an amendment
  has to echo back. Amending was impossible without them.
- `_normalize` for positions dropped `product_type` and `client_order_no`, the
  fields a conversion needs.
- The per-order cancel logic lived inside `cancel_all_open`; extracted to
  `_cancel_row` so the kill switch and a single cancel cannot drift apart.
- `loadOrderBook` accepted a non-list `data`, which would have thrown on every
  later `.find`/`.filter` and taken out the Amend and Cancel buttons.
- `verify_ui.py`'s duplicate-name scan covered `function` only. A duplicated
  `const` throws a SyntaxError that kills the **entire** script block — the
  page loads with no behaviour at all — and that is what a second
  `WORKING_STATUSES` did. The scan now covers `const` and `let`.

**Not built yet** (deliberately):

- **#4 Push updates over the interactive socket.** The largest of the set and
  the one that removes polling. Left until the feed entitlement question in §6
  is answered, since the same answer decides how it should be built.
- **#11 Corporate actions** and **#12 broker-side indicators** — both depend on
  data-coverage questions rather than on code.

**Still the first thing to do:** run the diagnostic against a real session
(§6). It now probes `UserProfile`, `DISStatus`, `TradeBook` and the feed
address as well, so one call answers what this account can actually use.

---

## Diagnostic results — live PAPER session on PROD, 18 Aug 2026

Client ID M09984. What Choice actually answers for this account.

| Endpoint | Result |
|---|---|
| FundsViewNew / FundsView | **works** |
| Holdings | **works** — 33 rows, with `LTP`, `ClosePrice`, `PriceDivisor`, `MarketLot` |
| NetPosition | **works** (empty) |
| MarketStatus | **works** |
| OrderBook / TradeBook | **works** (empty) |
| UserProfile | **works** |
| MultipleTouchline | **fails** — `Index was outside the bounds of the array.` |
| HistoricalData | **fails** — `Choice rejected the session. Sign in to Choice again.` |
| DISStatus | **fails** — `No data found` |
| Broadcast price feed | **address supplied** — `brd.choiceindia.co.in:4520` |

### The price feed is genuinely provisioned

`OdinBcastIP` came back populated. Checked against the SDK: `client.bcast_ip`
initialises to `None` and is assigned only from `response_data.get("OdinBcastIP")`
in the ValidateTOTP response (`choice_api/client.py:123`), so this is Choice's
own answer and not a default. That the host matches our `FALLBACK_FEED_HOST`
constant is a coincidence of the constant having been copied from the vendor
documentation.

Caveat worth keeping: this proves Choice *named* a feed host for this account.
It does not prove the socket will accept a logon or a subscription. Necessary
evidence, not sufficient — the remaining doubt is settled only by connecting.

### Three faults the diagnostic exposed, all now fixed

**1. Paper trading was filling at invented prices.** With `MultipleTouchline`
dead, `get_multiple_touchline` fell back to `_demo_quotes` — a fixture table —
for PAPER sessions holding *real* credentials. Against this account:

| Instrument | Fixture price used | Choice's own holdings record | Error |
|---|---|---|---|
| RELIANCE | 2504.50 | 1324.10 | **+89%** |
| INFY | 1540.20 | 1118.50 | **+38%** |

`_simulate_order` fills at that quote, so every paper fill was at the fiction.
A paper trade exists to answer "what would this strategy have done", and an
invented fill price makes the answer worthless while looking exactly like a
working system. This is the same class of fault the codebase already learned
from once, in `_normalize`, where substituting the close for a missing last
price inflated the portfolio a hundredfold.

Fixed: a session with real broker data never sees the fixture table. It is
priced from the holdings snapshot where possible, and refused otherwise. DEMO
still uses fixtures, correctly — it has no broker at all.

**2. Holdings is a working price source, and we were ignoring it.** Every row
carries a real scaled `LTP` and `ClosePrice`. That is a current Choice price
for anything the account holds, available today with no new entitlement, on an
account where the quote endpoint is dead. Now used as the fallback, tagged
`source: holdings_snapshot` so nothing downstream mistakes it for a live tick.
Limited to held instruments by nature.

**3. The diagnostic leaked the whole account.** Its own hint invited the reader
to share the file "with include_values off", but the `normalized` block
returned real balances and every holding regardless of the flag. Following that
advice disclosed the lot. Fixed — `normalized` now honours the flag like the
probes always did — and guarded by a check against the compiled binary.

### Still open, for Choice

- **`MultipleTouchline` returns `Index was outside the bounds of the array.`**
  That is a server-side .NET exception, not a clean entitlement refusal. Worth
  raising with that exact wording: it may be a defect on their side rather than
  a missing permission.
- **`HistoricalData` returns "Choice rejected the session"** on the same session
  where seven other endpoints succeeded moments earlier. Also worth quoting.
- **`UserProfile` carries no bank accounts.** Fields returned: `ClientId`,
  `Name`, `BOCode`, `DPCode`, `Depository`, `POAStatus`, `MobileNo`, `EmailID`.
  The deposit and withdrawal forms therefore take a typed account number rather
  than a picker fed from the profile.
- **Every holding reports `SellQty: 0`.** `POAStatus` is the field that would
  explain it, and is now shown on the Account card. If holdings cannot be sold
  without a POA or per-sale eDIS, that is worth knowing before a strategy tries.
- **`DISStatus` returns "No data found"**, which more likely means "no pending
  eDIS request" than "not entitled". The `edis` capability flag should not be
  read as a hard refusal.

---

## 1. The three sources we can draw on

| Source | Surface | What we use today |
|---|---|---|
| **Choice FINX REST** (`choice_api` SDK v1.2.0) | 8 modules, ~37 methods | 14 methods |
| **Choice websockets** | 2 sockets (price feed + interactive) | price feed only, and not from the UI |
| **Marketstack v2** (`API Endpoints v2.yaml`) | 46 endpoints | 2 endpoints (`eod`, `intraday`) |

So roughly **a third of the Choice REST surface and a twentieth of Marketstack
is wired up.** Most of what is missing is exactly the "basic needs" the
question is about.

---

## 2. Choice FINX REST — method by method

SDK lives at `C:\Users\kaival.trapasia\Desktop\indicator_lib\choice_api`.

### In use

`get_multiple_touchline`, `place_order`, `cancel_order`, `get_order_book`,
`get_order_book_v2`, `get_trade_book`, `get_holdings`, `get_net_position`,
`get_funds_view_new`, `get_market_status`, `get_historical_data`,
`search`, `get_token`, `get_details`.

### Sitting unused

| Module | Method | What it would give the user |
|---|---|---|
| `orders` | `modify_order` | Amend price/qty of a live order without cancel-and-replace |
| `orders` | `get_order_by_no` | Status of one order without pulling the whole book |
| `orders` | `get_order_messages` | The broker's own rejection text, verbatim |
| `portfolio` | `position_conversion` | Convert MIS intraday ↔ CNC delivery |
| `portfolio` | `verify_dis` / `get_dis_status` | eDIS authorisation — required to sell demat holdings |
| `funds` | `process_payout` | **Withdraw money to bank** |
| `funds` | `payment_via_hdfc_upi`, `check_vpa` | **Add money by UPI** |
| `funds` | `payment_via_netbanking`, `payment_via_razorpay` | **Add money by net banking / card** |
| `market` | `get_user_profile` | Bank accounts, demat BOID, segments enabled |
| `scrip_master` | `get_lot_size` | F&O lot size — quantity in lots, not raw shares |
| `historical` | `get_historical_data_with_indicators` | OHLCV with indicators computed broker-side |
| `indicators` | 22 methods (SMA, EMA, RSI, MACD, Bollinger, ATR, Supertrend, ADX, Stochastic, CCI, Williams %R, VWAP, OBV, Parabolic SAR, Ichimoku, Donchian, Heikin-Ashi, pivot points, …) | Indicator library we currently reimplement |

### Websockets — both wrapped, neither reachable from the UI

- `engine/app/choice_gateway/sockets_pricefeed.py` — uses `subscribe_touchline`.
  **`subscribe_best_five` (5-level market depth) is unused.**
- `engine/app/choice_gateway/sockets_interactive.py` — wraps
  `InteractiveSocketClient`, which pushes `ORD_NRML` (order updates),
  `TRD_MSG` (trade confirmations) and `MKT_STAT` (market status).
  **It has no backend route and no UI consumer — grep for it in `backend/`
  and `frontend-user/` returns nothing.** The UI polls instead.

---

## 2a. `PriceFeedSocketClient` — the most valuable unused thing we have

Worth its own section, because it is the only item on this list that could
unblock something we are currently *stuck* on.

### What it is

The Choice live price feed: FIX3.0 pipe-delimited messages, zlib-compressed,
over a WebSocket. **A different service from the OpenAPI REST endpoints** —
different host (`wss://brd.choiceindia.co.in:4520`), different protocol, and
the real address is handed out per session in the logon response as
`OdinBcastIP` / `OdinBcastPort`.

Two subscriptions:

| Call | Msg | Delivers |
|---|---|---|
| `subscribe_touchline` | 206 → 209 | LTP, Open, High, Low, Close, ATP, lower circuit, Volume, Open Interest, TotalBuyQty, TotalSellQty |
| `subscribe_best_five` | 127 → 128 | The 5-level bid/ask depth ladder |

### Its state in our code

`engine/app/choice_gateway/sockets_pricefeed.py` — 153 lines, a careful
wrapper that already handles the `OdinBcastIP` detail, per-user connections,
de-duplicated subscriptions and reconnects.

**It has zero callers.** Repo-wide grep finds no backend route, no test, no UI
consumer. It is complete, dead code.

> The `on_tick` in `engine/app/strategy_engine/runner.py:147` is a different
> `on_tick` and is *not* connected to this. It is fed by the poll loop in
> `scheduler.py:194`, which calls `get_multiple_touchline` over REST and then
> `stop.wait(POLL_SECONDS)`.

### Why it matters more than the rest of the list

Three separate problems have the same answer:

1. **Paper trading is blocked** because the account lacks the
   `MultipleTouchline` REST entitlement. The socket feed is a *different
   service* and may well be a separate entitlement line — Odin broadcast feed
   access is usually provisioned separately from OpenAPI REST market data.
   **If it is entitled, paper runs work without waiting on Choice.**
2. **"I have to refresh the page again and again."** A push feed removes the
   staleness problem at the source.
3. **"20 seconds is too fast, there will be multiple hits on the API."** A
   socket subscription is one connection regardless of refresh rate, instead of
   N quote requests per interval.

### Three things to fix before wiring it up

1. **`start()` gates on the wrong property.**
   ```python
   if not self.session.is_live:
       raise ChoiceNotConnected("A live Choice session is required for the price feed.")
   ```
   `is_live` means `mode is SessionMode.LIVE` — i.e. *sends real orders*. But
   the property that means "market data comes from Choice" is
   `uses_broker_data`. As written, the feed refuses to start in PAPER mode,
   which is the mode that most needs it. This is a live bug hiding in dead
   code: it surfaces the moment anyone calls it.
2. **Depth is not implemented.** `MSG_BEST_FIVE_REQUEST = 127` and
   `MSG_BEST_FIVE_INFO = 128` are defined as constants and never used; the
   wrapper only calls `subscribe_touchline`.
3. **Nothing consumes ticks.** Wiring the feed to the strategy scheduler means
   replacing the poll loop, not adding alongside it — otherwise both run.

### The cheap test, before building anything

Connecting a real session and reading whether the logon response carries
`OdinBcastIP` / `OdinBcastPort` answers "is this account provisioned for the
broadcast feed?" without placing a single order. `feed_endpoint()` already
reports which source it resolved (`logon_response` vs `vendor_default`) — a
`vendor_default` answer means the logon gave us nothing, which is itself the
signal.

---

## 3. Marketstack v2 — 46 endpoints, 2 in use

`engine/app/choice_gateway/marketstack.py` calls `eod` and `intraday`. Unused
and potentially useful:

- **`/v2/dividends`, `/v2/splits`** — corporate actions. Without these, a
  holding's average price and P&L silently go wrong the day a stock splits.
- **`/v2/tickerinfo`, `/v2/companyratings`** — company profile and analyst
  ratings next to a position.
- **`/v2/indexlist`, `/v2/indexinfo`** — index levels for a benchmark line on
  the equity curve.
- **`/v2/etflist`, `/v2/etfholdings`** — look through an ETF to what it holds.
- **`/v2/bondlist`, `/v2/bond`, `/v2/commodities`, `/v2/currencies`** — other
  asset classes.
- `/v2/company_facts`, `/v2/cik_code`, `/v2/submissions`, `/v2/frames/...` —
  SEC filings data. **US-only; not useful for Indian equities.**

> Coverage caveat: Marketstack is global-market-first. Before building on
> `dividends`/`splits`, check the coverage for NSE/BSE symbols on the current
> plan — the same way the intraday endpoint turned out to need a higher tier.

---

## 4. The gaps that actually push a user to choiceindia.com

Ranked by how often a user hits them, with the work each needs.

### 1. The order ticket can only trade four stocks — *biggest gap, smallest fix*

`frontend-user/index.html:1441` is a static dropdown:

```html
<select id="o-inst" onchange="updateTicket()">
  <option value="2885|RELIANCE">RELIANCE</option>
  <option value="1594|INFY">INFY</option>
  <option value="11536|TCS">TCS</option>
  <option value="1333|HDFCBANK">HDFCBANK</option>
</select>
```

Nothing in the JavaScript ever populates it. Meanwhile
`GET /api/v1/market/search` already exists (`backend/app/api/v1/market.py:17`)
and is backed by the full scrip master. **The platform can already trade
anything on NSE/BSE/MCX; the ticket exposes four names.** Anything else means
opening the Choice account.

### 2. No stop-loss orders

`engine/app/choice_gateway/orders.py:32` already accepts
`{"RL_MKT", "RL_LIMIT", "SL_MKT", "SL_LIMIT"}` and takes a `trigger_price`.
The ticket offers Market and Limit only. Protecting a position is the most
common reason to reach for the broker app mid-session.

Same story for products: the engine accepts `{"CNC","MIS","NRML","CO","BO"}`,
the ticket offers three — cover orders and bracket orders are already
supported below and simply not surfaced.

### 3. No way to modify a live order

We place and cancel. `modify_order` is unused, so changing a limit price means
cancel-and-replace, which loses queue priority — or going to Choice.

### 4. Everything is polled; nothing is pushed

This is the root of the "I have to refresh the page again and again"
complaint. `sockets_interactive.py` already wraps the socket that pushes order
and trade updates. It needs a backend route and a UI consumer. Until then,
every refresh interval is a trade-off between staleness and API load —
a trade-off that disappears entirely with a push feed.

### 5. Money in and money out

Six unused `funds` methods: withdraw (`process_payout`), UPI, net banking,
Razorpay. A margin shortfall mid-session currently means leaving the platform.

### 6. Position conversion (MIS ↔ CNC)

`position_conversion` is unused. Converting an intraday position to delivery
before close is a routine action, and there is no way to do it here.

### 7. eDIS

`verify_dis` / `get_dis_status` unused. Selling demat holdings needs eDIS
authorisation; without it a sell can fail at the last step with no explanation
from us.

### 8. No market depth

Only last-traded price via touchline. `subscribe_best_five` gives the 5-level
bid/ask ladder — the thing a trader looks at before sizing a limit order.

### 9. No profile or account page

`get_user_profile` is unused, so bank accounts, demat BOID and which segments
are enabled are invisible here.

### 10. F&O quantity is in shares, not lots

The ticket lets a user pick "NSE F&O" as a segment, but quantity is a raw
number and `get_lot_size` is unused. An F&O order with a non-multiple quantity
is rejected by the exchange, and we would not catch it first.

### 11. Corporate actions are not tracked

Marketstack `splits`/`dividends` unused. A split silently corrupts average
price and return percentages on the holdings page.

### 12. We reimplement indicators the broker computes

22 indicator methods plus `get_historical_data_with_indicators` are unused.
Worth comparing our values against theirs before choosing which to keep —
if they disagree, a backtest and a live run disagree too.

---

## 5. What to do first

My recommendation, in order:

0. **Test whether the price feed socket is entitled** (§2a) — costs one login,
   builds nothing, and the answer decides whether paper trading stays blocked.
   If it is entitled, wiring it up fixes three separate complaints at once and
   jumps to the top of this list.
1. **Instrument search in the order ticket** (#1) — replaces a 4-item dropdown
   with the search endpoint that already exists. Small, and it removes the most
   common reason to leave the platform.
2. **Stop-loss and the missing product types** (#2) — the engine already
   supports them; this is a form change plus a trigger-price field.
3. **Modify order** (#3) — one SDK call, one dialog.
4. **Push updates over the interactive socket** (#4) — the largest of the four,
   and the one that fixes staleness properly instead of tuning a poll interval.

Together those four cover placing, adjusting, protecting and watching an
order, which is most of what the official account is opened for.

Funds (#5) is the highest-value item after that, but it moves real money and
deserves its own design pass, not a quick add.

---

## 6. Step zero: confirm what this account is actually entitled to

We already know two entitlements are missing for Client ID M09984
(`MultipleTouchline` and `OpenGraph/ChartData`). Several endpoints above may be
gated the same way, and building against a locked endpoint wastes the work.

`backend/app/api/v1/diagnostics.py` already probes live endpoints and reports
which succeed — but it currently probes only the ones we use
(`FundsViewNew`, `FundsView`, `Holdings`, `NetPosition`, `MarketStatus`,
`OrderBook`, …). Extending that probe list to the unused endpoints would answer
"what can we actually build?" in a single call against a real session, before
any feature work starts.
