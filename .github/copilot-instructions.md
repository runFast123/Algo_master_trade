# Copilot Instructions — Choice FINX Algo Platform

Always adhere to the core architectural invariants, safety rules, and release protocols defined in [AGENTS.md](../AGENTS.md).

### Key Directives for AI Agents:
1. **Never break the 4-state `SessionMode` invariants** (`DISCONNECTED`, `DEMO`, `PAPER`, `LIVE`). Live money orders must only ever leave the process when `SessionMode == LIVE`.
2. **Never mock paper trading fills for connected accounts**. Authentic live touchline or holdings quotes must be used.
3. **Never auto-retry non-idempotent HTTP methods (`POST`, `PUT`, `DELETE`)**.
4. **Whenever making major updates or changes**, follow the mandatory release protocol in `AGENTS.md` (bump versions, run `pytest`, `verify_ui.py`, `verify_exe.py`, push git tag `vX.Y.Z`, and create a GitHub Release on `runFast123/Algo_master_trade` so all client machines auto-update).
5. **Always preserve thread safety** using `book_lock` for position/P&L calculations.
