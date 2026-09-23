"""Logging and error reporting for the connector.

``configure_logging``: the two loggers whose INFO lines are the service's
signals (``mcp_protocol`` and ``mcp_auth_ok``) write the bare message to stderr
and do not propagate. The lines are meant to be parsed by log tooling, so the
format is plain text on purpose. Every other logger reaches the root handler
the MCP SDK installs when the server is built (a Rich console handler at INFO);
the root is left unconfigured here.

``init_sentry`` reports errors to Sentry, opt-in: only when ``SENTRY_DSN`` is
set.
"""

from __future__ import annotations

import logging
import os
import sys

# The loggers whose INFO lines are the service's signals.
INFO_LOGGERS = ('lenz_mcp.oauth', 'lenz_mcp.protocol_log', 'lenz_mcp.decisions')


def configure_logging() -> None:
    for name in INFO_LOGGERS:
        logger = logging.getLogger(name)
        if any(getattr(h, '_lenz_mcp', False) for h in logger.handlers):
            continue  # idempotent: a re-import must not double every line
        handler = logging.StreamHandler(sys.stderr)
        handler._lenz_mcp = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


def _under_pytest() -> bool:
    return 'pytest' in sys.modules or os.path.basename(sys.argv[0] or '') in {'pytest', 'py.test'}


def init_sentry() -> None:
    dsn = os.environ.get('SENTRY_DSN', '')
    # Never from a test run: a developer's env file can carry a real DSN, and
    # a test's expected errors would be reported as real ones.
    if not dsn or os.environ.get('DEBUG', 'False') == 'True' or _under_pytest():
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        release=os.environ.get('SENTRY_RELEASE', ''),
        environment=os.environ.get('SENTRY_ENVIRONMENT', 'development'),
        traces_sample_rate=float(os.environ.get('SENTRY_TRACES_SAMPLE_RATE', '0.1')),
        send_default_pii=True,
    )
