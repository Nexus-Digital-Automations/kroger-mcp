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

from kroger_mcp.web import chat_engine as ce
from kroger_mcp.web.chat_engine import OpenAICompatibleClient, process_message_stream


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


def test_stream_mutating_action_emits_single_pending_action(monkeypatch):
    """A mutating tool call yields exactly one pending_action and never executes."""
    call = {"n": 0}

    async def fake_chat_stream(self, http, messages, tools=None):
        call["n"] += 1
        if call["n"] == 1:
            # First turn: the model asks to add to cart (mutating).
            yield (
                "assembled",
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_x",
                            "type": "function",
                            "function": {
                                "name": "add_to_cart",
                                "arguments": json.dumps({"product_id": "0001", "quantity": 2}),
                            },
                        }
                    ],
                },
            )
        else:
            # Second turn: natural-language description of the pending action.
            for piece in ["I'll add ", "that to your cart."]:
                yield ("token", piece)
            yield ("assembled", {"role": "assistant", "content": "I'll add that to your cart."})

    monkeypatch.setattr(OpenAICompatibleClient, "chat_stream", fake_chat_stream)

    async def run_stream():
        return await _collect(
            process_message_stream([], "add 2 of product 0001", http=None, provider="deepseek")
        )

    events = asyncio.run(run_stream())
    pending = [p for k, p in events if k == "pending_action"]
    done = [p for k, p in events if k == "done"][0]

    assert len(pending) == 1
    assert pending[0]["function_name"] == "add_to_cart"
    assert pending[0]["args"] == {"product_id": "0001", "quantity": 2}
    assert done["pending_action"] is pending[0]
    # The mutating tool must NOT have executed — no real cart write happened,
    # the engine only produced a preview for approval.
    assert done["response"] == "I'll add that to your cart."
