# syntax=docker/dockerfile:1

# --- build -------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

# argon2-cffi needs a compiler; it stays in this stage and never ships.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Runtime dependencies only. requirements-dev.txt (pytest, ruff, mypy, mongomock,
# watchfiles) is deliberately never copied — test tooling in a production image is
# extra attack surface and extra megabytes for no benefit.
COPY requirements.txt .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# --- runtime -----------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Non-root, no shell, no home. A compromised process gets as little as possible.
RUN groupadd --system --gid 1001 voltaris \
    && useradd --system --uid 1001 --gid voltaris --no-create-home --shell /usr/sbin/nologin voltaris

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv
COPY --chown=voltaris:voltaris app ./app
# Operational scripts ship with the image so `docker compose exec api python
# scripts/seed.py` works. They are a few KB and carry no secrets. Leaving them
# out is why seeding failed with "can't open file '/srv/scripts/seed.py'".
COPY --chown=voltaris:voltaris scripts ./scripts

USER voltaris
EXPOSE 8000

# Liveness only — readiness is polled by the orchestrator, and a slow database
# must not cause a healthy container to be restarted.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2).status==200 else 1)"

# No secrets baked in; every one arrives from the environment at run time.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-server-header"]
