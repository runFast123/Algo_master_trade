# Choice FINX Documentation Hub

Central index for the **Choice FINX Algo Trading Platform**. The four PDFs under
[`pdf/`](pdf/) are the authority; everything below summarises them. Where this
page and a PDF disagree, the PDF wins.

---

## 1. Official PDF reference guides (`docs/pdf/`)

| Document | Covers | Key points |
| :--- | :--- | :--- |
| [Choice OpenAPI Integration Guide](pdf/Choice_OpenAPI_Integration_Guide.pdf) | Onboarding, authentication modes, regulatory obligations | UAT and production environments, static-IP mandate, vendor empanelment, 10 orders/sec cap, credential handling, 5-year log retention |
| [FINX Interactive Socket API Reference](pdf/FINX_Interactive_Socket_API_Reference.pdf) | Order and trade push stream | `wss://finxsocket.choiceindia.com/ws/?token=<JWT>`, RS256 JWT from the logon response, heartbeat, `MKT_STAT` / `ORD_NRML` / `TRD_MSG` |
| [Live Data Feed Specification](pdf/Live_Data_Feed_Specification.pdf) | Level 1 / Level 2 market data | Feed address from the logon response, zlib framing, FIX3.0 pipe-delimited tags |
| [Partner Product Integration Guide](pdf/Partner_Product_Integration_Guide.pdf) | OAuth 2.0 partner flow | Redirect URL, callback parameters, **AES encryption of callback values** |

---

## 2. Environments

Per Integration Guide §10. The platform selects between them with `CHOICE_ENV`
and defaults to UAT.

| Environment | Base URL | Purpose |
| :--- | :--- | :--- |
| UAT | `https://uat.jiffy.in` | Integration testing. No real orders, no real money. |
| Production | `https://finxomne.choiceindia.com` | Live trading. Real orders, real funds. |

Production credentials are issued only after UAT certification by the Choice
Open API team and, for vendors, proof of exchange empanelment (§5.1).

---

## 3. Regulatory obligations

These are requirements, not recommendations. Integration Guide §2, §8, §9, §12.

| Requirement | Applies to | Detail |
| :--- | :--- | :--- |
| **Static IP** | Everyone | All API orders must originate from a declared static IP. Orders from any other address are rejected. Dynamic IPs, VPNs and proxies are not supported. |
| **Vendor empanelment** | Platforms serving multiple Choice clients (Type B) | Mandatory NSE/BSE/MCX empanelment before production access; server co-location at Choice on full empanelment. |
| **Strategy registration** | Everyone | Required for any strategy at or above 10 orders/second. Below that, no registration is needed. |
| **Credential handling** | Everyone | Never hard-code or commit keys. Use environment variables or a secrets manager. Rotate at least quarterly. |
| **Audit logging** | Everyone | Log every API call with timestamp, request id and response code. Retain **5 years**. |
| **Error handling** | Everyone | Treat every 4xx/5xx as actionable. Use exponential back-off. Do not retry indefinitely. |

> A desktop application that places orders from each end user's own machine
> cannot satisfy the static-IP mandate. Order flow has to leave from a server
> whose address is declared with Choice.

---

## 4. Authentication

### Mode 1 — Direct TOTP 2FA

Three calls, wrapped by `ChoiceClient.login()`:

1. `POST /api/OpenAPIV1/LoginTOTP` — base64-encoded mobile number
2. `POST /api/OpenAPIV1/GetClientLoginTOTP` — retrieves the OTP
3. `POST /api/OpenAPIV1/ValidateTOTP` — returns `SessionId` and `AccessToken`

Platform endpoint: `POST /api/v1/auth/choice/connect` (requires a signed-in
platform user; the broker session is attached to that user only).

**Credentials on the wire.** Every request — including the three unauthenticated
login calls — carries two headers built by `ChoiceClient.get_headers()`:

```
VendorId: <Client ID>
Bearer:   <API key>
```

The `SessionId` obtained from step 3 is added as `Authorization` only on calls
made after login.

**The API key expires.** Observed lifetime is about a day, and Choice reports
it distinctly from an unknown Client ID — a difference worth reading carefully,
because the two need different fixes:

| Response | Meaning |
| :--- | :--- |
| `Unauthorized, Token Expired` | Client ID valid, API key lapsed → reissue the key in the portal |
| `Unauthorized, VendorId Invalid or doesn't exists` | Client ID unknown in this environment → check the ID and whether it was issued for UAT or PROD |

### Mode 2 — Partner OAuth

Redirect the user to
`https://partner.choiceindia.com/auth/login?redirectUrl=<callback>`.
On success Choice redirects back with:

| Parameter | Meaning | Encrypted? |
| :--- | :--- | :--- |
| `cid` | Client id | **Yes — AES** |
| `sid` | Session id, used to authenticate subsequent API calls | **Yes — AES** |
| `accessToken` | 2FA token for the interactive socket | **Yes — AES** |
| `baseUrl` | API base URL, e.g. `https://finx.choiceindia.com/` | No — plain text |

> **All values except `baseUrl` arrive AES-encrypted under a vendor-specific
> key** issued by the Choice IT team once integration begins (Partner guide §6).
> A callback handler that reads them as plain text will neither work against
> real Choice nor be safe. This platform keeps the flow disabled until
> `CHOICE_OAUTH_AES_KEY` is set.

The vendor must supply the callback URL in advance, plus any dev/UAT/live
environments.

Platform endpoints: `POST /api/v1/auth/choice/oauth/start` issues a single-use
`state` bound to the signed-in user; `GET /api/v1/auth/choice/oauth/callback`
verifies that state before decrypting anything.

### Mode 3 — Sandbox

Enter `DEMO` as the Client ID. Creates a local sandbox session with ₹2,50,000
of simulated margin and sample holdings. Sandbox orders are simulated in
process and are never sent to Choice.

---

## 5. Interactive socket (order and trade updates)

* **URL** `wss://finxsocket.choiceindia.com/ws/?token=<JWT>`
* **Token** RS256-signed, taken from the logon response — it is *not* generated
  locally. Claims: `iat`, `nbf`, `exp` (about 8 hours), `UserId`, `DeviceId`,
  `SessionId`, `iss` (always `FINX`).
* **Heartbeat** The server closes the connection after **30 seconds** without
  one and checks every second, so send `"2"` comfortably inside that window —
  every 20–25 seconds, not every 30. The server replies `"3"`.
* **Connection errors** 401 expired or invalid token, 429 too many connections,
  500 server error.

### `ORD_NRML` status codes

| Code | Meaning |
| :--- | :--- |
| 1 | New / Placed |
| 2 | Partially traded |
| 3 | Fully traded |
| 4 | Cancelled |
| **5** | **Open / pending — the resting state of a working order** |
| 6 | Rejected |
| **7** | **Modified** |

### `MKT_STAT` event types

1 Normal open · 2 Normal close · 3 Pre-open · 4 Pre-open close · 5 Auction open ·
6 Auction close · 7 Auction open (secondary) · 8 Post-trade open ·
9 Post-trade close · 10 Special open · 11 Special close

---

## 6. Price feed socket (live quotes)

**The feed address is not a fixed URL.** The logon response carries
`OdinBcastIP` and `OdinBcastPort`, and those are what the client connects to.
Only fall back to a vendor default when the logon response omits them.

### Framing

Each packet is: **1 marker byte** — `5` compressed, `2` uncompressed — followed
by a **5-byte ASCII length**, followed by the zlib-compressed body. A
decompressed body may hold several sub-messages, split on `0x02` (STX) and
truncated at the first `0x00` (NUL).

Earlier revisions of this page described a "5-byte length prefix". That
merged two separate fields and would desynchronise a parser by one byte
per packet.

```
[1 byte: 5 or 2][5 bytes: ASCII length][zlib body]
```

### Message codes

| Code | Message |
| :--- | :--- |
| 101 | Login request |
| 102 | Login response (`70=10000` success, `10004` expiring within 15 days) |
| 127 | Best five request |
| 128 | Best five information |
| 206 | Touchline request |
| 209 | Touchline information |

### Message format

FIX-style `tag=value` pairs separated by `|`. Header tags: `63` version
(`FIX3.0`), `64` message code, `65` message length, `66` message time.

**Logon (101)** — `65` (length) and `401` (auth type) are both required, and
`67` is the **User Id**, not the vendor id:

```
63=FIX3.0|64=101|65=66|66=2026-05-04 133022|400=12|67=USER12|401=2|68=<token>|
```

`400`: 11 = web, 12 = mobile app. `401`: 1 = password, 2 = broadcast access token.

**Touchline request (206)** — multiple instruments in one message:

```
63=FIX3.0|64=206|65=107|66=2026-02-11 190231|1=1$7=2885|1=1$7=1594|230=1|4=<session_id>|
```

`230`: 1 = subscribe, 2 = unsubscribe. `4` is the session id from login.

### Segment ids (Annexure A)

| Id | Segment | Id | Segment |
| :-- | :--- | :-- | :--- |
| 1 | NSE Cash | 7 | NCDEX Derivatives |
| 2 | NSE Derivatives | 8 | NCDEX Spot |
| 3 | BSE Cash | 13 | NSECDS Derivatives |
| 5 | MCX Derivatives | 14 | NSE CDS Spot |
| 6 | MCX Spot | | |

Indices such as NIFTY 50 (token 26000) and BANKNIFTY (26009) trade in the
**cash** segment, id 1.

---

## 7. Platform architecture and review

* [Architecture](ARCHITECTURE.md) — component layout, data model, and which parts
* **[ROADMAP.md](ROADMAP.md)** — what to build next, ordered by value
* **[next-phase-plan-updated.md](../next-phase-plan-updated.md)** — the current phase, scoped and sequenced
  are built versus planned.
* [Audit report](AUDIT_REPORT.md) — the 12 August 2026 code, logic and security
  review, the fixes applied, and the four items still needing a decision.
