"""lenz-mcp serves BOTH protocol eras, and serves each client the same thing in either.

One endpoint serves the 2026-07-28 revision beside the 2025 one, and both are
permanent: Claude and ChatGPT probe the new one and fall back to the old, and some
of their surfaces still open 2025-era sessions. So every client-visible behaviour
has two wire shapes and a bug can live in exactly one of them: the tailored
manifest, the client identity the card and the deep-check wait key on, the auth
wall, the prompts, the refusals.

Everything here drives `lenz_mcp.asgi.application` through `src/lenz_mcp/testing.py`
— the assembled app, in the deployed middleware order — because that is the only
place the two eras are actually different. A test against the tool functions would
pass identically in both and prove nothing about either.

The rule this file enforces, and the smoke script checks again against a live
server (`scripts/smoke.py`): never let `server/discover` succeed unless everything it
advertises, and everything a client does next, works.
"""

from __future__ import annotations

import json
import logging
from importlib.metadata import version

import pytest

from lenz_mcp import client as mcp_client
from lenz_mcp.client import ApiResponse
from lenz_mcp.testing import LEGACY, MODERN, MODERN_VERSION, assembled_app, modern_meta

# The package's top-level name, whatever it is called in this checkout: its
# loggers are the server's own.
_PACKAGE_ROOT = mcp_client.__name__.split('.')[0]

SDK_MAJOR = int(version('mcp').split('.')[0])
KEY = 'Bearer lenz_dual_era_key'

# The identities that decide what a client is served. `openai-mcp` alone is a trap:
# the ChatGPT app and its Responses-API connector share the token and get different
# manifests, waits and cards (src/lenz_mcp/client.py::client_identity).
CLAUDE = 'Claude-User/1.0'
CHATGPT_APP = 'openai-mcp/1.0.0'
CHATGPT_CODEX = 'openai-mcp/1.0.0 (Codex)'
CHATGPT_API = 'openai-mcp/1.0.0 (Responses API)'
UNPARSED = 'openai-mcp/1.0.0 (Responses API) proxy/1.0'
CARD_CLIENTS = (CLAUDE, CHATGPT_APP, CHATGPT_CODEX)
NON_CARD_CLIENTS = (CHATGPT_API, UNPARSED, 'claude-code/2.1.4')
ERAS = (LEGACY, MODERN)


@pytest.fixture
def api(monkeypatch):
    """The public API, stubbed at lenz_mcp.client, recording what the MCP sent it.

    `lenz_mcp.client` is deliberately NOT one of the modules `assembled_app`
    re-imports, so a patch here survives the rebuild and the recording is of the
    real outbound call the tools make.
    """
    calls: list[dict] = []

    async def _me_usage(authorization):
        calls.append({'endpoint': '/me/usage', 'authorization': authorization, 'ua': mcp_client._user_agent()})
        return ApiResponse(status=200, data={'plan': 'pro', 'credits': {'remaining': 100}, 'costs': {'verify': 10}})

    monkeypatch.setattr(mcp_client, 'me_usage', _me_usage)
    return calls


@pytest.fixture
def server():
    with assembled_app(MCP_OAUTH_ENABLED=False, MCP_CARD_ENABLED=True) as harness:
        yield harness


@pytest.fixture
def server_no_card():
    with assembled_app(MCP_OAUTH_ENABLED=False, MCP_CARD_ENABLED=False) as harness:
        yield harness


def _wire(harness, user_agent=CLAUDE, era=LEGACY):
    wire = harness.wire(user_agent=user_agent, authorization=KEY)
    if era == LEGACY:
        wire.initialize()
    return wire


def _tools(harness, user_agent, era):
    wire = _wire(harness, user_agent, era)
    return {t['name']: t for t in wire.call('tools/list', era=era, name='tools/list')['tools']}


# ── the two eras serve the same server ───────────────────────────────


@pytest.mark.parametrize('era', ERAS)
def test_a_client_can_list_and_call_in_either_era(server, api, era):
    wire = _wire(server, CLAUDE, era)

    listed = wire.call('tools/list', era=era, name='tools/list')
    assert {'assess_claim', 'verify_claim', 'check_usage'} <= {t['name'] for t in listed['tools']}

    called = wire.call('tools/call', {'name': 'check_usage', 'arguments': {}}, era=era, name='check_usage')
    assert called['structuredContent']['status'] == 'ok'
    assert called.get('isError') is not True
    assert api[-1]['authorization'] == KEY


def test_the_modern_era_answers_discover_with_what_it_can_serve(server):
    """`server/discover` replaces the handshake in the 2026 era. It must advertise
    only what this stateless server can deliver: no listChanged, no subscribe."""
    wire = server.wire(user_agent=CLAUDE, authorization=KEY)
    result = wire.call('server/discover', era=MODERN, name='server/discover')

    assert MODERN_VERSION in result['supportedVersions']
    assert result['resultType'] == 'complete'
    advertised = [
        f'{area}.{flag}'
        for area, block in (result.get('capabilities') or {}).items()
        if isinstance(block, dict)
        for flag in ('listChanged', 'subscribe')
        if block.get(flag) is True
    ]
    assert advertised == [], f'advertises {advertised}, which a stateless server cannot deliver'


def test_everything_discover_advertises_actually_works(server, api):
    """The discover rule as a test. A client that sees a successful discover never falls
    back, so a half-working modern endpoint is worse than none."""
    wire = server.wire(user_agent=CLAUDE, authorization=KEY)
    capabilities = wire.call('server/discover', era=MODERN, name='server/discover')['capabilities']

    checked = set()
    if 'tools' in capabilities:
        tools = wire.call('tools/list', era=MODERN, name='tools/list')['tools']
        assert tools
        result = wire.call('tools/call', {'name': 'check_usage', 'arguments': {}}, era=MODERN, name='check_usage')
        assert result['structuredContent']['status'] == 'ok'
        checked.add('tools')
    if 'prompts' in capabilities:
        prompts = wire.call('prompts/list', era=MODERN, name='prompts/list')['prompts']
        assert prompts
        rendered = wire.call(
            'prompts/get',
            {'name': 'check_text', 'arguments': {'text': 'Water boils at 90 C.'}},
            era=MODERN,
            name='check_text',
        )
        assert rendered['messages']
        checked.add('prompts')
    if 'resources' in capabilities:
        wire.call('resources/list', era=MODERN, name='resources/list')
        checked.add('resources')

    unknown = set(capabilities) - checked - {'experimental'}
    assert not unknown, f'discover advertises {unknown} with no check that it works'


@pytest.mark.parametrize('era', ERAS)
def test_subscriptions_listen_is_refused_not_held_open(server, era):
    """The 2026 replacement for the GET stream. A stateless server on a serverless host
    cannot deliver it, so it must not be served — and `discover` must not advertise
    it (above). Held open, it would end in a truncated body at the request
    timeout, as the GET stream did, on POST."""
    wire = server.wire(user_agent=CLAUDE, authorization=KEY)
    response = wire.rpc('subscriptions/listen', {}, era=era, name='subscriptions/listen')
    assert response.status_code != 200 or 'error' in wire.reply(response)


def test_the_get_stream_is_still_refused(server):
    refused = server.wire().request('GET', '/mcp', headers={'accept': 'text/event-stream'})
    assert refused.status_code == 405


# ── the client identity every gate keys on ───────────────────────────


@pytest.mark.parametrize('era', ERAS)
@pytest.mark.parametrize(
    ('user_agent', 'expected'),
    [
        (CLAUDE, 'lenz-mcp/1.0 (Claude-User/1.0)'),
        (CHATGPT_APP, 'lenz-mcp/1.0 (openai-mcp/1.0.0)'),
        # The PARENTHESES are stripped by client._UA_UNSAFE before the origin goes into
        # our own UA comment: the value is attacker-controlled, and a client that could
        # close the comment could forge the rest of the header. The suffix survives as
        # text, so the API records the client and can still tell the connector apart.
        (CHATGPT_API, 'lenz-mcp/1.0 (openai-mcp/1.0.0 Responses API)'),
        (UNPARSED, 'lenz-mcp/1.0 (openai-mcp/1.0.0 Responses API proxy/1.0)'),
    ],
)
def test_the_outbound_call_carries_the_calling_client(server, api, era, user_agent, expected):
    """The outbound User-Agent is how the API tells first-party clients apart. On 2.x the
    old read (`request_ctx`) is gone; if the replacement fails this silently becomes
    a bare `lenz-mcp/1.0` for every client, and the attribution is lost with it."""
    _wire(server, user_agent, era).call(
        'tools/call', {'name': 'check_usage', 'arguments': {}}, era=era, name='check_usage'
    )
    assert api[-1]['ua'] == expected


@pytest.mark.parametrize('era', ERAS)
def test_a_client_that_sends_no_user_agent_is_not_guessed_at(server, api, era):
    wire = server.wire(user_agent='', authorization=KEY)
    if era == LEGACY:
        wire.initialize()
    wire.call('tools/call', {'name': 'check_usage', 'arguments': {}}, era=era, name='check_usage')
    assert api[-1]['ua'] == 'lenz-mcp/1.0'


@pytest.mark.parametrize('era', ERAS)
@pytest.mark.parametrize(
    ('user_agent', 'expected'),
    [(CLAUDE, 210.0), (CHATGPT_APP, 100.0), (CHATGPT_CODEX, 100.0), (CHATGPT_API, 45.0), (UNPARSED, 45.0)],
)
def test_the_deep_check_wait_resolves_through_the_identity(server, api, monkeypatch, era, user_agent, expected):
    """The MECHANISM, not the shipped numbers: with the table stubbed, whatever it
    holds for an identity is what the wait resolves to. The Responses-API connector
    and an unparsed suffix must NOT inherit the app's row.

    `api` is requested even though nothing here reads it: the helper drives a real
    `check_usage` to get a request in flight, and without the stub all ten rows call
    the configured API for real."""
    from lenz_mcp import config as mcp_config
    from lenz_mcp import server as mcp_server

    monkeypatch.setattr(
        mcp_config,
        'VERIFY_WAIT_SECONDS_BY_USER_AGENT',
        {'Claude-User': 210.0, 'openai-mcp': 100.0, 'openai-mcp (Codex)': 100.0},
    )
    monkeypatch.setattr(mcp_config, 'VERIFY_WAIT_SECONDS', 45.0)
    assert mcp_server is not None  # imported for the resolver the helper calls
    assert _wait_inside_request(server, user_agent, era) == expected


def _wait_inside_request(harness, user_agent, era):
    """Resolve the wait budget while a real request from `user_agent` is in flight."""
    from lenz_mcp import server as mcp_server

    seen: list[float] = []
    original = mcp_server.check_usage

    async def _probe(*args, **kwargs):
        seen.append(mcp_server._verify_wait_seconds())
        return await original(*args, **kwargs)

    mcp_server.mcp._tool_manager._tools['check_usage'].fn = _probe
    try:
        _wire(harness, user_agent, era).call(
            'tools/call', {'name': 'check_usage', 'arguments': {}}, era=era, name='check_usage'
        )
    finally:
        mcp_server.mcp._tool_manager._tools['check_usage'].fn = original
    return seen[-1]


def test_losing_the_client_identity_is_loud_and_counted(monkeypatch):
    """The old read sat inside a swallowed except: on 2.x it would have returned ''
    for every client, and every gate would have read "unknown" in silence. It is an
    ERROR with a stable key and a count, so a log-based alert can catch it.

    Every read is COUNTED; the ERROR is sampled on decades (1st, 10th, 100th) so a
    broken binding neither floods the log nor goes quiet. The count is reset first
    because it is a module global: an earlier test that reaches a tool function with
    no request in scope (the card suite calls several directly) has already moved it
    past a decade, after which this test would read zero records and fail for a
    reason that has nothing to do with the behaviour.
    """
    from lenz_mcp import client

    monkeypatch.setattr(client, 'identity_unresolved_count', 0)
    records = []
    logger = logging.getLogger('lenz_mcp.client')

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collect(level=logging.ERROR)
    logger.addHandler(handler)
    try:
        for _ in range(10):
            assert client.client_user_agent() == ''  # no request in scope at all
    finally:
        logger.removeHandler(handler)

    assert client.identity_unresolved_count == 10, 'every unresolved read must be counted'
    assert [r.levelno for r in records] == [logging.ERROR] * 2, 'the 1st and the 10th, not one per call'
    assert 'mcp_client_identity_unresolved' in records[0].getMessage()
    # The second line is what the first could never be: evidence of SCALE. A count
    # that is only ever read inside the one branch that logs always reads 1, and an
    # operator cannot tell one stray request from every client in the field.
    assert 'count=10' in records[1].getMessage(), records[1].getMessage()


@pytest.mark.parametrize('era', ERAS)
def test_another_middleware_in_front_does_not_lose_the_identity(server, api, era):
    """A deployed server does not run our middleware alone: the SDK's own and
    any tracing or error-reporting middleware sit in the chain too. A probe
    against a bare server proves nothing about that, so put one in front."""
    from lenz_mcp import server as mcp_server

    seen: list[str] = []

    async def _nosy(ctx, call_next):
        seen.append(ctx.method)
        return await call_next(ctx)

    mcp_server.mcp._lowlevel_server.middleware.insert(0, _nosy)
    try:
        _wire(server, CLAUDE, era).call(
            'tools/call', {'name': 'check_usage', 'arguments': {}}, era=era, name='check_usage'
        )
    finally:
        mcp_server.mcp._lowlevel_server.middleware.remove(_nosy)

    assert seen, 'the inserted middleware never ran; the chain is not what this test assumes'
    assert api[-1]['ua'] == f'lenz-mcp/1.0 ({CLAUDE})'


# ── the per-client manifest ──────────────────────────────────────────


@pytest.mark.parametrize('era', ERAS)
@pytest.mark.parametrize('user_agent', CARD_CLIENTS)
def test_a_card_client_gets_the_card_meta_in_either_era(server, era, user_agent):
    from lenz_mcp import mcp_card

    tools = _tools(server, user_agent, era)
    for name in mcp_card.CARD_TOOL_NAMES & set(tools):
        assert tools[name].get('_meta') == dict(mcp_card.CARD_TOOL_META), name
    assert mcp_card.CARD_ONLY_TOOL_NAMES <= set(tools), 'a card client keeps the card-only tools'


@pytest.mark.parametrize('era', ERAS)
@pytest.mark.parametrize('user_agent', NON_CARD_CLIENTS)
def test_a_non_card_client_never_sees_a_card_only_tool(server, era, user_agent):
    """`ui.visibility: ['app']` is a client-honored hint, not server enforcement —
    the Responses-API connector ignores it and lists everything to its model, and
    two of those tools START PAID CHECKS. Stripping is what actually withholds them."""
    from lenz_mcp import mcp_card

    tools = _tools(server, user_agent, era)
    assert not (mcp_card.CARD_ONLY_TOOL_NAMES & set(tools)), f'{user_agent} was served a card-only tool'


@pytest.mark.parametrize('era', ERAS)
def test_with_the_flag_off_nobody_gets_a_card(server_no_card, era):
    from lenz_mcp import mcp_card

    for user_agent in (CLAUDE, CHATGPT_APP):
        tools = _tools(server_no_card, user_agent, era)
        assert not (mcp_card.CARD_ONLY_TOOL_NAMES & set(tools))
        for tool in tools.values():
            assert not (tool.get('_meta') or {}).get('ui'), tool['name']


@pytest.mark.parametrize('era', ERAS)
def test_no_manifest_carries_an_openai_namespaced_key(server, era):
    """After the widget went nothing is OpenAI-specific on the wire. It is a
    property that should hold forever, so it is asserted rather than left to a diff."""
    for user_agent in CARD_CLIENTS + NON_CARD_CLIENTS:
        for tool in _tools(server, user_agent, era).values():
            keys = list(tool.get('_meta') or {})
            assert not [k for k in keys if k.startswith('openai/')], (user_agent, tool['name'], keys)


@pytest.mark.parametrize('era', ERAS)
def test_the_card_resource_is_served_on_read_and_never_listed(server, era):
    from lenz_mcp import mcp_card

    wire = _wire(server, CLAUDE, era)
    listed = wire.call('resources/list', era=era, name='resources/list')
    assert not [r for r in listed['resources'] if mcp_card.is_card_uri(str(r['uri']))]

    read = wire.call('resources/read', {'uri': mcp_card.CARD_URI}, era=era, name=mcp_card.CARD_URI)
    contents = read['contents'][0]
    assert contents['mimeType'] == mcp_card.CARD_MIME_TYPE
    assert contents['text'].lstrip().lower().startswith('<!doctype html>')


# ── prompts, auth, and the answers when something is wrong ───────────


@pytest.mark.parametrize('era', ERAS)
def test_a_prompt_renders_in_either_era(server, era):
    wire = _wire(server, CLAUDE, era)
    rendered = wire.call(
        'prompts/get', {'name': 'check_text', 'arguments': {'text': 'Water boils at 90 C.'}}, era=era, name='check_text'
    )
    assert rendered['messages'][0]['content']['text'].endswith('Water boils at 90 C.')


@pytest.mark.parametrize('era', ERAS)
@pytest.mark.parametrize('arguments', [{}, {'text': ''}, {'text': '   \n\t '}], ids=['omitted', 'empty', 'whitespace'])
def test_a_blank_prompt_argument_tells_the_person_what_to_paste(server, era, caplog, arguments):
    """2.x hides a prompt's own exception text and logs an ERROR traceback for it, so
    the prompts raise MCPError instead: the sentence survives and a blank submission
    is not a Sentry event.

    An argument OMITTED entirely never reaches the prompt — the SDK rejects a missing
    required argument first — so the middleware answers that shape the same way.
    Otherwise the same mistake would read differently depending on whether the person
    cleared the box or never filled it."""
    wire = _wire(server, CLAUDE, era)
    with caplog.at_level(logging.ERROR):
        response = wire.rpc('prompts/get', {'name': 'check_text', 'arguments': arguments}, era=era, name='check_text')
    error = wire.error(response)
    assert 'Paste the text to check.' in error['message']
    # Scoped to the SDK and our own loggers: the harness's event-loop teardown can emit
    # an asyncio ERROR of its own, and the claim here is about what the SERVER logs.
    from_server = [
        r for r in caplog.records if r.levelno >= logging.ERROR and r.name.split('.')[0] in {'mcp', _PACKAGE_ROOT}
    ]
    assert not from_server, [r.getMessage() for r in from_server]


@pytest.mark.parametrize('era', ERAS)
@pytest.mark.parametrize(
    'params',
    [
        {'name': 'check_text', 'arguments': ['text']},
        {'name': 'check_text', 'arguments': 'text'},
        {'name': {'nested': 'object'}, 'arguments': {}},
    ],
    ids=['arguments_a_list', 'arguments_a_string', 'name_an_object'],
)
def test_a_malformed_prompt_request_is_the_sdks_to_refuse(server, era, caplog, params):
    """Our argument guard runs BEFORE the SDK validates the params, so it sees shapes
    the SDK would have rejected. It must not touch them: `arguments` as a list raised
    AttributeError, a non-string `name` raised TypeError on the dict lookup, and both
    were answered `-32603 Internal server error` with an ERROR traceback — strictly
    worse than the `-32602` the SDK gives them, and the Sentry noise the guard exists
    to prevent, moved one layer out."""
    wire = _wire(server, CLAUDE, era)
    with caplog.at_level(logging.ERROR):
        response = wire.rpc('prompts/get', params, era=era, name='check_text')
    error = wire.error(response)
    # Which refusal is the SDK's business (the modern transport rejects a name that
    # does not match its `Mcp-Name` header even earlier). Ours is that it is a
    # refusal, not -32603 — an internal error means we crashed reading the request.
    assert error['code'] != -32603, error
    from_server = [
        r for r in caplog.records if r.levelno >= logging.ERROR and r.name.split('.')[0] in {'mcp', _PACKAGE_ROOT}
    ]
    assert not from_server, [r.getMessage() for r in from_server]


@pytest.mark.parametrize('era', ERAS)
def test_a_resource_uri_cannot_forge_a_log_line(server, era, caplog):
    """The URI is read off the raw params before the SDK validates it, and it goes
    straight into a log line. Unescaped, a newline in it writes a SECOND line that no
    reader can tell from one of ours — `mcp_auth_ok`, say. Every client-controlled
    value on a log line goes through `protocol_log.log_token`."""
    forged = 'ui://lenz/card-v1\nmcp_auth_ok user=1 key=forged'
    wire = _wire(server, CLAUDE, era)
    with caplog.at_level(logging.INFO, logger='lenz_mcp.middleware'):
        wire.rpc('resources/read', {'uri': forged}, era=era, name=forged)
    lines = [r.getMessage() for r in caplog.records if r.name == 'lenz_mcp.middleware']
    assert lines, 'the read was not logged at all'
    assert len(lines) == 1, lines
    logged_uri = lines[0].split('uri=')[1].split(' ')[0]
    # The value survives as ONE token: the characters that would end the field or the
    # record — whitespace, the newline, `=` — are the ones it may not carry.
    assert not any(c.isspace() or c == '=' for c in logged_uri), logged_uri
    assert 'key=forged' not in lines[0], lines[0]


@pytest.mark.parametrize('era', ERAS)
@pytest.mark.parametrize('method', ['tools/list', 'resources/list', 'prompts/get', 'resources/read'])
def test_a_notification_is_not_ours_to_answer_or_to_log(server, era, caplog, method):
    """The middleware chain wraps NOTIFICATIONS too, and nothing we do applies to one.
    `call_next` returns None for a notification, so a tailor logs a failure on it; the
    prompt guard raises into `_on_notify`, which swallows it as a traceback and
    answers nothing. Either way it is an ERROR-log amplifier any client can drive with
    a one-line loop, against an endpoint whose key branch accepts any `lenz_` token."""
    wire = _wire(server, CLAUDE, era)
    with caplog.at_level(logging.ERROR):
        response = wire.rpc(method, {'name': 'check_text', 'uri': 'ui://lenz/card-v1'}, notification=True, era=era)
    assert response.status_code == 202, response.text
    from_server = [
        r for r in caplog.records if r.levelno >= logging.ERROR and r.name.split('.')[0] in {'mcp', _PACKAGE_ROOT}
    ]
    assert not from_server, [r.getMessage() for r in from_server]


def test_a_broken_tailor_still_withholds_the_card_only_tools(monkeypatch, caplog):
    """Tailoring never breaks a call — but "return the list untouched" is not the
    harmless failure it reads as: the card-only tools start PAID checks, and leaving
    them listed to a client that does not render cards is the exact exposure the
    stripping exists to prevent. A failure must fail CLOSED."""
    from lenz_mcp import mcp_card, middleware

    def _boom():
        raise RuntimeError('card gate exploded')

    monkeypatch.setattr(mcp_card, 'card_active', _boom)
    card_only = sorted(mcp_card.CARD_ONLY_TOOL_NAMES)
    assert card_only, 'nothing to withhold — the test would pass vacuously'
    result = {'tools': [{'name': name} for name in [*card_only, 'assess_claim']]}

    with caplog.at_level(logging.ERROR):
        tailored = middleware.tailor_tool_list(result)

    assert [t['name'] for t in tailored['tools']] == ['assess_claim']
    assert any('stripping the card-only tools' in r.getMessage() for r in caplog.records)


def test_a_card_tools_meta_is_never_the_shared_constant(server):
    """`dict(CARD_TOOL_META)` is shallow, so the response would hand a client a
    reference to the nested dict `mcp_card` owns. One future write through it
    corrupts the constant for the life of the process."""
    from lenz_mcp import mcp_card

    tools = _tools(server, CLAUDE, MODERN)
    carded = [t for t in tools.values() if t.get('_meta', {}).get('ui')]
    assert carded, 'no tool carried card meta — the test would pass vacuously'
    for tool in carded:
        assert tool['_meta'] is not mcp_card.CARD_TOOL_META
        assert tool['_meta'] is not mcp_card.CARD_ONLY_TOOL_META
        assert tool['_meta']['ui'] is not mcp_card.CARD_TOOL_META.get('ui')
        assert tool['_meta']['ui'] is not mcp_card.CARD_ONLY_TOOL_META.get('ui')


def test_every_prompt_that_requires_an_argument_is_in_the_map(server):
    """`REQUIRED_PROMPT_ARGUMENTS` is hand-written, and a prompt missing from it
    regresses silently: the SDK answers a blank submission with its own generic
    message and logs an ERROR traceback, which is exactly what the map prevents, and
    no test would fail. So the map is checked against what the server actually
    registers."""
    from lenz_mcp import server as mcp_server

    wire = _wire(server, CLAUDE, MODERN)
    listed = wire.call('prompts/list', {}, era=MODERN)['prompts']
    required = {
        prompt['name']: sorted(a['name'] for a in prompt.get('arguments') or [] if a.get('required'))
        for prompt in listed
    }
    required = {name: args for name, args in required.items() if args}
    assert required, 'no prompt declares a required argument — the map has nothing to guard'
    for name, args in required.items():
        assert name in mcp_server.REQUIRED_PROMPT_ARGUMENTS, (
            f'prompt {name!r} requires {args} but is not in REQUIRED_PROMPT_ARGUMENTS: '
            'a blank submission of it is a generic SDK error and a Sentry traceback'
        )
        assert mcp_server.REQUIRED_PROMPT_ARGUMENTS[name][0] in args


@pytest.mark.parametrize('era', ERAS)
def test_an_unknown_resource_is_refused_and_logged(server, era, caplog):
    wire = _wire(server, CLAUDE, era)
    with caplog.at_level(logging.WARNING, logger='lenz_mcp.widget_resource'):
        response = wire.rpc('resources/read', {'uri': 'ui://lenz/card-v999'}, era=era, name='ui://lenz/card-v999')
    assert 'error' in wire.reply(response)
    assert any('mcp_resource_read_failed' in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize('era', ERAS)
def test_the_auth_wall_answers_a_keyless_client_with_the_oauth_challenge(era):
    """With OAuth on, a keyless request gets the 401 + WWW-Authenticate that starts
    discovery — in BOTH eras, since a modern client probes before it authenticates."""
    with assembled_app(
        MCP_OAUTH_ENABLED=True,
        WORKOS_AUTHKIT_DOMAIN='dual-era.authkit.app',
        MCP_SERVICE_SIGNING_KEYS='dual-era-signing-key-' + 'k' * 32,
    ) as harness:
        wire = harness.wire(user_agent=CLAUDE)
        method = 'tools/list' if era == LEGACY else 'server/discover'
        response = wire.rpc(method, era=era, name=method)

    assert response.status_code == 401
    assert 'oauth-protected-resource' in response.headers.get('www-authenticate', '')


@pytest.mark.parametrize('era', ERAS)
def test_an_api_key_is_served_with_oauth_on(api, era):
    """`validate_token_resource` must be False: our API-key branch returns a token
    with no `resource`, and under True the SDK 401s every one of them."""
    with assembled_app(
        MCP_OAUTH_ENABLED=True,
        WORKOS_AUTHKIT_DOMAIN='dual-era.authkit.app',
        MCP_SERVICE_SIGNING_KEYS='dual-era-signing-key-' + 'k' * 32,
    ) as harness:
        wire = _wire(harness, CLAUDE, era)
        result = wire.call('tools/call', {'name': 'check_usage', 'arguments': {}}, era=era, name='check_usage')

    assert result['structuredContent']['status'] == 'ok'
    assert api[-1]['authorization'] == KEY


def test_the_server_reports_a_version(server):
    """2.x defaults `version` to '', which would publish an empty `serverInfo.version`
    to every client and directory listing."""
    wire = server.wire(user_agent=CLAUDE)
    negotiated = wire.initialize()
    assert negotiated['serverInfo']['version']

    discovered = server.wire(user_agent=CLAUDE, authorization=KEY).call(
        'server/discover', era=MODERN, name='server/discover'
    )
    assert discovered['_meta']['io.modelcontextprotocol/serverInfo']['version']


def test_a_long_call_is_kept_alive_while_the_deep_check_runs(server, api, monkeypatch):
    """The deep-check wait holds one call open for minutes (Claude's is 130 s, sized
    under its measured modern-path ceiling). On the modern path the transport commits an SSE stream and pings while
    the handler runs, so a proxy idle-read timeout cannot cut a wait that is working.

    Driven at the ASGI layer on purpose: httpx buffers the whole response, so the
    same test through the harness's client would pass whether the pings existed or not.
    """
    import asyncio

    from mcp.server import _streamable_http_modern as modern

    from lenz_mcp import server as mcp_server

    monkeypatch.setattr(modern, '_SSE_PING_INTERVAL', 0.05)
    tool = mcp_server.mcp._tool_manager._tools['check_usage']
    original = tool.fn

    async def _go():
        release = asyncio.Event()

        async def _slow(*args, **kwargs):
            await release.wait()  # stands in for a deep check still running
            return await original(*args, **kwargs)

        tool.fn = _slow
        sent: list[dict] = []
        body = json.dumps(
            {
                'jsonrpc': '2.0',
                'id': 'ping-probe',
                'method': 'tools/call',
                'params': {'name': 'check_usage', 'arguments': {}, '_meta': modern_meta('pinger')},
            }
        ).encode()

        delivered = asyncio.Event()

        async def _receive():
            # The body once, then BLOCK — as a real ASGI server does. Returning the
            # same message again spins the transport's disconnect watcher forever
            # (it loops until it sees `http.disconnect`), and the test hangs instead
            # of failing.
            if delivered.is_set():
                await asyncio.Event().wait()
            delivered.set()
            return {'type': 'http.request', 'body': body, 'more_body': False}

        async def _send(message):
            sent.append(message)
            if message.get('type') == 'http.response.body' and b'ping' in message.get('body', b''):
                release.set()  # the stream is alive: let the call finish

        scope = {
            'type': 'http',
            'method': 'POST',
            'path': '/mcp',
            'headers': [
                (b'content-type', b'application/json'),
                (b'accept', b'application/json, text/event-stream'),
                (b'authorization', KEY.encode()),
                (b'user-agent', CLAUDE.encode()),
                (b'mcp-protocol-version', MODERN_VERSION.encode()),
                (b'mcp-method', b'tools/call'),
                (b'mcp-name', b'check_usage'),
            ],
        }
        try:
            await server.app(scope, _receive, _send)
        finally:
            tool.fn = original
        return sent

    sent = server.run(_go(), timeout=20)

    start = next(m for m in sent if m['type'] == 'http.response.start')
    content_type = dict((k.decode(), v.decode()) for k, v in start['headers'])['content-type']
    assert content_type.startswith('text/event-stream')

    bodies = [m.get('body', b'') for m in sent if m['type'] == 'http.response.body']
    assert any(b'ping' in b for b in bodies), 'nothing was sent while the handler ran: a long wait can be cut'
    first_ping = next(i for i, b in enumerate(bodies) if b'ping' in b)
    first_result = next(i for i, b in enumerate(bodies) if b'"result"' in b)
    assert first_ping < first_result, 'the result arrived before any keepalive: the stream was never kept alive'
