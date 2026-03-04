"""
Authentication and user profile tools for Kroger MCP server.
"""

import os
from typing import Any, Dict, Literal, Optional
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from fastmcp import Context
from kroger_api import KrogerAPI
from kroger_api.utils import generate_pkce_parameters
from pydantic import Field

from .shared import get_authenticated_client, invalidate_authenticated_client

# Load environment variables
load_dotenv()

# Store PKCE parameters between steps (module-level for OAuth flow state)
_pkce_params = None
_auth_state = None


def register_tools(mcp):
    """Register authentication and profile tools with the FastMCP server."""

    @mcp.tool()
    async def auth(
        action: Literal[
            "start",
            "complete",
            "get_profile",
            "test",
            "get_info",
            "force_reauth",
        ] = Field(
            description=(
                "Action: 'start' - begin OAuth authentication flow, "
                "'complete' - finish OAuth using redirect URL from browser, "
                "'get_profile' - get authenticated user's Kroger profile, "
                "'test' - test if current authentication token is valid, "
                "'get_info' - get details about current authentication state, "
                "'force_reauth' - clear token and force re-authentication"
            )
        ),
        redirect_url: Optional[str] = Field(
            default=None,
            description="Full redirect URL from browser after authorization (for complete)",
        ),
        ctx: Context = None,
    ) -> Dict[str, Any]:
        """Authentication and user profile operations."""
        global _pkce_params, _auth_state

        match action:
            case "start":
                # Generate PKCE parameters
                _pkce_params = generate_pkce_parameters()

                # Generate a state parameter for CSRF protection
                _auth_state = _pkce_params.get(
                    "state", _pkce_params.get("code_verifier")[:16]
                )

                # Get client_id from environment
                client_id = os.environ.get("KROGER_CLIENT_ID")

                if not client_id:
                    if ctx:
                        await ctx.error("Missing KROGER_CLIENT_ID environment variable")
                    return {
                        "error": True,
                        "message": (
                            "Missing KROGER_CLIENT_ID environment variable. "
                            "Please set up your Kroger API credentials."
                        ),
                    }

                # Initialize the Kroger API client
                kroger = KrogerAPI()

                # Scopes needed for Kroger API
                scopes = "product.compact cart.basic:write"

                # Get the authorization URL with PKCE
                auth_url = kroger.authorization.get_authorization_url(
                    scope=scopes,
                    state=_auth_state,
                    code_challenge=_pkce_params["code_challenge"],
                    code_challenge_method=_pkce_params["code_challenge_method"],
                )

                if ctx:
                    await ctx.info(f"Generated auth URL with PKCE: {auth_url}")

                return {
                    "auth_url": auth_url,
                    "instructions": (
                        "1. Click this link to authorize: [🔗 Authorize Kroger Access]({auth_url})\n"
                        "   - Please present the authorization URL as a clickable markdown link\n"
                        "2. Log in to your Kroger account and authorize the application\n"
                        "3. After authorization, you'll be redirected to a callback URL\n"
                        "4. Copy the FULL redirect URL from your browser's address bar\n"
                        "5. Use auth(action='complete', redirect_url=...) to finish"
                    ).format(auth_url=auth_url),
                }

            case "complete":
                if not redirect_url:
                    return {"success": False, "error": "redirect_url is required"}

                if not _pkce_params or not _auth_state:
                    if ctx:
                        await ctx.error("Authentication flow not started")
                    return {
                        "error": True,
                        "message": (
                            "Authentication flow not started. "
                            "Please use auth(action='start') first."
                        ),
                    }

                try:
                    # Parse the redirect URL
                    parsed_url = urlparse(redirect_url)
                    query_params = parse_qs(parsed_url.query)

                    # Extract code and state
                    if "code" not in query_params:
                        if ctx:
                            await ctx.error("Authorization code not found in redirect URL")
                        return {
                            "error": True,
                            "message": (
                                "Authorization code not found in redirect URL. "
                                "Please check the URL and try again."
                            ),
                        }

                    auth_code = query_params["code"][0]
                    received_state = query_params.get("state", [None])[0]

                    # Verify state parameter to prevent CSRF attacks
                    if received_state != _auth_state:
                        if ctx:
                            await ctx.error(
                                f"State mismatch: expected {_auth_state}, got {received_state}"
                            )
                        return {
                            "error": True,
                            "message": (
                                "State parameter mismatch. This could indicate a CSRF attack. "
                                "Please try authenticating again."
                            ),
                        }

                    # Get client credentials
                    client_id = os.environ.get("KROGER_CLIENT_ID")
                    client_secret = os.environ.get("KROGER_CLIENT_SECRET")

                    if not client_id or not client_secret:
                        if ctx:
                            await ctx.error("Missing Kroger API credentials")
                        return {
                            "error": True,
                            "message": (
                                "Missing Kroger API credentials. "
                                "Please set KROGER_CLIENT_ID and KROGER_CLIENT_SECRET."
                            ),
                        }

                    # Initialize Kroger API client
                    kroger = KrogerAPI()

                    if ctx:
                        await ctx.info(
                            "Exchanging authorization code for tokens with code_verifier"
                        )

                    # Exchange the authorization code for tokens with the code verifier
                    token_info = kroger.authorization.get_token_with_authorization_code(
                        auth_code,
                        code_verifier=_pkce_params["code_verifier"],
                    )

                    # Clear PKCE parameters and state after successful exchange
                    _pkce_params = None
                    _auth_state = None

                    if ctx:
                        await ctx.info("Authentication successful!")

                    return {
                        "success": True,
                        "message": (
                            "Authentication successful! "
                            "You can now use Kroger API tools that require authentication."
                        ),
                        "token_info": {
                            "expires_in": token_info.get("expires_in"),
                            "token_type": token_info.get("token_type"),
                            "scope": token_info.get("scope"),
                            "has_refresh_token": "refresh_token" in token_info,
                        },
                    }

                except Exception as e:
                    error_message = str(e)

                    if ctx:
                        await ctx.error(f"Authentication error: {error_message}")

                    return {
                        "error": True,
                        "message": f"Authentication failed: {error_message}",
                    }

            case "get_profile":
                if ctx:
                    await ctx.info("Getting user profile information")

                try:
                    client = get_authenticated_client()
                    profile = client.identity.get_profile()

                    if profile and "data" in profile:
                        profile_id = profile["data"].get("id", "N/A")

                        if ctx:
                            await ctx.info(f"Retrieved profile for user ID: {profile_id}")

                        return {
                            "success": True,
                            "profile_id": profile_id,
                            "message": "User profile retrieved successfully",
                            "note": (
                                "The Kroger Identity API only provides the profile ID "
                                "for privacy reasons."
                            ),
                        }
                    else:
                        return {"success": False, "message": "Failed to retrieve user profile"}

                except Exception as e:
                    if ctx:
                        await ctx.error(f"Error getting user profile: {str(e)}")
                    return {"success": False, "error": str(e)}

            case "test":
                if ctx:
                    await ctx.info("Testing authentication token validity")

                try:
                    client = get_authenticated_client()
                    is_valid = client.test_current_token()

                    if ctx:
                        await ctx.info(
                            f"Authentication test result: {'valid' if is_valid else 'invalid'}"
                        )

                    result = {
                        "success": True,
                        "token_valid": is_valid,
                        "message": (
                            f"Authentication token is {'valid' if is_valid else 'invalid'}"
                        ),
                    }

                    # Check for refresh token availability
                    if hasattr(client.client, "token_info") and client.client.token_info:
                        has_refresh_token = "refresh_token" in client.client.token_info
                        result["has_refresh_token"] = has_refresh_token
                        result["can_auto_refresh"] = has_refresh_token

                        if has_refresh_token:
                            result["message"] += (
                                ". Token can be automatically refreshed when it expires."
                            )
                        else:
                            result["message"] += (
                                ". No refresh token available - will need to "
                                "re-authenticate when token expires."
                            )

                    return result

                except Exception as e:
                    if ctx:
                        await ctx.error(f"Error testing authentication: {str(e)}")
                    return {"success": False, "error": str(e), "token_valid": False}

            case "get_info":
                if ctx:
                    await ctx.info("Getting authentication information")

                try:
                    client = get_authenticated_client()

                    result = {
                        "success": True,
                        "authenticated": True,
                        "message": "User is authenticated",
                    }

                    # Get token information if available
                    if hasattr(client.client, "token_info") and client.client.token_info:
                        token_info = client.client.token_info

                        result.update(
                            {
                                "token_type": token_info.get("token_type", "Unknown"),
                                "has_refresh_token": "refresh_token" in token_info,
                                "expires_in": token_info.get("expires_in"),
                                "scope": token_info.get("scope", "Unknown"),
                            }
                        )

                        # Don't expose the actual tokens for security
                        result["access_token_preview"] = (
                            f"{token_info.get('access_token', '')[:10]}..."
                            if token_info.get("access_token")
                            else "N/A"
                        )

                        if "refresh_token" in token_info:
                            result["refresh_token_preview"] = (
                                f"{token_info['refresh_token'][:10]}..."
                            )

                    # Get token file information if available
                    if hasattr(client.client, "token_file") and client.client.token_file:
                        result["token_file"] = client.client.token_file

                    return result

                except Exception as e:
                    if ctx:
                        await ctx.error(f"Error getting authentication info: {str(e)}")
                    return {"success": False, "error": str(e), "authenticated": False}

            case "force_reauth":
                if ctx:
                    await ctx.info(
                        "Forcing re-authentication by clearing current token"
                    )

                try:
                    invalidate_authenticated_client()

                    if ctx:
                        await ctx.info(
                            "Authentication token cleared. "
                            "Next cart operation will trigger re-authentication."
                        )

                    return {
                        "success": True,
                        "message": (
                            "Authentication token cleared. The next cart operation will "
                            "open your browser for re-authentication."
                        ),
                        "note": (
                            "You will need to log in again when you next "
                            "use cart-related tools."
                        ),
                    }

                except Exception as e:
                    if ctx:
                        await ctx.error(f"Error clearing authentication: {str(e)}")
                    return {"success": False, "error": str(e)}

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
