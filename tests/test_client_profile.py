"""The one resolved client profile, the identity registry, and the decision log.

Three per-client decisions used to re-derive the client from a bare User-Agent
ContextVar, in three modules, all keyed on the same string. On 2026-09-23
OpenAI shipped a ChatGPT build sending `openai-mcp/1.0.0 (ChatGPT)` and all
three stopped matching at once, silently. What this file pins is the structure
that replaced it:

- ONE profile, bound per request by the middleware, reset on every exit path,
  from which each decision reads the narrowest field that answers it;
- ONE registry of measured identities, which every table is generated from or
  checked against, so an identity can never again be known to one and unknown
  to another;
- a log line per decision, so the next time one moves, something says so.

The decisions themselves are tested where they live: the card in
`test_card.py`, the wait in `test_first_check.py`, both eras end to end in
`test_dual_era.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest

from lenz_mcp import client, config, decisions, mcp_card, middleware
from lenz_mcp.testing import DECLARES_APPS, DECLARES_NO_APPS, MODERN, assembled_app

CARD_TOOLS = {'start_verification_widget', 'get_verification_widget', 'select_claims_widget'}


# ── the profile is resolved once, from the request ───────────────────


class _Session:
    def __init__(self, capabilities=None, client_params=None):
        self.client_capabilities = capabilities
        self.client_params = client_params


class _Ctx:
    """A middleware context shaped like the SDK's `ServerRequestContext`."""

    def __init__(self, *, user_agent='', capabilities=None, protocol_version='2026-07-28', client_params=None):
        self.request = type('R', (), {'headers': {'user-agent': user_agent}})()
        self.session = _Session(capabilities, client_params)
        self.protocol_version = protocol_version
        self.method = 'tools/list'
        self.request_id = 1
        self.params = {}


def _bind(**kwargs):
    reset = middleware.bind_client_profile(_Ctx(**kwargs))
    return client.client_profile(), reset


def test_a_modern_request_that_declares_the_extension_is_read_as_declaring():
    from mcp_types import ClientCapabilities

    caps = ClientCapabilities.model_validate(DECLARES_APPS)
    profile, reset = _bind(user_agent='openai-mcp/1.0.0 (ChatGPT)', capabilities=caps)
    try:
        assert profile.declares_apps is True
        assert profile.declaration_source == client.DECLARATION_FROM_REQUEST
        assert profile.declared == 'yes'
        assert profile.era == client.ERA_MODERN
        assert profile.identity == 'openai-mcp (ChatGPT)'
        assert profile.vendor_token == 'openai-mcp'
    finally:
        reset()


def test_a_modern_request_that_declares_other_capabilities_is_read_as_not_declaring():
    from mcp_types import ClientCapabilities

    caps = ClientCapabilities.model_validate(DECLARES_NO_APPS)
    profile, reset = _bind(user_agent='Claude-User', capabilities=caps)
    try:
        assert profile.declares_apps is False
        assert profile.declaration_source == client.DECLARATION_FROM_REQUEST
        assert profile.declared == 'no'
    finally:
        reset()


def test_a_request_with_no_capabilities_at_all_is_absent_not_false():
    """The 2025 era, every request — and the difference is the whole design.

    The handshake declares capabilities once and this server is stateless, so
    `ctx.session.client_capabilities` is None on every in-session legacy
    request (measured against mcp 2.2.0: on the legacy `initialize` too, since
    the connection records them after middleware has run). The SDK's predicate
    answers False for that, which would read as "this client positively cannot
    render a card" and take the card away from the entire 2025 era in silence.
    """
    profile, reset = _bind(user_agent='Claude-User', capabilities=None, protocol_version='2025-11-25')
    try:
        assert profile.declares_apps is None
        assert profile.declaration_source == client.DECLARATION_ABSENT
        assert profile.declared == 'none'
        assert profile.era == client.ERA_LEGACY
        assert mcp_card.card_decision(profile) == (False, mcp_card.REASON_FLAG_OFF), 'the flag is off by default'
    finally:
        reset()


def test_a_capability_read_that_raises_falls_back_to_today_and_is_loud(monkeypatch, caplog):
    """A failed read is NEVER silently worse than the answer we gave before it.

    It falls to the vendor token — which is what decided before a declaration
    was read at all — and it says so at ERROR, because unlike an absent
    declaration this one is ours to fix.
    """

    class _Exploding:
        @property
        def client_capabilities(self):
            raise RuntimeError('capabilities exploded')

    ctx = _Ctx(user_agent='Claude-User')
    ctx.session = _Exploding()
    with caplog.at_level(logging.ERROR):
        reset = middleware.bind_client_profile(ctx)
    try:
        profile = client.client_profile()
        assert profile.declares_apps is None
        assert profile.declaration_source == client.DECLARATION_READ_FAILED
        assert profile.declared == 'read_failed'
        monkeypatch.setattr(config, 'CARD_ENABLED', True)
        assert mcp_card.card_decision(profile) == (True, mcp_card.REASON_READ_FAILED_TOKEN)
    finally:
        reset()
    assert any('declaration could not be read' in r.getMessage() for r in caplog.records)


def test_nothing_bound_reads_as_unknown_and_is_counted(monkeypatch):
    """No profile in scope wins nothing, and is reported rather than guessed at."""
    monkeypatch.setattr(client, 'identity_unresolved_count', 0)
    profile = client.client_profile()
    assert profile == client.UNKNOWN_PROFILE
    assert profile.identity == '' and profile.vendor_token == ''
    assert profile.declares_apps is None
    assert client.identity_unresolved_count == 1


# ── the profile never leaks between requests ─────────────────────────


def test_the_profile_is_reset_even_when_the_handler_raises():
    async def _boom(ctx):
        raise RuntimeError('handler exploded')

    ctx = _Ctx(user_agent='Claude-User')
    with pytest.raises(RuntimeError):
        asyncio.run(middleware.lenz_middleware(ctx, _boom))
    # Not merely "not Claude": nothing at all is bound, so the next request
    # cannot read a stale profile and the unresolved read is reported.
    with pytest.raises(LookupError):
        client._INBOUND_PROFILE.get()


def test_two_interleaved_requests_never_read_each_others_profile():
    """Each request resolves its own profile, in its own context.

    A ContextVar set in a task is invisible to a sibling task, which is what
    makes one profile per request safe on an async server — but only while the
    binding really is per request and really is reset. Asserted by running two
    requests that overlap: each reads its own client WHILE the other is bound.
    """
    seen: dict[str, list[str]] = {'a': [], 'b': []}

    async def _handler(ctx, gate, other, name):
        async def _inner(_ctx):
            seen[name].append(client.client_identity())
            gate.set()
            await other.wait()
            seen[name].append(client.client_identity())
            return {}

        return await middleware.lenz_middleware(ctx, _inner)

    async def _go():
        a_ready, b_ready = asyncio.Event(), asyncio.Event()
        await asyncio.gather(
            _handler(_Ctx(user_agent='Claude-User'), a_ready, b_ready, 'a'),
            _handler(_Ctx(user_agent='openai-mcp/1.0.0 (Codex)'), b_ready, a_ready, 'b'),
        )

    asyncio.run(_go())
    assert seen['a'] == ['Claude-User', 'Claude-User']
    assert seen['b'] == ['openai-mcp (Codex)', 'openai-mcp (Codex)']


# ── the registry is the one place an identity is spelled ─────────────


def test_every_wait_row_names_a_measured_identity():
    """The half-fix that caused this whole change, closed.

    `openai-mcp (ChatGPT)` was known to the card's tables and unknown to the
    wait's. Now the wait's keys must all be registry members, so an identity
    cannot be half-added.
    """
    stray = set(config.VERIFY_WAIT_SECONDS_BY_IDENTITY) - set(client.KNOWN_IDENTITIES)
    assert not stray, f'wait rows for identities no registry entry describes: {sorted(stray)}'


def test_every_measured_identity_has_a_wait_row():
    """And the other direction: a measured identity with no row silently gets
    the short default. That is a legitimate CHOICE — OpenAI's Responses-API
    connector is at the default on purpose — but it has to be written down as
    a row, not left as an omission nobody can tell from a mistake."""
    missing = set(client.KNOWN_IDENTITIES) - set(config.VERIFY_WAIT_SECONDS_BY_IDENTITY)
    assert not missing, f'measured identities with no wait row: {sorted(missing)}'


def test_the_registry_describes_what_it_measured():
    for identity, entry in client.KNOWN_IDENTITIES.items():
        assert entry.identity == identity, 'the key and the entry must name the same identity'
        assert client.parse_identity(f'{entry.vendor}/1.0') == entry.vendor, entry.vendor
        assert entry.surface and entry.measured and entry.source, identity
        # An identity is its vendor, or its vendor plus one suffix — the
        # shape `parse_identity` produces, so the registry cannot hold a
        # string no request could ever resolve to.
        assert identity == entry.vendor or identity.startswith(f'{entry.vendor} ('), identity
        assert client.parse_identity(identity) == identity, 'a registry key must be its own parse'


def test_the_chatgpt_apps_new_suffix_is_a_first_class_identity():
    """The instance this release exists for, named.

    A build of the ChatGPT app began sending `openai-mcp/1.0.0 (ChatGPT)` on
    2026-09-23. It must resolve to its own identity, carry the app's wait
    rather than the 45 s default, and — through its declaration, not its
    User-Agent — be served the card.
    """
    identity = client.parse_identity('openai-mcp/1.0.0 (ChatGPT)')
    assert identity == 'openai-mcp (ChatGPT)'
    assert identity in client.KNOWN_IDENTITIES
    assert config.verify_wait_seconds(identity) == config.VERIFY_WAIT_SECONDS_BY_IDENTITY['openai-mcp']
    assert config.verify_wait_seconds(identity) != config.VERIFY_WAIT_SECONDS


def test_the_probe_server_carries_the_same_delivery_rule():
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / 'scripts' / 'probe' / 'server.py'
    spec = importlib.util.spec_from_file_location('lenz_probe_server_rule', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        assert module.MESSAGE_DELIVERY_VENDOR_TOKENS == mcp_card.MESSAGE_DELIVERY_VENDOR_TOKENS
    finally:
        sys.modules.pop(spec.name, None)


# ── every decision says so ───────────────────────────────────────────


@contextlib.contextmanager
def _collecting():
    """Messages on the decisions logger, collected AT the logger itself.

    Not `caplog`: `lenz_mcp.decisions` is in `observability.INFO_LOGGERS` and
    so is configured `propagate: False`, as the deployment needs. Nothing
    reaches caplog's root handler, and every assertion below would see an empty
    list for the wrong reason. Same shape as test_protocol_log and test_oauth.

    A context manager rather than a fixture so a test can attach it INSIDE an
    `assembled_app` block: reconfiguring logging there rebuilds this logger's
    handler list and would silently drop a collector attached earlier.
    """
    logger = logging.getLogger('lenz_mcp.decisions')
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


def _lines(messages, prefix):
    return [m for m in messages if m.startswith(prefix)]


def _fields(line):
    return dict(part.split('=', 1) for part in line.split(' ')[1:])


def test_the_manifest_decision_is_logged_with_its_reason():
    with assembled_app(MCP_OAUTH_ENABLED=False, MCP_CARD_ENABLED=True) as harness, _collecting() as messages:
        wire = harness.wire(user_agent='openai-mcp/1.0.0 (ChatGPT)', capabilities=DECLARES_APPS)
        wire.call('tools/list', era=MODERN, name='tools/list', client_name='chatgpt')
    lines = _lines(messages, 'mcp_manifest ')
    assert len(lines) == 1, lines
    fields = _fields(lines[0])
    assert fields['identity'] == 'openai-mcp(ChatGPT)', 'log_token strips the space, and must stay reversible by eye'
    assert fields['client'] == 'chatgpt'
    assert fields['era'] == 'modern'
    assert fields['declared'] == 'yes'
    assert fields['vendor'] == 'openai-mcp'
    assert fields['card'] == 'on'
    assert fields['reason'] == mcp_card.REASON_DECLARED
    assert fields['wait'] == '100'


def test_the_manifest_line_says_which_branch_closed_the_card():
    """`card=off` alone cannot tell a client that told us it renders no cards
    from one whose row somebody forgot. The reason is what the host watch
    reads, so it is asserted per branch."""
    with assembled_app(MCP_OAUTH_ENABLED=False, MCP_CARD_ENABLED=True) as harness, _collecting() as messages:
        wire = harness.wire(user_agent='Claude-User', capabilities=DECLARES_NO_APPS)
        wire.call('tools/list', era=MODERN, name='tools/list', client_name='claude-code')
    fields = _fields(_lines(messages, 'mcp_manifest ')[0])
    assert (fields['client'], fields['card'], fields['reason']) == ('claude-code', 'off', 'no_declaration')


def test_the_legacy_era_logs_the_token_fallback():
    with assembled_app(MCP_OAUTH_ENABLED=False, MCP_CARD_ENABLED=True) as harness, _collecting() as messages:
        wire = harness.wire(user_agent='Claude-User')
        wire.initialize()
        wire.call('tools/list', name='tools/list')
    fields = _fields(_lines(messages, 'mcp_manifest ')[0])
    assert (fields['era'], fields['declared'], fields['card'], fields['reason']) == (
        'legacy',
        'none',
        'on',
        mcp_card.REASON_LEGACY_TOKEN,
    )


def test_the_card_delivery_decision_is_logged():
    reset = client.bind_client_user_agent('openai-mcp/1.0.0 (ChatGPT)')
    try:
        with _collecting() as messages:
            stamped = mcp_card.with_delivery({'status': 'ok'})
    finally:
        reset()
    assert stamped['_card'] == {'deliver': 'message'}
    fields = _fields(_lines(messages, 'mcp_card_delivery ')[0])
    assert fields == {'identity': 'openai-mcp(ChatGPT)', 'deliver': 'message'}


def test_an_exhausted_wait_is_logged_with_the_tool_that_spent_it():
    reset = client.bind_client_user_agent('openai-mcp/1.0.0 (ChatGPT)')
    try:
        with _collecting() as messages:
            decisions.log_wait_exhausted(wait=45.0, tool='verify_claim')
    finally:
        reset()
    fields = _fields(_lines(messages, 'mcp_verify_wait_exhausted ')[0])
    assert fields == {'identity': 'openai-mcp(ChatGPT)', 'wait': '45', 'tool': 'verify_claim'}


def test_no_decision_line_can_carry_a_raw_client_value():
    """Every logged value goes through `log_token`, so a client cannot forge a
    field or a second record. A User-Agent with a newline, a space and an `=`
    in it is the test case."""
    hostile = 'evil/1.0 (a=b\nmcp_manifest identity=Claude-User card=on)'
    reset = client.bind_client_user_agent(hostile)
    try:
        with _collecting() as messages:
            mcp_card.with_delivery({})
            decisions.log_wait_exhausted(wait=45.0, tool='verify_claim')
    finally:
        reset()
    assert messages, 'nothing was logged — the test would pass vacuously'
    for line in _lines(messages, 'mcp_'):
        assert '\n' not in line, line
        assert len(line.split(' ')) == len(_fields(line)) + 1, f'a value forged a field: {line}'


def test_the_decisions_logger_is_configured_to_emit():
    """A module whose success signal is an INFO line and no entry here emits
    nothing at all in production, where the floor for app loggers is WARNING.
    The host watch lost its first prod seed to exactly this."""
    from lenz_mcp import observability

    assert decisions.logger.name in observability.INFO_LOGGERS


def test_a_broken_decision_line_never_costs_a_client_its_manifest(monkeypatch, caplog):
    def _boom(*args, **kwargs):
        raise RuntimeError('logging exploded')

    monkeypatch.setattr(decisions, 'log_manifest', _boom)
    result = {'tools': [{'name': 'assess_claim'}]}
    with caplog.at_level(logging.ERROR):
        assert middleware.log_manifest_decision(result) is result
    assert any('could not be logged' in r.getMessage() for r in caplog.records)
