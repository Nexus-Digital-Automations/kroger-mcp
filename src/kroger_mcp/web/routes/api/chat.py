"""Chat API endpoints — multi-provider grocery assistant."""

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from kroger_mcp.auth.dependencies import current_user_id, reset_web_user_id, set_web_user_id
from kroger_mcp.web.chat_engine import (
    DEFAULT_PROVIDER,
    execute_approved_action,
    list_available_providers,
    process_message_stream,
)

router = APIRouter()

_VALID_MODES = {"ask", "auto"}


# -------------------------------------------------------------------
# Request models
# -------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: str
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class ChatMessageRequest(BaseModel):
    messages: list[dict[str, Any]] = []
    user_message: str
    provider: str | None = None  # None → server default (Gemma 4)
    conversation_id: str = "default"
    mode: str = "ask"


class ChatApproveRequest(BaseModel):
    id: str
    function_name: str
    args: dict[str, Any] = {}
    conversation_id: str = "default"


class ChatRejectRequest(BaseModel):
    id: str


# -------------------------------------------------------------------
# Endpoints
# -------------------------------------------------------------------


@router.get("/api/chat/providers")
async def chat_providers():
    """List selectable LLM providers (only those with a configured API key).

    API keys are read server-side and never included in the response.
    """
    return JSONResponse(
        content={
            "providers": list_available_providers(),
            "default": DEFAULT_PROVIDER,
        }
    )


def _sse(event_type: str, payload: Any) -> str:
    """Format one Server-Sent Events frame."""
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"


async def _chat_event_stream(request: Request, body: ChatMessageRequest) -> AsyncIterator[str]:
    """Bridge the chat orchestrator's events to an SSE byte stream.

    Aborts promptly if the client disconnects (frees the upstream LLM stream),
    and converts any unexpected failure into a terminal ``error`` event rather
    than a broken connection (status is already committed once streaming).
    """
    http = request.app.state.http
    mode = body.mode if body.mode in _VALID_MODES else "ask"
    token = set_web_user_id(current_user_id(request))
    try:
        async for event_type, payload in process_message_stream(
            messages=body.messages,
            user_message=body.user_message.strip(),
            http=http,
            provider=body.provider,
            mode=mode,
            conversation_id=body.conversation_id,
        ):
            if await request.is_disconnected():
                break
            yield _sse(event_type, payload)
    except Exception as exc:  # noqa: BLE001 - terminal event, not a swallow
        yield _sse("error", {"error": f"Chat processing failed: {str(exc)[:300]}"})
    finally:
        reset_web_user_id(token)


@router.post("/api/chat/message")
async def chat_message(request: Request, body: ChatMessageRequest):
    """Stream a chat reply (SSE) through the selected provider with tool calling.

    Emits ``token`` events as the model produces text, a single ``pending_action``
    for a mutating action awaiting approval, and a terminal ``done`` carrying the
    updated conversation. Read-only tool calls execute immediately (off-loop).
    """
    if not body.user_message.strip():
        return JSONResponse(status_code=400, content={"error": "Message cannot be empty"})

    return StreamingResponse(
        _chat_event_stream(request, body),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/chat/approve")
async def chat_approve(body: ChatApproveRequest, request: Request):
    """Execute a previously proposed mutating action after user approval."""
    try:
        result = await execute_approved_action(
            function_name=body.function_name,
            args=body.args,
            user_id=current_user_id(request),
            conversation_id=body.conversation_id,
        )
        return JSONResponse(content=result)
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "summary": f"Execution failed: {str(exc)[:300]}",
            },
        )


@router.post("/api/chat/reject")
async def chat_reject(body: ChatRejectRequest):
    """Reject a proposed mutating action."""
    return JSONResponse(
        content={
            "success": True,
            "message": "Action cancelled.",
        }
    )
