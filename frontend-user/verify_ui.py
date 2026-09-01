"""Drive the interface in a headless browser and check what came out.

    python frontend-user/verify_ui.py

Four phases, all against the shipped index.html rather than a copy — a copy
would drift, and the point is to run the code that ships:

1. the chart engine, driven against fixed data
2. the page's own render functions, which hold arithmetic the engine does not
3. the strategy builder, whose compiled output is then handed to the engine's
   real validator — a starter template that does not validate is the worst
   possible first experience for someone here to avoid writing JSON
4. a scan for two top-level functions sharing a name

What all of it looks for is the class of fault that does not throw. An
`emptyState` collision once left every chart's empty branch calling a function
that returned a string nobody used, so a chart with no data rendered a blank
box, with no error anywhere. Both declarations were used; neither was
undefined; no linter had anything to say.
"""

import re
import subprocess
import sys

# Report in UTF-8 whatever the console is set to. A detail string carrying a
# rupee sign or an arrow would otherwise raise UnicodeEncodeError on a cp1252
# console and kill the run — which exits non-zero and reads exactly like a
# detected failure, so a mutation test against this script silently "passes"
# no matter what is broken.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import tempfile
from pathlib import Path

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
INDEX = Path(__file__).resolve().parent / "index.html"

CASES = """
const R = [];
const T = (name, ok, detail) => R.push([name, !!ok, detail || ""]);
const host = id => { const d = document.createElement("div");
  d.className = "chart"; d.id = id; document.body.appendChild(d); return d; };
const marks = h => h.querySelectorAll("path.bar, path.bar-h, path.series-line").length;

// -- every chart draws something from ordinary data ------------------------
const bars = host("a");
barChartH(bars, [{label:"RELIANCE",value:120},{label:"TCS",value:80}]);
T("barChartH draws its bars", marks(bars) === 2, marks(bars) + " marks");

const div = host("b");
divergingBarH(div, [{label:"UP",value:50},{label:"DOWN",value:-30}]);
T("divergingBarH draws both signs", marks(div) === 2, marks(div) + " marks");
T("a negative bar grows from its own zero end",
  div.querySelectorAll("path.bar-h.is-neg").length === 1);

const line = host("c");
lineChart(line, [100, 120, 90, 140]);
const path = line.querySelector("path.series-line");
T("lineChart draws a series", !!path);
T("the line carries its own length for the draw-on",
  path && parseFloat(path.style.getPropertyValue("--len")) > 0,
  path ? path.style.getPropertyValue("--len") : "no path");

const col = host("d");
columnChart(col, [10, -5, 8]);
T("columnChart draws a column per value", marks(col) === 3, marks(col) + " marks");
T("only the losing column flips its baseline",
  col.querySelectorAll("path.bar.is-neg").length === 1);

// -- the histogram, whose bucketing is the only real arithmetic here -------
const h1 = host("e");
histogram(h1, [-5, -1, 0, 1, 2, 3, 8, 12]);
T("histogram draws buckets", marks(h1) > 0, marks(h1) + " buckets");

// The maximum must land in the last bucket rather than one past the end --
// the off-by-one that silently drops the best trade in the run.
const h2 = host("f");
histogram(h2, [0, 0, 0, 0, 0, 0, 0, 10]);
const counted = [...h2.querySelectorAll("path.bar")].length;
T("the largest value is not dropped off the end", counted === 2,
  counted + " non-empty buckets, expected 2");

// -- the failure that started this: empty data must SAY something ----------
const empties = [
  ["barChartH", h => barChartH(h, [])],
  ["divergingBarH", h => divergingBarH(h, [])],
  ["lineChart", h => lineChart(h, [1])],
  ["columnChart", h => columnChart(h, [])],
  ["histogram", h => histogram(h, [])],
];
empties.forEach(([name, run], i) => {
  const h = host("empty" + i);
  h.innerHTML = "<b>stale</b>";
  run(h);
  const note = h.querySelector(".empty");
  T(name + " explains itself when there is no data",
    !!note && note.textContent.trim().length > 0,
    note ? "" : "rendered nothing at all");
});

document.title = JSON.stringify(R);
"""

# Phase two drives the page's own render functions, which hold arithmetic the
# engine does not: grouping trades into months, averaging wins against losses,
# picking which holdings count as having moved. Loaded from file:// so nothing
# reaches the network; the page's own start-up is expected to fail on fetch and
# is ignored.
PAGE_CASES = """
const R = [];
const T = (name, ok, detail) => R.push([name, !!ok, detail || ""]);
const svgIn = id => {
  const h = document.getElementById(id);
  return h ? h.querySelectorAll("path.bar, path.bar-h, path.series-line").length : -1;
};

state.broker = { connected: true, mode: "PAPER" };

// -- today's movers --------------------------------------------------------
renderMovers([
  { symbol: "RELIANCE", day_pnl: 1200, day_change_pct: 1.4, current_price: 2500, quantity: 10 },
  { symbol: "TCS",      day_pnl: -800, day_change_pct: -0.9, current_price: 3900, quantity: 5 },
  { symbol: "FLAT",     day_pnl: 0,    day_change_pct: 0,    current_price: 100,  quantity: 1 },
]);
T("movers charts only the holdings that moved", svgIn("chart-movers") === 2,
  svgIn("chart-movers") + " bars, expected 2 (the flat one excluded)");
T("movers reports the net and the split",
  /1 up/.test(document.getElementById("movers-note").textContent) &&
  /1 down/.test(document.getElementById("movers-note").textContent),
  document.getElementById("movers-note").textContent);

const allFlat = document.getElementById("movers-card");
renderMovers([{ symbol: "X", day_pnl: 0 }]);
T("the movers card hides when nothing moved", allFlat.style.display === "none",
  "display=" + allFlat.style.display);

// -- backtest: months and the distribution note ---------------------------
const trade = (exit, pnl, ret) => ({
  exit_date: exit, entry_date: exit, pnl, return_pct: ret,
  entry_price: 100, exit_price: 100 + ret, charges: 10, qty: 1, side: "BUY",
});
renderBacktest({
  data_source: "SANDBOX_SYNTHETIC", logs: ["x"], verdict: null,
  metrics: {
    initial_capital: 100000, total_pnl: 900, return_pct: 0.9, equity_curve: [100000, 100500, 100900],
    total_trades: 5, winning_trades: 3, win_rate: 60, sharpe_ratio: 1.1,
    trades: [
      trade("2026-01-14", 500, 5), trade("2026-01-28", -200, -2),
      trade("2026-02-03", 300, 3), trade("2026-02-19", -100, -1),
      trade("2026-03-05", 400, 4),
    ],
  },
});
T("the monthly chart groups by exit month", svgIn("chart-monthly") === 3,
  svgIn("chart-monthly") + " bars, expected 3 months");
T("the monthly count names how many were positive",
  document.getElementById("monthly-note").textContent === "3 of 3 months positive",
  document.getElementById("monthly-note").textContent);
T("the distribution draws", svgIn("chart-dist") > 0, svgIn("chart-dist") + " buckets");
T("the distribution is explained in words",
  /average win/i.test(document.getElementById("dist-note").textContent),
  document.getElementById("dist-note").textContent);

// The provenance banner is the only thing telling the reader how much these
// numbers can be trusted. It used to credit everything that was not the sandbox
// to "Choice OpenAPI historical data", so a Marketstack run was attributed to
// the wrong provider and an unknown source was reassuring by default.
const banner = () => document.getElementById("b-provenance").textContent;
const bannerTone = () => document.getElementById("b-provenance")
  .querySelector(".note").className;
const runWith = source => renderBacktest({
  data_source: source, logs: [], verdict: null,
  metrics: { initial_capital: 100000, total_pnl: 1, equity_curve: [100000, 100001],
             trades: [trade("2026-01-14", 1, 1)] },
});

runWith("SANDBOX_SYNTHETIC");
T("sandbox data is flagged as a warning", /warn/.test(bannerTone()) && /Sandbox/.test(banner()),
  banner().slice(0, 50));

runWith("MARKETSTACK");
T("Marketstack data is credited to Marketstack, not Choice",
  /Marketstack/.test(banner()) && !/Choice OpenAPI/.test(banner()), banner().slice(0, 60));

runWith("CHOICE_OPENAPI");
T("Choice data is still credited to Choice",
  /Choice OpenAPI/.test(banner()), banner().slice(0, 50));

runWith("SOMETHING_NEW");
T("an unrecognised source is not vouched for",
  /warn/.test(bannerTone()) && /Unrecognised/.test(banner()), banner().slice(0, 60));

// A run with no trades at all must not throw, and must not leave a stale chart.
renderBacktest({ data_source: "X", logs: [], verdict: null,
  metrics: { initial_capital: 1, equity_curve: [1, 2], trades: [] } });
// -- positions: the totals under the table, and the order filter -----------
state.broker = { connected: true, mode: "PAPER" };
// Sized so the two formulas cannot agree: a big position up 10% and a tiny one
// down 50%. Weighted by capital that is +4.55%; averaging the percentages
// gives -20% and would report a losing book as it quietly compounds.
state.holdings = [
  { symbol: "A", quantity: 10, average_price: 100, current_price: 110,
    value: 1100, pnl: 100, return_pct: 10, day_pnl: 50, day_change_pct: 1 },
  { symbol: "B", quantity: 1, average_price: 100, current_price: 50,
    value: 50, pnl: -50, return_pct: -50, day_pnl: null, day_change_pct: null },
];
renderHoldings();

const foot = document.getElementById("hold-foot").textContent;
T("the holdings table totals its own columns", /2 holdings/.test(foot), foot.trim().slice(0, 60));
T("the overall return is weighted by what was invested, not averaged",
  /\\+4\\.5\\d%/.test(foot) && !/-20/.test(foot), foot.trim().slice(0, 80));
T("the summary counts only the holdings that were priced today",
  /Priced today/.test(document.getElementById("pos-day-foot").textContent),
  document.getElementById("pos-day-foot").textContent);

state.orders = [
  { id: "1", created_at: "2026-08-13T10:00:00Z", symbol: "A", side: "BUY", order_type: "LIMIT",
    quantity: 1, executed_price: 10, price_in_paisa: 1000, execution_mode: "LIVE", status: "SIMULATED" },
  { id: "2", created_at: "2026-08-13T10:01:00Z", symbol: "B", side: "SELL", order_type: "MARKET",
    quantity: 1, executed_price: 10, price_in_paisa: 1000, execution_mode: "LIVE", status: "REJECTED",
    failure_reason: "no margin" },
];
filterOrders("rejected");
const shown = document.querySelectorAll("#order-body tr").length;
T("the order filter narrows to rejections", shown === 1, shown + " rows");
T("the summary still counts every order, not just the filtered ones",
  /2 submitted/.test(document.getElementById("order-summary").textContent),
  document.getElementById("order-summary").textContent);

filterOrders("open");
const emptyNote = document.querySelector("#order-body .empty");
T("a filter matching nothing says so rather than claiming no orders",
  !!emptyNote && /Nothing matches/.test(emptyNote.textContent),
  emptyNote ? emptyNote.textContent.trim().slice(0, 50) : "no note");
filterOrders("all");

// -- overview: charts must admit when they are showing only the top slice --
state.holdings = Array.from({ length: 12 }, (_, i) => ({
  symbol: "S" + i, quantity: 1, average_price: 100, current_price: 100 + i,
  value: 1000 + i * 100, pnl: (i - 6) * 100, return_pct: i - 6,
  day_pnl: (i - 6) * 10, day_change_pct: i - 6,
}));
renderHoldings();
T("the allocation chart says it is showing only the top few",
  /top 8 of 12/.test(document.getElementById("alloc-total").textContent),
  document.getElementById("alloc-total").textContent);
T("so does the contribution chart",
  /top 8 of/.test(document.getElementById("contrib-note").textContent),
  document.getElementById("contrib-note").textContent);

const saidSo = !!document.getElementById("chart-dist").querySelector(".empty");
T("a run with no trades says so instead of drawing", saidSo,
  saidSo ? "" : "drew nothing and explained nothing");
T("no months are claimed when there were no trades",
  document.getElementById("monthly-note").textContent === "",
  document.getElementById("monthly-note").textContent);

document.title = JSON.stringify(R);
"""

# Phase three drives the strategy builder. Its whole job is to turn choices
# into the JSON the engine runs, so what matters is the JSON that comes out —
# collected here and handed to the real validator in Python, because a starter
# template that does not survive validation is the worst possible first
# experience for someone who came here to avoid writing JSON.
BUILDER_CASES = """
const R = [];
const T = (name, ok, detail) => R.push([name, !!ok, detail || ""]);
// For checks whose detail only makes sense as an explanation of failure —
// printing "ok ... the builder was partially overwritten" reads as a
// contradiction and trains the reader to skim past the result.
const F = (name, ok, why) => R.push([name, !!ok, ok ? "" : why]);

const compiled = TEMPLATES.map((t, i) => { loadTemplate(i, false); return compileDsl(); });
T("every starter template produces rules", compiled.length === TEMPLATES.length);

// A condition can be added and removed without disturbing the rest.
loadTemplate(0, false);
const before = builder.entry.length;
addCondition("entry");
const added = builder.entry.length;
removeCondition("entry", builder.entry.length - 1);
T("conditions can be added and removed",
  added === before + 1 && builder.entry.length === before,
  `${before} -> ${added} -> ${builder.entry.length}`);

// Two rows naming the same indicator must share one definition, or the engine
// computes the same series twice under different names.
builder.entry = [
  { left: { key: "RSI", params: { length: 14 } }, op: "<", rightKind: "value", value: 30,
    right: { key: "close", params: {} } },
  { left: { key: "RSI", params: { length: 14 } }, op: ">", rightKind: "value", value: 5,
    right: { key: "close", params: {} } },
];
const shared = compileDsl();
T("one indicator is defined once however many rules use it",
  Object.keys(shared.indicators).length === 1,
  Object.keys(shared.indicators).join(", "));

// MACD and its signal line are different fields off one indicator. If the
// round trip confuses them, "MACD crosses above signal" silently becomes
// "MACD crosses above MACD", which is never true.
loadTemplate(2, false);
const macd = compileDsl();
const entry0 = macd.entry_conditions[0];
T("MACD compiles against its signal line, not itself",
  entry0.field !== entry0.value && String(entry0.value).endsWith("_signal"),
  `${entry0.field} ${entry0.operator} ${entry0.value}`);

const backAgain = operandFor(entry0.value, macd.indicators);
T("the signal line survives the round trip back into the builder",
  backAgain && backAgain.key === "MACD_SIGNAL",
  backAgain ? backAgain.key : "did not map back");
T("the bare MACD line maps back to the line, not the signal",
  operandFor(entry0.field, macd.indicators)?.key === "MACD",
  String(operandFor(entry0.field, macd.indicators)?.key));

// An empty stop must not become a stop of zero, which would close every
// position on the bar it opened.
loadTemplate(0, false);
document.getElementById("s-stop").value = "";
document.getElementById("s-target").value = "";
const noRisk = compileDsl();
T("a blank stop means no stop rather than zero",
  !("stop_loss_pct" in noRisk.actions) && !("target_pct" in noRisk.actions),
  JSON.stringify(noRisk.actions));

// Hand-edited JSON that the builder cannot represent must leave it untouched.
loadTemplate(0, false);
const kept = JSON.stringify(builder.entry);
document.getElementById("s-dsl").value = JSON.stringify({
  indicators: { weird: { type: "SUPERTREND", length: 7 } },
  entry_conditions: [{ field: "weird", operator: "<", value: 1 }],
  exit_conditions: [{ field: "weird", operator: ">", value: 2 }], actions: { buy_qty: 1 },
});
applyJson();
F("rules the builder cannot show leave it untouched",
  JSON.stringify(builder.entry) === kept,
  "the builder was partially overwritten");

// -- the ALL/ANY toggle ----------------------------------------------------
//
// "Match all" must compile to exactly the flat list it always did, so every
// strategy saved before groups existed is untouched.
loadTemplate(0, false);
addCondition("entry");
const andDsl = compileDsl();
T("match-all still compiles to a plain AND list",
  Array.isArray(andDsl.entry_conditions) && andDsl.entry_conditions.length === 2
    && !andDsl.entry_conditions.some(c => "any" in c || "all" in c),
  JSON.stringify(andDsl.entry_conditions).slice(0, 60));

setJoin("entry", "any");
const orDsl = compileDsl();
T("match-any wraps the rows in one any group",
  orDsl.entry_conditions.length === 1 && Array.isArray(orDsl.entry_conditions[0].any)
    && orDsl.entry_conditions[0].any.length === 2,
  JSON.stringify(orDsl.entry_conditions).slice(0, 70));

T("the joiner shown between rows says or",
  document.getElementById("entry-rows").dataset.join === "any",
  document.getElementById("entry-rows").dataset.join);
T("the block description stops claiming every condition must hold",
  /Any one of these/.test(document.getElementById("entry-sub").textContent),
  document.getElementById("entry-sub").textContent.slice(0, 50));

// A single row has nothing to combine, so it must not be wrapped.
removeCondition("entry", 1);
T("one condition is never wrapped in a group",
  !("any" in (compileDsl().entry_conditions[0] || {})),
  JSON.stringify(compileDsl().entry_conditions));

// Round trip: an any-group in JSON must come back as the toggle, not be
// flattened into an AND list that means something else.
setJoin("entry", "any");
addCondition("entry");
const roundTrip = compileDsl();
document.getElementById("s-dsl").value = JSON.stringify(roundTrip);
builder.join.entry = "all";
applyJson();
T("an any group loads back as match-any", builder.join.entry === "any", builder.join.entry);
T("and recompiles to the same rules",
  JSON.stringify(compileDsl().entry_conditions) === JSON.stringify(roundTrip.entry_conditions),
  JSON.stringify(compileDsl().entry_conditions).slice(0, 70));

// Loading a template after using match-any must not inherit the joiner.
loadTemplate(1, false);
T("a template resets the joiner to its own logic", builder.join.entry === "all",
  builder.join.entry);

// -- several instruments, one strategy -------------------------------------
//
// Each symbol is its own run. A halt on one must be legible without reading
// the others, and must not be reported as the whole basket stopping.
const runFor = (symbol, status, extra) => Object.assign({
  status, params: { symbol, timeframe: "1m" },
  metrics: { position_qty: 0, realized_pnl: 0, orders: 0 },
  logs: [], orders: [], _local: { id: "r-" + symbol, symbol, strategyId: "s", name: "X" },
}, extra || {});

renderRuns([runFor("RELIANCE", "RUNNING"), runFor("TCS", "RUNNING"), runFor("INFY", "HALTED",
  { logs: ["Market data unavailable after 3 attempts"] })]);

const items = document.querySelectorAll("#run-body .run-item");
T("every instrument gets its own block", items.length === 3, items.length + " blocks");
T("the header counts only the live ones",
  /2 running/.test(document.getElementById("run-state").textContent),
  document.getElementById("run-state").textContent);
T("the halted run shows its reason",
  /Market data unavailable/.test(document.getElementById("run-body").textContent));
T("the caveat appears once, not per run",
  (document.getElementById("run-body").textContent.match(/never added together/g) || []).length === 1);
T("stop-all names how many it will stop",
  /Stop all 2/.test(document.getElementById("btn-stop-run").textContent),
  document.getElementById("btn-stop-run").textContent);

renderRuns([runFor("RELIANCE", "STOPPED"), runFor("TCS", "STOPPED")]);
T("with nothing running, stop is disabled",
  document.getElementById("btn-stop-run").disabled === true);

// A status call that fails must not be drawn as the last known state.
renderRuns([runFor("RELIANCE", "UNKNOWN", { error: "backend unreachable" })]);
T("an unreachable status is shown as unknown, not as running",
  /Status unavailable/.test(document.getElementById("run-body").textContent),
  document.getElementById("run-body").textContent.slice(0, 60));

renderRuns([]);
T("the monitor hides when there are no runs",
  document.getElementById("run-monitor").style.display === "none");

// -- consistency across periods --------------------------------------------
renderPeriods([
  { period: 1, from: "2026-01-01", to: "2026-03-31", return_pct: 8.2, pnl: 8200, trades: 12, win_rate: 66.7 },
  { period: 2, from: "2026-04-01", to: "2026-06-30", return_pct: -2.1, pnl: -2100, trades: 9, win_rate: 33.3 },
  { period: 3, from: "2026-07-01", to: "2026-09-30", return_pct: 0.0, pnl: 0, trades: 0, win_rate: null },
]);
const periodRows = document.querySelectorAll("#periods-body tr");
T("a row per period", periodRows.length === 3, periodRows.length + " rows");
T("the count names how many were profitable",
  /1 of 3/.test(document.getElementById("periods-note").textContent),
  document.getElementById("periods-note").textContent);
T("a period that never traded shows no win rate rather than zero",
  /—/.test(periodRows[2].textContent),
  periodRows[2].textContent.replace(/\\s+/g, " ").slice(0, 60));

renderPeriods([]);
T("the card hides when there were too few bars to split",
  document.getElementById("periods-card").style.display === "none");

// -- alerts on a run stopping by itself ------------------------------------
//
// A run that halts at 11am is otherwise found whenever the page is next looked
// at. The alert must fire on the transition only — a halted run re-announcing
// itself every five seconds is noise that gets muted, and then the next real
// halt is missed too.
const alerts = [];
const realToast = window.toast;
window.toast = (msg, kind) => { alerts.push(msg); };

state.runStatus = {};
const running = runFor("RELIANCE", "RUNNING");
announceRunChanges([running]);
T("a run that is merely running says nothing", alerts.length === 0, alerts.join(" | "));

const halted = runFor("RELIANCE", "HALTED", { logs: ["Choice API key expired during the run"] });
announceRunChanges([halted]);
T("a halt is announced with its reason",
  alerts.length === 1 && /API key expired/.test(alerts[0]), alerts.join(" | "));

announceRunChanges([halted]);
announceRunChanges([halted]);
T("a halt is announced once, not on every poll", alerts.length === 1,
  alerts.length + " alerts after three polls");

state.runStatus = {};
announceRunChanges([runFor("TCS", "RUNNING")]);
announceRunChanges([runFor("TCS", "STOPPED")]);
T("stopping a run deliberately is not an alert", alerts.length === 1,
  alerts.join(" | "));

// The kill switch and the daily loss cap stop everything, not one run.
state.wasHalted = false;
state.broker = { connected: true, mode: "PAPER" };
state.budget = { halted: true, halt_reason: "Daily loss budget reached",
                 max_daily_loss: 1, daily_loss_remaining: 0, daily_loss_used_pct: 100,
                 max_order_value: 1, max_order_quantity: 1, realized_pnl_today: -1 };
renderRiskStrip();
T("trading halting for the day is announced",
  alerts.some(a => /Trading halted/.test(a)), alerts.join(" | "));
const halts = alerts.filter(a => /Trading halted/.test(a)).length;
renderRiskStrip();
const stillOnce = alerts.filter(a => /Trading halted/.test(a)).length === halts;
T("and only once", stillOnce, stillOnce ? "" : "re-announced on the next render");

window.toast = realToast;

// -- auto refresh ----------------------------------------------------------
//
// The page used to need a manual Refresh to show current figures. Putting that
// on a timer is only an improvement if it does not disturb what is on screen.
let refreshCalls = 0;
const realLoaders = {};
["loadFunds", "loadPortfolio", "loadOrders", "loadStrategies", "loadRiskBudget"]
  .forEach(name => { realLoaders[name] = window[name];
                     window[name] = async () => { refreshCalls++; }; });

state.token = "t";
state.broker = { connected: true, mode: "PAPER" };

// The background poll must not replace populated rows with skeletons: that
// reads as the data being lost, on every cycle. It does not go through
// refreshAll at all, which is what guarantees it.
document.getElementById("hold-body").innerHTML = "<tr><td>REAL ROW</td></tr>";
state.view = "overview";
await autoRefreshTick();
T("a background poll leaves the table alone",
  /REAL ROW/.test(document.getElementById("hold-body").textContent),
  document.getElementById("hold-body").textContent.slice(0, 30));

// A deliberate refresh still says something is happening.
await refreshAll();
T("a manual refresh still shows skeletons",
  !/REAL ROW/.test(document.getElementById("hold-body").textContent));

// Never stack: a slow response would queue refreshes and fire them together
// at an API with a rate limit.
state.refreshing = true;
const callsBeforeStack = refreshCalls;
await refreshAll();
T("a refresh already in flight is not stacked", refreshCalls === callsBeforeStack,
  (refreshCalls - callsBeforeStack) + " extra loader calls");
state.refreshing = false;

// A hidden tab must not spend the broker's rate limit on data nobody reads.
Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
const hiddenBefore = refreshCalls;
await autoRefreshTick();
T("a hidden tab does not poll", refreshCalls === hiddenBefore,
  (refreshCalls - hiddenBefore) + " calls while hidden");
Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });

// The whole point of this change: a poll must not refetch everything. Reading
// the strategy builder does not require re-pulling holdings, and the strategy
// list only changes when one is saved, which reloads it anyway.
state.view = "overview";
const onOverview = refreshCalls;
await autoRefreshTick();
const overviewCalls = refreshCalls - onOverview;
T("a data view fetches only what it shows", overviewCalls === 4,
  overviewCalls + " requests, expected 4 (funds, portfolio, orders, limits)");

state.view = "strategy";
const onBuilder = refreshCalls;
await autoRefreshTick();
const builderCalls = refreshCalls - onBuilder;
T("a non-data view costs one request, not five", builderCalls === 1,
  builderCalls + " requests, expected 1 (the halt check)");
state.view = "overview";

// Off must mean off.
localStorage.setItem("refreshRate", "off");
startAutoRefresh();
T("off schedules no timer", state.autoTimer === null || state.autoTimer === undefined,
  "timer id " + state.autoTimer);
localStorage.setItem("refreshRate", "1m");
startAutoRefresh();
T("a minute is the default cadence", refreshIntervalMs() === 60000,
  refreshIntervalMs() + "ms");

// Signed out, the timer must not keep calling with no token.
state.token = null;
const outBefore = refreshCalls;
autoRefreshTick();
T("a signed-out page does not poll", refreshCalls === outBefore);
state.token = "t";

// The freshness readout is what separates live figures from frozen ones.
state.lastRefresh = Date.now();
renderFreshness();
T("freshness says how old the figures are",
  /just now/.test(document.getElementById("freshness").textContent),
  document.getElementById("freshness").textContent);

state.lastRefresh = Date.now() - 30 * 60 * 1000;
renderFreshness();
const fresh = document.getElementById("freshness");
T("figures well past an interval are marked stale",
  fresh.classList.contains("is-stale"), fresh.textContent);

["loadFunds", "loadPortfolio", "loadOrders", "loadStrategies", "loadRiskBudget"]
  .forEach(name => { window[name] = realLoaders[name]; });

// A rebuilt <select> must not throw away a choice made seconds earlier.
//
// `api` is stubbed because loadStrategies fetches before it rebuilds the
// select: on a file:// page the fetch throws, the catch returns early, and the
// select is never touched — so the check passed no matter what, which is
// exactly the vacuous result this file exists to avoid.
const realApi = window.api;
window.api = async () => ([{ id: "a", name: "One", dsl_definition: {}, created_at: "2026-01-01" },
                           { id: "b", name: "Two", dsl_definition: {}, created_at: "2026-01-01" }]);
const sel = document.getElementById("b-strategy");
sel.innerHTML = `<option value="a">One</option><option value="b">Two</option>`;
sel.value = "b";
await loadStrategies();
T("the chosen strategy survives a refresh", sel.value === "b", "became " + sel.value);
T("and the list was actually rebuilt", sel.options.length === 2,
  sel.options.length + " options");
window.api = realApi;

// -- Health: a failed GitHub check must not read as "Up to date" -----------
T("Health does not claim up to date before a successful check",
  !/Up to date/.test(document.getElementById("app-update-state").textContent),
  document.getElementById("app-update-state").textContent);

const realFetch = window.fetch;
const keptUpdateToast = window.toast;
let updateToasts = [];
window.toast = (msg) => { updateToasts.push(msg); };
window.fetch = async () => ({
  ok: true,
  json: async () => ({
    update_available: false,
    status: "error",
    current_version: "1.2.8",
    latest_version: "1.2.8",
    error: "GitHub rate limit exceeded",
    html_url: "https://github.com/runFast123/Algo_master_trade/releases",
  }),
});
await checkAppUpdates(false);
T("a failed update check is not painted as up to date",
  document.getElementById("app-update-state").textContent.trim() === "Check failed",
  document.getElementById("app-update-state").textContent);
T("a failed silent check does not toast latest-version",
  !updateToasts.some(m => /latest version/.test(m)), updateToasts.join(" | "));

window.fetch = async () => ({
  ok: true,
  json: async () => ({
    update_available: true,
    status: "update_available",
    current_version: "1.2.8",
    latest_version: "1.2.9",
    tag_name: "v1.2.9",
    release_name: "Choice FINX Algo v1.2.9",
    published_at: "2026-09-01T05:44:57Z",
  }),
});
await checkAppUpdates(false);
T("1.2.8 is offered 1.2.9 when GitHub answers",
  /Update available: v1.2.9/.test(document.getElementById("app-update-state").textContent),
  document.getElementById("app-update-state").textContent);
window.fetch = realFetch;
window.toast = keptUpdateToast;

let quotePaths = [];
const realApiQuotes = window.api;
window.api = async (path) => {
  quotePaths.push(path);
  if (path === "/auth/choice/status") {
    return { connected: true, mode: "PAPER", environment: "PROD",
             market_data_ok: true, credential_state: "OK" };
  }
  if (path === "/market/quotes") {
    return { status: "SUCCESS", data: [{ symbol: "RELIANCE", ltp: 1324.10 }] };
  }
  return {};
};
state.token = "t";
state.broker = { connected: true, mode: "PAPER" };
await refreshBroker();
T("broker refresh probes live quotes", quotePaths.includes("/market/quotes"),
  quotePaths.join(" | "));
window.api = realApiQuotes;

// -- finished runs are not re-polled ---------------------------------------
//
// Nothing removed runs from the list, so every run started in a session was
// re-fetched on every five-second poll for the rest of it — including ones
// that had stopped and could never change again.
const realApi2 = window.api;
let statusCalls = 0;
window.api = async (path) => {
  statusCalls++;
  return { status: "HALTED", params: { symbol: "X", timeframe: "1m" },
           metrics: {}, logs: ["stopped"], orders: [] };
};
state.runs = [{ id: "r1", strategyId: "s", name: "N", symbol: "X" }];
state.runResults = {};
state.runStatus = {};

await refreshRun();
const firstPoll = statusCalls;
await refreshRun();
await refreshRun();
T("a finished run is fetched once, not on every poll", statusCalls === firstPoll,
  (statusCalls - firstPoll) + " extra requests for a run that had stopped");

// A live run must still be polled every time.
window.api = async () => { statusCalls++; return { status: "RUNNING", params: {}, metrics: {}, logs: [], orders: [] }; };
state.runs = [{ id: "r2", strategyId: "s", name: "N", symbol: "Y" }];
state.runResults = {}; state.runStatus = {};
const liveStart = statusCalls;
await refreshRun();
await refreshRun();
T("a running run is still polled every time", statusCalls - liveStart === 2,
  (statusCalls - liveStart) + " requests over two polls");
window.api = realApi2;

// Both of these have to go through startRun. Setting the state by hand tests
// the assertion, not the code: an earlier version of these checks passed with
// the seeding and the pruning both deleted.
const alerts2 = [];
const keptToast = window.toast;
const keptPrompt = window.prompt;
const keptApi = window.api;
window.toast = m => alerts2.push(m);
window.prompt = () => "ZED";
window.api = async () => ({ run_id: "r3", symbol: "ZED", timeframe: "1m" });

state.runs = []; state.runResults = {}; state.runStatus = {};
await startRun("s", "N");
T("startRun records the run as running",
  state.runStatus.r3 === "RUNNING", String(state.runStatus.r3));

// With the status seeded, the very first poll can see a halt as a transition.
alerts2.length = 0;
announceRunChanges([{ status: "HALTED", params: { symbol: "ZED" }, logs: ["key expired"],
                      _local: { id: "r3", symbol: "ZED", name: "N" } }]);
T("a run halting before its first poll still alerts", alerts2.length === 1,
  alerts2.join(" | ").slice(0, 60));

// Starting a second batch drops the finished run rather than carrying it, and
// its status, for the rest of the session.
state.runResults.r3 = { status: "HALTED" };
window.api = async () => ({ run_id: "r4", symbol: "ACME", timeframe: "1m" });
await startRun("s", "N");
T("a finished run is dropped when a new batch starts",
  state.runs.length === 1 && state.runs[0].id === "r4",
  state.runs.map(r => r.id).join(", "));

window.toast = keptToast; window.prompt = keptPrompt; window.api = keptApi;

// -- the Choice server is chosen per connection ----------------------------
//
// It used to be one setting in a shared .env. On a multi-user install that
// meant one person testing against UAT moved everyone, and a production Client
// ID is rejected outright by the sandbox.
const envPicker = document.getElementById("c-env");
T("the connect dialog offers a server", !!envPicker && envPicker.options.length === 2,
  envPicker ? envPicker.options.length + " options" : "missing");
T("production is the default, since that is what a real account needs",
  envPicker && envPicker.value === "PROD", envPicker ? envPicker.value : "?");

// The chip must report the session's server, not the install's default —
// otherwise it tells someone they are on UAT while they trade on PROD.
state.broker = { connected: true, mode: "LIVE", environment: "UAT" };
const chipText = document.getElementById("mode-text");
chipText.textContent = state.broker.mode === "LIVE"
  ? `LIVE · ${state.broker.environment}` : "";
T("the chip names the server in use", /UAT/.test(chipText.textContent),
  chipText.textContent);

// The choice is remembered, so it is made once per account rather than on
// every reconnect.
localStorage.setItem("choiceEnv", "UAT");
prefillConnect();
T("the chosen server is restored on reconnect", envPicker.value === "UAT",
  envPicker.value);
localStorage.removeItem("choiceEnv");

// -- holdings and positions are different things ---------------------------
//
// "Portfolio value" counts holdings only. Someone with a large intraday
// position saw a figure that excluded it, with nothing on screen saying so —
// a scope silently presented as a total.
state.broker = { connected: true, mode: "PAPER" };
state.positions = [];
state.holdings = [{ symbol: "A", quantity: 10, average_price: 100,
                    current_price: 110, value: 1100, pnl: 100, return_pct: 10,
                    day_pnl: 50, day_change_pct: 1, priced_from_close: false }];
renderHoldings();

const holdCard = document.querySelector("#view-positions .card-sub");
T("the holdings table explains what a holding is",
  /demat/.test(document.getElementById("view-positions").textContent),
  "no explanation found");
const stated = /not counted in the figures above/
  .test(document.getElementById("view-positions").textContent);
T("the positions table says it is excluded from the figures above", stated,
  stated ? "" : "no exclusion stated");

// With no open positions there is nothing to exclude, so nothing is said.
document.getElementById("kpi-portfolio-foot").innerHTML = "";
state.summary = { cost: 1000 };
renderHoldings();

// With positions open, the tile must name the exclusion rather than leave the
// reader to notice the tables do not add up.
state.positions = [{ symbol: "B", value: 50000 }, { symbol: "C", value: 25000 }];
renderHoldings();
const foot = document.getElementById("kpi-portfolio-foot").textContent;
T("the portfolio tile names what it leaves out", /excludes 2 open positions/.test(foot),
  foot.slice(0, 80));

state.positions = [{ symbol: "B", value: 50000 }];
renderHoldings();
const singular = document.getElementById("kpi-portfolio-foot").textContent;
T("and gets the singular right",
  singular.includes("excludes 1 open position")
    && !singular.includes("excludes 1 open positions"),
  singular.slice(0, 80));

state.positions = [];
renderHoldings();
T("with nothing open it says nothing",
  !/excludes/.test(document.getElementById("kpi-portfolio-foot").textContent),
  document.getElementById("kpi-portfolio-foot").textContent.slice(0, 60));

// The ordering that made this worth extracting: loadPortfolio fetches
// holdings first and positions second. Computed inline, the exclusion note ran
// before the position list existed and only appeared on the *next* refresh —
// right sometimes, which is worse than absent.
const keptApi3 = window.api;
window.api = async (path) => {
  if (path.includes("/positions")) {
    return { data: [{ symbol: "NIFTYFUT", quantity: 50, average_price: 100,
                      current_price: 105, value: 5250, pnl: 250 }] };
  }
  return { data: [{ symbol: "A", quantity: 10, average_price: 100,
                    current_price: 110, value: 1100, pnl: 100, return_pct: 10,
                    day_pnl: 50, day_change_pct: 1, priced_from_close: false }],
           summary: { value: 1100, cost: 1000, pnl: 100, holdings: 1,
                      winners: 1, losers: 0, flat: 0, day_pnl: 50 } };
};
state.positions = [];
await loadPortfolio();
const afterLoad = document.getElementById("kpi-portfolio-foot").textContent;
T("one full load is enough for the exclusion to appear",
  afterLoad.includes("excludes 1 open position"), afterLoad.slice(0, 90));
T("and the positions card names the value left out",
  document.getElementById("pos-count").textContent.includes("not in portfolio value"),
  document.getElementById("pos-count").textContent);
window.api = keptApi3;

// -- choosing paper or real, both ways -------------------------------------
//
// Someone trading their own account decides whether their own orders are real.
// The control offers the other mode, so its label is the action; the chip
// beside it already says the state.
const modeBtn = document.getElementById("btn-mode");
T("the header offers a way to change mode", !!modeBtn,
  modeBtn ? "" : "no mode control");

const showMode = mode => {
  state.broker = { connected: true, mode, environment: "PROD" };
  const real = mode === "LIVE";
  const switchable = mode === "LIVE" || mode === "PAPER";
  modeBtn.style.display = switchable ? "" : "none";
  modeBtn.textContent = real ? "Switch to paper" : "Go live";
};

showMode("PAPER");
T("a paper session is offered live", modeBtn.textContent === "Go live",
  modeBtn.textContent);

showMode("LIVE");
T("a live session is offered paper", modeBtn.textContent === "Switch to paper",
  modeBtn.textContent);

showMode("DEMO");
T("a sandbox session is offered neither", modeBtn.style.display === "none",
  modeBtn.style.display);

// The escalation must state what changes rather than ask "are you sure".
const asked = [];
const keptConfirm = window.confirm;
const keptApi4 = window.api;
window.confirm = q => { asked.push(q); return false; };
state.broker = { connected: true, mode: "PAPER", environment: "PROD" };
await toggleTradingMode();
T("going live warns about real money",
  /real money/.test(asked[0] || ""), (asked[0] || "").slice(0, 60));
T("and says the switch is reversible",
  /switch back to paper/i.test(asked[0] || ""), "");

asked.length = 0;
state.broker = { connected: true, mode: "LIVE", environment: "PROD" };
await toggleTradingMode();
T("leaving live says open positions are untouched",
  /not touched/.test(asked[0] || ""), (asked[0] || "").slice(0, 60));

// Declining must change nothing.
let called = 0;
window.api = async () => { called++; return { changed: true }; };
window.confirm = () => false;
await toggleTradingMode();
T("declining the confirmation changes nothing", called === 0, called + " calls");

window.confirm = keptConfirm;
window.api = keptApi4;

const payload = document.createElement("div");
payload.id = "payload";
payload.textContent = JSON.stringify({
  templates: TEMPLATES.map((t, i) => ({ name: t.name, dsl: compiled[i] })),
});
document.body.appendChild(payload);
document.title = JSON.stringify(R);
"""


# Phase five measures layout instead of judging it. Every view is filled with
# representative data first, because an empty table never overflows and a page
# audited empty is a page audited in the one state nobody uses it in.
LAYOUT_CASES = r"""
const R = [];
const T = (name, ok, detail) => R.push([name, !!ok, detail || ""]);
const W = window.innerWidth;

state.token = "x";
state.broker = { connected: true, mode: "PAPER" };
state.funds = { AvailableMargin: 250000 };
state.holdings = [
  { symbol: "RELIANCE", quantity: 120, average_price: 2410.55, current_price: 2508.30,
    value: 300996, pnl: 11730, return_pct: 4.05, day_pnl: 1240, day_change_pct: 1.2 },
  { symbol: "HDFCBANK", quantity: 80, average_price: 1655.20, current_price: 1601.10,
    value: 128088, pnl: -4328, return_pct: -3.27, day_pnl: -880, day_change_pct: -0.9 },
  { symbol: "INFY", quantity: 200, average_price: 1490.00, current_price: 1533.75,
    value: 306750, pnl: 8750, return_pct: 2.94, day_pnl: 410, day_change_pct: 0.3 },
];
state.positions = state.holdings;
state.orders = state.holdings.map((h, i) => ({
  id: "o" + i, created_at: "2026-08-13T10:15:00Z", symbol: h.symbol,
  side: i % 2 ? "SELL" : "BUY", order_type: "LIMIT", quantity: h.quantity,
  executed_price: h.current_price, price_in_paisa: h.current_price * 100,
  execution_mode: "SIMULATED", status: i === 2 ? "REJECTED" : "SIMULATED",
  failure_reason: i === 2 ? "Insufficient margin for this order size" : null,
}));
state.summary = { total_value: 735834, total_pnl: 16152, day_pnl: 770 };
state.budget = { max_daily_loss: 20000, daily_loss_remaining: 18000, daily_loss_used_pct: 10,
                 max_order_value: 200000, max_order_quantity: 500, realized_pnl_today: -2000,
                 halted: false, as_of: "2026-08-13" };

try { renderHoldings(); } catch (e) {}
try { renderExposure(); } catch (e) {}
try { renderAllocation(state.holdings); renderContribution(state.holdings);
      renderMovers(state.holdings); } catch (e) {}
try { renderRecent(state.orders); } catch (e) {}

const VIEWS = ["overview", "positions", "strategy", "research", "health"];
// Anything with overflow-x is a scroller by design; a table that scrolls inside
// its own wrapper is correct, the same table widening the document is not.
const scroller = el => {
  for (let n = el; n && n !== document.body; n = n.parentElement) {
    const o = getComputedStyle(n).overflowX;
    if (o === "auto" || o === "scroll") return true;
  }
  return false;
};

// Badges are coloured text on a tinted wash of the same hue, which is exactly
// the pattern that quietly drops under 4.5:1. The tint is translucent, so the
// effective background has to be composited down to the first opaque ancestor
// rather than read straight off the element.
const parseColor = c => {
  const m = (c || "").match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/);
  return m ? [+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]] : null;
};
const over = (fg, bg) => fg.slice(0, 3).map((c, i) => c * fg[3] + bg[i] * (1 - fg[3]));
const lum = rgb => {
  const [r, g, b] = rgb.map(v => { v /= 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); });
  return .2126 * r + .7152 * g + .0722 * b;
};
const ratio = (a, b) => { const x = lum(a) + .05, y = lum(b) + .05; return x > y ? x / y : y / x; };
const effectiveBg = el => {
  let acc = null;
  for (let n = el; n; n = n.parentElement) {
    const c = parseColor(getComputedStyle(n).backgroundColor);
    if (!c || c[3] === 0) continue;
    if (acc === null) acc = c.slice();
    else acc = [...over(acc, c.slice(0, 3)), Math.min(1, acc[3] + c[3])];
    if (c[3] === 1) return acc.slice(0, 3);
  }
  return acc ? acc.slice(0, 3) : [0, 0, 0];
};
VIEWS.forEach(view => {
  switchView(view);
  const section = document.getElementById("view-" + view);

  const docOverflow = document.documentElement.scrollWidth - document.documentElement.clientWidth;
  // Naming the widest element that crosses the right edge turns "the page
  // scrolls sideways" into something fixable without bisecting the stylesheet.
  const edge = document.documentElement.clientWidth;
  const culprits = docOverflow <= 1 ? [] :
    [...document.querySelectorAll("body *")]
      // Something inside a scroller sticking out is that scroller doing its
      // job, and listing it buries the element that is actually to blame.
      .filter(el => el instanceof HTMLElement && el.offsetWidth > 0 && !scroller(el))
      .map(el => ({ el, right: el.getBoundingClientRect().right }))
      .filter(x => x.right > edge + 1)
      .sort((a, b) => b.right - a.right)
      .slice(0, 3)
      .map(x => `${x.el.tagName.toLowerCase()}${x.el.id ? "#" + x.el.id
        : "." + ((x.el.getAttribute("class") || "").split(" ")[0] || "?")}`
        + ` to ${Math.round(x.right)}px`);
  T(`${view} does not widen the document at ${W}px`, docOverflow <= 1,
    docOverflow > 1 ? `overflows by ${docOverflow}px — ${culprits.join(", ")}` : "");

  const spilling = [...section.querySelectorAll("*")].filter(el => {
    // SVG children have no box model — scrollWidth is the drawn extent and
    // clientWidth is 0, so every label reads as an overflow. Charts get their
    // own phase; this one is about HTML boxes.
    if (!(el instanceof HTMLElement)) return false;
    if (!el.offsetParent && el.offsetWidth === 0) return false;
    if (scroller(el)) return false;
    return el.scrollWidth - el.clientWidth > 1;
  });
  // className is an SVGAnimatedString on an SVG node, not a string, so read the
  // attribute instead — naming the offender is the whole value of this check.
  const name = e => e.tagName.toLowerCase()
    + (e.id ? "#" + e.id : "." + ((e.getAttribute("class") || "").split(" ")[0] || "?"));
  T(`${view} keeps its content inside its containers at ${W}px`, spilling.length === 0,
    spilling.slice(0, 4).map(e => `${name(e)} by ${e.scrollWidth - e.clientWidth}px`).join(", "));

  // A control too small to hit reliably is a formatting fault, not a taste one.
  const small = [...section.querySelectorAll("button, select, input, a[href]")]
    .filter(el => el.offsetParent !== null)
    .filter(el => { const r = el.getBoundingClientRect();
                    return r.height > 0 && r.height < 24; });
  T(`${view} has no controls under 24px tall at ${W}px`, small.length === 0,
    small.slice(0, 3).map(e => (e.id || e.textContent || e.tagName).toString().trim().slice(0, 24)).join(", "));

  // Every card should say what it is; an unlabelled panel is the formatting
  // fault that makes a dense page unreadable.
  const untitled = [...section.querySelectorAll(".card")].filter(c =>
    c.offsetParent !== null && !c.querySelector(".card-title, .eyebrow"));
  T(`${view} labels every card at ${W}px`, untitled.length === 0,
    untitled.map(c => c.id || "(unnamed card)").join(", "));

  // -- accessibility, per view ---------------------------------------------
  //
  // Measured rather than assumed. A bare `outline: none` on :focus
  // out-specifies the global :focus-visible rule, and the result is invisible
  // until someone tries to use the app without a mouse.
  const unfocusable = [...section.querySelectorAll("input, select, textarea, button")]
    .filter(el => el.offsetParent !== null)
    .filter(el => {
      el.focus();
      const cs = getComputedStyle(el);
      return cs.outlineStyle === "none" && parseFloat(cs.outlineWidth || 0) >= 0;
    })
    .filter(el => {
      // Only a fault when the element really is focused; some cannot take it.
      return document.activeElement === el;
    });
  T(`${view} shows a focus ring on every control at ${W}px`, unfocusable.length === 0,
    unfocusable.slice(0, 3).map(e => e.id || e.tagName.toLowerCase()).join(", "));

  // Both themes, explicitly. Headless Edge reports one `prefers-color-scheme`,
  // so testing whatever it happens to report leaves the other half of the
  // palette unchecked — and the tinted badges are defined per theme.
  ["light", "dark"].forEach(theme => {
    document.documentElement.setAttribute("data-theme", theme);
    const lowContrast = [...section.querySelectorAll(".delta, .tag, .chip")]
      .filter(el => el.offsetParent !== null && el.textContent.trim())
      .map(el => {
        const fg = parseColor(getComputedStyle(el).color);
        if (!fg) return null;
        const bg = effectiveBg(el);
        return { el, r: ratio(over(fg, bg), bg) };
      })
      .filter(x => x && x.r < 4.5);
    T(`${view} keeps badge text above 4.5:1 in ${theme} at ${W}px`, lowContrast.length === 0,
      lowContrast.slice(0, 3)
        .map(x => `"${x.el.textContent.trim().slice(0, 12)}" ${x.r.toFixed(2)}:1`).join(", "));
  });
  document.documentElement.removeAttribute("data-theme");
});

// Every header control has to be on screen, not merely inside a box that
// scrolls. The header used to be one row with `overflow-x: auto` and the
// scrollbar hidden, which satisfied both checks above � the document did not
// widen and nothing spilled its container � while putting New order, the mode
// switch, Theme and Sign out past the right edge, invisible, on any window
// under about 1230px. "Contained" is not the same as "reachable", so measure
// reachable.
const hidden = [...document.querySelectorAll(".topbar button, .topbar select")]
  .filter(el => el.offsetParent !== null)
  .filter(el => { const r = el.getBoundingClientRect();
                  return r.right > W + 1 || r.left < -1; });
T(`every header control is on screen at ${W}px`, hidden.length === 0,
  hidden.slice(0, 4).map(e => (e.id || e.textContent || e.tagName).toString().trim().slice(0, 20))
    .join(", "));

// A grid item's min-width defaults to `auto`, so a column refuses to shrink
// below the widest thing inside it and takes the page sideways. No current
// view happens to put a wide table in a grid column, which means the rule that
// prevents this is not exercised by any real page — so put one there and check
// the column still holds.
switchView("health");
const column = document.querySelector("#view-health .grid-2 > *");
const probe = document.createElement("div");
probe.className = "tbl-wrap";
probe.innerHTML = "<table><tbody><tr>" +
  Array.from({ length: 14 }, (_, i) => `<td>column-heading-${i}</td>`).join("") +
  "</tr></tbody></table>";
column.appendChild(probe);
const spill = document.documentElement.scrollWidth - document.documentElement.clientWidth;
T(`a wide table inside a grid column does not widen the page at ${W}px`, spill <= 1,
  spill > 1 ? `overflows by ${spill}px` : "");
probe.remove();

// -- document-level accessibility ------------------------------------------
T("the page has exactly one h1", document.querySelectorAll("h1").length === 1,
  document.querySelectorAll("h1").length + " found");
T("heading levels do not skip",
  !(document.querySelector("h3") && !document.querySelector("h2")));

const skip = document.querySelector(".skip-link");
T("a skip link reaches the content", !!skip && skip.getAttribute("href") === "#main",
  skip ? skip.getAttribute("href") : "no skip link");
T("the skip target exists and can take focus",
  !!document.getElementById("main") &&
  document.getElementById("main").getAttribute("tabindex") === "-1");

// The toast carries order confirmations, rejections and halt alerts. Without
// a live region none of it reaches a screen reader.
const toastEl = document.getElementById("toast");
T("the toast is a live region", toastEl && toastEl.getAttribute("role") === "status",
  toastEl ? String(toastEl.getAttribute("role")) : "missing");
toast("Order rejected", "bad");
T("a failure interrupts rather than queues",
  toastEl.getAttribute("aria-live") === "assertive", toastEl.getAttribute("aria-live"));
toast("Strategy saved", "ok");
T("a confirmation waits its turn",
  toastEl.getAttribute("aria-live") === "polite", toastEl.getAttribute("aria-live"));

switchView("overview");
document.title = JSON.stringify(R);
"""

# Wrap each harness so a thrown error is reported as a failed check rather than
# as a silent absence of output. "Nothing was reported" is the least useful
# thing a test can say.
# The order ticket. Until this change it offered four hardcoded symbols and two
# order types, so anything else — any other instrument, any stop-loss — had to
# be traded from the Choice account instead. These checks are mostly about what
# the form must *refuse*: a token it could not resolve, and a derivatives
# quantity that is not a whole lot, are both orders the exchange would reject
# after the fact.
ORDER_CASES = """
const R = [];
const T = (n, ok, d) => R.push([n, !!ok, d || ""]);

state.token = "x";
state.broker = { connected: true, mode: "PAPER", sends_real_orders: false };
openOrder();

const inst = document.getElementById("o-inst");
const type = document.getElementById("o-type");
const note = () => document.getElementById("o-inst-note").textContent;
const ticket = () => document.getElementById("o-ticket").textContent;
const result = () => document.getElementById("o-result").textContent;

// -- resolving an instrument ----------------------------------------------
inst.value = "RELIANCE"; instrumentTyped();
T("a known symbol resolves to its token", selectedInstrument().token === "2885",
  selectedInstrument().token);
T("the ticket names what it matched", note().indexOf("2885") > -1, note());

inst.value = "NOTAREALTHING"; instrumentTyped();
T("an unknown symbol resolves to no token at all",
  selectedInstrument().token === "", selectedInstrument().token);
T("and the form says it did not match", note().indexOf("No match") > -1, note());

// -- which price fields each order type shows ------------------------------
const shown = id => document.getElementById(id).style.display !== "none";
type.value = "RL_MKT"; orderTypeChanged();
T("a market order asks for no price",
  !shown("o-price-field") && !shown("o-trigger-field"));
type.value = "RL_LIMIT"; orderTypeChanged();
T("a limit order asks for a price only",
  shown("o-price-field") && !shown("o-trigger-field"));
type.value = "SL_MKT"; orderTypeChanged();
T("a stop-loss market order asks for a trigger only",
  !shown("o-price-field") && shown("o-trigger-field"));
type.value = "SL_LIMIT"; orderTypeChanged();
T("a stop-loss limit order asks for both",
  shown("o-price-field") && shown("o-trigger-field"));

// -- lot sizes -------------------------------------------------------------
state.instruments.NIFTYFUT = { token: "35000", lot: 50, name: "NIFTY FUT" };
inst.value = "NIFTYFUT"; type.value = "RL_MKT"; orderTypeChanged();
document.getElementById("o-qty").value = "30"; updateTicket();
T("a quantity that is not a whole lot is flagged",
  ticket().indexOf("multiple of the 50") > -1);
document.getElementById("o-qty").value = "100"; updateTicket();
T("a whole multiple is not flagged", ticket().indexOf("multiple of the 50") === -1);
T("and the ticket shows the quantity in lots", ticket().indexOf("lots of 50") > -1);

// -- what must never reach the API ----------------------------------------
//
// Each of these would be refused upstream, which is a worse place to find out:
// the order has already been sent, and the message comes back in the broker's
// words rather than the form's.
// Every path the form calls, not just the last: a placed order is followed by
// a refresh of the order list and the funds, so reading one variable would
// report the refresh and miss the order entirely.
// Method as well as path: placing an order is followed by a refresh that GETs
// the same "/orders/" path, so counting the path alone cannot tell the
// submission apart from the reload that follows it.
let calls = [];
const posted = () => calls.filter(c => c.method === "POST" && c.path === "/orders/").length;
const trace = () => calls.map(c => c.method + " " + c.path).join(" | ");
const realApi = window.api;
window.api = async (path, opts) => {
  calls.push({ path, method: (opts && opts.method) || "GET",
               body: opts && opts.body ? JSON.parse(opts.body) : null });
  return { message: "ok" };
};

inst.value = "GIBBERISH"; document.getElementById("o-qty").value = "1";
await doPlaceOrder({ preventDefault: () => {} });
T("an unresolved symbol is refused before any request", posted() === 0, trace());
T("and the form explains why", result().indexOf("not recognised") > -1, result());

calls = [];
inst.value = "NIFTYFUT"; document.getElementById("o-qty").value = "30";
await doPlaceOrder({ preventDefault: () => {} });
T("a part-lot quantity is refused before any request", posted() === 0, trace());
T("and the form names the lot", result().indexOf("lots of 50") > -1, result());

calls = [];
inst.value = "RELIANCE"; document.getElementById("o-qty").value = "1";
type.value = "SL_MKT"; orderTypeChanged();
document.getElementById("o-trigger").value = "0";
await doPlaceOrder({ preventDefault: () => {} });
T("a stop-loss with no trigger is refused before any request",
  posted() === 0, trace());

// -- the instrument's own segment wins over the dropdown -------------------
//
// A token is unique only within a segment: 2885 in segment 1 is Reliance
// equity, in segment 2 an unrelated derivative. The segment used to be taken
// from a dropdown defaulting to NSE Cash, so picking an F&O contract and
// leaving the dropdown alone sent an F&O token against the cash segment.
calls = [];
state.instruments.NIFTYFUT = { token: "35000", lot: 50, name: "NIFTY FUT", segment: 2 };
inst.value = "NIFTYFUT";
document.getElementById("o-seg").value = "1";        // deliberately wrong
document.getElementById("o-qty").value = "50";
type.value = "RL_LIMIT"; orderTypeChanged();
document.getElementById("o-price").value = "100";
await doPlaceOrder({ preventDefault: () => {} });
const sent = calls.find(c => c.path === "/orders/" && c.method === "POST");
T("the order follows the instrument's segment, not the dropdown",
  !!sent && sent.body.segment_id === 2,
  sent ? String(sent.body.segment_id) : trace());

// -- a large quantity is questioned even with no price ---------------------
//
// The value check needs a price, and a market order on an instrument you do
// not hold had none — so the large-order confirmation was skipped entirely,
// exactly when the instrument is unfamiliar.
state.instruments.NEWNAME = { token: "44444", lot: 1, name: "Unheld Co", segment: 1 };
state.holdings = [];
state.preview = null;
let asked = 0;
window.confirm = (text) => { asked++; return true; };
calls = [];
inst.value = "NEWNAME";
document.getElementById("o-seg").value = "1";
document.getElementById("o-qty").value = "10000";
type.value = "RL_MKT"; orderTypeChanged();
await doPlaceOrder({ preventDefault: () => {} });
T("a huge quantity with no price is still questioned", asked > 0, String(asked));
window.confirm = () => true;

// A valid order must still go through, or every check above would pass on a
// form that simply never submits.
calls = [];
inst.value = "RELIANCE"; document.getElementById("o-qty").value = "1";
type.value = "RL_LIMIT"; orderTypeChanged();
document.getElementById("o-price").value = "2500";
await doPlaceOrder({ preventDefault: () => {} });
T("a valid order is still submitted", posted() === 1, trace());

window.api = realApi;
document.title = JSON.stringify(R);
"""


# The broker actions that used to require the Choice website: amending and
# cancelling a working order, converting a position between products, and
# moving cash. Each one either moves money or changes what happens to a
# position at the close, so these checks are mostly about what must be refused
# and about the identifiers that have to survive the round trip.
ACTION_CASES = """
const R = [];
const T = (n, ok, d) => R.push([n, !!ok, d || ""]);

state.token = "x";
state.broker = { connected: true, mode: "LIVE", sends_real_orders: true,
                 environment: "PROD", vendor_id: "M09984" };

let calls = [];
window.api = async (path, opts) => {
  calls.push({ path, method: (opts && opts.method) || "GET",
               body: opts && opts.body ? JSON.parse(opts.body) : null });
  return { message: "done", data: {} };
};
const realConfirm = window.confirm;
window.confirm = () => true;

// -- which orders can be acted on -----------------------------------------
T("an open order counts as working", isWorking("OPEN"));
T("a partially filled order counts as working", isWorking("PARTIALLY FILLED"));
T("a rejected order does not", !isWorking("REJECTED"));
T("a cancelled order does not", !isWorking("CANCELLED"));
T("a filled order does not", !isWorking("COMPLETE"));

state.book = [
  { order_id: "101", exchange_order_no: "E101", gateway_order_no: "G101",
    symbol: "RELIANCE", side: "BUY", quantity: 10, price: 2500, trigger_price: 0,
    status: "OPEN", segment_id: 1, token: "2885", order_type: "RL_LIMIT",
    product_type: "CNC" },
  { order_id: "102", symbol: "INFY", side: "SELL", quantity: 5, price: 1500,
    status: "REJECTED", segment_id: 1, token: "1594", order_type: "RL_LIMIT" },
];
renderOrderBook(state.book);
const bookText = document.getElementById("book-body").textContent;
T("the book lists the working order", bookText.indexOf("RELIANCE") > -1);
T("and hides the rejected one", bookText.indexOf("INFY") === -1, bookText);

// -- an order that cannot be amended safely must not offer it --------------
//
// Choice needs the order echoed back in full. The book's field names are
// unverified, and the normaliser used to default a missing order type to
// RL_LIMIT — which turns a stop order into a plain limit on amendment and
// removes the protective stop, silently.
state.book = state.book.concat([{
  order_id: "103", symbol: "TCS", side: "BUY", quantity: 5, price: 3000,
  status: "OPEN", segment_id: 1, token: "11536",
  order_type: "", product_type: "",       // Choice named these differently
}]);
renderOrderBook(state.book);
const partialRow = [...document.querySelectorAll("#book-body tr")]
  .find(r => r.textContent.indexOf("TCS") > -1);
T("an order missing its type offers no Amend button",
  !!partialRow && partialRow.textContent.indexOf("Amend unavailable") > -1,
  partialRow ? partialRow.textContent.trim().slice(0, 80) : "row not found");

calls = [];
openModify("103");
await doModifyOrder({ preventDefault: () => {} });
T("and amending it directly reaches no endpoint either",
  calls.filter(c => c.path === "/orders/modify").length === 0,
  calls.map(c => c.path).join(" | "));

state.book = state.book.filter(o => o.order_id !== "103");
renderOrderBook(state.book);

// -- amending --------------------------------------------------------------
openModify("101");
T("the amend dialog opens on the right order",
  document.getElementById("m-qty").value === "10",
  document.getElementById("m-qty").value);
T("a limit order shows its price field",
  document.getElementById("m-price-field").style.display !== "none");
T("and hides the trigger it does not use",
  document.getElementById("m-trigger-field").style.display === "none");

calls = [];
document.getElementById("m-qty").value = "20";
document.getElementById("m-price").value = "2450";
await doModifyOrder({ preventDefault: () => {} });
const amend = calls.find(c => c.path === "/orders/modify");
T("amending posts to the modify endpoint", !!amend, calls.map(c => c.path).join(","));
T("and carries all three identifiers Choice needs to find the order",
  !!amend && amend.body.client_order_no === 101 &&
  amend.body.exchange_order_no === "E101" && amend.body.gateway_order_no === "G101",
  amend ? JSON.stringify(amend.body) : "");
T("and sends the new quantity and price in paisa",
  !!amend && amend.body.quantity === 20 && amend.body.price_in_paisa === 245000,
  amend ? amend.body.quantity + " @ " + amend.body.price_in_paisa : "");
T("and echoes the untouched fields back unchanged",
  !!amend && amend.body.order_type === "RL_LIMIT" && amend.body.product_type === "CNC" &&
  amend.body.side === "BUY" && amend.body.token === "2885",
  amend ? JSON.stringify(amend.body) : "");

// -- cancelling ------------------------------------------------------------
calls = [];
await cancelOrder("101");
const killed = calls.find(c => c.path.indexOf("/cancel") > -1);
T("cancelling posts to the cancel endpoint for that order",
  !!killed && killed.path === "/orders/101/cancel" && killed.method === "POST",
  calls.map(c => c.method + " " + c.path).join(","));

// -- converting a position -------------------------------------------------
T("an intraday position is offered delivery",
  convertAction({ product_type: "MIS" }, 0).indexOf("to CNC") > -1);
T("a delivery position is offered intraday",
  convertAction({ product_type: "CNC" }, 0).indexOf("to MIS") > -1);
T("a position with no product offers no conversion",
  convertAction({}, 0).indexOf("button") === -1,
  convertAction({}, 0));
T("and neither does one Choice cannot convert",
  convertAction({ product_type: "NRML" }, 0).indexOf("button") === -1);

calls = [];
state.positions = [{ symbol: "RELIANCE", quantity: 10, token: "2885",
                     segment_id: 1, product_type: "MIS", client_order_no: "55" }];
await convertPosition(0, "CNC");
const conv = calls.find(c => c.path === "/portfolio/convert");
T("converting posts both the source and the target product",
  !!conv && conv.body.source_product_type === "MIS" && conv.body.product_type === "CNC",
  conv ? JSON.stringify(conv.body) : calls.map(c => c.path).join(","));

// -- moving money ----------------------------------------------------------
//
// The live profile endpoint returns no bank accounts at all, so the number is
// typed rather than picked. A blank one must still never reach the API — there
// is nothing to send the money to.
state.banks = [];
openFunds();
calls = [];
document.getElementById("f-bank").value = "";
document.getElementById("f-amount").value = "5000";
await doMoveFunds();
T("a withdrawal with no account number is refused before any request",
  calls.length === 0, calls.map(c => c.path).join(" | "));

calls = [];
document.getElementById("f-amount").value = "0";
document.getElementById("f-bank").value = "12345678";
await doMoveFunds();
T("an amount of zero is refused before any request",
  calls.length === 0, calls.map(c => c.path).join(" | "));

document.getElementById("f-bank").value = "12345678";
openFunds();
document.getElementById("f-dir").value = "withdraw";
fundsDirectionChanged();
T("withdrawing hides the payment method",
  document.getElementById("f-method-field").style.display === "none");
T("and warns that the money leaves the account",
  document.getElementById("funds-note").textContent.indexOf("cannot be undone") > -1,
  document.getElementById("funds-note").textContent);

calls = [];
document.getElementById("f-amount").value = "5000";
await doMoveFunds();
const cash = calls.find(c => c.path === "/portfolio/funds/withdraw");
T("a withdrawal posts the amount and the registered account",
  !!cash && cash.body.amount === 5000 && cash.body.bank_acc_no === "12345678",
  cash ? JSON.stringify(cash.body) : calls.map(c => c.path).join(","));
T("and confirms explicitly, because the server refuses without it",
  !!cash && cash.body.confirm === true);

document.getElementById("f-dir").value = "add";
document.getElementById("f-method").value = "netbanking";
fundsDirectionChanged();
T("a net banking deposit asks for an IFSC",
  document.getElementById("f-ifsc-field").style.display !== "none");
T("and a deposit says nothing is debited here",
  document.getElementById("funds-note").textContent.indexOf("Nothing is debited") > -1,
  document.getElementById("funds-note").textContent);

// -- reading the profile ---------------------------------------------------
T("bank accounts are read whatever key Choice used",
  bankAccounts({ BankAccounts: [{ BankAccNo: "111", BankName: "HDFC" }] }).length === 1 &&
  bankAccounts({ BankDetails: [{ AccountNo: "222" }] }).length === 1 &&
  bankAccounts({ Banks: ["333"] }).length === 1);
T("and an account with none reported yields none, not a placeholder",
  bankAccounts({}).length === 0);

// -- handlers built from data must survive an apostrophe -------------------
//
// `esc()` escapes for HTML, and the HTML parser decodes an attribute *before*
// the handler is compiled as JavaScript — so an escaped quote came back as a
// live one and closed the string. A strategy named "Kunal's momentum" broke
// every button on its row silently, and a name chosen deliberately could run
// whatever it liked. Strategy names are free user input, so this was reachable.
window.api = async () => ([
  { id: "s1", name: "Kunal's momentum", dsl_json: {}, is_active: true },
  { id: "s2", name: "x'),window.__pwned=1,('", dsl_json: {}, is_active: true },
]);
await loadStrategies();
const handlers = [...document.querySelectorAll("#strat-body button")]
  .filter(b => b.getAttribute("onclick"));
T("a strategy list renders its row handlers", handlers.length > 0,
  String(handlers.length));

let broken = 0, firstBroken = "";
for (const b of handlers) {
  try { new Function(b.getAttribute("onclick")); }
  catch (e) { broken++; if (!firstBroken) firstBroken = b.getAttribute("onclick"); }
}
T("every row handler still compiles when the name contains a quote",
  broken === 0, broken + " broken, e.g. " + firstBroken);
T("and a name crafted to break out does not execute",
  window.__pwned === undefined, String(window.__pwned));

window.confirm = realConfirm;
document.title = JSON.stringify(R);
"""


GUARD = """
(async () => {
  try { %s } catch (e) {
    document.title = JSON.stringify([["the harness ran to completion", false,
      (e && e.message ? e.message : String(e))]]);
  }
})();
"""


def run_page(page: Path, profile: Path, width: int = 1440, height: int = 900) -> str:
    return subprocess.run(
        [EDGE, "--headless", "--disable-gpu", "--dump-dom",
         f"--window-size={width},{height}",
         "--virtual-time-budget=4000", f"--user-data-dir={profile}",
         page.as_uri()],
        # Edge writes UTF-8; the console codepage would mangle every rupee sign
        # and arrow in the reported detail.
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    ).stdout


def report(label: str, dumped: str, quiet_when_clean: bool = False) -> list:
    found = re.search(r"<title>(\[.*?\])</title>", dumped, re.S)
    if not found:
        print(f"\n{label}: nothing was reported — the code under test threw.")
        print(dumped[:1500])
        return [[label, False, "no report"]]

    import html
    import json
    results = json.loads(html.unescape(found.group(1)))
    failures = [r for r in results if not r[1]]

    # The layout sweep is five views at five widths. Printing a hundred passing
    # lines buries the handful that matter, so a clean sweep says so in one
    # line and anything failing is printed in full.
    if quiet_when_clean and not failures:
        print(f"  [ok  ] {label} — {len(results)} checks across every view")
        return results

    print(f"\n--- {label} ---")
    for name, ok, detail in results:
        if quiet_when_clean and ok:
            continue
        # Details lifted out of the DOM carry the markup's line breaks and
        # indentation; one check per line is the only thing that stays readable.
        flat = " ".join(str(detail).split())[:110]
        print(f"  [{'ok  ' if ok else 'FAIL'}] {name}" + (f"  - {flat}" if flat else ""))
    return results


def main() -> int:
    if not Path(EDGE).is_file():
        print(f"Edge not found at {EDGE}")
        return 2

    source = INDEX.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", source, re.S)
    if not blocks:
        print("No inline <script> found in index.html")
        return 2
    engine = blocks[0]
    if "function histogram" not in engine:
        print("The first script block is no longer the chart engine.")
        return 2

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        profile = Path(tmp) / "profile"

        harness = Path(tmp) / "harness.html"
        harness.write_text(
            f"<!doctype html><meta charset='utf-8'><body>"
            f"<script>{engine}</script><script>{CASES}</script>",
            encoding="utf-8",
        )
        results += report("the chart engine", run_page(harness, profile))

        # The real page, with the test appended so it runs after the page's own
        # scripts have defined everything.
        full = Path(tmp) / "index.html"
        full.write_text(source + f"<script>{GUARD % PAGE_CASES}</script>",
                        encoding="utf-8")
        results += report("the page's render functions", run_page(full, profile))

        order_page = Path(tmp) / "order.html"
        order_page.write_text(source + f"<script>{GUARD % ORDER_CASES}</script>",
                              encoding="utf-8")
        results += report("the order ticket", run_page(order_page, profile))

        action_page = Path(tmp) / "actions.html"
        action_page.write_text(source + f"<script>{GUARD % ACTION_CASES}</script>",
                               encoding="utf-8")
        results += report("broker actions", run_page(action_page, profile))

        builder_page = Path(tmp) / "builder.html"
        builder_page.write_text(source + f"<script>{GUARD % BUILDER_CASES}</script>",
                                encoding="utf-8")
        dumped = run_page(builder_page, profile)
        results += report("the builder and the live page", dumped)
        results += check_templates_against_the_engine(dumped)

        # Every view at the widths the app gets opened at: a narrow window, a
        # small laptop, either side of the 1180px breakpoint, and a wide
        # monitor.
        #
        # 492px is the floor, not a choice — headless Edge clamps the window
        # there whatever --window-size says, in both headless modes. Anything
        # narrower is untested rather than passing. The app serves on
        # 127.0.0.1 from a desktop binary, so a phone-width viewport is not a
        # real case; a narrow desktop window is, and that is covered.
        layout_page = Path(tmp) / "layout.html"
        layout_page.write_text(source + f"<script>{GUARD % LAYOUT_CASES}</script>",
                               encoding="utf-8")
        for width in (492, 768, 1180, 1440, 1920):
            results += report(f"layout at {width}px",
                              run_page(layout_page, profile, width=width),
                              quiet_when_clean=True)

    results += check_harnesses_parse()
    results += check_for_duplicate_names(source)

    failures = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failures)}/{len(results)} passed")
    return 1 if failures else 0


def check_templates_against_the_engine(dumped: str) -> list:
    """Run each starter template through the engine's own validator.

    The builder promising a template is valid means nothing; the validator that
    runs on save is the one whose opinion counts.
    """
    import html
    import json

    found = re.search(r'<div id="payload">(.*?)</div>', dumped, re.S)
    if not found:
        return [["the builder reported its compiled templates", False, "no payload"]]

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from engine.app.strategy_engine.dsl import DSLError, dsl_engine

    out = []
    print("\n--- starter templates, against the engine's validator ---")
    for entry in json.loads(html.unescape(found.group(1)))["templates"]:
        try:
            dsl_engine.validate(entry["dsl"])
            ok, detail = True, ""
        except DSLError as exc:
            ok, detail = False, str(exc)
        print(f"  [{'ok  ' if ok else 'FAIL'}] \"{entry['name']}\" is a strategy the engine accepts"
              + (f"  - {detail}" if detail else ""))
        out.append([f"template {entry['name']} validates", ok, detail])
    return out


def check_harnesses_parse() -> list:
    """Parse each harness before the browser sees it.

    These are JavaScript inside Python strings, so an escape can collapse
    between the two: a regex written for JS becomes a literal tab and newline
    if over-escaped, which is an invalid regex literal. The browser reports
    that as a page whose script never ran -- "nothing was reported" -- with no
    indication of where.

    Checked against the *interpolated* string, not the source text that
    produces it. Reading the file text is exactly what made an earlier attempt
    at this check report OK while the shipped harness was unparseable.
    """
    import tempfile

    out = []
    print()
    print("--- harnesses ---")
    for name in ("CASES", "PAGE_CASES", "ORDER_CASES", "ACTION_CASES",
                 "BUILDER_CASES", "LAYOUT_CASES"):
        body = GUARD % globals()[name]
        js = Path(tempfile.gettempdir()) / ("harness_" + name + ".js")
        js.write_text(body, encoding="utf-8")
        result = subprocess.run(["node", "--check", str(js)],
                                capture_output=True, text=True)
        ok = result.returncode == 0
        noise = (result.stdout + result.stderr).strip().splitlines()
        detail = "" if ok else (noise[-3][:90] if len(noise) >= 3 else "parse failed")
        print(f"  [{'ok  ' if ok else 'FAIL'}] {name} parses"
              + (f"  - {detail}" if detail else ""))
        out.append([name + " parses", ok, detail])
    return out


def check_for_duplicate_names(source: str) -> list:
    """A top-level name declared twice — functions and const/let alike.

    Two failure modes, silent in opposite ways. A duplicated `function` is a
    total override with no warning: `emptyState` was declared twice with
    different signatures, the later one won, and every chart's empty branch
    stopped doing anything at all. A duplicated `const` is the reverse — it
    throws a SyntaxError that kills the *entire* script block, so the page
    loads with no behaviour at all. `WORKING_STATUSES` did exactly that, and
    this scan covered only functions at the time, so it reported clean.
    """
    blocks = re.findall(r"<script>(.*?)</script>", source, re.S)
    joined = "\n".join(blocks)
    results = []

    for label, pattern in (
        ("function", r"^function\s+([A-Za-z_$][\w$]*)"),
        ("const or let", r"^(?:const|let)\s+([A-Za-z_$][\w$]*)\s*="),
    ):
        seen: dict = {}
        for name in re.findall(pattern, joined, re.M):
            seen[name] = seen.get(name, 0) + 1
        dupes = sorted(n for n, c in seen.items() if c > 1)
        results.append([f"no top-level {label} is declared twice",
                        not dupes, ", ".join(dupes)])

    print("\n--- shadowing ---")
    for name, ok, detail in results:
        print(f"  [{'ok  ' if ok else 'FAIL'}] {name}"
              + (f"  - {detail}" if detail else ""))
    return results


if __name__ == "__main__":
    sys.exit(main())
