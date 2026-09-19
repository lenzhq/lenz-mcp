"""What lenz-mcp answers when a client gets it WRONG, pinned.

The happy paths are covered many times over (test_server, test_card,
test_dual_era). The error answers are not, and they are where the SDK
majors actually differ: 1.x reports a failed prompt render or an unknown resource
as JSON-RPC code 0 inside an HTTP 200, while 2.x has typed codes and maps them
onto HTTP statuses. A migration can change every one of those without a single
existing test noticing.

So the answers are captured from the SERVING code into `era_errors.json` and
compared on every run. Any diff of that file is the reviewed list of what a change
did to the errors a client sees: each line of it is reviewed and explained, or it
is a bug.

Regenerate deliberately, never casually:

    LENZ_UPDATE_ERA_ERRORS=1 uv run pytest tests/test_era_errors.py --no-cov

The captured shape is the client-visible one: the HTTP status, the JSON-RPC error
code, whether the answer was a tool result carrying `isError`, and whether a
message is present (never its text, which is prose and would
turn every copy edit into a failure here; the wording that MATTERS to a model is
pinned by the per-client goldens).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lenz_mcp.testing import LEGACY, assembled_app

BASELINE = Path(__file__).resolve().parent / 'era_errors.json'
UPDATE = os.environ.get('LENZ_UPDATE_ERA_ERRORS') == '1'


def _shape(status: int, message: dict | None) -> dict:
    """The client-visible shape of one answer: status, code, and "was there a message".

    Deliberately not the message text. The migration rewrites some of that prose
    (2.x hides a prompt's own ValueError text, which is why the prompts raise
    MCPError now), and a golden full of sentences would fail on every copy edit
    for reasons that have nothing to do with the protocol.
    """
    error = (message or {}).get('error') or {}
    result = (message or {}).get('result') or {}
    return {
        'http': status,
        'code': error.get('code'),
        'has_message': bool(error.get('message')),
        'has_result': bool(result),
        # A FAILED TOOL CALL is a result with `isError`, not a JSON-RPC error — that is
        # the protocol, in both eras: the model is meant to read the failure, so it
        # travels as content. Recorded so a change cannot quietly turn one into
        # the other (a tool failure arriving as a transport error would be invisible
        # to the model and would read to a client as the server being broken).
        'is_error_result': result.get('isError'),
    }


def _capture(wire) -> dict:
    """Every error answer a client can provoke, in the 2025 era."""
    cases: dict[str, dict] = {}

    for prompt, argument in (('check_text', 'text'), ('check_last_answer', 'answer')):
        for label, params in (
            ('omitted', {'name': prompt, 'arguments': {}}),
            ('empty', {'name': prompt, 'arguments': {argument: ''}}),
            ('whitespace', {'name': prompt, 'arguments': {argument: '   \n\t '}}),
        ):
            response = wire.rpc('prompts/get', params, era=LEGACY)
            cases[f'prompts/get:{prompt}:{label}'] = _shape(response.status_code, wire.reply(response))

    unknown_resource = wire.rpc('resources/read', {'uri': 'ui://lenz/card-v999'}, era=LEGACY)
    cases['resources/read:unknown'] = _shape(unknown_resource.status_code, wire.reply(unknown_resource))

    unknown_tool = wire.rpc('tools/call', {'name': 'no_such_tool', 'arguments': {}}, era=LEGACY)
    cases['tools/call:unknown_tool'] = _shape(unknown_tool.status_code, wire.reply(unknown_tool))

    bad_arguments = wire.rpc('tools/call', {'name': 'assess_claim', 'arguments': {'claim': 12345}}, era=LEGACY)
    cases['tools/call:wrong_argument_type'] = _shape(bad_arguments.status_code, wire.reply(bad_arguments))

    unknown_method = wire.rpc('no/such/method', {}, era=LEGACY)
    cases['unknown_method'] = _shape(unknown_method.status_code, wire.reply(unknown_method))

    return cases


def test_the_error_answers_match_the_committed_baseline():
    with assembled_app(MCP_OAUTH_ENABLED=False) as harness:
        wire = harness.wire()
        wire.initialize()
        captured = _capture(wire)

    if UPDATE:
        BASELINE.write_text(json.dumps(captured, indent=2, sort_keys=True) + '\n')
        pytest.skip(f'baseline rewritten: {BASELINE.name}')

    expected = json.loads(BASELINE.read_text())
    assert captured == expected, (
        'the error a client sees changed. If this is an SDK migration, review and explain every '
        'difference; otherwise it is a bug. Regenerate with '
        'LENZ_UPDATE_ERA_ERRORS=1.'
    )


def test_every_error_case_produced_an_error():
    """A capture that recorded a SUCCESS for a case meant to fail is worthless as a
    baseline, and would pin the wrong thing forever."""
    expected = json.loads(BASELINE.read_text())
    assert expected, 'the baseline is empty'
    for name, shape in expected.items():
        failed = shape['code'] is not None or shape['is_error_result'] is True
        assert failed, f'{name} recorded neither a JSON-RPC error nor an isError result'
        if shape['code'] is not None:
            assert shape['has_message'], f'{name} recorded a JSON-RPC error with no message'
            assert not shape['has_result'], f'{name} recorded a result as well as an error'
