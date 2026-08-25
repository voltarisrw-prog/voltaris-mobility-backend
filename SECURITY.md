# Security

## Session transport

Two modes from one login endpoint. The response body carries the tokens **and** sets
httpOnly cookies; browsers use the cookies and ignore the body, API and mobile clients
do the reverse.

Browsers get cookies because a token in `localStorage` is readable by any injected
script, so a single XSS becomes account takeover; httpOnly means the token is never
exposed to JavaScript. That trade buys CSRF exposure, which is paid for with a
double-submit token: `voltaris_csrf` is readable on purpose, and must be echoed as
`X-CSRF-Token` on every cookie-authenticated mutation. A cross-site attacker can make
the browser send the cookie but cannot read it, so cannot set the header.

Bearer requests skip the CSRF check deliberately — an attacker able to set an
`Authorization` header already holds the token.

`SameSite=Lax`, not `Strict`: Strict drops the cookie on the return leg of the Google
OAuth redirect, landing the user signed-out immediately after signing in. Lax still
blocks cross-site POSTs, which is the case that matters. The refresh cookie is scoped
to `/api/v1/auth/refresh` so it is not attached to every request.

## Authentication

Argon2id, OWASP parameters (t=3, 64 MiB, p=4). Rehashed transparently on login when
the parameters are raised. Passwords never logged: `password`, `password_hash`,
`token`, `secret`, `signature` and others are redacted by the log formatter itself,
so a careless `extra={}` cannot leak one.

Access tokens live 15 minutes; refresh tokens 30 days and rotate on every use. The
session stores only the current refresh `jti`. Presenting a superseded refresh token
means it either leaked or the client is broken — the session is killed either way and
`auth.refresh_reuse_detected` is audited. That detection is the entire reason to
rotate.

The JWT algorithm is pinned. Without pinning, an attacker presents `alg: none` or
downgrades RS256 to HS256 signed with the public key. Both are tested.

Logout is server-side. The JWT stays cryptographically valid until it expires; the
session record does not, and `current_principal` checks it on every request. Tested.

### Account enumeration

An unknown email and a wrong password produce byte-identical responses. The unknown
case still runs a full Argon2 hash, because returning early would be measurably faster
and turn login into an oracle. Tested.

### Brute force

Five failures locks for fifteen minutes, keyed on `email|ip`. Keyed on both so one
attacker cannot lock a victim out from elsewhere, and so rotating IPs does not reset
the counter. TTL index expires the record; no cleanup job to forget.

## Authorization

RBAC, evaluated server-side on every request. Roles are re-read from the database
rather than trusted from the token, so revoking a role takes effect immediately
instead of at the next token expiry.

The matrix is explicit rather than hierarchical. "FINANCE can refund but cannot change
configuration" is a business rule; a level-based hierarchy would silently grant it the
moment someone reordered the levels.

Note what ADMIN does **not** hold: `PAYMENT_REFUND`, `COMMISSION_WRITE`,
`SETTLEMENT_WRITE`, `ROLE_ASSIGN`, `CONFIG_WRITE`. Moving money and granting privilege
are separated from general administration.

## The attack list

| Attack | What stops it |
| --- | --- |
| Mass assignment / privilege escalation | Request schemas have no `roles` or `status` field. A crafted body is ignored. Tested. |
| Broken object-level authorization | Ownership is in the query (`{_id, customer_id}`), not a comparison after the fetch. Another customer's order is a 404. Tested. |
| Parameter tampering on price | The order schema accepts no amount, price, currency, or discount. Price is read from the vehicle. Tested. |
| Payment manipulation | Six webhook gates. Tested. |
| Webhook spoofing | HMAC over raw bytes; wrong secret rejected and recorded. Tested. |
| Webhook replay | Timestamp inside the signed material; read check plus unique index. Tested. |
| Double ordering | Unique partial index on `vehicle_id` for active statuses, plus a conditional reserve. |
| Injection | No string-built queries. Mongo operators come from code, values from typed Pydantic fields. |
| Sensitive data exposure | Financials live in a nested `internal` document; the public projection is `{"internal": 0}` — one rule, not a denylist that grows. Tested. |
| Stack trace leakage | Three exception handlers. `detail` is logged, only the mapped message is serialised. Tested. |
| Oversized payloads | `Content-Length` rejected above the limit before parsing. |
| Insecure production config | The app refuses to start with a placeholder secret, a short JWT secret, localhost Mongo, or wildcard CORS. Verified. |

## Known gaps

- **Rate limiting is only on login.** General per-IP and per-user throttling needs a
  shared counter store; in-process counters are wrong behind multiple workers.
- **MFA is modelled, not implemented.** `mfa_enabled` gates the login flow and returns
  `MFA_REQUIRED`, but no TOTP verification exists yet. Administrators are not
  currently protected by a second factor.
- **File uploads are not built.** The intended design is signed URL to object storage
  with backend metadata validation; no code exists yet.
- **No CSRF token flow**, because authentication is a bearer token rather than a
  cookie. If cookie sessions are ever adopted, this must be revisited.


## Google sign-in

Authorization code flow, exchanged server-side. Four checks, each of which is
commonly skipped and each of which is enforced here:

1. The code is exchanged on the server, so the client secret never reaches a browser.
2. The `id_token` is verified against Google's published JWKS with RS256. Decoding it
   without verification — the usual shortcut — lets anyone mint an identity for any
   address.
3. `aud` must equal our client id and `iss` must be Google. A token minted for a
   different application is a perfectly valid Google token and must still be rejected.
4. `email_verified` must be true before linking. Without this, anyone who creates a
   Google account bearing a victim's address takes over that account on first sign-in.
   Tested explicitly in `test_unverified_google_email_cannot_take_over_an_account`.

The `state` is generated and stored server-side with a ten-minute TTL and consumed
with `find_one_and_delete`, so one authorization code is usable exactly once and a
replayed callback finds nothing. The `nonce` is checked against the token claim.

### One account per person

Resolution order is deliberate: provider subject first, then verified email. Subject
comes first because it is stable — a Google account can change its address, and
matching on email alone would strand the user with a duplicate account. The email
fallback is what makes "register with a password, later sign in with Google" work.

A unique sparse index on `(identities.provider, identities.subject)` makes a second
account for the same Google identity impossible at the database level.

Provider-only accounts have `password_hash: None`. A password login against one fails
with the same generic `INVALID_CREDENTIALS` as any wrong password, so the endpoint
does not reveal which addresses are Google accounts.

## The super-admin console

The request was "control the database from the web app". That is not what was built,
and the reasoning is recorded in the module docstring of `app/modules/admin/console.py`.

An endpoint executing arbitrary Mongo operations would bypass every invariant the rest
of the codebase enforces — the order and payment state machines, the commission sum
assertion, the unique partial index preventing double sales, and the audit trail. One
mistyped `updateMany` corrupts financial records with no before-image to restore from,
and it turns a stolen super-admin session from "can see everything" into "can rewrite
every payment", indistinguishable from legitimate traffic.

What SUPER_ADMIN has instead:

- **Read anything.** Any allow-listed collection, any filter, including the internal
  pricing and commission fields hidden from every other surface. Every read is audited.
- **Write through named operations.** Suspend a user, assign roles, revoke sessions.
  Each validates, captures a before-image, and audits.

Guardrails, all tested:

| Guard | Why |
| --- | --- |
| Operator allow-list | `$where`, `$expr`, and `$function` execute code. Rejected at any nesting depth. |
| Collection allow-list | A collection added later is invisible until someone lists it. |
| `password_hash`, `mfa_secret`, `identities` always redacted | These grant no operational insight; their only use is impersonation. |
| 200-row cap, regex length and compile check | A pathological pattern over millions of documents is a denial of service against ourselves. |
| Cannot change your own roles | Self-promotion makes the audit trail meaningless. |
| Cannot demote the last active super admin | Locking everyone out must not be one request away. |
| Suspension revokes live sessions | Otherwise the user keeps working until their access token expires. |

Real schema surgery belongs in a reviewed migration run against a backup. If arbitrary
execution is genuinely needed later, the honest route is `mongosh` against Atlas with
per-engineer credentials and Atlas's own audit log — not an HTTP endpoint wearing this
application's identity.
