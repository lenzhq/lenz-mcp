"""The connector sends `language: "auto"` when the model leaves `language` unset.

Behind its own switch (`MCP_LANGUAGE_AUTO_ENABLED`, off by default): an API
that does not accept `auto` answers 422 for it, so the connector only sends it
once the switch is on. Off, every body and every key is what it always was.
A code the model sets, because the user asked for one, is sent unchanged and
always wins. The tool descriptions do not depend on the switch.
"""

from __future__ import annotations

import asyncio
import json
import types

import httpx
import pytest

from lenz_mcp import client, config, server


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


@pytest.fixture
def api(monkeypatch):
    """The shared HTTP client, routed to a recorder that answers each endpoint plausibly."""
    sent: list[httpx.Request] = []

    def _handler(request):
        sent.append(request)
        path = request.url.path
        if path.endswith('/assess'):
            return httpx.Response(200, json={'claims': []})
        if path.endswith('/verify'):
            return httpx.Response(202, json={'task_id': 't' * 32})
        if '/verify/status/' in path:
            return httpx.Response(200, json={'status': 'processing'})
        if '/ask/' in path:
            return httpx.Response(200, json={'role': 'expert', 'content': 'An answer.'})
        return httpx.Response(404, json={})

    monkeypatch.setattr(client, '_http_client', httpx.AsyncClient(transport=httpx.MockTransport(_handler)))
    monkeypatch.setattr(server, '_sleep', _no_sleep)
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS', 0.0)
    monkeypatch.setattr(config, 'VERIFY_WAIT_SECONDS_BY_IDENTITY', {})
    return sent


async def _no_sleep(_seconds):
    return None


def _bodies(sent, suffix):
    return [json.loads(r.content) for r in sent if r.url.path.endswith(suffix)]


def _keys(sent, suffix):
    return [r.headers['idempotency-key'] for r in sent if r.url.path.endswith(suffix)]


def test_the_switch_is_off_by_default_and_named_like_the_others():
    assert config.LANGUAGE_AUTO_ENABLED is False


# ── the client ───────────────────────────────────────────────────────


@pytest.mark.parametrize('on', [True, False])
def test_a_code_the_caller_sets_is_sent_unchanged(monkeypatch, api, on):
    monkeypatch.setattr(config, 'LANGUAGE_AUTO_ENABLED', on)
    _run(client.assess('Bearer k', text='hi', language='en'))
    _run(client.verify('Bearer k', text='hi', language='de'))
    _run(client.ask('Bearer k', verification_id='abcd1234', message='why?', language='fr'))
    assert _bodies(api, '/assess')[0]['language'] == 'en'
    assert _bodies(api, '/verify')[0]['language'] == 'de'
    assert _bodies(api, 'abcd1234')[0]['language'] == 'fr'


def test_unset_becomes_auto_on_assess_verify_and_ask_when_the_switch_is_on(monkeypatch, api):
    monkeypatch.setattr(config, 'LANGUAGE_AUTO_ENABLED', True)
    _run(client.assess('Bearer k', text='hi', language=''))
    _run(client.assess('Bearer k', claims=['a', 'b'], language=''))
    _run(client.verify('Bearer k', text='hi', language=''))
    _run(client.ask('Bearer k', verification_id='abcd1234', message='why?'))
    assert [b['language'] for b in _bodies(api, '/assess')] == ['auto', 'auto']
    assert _bodies(api, '/verify')[0]['language'] == 'auto'
    assert _bodies(api, 'abcd1234')[0]['language'] == 'auto'


def test_the_bodies_are_what_they_always_were_when_the_switch_is_off(monkeypatch, api):
    monkeypatch.setattr(config, 'LANGUAGE_AUTO_ENABLED', False)
    _run(client.assess('Bearer k', text='hi', language=''))
    _run(client.verify('Bearer k', text='hi', language=''))
    _run(client.ask('Bearer k', verification_id='abcd1234', message='why?'))
    assert _bodies(api, '/assess') == [{'claim': 'hi', 'language': ''}]
    assert _bodies(api, '/verify') == [{'claim': 'hi', 'language': '', 'depth': 'standard'}]
    assert _bodies(api, 'abcd1234') == [{'message': 'why?', 'language': ''}]
    assert _keys(api, '/assess')[0] == client._idem_key('assess', 'hi', '')
    assert _keys(api, '/verify')[0] == client._idem_key('verify', 'hi', '')


def test_auto_is_part_of_the_key_and_differs_from_unset_and_from_a_code(monkeypatch, api):
    monkeypatch.setattr(config, 'LANGUAGE_AUTO_ENABLED', True)
    _run(client.assess('Bearer k', text='hi', language=''))
    _run(client.assess('Bearer k', text='hi', language=''))
    _run(client.assess('Bearer k', text='hi', language='en'))
    monkeypatch.setattr(config, 'LANGUAGE_AUTO_ENABLED', False)
    _run(client.assess('Bearer k', text='hi', language=''))
    auto, auto_again, explicit, unset = _keys(api, '/assess')
    assert auto == auto_again
    assert len({auto, explicit, unset}) == 3
    assert auto == client._idem_key('assess', 'hi', 'auto')
    assert unset == client._idem_key('assess', 'hi', '')


def test_a_select_or_a_citation_check_is_never_sent_a_language(monkeypatch, api):
    monkeypatch.setattr(config, 'LANGUAGE_AUTO_ENABLED', True)
    sent = []

    async def _spy(method, path, authorization, *, json=None, **kwargs):
        sent.append((path, json))
        return client.ApiResponse(status=200, data={})

    monkeypatch.setattr(client, '_request', _spy)
    _run(client.select('Bearer k', task_id='t1', texts=['a']))
    _run(client.citecheck('Bearer k', text='A draft [a](https://e.org/a).'))
    assert all('language' not in (body or {}) for _path, body in sent)


# ── the tools ────────────────────────────────────────────────────────


@pytest.mark.parametrize(('on', 'expected'), [(True, 'auto'), (False, '')])
def test_the_tools_send_the_effective_language_for_an_unset_one(monkeypatch, api, on, expected):
    monkeypatch.setattr(config, 'LANGUAGE_AUTO_ENABLED', on)
    _run(server.assess_claim('Der Rhein ist der längste Fluss Deutschlands.', _ctx()))
    _run(server.verify_claim('Der Rhein ist der längste Fluss Deutschlands.', _ctx()))
    _run(server.ask_followup('abcd1234', 'Welche Quelle ist am stärksten?', _ctx()))
    assert _bodies(api, '/assess')[0]['language'] == expected
    assert _bodies(api, '/verify')[0]['language'] == expected
    assert _bodies(api, 'abcd1234')[0]['language'] == expected


@pytest.mark.parametrize('on', [True, False])
def test_an_explicit_code_is_never_replaced(monkeypatch, api, on):
    monkeypatch.setattr(config, 'LANGUAGE_AUTO_ENABLED', on)
    text = 'Der Rhein ist der längste Fluss Deutschlands.'
    _run(server.assess_claim(text, _ctx(), language='en'))
    _run(server.verify_claim(text, _ctx(), language='en'))
    _run(server.ask_followup('abcd1234', 'Which source is strongest?', _ctx(), language='en'))
    assert _bodies(api, '/assess')[0]['language'] == 'en'
    assert _bodies(api, '/verify')[0]['language'] == 'en'
    assert _bodies(api, 'abcd1234')[0]['language'] == 'en'


def test_the_claim_text_goes_to_the_api_as_it_was_given(monkeypatch, api):
    monkeypatch.setattr(config, 'LANGUAGE_AUTO_ENABLED', True)
    text = 'Der Rhein ist der längste Fluss Deutschlands.'
    _run(server.assess_claim(text, _ctx()))
    assert _bodies(api, '/assess')[0]['claim'] == text


@pytest.mark.parametrize('tool', ['assess_claim', 'verify_claim', 'ask_followup'])
@pytest.mark.parametrize('on', [True, False])
def test_an_unsupported_code_never_reaches_the_api(monkeypatch, api, tool, on):
    monkeypatch.setattr(config, 'LANGUAGE_AUTO_ENABLED', on)
    arguments = {
        'assess_claim': {'claim': 'x'},
        'verify_claim': {'claim': 'x'},
        'ask_followup': {'verification_id': 'abcd1234', 'question': 'q'},
    }[tool]
    with pytest.raises(Exception) as excinfo:  # noqa: PT011
        _run(server.mcp.call_tool(tool, {**arguments, 'language': 'ko'}))
    assert api == []
    assert "'en'" in str(excinfo.value)


def test_auto_is_not_a_value_the_model_can_pass():
    tools = {t.name: t for t in _run(server.mcp.list_tools())}
    for name in ('assess_claim', 'verify_claim', 'ask_followup'):
        assert 'auto' not in tools[name].input_schema['properties']['language']['enum']


# ── the card's deep check ────────────────────────────────────────────


def _start(monkeypatch, api, *, language, on):
    monkeypatch.setattr(config, 'CARD_ENABLED', True)
    monkeypatch.setattr(config, 'LANGUAGE_AUTO_ENABLED', on)
    return _run(server._start_verification_for_card('Der Rhein ist lang.', _ctx(), '', language))


def test_the_card_passes_the_quick_checks_language_on_unchanged(monkeypatch, api):
    out = _start(monkeypatch, api, language='de', on=True)
    assert out['status'] == 'submitted'
    assert _bodies(api, '/verify')[0]['language'] == 'de'


def test_the_card_with_no_language_sends_auto_when_the_switch_is_on(monkeypatch, api):
    _start(monkeypatch, api, language='', on=True)
    assert _bodies(api, '/verify')[0]['language'] == 'auto'


def test_the_card_with_no_language_sends_what_it_always_sent_when_the_switch_is_off(monkeypatch, api):
    _start(monkeypatch, api, language='', on=False)
    assert _bodies(api, '/verify')[0]['language'] == ''


# ── what the model is told ───────────────────────────────────────────


def _tools():
    return {t.name: t for t in _run(server.mcp.list_tools())}


@pytest.mark.parametrize('on', [True, False])
def test_the_descriptions_do_not_depend_on_the_switch(monkeypatch, on):
    monkeypatch.setattr(config, 'LANGUAGE_AUTO_ENABLED', on)
    snapshot = {
        name: (tool.description, json.dumps(tool.input_schema, sort_keys=True)) for name, tool in _tools().items()
    }
    monkeypatch.setattr(config, 'LANGUAGE_AUTO_ENABLED', not on)
    assert snapshot == {
        name: (tool.description, json.dumps(tool.input_schema, sort_keys=True)) for name, tool in _tools().items()
    }


def test_the_field_description_states_the_rule():
    for name in ('assess_claim', 'verify_claim', 'ask_followup'):
        text = ' '.join(_tools()[name].input_schema['properties']['language']['description'].split())
        assert 'Leave unset' in text, name
        assert "language of the user's text" in text, name
        assert 'explicitly asked for the answer in another language' in text, name
        assert "never to match the conversation, the user's locale or the language of the claim" in text, name
        assert 'ISO 639-1' not in text, name
        for code in config.SUPPORTED_LANGUAGES:
            assert code in text, (name, code)


def test_the_claim_is_to_be_passed_in_the_users_own_language():
    for name, param in (('assess_claim', 'claim'), ('verify_claim', 'claim')):
        text = ' '.join(_tools()[name].input_schema['properties'][param]['description'].split())
        assert "the user's own words and language, not translated" in text, name


def test_the_assess_description_qualifies_what_follows_the_language():
    text = ' '.join(_tools()['assess_claim'].description.split())
    assert 'Verdicts and written results follow the language of the text' in text
    assert "a reviewer's note follows the text it saw" in text


def test_the_ask_description_says_the_reply_follows_the_check():
    text = ' '.join(_tools()['ask_followup'].description.split())
    assert 'Replies in the language of the check' in text
    assert 'Replies in English' not in text
