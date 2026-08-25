"""Is the frontend actually talking to the backend?

    python scripts/check_connection.py https://voltaris-api.onrender.com \
                                       https://voltaris-mobility-frontend.vercel.app

Walks the chain one layer at a time — reachable, ready, has data, CORS, cookie
policy — because "the site shows no cars" has five different causes and they
need different fixes.
"""

from __future__ import annotations

import asyncio
import sys

import httpx


async def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit("usage: check_connection.py <api-url> <frontend-url>")
    api = sys.argv[1].rstrip("/")
    web = sys.argv[2].rstrip("/")
    problems: list[str] = []

    print(f"\n  API      {api}\n  Frontend {web}\n  {'-' * 64}")

    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as c:
        # 1. Reachable at all. Long timeout: a sleeping free instance can take
        # a full minute to answer its first request.
        try:
            r = await c.get(f"{api}/healthz")
            print(f"  PASS  API reachable                    {r.json().get('service')}")
        except Exception as exc:
            print(f"  FAIL  API unreachable                  {type(exc).__name__}")
            print("\n  Nothing else can pass. Check the service is deployed and awake.\n")
            return 1

        # 2. Database connected.
        r = await c.get(f"{api}/readyz")
        body = r.json()
        if body.get("status") == "ready":
            print(f"  PASS  Database connected               indexes {body.get('indexes')}")
        else:
            print(f"  FAIL  Database not connected           {body}")
            problems.append("Set MONGODB_URI, and allow the host's IP in Atlas Network Access.")

        # 3. Data present. Empty is not an error, but it looks like one.
        r = await c.get(f"{api}/api/v1/vehicles")
        total = r.json().get("total", 0) if r.status_code == 200 else 0
        if r.status_code != 200:
            print(f"  FAIL  Vehicles endpoint                HTTP {r.status_code}")
            problems.append("The vehicles endpoint is failing; check the API logs.")
        elif total == 0:
            print("  WARN  No vehicles                      the marketplace will look empty")
            problems.append("Run scripts/seed.py, or add real listings.")
        else:
            print(f"  PASS  Vehicles present                 {total}")

        # 4. CORS. A preflight that omits the origin header is the single most
        # common reason a working API still shows nothing in a browser.
        r = await c.request(
            "OPTIONS",
            f"{api}/api/v1/vehicles",
            headers={
                "Origin": web,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        allowed = r.headers.get("access-control-allow-origin")
        creds = r.headers.get("access-control-allow-credentials")
        if allowed == web:
            print(f"  PASS  CORS allows the frontend         credentials={creds}")
        else:
            print(f"  FAIL  CORS rejects the frontend        allow-origin={allowed!r}")
            problems.append(f'Set CORS_ORIGINS=["{web}"] on the API — exact, no trailing slash.')

        # 5. Cookie policy. The subtle one.
        def site(url: str) -> str:
            host = url.split("//", 1)[-1].split("/")[0]
            return ".".join(host.split(".")[-2:])

        cross_site = site(api) != site(web)
        relation = "DIFFERENT sites" if cross_site else "the same site"
        print(f"  INFO  Frontend and API are             {relation}")
        if cross_site:
            print("  WARN  Cross-site cookies               login will not persist on SameSite=lax")
            problems.append(
                "Set SESSION_COOKIE_SAMESITE=none on the API (browsing works either "
                "way; only signed-in sessions break). Unset it once both are on "
                "voltaris.rw."
            )

        # 6. The frontend is actually pointed at this API.
        try:
            r = await c.get(f"{web}/cars")
            if r.status_code == 200:
                stocked = "floor is being stocked" not in r.text
                print(
                    f"  {'PASS' if stocked else 'WARN'}  Frontend renders listings         "
                    f"{'yes' if stocked else 'empty state shown'}"
                )
                if not stocked and total > 0:
                    problems.append(
                        "The API has vehicles but the frontend shows none: "
                        f"set NEXT_PUBLIC_API_BASE_URL={api}/api/v1 in Vercel and REDEPLOY "
                        "(NEXT_PUBLIC_* is baked in at build time)."
                    )
            else:
                print(f"  FAIL  Frontend /cars                   HTTP {r.status_code}")
        except Exception as exc:
            print(f"  FAIL  Frontend unreachable             {type(exc).__name__}")

    print(f"  {'-' * 64}")
    if problems:
        print("\n  What to fix, in order:\n")
        for i, p in enumerate(problems, 1):
            print(f"    {i}. {p}")
        print()
        return 1
    print("\n  Connected.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
