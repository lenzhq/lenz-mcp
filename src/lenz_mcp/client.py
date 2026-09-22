"""Async HTTP client for MCP→public-API calls.

Every call forwards the caller's ``Authorization`` header verbatim and stamps
the ``lenz-mcp`` User-Agent. The public API enforces auth/quota and logs the
call — the MCP never validates keys itself (v1).
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from lenz_mcp import config

logger = logging.getLogger(__name__)


# An unpaired UTF-16 surrogate. `json.loads` accepts `"\ud800"` from a tool
# argument, so one can reach us — but it is not a character, it cannot be UTF-8
# encoded, and every outbound use of the value raises `UnicodeEncodeError`
# (which is not an `httpx.HTTPError`): the body encoding, the idempotency-key
# hash, path interpolation. Dropping it is the only way to send the text at
# all, so we drop it rather than fail the call.
_LONE_SURROGATE = re.compile('[\ud800-\udfff]')


def _utf8_safe(value: Any) -> Any:
    """Strip lone surrogates from a value so it can be UTF-8 encoded."""
    if isinstance(value, str):
        return _LONE_SURROGATE.sub('', value)
    if isinstance(value, dict):
        return {_utf8_safe(k): _utf8_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_utf8_safe(v) for v in value]
    return value


def _idem_key(*parts: str) -> str:
    """Deterministic Idempotency-Key for a logical write operation.

    Derived from the operation's content (tool + inputs) so a *retry* of an
    identical call carries the SAME key — letting the public API dedupe it
    (replay the prior response / return the in-flight task) instead of
    re-running and double-charging. A random key per call (the old behavior)
    never matched, so the API could never dedupe. Distinct inputs → distinct
    key → run normally.

    Hashed over the SCRUBBED parts, matching the body `_request` will actually
    send — so the key describes the request, and a text that differs only by an
    unsendable surrogate still replays. `_utf8_safe` is a no-op on every valid
    string, so no existing key changes.
    """
    raw = '\x00'.join(str(p) for p in parts)
    return hashlib.sha256(_utf8_safe(raw).encode()).hexdigest()


class Credential(Protocol):
    """A credential resolved per request instead of forwarded as a string: the
    OAuth token exchange's per-call credential (``exchange.CallCredential``).

    ``header`` is the ``Authorization`` value to send; ``renew`` is asked once
    after the API answers 401 on it and returns a fresh value, or None to give
    up. Either may raise the exchange's own errors, which the tool wrapper maps
    to a tool result.
    """

    async def header(self) -> str: ...

    async def renew(self) -> str | None: ...


# What a tool forwards: the caller's header verbatim, the bridge's assertion,
# a per-call exchanged credential, or nothing.
Authorization = str | Credential | None


@dataclass
class ApiResponse:
    """Normalized result of an MCP→API call.

    ``ok`` is True for 2xx. ``status`` is the HTTP status (0 on transport
    error). ``data`` is the parsed JSON body (``{}`` if absent/unparseable).
    ``headers`` are the response headers with **lowercased keys**, so callers
    can read ``Retry-After`` off a 429 without guessing the server's casing.
    Defaulted so existing construction sites (all keyword-based) are unaffected.
    """

    status: int
    data: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


# Everything outside printable ASCII, plus the parenthesis pair that delimits
# the UA comment and the backslash that could escape it.
#
# This is an allow-list, which is the opposite of the usual rule for a logged
# field, because an allow-list of "what a UA looks like" rejects legitimate
# values (a `/`-bearing UA read as `unloggable`). The difference is that this
# bound is not a guess about shape: httpx encodes header values as ASCII
# (`_models.py::_normalize_header_value`), while Starlette decodes inbound
# headers as latin-1. So a client sending `User-Agent: Cursor/1.0 (café)`
# hands us a str that CANNOT go back out as a header, and the resulting
# UnicodeEncodeError is not an `httpx.HTTPError` — it would escape
# `_request`'s handler and fail every tool call from that client, forever.
# Blocklisting the control range alone would let that through. We are not guessing what a UA looks like; we are enforcing what
# the transport can carry.
_UA_UNSAFE = re.compile(r'[^\x20-\x7e]|[()\\]')


# Keeps `lenz-mcp/1.0 (<origin>)` to a sensible header length.
_ORIGIN_UA_MAX = 120


# The inbound client's User-Agent, bound per request by lenz_mcp.middleware. A
# ContextVar with NO default: "nobody bound one" and "the client sent none" are
# different facts, and only the first is a bug worth shouting about.
_INBOUND_USER_AGENT: contextvars.ContextVar[str] = contextvars.ContextVar('lenz_mcp_inbound_user_agent')

#: Unresolved identity reads since the process started. A count, not just a log
#: line: "every client silently reads as unknown" is only silent if nobody counts.
identity_unresolved_count = 0


def bind_client_user_agent(user_agent: str):
    """Bind the calling client's User-Agent for this request; returns a reset callable."""
    token = _INBOUND_USER_AGENT.set(user_agent or '')
    return lambda: _INBOUND_USER_AGENT.reset(token)


def _is_decade(count: int) -> bool:
    """True at 1, 10, 100, 1000 … — the sampling the identity alarm logs on."""
    while count >= 10 and count % 10 == 0:
        count //= 10
    return count == 1


def note_identity_unresolved(reason: str) -> None:
    """Record that a request reached us with no identity we could bind.

    ERROR, so it reaches Sentry, but not on every call — a broken binding would
    otherwise fire on every request the service takes. It logs on DECADES instead:
    the 1st, 10th, 100th. Once per process would be no alarm at all: the count
    is only read inside the branch that logs, so every line it could emit would
    say `count=1`, and an operator could not tell one stray request from every
    client reading as unknown.
    """
    global identity_unresolved_count
    identity_unresolved_count += 1
    if _is_decade(identity_unresolved_count):
        logger.error(
            'mcp_client_identity_unresolved count=%d reason=%s — every client now reads as unknown: '
            'no card, the short deep-check wait, and no client recorded by the API',
            identity_unresolved_count,
            reason,
        )


def client_user_agent() -> str:
    """The User-Agent of the MCP client driving the current request.

    The genuine client identity (``claude-code/2.1.4``, ``openai-mcp/1.0.0``,
    ``Claude-User``) is only visible on the inbound transport request. It is bound
    per request by ``lenz_mcp.middleware.bind_client_identity`` and read here, with
    no SDK import at all: the old read went through ``request_ctx``, which mcp 2.x
    deleted, inside a swallowed ``except`` — on 2.x that would have returned '' for
    every client, forever, with nothing in any log.

    Returns '' when the client sent no User-Agent (ordinary) and '' when nothing was
    bound (a bug, and reported as one).
    """
    try:
        return _INBOUND_USER_AGENT.get()
    except LookupError:
        note_identity_unresolved('no user agent bound for this request')
        return ''


# `token`, `token/version`, or either followed by ONE parenthesised suffix, and
# nothing else. Anything with a tail, a second group or stray text does not match.
_UA_SHAPE = re.compile(r'^([^/\s()]+)(?:/[^\s()]+)?(?:\s+\(([^()]*)\))?$')


def client_user_agent_token() -> str:
    """The leading token of the inbound client's User-Agent, or ''.

    ``Claude-User/1.0`` → ``Claude-User``; ``openai-mcp/1.0.0 (Codex)`` →
    ``openai-mcp``. Coarse on purpose: it says which VENDOR's client this is.
    What that client can do is `client_identity()` — the token alone is not
    enough to decide anything, because one token covers clients with different
    limits (see below).
    """
    return client_user_agent().split('/', 1)[0].strip()


def client_identity() -> str:
    """The client's leading token plus its parenthesised suffix, or ''.

    ``openai-mcp/1.0.0`` → ``openai-mcp``
    ``openai-mcp/1.0.0 (Codex)`` → ``openai-mcp (Codex)``
    ``openai-mcp/1.0.0 (Responses API)`` → ``openai-mcp (Responses API)``

    The suffix is load-bearing and the token alone is a TRAP. Measured
    2026-09-18: OpenAI's ChatGPT app and its Responses-API MCP connector both
    send ``openai-mcp``, and their tool-call ceilings are 119.8 s and 59.8 s.
    Worse, the API connector RETRIES a dropped call once before reporting 504,
    so a wait tuned for the app would make every slow deep check cost that
    developer two dropped calls and an error. This is the ONE identity the
    deep-check wait, the card gate and the card's delivery hint all read, so a
    client is never matched one way here and another way there.
    """
    ua = client_user_agent().strip()
    if not ua:
        return ''
    # Fail CLOSED on a shape we do not recognise. Returning the bare token for
    # `openai-mcp/1.0.0 (Responses API) proxy/1.0` would hand a proxied
    # connector the ChatGPT app's 100 s wait, its card and its paid app-only
    # tools — the privileged answer for the least identifiable client. An unparsed User-Agent is returned verbatim instead: it
    # cannot match a row in any table, so every gate reads it as unknown.
    match = _UA_SHAPE.match(ua)
    if not match:
        return ua
    token, suffix = match.group(1), (match.group(2) or '').strip()
    return f'{token} ({suffix})' if suffix else token


def _user_agent() -> str:
    """Our UA, carrying the origin client in a parenthetical comment.

    ``lenz-mcp/1.0 (claude-code/2.1.4)``. This is the usual convention for one
    client calling through another (``lenz-zapier/1.0.0 (lenz-io-node 2.6.0)``):
    the LEADING TOKEN names the calling product and its version, and the
    parenthetical carries the inner identity. So the API still sees an MCP
    call, and also learns which client actually made it.

    It also covers the API-key door, which ``mcp_auth_ok`` deliberately does
    not log (``oauth.py``: that branch validates nothing, so a line there would
    fire unauthenticated and carry no subject).

    The value is attacker-controlled, so everything outside printable ASCII is
    stripped — along with the parentheses and backslash that delimit and could
    escape the comment — and the result is truncated before it reaches a
    header. See ``_UA_UNSAFE`` for why that bound is ASCII and not a
    blocklist of the obviously-dangerous characters.
    """
    origin = _UA_UNSAFE.sub('', client_user_agent()).strip()[:_ORIGIN_UA_MAX].strip()
    return f'{config.USER_AGENT} ({origin})' if origin else config.USER_AGENT


def _headers(authorization: str | None, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {
        'User-Agent': _user_agent(),
        'Accept': 'application/json',
    }
    # Only the write tools (assess/verify/select) pass a content-derived key;
    # GETs (status/usage) are idempotent by nature and need none.
    if idempotency_key:
        headers['Idempotency-Key'] = idempotency_key
    if authorization:
        headers['Authorization'] = authorization
    return headers


# One shared client so TLS/keep-alive connections are reused across tool calls
# instead of paying a fresh handshake per request. Lazily created; lives for the
# process (a long-running ASGI server), so no explicit close.
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=config.DEFAULT_TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
        )
    return _http_client


async def _send(
    method: str,
    url: str,
    path: str,
    authorization: str | None,
    *,
    json: dict[str, Any] | None,
    timeout: float,
    idempotency_key: str | None,
) -> httpx.Response | None:
    """One HTTP request; None on a transport failure (logged)."""
    try:
        return await _get_client().request(
            method,
            url,
            json=_utf8_safe(json),
            headers=_headers(authorization, idempotency_key),
            timeout=timeout,
        )
    # Deliberately wider than `httpx.HTTPError`, which is narrower than what
    # this body can raise. `httpx.InvalidURL` is not an `HTTPError` at
    # all — its `__mro__` is `(InvalidURL, Exception, ...)` — and header/URL
    # encoding raises `UnicodeError`. Anything not caught here escapes the tool
    # body and reaches the model as a raw `ToolError` string with no log line,
    # so the failure is opaque to the user and invisible to us. The two values
    # with a realistic trigger are screened before they get here — the bearer
    # by `server.requires_auth`, the ids by `server._SENDABLE_ID_RE` — so this
    # is the backstop, not the diagnosis.
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:
        logger.warning('mcp_api_transport_error method=%s path=%s err=%s', method, path, exc)
        return None


async def _request(
    method: str,
    path: str,
    authorization: Authorization,
    *,
    json: dict[str, Any] | None = None,
    timeout: float = config.DEFAULT_TIMEOUT,
    idempotency_key: str | None = None,
) -> ApiResponse:
    url = f'{config.API_BASE_URL}{path}'
    credential: Credential | None = None
    header: str | None
    if authorization is None or isinstance(authorization, str):
        header = authorization
    else:
        credential = authorization
        header = await credential.header()
    resp = await _send(method, url, path, header, json=json, timeout=timeout, idempotency_key=idempotency_key)
    if resp is not None and resp.status_code == 401 and credential is not None:
        # An exchanged token the API no longer accepts (revoked, or expired
        # early): exchange again, once, and resend. The API refused the first
        # request outright, so resending a write repeats nothing.
        renewed = await credential.renew()
        if renewed is not None:
            resp = await _send(method, url, path, renewed, json=json, timeout=timeout, idempotency_key=idempotency_key)
    if resp is None:
        return ApiResponse(status=0, data={})

    try:
        body = resp.json()
        if not isinstance(body, dict):
            body = {'_raw': body}
    except ValueError:
        body = {}
    return ApiResponse(
        status=resp.status_code,
        data=body,
        headers={k.lower(): v for k, v in resp.headers.items()},
    )


async def assess(
    authorization: Authorization, *, text: str = '', claims: list[str] | None = None, language: str
) -> ApiResponse:
    # One text (`claim`, expanded server-side) or a list (`claims`, one row per
    # item). The idempotency key covers whichever was sent — the API rejects a
    # key reused with a different body — joined on a separator no claim
    # contains, so ["a b"] and ["a", "b"] never collide.
    if claims:
        body: dict = {'claims': list(claims), 'language': language}
        key = _idem_key('assess', 'claims', '\x1f'.join(claims), language)
    else:
        body = {'claim': text, 'language': language}
        key = _idem_key('assess', text, language)
    return await _request(
        'POST',
        '/assess',
        authorization,
        json=body,
        timeout=config.ASSESS_TIMEOUT,
        idempotency_key=key,
    )


async def verify(
    authorization: Authorization, *, text: str, language: str, depth: str = 'standard', retry_of: str | None = None
) -> ApiResponse:
    # `depth` is part of the idempotency key because the API rejects a key
    # reused with a different body (422, body-hash mismatch): without it, a
    # `low` submission of a claim just run at `standard` would come back as
    # `invalid_request` instead of running. The default depth keeps the
    # pre-depth key shape, so a repeat of a claim submitted before this
    # parameter existed still replays (24h) instead of being charged again.
    key_parts = ('verify', text, language) if depth == 'standard' else ('verify', text, language, depth)
    # A retry of a FAILED run needs its own key: the API replays a key's first
    # response for 24 h, failed runs included, so the same key would hand back the
    # failed task forever. Deterministic from the failed task_id, so a double
    # click on "Try again" still joins one run (src/lenz_mcp/mcp_card.py).
    if retry_of:
        key_parts = (*key_parts, 'retry_of', retry_of)
    return await _request(
        'POST',
        '/verify',
        authorization,
        json={'claim': text, 'language': language, 'depth': depth},
        idempotency_key=_idem_key(*key_parts),
    )


async def verify_status(authorization: Authorization, *, task_id: str) -> ApiResponse:
    return await _request('GET', f'/verify/status/{task_id}', authorization)


async def verification_detail(authorization: Authorization, *, verification_id: str) -> ApiResponse:
    """A stored result by its 8-hex verification_id (GET /verifications/{id}).

    The endpoint takes an OPTIONAL bearer: with the caller's key it also serves
    their own private claims; without one only public / unlisted ones.
    """
    return await _request('GET', f'/verifications/{verification_id}', authorization)


async def list_verifications(authorization: Authorization, *, page_size: int) -> ApiResponse:
    """The caller's own completed verifications, newest first (GET /verifications).

    The API scopes the list to the credential, and a row exists only once a run
    has completed. A GET: no idempotency key, no credit.
    """
    return await _request('GET', f'/verifications?page=1&page_size={int(page_size)}', authorization)


async def select(authorization: Authorization, *, task_id: str, texts: list[str]) -> ApiResponse:
    return await _request(
        'POST',
        f'/verify/{task_id}/select',
        authorization,
        json={'texts': texts},
        idempotency_key=_idem_key('select', task_id, *texts),
    )


async def ask(authorization: Authorization, *, verification_id: str, message: str, language: str = '') -> ApiResponse:
    # No idempotency key, deliberately, even though the REST endpoint honours
    # one. `ask` is a conversational append —
    # asking the same question twice is a legit second turn, and it gets a
    # different answer because the first exchange is now history. Every key
    # `_idem_key` derives is content-derived, so sending one here would
    # manufacture client-side exactly the implicit key the endpoint refuses to
    # derive server-side: the second ask would replay the first reply for the
    # full response TTL. A key belongs on a RETRY of one logical ask, which is
    # a thing only a caller that owns the retry can tell apart from a re-ask.
    # Longer timeout — /ask is synchronous and can block on source summaries
    # plus the LLM reply.
    return await _request(
        'POST',
        f'/ask/{verification_id}',
        authorization,
        json={'message': message, 'language': language},
        timeout=config.ASK_TIMEOUT,
    )


async def me_usage(authorization: Authorization) -> ApiResponse:
    return await _request('GET', '/me/usage', authorization)
