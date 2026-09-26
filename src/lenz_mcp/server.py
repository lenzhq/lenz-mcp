"""FastMCP server definition — the Lenz tools.

Each tool forwards the caller's ``Authorization`` header to the public API
(``src/lenz_mcp/client.py``), maps every error/shadow path to a clean structured
result (an agent must never receive a raw exception or HTTP error), and — on
results that resolve to an already-public claim — attaches a branded
``/c/<slug>`` link tagged ``utm_medium=mcp``.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import re
import time
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types import ToolAnnotations
from mcp_types import INVALID_PARAMS
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from lenz_mcp import client, config, exchange, links
from lenz_mcp.mcp_card import CARD_ONLY_TOOL_META, register_card_resources, with_delivery
from lenz_mcp.middleware import lenz_middleware
from lenz_mcp.widget_resource import request_is_chatgpt

logger = logging.getLogger(__name__)

# Confidence is returned as a bucket + this caveat, never a bare number:
# verdict-only panel scores are not calibrated probabilities and agents
# over-trust them.
CONFIDENCE_NOTE = (
    'Bucketed confidence (high/medium/low) reflecting panel agreement, not a '
    'calibrated probability. Treat as directional, not a guarantee.'
)

# `language` on assess_claim / verify_claim / ask_followup.
#
# Both the type and the wording exist to stop the client filling this in
# unasked. Typed as a bare `str` and described as an "ISO 639-1 code", it
# advertised ~180 codes against the twelve the API serves and read as an
# invitation to be helpful. A client could pass a code the API does not serve,
# for example `ko`, and get a 422 it could only escape by reading the error and
# retrying, several failed tool calls into the user's first session. A user
# inside the twelve could fare worse in one way: a client inferring the locale
# would get a verdict in a language nobody asked for, at no error.
#
# The enum is built from `config.SUPPORTED_LANGUAGES`, so a thirteenth
# language is a one-line change; `''` is "unset", which the API maps to English. Rejecting
# an unsupported code here costs the model one local self-correction instead
# of an API round trip, and tells a user who genuinely wants Korean that we
# don't serve it rather than hiding it in a 422 they never see.
#
# Deliberate narrowing: the API lowercases the code, so it accepts 'EN'
# where this enum does not. Clients send '' or a lowercase code in
# practice, and the cost of being wrong is one local retry against a listed set, not a
# failed call — so this is not worth a normalizing validator.
# Built from the configured list at import, which a type checker cannot evaluate.
LanguageCode = Literal[('', *config.SUPPORTED_LANGUAGES)]  # type: ignore[valid-type]

LANGUAGE_FIELD_DESCRIPTION = (
    'Leave unset. English is the default. Set this only if the user explicitly asked for the '
    'answer in another language — not to match the language of the claim, the conversation, or '
    f'the user locale. Supported: {", ".join(config.SUPPORTED_LANGUAGES)}.'
)

# Appended only on assess_claim results — measured escalation guidance
# (comparing quick verdicts with deep checks of the same claims: the deep
# check reverses the direction of ~1% of high-confidence quick verdicts, ~8% of
# medium, ~19% of low). The quick check is the default, so this is where the
# deep check gets offered: bounded to the rows that matter, and
# never started without the user's yes.
ASSESS_ESCALATION_NOTE = (
    ' This is a quick check: a first read that shows no sources. A deep check, which investigates a '
    'claim against independent sources, reverses about 19% of low-confidence quick verdicts, about 8% '
    'of medium and about 1% of high. On low confidence recommend one to the user; on medium confidence '
    'or a dissent offer one; on a list, only for the one or two claims that matter. Do not mention '
    "tool names or credits to the user. Never start one without the user's yes. The tool is "
    '`verify_claim`.'
)

# Appended after ASSESS_ESCALATION_NOTE, as its OWN sentence: what a row's
# `rationale` and `dissent` are, in the approved public wording. Kept
# separate so the escalation guidance can be reworded without touching it. It
# never says how a note is picked.
ASSESS_NOTES_NOTE = (
    " `rationale` is the reasoning of a reviewer who agrees with the panel's verdict; `dissent`, when "
    "set, is the reasoning of the reviewer farthest from it. Both are reviewers' notes, not checked "
    'sources. For sourced evidence, offer the user a deep check; if they agree, call `verify_claim`.'
)

# On every low-confidence assess_claim row, beside `recommend_verify: true`. The
# recommendation rides in the RESULT so it does not depend on the model
# remembering a rule from the instructions. Derived from `confidence` alone:
# it must never refer to a reviewer's notes, which many rows do not carry.
#
# Written as what to SAY, with the tool named only in a separate instruction:
# the model paraphrases this to the user, and Claude was seen relaying an
# earlier text as "Lenz recommends a deep check (verify_claim) … costs more
# credits". A person using Lenz inside an assistant sees neither a tool name
# nor credits. The same split governs
# every result string below that the model may repeat.
LOW_CONFIDENCE_NEXT_STEP = (
    'Lenz is not confident in this quick verdict. Tell the user that a deep check would investigate '
    'the claim against independent sources and takes about a minute to a minute and a half, and ask '
    'whether to run it. Do not mention tool names or credits to the user. If they say yes, call '
    '`verify_claim` with this claim.'
)

# How the model is asked to show a completed deep check: without it a sourced
# result reached the user as the bare label.
PRESENTATION_NOTE = (
    'Show the user the verdict with its score and how confident Lenz is, the key finding, any '
    'warnings, how many sources the check drew on (the `sources_total` field), and the top sources '
    'with what each one says. Do not reduce this to the verdict label, and do not show the user '
    'field or tool names.'
)

# On every completed deep check. The server is stateless and cannot know what a
# quick check said earlier in the conversation, so the rule travels with the
# result: the deep check can overrule the quick verdict, and when it does that
# is the product working, not an error to apologise for.
SUPERSEDES_NOTE = (
    'This deep check replaces any earlier quick verdict on the same claim. If the verdict changed, '
    'tell the user plainly that it changed and why, from the key finding and the sources. Do not '
    'apologise, do not average the two, do not present both as valid.'
)

# `sources[].snippet` on the API is usually a short quote, but it can be a
# longer passage, and nothing on the API says which is which. A quote is never
# cut: any sentence splitter meets "U.S.", "No. 5" or a script without spaces,
# and a cut that drops a negation inverts the evidence. So a snippet up to the
# limit passes whole, and a longer one shows no quote.
SOURCE_QUOTE_MAX_CHARS = 600

# A newly connected user often runs check_usage first, because nothing has
# told them what to type yet.
USAGE_NEXT_NOTE = (
    'To run a check the user can say, for example: "Check with Lenz whether <a factual claim> is true", or '
    'paste a draft and say "Check the claims in this draft with Lenz."'
)

# Result messages the model may repeat, in the same say/do split as the notes
# above: what to tell the user first, the tool only in an instruction.
STILL_RUNNING_AFTER_WAIT = (
    'Still running after {seconds} seconds. Tell the user it is still running. Do not mention tool '
    'names to the user. Call `get_verification` again with this task_id. If the conversation moves '
    'on first, call `list_verifications` later to find the finished result.'
)
# What a caller who SIGNED IN is told when their credential is refused. No key
# to create and no link: their sign-in is held by the app they are talking to,
# and reconnecting there is the only thing that fixes it.
OAUTH_REAUTH_MESSAGE = (
    'The Lenz sign-in for this connection is no longer valid. Tell the user to reconnect Lenz in '
    'this app and then ask again. Do not mention tool names to the user.'
)
# A check that was submitted and charged, whose credential then failed. The run
# is untouched by it, so the model is told how to collect the result rather
# than left to start (and pay for) the same check again.
CREDENTIAL_LOST_MID_RUN = (
    'The check itself is running and has already been charged. Once this is resolved, call '
    '`get_verification` with this task_id to collect the result. If the conversation moves on first, '
    'call `list_verifications` later to find it. Do not start the same check again. Do not mention '
    'tool names to the user.'
)
FOLLOWUP_NOT_COMPLETED = (
    "That deep check isn't complete yet. Tell the user it is still running and that you will answer "
    "once it finishes. Call `get_verification` until its status is 'completed', then call "
    '`ask_followup` again.'
)
# Transport failure: the reply (and its charge) MAY have landed server-side, and
# `ask_followup` is not idempotent, so the model is told not to resend blindly.
FOLLOWUP_UNREACHABLE = (
    "Couldn't reach Lenz to send the question, and it may already have been recorded. Do not resend "
    'it blindly: a resend may be charged twice. Call `get_verification` to check the conversation '
    'before resending.'
)

# `list_verifications`: enough to find a check from earlier today, short enough to read.
RECENT_CHECKS_LIMIT = 10
RECENT_CHECKS_MESSAGE = 'Call `get_verification` with a verification_id for the full result and its sources.'
NO_RECENT_CHECKS_MESSAGE = (
    'No completed deep checks yet. A check that is still running is not listed until it finishes, '
    'and quick checks are not stored.'
)

# The billing `code` values the API attaches to a quota/credit rejection on the
# tools the MCP calls (assess_claim / verify_claim / select_claims / ask_followup).
# Used ONLY on the legacy-403 path below — a 402 is unambiguous by status alone.
# Any other 403 is a real access denial (e.g. an IP block), not a billing wall.
#
# `invalid_count` is deliberately absent: it means a malformed `n`, which the
# API returns as 422. Treating it as quota would tell a caller to go buy
# credits for what was a bad request.
#
# The current API emits only `no_credits`. Older API revisions may send
# `insufficient_credits` or `no_chat_credits` on the legacy-403 path, so they
# stay in this set; without them an out-of-quota agent would hear "you're not
# allowed". Retire the whole set together with the 403 branch, never piecemeal.
_QUOTA_CODES = frozenset({'no_credits', 'insufficient_credits', 'no_chat_credits'})

# A stored result's public verification id (8 hex chars). A task_id
# is a 32-hex uuid, so the two never collide by shape.
_VERIFICATION_ID_RE = re.compile(r'^[0-9a-f]{8}$')

# What an id may contain to be interpolated into a URL path. This is an
# allow-list, and — like `client._UA_UNSAFE` — it is not a guess at the id's
# shape but the bound the transport imposes: `f'/verify/status/{task_id}'` with
# `../../admin` RESOLVES to `/api/v1/admin`, a CR raises `httpx.InvalidURL`
# (not an `HTTPError`), and a non-ASCII or surrogate character cannot be
# encoded at all. Deliberately looser than the 32-hex/8-hex forms we actually
# mint: the id routing rule (exactly 8 hex is a stored result, anything else
# is a run to wait on) stays intact, and a future id shape cannot be rejected
# by a rule that was only ever about the URL.
_SENDABLE_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')

# Everything outside printable ASCII. A Lenz key is `lenz_<32 hex>`, so a real
# credential never contains anything else —
# but uvicorn accepts `\x80`-`\xff` inbound and Starlette latin-1-decodes it,
# while httpx encodes header values as ASCII. A key copied from a styled page
# or a chat message, carrying a non-breaking space or a curly quote, therefore
# arrives intact and then cannot go back out.
_AUTH_UNSENDABLE = re.compile(r'[^\x20-\x7e]')


def build_transport_security(allowed_hosts: list[str]) -> TransportSecuritySettings:
    """DNS-rebinding settings from a Host allowlist.

    The mcp SDK auto-enables a localhost-only allowlist when FastMCP.host is
    127.0.0.1 — which would 421 every request behind a proxy (Host: the public domain).
    We override explicitly: an empty allowlist disables protection (functional
    behind a trusted proxy and over any host/IP in local dev); a non-empty one
    enables it restricted to those hosts (+ matching http/https origins).
    """
    if not allowed_hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    origins = [f'https://{h}' for h in allowed_hosts] + [f'http://{h}' for h in allowed_hosts]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(allowed_hosts),
        allowed_origins=origins,
    )


def _oauth_kwargs() -> dict[str, Any]:
    """Transport-auth kwargs for FastMCP — empty while OAuth is dark, so the
    server stays byte-identical to v1 (no verifier, no 401 wall, no PRM).

    When enabled: the dual-mode verifier accepts both WorkOS JWTs and
    ``lenz_`` API keys (the main regression path); a keyless request gets the
    SDK's ``401 + WWW-Authenticate`` — the signal that starts OAuth
    discovery. No global ``required_scopes``: consent is one grant for the
    whole connector.
    """
    if not config.OAUTH_ENABLED:
        return {}
    from mcp.server.auth.settings import AuthSettings

    from lenz_mcp.oauth import DualModeTokenVerifier

    return {
        'token_verifier': DualModeTokenVerifier(),
        'auth': AuthSettings(
            issuer_url=config.OAUTH_ISSUER,
            resource_server_url=config.MCP_PUBLIC_URL,
            required_scopes=None,
            # MUST stay False. With it True the SDK rejects any token whose
            # `AccessToken.resource` is not the MCP's own URL, and our API-key branch
            # returns `resource=None` (oauth.py) — every API-key user would get a 401.
            # The JWT branch enforces the audience itself. 2.x warns when it is unset
            # and flips the default in 3.0, so it is stated rather than inherited.
            validate_token_resource=False,
        ),
    }


def _server_version() -> str:
    """What `serverInfo.version` reports (`config.APP_VERSION`)."""
    return config.APP_VERSION


def _serve_no_subscriptions(server: MCPServer) -> None:
    """Refuse `subscriptions/listen`, and stop advertising what it implies.

    2026-07-28 replaced the server-initiated GET stream with a `subscriptions/listen`
    POST whose response IS the stream. A stateless server cannot deliver it:
    nothing is ever routed to that stream, and it dies at the platform's request
    timeout holding one of the instance's concurrency slots — the exact bug
    ServerStreamGuard fixed on GET, back on POST.

    Removing the handler is what makes `server/discover` HONEST about it: at 2026
    versions the SDK derives `tools.listChanged`, `prompts.listChanged`,
    `resources.listChanged` and `resources.subscribe` from whether this method is
    served (lowlevel/server.py::get_capabilities), so with it gone all four read
    false and the method answers 404 `-32601` in under 10 ms. Refusing it in
    middleware instead would leave discover advertising four things we cannot do,
    which is the one rule this migration is built around.

    It is a private attribute, and deliberately loud if the SDK moves it: a silent
    skip here would re-advertise subscriptions on the next minor. Configurable
    capabilities are tracked upstream (python-sdk#2896); this goes when they land.
    """
    handlers = getattr(getattr(server, '_lowlevel_server', None), '_request_handlers', None)
    if handlers is None or 'subscriptions/listen' not in handlers:
        raise RuntimeError(
            'mcp SDK changed where request handlers live: lenz-mcp cannot remove '
            "'subscriptions/listen', and server/discover would advertise streams it "
            'cannot serve. See src/lenz_mcp/server.py::_serve_no_subscriptions.'
        )
    del handlers['subscriptions/listen']


mcp = MCPServer(
    name='Lenz',
    # The job first, then triggers, then mechanics. Specific and confident,
    # never "always use me": over-triggering costs real money per call, and
    # connector directories expect a server to say when NOT to use it.
    #
    # The quick check is the default because it answers in 15-20 seconds and
    # costs one credit; a deep check takes a minute or more and costs ten (the
    # per-client wait is in config, VERIFY_WAIT_SECONDS_BY_USER_AGENT). The deep check is offered by confidence, and the
    # result rows carry the same recommendation so it does not rest on the
    # model remembering this text. No sentence about links: a deep check is
    # private, so there is no public page to surface.
    instructions=(
        'Lenz checks the factual claims in a draft, a pasted text or your previous answer against '
        'independent sources. '
        'Use it when the user names Lenz; asks you to check, double-check, verify, confirm or fact-check '
        'something; asks you to audit a draft or your previous answer for factual errors; asks whether '
        'something is true or accurate; or doubts a factual statement you made (“are you sure?”, '
        '“is that right?”). Only when the statement is a factual claim: a fact, a figure, a '
        'date, a quote or an attribution. Not for opinions, predictions, arithmetic, how code behaves '
        'or text with no checkable claim. '
        'Start with `assess_claim`, the quick check, for one claim or a whole text: a verdict and a '
        'confidence for each claim in about 15-20 seconds. Present a quick verdict as a first read, not '
        "as final. When a row carries a `rationale`, show it with the verdict as the reviewers' reasoning, "
        'never as sourced evidence; when it carries a `dissent`, say that a reviewer disagreed and why. '
        'Then act on its confidence: on low, or when the claim is high-stakes for the user '
        '(legal, medical, financial, or about to be published), recommend a deep check; on medium, '
        'or when a row carries a dissent, offer one; on high, mention that one is available without '
        'pushing it. On a long text, name at most the two claims that matter; never start '
        'deep checks across every row. '
        '`verify_claim` is the deep check: a 1-10 score, the key finding, warnings and the '
        'main sources, in about 60-90 seconds, for more credits. '
        "Never start one without the user's yes, unless they asked for sources, a deep check or a "
        'verification: then run it directly. Say a deep check is running; if the call returns '
        'first, `get_verification` waits for it. A deep check '
        'replaces any earlier quick verdict on the same claim: if it changed, say so plainly and '
        'why, without apologising or averaging the two. If a deep check fails '
        'as `upstream_unavailable`, say source verification is unavailable. '
        '`list_verifications` finds an earlier deep check, or one whose result never arrived. '
        '`ask_followup` answers a follow-up on a completed deep check. '
        '`check_usage` shows credits left; never a prerequisite. '
        'Verdicts are directional, not absolute: always show the confidence.'
    ),
    website_url=config.FRONTEND_URL,
    # 2.x defaults `version` to '', and an
    # empty serverInfo.version is what every directory listing and client would
    # then show. APP_VERSION is the build's, set in the environment at deploy.
    version=_server_version(),
    # Per-client tailoring and the request identity. In 1.x these were handler
    # swaps in `FastMCP._mcp_server.request_handlers`, a dict 2.x removes.
    middleware=[lenz_middleware],
    # `stateless_http` and `transport_security` moved to the app builder in 2.x
    # (see src/lenz_mcp/asgi.py); passing them here is a TypeError.
    **_oauth_kwargs(),
)

_serve_no_subscriptions(mcp)


@mcp.custom_route('/healthz', methods=['GET'], include_in_schema=False)
@mcp.custom_route('/mcp/healthz', methods=['GET'], include_in_schema=False)
async def healthz(_request: Request) -> JSONResponse:
    """Liveness probe (no auth, no I/O). `/mcp/healthz` is reachable wherever
    `/mcp/*` is routed to this service; bare `/healthz` may not be, so it only
    works reliably as a direct-container probe. This is a liveness check only; MCP protocol health
    is covered at deploy time by the smoke test's POST /mcp."""
    return JSONResponse({'status': 'ok'})


# The Lenz verdict card (MCP Apps): every published card URI, served
# on read, never listed. Gated per request by mcp_card.card_active().
# The per-client tailoring of tools/list and resources/list, and the logging of
# resource reads, are in src/lenz_mcp/middleware.py (one middleware, three methods).
register_card_resources(mcp)


# ── auth + error helpers ─────────────────────────────────────────────


# The tool a call is running, and that call's exchanged credential, bound by
# `requires_auth` for the call's duration. Only the exchange path reads them.
_CALL_TOOL: contextvars.ContextVar[str] = contextvars.ContextVar('lenz_mcp_call_tool', default='')
_CALL_CREDENTIAL: contextvars.ContextVar[exchange.CallCredential | None] = contextvars.ContextVar(
    'lenz_mcp_call_credential', default=None
)


def _authorization(ctx: Context) -> client.Authorization:
    """The credential each tool forwards to the public API.

    API-key callers: the inbound ``Authorization`` header, verbatim (v1
    behavior — the API validates the key). OAuth callers: the user's token was
    verified at the transport, but it is audience-bound to this server and the
    API cannot accept it, so the call gets its own
    ``exchange.CallCredential``. The client resolves it to an exchanged API
    token on the first request, scoped to the running tool, and every later
    request of the same call (each poll of a deep-check wait) reuses it.
    """
    if config.OAUTH_ENABLED:
        from mcp.server.auth.middleware.auth_context import get_access_token

        from lenz_mcp import oauth

        access_token = get_access_token()
        if access_token is not None and (access_token.claims or {}).get('auth_mode') == oauth.AUTH_MODE_OAUTH:
            credential = _CALL_CREDENTIAL.get()
            if credential is None or not credential.is_for(access_token.token):
                credential = exchange.CallCredential(access_token.token, exchange.scopes_for(_CALL_TOOL.get()))
                _CALL_CREDENTIAL.set(credential)
            return credential

    try:
        request = ctx.request_context.request
    except (ValueError, AttributeError):
        return None
    if request is None:
        return None
    return request.headers.get('authorization')


def _caller_signed_in() -> bool:
    """Whether this request authenticated with a Lenz sign-in rather than a key.

    Read from the same place the credential is (``_authorization``), so the two
    can never disagree about which door the caller came through.
    """
    if not config.OAUTH_ENABLED:
        return False
    from mcp.server.auth.middleware.auth_context import get_access_token

    from lenz_mcp import oauth

    access_token = get_access_token()
    return access_token is not None and (access_token.claims or {}).get('auth_mode') == oauth.AUTH_MODE_OAUTH


def _exchange_failure_result(exc: exchange.ExchangeFailed | exchange.ExchangeNotConfigured) -> dict[str, Any]:
    """The tool result for an OAuth call whose API token could not be obtained."""
    if isinstance(exc, exchange.ExchangeNotConfigured):
        logger.error('mcp_exchange_not_configured')
        return _error_result(client.ApiResponse(status=503, data={'retry_after': exchange.DEFAULT_RETRY_AFTER_S}))
    if exc.kind == 'reauth':
        # The user's sign-in is no longer accepted: the same answer as the API's
        # own 401, so a host re-authenticates exactly as it does today.
        return _error_result(client.ApiResponse(status=401, data={}))
    if exc.kind == 'scope':
        return {
            'status': 'error',
            'message': 'This Lenz connection is not allowed to do that. Retrying will not help.',
        }
    if exc.kind == 'approval':
        uri = exc.approval_uri if _is_own_url(exc.approval_uri) else config.API_CREDENTIALS_URL
        return {
            'status': 'approval_required',
            'message': (
                f'This app needs your approval before it can use your Lenz account. Approve it at {uri}, '
                'then ask again.'
            ),
            'approval_url': uri,
        }
    if exc.kind == 'blocked':
        return {
            'status': 'forbidden',
            'message': 'This app is blocked from using your Lenz account. Retrying will not help.',
        }
    return _error_result(
        client.ApiResponse(status=503, data={'retry_after': exc.retry_after or exchange.DEFAULT_RETRY_AFTER_S})
    )


def _credential_lost_mid_run(
    exc: exchange.ExchangeFailed | exchange.ExchangeNotConfigured, task_id: str
) -> dict[str, Any]:
    """The same mapping, for a check that is already running and already paid for.

    The credential failed between the submission and the verdict. The run is
    unaffected — it finishes server-side — so the result keeps its ``task_id``
    and says how to collect it, rather than reading as a check that never
    started.
    """
    result = _exchange_failure_result(exc)
    result['task_id'] = task_id
    result['message'] = f'{result["message"]} {CREDENTIAL_LOST_MID_RUN}'
    return result


def _is_own_url(value: str | None) -> bool:
    """Whether ``value`` is a page of the Lenz site, the only place an approval
    link may point to."""
    return isinstance(value, str) and value.startswith(f'{config.FRONTEND_URL}/') and not _AUTH_UNSENDABLE.search(value)


def _auth_required() -> dict[str, Any]:
    """What a caller is told when their credential is missing or refused.

    The advice has to match the door they came through. Someone who signed in
    has no API key to create, so key-creation advice is a dead end for them:
    what fixes it is signing in again in the app they are using.
    """
    if _caller_signed_in():
        return {'status': 'auth_required', 'message': OAUTH_REAUTH_MESSAGE}
    return {
        'status': 'auth_required',
        'message': (
            'No Lenz API key was provided. Create a free key at '
            f'{config.API_CREDENTIALS_URL} and set it as the Authorization bearer '
            'token in your MCP client configuration.'
        ),
    }


def _auth_unsendable() -> dict[str, Any]:
    """A credential that arrived fine and cannot be sent on.

    Same ``auth_required`` status, because it is the same problem for anything
    branching on it — but "Couldn't reach the Lenz API", which is what the
    caller saw before, is the wrong diagnosis and points nowhere. Nothing about
    the value is echoed: it is a credential.
    """
    logger.warning('mcp_auth_unsendable_header')  # invisible until now: it logged nothing
    return {
        'status': 'auth_required',
        'message': (
            'The Authorization header contains characters a Lenz API key never has — a curly '
            'quote or a non-breaking space picked up when it was copied. Copy the key again from '
            f'{config.API_CREDENTIALS_URL} and set it as the Authorization bearer token in your '
            'MCP client configuration.'
        ),
    }


def _sendable_id(value: str) -> bool:
    """Whether an id can be interpolated into a URL path. See ``_SENDABLE_ID_RE``."""
    return bool(_SENDABLE_ID_RE.match(value))


def _unsendable_id(kind: str, source: str) -> dict[str, Any]:
    return {
        'status': 'invalid_request',
        'message': (
            f"That isn't a Lenz {kind} — it contains characters no id has. Pass the {kind} "
            f'exactly as {source} returned it.'
        ),
    }


def requires_auth(fn):
    """Enforce the API-key gate on a tool so a new tool can't forget it.

    Short-circuits with ``_auth_required()`` when no Authorization header is
    present — or ``_auth_unsendable()`` when one is present that httpx cannot
    encode; otherwise runs the tool (which reads the header itself to forward
    it). ``functools.wraps`` preserves the signature so FastMCP still derives
    the right tool schema.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        # FastMCP injects ctx by name; tests pass it positionally. Detect by the
        # request_context attribute so both (and a duck-typed test ctx) work.
        ctx = kwargs.get('ctx') or next((a for a in args if hasattr(a, 'request_context')), None)
        tool_token = _CALL_TOOL.set(fn.__name__)
        credential_token = _CALL_CREDENTIAL.set(None)
        try:
            authorization = _authorization(ctx)
            if not authorization:
                return _auth_required()
            if isinstance(authorization, str) and _AUTH_UNSENDABLE.search(authorization):
                return _auth_unsendable()
            try:
                return await fn(*args, **kwargs)
            except (exchange.ExchangeFailed, exchange.ExchangeNotConfigured) as exc:
                # Only the exchange path raises these, from inside the client,
                # whenever the call's API token could not be obtained.
                return _exchange_failure_result(exc)
        finally:
            _CALL_CREDENTIAL.reset(credential_token)
            _CALL_TOOL.reset(tool_token)

    return wrapper


def _humanize_seconds(seconds: int) -> str:
    """Render a retry delay an agent can relay to a person verbatim."""
    if seconds < 90:
        return f'{seconds} seconds'
    minutes = round(seconds / 60)
    if minutes < 90:
        return f'{minutes} minutes'
    return f'{round(seconds / 3600)} hours'


def _quota_message(data: dict[str, Any]) -> str:
    """The out-of-credits sentence an MCP caller sees.

    Built from the 402's structured fields rather than passed through from
    ``detail``. The API's ``detail`` is developer copy and is deliberately
    frozen — the API documents it as a string customers grep and branch
    on — so it can name per-endpoint units ("No remaining /assess units.")
    that do not match the credit model (the payload's own
    ``credits_remaining`` is the real one) and mean nothing to someone who
    asked a chat client to check a fact and has never seen the API.

    ``cost`` and ``credits_remaining`` say the same thing in the vocabulary
    ``check_usage`` already uses, and they are numbers from the response, never
    hardcoded prices. Both are omitted when unresolvable rather than sent as
    null, so absence is the signal to fall back.
    """
    cost = data.get('cost')
    remaining = data.get('credits_remaining')
    # "Out of" only when the balance really is empty. A 402 also fires when a
    # balance is non-zero but too small for THIS call (a 10-credit verify on 3
    # credits), and "Out of Lenz credits: the account has 3." contradicts
    # itself.
    if isinstance(remaining, int) and remaining > 0:
        if isinstance(cost, int):
            return f'Not enough Lenz credits: this call costs {cost:,} and the account has {remaining:,}.'
        return f'Not enough Lenz credits: the account has {remaining:,}.'
    # Empty balance, or no usable numbers in the body. The generic sentence
    # beats echoing `detail`, which is the jargon this function exists to keep
    # out — including on the legacy-403 path, where `detail` is all there is.
    if isinstance(remaining, int) and isinstance(cost, int):
        return f'Out of Lenz credits: this call costs {cost} and the account has 0.'
    return 'Out of Lenz credits for this call.'


def _quota_exhausted(data: dict[str, Any]) -> dict[str, Any]:
    """The out-of-quota tool result, shared by the 402 and legacy-403 paths.

    ``manage_url`` points at the plans page, not the API-credentials page: the caller
    has a working key, so sending them to key creation was a dead end. The
    server's own ``upgrade_url`` wins when present so the destination can move
    without redeploying the MCP.

    OMITTED ENTIRELY FOR CHATGPT. OpenAI's app guidelines forbid commerce in
    digital goods — an app "cannot initiate new subscriptions or display upgrade
    options" — and a pricing URL handed to the model renders as an upgrade CTA
    in the conversation, which is exactly what those guidelines rule out.
    Every other client keeps the link: a developer reading a 402
    over the SDK needs the actionable destination, and no policy covers them.
    """
    result = {
        'status': 'quota_exhausted',
        'message': _quota_message(data),
    }
    if not request_is_chatgpt():
        result['manage_url'] = data.get('upgrade_url') or config.PLANS_URL
    return result


def _error_result(resp: client.ApiResponse) -> dict[str, Any]:
    """Map a non-2xx API response to a clean, agent-safe tool result."""
    status, data = resp.status, resp.data
    detail = data.get('detail') or data.get('error')
    if status == 0:
        return {'status': 'error', 'message': "Couldn't reach the Lenz API — please retry shortly."}
    if status == 401:
        logger.info('mcp_auth_401')  # friction gauge: real attempts blocked by the key wall
        return _auth_required()
    if status == 402:
        # Unambiguous: out of credits, or the tier doesn't cover this
        # capability. No code inspection needed — that's the point of 402.
        return _quota_exhausted(data)
    if status == 503:
        # Our model/search providers were rate-limited or down
        # (`upstream_unavailable`), or the door is closed under load
        # (`capacity`). Nothing about the request was wrong — the same call
        # succeeds after the stated wait, so say that instead of a generic error.
        wait = data.get('retry_after') or 60
        return {
            'status': 'service_unavailable',
            'message': detail or 'Lenz is temporarily unavailable — please retry shortly.',
            'retry_after_seconds': wait,
            'resolve_with': f'Retry the same call after about {wait} seconds. Do not rephrase the claim — '
            'nothing about it was wrong.',
        }
    if status == 403:
        # Legacy path. The API now sends 402 for quota, but the MCP and the API
        # are separate services that deploy independently, so this
        # must keep working against an older API. Delete once the API's 402 has
        # been live long enough that no rollback would reintroduce the 403.
        #
        # Strictly code-based: the old `'credit' in detail.lower()` substring
        # test would mislabel a genuine 403 (e.g. "you may not credit this
        # source") as a billing wall.
        code = data.get('code', '')
        if code in _QUOTA_CODES:
            return _quota_exhausted(data)
        return {'status': 'forbidden', 'message': detail or 'Access denied.'}
    if status == 429:
        # Pass the real wait through when the server states one — "shortly" is
        # a lie in front of the /extract daily cap, which can be hours away.
        retry_after = data.get('reset_in_seconds') or resp.headers.get('retry-after')
        message = 'Rate limited — slow down and retry shortly.'
        try:
            seconds = int(retry_after) if retry_after is not None else 0
        except (TypeError, ValueError):
            seconds = 0
        if seconds > 0:
            message = f'Rate limited — retry in {_humanize_seconds(seconds)}.'
        result: dict[str, Any] = {'status': 'rate_limited', 'message': message}
        if seconds > 0:
            result['retry_after_seconds'] = seconds
        # No upgrade link for ChatGPT — see _quota_exhausted.
        if data.get('upgrade_url') and not request_is_chatgpt():
            result['manage_url'] = data['upgrade_url']
        return result
    if status == 422:
        return {'status': 'invalid_request', 'message': detail or 'The request was invalid.'}
    if status == 409:
        # Two different conditions share this status, and only one clears.
        # `no_selection_pending` means the task has no pending selection —
        # already resolved, or never had one. Retrying that loops forever,
        # which is what an agent does when told "retry shortly".
        if data.get('code') == 'no_selection_pending':
            return {
                'status': 'already_resolved',
                'message': detail or 'This verification has no pending claim selection — it was already resolved.',
            }
        # An identical request (same idempotency key) is already in flight.
        return {'status': 'in_progress', 'message': 'An identical request is already being processed — retry shortly.'}
    return {'status': 'error', 'message': detail or 'The Lenz API returned an unexpected error.'}


# ── tools ────────────────────────────────────────────────────────────


@mcp.tool(
    title='Fast fact-check',
    # readOnlyHint=True: the hint means "modifies nothing
    # in the user's environment", which is true — a fast check reads sources
    # and returns a verdict. It spends a credit, but a client that reads the
    # hint as "safe to call freely" auto-fires only a 1-credit call here; the
    # 10-credit verify_claim keeps readOnlyHint=False on purpose, so the one
    # tool worth an approval click still gets one where a client asks.
    annotations=ToolAnnotations(title='Fast fact-check', readOnlyHint=True, destructiveHint=False, openWorldHint=True),
)
@requires_auth
async def assess_claim(
    claim: Annotated[
        str,
        Field(
            description=(
                'ONE statement to fact-check, in natural language (e.g. "Honey never spoils"). '
                'If the text contains several atomic claims, each is verdicted separately (up to 20, one credit per verdict). '
                'Leave empty when passing `claims`.'
            )
        ),
    ] = '',
    # The SDK injects the context by this annotation; an Optional would hide it.
    ctx: Context = None,  # type: ignore[assignment]
    language: Annotated[
        LanguageCode,
        Field(description=LANGUAGE_FIELD_DESCRIPTION),
    ] = '',
    claims: Annotated[
        list[str] | None,
        Field(
            description=(
                f'A list of up to {config.ASSESS_MAX_CLAIMS} claims the user listed separately — one '
                'verdict per item, in the '
                'same order. Each item is one claim, not a document; a pasted text goes in `claim` '
                'instead. A compound item is judged on its main '
                "claim and the rest is listed in that row's `identified_claims`. Mutually exclusive "
                'with `claim`.'
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """The quick check: a verdict and a bucketed confidence for each factual claim in
    a text or a list (3-model panel, about 15-20 seconds, one credit per claim).
    Use it when the user asks to fact-check or double-check a factual statement
    or a text (“fact-check this”, “double-check that”), asks whether one is true
    or accurate (“is that true?”, “is this accurate?”), or doubts a factual
    statement you made (“are you sure?”). Not for opinions, predictions,
    arithmetic or how code behaves. When the user doubts something you said,
    pass the specific statement being doubted, not the whole conversation.

    For one claim or a whole text (a draft, a pasted text or your previous
    answer), pass ONE text in ``claim`` (every claim found gets a row, up to
    20) or up to 20 claims in ``claims`` (one row per item, same order). A
    pasted text goes in ``claim`` whole and unedited — never split it, reword
    it, resolve its pronouns or add figures. ``claims`` is only for claims the
    user listed separately.
    Verdicts are True / Mostly True / Mixed / Mostly False / False. No sources:
    present each verdict as a first read, not as final. A row may carry
    ``rationale``, a reviewer's reasoning for the verdict, and ``dissent``, the
    reasoning of the reviewer farthest from it: reviewers' notes, not checked
    sources. A low-confidence row carries ``recommend_verify: true`` and a
    ``next_step``: recommend `verify_claim`, the deep check, and ask before
    running it. On medium offer it; on high mention it. A vague claim is
    assessed on its most likely reading (the row's ``claim``). Leave
    ``language`` unset.
    """
    authorization = _authorization(ctx)  # gate enforced by @requires_auth

    items = [c.strip() for c in (claims or []) if isinstance(c, str) and c.strip()]
    text = (claim or '').strip()
    if items and text:
        return {'status': 'error', 'message': 'Pass either `claim` (one text) or `claims` (a list), not both.'}
    if not items and not text:
        return {'status': 'error', 'message': 'Pass a `claim` to check, or a `claims` list.'}

    if items:
        resp = await client.assess(authorization, claims=items, language=language)
    else:
        resp = await client.assess(authorization, text=text, language=language)
    if not resp.ok:
        return _error_result(resp)

    data = resp.data
    raw_claims = data.get('claims') or []
    if not raw_claims:
        return {
            'status': 'no_claim',
            'message': data.get('error') or 'No verifiable factual claim was detected in the text.',
        }

    claims_out = []
    for c in raw_claims:
        entry = {
            'claim': c.get('claim', ''),
            'verdict': c.get('verdict', ''),
            'confidence': c.get('confidence', ''),
        }
        # A list item that produced no verdict stays IN POSITION (the caller
        # matches rows to items by index) and says why, in the API's own
        # vocabulary: no_claim | framing_failed | upstream_unavailable | timeout
        # (open — a new code passes through untouched).
        if c.get('error_code'):
            entry['error'] = c['error_code']
        # Claims found in the same item that were NOT assessed — resubmit
        # them as their own items if they matter.
        if c.get('identified_claims'):
            entry['identified_claims'] = list(c['identified_claims'])
        # One sentence on what to send next; the API sets it on every error
        # row and on a compound row, never on a plain verdict.
        if c.get('hint'):
            entry['hint'] = c['hint']
        # The reviewer notes, forwarded only when set: a null note adds
        # nothing, and a row from an older API or a replayed stored body has
        # no such keys at all (the MCP server and the API deploy independently).
        for note in ('rationale', 'dissent'):
            if isinstance(c.get(note), str) and c[note]:
                entry[note] = c[note]
        # The deep check reverses ~19% of low-confidence quick verdicts, so a
        # low row says so itself. Never on an Error row: it reads `low` too,
        # and there is no verdict to check.
        if c.get('confidence') == 'low' and not c.get('error_code') and c.get('verdict') != 'Error':
            entry['recommend_verify'] = True
            entry['next_step'] = LOW_CONFIDENCE_NEXT_STEP
        # Link only on a public result (the API returns verification_url only
        # for already-public cache-hits) — built from the verification_id, no DB.
        link = links.branded_link(links.verification_id_from_verification_url(c.get('verification_url')))
        if link:
            entry['lenz_url'] = link
        claims_out.append(entry)

    return {
        'status': 'ok',
        'claims': claims_out,
        'confidence_note': CONFIDENCE_NOTE + ASSESS_ESCALATION_NOTE + ASSESS_NOTES_NOTE,
        'source': 'Lenz fast fact-check (3-model panel)',
    }


def _card_active() -> bool:
    """A client that is seeing the live card (mcp_card.card_active).

    Read by the still-running message: a user watching a card does not need the
    model to narrate the progress steps.
    """
    from lenz_mcp import mcp_card

    return mcp_card.card_active()


def _submitted_message(*, already_running: bool) -> str:
    """Guidance for a verify submission that is STILL RUNNING after the in-tool wait.

    Every client is told the same thing: call get_verification with the task_id,
    and it waits. The old ChatGPT variant said "do NOT poll" because its card
    completed on its own and ChatGPT suppresses repeated identical tool calls —
    the verdict reached the user's screen but never the model, so it could not
    summarise, answer follow-ups or chain into ask_followup.
    With the wait server-side, one call is normally enough; only the card-on
    variant still asks the model not to narrate what the card already shows.
    """
    lead = 'This claim is already being checked.' if already_running else 'The check has started and is still running.'
    tail = (
        'Tell the user it is running and usually takes about a minute to a minute and a half. '
        'Do not mention tool names to the user. Then call '
        '`get_verification` with this task_id: it waits and returns the result when the check finishes. '
        'Call it again while it is still processing. If the conversation moves on first, call '
        '`list_verifications` later to find the finished result.'
    )
    if _card_active():
        return f'{lead} {tail} The user also sees a live card, so do not narrate the progress steps.'
    return f'{lead} {tail}'


_sleep = asyncio.sleep  # patch point for tests

# The advisory progress numbers that ride a still-running result.
# `elapsed_seconds` is a measurement of the run so far, never an estimate of what is left.
_PROGRESS_KEYS = ('index', 'total', 'elapsed_seconds')


def _verify_wait_seconds() -> float:
    """How long one tool call may wait for a deep check, for the client making it.

    Keyed on the client's IDENTITY — its User-Agent token plus any
    parenthesised suffix (``client.ClientProfile.identity``) — never the token
    alone. One token can cover clients with different ceilings: OpenAI's
    ChatGPT app and its Responses-API connector both send ``openai-mcp`` and
    cut at 119.8 s and 59.8 s respectively. A client whose tool-call timeout
    was never measured gets the short default, and an unrecognised suffix does
    NOT inherit its token's row. Table, measurements, and why this one decision
    still keys on a hand-maintained table of client strings where the card's
    two do not: config.VERIFY_WAIT_SECONDS_BY_IDENTITY.
    """
    return config.verify_wait_seconds(client.client_profile().identity)


async def _await_verification(
    ctx: Context, task_id: str, *, started_at: float | None = None, tool: str = ''
) -> dict[str, Any]:
    """Poll a run server-side until it leaves ``processing`` or the wait budget ends.

    The bounded long-poll that lets one tool call carry the answer when it can:
    a cache hit or a needs_input interrupt resolves in seconds.
    A real deep check takes longer: a client with a long wait may get it in
    the same call, while a client on the short default often needs a
    `get_verification` call on top (the per-client waits are in config). Budget and interval live in config. The last
    ``processing`` result is returned as-is when the budget runs out.

    The credential is resolved per POLL, not once per call: an OAuth caller's
    service assertion is short-lived,
    and a wait longer than its lifetime would otherwise 401 its own later polls and
    end a running check as ``auth_required``. An API key is forwarded as-is
    every time. On the exchange path the resolved credential is the call's own
    (``exchange.CallCredential``), so the polls share ONE exchanged token —
    the API issues it with enough life for the whole wait.
    """
    # The budget covers the WHOLE tool call, from `started_at` (the tool's own
    # entry) when the caller submitted something first. The client's cut is on
    # the call, not on this loop: timed from here, a slow submit plus a slow
    # last poll took `verify_claim` to ~128 s against the ChatGPT app's
    # measured 119.8 s cut. From entry, the worst case is
    # the wait plus one final poll.
    wait = _verify_wait_seconds()
    deadline = (started_at if started_at is not None else time.monotonic()) + wait
    # The first poll always runs, even when a submission alone spent the
    # budget: it is what carries a cache hit or the claim picker back in the
    # same call, and a submit to our own API outlasting a 45-130 s budget is
    # not a case worth losing that for.
    while True:
        try:
            out = await _verification_result(_authorization(ctx), task_id)
        except (exchange.ExchangeFailed, exchange.ExchangeNotConfigured) as exc:
            # The run is already submitted and already charged, so this failure
            # must not swallow its id: it is answered here, with the task_id,
            # instead of unwinding to the bare tool result `requires_auth`
            # builds for a failure before anything was started.
            return _credential_lost_mid_run(exc, task_id)
        if out.get('status') != 'processing':
            return out
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # The one place all three waiting tools converge, so the signal is
            # emitted here rather than at each caller's still-running message:
            # `verify_claim` is the call whose wait matters most, and it is not
            # one of the callers that writes such a message.
            _note_wait_exhausted(wait, tool)
            return out
        await _sleep(min(config.VERIFY_POLL_INTERVAL, remaining))


def _note_wait_exhausted(wait: float, tool: str) -> None:
    """Say that a deep check outlasted this client's wait. Never raises.

    Ordinary at the ceiling — the caller gets a task_id and `get_verification`
    collects the result. It is evidence when it is CONSTANT for one identity,
    which is what a missing or undersized wait row looks like from outside.
    """
    try:
        from lenz_mcp import decisions

        decisions.log_wait_exhausted(wait=wait, tool=tool)
    except Exception:  # noqa: BLE001 — a measurement never fails a tool call
        logger.exception('the exhausted deep-check wait could not be logged')


def _verify_outcome(
    out: dict[str, Any], task_id: str, *, already_running: bool = False, depth: str = '', claim: str = ''
) -> dict[str, Any]:
    """Shape the awaited result of a submission for the model.

    Still processing → ``status: submitted`` + task_id (the pre-wait contract,
    so nothing that branched on it breaks) with the current step, the depth
    requested when known, and the progress numbers. Anything else (completed /
    needs_input / failed / error) is returned as get_verification would, with
    the ``task_id`` added — needs_input MUST carry it, select_claims takes it.
    """
    # A failed STATUS poll (`error`: transport blip, 5xx) is not a failed run —
    # the submission was accepted and charged, so report it as such rather than
    # telling the model the check failed.
    if out.get('status') in ('processing', 'error'):
        submitted = {
            'status': 'submitted',
            'task_id': task_id,
            'step': out.get('step', 'Working...'),
            'message': _submitted_message(already_running=already_running),
        }
        # WHAT is being checked. The status API does not echo the claim, and a
        # running card that cannot name it shows a check with no subject.
        if claim:
            submitted['claim'] = claim
        # The depth REQUESTED. Absent on the select_claims path, which inherits
        # the parent's depth server-side, and the status API does not echo it.
        if depth:
            submitted['depth'] = depth
        # Present only on the processing path (an `error` result carries no
        # progress), so "step 2 of 5, 41 seconds in" is available where it is known.
        for key in _PROGRESS_KEYS:
            if key in out:
                submitted[key] = out[key]
        return submitted
    rest = {k: v for k, v in out.items() if k != 'status'}
    return {'status': out.get('status'), 'task_id': task_id, **rest}


@mcp.tool(
    title='Deep fact-check',
    annotations=ToolAnnotations(title='Deep fact-check', readOnlyHint=False, destructiveHint=False, openWorldHint=True),
)
@requires_auth
async def verify_claim(
    claim: Annotated[
        str,
        Field(
            description=(
                'ONE claim to check in depth against sources. Run it once the user agreed to a deep '
                'check, or when they asked for sources, a deep check or a verification.'
            )
        ),
    ],
    ctx: Context,
    language: Annotated[
        LanguageCode,
        Field(description=LANGUAGE_FIELD_DESCRIPTION),
    ] = '',
    depth: Annotated[
        Literal['standard', 'low'],
        Field(
            description=(
                'How much research to do. "standard" (default) is the full pass. "low" is a '
                'shallower one — fewer sources, the same models — for half the credits; it saves '
                'only about a fifth of the time, so both take about a minute or more. Pick "low" '
                'when the user wants to spend fewer credits. The '
                'charge follows the depth you request; the `depth` on the completed result is '
                'the depth the verdict was actually produced at (a "low" request may be served '
                'from an existing deeper check).'
            )
        ),
    ] = 'standard',
) -> dict[str, Any]:
    """The deep check: a verdict with a 1–10 score, the key finding, warnings and the
    main sources for ONE claim (about a minute to a minute and a half).

    Not the default. Run only when the user asked for sources, a deep check or
    a verification, or agreed after a quick check; a plain "check this" is
    `assess_claim`. Never start one unasked. It costs 10 credits against 1 for `assess_claim`
    (``depth="low"`` costs 5; `check_usage` has the live price list). Tell the
    user the deep check is running. Waits for the check and returns
    ``status: completed`` when it finishes in time — a claim Lenz has
    checked before comes back at once. Otherwise returns ``status: submitted``
    with a ``task_id``: call `get_verification` with it, which waits again. A
    completed result replaces any earlier quick verdict on the same claim; its
    ``supersedes`` and ``presentation`` say how to tell the user. If the text
    contains several claims the result is ``status: needs_input`` with a
    numbered list: show it to the user and call `select_claims` with the exact
    text of the chosen claim(s).
    """
    started_at = time.monotonic()  # the wait budget covers the submission too
    authorization = _authorization(ctx)  # gate enforced by @requires_auth

    resp = await client.verify(authorization, text=claim, language=language, depth=depth)

    # 409: an identical verify (same idempotency key) is already running — the
    # API returns its in-flight task_id. Wait on the existing run instead of
    # spawning (and paying for) a duplicate.
    already_running = resp.status == 409 and bool(resp.data.get('task_id'))
    if not already_running and not resp.ok:
        return _error_result(resp)

    task_id = resp.data.get('task_id') or resp.data.get('id')
    if not task_id:
        return {'status': 'error', 'message': 'The Lenz API did not return a task id.'}

    # The answer rides in THIS result whenever the run finishes inside the
    # client's wait: in Claude most runs, elsewhere a cache hit,
    # needs_input or a short run (config, VERIFY_WAIT_SECONDS_BY_USER_AGENT).
    out = await _await_verification(ctx, task_id, started_at=started_at, tool='verify_claim')
    return _verify_outcome(out, task_id, already_running=already_running, depth=depth, claim=claim)


@mcp.tool(
    title='Resolve a multi-claim interrupt',
    annotations=ToolAnnotations(
        title='Resolve a multi-claim interrupt', readOnlyHint=False, destructiveHint=False, openWorldHint=True
    ),
)
@requires_auth
async def select_claims(
    task_id: Annotated[
        str,
        Field(
            description='The task_id of the needs_input verification being resolved (from the get_verification response).'
        ),
    ],
    claims: Annotated[
        list[str],
        Field(
            description=(
                'One or more of the exact claim texts offered by get_verification (its '
                '`claims`). Each selected claim starts its own deep verification.'
            )
        ),
    ],
    ctx: Context,
) -> dict[str, Any]:
    """Resolve a `needs_input` verification by choosing which claim(s) to run.

    When `verify_claim` or `get_verification` returns ``status: needs_input``
    with reason ``multi_claim``, call this with
    that ``task_id`` and a ``claims`` list of one or more of the offered claim
    texts, exactly as listed. Each selected claim starts its own deep
    verification. With ONE claim selected this waits for the check and
    returns its result like `get_verification` (with the new ``task_id``);
    with several it returns one ``task_id`` per claim to pass to
    `get_verification`.
    """
    started_at = time.monotonic()  # the wait budget covers the selection too
    authorization = _authorization(ctx)  # gate enforced by @requires_auth
    task_id = (task_id or '').strip()
    if not _sendable_id(task_id):
        return _unsendable_id('task_id', 'verify_claim or get_verification')

    resp = await client.select(authorization, task_id=task_id, texts=claims)
    if not resp.ok:
        return _error_result(resp)

    items = resp.data.get('items') or []
    started = [{'task_id': it.get('task_id'), 'claim': it.get('claim_text', '')} for it in items]
    partial = bool(resp.data.get('partial'))

    # One selection is the common case (the user picked the claim that matters):
    # wait for it here so the verdict lands in this call, and the model is on
    # the NEW task_id — the parent stays needs_input forever.
    if len(started) == 1 and started[0]['task_id'] and not partial:
        new_task_id = started[0]['task_id']
        awaited = await _await_verification(ctx, new_task_id, started_at=started_at, tool='select_claims')
        out = _verify_outcome(awaited, new_task_id)
        out['claim'] = out.get('claim') or started[0]['claim']
        out['batch_id'] = resp.data.get('batch_id')
        return out

    out = {
        'status': 'partial' if partial else 'submitted',
        'batch_id': resp.data.get('batch_id'),
        'claims': started,
        'message': (
            f'Started {len(started)} verification(s). Call get_verification with each task_id — '
            'it waits for the check and returns the verdict when it finishes.'
        ),
    }
    if partial:
        out['message'] += ' Some selections were not started — retry the missing ones.'
    return out


def _needs_input_message(data: dict[str, Any]) -> str:
    """The human-readable half of a multi_claim result: numbered options + what to do."""
    texts = [c.get('text', '') if isinstance(c, dict) else str(c) for c in data.get('claims') or []]
    lead = (
        'The text contains several claims and each verification checks one. Show the user '
        'this list and ask which to check (or choose the one that matters to them), then call '
        'select_claims with this task_id and the exact text of the chosen claim(s):'
    )
    numbered = '\n'.join(f'{i}. {t}' for i, t in enumerate(texts, 1) if t)
    return f'{lead}\n{numbered}' if numbered else lead


async def _verification_result(
    authorization: client.Authorization, task_id: str, *, name_not_found: bool = False
) -> dict[str, Any]:
    """Fetch + map a deep verification status to an agent/widget-safe result.

    Shared by `get_verification` (model-visible) and `get_verification_widget`
    (widget-only) — one poll + one status→result mapping, two callers.
    """
    resp = await client.verify_status(authorization, task_id=task_id)
    if name_not_found and resp.status == 404:
        # The card tells "this check is gone" (unrecoverable) apart from a blip.
        return {'status': 'not_found', 'message': 'That check could not be found.'}
    if not resp.ok:
        return _error_result(resp)

    data = resp.data
    status = data.get('status')

    if status == 'processing':
        # Subset deliberately: `progress` is advisory and the model must not
        # read a verdict out of it. `index`/`total`/`elapsed_seconds` ride along
        # so the agent can say "step 2 of 5, 41 seconds in" rather than "working".
        progress = data.get('progress') or {}
        out: dict[str, Any] = {'status': 'processing', 'step': progress.get('step') or 'Working...'}
        for key in _PROGRESS_KEYS:
            if isinstance(progress.get(key), int):
                out[key] = progress[key]
        return out

    if status == 'needs_input':
        reason = data.get('reason', '')
        out = {'status': 'needs_input', 'task_id': task_id, 'reason': reason}
        if data.get('claims'):
            out['claims'] = data['claims']
        if reason == 'multi_claim':
            # The options ride in the TEXT, numbered, with the instruction to
            # put them to the user. When the picker card was the only path
            # here, models never reached select_claims at all.
            out['message'] = _needs_input_message(data)
            out['resolve_with'] = (
                'Call `select_claims` with this task_id and a `claims` list of one or more of the '
                'offered claim texts, exactly as listed.'
            )
        return out

    if status == 'failed':
        out = {'status': 'failed', 'message': data.get('error') or 'The verification failed.'}
        # Pass the REST contract's two axes through: where it stopped and why
        # (+ the derived retry signal), so an agent can branch on `retryable`
        # instead of re-submitting blindly. Absent on older/odd bodies.
        for key in ('failure_reason', 'failure_class', 'retryable'):
            if key in data:
                out[key] = data[key]
        return out

    if status == 'completed':
        return _completed_result(data.get('result') or {})

    return {'status': 'error', 'message': 'Unexpected verification status.'}


@mcp.tool(
    title='Get deep fact-check result',
    annotations=ToolAnnotations(
        title='Get deep fact-check result', readOnlyHint=True, destructiveHint=False, openWorldHint=False
    ),
    # Model-visible + untemplated: the model (and headless clients) poll this to get
    # the deep result — the MCP contract. Untemplated so repeated polling never
    # spawns a widget. The ChatGPT card polls get_verification_widget instead, so the
    # model and the card use separate tools and never collide.
)
@requires_auth
async def get_verification(
    task_id: Annotated[
        str,
        Field(
            description=(
                'The task_id returned by verify_claim or select_claims (waits for the running check), '
                'or the 8-character verification_id of a completed result (fetches it).'
            )
        ),
    ],
    ctx: Context,
) -> dict[str, Any]:
    """Get a deep `verify_claim` result: wait for a running check by ``task_id``,
    or fetch a completed one by ``verification_id``.

    With a ``task_id`` (from `verify_claim` or `select_claims`) this waits for
    the run to finish and returns ``status: completed`` with the
    verdict, the 1–10 Lenz score, confidence, key finding, executive summary,
    top sources, the ``depth`` the verdict was produced at and the
    ``verification_id`` — pass that to `ask_followup` for a grounded follow-up.
    If the check needs a decision (several claims, or a near-duplicate) it
    returns ``status: needs_input`` with the options. Still ``processing``
    after the wait means tell the user it is still running and call again;
    `list_verifications` finds it later if the conversation moves on. A
    completed result replaces any earlier quick verdict on the same claim; its
    ``supersedes`` and ``presentation`` say how to tell the user. With an 8-character
    ``verification_id`` it returns the stored result immediately. (No Lenz
    link: verify_claim results are private to the caller, so a claim-page link
    would be share-gated rather than publicly viewable.)
    """
    started_at = time.monotonic()  # the wait budget covers the whole call
    authorization = _authorization(ctx)
    ident = (task_id or '').strip()
    if not _sendable_id(ident):
        return _unsendable_id('task_id', 'verify_claim or select_claims')
    # Two ids, told apart by shape: an 8-hex verification_id names a stored
    # result, anything else is a run to wait on. The SDK's
    # `verifications.get(verification_id)` and this tool's name collide — a
    # caller passing a task_id to GET /verifications/{id} gets a 404 on a
    # paid-for run.
    if _VERIFICATION_ID_RE.match(ident):
        resp = await client.verification_detail(authorization, verification_id=ident)
        if resp.status == 404:
            return {
                'status': 'not_found',
                'message': (
                    f'No completed verification with id {ident} is readable with this key. Pass the '
                    'task_id from verify_claim to wait for a running check, or the verification_id '
                    'of a completed one.'
                ),
            }
        if not resp.ok:
            return _error_result(resp)
        return _completed_result(resp.data)
    out = await _await_verification(ctx, ident, started_at=started_at, tool='get_verification')
    if out.get('status') == 'processing':
        out['task_id'] = ident
        out['message'] = STILL_RUNNING_AFTER_WAIT.format(seconds=int(_verify_wait_seconds()))
    return out


@mcp.tool(
    title='Get deep fact-check result (widget)',
    annotations=ToolAnnotations(
        title='Get deep fact-check result (widget)', readOnlyHint=True, destructiveHint=False, openWorldHint=False
    ),
    # Widget-only twin of get_verification: hidden from the model (ui.visibility
    # ['app']) + widgetAccessible. The Lenz verdict card polls THIS over callTool, so
    # the model never polls it (no widget-per-poll spam) and structurally cannot
    # touch this path. Claude ignores it and polls get_verification.
    meta=CARD_ONLY_TOOL_META,
)
@requires_auth
async def get_verification_widget(
    task_id: Annotated[str, Field(description='task_id from verify_claim; polled by the Lenz widget in-card.')],
    ctx: Context,
) -> dict[str, Any]:
    """Widget-only twin of `get_verification` (hidden from the model)."""
    # Also takes the 8-character verification_id of a completed check: a
    # reopened Lenz card recovers a finished check by the id it stored. Kept out
    # of the docstring, which FastMCP publishes: this tool's description has been
    # live since the ChatGPT widget listed it, and it must not move.
    #
    # The body is separate so EVERY return path carries the delivery hint,
    # including the error ones: the card reads it from the most recent card-tool
    # response, and a card whose first call fails must still know how to deliver.
    return with_delivery(await _get_verification_for_card(task_id, ctx))


async def _get_verification_for_card(task_id: str, ctx: Context) -> dict[str, Any]:
    task_id = (task_id or '').strip()
    if not _sendable_id(task_id):
        return _unsendable_id('task_id', 'verify_claim')
    if _VERIFICATION_ID_RE.match(task_id):
        resp = await client.verification_detail(_authorization(ctx), verification_id=task_id)
        if resp.status == 404:
            return {'status': 'not_found', 'message': 'That check could not be found.'}
        if not resp.ok:
            return _error_result(resp)
        return _completed_result(resp.data)
    return await _verification_result(_authorization(ctx), task_id, name_not_found=True)


@mcp.tool(
    title='Start a deep fact-check (Lenz card)',
    annotations=ToolAnnotations(
        title='Start a deep fact-check (Lenz card)', readOnlyHint=False, destructiveHint=False, openWorldHint=True
    ),
    # Card-only (src/lenz_mcp/mcp_card.py): listed to a card client only with the
    # card on, marked app-only, stripped from every other manifest.
    meta=CARD_ONLY_TOOL_META,
)
@requires_auth
async def start_verification_widget(
    claim: Annotated[str, Field(description='The claim the Lenz card shows, exactly as it shows it.')],
    ctx: Context,
    retry_of: Annotated[
        str,
        Field(description='The task_id of a failed check the card is trying again. Empty for a first run.'),
    ] = '',
) -> dict[str, Any]:
    """Called by the Lenz card, never by the assistant: start a deep check and
    return its task_id at once.

    `verify_claim` waits for the check before it answers, which is right for
    the assistant and wrong for a card that has to show the check running. This
    submits and returns; the card then polls `get_verification_widget`. A repeat
    of the same claim joins the same check at no new charge (the API
    replays the key's first response for a day). The depth is the server's
    (config.CARD_VERIFY_DEPTH), never the card's. ``retry_of`` names a failed,
    retryable run of this user's to try again: it gets its own key, since the
    old key would replay the failure.
    """
    return with_delivery(await _start_verification_for_card(claim, ctx, retry_of))


async def _start_verification_for_card(claim: str, ctx: Context, retry_of: str) -> dict[str, Any]:
    authorization = _authorization(ctx)  # gate enforced by @requires_auth
    if not config.CARD_ENABLED:
        # Unlisted while the card is off; refused if called anyway, so a paid
        # check only ever starts from a card that is switched on.
        return {'status': 'invalid_request', 'message': 'The Lenz card is not available.'}
    text = (claim or '').strip()
    if not text:
        return {'status': 'invalid_request', 'message': 'The card sent no claim to check.'}
    retry_of = (retry_of or '').strip()
    if retry_of:
        if not _sendable_id(retry_of) or _VERIFICATION_ID_RE.match(retry_of):
            return {'status': 'invalid_request', 'message': 'That is not a check that can be tried again.'}
        # Ownership is the status API's: another user's task_id reads as not found.
        prior = await client.verify_status(authorization, task_id=retry_of)
        if not (prior.ok and prior.data.get('status') == 'failed' and prior.data.get('retryable') is True):
            return {'status': 'invalid_request', 'message': 'That is not a check that can be tried again.'}

    kwargs: dict[str, Any] = {'text': text, 'language': '', 'depth': config.CARD_VERIFY_DEPTH}
    if retry_of:
        kwargs['retry_of'] = retry_of
    resp = await client.verify(authorization, **kwargs)
    # 409: the same submission is in flight right now; its task_id is the answer.
    already_running = resp.status == 409 and bool(resp.data.get('task_id'))
    if resp.status == 402:
        # The card says "out of credits" or "not enough credits" and never a
        # number or a link, so it gets only which of the two this is.
        remaining = resp.data.get('credits_remaining')
        return {'status': 'quota_exhausted', 'balance_empty': not (isinstance(remaining, int) and remaining > 0)}
    if not already_running and not resp.ok:
        return _error_result(resp)
    task_id = resp.data.get('task_id') or resp.data.get('id')
    if not task_id:
        return {'status': 'error', 'message': 'The Lenz API did not return a task id.'}
    return {'status': 'submitted', 'task_id': task_id}


@mcp.tool(
    title='Start the checks chosen in the Lenz card',
    annotations=ToolAnnotations(
        title='Start the checks chosen in the Lenz card', readOnlyHint=False, destructiveHint=False, openWorldHint=True
    ),
    # Card-only (src/lenz_mcp/mcp_card.py): listed to a card client only with the
    # card on, marked app-only, stripped from every other manifest.
    meta=CARD_ONLY_TOOL_META,
)
@requires_auth
async def select_claims_widget(
    task_id: Annotated[str, Field(description='The task_id of the needs_input check the card is resolving.')],
    claims: Annotated[
        list[str],
        Field(description='The claim texts the user ticked in the card, exactly as the card offered them.'),
    ],
    ctx: Context,
) -> dict[str, Any]:
    """Called by the Lenz card, never by the assistant: start a PAID deep check
    for each claim the user ticked, and return their task_ids at once.

    `select_claims` waits for the verdict when one claim is chosen, which is
    right for the assistant and wrong for a card that has to show the checks
    running — and longer than some hosts allow a tool call to take. This submits
    and returns; the card then polls `get_verification_widget` per task_id.

    Retry-safe by construction: the same parent and the same texts derive the
    same idempotency key, so a lost answer, a re-mount or a double tap replays
    the first response and starts nothing new. A parent whose selection is
    already resolved answers `already_resolved`, which the card reads as "this
    one has moved on" rather than retrying forever.
    """
    return with_delivery(await _select_claims_for_card(task_id, claims, ctx))


async def _select_claims_for_card(task_id: str, claims: list[str], ctx: Context) -> dict[str, Any]:
    authorization = _authorization(ctx)  # gate enforced by @requires_auth
    if not config.CARD_ENABLED:
        # Unlisted while the card is off; refused if called anyway, so paid
        # checks only ever start from a card that is switched on.
        return {'status': 'invalid_request', 'message': 'The Lenz card is not available.'}
    task_id = (task_id or '').strip()
    if not _sendable_id(task_id):
        return _unsendable_id('task_id', 'verify_claim or get_verification')
    texts = _text_list(claims)
    if not texts:
        return {'status': 'invalid_request', 'message': 'The card sent no claims to check.'}
    # The cap is enforced at the paid boundary. The card disables its remaining
    # checkboxes, but that is a courtesy to the reader, not a guard: this call
    # can arrive with any list, and the API downstream allows twenty.
    if len(texts) > config.CARD_PICKER_MAX_CLAIMS:
        return {
            'status': 'invalid_request',
            'message': f'The card may start at most {config.CARD_PICKER_MAX_CLAIMS} checks at a time.',
        }

    resp = await client.select(authorization, task_id=task_id, texts=texts)
    if not resp.ok:
        return _error_result(resp)

    started = [
        {'task_id': item.get('task_id'), 'claim': item.get('claim_text', '')}
        for item in (resp.data.get('items') or [])
        if item.get('task_id')
    ]
    if not started:
        return {'status': 'error', 'message': 'The Lenz API started no checks.'}
    # `partial` means some picks did not start. The card shows the rows it got
    # and never silently drops the rest, so it is told.
    return {
        'status': 'partial' if resp.data.get('partial') else 'submitted',
        'claims': started,
        'batch_id': resp.data.get('batch_id'),
    }


def _text_list(value: Any) -> list[str]:
    """A list of non-empty strings, whatever arrived (an older payload, a null, a bare string)."""
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _source_quote(snippet: Any) -> str:
    """What the source says, whole, or nothing. See SOURCE_QUOTE_MAX_CHARS."""
    text = snippet.strip() if isinstance(snippet, str) else ''
    return text if len(text) <= SOURCE_QUOTE_MAX_CHARS else ''


def _source_row(source: dict[str, Any]) -> dict[str, str]:
    """One top source as the model should show it. Empty values are left out."""
    row = {'title': source.get('title') or '', 'url': source.get('url') or ''}
    optional = {
        'publisher': source.get('source_name'),
        'date': source.get('date'),
        'quote': _source_quote(source.get('snippet')),
    }
    row.update({key: value.strip() for key, value in optional.items() if isinstance(value, str) and value.strip()})
    return row


def _completed_result(result: dict[str, Any]) -> dict[str, Any]:
    """Trim a full verification detail to an agent-friendly result.

    No branded link: verify/API claims are private by default and the payload
    carries no public signal, so a link would point at a share-gated page.
    Discovery rides on assess cache-hit links + the directory listing.
    """
    # Filter url-less entries FIRST, then take the top 5 — slicing first would
    # drop good sources sitting past url-less ones (a fully-sourced result could
    # come back empty). `sources_total` is the full count the deep check drew on,
    # so the card/model can say "5 of 24" rather than implying only 5 exist.
    all_sources = [_source_row(s) for s in (result.get('sources') or []) if isinstance(s, dict) and s.get('url')]
    return {
        'status': 'completed',
        # The caller's own claim id — pass to `ask_followup` for a grounded follow-up.
        'verification_id': result.get('verification_id', ''),
        'claim': result.get('claim', ''),
        'verdict': result.get('verdict', ''),
        # The 1-10 score agents were asking chat for; absent (None) only on pre-score payloads.
        'lenz_score': result.get('lenz_score'),
        'confidence': result.get('confidence', ''),
        'confidence_note': CONFIDENCE_NOTE,
        # The depth the verdict was PRODUCED at, which can differ from the one
        # requested: a `low` request served from an existing deeper check reads
        # `standard`. Describes the evidence, not the charge.
        'depth': result.get('depth', 'standard'),
        'key_finding': result.get('key_finding', ''),
        'executive_summary': result.get('executive_summary', ''),
        # What the check flagged about its own verdict (a contested figure, a
        # dated source, a wording problem). Always a list of text.
        'warnings': _text_list(result.get('warnings')),
        'sources': all_sources[:5],
        'sources_total': len(all_sources),
        'presentation': PRESENTATION_NOTE,
        'supersedes': SUPERSEDES_NOTE,
        'source': 'Lenz deep fact-check',
    }


@mcp.tool(
    title='Check usage & credits',
    annotations=ToolAnnotations(
        title='Check usage & credits', readOnlyHint=True, destructiveHint=False, openWorldHint=False
    ),
)
@requires_auth
async def check_usage(ctx: Context) -> dict[str, Any]:
    """Return the account's remaining Lenz credits, and what they still buy.

    One pool funds every call. ``costs`` is the live price list — read the
    weight from there rather than assuming one. ``verify_claim`` is the
    expensive tool by an order of magnitude; ``assess_claim`` and
    ``ask_followup`` are the cheap ones.

    ``costs`` is keyed by capability at its default price. ``cost_options``
    holds the prices that depend on a request parameter, nested capability →
    parameter → value — today ``verify_claim`` at ``depth="low"``, under
    ``cost_options.verify.depth.low``. Read the low-depth price from there.

    ``assess_remaining`` and ``verify_remaining`` are two views of the SAME
    balance, not separate allowances: spending on one reduces both. Call this
    when the user asks about credits, or before a large batch to size it
    against ``credits_remaining``. It is not a prerequisite: never call it
    before an ordinary check. ``next`` is one example of what the user can ask.
    """
    authorization = _authorization(ctx)  # gate enforced by @requires_auth

    resp = await client.me_usage(authorization)
    if not resp.ok:
        return _error_result(resp)

    data = resp.data

    def _remaining(block: Any) -> int | None:
        return block.get('remaining') if isinstance(block, dict) else None

    raw_credits = data.get('credits')
    credits: dict[str, Any] = raw_credits if isinstance(raw_credits, dict) else {}

    return {
        'status': 'ok',
        'plan': data.get('plan', ''),
        # The balance, and the price list to convert it with.
        'credits_remaining': credits.get('remaining'),
        'costs': data.get('costs') or {},
        'cost_options': data.get('cost_options') or {},
        # Kept: the same balance projected into each capability's own unit.
        'assess_remaining': _remaining(data.get('assess')),
        'verify_remaining': _remaining(data.get('verify')),
        'resets_at': data.get('quota_resets_at') or data.get('resets_at'),
        'next': USAGE_NEXT_NOTE,
    }


@mcp.tool(
    title='Your recent checks',
    # A free GET of results that already exist: it cannot start or charge a check.
    annotations=ToolAnnotations(
        title='Your recent checks', readOnlyHint=True, destructiveHint=False, openWorldHint=False
    ),
)
@requires_auth
async def list_verifications(ctx: Context) -> dict[str, Any]:
    """List the user's most recent completed deep checks (`verify_claim`), newest first, up to 10.

    Call this when the user asks about an earlier deep check, or when a
    `verify_claim` was started and its result never arrived: a check that
    finished after the wait ended is listed here. A row appears only once a
    check has finished, so a check still running is not listed yet, and quick
    checks (`assess_claim`) are not stored. Each row carries the claim, the
    verdict, the score, the confidence, the key finding and a
    ``verification_id``: pass it to `get_verification` for the full result
    with its sources, or to `ask_followup`. Read-only: it never starts a check
    and costs no credits.
    """
    resp = await client.list_verifications(_authorization(ctx), page_size=RECENT_CHECKS_LIMIT)
    if not resp.ok:
        return _error_result(resp)

    items = resp.data.get('items')
    checks = [
        {
            'verification_id': item['verification_id'],
            'claim': item.get('claim', ''),
            'verdict': item.get('verdict', ''),
            'lenz_score': item.get('lenz_score'),
            'confidence': item.get('confidence', ''),
            'key_finding': item.get('key_finding', ''),
            'checked_at': item.get('created_at', ''),
        }
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict) and item.get('verification_id')
    ]
    total = resp.data.get('total')
    return {
        'status': 'ok',
        'checks': checks,
        # The account's full count, so "10 of 14" is sayable; the page when the body is odd.
        'total': total if isinstance(total, int) and checks else len(checks),
        'message': RECENT_CHECKS_MESSAGE if checks else NO_RECENT_CHECKS_MESSAGE,
    }


@mcp.tool(
    title='Ask a follow-up',
    # readOnlyHint=True, same reasoning as assess_claim: a grounded question
    # about a finished verification changes nothing and costs one credit.
    annotations=ToolAnnotations(title='Ask a follow-up', readOnlyHint=True, destructiveHint=False, openWorldHint=True),
)
@requires_auth
async def ask_followup(
    verification_id: Annotated[
        str,
        Field(
            description='The verification_id of a COMPLETED verify_claim result (returned by get_verification) — NOT a task_id.'
        ),
    ],
    question: Annotated[
        str,
        # max_length is declared, not just documented: the API rejects a longer
        # question with a 422 the model can only learn from by spending a tool
        # call on it. Bounded in the
        # schema, an over-long question costs one local self-correction —
        # the same reasoning as the `language` enum above.
        Field(
            max_length=config.ASK_MESSAGE_MAX_CHARS,
            description=(
                "The follow-up question, answered from the verification's full research, debate, "
                f'and adjudication. Maximum {config.ASK_MESSAGE_MAX_CHARS} characters — ask one focused '
                'question rather than a multi-part one.'
            ),
        ),
    ],
    ctx: Context,
    language: Annotated[
        LanguageCode,
        Field(description=LANGUAGE_FIELD_DESCRIPTION),
    ] = '',
) -> dict[str, Any]:
    """Ask a grounded follow-up question about a completed `verify_claim` result.

    The `verify_claim`/`get_verification` result is trimmed; this reads the FULL
    evidence (research, debate, per-panelist adjudication, all sources) the
    trimmed payload omits and answers ``question`` from it. Pass the
    ``verification_id`` returned by `get_verification` (NOT a task_id). Only
    works on a completed `verify_claim` — not on `assess_claim` results. Costs
    credits at the cheap rate, same as `assess_claim`. The conversation is kept
    server-side per verification, so ask
    follow-ups sequentially rather than in parallel. Replies in English unless
    the user explicitly asked for another language — leave ``language`` unset.
    """
    authorization = _authorization(ctx)  # gate enforced by @requires_auth
    verification_id = (verification_id or '').strip()
    if not _sendable_id(verification_id):
        return _unsendable_id('verification_id', 'get_verification')

    resp = await client.ask(authorization, verification_id=verification_id, message=question, language=language)
    if resp.ok:
        return {
            'status': 'ok',
            'answer': resp.data.get('content', ''),
            'source': "Lenz follow-up (grounded in the verification's full research, debate & adjudication)",
        }
    # /ask returns 400 for a not-yet-completed verification; _error_result has no
    # 400 branch, so surface it distinctly instead of a generic error.
    if resp.status == 400:
        return {
            'status': 'not_completed',
            'message': FOLLOWUP_NOT_COMPLETED,
        }
    # Transport failure: the reply (and its charge) MAY have landed server-side.
    # `ask_followup` is not idempotent, so warn against a blind resend.
    if resp.status == 0:
        return {
            'status': 'error',
            'message': FOLLOWUP_UNREACHABLE,
        }
    return _error_result(resp)


# ── prompts ──────────────────────────────────────────────────────────
#
# A visible button for people who do not know what to type.
# claude.ai and Claude Desktop list a remote connector's prompts under
# "+" -> Connectors -> "Add to <connector>", and Claude asks for them on every
# connect (a ListPromptsRequest arrives inside each Claude-User handshake).
# Choosing one puts the returned text
# in the chat as the user's own message, and the assistant takes it from there
# under the server instructions: the quick check first.
#
# A prompt cannot read the conversation, so the text to check is an argument.
# Titles and argument descriptions are read by a PERSON in Claude's menu,
# someone using Lenz inside an assistant: no tool names. The text is data, never
# a template: it is appended, so a `{` or `%s` in it arrives as typed.


#: The prompts whose one argument is the text to check, and the noun the person sees.
#: Read by src/lenz_mcp/middleware.py, which answers an OMITTED argument with the same
#: sentence `_required_text` gives an empty one — the SDK rejects a missing required
#: argument before the prompt function runs.
REQUIRED_PROMPT_ARGUMENTS = {
    'check_text': ('text', 'text'),
    'check_last_answer': ('answer', 'answer'),
}


def _required_text(value: str, what: str) -> str:
    """The pasted text, or a sentence telling the person what to paste.

    MCPError, not ValueError: 2.x answers a prompt's own exception with a generic
    "Error rendering prompt check_text" and logs a full ERROR traceback for it, so
    the sentence a person needs would be replaced by noise in the chat AND a Sentry
    event for every empty submission. An MCPError passes through with its message
    intact and is not an exception as far as the SDK is concerned.
    """
    text = (value or '').strip()
    if not text:
        raise MCPError(INVALID_PARAMS, f'Paste the {what} to check.')
    return text


@mcp.prompt(
    name='check_text',
    title='Check this text with Lenz',
    description=(
        'Check the factual claims in a text against independent sources: a draft, an article or any passage you paste.'
    ),
)
def check_text(
    text: Annotated[str, Field(description='The text to check: paste a draft, an article or any passage.')],
) -> str:
    return 'Check the claims in this text with Lenz:\n\n' + _required_text(text, 'text')


@mcp.prompt(
    name='check_last_answer',
    title='Check your last answer with Lenz',
    description='Check the factual claims in an answer your assistant gave against independent sources.',
)
def check_last_answer(
    answer: Annotated[
        str,
        Field(
            description=(
                'Paste the answer you want checked. Lenz cannot see the conversation, so it needs the text here.'
            )
        ),
    ],
) -> str:
    return 'Check with Lenz whether the factual claims in this answer are true:\n\n' + _required_text(answer, 'answer')
