"""Tests for the MCP OAuth layer (src/lenz_mcp/oauth.py + server wiring).

Follows test_server.py conventions: no DB, tools/verifier driven
directly, HTTP mocked. The RS256 keypair is generated once per module and
injected into the JWKS cache — no network, no real WorkOS.
"""

import asyncio
import importlib
import time
import types
from unittest.mock import MagicMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWK
from mcp.server.auth.provider import AccessToken

# Imported here, before the autouse `_oauth_config` patch: a first import of the
# ASGI module under that patch would see OAuth on around a server built with it off.
from lenz_mcp import asgi as _asgi  # noqa: F401, E402
from lenz_mcp import config, oauth, server
from lenz_mcp.testing import connector_env

ISSUER = 'https://test.authkit.app'
RESOURCE = 'https://lenz.io/mcp'
KID = 'test-key-1'

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk_dict(kid=KID) -> dict:
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(_PRIVATE_KEY.public_key(), as_dict=True)
    return {**jwk, 'kid': kid, 'alg': 'RS256', 'use': 'sig'}


def _mint(kid=KID, alg='RS256', key=_PRIVATE_KEY, **overrides) -> str:
    now = int(time.time())
    claims = {
        'iss': ISSUER,
        'aud': RESOURCE,
        'sub': '42',
        'exp': now + 300,
        'iat': now,
        'client_id': 'client_abc',
        **overrides,
    }
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={'kid': kid} if kid else {})


def _verifier(keys: dict | None = None) -> oauth.DualModeTokenVerifier:
    cache = oauth.JWKSCache(ISSUER)
    cache._keys = {k: PyJWK.from_dict(v) for k, v in (keys or {KID: _jwk_dict()}).items()}
    cache._fetched_at = time.monotonic()  # pretend freshly fetched: no network
    return oauth.DualModeTokenVerifier(jwks=cache)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _oauth_config(monkeypatch):
    monkeypatch.setattr(config, 'OAUTH_ENABLED', True)
    monkeypatch.setattr(config, 'OAUTH_ISSUER', ISSUER)
    monkeypatch.setattr(config, 'MCP_PUBLIC_URL', RESOURCE)


# ── dual-mode: API-key branch ────────────────────────────────────────


def test_lenz_key_passes_through_without_validation():
    """The #1 regression path: with OAuth on, a `lenz_` bearer must reach
    the tools (which forward it — the API stays the validation boundary)."""
    token = 'lenz_' + 'a' * 32
    out = _run(_verifier().verify_token(token))
    assert isinstance(out, AccessToken)
    assert out.token == token
    assert out.claims['auth_mode'] == oauth.AUTH_MODE_API_KEY
    assert out.scopes == []


# ── dual-mode: WorkOS JWT branch ─────────────────────────────────────


def test_valid_workos_jwt_verifies():
    out = _run(_verifier().verify_token(_mint()))
    assert isinstance(out, AccessToken)
    assert out.subject == '42'
    assert out.claims['auth_mode'] == oauth.AUTH_MODE_OAUTH
    assert out.client_id == 'client_abc'


def test_trailing_slash_audience_accepted():
    out = _run(_verifier().verify_token(_mint(aud=RESOURCE + '/')))
    assert out is not None


def test_wrong_audience_rejected():
    assert _run(_verifier().verify_token(_mint(aud='https://evil.example/mcp'))) is None


def test_wrong_issuer_rejected():
    assert _run(_verifier().verify_token(_mint(iss='https://evil.authkit.app'))) is None


def test_expired_token_rejected():
    now = int(time.time())
    token = _mint(exp=now - 600, iat=now - 900)
    assert _run(_verifier().verify_token(token)) is None


def test_missing_exp_rejected():
    assert _run(_verifier().verify_token(_mint(exp=None))) is None


def test_non_rs256_alg_rejected():
    token = jwt.encode(
        {'iss': ISSUER, 'aud': RESOURCE, 'sub': '42', 'exp': int(time.time()) + 300, 'iat': int(time.time())},
        'hs-secret-of-at-least-thirty-two-bytes!',
        algorithm='HS256',
        headers={'kid': KID},
    )
    assert _run(_verifier().verify_token(token)) is None


def test_wrong_signing_key_rejected():
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert _run(_verifier().verify_token(_mint(key=other))) is None


def test_garbage_token_rejected():
    assert _run(_verifier().verify_token('not-a-jwt')) is None


def test_non_numeric_sub_rejected():
    """A token whose subject is not a numeric id is rejected."""
    assert _run(_verifier().verify_token(_mint(sub='workos-user-xyz'))) is None


# The API applies the same rule to the same token, so the two never disagree
# about which user a token names.
@pytest.mark.parametrize('sub', ['1', '42', '12345678901234567890'])
def test_a_positive_decimal_user_id_is_accepted(sub):
    out = _run(_verifier().verify_token(_mint(sub=sub)))
    assert out is not None
    assert out.subject == sub


@pytest.mark.parametrize(
    'sub',
    [
        '0',  # no user 0
        '01',  # a leading zero: int() would read user 1
        '١٢',  # Arabic-Indic digits: str.isdigit() accepts them, int() reads 12
        '１２',  # fullwidth digits, the same trap
        '1.0',
        '-1',
        '+1',
        ' 1',
        '1\n',
        '',
        '123456789012345678901',  # 21 digits
    ],
)
def test_a_subject_outside_the_shared_rule_is_refused(sub):
    assert _run(_verifier().verify_token(_mint(sub=sub))) is None


def test_a_non_string_subject_is_refused():
    assert _run(_verifier().verify_token(_mint(sub=42))) is None


def test_the_subject_rule_is_the_apis_regex():
    assert oauth.SUBJECT_PATTERN.pattern == '[1-9][0-9]{0,19}'


def test_missing_kid_rejected():
    assert _run(_verifier().verify_token(_mint(kid=None))) is None


# ── JWKS rotation ────────────────────────────────────────────────────


def test_unknown_kid_triggers_refresh_then_verifies(monkeypatch):
    """Key rotation: a token signed with a kid not in the cache must trigger
    a JWKS refetch and then verify."""
    cache = oauth.JWKSCache(ISSUER)  # empty, never fetched

    async def _fake_refresh(self=cache):
        self._keys = {KID: PyJWK.from_dict(_jwk_dict())}
        self._fetched_at = time.monotonic()
        return True

    monkeypatch.setattr(cache, '_refresh', _fake_refresh)
    verifier = oauth.DualModeTokenVerifier(jwks=cache)
    out = _run(verifier.verify_token(_mint()))
    assert out is not None
    assert out.subject == '42'


def test_unknown_kid_after_recent_refresh_fails_closed(monkeypatch):
    """A refetch that still doesn't know the kid must NOT loop — within the
    throttle window the verifier fails closed."""
    cache = oauth.JWKSCache(ISSUER)
    cache._fetched_at = time.monotonic()  # just refreshed, keys still empty

    async def _boom(self=cache):
        raise AssertionError('must not refetch within the throttle window')

    monkeypatch.setattr(cache, '_refresh', _boom)
    verifier = oauth.DualModeTokenVerifier(jwks=cache)
    assert _run(verifier.verify_token(_mint())) is None


# ── JWKSCache._refresh (the real key-fetch path) ──────────────────────


class _FakeJWKSClient:
    """Stand-in for httpx.AsyncClient used as a context manager. Returns the
    RFC 8414 metadata for the well-known URL and the keys for the jwks_uri."""

    def __init__(self, *, metadata, keys, fail=False):
        self._metadata = metadata
        self._keys = keys
        self._fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        if self._fail:
            raise httpx.ConnectError('down')
        resp = MagicMock()
        resp.json.return_value = self._metadata if 'well-known' in url else {'keys': self._keys}
        return resp


def _patch_async_client(monkeypatch, **kw):
    monkeypatch.setattr(oauth.httpx, 'AsyncClient', lambda **_: _FakeJWKSClient(**kw))


def test_refresh_parses_keys_and_skips_bad_and_kidless(monkeypatch):
    good = _jwk_dict()
    bad = {'kid': 'broken', 'kty': 'RSA'}  # unparseable
    kidless = {'kty': 'RSA', 'n': 'x', 'e': 'AQAB'}  # no kid → skipped
    cache = oauth.JWKSCache(ISSUER)
    _patch_async_client(
        monkeypatch,
        metadata={'jwks_uri': f'{ISSUER}/oauth2/jwks'},
        keys=[good, bad, kidless],
    )
    _run(cache._refresh())
    assert set(cache._keys) == {KID}  # valid kept, bad + kidless dropped
    assert cache._fetched_at > 0


def test_refresh_rejects_non_https_jwks_uri(monkeypatch):
    cache = oauth.JWKSCache(ISSUER)
    _patch_async_client(monkeypatch, metadata={'jwks_uri': 'http://evil/jwks'}, keys=[_jwk_dict()])
    assert _run(cache._refresh()) is False
    assert cache._keys == {}
    assert cache._fetched_at == 0.0
    assert cache._failed_at > 0  # a failure: the backoff is armed


def test_refresh_failure_keeps_the_current_keys(monkeypatch):
    """Stale keys beat none: a failed refresh replaces nothing."""
    cache = oauth.JWKSCache(ISSUER)
    cache._keys = {KID: PyJWK.from_dict(_jwk_dict())}
    cache._fetched_at = 1.0
    _patch_async_client(monkeypatch, metadata={}, keys=[], fail=True)
    assert _run(cache._refresh()) is False
    assert set(cache._keys) == {KID}
    assert cache._fetched_at == 1.0
    assert cache._failed_at > 0


def test_refresh_with_no_parseable_key_is_a_failure(monkeypatch):
    cache = oauth.JWKSCache(ISSUER)
    cache._keys = {KID: PyJWK.from_dict(_jwk_dict())}
    _patch_async_client(monkeypatch, metadata={'jwks_uri': f'{ISSUER}/oauth2/jwks'}, keys=[{'kid': 'x', 'kty': 'RSA'}])
    assert _run(cache._refresh()) is False
    assert set(cache._keys) == {KID}
    assert cache._failed_at > 0


def _patch_counting_client(monkeypatch, *, metadata, keys, fail_first=0):
    """Like _patch_async_client but records every URL fetched across refreshes
    and can fail the first N GETs, so retry + rediscovery are observable."""
    calls: list[str] = []
    state = {'left': fail_first}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            calls.append(url)
            if state['left'] > 0:
                state['left'] -= 1
                raise httpx.ConnectError('down')
            resp = MagicMock()
            resp.json.return_value = metadata if 'well-known' in url else {'keys': keys}
            return resp

    monkeypatch.setattr(oauth.httpx, 'AsyncClient', lambda **_: _Client())
    return calls


def test_refresh_retries_a_transient_fetch_failure(monkeypatch):
    """One slow/failed WorkOS response must not blank verification for live
    sessions. Otherwise the fetch raises, every token 401s, and a client may
    give up instead of re-authenticating."""
    cache = oauth.JWKSCache(ISSUER)
    _patch_counting_client(
        monkeypatch,
        metadata={'jwks_uri': f'{ISSUER}/oauth2/jwks'},
        keys=[_jwk_dict()],
        fail_first=1,
    )

    assert _run(cache._refresh()) is True

    assert set(cache._keys) == {KID}
    assert cache._fetched_at > 0


def test_refresh_gives_up_after_the_attempt_budget(monkeypatch):
    """Retry is bounded — a genuinely-down WorkOS must not stall the request."""
    cache = oauth.JWKSCache(ISSUER)
    calls = _patch_counting_client(monkeypatch, metadata={}, keys=[], fail_first=99)

    assert _run(cache._refresh()) is False

    assert cache._keys == {}
    assert cache._fetched_at == 0.0
    assert len(calls) == oauth.JWKS_FETCH_ATTEMPTS


def test_refresh_reuses_the_discovered_jwks_uri(monkeypatch):
    """Discovery is per-endpoint, not per-refresh: memoizing the jwks_uri halves
    both the latency and the number of ways a refresh can fail."""
    cache = oauth.JWKSCache(ISSUER)
    calls = _patch_counting_client(monkeypatch, metadata={'jwks_uri': f'{ISSUER}/oauth2/jwks'}, keys=[_jwk_dict()])

    _run(cache._refresh())
    _run(cache._refresh())

    assert sum('well-known' in u for u in calls) == 1
    assert sum(u.endswith('/oauth2/jwks') for u in calls) == 2


def test_failed_refresh_drops_the_memoized_uri(monkeypatch):
    """A memoized endpoint must not outlive a WorkOS path change — on failure,
    forget it so the next refresh re-reads the metadata."""
    cache = oauth.JWKSCache(ISSUER)
    _patch_counting_client(monkeypatch, metadata={'jwks_uri': f'{ISSUER}/oauth2/jwks'}, keys=[_jwk_dict()])
    _run(cache._refresh())
    assert cache._jwks_uri

    _patch_counting_client(monkeypatch, metadata={}, keys=[], fail_first=99)
    _run(cache._refresh())

    assert cache._jwks_uri is None


def test_rotation_blackout_window_stays_short():
    """On an unknown kid the throttle returns None WITHOUT refetching, so its
    duration is a hard blackout: if WorkOS rotates keys just after a refresh,
    every token 401s until it lapses. Keep that window small."""
    assert oauth.JWKS_MIN_REFRESH_INTERVAL <= 15


# ── the cache contract: fresh 1h, stale to 24h, then hard-expired ─────


class _Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(oauth.time, 'monotonic', c)
    return c


def _cache_with_keys(clock, *, age: float) -> oauth.JWKSCache:
    cache = oauth.JWKSCache(ISSUER)
    cache._keys = {KID: PyJWK.from_dict(_jwk_dict())}
    cache._fetched_at = clock.now - age
    return cache


def _scripted_refresh(monkeypatch, cache, clock, results: list[bool]):
    """Replace the network with a script of outcomes; returns the list of calls."""
    calls: list[float] = []

    async def _refresh(self=cache):
        calls.append(clock.now)
        ok = results.pop(0)
        if ok:
            self._keys = {KID: PyJWK.from_dict(_jwk_dict())}
            self._fetched_at = clock.now
            self._failed_at = 0.0
        else:
            self._failed_at = clock.now
        return ok

    monkeypatch.setattr(cache, '_refresh', _refresh)
    return calls


def test_fresh_keys_are_served_without_a_fetch(monkeypatch, clock):
    cache = _cache_with_keys(clock, age=oauth.JWKS_FRESH_S - 1)
    calls = _scripted_refresh(monkeypatch, cache, clock, [])
    assert _run(cache.signing_key(KID)) is not None
    assert calls == []


def test_stale_keys_are_served_while_one_background_refresh_runs(monkeypatch, clock):
    cache = _cache_with_keys(clock, age=oauth.JWKS_FRESH_S + 1)
    calls = _scripted_refresh(monkeypatch, cache, clock, [True])

    async def _two_requests():
        first = await cache.signing_key(KID)
        second = await cache.signing_key(KID)  # the refresh is still pending: no second one
        await cache._background
        return first, second

    first, second = _run(_two_requests())
    assert first is not None and second is not None
    assert len(calls) == 1
    assert cache._fetched_at == clock.now  # renewed


def test_one_background_refresh_at_a_time_during_an_outage(monkeypatch, clock):
    """Requests arriving while the refresh is still queued must not queue a
    second one: during an outage each would be one more WorkOS fetch."""
    cache = _cache_with_keys(clock, age=oauth.JWKS_FRESH_S + 1)
    calls = _scripted_refresh(monkeypatch, cache, clock, [False, False, False])

    async def _burst():
        for _ in range(3):
            assert await cache.signing_key(KID) is not None
        await asyncio.sleep(0)
        await cache._background

    _run(_burst())
    assert len(calls) == 1


def test_a_failed_background_refresh_keeps_serving_and_backs_off(monkeypatch, clock):
    cache = _cache_with_keys(clock, age=oauth.JWKS_FRESH_S + 1)
    calls = _scripted_refresh(monkeypatch, cache, clock, [False, True])

    async def _request():
        key = await cache.signing_key(KID)
        if cache._background is not None:
            await cache._background
        return key

    assert _run(_request()) is not None  # served stale; the refresh failed
    clock.now += oauth.JWKS_RETRY_BACKOFF_S - 1
    assert _run(_request()) is not None
    assert len(calls) == 1  # inside the backoff: no second attempt
    clock.now += 2
    assert _run(_request()) is not None
    assert len(calls) == 2  # after it: retried, and renewed


def test_hard_expired_keys_are_not_served(monkeypatch, clock):
    """After a day with no successful fetch the old set is not trusted: an
    outage is a 503, not a verification against keys WorkOS may have revoked."""
    cache = _cache_with_keys(clock, age=oauth.JWKS_STALE_MAX_S + 1)
    _scripted_refresh(monkeypatch, cache, clock, [False])
    with pytest.raises(oauth.JWKSUnavailable):
        _run(cache.signing_key(KID))


def test_hard_expired_keys_are_refetched_in_the_foreground(monkeypatch, clock):
    cache = _cache_with_keys(clock, age=oauth.JWKS_STALE_MAX_S + 1)
    calls = _scripted_refresh(monkeypatch, cache, clock, [True])
    assert _run(cache.signing_key(KID)) is not None
    assert len(calls) == 1


def test_cold_start_outage_is_unavailable_and_backs_off(monkeypatch, clock):
    """No keys yet and WorkOS unreachable: a 503 (retryable), and one fetch a
    backoff window rather than one per request."""
    cache = oauth.JWKSCache(ISSUER)
    calls = _scripted_refresh(monkeypatch, cache, clock, [False, True])
    with pytest.raises(oauth.JWKSUnavailable):
        _run(cache.signing_key(KID))
    clock.now += oauth.JWKS_RETRY_BACKOFF_S - 1
    with pytest.raises(oauth.JWKSUnavailable):
        _run(cache.signing_key(KID))
    assert len(calls) == 1
    clock.now += 2
    assert _run(cache.signing_key(KID)) is not None
    assert len(calls) == 2


def test_unknown_kid_during_an_outage_is_unavailable_not_invalid(monkeypatch, clock):
    cache = _cache_with_keys(clock, age=oauth.JWKS_MIN_REFRESH_INTERVAL + 1)
    _scripted_refresh(monkeypatch, cache, clock, [False])
    with pytest.raises(oauth.JWKSUnavailable):
        _run(cache.signing_key('rotated-kid'))


def test_unknown_kid_inside_the_backoff_is_unavailable_without_a_fetch(monkeypatch, clock):
    cache = _cache_with_keys(clock, age=oauth.JWKS_FRESH_S)
    cache._failed_at = clock.now - 1
    calls = _scripted_refresh(monkeypatch, cache, clock, [])
    with pytest.raises(oauth.JWKSUnavailable):
        _run(cache.signing_key('rotated-kid'))
    assert calls == []


def test_unknown_kid_right_after_a_fetch_is_invalid(monkeypatch, clock):
    """The set was just read and does not hold the kid: a 401, no refetch."""
    cache = _cache_with_keys(clock, age=1)
    calls = _scripted_refresh(monkeypatch, cache, clock, [])
    assert _run(cache.signing_key('garbage-kid')) is None
    assert calls == []


def test_unknown_kid_is_refetched_once_the_throttle_lapses(monkeypatch, clock):
    cache = _cache_with_keys(clock, age=oauth.JWKS_MIN_REFRESH_INTERVAL + 1)
    calls = _scripted_refresh(monkeypatch, cache, clock, [True])
    assert _run(cache.signing_key('garbage-kid')) is None  # fetched, still unknown
    assert len(calls) == 1


def test_the_verifier_lets_unavailable_through(monkeypatch, clock):
    """Not a None: None is the 401 path."""
    cache = oauth.JWKSCache(ISSUER)
    _scripted_refresh(monkeypatch, cache, clock, [False])
    verifier = oauth.DualModeTokenVerifier(jwks=cache)
    with pytest.raises(oauth.JWKSUnavailable):
        _run(verifier.verify_token(_mint()))


def test_the_contract_numbers():
    assert oauth.JWKS_FRESH_S == 3600
    assert oauth.JWKS_STALE_MAX_S == 86400
    assert oauth.JWKS_RETRY_BACKOFF_S == 60
    assert oauth.RETRY_AFTER_S == 60


# ── _authorization under OAuth ───────────────────────────────────────


def _ctx(auth='Bearer lenz_testkey'):
    headers = {'authorization': auth} if auth else {}
    request = types.SimpleNamespace(headers=headers)
    return types.SimpleNamespace(request_context=types.SimpleNamespace(request=request))


def test_api_key_caller_forwards_raw_header(monkeypatch):
    access_token = AccessToken(
        token='lenz_abc',
        client_id='lenz-api-key',
        scopes=[],
        expires_at=None,
        claims={'auth_mode': oauth.AUTH_MODE_API_KEY},
    )
    monkeypatch.setattr('mcp.server.auth.middleware.auth_context.get_access_token', lambda: access_token)
    assert server._authorization(_ctx(auth='Bearer lenz_abc')) == 'Bearer lenz_abc'


# ── what a caller is told when their credential is refused ───────────


def _oauth_access_token(user='42'):
    return AccessToken(
        token='workos-token',
        client_id='oauth-host',
        scopes=[],
        expires_at=None,
        subject=user,
        claims={'auth_mode': oauth.AUTH_MODE_OAUTH, 'sub': user},
    )


def _signed_in(monkeypatch, access_token=None):
    monkeypatch.setattr(
        'mcp.server.auth.middleware.auth_context.get_access_token',
        lambda: access_token if access_token is not None else _oauth_access_token(),
    )


API_KEY_AUTH_REQUIRED = (
    'No Lenz API key was provided. Create a free key at '
    'https://lenz.io/api-credentials and set it as the Authorization bearer '
    'token in your MCP client configuration.'
)


def test_an_api_key_caller_keeps_the_words_it_has_today(monkeypatch):
    """Byte for byte: an API-key caller's advice is the advice that works for them."""
    monkeypatch.setattr(config, 'API_CREDENTIALS_URL', 'https://lenz.io/api-credentials')
    monkeypatch.setattr(config, 'OAUTH_ENABLED', False)
    assert server._auth_required() == {'status': 'auth_required', 'message': API_KEY_AUTH_REQUIRED}


def test_an_api_key_caller_under_oauth_keeps_the_same_words(monkeypatch):
    monkeypatch.setattr(config, 'API_CREDENTIALS_URL', 'https://lenz.io/api-credentials')
    _signed_in(
        monkeypatch,
        AccessToken(
            token='lenz_abc',
            client_id='lenz-api-key',
            scopes=[],
            expires_at=None,
            claims={'auth_mode': oauth.AUTH_MODE_API_KEY},
        ),
    )
    assert server._auth_required()['message'] == API_KEY_AUTH_REQUIRED


def test_a_signed_in_caller_is_told_to_reconnect(monkeypatch):
    """Someone who signed in has no API key to make: sending them to key
    creation is advice they cannot act on."""
    _signed_in(monkeypatch)
    out = server._auth_required()

    assert out['status'] == 'auth_required'
    assert 'reconnect' in out['message'].lower()
    assert 'API key' not in out['message']
    assert config.API_CREDENTIALS_URL not in out['message']
    assert 'http' not in out['message']


def test_a_refused_credential_mid_call_tells_a_signed_in_caller_to_reconnect(monkeypatch):
    """The API's own 401 lands on the same copy, which is where a caller whose
    sign-in expired actually meets it."""
    from lenz_mcp import client as api_client

    _signed_in(monkeypatch)

    async def _unauthorized(*_args, **_kwargs):
        return api_client.ApiResponse(status=401, data={'detail': 'Unauthorized'})

    monkeypatch.setattr(api_client, 'me_usage', _unauthorized)
    out = _run(server.check_usage(_ctx()))

    assert out['status'] == 'auth_required'
    assert 'reconnect' in out['message'].lower()


def test_the_reconnect_copy_names_no_tool_and_no_key(monkeypatch):
    for word in ('verify_claim', 'assess_claim', 'get_verification', 'api-credentials', 'API key'):
        assert word not in server.OAUTH_REAUTH_MESSAGE


def test_oauth_off_never_touches_auth_context(monkeypatch):
    monkeypatch.setattr(config, 'OAUTH_ENABLED', False)

    def _boom():
        raise AssertionError('auth context must not be consulted when OAuth is dark')

    monkeypatch.setattr('mcp.server.auth.middleware.auth_context.get_access_token', _boom)
    assert server._authorization(_ctx(auth='Bearer lenz_abc')) == 'Bearer lenz_abc'


# ── app construction (flag off / on) ─────────────────────────────────


@pytest.fixture
def app_built_off_the_shared_server():
    """Build an app off the SHARED `server.mcp` and put its session manager back.

    On 2.x `streamable_http_app()` does not just return an app — it BINDS a fresh
    `StreamableHTTPSessionManager` onto the low-level server. Build one off the
    process-wide `lenz_mcp.server.mcp` and every later test that enters
    `mcp.session_manager.run()` is entering a manager `asgi.application` no longer
    dispatches to; each request then answers `RuntimeError: Task group is not
    initialized`, which reads as a broken server rather than as a dirty fixture.

    Most MCP tests avoid this entirely by going through `lenz_mcp.testing.assembled_app`,
    which re-imports the module per test. These two need the shared instance —
    they are ABOUT how that instance is assembled — so they restore it instead.
    """
    saved: list[tuple] = []

    def _build(target=None):
        target = target or server.mcp
        lowlevel = target._lowlevel_server
        saved.append((lowlevel, lowlevel._session_manager))
        return target.streamable_http_app()

    try:
        yield _build
    finally:
        # Each instance the test built an app off, not whatever `server.mcp`
        # happens to name now: the flag-on test reloads the module underneath us.
        for lowlevel, manager in saved:
            lowlevel._session_manager = manager


def test_flag_off_app_has_no_auth_wall_or_oauth_routes(app_built_off_the_shared_server):
    """Byte-identical-to-v1 guarantee: with the flag off (the default in
    tests), the served app carries no auth middleware and no PRM route."""
    app = app_built_off_the_shared_server()
    assert app.user_middleware == []
    paths = [getattr(r, 'path', '') for r in app.routes]
    assert not any('oauth-protected-resource' in p for p in paths)
    assert '/healthz' in paths


def test_flag_on_app_wires_auth_and_prm(app_built_off_the_shared_server):
    """Reload the server with OAuth enabled: /mcp gains the 401 wall
    (WWW-Authenticate → OAuth discovery) and the SDK serves the PRM."""
    from lenz_mcp import config as config_module

    try:
        with connector_env(
            MCP_OAUTH_ENABLED=True,
            WORKOS_AUTHKIT_DOMAIN='test.authkit.app',
            MCP_PUBLIC_URL=RESOURCE,
        ):
            importlib.reload(config_module)
            reloaded = importlib.reload(server)
            app = app_built_off_the_shared_server(reloaded.mcp)
            middleware_names = [m.cls.__name__ for m in app.user_middleware]
            assert 'AuthenticationMiddleware' in middleware_names
            paths = [getattr(r, 'path', '') for r in app.routes]
            assert any('oauth-protected-resource' in p for p in paths)

            import httpx

            async def _unauthed_401():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url='https://lenz.io') as c:
                    return await c.post('/mcp', json={})

            resp = asyncio.run(_unauthed_401())
            assert resp.status_code == 401
            assert 'oauth-protected-resource' in resp.headers.get('www-authenticate', '')
    finally:
        # The environment is restored by the context manager; restore the modules
        # so later tests see the dark (v1) server again.
        importlib.reload(config_module)
        importlib.reload(server)


def test_the_auth_logger_reaches_prod_at_info():
    """`mcp_auth_ok` is an operational signal: the connector's own logging
    (observability.py) must send its logger's INFO lines out as bare text."""
    from lenz_mcp import observability

    assert 'lenz_mcp.oauth' in observability.INFO_LOGGERS
    assert oauth.logger.name == 'lenz_mcp.oauth'


# ── auth telemetry ───────────────────────────────────────────────────
#
# `mcp_auth_ok` is the only authentication line lenz-mcp emits. `tools/list`
# is answered by the SDK from the registered tool objects, so no handler of ours
# runs on it; the verifier is the sole hook that sees who is asking.


@pytest.fixture
def auth_logs():
    """Collect `lenz_mcp.oauth` records straight off the logger.

    NOT caplog: the logger is configured `propagate=False` (so the deployed
    logging gets one clean emission), and caplog installs its
    handler on the ROOT logger — it would silently capture nothing here and the
    assertions below would pass for the wrong reason.
    """
    import logging

    logger = logging.getLogger('lenz_mcp.oauth')
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collect(level=logging.INFO)
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def _auth_ok(records):
    return [r for r in records if r.getMessage().startswith('mcp_auth_ok')]


def test_verified_jwt_logs_user_pk_and_oauth_mode(auth_logs):
    _run(_verifier().verify_token(_mint(sub='1438')))
    lines = _auth_ok(auth_logs)
    assert len(lines) == 1
    # Exact fields, not substrings: `'user_pk=1438' in msg` would also accept
    # a garbled `user_pk=14380`, which is the bug this test exists to catch.
    assert lines[0].getMessage().split() == [
        'mcp_auth_ok',
        f'auth_mode={oauth.AUTH_MODE_OAUTH}',
        'user_pk=1438',
        'client_id=client_abc',
    ]


def test_api_key_path_authenticates_but_logs_nothing(auth_logs):
    """The `lenz_` branch must stay silent.

    The connector does not validate the key; the API does, on every tool
    call. A log line here would fire before any validation, and with no
    subject to carry it could never say WHICH user.
    """
    out = _run(_verifier().verify_token('lenz_' + 'a' * 32))
    assert out is not None  # still authenticates — only the logging is dropped
    assert _auth_ok(auth_logs) == []


@pytest.mark.parametrize(
    'mint_kwargs',
    [
        {'aud': 'https://evil.example/mcp'},
        {'iss': 'https://evil.example'},
        {'sub': 'not-a-pk'},
        None,
    ],
    ids=['bad-aud', 'bad-iss', 'non-numeric-sub', 'garbage'],
)
def test_rejected_tokens_never_log_auth_ok(auth_logs, mint_kwargs):
    """A rejected token must not log `mcp_auth_ok`: the line means "this user
    authenticated", so a false positive here reads as a user who never did.

    Tokens are minted INSIDE the test body, not in the parametrize list. A
    decorator argument is evaluated at COLLECTION time, which would freeze each
    token's `exp` to collection+300s; on a slow `-n auto` run the token could
    expire before the test executes and every assertion below would still pass
    — on expiry rather than on the audience/issuer/sub check the case is named
    for. Every other _mint() in this file is likewise called at test time.
    """
    token = 'garbage' if mint_kwargs is None else _mint(**mint_kwargs)
    assert _run(_verifier().verify_token(token)) is None
    assert _auth_ok(auth_logs) == []


def test_newline_in_client_id_cannot_forge_a_log_line(auth_logs):
    """client_id is the one logged field upstream validation doesn't constrain.

    The handler writes bare `%(message)s` and a line-based log reader splits on
    newlines, so an embedded `\\n` would emit a second, fully-formed
    `mcp_auth_ok` record for a user that never authenticated.
    """
    evil = 'abc\nmcp_auth_ok auth_mode=oauth user_pk=9999 client_id=forged'
    out = _run(_verifier().verify_token(_mint(sub='1438', client_id=evil)))
    assert out is not None
    assert out.client_id == evil  # the token itself is untouched
    lines = _auth_ok(auth_logs)
    assert len(lines) == 1
    msg = lines[0].getMessage()
    assert '\n' not in msg
    assert 'user_pk=9999' not in msg
    assert msg.endswith('client_id=unloggable')


def test_real_claude_client_id_survives_sanitizing(auth_logs):
    """Guard against a sanitizer that throws away every real value.

    A charset allow-list such as `[A-Za-z0-9_.:-]{1,64}` would reject them all:
    real client ids are URLs (Claude registers as the literal below), and the
    `/` alone fails it, so every real client id would log `unloggable`.
    The security property only needs control characters gone (see
    `test_newline_in_client_id_cannot_forge_a_log_line`). Pin the real value so
    a future tightening can't quietly blind the field.
    """
    real = 'https://claude.ai/oauth/mcp-oauth-client-metadata'
    out = _run(_verifier().verify_token(_mint(sub='1438', client_id=real)))
    assert out is not None
    lines = _auth_ok(auth_logs)
    assert len(lines) == 1
    assert lines[0].getMessage().endswith(f'client_id={real}')


@pytest.mark.parametrize(
    'evil',
    ['a\rb', 'a\x00b', 'a\x1fb', 'a\x7fb'],
    ids=['carriage-return', 'nul', 'unit-separator', 'delete'],
)
def test_any_control_character_is_rejected(auth_logs, evil):
    """`\\n` is not the only character a log pipeline or a downstream parser can
    be confused by. Reject the whole control range, not just newline."""
    out = _run(_verifier().verify_token(_mint(sub='1438', client_id=evil)))
    assert out is not None
    assert out.client_id == evil  # the token itself is untouched
    assert _auth_ok(auth_logs)[0].getMessage().endswith('client_id=unloggable')


def test_overlong_client_id_is_truncated_not_dropped(auth_logs):
    """A self-registered client must not be able to write an unbounded record,
    but a merely-long id is still worth more than `unloggable`."""
    long_id = 'https://example.test/' + ('x' * 500)
    out = _run(_verifier().verify_token(_mint(sub='1438', client_id=long_id)))
    assert out is not None
    logged = _auth_ok(auth_logs)[0].getMessage().split('client_id=', 1)[1]
    assert logged == long_id[: oauth._CLIENT_ID_LOG_MAXLEN]
    assert len(logged) == oauth._CLIENT_ID_LOG_MAXLEN


def test_auth_log_never_contains_the_token(auth_logs):
    token = _mint(sub='1438')
    _run(_verifier().verify_token(token))
    for record in auth_logs:
        assert token not in record.getMessage()
        assert token not in str(record.args)


# ── the outage contract on the wire: 503 + Retry-After, never 401 or 500 ──


def _post_mcp(harness, token: str):
    async def _post():
        transport = httpx.ASGITransport(app=harness.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url='https://lenz.io') as c:
            return await c.post(
                '/mcp',
                json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json, text/event-stream'},
            )

    return harness.run(_post())


_OAUTH_ON = dict(MCP_OAUTH_ENABLED=True, WORKOS_AUTHKIT_DOMAIN='test.authkit.app', MCP_PUBLIC_URL=RESOURCE)


def test_a_jwks_outage_answers_503_with_retry_after(monkeypatch):
    from lenz_mcp.testing import assembled_app

    async def _unavailable(self, kid):
        raise oauth.JWKSUnavailable('down')

    monkeypatch.setattr(oauth.JWKSCache, 'signing_key', _unavailable)
    with assembled_app(**_OAUTH_ON) as harness:
        resp = _post_mcp(harness, _mint())
    assert resp.status_code == 503
    assert resp.headers['retry-after'] == str(oauth.RETRY_AFTER_S)
    assert resp.json()['error'] == 'temporarily_unavailable'


def test_an_invalid_token_is_still_a_401(monkeypatch):
    from lenz_mcp.testing import assembled_app

    async def _no_key(self, kid):
        return None

    monkeypatch.setattr(oauth.JWKSCache, 'signing_key', _no_key)
    with assembled_app(**_OAUTH_ON) as harness:
        resp = _post_mcp(harness, _mint())
    assert resp.status_code == 401
    assert 'oauth-protected-resource' in resp.headers.get('www-authenticate', '')


def test_installing_the_handler_fails_loudly_without_the_middleware():
    from starlette.applications import Starlette

    with pytest.raises(RuntimeError, match='AuthenticationMiddleware not found'):
        oauth.install_unavailable_response(Starlette())
