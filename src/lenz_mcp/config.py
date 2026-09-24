"""MCP service configuration, read from the environment.

The connector is a plain API client: every value here comes
from an environment variable, with a default that is safe for anyone running
it against the public API.

Values are read once, at import. A test that needs another value sets the
environment and reloads this module (``lenz_mcp.testing.assembled_app``)
or patches the attribute.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse


def _env(name: str, default: str = '') -> str:
    """The variable's value, or ``default`` when it is unset OR empty.

    An empty value falls back too: a blank line in an env file sets the
    variable to ``''``, and that must not beat the default.
    """
    return os.environ.get(name) or default


def _env_flag(name: str) -> bool:
    """True only for the exact string ``True``; anything else is False."""
    return os.environ.get(name, 'False') == 'True'


def _env_list(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, '').split(',') if item.strip()]


def _resolve_api_base(raw: str) -> str:
    """Normalize the configured API base, tolerating a bare-origin override.

    The default already includes ``/api/v1``. But MCP_API_BASE_URL may be set
    to a bare origin (no path), and a bare origin would 404 every tool. So if
    the configured value has no path, append the version prefix defensively.
    """
    base = raw.rstrip('/')
    if not urlparse(base).path:  # bare origin, e.g. https://api.example.com
        base = f'{base}/api/v1'
    return base


# Frontend origin for building branded `/c/<slug>` result links.
FRONTEND_URL: str = os.environ.get('FRONTEND_URL', 'http://localhost:8000').rstrip('/')

# Where the MCP forwards fact-check calls. Public `/api/v1` origin by default;
# an override may be a bare origin (the version prefix is added automatically).
API_BASE_URL: str = _resolve_api_base(os.environ.get('MCP_API_BASE_URL', f'{FRONTEND_URL}/api/v1'))

# Stamped on every outbound MCP→API request; the API records the client from it,
# which is what distinguishes MCP traffic from SDK traffic.
USER_AGENT: str = _env('MCP_USER_AGENT', 'lenz-mcp/1.0')

# Host allowlist for DNS-rebinding protection (empty → protection off).
ALLOWED_HOSTS: list[str] = _env_list('MCP_ALLOWED_HOSTS')

# Branded-link UTM tagging (the same UTM vocabulary the rest of Lenz uses).
UTM_SOURCE = 'lenz'
UTM_MEDIUM = 'mcp'
UTM_CAMPAIGN = 'mcp-server'

# Where a keyless / bad-key caller is told to get a free key. Distinct from
# PLANS_URL on purpose: someone with no key needs the key-creation page,
# someone out of quota needs the pricing page.
API_CREDENTIALS_URL: str = f'{FRONTEND_URL}/api-credentials'

# Where an out-of-quota caller is sent. Fallback for the API's own
# ``upgrade_url``, which is authoritative when present — the MCP and the API
# are separate services and deploy independently, so the MCP must
# still produce a sane link against an older API.
PLANS_URL: str = f'{FRONTEND_URL}/plans'

# Outbound HTTP timeouts (seconds). /verify and the status polls return fast and
# keep the tight default; the two synchronous LLM endpoints need real headroom.
#
# These are sized off the slowest healthy responses, not off the typical case.
# A timeout that is too tight turns a slow but healthy request into a false
# "Couldn't reach the Lenz API": the API may still complete and bill the work
# after we stop listening (logged as `mcp_api_transport_error`). Keep them well
# under the MCP server's own request timeout, so a genuinely stuck upstream
# still yields a clean tool error.
DEFAULT_TIMEOUT = 15.0
ASSESS_TIMEOUT = 60.0
# Not derived from ASSESS_TIMEOUT: /ask is a single LLM reply, but it can block
# on source summaries, so it is not reliably the lighter of the two. Sized
# independently.
ASK_TIMEOUT = 60.0

# ── Deep-verify long-poll ─────────────────────────────────────────────
# `verify_claim` (after submitting), `get_verification` and a single-claim
# `select_claims` WAIT for the run inside the tool call, polling /verify/status
# every VERIFY_POLL_INTERVAL seconds for up to the client's wait. The wait is a
# ceiling: the call returns the moment the check leaves `processing`. ChatGPT
# suppresses repeated identical tool calls, so the model cannot poll the way the
# SDK's `wait()` does — the wait has to live server-side.
#
# The wait is per client, because it must stay under the CLIENT's tool-call
# timeout: a call still waiting when the client hangs up is an error to the
# user, where returning early hands back a task_id that `get_verification`
# picks up. The key is the client's IDENTITY, its User-Agent token plus any
# parenthesised suffix (`client.ClientProfile.identity`); a client with no row
# gets VERIFY_WAIT_SECONDS.
#
# This is the one per-client decision still keyed on a hand-maintained table of
# identity strings, and it stays that way deliberately (the card's two
# decisions moved off it in 2026-09). The failures are not symmetric. A wait
# that is too LONG is a hard error in the chat — "HTTP 504" — and for OpenAI's
# API connector a silent retry that costs the developer's user about two
# minutes of dead time first. A wait that is too SHORT is a recoverable
# `status: submitted` and a task_id `get_verification` collects. So an
# identity in no row falls to the SHORT default rather than inheriting its
# vendor's longer row, and a new suffix on a known app costs its users a
# slower answer, never an error. What makes that safe to live with is that it
# is now ANNOUNCED: the manifest decision line carries the wait, and the host
# watch says within the hour that a known vendor is on the default.
#
# Every key must be a `client.KNOWN_IDENTITIES` member
# (`tests/test_first_check.py`), so an identity can never again be known to
# the card and unknown to the wait — which is exactly what happened when the
# ChatGPT app grew its `(ChatGPT)` suffix.
#
# - `Claude-User` (claude.ai, Claude Desktop, the directory connector): 130 s.
#   Claude's cut depends on the PROTOCOL it negotiates, and the dev probe
#   connector (scripts/probe) has measured both eras:
#   - legacy (mcp SDK 1.x), 2026-09-17: calls of 30-210 s returned and 300 s
#     was cut at ~240 s (`client.gave_up after=240.16`);
#   - modern (2026-07-28), which Claude negotiates with a 2.x server, as this
#     one is; measured 2026-09-18: 100 s and 150 s completed, 210 s was cut
#     ("Tool call timed out waiting for server response") while a 210 s call
#     made directly to the same server completed, so the cut is Claude's. The exact ceiling between 150 and 210 s is
#     UNMEASURED. SSE pings do not extend it, and Claude sends no progress token.
#   The wait is sized to what is PROVEN: with the budget timed from the tool's
#   entry, a 130 s wait ends the whole call by ~145 s when the last poll is
#   slow (the client's 15 s timeout is per phase, not a total deadline, so this
#   is the realistic case rather than a hard bound), under the 150 s seen to
#   complete. A longer wait, right for legacy, would turn every deep check
#   past the modern ceiling into a hard timeout instead of `status: submitted`
#   and a task_id that `get_verification` collects.
# - `openai-mcp`, `openai-mcp (ChatGPT)` and `openai-mcp (Codex)` — the ChatGPT
#   APP. It sent the bare token until 2026-09-23, when a new build began
#   sending `(ChatGPT)`; its model-side calls carry `(Codex)`: 100 s.
#   The `(ChatGPT)` row is the SAME app and carries the same number, but its
#   ceiling is UNMEASURED on that build: 119.8 s was measured 2026-09-18 on the
#   previous one. Watch a deep check from it complete before trusting the row;
#   `scripts/probe/` is how a ceiling gets measured.
#   Measured 2026-09-18 with the dev probe connector: ChatGPT drops a tool call
#   at 119.8 s and shows the user "HTTP 504"; 100 s completes; progress
#   notifications do NOT extend it (the same as Claude). The whole tool call —
#   not just this wait — must land under about 110 s, so 100 leaves ~10 s for
#   the last poll and the HTTP hop. On the 45 s default a deep check that runs
#   past 45 s comes back "still running", possibly more than once, and a user who does not
#   ask again never sees a verdict. Deep checks that take longer hand back a
#   task_id, and the card polls
#   `get_verification_widget` for the rest, so nothing is lost at the ceiling.
# - `openai-mcp (Responses API)` — OpenAI's API MCP connector, which is how a
#   developer wires Lenz into their own agent: **45 s, the default, stated
#   explicitly** so nobody widens the app's row onto it. Measured 2026-09-18:
#   the SAME leading token as the app, and a 60 s ceiling — a 130 s call was
#   dropped at 59.76 s, the connector RETRIED the same tools/call once, that
#   attempt was dropped at 59.81 s, and the model was told
#   `504 Timed out waiting for MCP server response`. 50 s completes. Keeping
#   these callers at 45 s is not a change for them at all: it is what they have
#   today and it works, and the retry means an overrun costs them two dropped
#   calls, not one. A deep check that outlasts the budget hands the caller a
#   task_id to poll, which is the documented path.
#   The KEY is the identity, not the token, and an UNRECOGNISED suffix falls to
#   the default rather than to its token's row: a future `openai-mcp
#   (Something)` must never inherit the app's 100 s.
# - Everyone else: 45 s, unmeasured. The TypeScript MCP SDK many clients use
#   defaults to 60 s.
#
# A waiting call holds one of the server's concurrent request slots for its
# duration, and polls about 20 times a minute.
VERIFY_WAIT_SECONDS = 45.0
VERIFY_WAIT_SECONDS_BY_IDENTITY: dict[str, float] = {
    'Claude-User': 130.0,  # modern path: 150 s proven, 210 s cut (2026-09-18), see above
    'openai-mcp': 100.0,  # the ChatGPT app; measured 2026-09-18, cut at 119.8 s
    'openai-mcp (ChatGPT)': 100.0,  # the same app, from the build first seen 2026-09-23
    'openai-mcp (Codex)': 100.0,  # the same app, model-side calls
    # Named explicitly at the default, so it reads as a decision and not as an
    # omission: OpenAI's API connector cuts at 59.8 s and retries once.
    'openai-mcp (Responses API)': 45.0,
}
VERIFY_POLL_INTERVAL = 3.0


def verify_wait_seconds(identity: str) -> float:
    """How long a deep check may wait inside one tool call from `identity`.

    Read by the wait itself (`server._verify_wait_seconds`) and by the manifest
    decision line, so the number an operator reads in the log is the number the
    next deep check will actually get — not a second lookup that could differ.
    """
    return VERIFY_WAIT_SECONDS_BY_IDENTITY.get(identity, VERIFY_WAIT_SECONDS)


# ── Claude verdict card (MCP Apps) kill-switch ───────────────────────
# Off = today's manifest for every client.
CARD_ENABLED: bool = _env_flag('MCP_CARD_ENABLED')
# The depth the card's "Check against sources" button asks for. Standard: the
# person pressing it asked for sources, so they get the full check. The running
# state's line (card copy USUALLY) follows this depth; the button itself
# carries no timing.
CARD_VERIFY_DEPTH = 'standard'
# How many claims one press of the card's picker may start.
# It is enforced HERE, at the paid boundary, not only by the disabled
# checkboxes: the card's own cap is a courtesy, and a tool call is a tool call.
# The card carries the same number in PICKER_MAX_SELECTED (src/lenz_mcp/card/src/app.jsx);
# tests/test_card.py pins the two together.
CARD_PICKER_MAX_CLAIMS = 5

# How many items `assess_claim`'s `claims` list may carry, as the tool tells the
# model. The API owns the number (`/assess`'s item cap); the connector is a
# plain API client, so it mirrors the API's limits as constants.
ASSESS_MAX_CLAIMS = 20

# The API's other limits the tools state to the model, mirrored the same way.
# `ask_followup`'s message limit.
ASK_MESSAGE_MAX_CHARS = 500
# The `language` codes the API accepts, in the API's order, which the tool
# schema lists.
SUPPORTED_LANGUAGES: tuple[str, ...] = ('en', 'es', 'de', 'fr', 'it', 'pt', 'nl', 'sv', 'da', 'no', 'fi', 'bg')
# The path of a verification's result page on the frontend.
CLAIM_PATH_TEMPLATE = '/c/{slug}'

# ── OAuth (WorkOS Connect standalone) — dark behind MCP_OAUTH_ENABLED ─
# The AuthKit domain derives the issuer; a flag without a domain keeps OAuth
# effectively OFF (a verifier that can never verify would 401 everything —
# fail-closed, but be explicit and stay byte-identical to v1 instead).
WORKOS_AUTHKIT_DOMAIN: str = os.environ.get('WORKOS_AUTHKIT_DOMAIN', '')
OAUTH_ENABLED: bool = bool(_env_flag('MCP_OAUTH_ENABLED') and WORKOS_AUTHKIT_DOMAIN)
OAUTH_ISSUER: str = f'https://{WORKOS_AUTHKIT_DOMAIN}' if WORKOS_AUTHKIT_DOMAIN else ''

# The key the OAuth bridge signs its per-call act-as-user JWT with
# (src/lenz_mcp/bridge.py): the FIRST entry of MCP_SERVICE_SIGNING_KEYS, the list
# the API verifies against (a rotation lists the new key first). Shared with
# the API only; empty means the bridge refuses.
SERVICE_SIGNING_KEY: str = next(iter(_env_list('MCP_SERVICE_SIGNING_KEYS')), '')

# ── The token exchange (src/lenz_mcp/exchange.py) — dark behind LENZ_OAUTH_EXCHANGE ─
# On (with OAuth on), an OAuth tool call exchanges the user's verified token at
# the API's token endpoint for a scoped API access token, instead of signing
# the bridge assertion above. Off, nothing below is read.
OAUTH_EXCHANGE: bool = _env_flag('LENZ_OAUTH_EXCHANGE')
# This service's own OAuth client at the API (HTTP Basic on the exchange).
OAUTH_CLIENT_ID: str = _env('LENZ_OAUTH_CLIENT_ID')
OAUTH_CLIENT_SECRET: str = _env('LENZ_OAUTH_CLIENT_SECRET')
# Where the exchange is made. The API's token endpoint sits under its versioned
# base, so the default follows MCP_API_BASE_URL.
TOKEN_ENDPOINT: str = _env('LENZ_TOKEN_ENDPOINT', f'{API_BASE_URL}/oauth/token')
# The RFC 8707 resource the exchanged token is for: the public API's
# identifier, which is its public URL even when MCP_API_BASE_URL points
# somewhere else.
API_RESOURCE: str = f'{FRONTEND_URL}/api/v1'

# The resource indicator tokens are audience-bound to (RFC 8707). Exactly the
# public MCP URL — the verifier rejects any other audience. No trailing slash.
MCP_PUBLIC_URL: str = os.environ.get('MCP_PUBLIC_URL', f'{FRONTEND_URL}/mcp').rstrip('/')

# What `serverInfo.version` reports: the build's APP_VERSION (a release number
# or a release tag such as 2.0.0), else `dev`. Unset or 0 is an unversioned
# build. The SDK no longer falls back to its own version, so an unset one would
# publish an empty string to every client and directory listing.
APP_VERSION: str = os.environ.get('APP_VERSION', '').strip().removeprefix('v') or 'dev'
if APP_VERSION == '0':
    APP_VERSION = 'dev'
