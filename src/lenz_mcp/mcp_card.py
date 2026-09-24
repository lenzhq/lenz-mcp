"""The Lenz verdict card: an MCP Apps resource.

The card is a Preact bundle built in ``src/lenz_mcp/card/`` to a committed
``dist/``, served as ``text/html;profile=mcp-app`` and pointed at by
``assess_claim``'s ``_meta.ui.resourceUri`` in the tools/list a card client
receives. Behind ``MCP_CARD_ENABLED`` (off by default), and for a client that
DECLARES MCP Apps support (``card_decision``).

This module owns the card's identity (URI, mime type, versions), the two
per-client decisions the card needs — who is served one (``card_decision``)
and how that client's card must tell the model (``card_delivery``) — and the
resource registration. Both are pure functions of one
``client.ClientProfile``; the per-client tools/list tailoring lives in
``middleware.py`` (``tailor_tool_list``).

**Neither decision is keyed on the full client identity any more, and that is
the point.** Until 2026-09-23 both were, and both stopped matching at once
when OpenAI shipped a ChatGPT build sending ``openai-mcp/1.0.0 (ChatGPT)``
instead of ``openai-mcp/1.0.0`` — a hand-maintained table of strings the host
controls, with nothing to say when a lookup started missing. Exposure now
follows the client's own declaration, which a card host restates on every
modern request; delivery follows the vendor token, which a suffix does not
change. The deep-check wait still keys on the identity, on purpose
(``config.VERIFY_WAIT_SECONDS_BY_IDENTITY`` says why).

Two rules from measuring Claude (scripts/probe, 2026-09-17):

- **The URI is a published identity, bumped by hand.** Claude caches the card by
  URI, and the tool list (which carries the URI) per connector. A rebuilt
  bundle under the same URI is invisible to users until they re-add the
  connector, and old chats re-mount whatever HTML their URI names. So every
  release gets a new literal ``CARD_URI``, every earlier URI keeps serving its
  own committed bundle (``dist/versions.json``), and a test fails when a bundle
  changes without a new URI.
- **The card is served on read and never listed.** Claude lists resources once
  at connect and offers every listed one under "+ → Add from Lenz", where
  picking it pastes the raw HTML into the chat. The card is read by URI when a
  tool result needs it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

logger = logging.getLogger(__name__)

# Bump BY HAND, with a new dist file and a versions.json entry, whenever the
# bundle changes. Never derived from the bundle's bytes (see the module note).
CARD_URI = 'ui://lenz/card-v5'
CARD_URI_PREFIX = 'ui://lenz/'
CARD_MIME_TYPE = 'text/html;profile=mcp-app'

# The card's directory in this package: sources, fixtures and the served dist/.
CARD_DIR = Path(__file__).resolve().parent / 'card'
DIST_DIR = CARD_DIR / 'dist'
_VERSIONS_PATH = DIST_DIR / 'versions.json'

# The vendors whose card we have MEASURED, by their User-Agent's leading
# token. This is a FALLBACK and nothing else: it decides only for a request
# that carries no MCP Apps declaration at all, or whose declaration could not
# be read. On the 2025 era that is every request — the handshake declares
# capabilities once and this server is stateless, so nothing survives to the
# `tools/list` that follows — and the Claude renderer still opens 2025-era
# sessions, so without this fallback it would lose its own card.
#
# Deliberately the coarse token, not the identity: a new parenthesised suffix
# on a vendor's own client (`openai-mcp/1.0.0 (ChatGPT)`, 2026-09-23) must not
# silently withdraw the card, which is exactly what an identity table did.
# The stated cost: a legacy in-session request from Claude Code, or from
# anything else sending `Claude-User`, keeps today's card-only tools. Both
# self-correct the moment the client speaks the modern era, where the
# declaration decides.
CLAUDE_USER_AGENT_PRODUCT = 'Claude-User'
CARD_VENDOR_TOKENS = frozenset({CLAUDE_USER_AGENT_PRODUCT, 'openai-mcp'})

# Tools only the card calls: listed to Claude with the card on, hidden from the
# model by the MCP Apps visibility hint, stripped from every other manifest.
CARD_ONLY_TOOL_NAMES = frozenset({'start_verification_widget', 'get_verification_widget', 'select_claims_widget'})
CARD_ONLY_TOOL_META = {'ui': {'visibility': ['app']}}

# ── How the card tells the MODEL what it found ───────────────────────
# Two hosts, two mechanisms, and the card cannot tell which host it is in.
#
# Claude delivers `ui/update-model-context` silently: the model learns the
# result and the user sees nothing. ChatGPT ACCEPTS that call, answers success
# and never delivers it (measured 2026-09-17) — and then tells the user "no full
# verification was run" beside a card showing a sourced verdict. Its one working
# channel is `ui/message`, which it SENDS at once as a user turn.
#
# The card cannot infer which it is in: ChatGPT DECLARES updateModelContext, so
# the capability is no signal, and a dropped push still returns success, so
# there is nothing to detect afterwards. The SERVER knows — the card's own tool
# calls carry the client's User-Agent — so it says so, in the result of a call
# the card already makes. The card obeys it and never guesses.
#
# It rides under `_card`, the card's own namespace, so it can never collide with
# a field a model-facing schema grows later; these three tools are app-only, so
# no model sees it either way.
CARD_RESULT_NAMESPACE = '_card'
DELIVER_BY_MESSAGE = 'message'
DELIVER_BY_CONTEXT = 'context'
# Whose card must post a message instead of pushing context: OpenAI's, by
# VENDOR token. Every OpenAI surface that renders our card is a ChatGPT
# surface, and the failure this prevents is the model contradicting a sourced
# verdict the user is looking at — so the coarse answer is the right one here,
# and a proxied or relabelled ChatGPT gets it too. It grants nothing: a client
# that renders no card never reads the hint.
MESSAGE_DELIVERY_VENDOR_TOKENS = frozenset({'openai-mcp'})


def card_delivery(profile=None) -> str:
    """Which mechanism THIS client's card must use to tell the model.

    Keyed on the VENDOR token, not the identity. Both are attacker-controlled
    strings, but the question is "whose host is this" and nothing finer: the
    hint is read only by a card that is already rendering, and getting it wrong
    shows the user a model contradicting a verdict on screen.

    An unknown client gets ``context``: the card then pushes only if the host
    declares it, so a host nobody has measured is never made to post messages
    into the user's own turn that it did not ask for.
    """
    try:
        resolved = _profile(profile)
        if resolved.vendor_token in MESSAGE_DELIVERY_VENDOR_TOKENS:
            return DELIVER_BY_MESSAGE
    except Exception:  # noqa: BLE001 — the quieter mechanism is the safe default
        return DELIVER_BY_CONTEXT
    return DELIVER_BY_CONTEXT


def with_delivery(result: dict) -> dict:
    """Stamp a card-only tool's result with how this client must deliver."""
    if not isinstance(result, dict):
        return result
    deliver = card_delivery()
    result[CARD_RESULT_NAMESPACE] = {'deliver': deliver}
    try:
        from lenz_mcp import decisions

        decisions.log_card_delivery(deliver)
    except Exception:  # noqa: BLE001 — a log line never costs a card its result
        logger.exception('the card delivery decision could not be logged')
    return result


# The tools whose results render as the card: the quick check and a deep check
# the model ran itself — `verify_claim` and `get_verification`
# both answer with a completed result or a submitted task_id, and the card reads
# the shape, never the tool name.
CARD_TOOL_NAMES = frozenset({'assess_claim', 'verify_claim', 'get_verification'})
CARD_TOOL_META = {'ui': {'resourceUri': CARD_URI}}

# The card opens no network connection and loads nothing: its script and styles
# are inline and pinned by the HTML's own CSP. It draws its own hairline frame,
# so the host's border is declined.
#
# ALL FOUR CSP keys are declared, explicitly empty. Per SEP-1865
# (`McpUiResourceCsp`, ext-apps specification 2026-01-26) the object defines
# exactly `connectDomains` (fetch/XHR/WebSocket), `resourceDomains` (images,
# scripts, stylesheets, fonts, media), `frameDomains` (nested iframes) and
# `baseUriDomains` (the document's base URI), and "empty or omitted = no
# external connections (secure default)". Empty is therefore the STRONGEST
# posture and also the truthful one: the card talks only to the host.
#
# Empty and omitted mean the same thing to a conforming host, so naming all four
# buys nothing technically — it is for the reader: ChatGPT's developer mode
# showed a "CSP off" label against a card that declared only two of them
# (2026-09-18), and ChatGPT apps are expected to declare their policy. An explicit four-key declaration cannot be read as an omission.
# The card requests no browser `permissions` (no camera, microphone,
# geolocation or clipboard), so that key is absent rather than empty.
CARD_RESOURCE_META = {
    'ui': {
        'csp': {
            'connectDomains': [],
            'resourceDomains': [],
            'frameDomains': [],
            'baseUriDomains': [],
        },
        'prefersBorder': False,
    }
}

_PLACEHOLDER_HTML = (
    '<!doctype html><meta charset="utf-8">'
    '<p style="font-family:system-ui,sans-serif;padding:16px">The Lenz card is not built.</p>'
)


def card_versions() -> dict[str, dict[str, str]]:
    """Every card URI ever published → its committed bundle file and sha256, oldest first."""
    try:
        return json.loads(_VERSIONS_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        logger.warning('claude card versions.json missing or unreadable at %s', _VERSIONS_PATH)
        return {}


def card_html(uri: str) -> str:
    entry = card_versions().get(uri) or {}
    try:
        return (DIST_DIR / entry['file']).read_text(encoding='utf-8')
    except (KeyError, OSError, UnicodeDecodeError):
        logger.warning('claude card bundle missing for %s — serving placeholder', uri)
        return _PLACEHOLDER_HTML


def is_card_uri(uri: str) -> bool:
    return str(uri).startswith(CARD_URI_PREFIX)


def card_enabled() -> bool:
    from lenz_mcp import config

    return bool(config.CARD_ENABLED)


# Why a request was or was not offered the card. The log line carries it, and
# a reader of the log needs it: `card=off` alone cannot tell a client that told us
# it renders no cards from one whose table row we forgot.
REASON_FLAG_OFF = 'flag_off'
#: The request declared MCP Apps support. The card is on because the client
#: said it can render one.
REASON_DECLARED = 'declared'
#: The request declared its capabilities and MCP Apps was NOT among them. The
#: card is off because the client said so — Claude Code is this case.
REASON_NO_DECLARATION = 'no_declaration'
#: No declaration on this request at all (the 2025 era, every request):
#: decided by the vendor token.
# noqa on both: ruff reads a name ending in _TOKEN as a credential. These
# are log vocabulary — the vendor TOKEN of a User-Agent — and the strings
# are a wire format log tooling matches on, so they are not renamed.
REASON_LEGACY_TOKEN = 'legacy_token'  # noqa: S105
#: The declaration could not be read: decided by the vendor token, as it was
#: before this request's declaration existed. Never silently worse than that.
REASON_READ_FAILED_TOKEN = 'read_failed_token'  # noqa: S105
#: The decision itself raised. Distinct from `flag_off`, which is somebody's
#: choice: this one is a bug, and reading it as the kill switch would hide it.
REASON_ERROR = 'error'


def _profile(profile=None):
    from lenz_mcp import client

    return client.client_profile() if profile is None else profile


def card_decision(profile=None) -> tuple[bool, str]:
    """Whether this client is served the card, and WHY. Never raises.

    Keyed on what the client DECLARES — `mcp.server.apps.client_supports_apps`,
    the SDK's own predicate for the MCP Apps extension — and not on a table of
    User-Agent strings we maintain by hand. A host that renders cards says so
    on every modern request; a table only says what somebody last measured,
    and on 2026-09-23 the ChatGPT app changed its User-Agent and fell out of
    one.

    It fails OPEN, to today's answer: when there is no declaration to read
    (every 2025-era request) or the read raised, the vendor token decides, so a
    client cannot end up with LESS than it had before this rule existed. That
    is the opposite of the deep-check wait, which fails closed — and
    deliberately, because the failures are not alike. A missing card is an
    invisible downgrade; a wait that is too long is "HTTP 504" in the chat.
    """
    try:
        if not card_enabled():
            return False, REASON_FLAG_OFF
        resolved = _profile(profile)
        if resolved.declares_apps is not None:
            return resolved.declares_apps, (REASON_DECLARED if resolved.declares_apps else REASON_NO_DECLARATION)
        from lenz_mcp import client

        reason = (
            REASON_READ_FAILED_TOKEN
            if resolved.declaration_source == client.DECLARATION_READ_FAILED
            else REASON_LEGACY_TOKEN
        )
        return resolved.vendor_token in CARD_VENDOR_TOKENS, reason
    except Exception:  # noqa: BLE001 — a card is a courtesy, never a reason to break discovery
        logger.exception('the card decision failed; serving no card')
        return False, REASON_ERROR


def card_active(profile=None) -> bool:
    """The ONE predicate every card surface gates on. See `card_decision`."""
    return card_decision(profile)[0]


def register_card_resources(mcp: MCPServer) -> None:
    """Serve every published card URI with its own committed bundle."""
    for uri in card_versions() or {CARD_URI: {}}:

        def _make(bound_uri: str):
            def _card() -> str:  # pragma: no cover - thin serve-time wrapper
                return card_html(bound_uri)

            return _card

        mcp.resource(
            uri,
            name='Lenz verdict card',
            description='The Lenz verdict card (MCP Apps).',
            mime_type=CARD_MIME_TYPE,
            meta=CARD_RESOURCE_META,
        )(_make(uri))
