"""The tools read both shapes of the Lenz API's responses, and say the same thing.

The API answers in an older shape and a newer one. `api_shapes.json` holds the
same response in each (endpoint and outcome). `api_shapes_expected.json` is the
oracle: what each tool returned for the OLDER body before this server could
read the newer one, recorded once from that earlier code and frozen. Every
scenario here runs the tool on both bodies and requires exactly the oracle's
output from each, serialized byte for byte.

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


def _stub(m: pytest.MonkeyPatch, api: str, response: ApiResponse) -> None:
    async def _fake(*_args, **_kwargs):
        return response

    m.setattr(client, api, _fake)


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


def _output(scenario: str, shape: str) -> Any:
    fixture, runner = SCENARIOS[scenario]
    with pytest.MonkeyPatch.context() as m:
        return asyncio.run(runner(m, _response(fixture, shape)))


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
    assert _serialized(_output(scenario, 'legacy')) == _serialized(_oracle()[scenario])


@skip_while_writing
@pytest.mark.parametrize('scenario', sorted(SCENARIOS))
def test_the_newer_body_gives_the_same(scenario):
    expected = _oracle()[scenario]
    if scenario in KNOWN_DIFFERENCES:
        expected = {**expected, **KNOWN_DIFFERENCES[scenario]}
    assert _serialized(_output(scenario, 'canonical')) == _serialized(expected)


@skip_while_writing
def test_each_known_difference_is_one_message_that_really_differs():
    oracle = _oracle()
    for scenario, override in KNOWN_DIFFERENCES.items():
        assert scenario in SCENARIOS
        assert set(override) == {'message'}
        assert oracle[scenario]['message'] != override['message'], scenario
