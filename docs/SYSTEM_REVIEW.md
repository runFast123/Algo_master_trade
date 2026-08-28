# System review — 17 August 2026

A correctness pass over the whole platform, followed by an assessment of where
it stands against the Indian retail algo-trading market.

Baseline before the pass: 99 Python modules, one 4,049-line interface file,
251 tests, pyflakes clean.

---

## Part 1 — Faults found

Four, all in money handling. None of them threw an exception, which is why they
were still here: every one produced a plausible number.

### LOGIC-1 · A holding that was never priced was reported as flat

**Severity: medium.** Understates the day's move, silently.

`portfolio._normalize` substitutes the previous close when Choice sends no last
traded price, so the holding still counts toward portfolio value. That is the
right call. But `analytics.day_change` then compared the substitute against
itself and got exactly `0.00` — reported as *"traded flat today"*.

The same function already refuses to guess when the **close** is missing, with a
comment saying why:

> A missing close means "not known", never zero — a holding silently counted as
> flat would drag the whole day's figure toward zero and look plausible.

That is precisely what happened from the other side. Demonstrated:

```
TRADED    day_pnl=  500.0   day_change_pct= 4.76
ILLIQUID  day_pnl=    0.0   day_change_pct= 0.0
summary: day_pnl=500.0  day_priced=2 of 2      <- the untraded one counted as priced
```

**Fixed:** `_normalize` records `priced_from_close`, and `day_change` returns
`None` for those rows. They keep their portfolio value and drop out of the day
figure and the "priced today" count.

### LOGIC-2 · A profitable short was recorded as a loss

**Severity: high.** Wrong sign, wrong magnitude, and it feeds the risk cap.

`record_simulated_fill` assumed positions are never negative. `min(quantity,
held)` goes negative once `held` does — and a negative number is truthy — so
*adding* to a short booked a fabricated loss, while covering one booked nothing
at all, because only the SELL branch realised anything.

A short opened at 100 and covered at 80 — a gain of ₹300:

```
SELL 10 @ 100   -> position -10, average_price 0.0
SELL  5 @ 100   -> realised -1000.00      (fabricated, on an opening trade)
BUY  15 @  80   -> realised -1000.00      (the actual +300 never booked)
```

Three consequences: paper P&L is wrong; `average_price` of 0 makes every
unrealised figure on an open short nonsense; and `record_realized_pnl` feeds the
**daily loss cap**, so a profitable trade could halt trading for the day.

Reachable through the order ticket, which has no guard against selling more than
is held. Strategies are unaffected — `LiveStrategyRunner` sells only
`position_qty`.

**Fixed:** the book is signed throughout. Realisation happens on whichever side
reduces the position, with the sign flipped for shorts; opening and extending
realise nothing; a reversal starts the new side at the reversing price. Verified
across seven cases including partial closes, averaging up, and long→short flips.

### CONC-1 · The position book was mutated from several threads at once

**Severity: medium.** Rare, silent, and it loses money from the record.

Every paper run for a user shares **one** `ChoiceSession`, and the scheduler
gives each run its own thread — plus the API thread for manual orders.
`paper_realized_pnl += booked` is a read-modify-write. A lost update is money
missing from the day's realised figure, and from the cap that reads it.

**Fixed:** a `book_lock` around the fill path. Asserted directly (the lock is
held while the arithmetic runs) rather than by racing threads, because a
concurrency test that only sometimes fails is worse than none.

### DATA-2 · Marketstack results truncated the date range without saying so

**Severity: high for intraday.** The backtest measured a fraction of the period
requested and reported it as the whole thing.

One Marketstack call returns at most 1000 rows, and there was no paging. What
1000 bars actually buys:

| Timeframe | 1000 bars | |
|---|---|---|
| `1d` | 1000 trading days | ≈ 47.6 months |
| `1h` | 166.7 | ≈ 7.9 months |
| `15m` | 40.0 | ≈ 1.9 months |
| `5m` | 13.3 | **≈ 0.6 months** |
| `1m` | 2.7 | **≈ 0.1 months** |

A month-long 5-minute backtest silently measured about thirteen trading days.

**Fixed:** both endpoints page through `offset` until the reported total is
reached or a short page arrives.

### Verification

Every fix was mutation-tested — the guard was removed and the suite had to fail:

| Mutation | Result |
|---|---|
| Signed accounting reverted | detected (4 tests) |
| `priced_from_close` guard removed | detected (2 tests) |
| `book_lock` removed | detected |
| Pagination removed | detected (2 tests) |

**256 tests**, pyflakes clean, 203 UI checks, 30/30 against the binary.

---

## Part 1b — Second pass, 17 August 2026

After the features of the same day. Five findings, four of them mine from
earlier in the session.

### RUN-1 · Finished paper runs were re-polled forever

`state.runs` only ever grew — `concat` on start, nothing on stop. Every run
started in a session was re-fetched on every five-second poll for the rest of
it, including ones that had stopped and could never change again. Start three
runs, stop them, start three more: six requests every five seconds, **72 a
minute**, for news that could not come.

The same overspending the refresh cadence was just cut for, in a different
place.

**Fixed:** a run in a terminal state is answered from its last result and never
re-fetched; a live run is still polled every cycle. Finished runs from an
earlier batch are dropped when a new batch starts.

### RUN-2 · A run that halted before its first poll never alerted

The alert fires on the transition `RUNNING → halted`. `startRun` never recorded
a status, so the first poll saw `undefined → HALTED`, which is not a
transition, and said nothing. A run that failed immediately — an expired
credential on the first quote — stopped in silence.

**Fixed:** `startRun` seeds the status as `RUNNING`, which is what it is.

### SHIP-1 · The test harness was inside the shipped binary

`sync_ui()` copies all of `frontend-user/` into the bundle, and that directory
now holds `verify_ui.py`. Test tooling was being compiled into the product.
Not reachable over HTTP — the server falls back to `index.html` — but it had no
business there. The copy now ignores `*.py`, `*.md` and `__pycache__`.

### DEAD-1 · `refreshAll`'s `quiet` flag had no caller

Once the poll built its own loader list, nothing in the app passed `quiet`. The
parameter survived only because a test exercised it — dead flexibility kept
alive by its own check. Both removed; the check now drives `autoRefreshTick`,
which is what actually runs.

### TEST-1 · An escape collapsed and made a whole phase unparseable

Fixing a `SyntaxWarning`, `\\s` in a JS regex became a literal tab and newline
inside the regex literal. The browser reported it as a page whose script never
ran, with no indication of where.

**Twice I checked it and got a false all-clear**, because I parsed the *source
text* of the harness rather than the string Python actually produces. Reading
the file shows the escapes; importing the module shows what the browser gets.

`check_harnesses_parse()` now runs `node --check` over each interpolated
harness on every run, so this cannot recur silently.

### Verified end to end

OR groups were run through the real backtest engine, not just unit-tested:

| Entry | Trades |
|---|---|
| `{"any": [rsi < 35, close crosses above sma]}` | 25 |
| `{"all": [...]}` | 0 |
| flat list of the same two | 0 |

`all` matches a flat list exactly, so existing strategies are untouched, and
`any` is genuinely looser rather than merely accepted.

### Mutation results

Eight mutations, all detected after two rounds. **Two checks were vacuous on
first writing** — both set state by hand instead of calling `startRun`, so
deleting the seeding and the pruning changed nothing. Rewritten to exercise the
real path.

That is five vacuous checks caught across this session by the mutation pass.
Every one would have been reported as verified.

## Part 2 — Where this stands against the market

Compared with Streak, Tradetron, AlgoTest and uTrade Algos.

### Genuinely ahead

- **Provenance on every backtest.** The result names its data source and warns
  when the bars are not verified exchange data. No competitor shows this.
- **A risk budget applied before the order leaves the machine** — daily loss
  cap, per-order ceiling, kill switch — visible rather than discovered by
  hitting it.
- **Plain-English readback** of the strategy, generated by the engine that runs
  it, so the description cannot drift from the behaviour.
- **Local-first.** Credentials and history stay on the machine.

### Gaps against the field

| | Status | Notes |
|---|---|---|
| **Multi-leg / options** | Absent | The largest gap. Tradetron and AlgoTest are built around option spreads; the DSL is single-instrument, long-only |
| **OR between conditions** | Absent | `evaluate_conditions_all` is AND-only. Every competitor has both |
| **Basket / multi-symbol runs** | Absent | One run is one instrument. Scanning a watchlist is table stakes |
| **Short selling** | Partial | The book now handles shorts correctly, but no strategy can open one |
| **Walk-forward / out-of-sample** | Absent | A single backtest over one period overfits, and the verdict cannot see it |
| **Live socket feed** | Absent | 5-second REST polling. Fine at bar granularity, not for scalping |
| **Alerts** | Absent | No email/Telegram on fills, halts, or a run stopping |

### Built on 17 August 2026

Everything in the list below except options, which is left deliberately — see
the note at the end of this section.

**OR between conditions.** The top level of a condition list is still AND, so
every strategy saved before this behaves identically. OR arrives by making any
entry a group: `{"any": [...]}` or `{"all": [...]}`, nesting freely, so
"A and (B or C)" is written rather than inferred. The validator recurses — a
misspelt field inside a group would otherwise pass at save time and fail at run
time, which is the failure the validator exists to prevent — and the explainer
brackets groups so the sentence survives being read aloud.

In the builder it is one **Match all / Match any** toggle per block, and the
joiner drawn between rows changes with it. Not a joiner per pair of rows:
"A and B or C" means nothing until precedence is stated, and a control that
silently picks one is worse than one that cannot express the case. Mixed logic
is written in the JSON, which nests properly.

**Multi-symbol paper runs.** One strategy, several instruments, comma
separated. Each symbol becomes its own run — the scheduler already gives every
run a thread and the engine already keeps per-run state, so a basket is a
fan-out rather than a new concept. One instrument failing to start does not
drop the rest, and each run stops independently.

**Consistency across periods.** The same rules measured over consecutive
stretches of the range, reported per period with return, P&L, trades and win
rate. A headline return says nothing about *when* it was earned: in testing, a
run showing +1.9% overall was +4.5% and +4.3% in the first half and −3.3% and
−3.4% in the second.

Deliberately **not** called walk-forward. There is no parameter search here, so
nothing is fitted on one stretch and tested on the next; naming it walk-forward
would claim a rigour this does not have. The card says so on screen.

**Alerts.** A run that halts announces itself through a desktop notification,
which reaches a backgrounded tab where a toast does not — as does trading
halting for the day. On the transition only: a halted run re-announcing every
five seconds is noise that gets muted, and then the next real halt is missed
too. Permission is asked when a run starts, not on page load.

**Usability.** Duplicate a saved strategy; "Save & backtest" in one step; the
backtest range, symbol and timeframe are remembered between visits.

**Not built: options.** Multi-leg needs an instrument master, greeks, margin
modelling and multi-leg execution — weeks, not days, and it changes the shape
of the DSL, the order path and the risk manager. It is worth deciding
deliberately rather than arriving at halfway.

### What I would build next, in order

1. **OR groups in the DSL.** The single highest ratio of user value to work —
   about a day across the evaluator, validator, explainer and builder. Every
   competitor has it and its absence is immediately visible.
2. **Multi-symbol runs.** One strategy against a watchlist. The scheduler
   already runs a thread per run; this is mostly UI and a fan-out.
3. **Walk-forward backtesting.** Split the range, optimise on the first part,
   report on the second. This is the honest answer to overfitting and would pair
   well with the provenance work already done.
4. **Alerts on halts and run failures.** A paper run that stops at 11am is
   currently discovered whenever the page is next opened.
5. **Options,** if the market is the goal. Large — instrument master, greeks,
   margin, multi-leg execution. Worth deciding deliberately rather than drifting
   into.

### Usability, cheapest first

- **Duplicate a saved strategy.** "Open" loads one into the builder but saving
  makes a copy; there is no explicit duplicate-and-edit.
- **Backtest from the builder.** Currently: save, switch view, select, run.
- **Compare two backtests side by side.** Impossible today without screenshots.
- **Remember the last backtest range** rather than defaulting every time.
- **Show the instrument's recent chart** beside the builder, so a threshold can
  be picked against something rather than guessed.

### Known limits, stated

- **The two Choice entitlements.** `MultipleTouchline` and `OpenGraph/ChartData`
  are unavailable for Client ID M09984. Marketstack now covers historical data,
  but `MultipleTouchline` is what paper runs poll — until it is enabled, paper
  runs correctly refuse to start.
- **The proxy opens a TCP connection per API call.** Not a correctness fault;
  measurable overhead for a polling UI.
- **Sub-492px layout is untested** — headless Edge clamps there.
- **No walk-forward**, so any backtest verdict reflects one period only.
