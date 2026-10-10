"""Which mounting a card belongs to, on the real MCP wire.

The result that MOUNTS a card (`assess_claim`, `verify_claim`,
`get_verification`) says which conversation it is for and which call produced
it, so the card can tell a later chat about the same claim from a replay of an
earlier one (card/src/logic/identity.js reads it). What these pin:

- the stamp rides in `structuredContent._card` of exactly the mounting tools,
  and the card-only tools that service an existing card never mint one;
- `conversation` is a hash of the host's own conversation id (ChatGPT's
  `openai/session`, Codex's `threadId` / `sessionId`), absent when the host sends
  none, and neither the raw id nor the user's `openai/subject` appears anywhere
  in a result;
- `call_id` is new for every call;
- a quick check's `language` code rides on its rows for a card client, and the
  card-only start tool forwards it to the API.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re

import pytest

from lenz_mcp import client, config, mcp_card
from lenz_mcp.client import ApiResponse
from lenz_mcp.testing import DECLARES_APPS, DECLARES_NO_APPS, LEGACY, MODERN, assembled_app

KEY = 'Bearer lenz_x'
CHATGPT = 'openai-mcp/1.0.0 (ChatGPT)'
CODEX = 'openai-mcp/1.0.0 (Codex)'
CLAUDE = 'Claude-User'

SESSION = 'sess-9f3a-RAW-conversation-id'
SUBJECT = 'subject-5b2c-RAW-user-id'


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


@contextlib.contextmanager
def _app(*, card=True):
    with assembled_app(MCP_OAUTH_ENABLED=False, MCP_CARD_ENABLED=card) as harness:
        yield harness


def _call(harness, tool, arguments, *, ua=CHATGPT, meta=None, era=MODERN, capabilities=DECLARES_APPS):
    wire = harness.wire(user_agent=ua, authorization=KEY, capabilities=capabilities)
    if era == LEGACY:
        wire.initialize()
    params = {'name': tool, 'arguments': arguments}
    if meta is not None:
        params['_meta'] = meta
    result = wire.call('tools/call', params, era=era, name=tool)
    return result


def _stamp(result):
    return (result.get('structuredContent') or {}).get(mcp_card.CARD_RESULT_NAMESPACE)


@pytest.fixture
def api(monkeypatch):
    """The API, stubbed at the client module (which the harness does not re-import)."""
    seen = {'verify': []}

    async def _assess(authorization, **kwargs):
        return ApiResponse(
            status=200,
            data={
                'claims': [{'claim': 'Honey never spoils.', 'language': 'en', 'verdict': 'True', 'confidence': 'high'}]
            },
        )

    async def _verify(authorization, **kwargs):
        seen['verify'].append(kwargs)
        return ApiResponse(status=202, data={'task_id': 't' * 32})

    async def _verify_status(authorization, *, task_id):
        return ApiResponse(status=200, data={'status': 'failed', 'error': 'The verification failed.'})

    async def _detail(authorization, *, verification_id):
        return ApiResponse(status=404, data={})

    monkeypatch.setattr(client, 'assess', _assess)
    monkeypatch.setattr(client, 'verify', _verify)
    monkeypatch.setattr(client, 'verify_status', _verify_status)
    monkeypatch.setattr(client, 'verification_detail', _detail)
    return seen


MOUNTING_CALLS = {
    'assess_claim': {'claim': 'Honey never spoils.'},
    'verify_claim': {'claim': 'Honey never spoils.'},
    'get_verification': {'task_id': 'abcd1234'},
}


def test_the_calls_this_file_makes_are_the_mounting_tools():
    assert set(MOUNTING_CALLS) == set(mcp_card.CARD_TOOL_NAMES)


@pytest.mark.parametrize('era', [MODERN, LEGACY])
@pytest.mark.parametrize('tool', sorted(MOUNTING_CALLS))
def test_a_mounting_result_carries_the_conversation_hash_and_a_call_id(api, tool, era):
    with _app() as harness:
        result = _call(
            harness,
            tool,
            MOUNTING_CALLS[tool],
            meta={'openai/session': SESSION, 'openai/subject': SUBJECT},
            era=era,
        )
    stamp = _stamp(result)
    assert stamp['conversation'] == _hash(SESSION)
    assert re.fullmatch(r'[0-9a-f]{16}', stamp['call_id'])


@pytest.mark.parametrize('tool', sorted(MOUNTING_CALLS))
def test_neither_the_raw_session_nor_the_user_id_appears_anywhere_in_a_result(api, tool):
    with _app() as harness:
        result = _call(
            harness,
            tool,
            MOUNTING_CALLS[tool],
            meta={'openai/session': SESSION, 'openai/subject': SUBJECT, 'threadId': 'thread-RAW-1'},
        )
    everything = json.dumps(result)
    for raw in (SESSION, SUBJECT, 'thread-RAW-1'):
        assert raw not in everything


def test_a_new_call_is_a_new_id_and_a_new_conversation_is_a_new_hash(api):
    with _app() as harness:
        first = _stamp(_call(harness, 'assess_claim', {'claim': 'x'}, meta={'openai/session': 'one'}))
        again = _stamp(_call(harness, 'assess_claim', {'claim': 'x'}, meta={'openai/session': 'one'}))
        other = _stamp(_call(harness, 'assess_claim', {'claim': 'x'}, meta={'openai/session': 'two'}))
    assert first['conversation'] == again['conversation'] != other['conversation']
    assert len({first['call_id'], again['call_id'], other['call_id']}) == 3


def test_a_host_with_no_conversation_id_gets_a_call_id_and_no_conversation(api):
    with _app() as harness:
        claude = _stamp(_call(harness, 'assess_claim', {'claim': 'x'}, ua=CLAUDE))
        # The user's id alone is not a conversation.
        subject_only = _stamp(_call(harness, 'assess_claim', {'claim': 'x'}, meta={'openai/subject': SUBJECT}))
        empty = _stamp(_call(harness, 'assess_claim', {'claim': 'x'}, meta={'openai/session': ''}))
    for stamp in (claude, subject_only, empty):
        assert set(stamp) == {'call_id'}


@pytest.mark.parametrize(
    ('meta', 'raw'),
    [
        ({'threadId': 'thread-1'}, 'thread-1'),
        ({'sessionId': 'session-1'}, 'session-1'),
        ({'threadId': 'thread-1', 'sessionId': 'session-1'}, 'thread-1'),
        ({'openai/session': 'chat-1', 'threadId': 'thread-1'}, 'chat-1'),
    ],
)
def test_codex_ids_are_read_too(api, meta, raw):
    with _app() as harness:
        stamp = _stamp(_call(harness, 'assess_claim', {'claim': 'x'}, ua=CODEX, meta=meta))
    assert stamp['conversation'] == _hash(raw)


@pytest.mark.parametrize('bad', [42, None, ['a'], {'a': 1}, '   ', 'x' * 513])
def test_a_conversation_id_that_is_not_a_usable_string_is_ignored(api, bad):
    with _app() as harness:
        stamp = _stamp(_call(harness, 'assess_claim', {'claim': 'x'}, meta={'openai/session': bad}))
    assert set(stamp) == {'call_id'}


def test_the_card_only_tools_never_mint_an_identity(api):
    with _app() as harness:
        meta = {'openai/session': SESSION}
        results = [
            _call(harness, 'start_verification_widget', {'claim': 'x'}, meta=meta),
            _call(harness, 'get_verification_widget', {'task_id': 't' * 32}, meta=meta),
            _call(harness, 'select_claims_widget', {'task_id': 't' * 32, 'claims': ['x']}, meta=meta),
        ]
    for result in results:
        assert set(_stamp(result)) == {'deliver'}


def test_other_tools_are_left_alone(api):
    with _app() as harness:
        result = _call(harness, 'check_usage', {}, meta={'openai/session': SESSION})
    assert mcp_card.CARD_RESULT_NAMESPACE not in (result.get('structuredContent') or {})


def test_an_error_result_that_mounts_the_card_is_stamped_too(monkeypatch, api):
    async def _down(authorization, **kwargs):
        return ApiResponse(status=503, data={'detail': 'down'})

    monkeypatch.setattr(client, 'assess', _down)
    with _app() as harness:
        result = _call(harness, 'assess_claim', {'claim': 'x'}, meta={'openai/session': SESSION})
    assert result['structuredContent']['status'] != 'ok'
    assert _stamp(result)['conversation'] == _hash(SESSION)


def test_a_missing_credential_is_stamped_too(api):
    with _app() as harness:
        wire = harness.wire(user_agent=CHATGPT, capabilities=DECLARES_APPS)
        result = wire.call(
            'tools/call',
            {'name': 'assess_claim', 'arguments': {'claim': 'x'}, '_meta': {'openai/session': SESSION}},
            era=MODERN,
            name='assess_claim',
        )
    assert result['structuredContent']['status'] == 'auth_required'
    assert _stamp(result)['conversation'] == _hash(SESSION)


def test_no_stamp_where_no_card_is_served(api):
    meta = {'openai/session': SESSION}
    with _app(card=False) as harness:
        off = _call(harness, 'assess_claim', {'claim': 'x'}, meta=meta)
    with _app(card=True) as harness:
        no_apps = _call(harness, 'assess_claim', {'claim': 'x'}, ua=CLAUDE, meta=meta, capabilities=DECLARES_NO_APPS)
    for result in (off, no_apps):
        assert mcp_card.CARD_RESULT_NAMESPACE not in result['structuredContent']


# ── the quick check's language ────────────────────────────────────────


def _rows(monkeypatch, *rows):
    async def _assess(authorization, **kwargs):
        return ApiResponse(status=200, data={'claims': list(rows)})

    monkeypatch.setattr(client, 'assess', _assess)


def test_a_quick_row_carries_its_language_code_for_a_card_client(monkeypatch, api):
    # The older and the newer row shapes both say `language`.
    _rows(
        monkeypatch,
        {'claim': 'Honig verdirbt nie.', 'language': 'de', 'verdict': 'True', 'confidence': 'high'},
        {'claim': 'x', 'language': 'FR', 'status': 'failed', 'failure': {'code': 'no_checkable_claim'}},
        {'claim': 'y', 'language': 'xx', 'verdict': 'True', 'confidence': 'high'},
        {'claim': 'z', 'verdict': 'True', 'confidence': 'high'},
    )
    with _app() as harness:
        rows = _call(harness, 'assess_claim', {'claim': 'x'})['structuredContent']['claims']
    assert [row.get('language') for row in rows] == ['de', 'fr', None, None]


def test_a_client_without_the_card_sees_no_language_key(monkeypatch, api):
    _rows(monkeypatch, {'claim': 'Honig verdirbt nie.', 'language': 'de', 'verdict': 'True', 'confidence': 'high'})
    with _app(card=False) as harness:
        rows = _call(harness, 'assess_claim', {'claim': 'x'})['structuredContent']['claims']
    assert 'language' not in rows[0]


def test_the_start_tool_forwards_the_language_to_verify(api):
    with _app() as harness:
        _call(harness, 'start_verification_widget', {'claim': 'Honig verdirbt nie.', 'language': 'de'})
        _call(harness, 'start_verification_widget', {'claim': 'Honey never spoils.'})
    assert [call['language'] for call in api['verify']] == ['de', '']


def test_the_start_tool_takes_only_a_served_language(api):
    with _app() as harness:
        wire = harness.wire(user_agent=CHATGPT, authorization=KEY, capabilities=DECLARES_APPS)
        response = wire.rpc(
            'tools/call',
            {'name': 'start_verification_widget', 'arguments': {'claim': 'x', 'language': 'ko'}},
            era=MODERN,
            name='start_verification_widget',
        )
        message = wire.reply(response)
    assert api['verify'] == []
    assert 'error' in message or message['result'].get('isError')


def test_the_language_argument_is_in_the_card_only_schema_and_not_the_model_tools(api):
    with _app() as harness:
        wire = harness.wire(user_agent=CHATGPT, authorization=KEY, capabilities=DECLARES_APPS)
        tools = {t['name']: t for t in wire.call('tools/list', era=MODERN, name='tools/list')['tools']}
    schema = tools['start_verification_widget']['inputSchema']['properties']['language']
    assert schema['default'] == ''
    assert schema['enum'] == ['', *config.SUPPORTED_LANGUAGES]
    assert tools['start_verification_widget']['_meta'] == mcp_card.CARD_ONLY_TOOL_META
