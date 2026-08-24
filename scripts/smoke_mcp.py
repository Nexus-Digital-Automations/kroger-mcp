#!/usr/bin/env python3
"""Full-surface smoke test for the Smart Shopper (Kroger) MCP server.

Owner: Smart Shopper maintainers. Exercises every `action` of every registered
tool against the PRODUCTION server over the same stdio transport `.mcp.json`
uses (ssh -> kroger-run.sh on the prod Mac mini), so a pass proves the path the
user actually calls, not a local in-process approximation.

Write-capable actions are never committed — see `scripts/smoke_mcp_spec.py` for
the read/preview/skip classification and the reason attached to every skip.

Results append to output/mcp-smoke/results.jsonl after each call, so an
interrupted run resumes from the last completed action instead of replaying the
whole matrix. Pass --restart to discard checkpoints and start over.

Usage:
    uv run --frozen python scripts/smoke_mcp.py [--restart] [--tool NAME]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_mcp_spec import FIXTURE_PROBES, SKIP, SPEC  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "output" / "mcp-smoke"
RESULTS_PATH = OUT_DIR / "results.jsonl"
CALL_TIMEOUT_S = 90.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("smoke_mcp")


def build_transport() -> StdioTransport:
    """Mirror `.mcp.json` exactly so the smoke test uses the production path."""
    config = json.loads((REPO_ROOT / ".mcp.json").read_text())
    server = config["mcpServers"]["kroger"]
    return StdioTransport(command=server["command"], args=server["args"])


def unwrap(result: Any) -> Any:
    """Pull the JSON payload out of an MCP CallToolResult."""
    for attr in ("structured_content", "data"):
        payload = getattr(result, attr, None)
        if payload is not None:
            return payload
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"_text": text[:2000]}
        # Binary blocks (products.get_images returns a JPEG, not JSON) carry
        # base64 in .data with no .text — a successful result, not an empty one.
        blob = getattr(block, "data", None)
        if blob:
            return {
                "_binary": getattr(block, "mimeType", "application/octet-stream"),
                "_bytes": len(blob),
            }
    return {"_empty": True}


def first_id(payload: Any, *keys: str) -> str | None:
    """Find the first value for any of `keys` anywhere in a nested payload."""
    queue = [payload]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            for key in keys:
                value = node.get(key)
                if isinstance(value, str | int) and str(value):
                    return str(value)
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return None


async def discover_fixtures(client: Client) -> dict[str, str]:
    """Harvest real IDs so ID-scoped actions get a genuine target."""
    fixtures: dict[str, str] = {}
    for tool, action, placeholder, keys in FIXTURE_PROBES:
        try:
            payload = unwrap(
                await asyncio.wait_for(
                    client.call_tool(tool, {"action": action}), CALL_TIMEOUT_S
                )
            )
        except Exception as exc:  # noqa: BLE001 - discovery is best-effort
            log.warning("fixture probe %s.%s failed: %s", tool, action, exc)
            continue
        found = first_id(payload, *keys)
        if found:
            fixtures[placeholder] = found
            log.info("fixture %s = %s (from %s.%s)", placeholder, found, tool, action)
        else:
            log.warning("no fixture for %s from %s.%s", placeholder, tool, action)
    return fixtures


def resolve(args: dict[str, Any], fixtures: dict[str, str]) -> tuple[dict, str | None]:
    """Substitute $placeholders; report the first one with no fixture."""
    resolved: dict[str, Any] = {}
    for key, value in args.items():
        candidates = value if isinstance(value, list) else [value]
        substituted = []
        for item in candidates:
            if isinstance(item, str) and item.startswith("$"):
                if item not in fixtures:
                    return {}, item
                substituted.append(fixtures[item])
            else:
                substituted.append(item)
        resolved[key] = substituted if isinstance(value, list) else substituted[0]
    return resolved, None


def load_done() -> set[str]:
    if not RESULTS_PATH.exists():
        return set()
    done = set()
    for line in RESULTS_PATH.read_text().splitlines():
        if not line.strip():
            continue
        try:
            done.add(json.loads(line)["key"])
        except (json.JSONDecodeError, KeyError):
            continue
    return done


def record(row: dict[str, Any]) -> None:
    """Append-then-flush so an interrupted run loses at most the in-flight call."""
    with RESULTS_PATH.open("a") as handle:
        handle.write(json.dumps(row) + "\n")
        handle.flush()


def classify(payload: Any) -> tuple[str, str]:
    """Decide PASS/FAIL from a tool payload. Returns (status, detail)."""
    if isinstance(payload, dict):
        if payload.get("_empty"):
            return "FAIL", "empty response"
        if payload.get("success") is False:
            return "FAIL", str(payload.get("error") or payload.get("message"))[:400]
    return "PASS", ""


async def call_action(
    client: Client, tool: str, args: dict[str, Any]
) -> tuple[str, str, Any]:
    """Invoke one action. Every failure mode is a datum, so nothing propagates."""
    try:
        payload = unwrap(
            await asyncio.wait_for(client.call_tool(tool, args), CALL_TIMEOUT_S)
        )
        status, detail = classify(payload)
        return status, detail, payload
    except TimeoutError:
        return "FAIL", f"timeout >{CALL_TIMEOUT_S}s", None
    except Exception as exc:  # noqa: BLE001
        return "FAIL", f"{type(exc).__name__}: {exc}", None


def check_coverage(server_tools: set[str]) -> None:
    """The spec must account for exactly what the live server exposes."""
    missing = set(SPEC) - server_tools
    extra = server_tools - set(SPEC)
    if missing:
        log.error("spec covers tools the server does not expose: %s", sorted(missing))
    if extra:
        log.error("server exposes tools the spec does not cover: %s", sorted(extra))
    if not missing and not extra:
        log.info("coverage OK: spec matches all %d server tools", len(server_tools))


async def run(only_tool: str | None) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    done = load_done()
    log.info("resuming with %d actions already recorded", len(done))

    async with Client(build_transport()) as client:
        server_tools = {tool.name for tool in await client.list_tools()}
        log.info("server exposes %d tools", len(server_tools))
        check_coverage(server_tools)
        fixtures = await discover_fixtures(client)

        for tool_name, actions in SPEC.items():
            if only_tool and tool_name != only_tool:
                continue
            for action, (mode, raw_args, reason) in actions.items():
                key = f"{tool_name}.{action}"
                if key in done:
                    continue
                row = {"key": key, "tool": tool_name, "action": action, "mode": mode}

                if mode == SKIP:
                    record({**row, "status": "SKIP", "detail": reason})
                    continue

                args, unresolved = resolve(raw_args, fixtures)
                if unresolved:
                    record(
                        {
                            **row,
                            "status": "SKIP",
                            "detail": f"no fixture available for {unresolved}",
                        }
                    )
                    continue

                args["action"] = action
                started = time.perf_counter()
                status, detail, payload = await call_action(client, tool_name, args)
                elapsed = round(time.perf_counter() - started, 2)
                log.info("%-34s %-4s %6.2fs %s", key, status, elapsed, detail[:120])
                record(
                    {
                        **row,
                        "status": status,
                        "detail": detail,
                        "seconds": elapsed,
                        "sample": json.dumps(payload)[:600] if payload else None,
                    }
                )

    summarize()


def summarize() -> None:
    rows = [
        json.loads(line)
        for line in RESULTS_PATH.read_text().splitlines()
        if line.strip()
    ]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print("\n=== SMOKE SUMMARY ===")
    print(f"total actions recorded: {len(rows)}")
    for status in ("PASS", "FAIL", "SKIP"):
        print(f"  {status}: {counts.get(status, 0)}")
    failures = [r for r in rows if r["status"] == "FAIL"]
    if failures:
        print("\n--- FAILURES ---")
        for row in failures:
            print(f"  {row['key']}: {row['detail']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--restart", action="store_true", help="discard checkpoints and rerun all"
    )
    parser.add_argument("--tool", help="limit the run to a single tool")
    parser.add_argument(
        "--summary-only", action="store_true", help="print the summary and exit"
    )
    options = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if options.summary_only:
        summarize()
        return
    if options.restart and RESULTS_PATH.exists():
        RESULTS_PATH.unlink()
    asyncio.run(run(options.tool))


if __name__ == "__main__":
    main()
