"""In-process bridge from the chat engine to the real FastMCP tool surface.

Replaces a hand-maintained, partial tool registry with the actual 18 MCP
tools registered in server.py, so the chatbot's capabilities can never drift
from what the MCP server itself exposes.

Owns:
- a lazily-built FastMCP server singleton (mirrors what runs the real MCP
  server — one process-wide instance, never rebuilt per request)
- one long-lived fastmcp.Client per chat conversation. FastMCP mints a fresh
  session (and thus resets session-scoped gates like
  pantry(action='get_attention')) each time a Client connects, so keeping one
  Client open per conversation_id — instead of one per HTTP request — is what
  keeps those gates satisfied for the life of a conversation.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastmcp import Client

from ..server import create_server

_server = None


def get_server():
    global _server
    if _server is None:
        _server = create_server()
    return _server


async def list_openai_tools() -> list[dict[str, Any]]:
    """Fetch all registered MCP tools, converted to OpenAI function-calling format."""
    async with Client(get_server()) as client:
        mcp_tools = await client.list_tools()
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": getattr(t, "inputSchema", None)
                or getattr(t, "input_schema", None)
                or {"type": "object", "properties": {}},
            },
        }
        for t in mcp_tools
    ]


class _ConversationWorker:
    """Owns one conversation's fastmcp.Client for its entire lifetime, in one task.

    FastMCP's Client relies on anyio task groups whose cancel scopes are bound
    to the asyncio Task that opened them — entering and exiting the Client's
    `async with` block must happen in the *same* task. Since call_tool() is
    invoked from whichever HTTP-request task happens to be handling the
    current chat message (a different task each time), the Client can't be
    entered/exited directly from those request tasks. This worker owns a
    single dedicated task for the conversation's whole life and channels
    every call through a queue, so enter/exit always happen in that one task.
    """

    __slots__ = ("_queue", "_task", "last_used")

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._task = asyncio.create_task(self._run())
        self.last_used = time.monotonic()

    async def _run(self) -> None:
        async with Client(get_server()) as client:
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                name, args, future = item
                try:
                    future.set_result(await client.call_tool(name, args))
                except Exception as exc:  # noqa: BLE001 - propagated via future
                    future.set_exception(exc)

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._queue.put((name, args, future))
        return await future

    async def close(self) -> None:
        await self._queue.put(None)
        await self._task


_conversations: dict[str, _ConversationWorker] = {}
_lock = asyncio.Lock()
_IDLE_TTL_SECONDS = 30 * 60


async def _evict_stale() -> None:
    now = time.monotonic()
    stale = [cid for cid, w in _conversations.items() if now - w.last_used > _IDLE_TTL_SECONDS]
    for cid in stale:
        worker = _conversations.pop(cid)
        await worker.close()


async def get_conversation_worker(conversation_id: str) -> _ConversationWorker:
    """Return the long-lived worker for this conversation, creating it if needed."""
    async with _lock:
        await _evict_stale()
        worker = _conversations.get(conversation_id)
        if worker is None:
            worker = _ConversationWorker()
            _conversations[conversation_id] = worker
        worker.last_used = time.monotonic()
        return worker


def unwrap_tool_result(result: Any) -> Any:
    """FastMCP call_tool result -> plain dict/list, across client/version shapes.

    Handles CallToolResult(.data/.structured_content/.content), a bare list of
    content blocks, or a (content, structured) tuple — mirrors the proven
    pattern in scripts/verify_mcp_and_listcard.py::_tool_payload.
    """
    data = getattr(result, "data", None)
    if data is not None:
        return data
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    content = getattr(result, "content", None)
    if content is None:
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
            return result[1]
        content = result
    block = content[0]
    text = getattr(block, "text", None)
    if text is None and isinstance(block, dict):
        text = block.get("text")
    return json.loads(text) if text is not None else content


async def call_tool(conversation_id: str, name: str, args: dict[str, Any]) -> Any:
    """Invoke a real MCP tool in-process and return its unwrapped payload."""
    worker = await get_conversation_worker(conversation_id)
    result = await worker.call_tool(name, args)
    return unwrap_tool_result(result)
