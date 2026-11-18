"""What the model is told when a call is refused or a check fails.

A refusal is the turn where the user most needs a way forward, and the model can
only offer one it has been given. So these results carry the facts the API sent
(the price, the balance, when it resets, what the balance still covers) and say
plainly what each option is, and the model words it for the user.

The API bodies below are in the API's exact shape, with invented values.
"""

from __future__ import annotations

import asyncio
import types

import pytest
from mcp.server.auth.provider import AccessToken

from lenz_mcp import client, config, exchange, oauth, server
from lenz_mcp.client import ApiResponse

PLANS = 'https://lenz.example/plans?wall=00000000-0000-4000-8000-000000000001'


@pytest.fixture(autouse=True)
def _reset_shared_http_client():
    client._http_client = None
    yield
    client._http_client = None


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(server, '_sleep', _instant)
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS', 0.05)
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS_BY_IDENTITY', {'Claude-User': 0.05})


def _ctx(auth='Bearer lenz_testkey'):
    headers = {'authorization': auth} if auth else {}
    return types.SimpleNamespace(request_context=types.SimpleNamespace(request=types.SimpleNamespace(headers=headers)))


def _run(coro):
    return asyncio.run(coro)


def _patch_api(monkeypatch, name, response):
    calls = []

    async def _fake(*args, **kwargs):
        calls.append(kwargs)
        return response

    monkeypatch.setattr(client, name, _fake)
    return calls


def _signed_in(monkeypatch):
    monkeypatch.setattr(config, 'OAUTH_ENABLED', True)
    token = AccessToken(
        token='host-token',
        client_id='oauth-host',
        scopes=[],
        expires_at=None,
        subject='42',
        claims={'auth_mode': oauth.AUTH_MODE_OAUTH, 'sub': '42'},
    )
    monkeypatch.setattr('mcp.server.auth.middleware.auth_context.get_access_token', lambda: token)


# ── sign-in refusals ─────────────────────────────────────────────────


def test_a_key_the_api_refuses_is_not_described_as_missing(monkeypatch):
    """The caller did send a key: "no key was provided" sends them looking for
    a setting they already have. Say it was not accepted, and where to make one."""
    monkeypatch.setattr(config, 'OAUTH_ENABLED', False)
    _patch_api(
        monkeypatch, 'assess', ApiResponse(status=401, data={'detail': 'Unauthorized', 'code': 'not_authenticated'})
    )

    out = _run(server.assess_claim('x', _ctx(auth='Bearer lenz_ffffffffffffffffffffffffffffffff')))

    assert out['status'] == 'auth_required'
    assert 'No Lenz API key was provided' not in out['message']
    assert 'not accept' in out['message']
    assert 'revoked' in out['message']
    assert config.API_CREDENTIALS_URL in out['message']


def test_no_key_at_all_is_still_described_as_missing(monkeypatch):
    monkeypatch.setattr(config, 'OAUTH_ENABLED', False)
    out = _run(server.assess_claim('x', _ctx(auth=None)))
    assert out['status'] == 'auth_required'
    assert out['message'].startswith('No Lenz API key was provided.')


def test_a_signed_in_caller_refused_by_the_api_is_still_told_to_reconnect(monkeypatch):
    _signed_in(monkeypatch)
    out = server._error_result(ApiResponse(status=401, data={'code': 'invalid_token'}))
    assert out == {'status': 'auth_required', 'message': server.OAUTH_REAUTH_MESSAGE}


def test_a_token_without_the_scope_says_to_reconnect(monkeypatch):
    """A bare "forbidden" gives the user nothing to do. The connection lacks a
    permission, and reconnecting grants it: the same remedy, and the same
    status, as an expired sign-in."""
    _signed_in(monkeypatch)
    body = {'detail': 'The access token does not allow this call.', 'code': 'insufficient_scope', 'scope': 'assess'}

    out = server._error_result(ApiResponse(status=403, data=body))

    assert out == {'status': 'auth_required', 'message': server.MISSING_PERMISSION_MESSAGE}
    assert 'permission' in out['message']
    assert 'reconnect' in out['message'].lower()


def test_an_exchange_refused_for_scope_says_the_same_thing():
    out = server._exchange_failure_result(exchange.ExchangeFailed('scope', 'invalid_scope'))
    assert out == {'status': 'auth_required', 'message': server.MISSING_PERMISSION_MESSAGE}


def test_other_403s_stay_forbidden(monkeypatch):
    out = server._error_result(ApiResponse(status=403, data={'detail': 'Forbidden.'}))
    assert out['status'] == 'forbidden'
    blocked = server._exchange_failure_result(exchange.ExchangeFailed('blocked', 'access_denied'))
    assert blocked['status'] == 'forbidden'


# ── a failed deep check ──────────────────────────────────────────────


def _failed(code, detail, failure_class='internal', retryable=False, hint=None):
    return ApiResponse(
        status=200,
        data={
            'status': 'failed',
            'task_id': 'f' * 32,
            'failure': {
                'code': code,
                'detail': detail,
                'hint': hint,
                'failure_class': failure_class,
                'retryable': retryable,
                'docs_url': 'https://lenz.example/docs/errors#internal',
            },
        },
    )


@pytest.mark.parametrize(
    'detail',
    [
        'The check stopped on an error on our side.',
        'No sources about the claim were found.',
        'A service the check depends on was unavailable.',
    ],
)
def test_a_failed_deep_check_relays_the_reason_and_says_nothing_was_charged(monkeypatch, detail):
    """ "Pipeline stopped at: internal" tells the user nothing. The API states
    the reason in a sentence, and a deep check is charged only when it
    completes, which the user will want to know."""
    _patch_api(monkeypatch, 'verify_status', _failed('internal', detail))

    out = _run(server.get_verification('f' * 32, _ctx()))

    assert out['status'] == 'failed'
    assert 'Pipeline' not in out['message']
    assert out['message'].startswith(detail)
    assert 'Nothing was charged' in out['message']
    assert out['credits_charged'] == 0
    assert out['failure_reason'] == 'internal'
    assert out['failure_class'] == 'internal'
    assert out['retryable'] is False


def test_a_failed_deep_check_with_no_sentence_still_says_nothing_was_charged(monkeypatch):
    _patch_api(
        monkeypatch,
        'verify_status',
        ApiResponse(status=200, data={'status': 'failed', 'failure': {'code': 'conclusion_failed'}}),
    )
    out = _run(server.get_verification('f' * 32, _ctx()))
    assert 'Pipeline' not in out['message']
    assert 'Nothing was charged' in out['message']


def test_the_api_hint_written_for_integrators_stays_out(monkeypatch):
    """`failure.hint` is developer copy and can name an endpoint the model has no tool for."""
    hint = 'Send one factual claim, or run the text through /extract to enumerate its claims.'
    _patch_api(monkeypatch, 'verify_status', _failed('no_checkable_claim', 'No claim could be checked.', hint=hint))
    out = _run(server.get_verification('f' * 32, _ctx()))
    assert '/extract' not in str(out)
    assert out['message'].startswith('No claim could be checked.')


def test_too_few_sources_says_nothing_was_charged_once(monkeypatch):
    _patch_api(
        monkeypatch,
        'verify_status',
        _failed('research_empty', 'No sources about the claim were found.', 'insufficient_evidence'),
    )
    out = _run(server.get_verification('f' * 32, _ctx()))
    assert out['message'] == server.INSUFFICIENT_EVIDENCE_MESSAGE
    assert out['message'].lower().count('nothing was charged') == 1
