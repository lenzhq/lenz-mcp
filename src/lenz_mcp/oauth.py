"""OAuth token verification for the MCP resource server (WorkOS-issued JWTs).

Dark behind ``MCP_OAUTH_ENABLED``. When enabled, the FastMCP transport runs
every request's bearer token through ``DualModeTokenVerifier``:

- **``lenz_`` API keys** pass through as a synthetic ``AccessToken`` WITHOUT
  local validation. The MCP has no database, so the public API validates the
  key on each tool call, exactly as in v1: a bad key becomes a structured
  ``auth_required`` tool result, never a transport 401.
- **Anything else** is treated as a WorkOS JWT and verified OFFLINE against
  WorkOS's JWKS: RS256 only, issuer + audience (RFC 8707, trailing-slash
  normalized) + expiry enforced, ``kid`` rotation handled by a throttled
  JWKS refetch. Fail-closed on every error path, and an unreachable key set
  is a retryable 503, never a 401 (``JWKSCache``).
- **No/invalid token** → the SDK returns ``401 + WWW-Authenticate`` pointing
  at the protected-resource metadata — which is what starts OAuth discovery
  for directory clients.

The verified token's ``sub`` is the numeric Lenz user id, set when Lenz
completes the authorization, so the zero-DB server resolves the user with no
lookup. Tool calls then mint a
short-lived service assertion for that user or, with ``LENZ_OAUTH_EXCHANGE``
on, exchange the verified token for a scoped API token (see
``server._authorization`` and ``exchange``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx
import jwt
from jwt import PyJWK
from mcp.server.auth.provider import AccessToken, TokenVerifier
from starlette.applications import Starlette
from starlette.authentication import AuthenticationError
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response

from lenz_mcp import config

logger = logging.getLogger(__name__)

AUTH_MODE_API_KEY = 'api_key'
AUTH_MODE_OAUTH = 'oauth'

ALLOWED_ALGS = ('RS256',)
CLOCK_SKEW_LEEWAY = 30  # seconds
# The token's `sub`: a positive decimal user id, ASCII digits only, no leading
# zero, at most 20 digits. The API applies the same rule to the same token, so
# the two never disagree about who a token names: `str.isdigit()` would also
# accept `01` (which `int()` reads as user 1) and non-ASCII digits (`١٢`,
# which `int()` reads as 12). `[0-9]`, never `\d`: in Python `\d` matches
# every Unicode decimal digit.
SUBJECT_PATTERN = re.compile(r'[1-9][0-9]{0,19}')
# What may NOT appear in a client id we put on a LOG line. Every other logged
# field is already constrained — `sub` must be all digits — leaving
# `client_id`/`azp` as the one attacker-adjacent value, and WorkOS accepts
# self-service dynamic client registration, which is open to anyone. The
# handler writes bare `%(message)s` to stderr and a line-based log collector
# splits on newlines, so an embedded `\n` would emit a SECOND,
# fully-formed `mcp_auth_ok` line — forging a connect for a user id that never
# authenticated. Control characters are the whole of that risk, so they are the
# whole of what we reject.
#
# A charset ALLOW-list (`[A-Za-z0-9_.:-]{1,64}`) would be wrong: real client ids
# are URLs. Claude registers as `https://claude.ai/oauth/mcp-oauth-client-metadata`,
# which fails on the `/` alone, so such a client id would log as `unloggable` and
# the field would carry nothing. Blocklist the dangerous thing; do not allow-list
# a guessed shape.
#
# Safe because `client_id` is the LAST field on the line — a value containing
# spaces can make the tail hard to tokenize but cannot corrupt an earlier
# field. **Keep it last if you add fields.** Applied at the log boundary only;
# the token's real client_id is passed through untouched.
_UNLOGGABLE_CLIENT_ID = re.compile(r'[\x00-\x1f\x7f]')
# Bounds one log line. Claude's is 49 chars; this leaves room without letting a
# self-registered client write an unbounded record.
_CLIENT_ID_LOG_MAXLEN = 128


def _loggable_client_id(client_id: str) -> str:
    """Render ``client_id`` safe to interpolate into a single-line log record.

    Returns ``'unloggable'`` only for values carrying control characters, which
    are the sole way to forge a second record.
    """
    if not client_id or _UNLOGGABLE_CLIENT_ID.search(client_id):
        return 'unloggable'
    return client_id[:_CLIENT_ID_LOG_MAXLEN]


# One fetch attempt's budget. A blocking refresh (cold start, hard expiry, an
# unknown kid) holds the request that triggered it for up to TIMEOUT * ATTEMPTS
# (10s warm, 20s on a cold cache that must also discover). Shorter-with-a-retry
# beats one long attempt: a hung WorkOS response is the realistic failure,
# and the retry usually lands on a different backend.
JWKS_TIMEOUT = 5.0
JWKS_FETCH_ATTEMPTS = 2
# The cache contract. A fetched key set is FRESH for an hour. After that it is
# still SERVED, for up to a day, while one background refresh at a time tries
# to replace it; a failed refresh is retried no sooner than the backoff. After
# a day with no successful fetch the set is HARD-EXPIRED and no longer served.
JWKS_FRESH_S = 3600.0
JWKS_STALE_MAX_S = 86400.0
JWKS_RETRY_BACKOFF_S = 60.0
# Throttle unknown-`kid` refetches so a garbage-token storm can't turn the
# verifier into a WorkOS-DoS amplifier. Kept short because inside this window,
# after a SUCCESSFUL fetch, an unknown kid is answered as invalid (401) without
# refetching, so a WorkOS key rotation landing just after a refresh 401s every
# token until it lapses. One fetch per 15s is still ample storm protection.
JWKS_MIN_REFRESH_INTERVAL = 15.0


class JWKSUnavailable(AuthenticationError):
    """The keys cannot be read right now; the token was not judged.

    Raised instead of rejecting the token: a 401 tells a client its token is
    bad, and a connector host may drop the connection rather than retry. The
    ASGI layer turns this into a 503 with Retry-After (``unavailable_response``).
    """


RETRY_AFTER_S = int(JWKS_RETRY_BACKOFF_S)


def unavailable_response(conn: HTTPConnection, exc: Exception) -> Response:
    """``on_error`` for Starlette's AuthenticationMiddleware.

    A ``JWKSUnavailable`` is a retryable 503. Any other ``AuthenticationError``
    keeps Starlette's default (400 with the message), which nothing of ours raises.
    """
    if isinstance(exc, JWKSUnavailable):
        return JSONResponse(
            {
                'error': 'temporarily_unavailable',
                'error_description': 'The authorization server keys are unreachable. Retry shortly.',
            },
            status_code=503,
            headers={'Retry-After': str(RETRY_AFTER_S)},
        )
    return AuthenticationMiddleware.default_on_error(conn, exc)


def install_unavailable_response(app: Starlette) -> None:
    """Point the SDK's AuthenticationMiddleware at ``unavailable_response``.

    The SDK builds the middleware without an ``on_error``, and an exception out
    of a token verifier otherwise reaches Starlette's ServerErrorMiddleware as a
    500. The stack is built on the first request, so this runs before serving.
    Fails loudly when OAuth is on and the middleware is missing: without it an
    outage would read as a 500 again, silently.
    """
    for entry in app.user_middleware:
        if entry.cls is AuthenticationMiddleware:
            entry.kwargs['on_error'] = unavailable_response
            return
    raise RuntimeError('AuthenticationMiddleware not found: the JWKS outage contract cannot be installed')


def _accepted_audiences(resource: str) -> list[str]:
    """The PRM ``resource`` and the token ``aud`` may differ by a trailing
    slash depending on which side normalized — accept both spellings."""
    base = resource.rstrip('/')
    return [base, base + '/']


class JWKSCache:
    """The WorkOS key set, with an explicit freshness contract.

    - Keys are FRESH for ``JWKS_FRESH_S`` after a successful fetch.
    - Then STALE, and still served, for up to ``JWKS_STALE_MAX_S``: each request
      that finds the set stale starts a background refresh unless one is
      running or the last attempt failed less than ``JWKS_RETRY_BACKOFF_S`` ago.
    - Then HARD-EXPIRED: not served. The request refreshes in the foreground.
    - A cold start, a hard-expired set, or an unknown ``kid`` whose refetch
      fails, raises ``JWKSUnavailable`` (a 503), never a rejection (a 401). A
      failed foreground fetch arms the same backoff, so an outage costs WorkOS
      one fetch a minute, not one per request.
    - An unknown ``kid`` refetches at most once per ``JWKS_MIN_REFRESH_INTERVAL``;
      inside that window, after a successful fetch, the kid is simply unknown
      (a 401).

    The JWKS URI is discovered from the issuer's RFC 8414 metadata rather
    than hardcoded, so a WorkOS path change can't silently break verification
    (see ``_jwks_endpoint``, which memoizes it).
    """

    def __init__(self, issuer: str):
        self._issuer = issuer
        self._keys: dict[str, PyJWK] = {}
        self._jwks_uri: str | None = None
        self._lock = asyncio.Lock()
        # monotonic() of the last successful fetch, and of the last failed one.
        self._fetched_at = 0.0
        self._failed_at = 0.0
        self._background: asyncio.Task | None = None

    def _age(self, now: float) -> float:
        return now - self._fetched_at if self._fetched_at else float('inf')

    def _backing_off(self, now: float) -> bool:
        return bool(self._failed_at) and now - self._failed_at < JWKS_RETRY_BACKOFF_S

    async def signing_key(self, kid: str) -> PyJWK | None:
        """The key for ``kid``; None if the key set does not hold it.

        Raises ``JWKSUnavailable`` when no servable key set can be had.
        """
        now = time.monotonic()
        age = self._age(now)
        if age < JWKS_STALE_MAX_S:
            if age >= JWKS_FRESH_S:
                self._refresh_in_background(now)
            key = self._keys.get(kid)
            if key is not None:
                return key
            return await self._refetch_for_unknown_kid(kid)
        return await self._refetch_expired(kid)

    def _refresh_in_background(self, now: float) -> None:
        if self._background is not None and not self._background.done():
            return  # single-flight
        if self._backing_off(now):
            return
        self._background = asyncio.get_running_loop().create_task(self._locked_refresh())

    async def _locked_refresh(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._age(now) < JWKS_FRESH_S:
                return  # a foreground refresh already renewed the set
            if self._backing_off(now):
                return  # another refresh just failed while this one waited
            await self._refresh()

    async def _refetch_for_unknown_kid(self, kid: str) -> PyJWK | None:
        async with self._lock:
            key = self._keys.get(kid)  # a concurrent refresh may have won
            if key is not None:
                return key
            now = time.monotonic()
            if now - self._fetched_at < JWKS_MIN_REFRESH_INTERVAL:
                return None  # the set was just read and does not hold it
            if self._backing_off(now):
                raise JWKSUnavailable('jwks refresh backing off')
            if not await self._refresh():
                raise JWKSUnavailable('jwks refresh failed')
            return self._keys.get(kid)

    async def _refetch_expired(self, kid: str) -> PyJWK | None:
        async with self._lock:
            now = time.monotonic()
            if self._age(now) >= JWKS_STALE_MAX_S:  # nobody renewed it while we waited
                if self._backing_off(now):
                    raise JWKSUnavailable('jwks unavailable, backing off')
                if not await self._refresh():
                    raise JWKSUnavailable('jwks unavailable')
            return self._keys.get(kid)

    async def _jwks_endpoint(self, client: httpx.AsyncClient) -> str | None:
        """The issuer's JWKS URI, discovered once and reused.

        Discovery is RFC 8414 metadata rather than a hardcoded path, so a WorkOS
        path change can't silently break verification — but it doesn't change
        per refresh, so memoizing it halves both the latency of a refresh and the
        number of requests that can fail during one.
        """
        if self._jwks_uri:
            return self._jwks_uri
        meta = await client.get(f'{self._issuer}/.well-known/oauth-authorization-server')
        jwks_uri = (meta.json() or {}).get('jwks_uri', '')
        if not str(jwks_uri).startswith('https://'):
            logger.error('mcp_oauth_jwks_uri_invalid issuer=%s', self._issuer)
            return None
        self._jwks_uri = jwks_uri
        return jwks_uri

    async def _fetch_keys(self) -> list | None:
        async with httpx.AsyncClient(timeout=JWKS_TIMEOUT) as client:
            jwks_uri = await self._jwks_endpoint(client)
            if not jwks_uri:
                return None
            resp = await client.get(jwks_uri)
            return (resp.json() or {}).get('keys', [])

    async def _refresh(self) -> bool:
        """Fetch the key set; True when it was replaced.

        A failure keeps the current set (stale keys beat none) and stamps
        ``_failed_at``, which is what the backoff reads.
        """
        raw_keys: list | None = None
        for attempt in range(1, JWKS_FETCH_ATTEMPTS + 1):
            try:
                raw_keys = await self._fetch_keys()
                break
            except (httpx.HTTPError, ValueError):
                if attempt < JWKS_FETCH_ATTEMPTS:
                    # One slow or dropped WorkOS response must not blank
                    # verification for sessions that are mid-flight — a single
                    # unretried failure is enough to drop live connector sessions.
                    continue
                logger.exception('mcp_oauth_jwks_fetch_failed issuer=%s', self._issuer)
                # Forget the endpoint so a WorkOS path change can't wedge us on a
                # stale URI: the next refresh re-reads the metadata.
                self._jwks_uri = None
                self._failed_at = time.monotonic()
                return False

        parsed: dict[str, PyJWK] = {}
        for raw in raw_keys or []:
            kid = raw.get('kid')
            if not kid:
                continue
            try:
                parsed[kid] = PyJWK.from_dict(raw)
            except Exception:  # noqa: BLE001 — one bad key must not sink the set
                logger.warning('mcp_oauth_jwk_unparseable kid=%s', kid)
        if not parsed:
            # No usable jwks_uri, or a set with no parseable key: a failure, and
            # the current set stays.
            logger.error('mcp_oauth_jwks_empty issuer=%s', self._issuer)
            self._failed_at = time.monotonic()
            return False
        self._keys = parsed
        self._fetched_at = time.monotonic()
        self._failed_at = 0.0
        return True


class DualModeTokenVerifier(TokenVerifier):
    """API keys pass through to the API; WorkOS JWTs verified offline."""

    def __init__(self, jwks: JWKSCache | None = None):
        self._jwks = jwks or JWKSCache(config.OAUTH_ISSUER)

    async def verify_token(self, token: str) -> AccessToken | None:
        if token.startswith('lenz_'):
            # Deliberately NOT logged. The API validates the key on every tool
            # call, so this branch checks nothing and a log line here would
            # carry no subject.
            return AccessToken(
                token=token,
                client_id='lenz-api-key',
                scopes=[],
                expires_at=None,
                claims={'auth_mode': AUTH_MODE_API_KEY},
            )
        return await self._verify_workos_jwt(token)

    async def _verify_workos_jwt(self, token: str) -> AccessToken | None:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            return None

        if header.get('alg') not in ALLOWED_ALGS:
            logger.warning('mcp_oauth_token_alg_rejected alg=%s', header.get('alg'))
            return None
        kid = header.get('kid')
        if not kid:
            return None

        try:
            key = await self._jwks.signing_key(kid)
        except JWKSUnavailable as exc:
            logger.warning('mcp_oauth_jwks_unavailable reason=%s', exc)
            raise
        if key is None:
            logger.warning('mcp_oauth_unknown_kid')
            return None

        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=list(ALLOWED_ALGS),
                audience=_accepted_audiences(config.MCP_PUBLIC_URL),
                issuer=config.OAUTH_ISSUER,
                leeway=CLOCK_SKEW_LEEWAY,
                options={'require': ['exp', 'iat', 'sub', 'aud', 'iss']},
            )
        except jwt.InvalidTokenError as exc:
            # Friction gauge (redacted): the failure CLASS, never the token.
            logger.warning('mcp_oauth_token_rejected err=%s', type(exc).__name__)
            return None

        sub = claims.get('sub')
        if not isinstance(sub, str) or not SUBJECT_PATTERN.fullmatch(sub):
            # The subject is the numeric Lenz user id; anything else can't
            # be bridged to a user.
            logger.warning('mcp_oauth_non_numeric_sub')
            return None

        scope = claims.get('scope') or ''
        aud = claims.get('aud')
        client_id = str(claims.get('client_id') or claims.get('azp') or 'oauth-client')
        # The only per-user signal this service emits, and the reason it lives
        # HERE rather than in a tools/list handler: `tools/list` is answered by
        # the SDK straight from the registered tool objects, so no code of ours
        # runs on that request at all. The verifier is the single point that
        # sees both who is asking and that they asked.
        #
        # The user id only. Never the token, the email, or the claims blob.
        # client_id is
        # sanitized first (see _loggable_client_id): it is the only field here
        # that upstream validation doesn't already constrain.
        logged_client_id = _loggable_client_id(client_id)
        logger.info('mcp_auth_ok auth_mode=%s user_pk=%s client_id=%s', AUTH_MODE_OAUTH, sub, logged_client_id)
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scope.split() if isinstance(scope, str) and scope else [],
            expires_at=claims.get('exp'),
            resource=aud if isinstance(aud, str) else None,
            subject=sub,
            claims={**claims, 'auth_mode': AUTH_MODE_OAUTH},
        )
