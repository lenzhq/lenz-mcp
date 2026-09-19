"""The connector's own tests.

They import nothing but the connector, the standard library and the
connector's declared dependencies, and they run without loading a web
framework, under the connector's own pytest configuration.
The drive-the-real-app harness is `lenz_mcp.testing`.
"""

from __future__ import annotations

import sys

import pytest


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """The connector's tests must not load a web framework.

    The standalone config's proof that these tests are the connector's own:
    a test (or the code it drives) that reached for one would pass wherever it
    is already loaded, and fail only here.
    """
    if session.config.pluginmanager.has_plugin('django'):
        return  # a runner with that plugin loads the framework by design
    leaked = sorted(m for m in sys.modules if m == 'django' or m.startswith('django.'))
    if leaked:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        sys.stderr.write(f'\nconnector tests imported a web framework: {leaked[:5]}\n')
