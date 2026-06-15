"""kroger-mcp idle watchdog: stops per-session ssh-stdio process leaks.

The server is launched once per Claude session over ssh-stdio; an idle-but-open
channel would otherwise keep it alive forever. The watchdog self-exits after an
idle timeout (or when orphaned). These tests pin the exit predicate and the
activity heartbeat without actually killing the process.
"""

from __future__ import annotations

import asyncio

import kroger_mcp.server as server


def test_idle_exit_not_due_while_active():
    # Same instant and well within the window → keep running.
    assert server._idle_exit_due(now=100.0, last_activity=100.0, timeout=1800, ppid=500) is False
    assert server._idle_exit_due(now=1899.0, last_activity=100.0, timeout=1800, ppid=500) is False


def test_idle_exit_due_after_timeout():
    assert server._idle_exit_due(now=1900.0, last_activity=100.0, timeout=1800, ppid=500) is True


def test_idle_exit_due_when_orphaned():
    # Reparented to launchd (ppid==1) → exit immediately even if just-active.
    assert server._idle_exit_due(now=100.0, last_activity=100.0, timeout=1800, ppid=1) is True


def test_activity_middleware_bumps_clock():
    server._last_activity = 0.0

    async def _call_next(_ctx):
        return "ok"

    out = asyncio.run(server._ActivityMiddleware().on_message(object(), _call_next))
    assert out == "ok"
    assert server._last_activity > 0.0
