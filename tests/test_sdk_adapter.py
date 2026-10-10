"""The client's calls through the lenz-io SDK, pinned on the wire.

`client.py` sends every API call through `lenz_io.AsyncLenz`, built per call over
the one shared `httpx.AsyncClient`. These tests drive the real client functions
against an `httpx.MockTransport` (tests/api_wire.py) and pin what goes out (the
credential, our User-Agent, the API version, the idempotency key, the body, the
timeout) and what each kind of answer becomes for the tools (`ApiResponse`).
"""

from __future__ import annotations

import asyncio
import json
import types
from typing import Any

import httpx
import pytest

from lenz_mcp import client, config, server
from tests import api_wire

OLD_VERSION = {config.API_VERSION_HEADER: '2026-05-13'}


def _run(coro):
    return asyncio.run(coro)


def _timeout(seconds: float) -> dict[str, float]:
    return {'connect': seconds, 'read': seconds, 'write': seconds, 'pool': seconds}


def _ctx(auth: str | None = 'Bearer lenz_testkey'):
    headers = {'authorization': auth} if auth else {}
    request = types.SimpleNamespace(headers=headers)
    return types.SimpleNamespace(request_context=types.SimpleNamespace(request=request))


@pytest.fixture
def wire(monkeypatch):
    return api_wire.install(monkeypatch)


@pytest.fixture(autouse=True)
def _client_identity():
    reset = client.bind_client_user_agent('Claude-User/1.0')
    yield
    reset()


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(server, '_sleep', _instant)


# ── every endpoint on the wire ───────────────────────────────────────

AUTH = 'Bearer lat_AbC-123_xyz'
ASSESS_TIMEOUT = config.assess_timeout('Claude-User')

# (name, call, method, path, body, idempotency key or None, timeout)
ENDPOINTS: list[tuple[str, Any, str, str, Any, str | None, float]] = [
    (
        'assess single',
        lambda: client.assess(AUTH, text='One claim.', language='de', suggest_rewrite=True),
        'POST',
        '/api/v1/assess',
        {'text': 'One claim.', 'language': 'de', 'suggest_rewrite': True},
        client._idem_key('assess', 'One claim.', 'de', 'suggest_rewrite'),
        ASSESS_TIMEOUT,
    ),
    (
        'assess list',
        lambda: client.assess(AUTH, claims=['a', 'b'], language=''),
        'POST',
        '/api/v1/assess',
        {'claims': ['a', 'b'], 'language': 'auto'},
        client._idem_key('assess', 'claims', 'a\x1fb', 'auto'),
        ASSESS_TIMEOUT,
    ),
    (
        'verify',
        lambda: client.verify(AUTH, text='A claim.', language='', depth='low', retry_of='f' * 32),
        'POST',
        '/api/v1/verify',
        {'text': 'A claim.', 'language': 'auto', 'depth': 'low'},
        client._idem_key('verify', 'A claim.', 'auto', 'low', 'retry_of', 'f' * 32),
        config.DEFAULT_TIMEOUT,
    ),
    (
        'verify_status',
        lambda: client.verify_status(AUTH, task_id='t' * 32),
        'GET',
        '/api/v1/verify/status/' + 't' * 32,
        None,
        None,
        config.DEFAULT_TIMEOUT,
    ),
    (
        'verification_detail',
        lambda: client.verification_detail(AUTH, verification_id='deadbeef'),
        'GET',
        '/api/v1/verifications/deadbeef',
        None,
        None,
        config.DEFAULT_TIMEOUT,
    ),
    (
        'list_verifications',
        lambda: client.list_verifications(AUTH, page_size=10),
        'GET',
        '/api/v1/verifications',
        None,
        None,
        config.DEFAULT_TIMEOUT,
    ),
    (
        'select',
        lambda: client.select(AUTH, task_id='t' * 32, texts=['First.', 'Second.']),
        'POST',
        '/api/v1/verify/' + 't' * 32 + '/select',
        {'texts': ['First.', 'Second.']},
        client._idem_key('select', 't' * 32, 'First.', 'Second.'),
        config.DEFAULT_TIMEOUT,
    ),
    (
        'ask',
        lambda: client.ask(AUTH, verification_id='deadbeef', message='Why?'),
        'POST',
        '/api/v1/ask/deadbeef',
        {'message': 'Why?', 'language': 'auto'},
        None,
        config.ASK_TIMEOUT,
    ),
    (
        'citecheck',
        lambda: client.citecheck(AUTH, text='A draft [a](https://e.org/a).', max_citations=5),
        'POST',
        '/api/v1/citecheck',
        {'text': 'A draft [a](https://e.org/a).', 'max_citations': 5},
        client._idem_key('citecheck', '{"max_citations":5,"text":"A draft [a](https://e.org/a)."}'),
        config.DEFAULT_TIMEOUT,
    ),
    (
        'citecheck_status',
        lambda: client.citecheck_status(AUTH, citecheck_id='ab12cd34'),
        'GET',
        '/api/v1/citechecks/ab12cd34',
        None,
        None,
        config.DEFAULT_TIMEOUT,
    ),
    (
        'me_usage',
        lambda: client.me_usage(AUTH),
        'GET',
        '/api/v1/me/usage',
        None,
        None,
        config.DEFAULT_TIMEOUT,
    ),
]


@pytest.mark.parametrize(
    ('call', 'method', 'path', 'body', 'key', 'timeout'),
    [row[1:] for row in ENDPOINTS],
    ids=[row[0] for row in ENDPOINTS],
)
def test_each_endpoint_on_the_wire(wire, call, method, path, body, key, timeout):
    resp = _run(call())
    assert resp.ok
    request = wire.last
    assert len(wire.requests) == 1
    assert request.method == method
    assert request.url.path == path
    assert request.headers['authorization'] == AUTH
    assert request.headers['user-agent'] == f'{config.USER_AGENT} (Claude-User/1.0)'
    assert request.headers['x-lenz-api-version'] == config.API_VERSION == '2026-10-11'
    assert request.headers['accept'] == 'application/json'
    assert request.extensions['timeout'] == _timeout(timeout)
    if body is None:
        assert request.content == b''
    else:
        assert json.loads(request.content) == body
        assert request.headers['content-type'] == 'application/json'
    if key is None:
        # Reads carry none, and `ask` none on purpose (a re-ask is a new turn),
        # not even the random one the SDK would send unasked.
        assert 'idempotency-key' not in request.headers
    else:
        assert request.headers['idempotency-key'] == key


def test_the_list_asks_for_the_first_page_of_the_given_size(wire):
    _run(client.list_verifications(AUTH, page_size=10))
    assert dict(wire.last.url.params) == {'page': '1', 'page_size': '10'}


def test_suggest_rewrite_is_absent_unless_asked(wire):
    _run(client.assess(AUTH, text='x', language=''))
    assert wire.body() == {'text': 'x', 'language': 'auto'}


def test_citecheck_sends_exactly_the_fields_given(wire):
    pairs = [{'statement': 'S.', 'url': 'https://e.org/a', 'quote': 'q'}]
    _run(client.citecheck(AUTH, pairs=pairs))
    assert wire.body() == {'pairs': pairs}
    assert wire.last.headers['idempotency-key'] == client._idem_key(
        'citecheck', client._canonical_json({'pairs': pairs})
    )


# ── the credential ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ('header', 'sent'),
    [
        ('Bearer lenz_abc', 'Bearer lenz_abc'),
        ('bearer lat_x', 'Bearer lat_x'),
        ('BEARER   lat_x  ', 'Bearer lat_x'),
        ('  Bearer lat_x', 'Bearer lat_x'),
    ],
)
def test_the_bearer_token_is_sent_as_a_bearer(wire, header, sent):
    _run(client.me_usage(header))
    assert wire.last.headers['authorization'] == sent


@pytest.mark.parametrize(
    'header', [None, '', '   ', 'Bearer', 'Bearer   ', 'Basic dXNlcjpwYXNz', 'lenz_abc', 'Token x']
)
def test_a_value_that_is_not_a_bearer_is_refused_before_any_request(monkeypatch, wire, header):
    """Refused like a missing credential (401, no request) — and the process's
    own LENZ_API_KEY is never sent in its place (the SDK reads it when built
    with no key)."""
    monkeypatch.setenv('LENZ_API_KEY', 'lenz_from_the_environment')
    resp = _run(client.me_usage(header))
    assert resp.status == 401 and resp.data == {}
    assert wire.requests == []


@pytest.mark.parametrize('header', ['Bearer lat x', 'Bearer lat_\x00x', 'Bearer lenz_caf\xe9'])
def test_a_token_the_sdk_will_not_send_is_refused_like_a_missing_one(wire, header):
    resp = _run(client.me_usage(header))
    assert resp.status == 401 and resp.data == {}
    assert wire.requests == []


def test_a_refused_value_reads_as_the_missing_credential_answer(wire):
    out = _run(server.check_usage(_ctx('Basic dXNlcjpwYXNz')))
    assert out['status'] == 'auth_required'
    assert wire.requests == []


def test_the_anonymous_read_sends_no_credential_and_never_the_environments(monkeypatch, wire):
    """GET /verifications/{id} also answers without a credential (public and
    unlisted results), so a call with none is sent with none."""
    monkeypatch.setenv('LENZ_API_KEY', 'lenz_from_the_environment')
    resp = _run(client.verification_detail(None, verification_id='deadbeef'))
    assert resp.ok and resp.data['verification_id'] == 'deadbeef'
    assert 'authorization' not in wire.last.headers


def test_the_anonymous_read_still_refuses_a_credential_that_is_not_a_bearer(wire):
    resp = _run(client.verification_detail('Basic x', verification_id='deadbeef'))
    assert resp.status == 401
    assert wire.requests == []


# ── concurrency and the shared client ───────────────────────────────


def test_concurrent_calls_never_cross_their_credentials_or_user_agents(wire):
    def _answer(request):
        n = request.url.path.rsplit('/', 1)[-1]
        return api_wire.answer(200, {'task_id': n, 'status': 'processing'})

    wire.respond(_answer)

    async def _one(i: int):
        reset = client.bind_client_user_agent(f'client-{i}/1.0')
        try:
            await asyncio.sleep(0)
            return await client.verify_status(f'Bearer lat_{i}', task_id=f'task{i}')
        finally:
            reset()

    async def _all():
        return await asyncio.gather(*[_one(i) for i in range(25)])

    results = _run(_all())
    assert [r.data['task_id'] for r in results] == [f'task{i}' for i in range(25)]
    assert len(wire.requests) == 25
    for request in wire.requests:
        i = request.url.path.rsplit('task', 1)[-1]
        assert request.headers['authorization'] == f'Bearer lat_{i}'
        assert request.headers['user-agent'] == f'{config.USER_AGENT} (client-{i}/1.0)'
    # Sequential calls after concurrent ones and after a failure still work,
    # and no SDK instance closed the pool it borrowed.
    wire.respond(lambda _r: httpx.Response(500, text='boom'))
    assert _run(client.me_usage(AUTH)).status == 500
    wire.respond(api_wire.default_answer)
    assert _run(client.me_usage(AUTH)).ok
    assert client._http_client is not None and not client._http_client.is_closed


def test_a_replaced_shared_client_is_used_by_the_next_call(wire, monkeypatch):
    """The SDK is built per call over whatever `_http_client` is now: replacing
    it after the first request (as tests and the API's contract harness do)
    takes effect at once."""
    _run(client.me_usage(AUTH))
    assert len(wire.requests) == 1
    second = []

    def _other(request):
        second.append(request)
        return api_wire.answer(200, {'tier': 'pro'})

    monkeypatch.setattr(client, '_http_client', api_wire._REAL_ASYNC_CLIENT(transport=httpx.MockTransport(_other)))
    resp = _run(client.me_usage(AUTH))
    assert resp.data == {'tier': 'pro'}
    assert len(second) == 1 and len(wire.requests) == 1


# ── answers: success ─────────────────────────────────────────────────


def test_a_success_keeps_its_status_headers_and_body_with_unknown_keys(wire):
    """The body exactly as sent (the SDK's `raw`), the response's status (a
    receipt reads 202, as with the old client) and its headers."""
    body = {'task_id': 'a' * 32, 'status': 'queued', 'brand_new_field': {'x': [1, None]}}
    wire.respond(api_wire.answer(202, body, {'Location': '/api/v1/verify/status/' + 'a' * 32, 'Retry-After': '20'}))
    resp = _run(client.verify(AUTH, text='x', language=''))
    assert resp.status == 202 and resp.ok
    assert resp.data == body
    assert resp.headers['location'] == '/api/v1/verify/status/' + 'a' * 32
    assert resp.headers['retry-after'] == '20'
    assert resp.headers['x-lenz-api-version'] == config.API_VERSION


@pytest.mark.parametrize(
    ('call', 'status'),
    [
        (lambda: client.verify(AUTH, text='x', language=''), 202),
        (lambda: client.select(AUTH, task_id='t' * 32, texts=['a']), 202),
        (lambda: client.citecheck(AUTH, text='A draft [a](https://e.org/a).'), 202),
        (lambda: client.me_usage(AUTH), 200),
    ],
    ids=['verify', 'select', 'citecheck', 'usage'],
)
def test_receipts_read_202_and_reads_200(wire, call, status):
    resp = _run(call())
    assert resp.status == status and resp.ok


def test_a_success_body_is_the_callers_own_copy(wire):
    body = {'claims': [{'claim': 'c', 'verdict': 'True', 'confidence': 'high'}], 'more_claims': []}
    wire.respond(api_wire.answer(200, body))
    resp = _run(client.assess(AUTH, text='c', language=''))
    resp.data['claims'][0]['claim'] = 'changed'
    assert body['claims'][0]['claim'] == 'c'


def test_a_null_on_a_typed_field_is_read_not_refused(wire):
    """A stored replay of an older answer carries `error_code: null`; it is
    read, as sent, not refused as an unreadable answer."""
    body = {
        'claims': [{'claim': 'c', 'verdict': 'True', 'confidence': 'high', 'error_code': None, 'hint': None}],
        'error': None,
        'error_code': None,
        'more_claims': [],
    }
    wire.respond(api_wire.answer(200, body))
    resp = _run(client.assess(AUTH, text='c', language=''))
    assert resp.ok and resp.data == body


def test_a_success_dump_adds_no_defaults(wire):
    """What the API did not send stays absent: a model default must never read
    as an answer (a missing `depth` is not `''`, a missing list is not `[]`)."""
    body = {'task_id': 'a' * 32, 'status': 'completed', 'verification_id': 'deadbeef', 'result': {'score': 7}}
    wire.respond(api_wire.answer(200, body))
    resp = _run(client.verify_status(AUTH, task_id='a' * 32))
    assert resp.data == body


def test_an_unknown_status_value_passes_through(wire):
    wire.respond(api_wire.answer(200, {'status': 'weird_new_status'}))
    resp = _run(client.verify_status(AUTH, task_id='a' * 32))
    assert resp.ok and resp.data == {'status': 'weird_new_status'}


def test_an_answer_with_no_version_header_proceeds(wire):
    wire.respond(httpx.Response(200, json={'tier': 'free'}))
    assert _run(client.me_usage(AUTH)).data == {'tier': 'free'}


# ── answers: what is never a success ────────────────────────────────


def _not_json(status: int, content: bytes = b'', headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, content=content, headers={**api_wire.VERSION_HEADERS, **(headers or {})})


INVALID_SUCCESSES = {
    'empty 200': _not_json(200),
    'empty 200, Content-Length 0': _not_json(200, headers={'Content-Length': '0'}),
    'empty 202': _not_json(202),
    '204': _not_json(204),
    '205': _not_json(205),
    '200 html': _not_json(200, b'<html>hi</html>', {'content-type': 'text/html'}),
    '200 whitespace': _not_json(200, b'   ', {'content-type': 'application/json'}),
    '200 json array': api_wire.answer(200, [1, 2]),
    '200 json scalar': _not_json(200, b'42', {'content-type': 'application/json'}),
    '200 json null': _not_json(200, b'null', {'content-type': 'application/json'}),
    '200 wrong type': api_wire.answer(200, {'claims': 'not a list'}),
    '200 malformed nested row': api_wire.answer(200, {'claims': [42]}),
    '302 json body': api_wire.answer(302, {'claims': []}, {'Location': 'https://elsewhere.test/'}),
    '302 no body': _not_json(302, headers={'Location': 'https://elsewhere.test/'}),
    '304': _not_json(304),
}


@pytest.mark.parametrize('name', sorted(INVALID_SUCCESSES))
def test_an_answer_that_cannot_be_read_is_never_ok(wire, name, caplog):
    wire.respond(INVALID_SUCCESSES[name])
    resp = _run(client.assess(AUTH, text='x', language=''))
    assert not resp.ok
    assert resp.status == client.INVALID_RESPONSE_STATUS
    assert resp.data['code'] == client.INVALID_RESPONSE_CODE
    assert 'mcp_api_invalid_response' in caplog.text


def test_an_unreadable_answer_reaches_the_model_as_a_plain_error(wire):
    wire.respond(api_wire.answer(200, [1, 2]))
    out = _run(server.assess_claim('x', _ctx()))
    assert out['status'] == 'error'
    assert 'lenz_io' not in json.dumps(out) and 'pydantic' not in json.dumps(out)


# ── answers: errors, as the tools always read them ──────────────────


def _error(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return api_wire.answer(status, body, headers)


ERRORS: dict[str, tuple[httpx.Response, dict[str, Any], str | None]] = {
    '401': (_error(401, {'detail': 'Invalid API key', 'code': 'invalid_api_key'}), {}, None),
    '402': (
        _error(402, {'detail': 'No credits.', 'code': 'no_credits', 'credits_remaining': 0, 'cost': 10}),
        {},
        None,
    ),
    '403': (_error(403, {'detail': 'Forbidden', 'code': 'insufficient_scope'}), {}, None),
    '404': (_error(404, {'detail': 'Not found.', 'code': 'not_found'}), {}, None),
    '409 with task_id': (_error(409, {'code': 'idempotency_conflict', 'task_id': 't' * 32}), {}, None),
    '410': (_error(410, {'detail': 'Gone.', 'code': 'purged'}), {}, None),
    '422': (
        _error(422, {'detail': 'Invalid.', 'code': 'validation_error', 'errors': [{'loc': ['body', 'claim']}]}),
        {},
        None,
    ),
    '429 header and body agree': (
        _error(429, {'detail': 'Slow.', 'code': 'rate_limited', 'retry_after': 45}, {'Retry-After': '45'}),
        {},
        '45',
    ),
    '429 header only': (_error(429, {'detail': 'Slow.'}, {'Retry-After': '45'}), {}, '45'),
    '429 body only': (_error(429, {'detail': 'Slow.', 'retry_after': 30}), {}, None),
    '429 header conflicts with body': (
        _error(429, {'detail': 'Slow.', 'retry_after': 30}, {'Retry-After': '45'}),
        {},
        '45',
    ),
    '429 zero header': (_error(429, {'detail': 'Slow.'}, {'Retry-After': '0'}), {}, '0'),
    '429 malformed header': (_error(429, {'detail': 'Slow.'}, {'Retry-After': 'soon'}), {}, 'soon'),
    '429 date header': (
        _error(429, {'detail': 'Slow.'}, {'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT'}),
        {},
        'Wed, 21 Oct 2026 07:28:00 GMT',
    ),
    '500 html': (
        httpx.Response(
            500, text='<html>oops</html>', headers={**api_wire.VERSION_HEADERS, 'content-type': 'text/html'}
        ),
        {'data': {}},
        None,
    ),
    '503': (
        _error(503, {'detail': 'At capacity.', 'code': 'capacity', 'retry_after': 90}, {'Retry-After': '90'}),
        {},
        '90',
    ),
    '400': (_error(400, {'detail': 'Not completed.', 'code': 'verification_not_ready'}), {}, None),
    'error body that is an array': (_error(418, [1, 2]), {'data': {}}, None),
}


@pytest.mark.parametrize('name', sorted(ERRORS))
def test_an_error_answer_keeps_its_status_body_and_headers(wire, name):
    response, override, retry_after = ERRORS[name]
    wire.respond(response)
    resp = _run(client.verify(AUTH, text='x', language=''))
    assert len(wire.requests) == 1  # the SDK never retries (max_retries=0)
    assert resp.status == response.status_code
    expected_data = override['data'] if 'data' in override else json.loads(response.content)
    assert resp.data == expected_data
    assert resp.headers.get('retry-after') == retry_after
    assert not resp.ok


def test_a_429_tool_result_names_the_stated_wait(wire):
    wire.respond(_error(429, {'detail': 'Slow.'}, {'Retry-After': '45'}))
    out = _run(server.check_usage(_ctx()))
    assert out['status'] == 'rate_limited' and out['retry_after_seconds'] == 45


@pytest.mark.parametrize(
    ('exc', 'name'),
    [
        (httpx.ReadTimeout, 'timeout'),
        (httpx.ConnectError, 'connect'),
        (httpx.RemoteProtocolError, 'hung up'),
        (httpx.LocalProtocolError, 'could not be sent'),
    ],
)
def test_no_answer_at_all_is_status_0(wire, exc, name, caplog):
    def _fail(request):
        if exc is httpx.LocalProtocolError:
            raise exc(name)
        raise exc(name, request=request)

    wire.respond(_fail)
    resp = _run(client.verify(AUTH, text='x', language=''))
    assert resp.status == 0 and resp.data == {} and not resp.ok
    assert len(wire.requests) == 1
    assert 'mcp_api_transport_error' in caplog.text


# ── the API version ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    'response',
    [
        api_wire.answer(200, {'tier': 'free'}, OLD_VERSION),
        api_wire.answer(202, {'tier': 'free'}, OLD_VERSION),
        api_wire.answer(200, {'tier': 'free'}, {config.API_VERSION_HEADER: 'garbage'}),
    ],
    ids=['200', '202', 'garbage'],
)
def test_a_success_in_another_api_version_is_a_plain_connector_error(wire, response, caplog):
    """The SDK reads one API version, and a success in another cannot be read
    as this one's. The tool says so plainly, never in the SDK's words."""
    wire.respond(response)
    resp = _run(client.me_usage(AUTH))
    assert resp.status == client.INVALID_RESPONSE_STATUS and not resp.ok
    assert resp.data['code'] == client.API_VERSION_UNSUPPORTED_CODE
    assert 'mcp_api_version_mismatch' in caplog.text
    out = _run(server.check_usage(_ctx()))
    assert out['status'] == 'error'
    text = json.dumps(out)
    assert 'lenz-io' not in text and '2.x' not in text and 'contact support' not in text


@pytest.mark.parametrize(
    ('response', 'status', 'tool_status'),
    [
        (api_wire.answer(401, {'detail': 'Invalid API key'}, OLD_VERSION), 401, 'auth_required'),
        (api_wire.answer(402, {'detail': 'No credits.', 'code': 'no_credits'}, OLD_VERSION), 402, 'quota_exhausted'),
        (
            api_wire.answer(429, {'detail': 'Slow.'}, {**OLD_VERSION, 'Retry-After': '45'}),
            429,
            'rate_limited',
        ),
        (
            api_wire.answer(503, {'detail': 'At capacity.', 'code': 'capacity', 'retry_after': 90}, OLD_VERSION),
            503,
            'service_unavailable',
        ),
    ],
    ids=['401', '402', '429', '503'],
)
def test_an_error_in_another_api_version_is_still_that_error(wire, response, status, tool_status):
    """An error answer (400 and above) from an API in another version arrives
    as the real error, with its status, body and headers: a real out-of-credits
    or rate-limit answer is shown as one."""
    wire.respond(response)
    resp = _run(client.me_usage(AUTH))
    assert resp.status == status
    assert resp.data == json.loads(response.content)
    assert resp.headers.get('retry-after') == response.headers.get('retry-after')
    out = _run(server.check_usage(_ctx()))
    assert out['status'] == tool_status
    if status == 429:
        assert out['retry_after_seconds'] == 45


# ── ask ──────────────────────────────────────────────────────────────


def test_ask_400_reads_as_not_completed(wire):
    wire.respond(_error(400, {'detail': 'Ask is only available for completed verifications.'}))
    out = _run(server.ask_followup('deadbeef', 'Why?', _ctx()))
    assert out['status'] == 'not_completed'


@pytest.mark.parametrize('code', ['verification_not_ready', 'verification_failed'])
def test_ask_409_keeps_its_code_in_the_body(wire, code):
    """The SDK rebuilds `.code` and blanks some; the connector reads the body."""
    wire.respond(_error(409, {'code': code, 'task_id': 't' * 32}))
    resp = _run(client.ask(AUTH, verification_id='deadbeef', message='Why?'))
    assert resp.status == 409 and resp.data['code'] == code


def test_ask_answer_reaches_the_tool(wire):
    wire.respond(api_wire.answer(200, {'role': 'expert', 'content': 'Because.', 'created_at': '2026-10-10T10:00:00Z'}))
    out = _run(server.ask_followup('deadbeef', 'Why?', _ctx()))
    assert out['status'] == 'ok' and out['answer'] == 'Because.'


def test_asking_twice_sends_two_questions_without_a_key(wire):
    _run(client.ask(AUTH, verification_id='deadbeef', message='Why?'))
    _run(client.ask(AUTH, verification_id='deadbeef', message='Why?'))
    assert len(wire.requests) == 2
    assert all('idempotency-key' not in r.headers for r in wire.requests)


# ── renewal of an exchanged credential ──────────────────────────────


class _Credential:
    def __init__(self, renewed: str | None = 'Bearer lat_new'):
        self.renewed = renewed
        self.renewals = 0

    async def header(self) -> str:
        return 'Bearer lat_old'

    async def renew(self) -> str | None:
        self.renewals += 1
        return self.renewed


def test_a_401_renews_once_and_resends_the_same_request(wire):
    def _answer(request):
        if request.headers['authorization'] == 'Bearer lat_old':
            return _error(401, {'detail': 'Expired.'})
        return api_wire.answer(202, {'task_id': 'a' * 32, 'status': 'queued'})

    wire.respond(_answer)
    credential = _Credential()
    resp = _run(client.verify(credential, text='A claim.', language='en'))
    assert resp.ok
    assert credential.renewals == 1
    first, second = wire.requests
    assert [first.headers['authorization'], second.headers['authorization']] == ['Bearer lat_old', 'Bearer lat_new']
    assert first.content == second.content
    assert first.headers['idempotency-key'] == second.headers['idempotency-key']


def test_a_second_401_is_the_answer(wire):
    wire.respond(_error(401, {'detail': 'Expired.'}))
    credential = _Credential()
    resp = _run(client.me_usage(credential))
    assert resp.status == 401 and credential.renewals == 1 and len(wire.requests) == 2


def test_a_renewal_that_gives_up_keeps_the_401(wire):
    wire.respond(_error(401, {'detail': 'Expired.'}))
    credential = _Credential(renewed=None)
    resp = _run(client.me_usage(credential))
    assert resp.status == 401 and credential.renewals == 1 and len(wire.requests) == 1


def test_no_renewal_for_a_plain_key(wire):
    wire.respond(_error(401, {'detail': 'Invalid API key'}))
    resp = _run(client.me_usage('Bearer lenz_abc'))
    assert resp.status == 401 and len(wire.requests) == 1


# ── citation checks: a resend that names the running check ──────────


def test_a_409_naming_the_check_is_attached_to_and_polled(wire):
    """A resend of the same submission while the first is still being created:
    the API answers 409 naming the check. The SDK settles the call with it
    (`settled_by_conflict`); the connector keeps it a 409, and the tool
    attaches by id."""

    def _answer(request):
        if request.url.path.endswith('/citecheck'):
            return _error(409, {'code': 'idempotency_conflict', 'citecheck_id': 'ab12cd34', 'detail': 'In progress.'})
        return api_wire.answer(
            200,
            {
                'citecheck_id': 'ab12cd34',
                'status': 'completed',
                'outcome': 'clean',
                'citations': [],
                'summary': {},
                'more_citations': None,
            },
        )

    wire.respond(_answer)
    resp = _run(client.citecheck(AUTH, text='A draft [a](https://e.org/a).'))
    # The 409 it was (the SDK's `settled_by_conflict`), as the old client gave
    # it: the tool's `status == 409` branch attaches by the id in the body.
    assert resp.status == 409 and not resp.ok
    assert resp.data['citecheck_id'] == 'ab12cd34'
    assert len(wire.requests) == 1  # settled, not resent
    out = _run(server.check_citations('A draft [a](https://e.org/a).', _ctx()))
    assert out['status'] == 'completed' and out['citecheck_id'] == 'ab12cd34'
    assert wire.last.url.path == '/api/v1/citechecks/ab12cd34'


def test_a_409_without_a_check_id_is_an_error(wire):
    wire.respond(_error(409, {'code': 'idempotency_conflict', 'citecheck_id': None}))
    resp = _run(client.citecheck(AUTH, text='A draft [a](https://e.org/a).'))
    assert resp.status == 409 and resp.data['citecheck_id'] is None


# ── lone surrogates: dropped, in the body and the key alike ─────────


def _no_replacement(request: httpx.Request) -> bool:
    return '�' not in request.content.decode('utf-8')


def test_a_lone_surrogate_is_dropped_from_body_and_key_on_every_write(wire):
    _run(client.assess(AUTH, text='a\ud800b', language='en'))
    assert wire.body() == {'text': 'ab', 'language': 'en'}
    assert wire.last.headers['idempotency-key'] == client._idem_key('assess', 'ab', 'en')
    assert _no_replacement(wire.last)

    _run(client.assess(AUTH, claims=['a\ud800b', 'c'], language='en'))
    assert wire.body()['claims'] == ['ab', 'c']
    assert wire.last.headers['idempotency-key'] == client._idem_key('assess', 'claims', 'ab\x1fc', 'en')

    _run(client.verify(AUTH, text='a\ud800b', language='en'))
    assert wire.body()['text'] == 'ab'
    assert wire.last.headers['idempotency-key'] == client._idem_key('verify', 'ab', 'en')
    assert _no_replacement(wire.last)

    _run(client.ask(AUTH, verification_id='deadbeef', message='q\ud800?'))
    assert wire.body()['message'] == 'q?'
    assert _no_replacement(wire.last)

    _run(client.citecheck(AUTH, text='a\ud800b [x](https://e.org/x)'))
    assert wire.body() == {'text': 'ab [x](https://e.org/x)'}
    assert wire.last.headers['idempotency-key'] == client._idem_key(
        'citecheck', client._canonical_json({'text': 'ab [x](https://e.org/x)'})
    )
    assert _no_replacement(wire.last)

    _run(client.select(AUTH, task_id='t' * 32, texts=['a\ud800b']))
    assert wire.body() == {'texts': ['ab']}
    assert wire.last.headers['idempotency-key'] == client._idem_key('select', 't' * 32, 'ab')


def test_the_surrogate_key_is_the_one_an_earlier_release_sent():
    """The key of a text with a lone surrogate is the key of the text without
    it, as before: a resend across the upgrade still joins the first."""
    assert client._idem_key('verify', 'a\ud800b', 'en') == client._idem_key('verify', 'ab', 'en')


# ── arguments the SDK refuses before sending ────────────────────────


def test_an_empty_selection_is_refused_without_a_request(wire, caplog):
    resp = _run(client.select(AUTH, task_id='t' * 32, texts=[]))
    assert resp.status == 422 and not resp.ok
    # The SDK's sentence for this names its own parameters: not passed on.
    assert resp.data == {'code': 'empty_list', 'detail': 'The request was invalid.'}
    assert wire.requests == []
    assert 'mcp_api_request_refused op=select code=empty_list param=texts' in caplog.text


@pytest.mark.parametrize(
    ('call', 'code', 'detail'),
    [
        (lambda: client.assess(AUTH, text='   ', language=''), 'blank_input', 'claim is required.'),
        (lambda: client.assess(AUTH, claims=['a', '  '], language=''), 'blank_item', 'claims[1] is blank.'),
        (lambda: client.verify(AUTH, text='', language=''), 'blank_input', 'claim is required.'),
        (
            lambda: client.citecheck(AUTH, text='   '),
            'blank_input',
            'payload: Value error, send exactly one of text and pairs',
        ),
        (lambda: client.ask(AUTH, verification_id='deadbeef', message='  '), 'blank_input', 'Message cannot be empty.'),
        (lambda: client.list_verifications(AUTH, page_size=0), 'invalid_page_size', 'The request was invalid.'),
    ],
    ids=['assess blank', 'assess blank item', 'verify blank', 'citecheck blank', 'ask blank', 'page size'],
)
def test_a_refused_argument_reads_as_the_apis_422(wire, call, code, detail):
    """Blank input reads with the API's own sentence (the SDK's is the same: the
    old client sent it and showed the API's 422); every other refusal is the
    generic one."""
    resp = _run(call())
    assert resp.status == 422
    assert resp.data == {'code': code, 'detail': detail}
    assert wire.requests == []


def test_an_empty_id_is_refused_without_a_request(wire):
    resp = _run(client.verify_status(AUTH, task_id=''))
    assert resp.status == 422
    assert wire.requests == []


# ── blank input the SDK refuses before sending ──────────────────────


def test_a_blank_selection_item_is_left_out_as_the_api_does(wire):
    """The API has always dropped a blank item from a selection and run the
    rest; the SDK refuses the whole list. The connector drops it first, so a
    selection with one stray blank still starts the chosen checks. The key is
    the one this call always had."""
    resp = _run(client.select(AUTH, task_id='t' * 32, texts=['First.', '  ']))
    assert resp.ok
    assert wire.body() == {'texts': ['First.']}
    assert wire.last.headers['idempotency-key'] == client._idem_key('select', 't' * 32, 'First.', '  ')


def test_a_selection_of_only_blanks_is_refused_without_a_request(wire):
    resp = _run(client.select(AUTH, task_id='t' * 32, texts=[' ', '']))
    assert resp.status == 422
    assert resp.data == {'code': 'empty_list', 'detail': 'The request was invalid.'}
    assert wire.requests == []


def test_the_select_tool_still_starts_the_chosen_check_beside_a_blank(wire):
    wire.respond(api_wire.answer(202, {'items': [{'task_id': 'b' * 32, 'claim': 'First.'}]}))
    out = _run(server.select_claims('t' * 32, ['First.', ' '], _ctx()))
    assert out['status'] != 'invalid_request'
    assert [wire.body(r) for r in wire.requests if r.url.path.endswith('/select')] == [{'texts': ['First.']}]


def test_a_blank_follow_up_question_says_what_the_api_said(wire):
    """The old client sent it and showed the API's 422 sentence; the SDK
    refuses it before sending with the same sentence."""
    out = _run(server.ask_followup('deadbeef', '   ', _ctx()))
    assert out == {'status': 'invalid_request', 'message': 'Message cannot be empty.'}
    assert wire.requests == []


# ── a completed poll with nothing to show ───────────────────────────


@pytest.mark.parametrize(
    'body',
    [
        {'status': 'completed', 'task_id': 'a' * 32},
        {'status': 'completed', 'task_id': 'a' * 32, 'result': None},
        {'status': 'completed', 'task_id': 'a' * 32, 'result': 'nope'},
    ],
    ids=['absent', 'null', 'not an object'],
)
def test_a_completed_poll_without_a_result_is_a_plain_error(wire, body, caplog):
    """Not a verdict with every field empty (what the old client made of it)."""
    wire.respond(api_wire.answer(200, body))
    resp = _run(client.verify_status(AUTH, task_id='a' * 32))
    assert not resp.ok and resp.data['code'] == client.INVALID_RESPONSE_CODE
    assert 'mcp_api_invalid_response op=verify_status' in caplog.text
    out = _run(server.get_verification('a' * 32, _ctx()))
    assert out == {'status': 'error', 'message': client._INVALID_RESPONSE_DETAIL}


# ── keys refused before sending: invalid vs missing ─────────────────


def test_a_missing_key_is_refused_like_a_missing_credential(wire, monkeypatch):
    """The SDK raises LenzMissingKeyError (a sibling of LenzInvalidKeyError,
    both LenzAuthError) when a call that needs a key has none. The connector
    never builds one that way; if it ever did, the answer is the refusal,
    never a status-0 transport error or the SDK's own words."""
    monkeypatch.setenv('LENZ_API_KEY', 'lenz_from_the_environment')
    monkeypatch.setattr(client, '_bearer_token', lambda _header: '')
    resp = _run(client.me_usage('Bearer whatever'))
    assert resp.status == 401 and resp.data == {}
    assert wire.requests == []
