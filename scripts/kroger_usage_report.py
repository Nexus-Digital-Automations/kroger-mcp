#!/usr/bin/env python3
"""Kroger API usage report.

Reads the per-day call counters in ``kroger_api_calls`` (populated best-effort at
the retry choke point) and prints calls/day-by-API for the last N days, plus a
"Products vs daily budget" line. These are the hard numbers to show Kroger when
requesting a higher rate tier.

Read-only. Works on both SQLite (default) and Postgres (DATABASE_URL). Usage:
    uv run python scripts/kroger_usage_report.py [--days 30]
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

# Make the package importable when run as a standalone script.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from kroger_mcp.analytics.database import (  # noqa: E402
    ensure_initialized,
    get_db_connection,
)

# Public-tier daily budgets (calls/day per app). The Products cap is the binding
# ceiling for scale; the others are shown for context.
_DAILY_BUDGETS = {
    "Products": 10_000,
    "Cart": 5_000,
    "Identity": 5_000,
    "Locations": 1_600,
}


def _fetch_rows(days: int) -> list[tuple[str, str, str, int]]:
    """Return (call_date, api_family, outcome, total) for the last `days` days."""
    ensure_initialized()
    conn = get_db_connection()
    try:
        # date(...) arithmetic differs across backends, so filter in Python:
        # pull all rows and keep the most recent `days` distinct dates. The table
        # is tiny (a few rows per day), so this is cheap and backend-agnostic.
        rows = conn.execute(
            "SELECT call_date, api_family, outcome, "
            "SUM(call_count) AS total FROM kroger_api_calls "
            "GROUP BY call_date, api_family, outcome "
            "ORDER BY call_date DESC"
        ).fetchall()
    finally:
        if hasattr(conn, "close"):
            conn.close()

    parsed = [
        (str(r["call_date"]), r["api_family"], r["outcome"], int(r["total"]))
        for r in rows
    ]
    recent_dates = sorted({d for d, _, _, _ in parsed}, reverse=True)[:days]
    keep = set(recent_dates)
    return [row for row in parsed if row[0] in keep]


def _print_report(days: int) -> None:
    rows = _fetch_rows(days)
    if not rows:
        print("No Kroger API calls recorded yet. (Meter populates on live calls.)")
        return

    # by_date[date][family] = {"success": n, "failure": n}
    by_date: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"success": 0, "failure": 0})
    )
    families: set[str] = set()
    for call_date, family, outcome, total in rows:
        by_date[call_date][family][outcome] += total
        families.add(family)

    ordered_families = [f for f in ["Products", "Cart", "Locations", "Identity", "Other"]
                        if f in families]
    print(f"\nKroger API usage — last {len(by_date)} day(s) with activity\n")
    for call_date in sorted(by_date, reverse=True):
        print(f"  {call_date}")
        for family in ordered_families:
            counts = by_date[call_date].get(family)
            if not counts:
                continue
            ok, fail = counts["success"], counts["failure"]
            total = ok + fail
            budget = _DAILY_BUDGETS.get(family)
            budget_str = ""
            if budget:
                pct = total / budget * 100
                budget_str = f"  [{total}/{budget} budget, {pct:.1f}%]"
            fail_str = f", {fail} failed" if fail else ""
            print(f"    {family:<10} {total:>6} calls ({ok} ok{fail_str}){budget_str}")
        print()

    # Headline: today's Products consumption vs the binding 10k/day cap.
    latest = max(by_date)
    prod = by_date[latest].get("Products", {})
    prod_total = prod.get("success", 0) + prod.get("failure", 0)
    cap = _DAILY_BUDGETS["Products"]
    print(f"Latest day ({latest}): Products {prod_total}/{cap} "
          f"({prod_total / cap * 100:.1f}% of the binding daily budget).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Kroger API usage report")
    parser.add_argument("--days", type=int, default=30, help="Days to include (default 30)")
    args = parser.parse_args()
    _print_report(max(1, args.days))


if __name__ == "__main__":
    main()
