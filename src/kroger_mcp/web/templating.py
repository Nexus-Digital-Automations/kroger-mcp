"""Single shared Jinja2Templates instance for the whole web app.

Ownership: the one process-wide template environment. Every route module and
``app.py`` import ``templates`` from here instead of constructing their own
``Jinja2Templates(...)`` — a fresh environment per route module re-loads and
re-caches the same template tree N times, wasting per-worker RAM on a box shared
with other tenants. Lives in its own module (not ``app.py``) so route modules can
import it without a circular dependency, since ``app.py`` imports the routes.
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
