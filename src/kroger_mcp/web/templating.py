"""Single shared Jinja2Templates instance for the whole web app.

Ownership: the one process-wide template environment. Every route module and
``app.py`` import ``templates`` from here instead of constructing their own
``Jinja2Templates(...)`` — a fresh environment per route module re-loads and
re-caches the same template tree N times, wasting per-worker RAM on a box shared
with other tenants. Lives in its own module (not ``app.py``) so route modules can
import it without a circular dependency, since ``app.py`` imports the routes.

Performance config (Batch 4): the stock environment has no bytecode cache and
leaves ``auto_reload`` on, so on every render Jinja re-stats each template file
and, on a compile-cache miss, recompiles template source to bytecode. On the
shared single-worker mini that is wasted CPU + syscalls per request. We add a
``FileSystemBytecodeCache`` (compiled bytecode survives across renders) and turn
``auto_reload`` off in production (``APP_ENV=prod``) where templates never change
between deploys. Dev keeps ``auto_reload`` on for live edits.
"""

import os
import tempfile
from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemBytecodeCache, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).parent / "templates"

_IS_PROD = os.environ.get("APP_ENV", "dev") == "prod"

# Best-effort on-disk bytecode cache. If the temp dir can't be created we fall
# back to no cache (None) rather than failing app startup.
try:
    _bytecode_dir = Path(tempfile.gettempdir()) / "ss_jinja_cache"
    _bytecode_dir.mkdir(parents=True, exist_ok=True)
    _bytecode_cache: FileSystemBytecodeCache | None = FileSystemBytecodeCache(str(_bytecode_dir))
except OSError:
    _bytecode_cache = None

# Build the Environment ourselves (Starlette 1.2's Jinja2Templates no longer
# forwards env_options, so the only way to set auto_reload/bytecode_cache is via
# env=). Match Starlette's own defaults (FileSystemLoader + select_autoescape);
# Jinja2Templates(env=...) still injects the ``url_for`` global afterwards.
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(),
    auto_reload=not _IS_PROD,
    bytecode_cache=_bytecode_cache,
    cache_size=500,
)

templates = Jinja2Templates(env=_env)
