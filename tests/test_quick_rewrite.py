"""The quick check carries a suggested rewrite.

`assess_claim` always asks the API for one (it is not a tool argument), and a
row that comes back with a non-empty `suggested_rewrite` forwards it under a
note that says what it is: built from the reviewers' reasoning, not from
sources, and not checked itself. A row without one is exactly what it was.
"""

from __future__ import annotations

import asyncio
import json
import types

import httpx
import pytest

from lenz_mcp import client, server
from lenz_mcp.client import ApiResponse

REWRITE = 'The registry lists 4,200 filings for 2024.'


def _ctx():
    request = types.SimpleNamespace(headers={'authorization': 'Bearer lenz_testkey'})
    return types.SimpleNamespace(request_context=types.SimpleNamespace(request=request))


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_shared_http_client():
    client._http_client = None
    yield
    client._http_client = None


def _canonical_row(**extra):
    row = {
        'claim': 'The registry lists 5,000 filings for 2024.',
        'language': 'en',
        'status': 'completed',
        'verdict': 'False',
        'confidence': 'high',
        'verification_url': None,
        'more_claims': [],
        'rationale': 'The registry lists 4,200 filings.',
        'dissent': None,
        'failure': None,
    }
    row.update(extra)
    return row


def _assess(monkeypatch, rows):
    async def _fake(authorization, **kwargs):
        return ApiResponse(status=200, data={'claims': rows})

    monkeypatch.setattr(client, 'assess', _fake)
    return _run(server.assess_claim('x', _ctx()))


# ── what is sent ─────────────────────────────────────────────────────


def _sent(monkeypatch, **kwargs):
    sent = []

    def _handler(request):
        sent.append(request)
        return httpx.Response(200, json={'claims': []})

    monkeypatch.setattr(client, '_http_client', httpx.AsyncClient(transport=httpx.MockTransport(_handler)))
    _run(client.assess('Bearer lenz_testkey', **kwargs))
    return json.loads(sent[0].content), sent[0].headers['idempotency-key']


def test_the_tool_always_asks_for_a_rewrite(monkeypatch):
    seen = {}

    async def _fake(authorization, **kwargs):
        seen.update(kwargs)
        return ApiResponse(status=200, data={'claims': []})

    monkeypatch.setattr(client, 'assess', _fake)
    _run(server.assess_claim('x', _ctx()))
    assert seen['suggest_rewrite'] is True
    seen.clear()
    _run(server.assess_claim(claims=['a', 'b'], ctx=_ctx()))
    assert seen['suggest_rewrite'] is True


def test_it_is_not_an_argument_the_model_can_see():
    tools = {t.name: t for t in _run(server.mcp.list_tools())}
    assert 'suggest_rewrite' not in tools['assess_claim'].input_schema['properties']


def test_the_body_carries_the_flag_only_when_asked(monkeypatch):
    # The single form goes out as `text` (the SDK's spelling; the API reads both).
    body, _key = _sent(monkeypatch, text='a b', language='', suggest_rewrite=True)
    assert body == {'text': 'a b', 'language': 'auto', 'suggest_rewrite': True}
    body, _key = _sent(monkeypatch, claims=['a', 'b'], language='', suggest_rewrite=True)
    assert body == {'claims': ['a', 'b'], 'language': 'auto', 'suggest_rewrite': True}
    # A call that does not ask sends what it always sent.
    body, _key = _sent(monkeypatch, text='a b', language='')
    assert body == {'text': 'a b', 'language': 'auto'}


def test_a_key_from_before_the_flag_is_unchanged_and_the_flag_makes_a_new_one(monkeypatch):
    _body, plain = _sent(monkeypatch, text='a b', language='')
    assert plain == client._idem_key('assess', 'a b', 'auto')
    _body, plain_list = _sent(monkeypatch, claims=['a', 'b'], language='')
    assert plain_list == client._idem_key('assess', 'claims', '\x1f'.join(['a', 'b']), 'auto')
    _body, flagged = _sent(monkeypatch, text='a b', language='', suggest_rewrite=True)
    _body, flagged_again = _sent(monkeypatch, text='a b', language='', suggest_rewrite=True)
    _body, flagged_list = _sent(monkeypatch, claims=['a', 'b'], language='', suggest_rewrite=True)
    assert flagged == flagged_again == client._idem_key('assess', 'a b', 'auto', 'suggest_rewrite')
    # A body with the flag never reuses the key of one without it, or the API would refuse it.
    assert flagged != plain and flagged_list != plain_list


# ── what comes back ──────────────────────────────────────────────────


def test_a_rewrite_is_forwarded_under_the_quick_check_note(monkeypatch):
    out = _assess(monkeypatch, [_canonical_row(suggested_rewrite=f'  {REWRITE}  ')])
    row = out['claims'][0]
    assert row['suggested_rewrite'] == REWRITE
    assert row['suggested_rewrite_note'] == server.QUICK_REWRITE_NOTE


@pytest.mark.parametrize('value', [None, '', '   ', 5, ['x'], {'a': 1}, True])
def test_no_usable_rewrite_changes_nothing(monkeypatch, value):
    with_key = _assess(monkeypatch, [_canonical_row(suggested_rewrite=value)])['claims'][0]
    without_key = _assess(monkeypatch, [_canonical_row()])['claims'][0]
    assert with_key == without_key
    assert 'suggested_rewrite' not in with_key and 'suggested_rewrite_note' not in with_key


def test_a_row_from_an_api_that_never_sent_the_key_is_what_it_was(monkeypatch):
    row = _canonical_row()
    assert 'suggested_rewrite' not in row
    out = _assess(monkeypatch, [row])['claims'][0]
    assert out == {
        'claim': 'The registry lists 5,000 filings for 2024.',
        'verdict': 'False',
        'confidence': 'high',
        'rationale': 'The registry lists 4,200 filings.',
    }


def test_the_note_rides_only_on_the_rows_that_have_one(monkeypatch):
    rows = [
        _canonical_row(suggested_rewrite=REWRITE),
        _canonical_row(claim='B.'),
        _canonical_row(claim='C.', suggested_rewrite=None),
    ]
    out = _assess(monkeypatch, rows)['claims']
    assert [('suggested_rewrite' in r, 'suggested_rewrite_note' in r) for r in out] == [
        (True, True),
        (False, False),
        (False, False),
    ]


def test_an_error_row_never_carries_one(monkeypatch):
    row = _canonical_row(
        status='failed', verdict=None, confidence=None, rationale=None, failure={'code': 'no_checkable_claim'}
    )
    out = _assess(monkeypatch, [row])['claims'][0]
    assert 'suggested_rewrite' not in out


def test_a_deep_tier_row_serves_the_same_note(monkeypatch):
    # A claim already deep-checked is served with that check's rewrite in the quick row's shape.
    row = _canonical_row(suggested_rewrite=REWRITE, verification_url='https://lenz.io/api/v1/verifications/abc12345')
    out = _assess(monkeypatch, [row])['claims'][0]
    assert out['suggested_rewrite_note'] == server.QUICK_REWRITE_NOTE
    assert out['suggested_rewrite_note'] != server.SUGGESTED_REWRITE_NOTE


def test_the_note_says_what_it_is_and_is_not_the_deep_checks_wording():
    note = server.QUICK_REWRITE_NOTE
    assert "reviewers' reasoning" in note and 'not from sources' in note
    assert 'not been checked' in note
    assert 'suggestion' in note and 'deep check' in note
    assert "this check's findings" not in note
    assert 'credit' not in note.lower()


def test_the_description_has_one_sentence_about_it():
    description = ' '.join({t.name: t for t in _run(server.mcp.list_tools())}['assess_claim'].description.split())
    assert description.count('suggested_rewrite') == 1
    assert 'not verified' in description


# ── a slow quick check on a host with less room ──────────────────────


@pytest.mark.parametrize(
    ('user_agent', 'timeout'),
    [('openai-mcp/1.0.0 (Responses API)', 55.0), ('Claude-User/1.0', 105.0)],
)
def test_a_quick_check_that_outlasts_its_host_ends_the_way_it_always_did(monkeypatch, user_agent, timeout):
    """The rewrite adds time on the server only: the connector's timeouts are not changed.

    A call that runs past the host's limit ends as the graceful result it already
    had, with the rewrite flag on.
    """
    seen = []

    def _slow(request):
        seen.append(request)
        raise httpx.ReadTimeout('slow', request=request)

    monkeypatch.setattr(client, '_http_client', httpx.AsyncClient(transport=httpx.MockTransport(_slow)))
    reset = client.bind_client_user_agent(user_agent)
    try:
        out = _run(server.assess_claim('A claim to check.', _ctx()))
    finally:
        reset()
    assert json.loads(seen[0].content)['suggest_rewrite'] is True
    assert seen[0].extensions['timeout']['read'] == timeout
    assert out == {'status': 'error', 'message': "Couldn't reach the Lenz API — please retry shortly."}
