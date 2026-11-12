"""Token-saving layer between the MCP tools and every client.

Owner: Smart Shopper maintainers. An agent pays for every byte of the tool list
(once per session) and of every tool result. This middleware trims both without
changing a tool, action, or parameter name:

- Tool list: an optional parameter is advertised as its plain type instead of
  `anyOf: [T, null]`, `default: null` and pydantic `title`s are dropped, and
  the output schema is withheld. Validation still runs on the server-side
  pydantic model, so the call contract is unchanged.
- Tool results: the JSON is sent once, as compact text, instead of as text plus
  an identical `structuredContent` copy, and keys whose value is null are
  removed (absent and null mean the same thing to a reader).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import Tool, ToolResult
from mcp import types as mt

logger = logging.getLogger(__name__)

_NULL = {"type": "null"}


def slim_schema(node: Any) -> Any:
    """Return a copy of a JSON schema with nullable noise removed."""
    if isinstance(node, list):
        return [slim_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {k: slim_schema(v) for k, v in node.items() if k != "title"}
    variants = out.get("anyOf")
    if isinstance(variants, list) and _NULL in variants:
        rest = [v for v in variants if v != _NULL]
        if len(rest) == 1:
            out.pop("anyOf")
            out = {**rest[0], **out}
    if "default" in out and out["default"] is None:
        out.pop("default")
    return out


def drop_nulls(value: Any) -> Any:
    """Recursively remove dict keys whose value is None. List items are kept."""
    if isinstance(value, dict):
        return {k: drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [drop_nulls(v) for v in value]
    return value


def slim_result(result: ToolResult) -> ToolResult:
    """One compact JSON text block, nulls removed, no structured duplicate."""
    blocks = result.content
    if len(blocks) != 1 or not isinstance(blocks[0], mt.TextContent):
        return result
    try:
        payload = json.loads(blocks[0].text)
    except ValueError:
        return result  # plain-text result: nothing to compact
    text = json.dumps(drop_nulls(payload), separators=(",", ":"), ensure_ascii=False)
    return ToolResult(content=[mt.TextContent(type="text", text=text)], meta=result.meta)


class TokenSlimMiddleware(Middleware):
    """Applies `slim_schema` to the tool list and `slim_result` to tool calls."""

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        return [
            t.model_copy(update={"parameters": slim_schema(t.parameters), "output_schema": None})
            for t in tools
        ]

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        return slim_result(await call_next(context))
