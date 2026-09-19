"""The OAuth token exchange (src/lenz_mcp/exchange.py) and its wiring into the tools.

The tools are driven directly, as in test_server.py, with the Lenz API and its
token endpoint played by one ``httpx.MockTransport``. Time is a fake clock:
the deep-check wait advances it instead of sleeping, so a 130 s wait runs in
milliseconds and the cache's expiry arithmetic is exact.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import types
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import httpx
import pytest
from mcp.server.auth.provider import AccessToken

from lenz_mcp import client, config, exchange, oauth, server

ORIGIN = 'https://lenz.test'
API = f'{ORIGIN}/api/v1'
TOKEN_ENDPOINT = f'{API}/oauth/token'
CLIENT_ID = 'lenz-mcp-test-client'
CLIENT_SECRET = 'test-client-secret'  # noqa: S105 (a test fixture)
SUBJECT = 'subject-token-for-user-42'
CLAUDE = 'Claude-User/1.0'
# The API's own numbers: an exchanged token lives at most 10 minutes, and at
# least one tool call's worth (the longest wait plus a margin) even when the
# subject token is about to expire.
TOKEN_MAX_S = 600
TOKEN_FLOOR_S = 160


# ── the fake API ─────────────────────────────────────────────────────


@dataclass
class FakeLenz:
    """The token endpoint and the few API routes the tools call, on a fake clock."""

    now: float = 1000.0
    subject_expires_at: float = 1000.0 + 3600
    token_error: tuple[int, dict, dict] | None = None  # (status, body, headers) for every exchange
    token_error_after: int = 0  # how many exchanges succeed before token_error applies
    token_raises: bool = False
    run_done_at: float = 1000.0 + 125  # when the deep check completes
    reject_tokens: set[str] = field(default_factory=set)  # issued tokens the API now refuses
    reject_all_tokens: bool = False
    revoke_at: float | None = None  # from this time, every token issued so far is refused
    exchanges: list[dict] = field(default_factory=list)
    api_calls: list[tuple[str, str, str]] = field(default_factory=list)  # (method, path, authorization)
    issued: dict[str, tuple[float, frozenset[str]]] = field(default_factory=dict)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_ENDPOINT:
            return self._token(request)
        return self._api(request)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.exchanges.append({'form': form, 'authorization': request.headers.get('authorization', '')})
        if self.token_raises:
            raise httpx.ConnectError('connection refused', request=request)
        if self.token_error is not None and len(self.exchanges) > self.token_error_after:
            status, body, headers = self.token_error
            return httpx.Response(status, json=body, headers=headers)
        if self.now > self.subject_expires_at:
            return httpx.Response(400, json={'error': 'invalid_grant'})
        expires_at = min(self.now + TOKEN_MAX_S, max(self.subject_expires_at, self.now + TOKEN_FLOOR_S))
        token = f'lat_{len(self.issued):04d}'
        scopes = frozenset(form.get('scope', '').split())
        self.issued[token] = (expires_at, scopes)
        return httpx.Response(
            200,
            json={
                'access_token': token,
                'token_type': 'Bearer',
                'expires_in': int(expires_at - self.now),
                'scope': ' '.join(sorted(scopes)),
                'issued_token_type': exchange.ACCESS_TOKEN_TYPE,
            },
        )

    def _accepts(self, authorization: str) -> bool:
        token = authorization.removeprefix('Bearer ')
        if self.reject_all_tokens or token in self.reject_tokens or token not in self.issued:
            return False
        if self.revoke_at is not None and self.now >= self.revoke_at:
            self.reject_tokens.update(self.issued)
            self.revoke_at = None
            return False
        return self.now < self.issued[token][0]

    def _api(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix('/api/v1')
        authorization = request.headers.get('authorization', '')
        self.api_calls.append((request.method, path, authorization))
        if not self._accepts(authorization):
            return httpx.Response(401, json={'detail': 'Unauthorized'})
        if request.method == 'POST' and path == '/verify':
            return httpx.Response(202, json={'task_id': 'task-1', 'status': 'processing'})
        if path == '/verify/status/task-1':
            if self.now < self.run_done_at:
                return httpx.Response(200, json={'status': 'processing', 'progress': {'step': 'Research...'}})
            return httpx.Response(
                200,
                json={
                    'status': 'completed',
                    'result': {
                        'verification_id': 'pub12345',
                        'claim': 'the claim',
                        'verdict': 'True',
                        'lenz_score': 9,
                        'confidence': 'high',
                        'key_finding': 'It holds.',
                        'sources': [],
                    },
                },
            )
        if request.method == 'POST' and path == '/verify/task-0/select':
            return httpx.Response(
                200, json={'batch_id': 'batch-1', 'items': [{'task_id': 'task-1', 'claim_text': 'the claim'}]}
            )
        if path == '/me/usage':
            return httpx.Response(200, json={'plan': 'free', 'credits': {'remaining': 100}})
        if path == '/assess':
            return httpx.Response(200, json={'results': [{'claim': 'x', 'verdict': 'True'}]})
        return httpx.Response(404, json={'detail': 'Not found'})

    def exchange_count(self) -> int:
        return len(self.exchanges)


@pytest.fixture
def lenz(monkeypatch):
    fake = FakeLenz()
    transport = httpx.MockTransport(fake.handle)
    exchange.reset()
    monkeypatch.setattr(client, '_http_client', httpx.AsyncClient(transport=transport))
    monkeypatch.setattr(exchange, '_http_client', httpx.AsyncClient(transport=transport))

    monkeypatch.setattr(config, 'FRONTEND_URL', ORIGIN)
    monkeypatch.setattr(config, 'API_BASE_URL', API)
    monkeypatch.setattr(config, 'API_CREDENTIALS_URL', f'{ORIGIN}/api-credentials')
    monkeypatch.setattr(config, 'OAUTH_ENABLED', True)
    monkeypatch.setattr(config, 'OAUTH_CLIENT_ID', CLIENT_ID)
    monkeypatch.setattr(config, 'OAUTH_CLIENT_SECRET', CLIENT_SECRET)
    monkeypatch.setattr(config, 'TOKEN_ENDPOINT', TOKEN_ENDPOINT)
    monkeypatch.setattr(config, 'API_RESOURCE', API)

    # One fake clock for the wait, the cache and the API.
    monkeypatch.setattr(exchange, '_now', lambda: fake.now)
    monkeypatch.setattr(server, 'time', types.SimpleNamespace(monotonic=lambda: fake.now))

    async def _advance(seconds):
        fake.now += seconds

    monkeypatch.setattr(server, '_sleep', _advance)
    _signed_in(monkeypatch, SUBJECT)
    reset_ua = client.bind_client_user_agent(CLAUDE)
    yield fake
    reset_ua()
    exchange.reset()


def _signed_in(monkeypatch, subject_token: str, user: str = '42') -> None:
    token = AccessToken(
        token=subject_token,
        client_id='oauth-host',
        scopes=[],
        expires_at=None,
        subject=user,
        claims={'auth_mode': oauth.AUTH_MODE_OAUTH, 'sub': user},
    )
    monkeypatch.setattr('mcp.server.auth.middleware.auth_context.get_access_token', lambda: token)


def _ctx():
    request = types.SimpleNamespace(headers={})
    return types.SimpleNamespace(request_context=types.SimpleNamespace(request=request))


def _run(coro):
    return asyncio.run(coro)


def _status_polls(fake: FakeLenz) -> int:
    return sum(1 for method, path, _ in fake.api_calls if path.startswith('/verify/status/'))


# ── one exchange per tool call, not per poll ─────────────────────────


def test_a_long_deep_check_wait_exchanges_once(lenz):
    """Claude's 130 s wait polls about every 3 s: that must be ONE exchange, not
    one per poll."""
    out = _run(server.verify_claim('the claim', _ctx()))

    assert out['status'] == 'completed'
    assert _status_polls(lenz) > 40
    assert lenz.exchange_count() == 1
    tokens = {authorization for _, _, authorization in lenz.api_calls}
    assert tokens == {'Bearer lat_0000'}, 'every request of the call carries the same exchanged token'


def test_a_wait_that_runs_out_still_exchanged_once(lenz):
    lenz.run_done_at = lenz.now + 10_000
    out = _run(server.verify_claim('the claim', _ctx()))

    assert out['status'] == 'submitted'
    assert lenz.now - 1000.0 >= 130
    assert lenz.exchange_count() == 1


def test_the_next_call_reuses_the_cached_token(lenz):
    _run(server.check_usage(_ctx()))
    _run(server.check_usage(_ctx()))
    assert lenz.exchange_count() == 1

    # Another scope set is another grant of the API's: its own exchange.
    _run(server.assess_claim('the claim', _ctx()))
    assert lenz.exchange_count() == 2
    assert lenz.exchanges[1]['form']['scope'] == 'assess'


def test_a_new_subject_token_never_reuses_the_old_ones_entry(lenz, monkeypatch):
    _run(server.check_usage(_ctx()))
    _signed_in(monkeypatch, 'a-different-subject-token')
    _run(server.check_usage(_ctx()))
    assert lenz.exchange_count() == 2


def test_the_cache_drops_a_token_60_seconds_before_it_expires(lenz):
    _run(server.check_usage(_ctx()))  # a 600 s token
    lenz.now += TOKEN_MAX_S - 61
    _run(server.check_usage(_ctx()))
    assert lenz.exchange_count() == 1, 'still more than the margin left: served from the cache'

    lenz.now += 1  # exactly the margin left
    _run(server.check_usage(_ctx()))
    assert lenz.exchange_count() == 2


# ── a subject token that expires during the wait ─────────────────────


def test_a_subject_expiring_mid_wait_still_finishes_the_call(lenz):
    """The API issues at least one tool call's worth of life, and the call keeps
    the token it started with, so a subject token 20 s from expiry neither
    orphans the running check nor re-exchanges against a dead subject."""
    lenz.subject_expires_at = lenz.now + 20
    out = _run(server.verify_claim('the claim', _ctx()))

    assert out['status'] == 'completed'
    assert lenz.exchange_count() == 1


def test_a_refused_token_on_an_expired_subject_ends_in_reauth_not_a_hang(lenz):
    """The token is refused mid-wait (revoked), and the one re-exchange finds
    the subject expired: the call ends at once in the host's re-authentication."""
    lenz.subject_expires_at = lenz.now + 20
    lenz.revoke_at = lenz.now + 60
    out = _run(server.verify_claim('the claim', _ctx()))

    assert out['status'] == 'auth_required'
    assert lenz.exchange_count() == 2
    assert lenz.exchanges[1]['form']['subject_token'] == SUBJECT
    assert lenz.now - 1000.0 < 65, 'no polling after the re-exchange failed'
    # The submission landed and was charged: the id it produced must come back
    # with the failure, or the run is lost to the user.
    assert out['task_id'] == 'task-1'
    assert out['message'].startswith(server.OAUTH_REAUTH_MESSAGE), 'the reconnect copy, then how to collect'
    assert 'get_verification' in out['message']


# ── a 401 on an exchanged token: evict, exchange again once ──────────


def test_a_401_evicts_the_token_and_exchanges_once_more(lenz):
    _run(server.check_usage(_ctx()))
    lenz.reject_tokens.add('lat_0000')

    out = _run(server.check_usage(_ctx()))

    assert out['status'] == 'ok'
    assert lenz.exchange_count() == 2
    assert [a for m, p, a in lenz.api_calls[-2:]] == ['Bearer lat_0000', 'Bearer lat_0001']

    # The replacement is what the cache now holds.
    _run(server.check_usage(_ctx()))
    assert lenz.exchange_count() == 2
    assert lenz.api_calls[-1][2] == 'Bearer lat_0001'


def test_a_second_401_gives_up(lenz):
    lenz.reject_all_tokens = True
    out = _run(server.check_usage(_ctx()))

    assert out['status'] == 'auth_required'
    assert lenz.exchange_count() == 2, 'one exchange, one re-exchange, no more'
    assert len(lenz.api_calls) == 2


def test_a_long_wait_renews_at_most_once(lenz):
    lenz.reject_all_tokens = True
    lenz.run_done_at = lenz.now + 10_000
    out = _run(server.verify_claim('the claim', _ctx()))

    assert out['status'] == 'auth_required'
    assert lenz.exchange_count() == 2


# ── the request ──────────────────────────────────────────────────────


def test_the_exchange_request_is_the_rfc_8693_shape(lenz):
    _run(server.check_usage(_ctx()))

    sent = lenz.exchanges[0]
    assert sent['form'] == {
        'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
        'subject_token': SUBJECT,
        'subject_token_type': 'urn:ietf:params:oauth:token-type:access_token',
        'requested_token_type': 'urn:ietf:params:oauth:token-type:access_token',
        'resource': API,
        'scope': 'usage:read',
    }
    expected = base64.b64encode(f'{CLIENT_ID}:{CLIENT_SECRET}'.encode()).decode()
    assert sent['authorization'] == f'Basic {expected}'


# ── per-tool scopes ──────────────────────────────────────────────────


def test_every_tool_has_a_scope_row_and_nothing_else_does():
    registered = set(server.mcp._tool_manager._tools)
    assert set(exchange.TOOL_SCOPES) == registered


def test_every_scope_a_tool_asks_for_exists():
    for tool, scopes in exchange.TOOL_SCOPES.items():
        assert scopes, f'{tool} asks for no scope'
        assert scopes <= exchange.API_SCOPES, tool


def test_the_connector_never_asks_for_more_than_its_tools_use():
    asked = frozenset().union(*exchange.TOOL_SCOPES.values())
    assert asked == {'assess', 'verify', 'ask', 'usage:read', 'history:read'}


def test_the_scope_table():
    assert exchange.TOOL_SCOPES == {
        'assess_claim': {'assess'},
        'verify_claim': {'verify'},
        'select_claims': {'verify'},
        'get_verification': {'verify', 'history:read'},
        'check_usage': {'usage:read'},
        'list_verifications': {'history:read'},
        'ask_followup': {'ask'},
        'start_verification_widget': {'verify', 'history:read'},
        'select_claims_widget': {'verify'},
        'get_verification_widget': {'verify', 'history:read'},
    }


@pytest.mark.parametrize(
    ('call', 'scope'),
    [
        (lambda: server.assess_claim('the claim', _ctx()), 'assess'),
        (lambda: server.verify_claim('the claim', _ctx()), 'verify'),
        (lambda: server.get_verification('task-1', _ctx()), 'history:read verify'),
        (lambda: server.check_usage(_ctx()), 'usage:read'),
        (lambda: server.list_verifications(_ctx()), 'history:read'),
        (lambda: server.ask_followup('pub12345', 'why?', _ctx()), 'ask'),
    ],
)
def test_each_tool_exchanges_for_its_own_scopes(lenz, call, scope):
    _run(call())
    assert lenz.exchanges[0]['form']['scope'] == scope


def test_a_call_outside_any_tool_asks_for_no_scope_and_fails_closed(lenz):
    credential = exchange.CallCredential(SUBJECT, exchange.scopes_for('not_a_tool'))
    with pytest.raises(exchange.ExchangeFailed) as raised:
        _run(credential.header())
    assert raised.value.kind == 'scope'
    assert lenz.exchange_count() == 0


# ── error mapping, by OAuth error code ───────────────────────────────


def _refuse(lenz, status, body, headers=None):
    lenz.token_error = (status, body, headers or {})
    out = _run(server.check_usage(_ctx()))
    assert not lenz.api_calls, 'no API call is made without a token'
    return out


def test_invalid_grant_is_reauthentication(lenz):
    out = _refuse(lenz, 400, {'error': 'invalid_grant'})
    assert out == server._auth_required()


def test_a_refused_exchange_tells_a_signed_in_caller_to_reconnect(lenz):
    """The literal copy, not `_auth_required()` evaluated a second time: this
    caller signed in and has no API key to create, and the exchange is where a
    sign-in that stopped being valid is met."""
    out = _refuse(lenz, 400, {'error': 'invalid_grant'})

    assert out == {'status': 'auth_required', 'message': server.OAUTH_REAUTH_MESSAGE}
    assert 'reconnect' in out['message'].lower()
    assert 'API key' not in out['message']
    assert config.API_CREDENTIALS_URL not in out['message']


@pytest.mark.parametrize(
    ('status', 'error'),
    [
        (401, 'invalid_client'),
        (400, 'invalid_request'),
        (400, 'unsupported_grant_type'),
        (400, 'unauthorized_client'),
        (400, 'invalid_target'),
    ],
)
def test_our_own_misconfiguration_is_an_outage_never_a_reauth(lenz, status, error):
    out = _refuse(lenz, status, {'error': error, 'error_description': 'nope'})
    assert out['status'] == 'service_unavailable'
    assert out['retry_after_seconds'] == exchange.DEFAULT_RETRY_AFTER_S


def test_invalid_scope_is_a_tool_error_with_no_retry(lenz):
    out = _refuse(lenz, 400, {'error': 'invalid_scope'})
    assert out['status'] == 'error'
    assert 'Retrying will not help' in out['message']
    assert lenz.exchange_count() == 1


def test_interaction_required_sends_the_user_to_approve_the_app(lenz):
    uri = f'{ORIGIN}/api-credentials#connected-apps'
    out = _refuse(lenz, 400, {'error': 'interaction_required', 'approval_uri': uri})
    assert out['status'] == 'approval_required'
    assert out['approval_url'] == uri
    assert uri in out['message']


def test_an_approval_link_off_the_lenz_site_is_never_passed_on(lenz):
    out = _refuse(lenz, 400, {'error': 'interaction_required', 'approval_uri': 'https://evil.example/approve'})
    assert out['approval_url'] == config.API_CREDENTIALS_URL
    assert 'evil.example' not in out['message']


def test_access_denied_is_a_plain_error_with_no_retry(lenz):
    out = _refuse(lenz, 400, {'error': 'access_denied', 'error_description': 'This application is blocked.'})
    assert out['status'] == 'forbidden'
    assert 'blocked' in out['message']
    assert 'retry_after_seconds' not in out
    assert lenz.exchange_count() == 1


@pytest.mark.parametrize(
    ('status', 'body', 'retry_after', 'expected'),
    [
        (429, {'error': 'slow_down'}, '30', 30),
        (503, {'error': 'temporarily_unavailable'}, '60', 60),
        (503, {}, None, exchange.DEFAULT_RETRY_AFTER_S),
        (502, {}, None, exchange.DEFAULT_RETRY_AFTER_S),
    ],
)
def test_a_busy_endpoint_is_an_outage_carrying_its_wait(lenz, status, body, retry_after, expected):
    out = _refuse(lenz, status, body, {'Retry-After': retry_after} if retry_after else {})
    assert out['status'] == 'service_unavailable'
    assert out['retry_after_seconds'] == expected


def test_a_transport_failure_is_retried_once_then_an_outage(lenz):
    lenz.token_raises = True
    out = _run(server.check_usage(_ctx()))
    assert out['status'] == 'service_unavailable'
    assert lenz.exchange_count() == 2
    assert not lenz.api_calls


def test_a_malformed_success_is_an_outage(lenz, monkeypatch):
    lenz.token_error = (200, {'access_token': 'lat_x', 'token_type': 'Bearer'}, {})  # no expires_in
    out = _run(server.check_usage(_ctx()))
    assert out['status'] == 'service_unavailable'
    assert not lenz.api_calls


def test_missing_client_credentials_are_an_outage_not_a_raw_error(lenz, monkeypatch):
    monkeypatch.setattr(config, 'OAUTH_CLIENT_SECRET', '')
    out = _run(server.check_usage(_ctx()))
    assert out['status'] == 'service_unavailable'
    assert lenz.exchange_count() == 0


def test_a_refusal_mid_wait_ends_the_call_with_its_mapping(lenz):
    """After the submit, a refused re-exchange is still a clean tool result."""
    lenz.revoke_at = lenz.now + 30
    lenz.token_error = (400, {'error': 'access_denied'}, {})
    lenz.token_error_after = 1  # the first exchange succeeds

    out = _run(server.verify_claim('the claim', _ctx()))

    assert out['status'] == 'forbidden'
    assert lenz.exchange_count() == 2
    assert out['task_id'] == 'task-1'
    assert 'already been charged' in out['message']


def test_a_credential_failure_before_the_submit_carries_no_task_id(lenz):
    """Nothing was started, so there is nothing to collect: the plain mapping."""
    lenz.token_error = (400, {'error': 'invalid_grant'}, {})
    out = _run(server.verify_claim('the claim', _ctx()))

    assert out == server._auth_required()
    assert not lenz.api_calls


def test_a_failure_while_select_waits_keeps_the_new_task_id(lenz):
    lenz.revoke_at = lenz.now + 30
    lenz.token_error = (503, {'error': 'temporarily_unavailable'}, {})
    lenz.token_error_after = 1

    out = _run(server.select_claims('task-0', ['the claim'], _ctx()))

    assert out['status'] == 'service_unavailable'
    assert out['task_id'] == 'task-1'
    assert 'get_verification' in out['message']


# ── the cache ────────────────────────────────────────────────────────


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(exchange, '_now', lambda: now[0])
    return now


def _issued(name: str, expires_at: float) -> exchange.IssuedToken:
    return exchange.IssuedToken(access_token=name, expires_at=expires_at)


def test_the_cache_is_a_bounded_lru(clock):
    cache = exchange.TokenCache(max_entries=3)
    keys = [exchange._cache_key(f'subject-{i}', frozenset({'verify'})) for i in range(4)]
    for i, key in enumerate(keys[:3]):
        cache.put(key, _issued(f'lat_{i}', clock[0] + 600))
    assert cache.get(keys[0]) is not None  # the oldest is now the most recent
    cache.put(keys[3], _issued('lat_3', clock[0] + 600))

    assert len(cache) == 3
    assert cache.get(keys[1]) is None, 'the least recently used entry went'
    assert cache.get(keys[0]) is not None
    assert cache.get(keys[3]) is not None


def test_the_cache_never_stores_a_token_inside_the_margin(clock):
    cache = exchange.TokenCache()
    key = exchange._cache_key('subject', frozenset({'verify'}))
    cache.put(key, _issued('lat_short', clock[0] + exchange.CACHE_MARGIN_S))
    assert len(cache) == 0


def test_the_margin_is_60_seconds(clock):
    assert exchange.CACHE_MARGIN_S == 60
    cache = exchange.TokenCache()
    key = exchange._cache_key('subject', frozenset({'verify'}))
    cache.put(key, _issued('lat_a', clock[0] + 600))
    clock[0] += 539
    assert cache.get(key) is not None
    clock[0] += 1
    assert cache.get(key) is None
    assert len(cache) == 0, 'an entry inside the margin is dropped, not kept'


def test_the_cache_key_is_a_hash_of_the_subject_never_the_subject(clock):
    key = exchange._cache_key(SUBJECT, frozenset({'verify', 'history:read'}))
    assert key == (hashlib.sha256(SUBJECT.encode()).hexdigest(), frozenset({'verify', 'history:read'}))
    assert SUBJECT not in repr(key)


def test_eviction_leaves_a_newer_token_alone(clock):
    cache = exchange.TokenCache()
    key = exchange._cache_key('subject', frozenset({'verify'}))
    old, new = _issued('lat_old', clock[0] + 600), _issued('lat_new', clock[0] + 600)
    cache.put(key, new)
    cache.evict(key, old)
    assert cache.get(key) == new
    cache.evict(key, new)
    assert cache.get(key) is None


def test_the_subject_token_is_never_logged(lenz, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        _run(server.verify_claim('the claim', _ctx()))
        lenz.token_error = (400, {'error': 'invalid_grant'}, {})
        exchange.reset()
        exchange._http_client = httpx.AsyncClient(transport=httpx.MockTransport(lenz.handle))
        _run(server.check_usage(_ctx()))
    text = '\n'.join(r.getMessage() for r in caplog.records)
    assert SUBJECT not in text
    assert 'lat_0000' not in text
    assert CLIENT_SECRET not in text


# ── the API-key door is untouched ────────────────────────────────────


def test_an_api_key_caller_never_exchanges(lenz, monkeypatch):
    key_token = AccessToken(
        token='lenz_' + 'a' * 32,
        client_id='lenz-api-key',
        scopes=[],
        expires_at=None,
        claims={'auth_mode': oauth.AUTH_MODE_API_KEY},
    )
    monkeypatch.setattr('mcp.server.auth.middleware.auth_context.get_access_token', lambda: key_token)
    request = types.SimpleNamespace(headers={'authorization': 'Bearer lenz_' + 'a' * 32})
    ctx = types.SimpleNamespace(request_context=types.SimpleNamespace(request=request))
    monkeypatch.setattr(lenz, '_accepts', lambda authorization: True)

    out = _run(server.check_usage(ctx))

    assert out['status'] == 'ok'
    assert lenz.exchange_count() == 0
    assert lenz.api_calls[0][2] == 'Bearer lenz_' + 'a' * 32
