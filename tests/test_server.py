"""Tests for the Lenz MCP connector.

The MCP is a thin adapter: it forwards the caller's Authorization header to the
public API and maps responses to clean tool results. These tests drive the
tool functions directly (they remain plain coroutine functions after the
@mcp.tool decorator), mocking the HTTP layer and the branded-link resolver.
"""

import asyncio
import re
import types

import httpx
import pytest

from lenz_mcp import client, config, links, server
from lenz_mcp.client import ApiResponse

# ── helpers ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_shared_http_client():
    """The client is a module-global singleton; reset it around each test so a
    cached real client never leaks into a MockTransport test (or vice versa)."""
    client._http_client = None
    yield
    client._http_client = None


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    """The in-tool wait (verify_claim / get_verification / select_claims) must
    never sleep for real in tests: no-op the sleep and shrink the budget so a
    stub that stays `processing` returns after a few spins, not 45s."""

    async def _instant(_seconds):
        return None

    monkeypatch.setattr(server, '_sleep', _instant)
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS', 0.05)
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS_BY_IDENTITY', {'Claude-User': 0.05})


def _status_sequence(monkeypatch, *responses):
    """Stub verify_status to answer `responses` in order, repeating the last one.
    Returns the list of calls it received."""
    calls = []
    seq = list(responses)

    async def _fake(_auth, *, task_id):
        calls.append(task_id)
        resp = seq.pop(0) if len(seq) > 1 else seq[0]
        return resp

    monkeypatch.setattr(client, 'verify_status', _fake)
    return calls


_PROCESSING = ApiResponse(status=200, data={'status': 'processing', 'progress': {'step': 'Research...'}})
_COMPLETED = ApiResponse(
    status=200,
    data={
        'status': 'completed',
        'result': {
            'verification_id': 'pub12345',
            'claim': 'big claim',
            'verdict': 'False',
            'lenz_score': 2,
            'confidence': 'high',
            'key_finding': 'It is small.',
            'sources': [{'title': 'NASA', 'url': 'https://nasa.gov'}],
        },
    },
)
_MULTI_CLAIM = ApiResponse(
    status=200,
    data={
        'status': 'needs_input',
        'reason': 'multi_claim',
        'claims': [{'text': 'claim A', 'domain': 'science'}, {'text': 'claim B', 'domain': ''}],
        'hint': 'Pick one.',
    },
)


def _ctx(auth='Bearer lenz_testkey'):
    """Minimal stand-in for mcp Context exposing request.headers.get()."""
    headers = {'authorization': auth} if auth else {}
    request = types.SimpleNamespace(headers=headers)
    request_context = types.SimpleNamespace(request=request)
    return types.SimpleNamespace(request_context=request_context)


def _run(coro):
    return asyncio.run(coro)


def _patch_api(monkeypatch, name, response):
    async def _fake(*_args, **_kwargs):
        return response

    monkeypatch.setattr(client, name, _fake)


# ── auth gating ──────────────────────────────────────────────────────


def test_assess_without_key_returns_auth_required():
    out = _run(server.assess_claim('the earth is round', _ctx(auth=None)))
    assert out['status'] == 'auth_required'
    assert config.API_CREDENTIALS_URL in out['message']


def test_all_tools_gate_on_missing_key(monkeypatch):
    assert _run(server.verify_claim('x', _ctx(auth=None)))['status'] == 'auth_required'
    assert _run(server.get_verification('tid', _ctx(auth=None)))['status'] == 'auth_required'
    assert _run(server.select_claims('tid', ['a'], _ctx(auth=None)))['status'] == 'auth_required'
    assert _run(server.check_usage(_ctx(auth=None)))['status'] == 'auth_required'
    assert _run(server.ask_followup('vid', 'q', _ctx(auth=None)))['status'] == 'auth_required'


# ── assess ───────────────────────────────────────────────────────────


def test_assess_happy_path_with_public_link(monkeypatch):
    _patch_api(
        monkeypatch,
        'assess',
        ApiResponse(
            status=200,
            data={
                'claims': [
                    {
                        'claim': 'The Earth is round.',
                        'verdict': 'True',
                        'confidence': 'high',
                        'verification_url': 'https://lenz.io/api/v1/verifications/abc12345',
                    }
                ]
            },
        ),
    )
    # branded_link is now a pure function — let it run for real.
    out = _run(server.assess_claim('the earth is round', _ctx()))
    assert out['status'] == 'ok'
    assert out['claims'][0]['verdict'] == 'True'
    # Link built straight from the verification_id in verification_url (no slug, no DB).
    assert '/c/abc12345' in out['claims'][0]['lenz_url']
    assert 'utm_medium=mcp' in out['claims'][0]['lenz_url']
    assert 'confidence_note' in out


_RATIONALE = 'The registry lists 4,200 filings for 2024, and the figure has not been revised since.'
_DISSENT = 'The registry figure is provisional, so the 2024 total cannot yet be confirmed.'


def _assess_rows(monkeypatch, rows):
    _patch_api(monkeypatch, 'assess', ApiResponse(status=200, data={'claims': rows}))
    return _run(server.assess_claim('x', _ctx()))


def test_assess_forwards_the_reviewer_notes_when_set(monkeypatch):
    out = _assess_rows(
        monkeypatch,
        [
            {
                'claim': 'A',
                'verdict': 'True',
                'confidence': 'medium',
                'rationale': _RATIONALE,
                'dissent': _DISSENT,
            }
        ],
    )
    assert out['claims'][0]['rationale'] == _RATIONALE
    assert out['claims'][0]['dissent'] == _DISSENT


def test_assess_omits_null_notes(monkeypatch):
    """A null note is not forwarded: the entry stays as small as it was."""
    out = _assess_rows(
        monkeypatch,
        [{'claim': 'A', 'verdict': 'True', 'confidence': 'high', 'rationale': None, 'dissent': None}],
    )
    assert out['claims'][0] == {'claim': 'A', 'verdict': 'True', 'confidence': 'high'}


def test_assess_tolerates_a_row_from_before_the_notes(monkeypatch):
    """`lenz-mcp` and the Lenz API deploy independently, and the MCP client's
    deterministic idempotency key replays a stored body for 24h: a row can
    arrive with no note keys at all. Guarded reads, no error."""
    out = _assess_rows(monkeypatch, [{'claim': 'A', 'verdict': 'True', 'confidence': 'high'}])
    assert out['status'] == 'ok'
    assert 'rationale' not in out['claims'][0] and 'dissent' not in out['claims'][0]


def test_assess_note_says_what_the_notes_are_in_its_own_sentence(monkeypatch):
    """The reviewer-notes sentence is SEPARATE from the escalation guidance,
    so one can be reworded without the other, and it never says how a note
    is picked."""
    out = _assess_rows(monkeypatch, [{'claim': 'A', 'verdict': 'True', 'confidence': 'high'}])
    note = out['confidence_note']
    assert server.ASSESS_NOTES_NOTE in note
    assert server.ASSESS_NOTES_NOTE not in server.ASSESS_ESCALATION_NOTE
    assert "reviewers' notes, not checked sources" in server.ASSESS_NOTES_NOTE
    # The note is pinned to its approved wording, and the tool description says
    # nothing about the notes beyond one approved sentence: any added clause on
    # how a note is chosen fails here.
    assert server.ASSESS_NOTES_NOTE == (
        " `rationale` is the reasoning of a reviewer who agrees with the panel's verdict; `dissent`, when "
        "set, is the reasoning of the reviewer farthest from it. Both are reviewers' notes, not checked "
        'sources. For sourced evidence, offer the user a deep check; if they agree, call `verify_claim`.'
    )
    doc = ' '.join((server.assess_claim.__doc__ or '').split())
    note_sentences = [s for s in re.split(r'(?<=[.!?:])\s+', doc) if 'rationale' in s or 'dissent' in s]
    assert note_sentences == [
        "A row may carry ``rationale``, a reviewer's reasoning for the verdict, and ``dissent``, "
        'the reasoning of the reviewer farthest from it:'
    ]


def test_assess_fresh_claim_has_no_link(monkeypatch):
    _patch_api(
        monkeypatch,
        'assess',
        ApiResponse(
            status=200,
            data={'claims': [{'claim': 'X', 'verdict': 'Mixed', 'confidence': 'low', 'verification_url': None}]},
        ),
    )
    # No verification_url → fresh/private claim → no link.
    out = _run(server.assess_claim('x', _ctx()))
    assert 'lenz_url' not in out['claims'][0]


def test_assess_link_gate_only_uses_verification_url(monkeypatch):
    # Public-gate invariant: a branded link is built ONLY from verification_url
    # (which the API returns only for already-public claims). A claim entry with
    # some other id-ish field but no verification_url must NOT be linked — guards
    # against a future change linking from the wrong field and leaking private claims.
    _patch_api(
        monkeypatch,
        'assess',
        ApiResponse(
            status=200,
            data={
                'claims': [
                    {
                        'claim': 'X',
                        'verdict': 'True',
                        'confidence': 'high',
                        'verification_id': 'priv999',
                        'verification_url': None,
                    }
                ]
            },
        ),
    )
    out = _run(server.assess_claim('x', _ctx()))
    assert 'lenz_url' not in out['claims'][0]


def test_assess_no_claim(monkeypatch):
    _patch_api(
        monkeypatch,
        'assess',
        ApiResponse(status=200, data={'claims': [], 'error': 'No verifiable claim detected', 'error_code': 'no_claim'}),
    )
    out = _run(server.assess_claim('hello there', _ctx()))
    assert out['status'] == 'no_claim'


def test_assess_claims_list_one_row_per_item_in_position(monkeypatch):
    """The list form: every item stays in position — verdict rows, a compound
    row carrying identified_claims, and an error row saying why — so the
    agent can match answers to what it sent by index."""
    seen = {}

    async def _fake(authorization, **kwargs):
        seen.update(kwargs)
        return ApiResponse(
            status=200,
            data={
                'claims': [
                    {
                        'claim': 'A',
                        'verdict': 'True',
                        'confidence': 'high',
                        'verification_url': None,
                        'error_code': None,
                        'candidate_claims': [],
                        'identified_claims': [],
                        'hint': None,
                    },
                    {
                        'claim': 'B primary',
                        'verdict': 'Mixed',
                        'confidence': 'medium',
                        'verification_url': None,
                        'error_code': None,
                        'candidate_claims': [],
                        'identified_claims': ['B second'],
                        'hint': 'Assessed the main claim only. Send identified_claims as their own items to check the rest.',
                    },
                    {
                        'claim': 'hello',
                        'verdict': 'Error',
                        'confidence': 'low',
                        'verification_url': None,
                        'error_code': 'no_claim',
                        'candidate_claims': [],
                        'identified_claims': [],
                        'hint': 'No statement that can be true or false was found in the input.',
                    },
                    {
                        'claim': 'slow one',
                        'verdict': 'Error',
                        'confidence': 'low',
                        'verification_url': None,
                        'error_code': 'timeout',
                        'candidate_claims': [],
                        'identified_claims': [],
                        'hint': "This item was not processed inside the call's time budget; nothing was charged.",
                    },
                ],
                'error': None,
            },
        )

    monkeypatch.setattr(client, 'assess', _fake)
    out = _run(server.assess_claim(ctx=_ctx(), claims=['A', 'B', 'hello', 'slow one']))
    assert seen['claims'] == ['A', 'B', 'hello', 'slow one']
    assert 'text' not in seen or not seen['text']
    assert out['status'] == 'ok'
    rows = out['claims']
    assert len(rows) == 4
    assert rows[0] == {'claim': 'A', 'verdict': 'True', 'confidence': 'high'}
    assert rows[1]['identified_claims'] == ['B second']
    assert rows[1]['hint'].startswith('Assessed the main claim only')
    assert rows[2]['verdict'] == 'Error'
    assert rows[2]['error'] == 'no_claim'
    # The always-empty field is never forwarded to the agent.
    assert 'candidate_claims' not in rows[2]
    assert rows[3]['error'] == 'timeout'
    assert 'candidate_claims' not in rows[3]
    assert rows[3]['hint'].startswith('This item was not processed')


def test_assess_claims_list_strips_blank_items_and_rejects_both_forms():
    out = _run(server.assess_claim(claim='x', ctx=_ctx(), claims=['y']))
    assert out['status'] == 'error'
    assert 'not both' in out['message']
    out = _run(server.assess_claim(claim='', ctx=_ctx(), claims=['  ', '']))
    assert out['status'] == 'error'


def test_assess_client_idempotency_key_covers_the_list(monkeypatch):
    """A retry of the same list carries the same key; a different list — or
    the same words split differently — does not."""
    captured = []

    async def _fake_request(method, path, authorization, *, json=None, timeout=None, idempotency_key=None):
        captured.append((json, idempotency_key))
        return ApiResponse(status=200, data={'claims': []})

    monkeypatch.setattr(client, '_request', _fake_request)
    _run(client.assess('Bearer k', claims=['a', 'b'], language=''))
    _run(client.assess('Bearer k', claims=['a', 'b'], language=''))
    _run(client.assess('Bearer k', claims=['a b'], language=''))
    _run(client.assess('Bearer k', text='a b', language=''))
    (body1, key1), (body2, key2), (body3, key3), (body4, key4) = captured
    assert body1 == {'claims': ['a', 'b'], 'language': ''}
    assert key1 == key2
    assert key3 != key1
    assert body4 == {'claim': 'a b', 'language': ''}
    assert key4 != key3


def test_assess_quota_exhausted(monkeypatch):
    _patch_api(
        monkeypatch,
        'assess',
        ApiResponse(
            status=402,
            data={
                'detail': 'No remaining /assess units.',
                'code': 'no_credits',
                'upgrade_url': 'https://lenz.io/plans',
            },
        ),
    )
    out = _run(server.assess_claim('x', _ctx()))
    assert out['status'] == 'quota_exhausted'
    # The server's own upgrade_url wins over the compiled-in fallback.
    assert out['manage_url'] == 'https://lenz.io/plans'


def test_assess_quota_exhausted_402_without_upgrade_url_falls_back_to_plans(monkeypatch):
    _patch_api(
        monkeypatch,
        'assess',
        ApiResponse(status=402, data={'detail': 'No remaining /assess units.', 'code': 'no_credits'}),
    )
    out = _run(server.assess_claim('x', _ctx()))
    assert out['status'] == 'quota_exhausted'
    # Plans page, NOT the API-credentials page — the caller already has a working key.
    assert out['manage_url'] == config.PLANS_URL
    assert out['manage_url'] != config.API_CREDENTIALS_URL


def test_assess_legacy_403_quota_still_maps(monkeypatch):
    """MCP and API deploy independently — an older API still sends 403."""
    _patch_api(
        monkeypatch,
        'assess',
        ApiResponse(status=403, data={'detail': 'No remaining /assess units.', 'code': 'no_credits'}),
    )
    out = _run(server.assess_claim('x', _ctx()))
    assert out['status'] == 'quota_exhausted'
    assert out['manage_url'] == config.PLANS_URL


def test_assess_bad_key_maps_to_auth_required(monkeypatch):
    _patch_api(monkeypatch, 'assess', ApiResponse(status=401, data={'detail': 'Invalid key'}))
    out = _run(server.assess_claim('x', _ctx(auth='Bearer lenz_bad')))
    assert out['status'] == 'auth_required'


def test_assess_transport_error(monkeypatch):
    _patch_api(monkeypatch, 'assess', ApiResponse(status=0, data={}))
    out = _run(server.assess_claim('x', _ctx()))
    assert out['status'] == 'error'


def test_assess_503_tells_the_agent_to_retry_not_rephrase(monkeypatch):
    """A provider-side exhaustion is not a generic error: the agent gets the
    wait and an instruction to retry the SAME call."""
    _patch_api(
        monkeypatch,
        'assess',
        ApiResponse(
            status=503,
            data={
                'detail': 'Our model providers are temporarily unavailable.',
                'code': 'upstream_unavailable',
                'retry_after': 90,
            },
        ),
    )
    out = _run(server.assess_claim('x', _ctx()))
    assert out['status'] == 'service_unavailable'
    assert out['retry_after_seconds'] == 90
    assert 'Do not rephrase' in out['resolve_with']


# ── verify + get_verification ────────────────────────────────────────


def test_verify_returns_task_id(monkeypatch):
    """Still processing when the wait budget ends → the pre-wait contract:
    `submitted` + task_id, with the current step and a message that names
    get_verification and says it WAITS (never "do not poll")."""
    _patch_api(monkeypatch, 'verify', ApiResponse(status=202, data={'task_id': 'task-123', 'status': 'queued'}))
    calls = _status_sequence(monkeypatch, _PROCESSING)
    out = _run(server.verify_claim('big claim', _ctx()))
    assert out['status'] == 'submitted'
    assert out['task_id'] == 'task-123'
    assert out['step'] == 'Research...'
    assert 'get_verification' in out['message']
    assert 'wait' in out['message'].lower()
    assert 'not poll' not in out['message'].lower()
    assert calls and set(calls) == {'task-123'}


def test_verify_returns_the_verdict_when_the_run_finishes_in_time(monkeypatch):
    """The answer rides in the verify_claim result itself: a cache
    hit or a `low` run completes inside the budget, so one call carries the
    verdict + verification_id — no get_verification round-trip."""
    _patch_api(monkeypatch, 'verify', ApiResponse(status=202, data={'task_id': 'task-123', 'status': 'queued'}))
    calls = _status_sequence(monkeypatch, _PROCESSING, _PROCESSING, _COMPLETED)
    out = _run(server.verify_claim('big claim', _ctx()))
    assert out['status'] == 'completed'
    assert out['task_id'] == 'task-123'
    assert out['verification_id'] == 'pub12345'
    assert out['verdict'] == 'False'
    assert out['key_finding'] == 'It is small.'
    assert len(calls) == 3


def test_verify_surfaces_needs_input_with_the_claims_in_the_text(monkeypatch):
    """A multi-claim interrupt lands during the wait and the options are in the
    result TEXT, numbered, with the task_id select_claims needs — the picker
    card must never be the only path (a client that renders no card would
    otherwise never reach select_claims)."""
    _patch_api(monkeypatch, 'verify', ApiResponse(status=202, data={'task_id': 'task-123', 'status': 'queued'}))
    _status_sequence(monkeypatch, _MULTI_CLAIM)
    out = _run(server.verify_claim('claim A and claim B', _ctx()))
    assert out['status'] == 'needs_input'
    assert out['task_id'] == 'task-123'
    assert out['reason'] == 'multi_claim'
    assert '1. claim A' in out['message'] and '2. claim B' in out['message']
    assert 'select_claims' in out['message']
    assert 'user' in out['message'].lower()  # put to the user, not auto-picked
    assert 'select_claims' in out['resolve_with']


def test_verify_status_poll_failure_still_reports_the_submission(monkeypatch):
    """A transport blip on the STATUS poll is not a failed run: the claim was
    accepted and charged, so the model gets `submitted` + task_id, not `error`."""
    _patch_api(monkeypatch, 'verify', ApiResponse(status=202, data={'task_id': 'task-123', 'status': 'queued'}))
    _status_sequence(monkeypatch, ApiResponse(status=0, data={}))
    out = _run(server.verify_claim('big claim', _ctx()))
    assert out['status'] == 'submitted'
    assert out['task_id'] == 'task-123'


def test_verify_forwards_depth_and_defaults_to_standard(monkeypatch):
    """`depth` is the one request parameter that changes the price, so the
    tool must pass exactly what the agent asked for — and ask for the full
    pass when it said nothing."""
    seen = []

    async def _fake(_auth, **kwargs):
        seen.append(kwargs)
        return ApiResponse(status=202, data={'task_id': 'task-123'})

    monkeypatch.setattr(client, 'verify', _fake)
    _status_sequence(monkeypatch, _COMPLETED)
    _run(server.verify_claim('big claim', _ctx(), depth='low'))
    _run(server.verify_claim('big claim', _ctx()))
    assert [k['depth'] for k in seen] == ['low', 'standard']


def test_verify_no_credits(monkeypatch):
    # Real /verify quota rejection is a 402 — status alone is enough. The
    # message is ours, not the API's `detail`: see
    # test_quota_message_never_echoes_the_api_detail.
    _patch_api(
        monkeypatch,
        'verify',
        ApiResponse(status=402, data={'detail': 'No remaining claim checks.', 'code': 'no_credits'}),
    )
    out = _run(server.verify_claim('x', _ctx()))
    assert out['status'] == 'quota_exhausted'
    assert out['message'] == 'Out of Lenz credits for this call.'


def test_quota_message_reports_the_balance_and_the_price(monkeypatch):
    """The 402 body already carries the two numbers worth saying, so say them.

    `cost` and `credits_remaining` are read from the response, never
    hardcoded: the prices are set by the API.
    """
    _patch_api(
        monkeypatch,
        'verify',
        ApiResponse(
            status=402,
            data={
                'detail': 'No remaining claim checks.',
                'code': 'no_credits',
                'cost': 10,
                'credits_remaining': 3,
            },
        ),
    )
    out = _run(server.verify_claim('x', _ctx()))
    assert out['message'] == 'Not enough Lenz credits: this call costs 10 and the account has 3.'


def test_verify_short_on_credits_writes_a_thousands_separator(monkeypatch):
    """A balance a model reads back to a person is written the way /billing writes it."""
    _patch_api(
        monkeypatch,
        'verify',
        ApiResponse(status=402, data={'code': 'no_credits', 'cost': 10000, 'credits_remaining': 1960}),
    )
    out = _run(server.verify_claim('x', _ctx()))
    assert out['message'] == 'Not enough Lenz credits: this call costs 10,000 and the account has 1,960.'


def test_quota_message_does_not_claim_empty_when_the_balance_is_short(monkeypatch):
    """A 402 also fires when the balance is non-zero but too small for THIS
    call — a 10-credit verify on 3 credits. Saying "out of credits: the
    account has 3" contradicts itself, so the short case says "not enough".
    """
    _patch_api(
        monkeypatch,
        'verify',
        ApiResponse(status=402, data={'code': 'no_credits', 'credits_remaining': 3}),
    )
    out = _run(server.verify_claim('x', _ctx()))
    assert out['message'] == 'Not enough Lenz credits: the account has 3.'
    assert 'Out of' not in out['message']


def test_quota_message_never_echoes_the_api_detail(monkeypatch):
    """`detail` is developer copy that API clients may match on, so it
    can name things a person in a chat client would not recognise.
    Text like "No remaining /assess units." means nothing to a person in a
    chat client. It must not reach one.
    """
    _patch_api(
        monkeypatch,
        'assess',
        ApiResponse(
            status=402,
            data={'detail': 'No remaining /assess units.', 'code': 'no_credits', 'credits_remaining': 0},
        ),
    )
    out = _run(server.assess_claim('x', _ctx()))
    assert out['status'] == 'quota_exhausted'
    assert '/assess units' not in out['message']
    assert 'units' not in out['message']
    assert out['message'] == 'Out of Lenz credits for this call.'


def test_403_mentioning_credit_in_prose_is_not_quota(monkeypatch):
    """The deleted `'credit' in detail.lower()` heuristic mislabeled this.

    A genuine authorization denial whose wording happens to contain the word
    "credit" must surface as forbidden, not as a billing wall.
    """
    _patch_api(
        monkeypatch,
        'verify',
        ApiResponse(status=403, data={'detail': 'You may not credit this source.'}),
    )
    out = _run(server.verify_claim('x', _ctx()))
    assert out['status'] == 'forbidden'


def test_403_invalid_count_is_not_quota(monkeypatch):
    """`invalid_count` is a malformed request, not an empty wallet."""
    _patch_api(
        monkeypatch,
        'verify',
        ApiResponse(status=403, data={'detail': 'Bad n.', 'code': 'invalid_count'}),
    )
    out = _run(server.verify_claim('x', _ctx()))
    assert out['status'] == 'forbidden'


def test_403_without_quota_code_maps_to_forbidden(monkeypatch):
    # A non-quota 403 (e.g. IP block) has no quota `code` → must NOT be
    # mislabeled as "out of quota — upgrade".
    _patch_api(monkeypatch, 'verify', ApiResponse(status=403, data={'detail': 'Forbidden.'}))
    out = _run(server.verify_claim('x', _ctx()))
    assert out['status'] == 'forbidden'
    assert 'quota' not in out.get('message', '').lower()


def test_429_surfaces_the_real_wait_not_shortly(monkeypatch):
    """A daily fair-use cap can be hours out, so "shortly" would be wrong."""
    _patch_api(
        monkeypatch,
        'verify',
        ApiResponse(
            status=429,
            data={
                'detail': 'Daily fair-use limit reached for this account.',
                'code': 'extract_daily_limit',
                'reset_in_seconds': 7200,
                'upgrade_url': 'https://lenz.io/plans',
            },
        ),
    )
    out = _run(server.verify_claim('x', _ctx()))
    assert out['status'] == 'rate_limited'
    assert out['retry_after_seconds'] == 7200
    assert '2 hours' in out['message']
    assert 'shortly' not in out['message']
    assert out['manage_url'] == 'https://lenz.io/plans'


def test_429_falls_back_to_the_retry_after_header(monkeypatch):
    """No body field — read the header the client now lowercases for us."""
    _patch_api(
        monkeypatch,
        'verify',
        ApiResponse(status=429, data={'detail': 'Slow down.'}, headers={'retry-after': '45'}),
    )
    out = _run(server.verify_claim('x', _ctx()))
    assert out['status'] == 'rate_limited'
    assert out['retry_after_seconds'] == 45
    assert '45 seconds' in out['message']


def test_429_without_any_retry_hint_keeps_the_generic_message(monkeypatch):
    _patch_api(monkeypatch, 'verify', ApiResponse(status=429, data={}))
    out = _run(server.verify_claim('x', _ctx()))
    assert out['status'] == 'rate_limited'
    assert 'shortly' in out['message']
    assert 'retry_after_seconds' not in out


def test_verify_409_surfaces_inflight_task_id(monkeypatch):
    # An identical verify is already running → API returns 409 + the in-flight
    # task_id. The tool surfaces it as a normal submission (no duplicate spend).
    _patch_api(
        monkeypatch,
        'verify',
        ApiResponse(status=409, data={'detail': 'already in progress.', 'task_id': 'task-existing'}),
    )
    calls = _status_sequence(monkeypatch, _PROCESSING)
    out = _run(server.verify_claim('big claim', _ctx()))
    assert out['status'] == 'submitted'
    assert out['task_id'] == 'task-existing'
    assert 'already being checked' in out['message']
    # The wait runs on the EXISTING task, not a duplicate.
    assert set(calls) == {'task-existing'}


def test_verify_409_waits_on_the_existing_run_and_returns_its_verdict(monkeypatch):
    _patch_api(
        monkeypatch,
        'verify',
        ApiResponse(status=409, data={'detail': 'already in progress.', 'task_id': 'task-existing'}),
    )
    _status_sequence(monkeypatch, _COMPLETED)
    out = _run(server.verify_claim('big claim', _ctx()))
    assert out['status'] == 'completed'
    assert out['task_id'] == 'task-existing'


def test_409_without_task_id_maps_to_in_progress(monkeypatch):
    # A 409 on a tool that can't surface a task (e.g. assess) → clean in_progress.
    _patch_api(monkeypatch, 'assess', ApiResponse(status=409, data={'detail': 'already in progress.'}))
    out = _run(server.assess_claim('x', _ctx()))
    assert out['status'] == 'in_progress'


def test_get_verification_processing(monkeypatch):
    """Still running when the budget ends → `processing` with the step, and the
    tool polled more than once (it waited rather than answering the first read)."""
    calls = _status_sequence(monkeypatch, _PROCESSING)
    out = _run(server.get_verification('tid', _ctx()))
    assert out['status'] == 'processing'
    assert out['step'] == 'Research...'
    assert out['task_id'] == 'tid'
    assert 'again' in out['message']
    assert len(calls) >= 2


def test_get_verification_waits_until_completed(monkeypatch):
    calls = _status_sequence(monkeypatch, _PROCESSING, _PROCESSING, _COMPLETED)
    out = _run(server.get_verification('tid', _ctx()))
    assert out['status'] == 'completed'
    assert out['verification_id'] == 'pub12345'
    assert len(calls) == 3


def test_get_verification_stops_waiting_on_needs_input(monkeypatch):
    calls = _status_sequence(monkeypatch, _PROCESSING, _MULTI_CLAIM)
    out = _run(server.get_verification('tid', _ctx()))
    assert out['status'] == 'needs_input'
    assert out['task_id'] == 'tid'
    assert '1. claim A' in out['message']
    assert len(calls) == 2


def test_get_verification_by_verification_id_fetches_the_stored_result(monkeypatch):
    """An 8-hex verification_id names a STORED result: fetched from
    GET /verifications/{id} and mapped like a completed poll — no status call."""
    status_calls = _status_sequence(monkeypatch, _COMPLETED)
    seen = {}

    async def _detail(_auth, *, verification_id):
        seen['id'] = verification_id
        return ApiResponse(status=200, data=_COMPLETED.data['result'])

    monkeypatch.setattr(client, 'verification_detail', _detail)
    out = _run(server.get_verification('ab12cd34', _ctx()))
    assert seen['id'] == 'ab12cd34'
    assert status_calls == []
    assert out['status'] == 'completed'
    assert out['verdict'] == 'False'
    assert out['verification_id'] == 'pub12345'
    assert out['sources_total'] == 1


def test_get_verification_unknown_verification_id_is_not_found(monkeypatch):
    _patch_api(monkeypatch, 'verification_detail', ApiResponse(status=404, data={'detail': 'Not found.'}))
    out = _run(server.get_verification('deadbeef', _ctx()))
    assert out['status'] == 'not_found'
    assert 'task_id' in out['message'] and 'verification_id' in out['message']


def test_get_verification_treats_anything_but_8_hex_as_a_task_id(monkeypatch):
    """A 32-hex uuid, or any other shape, is a run to wait on — only the exact
    8-hex form is a verification_id (an uppercase or 7-char id is a task)."""
    calls = _status_sequence(monkeypatch, _COMPLETED)
    for ident in ('0123456789abcdef0123456789abcdef', 'PUB12345', 'abc1234', 'task-1'):
        _run(server.get_verification(ident, _ctx()))
    assert calls == ['0123456789abcdef0123456789abcdef', 'PUB12345', 'abc1234', 'task-1']


def test_get_verification_needs_input(monkeypatch):
    _patch_api(
        monkeypatch,
        'verify_status',
        ApiResponse(
            status=200,
            data={'status': 'needs_input', 'reason': 'multi_claim', 'claims': [{'text': 'a'}, {'text': 'b'}]},
        ),
    )
    out = _run(server.get_verification('tid', _ctx()))
    assert out['status'] == 'needs_input'
    assert out['task_id'] == 'tid'
    assert out['reason'] == 'multi_claim'
    assert len(out['claims']) == 2
    assert 'select' in out['resolve_with']
    assert '1. a' in out['message'] and '2. b' in out['message']


def test_get_verification_duplicate_hint(monkeypatch):
    _patch_api(
        monkeypatch,
        'verify_status',
        ApiResponse(
            status=200,
            data={
                'status': 'needs_input',
                'reason': 'duplicate_found',
                'similar_claims': [{'verification_id': 'x', 'verdict': 'True', 'url': 'https://lenz.io/c/x'}],
            },
        ),
    )
    out = _run(server.get_verification('tid', _ctx()))
    assert out['reason'] == 'duplicate_found'
    assert 'similar_claims' in out
    assert 'similar_claims' in out['resolve_with']


# ── select ───────────────────────────────────────────────────────────


def test_select_fans_out(monkeypatch):
    _patch_api(
        monkeypatch,
        'select',
        ApiResponse(
            status=202,
            data={
                'batch_id': 'b1',
                'items': [
                    {'task_id': 't-a', 'claim_text': 'claim A'},
                    {'task_id': 't-b', 'claim_text': 'claim B'},
                ],
            },
        ),
    )
    calls = _status_sequence(monkeypatch, _COMPLETED)
    out = _run(server.select_claims('parent', ['claim A', 'claim B'], _ctx()))
    assert out['status'] == 'submitted'
    assert out['batch_id'] == 'b1'
    assert [c['task_id'] for c in out['claims']] == ['t-a', 't-b']
    assert 'get_verification' in out['message']
    assert calls == []  # several selections are not awaited — the model gets the ids


def test_select_single_claim_waits_and_answers_on_the_new_task_id(monkeypatch):
    """One selection (the common case) is awaited in-call and the result carries
    the NEW task_id — the parent stays needs_input forever, and a model left on
    it reports it cannot see a result."""
    _patch_api(
        monkeypatch,
        'select',
        ApiResponse(status=202, data={'batch_id': 'b1', 'items': [{'task_id': 't-a', 'claim_text': 'claim A'}]}),
    )
    calls = _status_sequence(monkeypatch, _PROCESSING, _COMPLETED)
    out = _run(server.select_claims('parent', ['claim A'], _ctx()))
    assert out['status'] == 'completed'
    assert out['task_id'] == 't-a'
    assert out['verification_id'] == 'pub12345'
    assert out['batch_id'] == 'b1'
    assert set(calls) == {'t-a'}


def test_select_single_claim_still_running_reports_submitted_with_the_new_task_id(monkeypatch):
    _patch_api(
        monkeypatch,
        'select',
        ApiResponse(status=202, data={'batch_id': 'b1', 'items': [{'task_id': 't-a', 'claim_text': 'claim A'}]}),
    )
    _status_sequence(monkeypatch, _PROCESSING)
    out = _run(server.select_claims('parent', ['claim A'], _ctx()))
    assert out['status'] == 'submitted'
    assert out['task_id'] == 't-a'
    assert out['claim'] == 'claim A'
    assert 'get_verification' in out['message']


def test_select_partial(monkeypatch):
    _patch_api(
        monkeypatch,
        'select',
        ApiResponse(
            status=202, data={'batch_id': 'b1', 'items': [{'task_id': 't-a', 'claim_text': 'A'}], 'partial': True}
        ),
    )
    out = _run(server.select_claims('parent', ['A', 'B'], _ctx()))
    assert out['status'] == 'partial'
    assert 'retry' in out['message'].lower()


def test_select_invalid_selection(monkeypatch):
    _patch_api(
        monkeypatch,
        'select',
        ApiResponse(
            status=422, data={'detail': 'Selected text was not one of the offered claims', 'error': 'invalid_selection'}
        ),
    )
    out = _run(server.select_claims('parent', ['nope'], _ctx()))
    assert out['status'] == 'invalid_request'


def test_select_client_builds_per_task_path(monkeypatch):
    captured = {}

    async def _fake_request(
        method, path, authorization, *, json=None, timeout=client.config.DEFAULT_TIMEOUT, idempotency_key=None
    ):
        captured.update(method=method, path=path, json=json, idempotency_key=idempotency_key)
        return ApiResponse(status=202, data={'batch_id': 'b', 'items': []})

    monkeypatch.setattr(client, '_request', _fake_request)
    _run(client.select('Bearer k', task_id='TID', texts=['a', 'b']))
    assert captured['method'] == 'POST'
    assert captured['path'] == '/verify/TID/select'
    assert captured['json'] == {'texts': ['a', 'b']}
    # select is a write op → carries a content-derived idempotency key
    assert captured['idempotency_key'] == client._idem_key('select', 'TID', 'a', 'b')


def test_get_verification_failed(monkeypatch):
    _patch_api(
        monkeypatch,
        'verify_status',
        ApiResponse(status=200, data={'status': 'failed', 'error': 'Pipeline stopped at: research_empty'}),
    )
    out = _run(server.get_verification('tid', _ctx()))
    assert out['status'] == 'failed'
    assert 'retryable' not in out  # older body without the fields: nothing invented


def test_get_verification_failed_passes_the_retry_contract_through(monkeypatch):
    _patch_api(
        monkeypatch,
        'verify_status',
        ApiResponse(
            status=200,
            data={
                'status': 'failed',
                'error': 'Pipeline stopped at: research_empty',
                'failure_reason': 'research_empty',
                'failure_class': 'upstream_unavailable',
                'retryable': True,
            },
        ),
    )
    out = _run(server.get_verification('tid', _ctx()))
    assert out['failure_reason'] == 'research_empty'
    assert out['failure_class'] == 'upstream_unavailable'
    assert out['retryable'] is True


def test_get_verification_completed_no_link(monkeypatch):
    _patch_api(
        monkeypatch,
        'verify_status',
        ApiResponse(
            status=200,
            data={
                'status': 'completed',
                'result': {
                    'verification_id': 'pub99999',
                    'claim': 'The Earth is round.',
                    'verdict': 'True',
                    'lenz_score': 9,
                    'confidence': 'high',
                    'executive_summary': 'Verified.',
                    'sources': [
                        {'title': 'NASA', 'url': 'https://nasa.gov'},
                        {'title': 'no-url', 'url': ''},
                    ],
                },
            },
        ),
    )
    out = _run(server.get_verification('tid', _ctx()))
    assert out['status'] == 'completed'
    assert out['verdict'] == 'True'
    # the 1-10 score rides on the result, so nobody has to spend a follow-up
    # question to learn it
    assert isinstance(out['lenz_score'], int) and 1 <= out['lenz_score'] <= 10
    assert out['lenz_score'] == 9
    # the caller's own claim id is surfaced so it can be passed to `ask`
    assert out['verification_id'] == 'pub99999'
    # verify results are private by default → never linked
    assert 'lenz_url' not in out
    # source without a url is dropped; only the NASA source survives
    assert out['sources'] == [{'title': 'NASA', 'url': 'https://nasa.gov'}]
    # sources_total counts the url-having sources the deep check drew on
    # (the widget/model use it to show "N of total" rather than implying 5 is all)
    assert out['sources_total'] == 1
    # no `depth` on the body (an API predating the parameter) reads as the full pass
    assert out['depth'] == 'standard'


def test_completed_result_carries_none_score_on_a_pre_score_payload(monkeypatch):
    """A body predating `lenz_score` reads as None — never a fabricated number,
    and never a KeyError for the agent reading the result."""
    _patch_api(
        monkeypatch,
        'verify_status',
        ApiResponse(status=200, data={'status': 'completed', 'result': {'verdict': 'True'}}),
    )
    out = _run(server.get_verification('tid', _ctx()))
    assert 'lenz_score' in out
    assert out['lenz_score'] is None


def test_completed_result_echoes_the_served_depth(monkeypatch):
    """The completed result carries the depth the verdict was PRODUCED at —
    a `low` request can be answered from an existing deeper check, and the
    agent should be able to tell which evidence breadth it is looking at."""
    _patch_api(
        monkeypatch,
        'verify_status',
        ApiResponse(status=200, data={'status': 'completed', 'result': {'verdict': 'True', 'depth': 'low'}}),
    )
    out = _run(server.get_verification('tid', _ctx()))
    assert out['depth'] == 'low'


def test_completed_result_filters_url_less_sources_before_slicing(monkeypatch):
    # The first 5 evidence entries lack a url; the good source is at position 6.
    # filter-then-slice must keep it (slice-then-filter would return []).
    bad = [{'title': f'n{i}', 'url': ''} for i in range(5)]
    good = {'title': 'NASA', 'url': 'https://nasa.gov'}
    _patch_api(
        monkeypatch,
        'verify_status',
        ApiResponse(
            status=200,
            data={'status': 'completed', 'result': {'verdict': 'True', 'sources': bad + [good]}},
        ),
    )
    out = _run(server.get_verification('tid', _ctx()))
    assert out['sources'] == [good]


def test_get_verification_widget_delegates_to_shared_mapper(monkeypatch):
    # The widget-only twin is a distinct registered tool; assert its own entry
    # point returns the same mapped result as get_verification (both delegate to
    # _verification_result) so the card polls the same shape the model does.
    _patch_api(
        monkeypatch,
        'verify_status',
        ApiResponse(
            status=200,
            data={
                'status': 'completed',
                'result': {
                    'verification_id': 'pub12345',
                    'verdict': 'False',
                    'lenz_score': 2,
                    'confidence': 'high',
                    'sources': [{'title': 'NASA', 'url': 'https://nasa.gov'}],
                },
            },
        ),
    )
    out = _run(server.get_verification_widget('tid', _ctx()))
    assert out['status'] == 'completed'
    assert out['verdict'] == 'False'
    assert out['lenz_score'] == 2
    assert out['verification_id'] == 'pub12345'
    assert out['sources'] == [{'title': 'NASA', 'url': 'https://nasa.gov'}]
    assert out['sources_total'] == 1


def test_submitted_message_names_the_waiting_call_for_every_client(monkeypatch):
    """Every client is told to call get_verification, which WAITS. The old ChatGPT
    variant said "do NOT poll" and the verdict never reached the model (it could
    not summarise or chain into ask_followup). The card is
    mentioned only when it is actually on for this request."""
    monkeypatch.setattr('lenz_mcp.server.request_is_chatgpt', lambda: False)
    claude = server._submitted_message(already_running=False)
    assert 'get_verification' in claude and 'wait' in claude.lower()
    assert 'not poll' not in claude.lower()
    assert 'card' not in claude.lower()

    # A client with the card OFF (the default) reads exactly like Claude.
    # The client is put in scope as a bound PROFILE, the way the middleware
    # binds one: the card reads what this request declared, not its User-Agent.
    monkeypatch.setattr(
        'lenz_mcp.client.client_profile',
        lambda: client.ClientProfile.from_user_agent(
            'openai-mcp/1.0.0', declares_apps=True, declaration_source='request'
        ),
    )
    monkeypatch.setattr(config, 'CARD_ENABLED', False)
    assert server._submitted_message(already_running=False) == claude

    # Card ON: same instruction, plus "don't narrate what the card shows".
    monkeypatch.setattr(config, 'CARD_ENABLED', True)
    card = server._submitted_message(already_running=False)
    assert 'get_verification' in card and 'card' in card.lower()
    assert 'not poll' not in card.lower()


# ── check_usage ──────────────────────────────────────────────────────


def test_check_usage(monkeypatch):
    _patch_api(
        monkeypatch,
        'me_usage',
        ApiResponse(
            status=200,
            data={
                'plan': 'free',
                'assess': {'remaining': 100},
                'verify': {'remaining': 10},
                'quota_resets_at': '2026-07-01T00:00:00+00:00',
            },
        ),
    )
    out = _run(server.check_usage(_ctx()))
    assert out['plan'] == 'free'
    assert out['assess_remaining'] == 100
    assert out['verify_remaining'] == 10
    assert out['resets_at'].startswith('2026-07-01')
    # an API without the pool sends no prices; the agent gets empty maps, not None
    assert out['costs'] == {}
    assert out['cost_options'] == {}


def test_check_usage_passes_the_price_list_and_depth_prices_through(monkeypatch):
    """`verify_claim` exposes `depth`, so the price of `low` has to reach the
    agent — it lives under `cost_options`, not `costs` (which is keyed by
    capability at its default price)."""
    _patch_api(
        monkeypatch,
        'me_usage',
        ApiResponse(
            status=200,
            data={
                'plan': 'pro',
                'credits': {'remaining': 4990, 'total': 5000},
                'costs': {'verify': 10, 'assess': 1, 'ask': 1, 'extract': 0},
                'cost_options': {'verify': {'depth': {'standard': 10, 'low': 5}}},
            },
        ),
    )
    out = _run(server.check_usage(_ctx()))
    assert out['credits_remaining'] == 4990
    assert out['costs']['verify'] == 10
    assert out['cost_options']['verify']['depth']['low'] == 5


# ── ask ──────────────────────────────────────────────────────────────


def test_ask_happy_path(monkeypatch):
    _patch_api(
        monkeypatch,
        'ask',
        ApiResponse(
            status=200,
            data={'role': 'expert', 'content': 'The NASA source is strongest.', 'created_at': '2026-07-04T00:00:00Z'},
        ),
    )
    out = _run(server.ask_followup('pub99999', 'which source is strongest?', _ctx()))
    assert out['status'] == 'ok'
    assert out['answer'] == 'The NASA source is strongest.'


def test_ask_quota_exhausted(monkeypatch):
    # /ask quota rejection is a 402.
    _patch_api(
        monkeypatch,
        'ask',
        ApiResponse(status=402, data={'detail': 'No remaining ask credits.', 'code': 'no_credits'}),
    )
    out = _run(server.ask_followup('vid', 'q', _ctx()))
    assert out['status'] == 'quota_exhausted'
    assert out['manage_url'] == config.PLANS_URL


def test_ask_not_completed_maps_to_distinct_status(monkeypatch):
    # 400 = verification not completed; surfaced distinctly, not a generic error.
    _patch_api(
        monkeypatch,
        'ask',
        ApiResponse(status=400, data={'detail': 'Ask is only available for completed verifications.'}),
    )
    out = _run(server.ask_followup('vid', 'q', _ctx()))
    assert out['status'] == 'not_completed'
    assert 'get_verification' in out['message']


def test_ask_transport_error_warns_against_blind_retry(monkeypatch):
    _patch_api(monkeypatch, 'ask', ApiResponse(status=0, data={}))
    out = _run(server.ask_followup('vid', 'q', _ctx()))
    assert out['status'] == 'error'
    assert 'charged' in out['message']  # non-idempotent: don't blindly resend


def test_client_ask_targets_ask_endpoint(monkeypatch):
    cap = _capture_request(monkeypatch)
    _run(client.ask('Bearer k', verification_id='V1', message='hi', language='es'))
    assert cap['method'] == 'POST'
    assert cap['path'] == '/ask/V1'
    assert cap['json'] == {'message': 'hi', 'language': 'es'}
    assert cap['timeout'] == client.config.ASK_TIMEOUT
    assert cap['idempotency_key'] is None  # conversational append — no dedupe key


def test_sync_tool_read_timeouts_clear_the_observed_backend_ceiling():
    """A read timeout at or below what the backend actually takes turns a slow-
    but-healthy request into a false "Couldn't reach the Lenz API" for the user
    — the API would finish the work, and we would have stopped listening.

    Both ceilings leave real headroom above the slowest expected responses of
    /assess and /ask, not a value that merely matches them.
    """
    assert config.ASSESS_TIMEOUT >= 50, 'the assess timeout lost its headroom'
    assert config.ASK_TIMEOUT >= 45, 'the ask timeout lost its headroom'
    # Both must stay well under any request timeout in front of the server,
    # so a stuck upstream still returns a clean tool error.
    assert max(config.ASSESS_TIMEOUT, config.ASK_TIMEOUT) <= 120


# ── outbound HTTP headers ────────────────────────────────────────────


def test_outbound_headers_forward_auth_and_stamp_ua():
    headers = client._headers('Bearer lenz_abc')
    assert headers['Authorization'] == 'Bearer lenz_abc'
    assert headers['User-Agent'] == config.USER_AGENT
    assert 'lenz-mcp' in headers['User-Agent']
    # No idempotency key unless one is passed (GETs don't get one).
    assert 'Idempotency-Key' not in headers


def test_outbound_headers_include_idempotency_key_when_passed():
    headers = client._headers('Bearer lenz_abc', idempotency_key='abc123')
    assert headers['Idempotency-Key'] == 'abc123'


def test_outbound_headers_omit_auth_when_absent():
    assert 'Authorization' not in client._headers(None)


# ── idempotency key (content-derived, retry-safe) ─────────────────────


def test_idem_key_is_deterministic_and_content_scoped():
    # Same logical operation → same key (so a retry dedupes on the API side).
    assert client._idem_key('verify', 'the earth is round', 'en') == client._idem_key(
        'verify', 'the earth is round', 'en'
    )
    # Different tool / text / language → different key (distinct operations run).
    assert client._idem_key('verify', 'x', 'en') != client._idem_key('assess', 'x', 'en')
    assert client._idem_key('verify', 'x', 'en') != client._idem_key('verify', 'y', 'en')
    assert client._idem_key('verify', 'x', 'en') != client._idem_key('verify', 'x', 'es')


def test_write_tools_send_stable_content_derived_key(monkeypatch):
    cap = _capture_request(monkeypatch)
    _run(client.assess('Bearer k', text='hi', language='en'))
    first = cap['idempotency_key']
    _run(client.assess('Bearer k', text='hi', language='en'))
    second = cap['idempotency_key']
    assert first and first == second  # identical assess → identical key → dedupe
    assert first == client._idem_key('assess', 'hi', 'en')


def test_verify_idempotency_key_separates_depths(monkeypatch):
    """The API answers 422 when a key is reused with a different body, so
    the same claim at `standard` and at `low` must carry different keys —
    otherwise a legitimate depth switch is rejected as an invalid request.
    The default depth keeps the key shape from before the parameter existed,
    so a repeat of an older submission still replays instead of re-charging."""
    cap = _capture_request(monkeypatch)
    _run(client.verify('Bearer k', text='hi', language='en', depth='standard'))
    standard = cap['idempotency_key']
    assert standard == client._idem_key('verify', 'hi', 'en')  # legacy shape for the default
    _run(client.verify('Bearer k', text='hi', language='en', depth='low'))
    low = cap['idempotency_key']
    assert standard and low and standard != low
    _run(client.verify('Bearer k', text='hi', language='en', depth='low'))
    assert cap['idempotency_key'] == low  # a retry of the same low call still dedupes


def test_get_requests_send_no_idempotency_key(monkeypatch):
    cap = _capture_request(monkeypatch)
    _run(client.verify_status('Bearer k', task_id='T1'))
    assert cap['idempotency_key'] is None
    _run(client.me_usage('Bearer k'))
    assert cap['idempotency_key'] is None


# ── DNS-rebinding transport security ──────────────────────────────────


def test_transport_security_off_when_no_allowlist():
    ts = server.build_transport_security([])
    assert ts.enable_dns_rebinding_protection is False


def test_transport_security_on_with_allowlist():
    ts = server.build_transport_security(['lenz.io', 'localhost:*'])
    assert ts.enable_dns_rebinding_protection is True
    assert ts.allowed_hosts == ['lenz.io', 'localhost:*']
    assert 'https://lenz.io' in ts.allowed_origins
    assert 'http://lenz.io' in ts.allowed_origins


# ── link resolution ──────────────────────────────────────────────────


def test_verification_id_extraction():
    assert links.verification_id_from_verification_url('https://lenz.io/api/v1/verifications/abc12345') == 'abc12345'
    assert links.verification_id_from_verification_url(None) is None
    assert links.verification_id_from_verification_url('') is None


def test_branded_link_none_verification_id():
    assert links.branded_link(None) is None


def test_branded_link_builds_utm_url_from_verification_id():
    # Pure string-building, no DB: the bare verification_id goes straight into /c/,
    # and the web app's 301 (query-preserving) canonicalizes to /c/<slug>.
    url = links.branded_link('abc12345')
    assert url == (f'{config.FRONTEND_URL}/c/abc12345?utm_source=lenz&utm_medium=mcp&utm_campaign=mcp-server')


# ── client HTTP layer (the MCP↔API integration boundary) ──────────────


def _mock_transport(monkeypatch, handler):
    """Make client's httpx.AsyncClient route through a MockTransport (no network)."""
    real_client = httpx.AsyncClient  # capture before patching to avoid recursion

    def _factory(*_args, **kwargs):
        kwargs.pop('transport', None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(client.httpx, 'AsyncClient', _factory)
    client._http_client = None  # force the shared client to rebuild via the patched factory


def test_request_success_forwards_auth_and_ua(monkeypatch):
    seen = {}

    def handler(request):
        seen['method'] = request.method
        seen['url'] = str(request.url)
        seen['auth'] = request.headers.get('authorization')
        seen['ua'] = request.headers.get('user-agent')
        return httpx.Response(200, json={'ok': True})

    _mock_transport(monkeypatch, handler)
    resp = _run(client._request('GET', '/x', 'Bearer k'))
    assert resp.ok and resp.status == 200 and resp.data == {'ok': True}
    assert seen['method'] == 'GET'
    assert seen['url'].endswith('/api/v1/x')  # config.API_BASE_URL + path
    assert seen['auth'] == 'Bearer k'
    assert 'lenz-mcp' in seen['ua']


def test_request_non_dict_json_is_wrapped(monkeypatch):
    _mock_transport(monkeypatch, lambda req: httpx.Response(200, json=[1, 2, 3]))
    resp = _run(client._request('GET', '/x', 'Bearer k'))
    assert resp.data == {'_raw': [1, 2, 3]}


def test_request_non_json_body_is_empty_dict(monkeypatch):
    _mock_transport(monkeypatch, lambda req: httpx.Response(200, text='not json'))
    resp = _run(client._request('GET', '/x', 'Bearer k'))
    assert resp.status == 200 and resp.data == {}


def test_request_transport_error_yields_status_0(monkeypatch):
    def handler(_request):
        raise httpx.ConnectError('boom')

    _mock_transport(monkeypatch, handler)
    resp = _run(client._request('GET', '/x', 'Bearer k'))
    assert not resp.ok and resp.status == 0 and resp.data == {}


def _capture_request(monkeypatch):
    """Capture the args (method, path, json, timeout, auth, idempotency_key) of the next _request call."""
    cap = {}

    async def _fake(
        method, path, authorization, *, json=None, timeout=client.config.DEFAULT_TIMEOUT, idempotency_key=None
    ):
        cap.update(
            method=method, path=path, json=json, timeout=timeout, auth=authorization, idempotency_key=idempotency_key
        )
        return ApiResponse(status=200, data={})

    monkeypatch.setattr(client, '_request', _fake)
    return cap


def test_client_assess_targets_assess_endpoint(monkeypatch):
    cap = _capture_request(monkeypatch)
    _run(client.assess('Bearer k', text='hi', language='es'))
    assert cap['method'] == 'POST'
    assert cap['path'] == '/assess'
    assert cap['json'] == {'claim': 'hi', 'language': 'es'}
    assert cap['timeout'] == client.config.ASSESS_TIMEOUT  # assess gets the generous read timeout
    assert cap['auth'] == 'Bearer k'


def test_client_verify_targets_verify_endpoint(monkeypatch):
    cap = _capture_request(monkeypatch)
    _run(client.verify('Bearer k', text='hi', language=''))
    assert cap['method'] == 'POST'
    assert cap['path'] == '/verify'
    assert cap['json'] == {'claim': 'hi', 'language': '', 'depth': 'standard'}


def test_client_verify_sends_the_requested_depth(monkeypatch):
    cap = _capture_request(monkeypatch)
    _run(client.verify('Bearer k', text='hi', language='', depth='low'))
    assert cap['json'] == {'claim': 'hi', 'language': '', 'depth': 'low'}


def test_client_verify_status_targets_status_endpoint(monkeypatch):
    cap = _capture_request(monkeypatch)
    _run(client.verify_status('Bearer k', task_id='T1'))
    assert cap['method'] == 'GET'
    assert cap['path'] == '/verify/status/T1'
    assert cap['json'] is None


def test_client_verification_detail_targets_verifications_endpoint(monkeypatch):
    cap = _capture_request(monkeypatch)
    _run(client.verification_detail('Bearer k', verification_id='pub12345'))
    assert cap['method'] == 'GET'
    assert cap['path'] == '/verifications/pub12345'
    assert cap['json'] is None


def test_client_me_usage_targets_usage_endpoint(monkeypatch):
    cap = _capture_request(monkeypatch)
    _run(client.me_usage('Bearer k'))
    assert cap['method'] == 'GET'
    assert cap['path'] == '/me/usage'
    assert cap['json'] is None


# ── ASGI app + MCP protocol wiring ────────────────────────────────────


def test_asgi_app_builds_and_exposes_routes():
    # Imports lenz_mcp.asgi → configures logging + builds the FastMCP app.
    # Guards against startup/wiring regressions (the service never connects to a DB).
    from lenz_mcp.asgi import application

    paths = {getattr(r, 'path', getattr(r, 'path_format', None)) for r in application.routes}
    assert '/mcp' in paths
    assert '/healthz' in paths
    # /mcp/healthz is the liveness path under the /mcp prefix, for a proxy
    # that forwards only /mcp/* to this server.
    assert '/mcp/healthz' in paths


def test_mcp_healthz_route_responds_200():
    # A liveness probe GETs /mcp/healthz; prove it routes to the no-auth, no-I/O
    # handler (not shadowed by the exact /mcp protocol route) and returns 200.
    from starlette.testclient import TestClient

    from lenz_mcp.asgi import application

    # No context manager: skip lifespan (the session manager) — the custom
    # route is independent of it, and we only care about routing + response.
    client = TestClient(application)
    for path in ('/healthz', '/mcp/healthz'):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.json() == {'status': 'ok'}, path


# The server instructions are pinned in test_first_check.py.


def test_protocol_all_tools_registered():
    tools = _run(server.mcp.list_tools())
    assert {t.name for t in tools} == {
        'assess_claim',
        'verify_claim',
        'select_claims',
        'get_verification',
        'get_verification_widget',
        'start_verification_widget',
        'select_claims_widget',
        'check_usage',
        'ask_followup',
        'list_verifications',
    }


# Read-only tools per their ToolAnnotations — the rest consume quota / mutate state.
# get_verification_widget is the widget-only twin of get_verification (both read-only).
# assess_claim and ask_followup are read-only too: the hint means
# "modifies nothing in the user's environment", which both satisfy; each costs
# one credit. verify_claim (10 credits) and select_claims (it resumes a verify)
# stay False, so the calls worth an approval click keep one where a client asks.
# list_verifications is a free GET of completed results.
_READ_ONLY_TOOLS = {
    'get_verification',
    'get_verification_widget',
    'check_usage',
    'assess_claim',
    'ask_followup',
    'list_verifications',
}


def test_protocol_every_tool_has_title_and_described_params():
    """Connector directories and catalog scorers grade tool metadata: each
    tool needs a human title (both the top-level title AND the
    annotations.title that directories check), and every input parameter
    needs a non-empty description. Guards against a new tool shipping bare."""
    for tool in _run(server.mcp.list_tools()):
        assert tool.title, f'{tool.name} is missing a title'
        assert getattr(tool.annotations, 'title', None), f'{tool.name} is missing annotations.title'
        props = (tool.input_schema or {}).get('properties', {})
        undocumented = [p for p, schema in props.items() if not (schema.get('description') or '').strip()]
        assert not undocumented, f'{tool.name} has undocumented params: {undocumented}'


def test_protocol_read_only_hints_match_tool_semantics():
    """read-only tools must advertise readOnlyHint=True so clients can surface
    them as safe; quota-consuming (write) tools must not claim to be read-only.
    EVERY tool must set destructiveHint explicitly — both the Anthropic AND the
    OpenAI directories flag a tool with no destructiveHint annotation, so none
    may ship without it. None of our tools perform destructive updates, so the
    value is always False.

    The names below are 2.x's python-side ones; `readOnlyHint` / `destructiveHint`
    are still what goes on the wire (they are the fields' aliases, and still the
    constructor kwargs), so the directories see exactly what this asserts."""
    for tool in _run(server.mcp.list_tools()):
        read_only = tool.name in _READ_ONLY_TOOLS
        hint = bool(getattr(tool.annotations, 'read_only_hint', False))
        assert hint is read_only, f'{tool.name} has wrong readOnlyHint ({hint})'
        destructive = getattr(tool.annotations, 'destructive_hint', None)
        assert destructive is False, f'{tool.name} must set destructiveHint=False explicitly, got {destructive}'


def test_protocol_verify_depth_is_a_closed_enum():
    """`depth` is advertised as an enum with its default, so a client sees
    the two legal values in the manifest instead of guessing — and FastMCP
    rejects anything else before the API is called."""
    tool = next(t for t in _run(server.mcp.list_tools()) if t.name == 'verify_claim')
    depth = tool.input_schema['properties']['depth']
    assert depth['enum'] == ['standard', 'low']
    assert depth['default'] == 'standard'
    assert 'depth' not in tool.input_schema.get('required', [])


def test_protocol_language_is_a_closed_enum_on_every_tool_that_takes_it():
    """`language` must advertise exactly the codes the API serves, so an
    unsupported one never leaves the client.

    Typed as a bare `str`, the field let a client model pass the user's own
    locale (`ko`, `ja`, `ru`, …) and collect a 422 it could only recover from
    by reading the error text and retrying: a first experience of Lenz made
    of failed tool calls. The enum is the
    connector's copy of the API's codes (`config.SUPPORTED_LANGUAGES`, which
    must match the codes the API serves).
    """
    SUPPORTED_LANGUAGES = config.SUPPORTED_LANGUAGES

    tools = {t.name: t for t in _run(server.mcp.list_tools())}
    takes_language = {n for n, t in tools.items() if 'language' in (t.input_schema or {}).get('properties', {})}
    assert takes_language == {'assess_claim', 'verify_claim', 'ask_followup'}

    for name in sorted(takes_language):
        schema = tools[name].input_schema
        language = schema['properties']['language']
        assert language['enum'] == ['', *SUPPORTED_LANGUAGES], name
        assert language['default'] == '', name
        assert 'language' not in schema.get('required', []), name


def test_protocol_ask_followup_question_declares_the_api_length_cap():
    """The `question` bound is advertised in the schema, and it is the SAME
    number the API enforces.

    The connector mirrors the API's cap (`config.ASK_MESSAGE_MAX_CHARS`, which
    must equal the limit the API enforces), so this fails only if someone
    reintroduces a literal in the schema. Undeclared, the bound costs a whole
    tool call to discover: an over-long question would come back as a 422.
    """
    tools = {t.name: t for t in _run(server.mcp.list_tools())}
    question = tools['ask_followup'].input_schema['properties']['question']

    assert question['maxLength'] == config.ASK_MESSAGE_MAX_CHARS
    assert str(config.ASK_MESSAGE_MAX_CHARS) in question['description']


def test_protocol_unsupported_language_never_reaches_the_api(monkeypatch):
    """The whole point of the enum: FastMCP rejects `ko` during schema
    validation, so the model self-corrects locally instead of burning a call
    on a 422 the user sees as a failed tool invocation."""
    called = []

    async def _explode(*args, **kwargs):
        called.append(kwargs)
        raise AssertionError('the API must not be called with an unsupported language')

    monkeypatch.setattr(client, 'assess', _explode)

    with pytest.raises(Exception) as excinfo:
        _run(server.mcp.call_tool('assess_claim', {'claim': 'the earth is round', 'language': 'ko'}))

    assert not called
    # The error names the legal values, which is what lets the model recover
    # in one turn rather than by guessing.
    assert "'en'" in str(excinfo.value) and "'bg'" in str(excinfo.value)


def test_protocol_language_description_does_not_invite_the_clients_locale():
    """The description must state the rule, not just the format.

    "Optional ISO 639-1 code … Defaults to English" advertised ~180 codes
    against the twelve we serve AND read as an invitation to fill the field
    in helpfully, so a client could request a verdict in the user's locale
    that nobody asked for, silently and at no error.
    """
    tools = {t.name: t for t in _run(server.mcp.list_tools())}
    for name in ('assess_claim', 'verify_claim', 'ask_followup'):
        description = tools[name].input_schema['properties']['language']['description']
        assert 'ISO 639-1' not in description, name
        assert 'unset' in description.lower(), name
        assert 'explicitly' in description.lower(), name


def test_protocol_call_tool_dispatches_and_injects_context():
    # call_tool goes through the real MCPServer machinery (schema validation +
    # Context injection). With no HTTP request context, _authorization returns
    # None → the auth gate fires, proving the tool is wired end-to-end.
    # 2.x returns a CallToolResult rather than the (content, structured) pair.
    result = _run(server.mcp.call_tool('check_usage', {}))
    assert result.structured_content['status'] == 'auth_required'


# ── config: API base URL resolution ───────────────────────────────────


def test_resolve_api_base_leaves_pathed_url_unchanged():
    assert client.config._resolve_api_base('https://lenz.io/api/v1') == 'https://lenz.io/api/v1'
    assert client.config._resolve_api_base('https://lenz.io/api/v1/') == 'https://lenz.io/api/v1'


def test_resolve_api_base_appends_version_to_bare_origin():
    # An operator may configure a bare origin with no path. It must still
    # resolve /assess and the rest, so the version is appended.
    assert client.config._resolve_api_base('https://api.example.com') == 'https://api.example.com/api/v1'
    assert client.config._resolve_api_base('https://api.example.com/') == 'https://api.example.com/api/v1'


# ── ChatGPT app policy: no upgrade options, ever ──────────────────────
#
# OpenAI's app guidelines forbid commerce in digital goods: an app "cannot
# initiate new subscriptions or display upgrade options". Handing ChatGPT's
# model a pricing URL is displaying one — it renders as an upgrade CTA in the
# conversation, which the guidelines do not allow. Claude / SDK callers are unaffected: they are developers reading a 402,
# and the link is the actionable part of the error there.


@pytest.fixture
def _as_chatgpt(monkeypatch):
    monkeypatch.setattr('lenz_mcp.server.request_is_chatgpt', lambda: True)


def _quota_402():
    return ApiResponse(
        status=402,
        data={'detail': 'No remaining credits.', 'code': 'no_credits', 'upgrade_url': 'https://lenz.io/plans?wall=abc'},
    )


def test_chatgpt_quota_wall_carries_no_upgrade_link(monkeypatch, _as_chatgpt):
    _patch_api(monkeypatch, 'assess', _quota_402())
    out = _run(server.assess_claim('x', _ctx()))
    assert out['status'] == 'quota_exhausted'
    assert 'manage_url' not in out, 'ChatGPT must not be handed a pricing URL'
    assert 'plans' not in str(out).lower()


def test_chatgpt_rate_limit_carries_no_upgrade_link(monkeypatch, _as_chatgpt):
    _patch_api(
        monkeypatch,
        'assess',
        ApiResponse(status=429, data={'detail': 'Slow down.', 'upgrade_url': 'https://lenz.io/plans'}),
    )
    out = _run(server.assess_claim('x', _ctx()))
    assert out['status'] == 'rate_limited'
    assert 'manage_url' not in out


def test_non_chatgpt_clients_keep_the_upgrade_link(monkeypatch):
    """The policy is ChatGPT's. A developer reading a 402 over the SDK still gets the link."""
    monkeypatch.setattr('lenz_mcp.server.request_is_chatgpt', lambda: False)
    _patch_api(monkeypatch, 'assess', _quota_402())
    out = _run(server.assess_claim('x', _ctx()))
    assert out['manage_url'] == 'https://lenz.io/plans?wall=abc'


# ── origin-client attribution ────────────────────────────────────────────


@pytest.fixture
def transport_ua():
    """Bind the calling client's User-Agent for the current MCP request.

    2.x removed the SDK's `request_ctx`. The identity now rides
    `lenz_mcp.client`'s own ContextVar, bound per request by
    `lenz_mcp.middleware` from the inbound header; `bind_client_user_agent` is
    that same call, so the read below is the real path.

    `None` still means "no transport request in scope at all", and on 2.x that
    is the ABSENCE of a binding — not an empty one, which is what a request
    carrying no User-Agent gets. Keeping the two apart is the point of the
    ContextVar having no default.
    """
    resets = []

    def _set(ua):
        if ua is not None:
            resets.append(client.bind_client_user_agent(ua))

    yield _set
    for reset in reversed(resets):
        reset()


def test_outbound_ua_carries_the_origin_client(transport_ua):
    """The API must still see the connector while the real client becomes readable.

    The leading token identifies the calling product to the API, so the origin
    goes in the parenthetical, the convention the API already documents for
    `lenz-zapier/1.0.0 (lenz-io-node 2.6.0)`.
    """
    transport_ua('claude-code/2.1.4 (external, cli)')
    ua = client._headers(None)['User-Agent']

    assert ua.startswith(config.USER_AGENT)
    assert 'claude-code/2.1.4' in ua


def test_outbound_ua_is_unchanged_without_a_request(transport_ua):
    """No transport request in scope (or no UA) → the bare UA, not `lenz-mcp/1.0 ()`."""
    transport_ua(None)
    assert client._headers(None)['User-Agent'] == config.USER_AGENT
    transport_ua('   ')
    assert client._headers(None)['User-Agent'] == config.USER_AGENT


def test_outbound_ua_strips_control_characters_and_parens(transport_ua):
    """The origin UA is attacker-controlled: it reaches an outbound header and
    the API's logs. A CR/LF would forge a header; a stray paren would break the
    comment the parser reads.

    Note this is an ALLOW-list (printable ASCII), against the usual rule for a
    logged field. An earlier version of this test said "blocklist, never
    allow-list a guessed shape" — that reasoning is right for a field whose
    shape we are guessing at, but here a blocklist let \x80-\xff through to a
    header the transport cannot carry. See `client._UA_UNSAFE`: the bound here is not
    a guess about what a UA looks like, it is what the transport can carry.
    """
    transport_ua('evil/1.0\r\nX-Injected: yes\x00 (spoof) \\')
    ua = client._headers(None)['User-Agent']

    assert '\r' not in ua and '\n' not in ua and '\x00' not in ua
    assert ua.count('(') == 1 and ua.count(')') == 1
    assert 'X-Injected' in ua  # neutralised into the comment, not a header


def test_outbound_ua_survives_a_non_ascii_client(transport_ua):
    """A non-ASCII origin UA must not be able to break every call.

    httpx encodes header values as ASCII while Starlette decodes inbound
    headers as latin-1, so `User-Agent: Cursor/1.0 (caf\xe9)` hands us a str
    that cannot go back out as a header. The resulting UnicodeEncodeError is
    NOT an httpx.HTTPError, so it would escape `_request`'s handler and fail
    every tool call from that client, permanently.
    """
    transport_ua('Cursor/1.0 (caf\xe9) \u2014 \xff')
    ua = client._headers(None)['User-Agent']

    assert ua.encode('ascii')  # would raise before the fix
    assert all(0x20 <= ord(c) <= 0x7E for c in ua)
    # Really reaches the wire: httpx builds the header without raising.
    assert httpx.Headers({'User-Agent': ua}).raw


def test_outbound_ua_is_truncated(transport_ua):
    """Bounded to a short header: the API records the client, and a long value has no use there."""
    transport_ua('x' * 5000)
    assert len(client._headers(None)['User-Agent']) < 200


# ── the transport bound: values that cannot ride an HTTP request ─────────
#
# `_request`'s body can raise outside `httpx.HTTPError` — a non-ASCII header
# value, a lone surrogate, an unencodable URL. Anything it does not catch
# escapes the tool body and reaches the model as a raw ToolError string, with
# no `mcp_api_transport_error` line for us. These pin the whole class.


async def _unreachable(*_args, **_kwargs):
    raise AssertionError('the API must not be called with an unsendable id')


def test_request_survives_a_non_ascii_authorization_header(monkeypatch):
    """A key pasted with a curly quote or a non-breaking space is the realistic
    trigger: uvicorn accepts \\x80-\\xff inbound and Starlette latin-1-decodes
    it, but httpx encodes header values as ASCII. The UnicodeEncodeError is not
    an httpx.HTTPError — before the fix it escaped and failed every tool call.
    """
    _mock_transport(monkeypatch, lambda req: httpx.Response(200, json={'ok': True}))
    resp = _run(client._request('GET', '/x', 'Bearer lenz_caf\xe9'))
    assert resp.status == 0 and resp.data == {}


def test_request_survives_an_unencodable_url(monkeypatch):
    """`httpx.InvalidURL.__mro__` is (InvalidURL, Exception, ...) — NOT an
    HTTPError — so a task_id carrying a CR escaped the handler."""
    _mock_transport(monkeypatch, lambda req: httpx.Response(200, json={'ok': True}))
    resp = _run(client._request('GET', '/verify/status/ab\rcd', 'Bearer k'))
    assert resp.status == 0 and resp.data == {}


def test_idem_key_survives_a_lone_surrogate():
    """`json.loads` accepts "\\ud800" from a tool argument, and `.encode()` then
    raises UnicodeEncodeError — outside `_request`'s try entirely."""
    assert client._idem_key('verify', 'a\ud800b', 'en')  # would raise before the fix


def test_request_strips_lone_surrogates_from_the_json_body(monkeypatch):
    """A lone surrogate cannot be UTF-8 encoded, so it cannot be sent at all.
    Dropping it sends the claim; keeping it fails the call."""
    seen = {}

    def handler(request):
        seen['body'] = request.content
        return httpx.Response(200, json={'ok': True})

    _mock_transport(monkeypatch, handler)
    resp = _run(client._request('POST', '/assess', 'Bearer k', json={'claim': 'a\ud800b'}))
    assert resp.status == 200
    assert b'ab' in seen['body'] and b'\\ud800' not in seen['body']


def test_a_key_that_cannot_be_sent_is_named_at_the_gate(monkeypatch):
    """ "Couldn't reach the Lenz API" is the wrong diagnosis for a malformed key,
    and it leaves the user nothing to act on. The gate names it and links the
    key page instead."""
    monkeypatch.setattr(client, 'assess', _unreachable)
    out = _run(server.assess_claim('the earth is round', _ctx(auth='Bearer lenz_caf\xe9')))
    assert out['status'] == 'auth_required'
    assert config.API_CREDENTIALS_URL in out['message']


def test_all_tools_gate_on_an_unsendable_key():
    bad = 'Bearer lenz_caf\xe9'
    assert _run(server.verify_claim('x', _ctx(auth=bad)))['status'] == 'auth_required'
    assert _run(server.get_verification('tid', _ctx(auth=bad)))['status'] == 'auth_required'
    assert _run(server.select_claims('tid', ['a'], _ctx(auth=bad)))['status'] == 'auth_required'
    assert _run(server.check_usage(_ctx(auth=bad)))['status'] == 'auth_required'
    assert _run(server.ask_followup('vid', 'q', _ctx(auth=bad)))['status'] == 'auth_required'
    assert _run(server.assess_claim('x', _ctx(auth=bad)))['status'] == 'auth_required'


@pytest.mark.parametrize('ident', ['../../admin', 'ab\rcd', 'a\ud800b', 'caf\xe9', 'a b', ''])
def test_ids_that_cannot_ride_a_url_path_are_rejected(monkeypatch, ident):
    """`f'/verify/status/{task_id}'` with `../../admin` resolves to
    `/api/v1/admin`; a CR raises InvalidURL; a surrogate cannot be encoded.
    None of them reaches the API."""
    monkeypatch.setattr(client, 'verify_status', _unreachable)
    monkeypatch.setattr(client, 'select', _unreachable)
    monkeypatch.setattr(client, 'ask', _unreachable)
    monkeypatch.setattr(client, 'verification_detail', _unreachable)

    for out in (
        _run(server.get_verification(ident, _ctx())),
        _run(server.get_verification_widget(ident, _ctx())),
        _run(server.select_claims(ident, ['a'], _ctx())),
        _run(server.ask_followup(ident, 'q', _ctx())),
    ):
        assert out['status'] == 'invalid_request', out


def test_a_well_formed_id_still_reaches_the_api(monkeypatch):
    """The bound is what a URL path can carry, not a guess at the id's shape —
    the routing rule (8-hex is a stored result, anything else is a run to
    wait on) is unchanged."""
    calls = _status_sequence(monkeypatch, _COMPLETED)
    for ident in ('0123456789abcdef0123456789abcdef', 'PUB12345', 'abc1234', 'task-1', 'tid'):
        _run(server.get_verification(ident, _ctx()))
    assert calls == ['0123456789abcdef0123456789abcdef', 'PUB12345', 'abc1234', 'task-1', 'tid']
