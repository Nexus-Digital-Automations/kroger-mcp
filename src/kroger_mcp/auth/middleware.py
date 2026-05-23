"""Authentication middleware for Starlette/FastAPI.

Checks for a session cookie on every request. If valid, attaches the user
to request.state.user. Otherwise: HTML routes redirect to /login (302),
API routes return JSON 401 (browsers can't follow a redirect for fetch()).
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

# /callback is the Kroger OAuth redirect target; it must be reachable without
# our own session cookie because the browser is mid-OAuth-handshake when it
# lands here.
PUBLIC_PATHS = {"/login", "/register", "/logout", "/callback"}
PUBLIC_PREFIXES = ("/static/", "/api/auth/")

SESSION_COOKIE = "kroger_session"


class AuthMiddleware(BaseHTTPMiddleware):
    """Enforces authentication on all non-public routes."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            request.state.user = None
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE)
        if token:
            from kroger_mcp.auth.sessions import validate_session

            user = validate_session(token)
            if user:
                request.state.user = user
                return await call_next(request)

        request.state.user = None
        if path.startswith("/api/"):
            return JSONResponse({"error": "authentication required"}, status_code=401)
        return RedirectResponse(url="/login", status_code=302)
