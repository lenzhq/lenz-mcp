"""What `serverInfo.version` reports, read from APP_VERSION.

A build passes its version in APP_VERSION. It may be a release number (412) or
a release tag (2.0.0); unset or 0 means an unversioned build and reports `dev`.
"""

from __future__ import annotations

import importlib

import pytest

from lenz_mcp import config
from lenz_mcp.testing import connector_env


@pytest.mark.parametrize(
    ('raw', 'reported'),
    [
        (None, 'dev'),
        ('', 'dev'),
        ('0', 'dev'),
        ('412', '412'),
        ('2.0.0', '2.0.0'),
        (' 2.1.3 ', '2.1.3'),
        ('v2.0.0', '2.0.0'),
    ],
)
def test_the_version_the_server_reports(raw, reported):
    original = dict(config.__dict__)
    try:
        with connector_env(APP_VERSION=raw):
            assert importlib.reload(config).APP_VERSION == reported
    finally:
        config.__dict__.clear()
        config.__dict__.update(original)
