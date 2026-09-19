"""The package version, read at build time only (pyproject.toml's version source).

A release builds with LENZ_MCP_VERSION set to its tag; any other build, a
development checkout's lock included, gets a development version. The running
server reports APP_VERSION instead (config.APP_VERSION).
"""

import os

__version__ = os.environ.get('LENZ_MCP_VERSION', '').strip().removeprefix('v') or '0.0.0.dev0'
