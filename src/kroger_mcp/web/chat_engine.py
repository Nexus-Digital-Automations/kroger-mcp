"""
Multi-provider chat engine for Smart Shopper.

Provides:
- The full MCP tool surface (see mcp_bridge) exposed as OpenAI-compatible
  function-calling tools, with per-call risk tiering (see risk_policy)
- A generic OpenAI-compatible client + provider registry (Gemma 4, DeepSeek,
  OpenAI, OpenRouter, Groq, Together, Mistral) with per-provider env-var API keys
- Conversation orchestrator with mode-aware approval flow: read-only calls
  always auto-execute, hard-blocked calls always confirm, and "write" calls
  confirm in Ask mode / auto-execute in Auto mode
"""

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import requests

from ..auth.dependencies import reset_web_user_id, set_web_user_id
from . import mcp_bridge, risk_policy

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Smart Shopper's personal chef and grocery assistant. You help users manage their Kroger grocery shopping through conversation.

Your personality:
- Culinary expert with knowledge of food history, cultural traditions, and flavor science
- Health-focused: prioritize whole foods, minimize ultra-processed items
- Flavor first: never sacrifice taste for convenience
- Knowledgeable but never pretentious
- Concise and helpful

Your store: Kroger — 336 North Loop, Conroe, TX (Location ID: 03400014)

Guidelines:
- Always check product safety before recommending items for purchase
- Suggest seasonal produce when relevant
- When users want to modify their cart, pantry, recipes, or meal plans, describe the action clearly so they can approve it
- Format results clearly with prices and safety info when available
- Be conversational and friendly, not robotic
- If a search returns no results, suggest alternative search terms
- When listing products, include price and brand"""

# ---------------------------------------------------------------------------
# LLM providers (OpenAI-compatible /chat/completions)
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 30  # Truncate to prevent token overflow

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


# ---------------------------------------------------------------------------
# Tool surface
#
# The chatbot's capabilities are the real MCP tools registered in server.py,
# fetched and invoked in-process via mcp_bridge — see that module for why
# (single source of truth instead of a hand-maintained, drifting subset).
# ---------------------------------------------------------------------------


_TOOLS_ARRAY: list[dict[str, Any]] | None = None
_TOOLS_ARRAY_LOCK = asyncio.Lock()


async def _ensure_tools_array() -> list[dict[str, Any]]:
    """Fetch and cache the full MCP tool surface, converted for tool calling.

    Built lazily (not at import time) since fetching real tool schemas
    requires an async round-trip through mcp_bridge; cached for the process
    lifetime since the registered MCP tools don't change at runtime.
    """
    global _TOOLS_ARRAY
    if _TOOLS_ARRAY is None:
        async with _TOOLS_ARRAY_LOCK:
            if _TOOLS_ARRAY is None:
                _TOOLS_ARRAY = await mcp_bridge.list_openai_tools()
    return _TOOLS_ARRAY


# ---------------------------------------------------------------------------
# Preview generation for mutating actions
# ---------------------------------------------------------------------------

_ACTION_LABELS: dict[tuple[str, str], str] = {
    ("cart", "add"): "Add to cart",
    ("cart", "remove"): "Remove from cart",
    ("cart", "clear"): "Clear entire cart",
    ("cart", "mark_placed"): "Mark order as placed",
    ("pantry", "add"): "Add to pantry",
    ("pantry", "update_item"): "Update pantry level",
    ("pantry", "remove"): "Remove from pantry",
    ("recipes", "delete"): "Delete recipe",
    ("recipes", "add_to_cart"): "Add recipe to cart",
    ("shopping_list", "add_to_cart"): "Add shopping list to cart",
    ("meal_plan", "add_to_cart"): "Add meal plan ingredients to cart",
    ("favorites", "order"): "Order favorites list",
    ("info", "set_servings"): "Set household servings",
    ("privacy", "delete_my_data"): "Delete shared data",
}


def _recipe_name(recipe_id: str | None) -> str | None:
    """Best-effort recipe-name lookup for a preview; None on any failure."""
    if not recipe_id:
        return None
    try:
        from kroger_mcp.tools.recipe_tools import _load_recipes

        data = _load_recipes()
        recipe = next((r for r in data.get("recipes", []) if r.get("id") == recipe_id), None)
        return recipe.get("name") if recipe else None
    except Exception:
        return None


def _generate_preview(tool_name: str, action: str | None, args: dict[str, Any]) -> dict[str, Any]:
    """Generate a human-readable preview for a write or hard-blocked tool call.

    Hand-tunes the highest-traffic (tool, action) pairs for good UX; every
    other action falls back to a generic dump of its arguments.
    """
    key = (tool_name, action or "")
    label = _ACTION_LABELS.get(key, (action or tool_name).replace("_", " ").title())
    details: dict[str, Any] = {}

    if key == ("cart", "add"):
        if args.get("items"):
            details["items"] = args["items"]
        else:
            details["product_id"] = args.get("product_id", "")
            details["quantity"] = args.get("quantity", 1)
            details["modality"] = args.get("modality", "PICKUP")

    elif key == ("cart", "remove"):
        details["product_id"] = args.get("product_id", "")

    elif key == ("pantry", "add"):
        details["product_id"] = args.get("product_id") or args.get("product_ids", "")
        details["level"] = f"{args.get('level', 100)}%"
        desc = args.get("description", "")
        if desc:
            details["item"] = desc

    elif key == ("pantry", "update_item"):
        details["product_id"] = args.get("product_id", "")
        details["new_level"] = f"{args.get('level', 0)}%"

    elif key == ("pantry", "remove"):
        details["product_id"] = args.get("product_id") or args.get("product_ids", "")

    elif key == ("recipes", "delete"):
        details["recipe_id"] = args.get("recipe_id", "")
        name = _recipe_name(args.get("recipe_id"))
        if name:
            details["recipe_name"] = name

    elif key in (("recipes", "add_to_cart"), ("shopping_list", "add_to_cart")):
        if args.get("recipe_id"):
            details["recipe_id"] = args["recipe_id"]
            name = _recipe_name(args.get("recipe_id"))
            if name:
                details["recipe_name"] = name
        if args.get("servings"):
            details["servings"] = args["servings"]

    elif key == ("meal_plan", "add_to_cart"):
        details["plan_id"] = args.get("plan_id", "")

    elif key == ("favorites", "order"):
        details["list_id"] = args.get("list_id", "default")

    elif key == ("info", "set_servings"):
        details["servings"] = args.get("servings", 0)

    elif key == ("privacy", "delete_my_data"):
        details["warning"] = "Permanently deletes your shared anonymized data."

    else:
        # Generic fallback: every arg except the action selector itself.
        details = {k: v for k, v in args.items() if k != "action" and v is not None}

    return {"action": label, "details": details}


# ---------------------------------------------------------------------------
# Conversation orchestrator
# ---------------------------------------------------------------------------


def _truncate_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep history within token limits by trimming older messages."""
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages
    # Always keep the system prompt (first message) and the most recent messages
    system = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]
    return system + non_system[-(MAX_HISTORY_MESSAGES - len(system)) :]


def process_message(
    messages: list[dict[str, Any]],
    user_message: str,
    provider: str | None = None,
) -> dict[str, Any]:
    """
    Process a plain-text chat message through the selected LLM provider.

    This sync path does not support tool calling (the real tool surface is
    fetched asynchronously via mcp_bridge — see process_message_stream, the
    only production code path). It exists solely as the pre-streaming
    baseline used by the event-loop-blocking regression test
    (tests/test_concurrency.py); called with tools=None so a provider never
    has anything to call.

    Args:
        messages: Conversation history (role/content dicts).
        user_message: The new user message.
        provider: Provider id (see PROVIDER_REGISTRY); None → DEFAULT_PROVIDER.

    Returns:
        {response: str, messages: [...], pending_action: None}
    """
    client = get_client(provider)
    logger.info("process_message provider=%s model=%s", client.provider_id, client.model)

    full_messages: list[dict[str, Any]] = []
    has_system = any(m.get("role") == "system" for m in messages)
    if not has_system:
        full_messages.append({"role": "system", "content": SYSTEM_PROMPT})
    for m in messages:
        if m.get("role") == "system" and not has_system:
            continue
        full_messages.append(m)
    full_messages.append({"role": "user", "content": user_message})
    full_messages = _truncate_history(full_messages)

    result = client.chat(full_messages, tools=None)

    if result.get("error"):
        error_msg = result.get("message", "Unknown error")
        full_messages.append({"role": "assistant", "content": error_msg})
        return {"response": error_msg, "messages": full_messages, "pending_action": None}

    try:
        choice = result["choices"][0]["message"]
    except (KeyError, IndexError):
        err = f"Unexpected response format from {client.label}."
        full_messages.append({"role": "assistant", "content": err})
        return {"response": err, "messages": full_messages, "pending_action": None}

    content = choice.get("content", "")
    full_messages.append({"role": "assistant", "content": content})
    return {"response": content, "messages": full_messages, "pending_action": None}


async def process_message_stream(
    messages: list[dict[str, Any]],
    user_message: str,
    http: httpx.AsyncClient,
    provider: str | None = None,
    mode: str = "ask",
    conversation_id: str | None = None,
) -> AsyncIterator[tuple[str, Any]]:
    """Streaming counterpart of :func:`process_message`.

    Async generator yielding event tuples consumed by the SSE route:
      - ``("token", text)``        incremental assistant text
      - ``("pending_action", a)``  an action awaiting approval (at most one)
      - ``("done", result)``       terminal payload {response, messages, pending_action}
      - ``("error", message)``     provider/setup failure (terminal)

    Preserves the sync orchestrator's 3-call tool-use structure. Every tool
    call is classified by risk_policy.classify() into read_only (always
    auto-executes), hard_blocked (always confirms, any mode), or write
    (confirms in "ask" mode, auto-executes in "auto" mode). Auto-executed
    calls dispatch through mcp_bridge, which invokes the real MCP tool
    in-process, off the event loop.
    """
    client = get_client(provider)
    logger.info("process_message_stream provider=%s model=%s", client.provider_id, client.model)
    tools_array = await _ensure_tools_array()

    full_messages: list[dict[str, Any]] = []
    has_system = any(m.get("role") == "system" for m in messages)
    if not has_system:
        full_messages.append({"role": "system", "content": SYSTEM_PROMPT})
    for m in messages:
        if m.get("role") == "system" and not has_system:
            continue
        full_messages.append(m)
    full_messages.append({"role": "user", "content": user_message})
    full_messages = _truncate_history(full_messages)

    # --- Call 1: stream the model's first turn -----------------------------
    assembled: dict[str, Any] | None = None
    async for kind, payload in client.chat_stream(http, full_messages, tools=tools_array):
        if kind == "token":
            yield ("token", payload)
        elif kind == "error":
            full_messages.append({"role": "assistant", "content": payload})
            yield ("done", {"response": payload, "messages": full_messages, "pending_action": None})
            return
        elif kind == "assembled":
            assembled = payload

    if assembled is None:
        err = f"Unexpected response from {client.label}."
        full_messages.append({"role": "assistant", "content": err})
        yield ("done", {"response": err, "messages": full_messages, "pending_action": None})
        return

    tool_calls = assembled.get("tool_calls")
    if not tool_calls:
        # Plain text reply — already streamed; finalize.
        content = assembled.get("content") or ""
        full_messages.append({"role": "assistant", "content": content})
        yield ("done", {"response": content, "messages": full_messages, "pending_action": None})
        return

    # --- Tool call(s): auto-execute per risk tier, gate the rest -----------
    full_messages.append(assembled)
    pending_action: dict[str, Any] | None = None
    tool_results: list[dict[str, Any]] = []

    for tool_call in tool_calls:
        fn_name = tool_call["function"]["name"]
        try:
            fn_args = json.loads(tool_call["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            fn_args = {}

        tool_id = tool_call.get("id", f"call_{uuid.uuid4().hex[:12]}")
        tier = risk_policy.classify(fn_name, fn_args.get("action"), fn_args)
        auto_execute = tier == "read_only" or (tier == "write" and mode == "auto")

        if not auto_execute:
            if pending_action is not None:
                # Only one pending_action is supported per turn; any further
                # gated calls in this turn stay unresolved until re-prompted.
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": json.dumps({"status": "awaiting_user_approval"}),
                    }
                )
                continue
            preview = _generate_preview(fn_name, fn_args.get("action"), fn_args)
            pending_action = {
                "id": f"act_{uuid.uuid4().hex[:12]}",
                "function_name": fn_name,
                "args": fn_args,
                "tool_call_id": tool_id,
                "description": preview["action"],
                "preview": preview["details"],
            }
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": json.dumps(
                        {"status": "awaiting_user_approval", "action": preview["action"]}
                    ),
                }
            )
        else:
            try:
                tool_result = await mcp_bridge.call_tool(
                    conversation_id or "default", fn_name, fn_args
                )
            except Exception as exc:
                tool_result = {"error": f"Tool error: {str(exc)[:200]}"}

            result_str = json.dumps(tool_result, default=str)
            if len(result_str) > 4000:
                result_str = result_str[:4000] + "... (truncated)"
            tool_results.append(
                {"role": "tool", "tool_call_id": tool_id, "content": result_str}
            )

    full_messages.extend(tool_results)

    # --- Call 2: describe a pending mutating action ------------------------
    if pending_action:
        described: list[str] = []
        async for kind, payload in client.chat_stream(http, full_messages, tools=tools_array):
            if kind == "token":
                described.append(payload)
                yield ("token", payload)
        follow_content = "".join(described)
        if not follow_content:
            follow_content = (
                f"I'd like to **{pending_action['description']}**. Please review and approve."
            )
            yield ("token", follow_content)
        full_messages.append({"role": "assistant", "content": follow_content})
        yield ("pending_action", pending_action)
        yield (
            "done",
            {
                "response": follow_content,
                "messages": full_messages,
                "pending_action": pending_action,
            },
        )
        return

    # --- Call 3: summarize read-only tool results --------------------------
    summary: list[str] = []
    async for kind, payload in client.chat_stream(http, full_messages, tools=tools_array):
        if kind == "token":
            summary.append(payload)
            yield ("token", payload)
    content = "".join(summary)
    if not content:
        content = "I found some results but had trouble generating a summary."
        yield ("token", content)
    full_messages.append({"role": "assistant", "content": content})
    yield ("done", {"response": content, "messages": full_messages, "pending_action": None})


# (tool, action) pairs whose real MCP tool defaults to a harmless preview and
# only actually writes when the caller sets this execute flag — approving the
# pending_action must force it on, or the "approved" call would just preview
# again and never execute.
_EXECUTE_FLAG_OVERRIDES: dict[tuple[str, str], tuple[str, Any]] = {
    ("cart", "add"): ("preview_only", False),
    ("recipes", "add_to_cart"): ("confirm", True),
    ("shopping_list", "add_to_cart"): ("confirm", True),
    ("meal_plan", "add_to_cart"): ("confirm", True),
    ("favorites", "order"): ("confirm", True),
}


async def execute_approved_action(
    function_name: str,
    args: dict[str, Any],
    user_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """
    Execute a previously approved tool call against the real MCP tool surface.

    Returns:
        {success: bool, result: {...}, summary: str}
    """
    action = args.get("action")
    exec_args = dict(args)
    flag = _EXECUTE_FLAG_OVERRIDES.get((function_name, action or ""))
    if flag:
        exec_args[flag[0]] = flag[1]

    token = set_web_user_id(user_id)
    try:
        result = await mcp_bridge.call_tool(conversation_id or "default", function_name, exec_args)
    except Exception as exc:
        return {"success": False, "result": {}, "summary": f"Action failed: {str(exc)[:200]}"}
    finally:
        reset_web_user_id(token)

    if isinstance(result, dict) and result.get("success") is False:
        return {
            "success": False,
            "result": result,
            "summary": result.get("error") or result.get("message") or "Action failed.",
        }

    label = _ACTION_LABELS.get((function_name, action or ""), (action or function_name))
    summary = f"{label} completed successfully."

    if (function_name, action) == ("cart", "add"):
        qty = exec_args.get("quantity", 1)
        summary = f"Added item (x{qty}) to your cart."
    elif (function_name, action) == ("cart", "remove"):
        summary = "Removed item from your cart."
    elif (function_name, action) == ("cart", "clear"):
        summary = "Cart has been cleared."
    elif (function_name, action) == ("cart", "mark_placed"):
        summary = "Order marked as placed."
    elif (function_name, action) == ("pantry", "add"):
        summary = f"Added item to pantry at {exec_args.get('level', 100)}%."
    elif (function_name, action) == ("pantry", "update_item"):
        summary = f"Updated pantry level to {exec_args.get('level', 0)}%."
    elif (function_name, action) == ("pantry", "remove"):
        summary = "Removed item from pantry."
    elif (function_name, action) == ("recipes", "delete"):
        summary = "Recipe deleted."
    elif (function_name, action) in (("recipes", "add_to_cart"), ("shopping_list", "add_to_cart")):
        added = isinstance(result, dict) and (result.get("added") or result.get("added_count"))
        summary = f"Added {added or 'the'} ingredients to your cart." if added else summary
    elif (function_name, action) == ("meal_plan", "add_to_cart"):
        summary = "Added meal plan ingredients to your cart."
    elif (function_name, action) == ("favorites", "order"):
        summary = "Favorites list ordered to your cart."
    elif (function_name, action) == ("info", "set_servings"):
        summary = f"Household servings set to {exec_args.get('servings', 0)}."
    elif (function_name, action) == ("privacy", "delete_my_data"):
        summary = "Your shared data has been deleted."

    return {"success": True, "result": result, "summary": summary}
