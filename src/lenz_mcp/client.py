"""Async client for MCP→public-API calls, through the official ``lenz-io`` SDK.

Every call sends the caller's bearer token (read from their ``Authorization``
header) and stamps the ``lenz-mcp`` User-Agent. The public API enforces
auth/quota and logs the call — the MCP never validates keys itself (v1).
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import httpx
from lenz_io import (
    AsyncLenz,
    LenzApiVersionError,
    LenzConnectionError,
    LenzError,
    LenzInvalidResponseError,
)
from pydantic import BaseModel, ValidationError

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

    Hashed over the SCRUBBED parts, matching the body the client will actually
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


# What a tool forwards: the caller's header (its bearer token is sent), a per-call exchanged
# credential, or nothing.
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
# the request's handler and fail every tool call from that client, forever.
# Blocklisting the control range alone would let that through. We are not guessing what a UA looks like; we are enforcing what
# the transport can carry.
_UA_UNSAFE = re.compile(r'[^\x20-\x7e]|[()\\]')


# Keeps `lenz-mcp/1.0 (<origin>)` to a sensible header length.
_ORIGIN_UA_MAX = 120


# The characters a leading product token may NOT contain: the `/` before a
# version, whitespace before a suffix, and the parentheses that delimit one.
# `_UA_SHAPE`'s first group is the same class, so the token and the identity
# always agree about where the token ends.
_UA_TOKEN_END = re.compile(r'[/\s()]')

# `token`, `token/version`, or either followed by ONE parenthesised suffix, and
# nothing else. Anything with a tail, a second group or stray text does not match.
_UA_SHAPE = re.compile(r'^([^/\s()]+)(?:/[^\s()]+)?(?:\s+\(([^()]*)\))?$')


def parse_vendor_token(user_agent: str) -> str:
    """The leading token of a User-Agent, or ''.

    ``Claude-User/1.0`` → ``Claude-User``; ``openai-mcp/1.0.0 (Codex)`` →
    ``openai-mcp``. Coarse on purpose: it says which VENDOR's client this is,
    and nothing about what that client can do.

    It stops at WHITESPACE as well as `/`, because a version is optional and a
    suffix is not tied to one: `Claude-User` ships versionless today, so
    `openai-mcp (Codex)` is a shape a host can send. Splitting on `/` alone
    returned that whole string, which matches no vendor — the card would go
    away on its legacy requests and its delivery hint would flip to the silent
    push, which is the exact failure this release exists to prevent.

    Unlike `parse_identity` it does NOT fail closed on an unparsable value,
    because its two readers want exactly the coarse answer: the card's delivery
    hint (a proxied ChatGPT still needs `message`, or the model contradicts the
    card in front of the user) and the card's legacy-era exposure fallback.
    Neither hands a client anything a vendor's own clients do not already get.
    """
    return _UA_TOKEN_END.split((user_agent or '').strip(), 1)[0]


def parse_identity(user_agent: str) -> str:
    """A User-Agent's leading token plus its parenthesised suffix, or ''.

    ``openai-mcp/1.0.0`` → ``openai-mcp``
    ``openai-mcp/1.0.0 (Codex)`` → ``openai-mcp (Codex)``
    ``openai-mcp/1.0.0 (Responses API)`` → ``openai-mcp (Responses API)``

    The suffix is load-bearing and the token alone is a TRAP for anything sized
    to a client's limits. Measured 2026-09-18: OpenAI's ChatGPT app and its
    Responses-API MCP connector both send ``openai-mcp``, and their tool-call
    ceilings are 119.8 s and 59.8 s. Worse, the API connector RETRIES a dropped
    call once before reporting 504, so a wait tuned for the app would make every
    slow deep check cost that developer two dropped calls and an error.

    This is the key of the deep-check wait, and of nothing else. The card's two
    decisions moved OFF it: keyed here, a new suffix on a known app (which is
    what `openai-mcp/1.0.0 (ChatGPT)` was) silently withdraws the card.
    """
    ua = (user_agent or '').strip()
    if not ua:
        return ''
    # Fail CLOSED on a shape we do not recognise. Returning the bare token for
    # `openai-mcp/1.0.0 (Responses API) proxy/1.0` would hand a proxied
    # connector the ChatGPT app's 100 s wait — the privileged answer for the
    # least identifiable client, and the one whose failure is a hard error in
    # the chat. An unparsed User-Agent is returned verbatim instead: it cannot
    # match a row in the wait table, so the wait reads it as unknown.
    match = _UA_SHAPE.match(ua)
    if not match:
        return ua
    token, suffix = match.group(1), (match.group(2) or '').strip()
    return f'{token} ({suffix})' if suffix else token


# ── the client profile ───────────────────────────────────────────────
# ONE resolved description of the client driving this request, bound once by
# `lenz_mcp.middleware`, from which every per-client decision is a pure
# function. Before this there were three: the card gate, the card's delivery
# hint and the deep-check wait each re-derived the client from a bare
# User-Agent ContextVar, in three modules, and a fourth reader would have done
# it a fourth way. They also all keyed on the same string — the full identity —
# so when OpenAI put a new suffix on the ChatGPT app's User-Agent
# (`openai-mcp/1.0.0 (ChatGPT)`), all three stopped matching at once.
#
# The decisions now read DIFFERENT fields of the profile, each the narrowest
# fact that answers it, and they live beside the thing they decide:
#
#   card exposure  → `declares_apps`, the client's own MCP Apps declaration
#                    (mcp_card.card_active), with the vendor token as the
#                    legacy-era and failed-read fallback;
#   card delivery  → `vendor_token` (mcp_card.card_delivery);
#   deep-check wait→ `identity` (server._verify_wait_seconds), which stays a
#                    hand-maintained table because a wait that is too LONG is a
#                    hard error in the chat, so it must fail closed.

#: A client declares MCP Apps support on this request, and does not.
DECLARATION_FROM_REQUEST = 'request'
#: No declaration on this request. Not a failure: the 2025 era declares
#: capabilities at the handshake, the server is stateless, so every in-session
#: legacy request carries none. Measured against mcp 2.2.0: on the legacy
#: `initialize` too, since the connection's capabilities are recorded by the
#: handler AFTER middleware has run.
DECLARATION_ABSENT = 'none'
#: The declaration could not be read (the SDK predicate raised). Distinct from
#: absent, because it is a bug and it is logged as one.
DECLARATION_READ_FAILED = 'read_failed'

ERA_MODERN = 'modern'
ERA_LEGACY = 'legacy'

# The first protocol revision with no handshake, where the client declares its
# capabilities in every request's `_meta`. Versions are calendar dates, so they
# compare correctly as text (the same rule `protocol_log._era_requested` uses).
_FIRST_MODERN_VERSION = '2026-07-28'


@dataclass(frozen=True)
class ClientProfile:
    """Everything this request says about the client that made it.

    Frozen, and built once per request: a decision reads a field, never the
    request. That is what makes the three decisions testable with a constructed
    profile and no request in flight, and what keeps a fourth decision from
    inventing a fourth way to ask who the client is.
    """

    #: The raw `User-Agent` header, '' when the client sent none.
    user_agent: str
    #: Leading token plus parenthesised suffix, or the raw UA when it does not
    #: parse. See `parse_identity` for why an unparsed value is kept verbatim.
    identity: str
    #: The raw leading token: which VENDOR's client this is. '' with no UA.
    vendor_token: str
    #: True/False when this request carried a declaration, None when it did not
    #: or the read failed — `declaration_source` says which.
    declares_apps: bool | None
    declaration_source: str
    era: str
    #: The clientInfo NAME, when this request carried one. NOT the User-Agent
    #: and never a substitute for it: `Claude-User` is sent by both the Claude
    #: app and Claude Code, which want opposite card answers, and the name is
    #: the only thing that tells them apart in a log line. Absent on a 2025-era
    #: in-session request, where the handshake's identity did not survive this
    #: stateless server. Client-controlled, so logged through `log_token` like
    #: everything else and READ for nothing.
    client_name: str = ''

    @classmethod
    def from_user_agent(
        cls,
        user_agent: str,
        *,
        declares_apps: bool | None = None,
        declaration_source: str = DECLARATION_ABSENT,
        era: str = ERA_LEGACY,
        client_name: str = '',
    ) -> ClientProfile:
        ua = user_agent or ''
        return cls(
            user_agent=ua,
            identity=parse_identity(ua),
            vendor_token=parse_vendor_token(ua),
            declares_apps=declares_apps,
            declaration_source=declaration_source,
            era=era,
            client_name=client_name,
        )

    @property
    def declared(self) -> str:
        """One token for the log line: `yes` / `no` / `none` / `read_failed`."""
        if self.declaration_source != DECLARATION_FROM_REQUEST:
            return self.declaration_source
        return 'yes' if self.declares_apps else 'no'


@dataclass(frozen=True)
class KnownIdentity:
    """An identity we have MEASURED, and what we measured about it.

    The registry below is the ONLY place an identity string is spelled: the
    wait table's keys, the goldens' client matrix and the probe server's
    constants are all pinned to it, so an identity can never again be known to
    one table and unknown to another — which is exactly how a new User-Agent
    suffix got a card decision and no wait row.
    """

    identity: str
    vendor: str
    #: Which surface of that vendor this is, in plain words.
    surface: str
    #: When it was last measured, and where the measurement is written down.
    measured: str
    source: str
    #: What we measured it to declare, for the goldens' declaration axis. None
    #: where it varies by build or has not been measured.
    declares_apps: bool | None = None


#: Measured client identities. Adding one here is half a change: give it a row
#: in `config.VERIFY_WAIT_SECONDS_BY_IDENTITY` too, or the pin test fails.
_PROBE_README = 'scripts/probe/README.md'
KNOWN_IDENTITIES: dict[str, KnownIdentity] = {
    entry.identity: entry
    for entry in (
        KnownIdentity(
            identity='Claude-User',
            vendor='Claude-User',
            surface='claude.ai, Claude Desktop and the directory connector; also Claude Code',
            measured='2026-09-18',
            source=_PROBE_README,
            # Both of the Claude app's clients declare the extension; Claude
            # Code sends the same User-Agent and declares no UI at all. One
            # identity, two capability profiles — which is why the card reads
            # the declaration and not this string.
            declares_apps=None,
        ),
        KnownIdentity(
            identity='openai-mcp',
            vendor='openai-mcp',
            surface='the ChatGPT app',
            measured='2026-09-18',
            source=_PROBE_README,
            declares_apps=True,
        ),
        KnownIdentity(
            identity='openai-mcp (ChatGPT)',
            vendor='openai-mcp',
            surface='the ChatGPT app, which grew this suffix on 2026-09-23',
            measured='2026-09-23',
            source=_PROBE_README,
            declares_apps=True,
        ),
        KnownIdentity(
            identity='openai-mcp (Codex)',
            vendor='openai-mcp',
            surface='the ChatGPT app, model-side calls',
            measured='2026-09-18',
            source=_PROBE_README,
            declares_apps=True,
        ),
        KnownIdentity(
            identity='openai-mcp (Responses API)',
            vendor='openai-mcp',
            surface="OpenAI's Responses-API MCP connector",
            measured='2026-09-18',
            source=_PROBE_README,
            declares_apps=True,
        ),
    )
}


# The inbound client's profile, bound per request by lenz_mcp.middleware. A
# ContextVar with NO default: "nobody bound one" and "the client sent none" are
# different facts, and only the first is a bug worth shouting about.
_INBOUND_PROFILE: contextvars.ContextVar[ClientProfile] = contextvars.ContextVar('lenz_mcp_inbound_client_profile')

#: The profile a request with nothing bound reads as. Privileged nowhere: no
#: identity matches a wait row, no token matches a card vendor.
UNKNOWN_PROFILE = ClientProfile.from_user_agent('')

#: Unresolved identity reads since the process started. A count, not just a log
#: line: "every client silently reads as unknown" is only silent if nobody counts.
identity_unresolved_count = 0


def bind_client_profile(profile: ClientProfile):
    """Bind this request's resolved client profile; returns a reset callable."""
    token = _INBOUND_PROFILE.set(profile)
    return lambda: _INBOUND_PROFILE.reset(token)


def bind_client_user_agent(user_agent: str, **kwargs):
    """Bind a profile built from a User-Agent alone: no declaration, legacy era.

    The shape a 2025-era in-session request really has, and the one every
    caller outside the middleware wants. `bind_client_profile` is the full form.
    """
    return bind_client_profile(ClientProfile.from_user_agent(user_agent, **kwargs))


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


def client_profile() -> ClientProfile:
    """The resolved profile of the MCP client driving the current request.

    The ONE place a per-client decision asks who the client is. Bound per
    request by ``lenz_mcp.middleware.bind_client_profile``, with no SDK import
    here at all: the old read went through ``request_ctx``, which mcp 2.x
    deleted, inside a swallowed ``except`` — on 2.x that would have returned ''
    for every client, forever, with nothing in any log.

    Returns ``UNKNOWN_PROFILE`` when nothing was bound (a bug, and reported as
    one). A client that simply sent no User-Agent is an ordinary profile whose
    ``user_agent`` is '' — a different fact, and not an error.
    """
    try:
        return _INBOUND_PROFILE.get()
    except LookupError:
        note_identity_unresolved('no client profile bound for this request')
        return UNKNOWN_PROFILE


def client_user_agent() -> str:
    """The User-Agent of the MCP client driving the current request, or ''.

    A thin reader of `client_profile()`, kept because the outbound UA builder
    and the log lines want the raw header and nothing else.
    """
    return client_profile().user_agent


def client_user_agent_token() -> str:
    """This request's vendor token. A thin reader of `client_profile()`."""
    return client_profile().vendor_token


def client_identity() -> str:
    """This request's identity. A thin reader of `client_profile()`."""
    return client_profile().identity


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


# ── the API calls, through the lenz-io SDK ───────────────────────────
# Every call builds its own `AsyncLenz` over the ONE shared `httpx.AsyncClient`
# below: the caller's token, our User-Agent and the call's timeout belong to that
# call, and nothing is cached between calls. Building one costs microseconds; a
# cached one would keep a replaced `_http_client` (tests, the API's contract
# harness) or another caller's token. The SDK sends the API version header,
# encodes the body, maps the answer to its typed models and errors; this module
# turns both back into the `ApiResponse` the tools have always read.
#
# What stays here and not in the SDK: the content-derived idempotency keys
# (`_idem_key`), the surrogate scrub (the key and the body describe the same
# text), the 401 renewal of an exchanged token, and every wait and poll loop
# (server.py). The SDK never retries (`max_retries=0`): a tool call has its own
# budget, and a retry it did not ask for would spend it.

#: What a failed call says when the API's answer could not be read. Never an
#: `ok` status: `ApiResponse.ok` is status-only, so a 2xx here would read as success.
INVALID_RESPONSE_CODE = 'invalid_response'
INVALID_RESPONSE_STATUS = 502
_INVALID_RESPONSE_DETAIL = 'The Lenz API sent an answer the connector could not read. Please retry shortly.'

#: What a call says when the API answered in another API version. The SDK reads
#: exactly one version (config.API_VERSION) and refuses every answer in another,
#: error answers included, so a 402 or a 429 from an API that does not serve this
#: version arrives as this, without its own status.
API_VERSION_UNSUPPORTED_CODE = 'api_version_unsupported'
_API_VERSION_DETAIL = 'The Lenz API answered in a version this connector does not read. Please retry later.'


def _invalid_response(op: str, status: int | str) -> ApiResponse:
    logger.warning('mcp_api_invalid_response op=%s status=%s', op, status)
    return ApiResponse(
        status=INVALID_RESPONSE_STATUS, data={'code': INVALID_RESPONSE_CODE, 'detail': _INVALID_RESPONSE_DETAIL}
    )


# One shared client so TLS/keep-alive connections are reused across tool calls
# instead of paying a fresh handshake per request. Lazily created; lives for the
# process (a long-running ASGI server), so no explicit close. Every SDK instance
# borrows it, and a borrowed client is never closed by the SDK.
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=config.DEFAULT_TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
            # The SDK sets the version, content type and credential per request,
            # but leaves Accept to the client it borrows.
            headers={'Accept': 'application/json'},
        )
    return _http_client


class _Recorder:
    """The shared client as one call's SDK sees it: every request passes through
    unchanged, and the last answer is kept.

    The SDK hands back a model or an exception, not the response. The response
    is what tells an answer we can use from one we cannot (a 204, a redirect
    whose body happens to parse, an empty body), and what an error result is
    built from, byte for byte as before: its status, its body and its headers,
    which the tools read (a 429's Retry-After). One per call, so concurrent calls
    never see each other's answers.
    """

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self.response: httpx.Response | None = None

    def __getattr__(self, name: str) -> Any:
        # `timeout` and `headers`, which the SDK reads off a borrowed client.
        return getattr(self._http, name)

    async def request(self, *args: Any, **kwargs: Any) -> httpx.Response:
        self.response = None
        self.response = await self._http.request(*args, **kwargs)
        return self.response


def _bearer_token(header: str | None) -> str | None:
    """The token of a ``Bearer`` Authorization value, or None.

    The scheme is matched case-insensitively and surrounding whitespace is
    dropped. Anything else (another scheme, a bare token, an empty one) is no
    credential: the SDK sends ``Bearer <token>`` itself, so a value that is not a
    bearer cannot be forwarded as it came.
    """
    scheme, _, token = (header or '').strip().partition(' ')
    if scheme.lower() != 'bearer':
        return None
    return token.strip() or None


def _from_response(response: httpx.Response) -> ApiResponse:
    """An error answer as the tools read it: its status, its JSON object body
    (a non-object kept under ``_raw``, a non-JSON body as ``{}``) and its headers
    with lowercased names."""
    try:
        body = response.json()
        if not isinstance(body, dict):
            body = {'_raw': body}
    except ValueError:
        body = {}
    return ApiResponse(
        status=response.status_code,
        data=body,
        headers={k.lower(): v for k, v in response.headers.items()},
    )


def _usable_success(response: httpx.Response) -> bool:
    """Whether a response the SDK read as a success carries a body we can use.

    No endpoint the connector calls answers 204 or 205 or an empty body, and a
    redirect is never followed: any of those is an answer from something other
    than the API (a proxy, a load balancer), not a result.
    """
    return (
        200 <= response.status_code < 300 and response.status_code not in (204, 205) and bool(response.content.strip())
    )


Invoke = Callable[[AsyncLenz, dict[str, Any]], Awaitable[BaseModel]]


async def _attempt(
    op: str, path: str, header: str | None, invoke: Invoke, *, timeout: float, anonymous: bool
) -> ApiResponse:
    """One request through the SDK, as an `ApiResponse`.

    ``anonymous``: the endpoint also answers without a credential, and the call
    has none (``header`` is None), so none is sent.
    """
    token = _bearer_token(header)
    if token is None and not (anonymous and header is None):
        # Refused here, never sent: the same answer as the API's own 401. Never
        # an SDK built without a key, which would read LENZ_API_KEY from the
        # environment, or send a call with nobody's credential.
        return ApiResponse(status=401, data={})
    recorder = _Recorder(_get_client())
    options: dict[str, Any] = {'timeout': timeout, 'extra_headers': {'User-Agent': _user_agent()}}
    try:
        sdk = AsyncLenz(
            # '' and not None: None reads LENZ_API_KEY.
            api_key=token or '',
            base_url=config.API_BASE_URL,
            http_client=cast(httpx.AsyncClient, recorder),
            max_retries=0,
            legacy_aliases=False,
        )
        model = await invoke(sdk, options)
    except LenzApiVersionError as exc:
        served = _UA_UNSAFE.sub('', str(exc.api_version or ''))[:40]
        logger.warning('mcp_api_version_mismatch op=%s served=%s', op, served)
        return ApiResponse(
            status=INVALID_RESPONSE_STATUS, data={'code': API_VERSION_UNSUPPORTED_CODE, 'detail': _API_VERSION_DETAIL}
        )
    except LenzConnectionError as exc:
        # A timeout or a connection that failed: no answer at all. (Before the
        # catch-all below: these carry status 0 and no response.)
        logger.warning('mcp_api_transport_error op=%s path=%s err=%s', op, path, type(exc.__cause__ or exc).__name__)
        return ApiResponse(status=0, data={})
    except LenzInvalidResponseError as exc:
        # Not JSON, on any status (a proxy's page, an empty body, a redirect).
        if recorder.response is not None and recorder.response.status_code >= 400:
            # An error answer whose body is not JSON (a 500 page): its status is
            # still the answer, as it always was.
            return _from_response(recorder.response)
        return _invalid_response(op, exc.status_code)
    except LenzError as exc:
        if recorder.response is not None:
            return _from_response(recorder.response)
        # An error raised before anything was sent (none is expected: the key is
        # checked above). Never the SDK's own words, which name its parameters.
        logger.warning('mcp_api_local_error op=%s error=%s', op, type(exc).__name__)
        return ApiResponse(status=exc.status_code or 0, data={})
    except ValidationError:
        # JSON that is not the shape this endpoint answers with (an array, a
        # scalar, a wrong type).
        return _invalid_response(op, recorder.response.status_code if recorder.response is not None else 'none')
    # Deliberately wider than `httpx.HTTPError`, which is narrower than what a
    # request can raise. `httpx.InvalidURL` is not an `HTTPError` at all, and
    # header encoding raises `UnicodeError`. Anything not caught here escapes the
    # tool body and reaches the model as a raw `ToolError` string with no log
    # line. The two values with a realistic trigger are screened before they get
    # here — the bearer by `server.requires_auth`, the ids by
    # `server._SENDABLE_ID_RE` — so this is the backstop, not the diagnosis.
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:
        logger.warning('mcp_api_transport_error op=%s path=%s err=%s', op, path, type(exc).__name__)
        return ApiResponse(status=0, data={})
    except ValueError as exc:
        # The SDK refused the arguments before sending (an empty id, a page size
        # out of range). The tools screen these first, so this is a backstop;
        # its message names SDK parameters and is not passed on.
        logger.warning('mcp_api_request_refused op=%s error=%s', op, type(exc).__name__)
        return ApiResponse(status=422, data={'detail': 'The request was invalid.'})

    response = recorder.response
    if response is not None and response.status_code >= 400:
        # The SDK settled an error answer itself (a citation check's 409 naming
        # the check a resend already started). The tools read that answer as it
        # came: the status and the body that names the check.
        return _from_response(response)
    if response is None:
        return _invalid_response(op, 'none')
    if not _usable_success(response):
        return _invalid_response(op, response.status_code)
    return ApiResponse(
        status=response.status_code,
        data=model.model_dump(mode='json', exclude_unset=True),
        headers={k.lower(): v for k, v in response.headers.items()},
    )


async def _call(
    op: str,
    path: str,
    authorization: Authorization,
    invoke: Invoke,
    *,
    timeout: float = config.DEFAULT_TIMEOUT,
    anonymous: bool = False,
) -> ApiResponse:
    credential: Credential | None = None
    header: str | None
    if authorization is None or isinstance(authorization, str):
        header = authorization
    else:
        credential = authorization
        header = await credential.header()
    resp = await _attempt(op, path, header, invoke, timeout=timeout, anonymous=anonymous)
    if resp.status == 401 and credential is not None:
        # An exchanged token the API no longer accepts (revoked, or expired
        # early): exchange again, once, and resend the same request (same body,
        # same key). The API refused the first request outright, so resending
        # a write repeats nothing.
        renewed = await credential.renew()
        if renewed is not None:
            resp = await _attempt(op, path, renewed, invoke, timeout=timeout, anonymous=anonymous)
    return resp


def effective_language(language: str) -> str:
    """The language a request carries: the caller's code, else ``auto``.

    Applied where a request is built, so the body and the idempotency key
    describe the same thing: ``auto``, an explicit code and nothing are three
    different requests with three keys.
    """
    return language or 'auto'


async def assess(
    authorization: Authorization,
    *,
    text: str = '',
    claims: list[str] | None = None,
    language: str,
    suggest_rewrite: bool = False,
) -> ApiResponse:
    # One text (sent as `text`, expanded server-side) or a list (`claims`, one
    # row per item). The idempotency key covers whichever was sent — the API
    # rejects a key reused with a different body — joined on a separator no
    # claim contains, so ["a b"] and ["a", "b"] never collide.
    #
    # `suggest_rewrite` asks for the claim rewritten with its wrong part
    # corrected, on the rows that have one. It is part of the body, so it is
    # part of the key: a call that does not send it keeps the key it always had
    # (a repeat inside the replay window still replays), and one that does gets
    # its own, since the API refuses a key reused with a different body.
    language = effective_language(language)
    if claims:
        parts: tuple[str, ...] = ('assess', 'claims', '\x1f'.join(claims), language)
    else:
        parts = ('assess', text, language)
    if suggest_rewrite:
        parts = (*parts, 'suggest_rewrite')
    key = _idem_key(*parts)
    items = [_utf8_safe(c) for c in claims] if claims else None
    single = _utf8_safe(text)

    async def invoke(sdk: AsyncLenz, options: dict[str, Any]) -> BaseModel:
        if items is not None:
            return await sdk.assess(
                claims=items, language=language, suggest_rewrite=suggest_rewrite, idempotency_key=key, **options
            )
        return await sdk.assess(
            text=single, language=language, suggest_rewrite=suggest_rewrite, idempotency_key=key, **options
        )

    return await _call('assess', '/assess', authorization, invoke, timeout=config.assess_timeout(client_identity()))


async def verify(
    authorization: Authorization, *, text: str, language: str, depth: str = 'standard', retry_of: str | None = None
) -> ApiResponse:
    # `depth` is part of the idempotency key because the API rejects a key
    # reused with a different body (422, body-hash mismatch): without it, a
    # `low` submission of a claim just run at `standard` would come back as
    # `invalid_request` instead of running. The default depth keeps the
    # pre-depth key shape, so a repeat of a claim submitted before this
    # parameter existed still replays (24h) instead of being charged again.
    language = effective_language(language)
    key_parts = ('verify', text, language) if depth == 'standard' else ('verify', text, language, depth)
    # A retry of a FAILED run needs its own key: the API replays a key's first
    # response for 24 h, failed runs included, so the same key would hand back the
    # failed task forever. Deterministic from the failed task_id, so a double
    # click on "Try again" still joins one run (src/lenz_mcp/mcp_card.py).
    if retry_of:
        key_parts = (*key_parts, 'retry_of', retry_of)
    key = _idem_key(*key_parts)
    claim = _utf8_safe(text)

    async def invoke(sdk: AsyncLenz, options: dict[str, Any]) -> BaseModel:
        return await sdk.verify(claim=claim, language=language, depth=depth, idempotency_key=key, **options)

    return await _call('verify', '/verify', authorization, invoke)


async def verify_status(authorization: Authorization, *, task_id: str) -> ApiResponse:
    async def invoke(sdk: AsyncLenz, options: dict[str, Any]) -> BaseModel:
        return await sdk.get_status(task_id, **options)

    return await _call('verify_status', '/verify/status/{task_id}', authorization, invoke)


async def verification_detail(authorization: Authorization, *, verification_id: str) -> ApiResponse:
    """A stored result by its 8-hex verification_id (GET /verifications/{id}).

    The endpoint takes an OPTIONAL bearer: with the caller's key it also serves
    their own private claims; without one only public / unlisted ones. So a call
    with no credential at all is sent without one, while a credential that is
    not a bearer is refused like on every other endpoint.
    """

    async def invoke(sdk: AsyncLenz, options: dict[str, Any]) -> BaseModel:
        return await sdk.verifications.get(verification_id, **options)

    return await _call('verification_detail', '/verifications/{id}', authorization, invoke, anonymous=True)


async def list_verifications(authorization: Authorization, *, page_size: int) -> ApiResponse:
    """The caller's own completed verifications, newest first (GET /verifications).

    The API scopes the list to the credential, and a row exists only once a run
    has completed. A GET: no idempotency key, no credit.
    """
    size = int(page_size)

    async def invoke(sdk: AsyncLenz, options: dict[str, Any]) -> BaseModel:
        return await sdk.verifications.list(page=1, page_size=size, **options)

    return await _call('list_verifications', '/verifications', authorization, invoke)


async def select(authorization: Authorization, *, task_id: str, texts: list[str]) -> ApiResponse:
    key = _idem_key('select', task_id, *texts)
    chosen = [_utf8_safe(t) for t in texts]

    async def invoke(sdk: AsyncLenz, options: dict[str, Any]) -> BaseModel:
        return await sdk.select(task_id, texts=chosen, idempotency_key=key, **options)

    return await _call('select', '/verify/{task_id}/select', authorization, invoke)


async def ask(authorization: Authorization, *, verification_id: str, message: str, language: str = '') -> ApiResponse:
    # No idempotency key, deliberately, even though the REST endpoint honours
    # one (and the SDK sends a random one unless told not to). `ask` is a
    # conversational append — asking the same question twice is a legit second
    # turn, and it gets a different answer because the first exchange is now
    # history. Every key `_idem_key` derives is content-derived, so sending one
    # here would manufacture client-side exactly the implicit key the endpoint
    # refuses to derive server-side: the second ask would replay the first reply
    # for the full response TTL. A key belongs on a RETRY of one logical ask,
    # which is a thing only a caller that owns the retry can tell apart from a
    # re-ask, and the connector never retries one. Longer timeout — /ask is
    # synchronous and can block on source summaries plus the LLM reply.
    language = effective_language(language)
    question = _utf8_safe(message)

    async def invoke(sdk: AsyncLenz, options: dict[str, Any]) -> BaseModel:
        return await sdk.ask.send(verification_id, message=question, language=language, idempotency=False, **options)

    return await _call('ask', '/ask/{verification_id}', authorization, invoke, timeout=config.ASK_TIMEOUT)


def _canonical_json(value: Any) -> str:
    """The same JSON for the same content, whatever the key order.

    Sorted keys, no spaces, characters kept as written, and any unsendable
    surrogate dropped first, so the text hashed is the text sent.
    """
    return json.dumps(_utf8_safe(value), sort_keys=True, ensure_ascii=False, separators=(',', ':'))


async def citecheck(
    authorization: Authorization,
    *,
    text: str | None = None,
    pairs: list[dict[str, Any]] | None = None,
    max_citations: int | None = None,
) -> ApiResponse:
    """Start a citation check (POST /citecheck): a draft's text, or statement-source pairs.

    Exactly the fields given are sent, and the idempotency key is derived from
    that complete body plus the operation name. So an input mode, a pair
    boundary, a quote, a url against a doi, and an omitted field against an
    explicit one are all different requests with different keys, while the same
    request sent twice joins the first (the API replays a key's first answer for
    a day, a failed check included).
    """
    body: dict[str, Any] = {}
    if text is not None:
        body['text'] = text
    if pairs is not None:
        body['pairs'] = pairs
    if max_citations is not None:
        body['max_citations'] = max_citations
    key = _idem_key('citecheck', _canonical_json(body))
    sent = _utf8_safe(body)

    async def invoke(sdk: AsyncLenz, options: dict[str, Any]) -> BaseModel:
        return await sdk.citecheck(
            sent.get('text'),
            pairs=sent.get('pairs'),
            max_citations=sent.get('max_citations'),
            idempotency_key=key,
            **options,
        )

    return await _call('citecheck', '/citecheck', authorization, invoke)


async def citecheck_status(authorization: Authorization, *, citecheck_id: str) -> ApiResponse:
    """A citation check as it stands (GET /citechecks/{id}). Never cached by the API."""

    async def invoke(sdk: AsyncLenz, options: dict[str, Any]) -> BaseModel:
        return await sdk.get_citecheck(citecheck_id, **options)

    return await _call('citecheck_status', '/citechecks/{id}', authorization, invoke)


async def me_usage(authorization: Authorization) -> ApiResponse:
    async def invoke(sdk: AsyncLenz, options: dict[str, Any]) -> BaseModel:
        return await sdk.usage(**options)

    return await _call('me_usage', '/me/usage', authorization, invoke)
