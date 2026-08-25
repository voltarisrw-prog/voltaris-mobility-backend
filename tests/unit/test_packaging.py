"""Guards against the two failures that took the local stack down.

Both were invisible to every other test: the app was fine, the *packaging* was
wrong. These are cheap and they fail loudly the moment someone reintroduces the
problem.
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_the_image_ships_the_operational_scripts():
    """seed.py and smoke.py must be inside the container.

    Without this the Dockerfile copied only `app/`, and
    `docker compose exec api python scripts/seed.py` failed with
    "can't open file '/srv/scripts/seed.py'".
    """
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY --chown=voltaris:voltaris scripts ./scripts" in dockerfile

    ignored = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "scripts" not in ignored, ".dockerignore would undo the COPY"


def test_the_image_still_excludes_what_it_should():
    ignored = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # Tests and secrets have no business in a production image.
    for excluded in ("tests", ".env", ".venv", ".git"):
        assert excluded in ignored, f"{excluded} must stay out of the image"


def test_smoke_waits_for_the_server_before_running_checks():
    """Twenty-one identical connection errors is a useless report.

    `docker compose up -d` returns before the app can serve; the smoke test has
    to distinguish "not up yet" from "the app said no".
    """
    source = (ROOT / "scripts" / "smoke.py").read_text()
    assert "async def wait_for_server" in source
    assert "CONNECTION_ERRORS" in source
    assert "await wait_for_server(client)" in source


def test_dev_script_rebuilds_and_waits():
    """The three ordering hazards, all guarded in one place."""
    source = (ROOT / "scripts" / "dev.sh").read_text()
    assert "up -d --build" in source, "must rebuild, or it runs stale code"
    assert "readyz" in source, "must wait for readiness before seeding"
    assert "--no-cache" in source, "must be able to recover from a stale image"


def test_seed_refuses_production():
    source = (ROOT / "scripts" / "seed.py").read_text()
    assert "refusing to seed a production database" in source


def test_probe_endpoints_answer_head_as_well_as_get():
    """Render and most uptime monitors probe with HEAD.

    A GET-only route replies 405, which on a status-code dashboard is
    indistinguishable from the service being broken.
    """
    source = (ROOT / "app" / "api" / "v1" / "routes" / "health.py").read_text()
    for path in ('"/"', '"/healthz"', '"/readyz"'):
        assert f'api_route({path}, methods=["GET", "HEAD"]' in source, f"{path} is GET-only"
