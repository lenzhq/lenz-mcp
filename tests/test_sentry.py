"""Sentry's MCP integration must actually be enabled for this server.

It is auto-enabled, and it enables itself by importing from the installed `mcp`
SDK. When that import fails it raises `DidNotEnable`, which auto-loading
swallows: the server would lose MCP handler spans and error capture with no
error anywhere. That is exactly what `sentry-sdk` below 2.64.0 does on `mcp`
2.x: it imports `mcp.server.lowlevel.server.request_ctx`, which 2.x removed. So
this test is the tripwire for a version change on EITHER side: the MCP SDK
moving an internal, or sentry-sdk falling below its floor.

`init_sentry` skips itself under pytest and without a DSN, so the check runs in
a fresh interpreter, outside pytest, with a dummy DSN.
"""

from __future__ import annotations

import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The first sentry-sdk release whose `sentry_sdk/integrations/mcp.py` supports
# the mcp 2.x handler signature (its changelog: "Support MCP SDK v2 handler
# signature and removed request_ctx"). Below it the integration disables itself
# on mcp 2.x, silently.
MIN_SENTRY_FOR_MCP_2X = (2, 64, 0)

# The server's OWN initialisation, not a bare `sentry_sdk.init`: a probe that
# skipped it would keep passing if `init_sentry` ever turned the MCP integration
# off, which is the silent disable this test exists to catch. `sys.argv[0]` is
# `-c` here and nothing imports pytest, so `init_sentry` does not skip itself.
_PROBE = """
import sentry_sdk
from lenz_mcp import observability
observability.init_sentry()
client = sentry_sdk.get_client()
if not client.is_active():
    print('SENTRY_INACTIVE')
else:
    print('MCP_ENABLED' if 'mcp' in client.integrations else 'MCP_MISSING')
"""


def _release(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split('.')[:3])


def test_the_installed_sentry_supports_the_installed_mcp_sdk():
    assert _release(version('sentry-sdk')) >= MIN_SENTRY_FOR_MCP_2X, (
        f'sentry-sdk below {".".join(map(str, MIN_SENTRY_FOR_MCP_2X))} turns its MCP integration off '
        'on mcp 2.x, silently'
    )


def test_the_mcp_integration_is_enabled_in_a_real_client():
    env = {
        **os.environ,
        'PYTHONPATH': str(ROOT / 'src'),
        'SENTRY_DSN': 'https://public@example.ingest.sentry.io/1',
        'SENTRY_TRACES_SAMPLE_RATE': '0',
        'DEBUG': 'False',
    }
    result = subprocess.run(  # noqa: S603
        [sys.executable, '-c', _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert 'MCP_ENABLED' in result.stdout, (
        f'Sentry did not enable its MCP integration against mcp {version("mcp")}: {result.stdout} {result.stderr}'
    )
