# Distribution plan — sharing the platform with multiple users

Written 17 August 2026, against the shipped build.

What you asked for:

1. Users can work in **UAT and PROD**, choosing per user
2. Every user connects **their own** Choice credentials — vendor ID, API key,
   mobile — from **their own static IP**
3. **No repeated logins** — the `SECRET_KEY` problem fixed
4. **Multiple users, full access** to every feature
5. **Admin only for you**, no other user
6. **You can see who is using it, and how many**

Points 1–4 are straightforward. Points 5 and 6 have a constraint that has to be
stated before any plan is worth reading.

---

## The constraint: a local database cannot keep you in sole control

The desktop build gives every user their own SQLite database at
`%LOCALAPPDATA%\ChoiceFinxTrader\algo.db`. Two consequences follow, and neither
is a bug to fix:

**You cannot see who is using it.** `/api/v1/admin/stats` counts rows in the
database it is running against. On a user's machine that is *their* database —
it will report one tenant and one user, forever. There is no central anything
to ask.

**You cannot keep admin away from them.** A user owns their machine, so they own
that SQLite file. One `UPDATE users SET role='admin'` and they are an admin of
their own install. That is exactly how `test@gmail.com` was promoted earlier in
this project. No amount of application code prevents it, because the check runs
on hardware the other person controls.

So "admin only for me, and I can see the users" requires **something central**.
There is no version of that which lives entirely in a file you hand over.

The good news is that it does *not* require moving the trading itself to a
server — which matters, because trading from a server would break the very
thing you want in point 2.

---

## The architecture that satisfies all six

Split what is local from what is central, along the line that regulation
already draws:

| | Runs where | Why |
|---|---|---|
| Trading, strategies, backtests, the user's data | **The user's machine** | Their credentials, their static IP, their broker relationship |
| Licensing, user registry, usage, remote disable | **Your server** | The only place you control |

Each user is a Choice client trading their own account from their own declared
IP. You are not routing anyone's orders. The desktop app calls home only to say
"this licence is alive", never to trade.

---

## Phase 1 — DONE, 17 August 2026

Items 1.1, 1.2 and 1.3 are built. 1.4 turned out to be unnecessary and 1.5 is a
purchase, not code — both explained below.

**1.1 Signing key persisted.** Generated once into
`%LOCALAPPDATA%\ChoiceFinxTrader\secret.key` and reused. A configured
`SECRET_KEY` still wins, so a server deployment is unaffected, and
`APP_ENV=production` still refuses to invent one — several server processes
each generating their own would reject each other's tokens. If the directory
cannot be written the app still starts, and says why sign-in will not persist.

**1.2 Choice server chosen per connection.** `environment` is now part of the
connect request, validated against the known servers, stored on the session and
recorded in the audit trail. The connect dialog offers Production or UAT,
defaults to Production, and remembers the choice.

One bug fell out of this: `describe()` reported the *deployment's*
`CHOICE_ENV`, not the session's. With a per-connection server that would have
told a user they were on UAT while their orders went to Production. Now
reported from the session.

**1.3 Admin page removed from the desktop bundle.** It read the user's own local
database, so it showed them nothing about anyone else while handing them a
control surface. Build with `CHOICE_BUNDLE_ADMIN=1` for a server deployment,
where the database is central and you own the host.

**1.4 First-run setup — not needed, and not built.** Its two jobs were
generating a `SECRET_KEY` (now automatic, invisible) and choosing an
environment (now in the connect dialog, where the credentials it applies to are
also entered). A separate screen would have asked for the same thing twice.

**1.5 Code signing — a purchase.** `build_exe.py` prints the `signtool`
command; it needs a certificate from a CA.

### What a client now receives

One `.exe`. They register in the app, pick Production, enter their own Choice
credentials, and stay signed in across restarts. No `.env`, no configuration,
nothing to paste.

### Verified

Three mutations detected: the key generated per process again, the environment
dropped on the way to the broker login, and `describe()` reporting the install
default.

**Two of these checks were vacuous when first written.** The connect test used
sandbox mode, which never reaches `login_totp` and returns 200 regardless; and
nothing made the session's environment differ from the deployment's, so
reporting either passed. Both now assert at the call site.

---

## Phase 1 — original plan

Small, self-contained, no server needed. Do this first; it is useful even if
Phase 2 never happens.

### 1.1 Persist the signing key (fixes the repeated logins)

`SECRET_KEY` is unset on a fresh install, so `backend/app/config.py` generates a
random one **per process** and logs "Sessions will not survive a restart". Every
restart invalidates every token, and the user signs in again.

Fix it the same way `session.key` is already handled: generate once, store
beside the database, reuse. No `.env` editing, nothing for the user to do.

- `%LOCALAPPDATA%\ChoiceFinxTrader\secret.key`, created on first launch
- Keep `SECRET_KEY` from the environment winning when it is set, so a server
  deployment is unaffected

### 1.2 Choose the Choice environment per connection

Today `CHOICE_ENV` is one global setting in a `.env` file, so a user is in UAT
or PROD for everything, and switching means editing a file. Move it to the
connect dialog, beside the mode selector, and store it on the session.

- The connect request carries `environment: "UAT" | "PROD"`
- `ChoiceSession` resolves its base URL from that, falling back to the
  configured default
- The mode chip shows which environment the session is on, because "am I on the
  sandbox?" must never be a guess

This also removes the single most common setup failure: a production Client ID
rejected because the app defaulted to UAT.

### 1.3 Take the admin pages out of the desktop build

`frontend-admin/index.html` is copied into the bundle and served at `/admin`. On
a user's machine it shows their own data and is a control surface you do not
want to hand out. Stop bundling it. It stays available for the server
deployment, where it belongs.

### 1.4 A first-run setup screen

One screen on first launch: environment, and a note that Choice credentials are
entered when connecting. Everything else is generated. The user receives one
`.exe` and nothing else.

### 1.5 Sign the binary

Unsigned, every user gets a SmartScreen warning and some will not proceed.
`build_exe.py` already prints the `signtool` command; it needs a code-signing
certificate (roughly ₹15–40k/year from a CA).

**After Phase 1:** you can hand out the `.exe`. Users have full access, their own
credentials, their own IP, and they stay logged in. You still cannot see them.

---

## Phase 2 — DONE, 17 August 2026

Built as `licence-server/`, plus `client-desktop/app/licence.py`. See
`licence-server/README.md` for running it.

**Off by default.** With no licence server configured the app behaves exactly as
before — a build already handed to someone must not become a brick the day the
service exists. A licensed release sets `LICENCE_SERVER_URL` at build time.

### The whole loop, run against a live service

```
1. issued          : CFX-013BAE-A0A928-DD9428
2. activated for   : Acme Capital
3. app may run     : active
4. owner sees      : 1 install, version 1.1.0, PROD
5. after revoke    : revoked
6. user is told    : This licence has been withdrawn. Contact the platform oper...
7. after restore   : active
```

### What the client experiences

| Situation | What happens |
|---|---|
| Activated, service reachable | Runs; checks in every 6 hours |
| Activated, service unreachable | **Keeps running** for the grace period (7 days) |
| Offline past the grace | Stops at next launch, and says why |
| Licence withdrawn | Stops at next launch, and says why |
| No key entered | Asks for one |

**The grace period is the design, not a concession.** An app that stops the
moment a network hiccups is worse than no licensing — someone could be managing
a live position. So "the service said no" and "the service could not be
reached" are handled differently and tested separately.

A running app is never stopped mid-session by a heartbeat either. Pulling the
floor out at 2pm is worse than a withdrawn licence running until the next
launch, which is at most a working day away. The message the user sees says
explicitly that positions live at the broker and are unaffected.

### What it stores, and does not

Per installation: a random id the app generates for itself, version,
environment, timestamps, and an optional label someone typed. **No hostname, no
username, no positions, no P&L, no strategies, no credentials.** A test asserts
the column list, so widening it is a deliberate act rather than a drift.

### Seats

Optional per licence. Re-activating an installation that already exists does not
consume another seat, so a client reinstalling is not locked out of their own
licence.

### Verified

Five mutations, all detected: a revoked licence still activating, the seat limit
disabled, the operator token not required, the offline grace ignored, and an
unreachable service treated as a revocation.

The operator endpoints fail **closed** when no token is configured — 503 rather
than an open registry.

---

## Phase 2 — original plan

This is the part that needs a server. It is small — it does no trading, holds no
credentials, and touches no market data.

### 2.1 A licence service

A minimal FastAPI app on a host you control:

- `POST /licences` — you issue a key for a client
- `POST /activate` — the desktop app sends its key plus an install fingerprint
  on first run; the service records it and returns a signed token
- `POST /heartbeat` — the app reports every few hours: version, environment,
  last-seen. **No positions, no P&L, no credentials.**
- `GET /admin/installs` — your dashboard

The desktop app refuses to start without an activated licence, and stops after
a grace period (say 7 days offline) if the key is revoked. The grace period
matters: a user's connection failing must not stop them managing a live
position.

### 2.2 What you would see

One row per install: client name, licence key, version, environment, first seen,
last seen, active runs count. That answers "who is using it and how many" —
which is exactly what the local admin page cannot.

### 2.3 What this deliberately does not collect

Nothing about their trading. Not positions, not P&L, not strategies, not
credentials. Partly because it is not yours, and partly because holding other
people's trading data changes what obligations you are under.

**Effort:** the service is a day. The client-side activation and heartbeat is
another day. The dashboard reuses the admin UI patterns already here.

---

## Phase 3 — the regulatory questions

Not code. Put these to Choice in writing before anyone routes a live order.

Your own `/api/v1/admin/compliance` endpoint already names them:

**Static IP (OPEN-1).** The checklist currently reports this as *failing*
because a desktop build places orders from each user's machine. Your model
changes that: each user is a Choice client, using their own vendor ID and API
key, from their own IP declared with Choice. That is a materially different
position from one vendor routing many clients' orders. **Confirm with Choice
that a per-user declared IP satisfies them** — and update the checklist wording
once you have the answer, because it currently describes the other model.

**Empanelment (EMPANEL).** "Required for a platform serving multiple Choice
clients." Whether distributing software to Choice clients — who each use their
own credentials — counts as serving them is the question to ask. It is not
answerable from the code.

**UAT certification (OPEN-3).** Confirm in writing before live routing.

---

## Order of work

| | What | Effort | Unblocks |
|---|---|---|---|
| 1 | Persist the signing key | ~1 hour | Users stop re-logging in |
| 2 | Environment per connection | ~2 hours | UAT and PROD, per user |
| 3 | Drop admin from the bundle | ~15 min | No stray control surface |
| 4 | First-run setup | ~1 hour | One file to hand over |
| 5 | Code signing | procurement | No SmartScreen warning |
| — | *Ship to first users for paper trading* | | |
| 6 | Licence service | ~1 day | You issue and revoke access |
| 7 | Activation + heartbeat | ~1 day | You see installs |
| 8 | Owner dashboard | ~half day | Who and how many |
| 9 | Choice: IP, empanelment, UAT sign-off | your call | Live routing |

Items 1–4 are worth doing regardless. Items 6–8 only matter if you want control
and visibility, which you said you do.

---

## One thing worth deciding early

If you ever want to *stop* a specific user, that has to be designed in from the
start — a licence check added later cannot reach installs already in the field.
Phase 2's activation is what makes revocation possible. Handing out unlicensed
builds first and adding licensing later means the first cohort keeps working
forever.
