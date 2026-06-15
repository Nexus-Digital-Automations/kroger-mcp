#!/usr/bin/env python3
"""Local load test for the Smart Shopper web app — measures req/s and latency
percentiles at increasing concurrency, to find the worker-scaling plateau.

$0 and safe: hits only local cached/DB paths (/login, /dashboard). It does NOT
exercise Kroger-API endpoints, so no external rate limits are touched.

Usage:
    python scripts/loadtest.py [--base-url URL] [--duration SEC] [--label NAME]

Registers a throwaway user, logs in, then for each concurrency level fires that
many async workers in a tight GET loop for `duration` seconds. Reports
throughput + p50/p95/p99 per path per level. Run once per server worker-count to
compare WEB_WORKERS settings.
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid

import httpx

CONCURRENCY_LEVELS = [1, 4, 8, 16, 32, 64]
PATHS = ["/login", "/dashboard"]


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


async def _provision(client: httpx.AsyncClient, base_url: str) -> None:
    """Register + log in a throwaway user; raises if either fails."""
    rid = uuid.uuid4().hex[:8]
    email = f"load-{rid}@example.test"
    pw = f"Pw!Load{rid}"
    r = await client.post(
        f"{base_url}/register",
        data={
            "display_name": f"__LOAD__{rid}",
            "email": email,
            "password": pw,
            "confirm_password": pw,
        },
    )
    if r.status_code >= 400:
        raise RuntimeError(f"register failed: {r.status_code} {r.text[:200]}")
    r = await client.post(f"{base_url}/login", data={"email": email, "password": pw})
    if r.status_code >= 400:
        raise RuntimeError(f"login failed: {r.status_code} {r.text[:200]}")
    # Record consent so the privacy gate doesn't redirect interactive routes.
    await client.post(f"{base_url}/api/settings/consent", json={"updates": {}})


async def _run_level(
    client: httpx.AsyncClient, url: str, concurrency: int, duration: float
) -> dict:
    latencies: list[float] = []
    statuses: dict[int, int] = {}
    stop_at = time.monotonic() + duration

    async def worker() -> None:
        while time.monotonic() < stop_at:
            t0 = time.perf_counter()
            try:
                resp = await client.get(url)
                code = resp.status_code
            except Exception:
                code = -1
            latencies.append((time.perf_counter() - t0) * 1000.0)
            statuses[code] = statuses.get(code, 0) + 1

    t_start = time.monotonic()
    await asyncio.gather(*[worker() for _ in range(concurrency)])
    elapsed = time.monotonic() - t_start

    n = len(latencies)
    return {
        "concurrency": concurrency,
        "requests": n,
        "rps": n / elapsed if elapsed else 0.0,
        "p50": _pct(latencies, 50),
        "p95": _pct(latencies, 95),
        "p99": _pct(latencies, 99),
        "statuses": statuses,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--duration", type=float, default=5.0)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    limits = httpx.Limits(max_connections=256, max_keepalive_connections=256)
    async with httpx.AsyncClient(
        timeout=30.0, limits=limits, follow_redirects=False
    ) as client:
        await _provision(client, args.base_url)

        # Confirm the heavy path actually serves 200 before measuring.
        probe = await client.get(f"{args.base_url}/dashboard")
        print(f"[{args.label or 'run'}] /dashboard probe -> {probe.status_code}")

        for path in PATHS:
            url = f"{args.base_url}{path}"
            print(f"\n=== {args.label or 'run'}  {path} ===")
            print(
                f"{'conc':>5} {'reqs':>8} {'req/s':>9} {'p50ms':>8} {'p95ms':>8} {'p99ms':>8}  statuses"
            )
            for c in CONCURRENCY_LEVELS:
                r = await _run_level(client, url, c, args.duration)
                print(
                    f"{r['concurrency']:>5} {r['requests']:>8} {r['rps']:>9.1f} "
                    f"{r['p50']:>8.1f} {r['p95']:>8.1f} {r['p99']:>8.1f}  {r['statuses']}"
                )


if __name__ == "__main__":
    asyncio.run(main())
