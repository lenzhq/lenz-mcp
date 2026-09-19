"""Protocol logging on the MCP endpoint.

Newer Claude clients probe a protocol version `mcp==1.28.1` does not support and
get a 400 from the SDK before any handler runs, so nothing we log inside the
server sees them. The log sits OUTSIDE the SDK app and records, for every
request to the endpoint: the version header, the JSON-RPC method, what an
`initialize` declares (version, capabilities, client), and the status we
answered. It observes only: the request reaches the app byte for byte and the
response is untouched, so a logged 400 stays a 400.
"""

import asyncio
import contextlib
import json
import logging

import pytest

from lenz_mcp import asgi, protocol_log
from lenz_mcp.testing import assembled_app

_TIMEOUT_S = 5
_LOGGER = 'lenz_mcp.protocol_log'


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, _TIMEOUT_S))


@contextlib.contextmanager
def _collecting():
    """Messages on the protocol logger, collected at the logger itself.

    Not `caplog`: `lenz_mcp.protocol_log` is configured `propagate: False` (as the
    deployed logging needs), so nothing reaches caplog's root handler and every assertion would
    see an empty list for the wrong reason. Same shape as test_oauth.

    A context manager as well as a fixture so a test can attach it inside an
    `assembled_app` block: reconfiguring logging there rebuilds this logger's
    handler list and would silently drop a collector attached earlier.
    """
    logger = logging.getLogger(_LOGGER)
    messages: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    handler = _Collect(level=logging.INFO)
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


@pytest.fixture
def logs():
    with _collecting() as messages:
        yield messages


def _stub(status=200):
    """The protocol log around a sentinel app that records what reached it."""
    reached = []

    async def _inner(scope, receive, send):
        body = b''
        while True:
            message = await receive()
            body += message.get('body', b'')
            if not message.get('more_body'):
                break
        reached.append((scope['method'], scope['path'], body))
        await send({'type': 'http.response.start', 'status': status, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'{}'})

    return protocol_log.ProtocolLog(_inner, path='/mcp'), reached


async def _drive(app, method, path, *, body=b'', headers=()):
    chunks = [body[: len(body) // 2], body[len(body) // 2 :]] if body else [b'']
    incoming = [
        {'type': 'http.request', 'body': chunk, 'more_body': i < len(chunks) - 1} for i, chunk in enumerate(chunks)
    ]
    sent = []

    async def _receive():
        return incoming.pop(0) if incoming else {'type': 'http.disconnect'}

    async def _send(message):
        sent.append(message)

    scope = {
        'type': 'http',
        'method': method,
        'path': path,
        'headers': [(k.lower().encode(), v.encode()) for k, v in headers],
    }
    await app(scope, _receive, _send)
    return sent


def _initialize(version='2025-06-18', capabilities=None, client=None):
    return json.dumps(
        {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {
                'protocolVersion': version,
                'capabilities': capabilities if capabilities is not None else {},
                'clientInfo': client or {'name': 'claude-ai', 'version': '0.1.0'},
            },
        }
    ).encode()


def test_initialize_logs_version_capabilities_and_client(logs):
    app, reached = _stub()
    body = _initialize(
        capabilities={'roots': {'listChanged': True}, 'elicitation': {'form': {}, 'url': {}}, 'sampling': {}},
        client={'name': 'claude-ai', 'version': '0.1.0'},
    )
    _run(_drive(app, 'POST', '/mcp', body=body, headers=[('user-agent', 'Claude-User')]))

    (line,) = logs
    assert line.startswith('mcp_protocol ')
    assert 'rpc=initialize' in line
    assert 'init_version=2025-06-18' in line
    assert 'capabilities=elicitation,elicitation.form,elicitation.url,roots,roots.listChanged,sampling' in line
    assert 'client=claude-ai/0.1.0' in line
    assert 'ua=Claude-User' in line
    assert 'status=200' in line
    # Observation only: the app received the exact body.
    assert reached == [('POST', '/mcp', body)]


def test_a_request_the_app_rejects_is_logged_with_its_version_and_status(logs):
    app, _ = _stub(status=400)
    body = json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}).encode()
    sent = _run(_drive(app, 'POST', '/mcp', body=body, headers=[('mcp-protocol-version', '2026-07-28')]))

    assert sent[0]['status'] == 400  # untouched
    (line,) = logs
    assert 'header_version=2026-07-28' in line
    assert 'rpc=tools/list' in line
    assert 'status=400' in line


def test_capabilities_declared_outside_initialize_are_logged_too(logs):
    """A newer client may declare what it supports on each request rather than
    once at initialize. Whatever it puts under `params._meta` or
    `params.capabilities` is recorded by key, so the log shows it either way."""
    app, _ = _stub()
    body = json.dumps(
        {
            'jsonrpc': '2.0',
            'id': 3,
            'method': 'tools/call',
            'params': {
                'name': 'assess_claim',
                'arguments': {'claim': 'A private sentence the log must never carry.'},
                '_meta': {'io.modelcontextprotocol/clientCapabilities': {'tasks': {}}, 'progressToken': 7},
            },
        }
    ).encode()
    _run(_drive(app, 'POST', '/mcp', body=body))

    (line,) = logs
    assert 'rpc=tools/call' in line
    assert 'tool=assess_claim' in line
    assert (
        'meta=io.modelcontextprotocol/clientCapabilities,io.modelcontextprotocol/clientCapabilities.tasks,progressToken'
    ) in line
    assert 'private sentence' not in line


def test_a_batch_logs_every_method(logs):
    app, _ = _stub()
    body = json.dumps(
        [
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/list'},
        ]
    ).encode()
    _run(_drive(app, 'POST', '/mcp', body=body))
    (line,) = logs
    assert 'rpc=notifications/initialized,tools/list' in line


def test_values_a_client_controls_cannot_forge_a_log_line(logs):
    app, _ = _stub()
    body = _initialize(
        version='2025-06-18\nmcp_protocol status=200',
        capabilities={'roots x=1': {}},
        client={'name': 'evil\nclient', 'version': '1 2'},
    )
    _run(_drive(app, 'POST', '/mcp', body=body, headers=[('user-agent', 'a b\tc')]))
    (line,) = logs
    assert '\n' not in line and '\t' not in line
    # One record, and no value can add a second `key=` to it.
    assert line.count('status=') == 1
    assert line.count('init_version=') == 1
    assert line.count('x=') == 0


def test_an_unparseable_body_is_passed_through_and_logged(logs):
    app, reached = _stub(status=400)
    sent = _run(_drive(app, 'POST', '/mcp', body=b'{not json'))
    assert reached == [('POST', '/mcp', b'{not json')]
    assert sent[0]['status'] == 400
    (line,) = logs
    assert 'rpc=-' in line
    assert 'status=400' in line


def test_a_get_on_the_endpoint_is_logged(logs):
    app, _ = _stub(status=405)
    _run(_drive(app, 'GET', '/mcp'))
    (line,) = logs
    assert 'http_method=GET' in line
    assert 'status=405' in line


@pytest.mark.parametrize('path', ['/mcp/healthz', '/healthz', '/.well-known/oauth-protected-resource'])
def test_other_paths_are_not_logged(logs, path):
    app, reached = _stub()
    _run(_drive(app, 'GET', path))
    assert logs == []
    assert reached == [('GET', path, b'')]


def test_a_logging_failure_never_breaks_the_request(monkeypatch, logs):
    app, reached = _stub()

    def _boom(*_args, **_kwargs):
        raise RuntimeError('log sink down')

    monkeypatch.setattr(protocol_log, '_describe', _boom)
    before = protocol_log.log_failures
    sent = _run(_drive(app, 'POST', '/mcp', body=_initialize()))
    assert sent[0]['status'] == 200
    assert len(reached) == 1
    # Swallowed, and counted so a silently broken log line is findable.
    assert protocol_log.log_failures == before + 1


def test_a_request_rejected_before_its_body_is_read_never_has_it_read(logs):
    """The log must not read ahead of the app. With OAuth
    on, the SDK's auth layer 401s a keyless POST without touching its body; the
    wrapper must leave it that way, so a client cannot make a rejected request
    wait on (or buffer) an upload."""
    receive_calls = []

    async def _rejects_without_reading(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 401, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    async def _receive():
        receive_calls.append(1)
        return {'type': 'http.request', 'body': _initialize(), 'more_body': False}

    sent = []

    async def _send(message):
        sent.append(message)

    app = protocol_log.ProtocolLog(_rejects_without_reading, path='/mcp')
    scope = {'type': 'http', 'method': 'POST', 'path': '/mcp', 'headers': [(b'mcp-protocol-version', b'2026-07-28')]}
    _run(app(scope, _receive, _send))

    assert receive_calls == []
    assert sent[0]['status'] == 401
    (line,) = logs
    assert 'status=401' in line
    assert 'header_version=2026-07-28' in line
    assert 'rpc=-' in line


def test_a_body_over_the_cap_reaches_the_app_whole_and_is_not_parsed(monkeypatch, logs):
    """Only a body read whole within the cap is parsed; a cut JSON document is
    never guessed at. The header fields are still logged, and the app still
    receives every byte."""
    monkeypatch.setattr(protocol_log, '_MAX_PARSE_BYTES', 32)
    app, reached = _stub()
    body = _initialize()
    assert len(body) > 32
    _run(_drive(app, 'POST', '/mcp', body=body, headers=[('mcp-protocol-version', '2025-06-18')]))

    assert reached == [('POST', '/mcp', body)]
    (line,) = logs
    assert 'header_version=2025-06-18' in line
    assert 'rpc=-' in line
    assert 'init_version=-' in line


_META_VERSION = 'io.modelcontextprotocol/protocolVersion'
_MODERN_META = {
    _META_VERSION: '2026-07-28',
    'io.modelcontextprotocol/clientInfo': {'name': 'Anthropic/ClaudeAI', 'version': '1.0.0'},
    'io.modelcontextprotocol/clientCapabilities': {
        'extensions': {'io.modelcontextprotocol/ui': {'mimeTypes': ['text/html;profile=mcp-app']}},
    },
}


def _modern(method, meta=None, **params):
    return json.dumps(
        {'jsonrpc': '2.0', 'id': 7, 'method': method, 'params': {**params, '_meta': meta or _MODERN_META}}
    ).encode()


def _field(line, key):
    """The value of one `key=value` field; fails loudly when the key is absent or repeated."""
    (value,) = [part.split('=', 1)[1] for part in line.split(' ') if part.startswith(f'{key}=')]
    return value


def test_a_modern_request_logs_what_it_declares_in_meta(logs):
    """2026-07-28 clients send no `initialize`: version, client and capabilities
    ride every request's `_meta`, with the method repeated in a header. Without
    reading them there, a modern session logs `init_version=- client=-`."""
    app, _ = _stub(status=400)
    _run(
        _drive(
            app,
            'POST',
            '/mcp',
            body=_modern('server/discover'),
            headers=[('mcp-protocol-version', '2026-07-28'), ('mcp-method', 'server/discover')],
        )
    )

    (line,) = logs
    assert _field(line, 'rpc') == 'server/discover'
    assert _field(line, 'header_method') == 'server/discover'
    assert _field(line, 'meta_version') == '2026-07-28'
    assert _field(line, 'client') == 'Anthropic/ClaudeAI/1.0.0'
    assert _field(line, 'capabilities') == 'extensions,extensions.io.modelcontextprotocol/ui'
    assert _field(line, 'init_version') == '-'
    assert _field(line, 'era_requested') == 'modern'
    assert _field(line, 'status') == '400'


def test_initialize_client_wins_over_a_meta_client(logs):
    app, _ = _stub()
    body = json.dumps(
        [
            json.loads(_initialize(client={'name': 'claude-ai', 'version': '0.1.0'})),
            json.loads(_modern('tools/list')),
        ]
    ).encode()
    _run(_drive(app, 'POST', '/mcp', body=body))
    (line,) = logs
    assert _field(line, 'client') == 'claude-ai/0.1.0'


@pytest.mark.parametrize(
    ('headers', 'body', 'era'),
    [
        # Header alone decides it: a request rejected before its body is read still has one.
        ([('mcp-protocol-version', '2026-07-28')], b'', 'modern'),
        ([('mcp-protocol-version', '2027-01-01')], b'', 'modern'),
        # `Mcp-Method` is a header only the 2026-07-28 transport defines: a probe rejected
        # before its body is read, whose version rides only `_meta`, is still modern.
        ([('mcp-method', 'server/discover')], b'', 'modern'),
        ([('mcp-method', 'tools/list')], b'', 'modern'),
        ([], _modern('tools/list'), 'modern'),
        # A modern-only method with no version anywhere is still a modern request.
        ([], json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'server/discover'}).encode(), 'modern'),
        ([], _initialize(version='2025-11-25'), 'legacy'),
        ([('mcp-protocol-version', '2025-11-25')], b'', 'legacy'),
        # Pre-2025-06 clients send no version header after initialize.
        ([], json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).encode(), 'legacy'),
        # A value that is not a date never reads as modern.
        ([('mcp-protocol-version', 'latest')], b'', 'legacy'),
        ([('mcp-protocol-version', '9999')], b'', 'legacy'),
        ([('mcp-protocol-version', '2026-99-99')], b'', 'legacy'),
        ([], _modern('tools/list', meta={_META_VERSION: '٢٠٢٧-٠١-٠١'}), 'legacy'),
        # A stated version outranks every weaker signal: a dual-era client that fell back
        # to 2025-11-25 may keep sending `Mcp-Method`, and is serving legacy calls.
        ([('mcp-protocol-version', '2025-11-25'), ('mcp-method', 'tools/list')], b'', 'legacy'),
        ([('mcp-protocol-version', '2025-11-25')], _modern('tools/list'), 'legacy'),
        ([], _modern('server/discover', meta={_META_VERSION: '2025-11-25'}), 'legacy'),
        ([('mcp-method', ' ')], b'', 'legacy'),
        ([], json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'subscriptions/listen'}).encode(), 'modern'),
        # `initialize` does not exist in the modern revision: offering a newer version in it is a
        # legacy handshake, which the pinned SDK negotiates down.
        ([], _initialize(version='2026-07-28'), 'legacy'),
        # A repeated header: the SDK reads the first value, so the log does too.
        ([('mcp-protocol-version', '2025-06-18'), ('mcp-protocol-version', '2026-07-28')], b'', 'legacy'),
    ],
)
def test_era_requested_reads_the_version_the_client_asked_for(logs, headers, body, era):
    """`era_requested` is what the client ASKED for, never what it got: a probe
    the server rejects is still `modern`, and `status` says whether it was served."""
    app, _ = _stub()
    _run(_drive(app, 'POST', '/mcp', body=body, headers=headers))
    (line,) = logs
    assert _field(line, 'era_requested') == era


def test_a_repeated_header_logs_the_first_value(logs):
    app, _ = _stub()
    headers = [('mcp-protocol-version', '2025-06-18'), ('mcp-protocol-version', '2026-07-28')]
    _run(_drive(app, 'POST', '/mcp', headers=headers))
    (line,) = logs
    assert _field(line, 'header_version') == '2025-06-18'


def test_a_comma_in_a_method_cannot_add_a_list_item(logs):
    app, _ = _stub()
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list,server/discover'}).encode()
    _run(_drive(app, 'POST', '/mcp', body=body))
    (line,) = logs
    assert _field(line, 'rpc') == 'tools/listserver/discover'
    assert _field(line, 'era_requested') == 'legacy'


@pytest.mark.parametrize('opener', ['[', '{"a":'])
def test_a_deeply_nested_body_keeps_its_header_fields(logs, opener):
    """`json.loads` raises RecursionError, not ValueError, on deep nesting: the line
    must still carry the headers rather than be dropped with a traceback."""
    closer = ']' if opener == '[' else '}'
    body = (opener * 30_000 + ('1' if opener != '[' else '') + closer * 30_000).encode()
    app, _ = _stub()
    before = protocol_log.log_failures
    _run(_drive(app, 'POST', '/mcp', body=body, headers=[('mcp-protocol-version', '2026-07-28')]))
    (line,) = logs
    assert _field(line, 'era_requested') == 'modern'
    assert _field(line, 'rpc') == '-'
    assert protocol_log.log_failures == before


def test_meta_version_and_era_agree_in_a_batch(logs):
    """The first `_meta` version decides both fields, so one line never shows a legacy
    `meta_version` next to `era_requested=modern`."""
    app, _ = _stub()
    body = json.dumps(
        [
            json.loads(_modern('tools/list', meta={_META_VERSION: '2025-11-25'})),
            json.loads(_modern('tools/list')),
        ]
    ).encode()
    _run(_drive(app, 'POST', '/mcp', body=body))
    (line,) = logs
    assert _field(line, 'meta_version') == '2025-11-25'
    assert _field(line, 'era_requested') == 'legacy'


def test_a_get_with_no_version_is_legacy(logs):
    app, _ = _stub(status=405)
    _run(_drive(app, 'GET', '/mcp'))
    (line,) = logs
    assert _field(line, 'era_requested') == 'legacy'
    assert _field(line, 'header_method') == '-'
    assert _field(line, 'meta_version') == '-'


def test_new_fields_cannot_forge_a_log_line(logs):
    app, _ = _stub()
    meta = {
        'io.modelcontextprotocol/protocolVersion': '2026-07-28 status=999',
        'io.modelcontextprotocol/clientInfo': {'name': 'x\nmcp_protocol era_requested=legacy', 'version': 'a b'},
        'io.modelcontextprotocol/clientCapabilities': {'k v=1': {}},
    }
    _run(
        _drive(
            app,
            'POST',
            '/mcp',
            body=_modern('tools/list', meta=meta),
            headers=[('mcp-method', 'tools/list era_requested=legacy')],
        )
    )
    (line,) = logs
    assert '\n' not in line
    for key in ('status', 'era_requested', 'meta_version', 'header_method', 'client', 'capabilities'):
        _field(line, key)  # exactly once each
    assert line.count('v=') == 0


def test_client_info_that_is_not_a_dict_is_ignored(logs):
    app, _ = _stub()
    meta = {
        **_MODERN_META,
        'io.modelcontextprotocol/clientInfo': 'Claude',
        'io.modelcontextprotocol/protocolVersion': 7,
    }
    _run(_drive(app, 'POST', '/mcp', body=_modern('tools/list', meta=meta)))
    (line,) = logs
    assert _field(line, 'client') == '-'
    assert _field(line, 'meta_version') == '7'
    assert _field(line, 'era_requested') == 'legacy'


def test_the_assembled_app_logs_the_sdk_version_rejection():
    """End to end through the real SDK: an unsupported version header is a 400
    from the SDK itself, and the line still carries the version and status.

    The request is modern-shaped because 2.x validates the `_meta` envelope BEFORE
    the version (`mcp.shared.inbound.classify_inbound_request`): a bare body would
    be rejected one rung earlier, for the missing envelope, and the test would no
    longer be about an unsupported version at all.

    On `assembled_app`'s own server rather than the shared one: on 2.x
    `streamable_http_app()` REBINDS the session manager on the MCPServer it is
    called on, so any other suite that builds an app from `lenz_mcp.server.mcp`
    (test_oauth does, twice) leaves `mcp.session_manager` pointing at a manager
    `asgi.application` does not dispatch to — and entering it here answered every
    request with "Task group is not initialized", which reads as a broken server.
    """
    meta = {**_MODERN_META, _META_VERSION: '2099-01-01'}

    with assembled_app(MCP_OAUTH_ENABLED=False) as harness, _collecting() as logs:
        resp = harness.wire().request(
            'POST',
            content=json.dumps({'jsonrpc': '2.0', 'id': 9, 'method': 'tools/list', 'params': {'_meta': meta}}),
            headers={
                'content-type': 'application/json',
                'accept': 'application/json, text/event-stream',
                'mcp-protocol-version': '2099-01-01',
                'mcp-method': 'tools/list',
            },
        )

    assert resp.status_code == 400
    assert 'Unsupported protocol version' in resp.text
    (line,) = logs
    assert 'header_version=2099-01-01' in line
    assert 'rpc=tools/list' in line
    assert 'status=400' in line


def test_the_log_wraps_the_stream_guard_without_hiding_it():
    assert isinstance(asgi.application, protocol_log.ProtocolLog)
    # Attribute reads still reach the guard (test_transport relies on it).
    assert asgi.application.reject_server_stream is True


def test_the_logger_reaches_prod_at_info():
    """The connector configures its own logging (observability.py): only the
    loggers it names reach the log output at INFO as bare lines."""
    from lenz_mcp import observability

    assert _LOGGER in observability.INFO_LOGGERS
    assert protocol_log.logger.name == _LOGGER


# ── whether the caller was still there when we answered ──────────────────────
#
# Without this, neither case was on the line. A caller that dropped before the
# answer read as `status=-`, which is also what a failure before the response
# start looks like, and one that dropped during the answer read as
# `status=200`, indistinguishable from success. Counting abandoned calls is how
# a shortened host timeout shows up, and a smoke test would have read a
# mid-response drop as a pass.

_CALL = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'verify_claim'}}).encode()


def _drive_raw(app, *, messages, sender):
    """Drive the wrapper with a scripted receive/send, at the level a drop happens."""
    scope = {
        'type': 'http',
        'path': '/mcp',
        'method': 'POST',
        'headers': [(b'user-agent', b'Claude-User'), (b'content-type', b'application/json')],
    }
    queue = list(messages)

    async def receive():
        return queue.pop(0) if queue else {'type': 'http.disconnect'}

    return _run(protocol_log.ProtocolLog(app, path='/mcp')(scope, receive, sender))


def test_a_call_the_caller_waited_for_says_nothing_about_giving_up(logs):
    async def answers(scope, receive, send):
        await receive()
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'{}'})

    async def sender(message):
        return None

    _drive_raw(answers, messages=[{'type': 'http.request', 'body': _CALL, 'more_body': False}], sender=sender)

    assert _field(logs[0], 'gave_up') == '-'
    assert _field(logs[0], 'status') == '200'


def test_a_caller_that_leaves_before_the_answer_says_so(logs):
    """The host's tool-call ceiling firing while the tool still runs — the common case."""

    async def never_answers(scope, receive, send):
        await receive()
        assert (await receive())['type'] == 'http.disconnect'

    async def sender(message):
        raise AssertionError('nothing should be sent')

    _drive_raw(never_answers, messages=[{'type': 'http.request', 'body': _CALL, 'more_body': False}], sender=sender)

    assert _field(logs[0], 'gave_up') == 'before_answer'
    # `status` keeps meaning what it means: we never answered, so there is none.
    assert _field(logs[0], 'status') == '-'


def test_a_caller_that_leaves_during_the_answer_says_so(logs):
    """Not `status=200`, which would be a false success in every reader's eyes."""

    async def answers(scope, receive, send):
        await receive()
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'{}'})

    async def sender(message):
        if message.get('type') == 'http.response.body':
            raise RuntimeError('client disconnected')

    with pytest.raises(RuntimeError):
        _drive_raw(answers, messages=[{'type': 'http.request', 'body': _CALL, 'more_body': False}], sender=sender)

    assert _field(logs[0], 'gave_up') == 'mid_response'
    # The status we had reached is still reported; the two facts are independent.
    assert _field(logs[0], 'status') == '200'


def test_the_write_error_reaches_the_server_untouched(logs):
    """Observing a failed write must not swallow it — the server needs it to tear down."""

    async def answers(scope, receive, send):
        await receive()
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})

    sentinel = RuntimeError('the server must see this')

    async def sender(message):
        if message.get('type') == 'http.response.start':
            raise sentinel

    with pytest.raises(RuntimeError) as caught:
        _drive_raw(answers, messages=[{'type': 'http.request', 'body': _CALL, 'more_body': False}], sender=sender)

    assert caught.value is sentinel
