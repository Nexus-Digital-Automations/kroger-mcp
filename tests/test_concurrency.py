"""Regression test for the headline Phase 1 fix: a slow chat must not block
other requests on the event loop.

Before Phase 1, the async chat handler called the SYNC process_message (which
did a blocking requests.post) directly on the event loop — one slow chat stalled
every other request. The fix streams via async httpx and offloads blocking tool
work to threads. This test pins the difference: with the old sync path a
concurrent quick request is delayed until the chat finishes; with the streaming
path it is served immediately.
"""

from __future__ import annotations

import asyncio
import time

from kroger_mcp.web.chat_engine import (
    OpenAICompatibleClient,
    process_message,
    process_message_stream,
)

_DELAY = 0.25  # simulated slow LLM round-trip


def test_streaming_chat_does_not_block_concurrent_requests(monkeypatch):
    # Slow SYNC provider — mimics the pre-fix blocking requests.post.
    def slow_chat(self, messages, tools=None):
        time.sleep(_DELAY)
        return {"choices": [{"message": {"content": "ok"}}]}

    # Slow ASYNC streaming provider — awaits, so the loop stays free.
    async def slow_chat_stream(self, http, messages, tools=None):
        await asyncio.sleep(_DELAY)
        yield ("assembled", {"role": "assistant", "content": "ok"})

    monkeypatch.setattr(OpenAICompatibleClient, "chat", slow_chat)
    monkeypatch.setattr(OpenAICompatibleClient, "chat_stream", slow_chat_stream)

    async def scenario(*, streaming: bool) -> list[str]:
        order: list[str] = []

        async def chat_request() -> None:
            if streaming:
                async for _ in process_message_stream([], "hi", http=None, provider="deepseek"):
                    pass
            else:
                # The pre-fix shape: a sync call sitting directly on the loop.
                process_message([], "hi", provider="deepseek")
            order.append("chat")

        async def quick_request() -> None:
            await asyncio.sleep(0.02)  # a fast endpoint (~20ms)
            order.append("quick")

        await asyncio.gather(chat_request(), quick_request())
        return order

    blocking_order = asyncio.run(scenario(streaming=False))
    streaming_order = asyncio.run(scenario(streaming=True))

    # Old path: the sync chat monopolises the loop for _DELAY, so the 20ms
    # request can't run until the chat completes.
    assert blocking_order == ["chat", "quick"]
    # Fixed path: the streamed chat awaits, so the quick request finishes first.
    assert streaming_order == ["quick", "chat"]
