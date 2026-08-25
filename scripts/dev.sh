#!/usr/bin/env bash
#
# Start the backend and prove it works. One command, in the right order.
#
#     ./scripts/dev.sh
#
# Exists because the four steps have to run in sequence, and `docker compose up`
# blocks forever in the foreground — so pasting them as a block leaves three of
# them queued behind a process that never exits. This starts detached, waits for
# readiness, then seeds and smoke-tests.

set -uo pipefail

# Compose prints a Bake notice when buildx is absent. Harmless, and noise.
export COMPOSE_BAKE=false

COMPOSE_FILES=(-f docker-compose.yml)
BASE_URL="http://localhost:8000"

if [[ "${1:-}" == "--local-db" ]]; then
  COMPOSE_FILES+=(-f docker-compose.local-db.yml)
  echo "  using the bundled MongoDB"
fi

if [[ ! -f .env ]]; then
  echo "  no .env — copying .env.example. Set MONGODB_URI before rerunning."
  cp .env.example .env
  exit 1
fi

echo
echo "  1/4  building and starting"
# --build because compose reuses an existing image and does NOT rebuild when
# app/ changes; without it you can run week-old code and never know.
# --remove-orphans clears containers from services no longer in the file.
docker compose "${COMPOSE_FILES[@]}" up -d --build --remove-orphans

echo "  2/4  waiting for the API to report ready"
for attempt in $(seq 1 40); do
  response=$(curl -sf "${BASE_URL}/readyz" 2>/dev/null || true)
  case "$response" in
    *'"status":"ready"'*)
      echo "       ready"
      break
      ;;
    *'"degraded"'*)
      # The process stays up when the database is unreachable, on purpose — so
      # this prints the reason instead of a bare timeout.
      echo
      echo "  DATABASE NOT REACHABLE: $response"
      echo "  Check MONGODB_URI, and that your IP is on the Atlas allowlist."
      echo "  What the container actually received:"
      docker compose "${COMPOSE_FILES[@]}" exec -T api env | grep MONGODB || true
      exit 1
      ;;
  esac
  if [[ $attempt -eq 40 ]]; then
    echo
    echo "  API did not come up. Last 40 log lines:"
    docker compose "${COMPOSE_FILES[@]}" logs --tail 40 api
    exit 1
  fi
  sleep 2
done

echo "  3/4  seeding"
if ! docker compose "${COMPOSE_FILES[@]}" exec -T api python scripts/seed.py; then
  # A stale image is the usual cause: scripts/ only started shipping in a later
  # build, so an old layer has no /srv/scripts. Rebuilding from scratch fixes it.
  echo
  echo "  seeding failed — rebuilding without cache and retrying once"
  docker compose "${COMPOSE_FILES[@]}" build --no-cache api
  docker compose "${COMPOSE_FILES[@]}" up -d --remove-orphans
  sleep 5
  if ! docker compose "${COMPOSE_FILES[@]}" exec -T api python scripts/seed.py; then
    echo
    echo "  seeding still failing. Last 40 log lines:"
    docker compose "${COMPOSE_FILES[@]}" logs --tail 40 api
    exit 1
  fi
fi

echo "  4/4  smoke test"
# Prefer running from the host: it exercises the published port, which is what
# the frontend will actually talk to. Fall back into the container when the host
# has no httpx.
if python3 -c "import httpx" 2>/dev/null; then
  python3 scripts/smoke.py "$BASE_URL" || smoke_failed=1
else
  docker compose "${COMPOSE_FILES[@]}" exec -T api python scripts/smoke.py http://localhost:8000 || smoke_failed=1
fi

if [[ -n "${smoke_failed:-}" ]]; then
  echo "  smoke test reported failures — see above. The stack is still running."
  exit 1
fi

cat <<INFO

  API      ${BASE_URL}
  Docs     ${BASE_URL}/docs
  Logs     docker compose logs -f api
  Watch    docker compose watch          (syncs app/ and reloads)
  Stop     docker compose down

  Frontend: NEXT_PUBLIC_API_BASE_URL=${BASE_URL}/api/v1

INFO
