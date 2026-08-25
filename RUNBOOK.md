# Running the backend

## The short version

```bash
cp .env.example .env      # set MONGODB_URI
./scripts/dev.sh
```

That builds, starts, waits for readiness, seeds, and smoke-tests — in that order.

`dev.sh` exists because these steps have ordering hazards that are invisible until
they bite:

- `docker compose up` (without `-d`) never exits, so anything pasted after it stays
  queued behind it forever.
- `docker compose up -d` returns when the container **starts**, not when the app is
  **ready**. Running the smoke test in that gap gives twenty-one connection errors
  that say nothing about the app.
- `docker compose up` reuses an existing image and does not rebuild when `app/`
  changes, so you can run week-old code and never notice.

The script waits, rebuilds, and retries. Use it.

---

## Step by step

### 1. Configure

```bash
cp .env.example .env
```

Set **`MONGODB_URI`**. Nothing starts without it — Compose fails with a message
rather than substituting a default, which is how an earlier version of this file
quietly pointed at the wrong database.

```dotenv
MONGODB_URI=mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true&w=majority
MONGODB_DATABASE=voltaris
JWT_SECRET=<openssl rand -base64 48>
CORS_ORIGINS=["http://localhost:3000"]
```

**On Atlas, add your IP to the allowlist** (Network Access → Add IP Address). A
missing entry looks exactly like a wrong password: a timeout and a `degraded`
readiness check.

### 2. Start

```bash
docker compose up -d --build --remove-orphans
```

Three flags, each earning its place:

| Flag | Why |
| --- | --- |
| `-d` | Detached. Without it the terminal is captured and nothing else runs. |
| `--build` | **Compose reuses an existing image and does not rebuild when `app/` changes.** Without this you can run week-old code and never know — check the `indexes` object in the startup log: the current build lists 20 collections. |
| `--remove-orphans` | Clears containers from services no longer in the compose file. |

**Wait for readiness before doing anything else.** `-d` returns when the container
starts, not when the app can serve — and it has to reach Atlas first. In that window
Docker's port proxy accepts connections and closes them, which looks like a broken
app but is not:

```bash
until curl -sf localhost:8000/readyz | grep -q '"ready"'; do sleep 2; done
```

Then, in the same terminal:

```bash
docker compose logs -f api      # follow the logs
docker compose watch            # sync app/ and reload on save
```

**No Atlas?** Leave `MONGODB_URI` unset and add the local overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.local-db.yml up -d --build
```

**Without Docker:**

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

### 3. Seed

```bash
docker compose exec api python scripts/seed.py
```

Eight vehicles with real Rwandan pricing, a dealer, two guides, two blog posts, a
charging location, and a super admin (`root@voltaris.rw` /
`development-only-password`). It refuses to run against production, and it clears
the vehicle, dealer, article, and charging collections first — never point it at
data you care about.

### 4. Prove it works

```bash
python scripts/smoke.py http://localhost:8000
```

Twenty-one checks against the live HTTP surface. Not a list of things that ought
to work — it registers a user, places an order, and tries to forge a payment:

```
  PASS  health: process is up                                voltaris-api
  PASS  health: database is reachable                        indexes ok
  PASS  marketplace: vehicles list                           8 vehicles
  PASS  marketplace: internal pricing is not exposed         clean
  PASS  marketplace: filters and sorting                     3 under 25M, correctly ordered
  PASS  marketplace: facets                                  6 makes
  PASS  marketplace: vehicle detail by slug                  byd-atto-3-2023-kigali
  PASS  marketplace: unknown slug returns the error envelope req_cbdcc1d903c7
  PASS  catalog: dealers, content, charging                  1 dealers, 2 guides, 2 posts, 1 chargers
  PASS  leads: anonymous enquiry is accepted                 INQ-2608-B383E9
  PASS  leads: honeypot submission is silently dropped       accepted and discarded
  PASS  auth: register, login, session                       smoke-66fa@example.com as BUYER
  PASS  auth: registration cannot self-assign a role         roles ignored, as they must be
  PASS  auth: unknown email and wrong password are indistinguishable identical responses
  PASS  account: profile and saved vehicles                  profile, save, idempotent re-save, unsave
  PASS  orders: price comes from the vehicle, not the request VM-202608-107C94 at 27000000 RWF
  PASS  payments: no client route can mark an order paid     all three attempts rejected
  PASS  payments: unsigned webhook is rejected               signature required
  PASS  security: a buyer cannot reach the admin console     403 as expected
  PASS  security: account routes reject an anonymous caller  401 as expected
  PASS  security: response headers are set                   nosniff, DENY, referrer-policy, request id
```

Exits non-zero on any failure, so it doubles as a deploy gate. Point it at staging
after a release: `python scripts/smoke.py https://staging-api.voltaris.rw`.

## 5. Look around

- `http://localhost:8000/` — a signpost: service name, environment, and where to go
- `http://localhost:8000/docs` — interactive OpenAPI, 46 endpoints (development only)
- `http://localhost:8000/readyz` — expect `{"status":"ready","indexes":"ok"}`

## 6. Connect the frontend

```dotenv
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api/v1
```

The `/api/v1` suffix is required. Then `npm run dev` in the frontend, and the
marketplace should show the seeded vehicles.

---

## When something is wrong

**Read `/readyz` first.** It is the difference between "the app is broken" and
"the app cannot see its database":

| Response | Meaning |
| --- | --- |
| `{"status":"ready","indexes":"ok"}` | Everything is connected |
| `{"status":"degraded","database":"unreachable"}` | Wrong URI, Atlas allowlist, or the database is down. The process stays up on purpose. |
| `{"status":"degraded","indexes":"failed"}` | Connected but index creation failed — usually insufficient database permissions |

**`can't open file '/srv/scripts/seed.py'`** — the image predates the build that
started shipping `scripts/`. Rebuild from scratch:

```bash
docker compose build --no-cache api && docker compose up -d
```

**Smoke test reports connection errors on every check** — the server is not
answering, usually because it is still booting. The script now waits and says so
in one line instead of twenty-one. If it still cannot connect:
`docker compose ps` and `docker compose logs --tail 40 api`.

**Running old code?** Check the `indexes` object in the startup log. The current
build lists 20 collections; anything fewer means the image predates your source.
Rebuild:

```bash
docker compose up -d --build
```

**Check what the container actually got**, not what the `.env` says:

```bash
docker compose exec api env | grep MONGODB
```

**Every error response carries a `request_id`.** Quote it and the matching log line
has the internal detail the client was never given:

```bash
docker compose logs api | grep req_82931
```

**Mongo unhealthy on the local overlay?** If the logs say *"This node is not a
member of the config"*, the replica set config on the volume names a dead
container. Clear it:

```bash
docker compose -f docker-compose.yml -f docker-compose.local-db.yml down -v
```

More failure modes in `OPERATIONS.md`.

## Tests

```bash
pytest tests/unit tests/api tests/security      # 124, no database needed
MONGODB_TEST_URI=mongodb://localhost:27017 pytest tests/integration
```

The second one matters. The default suite runs against an in-memory Mongo double
that does not enforce unique or partial indexes, so the guarantees that depend on
them — one active order per vehicle, no duplicate webhook, one commission per
order — are proven only there. See `TESTING.md`.
