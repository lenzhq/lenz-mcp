"""What the model is told when a call is refused or a check fails.

A refusal is the turn where the user most needs a way forward, and the model can
only offer one it has been given. So these results carry the facts the API sent
(the price, the balance, when it resets) and say plainly what each option is, and
the model words it for the user. They carry no list of cheaper checks and make no
extra API call: a cheaper check is the user's choice, and the model is told so.

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


# ── a text with nothing checkable in it ──────────────────────────────


def _no_claim_failure():
    return {
        'code': 'no_checkable_claim',
        'detail': 'No claim in the input could be checked against public evidence.',
        'hint': 'The input is an opinion. Send one factual claim, or run the text through /extract.',
        'failure_class': 'invalid_input',
        'retryable': False,
        'docs_url': 'https://lenz.example/docs/errors#invalid-input',
    }


def _assess_no_claim_answer():
    return ApiResponse(
        status=200,
        data={'status': 'no_checkable_claim', 'claims': [], 'failure': _no_claim_failure(), 'more_claims': []},
    )


def _checked_row(claim, verdict='True', confidence='high'):
    return {
        'claim': claim,
        'language': 'en',
        'status': 'completed',
        'verdict': verdict,
        'confidence': confidence,
        'verification_url': None,
        'rationale': None,
        'dissent': None,
        'suggested_rewrite': None,
        'more_claims': [],
        'failure': None,
    }


def _no_claim_row(claim):
    return {
        **_checked_row(claim),
        'status': 'failed',
        'verdict': None,
        'confidence': None,
        'failure': _no_claim_failure(),
    }


def _says_what_to_do(step, lead):
    """The next step is the lead, then what Lenz checks in the instructions' own words, then what to do."""
    assert step.startswith(lead)
    # What is and is not checkable is the server instructions' sentence, not a second list.
    checkable = (
        'It checks factual claims, such as a fact, a figure, a date, a quote or an attribution; opinions, '
        'predictions, arithmetic, how programming code behaves and text with no checkable claim are outside '
        'what it can check.'
    )
    assert checkable in server.mcp.instructions
    assert checkable.replace('It checks', 'Lenz checks') in step
    assert 'Ask them for a claim to check' in step
    assert 'Do not send the same text again unchanged' in step
    # The restatement is the instructions' own rule, and never an invented claim.
    assert (
        'one claim as a statement, with what the conversation says it refers to and nothing else changed'
    ) in server.mcp.instructions
    assert 'only if the conversation makes clear what they mean' in step
    assert 'with what the conversation says it refers to and nothing else changed' in step
    assert 'Never make up a claim the user did not make' in step


def test_a_quick_check_with_no_claim_says_nothing_was_checked_and_what_to_do(monkeypatch):
    calls = _patch_api(monkeypatch, 'assess', _assess_no_claim_answer())

    out = _run(server.assess_claim('Pineapple on pizza is a crime.', _ctx()))

    assert out['status'] == 'no_claim'
    assert out['message'] == 'No verifiable claim detected'
    _says_what_to_do(out['next_step'], 'Nothing was checked and nothing was charged.')
    # The API's hint is written for an integrator and names an endpoint the model has no tool for.
    assert '/extract' not in str(out)
    assert set(out) == {'status', 'message', 'next_step'}
    assert len(calls) == 1


def test_a_quick_check_answer_with_no_rows_and_another_reason_gets_no_such_next_step(monkeypatch):
    """Only a no-claim answer is known to be uncharged and the text's own doing."""
    body = {
        'status': 'error',
        'claims': [],
        'failure': {'code': 'internal', 'detail': 'The check stopped on our side.', 'retryable': False},
        'more_claims': [],
    }
    _patch_api(monkeypatch, 'assess', ApiResponse(status=200, data=body))

    out = _run(server.assess_claim('Acme was founded in 1999.', _ctx()))

    assert out['status'] == 'no_claim'
    assert 'next_step' not in out


def test_a_list_says_what_to_do_on_the_unchecked_items_only(monkeypatch):
    """The rows that were checked read exactly as before; the item with no claim says what to do."""
    body = {
        'status': 'ok',
        'claims': [
            _checked_row('Water boils at 100 C at sea level.'),
            _no_claim_row('I think pineapple on pizza is wrong.'),
            _checked_row('The Danube flows through ten countries.', 'Mostly True', 'low'),
        ],
        'failure': None,
        'more_claims': [],
    }
    _patch_api(monkeypatch, 'assess', ApiResponse(status=200, data=body))

    out = _run(
        server.assess_claim(
            ctx=_ctx(),
            claims=['Water boils at 100 C at sea level.', 'I think pineapple on pizza is wrong.', 'The Danube.'],
        )
    )

    first, none_found, last = out['claims']
    assert first == {'claim': 'Water boils at 100 C at sea level.', 'verdict': 'True', 'confidence': 'high'}
    assert 'next_step' not in first
    assert none_found['error'] == 'no_claim'
    _says_what_to_do(none_found['next_step'], 'This item was not checked and was not charged.')
    # A low-confidence row keeps the recommendation it always had, and nothing of the no-claim step.
    assert last['next_step'] == server.LOW_CONFIDENCE_NEXT_STEP
    assert 'next_step' not in out


def test_a_list_row_that_failed_for_another_reason_gets_no_such_next_step(monkeypatch):
    slow = {
        **_checked_row('A slow one.'),
        'status': 'failed',
        'verdict': None,
        'confidence': None,
        'failure': {'code': 'timeout', 'detail': 'Out of time.', 'hint': 'Retry it.', 'retryable': True},
    }
    body = {'status': 'ok', 'claims': [_checked_row('A.'), slow], 'failure': None, 'more_claims': []}
    _patch_api(monkeypatch, 'assess', ApiResponse(status=200, data=body))

    out = _run(server.assess_claim(ctx=_ctx(), claims=['A.', 'A slow one.']))

    assert out['claims'][1]['error'] == 'timeout'
    assert 'next_step' not in out['claims'][1]


def test_a_normal_quick_check_carries_no_no_claim_text(monkeypatch):
    body = {'status': 'ok', 'claims': [_checked_row('Water boils at 100 C at sea level.')], 'failure': None}
    _patch_api(monkeypatch, 'assess', ApiResponse(status=200, data={**body, 'more_claims': []}))

    out = _run(server.assess_claim('Water boils at 100 C at sea level.', _ctx()))

    assert 'checks factual claims' not in str(out)
    assert 'next_step' not in out and 'next_step' not in out['claims'][0]


def test_a_deep_check_with_no_claim_says_nothing_was_checked_and_what_to_do(monkeypatch):
    _patch_api(
        monkeypatch,
        'verify_status',
        _failed('no_checkable_claim', 'No claim in the input could be checked.', 'invalid_input'),
    )

    out = _run(server.get_verification('f' * 32, _ctx()))

    assert out['status'] == 'failed'
    assert out['message'].startswith('No claim in the input could be checked.')
    assert out['credits_charged'] == 0
    assert out['failure_reason'] == 'not_a_claim'
    _says_what_to_do(out['next_step'], 'Nothing was checked and nothing was charged.')


def test_a_deep_check_that_fails_for_another_reason_gets_no_such_next_step(monkeypatch):
    _patch_api(monkeypatch, 'verify_status', _failed('internal', 'The check stopped on an error on our side.'))
    out = _run(server.get_verification('f' * 32, _ctx()))
    assert 'next_step' not in out


def test_a_deep_check_run_to_a_no_claim_answers_the_same_in_verify_claim_and_select_claims(monkeypatch):
    """The wait inside `verify_claim` and `select_claims` reads the same failed status."""
    _patch_api(monkeypatch, 'verify', ApiResponse(status=202, data={'task_id': 'a' * 32}))
    _patch_api(monkeypatch, 'select', ApiResponse(status=202, data={'items': [{'task_id': 'b' * 32, 'claim': 'X.'}]}))
    _patch_api(
        monkeypatch,
        'verify_status',
        _failed('no_checkable_claim', 'No claim in the input could be checked.', 'invalid_input'),
    )

    verified = _run(server.verify_claim('Pineapple on pizza is a crime.', _ctx()))
    selected = _run(server.select_claims('c' * 32, ['X.'], _ctx()))

    for out in (verified, selected):
        assert out['status'] == 'failed'
        _says_what_to_do(out['next_step'], 'Nothing was checked and nothing was charged.')


def test_a_batch_of_deep_checks_says_it_on_the_unchecked_items_only(monkeypatch):
    _patch_api(monkeypatch, 'verify', ApiResponse(status=202, data={'task_id': 'a' * 32}))
    answers = {
        'a' * 32: ApiResponse(
            status=200,
            data={'status': 'completed', 'result': {'verification_id': 'a1b2c3d4', 'claim': 'A.', 'verdict': 'True'}},
        )
    }
    body = _failed('no_checkable_claim', 'No claim in the input could be checked.', 'invalid_input')
    calls = {'n': 0}

    async def _status(*args, **kwargs):
        calls['n'] += 1
        return answers['a' * 32] if calls['n'] == 1 else body

    monkeypatch.setattr(client, 'verify_status', _status)
    ids = iter(['a' * 32, 'b' * 32])

    async def _submit(*args, **kwargs):
        return ApiResponse(status=202, data={'task_id': next(ids)})

    monkeypatch.setattr(client, 'verify', _submit)

    out = _run(server.verify_claim(ctx=_ctx(), claims=['A.', 'I think so.']))

    done, none_found = out['items']
    assert done['status'] == 'completed' and 'next_step' not in done
    assert none_found['status'] == 'failed'
    _says_what_to_do(none_found['next_step'], 'Nothing was checked and nothing was charged.')
    assert 'next_step' not in out


# ── out of credits ───────────────────────────────────────────────────

RESETS = '2026-12-01T00:00:00+00:00'


def _no_credits(**fields):
    body = {
        'detail': 'No remaining claim checks.',
        'code': 'no_credits',
        'docs_url': 'https://lenz.example/docs/errors#quota',
        'upgrade_url': PLANS,
        'wall_id': '00000000-0000-4000-8000-000000000001',
        'remaining': 0,
        'resets_at': RESETS,
        'credits_remaining': 0,
        'cost': 10,
    }
    body.update(fields)
    return ApiResponse(status=402, data=body)


@pytest.fixture
def no_usage_read(monkeypatch):
    """`GET /me/usage` fails the test if a refusal reaches for it."""
    calls = []

    async def _forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError('a refusal must not read the usage endpoint')

    monkeypatch.setattr(client, 'me_usage', _forbidden)
    return calls


@pytest.fixture
def chatgpt(monkeypatch):
    monkeypatch.setattr(server, 'request_is_chatgpt', lambda: True)


def test_out_of_credits_forwards_the_facts_and_names_the_options(monkeypatch):
    _patch_api(monkeypatch, 'verify', _no_credits())

    out = _run(server.verify_claim('Acme was founded in 1999.', _ctx()))

    assert out['status'] == 'quota_exhausted'
    assert out['cost'] == 10
    assert out['credits_remaining'] == 0
    assert out['resets_at'] == RESETS
    assert out['manage_url'] == PLANS
    assert (
        out['message']
        == 'Out of Lenz credits: this call costs 10 and the account has 0. Monthly credits renew on 1 December 2026.'
    )
    step = out['next_step']
    assert 'nothing was charged' in step
    # The link is named for what it is, and as something for the user.
    assert '`manage_url`' in step and 'add credits or change plan' in step and 'give' in step
    assert 'monthly credits renew on the date in the message' in step
    assert 'refused again' in step
    assert 'balance_covers' not in out


def test_a_cheaper_check_is_the_users_choice_on_every_refusal(monkeypatch):
    """The model already knows the prices; what it must not do is spend the
    user's last credits on a smaller check nobody chose. Stated as a rule with no
    price and no call to make, so it fits a deep check, a batch and a citation check."""
    _patch_api(monkeypatch, 'verify', _no_credits(credits_remaining=7))

    out = _run(server.verify_claim('Acme was founded in 1999.', _ctx()))

    assert server.QUOTA_CHEAPER_IS_THE_USERS in out['next_step']
    rule = server.QUOTA_CHEAPER_IS_THE_USERS
    assert "the user's choice" in rule and 'only when the user says yes' in rule
    assert not any(ch.isdigit() for ch in rule)
    for recipe in ('`', 'depth', 'verify_claim', 'assess_claim', 'check_citations', 'max_citations'):
        assert recipe not in rule, recipe


def test_a_short_balance_states_the_numbers_and_nothing_about_what_it_covers(monkeypatch, no_usage_read):
    """7 credits against a call that costs 10: the refusal says so, and that is all.
    The model works out what 7 credits buy from the prices it already has."""
    _patch_api(monkeypatch, 'verify', _no_credits(credits_remaining=7))

    out = _run(server.verify_claim('Acme was founded in 1999.', _ctx()))

    assert no_usage_read == []
    assert out['message'] == (
        'Not enough Lenz credits: this call costs 10 and the account has 7. Monthly credits renew on 1 December 2026.'
    )
    assert out['credits_remaining'] == 7
    assert 'balance_covers' not in out
    assert 'still pay for' not in out['message'] and 'balance_covers' not in out['next_step']


def test_chatgpt_gets_the_facts_but_no_link_and_no_call_to_buy(monkeypatch, chatgpt):
    _patch_api(monkeypatch, 'verify', _no_credits())

    out = _run(server.verify_claim('Acme was founded in 1999.', _ctx()))

    assert 'manage_url' not in out
    assert out['resets_at'] == RESETS
    assert '1 December 2026' in out['message']
    text = f'{out["message"]} {out["next_step"]}'.lower()
    for commercial in ('manage_url', 'plan', 'buy', 'purchase', 'upgrade', 'add credits', 'http'):
        assert commercial not in text, commercial
    assert 'monthly credits renew on the date in the message' in out['next_step']
    assert server.QUOTA_CHEAPER_IS_THE_USERS in out['next_step']


def test_a_refusal_without_a_renewal_date_in_its_body_leaves_the_date_out(monkeypatch, no_usage_read):
    """The date comes from the 402 and from nowhere else: no body date, no date, no call."""
    body = _no_credits(credits_remaining=7).data
    del body['resets_at']
    _patch_api(monkeypatch, 'verify', ApiResponse(status=402, data=body))

    out = _run(server.verify_claim('x', _ctx()))

    assert no_usage_read == []
    assert 'resets_at' not in out
    assert 'renew' not in out['message'] and 'renew' not in out['next_step']
    assert out['message'] == 'Not enough Lenz credits: this call costs 10 and the account has 7.'
    assert server.QUOTA_CHEAPER_IS_THE_USERS in out['next_step']


def test_several_deep_checks_report_the_whole_price(monkeypatch):
    """Three claims at 10 each is 30, not "this call costs 10"."""
    _patch_api(monkeypatch, 'verify', _no_credits())

    out = _run(server.verify_claim('', _ctx(), claims=['a', 'b', 'c']))

    assert out['status'] == 'quota_exhausted'
    assert out['cost'] == 30
    assert out['requested'] == 3
    assert 'costs 30' in out['message']
    assert '10 for each of 3' in out['message']
    assert server.QUOTA_CHEAPER_IS_THE_USERS in out['next_step']


def test_a_quick_check_list_gives_its_size_and_price_and_no_list_of_options(monkeypatch, no_usage_read):
    body = {'detail': 'Insufficient /assess quota for 3 claims.', 'requested': 3, 'remaining': 2}
    _patch_api(monkeypatch, 'assess', _no_credits(**body, credits_remaining=2, cost=3))

    out = _run(server.assess_claim('', _ctx(), claims=['a', 'b', 'c']))

    assert no_usage_read == []
    assert out['message'] == (
        'Not enough Lenz credits: this call costs 3 (1 for each of 3 claims) and the account has 2. '
        'Monthly credits renew on 1 December 2026.'
    )
    assert out['requested'] == 3
    assert 'balance_covers' not in out
    assert server.QUOTA_CHEAPER_IS_THE_USERS in out['next_step']


def test_a_deep_check_selection_gives_its_price_and_no_list_of_options(monkeypatch, no_usage_read):
    body = {'detail': 'Insufficient credits for selection.', 'requested': 2, 'remaining': 1}
    _patch_api(monkeypatch, 'select', _no_credits(**body, credits_remaining=12, cost=20))

    out = _run(server.select_claims('a' * 32, ['first', 'second'], _ctx()))

    assert no_usage_read == []
    assert out['cost'] == 20 and out['requested'] == 2 and out['credits_remaining'] == 12
    assert 'balance_covers' not in out
    assert server.QUOTA_CHEAPER_IS_THE_USERS in out['next_step']


@pytest.mark.parametrize('balance', [0, 7])
def test_no_refusal_reads_the_usage_endpoint(monkeypatch, no_usage_read, balance):
    _patch_api(monkeypatch, 'verify', _no_credits(credits_remaining=balance))
    _run(server.verify_claim('x', _ctx()))
    _patch_api(monkeypatch, 'assess', _no_credits(credits_remaining=balance, cost=1))
    _run(server.assess_claim('x', _ctx()))
    assert no_usage_read == []


def test_a_successful_call_reads_no_usage(monkeypatch, no_usage_read):
    _patch_api(monkeypatch, 'assess', ApiResponse(status=200, data={'failure': None, 'claims': [], 'more_claims': []}))
    _run(server.assess_claim('x', _ctx()))
    assert no_usage_read == []


# ── a citation check short of credits ────────────────────────────────

PAIRS = [
    {'statement': 'Acme was founded in 1999.', 'url': 'https://example.org/a'},
    {'statement': 'Acme employs 400 people.', 'url': 'https://example.org/b'},
    {'statement': 'Acme is based in Oslo.', 'url': 'https://example.org/c'},
]


def _short_failure():
    return {
        'code': 'insufficient_credits',
        'detail': 'The account did not have enough credits for this work.',
        'hint': 'This check needs 3 credits, one per citation it checks. Nothing was charged.',
        'failure_class': 'invalid_input',
        'retryable': False,
        'docs_url': 'https://lenz.example/docs/errors#invalid-input',
    }


def _short_citecheck():
    """A citation check the balance did not cover: the API ends it as failed."""
    rows = []
    for index, pair in enumerate(PAIRS):
        rows.append(
            {
                'index': index,
                'reference': pair['url'],
                'cited_url': pair['url'],
                'doi': None,
                'statement': pair['statement'],
                'quotes': [],
                'position': None,
                'result': None,
                'check': {'status': 'failed', 'failure': _short_failure()},
            }
        )
    return ApiResponse(
        status=200,
        data={
            'citecheck_id': 'ab12cd34',
            'status': 'failed',
            'outcome': 'unchecked',
            'language': 'en',
            'poll_after_seconds': None,
            'policy': {'max_citations': 3},
            'summary': {
                'citations_found': 3,
                'citations_selected': 3,
                'citation_limit': 3,
                'citation_limit_exceeded': False,
                'citation_checks': {'checked': 0, 'unchecked': 0, 'failed': 3},
                'citation_issues': 0,
            },
            'credits': {'charged': 0},
            'citations': rows,
            'citation_issues': [],
            'failure': _short_failure(),
            'more_citations': [],
        },
    )


def _pairs():
    return [server.CitationPair(**p) for p in PAIRS]


def test_a_citation_check_short_of_credits_reads_as_a_credit_problem(monkeypatch, no_usage_read):
    """It used to come back as a failed check of the input, "the same input sent
    again returns this same result for a day": wrong on both counts. It is a
    refusal for credits, with the link and the citations that were not checked.
    The record states neither the balance nor a renewal date, so the result
    says what happened when it was submitted and asks the API for nothing."""
    _patch_api(monkeypatch, 'citecheck', ApiResponse(status=202, data={'citecheck_id': 'ab12cd34', 'status': 'queued'}))
    _patch_api(monkeypatch, 'citecheck_status', _short_citecheck())

    out = _run(server.check_citations('', _ctx(), pairs=_pairs()))

    assert no_usage_read == []
    assert out['status'] == 'quota_exhausted'
    assert out['citecheck_id'] == 'ab12cd34'
    assert out['requested'] == 3
    assert 'credits_remaining' not in out and 'cost' not in out and 'resets_at' not in out
    assert out['manage_url'] == config.PLANS_URL
    assert out['message'] == (
        'This citation check did not run: the balance did not cover its 3 citations when it was submitted.'
    )
    step = out['next_step']
    assert step.startswith('Nothing was charged. Tell the user what was not checked and why')
    assert '`manage_url`' in step and 'give the user that link' in step
    assert server.QUOTA_CHEAPER_IS_THE_USERS in step
    assert 'renew' not in step and 'refused again' not in step
    # Nothing about the input being wrong, and nothing about replay.
    text = f'{out["message"]} {step}'
    assert 'day' not in text and 'change the input' not in text
    assert 'failure_class' not in out
    assert 'balance_covers' not in out
    assert [row['statement'] for row in out['not_checked']] == [p['statement'] for p in PAIRS]


def test_a_citation_check_short_of_credits_read_back_later_makes_no_usage_call(monkeypatch, no_usage_read):
    _patch_api(monkeypatch, 'citecheck_status', _short_citecheck())

    out = _run(server.get_citation_check('ab12cd34', _ctx()))

    assert no_usage_read == []
    assert out['status'] == 'quota_exhausted'
    assert 'did not cover its 3 citations' in out['message']
    assert 'when it was submitted' in out['message']
    # The balance now is unknown: neither "out of" nor "not enough" can be said of it.
    assert 'Out of' not in out['message'] and 'Not enough' not in out['message']


def test_a_citation_check_with_no_credits_at_all_gives_the_numbers(monkeypatch):
    """The citation 402 states the balance as `remaining` (one credit per citation)."""
    body = {
        'detail': 'No remaining credits for citation checks.',
        'code': 'no_credits',
        'upgrade_url': PLANS,
        'remaining': 0,
        'resets_at': RESETS,
        'cost': 1,
    }
    _patch_api(monkeypatch, 'citecheck', ApiResponse(status=402, data=body))

    out = _run(server.check_citations('', _ctx(), pairs=_pairs()))

    assert out['status'] == 'quota_exhausted'
    assert out['credits_remaining'] == 0
    assert out['cost_per_citation'] == 1
    assert 'cost' not in out
    assert out['message'].startswith('Out of Lenz credits: each citation checked costs 1 and the account has 0.')
    assert '1 December 2026' in out['message']
    assert out['manage_url'] == PLANS


def test_a_citation_check_short_of_credits_gives_chatgpt_no_link(monkeypatch, chatgpt, no_usage_read):
    _patch_api(monkeypatch, 'citecheck_status', _short_citecheck())
    out = _run(server.get_citation_check('ab12cd34', _ctx()))
    assert 'manage_url' not in out
    assert 'plan' not in out['next_step'] and 'manage_url' not in out['next_step']
    assert server.QUOTA_CHEAPER_IS_THE_USERS in out['next_step']


def test_a_refused_sign_in_at_the_exchange_is_told_to_reconnect():
    """The exchange runs only for a signed-in caller, so its refusal never talks about keys."""
    out = server._exchange_failure_result(exchange.ExchangeFailed('reauth', 'invalid_grant'))
    assert out == {'status': 'auth_required', 'message': server.OAUTH_REAUTH_MESSAGE}


def test_a_running_check_is_not_said_to_be_charged_already():
    """A deep check is charged when it completes, as every failed check now says."""
    assert 'already been charged' not in server.CREDENTIAL_LOST_MID_RUN
    assert 'charged when it completes' in server.CREDENTIAL_LOST_MID_RUN


def test_the_reset_date_is_when_monthly_credits_renew_not_when_the_balance_comes_back(monkeypatch):
    """`resets_at` ends the monthly allowance's period. Credits outside it (a
    one-time grant, extra credits) do not come back then, so the result never
    says the balance resets."""
    _patch_api(monkeypatch, 'verify', _no_credits())
    out = _run(server.verify_claim('x', _ctx()))
    assert 'Monthly credits renew on 1 December 2026.' in out['message']
    assert 'resets' not in out['message'] and 'resets' not in out['next_step']
