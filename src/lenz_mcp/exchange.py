"""The OAuth caller's API credential: an RFC 8693 token exchange.

An OAuth tool call cannot forward the user's AuthKit token to the Lenz API: it
is audience-bound to this connector, and a resource server must not pass on a
token issued for itself. So the connector authenticates to the API's token
endpoint as its own OAuth client (HTTP Basic) and exchanges the user's token,
already verified at the transport, for a short-lived Lenz API access token
carrying only the scopes the tool in hand needs.

This is how an OAuth caller is served: there is no other credential path, so a
deployment with OAuth on and no client credentials cannot serve one.

Three properties matter more than the rest:

- **One exchange per tool call, not per poll.** A deep check polls the API
  every few seconds for up to a couple of minutes. The token a call obtains is
  PINNED for that call (``CallCredential``), so the polls reuse it, and a
  process-local LRU lets the next tool call on the same subject token and the
  same scopes reuse it too. The cache hands out a token only while more than
  ``CACHE_MARGIN_S`` of its life remains; a pinned token is used until it
  actually expires, because the API issues every exchanged token with enough
  life for one tool call.
- **The cache never holds the user's token.** Entries are keyed on its SHA-256
  and the scope set, so a new sign-in (a new token) never reuses a grant made
  for the old one, and two users can never share an entry.
- **Failures are mapped by the OAuth error code, never the HTTP status.**
  ``invalid_grant`` means the user must sign in again; the client and request
  errors mean this service is misconfigured, which is an outage to report,
  not a reason to tell every host to drop its connection.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import httpx

from lenz_mcp import config

logger = logging.getLogger(__name__)

TOKEN_EXCHANGE_GRANT = 'urn:ietf:params:oauth:grant-type:token-exchange'  # noqa: S105 (a URN, not a secret)
ACCESS_TOKEN_TYPE = 'urn:ietf:params:oauth:token-type:access_token'  # noqa: S105 (a URN, not a secret)

# The cache stops handing out a token this long before it expires, so a call
# that starts on a cached token is never the one that runs it out.
CACHE_MARGIN_S = 60.0
# Bounded: one entry per (subject token, scope set) seen within a token's life.
CACHE_MAX_ENTRIES = 1024
# A pinned token is refreshed this long before its expiry, to absorb clock and
# network skew between here and the API.
PIN_SKEW_S = 5.0
# The exchange is a small form POST; it must not eat a tool call's budget.
EXCHANGE_TIMEOUT_S = 10.0
# The wait a caller is told when the endpoint states none.
DEFAULT_RETRY_AFTER_S = 60

# Every scope the API defines. A tool may ask only for names in this set.
API_SCOPES = frozenset(
    {
        'assess',
        'verify',
        'ask',
        'ask:read',
        'extract',
        'history:read',
        'history:write',
        'usage:read',
        'webhooks:manage',
    }
)

# What each tool asks for: exactly the API routes it calls, and nothing else.
# `get_verification` reads a stored result (`GET /verifications/{id}`,
# history) or waits on a run (`GET /verify/status`); the API answers the status
# of a run this grant did not itself submit only under `history:read`, so the
# tool asks for both. Every registered tool must have a row: a tool without one
# cannot obtain a credential at all (tests hold the table to the tool list).
TOOL_SCOPES: dict[str, frozenset[str]] = {
    'assess_claim': frozenset({'assess'}),
    'verify_claim': frozenset({'verify'}),
    'select_claims': frozenset({'verify'}),
    'get_verification': frozenset({'verify', 'history:read'}),
    'check_usage': frozenset({'usage:read'}),
    'list_verifications': frozenset({'history:read'}),
    'ask_followup': frozenset({'ask'}),
    # The card-only tools ask for what their model-visible twins ask for, plus
    # what the card's own paths need: a retry reads the status of the check it
    # retries, which a later sign-in makes somebody else's run to read.
    'start_verification_widget': frozenset({'verify', 'history:read'}),
    'select_claims_widget': frozenset({'verify'}),
    'get_verification_widget': frozenset({'verify', 'history:read'}),
}

# OAuth error codes that mean this service's own configuration or request is
# wrong. Nothing the user can do fixes them, and re-authenticating would not.
_OPERATIONAL_ERRORS = frozenset(
    {'invalid_client', 'invalid_request', 'unsupported_grant_type', 'unauthorized_client', 'invalid_target'}
)
# Codes the endpoint answers when it is overloaded or its own dependencies are.
_BUSY_ERRORS = frozenset({'temporarily_unavailable', 'slow_down', 'server_error'})


class ExchangeNotConfigured(RuntimeError):
    """The exchange is on but this service has no client credentials to make it."""


class ExchangeFailed(Exception):
    """The exchange did not produce a token; ``kind`` says what the caller sees.

    - ``reauth``: the user's token was refused; they must sign in again.
    - ``unavailable``: an outage or a misconfiguration; retry after ``retry_after``.
    - ``scope``: the tool asked for a scope this connection does not hold.
    - ``approval``: the user must approve this app first, at ``approval_uri``.
    - ``blocked``: this app is blocked from the user's account.
    """

    def __init__(
        self,
        kind: str,
        error: str,
        *,
        retry_after: int | None = None,
        approval_uri: str | None = None,
    ):
        super().__init__(f'{kind}: {error}')
        self.kind = kind
        self.error = error
        self.retry_after = retry_after
        self.approval_uri = approval_uri


@dataclass(frozen=True)
class IssuedToken:
    access_token: str
    expires_at: float  # on the ``_now`` clock


_now = time.monotonic  # patch point for tests


def scopes_for(tool: str) -> frozenset[str]:
    """The scopes ``tool`` asks for. A tool with no row gets none, and fails closed."""
    return TOOL_SCOPES.get(tool, frozenset())


def _cache_key(subject_token: str, scopes: frozenset[str]) -> tuple[str, frozenset[str]]:
    return hashlib.sha256(subject_token.encode()).hexdigest(), frozenset(scopes)


class TokenCache:
    """A bounded LRU of exchanged tokens, keyed on (sha256(subject token), scopes)."""

    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES, margin_s: float = CACHE_MARGIN_S):
        self.max_entries = max_entries
        self.margin_s = margin_s
        self._entries: OrderedDict[tuple[str, frozenset[str]], IssuedToken] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: tuple[str, frozenset[str]]) -> IssuedToken | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at - _now() <= self.margin_s:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry

    def put(self, key: tuple[str, frozenset[str]], token: IssuedToken) -> None:
        if token.expires_at - _now() <= self.margin_s:
            return  # never servable, so never stored
        self._entries[key] = token
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def evict(self, key: tuple[str, frozenset[str]], token: IssuedToken | None = None) -> None:
        """Drop the entry; with ``token``, only if it is still that token (a
        concurrent call may already have replaced it with a good one)."""
        entry = self._entries.get(key)
        if entry is not None and (token is None or entry.access_token == token.access_token):
            del self._entries[key]

    def clear(self) -> None:
        self._entries.clear()


_cache = TokenCache()
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=EXCHANGE_TIMEOUT_S)
    return _http_client


def reset() -> None:
    """Forget every cached token (tests; never needed in service)."""
    global _http_client
    _cache.clear()
    _http_client = None


def _retry_after(resp: httpx.Response) -> int:
    try:
        seconds = int(resp.headers.get('retry-after', ''))
    except ValueError:
        return DEFAULT_RETRY_AFTER_S
    return seconds if seconds > 0 else DEFAULT_RETRY_AFTER_S


async def _post(data: dict[str, str]) -> httpx.Response:
    """POST the exchange once, retrying ONE transport failure: the API client
    has no retry layer, and a dropped connection is the common transient."""
    client_id, secret = config.OAUTH_CLIENT_ID, config.OAUTH_CLIENT_SECRET
    if not client_id or not secret:
        raise ExchangeNotConfigured('LENZ_OAUTH_CLIENT_ID and LENZ_OAUTH_CLIENT_SECRET must be set to exchange tokens.')
    last: Exception | None = None
    for _attempt in range(2):
        try:
            return await _get_client().post(
                config.TOKEN_ENDPOINT,
                data=data,
                auth=httpx.BasicAuth(client_id, secret),
                headers={'User-Agent': config.USER_AGENT, 'Accept': 'application/json'},
            )
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:
            last = exc
    logger.warning('mcp_exchange_transport_error err=%s', type(last).__name__)
    raise ExchangeFailed('unavailable', 'transport_error', retry_after=DEFAULT_RETRY_AFTER_S)


async def exchange(subject_token: str, scopes: frozenset[str]) -> IssuedToken:
    """One exchange at the token endpoint, uncached. Raises ``ExchangeFailed``
    or ``ExchangeNotConfigured``; never returns a token it could not parse."""
    if not scopes or not scopes <= API_SCOPES:
        # A programming error, caught before it reaches the API: no tool may ask
        # for nothing, or for a scope the API does not define.
        raise ExchangeFailed('scope', 'invalid_scope')
    started = _now()
    resp = await _post(
        {
            'grant_type': TOKEN_EXCHANGE_GRANT,
            'subject_token': subject_token,
            'subject_token_type': ACCESS_TOKEN_TYPE,
            'requested_token_type': ACCESS_TOKEN_TYPE,
            'resource': config.API_RESOURCE,
            'scope': ' '.join(sorted(scopes)),
        }
    )
    try:
        body: Any = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}

    if resp.status_code == 200:
        token = body.get('access_token')
        expires_in = body.get('expires_in')
        token_type = body.get('token_type')
        if (
            isinstance(token, str)
            and token
            and isinstance(expires_in, int)
            and not isinstance(expires_in, bool)
            and expires_in > 0
            and isinstance(token_type, str)
            and token_type.lower() == 'bearer'
        ):
            logger.info(
                'mcp_exchange_ok scopes=%s expires_in=%d ms=%d',
                ','.join(sorted(scopes)),
                expires_in,
                int((_now() - started) * 1000),
            )
            return IssuedToken(access_token=token, expires_at=started + expires_in)
        logger.error('mcp_exchange_malformed_response')
        raise ExchangeFailed('unavailable', 'malformed_response', retry_after=DEFAULT_RETRY_AFTER_S)

    raise _failure(resp, body)


def _failure(resp: httpx.Response, body: dict[str, Any]) -> ExchangeFailed:
    """The caller-facing failure for a refused exchange, by its OAuth error code."""
    error = body.get('error') if isinstance(body.get('error'), str) else ''
    status = resp.status_code
    if error == 'invalid_grant':
        logger.info('mcp_exchange_refused error=invalid_grant')
        return ExchangeFailed('reauth', error)
    if error == 'invalid_scope':
        logger.error('mcp_exchange_refused error=invalid_scope')
        return ExchangeFailed('scope', error)
    if error == 'interaction_required':
        uri = body.get('approval_uri')
        logger.info('mcp_exchange_refused error=interaction_required')
        return ExchangeFailed('approval', error, approval_uri=uri if isinstance(uri, str) else None)
    if error == 'access_denied':
        logger.info('mcp_exchange_refused error=access_denied')
        return ExchangeFailed('blocked', error)
    if error in _OPERATIONAL_ERRORS:
        # This service's configuration is wrong: loud, because only an operator
        # can fix it, and every OAuth call fails until then.
        logger.error('mcp_exchange_misconfigured error=%s status=%d', error, status)
        return ExchangeFailed('unavailable', error, retry_after=DEFAULT_RETRY_AFTER_S)
    if error in _BUSY_ERRORS or status == 429 or status >= 500:
        logger.warning('mcp_exchange_unavailable error=%s status=%d', error or '-', status)
        return ExchangeFailed('unavailable', error or f'http_{status}', retry_after=_retry_after(resp))
    logger.error('mcp_exchange_unexpected error=%s status=%d', error or '-', status)
    return ExchangeFailed('unavailable', error or f'http_{status}', retry_after=DEFAULT_RETRY_AFTER_S)


async def token_for(subject_token: str, scopes: frozenset[str]) -> IssuedToken:
    """A token for this subject and scope set: the cached one while it has more
    than ``CACHE_MARGIN_S`` left, otherwise a fresh exchange (then cached)."""
    key = _cache_key(subject_token, scopes)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    issued = await exchange(subject_token, scopes)
    _cache.put(key, issued)
    return issued


class CallCredential:
    """The API credential of ONE tool call on the exchange path.

    The first request of the call obtains a token (from the cache or a fresh
    exchange) and every later request of the same call reuses it until it
    expires, so a long deep-check wait costs one exchange at most. When the API
    answers 401 on it, ``renew`` evicts it and exchanges again, once per call;
    a second 401 is the caller's answer.
    """

    def __init__(self, subject_token: str, scopes: frozenset[str]):
        self._subject_token = subject_token
        self.scopes = frozenset(scopes)
        self._token: IssuedToken | None = None
        self._renewed = False
        self._lock = asyncio.Lock()

    def is_for(self, subject_token: str) -> bool:
        return self._subject_token == subject_token

    async def header(self) -> str:
        async with self._lock:
            if self._token is None or self._token.expires_at - _now() <= PIN_SKEW_S:
                self._token = await token_for(self._subject_token, self.scopes)
            return f'Bearer {self._token.access_token}'

    async def renew(self) -> str | None:
        """After a 401: evict the token and exchange again, at most once per call."""
        async with self._lock:
            if self._renewed:
                return None
            self._renewed = True
            key = _cache_key(self._subject_token, self.scopes)
            _cache.evict(key, self._token)
            logger.info('mcp_exchange_renew_after_401')
            self._token = await exchange(self._subject_token, self.scopes)
            _cache.put(key, self._token)
            return f'Bearer {self._token.access_token}'
