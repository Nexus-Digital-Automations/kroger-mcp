"""Chat API endpoints — DeepSeek-powered grocery assistant."""

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kroger_mcp.web.chat_engine import (
    execute_approved_action,
    process_message,
)

router = APIRouter()


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


class ChatApproveRequest(BaseModel):
    id: str
    function_name: str
    args: dict[str, Any] = {}


class ChatRejectRequest(BaseModel):
    id: str


# -------------------------------------------------------------------
# Endpoints
# -------------------------------------------------------------------


@router.post("/api/chat/message")
async def chat_message(body: ChatMessageRequest):
    """Process a chat message through DeepSeek with tool calling.

    Read-only tool calls execute immediately.
    Mutating tool calls return a pending_action for user approval.
    """
    if not body.user_message.strip():
        return JSONResponse(
            status_code=400,
            content={"error": "Message cannot be empty"},
        )

    try:
        result = process_message(
            messages=body.messages,
            user_message=body.user_message.strip(),
        )
        return JSONResponse(content=result)
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={
                "error": f"Chat processing failed: {str(exc)[:300]}",
            },
        )


@router.post("/api/chat/approve")
async def chat_approve(body: ChatApproveRequest):
    """Execute a previously proposed mutating action after user approval."""
    try:
        result = execute_approved_action(
            function_name=body.function_name,
            args=body.args,
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
