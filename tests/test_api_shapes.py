"""The tools read both shapes of the Lenz API's responses, and say the same thing.

The API answers in an older shape and a newer one. `api_shapes.json` holds the
same response in each (endpoint and outcome). `api_shapes_expected.json` is the
oracle: what each tool returned for the OLDER body before this server could
read the newer one, recorded once from that earlier code and frozen. Every
scenario here runs the tool on both bodies and requires exactly the oracle's
output from each, serialized byte for byte.

The newer body is also served over HTTP, through the real client (the lenz-io
SDK) on an `httpx.MockTransport`: the answer the SDK hands back must give the
tools exactly what the raw body gave them. Keys are compared sorted there,
because the SDK's models order the keys they dump.

The oracle comes from the release before this server read the newer shape,
commit b8da55d069e31e81dc7c6d61574a3dd722aeb5db, and the writer refuses to run
on any other source. To rebuild it:

    git worktree add --detach /tmp/lenz-mcp-oracle b8da55d069e31e81dc7c6d61574a3dd722aeb5db
    cp tests/test_api_shapes.py tests/api_shapes.json /tmp/lenz-mcp-oracle/tests/
    cd /tmp/lenz-mcp-oracle && uv sync --group dev
    LENZ_MCP_WRITE_API_ORACLE=<this checkout>/tests/api_shapes_expected.json \
        uv run pytest tests/test_api_shapes.py
"""

import asyncio
import json
import os
import pathlib
import subprocess
import types
from collections.abc import Callable
from typing import Any

import pytest

from lenz_mcp import client, config, server
from lenz_mcp.client import ApiResponse
from tests import api_wire

HERE = pathlib.Path(__file__).parent
SHAPES: dict[str, dict[str, Any]] = json.loads((HERE / 'api_shapes.json').read_text(encoding='utf-8'))
ORACLE_PATH = HERE / 'api_shapes_expected.json'
WRITE_ORACLE = os.environ.get('LENZ_MCP_WRITE_API_ORACLE', '')
# The release whose behaviour the oracle records.
ORACLE_COMMIT = 'b8da55d069e31e81dc7c6d61574a3dd722aeb5db'
BOTH = ('legacy', 'canonical')


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(server, '_sleep', _instant)
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS', 0.05)
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS_BY_IDENTITY', {'Claude-User': 0.05})
    monkeypatch.setattr(config, 'CARD_ENABLED', True)


def _ctx():
    request = types.SimpleNamespace(headers={'authorization': 'Bearer lenz_testkey'})
    return types.SimpleNamespace(request_context=types.SimpleNamespace(request=request))


def _response(fixture: str, shape: str) -> ApiResponse:
    entry = SHAPES[fixture][shape]
    # Lowercased like the real client's headers.
    headers = {key.lower(): value for key, value in entry['headers'].items()}
    return ApiResponse(status=entry['status'], data=entry['body'], headers=headers)


# When set, `_stub` routes an endpoint's answer to the HTTP transport instead of
# replacing the client function, so the request goes through the SDK.
_WIRE_ROUTES: dict[str, ApiResponse] | None = None


def _stub(m: pytest.MonkeyPatch, api: str, response: ApiResponse) -> None:
    if _WIRE_ROUTES is not None:
        _WIRE_ROUTES[api] = response
        return

    async def _fake(*_args, **_kwargs):
        return response

    m.setattr(client, api, _fake)


def _api_of(request: Any) -> str:
    """Which client function sent this request."""
    path = request.url.path.removeprefix('/api/v1')
    if path == '/assess':
        return 'assess'
    if path == '/verify':
        return 'verify'
    if path.startswith('/verify/status/'):
        return 'verify_status'
    if path.endswith('/select'):
        return 'select'
    if path.startswith('/ask/'):
        return 'ask'
    if path == '/citecheck':
        return 'citecheck'
    if path.startswith('/citechecks/'):
        return 'citecheck_status'
    if path == '/me/usage':
        return 'me_usage'
    if path == '/verifications':
        return 'list_verifications'
    if path.startswith('/verifications/'):
        return 'verification_detail'
    raise AssertionError(f'unexpected request {request.method} {path}')


def _served(routes: dict[str, ApiResponse]) -> Callable[[Any], Any]:
    """An HTTP handler answering each request with its endpoint's response, in
    the API version the connector reads."""

    def handler(request):
        response = routes[_api_of(request)]
        return api_wire.answer(response.status, response.data, response.headers)

    return handler


def run_through_the_wire(runner: Callable[[pytest.MonkeyPatch], Any]) -> Any:
    """Run a tool scenario whose `_stub` answers come over HTTP, through the SDK."""
    global _WIRE_ROUTES
    routes: dict[str, ApiResponse] = {}
    _WIRE_ROUTES = routes
    try:
        with pytest.MonkeyPatch.context() as m:
            wire = api_wire.install(m)
            wire.respond(_served(routes))
            # No client identity bound, as in the stubbed runs: the same card
            # and wait decisions on both paths.
            return asyncio.run(runner(m))
    finally:
        _WIRE_ROUTES = None


# ── what each scenario runs ──────────────────────────────────────────


def _assess(m, response):
    _stub(m, 'assess', response)
    return server.assess_claim('The text to check.', _ctx())


def _poll(m, response):
    _stub(m, 'verify_status', response)
    return server.get_verification('a' * 32, _ctx())


def _select(m, response):
    _stub(m, 'select', response)
    return server.select_claims('a' * 32, ['first', 'second'], _ctx())


def _card_select(m, response):
    _stub(m, 'select', response)
    return server._select_claims_for_card('a' * 32, ['first', 'second'], _ctx())


def _verify_error(m, response):
    _stub(m, 'verify', response)
    return server.verify_claim('The claim.', _ctx())


def _usage(m, response):
    _stub(m, 'me_usage', response)
    return server.check_usage(_ctx())


def _list(m, response):
    _stub(m, 'list_verifications', response)
    return server.list_verifications(_ctx())


def _card_retry(m, response):
    """A card retry of a failed run: allowed only when the poll says retryable."""
    _stub(m, 'verify_status', response)
    _stub(m, 'verify', ApiResponse(status=202, data={'task_id': 'n' * 32, 'status': 'queued'}))
    return server._start_verification_for_card('The claim.', _ctx(), retry_of='f' * 32)


Runner = Callable[[pytest.MonkeyPatch, ApiResponse], Any]

SCENARIOS: dict[str, tuple[str, Runner]] = {}
for _name, _runner in {
    'assess__list_mixed_rows': _assess,
    'assess__single_no_claim': _assess,
    'assess__list_compound_item': _assess,
    'assess__single_one_claim': _assess,
    'assess__single_text_several_claims': _assess,
    'assess__list_all_error_rows': _assess,
    'synthetic__assess_low_confidence_row': _assess,
    'synthetic__assess_failed_row_without_failure_block': _assess,
    'synthetic__assess_empty_more_claims': _assess,
    'verify__status_failed_live_retryable': _poll,
    'verify__status_failed_live': _poll,
    'verify__status_not_a_claim': _poll,
    'verify__status_task_stuck': _poll,
    'verify__stored_progress_failed_crashed': _poll,
    'verify__stored_progress_failed_insufficient_evidence': _poll,
    'verify__status_failed_durable': _poll,
    'verify__status_failed_durable_framing': _poll,
    'verify__status_not_a_claim_durable': _poll,
    'verify__status_cancelled_durable': _poll,
    'verify__status_needs_input': _poll,
    'synthetic__needs_input_text_null': _poll,
    'verify__status_completed': _poll,
    'verify__status_processing': _poll,
    'verify__select_202': _select,
    'verify__batch_202': _select,
    'verify__select_no_selection_pending_409': _select,
    'errors__rate_limited_extract': _verify_error,
    'review__429_review_in_flight': _verify_error,
    'synthetic__429_zero_wait_header': _verify_error,
    'synthetic__429_in_flight_no_header': _verify_error,
    'verify__capacity_503': _verify_error,
    'errors__service_unavailable_ask': _verify_error,
    'errors__validation_wrong_type': _verify_error,
    'verify__missing_claim_422': _verify_error,
    'verify__invalid_depth_422': _verify_error,
    'verify__misnamed_field_422': _verify_error,
    'assess__422_no_input_field': _verify_error,
    'verify__blank_claim_422': _verify_error,
    'verify__batch_empty_422': _verify_error,
    'errors__payment_required_verify': _verify_error,
    'errors__not_found_verify_status': _verify_error,
    'account__me_usage_pro_extra': _usage,
    'account__me_usage_free_partly_spent': _usage,
    'account__me_usage_extra_only': _usage,
    # An older body with the pool and prices but no per-capability blocks is
    # still the older shape (the same body stands in for both).
    'synthetic__usage_pool_without_blocks': _usage,
    'verify__list_200': _list,
}.items():
    SCENARIOS[_name] = (_name, _runner)
for _name in ('verify__select_202', 'verify__batch_202'):
    SCENARIOS[f'card_select:{_name}'] = (_name, _card_select)
for _name in (
    'verify__status_failed_live_retryable',
    'verify__status_failed_durable',
    'verify__status_not_a_claim',
    'verify__stored_progress_failed_crashed',
):
    SCENARIOS[f'card_retry:{_name}'] = (_name, _card_retry)

# Where the newer body cannot say what the older one said word for word,
# because the older text is not derivable from it. Each entry names the one
# field that differs and the value the newer body gives instead.
KNOWN_DIFFERENCES: dict[str, dict[str, Any]] = {
    # The older API wrote a failure read back from storage as "Pipeline
    # stopped: <code>." and a live one as "Pipeline stopped at: <code>"; the
    # newer body does not say which it was, so both read as the live form.
    'verify__status_failed_durable': {'message': 'Pipeline stopped at: conclusion_failed'},
    'verify__status_failed_durable_framing': {'message': 'Pipeline stopped at: framing_failed'},
    'verify__status_not_a_claim_durable': {'message': 'Not a verifiable claim.'},
    # The API's own sentence for a blank claim changed with the newer shape.
    'verify__blank_claim_422': {'message': 'claim is required.'},
}


# Deliberate behaviour changes made after the oracle was recorded. The oracle
# stays frozen; each change is applied to it here, by name, so the shape tests
# keep checking everything else byte for byte.
#
# `dissent` is deprecated (the API always sends null): the confidence note no
# longer describes it.
_OLD_ESCALATION_CLAUSE = 'on medium confidence or a dissent offer one;'
_NEW_ESCALATION_CLAUSE = 'on medium confidence offer one;'
_OLD_NOTES_NOTE = (
    " `rationale` is the reasoning of a reviewer who agrees with the panel's verdict; `dissent`, when "
    "set, is the reasoning of the reviewer farthest from it. Both are reviewers' notes, not checked "
    'sources. For sourced evidence, offer the user a deep check; if they agree, call `verify_claim`.'
)


def _since_the_oracle(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    out = dict(value)
    note = out.get('confidence_note')
    if isinstance(note, str):
        note = note.replace(_OLD_ESCALATION_CLAUSE, _NEW_ESCALATION_CLAUSE)
        out['confidence_note'] = note.replace(_OLD_NOTES_NOTE, server.ASSESS_NOTES_NOTE)
    return out


def test_the_oracle_changes_still_apply():
    """Each deliberate change names text the oracle really holds, and the new
    text is what the server says now."""
    raw = ORACLE_PATH.read_text(encoding='utf-8')
    assert _OLD_ESCALATION_CLAUSE in raw
    assert json.dumps(_OLD_NOTES_NOTE, ensure_ascii=False)[1:-1] in raw
    assert _NEW_ESCALATION_CLAUSE in server.ASSESS_ESCALATION_NOTE


def _output(scenario: str, shape: str) -> Any:
    fixture, runner = SCENARIOS[scenario]
    with pytest.MonkeyPatch.context() as m:
        return asyncio.run(runner(m, _response(fixture, shape)))


def _wire_output(scenario: str) -> Any:
    """The scenario with the newer body served over HTTP, through the SDK."""
    fixture, runner = SCENARIOS[scenario]
    return run_through_the_wire(lambda m: runner(m, _response(fixture, 'canonical')))


def sorted_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True)


def _serialized(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)


def _oracle() -> dict[str, Any]:
    return json.loads(ORACLE_PATH.read_text(encoding='utf-8'))


@pytest.mark.skipif(not WRITE_ORACLE, reason='writes the oracle only when asked')
def test_write_the_oracle():
    # Provenance: the server under test must be exactly the oracle commit's.
    source = pathlib.Path(server.__file__).resolve().parent

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603 — fixed git arguments
            ['git', '-C', str(source), *args],  # noqa: S607 — git from PATH, a developer command
            capture_output=True,
            text=True,
            check=False,
        )

    head = git('rev-parse', 'HEAD').stdout.strip()
    assert head == ORACLE_COMMIT, f'the oracle is written only from {ORACLE_COMMIT}, not {head or "no git checkout"}'
    assert git('diff', '--quiet', 'HEAD', '--', '.').returncode == 0, 'the server source differs from the commit'
    oracle = {scenario: _output(scenario, 'legacy') for scenario in SCENARIOS}
    pathlib.Path(WRITE_ORACLE).write_text(_serialized(oracle) + '\n', encoding='utf-8')


skip_while_writing = pytest.mark.skipif(bool(WRITE_ORACLE), reason='the oracle is being written')


@skip_while_writing
def test_every_scenario_has_an_oracle_entry_and_both_shapes():
    assert set(_oracle()) == set(SCENARIOS)
    for fixture, _runner in SCENARIOS.values():
        assert set(SHAPES[fixture]) == set(BOTH), fixture


@skip_while_writing
@pytest.mark.parametrize('scenario', sorted(SCENARIOS))
def test_the_older_body_gives_what_it_gave_before(scenario):
    assert _serialized(_output(scenario, 'legacy')) == _serialized(_since_the_oracle(_oracle()[scenario]))


@skip_while_writing
@pytest.mark.parametrize('scenario', sorted(SCENARIOS))
def test_the_newer_body_gives_the_same(scenario):
    expected = _since_the_oracle(_oracle()[scenario])
    if scenario in KNOWN_DIFFERENCES:
        expected = {**expected, **KNOWN_DIFFERENCES[scenario]}
    assert _serialized(_output(scenario, 'canonical')) == _serialized(expected)


@skip_while_writing
@pytest.mark.parametrize('scenario', sorted(SCENARIOS))
def test_the_newer_body_through_the_sdk_gives_the_same(scenario):
    expected = _since_the_oracle(_oracle()[scenario])
    if scenario in KNOWN_DIFFERENCES:
        expected = {**expected, **KNOWN_DIFFERENCES[scenario]}
    assert sorted_json(_wire_output(scenario)) == sorted_json(expected)


@skip_while_writing
def test_each_known_difference_is_one_message_that_really_differs():
    oracle = _oracle()
    for scenario, override in KNOWN_DIFFERENCES.items():
        assert scenario in SCENARIOS
        assert set(override) == {'message'}
        assert oracle[scenario]['message'] != override['message'], scenario


# ── ask and citation checks: the raw body and the SDK give the same ──
#
# These endpoints have no oracle from before the newer shape. Each scenario runs
# twice on the newer body: once with the client function stubbed to return it
# as it came, once with it served over HTTP and read by the SDK. The tool
# results must be the same (keys sorted).

CITECHECK_SHAPES: dict[str, dict[str, Any]] = json.loads((HERE / 'citecheck_shapes.json').read_text(encoding='utf-8'))
ASK_BODIES: dict[str, ApiResponse] = {
    'answered': ApiResponse(
        status=200, data={'role': 'expert', 'content': 'Because **evidence**.', 'created_at': '2026-10-10T10:00:00Z'}
    ),
    'not_completed_400': ApiResponse(
        status=400,
        data={'detail': 'Ask is only available for completed verifications.', 'code': 'verification_not_ready'},
    ),
    'not_ready_409': ApiResponse(status=409, data={'code': 'verification_not_ready', 'task_id': 'a' * 32}),
    'failed_409': ApiResponse(status=409, data={'code': 'verification_failed', 'task_id': 'a' * 32}),
    'no_credits_402': ApiResponse(
        status=402,
        data={
            'detail': 'No remaining ask credits.',
            'code': 'no_credits',
            'upgrade_url': 'https://lenz.io/plans',
            'credits_remaining': 0,
            'cost': 1,
        },
    ),
    'not_found_404': ApiResponse(status=404, data={'detail': 'Verification not found.', 'code': 'not_found'}),
    'key_reused_422': ApiResponse(
        status=422,
        data={'detail': 'Idempotency-Key reused with a different request body.', 'code': 'idempotency_body_mismatch'},
    ),
    'ask_failed_502': ApiResponse(
        status=502, data={'detail': 'The chat model could not answer this question.', 'code': 'ask_failed'}
    ),
    'unavailable_503': ApiResponse(
        status=503,
        data={'detail': 'The chat model is unavailable right now.', 'code': 'upstream_unavailable', 'retry_after': 90},
        headers={'retry-after': '90'},
    ),
}


def _citecheck_response(name: str) -> ApiResponse:
    entry = CITECHECK_SHAPES[name]['canonical']
    headers = {key.lower(): value for key, value in entry.get('headers', {}).items()}
    return ApiResponse(status=entry['status'], data=entry['body'], headers=headers)


def _both_ways(runner: Callable[[pytest.MonkeyPatch], Any]) -> tuple[str, str]:
    with pytest.MonkeyPatch.context() as m:
        stubbed = asyncio.run(runner(m))
    return sorted_json(stubbed), sorted_json(run_through_the_wire(runner))


@pytest.mark.parametrize('name', sorted(ASK_BODIES))
def test_ask_reads_the_same_through_the_sdk(name):
    def runner(m):
        _stub(m, 'ask', ASK_BODIES[name])
        return server.ask_followup('deadbeef', 'Why?', _ctx())

    stubbed, through = _both_ways(runner)
    assert through == stubbed


_CITECHECK_GETS = sorted(name for name in CITECHECK_SHAPES if name.startswith('get_'))
_CITECHECK_STARTS = [
    'receipt_202',
    'idempotent_replay_202',
    'idempotency_conflict_409',
    '402_no_credits',
    '422_url_input',
    '422_invalid_doi',
    '422_unknown_field',
    'idempotency_body_mismatch_422',
    '429_citecheck_in_flight',
    '503_capacity',
    '503_citations_unavailable',
]


@pytest.mark.parametrize('name', _CITECHECK_GETS)
def test_a_citation_check_reads_the_same_through_the_sdk(name):
    def runner(m):
        _stub(m, 'citecheck_status', _citecheck_response(name))
        return server.get_citation_check('ab12cd34', _ctx())

    stubbed, through = _both_ways(runner)
    assert through == stubbed


@pytest.mark.parametrize('name', _CITECHECK_STARTS)
def test_a_citation_check_start_reads_the_same_through_the_sdk(name):
    def runner(m):
        _stub(m, 'citecheck', _citecheck_response(name))
        _stub(m, 'citecheck_status', _citecheck_response('get_completed_issues_found'))
        return server.check_citations('A draft [a](https://e.org/a).', _ctx())

    stubbed, through = _both_ways(runner)
    assert through == stubbed


def test_the_citation_scenarios_cover_every_start_and_read():
    names = set(CITECHECK_SHAPES)
    covered = set(_CITECHECK_GETS) | set(_CITECHECK_STARTS)
    assert names == covered
