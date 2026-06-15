#!/usr/bin/env python3
"""Local load test for the Smart Shopper web app — measures req/s and latency
percentiles at increasing concurrency, to find the worker-scaling plateau.

$0 and safe: hits only local cached/DB paths. It does NOT exercise Kroger-API
endpoints, so no external rate limits are touched.

Two workloads (``--mode``):
  read  (default) — GET /login + /dashboard (cached/read DB paths).
  write           — POST /api/safety/approved with a UNIQUE product_id per
                    request. That handler upserts one row into safe_products
                    (no Kroger call), so each request is a distinct DB write.
                    This is the path that exposes the SQLite single-writer lock
                    (db-wide → serialized) vs Postgres row-level locks
                    (concurrent) — i.e. where the backend choice actually shows.

Usage:
    python scripts/loadtest.py [--base-url URL] [--duration SEC] [--label NAME]
                               [--mode read|write]

Registers a throwaway user, logs in, then for each concurrency level fires that
many async workers in a tight loop for `duration` seconds. Reports throughput +
p50/p95/p99 per workload per level. Run once per (backend × worker-count) to
compare SQLite vs Postgres and WEB_WORKERS settings.
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

import httpx

CONCURRENCY_LEVELS = [1, 4, 8, 16, 32, 64]
READ_PATHS = ["/login", "/dashboard"]

# An async call that issues one request and returns its HTTP status code.
RequestFn = Callable[[], Awaitable[int]]


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


def _get_request(client: httpx.AsyncClient, url: str) -> RequestFn:
    """A read workload: GET the same URL each time."""

    async def fire() -> int:
        resp = await client.get(url)
        return resp.status_code

    return fire


def _write_request(client: httpx.AsyncClient, base_url: str) -> RequestFn:
    """A write workload: upsert a UNIQUE safe_products row per request.

    Each call carries a fresh product_id so the rows are distinct — distinct
    rows contend on SQLite's db-wide write lock but not on Postgres row locks,
    which is exactly the difference this benchmark exists to measure.
    """
    url = f"{base_url}/api/safety/approved"

    async def fire() -> int:
        resp = await client.post(url, json={"product_id": f"LOAD-{uuid.uuid4().hex}"})
        return resp.status_code

    return fire


async def _run_level(request_fn: RequestFn, concurrency: int, duration: float) -> dict:
    latencies: list[float] = []
    statuses: dict[int, int] = {}
    stop_at = time.monotonic() + duration

    async def worker() -> None:
        while time.monotonic() < stop_at:
            t0 = time.perf_counter()
            try:
                code = await request_fn()
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
    ap.add_argument("--mode", choices=["read", "write"], default="read")
    args = ap.parse_args()

    limits = httpx.Limits(max_connections=256, max_keepalive_connections=256)
    async with httpx.AsyncClient(
        timeout=30.0, limits=limits, follow_redirects=False
    ) as client:
        await _provision(client, args.base_url)

        # Confirm the workload actually serves before measuring.
        if args.mode == "write":
            probe = await client.post(
                f"{args.base_url}/api/safety/approved",
                json={"product_id": f"LOAD-{uuid.uuid4().hex}"},
            )
            workloads: list[tuple[str, RequestFn]] = [
                ("POST /api/safety/approved", _write_request(client, args.base_url))
            ]
        else:
            probe = await client.get(f"{args.base_url}/dashboard")
            workloads = [
                (f"GET {p}", _get_request(client, f"{args.base_url}{p}")) for p in READ_PATHS
            ]
        print(f"[{args.label or 'run'}] {args.mode} probe -> {probe.status_code}")

        for name, request_fn in workloads:
            print(f"\n=== {args.label or 'run'}  {name} ===")
            print(
                f"{'conc':>5} {'reqs':>8} {'req/s':>9} {'p50ms':>8} {'p95ms':>8} {'p99ms':>8}  statuses"
            )
            for c in CONCURRENCY_LEVELS:
                r = await _run_level(request_fn, c, args.duration)
                print(
                    f"{r['concurrency']:>5} {r['requests']:>8} {r['rps']:>9.1f} "
                    f"{r['p50']:>8.1f} {r['p95']:>8.1f} {r['p99']:>8.1f}  {r['statuses']}"
                )


if __name__ == "__main__":
    asyncio.run(main())
