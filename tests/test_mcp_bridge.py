"""Tests for the in-process MCP tool-surface bridge (mcp_bridge.py).

Covers the session-continuity fix: one fastmcp.Client must stay open per
chat conversation (not per HTTP request), since FastMCP mints a fresh
session — and resets session-scoped gates like pantry(get_attention) — each
time a Client connects. The Client's enter/exit lives inside a dedicated
per-conversation worker task (see _ConversationWorker's docstring) because
anyio's cancel scopes are bound to the task that opened them.
"""

from __future__ import annotations

import asyncio

from kroger_mcp.web import mcp_bridge


def test_conversation_worker_is_reused_across_calls():
    async def run():
        first = await mcp_bridge.get_conversation_worker("conv-reuse")
        second = await mcp_bridge.get_conversation_worker("conv-reuse")
        try:
            assert first is second
        finally:
            await first.close()
            mcp_bridge._conversations.clear()

    asyncio.run(run())


def test_stale_conversation_is_evicted(monkeypatch):
    fake_time = {"t": 0.0}
    monkeypatch.setattr(mcp_bridge.time, "monotonic", lambda: fake_time["t"])

    async def run():
        first = await mcp_bridge.get_conversation_worker("conv-stale")
        fake_time["t"] += mcp_bridge._IDLE_TTL_SECONDS + 1
        second = await mcp_bridge.get_conversation_worker("conv-stale")
        try:
            assert first is not second
        finally:
            await second.close()
            mcp_bridge._conversations.clear()

    asyncio.run(run())
