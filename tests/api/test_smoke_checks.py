"""Runs scripts/smoke.py's checks against the app in-process.

The script is a deploy gate pointed at real environments, so it must not be the
one thing nobody tests. This drives the same check list through ASGI, which means
a broken check fails CI rather than a deployment.

Run as one sequence, not parametrised, because the checks *are* a sequence: the
auth check leaves a bearer token on the shared client that everything after it
needs. Parametrising them would test a scenario that never happens in practice.
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from scripts.smoke import CHECKS

# The database check needs a real lifespan, which the mocked fixture does not run.
SKIP = {"health: database is reachable"}


def test_the_check_list_is_intact():
    names = [name for name, _ in CHECKS]
    assert len(names) >= 18, f"only {len(names)} checks registered"
    assert len(set(names)) == len(names), "duplicate check names"


async def test_all_smoke_checks_pass_in_sequence(api, db, seeded):
    _ = db
    failures: list[str] = []
    results: list[str] = []

    for name, fn in CHECKS:
        if name in SKIP:
            continue
        try:
            detail = await fn(api)
            results.append(f"PASS {name}: {detail}")
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    assert not failures, "smoke checks failed:\n  " + "\n  ".join(failures)
    assert len(results) == len(CHECKS) - len(SKIP)
