"""
Provider registry + OpenAI-compatible chat client, shared across layers.

Extracted from web/chat_engine.py so analytics code (e.g. the weekly draft's
Gemma selection) can call an LLM without importing from the web layer —
dependencies point inward. The web chat engine imports these same names back,
so its public surface is unchanged.

Failure contract: OpenAICompatibleClient never raises — chat() returns
{"error": True, "message": ...} and chat_stream() yields ("error", message)
for a missing key, non-200 response, timeout, or connection error.
"""

import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import requests

logger = logging.getLogger(__name__)

# Every preset below speaks the OpenAI-compatible chat-completions schema, so a
# single client serves all of them. Adding a provider = one entry here. Default
# models are the cheapest option per provider that reliably supports the tool
# calling this assistant depends on.
PROVIDER_REGISTRY: dict[str, dict[str, str]] = {
    "gemma4": {
        "label": "Gemma 4",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "model": "gemma-4-31b-it",
        "api_key_env": "GEMINI_API_KEY",
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "deepseek/deepseek-chat",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.3-70b-versatile",
        "api_key_env": "GROQ_API_KEY",
    },
    "together": {
        "label": "Together",
        "base_url": "https://api.together.xyz/v1/chat/completions",
        "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "api_key_env": "TOGETHER_API_KEY",
    },
    "mistral": {
        "label": "Mistral",
        "base_url": "https://api.mistral.ai/v1/chat/completions",
        "model": "mistral-small-latest",
        "api_key_env": "MISTRAL_API_KEY",
    },
}

DEFAULT_PROVIDER = "gemma4"


class OpenAICompatibleClient:
    """Chat-completions client for any OpenAI-compatible provider.

    Failure modes: returns {"error": True, "message": ...} (never raises) for a
    missing key, non-200 response, timeout, or connection error, so callers can
    surface the message to the user unchanged.
    """

    def __init__(self, provider_id: str):
        preset = PROVIDER_REGISTRY[provider_id]  # caller guarantees a valid id
        self.provider_id = provider_id
        self.label = preset["label"]
        self.api_url = preset["base_url"]
        self.model = preset["model"]
        self._key_env = preset["api_key_env"]
        self.api_key = os.environ.get(self._key_env, "")

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            logger.warning(
                "chat requested for provider=%s but %s is not set",
                self.provider_id,
                self._key_env,
            )
            return {
                "error": True,
                "message": (
                    f"{self.label} API key not configured. "
                    f"Add {self._key_env} to your .env file."
                ),
            }

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            resp = requests.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=45,
            )
            if resp.status_code != 200:
                body = resp.text[:300]
                logger.error(
                    "provider=%s model=%s http=%s body=%s",
                    self.provider_id,
                    self.model,
                    resp.status_code,
                    body,
                )
                return {
                    "error": True,
                    "message": f"{self.label} API error ({resp.status_code}): {body}",
                }
            return resp.json()
        except requests.Timeout:
            logger.error("provider=%s request timed out", self.provider_id)
            return {
                "error": True,
                "message": f"{self.label} API request timed out. Please try again.",
            }
        except requests.ConnectionError:
            logger.error("provider=%s connection error", self.provider_id)
            return {
                "error": True,
                "message": f"Could not connect to {self.label} API. Check your internet connection.",
            }
        except Exception as exc:
            logger.error(
                "provider=%s request failed: %s",
                self.provider_id,
                exc,
                exc_info=True,
            )
            return {"error": True, "message": f"{self.label} request failed: {str(exc)[:200]}"}

    async def chat_stream(
        self,
        http: httpx.AsyncClient,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[tuple[str, Any]]:
        """Stream a chat completion over SSE without blocking the event loop.

        Yields ``("token", text)`` for each content delta as the model produces
        it, then a terminal ``("assembled", assistant_message)`` carrying the
        full content and any reassembled ``tool_calls``. On failure yields a
        single ``("error", message)`` and stops (mirrors the sync ``chat``
        error contract — never raises to the caller).

        Tool-call arguments arrive as fragmented JSON string deltas keyed by
        ``index``; they are concatenated and only parsed by the orchestrator
        once the stream completes.
        """
        if not self.api_key:
            logger.warning(
                "chat_stream requested for provider=%s but %s is not set",
                self.provider_id,
                self._key_env,
            )
            yield (
                "error",
                f"{self.label} API key not configured. Add {self._key_env} to your .env file.",
            )
            return

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        content_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, Any]] = {}

        try:
            async with http.stream(
                "POST",
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")[:300]
                    logger.error(
                        "provider=%s model=%s http=%s body=%s",
                        self.provider_id,
                        self.model,
                        resp.status_code,
                        body,
                    )
                    yield ("error", f"{self.label} API error ({resp.status_code}): {body}")
                    return

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        content_parts.append(text)
                        yield ("token", text)
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = tool_calls_by_index.setdefault(
                            idx, {"id": None, "name": None, "arguments": ""}
                        )
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
        except httpx.TimeoutException:
            logger.error("provider=%s stream timed out", self.provider_id)
            yield ("error", f"{self.label} API request timed out. Please try again.")
            return
        except httpx.HTTPError as exc:
            logger.error("provider=%s stream failed: %s", self.provider_id, exc, exc_info=True)
            yield ("error", f"Could not reach {self.label} API: {str(exc)[:200]}")
            return

        # Reassemble the full assistant message from the streamed deltas.
        content = "".join(content_parts)
        assembled: dict[str, Any] = {"role": "assistant", "content": content or None}
        if tool_calls_by_index:
            assembled["tool_calls"] = [
                {
                    "id": tool_calls_by_index[idx]["id"] or f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": tool_calls_by_index[idx]["name"] or "",
                        "arguments": tool_calls_by_index[idx]["arguments"] or "{}",
                    },
                }
                for idx in sorted(tool_calls_by_index)
            ]
        yield ("assembled", assembled)


_client_cache: dict[str, OpenAICompatibleClient] = {}


def get_client(provider_id: str | None = None) -> OpenAICompatibleClient:
    """Return a cached client for provider_id, falling back to DEFAULT_PROVIDER.

    An unknown id is treated as the default (logged at WARNING) so a stale or
    malformed frontend selection degrades gracefully instead of erroring.
    """
    pid = provider_id or DEFAULT_PROVIDER
    if pid not in PROVIDER_REGISTRY:
        logger.warning("unknown provider=%r; falling back to %s", pid, DEFAULT_PROVIDER)
        pid = DEFAULT_PROVIDER
    if pid not in _client_cache:
        _client_cache[pid] = OpenAICompatibleClient(pid)
    return _client_cache[pid]


def list_available_providers() -> list[dict[str, str]]:
    """Providers whose API key is configured. Never exposes the keys themselves."""
    available: list[dict[str, str]] = []
    for pid, preset in PROVIDER_REGISTRY.items():
        if os.environ.get(preset["api_key_env"], "").strip():
            available.append({"id": pid, "label": preset["label"], "model": preset["model"]})
    return available
