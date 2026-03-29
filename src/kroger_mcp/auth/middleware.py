"""Authentication middleware for Starlette/FastAPI.

Checks for a session cookie on every request. If valid, attaches the user
to request.state.user. If not, redirects to /login (except public routes).
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

# Routes that don't require authentication
PUBLIC_PATHS = {"/login", "/register", "/logout"}
PUBLIC_PREFIXES = ("/static/", "/api/auth/")

SESSION_COOKIE = "kroger_session"


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces authentication on all non-public routes."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Allow public routes
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            request.state.user = None
            return await call_next(request)

        # Check session cookie
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            from kroger_mcp.auth.sessions import validate_session

            user = validate_session(token)
            if user:
                request.state.user = user
                return await call_next(request)

        # No valid session — redirect to login
        request.state.user = None
        return RedirectResponse(url="/login", status_code=302)
