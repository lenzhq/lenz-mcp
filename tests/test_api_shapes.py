"""The tools read both shapes of the Lenz API's responses.

The API answers in an older shape and a newer one. `api_shapes.json` holds the
same response in each (endpoint, outcome), and every test here runs a tool on
both and checks two things: the tool result is the SAME whichever shape
arrived, and it says what it said before the newer shape existed. The card and
the model therefore see one vocabulary either way.
"""

import asyncio
import json
import pathlib
import types
from typing import Any

import pytest

from lenz_mcp import client, config, server
from lenz_mcp.client import ApiResponse

SHAPES: dict[str, dict[str, Any]] = json.loads((pathlib.Path(__file__).parent / 'api_shapes.json').read_text())
BOTH = ('legacy', 'canonical')


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(server, '_sleep', _instant)
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS', 0.05)
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS_BY_IDENTITY', {'Claude-User': 0.05})


def _ctx():
    request = types.SimpleNamespace(headers={'authorization': 'Bearer lenz_testkey'})
    return types.SimpleNamespace(request_context=types.SimpleNamespace(request=request))


def _response(name: str, shape: str) -> ApiResponse:
    entry = SHAPES[name][shape]
    return ApiResponse(status=entry['status'], data=entry['body'], headers=entry['headers'])


def _stub(monkeypatch, api: str, response: ApiResponse) -> None:
    async def _fake(*_args, **_kwargs):
        return response

    monkeypatch.setattr(client, api, _fake)


def _both(monkeypatch, run) -> dict[str, Any]:
    """Run ``run(shape)`` for each shape; assert the results match; return one."""
    results = {}
    for shape in BOTH:
        with monkeypatch.context() as m:
            results[shape] = run(m, shape)
    assert results['legacy'] == results['canonical']
    return results['legacy']


def _run(coro):
    return asyncio.run(coro)


def test_every_fixture_has_both_shapes():
    for name, entry in SHAPES.items():
        assert set(entry) == set(BOTH), name


# ── assess ───────────────────────────────────────────────────────────


def test_assess_rows_read_alike_in_both_shapes(monkeypatch):
    def run(m, shape):
        _stub(m, 'assess', _response('assess__list_mixed_rows', shape))
        return _run(server.assess_claim(ctx=_ctx(), claims=['a', 'b', 'c', 'd']))

    out = _both(monkeypatch, run)
    rows = out['claims']
    assert rows[0] == {'claim': 'Water boils at 100 C at sea level.', 'verdict': 'True', 'confidence': 'high'}
    # A failed row keeps the older reading: verdict Error, confidence low, the
    # older per-endpoint word for "nothing checkable", and the hint.
    assert rows[1]['verdict'] == 'Error'
    assert rows[1]['confidence'] == 'low'
    assert rows[1]['error'] == 'no_claim'
    assert rows[1]['hint'].startswith('The input is a greeting.')
    assert rows[2]['error'] == 'upstream_unavailable'
    assert rows[3]['error'] == 'framing_failed'
    assert not any(row.get('recommend_verify') for row in rows)


def test_assess_unchecked_extras_read_from_either_name(monkeypatch):
    for shape in BOTH:
        with monkeypatch.context() as m:
            _stub(m, 'assess', _response('assess__list_compound_item', shape))
            out = _run(server.assess_claim(ctx=_ctx(), claims=['x', 'y']))
        assert out['claims'][0]['identified_claims'] == ['Second claim.', 'Third claim.'], shape
        assert 'identified_claims' not in out['claims'][1], shape


def test_assess_one_claim_reads_alike(monkeypatch):
    def run(m, shape):
        _stub(m, 'assess', _response('assess__single_one_claim', shape))
        return _run(server.assess_claim('The Earth is round.', _ctx()))

    assert _both(monkeypatch, run)['status'] == 'ok'


@pytest.mark.parametrize('shape', BOTH)
def test_assess_no_claim_in_either_shape(monkeypatch, shape):
    _stub(monkeypatch, 'assess', _response('assess__single_no_claim', shape))
    out = _run(server.assess_claim('hello there', _ctx()))
    assert out['status'] == 'no_claim'
    # The API's own sentence: `failure.detail` in the newer shape, `error` in the older.
    body = SHAPES['assess__single_no_claim'][shape]['body']
    expected = body['failure']['detail'] if shape == 'canonical' else body['error']
    assert out['message'] == expected


def test_a_low_confidence_verdict_row_still_recommends_the_deep_check(monkeypatch):
    rows = {
        'legacy': {'claim': 'c', 'verdict': 'Mixed', 'confidence': 'low', 'error_code': None, 'identified_claims': []},
        'canonical': {
            'claim': 'c',
            'status': 'completed',
            'verdict': 'Mixed',
            'confidence': 'low',
            'more_claims': [],
            'failure': None,
        },
    }

    def run(m, shape):
        _stub(m, 'assess', ApiResponse(status=200, data={'claims': [rows[shape]]}))
        return _run(server.assess_claim('c', _ctx()))

    assert _both(monkeypatch, run)['claims'][0]['recommend_verify'] is True


# ── deep check: poll ─────────────────────────────────────────────────


def _poll(m, name, shape):
    async def _status(_auth, *, task_id):
        return _response(name, shape)

    m.setattr(client, 'verify_status', _status)
    return _run(server.get_verification('a' * 32, _ctx()))


@pytest.mark.parametrize(
    ('name', 'reason', 'failure_class', 'retryable'),
    [
        ('verify__status_failed_live_retryable', 'adjudication_failed', 'upstream_unavailable', True),
        ('verify__status_failed_durable', 'conclusion_failed', 'internal', False),
        # The newer API's `no_checkable_claim` reads as the older `not_a_claim`.
        ('verify__status_not_a_claim', 'not_a_claim', 'invalid_input', False),
    ],
)
def test_a_failed_check_reads_its_reason_from_either_shape(monkeypatch, name, reason, failure_class, retryable):
    results = {}
    for shape in BOTH:
        with monkeypatch.context() as m:
            results[shape] = _poll(m, name, shape)
    for shape, out in results.items():
        assert out['status'] == 'failed', shape
        assert out['failure_reason'] == reason, shape
        assert out['failure_class'] == failure_class, shape
        assert out['retryable'] is retryable, shape
        assert out['message'], shape
    # Only the API's sentence may differ between the shapes.
    strip = [{k: v for k, v in out.items() if k != 'message'} for out in results.values()]
    assert strip[0] == strip[1]
    assert results['canonical']['message'] == SHAPES[name]['canonical']['body']['failure']['detail']
    assert results['legacy']['message'] == SHAPES[name]['legacy']['body']['error']


def test_the_claim_picker_reads_alike_in_both_shapes(monkeypatch):
    out = _both(monkeypatch, lambda m, shape: _poll(m, 'verify__status_needs_input', shape))
    assert out['status'] == 'needs_input'
    # Options keep `text`, which the card and older tool results use.
    assert out['claims'] == [
        {'text': 'The Earth is round.', 'domain': 'Science'},
        {'text': 'Water boils at 100C at sea level.', 'domain': 'Science'},
    ]
    assert '1. The Earth is round.' in out['message']
    assert '2. Water boils at 100C at sea level.' in out['message']


def test_a_completed_check_reads_alike_in_both_shapes(monkeypatch):
    out = _both(monkeypatch, lambda m, shape: _poll(m, 'verify__status_completed', shape))
    assert out['status'] == 'completed'
    assert out['claim'] == 'The Earth is round.'
    assert out['lenz_score'] == 9


# ── select ───────────────────────────────────────────────────────────


@pytest.mark.parametrize('name', ['verify__select_202', 'verify__batch_202'])
def test_select_receipt_items_read_the_claim_from_either_name(monkeypatch, name):
    def run(m, shape):
        _stub(m, 'select', _response(name, shape))
        return _run(server.select_claims('a' * 32, ['x', 'y'], _ctx()))

    out = _both(monkeypatch, run)
    assert [c['claim'] for c in out['claims']] == ['The Earth is round.', 'Water boils at 100C at sea level.']


def test_the_card_select_reads_the_claim_from_either_name(monkeypatch):
    monkeypatch.setattr(config, 'CARD_ENABLED', True)

    def run(m, shape):
        _stub(m, 'select', _response('verify__select_202', shape))
        return _run(server._select_claims_for_card('a' * 32, ['x', 'y'], _ctx()))

    out = _both(monkeypatch, run)
    assert [c['claim'] for c in out['claims']] == ['The Earth is round.', 'Water boils at 100C at sea level.']


@pytest.mark.parametrize(
    ('name', 'allowed'),
    [('verify__status_failed_live_retryable', True), ('verify__status_failed_durable', False)],
)
@pytest.mark.parametrize('shape', BOTH)
def test_a_card_retry_reads_the_retry_signal_from_either_shape(monkeypatch, name, allowed, shape):
    monkeypatch.setattr(config, 'CARD_ENABLED', True)

    async def _status(_auth, *, task_id):
        return _response(name, shape)

    started = []

    async def _verify(_auth, **kwargs):
        started.append(kwargs)
        return ApiResponse(status=202, data={'task_id': 'n' * 32, 'status': 'queued'})

    monkeypatch.setattr(client, 'verify_status', _status)
    monkeypatch.setattr(client, 'verify', _verify)
    out = _run(server._start_verification_for_card('The claim.', _ctx(), retry_of='f' * 32))
    assert (out['status'] == 'submitted') is allowed
    assert bool(started) is allowed


# ── errors ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ('name', 'seconds'), [('errors__rate_limited_extract', 900), ('review__429_review_in_flight', 60)]
)
def test_a_429_reads_the_wait_from_either_name(monkeypatch, name, seconds):
    def run(m, shape):
        entry = SHAPES[name][shape]
        # No header: the body alone must carry the wait.
        _stub(m, 'verify', ApiResponse(status=entry['status'], data=entry['body']))
        return _run(server.verify_claim('x', _ctx()))

    out = _both(monkeypatch, run)
    assert out['status'] == 'rate_limited'
    assert out['retry_after_seconds'] == seconds


def test_a_503_reads_alike_in_both_shapes(monkeypatch):
    def run(m, shape):
        _stub(m, 'verify', _response('verify__capacity_503', shape))
        return _run(server.verify_claim('x', _ctx()))

    out = _both(monkeypatch, run)
    assert out['status'] == 'service_unavailable'
    assert out['retry_after_seconds'] == 90


def test_a_402_reads_alike_in_both_shapes(monkeypatch):
    def run(m, shape):
        _stub(m, 'verify', _response('errors__payment_required_verify', shape))
        return _run(server.verify_claim('x', _ctx()))

    assert _both(monkeypatch, run)['status'] == 'quota_exhausted'


def test_a_409_no_selection_pending_reads_alike(monkeypatch):
    def run(m, shape):
        _stub(m, 'select', _response('verify__select_no_selection_pending_409', shape))
        return _run(server.select_claims('a' * 32, ['x'], _ctx()))

    out = _both(monkeypatch, run)
    assert out == {'status': 'already_resolved', 'message': 'This task has no pending claim selection.'}


@pytest.mark.parametrize('shape', BOTH)
def test_a_validation_422_is_an_invalid_request_in_either_shape(monkeypatch, shape):
    _stub(monkeypatch, 'verify', _response('errors__validation_wrong_type', shape))
    out = _run(server.verify_claim('x', _ctx()))
    assert out['status'] == 'invalid_request'
    assert out['message']


# ── usage ────────────────────────────────────────────────────────────


@pytest.mark.parametrize('name', ['account__me_usage_pro_extra', 'account__me_usage_free_partly_spent'])
def test_usage_reads_alike_in_both_shapes(monkeypatch, name):
    def run(m, shape):
        _stub(m, 'me_usage', _response(name, shape))
        return _run(server.check_usage(_ctx()))

    out = _both(monkeypatch, run)
    legacy = SHAPES[name]['legacy']['body']
    # The per-capability projections follow from the pool and the price list
    # when the per-capability blocks are absent.
    assert out['assess_remaining'] == legacy['assess']['remaining']
    assert out['verify_remaining'] == legacy['verify']['remaining']
    assert out['credits_remaining'] == legacy['credits']['remaining']
    assert out['resets_at']
