"""Critical-path tests for the SSE streaming chat engine.

Covers the two riskiest parts of the event-loop-unblock rewrite:
  1. chat_stream's SSE delta parsing — content reassembly AND fragmented
     tool-call argument concatenation by index.
  2. process_message_stream equivalence with the legacy sync process_message
     for the user-visible outcome (response text, pending_action, history),
     using mocked providers so no real LLM is called.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from kroger_mcp.web import chat_engine as ce
from kroger_mcp.web.chat_engine import OpenAICompatibleClient, process_message_stream


@pytest.fixture(autouse=True)
def _stub_tools_array(monkeypatch):
    """Avoid spinning up the real FastMCP server (DB migration, tool
    introspection) for every test — process_message_stream now fetches the
    tool surface via mcp_bridge on first use. Tests that care about the real
    surface (test_tool_surface_covers_all_registered_mcp_tools) do it explicitly.
    """

    async def _fake_tools_array():
        return []

    monkeypatch.setattr(ce, "_ensure_tools_array", _fake_tools_array)


def _sse_body(chunks: list[dict]) -> bytes:
    """Render OpenAI-style streamed chat-completion chunks as an SSE body."""
    lines = [f"data: {json.dumps(c)}" for c in chunks]
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def _mock_http(body: bytes) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _collect(agen):
    return [ev async for ev in agen]


def test_chat_stream_reassembles_text_tokens():
    body = _sse_body(
        [
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": ", chef"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
    )

    async def run():
        client = OpenAICompatibleClient("deepseek")
        client.api_key = "test-key"  # bypass the missing-key guard
        async with _mock_http(body) as http:
            return await _collect(client.chat_stream(http, [{"role": "user", "content": "hi"}]))

    events = asyncio.run(run())
    tokens = [p for k, p in events if k == "token"]
    assembled = [p for k, p in events if k == "assembled"][0]
    assert tokens == ["Hello", ", chef"]
    assert assembled["content"] == "Hello, chef"
    assert "tool_calls" not in assembled


def test_chat_stream_concatenates_fragmented_tool_call_args():
    # Tool-call arguments arrive split across deltas keyed by index — the
    # parser must concatenate them into one valid JSON string.
    body = _sse_body(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "search_products", "arguments": '{"sea'},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'rch_term": "milk"}'}}]}}
                ]
            },
        ]
    )

    async def run():
        client = OpenAICompatibleClient("deepseek")
        client.api_key = "test-key"
        async with _mock_http(body) as http:
            return await _collect(client.chat_stream(http, [{"role": "user", "content": "find milk"}]))

    events = asyncio.run(run())
    assembled = [p for k, p in events if k == "assembled"][0]
    assert assembled["content"] is None
    tc = assembled["tool_calls"][0]
    assert tc["id"] == "call_1"
    assert tc["function"]["name"] == "search_products"
    # The two fragments must form one parseable arguments object.
    assert json.loads(tc["function"]["arguments"]) == {"search_term": "milk"}


def test_chat_stream_surfaces_non_200_as_error_event():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"unauthorized")

    async def run():
        client = OpenAICompatibleClient("deepseek")
        client.api_key = "test-key"
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await _collect(client.chat_stream(http, [{"role": "user", "content": "hi"}]))

    events = asyncio.run(run())
    assert events[0][0] == "error"
    assert "401" in events[0][1]


def test_stream_plaintext_matches_sync(monkeypatch):
    """For a plain-text reply, the streamed outcome matches the sync engine."""
    reply = "Tomatoes are in season now."

    # Sync path returns the canonical non-streaming response.
    def fake_chat(self, messages, tools=None):
        return {"choices": [{"message": {"content": reply}}]}

    # Streaming path yields the equivalent tokens + assembled message.
    async def fake_chat_stream(self, http, messages, tools=None):
        for piece in ["Tomatoes ", "are in ", "season now."]:
            yield ("token", piece)
        yield ("assembled", {"role": "assistant", "content": reply})

    monkeypatch.setattr(OpenAICompatibleClient, "chat", fake_chat)
    monkeypatch.setattr(OpenAICompatibleClient, "chat_stream", fake_chat_stream)

    sync_result = ce.process_message([], "what's fresh?", provider="deepseek")

    async def run_stream():
        events = await _collect(process_message_stream([], "what's fresh?", http=None, provider="deepseek"))
        tokens = "".join(p for k, p in events if k == "token")
        done = [p for k, p in events if k == "done"][0]
        return tokens, done

    tokens, done = asyncio.run(run_stream())

    assert tokens == reply  # streamed text equals the full reply
    assert done["response"] == sync_result["response"] == reply
    assert done["pending_action"] is None and sync_result["pending_action"] is None
    # Final assistant turn matches.
    assert done["messages"][-1] == {"role": "assistant", "content": reply}
    assert sync_result["messages"][-1] == {"role": "assistant", "content": reply}


def _one_tool_call_then_text(tool_name: str, tool_args: dict, follow_text: str):
    """Build a fake chat_stream: turn 1 emits one tool call, turn 2 free text."""
    call = {"n": 0}

    async def fake_chat_stream(self, http, messages, tools=None):
        call["n"] += 1
        if call["n"] == 1:
            yield (
                "assembled",
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_x",
                            "type": "function",
                            "function": {"name": tool_name, "arguments": json.dumps(tool_args)},
                        }
                    ],
                },
            )
        else:
            words = follow_text.split(" ")
            for i, piece in enumerate(words):
                yield ("token", piece if i == len(words) - 1 else piece + " ")
            yield ("assembled", {"role": "assistant", "content": follow_text})

    return fake_chat_stream


def test_stream_mutating_action_emits_single_pending_action(monkeypatch):
    """A hard-blocked tool call yields exactly one pending_action and never executes."""
    fake_chat_stream = _one_tool_call_then_text(
        "cart",
        {"action": "add", "product_id": "0001", "quantity": 2, "preview_only": False},
        "I'll add that to your cart.",
    )
    monkeypatch.setattr(OpenAICompatibleClient, "chat_stream", fake_chat_stream)

    async def run_stream():
        return await _collect(
            process_message_stream([], "add 2 of product 0001", http=None, provider="deepseek")
        )

    events = asyncio.run(run_stream())
    pending = [p for k, p in events if k == "pending_action"]
    done = [p for k, p in events if k == "done"][0]

    assert len(pending) == 1
    assert pending[0]["function_name"] == "cart"
    assert pending[0]["args"]["action"] == "add"
    assert done["pending_action"] is pending[0]
    # The hard-blocked tool must NOT have executed — no real cart write
    # happened, the engine only produced a preview for approval.
    assert done["response"] == "I'll add that to your cart."


def test_cart_preview_only_auto_executes_without_approval(monkeypatch):
    """A cart(add, preview_only=True) call is read-only and never gates."""
    fake_chat_stream = _one_tool_call_then_text(
        "cart",
        {"action": "add", "product_id": "0001", "quantity": 1, "preview_only": True},
        "Here's a preview of that item.",
    )
    monkeypatch.setattr(OpenAICompatibleClient, "chat_stream", fake_chat_stream)

    async def fake_call_tool(conversation_id, name, args):
        return {"success": True, "preview": True}

    monkeypatch.setattr(ce.mcp_bridge, "call_tool", fake_call_tool)

    async def run_stream():
        return await _collect(
            process_message_stream([], "preview adding item 0001", http=None, provider="deepseek")
        )

    events = asyncio.run(run_stream())
    pending = [p for k, p in events if k == "pending_action"]
    done = [p for k, p in events if k == "done"][0]

    assert pending == []
    assert done["pending_action"] is None


def test_mode_ask_confirms_normal_write(monkeypatch):
    """A write-tier call (pantry add) confirms in Ask mode."""
    fake_chat_stream = _one_tool_call_then_text(
        "pantry", {"action": "add", "product_id": "0002", "level": 100}, "Added to pantry."
    )
    monkeypatch.setattr(OpenAICompatibleClient, "chat_stream", fake_chat_stream)

    async def run_stream():
        return await _collect(
            process_message_stream(
                [], "add milk to pantry", http=None, provider="deepseek", mode="ask"
            )
        )

    events = asyncio.run(run_stream())
    pending = [p for k, p in events if k == "pending_action"]
    assert len(pending) == 1
    assert pending[0]["function_name"] == "pantry"


def test_mode_auto_executes_normal_write(monkeypatch):
    """The same write-tier call auto-executes in Auto mode, no approval step."""
    fake_chat_stream = _one_tool_call_then_text(
        "pantry", {"action": "add", "product_id": "0002", "level": 100}, "Added to pantry."
    )
    monkeypatch.setattr(OpenAICompatibleClient, "chat_stream", fake_chat_stream)

    async def fake_call_tool(conversation_id, name, args):
        return {"success": True}

    monkeypatch.setattr(ce.mcp_bridge, "call_tool", fake_call_tool)

    async def run_stream():
        return await _collect(
            process_message_stream(
                [], "add milk to pantry", http=None, provider="deepseek", mode="auto"
            )
        )

    events = asyncio.run(run_stream())
    pending = [p for k, p in events if k == "pending_action"]
    done = [p for k, p in events if k == "done"][0]
    assert pending == []
    assert done["pending_action"] is None


def test_hard_blocked_always_confirms_even_in_auto_mode(monkeypatch):
    """cart(clear) still confirms in Auto mode — hard-blocked has no exceptions."""
    fake_chat_stream = _one_tool_call_then_text("cart", {"action": "clear"}, "Clearing your cart.")
    monkeypatch.setattr(OpenAICompatibleClient, "chat_stream", fake_chat_stream)

    async def run_stream():
        return await _collect(
            process_message_stream(
                [], "clear my cart", http=None, provider="deepseek", mode="auto"
            )
        )

    events = asyncio.run(run_stream())
    pending = [p for k, p in events if k == "pending_action"]
    assert len(pending) == 1
    assert pending[0]["function_name"] == "cart"
    assert pending[0]["args"]["action"] == "clear"


def test_tool_surface_covers_all_registered_mcp_tools():
    """mcp_bridge exposes every tool the real MCP server registers — no drift."""
    from fastmcp import Client

    from kroger_mcp.server import create_server
    from kroger_mcp.web import mcp_bridge

    async def run():
        async with Client(create_server()) as client:
            expected = {t.name for t in await client.list_tools()}
        actual = {t["function"]["name"] for t in await mcp_bridge.list_openai_tools()}
        return expected, actual

    expected, actual = asyncio.run(run())
    assert actual == expected
    assert len(expected) >= 18


def test_execute_approved_action_scopes_to_web_user(monkeypatch):
    """An approved action runs as the requesting web user, not the MCP default."""
    from kroger_mcp.auth.dependencies import mcp_user_id

    captured = {}

    async def fake_call_tool(conversation_id, name, args):
        captured["user_id"] = mcp_user_id()
        return {"success": True}

    monkeypatch.setattr(ce.mcp_bridge, "call_tool", fake_call_tool)

    result = asyncio.run(
        ce.execute_approved_action(
            function_name="pantry",
            args={"action": "add", "product_id": "0001"},
            user_id="user-123",
            conversation_id="c1",
        )
    )

    assert captured["user_id"] == "user-123"
    assert result["success"] is True
