"""Make the connector importable from a checkout.

The checkout is not an installed package (`[tool.uv] package = false`), so
`python -m evals.tool_choice` finds `evals` from the working directory but not
`lenz_mcp`, which lives under `src/`. Importing this module puts it on the path,
so the commands in CONTRIBUTING.md work as written, whichever way the eval is
started.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / 'src'

if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
