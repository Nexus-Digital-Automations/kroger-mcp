"""Authentication routes — login, register, logout."""

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from kroger_mcp.auth.middleware import SESSION_COOKIE
from kroger_mcp.auth.passwords import hash_password, verify_password
from kroger_mcp.auth.sessions import create_session, delete_session

router = APIRouter()

templates = Jinja2Templates(directory=str(__file__).rsplit("/routes", 1)[0] + "/templates")


def _get_user_by_email(email: str) -> dict | None:
    """Look up a user by email. Returns dict or None."""
    from kroger_mcp.analytics.database import get_backend

    if get_backend() == "postgresql":
        from kroger_mcp.analytics.pg_database import get_pg_connection

        conn = get_pg_connection()
        try:
            cur = conn.execute(
                "SELECT id, email, password_hash, display_name, kroger_profile_id "
                "FROM users WHERE email = %s AND is_active = TRUE",
                (email,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": str(row[0]),
                "email": row[1],
                "password_hash": row[2],
                "display_name": row[3],
                "kroger_profile_id": row[4],
            }
        finally:
            from kroger_mcp.analytics.pg_database import _get_pool

            _get_pool().putconn(conn)
    else:
        from kroger_mcp.analytics.database import get_db_connection

        conn = get_db_connection()
        try:
            conn.row_factory = lambda c, r: {col[0]: r[i] for i, col in enumerate(c.description)}
            cur = conn.execute(
                "SELECT id, email, password_hash, display_name, kroger_profile_id "
                "FROM users WHERE email = ? AND is_active = 1",
                (email,),
            )
            return cur.fetchone()
        finally:
            conn.close()


def _create_user(email: str, display_name: str, password: str) -> str:
    """Create a new user. Returns the user ID."""
    from kroger_mcp.analytics.database import get_backend

    pw_hash = hash_password(password)
    user_id = str(uuid.uuid4())

    if get_backend() == "postgresql":
        from kroger_mcp.analytics.pg_database import _get_pool, get_pg_connection

        conn = get_pg_connection()
        try:
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, display_name) "
                "VALUES (%s, %s, %s) RETURNING id",
                (email, pw_hash, display_name),
            )
            user_id = str(cur.fetchone()[0])
            conn.commit()
        finally:
            _get_pool().putconn(conn)
    else:
        from kroger_mcp.analytics.database import get_db_connection

        conn = get_db_connection()
        try:
            conn.execute(
                "INSERT INTO users (id, email, password_hash, display_name) " "VALUES (?, ?, ?, ?)",
                (user_id, email, pw_hash, display_name),
            )
            conn.commit()
        finally:
            conn.close()

    return user_id


def _email_exists(email: str) -> bool:
    """Check if an email is already registered."""
    return _get_user_by_email(email) is not None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html",
        {"request": request, "active_page": "login", "error": None},
    )


@router.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""

    if not email or not password:
        return templates.TemplateResponse(request, "login.html",
            {
                "active_page": "login",
                "error": "Email and password are required.",
            },
            status_code=400,
        )

    user = _get_user_by_email(email)
    if not user or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(request, "login.html",
            {"request": request, "active_page": "login", "error": "Invalid email or password."},
            status_code=401,
        )

    # Create session
    ip = request.client.host if request.client else ""
    token = create_session(user["id"], ip)

    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=30 * 24 * 3600,  # 30 days
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html",
        {"request": request, "active_page": "register", "error": None},
    )


@router.post("/register")
async def register_submit(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    display_name = (form.get("display_name") or "").strip()
    password = form.get("password") or ""
    confirm = form.get("confirm_password") or ""

    # Validate
    errors = []
    if not email or "@" not in email:
        errors.append("Valid email is required.")
    if not display_name:
        errors.append("Display name is required.")
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if password != confirm:
        errors.append("Passwords do not match.")
    if email and _email_exists(email):
        errors.append("An account with this email already exists.")

    if errors:
        return templates.TemplateResponse(request, "register.html",
            {"request": request, "active_page": "register", "error": " ".join(errors)},
            status_code=400,
        )

    # Create user + session
    user_id = _create_user(email, display_name, password)
    ip = request.client.host if request.client else ""
    token = create_session(user_id, ip)

    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=30 * 24 * 3600,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        delete_session(token)

    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response
