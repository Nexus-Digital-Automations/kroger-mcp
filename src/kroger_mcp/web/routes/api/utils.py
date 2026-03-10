"""Shared utilities for API routes."""
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from kroger_mcp.tools.shared import (
    get_authenticated_client as _get_auth,
    get_client_credentials_client as _get_pub,
)


def get_kroger_client():
    """Get authenticated Kroger client, raise 401 if not available."""
    try:
        return _get_auth()
    except Exception as e:
        raise HTTPException(
            status_code=401,
            detail=f"Authentication required: {str(e)}",
        )


def get_public_client():
    """Get public (client credentials) Kroger client."""
    try:
        return _get_pub()
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Kroger API unavailable: {str(e)}",
        )


def error_response(message: str, status_code: int = 400):
    return JSONResponse(
        status_code=status_code,
        content={"error": message},
    )
