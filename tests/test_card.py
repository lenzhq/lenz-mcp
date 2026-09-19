"""The Lenz verdict card, server side.

Server side only; the card's own logic is tested with `node --test` in
src/lenz_mcp/card/. What these pin:

- the flag (MCP_CARD_ENABLED) is off by default, and off means every
  client's tools/list, resources/list and call results are what they are today;
- on, and only for a MEASURED client (`Claude-User` and the ChatGPT app),
  `assess_claim` points at the card (`_meta.ui.resourceUri`) and the card-only
  tools are listed with `_meta.ui.visibility: ["app"]`; nothing
  OpenAI-namespaced reaches Claude;
- the card is an MCP Apps resource (`text/html;profile=mcp-app`) that is served
  on read but never listed (a listed resource shows in Claude's "+" menu and
  pastes the raw HTML), with a hand-bumped URI whose bundle is pinned by hash,
  and every earlier URI still served;
- `start_verification_widget` submits and returns the task_id AT ONCE, never
  waiting (the card's own calls arrive as `Claude-User`, which `verify_claim`
  would hold for 130 s).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re

import pytest

from lenz_mcp import client, config, mcp_card, server
from lenz_mcp.client import ApiResponse
from lenz_mcp.mcp_card import CARD_DIR
from lenz_mcp.testing import assembled_app

CARD_TOOLS = {'start_verification_widget', 'get_verification_widget', 'select_claims_widget'}

# The per-client manifest is no longer something a test can reach by hand: 2.x
# removed `FastMCP._mcp_server.request_handlers`, and the tailoring moved into
# `lenz_mcp.middleware`, which runs over the SERIALIZED wire dict inside a real
# request. So every manifest assertion below drives the assembled app through
# `src/lenz_mcp/testing.py`, with the client's identity where the deployed server reads it
# — the request's own User-Agent header — rather than a patched
# `client.client_user_agent`. Wire dicts, so the keys are the protocol's
# (`_meta`, `inputSchema`), not the SDK models'.

KEY = 'Bearer lenz_x'


def _run(coro):
    return asyncio.run(coro)


@contextlib.contextmanager
def _app(*, card: bool):
    """The assembled server, built with the card flag `card`."""
    with assembled_app(MCP_OAUTH_ENABLED=False, MCP_CARD_ENABLED=card) as harness:
        yield harness


def _wire(harness, ua):
    wire = harness.wire(user_agent=ua, authorization=KEY)
    wire.initialize()
    return wire


def _tools(harness, ua):
    return {t['name']: t for t in _wire(harness, ua).call('tools/list', name='tools/list')['tools']}


def _dump(tools):
    return json.dumps([tools[name] for name in sorted(tools)], sort_keys=True)


def _resources(harness, ua):
    return {str(r['uri']) for r in _wire(harness, ua).call('resources/list', name='resources/list')['resources']}


# ── the flag ──────────────────────────────────────────────────────────


def test_the_card_is_off_by_default():
    assert config.CARD_ENABLED is False


@pytest.mark.parametrize('ua', ['Claude-User', 'claude-code/2.1.4', 'python-httpx/0.28', ''])
def test_flag_off_serves_todays_manifest(ua):
    """Off: the Claude manifest is the generic non-ChatGPT one, byte for byte,
    with no card tool, no ui meta and no card resource listed."""
    with _app(card=False) as harness:
        tools = _tools(harness, ua)
        assert _dump(tools) == _dump(_tools(harness, 'python-httpx/0.28'))
        assert not CARD_TOOLS & set(tools)
        for name, tool in tools.items():
            assert 'ui' not in (tool.get('_meta') or {}), name
        assert not {uri for uri in _resources(harness, ua) if uri.startswith('ui://lenz/')}


def test_the_shared_widget_poll_tool_is_published_as_before():
    # ChatGPT's widget lists get_verification_widget whatever the Claude flag
    # says, so its published description must not move with the card.
    tools = {t.name: t for t in _run(server.mcp.list_tools())}
    assert (
        tools['get_verification_widget'].description
        == 'Widget-only twin of `get_verification` (hidden from the model).'
    )
    assert tools['get_verification_widget'].input_schema['properties']['task_id']['description'] == (
        'task_id from verify_claim; polled by the Lenz widget in-card.'
    )


def test_flag_off_leaves_call_results_untouched(monkeypatch):
    """The Authorization rides the real request rather than a patched
    `server._authorization`: `assembled_app` re-imports `lenz_mcp.server`, so a
    patch on the pre-import module would not be the code the app runs.
    `lenz_mcp.client` is deliberately not re-imported, so the API stub holds."""

    async def _assess(*args, **kwargs):
        return ApiResponse(status=200, data={'claims': [{'claim': 'X', 'verdict': 'True', 'confidence': 'high'}]})

    monkeypatch.setattr(client, 'assess', _assess)
    with _app(card=False) as harness:
        result = _wire(harness, 'Claude-User').call(
            'tools/call', {'name': 'assess_claim', 'arguments': {'claim': 'x'}}, name='assess_claim'
        )
    assert result.get('_meta') is None


# ── flag on: who gets the card ────────────────────────────────────────


def test_claude_gets_the_card_on_assess_and_the_card_only_tools():
    with _app(card=True) as harness:
        tools = _tools(harness, 'Claude-User')
    # The quick check and a deep check the model ran itself.
    for name in ('assess_claim', 'verify_claim', 'get_verification'):
        assert tools[name]['_meta'] == {'ui': {'resourceUri': mcp_card.CARD_URI}}, name
    for name in CARD_TOOLS:
        assert tools[name]['_meta'] == {'ui': {'visibility': ['app']}}, name
    # Not the picker and not the tools whose answers are not a verdict.
    for name in ('select_claims', 'check_usage', 'ask_followup'):
        assert 'ui' not in (tools[name].get('_meta') or {}), name
    for name, tool in tools.items():
        assert not any(k.startswith('openai/') for k in (tool.get('_meta') or {})), name


# `openai-mcp` is NOT in this list any more: ChatGPT is a card host now
# (see test_chatgpt_gets_the_same_card_manifest_as_claude). Every client we have
# not measured the card in still gets today's manifest.
@pytest.mark.parametrize('ua', ['claude-code/2.1.4', 'python-httpx/0.28', 'openai-mcp-lookalike/1.0', ''])
def test_other_clients_are_unchanged_with_the_card_on(ua):
    with _app(card=True) as harness:
        with_card = _dump(_tools(harness, ua))
    with _app(card=False) as harness:
        without_card = _dump(_tools(harness, ua))
    assert with_card == without_card


def test_openais_api_connector_is_not_a_card_host():
    # Measured 2026-09-18: OpenAI's Responses-API MCP connector sends the SAME
    # leading token as the ChatGPT app (`openai-mcp`), renders no card, cuts a
    # tool call at 59.8 s against the app's 119.8 s, and — unlike the app —
    # lists app-only tools to its model. Matching on the token would hand that
    # developer's model `start_verification_widget` and `select_claims_widget`,
    # which START PAID CHECKS.
    with _app(card=True) as harness:
        connector = _tools(harness, 'openai-mcp/1.0.0 (Responses API)')
        unknown = _tools(harness, 'openai-mcp/1.0.0 (Something New)')
    for name in ['start_verification_widget', 'get_verification_widget', 'select_claims_widget']:
        assert name not in connector, f'{name} must not be listed to the API connector'
    # And its manifest is today's: no card meta anywhere.
    for name, tool in connector.items():
        assert 'ui' not in (tool.get('_meta') or {}), name
    # An unrecognised suffix is treated the same way, never as the app.
    assert 'start_verification_widget' not in unknown


def test_the_delivery_hint_follows_the_app_not_the_token(monkeypatch, authed):
    async def _verify(authorization, **kwargs):
        return ApiResponse(status=202, data={'task_id': 't' * 32})

    monkeypatch.setattr(client, 'verify', _verify)
    _no_status(monkeypatch)
    for ua, deliver in [
        ('openai-mcp/1.0.0', 'message'),
        ('openai-mcp/1.0.0 (Codex)', 'message'),
        # The API connector renders no card, so it is never told to post one.
        ('openai-mcp/1.0.0 (Responses API)', 'context'),
        ('openai-mcp/1.0.0 (Something New)', 'context'),
    ]:
        monkeypatch.setattr(client, 'client_user_agent', lambda ua=ua: ua)
        out = _run(server.start_verification_widget('The claim.', _Ctx()))
        assert out[mcp_card.CARD_RESULT_NAMESPACE] == {'deliver': deliver}, ua


def test_chatgpt_gets_the_same_card_manifest_as_claude():
    # One card, two hosts. ChatGPT gets the standard card and the same tools
    # as Claude, not a separate host-specific widget.
    with _app(card=True) as harness:
        tools = _tools(harness, 'openai-mcp/1.0.0')
        claude = _dump(_tools(harness, 'Claude-User/1.0'))
    assert _dump(tools) == claude
    for name in ['assess_claim', 'verify_claim', 'get_verification']:
        assert tools[name]['_meta']['ui']['resourceUri'] == mcp_card.CARD_URI
    for name in ['start_verification_widget', 'get_verification_widget', 'select_claims_widget']:
        assert tools[name]['_meta'] == mcp_card.CARD_ONLY_TOOL_META


def test_no_manifest_carries_an_openai_key_any_more():
    # The skybridge keys (openai/outputTemplate, openai/widgetAccessible) are
    # deleted, not merely hidden: nothing emits them for any client.
    with _app(card=True) as harness:
        for ua in ['openai-mcp/1.0.0', 'Claude-User/1.0', 'claude-code/2.1.4', 'node', '']:
            for name, tool in _tools(harness, ua).items():
                keys = list((tool.get('_meta') or {}).keys())
                assert not [k for k in keys if k.startswith('openai/')], (ua, name, keys)


# ── the resource ──────────────────────────────────────────────────────


def test_the_card_resource_is_served_but_never_listed():
    with _app(card=True) as harness:
        for ua in ('Claude-User', 'openai-mcp/1.0.0', ''):
            assert mcp_card.CARD_URI not in _resources(harness, ua)
    contents = list(_run(server.mcp.read_resource(mcp_card.CARD_URI)))
    assert contents[0].mime_type == 'text/html;profile=mcp-app'
    html = contents[0].content
    assert 'Content-Security-Policy' in html
    assert "default-src 'none'" in html
    assert (contents[0].meta or {}).get('ui', {}).get('prefersBorder') is False


def test_every_card_uri_is_pinned_to_its_bundle():
    """A hand-bumped URI is the card's published identity: Claude caches by it.
    Each registered URI serves one committed bundle whose hash is recorded, so a
    rebuilt bundle without a new URI fails here, and every earlier URI keeps
    serving the bundle old chats were rendered with."""
    versions = mcp_card.card_versions()
    assert mcp_card.CARD_URI == list(versions)[-1]
    for uri, entry in versions.items():
        path = mcp_card.DIST_DIR / entry['file']
        assert path.is_file(), uri
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry['sha256'], (
            f'{entry["file"]} changed without a new card URI: bump CARD_URI by hand and record the new hash'
        )
        served = list(_run(server.mcp.read_resource(uri)))[0].content
        assert served == path.read_text(encoding='utf-8')


def test_the_card_uri_is_a_literal():
    import inspect

    source = inspect.getsource(mcp_card)
    assert "CARD_URI = 'ui://lenz/" in source
    assert 'hashlib' not in source.split('def card_versions', 1)[0]


# ── start_verification_widget ─────────────────────────────────────────


class _Ctx:
    request_context = None


def _shape(result):
    """A card-tool result without its delivery hint.

    Every card-only tool stamps `_card.deliver` (mcp_card.with_delivery), and
    the hint has its own tests below; these assertions are about the shape the
    card reads."""
    assert mcp_card.CARD_RESULT_NAMESPACE in result, 'a card tool must always say how to deliver'
    return {k: v for k, v in result.items() if k != mcp_card.CARD_RESULT_NAMESPACE}


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setattr(server, '_authorization', lambda ctx: 'Bearer lenz_x')
    monkeypatch.setattr(config, 'CARD_ENABLED', True)


def test_with_the_card_off_the_start_tool_spends_nothing(monkeypatch):
    # Hidden from every manifest while the flag is off, and refused if called
    # anyway: a paid check starts only from a card that is switched on.
    monkeypatch.setattr(server, '_authorization', lambda ctx: 'Bearer lenz_x')
    monkeypatch.setattr(config, 'CARD_ENABLED', False)

    async def _verify(authorization, **kwargs):
        raise AssertionError('no submission while the card is off')

    monkeypatch.setattr(client, 'verify', _verify)
    assert _run(server.start_verification_widget('The claim.', _Ctx()))['status'] == 'invalid_request'


def _no_status(monkeypatch):
    async def _boom(*args, **kwargs):
        raise AssertionError('start_verification_widget must not wait for the run')

    monkeypatch.setattr(client, 'verify_status', _boom)


def test_start_returns_the_task_id_at_once(monkeypatch, authed):
    calls = []

    async def _verify(authorization, **kwargs):
        calls.append(kwargs)
        return ApiResponse(status=202, data={'task_id': 't' * 32, 'status': 'queued'})

    monkeypatch.setattr(client, 'verify', _verify)
    _no_status(monkeypatch)
    out = _shape(_run(server.start_verification_widget('The claim.', _Ctx())))
    assert out == {'status': 'submitted', 'task_id': 't' * 32}
    assert calls == [{'text': 'The claim.', 'language': '', 'depth': 'standard'}]


def test_the_server_alone_picks_the_depth_of_a_card_check(monkeypatch, authed):
    # The card sends a claim and nothing else; its depth is one server constant,
    # so no card (or anything posing as one) can pick a different price.
    import inspect

    assert 'depth' not in inspect.signature(server.start_verification_widget).parameters
    monkeypatch.setattr(config, 'CARD_VERIFY_DEPTH', 'low')
    calls = _record_verify(monkeypatch)
    _no_status(monkeypatch)
    _run(server.start_verification_widget('The claim.', _Ctx()))
    assert calls[0]['depth'] == 'low'


def test_start_joins_a_check_already_running(monkeypatch, authed):
    async def _verify(authorization, **kwargs):
        return ApiResponse(status=409, data={'task_id': 'r' * 32, 'detail': 'in progress'})

    monkeypatch.setattr(client, 'verify', _verify)
    _no_status(monkeypatch)
    assert _shape(_run(server.start_verification_widget('The claim.', _Ctx()))) == {
        'status': 'submitted',
        'task_id': 'r' * 32,
    }


def test_start_maps_an_api_error(monkeypatch, authed):
    async def _verify(authorization, **kwargs):
        return ApiResponse(status=402, data={'cost': 10, 'credits_remaining': 0})

    monkeypatch.setattr(client, 'verify', _verify)
    out = _shape(_run(server.start_verification_widget('The claim.', _Ctx())))
    # No numbers and no link reach the card.
    assert out == {'status': 'quota_exhausted', 'balance_empty': True}


def test_start_says_when_the_balance_is_short_but_not_empty(monkeypatch, authed):
    async def _verify(authorization, **kwargs):
        return ApiResponse(status=402, data={'cost': 10, 'credits_remaining': 3})

    monkeypatch.setattr(client, 'verify', _verify)
    assert _shape(_run(server.start_verification_widget('The claim.', _Ctx()))) == {
        'status': 'quota_exhausted',
        'balance_empty': False,
    }


def test_the_card_poll_tool_recovers_a_finished_check_by_verification_id(monkeypatch, authed):
    seen = []

    async def _detail(authorization, *, verification_id):
        seen.append(verification_id)
        return ApiResponse(status=200, data={'verification_id': 'abcd1234', 'claim': 'X', 'verdict': 'False'})

    async def _no_status(*args, **kwargs):
        raise AssertionError('a verification_id is fetched, not polled')

    monkeypatch.setattr(client, 'verification_detail', _detail)
    monkeypatch.setattr(client, 'verify_status', _no_status)
    out = _run(server.get_verification_widget('abcd1234', _Ctx()))
    assert seen == ['abcd1234']
    assert out['status'] == 'completed' and out['verification_id'] == 'abcd1234'


def test_start_refuses_an_empty_claim(monkeypatch, authed):
    async def _verify(authorization, **kwargs):
        raise AssertionError('no submission for an empty claim')

    monkeypatch.setattr(client, 'verify', _verify)
    assert _run(server.start_verification_widget('   ', _Ctx()))['status'] == 'invalid_request'


# ── "Try again": retry_of ─────────────────────────────────────────────


def _status(monkeypatch, body, status=200, seen=None):
    async def _verify_status(authorization, *, task_id):
        if seen is not None:
            seen.append(task_id)
        return ApiResponse(status=status, data=body)

    monkeypatch.setattr(client, 'verify_status', _verify_status)


def _record_verify(monkeypatch):
    calls = []

    async def _verify(authorization, **kwargs):
        calls.append(kwargs)
        return ApiResponse(status=202, data={'task_id': 'n' * 32, 'status': 'queued'})

    monkeypatch.setattr(client, 'verify', _verify)
    return calls


def test_a_retry_of_a_failed_retryable_run_gets_its_own_key(monkeypatch, authed):
    seen = []
    _status(monkeypatch, {'status': 'failed', 'retryable': True, 'failure_class': 'upstream_unavailable'}, seen=seen)
    calls = _record_verify(monkeypatch)
    out = _shape(_run(server.start_verification_widget('The claim.', _Ctx(), retry_of='f' * 32)))
    assert out == {'status': 'submitted', 'task_id': 'n' * 32}
    assert seen == ['f' * 32]
    assert calls == [{'text': 'The claim.', 'language': '', 'depth': 'standard', 'retry_of': 'f' * 32}]


@pytest.mark.parametrize(
    ('body', 'status'),
    [
        ({'status': 'failed', 'retryable': False, 'failure_class': 'invalid_input'}, 200),
        ({'status': 'completed', 'result': {}}, 200),
        ({'status': 'processing', 'progress': {}}, 200),
        ({'detail': 'Task not found.'}, 404),  # another user's task, or none
        ({'status': 'failed'}, 200),  # no retryable flag at all
    ],
)
def test_a_retry_is_refused_unless_the_run_failed_and_can_be_retried(monkeypatch, authed, body, status):
    _status(monkeypatch, body, status=status)
    calls = _record_verify(monkeypatch)
    out = _run(server.start_verification_widget('The claim.', _Ctx(), retry_of='f' * 32))
    assert out['status'] == 'invalid_request'
    assert calls == []


@pytest.mark.parametrize('retry_of', ['abcd1234', '../x', 'a b'])
def test_a_retry_of_something_that_is_not_a_task_id_is_refused(monkeypatch, authed, retry_of):
    _status(monkeypatch, {'status': 'failed', 'retryable': True})
    calls = _record_verify(monkeypatch)
    assert (
        _run(server.start_verification_widget('The claim.', _Ctx(), retry_of=retry_of))['status'] == 'invalid_request'
    )
    assert calls == []


def test_the_retry_key_is_deterministic_and_differs_from_the_first_run():
    from lenz_mcp.client import _idem_key

    first = _idem_key('verify', 'The claim.', '')
    retry = _idem_key('verify', 'The claim.', '', 'retry_of', 'f' * 32)
    assert first != retry
    assert retry == _idem_key('verify', 'The claim.', '', 'retry_of', 'f' * 32)


def test_client_verify_puts_retry_of_in_the_key_not_the_body(monkeypatch):
    sent = {}

    async def _request(method, path, authorization, *, json=None, idempotency_key=None, **kwargs):
        sent.update(json=json, key=idempotency_key)
        return ApiResponse(status=202, data={'task_id': 'x'})

    monkeypatch.setattr(client, '_request', _request)
    _run(client.verify('Bearer k', text='The claim.', language='', retry_of='f' * 32))
    assert sent['json'] == {'claim': 'The claim.', 'language': '', 'depth': 'standard'}
    assert sent['key'] == client._idem_key('verify', 'The claim.', '', 'retry_of', 'f' * 32)
    _run(client.verify('Bearer k', text='The claim.', language=''))
    assert sent['key'] == client._idem_key('verify', 'The claim.', '')


def test_the_card_poll_tool_names_a_gone_task(monkeypatch, authed):
    """A reopened card must tell "this check is gone" apart from a blip; the
    model-facing get_verification keeps its generic error."""

    async def _status(authorization, *, task_id):
        return ApiResponse(status=404, data={'detail': 'Task not found.'})

    monkeypatch.setattr(client, 'verify_status', _status)
    assert _run(server.get_verification_widget('t' * 32, _Ctx()))['status'] == 'not_found'
    monkeypatch.setattr(server, '_verify_wait_seconds', lambda: 0)
    assert _run(server.get_verification('t' * 32, _Ctx()))['status'] == 'error'


def test_a_submitted_verify_names_the_claim_it_is_checking(monkeypatch, authed):
    # The status API does not echo the claim, so a running card could not name
    # what it is checking. verify_claim knows it; it rides on the result.
    async def _verify(authorization, **kwargs):
        return ApiResponse(status=202, data={'task_id': 't' * 32, 'status': 'queued'})

    async def _status(authorization, *, task_id):
        return ApiResponse(
            status=200,
            data={
                'status': 'processing',
                'progress': {'step': 'research', 'index': 2, 'total': 5, 'elapsed_seconds': 20},
            },
        )

    monkeypatch.setattr(client, 'verify', _verify)
    monkeypatch.setattr(client, 'verify_status', _status)
    monkeypatch.setattr(server.config, 'VERIFY_POLL_INTERVAL', 0)
    monkeypatch.setattr(server, '_verify_wait_seconds', lambda: 0)
    out = _run(server.verify_claim('The claim to check.', _Ctx()))
    assert out['status'] == 'submitted'
    assert out['claim'] == 'The claim to check.'
    assert out['step'] == 'research'


# ── select_claims_widget (the picker's submit) ────────────────


def test_with_the_card_off_the_picker_tool_spends_nothing(monkeypatch):
    monkeypatch.setattr(server, '_authorization', lambda ctx: 'Bearer lenz_x')
    monkeypatch.setattr(config, 'CARD_ENABLED', False)

    async def _select(authorization, **kwargs):
        raise AssertionError('no selection while the card is off')

    monkeypatch.setattr(client, 'select', _select)
    out = _run(server.select_claims_widget('t' * 32, ['A claim.'], _Ctx()))
    assert out['status'] == 'invalid_request'


def test_the_picker_starts_every_pick_and_returns_at_once(monkeypatch, authed):
    calls = []

    async def _select(authorization, **kwargs):
        calls.append(kwargs)
        return ApiResponse(
            status=202,
            data={
                'batch_id': 'b1',
                'items': [
                    {'task_id': 'a' * 32, 'claim_text': 'One.'},
                    {'task_id': 'b' * 32, 'claim_text': 'Two.'},
                ],
            },
        )

    async def _boom(*args, **kwargs):
        raise AssertionError('select_claims_widget must not wait for any verdict')

    monkeypatch.setattr(client, 'select', _select)
    monkeypatch.setattr(client, 'verify_status', _boom)
    monkeypatch.setattr(server, '_await_verification', _boom)

    out = _shape(_run(server.select_claims_widget('t' * 32, ['One.', 'Two.'], _Ctx())))
    assert out == {
        'status': 'submitted',
        'batch_id': 'b1',
        'claims': [
            {'task_id': 'a' * 32, 'claim': 'One.'},
            {'task_id': 'b' * 32, 'claim': 'Two.'},
        ],
    }
    # ONE call, with the texts the card sent. Unlike select_claims, a single
    # pick is not awaited either: the card polls it like any other.
    assert calls == [{'task_id': 't' * 32, 'texts': ['One.', 'Two.']}]
    assert _run(server.select_claims_widget('t' * 32, ['One.'], _Ctx()))['status'] == 'submitted'


def test_the_picker_says_so_when_only_some_picks_started(monkeypatch, authed):
    async def _select(authorization, **kwargs):
        return ApiResponse(status=202, data={'partial': True, 'items': [{'task_id': 'a' * 32, 'claim_text': 'One.'}]})

    monkeypatch.setattr(client, 'select', _select)
    out = _run(server.select_claims_widget('t' * 32, ['One.', 'Two.'], _Ctx()))
    assert out['status'] == 'partial'
    assert out['claims'] == [{'task_id': 'a' * 32, 'claim': 'One.'}]


def test_the_picker_refuses_an_id_it_could_not_have_been_given(monkeypatch, authed):
    async def _select(authorization, **kwargs):
        raise AssertionError('nothing is sent for an unsendable id')

    monkeypatch.setattr(client, 'select', _select)
    for bad in ['', '   ', 'has spaces', 'x' * 129, 'semi;colon']:
        assert _run(server.select_claims_widget(bad, ['One.'], _Ctx()))['status'] == 'invalid_request'


def test_the_picker_refuses_an_empty_selection(monkeypatch, authed):
    async def _select(authorization, **kwargs):
        raise AssertionError('nothing is sent for an empty selection')

    monkeypatch.setattr(client, 'select', _select)
    for bad in [[], ['', '  '], 'not a list', None]:
        assert _run(server.select_claims_widget('t' * 32, bad, _Ctx()))['status'] == 'invalid_request'


def test_a_replayed_selection_starts_nothing_new(monkeypatch, authed):
    # The card may call twice (a lost answer, a re-mount, a double tap). The
    # same parent and the same texts derive the same idempotency key, so the
    # API replays its first response and no second check is paid for.
    import lenz_mcp.client as real_client

    keys = []

    async def _request(method, path, authorization, **kwargs):
        keys.append(kwargs.get('idempotency_key'))
        return ApiResponse(status=202, data={'items': [{'task_id': 'a' * 32, 'claim_text': 'One.'}]})

    monkeypatch.setattr(real_client, '_request', _request)
    first = _run(server.select_claims_widget('t' * 32, ['One.'], _Ctx()))
    second = _run(server.select_claims_widget('t' * 32, ['One.'], _Ctx()))
    assert first == second
    assert len(keys) == 2 and keys[0] == keys[1] and keys[0]
    # A different selection of the same parent is a different key: it must not
    # replay the first pick's answer.
    _run(server.select_claims_widget('t' * 32, ['Two.'], _Ctx()))
    assert keys[2] != keys[0]


def test_a_parent_already_resolved_is_not_a_retry_loop(monkeypatch, authed):
    async def _select(authorization, **kwargs):
        return ApiResponse(status=409, data={'code': 'no_selection_pending', 'detail': 'Already resolved.'})

    monkeypatch.setattr(client, 'select', _select)
    out = _run(server.select_claims_widget('t' * 32, ['One.'], _Ctx()))
    assert out['status'] == 'already_resolved'


def test_the_picker_tool_says_it_starts_paid_checks(monkeypatch):
    # Model-invisible, but a human reviews it: the docstring states plainly what
    # pressing the card's button spends.
    doc = server.select_claims_widget.__doc__ or ''
    assert 'PAID' in doc
    assert 'never by the assistant' in doc


def test_the_picker_cap_is_enforced_where_the_money_is(monkeypatch, authed):
    # The card's disabled checkboxes are a courtesy to the reader. This call can
    # arrive with any list, and the API downstream allows twenty.
    async def _select(authorization, **kwargs):
        raise AssertionError('nothing is sent past the cap')

    monkeypatch.setattr(client, 'select', _select)
    too_many = [f'Claim {i}.' for i in range(config.CARD_PICKER_MAX_CLAIMS + 1)]
    out = _run(server.select_claims_widget('t' * 32, too_many, _Ctx()))
    assert out['status'] == 'invalid_request'
    assert str(config.CARD_PICKER_MAX_CLAIMS) in out['message']


def test_the_card_and_the_server_agree_on_the_cap():
    # Two languages, one number: the card cannot read config.py, so the JS
    # constant is pinned to it here rather than left to drift.
    source = (CARD_DIR / 'src' / 'app.jsx').read_text()
    match = re.search(r'const PICKER_MAX_SELECTED = (\d+);', source)
    assert match, 'PICKER_MAX_SELECTED is gone from the card'
    assert int(match.group(1)) == config.CARD_PICKER_MAX_CLAIMS


def test_the_picker_cap_allows_exactly_its_number(monkeypatch, authed):
    seen = []

    async def _select(authorization, **kwargs):
        seen.append(len(kwargs['texts']))
        return ApiResponse(
            status=202,
            data={'items': [{'task_id': f'{i}' * 8, 'claim_text': f'Claim {i}.'} for i in range(len(kwargs['texts']))]},
        )

    monkeypatch.setattr(client, 'select', _select)
    exactly = [f'Claim {i}.' for i in range(config.CARD_PICKER_MAX_CLAIMS)]
    assert _run(server.select_claims_widget('t' * 32, exactly, _Ctx()))['status'] == 'submitted'
    assert seen == [config.CARD_PICKER_MAX_CLAIMS]


# ── How the card is told to deliver ───────────────────────────────────


def _as_client(monkeypatch, user_agent):
    monkeypatch.setattr(client, 'client_user_agent', lambda: user_agent)


@pytest.mark.parametrize(
    ('user_agent', 'deliver'),
    [
        ('Claude-User/1.0', 'context'),
        ('Claude-User', 'context'),
        # ChatGPT accepts update-model-context and never delivers it, so its card
        # has to say it in the chat instead.
        ('openai-mcp/1.0.0', 'message'),
        ('openai-mcp/1.0.0 (Codex)', 'message'),
        # An unmeasured client gets the quiet mechanism: a host nobody has
        # measured is never made to post into the user's own turn.
        ('claude-code/2.1.4', 'context'),
        ('node', 'context'),
        ('', 'context'),
    ],
)
def test_the_server_tells_the_card_how_to_deliver(monkeypatch, authed, user_agent, deliver):
    _as_client(monkeypatch, user_agent)

    async def _verify(authorization, **kwargs):
        return ApiResponse(status=202, data={'task_id': 't' * 32})

    monkeypatch.setattr(client, 'verify', _verify)
    _no_status(monkeypatch)
    out = _run(server.start_verification_widget('The claim.', _Ctx()))
    assert out[mcp_card.CARD_RESULT_NAMESPACE] == {'deliver': deliver}


def test_every_card_tool_says_how_to_deliver_on_every_path(monkeypatch, authed):
    # Including the error paths: the card reads the hint from the most recent
    # card-tool response, and a card whose first call fails still has to know.
    _as_client(monkeypatch, 'openai-mcp/1.0.0')

    async def _boom(*args, **kwargs):
        return ApiResponse(status=500, data={'detail': 'nope'})

    monkeypatch.setattr(client, 'verify', _boom)
    monkeypatch.setattr(client, 'verify_status', _boom)
    monkeypatch.setattr(client, 'select', _boom)
    monkeypatch.setattr(client, 'verification_detail', _boom)

    results = [
        _run(server.start_verification_widget('The claim.', _Ctx())),
        _run(server.start_verification_widget('', _Ctx())),  # invalid_request
        _run(server.get_verification_widget('t' * 32, _Ctx())),
        _run(server.get_verification_widget('bad id', _Ctx())),  # invalid_request
        _run(server.get_verification_widget('abcd1234', _Ctx())),  # verification-id path
        _run(server.select_claims_widget('t' * 32, ['One.'], _Ctx())),
        _run(server.select_claims_widget('t' * 32, [], _Ctx())),  # invalid_request
    ]
    for out in results:
        assert out[mcp_card.CARD_RESULT_NAMESPACE] == {'deliver': 'message'}, out


def test_the_delivery_hint_is_never_shown_to_a_model():
    # The three tools that carry it are app-only, and the model-facing twins
    # (get_verification, verify_claim, select_claims) must not grow it.
    assert mcp_card.CARD_RESULT_NAMESPACE.startswith('_')
    assert set(mcp_card.CARD_ONLY_TOOL_NAMES) == {
        'start_verification_widget',
        'get_verification_widget',
        'select_claims_widget',
    }


def test_the_card_gate_and_the_wait_read_the_client_the_same_way(monkeypatch):
    # One token, three readers: a client matched one way here and another way
    # there is how a host ends up on the wrong wait with the right card.
    _as_client(monkeypatch, 'openai-mcp/1.0.0 (Codex)')
    assert client.client_user_agent_token() == 'openai-mcp'
    assert mcp_card.request_is_claude() is False
    assert mcp_card.card_delivery() == 'message'
    _as_client(monkeypatch, 'Claude-User/1.0')
    assert client.client_user_agent_token() == 'Claude-User'
    assert mcp_card.request_is_claude() is True
    assert mcp_card.card_delivery() == 'context'


def test_the_card_declares_a_csp_and_it_allows_nothing():
    # SEP-1865's McpUiResourceCsp defines exactly these four keys (ext-apps
    # specification 2026-01-26). All four are declared and all four are empty:
    # the card loads nothing external and talks only to the host, and an
    # explicit declaration cannot be read as an omission by a host that shows
    # the user whether a policy is in force.
    csp = mcp_card.CARD_RESOURCE_META['ui']['csp']
    assert set(csp) == {'connectDomains', 'resourceDomains', 'frameDomains', 'baseUriDomains'}
    assert all(value == [] for value in csp.values()), csp
    # No browser capability is requested, so the key is absent, not empty.
    assert 'permissions' not in mcp_card.CARD_RESOURCE_META['ui']


def test_the_built_card_loads_nothing_external():
    # The policy above is only true if the bundle really needs nothing: one
    # font, image, source map or analytics URL would be blocked at runtime by
    # the very policy we declare, and the card would break in the host that
    # enforces it rather than in review.
    # Every published bundle, by name from versions.json — never a hardcoded
    # file, which would silently stop checking the card after a URI bump.
    published = mcp_card.card_versions()
    assert published, 'versions.json names no bundle'
    html = '\n'.join((mcp_card.DIST_DIR / entry['file']).read_text() for entry in published.values())
    for pattern in [
        r'src=[\'"]https?://',
        r'href=[\'"]https?://',
        r'@import',
        r'sourceMappingURL',
        r'fonts\.googleapis',
        r'\bfetch\(\s*[\'"]https?://',
    ]:
        assert not re.search(pattern, html), f'the card must load nothing external: {pattern}'


def test_a_read_of_the_deleted_widget_uri_is_a_clean_not_found(caplog):
    # A ChatGPT connection made before the skybridge deletion holds a manifest
    # naming ui://widget/verdict. That read must answer a JSON-RPC error, never
    # a 500, and must name the URI it was asked for — the only signal that a
    # published manifest has drifted from what this server serves.
    with _app(card=True) as harness:
        wire = _wire(harness, 'Claude-User')
        with caplog.at_level('WARNING'):
            response = wire.rpc('resources/read', {'uri': 'ui://widget/verdict'}, name='ui://widget/verdict')

    # Not an AttributeError/NameError from our own logging path: the error the
    # SDK raises for a URI nobody registered, rendered as a JSON-RPC error.
    assert 'unknown resource' in wire.error(response)['message'].lower()
    assert any('mcp_resource_read_failed' in r.getMessage() for r in caplog.records)
    assert any('ui://widget/verdict' in r.getMessage() for r in caplog.records)


def test_the_card_uri_still_reads():
    # The other half of the same guard: what we DO serve is served.
    with _app(card=True) as harness:
        read = _wire(harness, 'Claude-User').call('resources/read', {'uri': mcp_card.CARD_URI}, name=mcp_card.CARD_URI)
    body = read['contents'][0]['text']
    assert body.lstrip().startswith('<!doctype html'), body[:60]


def test_every_card_only_tool_tells_a_model_to_leave_it_alone(monkeypatch):
    """Belt and braces for a client that ignores `ui.visibility: ['app']`.

    OpenAI's API connector does ignore it (measured 2026-09-18), which is why it
    is not a card host and the tools are stripped from its list server-side.
    Whether the ChatGPT APP hides them is still unmeasured (see the probe
    README's open table), so the descriptions have to stand on their own: two of
    these three START PAID CHECKS.
    """
    with _app(card=True) as harness:
        tools = _tools(harness, 'Claude-User/1.0')
    for name in ['start_verification_widget', 'select_claims_widget']:
        text = tools[name].get('description') or ''
        assert 'never by the assistant' in text, name
        assert 'card' in text.lower(), name
    # And calling one as a model is no worse than verify_claim: the flag gates
    # it, the key replays a repeat, and the API owns credits and ownership.
    monkeypatch.setattr(config, 'CARD_ENABLED', False)
    monkeypatch.setattr(server, '_authorization', lambda ctx: 'Bearer lenz_x')
    for call in [
        server.start_verification_widget('The claim.', _Ctx()),
        server.select_claims_widget('t' * 32, ['The claim.'], _Ctx()),
    ]:
        assert _run(call)['status'] == 'invalid_request'


def test_an_unreadable_client_is_privileged_nowhere(monkeypatch):
    """'' must be an unknown client at every gate, not a privileged one.

    The identity read goes through an SDK internal that mcp 2.x removes, and it
    is wrapped: a failure returns ''. Moving to mcp 2.x makes that read
    loud, but loud is not the same as safe — what makes it safe is that '' wins
    nothing here. One seam, three gates, asserted together.
    """
    monkeypatch.setattr(client, 'client_user_agent', lambda: '')
    assert client.client_identity() == ''
    assert mcp_card.request_is_claude() is False
    assert mcp_card.request_is_chatgpt_app() is False
    assert mcp_card.card_delivery() == mcp_card.DELIVER_BY_CONTEXT
    monkeypatch.setattr(config, 'CARD_ENABLED', True)
    assert mcp_card.card_active() is False, 'no card for a client we cannot identify'
    assert '' not in config.VERIFY_WAIT_SECONDS_BY_USER_AGENT


def test_a_client_we_cannot_parse_is_privileged_nowhere(monkeypatch):
    # The same rule for a User-Agent that parses to nothing we recognise: it is
    # returned verbatim precisely so it matches no row at any gate.
    for ua in [
        'openai-mcp/1.0.0 (Responses API) proxy/1.0',
        'openai-mcp/1.0.0 (a) (b)',
        'Claude-User/1.0 (Something)',
        'garbage (',
    ]:
        monkeypatch.setattr(client, 'client_user_agent', lambda ua=ua: ua)
        assert mcp_card.request_is_claude() is False, ua
        assert mcp_card.request_is_chatgpt_app() is False, ua
        assert mcp_card.card_delivery() == mcp_card.DELIVER_BY_CONTEXT, ua
        assert client.client_identity() not in config.VERIFY_WAIT_SECONDS_BY_USER_AGENT, ua
