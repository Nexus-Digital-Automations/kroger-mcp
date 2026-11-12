"""Tests for the token-saving middleware (kroger_mcp.token_slim)."""

from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any

import pytest
from fastmcp.tools.tool import ToolResult
from mcp import types as mt

from kroger_mcp.token_slim import drop_nulls, slim_result, slim_schema


def test_slim_schema_collapses_nullable_and_drops_noise():
    schema = {
        "title": "favoritesArguments",
        "type": "object",
        "properties": {
            "action": {"title": "Action", "enum": ["a", "b"], "type": "string"},
            "name": {
                "title": "Name",
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
                "description": "List name",
            },
            "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": 10},
            "either": {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]},
        },
        "required": ["action"],
    }
    assert slim_schema(schema) == {
        "type": "object",
        "properties": {
            "action": {"enum": ["a", "b"], "type": "string"},
            "name": {"type": "string", "description": "List name"},
            "limit": {"type": "integer", "default": 10},
            # More than one non-null variant: left as a union.
            "either": {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]},
        },
        "required": ["action"],
    }


def test_slim_schema_does_not_mutate_input():
    schema = {"title": "X", "anyOf": [{"type": "string"}, {"type": "null"}]}
    slim_schema(schema)
    assert schema == {"title": "X", "anyOf": [{"type": "string"}, {"type": "null"}]}


def test_drop_nulls_is_recursive_and_keeps_list_items():
    value = {"a": None, "b": {"c": None, "d": 0, "e": False}, "f": [None, {"g": None}], "h": ""}
    assert drop_nulls(value) == {"b": {"d": 0, "e": False}, "f": [None, {}], "h": ""}


def _text_result(payload: Any) -> ToolResult:
    text = json.dumps(payload, indent=2)
    return ToolResult(
        content=[mt.TextContent(type="text", text=text)],
        structured_content=payload if isinstance(payload, dict) else None,
    )


def test_slim_result_compacts_json_and_drops_structured_copy():
    out = slim_result(_text_result({"success": True, "item": None, "items": [1, 2]}))
    assert out.structured_content is None
    assert len(out.content) == 1
    block = out.content[0]
    assert isinstance(block, mt.TextContent)
    assert block.text == '{"success":true,"items":[1,2]}'


def test_slim_result_keeps_unicode_readable():
    out = slim_result(_text_result({"name": "Jalapeño"}))
    block = out.content[0]
    assert isinstance(block, mt.TextContent)
    assert block.text == '{"name":"Jalapeño"}'


def test_slim_result_passes_through_non_json_and_multi_block():
    plain = ToolResult(content=[mt.TextContent(type="text", text="not json")])
    assert slim_result(plain) is plain
    image = ToolResult(content=[mt.ImageContent(type="image", data="aGk=", mimeType="image/png")])
    assert slim_result(image) is image
    two = ToolResult(
        content=[mt.TextContent(type="text", text="{}"), mt.TextContent(type="text", text="{}")]
    )
    assert slim_result(two) is two


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = importlib.import_module("kroger_mcp.analytics.database")
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "token_slim.db"))
    monkeypatch.delenv("KROGER_MCP_USER_ID", raising=False)
    db.reset_initialization()
    db.ensure_initialized()
    yield
    db.reset_initialization()


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_server_tool_list_is_slim_but_complete(fresh_db):
    from fastmcp import Client

    from kroger_mcp.server import create_server

    async def run() -> list[mt.Tool]:
        async with Client(create_server()) as client:
            return await client.list_tools()

    tools = asyncio.run(run())
    assert len(tools) == 19
    for tool in tools:
        assert tool.outputSchema is None, tool.name
        schema = tool.inputSchema
        assert schema["required"][0] == "action", tool.name
        assert schema["properties"]["action"]["enum"], tool.name
        for node in _walk(schema):
            assert "title" not in node, tool.name
            assert node.get("anyOf") != [{"type": "string"}, {"type": "null"}], tool.name
        assert tool.description, tool.name


def test_server_tool_call_returns_single_compact_json(fresh_db):
    from fastmcp import Client

    from kroger_mcp.server import create_server

    async def run() -> mt.CallToolResult:
        async with Client(create_server()) as client:
            result = await client.call_tool("info", {"action": "get_preferences"})
            return result

    result = asyncio.run(run())
    assert result.structured_content is None
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, mt.TextContent)
    payload = json.loads(block.text)
    assert block.text == json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    assert payload.get("success") is not False
    assert all(v is not None for node in _walk(payload) for v in node.values())
