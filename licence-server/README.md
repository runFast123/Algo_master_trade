# Licence service

Answers two questions and nothing else:

* may this installation run?
* which installations exist, and when was each last seen?

It holds no credentials, sees no market data, and never touches an order.

## What it stores

A licence per client, and one row per installation: a random id the app
generates for itself, the app version, the Choice environment, and timestamps.

**Not stored:** hostname, username, positions, P&L, strategies, broker
credentials. Not squeamishness — holding other people's trading data changes
what obligations you are under, and none of it is needed to answer the two
questions above.

## Running it

```bash
pip install -r licence-server/requirements.txt

export LICENCE_ADMIN_TOKEN="a long random string"      # required
export LICENCE_DATABASE_URL="sqlite:///licences.db"     # optional
export LICENCE_GRACE_DAYS=7                             # optional

cd licence-server && uvicorn app.main:app --host 0.0.0.0 --port 8100
```

Put it behind HTTPS. The operator token is a bearer token; over plain HTTP it
is readable by anyone on the path.

Without `LICENCE_ADMIN_TOKEN` the operator endpoints return 503 rather than
running unprotected — an unset secret must fail closed.

## Issuing a licence

Open the service in a browser, enter the operator token, and use **Issue a
licence**. The key is shown once, prominently; send that string to the client.

Optionally set a seat count. Re-activating an installation that already exists
does not consume another seat, so a client reinstalling is not locked out of
their own licence.

## Building a licensed desktop app

Licensing is **off** unless a server is configured, so an existing build keeps
working exactly as before. To produce a licensed release, set the URL in
`client-desktop/app/config.py`:

```python
LICENCE_SERVER_URL: str = "https://licences.example.com"
```

On first launch that build asks for the key, activates, and stores the result in
`%LOCALAPPDATA%\ChoiceFinxTrader\licence.json`.

## What the client experiences

| Situation | What happens |
|---|---|
| Activated, service reachable | Runs. Checks in every 6 hours. |
| Activated, service unreachable | **Keeps running** for `LICENCE_GRACE_DAYS`. |
| Offline longer than the grace | Stops at the next launch, and says why. |
| Licence withdrawn | Stops at the next launch, and says why. |
| No key entered | Asks for one. |

The grace period is the important part. A desktop app that stops the moment a
network hiccups is worse than no licensing at all — someone could be managing a
live position. So an unreachable service and a withdrawn licence are handled
differently, deliberately, and there are tests for both.

Withdrawing a licence takes effect at the next check-in, or within the grace
period if that copy is offline. It is not instant, and cannot be: the app has
to keep working when the network does not.

The running app is never stopped mid-session by a heartbeat. Pulling the floor
out from under someone at 2pm is worse than letting a withdrawn licence run
until the next launch, which is at most a working day away.

**Positions are unaffected by any of this.** They live at the broker, not here,
and the message the user sees says so.

## Endpoints

| Method | Path | Who |
|---|---|---|
| POST | `/api/activate` | the app |
| POST | `/api/heartbeat` | the app |
| POST | `/api/licences` | operator |
| GET | `/api/licences` | operator |
| POST | `/api/licences/{key}/revoke` | operator |
| POST | `/api/licences/{key}/restore` | operator |

## Tests

```bash
python -m pytest licence-server/tests client-desktop/tests -q
```

The refusals are what matter: an unknown key, a revoked one, a seat limit, an
unauthenticated operator call, and the difference between "revoked" and
"unreachable". A licence service that fails open is decoration.
