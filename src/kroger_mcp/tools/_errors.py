"""
Error-handling decorator for tool modules.

Owns:
  - handle_errors: wrap a sync function so unhandled exceptions are logged and
    a configurable default is returned instead of propagating.

Does NOT own:
  - Async error handling — every current swallow site in tools/* is sync.
    Add an async variant only when an async caller needs it.
  - Domain validation — handle_errors is for unexpected failure paths, not
    for routine business-rule rejection (those should raise explicitly).

Called by (after Phase 2 migration):
  - Any tools/*.py site that previously did `try: ...; except Exception: pass`
    or `try: ...; except Exception as e: print("Warning: ...")`. Replaces both
    forms with a single uniform pattern.

Calls:
  - logging.getLogger only.
"""

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

T = TypeVar("T")

# Each module that uses @handle_errors gets its own logger via getLogger(fn.__module__),
# so log records carry the originating module name rather than this helper's name.
# That preserves grep-ability when scanning logs for which tool failed.


def handle_errors(
    *,
    default: Any = None,
    level: int = logging.WARNING,
    reraise: bool = False,
) -> Callable[[Callable[..., T]], Callable[..., T | Any]]:
    """Decorator that catches Exception, logs it, and returns `default`.

    Args:
        default:  Value to return when the wrapped callable raises. Ignored if reraise.
        level:    logging level for the captured exception (WARNING by default).
        reraise:  If True, log then re-raise. Useful for callers that want
                  observable failure without losing the exception.

    The decorator catches `Exception` (not `BaseException`), so KeyboardInterrupt
    and SystemExit still propagate — that's deliberate so dev workflows aren't
    broken by silenced Ctrl-C.

    Usage:
        @handle_errors(default={})
        def load_pantry_snapshot():
            ...   # any failure -> warn-level log, returns {}

        @handle_errors(level=logging.ERROR, reraise=True)
        def critical_op():
            ...   # logs at ERROR then re-raises
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T | Any]:
        log = logging.getLogger(fn.__module__)

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T | Any:
            try:
                return fn(*args, **kwargs)
            except Exception:
                # exc_info=True captures traceback so the log entry is debuggable
                # even though the exception is being swallowed.
                log.log(level, "%s raised; returning default", fn.__qualname__, exc_info=True)
                if reraise:
                    raise
                return default

        return wrapper

    return decorator
