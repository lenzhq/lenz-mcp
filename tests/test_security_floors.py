"""The lock holds no release below the first one that fixes a known advisory.

These packages reach the image through other dependencies, so a lock refresh
could resolve an older release without anything in pyproject.toml changing.
The floors are the first fixed releases.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FLOORS = {
    'anyio': (4, 14, 2),
    'cryptography': (48, 0, 1),
    'pyjwt': (2, 13, 0),
    'urllib3': (2, 7, 0),
}


def _release(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split('.')[:3])


@pytest.mark.parametrize(('name', 'floor'), sorted(FLOORS.items()))
def test_the_locked_release_is_at_or_above_its_fix(name, floor):
    lock = tomllib.loads((ROOT / 'uv.lock').read_text())
    versions = [p['version'] for p in lock['package'] if p['name'] == name]
    assert versions, f'{name} is not in uv.lock'
    for version in versions:
        assert _release(version) >= floor, f'{name} {version} is below {".".join(map(str, floor))}'
