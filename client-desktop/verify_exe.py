"""Verify the frozen binary: run its bundled backend and drive the API.

    python client-desktop/verify_exe.py

Run this after every build. The test suite runs against the *installed*
packages, so it cannot see a module PyInstaller failed to bundle — a Fernet
import that pytest was happy with once shipped a binary that returned 500 on
every broker call. Only driving the executable itself catches that.

Which means: when a check here cannot fail, it is not evidence. Two versions of
the session-store check below passed while the feature was broken, because a
live in-memory session short-circuits the path they meant to exercise. Prefer a
check that observes a side effect over one that only reads a status code.

Uses --run-backend so no browser window is opened on the user's desktop.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

EXE = str(Path(__file__).resolve().parent / "dist" / "ChoiceFinxTrader.exe")
PORT = 8931
BASE = f"http://127.0.0.1:{PORT}"
FAILURES = []


def check(desc, ok, detail=""):
    if not ok:
        FAILURES.append(desc)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {desc}" + (f"  - {detail}" if detail else ""))


def call(path, token=None, data=None):
    req = urllib.request.Request(BASE + path, method="POST" if data is not None else "GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


proc = subprocess.Popen([EXE, "--run-backend", str(PORT)],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

print("starting the frozen backend...")
deadline = time.time() + 180
up = False
while time.time() < deadline:
    if proc.poll() is not None:
        print("the executable exited during startup:")
        print(proc.stdout.read()[:3000])
        sys.exit(1)
    if call("/api/v1/openapi.json")[0]:
        up = True
        break
    time.sleep(2)

if not up:
    print("backend never answered")
    proc.kill()
    sys.exit(1)

print(f"frozen backend answering on {BASE}\n")

print("--- the binary boots and serves ---")
s, _ = call("/docs")
check("API docs respond", s == 200, f"status {s}")

print("\n--- auth and authorisation inside the binary ---")
email = f"exe_verify_{int(time.time())}@t.com"
s, body = call("/api/v1/auth/register", data={
    "email": email, "password": "Passw0rd!secure",
    "full_name": "Verify", "tenant_name": f"VerifyCo{int(time.time())}"})
check("registration works", s in (200, 201), f"status {s}")
token = json.loads(body).get("access_token") if s in (200, 201) else None

s, _ = call("/api/v1/portfolio/holdings")
check("holdings without a token is refused", s in (401, 403), f"status {s}")
s, _ = call("/api/v1/portfolio/holdings", token=token)
check("holdings without a broker session returns 409", s == 409, f"status {s}")
s, _ = call("/api/v1/admin/stats", token=token)
check("admin API refuses a plain trader", s == 403, f"status {s}")

# The session store imports cryptography.fernet, which PyInstaller bundles
# only when it is named explicitly. A missing module there is invisible under
# pytest -- which runs against the installed package -- and surfaces as a 500
# the first time a real user connects. So exercise it in the binary.
print("\n--- the broker session store, as compiled into the binary ---")
s, body = call("/api/v1/auth/choice/status", token=token)
check("choice status responds", s == 200, f"status {s} {body[:90]}")
check("no import error behind it", "Internal Server" not in body, body[:90])

# Importing the module is not the same as running the cipher: constructing a
# Fernet pulls in the hazmat backend, which PyInstaller can miss separately.
# load() only builds one when a record exists *and* no live session is already
# held in memory -- so this has to run before connecting, or the in-memory
# session short-circuits it and the check silently proves nothing.
store = Path(os.environ["LOCALAPPDATA"]) / "ChoiceFinxTrader" / "sessions.json"
original = store.read_text(encoding="utf-8") if store.exists() else None
uid = json.loads(call("/api/v1/auth/me", token=token)[1]).get("id")
try:
    records = json.loads(original) if original else {}
    records[str(uid)] = "gAAAAABmnot-a-real-token"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(records), encoding="utf-8")

    s, body = call("/api/v1/auth/choice/status", token=token)
    check("a corrupt stored session does not crash the binary", s == 200,
          f"status {s} {body[:90]}")
    left = json.loads(store.read_text(encoding="utf-8"))
    gone = str(uid) not in left
    check("the cipher ran and discarded the bad record", gone,
          "" if gone else "record still present - Fernet never reached it")
finally:
    # Never leave the user's own remembered session damaged by a test run.
    if original is not None:
        store.write_text(original, encoding="utf-8")
    elif store.exists():
        store.unlink()

s, body = call("/api/v1/auth/choice/connect", token=token, data={
    "mode": "sandbox", "vendor_id": "DEMO", "api_key": "DEMO",
    "mobile_no": "", "remember": True})
check("connect with remember=true works", s in (200, 201), f"status {s} {body[:90]}")

print("\n--- price scaling, as compiled into the binary ---")
s, _ = call("/api/v1/auth/choice/connect", token=token, data={
    "mode": "sandbox", "vendor_id": "DEMO", "api_key": "DEMO", "mobile_no": ""})
check("sandbox session connects", s in (200, 201), f"status {s}")

s, body = call("/api/v1/portfolio/holdings", token=token)
check("holdings return", s == 200, f"status {s}")
rows = json.loads(body).get("data", []) if s == 200 else []
if rows:
    worst = max((r.get("current_price") or 0) for r in rows)
    check("no holding priced absurdly high", worst < 100000, f"max {worst}")
    check("every holding carries a price",
          all(r.get("current_price") is not None for r in rows))

# The compiled helper itself, imported from the frozen tree, is the real proof.
s, body = call("/api/v1/orders/", token=token, data={
    "segment_id": 1, "token": "2885", "symbol": "RELIANCE", "side": "BUY",
    "order_type": "RL_MKT", "quantity": 1})
check("a sandbox order is simulated, not sent", s in (200, 201), f"status {s}")
if s in (200, 201):
    o = json.loads(body)
    order = o.get("order", o)          # the envelope wraps the order row
    check("the order is marked simulated",
          str(order.get("status", "")).upper() == "SIMULATED",
          str(order.get("status")))
    check("the simulated fill has a sane price",
          0 < (order.get("executed_price") or 0) < 100000,
          str(order.get("executed_price")))

print("")
print("--- the new features, as compiled into the binary ---")
s, body = call("/api/v1/orders/preview", token=token,
               data={"side": "BUY", "quantity": 10, "price": 1314.90})
check("cost preview responds", s == 200, f"status {s}")
if s == 200:
    q = json.loads(body)
    check("charges are itemised", len(q.get("charge_breakdown", {})) == 6)
    check("break-even is above the buy price", q["breakeven_price"] > q["price"])

s, body = call("/api/v1/orders/limits", token=token)
check("risk budget responds", s == 200, f"status {s}")

s, body = call("/api/v1/portfolio/holdings", token=token)
summary = json.loads(body).get("summary") if s == 200 else None
check("holdings carry a summary", bool(summary))
if summary:
    check("day P&L is computed", summary.get("day_pnl") is not None,
          str(summary.get("day_pnl")))
    check("concentration is computed", summary.get("largest_position_pct") is not None)

s, body = call("/api/v1/market/status", token=token)
check("market status has a calendar", s == 200 and "calendar" in body, f"status {s}")

s, body = call("/api/v1/orders/export.csv", token=token)
check("orders CSV exports", s == 200 and "symbol" in body, f"status {s}")
s, body = call("/api/v1/portfolio/holdings/export.csv", token=token)
check("holdings CSV exports", s == 200 and "day_pnl" in body, f"status {s}")

s, body = call("/api/v1/orders/reconcile", token=token)
check("reconciliation responds", s == 200, f"status {s}")

# -- the broker actions that replace the Choice website --------------------
#
# This is a sandbox session, so the read-only routes must answer and every
# money-moving route must refuse. Both halves matter: the first proves the
# route survived into the binary, the second proves a simulated session cannot
# move real cash. A 404 here means the route was never compiled in.

s, body = call("/api/v1/market/profile", token=token)
check("account profile responds", s == 200, f"status {s}")

s, body = call("/api/v1/market/instrument/2885", token=token)
check("instrument details carry a lot size",
      s == 200 and "lot_size" in body, f"status {s}")

s, body = call("/api/v1/market/feed", token=token)
check("price feed status responds", s == 200, f"status {s}")
check("and a sandbox session is not reported as provisioned",
      s == 200 and json.loads(body).get("provisioned") is False)

s, body = call("/api/v1/portfolio/edis", token=token)
check("eDIS status responds", s == 200, f"status {s}")

s, body = call("/api/v1/portfolio/funds/withdraw", token=token,
               data={"amount": 1000, "bank_acc_no": "12345678"})
check("a withdrawal without confirmation is refused", s == 400, f"status {s}")

s, body = call("/api/v1/portfolio/funds/withdraw", token=token,
               data={"amount": 1000, "bank_acc_no": "12345678", "confirm": True})
check("a simulated session cannot withdraw",
      s >= 400 and "simulated" in body.lower(), f"status {s}")

s, body = call("/api/v1/portfolio/funds/add", token=token,
               data={"amount": 1000, "method": "upi", "bank_acc_no": "12345678",
                     "user_vpa": "someone@bank"})
check("a simulated session cannot deposit",
      s >= 400 and "simulated" in body.lower(), f"status {s}")

s, body = call("/api/v1/orders/modify", token=token, data={
    "client_order_no": 1, "exchange_order_no": "E1", "gateway_order_no": "G1",
    "segment_id": 1, "token": "2885", "symbol": "RELIANCE", "side": "BUY",
    "order_type": "RL_LIMIT", "quantity": 1, "price_in_paisa": 250000})
check("a simulated order cannot be amended",
      s >= 400 and "immediately" in body.lower(), f"status {s}")

s, body = call("/api/v1/portfolio/convert", token=token, data={
    "segment_id": 1, "token": 2885, "client_order_no": 1, "side": "BUY",
    "quantity": 1, "product_type": "CNC", "source_product_type": "MIS"})
check("a simulated session has no position to convert",
      s >= 400 and "simulated" in body.lower(), f"status {s}")

# A stop-loss order carries a trigger. The engine has accepted these all along;
# until now nothing could send one, so nothing proved the path worked.
s, body = call("/api/v1/orders/", token=token, data={
    "segment_id": 1, "token": "2885", "symbol": "RELIANCE", "side": "SELL",
    "order_type": "SL_LIMIT", "quantity": 1, "price_in_paisa": 240000,
    "trigger_price_in_paisa": 245000})
check("a stop-loss limit order is accepted", s == 200, f"status {s} {body[:160]}")

# The diagnostic is meant to be handed to someone else to read. Its own hint
# says so. Until now the `normalized` block returned real balances and holdings
# whatever `include_values` said, so following that advice disclosed the whole
# account. Values must stay out unless they are asked for.
s, body = call("/api/v1/diagnostics/choice", token=token)
check("the diagnostic responds", s == 200, f"status {s}")
if s == 200:
    diag = json.loads(body)
    check("and reports that it holds no values", diag.get("includes_values") is False)
    funds = (diag.get("normalized") or {}).get("funds") or {}
    values = (funds.get("data") or {}) if isinstance(funds.get("data"), dict) else {}
    check("and the normalized block carries types, not amounts",
          all(isinstance(v, str) for v in values.values()),
          str(list(values.items())[:3]))

s, body = call("/api/v1/diagnostics/choice?include_values=true", token=token)
if s == 200:
    diag = json.loads(body)
    check("asking for values does return them", diag.get("includes_values") is True)

s, body = call("/api/v1/orders/halt", token=token, data={})
check("kill switch responds", s == 200, f"status {s}")
s, body = call("/api/v1/orders/", token=token, data={
    "segment_id": 1, "token": "2885", "symbol": "RELIANCE", "side": "BUY",
    "order_type": "RL_MKT", "quantity": 1})
check("orders refused after the halt", s >= 400 and "halt" in body.lower(),
      f"status {s}")

# The PyInstaller bootloader spawns a child; terminating the parent leaves it
# holding the binary open, which then blocks the next build.
subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
               capture_output=True)
try:
    proc.wait(timeout=20)
except subprocess.TimeoutExpired:
    proc.kill()

print("\n" + "=" * 58)
print(f"FAILURES: {len(FAILURES)}")
for f in FAILURES:
    print("  -", f)
sys.exit(1 if FAILURES else 0)
