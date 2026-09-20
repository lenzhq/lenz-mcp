"""The Lenz card's fixtures are what lenz-mcp's tools return today.

src/lenz_mcp/card/fixtures/fixtures.json feeds the card's stub-host tests and the
state gallery. It is built by running synthetic API responses (invented
bodies in the API's exact shape, in src/lenz_mcp/card/fixtures/captured/) through
the real tool code, so a change to that mapping must rebuild it, or the card
would be tested against payloads the server no longer sends.
"""

from __future__ import annotations

import importlib.util
import json
import re

from lenz_mcp.mcp_card import CARD_DIR

FIXTURES = CARD_DIR / 'fixtures'


def _builder():
    spec = importlib.util.spec_from_file_location('card_fixture_builder', FIXTURES / 'build_fixtures.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixtures_json_is_current():
    builder = _builder()
    expected = builder.render(builder.build())
    actual = (FIXTURES / 'fixtures.json').read_text(encoding='utf-8')
    assert actual == expected, 'Rebuild: uv run python src/lenz_mcp/card/fixtures/build_fixtures.py'


def test_every_fixture_says_where_its_payload_came_from():
    data = json.loads((FIXTURES / 'fixtures.json').read_text(encoding='utf-8'))
    kinds = ('synthetic', 'composed', 'edited', 'hand-built')
    for group in data.values():
        for name, fixture in group.items():
            assert fixture['provenance'].startswith(kinds), name


def _dissents(node):
    """Every `dissent` string anywhere in a fixture, at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == 'dissent' and isinstance(value, str):
                yield value
            else:
                yield from _dissents(value)
    elif isinstance(node, list):
        for item in node:
            yield from _dissents(item)


def test_no_fixture_dissent_carries_the_cards_own_prefix():
    """The API sends a dissent as the reviewer's bare reasoning; the card adds
    DISSENT_PREFIX itself (copy.js), so a fixture that repeats it renders the
    phrase twice."""
    copy_js = (CARD_DIR / 'src' / 'copy.js').read_text(encoding='utf-8')
    match = re.search(r"DISSENT_PREFIX = '([^']+)'", copy_js)
    assert match, 'DISSENT_PREFIX not found in copy.js'
    prefix = match.group(1).strip()
    for path in (FIXTURES / 'fixtures.json', *sorted((FIXTURES / 'captured').glob('*.json'))):
        for dissent in _dissents(json.loads(path.read_text(encoding='utf-8'))):
            assert not dissent.lstrip().startswith(prefix), f'{path.name}: {dissent[:60]}'
